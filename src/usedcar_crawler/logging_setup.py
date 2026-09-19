"""日志初始化。

特性：
- 控制台 + 滚动文件双通道，文件按天切割并自动清理；
- 可选 JSON Lines 输出，方便接入 ELK / Loki；
- ``trace_id`` 贯穿单次采集任务，日志可按任务串联。
"""

from __future__ import annotations

import json
import logging
import logging.handlers
import sys
import uuid
from contextvars import ContextVar
from pathlib import Path
from typing import Any

_trace_id: ContextVar[str] = ContextVar("trace_id", default="-")

_RESERVED = {
    "name", "msg", "args", "levelname", "levelno", "pathname", "filename", "module",
    "exc_info", "exc_text", "stack_info", "lineno", "funcName", "created", "msecs",
    "relativeCreated", "thread", "threadName", "processName", "process", "taskName",
    "message", "asctime",
}


def new_trace_id() -> str:
    """生成并绑定一个新的任务追踪 ID。"""
    tid = uuid.uuid4().hex[:12]
    _trace_id.set(tid)
    return tid


def get_trace_id() -> str:
    return _trace_id.get()


class TraceIdFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.trace_id = get_trace_id()
        return True


class HumanFormatter(logging.Formatter):
    """本地开发可读格式。"""

    def format(self, record: logging.LogRecord) -> str:
        base = (
            f"{self.formatTime(record, '%Y-%m-%d %H:%M:%S')} "
            f"{record.levelname:<7} [{record.trace_id}] "
            f"{record.name}: {record.getMessage()}"
        )
        extra = {k: v for k, v in record.__dict__.items() if k not in _RESERVED and k != "trace_id"}
        if extra:
            base += " | " + " ".join(f"{k}={v}" for k, v in sorted(extra.items()))
        if record.exc_info:
            base += "\n" + self.formatException(record.exc_info)
        return base


class JsonFormatter(logging.Formatter):
    """结构化格式，字段名固定，便于下游解析。"""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S"),
            "level": record.levelname,
            "logger": record.name,
            "trace_id": get_trace_id(),
            "msg": record.getMessage(),
        }
        for key, value in record.__dict__.items():
            if key not in _RESERVED and key != "trace_id":
                payload[key] = value
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False, default=str)


def setup_logging(
    *,
    level: str = "INFO",
    json_mode: bool = False,
    log_dir: str | Path | None = "logs",
    retention_days: int = 14,
) -> logging.Logger:
    """初始化根日志器，幂等（重复调用不会叠加 handler）。"""
    root = logging.getLogger()
    for handler in list(root.handlers):
        root.removeHandler(handler)

    root.setLevel(getattr(logging, level.upper(), logging.INFO))
    fmt = JsonFormatter() if json_mode else HumanFormatter()
    trace_filter = TraceIdFilter()

    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(fmt)
    console.addFilter(trace_filter)
    root.addHandler(console)

    if log_dir:
        path = Path(log_dir)
        path.mkdir(parents=True, exist_ok=True)
        file_handler = logging.handlers.TimedRotatingFileHandler(
            path / "crawler.log", when="midnight", backupCount=retention_days, encoding="utf-8"
        )
        file_handler.setFormatter(fmt)
        file_handler.addFilter(trace_filter)
        root.addHandler(file_handler)

    # 第三方库降噪
    for noisy in ("urllib3", "httpx", "httpcore", "asyncio", "apscheduler.executors"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    return logging.getLogger("usedcar_crawler")


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(f"usedcar_crawler.{name}")
