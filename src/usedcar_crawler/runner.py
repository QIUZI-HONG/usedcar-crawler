"""采集任务编排：一次任务 = 若干源 × 若干页 的 抓取 → 解析 → 清洗 → 去重 → 入库 闭环。

设计原则：
- **单源失败不影响其他源**：一个源挂了，任务继续，最后在日志里如实标注；
- **质量优先于数量**：字段缺失率超阈值会告警并把任务标为 ``partial``，不粉饰成功率；
- **一切可观测**：每次任务落 ``crawl_log``，字段包括抓取量、去重量、拒绝量、耗时分位。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from .config import get_settings
from .dedupe import dedupe_by_key
from .errors import CrawlerError, FetchError, ParseError, RobotsDeniedError, StorageError
from .fetcher import FetcherService
from .logging_setup import get_logger, new_trace_id
from .models import CrawlLog
from .notify import Notifier
from .parsers import get_parser
from .parsers.base import select_all, make_selector
from .sources.registry import SourceRegistry, SourceSpec, get_registry
from .storage.repository import Repository, build_engine

log = get_logger("runner")


@dataclass
class TaskSpec:
    """一次采集任务的输入参数。"""

    line: str = "all"
    sources: list[str] = field(default_factory=list)
    pages: int = 1
    task_type: str = "daily_incr"
    adaptive: bool = False
    fixture: bool = False


class CrawlRunner:
    """采集任务执行器。"""

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

    # ---------------- 目标源解析 ----------------
    def resolve_sources(self, task: TaskSpec) -> list[SourceSpec]:
        """确定本次任务要跑的源。

        语义：**显式指定的 ``sources`` 优先**，此时 ``line`` 仅作为日志维度；
        未指定 ``sources`` 时按 ``line`` 取该业务线的全部源。
        """
        if task.sources:
            # 显式指定：即使不可采集也返回，由合规闸门给出明确原因（而不是静默跳过）
            specs = [self.registry.get(key) for key in task.sources]
        else:
            # 自动选择：直接排除已禁用/已判定禁止的源，避免产生无意义的失败记录
            specs = [spec for spec in self.registry.by_line(task.line, include_disabled=True) if spec.runnable]
        if task.fixture:
            specs = [s for s in specs if s.tier == "fixture"]
        else:
            # 演示样本源（tier=fixture）只服务于离线自检与 CI，绝不混入真实增量任务
            specs = [s for s in specs if s.tier != "fixture"]
        if not specs:
            raise CrawlerError(f"没有匹配的采集源：line={task.line} sources={task.sources} fixture={task.fixture}")
        return specs

    # ---------------- 主流程 ----------------
    def run(self, task: TaskSpec) -> list[CrawlLog]:
        """执行任务，返回每个源的运行日志。"""
        new_trace_id()
        specs = self.resolve_sources(task)
        log.info(
            "采集任务开始",
            extra={"line": task.line, "task_type": task.task_type, "sources": len(specs), "pages": task.pages},
        )
        logs = [self._run_source(spec, task) for spec in specs]
        succeeded = sum(1 for item in logs if item.status == "success")
        log.info(
            "采集任务结束",
            extra={"total": len(logs), "success": succeeded,
                   "partial": sum(1 for i in logs if i.status == "partial"),
                   "failed": sum(1 for i in logs if i.status == "failed")},
        )
        return logs

    def _run_source(self, spec: SourceSpec, task: TaskSpec) -> CrawlLog:
        new_trace_id()
        started = datetime.now()

        # 合规闸门：配置里已判定禁止 / 人工禁用的源，连请求都不发
        guard = self._compliance_guard(spec)
        if guard is not None:
            payload = CrawlLog(
                business_line=spec.line,
                source_platform=spec.key,
                task_type=task.task_type,
                status="failed",
                error_count=1,
                error_detail=guard,
                started_at=started,
                finished_at=datetime.now(),
            )
            self.repo.log_crawl(payload.model_dump())
            self._safe_notify(
                "compliance_blocked",
                title=f"合规拦截：{spec.name}",
                detail=guard,
                metrics={"来源": spec.key, "档案状态": spec.compliance.robots_status},
            )
            return payload

        collected: list[dict[str, Any]] = []
        pages_ok = 0
        errors: list[str] = []
        missing_ratio = 0.0
        drift_fields: list[str] = []

        for page in range(1, task.pages + 1):
            url = spec.build_url(page)
            try:
                result = self.fetcher.fetch(spec, url, tier="fixture" if task.fixture else None)
                parser = get_parser(spec.parser)
                outcome = parser.parse(result.html, spec, result.url, adaptive=task.adaptive)
            except RobotsDeniedError as exc:
                errors.append(f"page{page} robots: {exc}")
                log.error("robots.txt 禁止抓取，跳过该源", extra={"source": spec.key, "url": url})
                break
            except (FetchError, ParseError) as exc:
                errors.append(f"page{page}: {exc}")
                log.warning("页面处理失败", extra={"source": spec.key, "page": page, "err": str(exc)})
                break

            pages_ok += 1
            for record in outcome.records:
                record["raw_ref"] = result.snapshot_path
                record["captured_at"] = started
            collected.extend(outcome.records)
            # 记"最差字段缺失率"而不是平均值：单个必需字段大面积缺失才是改版信号
            missing_ratio = max(missing_ratio, outcome.worst_field_ratio)
            if outcome.has_drift:
                drift_fields = sorted(set(drift_fields) | set(outcome.drift_fields))
            if not outcome.records:
                log.info("该页无有效数据，提前结束翻页", extra={"source": spec.key, "page": page})
                break

        deduped, duplicates = dedupe_by_key(collected)
        upsert = None
        try:
            upsert = self.repo.upsert_vehicles(spec.line, deduped)
        except StorageError as exc:
            errors.append(f"storage: {exc}")
            log.error("入库失败", extra={"source": spec.key, "err": str(exc)})

        status = "success"
        if errors or drift_fields:
            status = "partial"
        if pages_ok == 0:
            status = "failed"

        payload = CrawlLog(
            business_line=spec.line,
            source_platform=spec.key,
            task_type=task.task_type,
            status=status,
            pages_fetched=pages_ok,
            fetched_count=len(collected),
            parsed_count=len(deduped),
            inserted_count=upsert.inserted if upsert else 0,
            updated_count=upsert.updated if upsert else 0,
            dup_count=duplicates,
            error_count=len(errors) + (upsert.rejected if upsert else 0),
            missing_ratio=missing_ratio,
            error_detail=" | ".join(errors + (upsert.reject_samples if upsert else []))[:2000],
            started_at=started,
            finished_at=datetime.now(),
        )
        self.repo.log_crawl(payload.model_dump())
        self._alert(spec, payload, drift_fields)
        log.info(
            "源采集完成",
            extra={"source": spec.key, "status": status, "pages": pages_ok,
                   "parsed": payload.parsed_count, "inserted": payload.inserted_count,
                   "updated": payload.updated_count, "dups": duplicates,
                   "elapsed": payload.elapsed_seconds},
        )
        return payload

    @staticmethod
    def _compliance_guard(spec: SourceSpec) -> str | None:
        """返回拦截原因；``None`` 表示允许采集。"""
        # 先判 robots：这是最有信息量的原因（"为什么不能抓"），也最需要被审计
        if spec.compliance.robots_status == "disallowed":
            return (
                f"源 {spec.key} 的 robots.txt 明确禁止抓取"
                f"（{spec.compliance.note or 'Disallow: /'}），已按合规要求拦截，请更换数据源"
            )
        if not spec.enabled:
            return f"源 {spec.key} 已在配置中禁用：{spec.compliance.note or '未说明原因'}"
        return None

    def _alert(self, spec: SourceSpec, payload: CrawlLog, drift_fields: list[str]) -> None:
        """失败与改版两条告警线，避免"数据静默不更新"。"""
        if drift_fields:
            self._safe_notify(
                "schema_drift",
                title=f"疑似站点改版：{spec.name}",
                detail=f"必需字段缺失率 {payload.missing_ratio:.1%}，受影响字段：{', '.join(drift_fields)}",
                metrics={"来源": spec.key, "卡片数": payload.fetched_count},
            )
        if payload.status == "failed":
            self._safe_notify(
                "source_failed",
                title=f"采集失败：{spec.name}",
                detail=payload.error_detail or "所有档位均取数失败",
                metrics={"来源": spec.key, "页数": payload.pages_fetched},
            )

    def _safe_notify(self, event: str, *, title: str, detail: str, metrics: dict[str, Any]) -> None:
        try:
            self.notifier.send(event, title=title, detail=detail, metrics=metrics)
        except Exception as exc:  # noqa: BLE001 - 通知永远不能影响采集
            log.error("告警通道异常", extra={"event": event, "err": str(exc)})


def probe_source(spec: SourceSpec, fetcher: FetcherService | None = None, *, page: int = 1,
                 fixture: bool = False) -> dict[str, Any]:
    """选择器现场校准：抓一页，报告每个选择器的命中数量。

    上线新源或站点改版后的第一步就该跑这个命令，而不是直接猜选择器。
    """
    service = fetcher or FetcherService()
    url = spec.build_url(page)
    result = service.fetch(spec, url, tier="fixture" if fixture else None)
    page_obj = make_selector(result.html, url=result.url)
    report: dict[str, Any] = {
        "source": spec.key,
        "url": url,
        "status": result.status,
        "tier": result.tier,
        "bytes": len(result.html),
        "selectors": {},
    }
    for field_name, selector in spec.selectors.items():
        hits = len(select_all(page_obj, selector))
        report["selectors"][field_name] = hits
    report["missing_required"] = [name for name in spec.required if not report["selectors"].get(name)]
    report["ok"] = not report["missing_required"]
    return report
