"""字段清洗与标准化。

爬虫岗位的真实工作是"把脏数据整理成别人能直接用的表"。本模块把所有
"脏 -> 净"的规则集中为纯函数，便于单元测试与复用（解析器不写正则）。

约定：可缺失字段统一返回 ``None``，绝不返回 ``0`` 或空串来冒充有效值
（缺失与 0 元的车没有区别，这是数据质量的红线）。
"""

from __future__ import annotations

import re
from datetime import date
from decimal import Decimal, InvalidOperation

# --------------------------------------------------------------------------- #
# 基础
# --------------------------------------------------------------------------- #

_WHITESPACE = re.compile(r"\s+")
_NUMBER = re.compile(r"-?\d+(?:\.\d+)?")
# 价格：15.98万 / ￥15.98万 / 15.98 万元 / 15.98-16.5万
_PRICE = re.compile(r"(\d+(?:\.\d+)?)\s*(?:-\s*\d+(?:\.\d+)?)?\s*(万)?")
# 里程：3.2万公里 / 5.8万km / 58000公里
_MILEAGE = re.compile(r"(\d+(?:\.\d+)?)\s*(万)?\s*(?:公里|千米|km|KM)?")

NULL_TOKENS = {"", "-", "--", "暂无", "无", "面议", "待定", "null", "None", "nan", "NaN", "未披露"}

# 主流品牌（含别名），用于从标题中切分品牌 / 车型。
# 说明：真实生产环境应改为车型库表驱动，此处用常量保证零依赖可运行。
BRAND_ALIASES: dict[str, str] = {
    # EV 阵营
    "比亚迪": "比亚迪", "BYD": "比亚迪", "特斯拉": "特斯拉", "Tesla": "特斯拉",
    "蔚来": "蔚来", "小鹏": "小鹏", "理想": "理想", "极氪": "极氪", "问界": "问界",
    "埃安": "埃安", "AION": "埃安", "零跑": "零跑", "哪吒": "哪吒", "岚图": "岚图",
    "深蓝": "深蓝", "智己": "智己", "阿维塔": "阿维塔", "腾势": "腾势", "五菱": "五菱",
    "欧拉": "欧拉", "几何": "几何", "银河": "吉利银河", "大众": "大众", "宝马": "宝马",
    "奔驰": "奔驰", "奥迪": "奥迪", "保时捷": "保时捷", "沃尔沃": "沃尔沃",
    # FUEL 阵营
    "丰田": "丰田", "本田": "本田", "日产": "日产", "马自达": "马自达", "三菱": "三菱",
    "别克": "别克", "雪佛兰": "雪佛兰", "福特": "福特", "凯迪拉克": "凯迪拉克",
    "现代": "现代", "起亚": "起亚", "吉利": "吉利", "长安": "长安", "长城": "长城",
    "哈弗": "哈弗", "奇瑞": "奇瑞", "红旗": "红旗", "荣威": "荣威", "名爵": "名爵",
    "传祺": "传祺", "领克": "领克", "标致": "标致", "雪铁龙": "雪铁龙", "斯柯达": "斯柯达",
    "雷克萨斯": "雷克萨斯", "捷豹": "捷豹", "路虎": "路虎", "Jeep": "Jeep",
}
# 长品牌名优先匹配，避免「吉利」抢占「吉利银河」
_BRANDS_SORTED = sorted(BRAND_ALIASES, key=len, reverse=True)

_BATTERY_MAP = [
    (("三元锂", "三元", "NCM", "NCA"), "三元锂"),
    (("磷酸铁锂", "铁锂", "LFP", "刀片"), "磷酸铁锂"),
    (("钴酸锂",), "钴酸锂"),
    (("锰酸锂",), "锰酸锂"),
    (("钠离子", "钠电"), "钠离子"),
]

_GEARBOX_MAP = [
    (("双离合", "DCT", "DSG", "PDK"), "DCT"),
    (("手自一体", "AT", "自动"), "AT"),
    (("CVT", "无级"), "CVT"),
    (("手动", "MT"), "MT"),
    (("单速", "固定齿比", "电动车单速"), "单速"),
    (("AMT",), "AMT"),
]

_EMISSION_LEVELS = ["国六B", "国六A", "国六", "国五", "国四", "国三", "国二", "国一"]

_RANGE_STANDARD = [("CLTC", "CLTC"), ("NEDC", "NEDC"), ("WLTP", "WLTP"), ("EPA", "EPA"),
                   ("综合续航", "综合"), ("工况", "综合")]


def is_null(value: object) -> bool:
    """判断字段是否为无效值（缺失 / 占位文案）。"""
    if value is None:
        return True
    if isinstance(value, str):
        return value.strip() in NULL_TOKENS
    return False


def clean_text(value: object) -> str | None:
    """去除多余空白与全角空格；无效值返回 None。"""
    if is_null(value):
        return None
    text = _WHITESPACE.sub(" ", str(value).replace("\u3000", " ")).strip()
    return text or None


