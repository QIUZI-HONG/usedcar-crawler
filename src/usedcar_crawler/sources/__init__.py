"""采集源层。"""

from __future__ import annotations

from .registry import SourceRegistry, SourceSpec, get_registry, load_registry

__all__ = ["SourceRegistry", "SourceSpec", "get_registry", "load_registry"]
