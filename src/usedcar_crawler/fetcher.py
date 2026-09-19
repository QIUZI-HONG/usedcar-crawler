"""取数层：基于 Scrapling 的三档降级抓取。

核心设计（也是"大厂成本纪律"的体现）：
    T1 http    —— ``Fetcher``：SSR 页面与公开 JSON 接口，快且省资源
    T2 stealth —— ``StealthyFetcher``：TLS 指纹校验 / Cloudflare 场景
    T3 dynamic —— ``DynamicFetcher``：SPA、需等待 XHR

每次请求前必须先过两道闸门：
    1. ``RobotsGate``   —— 合规闸门，robots.txt 禁止则直接抛 ``RobotsDeniedError``，不重试；
    2. ``TokenBucket``  —— 限速闸门，确保对目标站点保持礼貌。

所有响应都会落盘快照（gzip + 元数据），保证"数据可溯源、解析可回放"。
"""

from __future__ import annotations

import gzip
import hashlib
import json
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Callable, Protocol
from urllib.parse import urlparse
from urllib.robotparser import RobotFileParser

from tenacity import (
    RetryError,
    Retrying,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential,
)

from .config import FetchCfg, PROJECT_ROOT, get_settings
from .errors import (
    BlockedError,
    ConfigError,
    FetchError,
    FatalError,
    NotFoundError,
    RobotsDeniedError,
)
from .logging_setup import get_logger
from .rate_limit import TokenBucket
from .sources.registry import SourceSpec

log = get_logger("fetcher")

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

# 被拦截的响应特征（状态码命中或响应体命中关键词）
_BLOCK_STATUS = {401, 403, 405, 429, 503}
_BLOCK_MARKERS = (
    "just a moment", "cf-challenge", "cloudflare", "attention required",
    "captcha", "验证码", "访问异常", "访问过于频繁", "请开启javascript",
)
# JS 空壳页特征：说明 T1/T2 拿不到真实数据，需要升级到 dynamic
_JS_SHELL_MARKERS = ('id="app"', "id='app'", 'id="root"', "window.__NUXT__", "__next_data__")


@dataclass
class FetchResult:
    """一次成功取数的结果。"""

    url: str
    status: int
    html: str
    tier: str
    elapsed: float
    snapshot_path: str | None = None
    headers: dict[str, str] = field(default_factory=dict)


class Transport(Protocol):
    """取数后端协议。测试注入假实现即可完全离线运行。"""

    def __call__(self, url: str, *, tier: str, timeout: int, headers: dict[str, str], proxy: str) -> tuple[int, str]: ...


def looks_blocked(status: int, body: str) -> bool:
    """判断是否被反爬拦截。"""
    if status in _BLOCK_STATUS:
        return True
    lowered = body[:4000].lower()
    return any(marker in lowered for marker in _BLOCK_MARKERS)


def ensure_status_ok(status: int, url: str, tier: str) -> None:
    """状态码闸门。

    Scrapling 的 ``Fetcher`` 对 4xx/5xx **不抛异常**，直接返回 Response。
    没有这道闸门，404 页面会被当成正常数据入库——这是真实事故的常见来源。
    """
    if 200 <= status < 300:
        return
    if status in (404, 410):
        raise NotFoundError(f"页面不存在（status={status}）", context={"url": url, "tier": tier})
    if status in _BLOCK_STATUS:
        raise BlockedError(f"疑似被拦截（status={status}）", context={"url": url, "tier": tier})
    raise FetchError(f"响应状态异常（status={status}）", context={"url": url, "tier": tier})


def looks_js_shell(body: str) -> bool:
    """判断是否为"JS 空壳页"（需要浏览器渲染档位）。"""
    if len(body) > 200_000:
        return False
    lowered = body.lower()
    return any(marker in lowered for marker in _JS_SHELL_MARKERS) and len(body) < 20_000


