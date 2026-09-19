"""集成测试：真实走一遍 HTTP 取数（本地起服务，不外联）。

为什么要有这个文件：
- 单元测试用假 Transport，能证明"逻辑对"，但证明不了"Scrapling 集成没写错"；
- 这里用一个本地 HTTP 服务，让 Scrapling 的 ``Fetcher`` 真正发请求、真正解析响应，
  同时验证 robots 闸门、快照落盘、入库闭环。

实测踩坑记录（已在实现中修正）：
- ``Fetcher`` 的入口是 ``.get()``，``StealthyFetcher`` / ``DynamicFetcher`` 才是 ``.fetch()``；
- ``element.text`` 只返回节点自身文本，含子节点时必须用 ``get_all_text()``。
"""

from __future__ import annotations

import functools
import http.server
import socketserver
import threading
from pathlib import Path

import pytest

from usedcar_crawler.fetcher import FetcherService
from usedcar_crawler.runner import CrawlRunner, TaskSpec
from usedcar_crawler.sources.registry import SourceRegistry

FIXTURES = Path(__file__).resolve().parent / "fixtures"


class _QuietHandler(http.server.SimpleHTTPRequestHandler):
    """静默的静态文件服务，避免测试输出被访问日志淹没。"""

    def log_message(self, *args, **kwargs) -> None:  # noqa: D102
        return


@pytest.fixture(scope="module")
def http_base() -> str:
    handler = functools.partial(_QuietHandler, directory=str(FIXTURES))
    with socketserver.TCPServer(("127.0.0.1", 0), handler) as server:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        yield f"http://127.0.0.1:{server.server_address[1]}"
        server.shutdown()


@pytest.fixture
def http_spec(registry, http_base):
    """把离线样本源改造成真实 HTTP 源（选择器完全复用）。"""
    original = registry.get("local_fixture_ev")
    return original.model_copy(
        update={
            "key": "integration_ev",
            "tier": "http",
            "base_url": http_base,
            "url_template": f"{http_base}/ev_list.html?page={{page}}",
        }
    )


class TestRealHttpFetch:
    def test_scrapling_http_tier_returns_parsable_html(self, http_spec, fetch_cfg, tmp_path) -> None:
        """验证 Scrapling Fetcher 的真实 HTTP 路径 + 快照留证。"""
        cfg = fetch_cfg.model_copy(update={"snapshot_raw": True, "raw_dir": str(tmp_path / "raw")})
        service = FetcherService(cfg)
        result = service.fetch(http_spec, http_spec.build_url(1))

        assert result.status == 200
        assert result.tier == "http"
        assert "car-item" in result.html
        assert "比亚迪" in result.html
        assert result.elapsed > 0
        # 快照必须落盘（数据可溯源）
        assert result.snapshot_path is not None
        snapshots = list((tmp_path / "raw").rglob("*.html.gz"))
        assert len(snapshots) == 1
        metas = list((tmp_path / "raw").rglob("*.meta.json"))
        assert len(metas) == 1

    def test_robots_txt_missing_is_treated_as_allowed(self, http_spec, fetch_cfg) -> None:
        """目标站未声明 robots.txt（404）时应正常抓取，而不是误报拦截。"""
        service = FetcherService(fetch_cfg.model_copy(update={"respect_robots": True}))
        service.robots._cache.clear()
        result = service.fetch(http_spec, http_spec.build_url(1))
        assert result.status == 200

    def test_http_404_raises_not_found(self, http_spec, fetch_cfg) -> None:
        """不存在的路径必须报错：Scrapling 对 404 不抛异常，靠状态码闸门兜住。"""
        from usedcar_crawler.errors import NotFoundError

        cfg = fetch_cfg.model_copy(update={"tier_ladder": ["http"], "max_retries": 1, "backoff_base": 0.01})
        service = FetcherService(cfg)
        with pytest.raises(NotFoundError):
            service.fetch(http_spec, f"{http_spec.base_url}/not-exists.html")


class TestEndToEndOverHttp:
    def test_full_pipeline_persists_over_real_http(self, http_spec, fetch_cfg, repo, fake_notifier) -> None:
        """真实 HTTP -> 解析 -> 清洗 -> 校验 -> 入库，落库 3 条（1 条价格「面议」被拒）。"""
        service = FetcherService(fetch_cfg)
        runner = CrawlRunner(
            fetcher=service, repo=repo, notifier=fake_notifier, registry=SourceRegistry([http_spec])
        )
        logs = runner.run(TaskSpec(line="ev", sources=["integration_ev"], pages=1, task_type="daily_incr"))

        assert len(logs) == 1
        log = logs[0]
        assert log.status == "success"
        assert log.fetched_count == 4
        assert log.inserted_count == 3
        assert log.pages_fetched == 1
        assert log.missing_ratio < 0.3

        rows = repo.query("ev")
        assert len(rows) == 3
        han = next(row for row in rows if row["model"] == "汉EV")
        assert float(han["price_wan"]) == 15.98
        assert han["battery_type"] == "三元锂"
        assert han["range_km"] == 605
        assert han["location_city"] == "广州"
