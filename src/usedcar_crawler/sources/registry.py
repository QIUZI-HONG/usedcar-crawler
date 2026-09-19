"""源注册表：把 ``config/sites.yaml`` 变成强类型对象。

价值：新增或调整采集源只改 YAML，不改代码；配置错误在**启动时**暴露，
而不是在跑到第 8 页时才崩。
"""

from __future__ import annotations

import functools
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field, field_validator, model_validator

from ..config import DEFAULT_CONFIG_DIR
from ..errors import ConfigError

Tier = Literal["http", "stealth", "dynamic", "fixture"]
ParserName = Literal["ev", "fuel", "guazi_md"]
RobotsStatus = Literal["allowed", "disallowed", "unknown"]
# both：详情页源一份数据同时覆盖油/电两条线，入库时按车辆能源类型分流
BusinessLine = Literal["ev", "fuel", "both"]


class ComplianceInfo(BaseModel):
    """合规档案：把 robots.txt 的核查结论固化在配置里（可审计、可追溯）。

    为什么要写死结论：
    - 避免对明确禁止抓取的站点反复发起探测（既浪费资源，也不礼貌）；
    - 让"能不能抓"这件事在**配置评审**阶段就被看见，而不是等到线上被拦。
    运行时的 ``RobotsGate`` 仍然是最终裁判，两者互为校验。
    """

    robots_status: RobotsStatus = "unknown"
    checked_at: str = ""
    note: str = ""
    # 部分站点（如瓜子）Disallow: /*?* —— 禁止带查询参数的 URL，翻页必须走路径形式
    allow_query_params: bool = True

    @property
    def runnable(self) -> bool:
        return self.robots_status != "disallowed"


class SourceSpec(BaseModel):
    """单个采集源的完整定义。"""

    key: str
    line: BusinessLine
    name: str
    enabled: bool = True
    tier: Tier = "http"
    parser: ParserName
    base_url: str = ""
    url_template: str
    # page    ：列表页翻页（{page} 为页码）
    # api     ：公开 JSON 接口
    # sitemap ：站点地图 -> 逐条详情页（详情页源，{page} 为第几个子地图）
    list_mode: Literal["page", "api", "sitemap"] = "page"
    max_pages: int = Field(default=10, ge=1, le=1000)
    # sitemap 模式下每个子地图采样多少条详情页（全量动辄十万条，采样是礼貌也是效率）
    detail_sample: int = Field(default=20, ge=1, le=500)
    # 命中风控后的冷却秒数与该次任务允许的冷却次数（超过即熔断，避免越试越死）
    cooldown_seconds: float = Field(default=45.0, ge=0.0, le=600)
    max_cooldowns: int = Field(default=3, ge=0, le=20)
    qps: float | None = Field(default=None, gt=0, le=5)
    selectors: dict[str, str] = Field(default_factory=dict)
    required: list[str] = Field(default_factory=lambda: ["title", "price"])
    headers: dict[str, str] = Field(default_factory=dict)
    compliance: ComplianceInfo = ComplianceInfo()

    @property
    def is_detail_mode(self) -> bool:
        """是否为「站点地图 -> 详情页」型采集源。"""
        return self.list_mode == "sitemap"

    @field_validator("selectors")
    @classmethod
    def _no_empty_selector(cls, value: dict[str, str]) -> dict[str, str]:
        for name, selector in value.items():
            if not str(selector).strip():
                raise ValueError(f"选择器 {name} 为空")
        return value

    @model_validator(mode="after")
    def _check_required_fields(self) -> "SourceSpec":
        # 详情页源的数据契约由 parser 承担（它是结构化文档而非 DOM），不需要 CSS 选择器
        if not self.is_detail_mode:
            missing = [f for f in self.required if f not in self.selectors]
            if missing:
                raise ValueError(f"required 字段未在 selectors 中定义：{missing}")
            if "card" not in self.selectors:
                raise ValueError("必须定义 card（列表项容器）选择器")
        if "{page}" not in self.url_template and self.max_pages > 1:
            raise ValueError("url_template 缺少 {page} 占位符，无法翻页")
        # 合规硬校验：站点禁止 query 参数时，配置里出现 ? 直接拒绝加载
        if (
            not self.compliance.allow_query_params
            and "?" in self.url_template
            and self.tier != "fixture"
        ):
            raise ValueError(
                f"站点 {self.key} 的 robots.txt 禁止带查询参数的 URL（Disallow: /*?*），"
                f"但 url_template 含 '?'：{self.url_template}"
            )
        return self

    def build_url(self, page: int) -> str:
        """生成第 page 页的请求地址。"""
        return self.url_template.format(page=page)

    @property
    def effective_qps(self) -> float | None:
        return self.qps

    @property
    def runnable(self) -> bool:
        return self.enabled and self.compliance.runnable


class SourceRegistry:
    """站点配置容器。"""

    def __init__(self, sources: list[SourceSpec]) -> None:
        if not sources:
            raise ConfigError("sites.yaml 中未定义任何采集源")
        self._sources = {s.key: s for s in sources}

    def __len__(self) -> int:
        return len(self._sources)

    def all(self, *, include_disabled: bool = False) -> list[SourceSpec]:
        specs = list(self._sources.values())
        return specs if include_disabled else [s for s in specs if s.enabled]

    def get(self, key: str, *, include_disabled: bool = True) -> SourceSpec:
        try:
            return self._sources[key]
        except KeyError as exc:
            available = ", ".join(sorted(self._sources))
            raise ConfigError(f"未知采集源 '{key}'，可用：{available}") from exc

    def by_line(self, line: str, *, include_disabled: bool = False) -> list[SourceSpec]:
        """按业务线取源。

        ``line`` 为 ``ev`` / ``fuel`` 时，``line=both`` 的源同样命中——它一份数据
        同时产出两条线，入库阶段会按能源类型分流，不应被任一条线漏掉。
        """
        specs = self.all(include_disabled=include_disabled)
        if line == "all":
            return specs
        return [s for s in specs if s.line == line or s.line == "both"]

    def blocked(self) -> list[SourceSpec]:
        """返回因合规或人工禁用而不参与采集的源（用于巡检报告）。"""
        return [s for s in self._sources.values() if not s.runnable]

    def keys(self) -> list[str]:
        return sorted(self._sources)


def load_registry(path: str | Path | None = None) -> SourceRegistry:
    """从 YAML 加载并校验源配置。"""
    cfg_path = Path(path) if path else DEFAULT_CONFIG_DIR / "sites.yaml"
    if not cfg_path.exists():
        raise ConfigError(f"站点配置不存在：{cfg_path}")
    raw = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
    defaults = raw.get("defaults") or {}
    default_headers = defaults.get("headers") or {}
    default_qps = defaults.get("qps")

    sources: list[SourceSpec] = []
    for index, item in enumerate(raw.get("sources") or []):
        if not isinstance(item, dict):
            raise ConfigError(f"sources[{index}] 必须是映射结构")
        merged = dict(item)
        merged.setdefault("headers", dict(default_headers))
        if merged.get("qps") is None and default_qps:
            merged["qps"] = default_qps
        try:
            sources.append(SourceSpec(**merged))
        except Exception as exc:  # noqa: BLE001 - 统一转成配置异常，附上源标识
            raise ConfigError(f"源配置校验失败 [{item.get('key', index)}]：{exc}") from exc
    return SourceRegistry(sources)


@functools.lru_cache(maxsize=1)
def get_registry() -> SourceRegistry:
    return load_registry()
