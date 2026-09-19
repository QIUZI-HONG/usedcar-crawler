"""业务线 B：燃油二手车解析器。

燃油车的估值主线是「车龄 + 里程 + 排量/变速箱工况」，排放标准决定限行城市能否过户，
这些都是新能源线不存在的字段，因此单独一条业务线。
"""

from __future__ import annotations

from typing import Any

from ..models import FuelVehicle
from ..pipeline import cleaners as cl
from ..sources.registry import SourceSpec
from .base import BaseParser, _node_text, build_record_dict, pick_first_text, pick_texts


class FuelParser(BaseParser):
    """燃油车源解析器。"""

    line = "fuel"

    def parse_line_fields(self, card: Any, spec: SourceSpec) -> dict[str, Any]:
        selectors = spec.selectors
        # 瓜子等站点的「1.5L 自动 / 国六」常写在同一个信息行里，整行取回后统一正则提取
        info_texts = pick_texts(card, selectors.get("info_row", "")) if selectors.get("info_row") else []
        card_text = _node_text(card) or ""
        if not info_texts and any(k in card_text for k in ("L", "T", "自动", "手动", "国")):
            info_texts = [card_text]
        return {
            "displacement_raw": pick_first_text(card, selectors.get("displacement", "")),
            "gearbox_raw": pick_first_text(card, selectors.get("gearbox", "")),
            "emission_raw": pick_first_text(card, selectors.get("emission", "")),
            "info_texts": info_texts,
        }

    def to_model(self, record: dict[str, Any]) -> FuelVehicle:
        """原始字典 -> 领域模型。"""
        extra = " ".join(record.get("info_texts") or [])
        payload = build_record_dict(record)
        payload["raw_ref"] = record.get("raw_ref")
        payload.update(
            {
                "displacement_l": cl.parse_displacement(record.get("displacement_raw") or extra),
                "gearbox": cl.normalize_gearbox(record.get("gearbox_raw") or extra),
                "emission_standard": cl.normalize_emission(record.get("emission_raw") or extra),
                "is_deleted": bool(record.get("is_deleted", False)),
            }
        )
        return FuelVehicle(**payload)

    @staticmethod
    def _reg_fallbacks(card: Any) -> list[str]:
        return ['span[class*="year"]::text', 'div[class*="info"] span::text', 'span[class*="info"]::text']

    @staticmethod
    def _mileage_fallbacks(card: Any) -> list[str]:
        return ['span[class*="km"]::text', 'div[class*="info"] span::text', 'span[class*="info"]::text']
