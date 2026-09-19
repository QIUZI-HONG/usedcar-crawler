"""解析层基类：自适应选择器 + 缺失率监控。

两个关键能力：
1. **选择器自适应**：首跑 ``auto_save=True`` 学习元素指纹，站点改版后用 ``adaptive=True`` 自愈；
2. **缺失率监控**：必需字段缺失率超阈值即判定为"站点改版"，产出 ``SchemaDriftError`` 级别的告警，
   把线上事故前置成一条告警。

解析器只负责"从 DOM 取字符串"，所有数值语义交给 ``pipeline.cleaners``，职责不混。
"""

from __future__ import annotations

import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Iterable

from ..dedupe import make_vehicle_key
from ..errors import ParseError, SchemaDriftError
from ..logging_setup import get_logger
from ..sources.registry import SourceSpec

log = get_logger("parser")

_ATTR_RE = re.compile(r"::attr\(([^)]+)\)")
_TEXT_SUFFIX = "::text"
_SOURCE_ID_PATTERNS = (
    re.compile(r"[?&](?:id|carid|infoId|car_id)=([A-Za-z0-9_-]{4,})"),
    re.compile(r"/(\d{6,})(?:\.html?|/)?$"),
    re.compile(r"-(\d{6,})(?:\.html?)?$"),
)


def make_selector(html: str, url: str | None = None) -> Any:
    """构造 Scrapling 解析对象（兼容不同版本的类名）。

    Scrapling 仅安装了 parser 组件时也能工作，不需要浏览器依赖。
    """
    try:
        from scrapling.parser import Selector  # noqa: PLC0415
    except ImportError:  # pragma: no cover - 兼容旧版本命名
        try:
            from scrapling.parser import Adaptor as Selector  # type: ignore[no-redef]  # noqa: PLC0415
        except ImportError as exc:
            raise ParseError("未安装 scrapling，请执行：pip install scrapling") from exc
    if url:
        try:
            return Selector(html, url=url)
        except TypeError:
            log.debug("当前 Scrapling 版本 Selector 不接受 url 参数，退回无参构造")
    return Selector(html)


def _as_list(nodes: Any) -> list[Any]:
    """把 Scrapling 的返回统一成 list。"""
    if nodes is None:
        return []
    if isinstance(nodes, list):
        return nodes
    if isinstance(nodes, (str, bytes)):
        return [nodes]
    try:
        return list(nodes)
    except TypeError:
        return [nodes]


def select_all(node: Any, selector: str, *, adaptive: bool = False, auto_save: bool = False) -> list[Any]:
    """在节点上执行 CSS 选择器，支持自适应定位。

    ``adaptive`` / ``auto_save`` 是 Scrapling 的抗改版能力；不同版本参数名存在差异
    （``adaptive`` 与 ``auto_match``），这里做一次兼容降级，并在降级时留下 debug 日志，
    绝不静默吞掉异常。
    """
    base = _strip_pseudo(selector)
    kwargs: dict[str, bool] = {}
    if auto_save:
        kwargs["auto_save"] = True
    if adaptive:
        kwargs["adaptive"] = True
    if kwargs:
        try:
            return _as_list(node.css(base, **kwargs))
        except TypeError:
            alt = {k.replace("adaptive", "auto_match"): v for k, v in kwargs.items()}
            try:
                return _as_list(node.css(base, **alt))
            except TypeError:
                log.debug("当前 Scrapling 版本不支持自适应参数，退回普通选择器", extra={"selector": base})
    return _as_list(node.css(base))


def _node_text(element: Any) -> str | None:
    """提取元素文本。

    关键点（实测坑）：Scrapling 的 ``element.text`` 只返回**该节点自身的直接文本**，
    对 ``<span class="price"><em>15.98</em>万</span>`` 会只得到 ``"万"``。
    因此：节点含子元素时统一走 ``get_all_text()``，否则用 ``.text``；两者都空再兜底一次。
    """
    if isinstance(element, (str, bytes)):
        text = element.decode("utf-8", "ignore") if isinstance(element, bytes) else element
        return text.strip() or None

    has_children = False
    try:
        has_children = len(list(getattr(element, "children", []) or [])) > 0
    except TypeError:  # pragma: no cover - 属性异常时按无子节点处理
        has_children = False

    text: Any = None
    if has_children:
        try:
            text = element.get_all_text()
        except Exception:  # noqa: BLE001 - 兼容个别版本差异
            text = getattr(element, "text", None)
    else:
        text = getattr(element, "text", None)

    if text is None or not str(text).strip():
        try:
            text = element.get_all_text()
        except Exception:  # noqa: BLE001
            pass
    if text is None:
        return None
    cleaned = re.sub(r"\s+", " ", str(text)).strip()
    return cleaned or None


def _strip_pseudo(selector: str) -> str:
    """去掉 ``::text`` / ``::attr(x)`` 伪元素，得到纯选择器。"""
    cleaned = _ATTR_RE.sub("", selector)
    return cleaned.replace(_TEXT_SUFFIX, "").strip()


