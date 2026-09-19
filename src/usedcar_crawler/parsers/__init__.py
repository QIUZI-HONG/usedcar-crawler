"""解析器注册表：业务线 -> 解析器实例。"""

from __future__ import annotations

from .base import BaseParser
from .ev import EVParser
from .fuel import FuelParser

_PARSERS: dict[str, BaseParser] = {
    "ev": EVParser(),
    "fuel": FuelParser(),
}


def get_parser(name: str) -> BaseParser:
    try:
        return _PARSERS[name]
    except KeyError as exc:
        raise ValueError(f"未知解析器 '{name}'，可用：{sorted(_PARSERS)}") from exc


__all__ = ["BaseParser", "EVParser", "FuelParser", "get_parser"]
