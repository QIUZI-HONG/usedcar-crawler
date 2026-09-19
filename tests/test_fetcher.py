"""取数层测试：robots 合规闸门、三档降级、重试、快照留证。

全部使用假 Transport，不发真实网络请求。
"""

from __future__ import annotations

import gzip
import json

import pytest

from usedcar_crawler.config import FetchCfg
from usedcar_crawler.errors import BlockedError, FetchError, RobotsDeniedError
from usedcar_crawler.fetcher import (
    FetchResult,
    FetcherService,
    RobotsGate,
    SnapshotStore,
    looks_blocked,
    looks_js_shell,
)
from usedcar_crawler.rate_limit import TokenBucket

GOOD_HTML = "<html><body><div class='car-item'><h3 class='title'>大众 朗逸</h3></div></body></html>"
BLOCKED_HTML = "<html><body>Just a moment... cf-challenge</body></html>"


class FakeTransport:
    """可编排的假取数后端：按档位返回预设响应，并记录调用顺序。"""

    def __init__(self, responses: dict[str, object]) -> None:
        self.responses = responses
        self.calls: list[str] = []

    def __call__(self, url, *, tier, timeout, headers, proxy):  # noqa: ANN001, D102
        self.calls.append(tier)
        response = self.responses.get(tier)
        if isinstance(response, Exception):
            raise response
        if response is None:
            raise FetchError(f"档位 {tier} 无预设响应")
        return response


def _service(fetch_cfg: FetchCfg, transport, *, ladder=("http", "stealth", "dynamic")) -> FetcherService:
    cfg = fetch_cfg.model_copy(
        update={"tier_ladder": list(ladder), "snapshot_raw": False, "respect_robots": True}
    )
    return FetcherService(cfg, transport=transport)


class TestRobotsGate:
    def test_disallow_raises_and_wins_veto(self) -> None:
        from urllib.robotparser import RobotFileParser

        gate = RobotsGate(enabled=True)
        parser = RobotFileParser()
        parser.parse(["User-agent: *", "Disallow: /usedcar"])
        gate._cache["https://example.com"] = parser

        with pytest.raises(RobotsDeniedError):
            gate.ensure_allowed("https://example.com/usedcar?page=1")
        # 未禁止的路径应放行
        gate.ensure_allowed("https://example.com/about")

    def test_fetch_aborts_before_any_request_when_disallowed(self, fetch_cfg, registry) -> None:
        from urllib.robotparser import RobotFileParser

        transport = FakeTransport({"http": (200, GOOD_HTML)})
        service = _service(fetch_cfg, transport)
        parser = RobotFileParser()
        parser.parse(["User-agent: *", "Disallow: /"])
        service.robots._cache["https://example.com"] = parser

        spec = registry.get("local_fixture_ev").model_copy(
            update={"key": "fake", "tier": "http", "base_url": "https://example.com",
                    "url_template": "https://example.com/list?page={page}"}
        )
        with pytest.raises(RobotsDeniedError):
            service.fetch(spec, "https://example.com/list?page=1")
        assert transport.calls == []  # 合规闸门在任何网络请求之前生效

    def test_local_scheme_bypasses_robots(self, fetch_cfg) -> None:
        gate = RobotsGate(enabled=True)
        gate.ensure_allowed("local://fixtures/ev_list.html")

    def test_disabled_gate_logs_warning(self) -> None:
        RobotsGate(enabled=False).ensure_allowed("https://example.com/x")