def pick_text(node: Any, selector: str, *, adaptive: bool = False, auto_save: bool = False) -> str | None:
    """取第一个匹配元素的文本。"""
    nodes = select_all(node, selector, adaptive=adaptive, auto_save=auto_save)
    for element in nodes:
        text = _node_text(element)
        if text:
            return text
    return None


def pick_texts(node: Any, selector: str) -> list[str]:
    """取所有匹配元素的文本（如「2021年 / 3.2万公里」被拆成多个 span）。"""
    texts: list[str] = []
    for element in select_all(node, selector):
        text = _node_text(element)
        if text:
            texts.append(text)
    return texts


def pick_attr(node: Any, selector: str, default: str | None = None) -> str | None:
    """取第一个匹配元素的指定属性。"""
    attr_match = _ATTR_RE.search(selector)
    attr = attr_match.group(1) if attr_match else "href"
    for element in select_all(node, selector):
        if isinstance(element, str):
            continue
        attrib = getattr(element, "attrib", None) or {}
        value = attrib.get(attr)
        if value:
            return str(value)
    return default


def pick_first_text(node: Any, selector: str, *, fallback: Iterable[str] = ()) -> str | None:
    """按顺序尝试多个选择器，返回第一个有值的文本。

    站点常见做法是"同一字段在不同卡片里位置不同"，多选择器兜底比写死一个稳。
    """
    for candidate in (selector, *fallback):
        if not candidate:
            continue
        text = pick_text(node, candidate)
        if text:
            return text
    return None


def extract_source_id(url: str | None) -> str | None:
    """从详情页 URL 中提取平台车源 ID。"""
    if not url:
        return None
    for pattern in _SOURCE_ID_PATTERNS:
        match = pattern.search(url)
        if match:
            return match.group(1)
    path = url.split("?")[0].rstrip("/")
    tail = path.rsplit("/", 1)[-1]
    return tail if tail and not tail.startswith("index") else None


@dataclass
class ParseOutcome:
    """解析结果与质量指标。"""

    records: list[dict] = field(default_factory=list)
    cards_found: int = 0
    missing_ratio: float = 0.0
    drift_fields: list[str] = field(default_factory=list)
    field_missing: dict[str, float] = field(default_factory=dict)

    @property
    def has_drift(self) -> bool:
        return bool(self.drift_fields)

    @property
    def worst_field_ratio(self) -> float:
        """缺失率最高的那个必需字段的比例。

        告警与 ``crawl_log.missing_ratio`` 用它：单个字段大面积缺失才是"改版"的信号，
        把所有字段的缺失混在一起平均会稀释掉这个问题。
        """
        return max(self.field_missing.values(), default=0.0)


