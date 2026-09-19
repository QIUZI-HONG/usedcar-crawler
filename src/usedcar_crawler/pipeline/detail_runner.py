"""详情页型数据源（sitemap -> 详情页）的采集编排。

与 ``runner.py`` 的区别，也是本项目两条采集路径的分工：

| | 列表页路径 (``runner.py``) | 详情页路径 (本模块) |
|---|---|---|
| 目标 | 列表页一次拿到几十条 | 站点地图逐条取详情 |
| 字段 | 卡片上的少量字段，品牌车型靠标题猜 | 结构化字段，品牌/车系/价格/车况齐全 |
| 页数 | 页数少、每页信息密度高 | 单条一页，总量大，必须抽样 |
| 失败模式 | 站点改版导致选择器失效 | **频率限制**，以 HTTP 200 返回风控页 |

因此本模块的重心不是"选择器自适应"，而是：

1. **内容契约校验**：HTTP 200 不等于拿到数据，必须校验响应结构（``looks_like_detail_md``）；
2. **风控冷却与熔断**：命中风控就等，等够次数仍被拦就收手——不硬刚、不静默丢数据；
3. **确定性抽样**：等距抽样保证样本在城市/品牌上均匀铺开且可复现；
4. **如实记账**：下架、风控、解析失败分别计数，全部写进 ``crawl_log``。
"""

from __future__ import annotations

import re
import time
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from ..config import PROJECT_ROOT, get_settings
from ..dedupe import dedupe_by_key
from ..errors import (
    BlockedError,
    CrawlerError,
    NotFoundError,
    ParseError,
    RobotsDeniedError,
    StorageError,
)
from ..fetcher import FetcherService
from ..logging_setup import get_logger, new_trace_id
from ..models import CrawlLog
from ..notify import Notifier
from ..parsers.guazi_md import build_raw_record, looks_like_detail_md, parse_detail_md, validate_record_shape
from ..sources.registry import SourceRegistry, SourceSpec, get_registry
from ..sources.sitemap import is_index, parse_sitemap, sample_evenly
from ..storage.repository import Repository, build_engine

log = get_logger("detail_runner")

# 抽样完成后用于"站点改版"判定的关键字段：任何一个大面积缺失都说明页面结构变了
DRIFT_FIELDS = ("title_raw", "price_raw", "reg_date_raw", "mileage_raw")

# 回放时从 raw 正文里找回车源 URL（frontmatter 的 source/canonical 最权威）
_URL_IN_BODY = re.compile(r"^(?:source|canonical|url)\s*:\s*['\"]?(https?://[^\s'\"]+)", re.MULTILINE)


def _url_from_raw(path: Path, body: str) -> str:
    """确定一条 raw 响应对应的车源 URL，供解析器兜底取 ID 用。"""
    match = _URL_IN_BODY.search(body)
    if match:
        return match.group(1)
    return f"https://www.guazi.com/car-detail/{path.stem}.md"


@dataclass
class DetailTaskSpec:
    """详情页采集任务的输入参数。"""

    line: str = "all"
    sources: list[str] = field(default_factory=list)
    maps: int = 0        # 取前几个子地图；0 表示用源配置的 max_pages
    sample: int = 0      # 每个子地图采样多少条；0 表示用源配置的 detail_sample
    task_type: str = "daily_incr"
    resume: bool = True  # 断点续爬：跳过历史上已成功采集过的详情页
    # 覆盖源配置里的冷却参数。配额紧张时用「长冷却 + 放宽次数」把一次任务拖成耐心长跑，
    # 配额一恢复就继续累积；配合断点续爬，中断不会丢进度。
    cooldown_seconds: float | None = None
    max_cooldowns: int | None = None

    def cooldown_for(self, spec: SourceSpec) -> float:
        return self.cooldown_seconds if self.cooldown_seconds is not None else spec.cooldown_seconds

    def max_cooldowns_for(self, spec: SourceSpec) -> int:
        return self.max_cooldowns if self.max_cooldowns is not None else spec.max_cooldowns