def _to_decimal(raw: str) -> Decimal | None:
    try:
        return Decimal(raw).quantize(Decimal("0.01"))
    except (InvalidOperation, ValueError):
        return None


def parse_price_wan(value: object) -> Decimal | None:
    """价格标准化为「万元」。

    >>> parse_price_wan("15.98万")
    Decimal('15.98')
    >>> parse_price_wan("￥6.80万元") 
    Decimal('6.80')
    >>> parse_price_wan("面议") is None
    True
    """
    text = clean_text(value)
    if text is None:
        return None
    match = _PRICE.search(text.replace(",", ""))
    if not match:
        return None
    amount = _to_decimal(match.group(1))
    if amount is None:
        return None
    # 无「万」单位时按元处理，折算为万元
    if not match.group(2) and amount >= 1000:
        amount = (amount / Decimal(10_000)).quantize(Decimal("0.01"))
    return amount if amount > 0 else None


def parse_mileage_km(value: object) -> int | None:
    """里程标准化为「公里」。

    >>> parse_mileage_km("3.2万公里")
    32000
    >>> parse_mileage_km("5.8万km")
    58000
    >>> parse_mileage_km("未知") is None
    True
    """
    text = clean_text(value)
    if text is None:
        return None
    match = _MILEAGE.search(text.replace(",", ""))
    if not match:
        return None
    try:
        amount = float(match.group(1))
    except ValueError:
        return None
    if match.group(2):  # 带「万」
        amount *= 10_000
    if amount < 0:
        return None
    # 纯数字且过小（如 "3.2"）大概率是"万公里"漏单位，按业务常识保留原值
    return int(round(amount))


def parse_year_month(value: object) -> tuple[int | None, int | None]:
    """上牌时间标准化为 (年, 月)，月缺失时为 None。

    月份必须**紧跟在年份分隔符之后**（年 / - / / / .），否则会把
    「2021款 1.5L」里的排量误判成月份。

    >>> parse_year_month("2022年6月")
    (2022, 6)
    >>> parse_year_month("2021-03 上牌")
    (2021, 3)
    >>> parse_year_month("2021款 1.5L 自动")[1] is None
    True
    """
    text = clean_text(value)
    if text is None:
        return None, None
    year_match = re.search(r"(19|20)\d{2}", text)
    if not year_match:
        return None, None
    year = int(year_match.group(0))
    # 月份必须紧跟在「年 / - / / / .」分隔符之后，否则会把「2021款 1.5L」的排量误判成月份
    month_match = re.search(r"(19|20)\d{2}\s*[年\-/.]\s*(0?[1-9]|1[0-2])(?!\d)", text)
    month = int(month_match.group(2)) if month_match else None
    return year, month


def parse_int(value: object) -> int | None:
    """从文案中提取第一个整数，如「过户0次」-> 0。"""
    text = clean_text(value)
    if text is None:
        return None
    match = _NUMBER.search(text)
    if not match:
        return None
    try:
        return int(float(match.group(0)))
    except ValueError:
        return None


def parse_float(value: object) -> float | None:
    text = clean_text(value)
    if text is None:
        return None
    match = _NUMBER.search(text)
    if not match:
        return None
    try:
        return float(match.group(0))
    except ValueError:
        return None


def parse_percent(value: object) -> float | None:
    """提取百分比数值（如 SOH 91.5% -> 91.5），只允许 0~100。"""
    number = parse_float(value)
    if number is None or not 0 < number <= 100:
        return None
    return round(number, 1)


def parse_range(value: object) -> tuple[int | None, str | None]:
    """续航标准化为 (公里数, 工况口径)。

    >>> parse_range("CLTC续航605km")
    (605, 'CLTC')
    >>> parse_range("纯电续航 510 公里")
    (510, None)
    """
    text = clean_text(value)
    if text is None:
        return None, None
    km_match = re.search(r"(\d{2,4})\s*(?:km|KM|公里|千米)", text)
    km = int(km_match.group(1)) if km_match else None
    # 无单位时退化为取区间内合理数值
    if km is None:
        km = parse_int(text)
    standard = None
    upper = text.upper()
    for keyword, label in _RANGE_STANDARD:
        if keyword.upper() in upper:
            standard = label
            break
    if km is not None and not (30 <= km <= 2000):
        km = None
    return km, standard


def normalize_battery(value: object) -> str | None:
    """电池类型归一化。"""
    text = clean_text(value)
    if text is None:
        return None
    upper = text.upper()
    for keywords, label in _BATTERY_MAP:
        if any(k.upper() in upper for k in keywords):
            return label
    return "未知"


def normalize_gearbox(value: object) -> str | None:
    """变速箱归一化。"""
    text = clean_text(value)
    if text is None:
        return None
    upper = text.upper()
    for keywords, label in _GEARBOX_MAP:
        for keyword in keywords:
            if keyword.upper() in upper:
                return label
    return "未知"


def normalize_emission(value: object) -> str | None:
    """排放标准归一化（长串优先，避免「国六」误吃「国六B」）。"""
    text = clean_text(value)
    if text is None:
        return None
    compact = text.replace(" ", "").upper()
    for level in _EMISSION_LEVELS:
        if level.upper() in compact:
            return level
    return "未知"


