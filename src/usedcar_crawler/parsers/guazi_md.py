"""瓜子二手车「车源详情 Markdown」解析器。

数据来源说明（合规依据见 ``docs/DATA_SOURCE.md``）：
瓜子在 robots.txt 中显式放行了 ``/car-detail/*.md`` 与 ``/*.md$``，并通过
``/guazisou/cardetail/pc_cardetail_md_index.xml`` 提供了面向机器的车源索引。
本项目**只走这条显式放行的通道**，不解析其受保护的 HTML 页面。

与列表页解析器（``ev.py`` / ``fuel.py``）的关系：
详情页给出的字段比列表页更全、更准，因此**不再从标题里猜品牌车型，也不再靠
宽松正则从卡片文案里捞价格**——直接吃结构化字段，并用严格的键名契约校验。
但两条路径最终产出**同一种"原始记录"结构**，因此清洗、校验、入库、导出
全链路完全复用，不需要为详情页源另起一套存储逻辑。

能源类型分流：一份详情页可能属于新能源线也可能属于燃油线，
``energy.type`` 决定它进 ``ev_vehicles`` 还是 ``fuel_vehicles``。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

import yaml

from ..dedupe import make_vehicle_key
from ..errors import ParseError

# 详情页正文的键值行：`id:c172686863204199` / `full_payment:284700元`
# 注意不能要求冒号后必须有空格——瓜子的正文是 `id:xxx` 这种紧凑写法
_KV_LINE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)\s*:\s*(.*)$")
_FRONTMATTER = re.compile(r"^---\s*\n(.*?)\n---\s*\n?", re.DOTALL)

# 判定响应是否为"真的详情页"：瓜子的风控页是一个 <div id="app"> 的 JS 空壳
_HTML_SHELL_MARKERS = ("<!doctype html", "<html", 'id="app"', "guazi-mall-ucenter")
_REQUIRED_KEYS = ("full_payment",)

# 正文中出现的特征（详情页特有的文本线索）
_BATTERY_HEALTH = re.compile(r"电池健康度\s*(?:[:：]|为)?\s*(\d{1,3}(?:\.\d+)?)\s*%")
_BATTERY_TYPE = re.compile(r"(三元锂|磷酸铁锂|刀片电池|钠离子电池|锰酸锂|钴酸锂)")
_RANGE_LABELLED = re.compile(r"(?:纯电|电动)续航\s*(?:约)?\s*(\d{2,4})\s*(?:km|KM|公里)")
_RANGE_WITH_STD = re.compile(r"(CLTC|NEDC|WLTP|EPA)\s*(?:工况)?\s*续航\s*(?:约)?\s*(\d{2,4})\s*(?:km|KM|公里)")
_RANGE_PLAIN = re.compile(r"续航\s*(?:约)?\s*(\d{2,4})\s*(?:km|KM|公里)")
# 前缀式写法：数字在"续航"之前，如「610km续航」「210km纯电续航」「1300km综合续航」。
# 瓜子的文案里两种语序都会出现，只认后缀式会白白丢掉一半续航样本。
# 修饰词限定在标点之前，避免把「续航1625km，鸿蒙座舱」这类跨句误配。
_RANGE_PREFIX = re.compile(r"(\d{2,4})\s*(?:km|KM|公里)\s*([^\s，。；,;、/]{0,6})续航")
_EXTENDED = re.compile(r"增程|插电混动|插混|PHEV|DM-i|DM-p")


@dataclass
class DetailDoc:
    """一份详情页解析结果。"""

    url: str
    frontmatter: dict[str, Any] = field(default_factory=dict)
    fields: dict[str, str] = field(default_factory=dict)          # 正文扁平键值
    sections: dict[str, str] = field(default_factory=dict)        # 段落限定键（energy.type）
    text: str = ""                                                # 全文，供特征正则使用

    def get(self, *keys: str, default: str | None = None) -> str | None:
        """按顺序取候选键，返回第一个非空值（兼容键名在不同车型上的差异）。"""
        for key in keys:
            value = self.sections.get(key) or self.fields.get(key)
            if value not in (None, "", "-"):
                return str(value)
        return default


def looks_like_detail_md(body: str | None) -> bool:
    """校验响应是否为合格的车源详情页。

    这是详情页源最重要的一道闸门：瓜子在触发频率限制时会 **以 HTTP 200** 返回一个
    验证页。若不校验内容结构，这些空壳页会被当成"解析不到字段的合法页面"，
    静默推高缺失率、污染统计口径——正是爬虫项目最常见的数据质量事故。
    """
    if not body or len(body) < 200:
        return False
    head = body.lstrip()[:600].lower()
    if any(marker in head for marker in _HTML_SHELL_MARKERS):
        return False
    if not head.startswith("---"):
        return False
    return all(key in body for key in _REQUIRED_KEYS)


def split_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    """切出 YAML frontmatter 与正文。

    frontmatter 是标准 YAML（``description`` / ``highlights`` 为块标量），
    用 yaml 解析；正文是 ``key:value`` 紧凑写法，**不是**合法 YAML，
    交给 :func:`parse_body` 处理。
    """
    match = _FRONTMATTER.match(text)
    if not match:
        raise ParseError("详情页缺少 frontmatter，页面结构可能已变化")
    try:
        meta = yaml.safe_load(match.group(1)) or {}
    except yaml.YAMLError as exc:
        raise ParseError(f"frontmatter 不是合法 YAML：{exc}") from exc
    if not isinstance(meta, dict):
        raise ParseError("frontmatter 顶层不是映射结构")
    return meta, text[match.end():]


def parse_body(body: str) -> tuple[dict[str, str], dict[str, str]]:
    """解析正文键值，返回 (扁平键值, 段落限定键值)。

    段落限定键解决"同名键在不同段落含义不同"的问题：``energy`` 段下的 ``type``
    是能源类型，若与别处的 ``type`` 混在一起就会取错。因此同时保留
    ``type`` 与 ``energy.type`` 两份，取用时优先用带段落前缀的那个。
    """
    flat: dict[str, str] = {}
    scoped: dict[str, str] = {}
    section: str | None = None
    for raw_line in body.splitlines():
        line = raw_line.strip()
        if not line or line.startswith(("-", "#")):
            continue
        match = _KV_LINE.match(line)
        if not match:
            continue
        key, value = match.group(1), match.group(2).strip()
        if not value:            # 无值行是段落标题
            section = key
            continue
        flat.setdefault(key, value)
        if section:
            scoped.setdefault(f"{section}.{key}", value)
    if "full_payment" not in flat:
        raise ParseError("正文缺少价格字段 full_payment，页面结构可能已变化")
    return flat, scoped


def parse_detail_md(text: str, url: str) -> DetailDoc:
    """把详情页 Markdown 解析成 :class:`DetailDoc`。"""
    meta, body = split_frontmatter(text)
    flat, scoped = parse_body(body)
    # 特征正则在"全文"上跑：电池健康度、续航这类信息只出现在 frontmatter 的
    # description / highlights 散文里，不在结构化字段中
    full_text = " ".join(
        str(meta.get(key, "")) for key in ("title", "description", "highlights")
    ) + " " + body
    return DetailDoc(url=url, frontmatter=meta, fields=flat, sections=scoped, text=full_text)


def resolve_line(doc: DetailDoc) -> str:
    """判定该车源属于哪条业务线。

    优先用结构化的 ``energy.type``；缺失时退化为技术参数判断
    （新能源车的排量常写作 ``0.0L``，排放标准写作 ``新能源``）。
    """
    energy = (doc.get("energy.type", "type") or "").strip()
    if "新能源" in energy or "电动" in energy:
        return "ev"
    if "燃油" in energy or "汽油" in energy or "柴油" in energy:
        return "fuel"
    emission = (doc.get("emission_standard") or "").strip()
    engine = (doc.get("engine") or "").strip()
    if "新能源" in emission or engine.startswith(("0.0", "0.00")):
        return "ev"
    return "fuel"


def parse_range_from_text(text: str) -> tuple[int | None, str | None]:
    """从散文里提取续航，返回 ``(公里, 工况口径)``。

    这里有一个必须避开的坑：**增程/插混车型的"综合续航"不是纯电续航**。
    问界 M7 增程版标注「CLTC 综合续航 1625km」，那是"满油满电"的总里程，
    若直接当作续航入库，会与纯电车的 CLTC 续航混在同一张散点图上，
    得出完全错误的"续航越长的车越贵"结论。

    因此对增程/插混车**只接受明确标注「纯电续航」的数字**，拿不到就如实留空——
    缺失是可解释的，错误值是会误导决策的。
    """
    extended = bool(_EXTENDED.search(text))

    match = _RANGE_LABELLED.search(text)
    if match:
        return _sane_range(int(match.group(1))), "纯电"

    match = _RANGE_WITH_STD.search(text)
    if match and not _is_combined(text, match.start()):
        return _sane_range(int(match.group(2))), match.group(1).upper()

    match = _RANGE_PLAIN.search(text)
    if match and not extended and not _is_combined(text, match.start()):
        return _sane_range(int(match.group(1))), None

    # 前缀式写法放在最后：后缀式更明确，能匹配上就不必退而求其次
    match = _RANGE_PREFIX.search(text)
    if match:
        modifier = match.group(2)
        if "综合" in modifier:
            # 「1300km综合续航」同样是"满油满电"，不能当纯电续航
            return None, None
        if "纯电" in modifier or "电动" in modifier:
            return _sane_range(int(match.group(1))), "纯电"
        if not extended:
            return _sane_range(int(match.group(1))), None

    return None, None


def _is_combined(text: str, position: int) -> bool:
    """判断 ``position`` 前的 6 个字符是否把该续航标成了"综合续航"。"""
    return "综合" in text[max(0, position - 6): position + 2]


def _sane_range(km: int) -> int | None:
    """续航合理区间校验（30~1500 公里），超出即视为解析错误。"""
    return km if 30 <= km <= 1500 else None


def extract_features(doc: DetailDoc) -> dict[str, Any]:
    """提取详情页特有的衍生特征（电动车按需使用）。"""
    health = _BATTERY_HEALTH.search(doc.text)
    battery = _BATTERY_TYPE.search(doc.text)
    range_km, range_standard = parse_range_from_text(doc.text)
    # 把工况口径拼进 range_raw，让共用的 cleaners.parse_range 能一并解析出 CLTC/NEDC，
    # 而不是在这里另写一套标准化逻辑
    range_raw = None
    if range_km:
        range_raw = f"{range_standard}续航{range_km}公里" if range_standard else f"{range_km}公里"
    return {
        "battery_health_raw": f"{health.group(1)}%" if health else None,
        "battery_raw": battery.group(1) if battery else None,
        "range_raw": range_raw,
    }


def build_raw_record(
    doc: DetailDoc,
    *,
    source_platform: str,
    raw_ref: str | None = None,
) -> dict[str, Any]:
    """产出与列表页解析器同构的「原始记录」。

    字段命名刻意与 ``parsers/base.py::build_record_dict`` 对齐，
    这样清洗、模型校验、入库、导出可以零改动复用。
    """
    source_id = doc.get("id") or _id_from_url(doc.url)
    if not source_id:
        raise ParseError(f"无法从详情页确定车源 ID：{doc.url}")

    line = resolve_line(doc)
    features = extract_features(doc) if line == "ev" else {}

    record: dict[str, Any] = {
        "vehicle_key": make_vehicle_key(source_platform, source_id),
        "source_platform": source_platform,
        "source_id": source_id,
        "title_raw": doc.frontmatter.get("title") or doc.get("model"),
        # 详情页直接给出结构化品牌/车系，不要让清洗层再从标题里猜
        "brand_raw": doc.get("brand"),
        "model_raw": doc.get("series") or doc.get("model"),
        "price_raw": doc.get("full_payment"),
        "new_car_price_raw": doc.get("guide_price"),
        "reg_date_raw": doc.get("first_register"),
        "mileage_raw": doc.get("mileage"),
        "city_raw": doc.get("city"),
        "transfer_raw": doc.get("transfer_times"),
        "detail_url": doc.frontmatter.get("source") or doc.frontmatter.get("canonical") or doc.url,
        "raw_ref": raw_ref,
        # 业务线标记：一份详情页只属于一条线，由调用方据此分流入库
        "_line": line,
        # 车况档案：原样透传，写入原始层便于复核
        "_condition_grade": doc.get("condition_grade"),
        "_condition_desc": doc.get("condition_desc"),
        "_appearance_score": doc.get("appearance_score"),
    }

    if line == "ev":
        record.update(
            {
                "battery_raw": features.get("battery_raw"),
                "battery_health_raw": features.get("battery_health_raw"),
                "range_raw": features.get("range_raw"),
                "fast_charge_raw": None,
            }
        )
    else:
        record.update(
            {
                "displacement_raw": doc.get("engine"),
                "gearbox_raw": doc.get("transmission"),
                "emission_raw": doc.get("emission_standard"),
                "info_texts": [t for t in (doc.get("drive_mode"), doc.get("engine")) if t],
            }
        )
    return record


def _id_from_url(url: str) -> str | None:
    from ..sources.sitemap import detail_id  # noqa: PLC0415 - 避免模块级循环依赖

    return detail_id(url)


def validate_record_shape(record: dict[str, Any]) -> None:
    """对解析结果做一次"契约体检"，把结构性异常在入库前拦下。

    为什么不用 Pydantic 模型直接校验：模型校验发生在清洗**之后**，
    而这里要回答的是"解析器有没有读懂页面"。区分这两种失败
    （页面读不懂 vs 数据本身不合格）能让告警指向正确的方向。
    """
    for key in ("vehicle_key", "source_platform", "price_raw", "title_raw"):
        if not record.get(key):
            raise ParseError(f"解析结果缺少关键字段 {key}")