class RobotsGate:
    """robots.txt 合规闸门（按 host 缓存）。

    ``respect_robots=False`` 需要书面合规审批，仅在明确授权的内部环境中使用。
    """

    def __init__(self, *, enabled: bool = True, user_agent: str = USER_AGENT, timeout: int = 10) -> None:
        self.enabled = enabled
        self.user_agent = user_agent
        self.timeout = timeout
        self._cache: dict[str, RobotFileParser | None] = {}

    def _parser_for(self, host_root: str) -> RobotFileParser | None:
        if host_root in self._cache:
            return self._cache[host_root]
        parser: RobotFileParser | None = None
        robots_url = f"{host_root}/robots.txt"
        try:
            request = urllib.request.Request(robots_url, headers={"User-Agent": self.user_agent})
            with urllib.request.urlopen(request, timeout=self.timeout) as response:  # noqa: S310
                content = response.read().decode("utf-8", errors="ignore")
            parser = RobotFileParser()
            parser.parse(content.splitlines())
        except urllib.error.HTTPError as exc:
            # 404 表示未声明规则：按"未禁止"处理（这是通行惯例）
            parser = None if exc.code in (404, 410) else None
        except Exception as exc:  # noqa: BLE001 - 网络异常时不阻断，但要留痕
            log.warning("robots.txt 获取失败，按未声明处理", extra={"url": robots_url, "err": str(exc)})
            parser = None
        self._cache[host_root] = parser
        return parser

    def ensure_allowed(self, url: str) -> None:
        if not self.enabled:
            if not url.startswith(("file://", "local://")):
                log.warning("robots 校验已关闭，请确认具备合规授权", extra={"url": url})
            return
        parsed = urlparse(url)
        if parsed.scheme in ("file", "local", ""):
            return
        host_root = f"{parsed.scheme}://{parsed.netloc}"
        parser = self._parser_for(host_root)
        if parser is not None and not parser.can_fetch(self.user_agent, url):
            raise RobotsDeniedError("目标路径被 robots.txt 禁止", context={"url": url})


