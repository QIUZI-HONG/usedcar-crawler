"""合规巡检：把 robots.txt 的核查变成一条可重复执行的命令，而不是一次性的人工确认。

两级防线：
1. **配置档案**（``config/sites.yaml`` 的 ``compliance``）：已知结论固化，明确禁止的源直接禁用；
2. **运行时闸门**（``RobotsGate``）：每次真实请求前仍然联网复核，确保结论不会过期。

``compliance --verify`` 会真正读取 robots.txt 并给出"按当前配置能否抓"的结论。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .errors import RobotsDeniedError
from .fetcher import RobotsGate
from .logging_setup import get_logger
from .sources.registry import SourceSpec

log = get_logger("compliance")


@dataclass
class ComplianceReport:
    key: str
    name: str
    line: str
    enabled: bool
    recorded_status: str
    live_status: str = "skipped"
    checked_urls: list[str] = field(default_factory=list)
    reason: str = ""

    @property
    def verdict(self) -> str:
        if not self.enabled:
            return "已禁用"
        if self.recorded_status == "disallowed":
            return "禁止抓取"
        if self.live_status == "denied":
            return "禁止抓取（实时核查）"
        if self.live_status == "allowed":
            return "允许抓取"
        return "待核查"


def verify_source(spec: SourceSpec, gate: RobotsGate | None = None, *, pages: tuple[int, ...] = (1, 2)) -> ComplianceReport:
    """对单个源做 robots.txt 实时核查（含翻页样本）。"""
    report = ComplianceReport(
        key=spec.key,
        name=spec.name,
        line=spec.line,
        enabled=spec.enabled,
        recorded_status=spec.compliance.robots_status,
    )
    if spec.tier == "fixture":
        report.live_status = "skipped"
        report.reason = "离线样本源，不涉及网络请求"
        return report
    if not spec.enabled:
        report.live_status = "skipped"
        report.reason = spec.compliance.note or "已在配置中禁用"
        return report

    gate = gate or RobotsGate(enabled=True)
    denied: list[str] = []
    for page in pages:
        url = spec.build_url(page)
        report.checked_urls.append(url)
        try:
            gate.ensure_allowed(url)
        except RobotsDeniedError as exc:
            denied.append(f"{url} -> {exc}")
    report.live_status = "denied" if denied else "allowed"
    if denied:
        report.reason = " | ".join(denied)
    return report


def verify_all(registry_specs: list[SourceSpec], *, gate: RobotsGate | None = None) -> list[ComplianceReport]:
    return [verify_source(spec, gate) for spec in registry_specs]