class TestTierLadder:
    def test_escalates_when_blocked(self, fetch_cfg, registry) -> None:
        """T1 被拦截 -> 自动升级 T2 并成功。"""
        transport = FakeTransport({"http": (403, BLOCKED_HTML), "stealth": (200, GOOD_HTML)})
        spec = registry.get("local_fixture_ev").model_copy(update={"tier": "http"})
        result = _service(fetch_cfg, transport).fetch(spec, "local://x")

        assert isinstance(result, FetchResult)
        assert result.tier == "stealth"
        assert transport.calls == ["http", "stealth"]
        assert result.status == 200

    def test_escalates_on_js_shell(self, fetch_cfg, registry) -> None:
        """SPA 空壳页 -> 升级到浏览器档位。"""
        shell = "<html><body><div id='app'></div></body></html>"
        transport = FakeTransport({"http": (200, shell), "stealth": (200, GOOD_HTML)})
        spec = registry.get("local_fixture_ev").model_copy(update={"tier": "http"})
        result = _service(fetch_cfg, transport).fetch(spec, "local://x")
        assert result.tier == "stealth"

    def test_all_tiers_blocked_raises_blocked_error(self, fetch_cfg, registry) -> None:
        transport = FakeTransport({tier: (403, BLOCKED_HTML) for tier in ("http", "stealth", "dynamic")})
        spec = registry.get("local_fixture_ev").model_copy(update={"tier": "http"})
        with pytest.raises(BlockedError):
            _service(fetch_cfg, transport).fetch(spec, "local://x")
        assert transport.calls == ["http", "stealth", "dynamic"]

    def test_fetch_error_escalates_then_succeeds(self, fetch_cfg, registry) -> None:
        transport = FakeTransport({"http": FetchError("连接重置"), "stealth": (200, GOOD_HTML)})
        spec = registry.get("local_fixture_ev").model_copy(update={"tier": "http"})
        result = _service(fetch_cfg, transport).fetch(spec, "local://x")
        assert result.tier == "stealth"

    def test_all_tiers_exhausted_raises_fetch_error(self, fetch_cfg, registry) -> None:
        transport = FakeTransport({tier: FetchError("超时") for tier in ("http", "stealth", "dynamic")})
        spec = registry.get("local_fixture_ev").model_copy(update={"tier": "http"})
        with pytest.raises(FetchError) as exc_info:
            _service(fetch_cfg, transport).fetch(spec, "local://x")
        assert "所有档位均取数失败" in str(exc_info.value)

    def test_starting_tier_is_respected(self, fetch_cfg, registry) -> None:
        """反爬强的站点直接从 stealth 起步，不做无谓的 T1 尝试。"""
        transport = FakeTransport({"stealth": (200, GOOD_HTML)})
        spec = registry.get("local_fixture_ev").model_copy(update={"tier": "stealth"})
        result = _service(fetch_cfg, transport).fetch(spec, "local://x")
        assert transport.calls == ["stealth"]
        assert result.tier == "stealth"

    def test_retry_then_success(self, fetch_cfg, registry) -> None:
        """同档位内先失败后成功：验证重试生效。"""
        attempts = {"count": 0}

        class FlakyTransport:
            def __call__(self, url, *, tier, timeout, headers, proxy):  # noqa: ANN001
                attempts["count"] += 1
                if attempts["count"] == 1:
                    raise FetchError("瞬时抖动")
                return 200, GOOD_HTML

        cfg = fetch_cfg.model_copy(update={"tier_ladder": ["http"], "max_retries": 2, "backoff_base": 0.01})
        spec = registry.get("local_fixture_ev").model_copy(update={"tier": "http"})
        result = FetcherService(cfg, transport=FlakyTransport()).fetch(spec, "local://x")
        assert attempts["count"] == 2
        assert result.status == 200

    def test_results_are_not_retried_on_retryable_false(self, fetch_cfg, registry) -> None:
        from usedcar_crawler.errors import ParseError

        attempts = {"count": 0}

        class FatalTransport:
            def __call__(self, url, *, tier, timeout, headers, proxy):  # noqa: ANN001
                attempts["count"] += 1
                raise ParseError("解析失败不该重试")

        cfg = fetch_cfg.model_copy(update={"tier_ladder": ["http"], "max_retries": 3, "backoff_base": 0.01})
        spec = registry.get("local_fixture_ev").model_copy(update={"tier": "http"})
        with pytest.raises(FetchError):
            FetcherService(cfg, transport=FatalTransport()).fetch(spec, "local://x")
        assert attempts["count"] == 1