class BaseParser(ABC):
    """双业务线解析器的公共骨架。"""

    line: str = ""

    def parse(self, html: str, spec: SourceSpec, url: str, *, adaptive: bool = False) -> ParseOutcome:
        """解析整页，返回车源列表与数据质量指标。"""
        page = make_selector(html, url=url)
        cards = select_all(page, spec.selectors["card"], adaptive=adaptive)
        if not cards:
            raise ParseError("列表容器选择器零命中，页面结构可能已变", context={"url": url, "selector": spec.selectors["card"]})

        records: list[dict] = []
        failures: dict[str, int] = {name: 0 for name in spec.required}
        for card in cards:
            record = self._parse_card(card, spec, page_url=url)
            if record is None:
                continue
            records.append(record)

        total = len(records)
        drift_fields: list[str] = []
        field_missing: dict[str, float] = {}
        if total:
            for name in spec.required:
                missing = sum(1 for record in records if self._raw_value(record, name) in (None, ""))
                failures[name] = missing
                field_missing[name] = round(missing / total, 4)
                if missing / total > self._drift_threshold():
                    drift_fields.append(name)
        ratio = (sum(failures.values()) / (total * len(failures))) if total and failures else 0.0
        outcome = ParseOutcome(
            records=records,
            cards_found=len(cards),
            missing_ratio=round(ratio, 4),
            drift_fields=drift_fields,
            field_missing=field_missing,
        )
        if outcome.has_drift:
            log.error(
                "检测到疑似站点改版（必需字段缺失率过高）",
                extra={"url": url, "source": spec.key, "drift_fields": ",".join(drift_fields),
                       "worst_field_ratio": outcome.worst_field_ratio, "cards": len(cards)},
            )
        return outcome

    @staticmethod
    def _raw_value(record: dict, field: str) -> Any:
        """把 ``required`` 里的逻辑字段名映射到解析记录中的原始字段。

        解析记录统一使用 ``*_raw`` 存放原始文案（``title`` -> ``title_raw``），
        这里做一次映射，避免"字段名对不上导致全字段误判缺失"这类低级但致命的错误。
        """
        if f"{field}_raw" in record:
            return record[f"{field}_raw"]
        return record.get(field)

    @staticmethod
    def _drift_threshold() -> float:
        from ..config import get_settings  # noqa: PLC0415 - 避免模块级循环依赖

        return get_settings().fetch.drift_missing_ratio

    def ensure_quality(self, outcome: ParseOutcome, spec: SourceSpec) -> None:
        """质量闸门：漂移则抛出 ``SchemaDriftError``，由上层决定告警或中断。"""
        if outcome.has_drift:
            raise SchemaDriftError(
                "必需字段缺失率超阈值，判定为站点改版",
                context={"source": spec.key, "fields": ",".join(outcome.drift_fields),
                         "worst_field_ratio": outcome.worst_field_ratio},
            )

    # ---------------- 子类实现 ----------------
    def _parse_card(self, card: Any, spec: SourceSpec, *, page_url: str) -> dict | None:
        """解析单个卡片，公共字段在此统一处理，专属字段交给 ``parse_line_fields``。"""
        selectors = spec.selectors
        title = pick_first_text(card, selectors.get("title", ""))
        if not title:
            log.debug("卡片标题为空，跳过", extra={"source": spec.key})
            return None

        link = pick_attr(card, selectors.get("link", "a::attr(href)"))
        detail_url = self._absolute(spec, link)
        price_raw = pick_first_text(card, selectors.get("price", ""))
        # 部分站点把「上牌时间 / 里程」放在同一段文案中，这里合并候选文本供标准化使用
        reg_raw = pick_first_text(card, selectors.get("reg_date", ""), fallback=self._reg_fallbacks(card))
        mileage_raw = pick_first_text(card, selectors.get("mileage", ""), fallback=self._mileage_fallbacks(card))
        new_price_raw = pick_first_text(card, selectors.get("new_car_price", ""))
        record: dict[str, Any] = {
            "source_platform": spec.key,
            "title_raw": title,
            "price_raw": price_raw,
            "new_car_price_raw": new_price_raw,
            "reg_date_raw": reg_raw,
            "mileage_raw": mileage_raw,
            "city_raw": pick_first_text(card, selectors.get("city", "")),
            "detail_url": detail_url,
            # 透传原始文案，便于排障；也会写入原始表
            "source_id": extract_source_id(detail_url),
        }
        record.update(self.parse_line_fields(card, spec))
        if record.get("source_id") is None:
            record["source_id"] = record.get("source_id_fallback")
        record["vehicle_key"] = make_vehicle_key(
            spec.key,
            record.get("source_id"),
            fallback=f"{title}|{price_raw}|{mileage_raw}",
        )
        return record

    @abstractmethod
    def parse_line_fields(self, card: Any, spec: SourceSpec) -> dict[str, Any]:
        """业务线专属字段（EV：电池/续航；FUEL：排量/变速箱/排放）。"""

    # ---------------- 辅助 ----------------
    @staticmethod
    def _absolute(spec: SourceSpec, link: str | None) -> str | None:
        from ..pipeline.cleaners import relative_url  # noqa: PLC0415

        return relative_url(spec.base_url, link)

    @staticmethod
    def _reg_fallbacks(card: Any) -> list[str]:
        """上牌时间兜底：部分站点的年月混在通用信息块里。"""
        return []

    @staticmethod
    def _mileage_fallbacks(card: Any) -> list[str]:
        return []


def build_record_dict(record: dict[str, Any]) -> dict[str, Any]:
    """把解析原始字典转成模型可用的干净字段（数值语义统一在此落实）。

    保留 ``*_raw`` 字段用于排障与审计，但存储层只落标准化结果 + 原始片段摘要。
    """
    from ..pipeline import cleaners as cl  # noqa: PLC0415

    year, month = cl.parse_year_month(record.get("reg_date_raw"))
    # 详情页类数据源会直接给出结构化的品牌/车系，比从标题里切分准确得多；
    # 列表页数据源没有这两个字段，退回标题切分。两条路径产出同一套语义。
    brand_raw = cl.clean_text(record.get("brand_raw"))
    model_raw = cl.clean_text(record.get("model_raw"))
    if brand_raw and model_raw:
        brand, model = cl.normalize_brand(brand_raw), model_raw
    else:
        brand, model = cl.split_brand_model(record.get("title_raw"))
    payload: dict[str, Any] = {
        "vehicle_key": record.get("vehicle_key"),
        "source_platform": record.get("source_platform"),
        "source_id": record.get("source_id"),
        "title_raw": record.get("title_raw"),
        "brand": brand,
        "model": model,
        "price_wan": cl.parse_price_wan(record.get("price_raw")),
        "new_car_price_wan": cl.parse_price_wan(record.get("new_car_price_raw")),
        "mileage_km": cl.parse_mileage_km(record.get("mileage_raw")),
        "reg_year": year,
        "reg_month": month,
        "transfer_count": cl.parse_int(record.get("transfer_raw")) if record.get("transfer_raw") else None,
        "location_city": cl.clean_city(record.get("city_raw")),
        "detail_url": record.get("detail_url"),
        "raw_ref": record.get("raw_ref"),
    }
    return payload
