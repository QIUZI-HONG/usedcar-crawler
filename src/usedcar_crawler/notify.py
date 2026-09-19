"""告警通知：站点改版 / 采集失败主动推送，而不是等业务方发现数据没更新。

未配置 webhook 时退化为只写日志，不抛异常——通知失败绝不能反过来影响采集主流程。
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any

from .config import NotifyCfg, get_settings
from .logging_setup import get_logger

log = get_logger("notify")


class Notifier:
    """企微 / 飞书群机器人 Webhook 推送。"""

    def __init__(self, cfg: NotifyCfg | None = None) -> None:
        self.cfg = cfg or get_settings().notify

    @property
    def enabled(self) -> bool:
        return bool(self.cfg.enabled and self.cfg.webhook_url)

    def send(self, event: str, *, title: str, detail: str, metrics: dict[str, Any] | None = None) -> bool:
        """发送通知；返回是否真正投递成功。

        :param event: 事件名，需命中 ``notify.on_events`` 白名单才会发送
        """
        if event not in self.cfg.on_events:
            return False
        if not self.enabled:
            log.warning("告警事件（未配置 webhook，仅记录日志）", extra={"event": event, "title": title,
                                                                      "detail": detail})
            return False
        payload = {
            "msgtype": "markdown",
            "markdown": {"content": self._render(title, detail, metrics)},
        }
        try:
            request = urllib.request.Request(
                self.cfg.webhook_url,
                data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(request, timeout=10) as response:  # noqa: S310
                ok = 200 <= response.status < 300
            log.info("告警已推送", extra={"event": event, "ok": ok})
            return ok
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            log.error("告警推送失败", extra={"event": event, "err": str(exc)})
            return False

    @staticmethod
    def _render(title: str, detail: str, metrics: dict[str, Any] | None) -> str:
        lines = [f"**{title}**", "", detail]
        if metrics:
            lines.append("")
            lines.extend(f"- {key}: {value}" for key, value in metrics.items())
        return "\n".join(lines)