def parse_displacement(value: object) -> float | None:
    """排量标准化为升（1.5T -> 1.5），过滤不合理值。"""
    text = clean_text(value)
    if text is None:
        return None
    match = re.search(r"(\d(?:\.\d)?)\s*(?:L|T|升|L升)?", text, re.IGNORECASE)
    if not match:
        return None
    try:
        litres = float(match.group(1))
    except ValueError:
        return None
    return litres if 0.6 <= litres <= 8.0 else None


def normalize_brand(value: object) -> str | None:
    """把源站给出的品牌名映射到标准品牌。

    详情页类数据源（如瓜子）已直接给出 ``brand`` 字段，比从标题里猜可靠得多。
    但仍要做一次归一，让「BYD」「比亚迪」这类写法落到同一个品牌上，
    否则看板的品牌排行会把同一品牌拆成两行。

    >>> normalize_brand("BYD")
    '比亚迪'
    >>> normalize_brand("东风本田")
    '东风本田'
    """
    text = clean_text(value)
    if text is None:
        return None
    for candidate in _BRANDS_SORTED:
        if text.upper().startswith(candidate.upper()):
            return BRAND_ALIASES[candidate]
    return text


def split_brand_model(title: object, *, brand_hint: object = None) -> tuple[str | None, str | None]:
    """从标题中切分品牌与车型。

    >>> split_brand_model("比亚迪 汉EV 2022款 605KM 尊享型")
    ('比亚迪', '汉EV')
    >>> split_brand_model("大众朗逸 2021款 1.5L 自动舒适版")
    ('大众', '朗逸')
    """
    text = clean_text(title)
    if text is None:
        return None, None

    hint = clean_text(brand_hint)
    if hint:
        normalized = BRAND_ALIASES.get(hint, brand_hint if isinstance(brand_hint, str) else None)
        remainder = text[len(hint):] if text.startswith(hint) else text
        return normalized, (clean_text(remainder) or None)

    # 标题前缀匹配品牌（长名优先）
    for candidate in _BRANDS_SORTED:
        if text.upper().startswith(candidate.upper()):
            brand = BRAND_ALIASES[candidate]
            remainder = text[len(candidate):].strip(" -·|　")
            model = _extract_model(remainder)
            return brand, model

    # 品牌名出现在标题中后段（如「汉EV 2022款 比亚迪」很少见，做兜底扫描）
    for candidate in _BRANDS_SORTED:
        if candidate.upper() in text.upper():
            remainder = text.replace(candidate, " ", 1).strip(" -·|　")
            return BRAND_ALIASES[candidate], _extract_model(remainder)

    return None, _extract_model(text)


_MODEL_CUT = re.compile(
    r"(?:19|20)\d{2}|款|新车|二手|准新|上牌|手动挡|自动挡|舒适版|豪华版|尊享版|精英版|旗舰版"
)


def _extract_model(remainder: str | None) -> str | None:
    """从剩余文案中截取车型名（去掉年款、配置、动力等噪音）。

    规则：在第一个"年款标记"处截断，再剥离动力/里程等修饰，最后统一去掉空白字符
    （跨平台比车时"汉 EV" 与 "汉EV" 必须归一）。
    """
    text = clean_text(remainder)
    if text is None:
        return None
    text = re.split(r"[|｜/]", text)[0].strip()
    match = _MODEL_CUT.search(text)
    if match and match.start() > 0:
        text = text[: match.start()]
    text = re.sub(r"\s+", "", text)
    text = re.sub(r"(新能源|纯电动|插电混动|标准续航后驱版)$", "", text)
    return clean_text(text) or None


def clean_city(value: object) -> str | None:
    """城市名归一（去「市」后缀）。

    >>> clean_city("广州市")
    '广州'
    """
    text = clean_text(value)
    if text is None:
        return None
    text = re.sub(r"[市区县]$", "", text)
    return text or None


def relative_url(base_url: str | None, href: object) -> str | None:
    """把相对链接补全为绝对链接。"""
    link = clean_text(href)
    if link is None:
        return None
    if link.startswith(("http://", "https://", "file://", "local://")):
        return link
    if not base_url:
        return link
    return f"{base_url.rstrip('/')}/{link.lstrip('/')}"


def safe_ratio(numerator: Decimal | float | None, denominator: Decimal | float | None) -> float | None:
    """保值率 = 现价 / 新车指导价，分母非法时返回 None。"""
    if numerator is None or denominator is None:
        return None
    try:
        num, den = float(numerator), float(denominator)
    except (TypeError, ValueError):
        return None
    if den <= 0 or num <= 0:
        return None
    return round(num / den, 4)


def sane_year(year: int | None) -> int | None:
    """校验年份合理区间（1990 ~ 当前年+1），防止脏数据污染统计。"""
    if year is None:
        return None
    upper = date.today().year + 1
    return year if 1990 <= year <= upper else None