@dataclass
class ReplaySpec:
    """raw 层回放任务的输入参数。

    回放与实时采集的差别只有"正文从哪来"这一个变量：一个读本地 raw 文件，
    一个走网络。解析、分流、去重、入库、记账全部复用同一条链路。
    """

    line: str = "all"
    sources: list[str] = field(default_factory=list)
    raw_dir: str | None = None     # 缺省 data/captured/<source>
    pattern: str = "*.md"
    task_type: str = "replay"


class DetailCrawlRunner:
    """详情页型数据源的执行器。"""

    def __init__(
        self,
        *,
        fetcher: FetcherService | None = None,
        repo: Repository | None = None,
        notifier: Notifier | None = None,
        registry: SourceRegistry | None = None,
    ) -> None:
        settings = get_settings()
        self.settings = settings
        self.registry = registry or get_registry()
        self.fetcher = fetcher or FetcherService(settings.fetch)
        self.repo = repo or Repository(build_engine(settings.database), settings=settings)
        self.notifier = notifier or Notifier(settings.notify)
        self.repo.init_schema()

    # ------------------------------------------------------------------ #
    # 任务编排
    # ------------------------------------------------------------------ #
    def resolve_sources(self, task: DetailTaskSpec) -> list[SourceSpec]:
        """挑出本次要跑的详情页型源。"""
        if task.sources:
            specs = [self.registry.get(key) for key in task.sources]
        else:
            specs = [s for s in self.registry.by_line(task.line, include_disabled=True) if s.runnable]
        specs = [s for s in specs if s.is_detail_mode]
        # 列表页型源由 runner.py 负责，这里显式跳过并说明原因，避免"配了却没跑"
        skipped = [s.key for s in self.registry.by_line(task.line, include_disabled=True)
                   if s.runnable and not s.is_detail_mode]
        if skipped:
            log.info("以下源为列表页型，请用 run 命令采集", extra={"sources": ",".join(skipped)})
        if not specs:
            raise CrawlerError(
                f"没有匹配的详情页型采集源：line={task.line} sources={task.sources}"
            )
        return specs

    def run(self, task: DetailTaskSpec) -> list[CrawlLog]:
        new_trace_id()
        specs = self.resolve_sources(task)
        log.info("详情页采集任务开始", extra={"sources": len(specs), "maps": task.maps, "sample": task.sample})
        logs = [self._run_source(spec, task) for spec in specs]
        log.info(
            "详情页采集任务结束",
            extra={"total": len(logs), "success": sum(1 for i in logs if i.status == "success")},
        )
        return logs

    # ------------------------------------------------------------------ #
    # raw 层回放
    # ------------------------------------------------------------------ #
    def replay(self, task: ReplaySpec) -> list[CrawlLog]:
        """把已落盘的原始响应重新解析入库（``crawl`` 的离线孪生）。

        为什么需要它（计划书里"raw 层可重跑"的落地）：

        1. **解析规则演进**：修正字段提取逻辑后，无需重新联网即可重算全量数据；
        2. **采集环境受限**：出口 IP 被目标站点限流时，可用已捕获的原始响应
           完成入库与看板，采集与解析两条链路各自独立可用；
        3. **可取证**：每条记录的 ``raw_ref`` 都指向具体文件，随时可复核。
        """
        new_trace_id()
        specs = self.resolve_sources(DetailTaskSpec(line=task.line, sources=task.sources))
        logs: list[CrawlLog] = []
        for spec in specs:
            logs.extend(self._replay_source(spec, task))
        log.info("raw 层回放结束", extra={"sources": len(specs), "logs": len(logs)})
        return logs

    def _replay_source(self, spec: SourceSpec, task: ReplaySpec) -> list[CrawlLog]:
        new_trace_id()
        started = datetime.now()
        directory = self._replay_dir(spec, task)
        errors: list[str] = []
        counters = Counter()
        records: list[dict[str, Any]] = []

        files = sorted(directory.glob(task.pattern)) if directory.exists() else []
        log.info("回放 raw 层", extra={"source": spec.key, "dir": str(directory), "files": len(files)})
        if not files:
            errors.append(f"raw 目录为空或不存在：{directory}")

        for path in files:
            counters["maps_ok"] += 1
            try:
                body = path.read_text(encoding="utf-8")
                if not looks_like_detail_md(body):
                    # 与在线采集同一道闸门：不合格的响应绝不进入解析统计
                    counters["parse_error"] += 1
                    errors.append(f"{path.name}: 不满足详情页内容契约")
                    continue
                doc = parse_detail_md(body, _url_from_raw(path, body))
                record = build_raw_record(doc, source_platform=spec.key, raw_ref=str(path))
                validate_record_shape(record)
            except (ParseError, OSError) as exc:
                counters["parse_error"] += 1
                errors.append(f"{path.name}: {exc}")
                continue
            records.append(record)
            counters["fetched"] += 1

        buckets: dict[str, list[dict]] = {"ev": [], "fuel": []}
        for record in records:
            buckets[record.get("_line", "fuel")].append(record)

        missing_ratio = self._worst_missing_ratio(records)
        lines = self._target_lines(spec, task)
        logs: list[CrawlLog] = []
        for line in lines:
            records_for_line, duplicates = dedupe_by_key(buckets[line])
            upsert = None
            try:
                upsert = self.repo.upsert_vehicles(line, records_for_line)
            except StorageError as exc:
                errors.append(f"storage[{line}]: {exc}")
                log.error("入库失败", extra={"line": line, "err": str(exc)})

            payload = CrawlLog(
                business_line=line,
                source_platform=spec.key,
                task_type=task.task_type,
                status=self._decide_status(counters, errors, missing_ratio, len(records_for_line)),
                pages_fetched=len(files),
                fetched_count=counters["fetched"],
                parsed_count=len(records_for_line),
                inserted_count=upsert.inserted if upsert else 0,
                updated_count=upsert.updated if upsert else 0,
                dup_count=duplicates,
                error_count=counters["parse_error"] + (upsert.rejected if upsert else 0),
                missing_ratio=missing_ratio,
                error_detail=self._detail(counters, errors, upsert),
                started_at=started,
                finished_at=datetime.now(),
            )
            self.repo.log_crawl(payload.model_dump())
            logs.append(payload)
            log.info(
                "回放入库完成",
                extra={"source": spec.key, "line": line, "status": payload.status,
                       "parsed": payload.parsed_count, "inserted": payload.inserted_count,
                       "updated": payload.updated_count, "dups": duplicates,
                       "rejected": upsert.rejected if upsert else 0},
            )

        if logs:
            return logs
        payload = CrawlLog(
            business_line="both", source_platform=spec.key, task_type=task.task_type,
            status="failed", error_count=1,
            error_detail=self._detail(counters, errors, None),
            started_at=started, finished_at=datetime.now(),
        )
        self.repo.log_crawl(payload.model_dump())
        return [payload]

    @staticmethod
    def _replay_dir(spec: SourceSpec, task: ReplaySpec) -> Path:
        if task.raw_dir:
            return Path(task.raw_dir)
        return PROJECT_ROOT / "data" / "captured" / spec.key

    # ------------------------------------------------------------------ #
    # 单源执行
    # ------------------------------------------------------------------ #
    def _run_source(self, spec: SourceSpec, task: DetailTaskSpec) -> CrawlLog:
        new_trace_id()
        started = datetime.now()
        guard = self._compliance_guard(spec)
        if guard is not None:
            payload = CrawlLog(
                business_line="both", source_platform=spec.key, task_type=task.task_type,
                status="failed", error_count=1, error_detail=guard,
                started_at=started, finished_at=datetime.now(),
            )
            self.repo.log_crawl(payload.model_dump())
            return payload

        maps = task.maps or spec.max_pages
        sample = task.sample or spec.detail_sample
        errors: list[str] = []

        # ---- 阶段 1：从站点地图发现详情页 URL ----
        counters = Counter()
        try:
            detail_urls = self._discover(spec, maps=maps, sample=sample, errors=errors, counters=counters)
        except CrawlerError as exc:
            errors.append(f"discover: {exc}")
            detail_urls = []
        log.info("详情页 URL 发现完成", extra={"source": spec.key, "maps": maps, "urls": len(detail_urls)})

        # ---- 阶段 2：逐条采集详情 ----
        records = self._collect(
            spec, detail_urls, counters, errors,
            resume=task.resume,
            cooldown=task.cooldown_for(spec),
            max_cooldowns=task.max_cooldowns_for(spec),
        )

        # ---- 阶段 3：分流 -> 去重 -> 入库 ----
        buckets: dict[str, list[dict]] = {"ev": [], "fuel": []}
        for record in records:
            buckets[record.get("_line", "fuel")].append(record)

        missing_ratio = self._worst_missing_ratio(records)
        lines = self._target_lines(spec, task)
        logs: list[CrawlLog] = []
        for line in lines:
            records_for_line, duplicates = dedupe_by_key(buckets[line])
            upsert = None
            try:
                upsert = self.repo.upsert_vehicles(line, records_for_line)
            except StorageError as exc:
                errors.append(f"storage[{line}]: {exc}")
                log.error("入库失败", extra={"line": line, "err": str(exc)})

            payload = CrawlLog(
                business_line=line,
                source_platform=spec.key,
                task_type=task.task_type,
                status=self._decide_status(counters, errors, missing_ratio, len(records_for_line)),
                pages_fetched=counters["maps_ok"],
                fetched_count=counters["fetched"],
                parsed_count=len(records_for_line),
                inserted_count=upsert.inserted if upsert else 0,
                updated_count=upsert.updated if upsert else 0,
                dup_count=duplicates,
                error_count=counters["not_found"] + counters["parse_error"] + counters["error"]
                + (upsert.rejected if upsert else 0),
                missing_ratio=missing_ratio,
                error_detail=self._detail(counters, errors, upsert),
                started_at=started,
                finished_at=datetime.now(),
            )
            self.repo.log_crawl(payload.model_dump())
            logs.append(payload)
            log.info(
                "业务线采集完成",
                extra={"source": spec.key, "line": line, "status": payload.status,
                       "parsed": payload.parsed_count, "inserted": payload.inserted_count,
                       "updated": payload.updated_count, "dups": duplicates,
                       "blocked": counters["blocked"], "elapsed": payload.elapsed_seconds},
            )

        if counters["circuit_broken"]:
            self._safe_notify(
                "source_failed",
                title=f"风控熔断：{spec.name}",
                detail=f"冷却 {task.max_cooldowns_for(spec)} 次后仍被拦截，本次任务提前收手，"
                       f"已采集 {counters['fetched']} 条有效数据；建议降低 QPS 或延后重试。",
                metrics={"来源": spec.key, "被拦": counters["blocked"]},
            )
        if errors or counters["not_found"] or counters["parse_error"]:
            self._safe_notify(
                "source_failed",
                title=f"采集存在异常：{spec.name}",
                detail="；".join(errors[:5]) or f"下架 {counters['not_found']} 条",
                metrics={"来源": spec.key, "错误": len(errors)},
            )
        return logs[0] if logs else CrawlLog(
            business_line="both", source_platform=spec.key, task_type=task.task_type,
            status="failed", error_count=1, error_detail="未产出任何业务线记录",
            started_at=started, finished_at=datetime.now(),
        )

    # ------------------------------------------------------------------ #
    # 阶段实现
    # ------------------------------------------------------------------ #
    def _discover(
        self, spec: SourceSpec, *, maps: int, sample: int, errors: list[str], counters: Counter
    ) -> list[str]:
        """发现详情页 URL；支持「一层 urlset」与「索引 + 子地图」两种形态。"""
        urls: list[str] = []
        for map_no in range(1, maps + 1):
            url = spec.build_url(map_no)
            urls.extend(self._expand(spec, url, sample=sample, errors=errors, counters=counters, depth=0))
        # 跨子地图去重：同一个车源可能同时出现在多张地图里
        seen: dict[str, None] = {}
        for url in urls:
            seen.setdefault(url, None)
        return list(seen)

    def _expand(
        self,
        spec: SourceSpec,
        url: str,
        *,
        sample: int,
        errors: list[str],
        counters: Counter,
        depth: int,
    ) -> list[str]:
        """取一张地图；若是索引则下钻一层子地图。"""
        try:
            result = self.fetcher.fetch_once(spec, url, tier="http")
            counters["maps_ok"] += 1
            if not result.html or is_index(result.html):
                if depth >= 1:
                    return []
                children = parse_sitemap(result.html)
                log.info("站点地图为索引结构，下钻子地图", extra={"index": url, "children": len(children)})
                out: list[str] = []
                for child in children:
                    out.extend(self._expand(spec, child, sample=sample, errors=errors,
                                            counters=counters, depth=depth + 1))
                return out
            found = parse_sitemap(result.html)
        except RobotsDeniedError as exc:
            errors.append(f"robots 拒绝 {url}: {exc}")
            log.error("robots.txt 禁止该路径，跳过", extra={"url": url})
            return []
        except CrawlerError as exc:
            errors.append(f"map {url}: {exc}")
            log.warning("站点地图取数失败", extra={"url": url, "err": str(exc)})
            return []
        return sample_evenly(found, sample)

    def _collect(
        self,
        spec: SourceSpec,
        urls: list[str],
        counters: Counter,
        errors: list[str],
        *,
        resume: bool = True,
        cooldown: float | None = None,
        max_cooldowns: int | None = None,
    ) -> list[dict[str, Any]]:
        """逐条采集详情页；风控冷却 + 熔断 + 断点续爬在这里落地。"""
        cooldown = spec.cooldown_seconds if cooldown is None else cooldown
        max_cooldowns = spec.max_cooldowns if max_cooldowns is None else max_cooldowns
        records: list[dict[str, Any]] = []
        done = self._load_checkpoint(spec) if resume else set()
        if done:
            log.info("断点续爬：跳过已成功采集的详情页", extra={"source": spec.key, "already": len(done)})

        pending = [u for u in urls if u not in done]
        counters["skipped"] = len(urls) - len(pending)
        cooldowns_used = 0
        index = 0
        while index < len(pending):
            url = pending[index]
            try:
                result = self._fetch_detail(spec, url)
            except NotFoundError:
                # 404 = 车源已下架。这是正常业务事件，不是故障，单独计数
                counters["not_found"] += 1
                self._append_checkpoint(spec, url)
                index += 1
                continue
            except BlockedError as exc:
                counters["blocked"] += 1
                if cooldowns_used >= max_cooldowns:
                    counters["circuit_broken"] = 1
                    log.error(
                        "风控冷却次数用尽，熔断本次采集",
                        extra={"source": spec.key, "blocked": counters["blocked"], "done": counters["fetched"]},
                    )
                    break
                cooldowns_used += 1
                log.warning(
                    "命中风控，冷却后重试同一 URL",
                    extra={"source": spec.key, "cooldown": cooldown,
                           "round": cooldowns_used, "max": max_cooldowns, "err": str(exc)},
                )
                time.sleep(cooldown)
                continue  # 不推进 index：冷却之后原样重试，不丢样本
            except CrawlerError as exc:
                counters["error"] += 1
                errors.append(f"{url}: {exc}")
                index += 1
                continue

            try:
                doc = parse_detail_md(result.html, url)
                record = build_raw_record(doc, source_platform=spec.key, raw_ref=result.snapshot_path)
                validate_record_shape(record)
            except ParseError as exc:
                counters["parse_error"] += 1
                errors.append(f"{url}: {exc}")
                index += 1
                continue

            records.append(record)
            counters["fetched"] += 1
            # 采集成功即落 checkpoint：被限流打断后重跑，已完成的请求不会白费
            self._append_checkpoint(spec, url)
            index += 1
        return records

    # ---------------- 断点续爬 ----------------
    def _checkpoint_path(self, spec: SourceSpec) -> Path:
        return PROJECT_ROOT / "data" / "checkpoints" / f"{spec.key}.urls"

    def _load_checkpoint(self, spec: SourceSpec) -> set[str]:
        path = self._checkpoint_path(spec)
        if not path.exists():
            return set()
        return {line.strip() for line in path.read_text(encoding="utf-8").splitlines() if line.strip()}

    def _append_checkpoint(self, spec: SourceSpec, url: str) -> None:
        path = self._checkpoint_path(spec)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as handle:
                handle.write(url + "\n")
        except OSError as exc:  # 断点文件写失败不该中断采集
            log.warning("断点文件写入失败", extra={"source": spec.key, "err": str(exc)})

    def _fetch_detail(self, spec: SourceSpec, url: str):
        """取一条详情页并校验内容契约。

        HTTP 200 不代表拿到数据：瓜子触发频率限制时返回的验证页也是 200。
        因此这里在**取数成功之后**再加一道结构校验，不通过就按被拦截处理，
        交给上层冷却重试——绝不把空壳页当成"解析不到字段的合法页面"混进统计。
        """
        result = self.fetcher.fetch_once(spec, url, tier=self._detail_tier(spec))
        if not looks_like_detail_md(result.html):
            raise BlockedError(
                f"响应不符合详情页结构契约（len={len(result.html)}），疑似风控页",
                context={"url": url},
            )
        return result

    @staticmethod
    def _detail_tier(spec: SourceSpec) -> str:
        """详情页固定走 http 档：隐形/浏览器档对频率限制无效，只会加重风控。"""
        return "http" if spec.tier not in ("stealth", "dynamic") else spec.tier

    # ------------------------------------------------------------------ #
    # 辅助
    # ------------------------------------------------------------------ #
    @staticmethod
    def _target_lines(spec: SourceSpec, task: DetailTaskSpec) -> list[str]:
        if task.line in ("ev", "fuel"):
            return [task.line]
        if spec.line in ("ev", "fuel"):
            return [spec.line]
        return ["ev", "fuel"]

    @staticmethod
    def _worst_missing_ratio(records: list[dict[str, Any]]) -> float:
        if not records:
            return 0.0
        total = len(records)
        return max(
            (sum(1 for r in records if r.get(f) in (None, "", "-")) / total for f in DRIFT_FIELDS),
            default=0.0,
        )

    def _decide_status(
        self, counters: Counter, errors: list[str], missing_ratio: float, parsed: int
    ) -> str:
        if counters["circuit_broken"]:
            return "partial"
        if parsed == 0:
            return "failed"
        if errors or counters["parse_error"]:
            return "partial"
        if missing_ratio > self.settings.fetch.drift_missing_ratio:
            return "partial"
        return "success"

    @staticmethod
    def _detail(counters: Counter, errors: list[str], upsert: Any) -> str:
        parts = [
            f"详情页成功 {counters['fetched']}",
            f"已下架(404) {counters['not_found']}",
            f"风控拦截 {counters['blocked']}",
            f"解析失败 {counters['parse_error']}",
        ]
        if counters["skipped"]:
            parts.append(f"续爬跳过 {counters['skipped']}")
        if counters["circuit_broken"]:
            parts.append("已触发熔断")
        if upsert is not None and upsert.rejected:
            parts.append(f"校验拒收 {upsert.rejected}")
        if upsert is not None and upsert.reject_samples:
            parts.append("拒收样例: " + " | ".join(upsert.reject_samples[:2]))
        if errors:
            parts.append("错误: " + " | ".join(errors[:3]))
        return "; ".join(parts)[:2000]

    @staticmethod
    def _compliance_guard(spec: SourceSpec) -> str | None:
        if spec.compliance.robots_status == "disallowed":
            return (
                f"源 {spec.key} 的 robots.txt 明确禁止抓取"
                f"（{spec.compliance.note or 'Disallow: /'}），已按合规要求拦截，请更换数据源"
            )
        if not spec.enabled:
            return f"源 {spec.key} 已在配置中禁用：{spec.compliance.note or '未说明原因'}"
        return None

    def _safe_notify(self, event: str, *, title: str, detail: str, metrics: dict[str, Any]) -> None:
        try:
            self.notifier.send(event, title=title, detail=detail, metrics=metrics)
        except Exception as exc:  # noqa: BLE001 - 告警永远不能影响采集
            log.error("告警通道异常", extra={"event": event, "err": str(exc)})
