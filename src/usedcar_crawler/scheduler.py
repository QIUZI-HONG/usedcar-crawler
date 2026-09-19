"""定时调度：把"能跑"变成"每天准点跑"。

- 任务定义在 ``config/settings.yaml`` 的 ``schedule.jobs``，改频率不改代码；
- 单进程串行执行 + ``max_instances=1``，避免上一个任务没跑完又叠一个；
- 每次运行独立 trace_id，日志可按任务串联。
"""

from __future__ import annotations

import time
from typing import Any

from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger

from .config import ScheduleJobCfg, get_settings
from .errors import ConfigError
from .logging_setup import get_logger, new_trace_id
from .runner import CrawlRunner, TaskSpec

log = get_logger("scheduler")


def parse_cron(expression: str) -> CronTrigger:
    """解析 6 段 cron：``秒 分 时 日 月 周``。"""
    parts = expression.split()
    if len(parts) != 6:
        raise ConfigError(f"cron 表达式必须是 6 段（秒 分 时 日 月 周），当前：{expression!r}")
    second, minute, hour, day, month, day_of_week = parts
    return CronTrigger(
        second=second, minute=minute, hour=hour, day=day, month=month, day_of_week=day_of_week,
        timezone=get_settings().app.timezone,
    )


def build_job(job: ScheduleJobCfg) -> dict[str, Any]:
    return {
        "func": _execute,
        "trigger": parse_cron(job.cron),
        "id": job.name,
        "name": job.name,
        "kwargs": {
            "line": job.line,
            "sources": job.sources,
            "pages": job.pages,
            "task_type": "weekly_full" if job.line == "all" else "daily_incr",
        },
        "max_instances": 1,
        "coalesce": True,
        "misfire_grace_time": 3600,
    }


def _execute(*, line: str, sources: list[str], pages: int, task_type: str) -> None:
    new_trace_id()
    log.info("调度任务触发", extra={"line": line, "pages": pages, "task_type": task_type})
    CrawlRunner().run(TaskSpec(line=line, sources=list(sources), pages=pages, task_type=task_type))


def start_scheduler(*, run_once: bool = False, job_name: str | None = None) -> int:
    settings = get_settings()
    jobs = settings.schedule.jobs
    if not jobs:
        raise ConfigError("settings.yaml 中未配置任何 schedule.jobs")

    if run_once:
        targets = [job for job in jobs if job_name is None or job.name == job_name]
        if not targets:
            raise ConfigError(f"未找到任务：{job_name}")
        for job in targets:
            _execute(line=job.line, sources=job.sources, pages=job.pages,
                     task_type="weekly_full" if job.line == "all" else "daily_incr")
        return 0

    if not settings.schedule.enabled:
        log.warning("调度已通过配置禁用（schedule.enabled=false）")
        return 0

    scheduler = BlockingScheduler(timezone=settings.app.timezone)
    for job in jobs:
        scheduler.add_job(**build_job(job))
        log.info("已注册定时任务", extra={"job": job.name, "cron": job.cron, "line": job.line})

    log.info("调度器启动，按 Ctrl+C 退出")
    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):  # pragma: no cover
        log.info("调度器已停止")
        time.sleep(0)
    return 0