class TestStatusCodeGate:
    """状态码闸门：Scrapling 对 4xx/5xx 不抛异常，必须由我们判定。"""

    def test_404_raises_not_found_and_stops(self, fetch_cfg, registry) -> None:
        from usedcar_crawler.errors import NotFoundError

        transport = FakeTransport({"http": (404, "<html>not found</html>")})
        spec = registry.get("local_fixture_ev").model_copy(update={"tier": "http"})
        with pytest.raises(NotFoundError):
            _service(fetch_cfg, transport).fetch(spec, "local://x")
        assert transport.calls == ["http"]  # 404 不做无意义的升档

    def test_500_is_retryable_fetch_error(self, fetch_cfg, registry) -> None:
        transport = FakeTransport({tier: (500, "oops") for tier in ("http", "stealth", "dynamic")})
        spec = registry.get("local_fixture_ev").model_copy(update={"tier": "http"})
        with pytest.raises(FetchError):
            _service(fetch_cfg, transport).fetch(spec, "local://x")

    @pytest.mark.parametrize("status", [200, 201, 204, 299])
    def test_success_statuses_pass(self, status: int) -> None:
        from usedcar_crawler.fetcher import ensure_status_ok

        ensure_status_ok(status, "https://x/1", "http")

    @pytest.mark.parametrize("status", [404, 410])
    def test_not_found_statuses(self, status: int) -> None:
        from usedcar_crawler.errors import NotFoundError
        from usedcar_crawler.fetcher import ensure_status_ok

        with pytest.raises(NotFoundError):
            ensure_status_ok(status, "https://x/1", "http")

    @pytest.mark.parametrize("status", [401, 403, 405, 429, 503])
    def test_blocked_statuses(self, status: int) -> None:
        from usedcar_crawler.fetcher import ensure_status_ok

        with pytest.raises(BlockedError):
            ensure_status_ok(status, "https://x/1", "http")


class TestSnapshot:
    def test_snapshot_writes_body_and_meta(self, tmp_path) -> None:
        store = SnapshotStore(tmp_path, enabled=True)
        path = store.save(line="ev", source="demo", url="https://x/1", status=200, tier="http", body=GOOD_HTML)
        assert path is not None

        body_path = tmp_path / "ev" / __import__("datetime").date.today().isoformat() / "demo"
        gz_files = list(body_path.glob("*.html.gz"))
        meta_files = list(body_path.glob("*.meta.json"))
        assert len(gz_files) == 1 and len(meta_files) == 1
        with gzip.open(gz_files[0], "rt", encoding="utf-8") as handle:
            assert handle.read() == GOOD_HTML
        meta = json.loads(meta_files[0].read_text(encoding="utf-8"))
        assert meta["url"] == "https://x/1"
        assert meta["tier"] == "http"
        assert meta["size"] == len(GOOD_HTML)

    def test_snapshot_disabled_returns_none(self, tmp_path) -> None:
        assert SnapshotStore(tmp_path, enabled=False).save(
            line="ev", source="s", url="u", status=200, tier="http", body="x"
        ) is None


class TestHelpers:
    @pytest.mark.parametrize("body", ["Just a moment...", "验证码", "访问过于频繁", "Access denied by cloudflare"])
    def test_looks_blocked_by_marker(self, body: str) -> None:
        assert looks_blocked(200, f"<html>{body}</html>")

    def test_looks_blocked_by_status(self) -> None:
        assert looks_blocked(429, GOOD_HTML)
        assert not looks_blocked(200, GOOD_HTML)

    def test_looks_js_shell(self) -> None:
        assert looks_js_shell("<html><body><div id='app'></div></body></html>")
        assert not looks_js_shell(GOOD_HTML)
        assert not looks_js_shell("<html>" + "x" * 300_000 + "</html>")


class TestTokenBucket:
    def test_burst_then_throttle(self) -> None:
        bucket = TokenBucket(rate=50, capacity=1)
        assert bucket.acquire(timeout=1) == 0.0  # 首次立即取到
        waited = bucket.acquire(timeout=1)
        assert 0 < waited < 0.5  # 第二次必须等待补充

    def test_timeout_raises(self) -> None:
        bucket = TokenBucket(rate=0.5, capacity=1)
        bucket.acquire(timeout=0.01)
        with pytest.raises(TimeoutError):
            bucket.acquire(timeout=0.01)

    def test_invalid_rate_rejected(self) -> None:
        with pytest.raises(ValueError):
            TokenBucket(rate=0)
