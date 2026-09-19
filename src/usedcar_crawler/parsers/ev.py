"""业务线 A：新能源二手车解析器。

新能源车的估值逻辑与燃油车完全不同——电池健康度、续航口径、电池类型直接决定残值，
因此独立建表、独立解析、独立字段校验。
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from ..models import EVVehicle
from ..pipeline import cleaners as cl
from ..sources.registry import SourceSpec
from .base import BaseParser, _node_text, build_record_dict, pick_first_text, pick_texts


class EVParser(BaseParser):
    """新能源车源解析器。"""

    line = "ev"

    def parse_line_fields(self, card: Any, spec: SourceSpec) -> dict[str, Any]:
        selectors = spec.selectors
        battery_raw = pick_first_text(card, selectors.get("battery_info", ""))
        range_texts = pick_texts(card, selectors.get("range_info", "")) if selectors.get("range_info") else []
        # 部分站点把续航/电池写在同一段通用文案里，用整卡文本兜底
        card_text = _node_text(card) or ""
        extra = card_text if ("续航" in card_text or "电池" in card_text) else None
        return {
            "battery_raw": battery_raw or (extra if extra and "电池" in extra else None),
            "range_raw": " ".join(range_texts) if range_texts else extra,
            "battery_health_raw": pick_first_text(card, selectors.get("battery_health", "")),
            "fast_charge_raw": pick_first_text(card, selectors.get("fast_charge", "")),
        }

    def to_model(self, record: dict[str, Any]) -> EVVehicle:
        """原始字典 -> 领域模型（校验失败会抛 ValidationError，由上层按单条丢弃）。"""
        range_km, range_standard = cl.parse_range(record.get("range_raw"))
        payload = build_record_dict(record)
        payload["raw_ref"] = record.get("raw_ref")
        payload.update(
            {
                "battery_type": cl.normalize_battery(record.get("battery_raw")),
                "range_km": range_km,
                "range_standard": range_standard,
                "battery_health": cl.parse_percent(record.get("battery_health_raw")),
                "fast_charge_kw": cl.parse_float(record.get("fast_charge_raw")),
                "is_deleted": bool(record.get("is_deleted", False)),
            }
        )
        return EVVehicle(**payload)

    @staticmethod
    def _reg_fallbacks(card: Any) -> list[str]:
        return ['span[class*="date"]::text', 'span[class*="info"]::text']

    @staticmethod
    def _mileage_fallbacks(card: Any) -> list[str]:
        return ['span[class*="km"]::text', 'span[class*="info"]::text']
