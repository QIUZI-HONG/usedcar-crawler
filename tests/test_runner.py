"""编排层与配置层测试：端到端流程、失败隔离、改版告警、配置校验。"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from usedcar_crawler.config import get_settings
from usedcar_crawler.errors import ConfigError
from usedcar_crawler.fetcher import FetcherService
from usedcar_crawler.runner import CrawlRunner, TaskSpec
from usedcar_crawler.sources.registry import SourceRegistry, SourceSpec, load_registry

FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures"
# 端到端用例固定用这两个 CI 样本源：demo_* 是演示用的大样本，与断言的条目数无关
CI_SOURCES = ["local_fixture_ev", "local_fixture_fuel"]


@pytest.fixture
def runner(repo, fetch_cfg, registry, fake_notifier):
    fetcher = FetcherService(fetch_cfg, fixtures_dir=FIXTURE_DIR)
    specs = [spec for spec in registry.all() if spec.tier == "fixture"]
    return CrawlRunner(fetcher=fetcher, repo=repo, notifier=fake_notifier, registry=SourceRegistry(specs))


class TestEndToEnd:
    def test_both_lines_run_offline(self, runner, repo) -> None:
        logs = runner.run(TaskSpec(sources=list(CI_SOURCES), pages=1, task_type="selftest", fixture=True))
        by_line = {item.business_line: item for item in logs}

        assert set(by_line) == {"ev", "fuel"}
        assert all(item.status == "success" for item in logs)
        # EV: 4 张卡片，其中「面议」被拒 -> 3 条入库
        assert by_line["ev"].fetched_count == 4
        assert by_line["ev"].inserted_count == 3
        assert by_line["ev"].error_count == 1
        # FUEL: 4 张卡片，其中「暂无」价格被拒 -> 3 条入库
        assert by_line["fuel"].fetched_count == 4
        assert by_line["fuel"].inserted_count == 3
        assert len(repo.query("ev")) == 3
        assert len(repo.query("fuel")) == 3

    def test_second_run_is_idempotent(self, runner, repo) -> None:
        runner.run(TaskSpec(sources=list(CI_SOURCES), pages=1, task_type="selftest", fixture=True))
        logs = runner.run(TaskSpec(sources=list(CI_SOURCES), pages=1, task_type="selftest", fixture=True))
        assert all(item.inserted_count == 0 for item in logs)
        assert all(item.updated_count == 3 for item in logs)
        assert len(repo.query("ev")) == 3

    def test_crawl_log_is_persisted(self, runner, repo) -> None:
        runner.run(TaskSpec(sources=["local_fixture_ev"], pages=1, task_type="selftest", fixture=True))
        logs = repo.recent_logs(5)
        assert logs and logs[0]["business_line"] == "ev"
        assert logs[0]["status"] == "success"
        assert logs[0]["finished_at"] is not None

    def test_single_source_selection(self, runner) -> None:
        logs = runner.run(TaskSpec(line="ev", sources=["local_fixture_fuel"], pages=1,
                                   task_type="selftest", fixture=True))
        # 显式指定源时以 sources 为准（source 自带 line=fuel，故按 fuel 处理）
        assert len(logs) == 1


class TestFailureIsolation:
    def test_broken_source_does_not_block_others(self, repo, fetch_cfg, registry, fake_notifier) -> None:
        """一个源彻底失败时，其他源必须照常完成，且失败被如实记录。"""
        broken = registry.get("local_fixture_ev").model_copy(
            update={"key": "broken_ev", "url_template": "not-exist.html?page={page}", "tier": "fixture"}
        )
        healthy = registry.get("local_fixture_fuel")
        runner = CrawlRunner(
            fetcher=FetcherService(fetch_cfg, fixtures_dir=FIXTURE_DIR),
            repo=repo,
            notifier=fake_notifier,
            registry=SourceRegistry([broken, healthy]),
        )
        logs = runner.run(TaskSpec(line="all", pages=1, task_type="selftest", fixture=True))
        by_source = {item.source_platform: item for item in logs}

        assert by_source["broken_ev"].status == "failed"
        assert by_source["broken_ev"].error_count >= 1
        assert by_source["local_fixture_fuel"].status == "success"
        # 失败必须告警
        assert ("source_failed", "采集失败：本地样本·新能源（离线）") in fake_notifier.events

    def test_drift_triggers_partial_status_and_alert(self, repo, fetch_cfg, registry, fake_notifier) -> None:
        """里程选择器失效 -> 状态 partial + schema_drift 告警，而不是悄悄少字段。"""
        drifted = registry.get("local_fixture_ev").model_copy(
            update={"selectors": {**registry.get("local_fixture_ev").selectors,
                                  "mileage": "span.mileage-not-exist::text"}}
        )
        runner = CrawlRunner(
            fetcher=FetcherService(fetch_cfg, fixtures_dir=FIXTURE_DIR),
            repo=repo,
            notifier=fake_notifier,
            registry=SourceRegistry([drifted]),
        )
        logs = runner.run(TaskSpec(line="ev", pages=1, task_type="selftest", fixture=True))
        assert logs[0].status == "partial"
        assert logs[0].missing_ratio > 0.3
        assert any(event == "schema_drift" for event, _ in fake_notifier.events)

    def test_robots_denied_marks_source_failed(self, repo, fetch_cfg, registry, fake_notifier) -> None:
        from urllib.robotparser import RobotFileParser

        spec = registry.get("local_fixture_ev").model_copy(
            update={"tier": "http", "base_url": "https://blocked.example.com",
                    "url_template": "https://blocked.example.com/list?page={page}"}
        )
        service = FetcherService(fetch_cfg, transport=lambda *a, **k: (200, "<html></html>"))
        parser = RobotFileParser()
        parser.parse(["User-agent: *", "Disallow: /"])
        service.robots._cache["https://blocked.example.com"] = parser

        runner = CrawlRunner(fetcher=service, repo=repo, notifier=fake_notifier, registry=SourceRegistry([spec]))
        logs = runner.run(TaskSpec(line="ev", pages=1, task_type="selftest"))
        assert logs[0].status == "failed"
        assert "robots" in logs[0].error_detail


class TestComplianceGuard:
    """合规是硬约束：判定禁止的源绝不能发出请求。"""

    def test_disallowed_source_is_refused_without_any_request(self, repo, fetch_cfg, registry, fake_notifier) -> None:
        calls: list[str] = []

        def spy_transport(url, *, tier, timeout, headers, proxy):  # noqa: ANN001
            calls.append(url)
            return 200, "<html></html>"

        spec = registry.get("dongchedi_ev")
        assert spec.compliance.robots_status == "disallowed"
        runner = CrawlRunner(
            fetcher=FetcherService(fetch_cfg, transport=spy_transport),
            repo=repo,
            notifier=fake_notifier,
            registry=SourceRegistry([spec]),
        )
        logs = runner.run(TaskSpec(line="ev", sources=["dongchedi_ev"], pages=1, task_type="daily_incr"))

        assert calls == []                      # 一次请求都没发
        assert logs[0].status == "failed"
        assert "robots.txt 明确禁止抓取" in logs[0].error_detail
        assert ("compliance_blocked", "合规拦截：懂车帝二手车·新能源（禁止抓取）") in fake_notifier.events

    def test_disabled_source_excluded_from_automatic_selection(self, registry) -> None:
        assert "dongchedi_ev" not in {spec.key for spec in registry.by_line("ev")}
        assert "autohome_fuel" not in {spec.key for spec in registry.by_line("fuel")}
        # 但显式查询仍可取到，且能说明被禁原因
        assert not registry.get("dongchedi_ev").runnable
        assert registry.get("dongchedi_ev").compliance.note

    def test_query_param_template_rejected_when_disallowed(self) -> None:
        """瓜子 robots 禁止 /*?*：配置里出现 query 参数必须直接拒绝加载。"""
        with pytest.raises(Exception) as exc_info:
            SourceSpec(
                key="bad_guazi", line="fuel", name="坏配置", parser="fuel",
                url_template="https://www.guazi.com/bj/buy/?page={page}",
                selectors={"card": ".i"}, required=[],
                compliance={"robots_status": "allowed", "allow_query_params": False},
            )
        assert "禁止带查询参数" in str(exc_info.value)

    def test_allowed_source_passes_guard(self, registry) -> None:
        """生产源是瓜子 Markdown 通道；其 URL 必须不含查询参数。"""
        spec = registry.get("guazi_md")
        assert spec.runnable
        assert "?" not in spec.url_template
        assert spec.compliance.robots_status == "allowed"

    def test_guazi_html_list_page_is_disabled(self, registry) -> None:
        """robots 允许但未启用的源（HTML 列表页）必须显式不可采集，且能说明原因。"""
        spec = registry.get("guazi_fuel")
        assert not spec.runnable
        assert not spec.enabled
        assert spec.compliance.note

    def test_only_one_real_source_is_enabled(self, registry) -> None:
        """真实生产源只能有一个，避免未校准的选择器混入正式数据。"""
        real = [spec.key for spec in registry.all() if spec.tier != "fixture"]
        assert real == ["guazi_md"]

    def test_compliance_verify_skips_fixture_sources(self) -> None:
        from usedcar_crawler.compliance import verify_source
        from usedcar_crawler.sources.registry import get_registry

        spec = get_registry().get("local_fixture_ev")
        report = verify_source(spec)
        assert report.live_status == "skipped"
        assert report.verdict == "待核查" or report.verdict == "允许抓取"


class TestRegistry:
    def test_real_config_loads_both_lines(self, registry) -> None:
        assert len(registry) >= 6
        # 真实生产源是 both：一份详情页数据同时覆盖两条业务线，入库时按能源类型分流
        real = [spec for spec in registry.all() if spec.tier != "fixture"]
        assert {spec.line for spec in real} == {"both"}
        assert {spec.line for spec in registry.all(include_disabled=True)} == {"ev", "fuel", "both"}
        assert registry.get("local_fixture_ev").tier == "fixture"

    def test_both_line_source_matches_either_business_line(self, registry) -> None:
        """line=both 的源在按 ev / fuel 取源时都必须命中，不能被任一条线漏掉。"""
        for line in ("ev", "fuel"):
            assert "guazi_md" in {spec.key for spec in registry.by_line(line, include_disabled=True)}
        assert "guazi_md" in {spec.key for spec in registry.by_line("all", include_disabled=True)}

    def test_unknown_source_lists_available(self, registry) -> None:
        with pytest.raises(ConfigError) as exc_info:
            registry.get("not-a-source")
        assert "可用" in str(exc_info.value)

    def test_by_line_filters_disabled_sources(self, registry) -> None:
        """默认只返回可采集的源；禁用源需要显式 include_disabled 才能拿到。"""
        # 不写死数量，改为断言语义：可用源两两成对，禁用源只在 include_disabled 时出现
        available = {spec.key for spec in registry.all()}
        everything = {spec.key for spec in registry.all(include_disabled=True)}
        assert {"dongchedi_ev", "autohome_fuel"} <= everything - available
        assert available < everything
        assert {spec.key for spec in registry.by_line("all")} == available
        assert {spec.key for spec in registry.by_line("all", include_disabled=True)} == everything

    def test_blocked_sources_are_reportable(self, registry) -> None:
        blocked = {spec.key for spec in registry.blocked()}
        assert {"dongchedi_ev", "autohome_fuel"} <= blocked

    def test_missing_required_selector_rejected(self, tmp_path) -> None:
        config = {
            "sources": [{
                "key": "bad", "line": "ev", "name": "坏配置", "parser": "ev",
                "url_template": "https://x.com/l?page={page}",
                "selectors": {"card": ".item", "title": "h3::text"},
                "required": ["title", "price"],
            }]
        }
        path = tmp_path / "sites.yaml"
        path.write_text(yaml.safe_dump(config, allow_unicode=True), encoding="utf-8")
        with pytest.raises(ConfigError) as exc_info:
            load_registry(path)
        assert "required 字段未在 selectors 中定义" in str(exc_info.value)

    def test_missing_card_selector_rejected(self, tmp_path) -> None:
        config = {"sources": [{
            "key": "bad", "line": "fuel", "name": "坏配置", "parser": "fuel",
            "url_template": "https://x.com/l?page={page}",
            "selectors": {"title": "h3::text"}, "required": ["title"],
        }]}
        path = tmp_path / "sites.yaml"
        path.write_text(yaml.safe_dump(config, allow_unicode=True), encoding="utf-8")
        with pytest.raises(ConfigError):
            load_registry(path)

    def test_paging_without_placeholder_rejected(self) -> None:
        with pytest.raises(Exception) as exc_info:
            SourceSpec(
                key="x", line="ev", name="x", parser="ev", url_template="https://x.com/list",
                max_pages=10, selectors={"card": ".i"}, required=[],
            )
        assert "{page}" in str(exc_info.value)

    def test_absent_config_file_fails_loudly(self, tmp_path) -> None:
        with pytest.raises(ConfigError):
            load_registry(tmp_path / "none.yaml")


class TestConfig:
    def test_settings_defaults_and_env_override(self, monkeypatch) -> None:
        from usedcar_crawler.config import reload_settings

        settings = reload_settings()
        assert settings.fetch.respect_robots is True
        assert settings.fetch.tier_ladder == ["http", "stealth", "dynamic"]

        monkeypatch.setenv("UCC_FETCH__DEFAULT_QPS", "0.2")
        monkeypatch.setenv("UCC_DATABASE__URL", "sqlite:///./data/override.db")
        updated = reload_settings()
        assert updated.fetch.default_qps == 0.2
        assert "override" in updated.database.url

        monkeypatch.undo()
        reload_settings()

    def test_invalid_qps_rejected(self, monkeypatch) -> None:
        from pydantic import ValidationError

        from usedcar_crawler.config import reload_settings

        monkeypatch.setenv("UCC_FETCH__DEFAULT_QPS", "999")
        with pytest.raises(ValidationError):
            reload_settings()
        monkeypatch.undo()
        reload_settings()

    def test_export_columns_are_line_specific(self) -> None:
        from usedcar_crawler.pipeline.exporter import columns_for

        assert "battery_health" in columns_for("ev")
        assert "emission_standard" in columns_for("fuel")
        assert "battery_health" not in columns_for("fuel")


class TestScheduler:
    def test_six_part_cron_parses(self) -> None:
        from usedcar_crawler.scheduler import parse_cron

        trigger = parse_cron("0 30 2 * * *")
        assert trigger is not None

    def test_bad_cron_rejected(self) -> None:
        from usedcar_crawler.scheduler import parse_cron

        with pytest.raises(ConfigError):
            parse_cron("0 30 2")

    def test_jobs_registered_from_config(self) -> None:
        from usedcar_crawler.scheduler import build_job

        jobs = get_settings().schedule.jobs
        assert jobs, "settings.yaml 应至少配置一个定时任务"
        job = build_job(jobs[0].model_copy(update={"line": "ev", "pages": 3}))
        assert job["id"] == jobs[0].name
        assert job["max_instances"] == 1
        assert job["kwargs"]["pages"] == 3

# --------------------------------------------------------------------------- #
# raw 层回放（离线，零网络）
# --------------------------------------------------------------------------- #
class TestReplay:
    """回放是"解析口径修好后无需重新联网即可重算全量"的落地。

    这里刻意用真实抓下来的详情页夹具，确保回放链路与在线采集产出同构。
    """

    @pytest.fixture
    def replay_runner(self, repo, fetch_cfg, registry, fake_notifier):
        from usedcar_crawler.pipeline.detail_runner import DetailCrawlRunner

        return DetailCrawlRunner(
            fetcher=FetcherService(fetch_cfg, fixtures_dir=FIXTURE_DIR),
            repo=repo,
            notifier=fake_notifier,
            registry=registry,
        )

    @staticmethod
    def _raw_dir() -> str:
        return str(FIXTURE_DIR / "guazi_md")

    def test_replay_splits_into_both_lines(self, replay_runner, repo) -> None:
        from usedcar_crawler.pipeline.detail_runner import ReplaySpec

        logs = replay_runner.replay(ReplaySpec(sources=["guazi_md"], raw_dir=self._raw_dir()))
        by_line = {item.business_line: item for item in logs}

        assert set(by_line) == {"ev", "fuel"}
        assert by_line["ev"].inserted_count == 1
        assert by_line["fuel"].inserted_count == 1
        assert by_line["ev"].task_type == "replay"
        assert len(repo.query("ev")) == 1
        assert len(repo.query("fuel")) == 1

    def test_replay_is_idempotent(self, replay_runner, repo) -> None:
        from usedcar_crawler.pipeline.detail_runner import ReplaySpec

        spec = ReplaySpec(sources=["guazi_md"], raw_dir=self._raw_dir())
        replay_runner.replay(spec)
        logs = replay_runner.replay(spec)

        assert all(item.inserted_count == 0 for item in logs)
        assert all(item.updated_count == 1 for item in logs)

    def test_replay_rejects_responses_outside_content_contract(self, replay_runner, tmp_path) -> None:
        """风控页即使落盘，回放时也必须被同一道闸门拦下。"""
        from usedcar_crawler.pipeline.detail_runner import ReplaySpec

        raw = tmp_path / "raws"
        raw.mkdir()
        (raw / "shell.md").write_text(
            '<!DOCTYPE html><html><body><div id="app"></div>guazi-mall-ucenter</body></html>',
            encoding="utf-8",
        )

        logs = replay_runner.replay(ReplaySpec(sources=["guazi_md"], raw_dir=str(raw)))

        assert all(item.status == "failed" for item in logs)
        assert all(item.parsed_count == 0 for item in logs)