class SnapshotStore:
    """原始响应快照：gzip 正文 + JSON 元数据，供回放与取证。"""

    def __init__(self, root: str | Path, enabled: bool = True) -> None:
        self.root = Path(root)
        self.enabled = enabled

    def save(self, *, line: str, source: str, url: str, status: int, tier: str, body: str) -> str | None:
        if not self.enabled:
            return None
        digest = hashlib.md5(f"{url}{body[:2048]}".encode("utf-8")).hexdigest()[:16]
        folder = self.root / line / date.today().isoformat() / source
        folder.mkdir(parents=True, exist_ok=True)
        body_path = folder / f"{digest}.html.gz"
        with gzip.open(body_path, "wt", encoding="utf-8") as handle:
            handle.write(body)
        meta_path = folder / f"{digest}.meta.json"
        meta_path.write_text(
            json.dumps(
                {
                    "url": url,
                    "status": status,
                    "tier": tier,
                    "fetched_at": datetime.now().isoformat(timespec="seconds"),
                    "content_md5": hashlib.md5(body.encode("utf-8")).hexdigest(),
                    "size": len(body),
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        try:
            return str(body_path.relative_to(PROJECT_ROOT))
        except ValueError:
            return str(body_path)


class ScraplingTransport:
    """真实取数后端：按档位调用 Scrapling 的对应 Fetcher。

    Scrapling 是按需导入的——只用解析器（parser-only 安装）时不应强制安装浏览器依赖。
    """

    def __init__(self, cfg: FetchCfg) -> None:
        self.cfg = cfg
        self._http = None
        self._stealth = None
        self._dynamic = None

    # -- 各档位首次使用时惰性导入 --
    def _load(self, tier: str) -> Callable[..., object]:
        try:
            from scrapling.fetchers import DynamicFetcher, Fetcher, StealthyFetcher  # noqa: PLC0415
        except ModuleNotFoundError as exc:  # pragma: no cover - 环境缺依赖
            raise FatalError(
                "未安装 Scrapling 取数依赖，请执行：pip install \"scrapling[fetchers]\" && scrapling install"
            ) from exc
        mapping = {"http": Fetcher, "stealth": StealthyFetcher, "dynamic": DynamicFetcher}
        if tier not in mapping:
            raise ConfigError(f"不支持的取数档位：{tier}")
        return mapping[tier]

    def _call(self, fetcher: Callable[..., object], url: str, *, tier: str, timeout: int,
              headers: dict[str, str], proxy: str) -> object:
        """按档位调用 Scrapling。

        重要（实测差异，容易踩）：``Fetcher`` 提供的是 ``.get()``（httpx 风格），
        而 ``StealthyFetcher`` / ``DynamicFetcher`` 提供的是 ``.fetch()``。两者不可互换。
        """
        kwargs: dict[str, object] = {"timeout": timeout}
        if headers:
            kwargs["headers"] = headers
        if proxy:
            kwargs["proxy"] = proxy
        if tier == "http":
            kwargs["stealthy_headers"] = True
            kwargs["follow_redirects"] = True
            entry = getattr(fetcher, "get", None)
        else:
            kwargs["headless"] = True
            kwargs["network_idle"] = True
            entry = getattr(fetcher, "fetch", None)
        if entry is None:  # pragma: no cover - 版本差异兜底
            raise ConfigError(f"档位 {tier} 对应的 Scrapling 类缺少可用入口方法，请检查 scrapling 版本")
        try:
            return entry(url, **kwargs)
        except TypeError as exc:
            if "proxy" in kwargs and "proxy" in str(exc):
                raise ConfigError(
                    "当前 Scrapling 版本不支持 proxy 参数，请升级 scrapling 或改用环境变量出口代理"
                ) from exc
            raise

    def __call__(self, url: str, *, tier: str, timeout: int, headers: dict[str, str], proxy: str) -> tuple[int, str]:
        fetcher = self._load(tier)
        page = self._call(fetcher, url, tier=tier, timeout=timeout, headers=headers, proxy=proxy)
        status = int(getattr(page, "status", 200) or 200)
        body = getattr(page, "html_content", None) or getattr(page, "body", None) or str(page)
        if isinstance(body, bytes):
            body = body.decode("utf-8", errors="ignore")
        return status, str(body)


class FixtureTransport:
    """离线样本后端：读取本地 HTML，用于自检、CI 与演示（零网络）。"""

    def __init__(self, fixtures_dir: Path) -> None:
        self.fixtures_dir = Path(fixtures_dir)

    def __call__(self, url: str, *, tier: str, timeout: int, headers: dict[str, str], proxy: str) -> tuple[int, str]:
        parsed = urlparse(url)
        raw_path = parsed.path
        if parsed.scheme == "file":
            candidate = Path(raw_path.lstrip("/")) if raw_path.startswith("/") else Path(raw_path)
            if not candidate.is_absolute():
                candidate = PROJECT_ROOT / raw_path.lstrip("/")
        else:
            candidate = self.fixtures_dir / Path(raw_path).name
        if not candidate.exists():
            raise FetchError(f"离线样本不存在：{candidate}", context={"url": url})
        return 200, candidate.read_text(encoding="utf-8")


class FetcherService:
    """对外唯一入口：``fetch(spec, url)``。"""

    def __init__(
        self,
        cfg: FetchCfg | None = None,
        *,
        transport: Transport | None = None,
        fixtures_dir: str | Path | None = None,
    ) -> None:
        settings = get_settings()
        self.cfg = cfg or settings.fetch
        self.robots = RobotsGate(enabled=self.cfg.respect_robots, timeout=min(self.cfg.request_timeout, 10))
        self.snapshot = SnapshotStore(
            PROJECT_ROOT / self.cfg.raw_dir if not Path(self.cfg.raw_dir).is_absolute() else self.cfg.raw_dir,
            enabled=self.cfg.snapshot_raw,
        )
        self._buckets: dict[str, TokenBucket] = {}
        self._transport: Transport = transport or ScraplingTransport(self.cfg)
        self._fixture_transport = FixtureTransport(fixtures_dir or PROJECT_ROOT / "tests" / "fixtures")

    # ---------------- 限速 ----------------
    def _bucket(self, key: str, qps: float | None) -> TokenBucket:
        rate = qps or self.cfg.default_qps
        if key not in self._buckets:
            self._buckets[key] = TokenBucket(rate)
        return self._buckets[key]

    # ---------------- 档位阶梯 ----------------
    def _tier_ladder(self, start: str) -> list[str]:
        if start == "fixture":
            return ["fixture"]
        ladder = list(self.cfg.tier_ladder)
        if start not in ladder:
            ladder.insert(0, start)
        else:
            ladder = ladder[ladder.index(start):]
        return ladder

    def fetch(self, spec: SourceSpec, url: str, *, tier: str | None = None) -> FetchResult:
        """抓取单页，按档位阶梯自动降级。

        :raises RobotsDeniedError: robots.txt 禁止
        :raises FetchError: 所有档位均失败
        """
        self.robots.ensure_allowed(url)
        ladder = self._tier_ladder(tier or spec.tier)
        errors: list[str] = []
        for index, current in enumerate(ladder):
            started = time.monotonic()
            try:
                status, body = self._fetch_with_retry(spec, url, current)
            except RobotsDeniedError:
                raise
            except NotFoundError as exc:
                # 404 不是"取数失败"，升档与重试都无意义，立即失败
                log.error("页面不存在，终止该页抓取", extra={"url": url, "tier": current})
                raise exc
            except FetchError as exc:
                errors.append(f"{current}: {exc}")
                # 末档仍被拦截时直接抛出，保留"被反爬拦死"这一明确信号（比笼统的失败更可诊断）
                if isinstance(exc, BlockedError) and index == len(ladder) - 1:
                    raise
                log.warning("档位取数失败，尝试降级", extra={"tier": current, "url": url, "err": str(exc)})
                continue

            elapsed = round(time.monotonic() - started, 3)
            has_next = index < len(ladder) - 1
            if has_next and (looks_blocked(status, body) or looks_js_shell(body)):
                reason = "被拦截" if looks_blocked(status, body) else "JS 空壳页"
                errors.append(f"{current}: {reason}(status={status})")
                log.warning("响应不可用，升级档位", extra={"tier": current, "status": status, "reason": reason})
                continue

            if looks_blocked(status, body):
                message = f"最终档位仍被拦截，status={status}"
                raise BlockedError(message, context={"url": url, "errors": " | ".join(errors)})

            snapshot_path = self.snapshot.save(
                line=spec.line, source=spec.key, url=url, status=status, tier=current, body=body
            )
            log.info(
                "取数成功",
                extra={"source": spec.key, "tier": current, "status": status,
                       "bytes": len(body), "elapsed": elapsed},
            )
            return FetchResult(url=url, status=status, html=body, tier=current,
                               elapsed=elapsed, snapshot_path=snapshot_path)

        raise FetchError("所有档位均取数失败", context={"url": url, "errors": " | ".join(errors)})

    def fetch_once(self, spec: SourceSpec, url: str, *, tier: str = "http") -> FetchResult:
        """单档位取数，**不做档位升/降级**，供详情页批量采集使用。

        为什么详情页需要另一个入口：``fetch`` 的档位阶梯建立在"升级档位可能拿到数据"
        这一前提上，而详情页源（这里走的是站点显式放行的 Markdown 通道）遇到的是
        **频率限制**——升级到隐身/浏览器档位不但拿不到数据，还会加重风控。
        因此这里把决策权交回调用方：命中风控就抛 ``BlockedError``，
        由上层执行冷却与熔断，而不是盲目加重请求。

        同时仍保留 ``fetch`` 的全部合规与稳定性设施：robots 闸门、令牌桶限速、
        指数退避重试、原始快照落盘。

        :raises RobotsDeniedError: robots.txt 禁止
        :raises BlockedError: 命中反爬（状态码或响应体特征）
        :raises NotFoundError: 页面已下架
        """
        self.robots.ensure_allowed(url)
        started = time.monotonic()
        status, body = self._fetch_with_retry(spec, url, tier)
        elapsed = round(time.monotonic() - started, 3)
        if looks_blocked(status, body):
            raise BlockedError(f"命中反爬拦截（status={status}）", context={"url": url, "tier": tier})
        snapshot_path = self.snapshot.save(
            line=spec.line, source=spec.key, url=url, status=status, tier=tier, body=body
        )
        return FetchResult(url=url, status=status, html=body, tier=tier,
                           elapsed=elapsed, snapshot_path=snapshot_path)

    def _fetch_with_retry(self, spec: SourceSpec, url: str, tier: str) -> tuple[int, str]:
        """单档位请求，带指数退避重试与限速。"""
        bucket = self._bucket(f"{spec.key}:{tier}", spec.effective_qps)
        transport = self._fixture_transport if tier == "fixture" else self._transport
        proxy = self.cfg.proxy

        retryer = Retrying(
            stop=stop_after_attempt(self.cfg.max_retries + 1),
            wait=wait_exponential(multiplier=self.cfg.backoff_base, min=1, max=30),
            retry=retry_if_exception(lambda exc: getattr(exc, "retryable", False)),
            reraise=True,
        )
        try:
            for attempt in retryer:
                with attempt:
                    bucket.acquire(timeout=self.cfg.request_timeout * 2)
                    status, body = transport(url, tier=tier, timeout=self.cfg.request_timeout,
                                             headers=spec.headers, proxy=proxy)
                    ensure_status_ok(status, url, tier)
                    return status, body
        except RetryError as exc:  # pragma: no cover - reraise=True 时不触发
            raise FetchError(f"重试耗尽：{exc}", context={"url": url, "tier": tier}) from exc
        except (RobotsDeniedError, NotFoundError):
            raise
        except FetchError:
            raise
        except Exception as exc:  # noqa: BLE001 - 第三方异常统一转换，避免污染上层
            raise FetchError(f"取数异常：{exc}", context={"url": url, "tier": tier}) from exc
        raise FetchError("取数未返回结果", context={"url": url, "tier": tier})  # pragma: no cover
