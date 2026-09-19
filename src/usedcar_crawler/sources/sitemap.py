"""站点地图（sitemap）解析。

详情页型数据源的第一跳：`sitemap index -> 子地图 -> 详情页 URL`。
瓜子把车源详情页的 Markdown 版本编入
``/guazisou/cardetail/pc_cardetail_md_index.xml``，本模块负责把这条链路解开。

只做"从 XML 里取 URL"这一件事，不做网络请求——纯函数好测试，
也让"取到的 URL 是否被 robots 允许"这类合规判断留在取数层统一裁决。
"""

from __future__ import annotations

import gzip
import re
import xml.etree.ElementTree as ET
from urllib.parse import urlparse

from ..errors import ParseError

# 站点地图可能带命名空间（标准 sitemaps.org 命名空间），用通配匹配
_NS = "{http://www.sitemaps.org/schemas/sitemap/0.9}"
_LOC_ANY = re.compile(r"<loc>\s*(.*?)\s*</loc>", re.DOTALL)


def _decode(raw: bytes | str) -> str:
    """sitemap 偶有 gzip 或 BOM，统一成干净文本。"""
    if isinstance(raw, str):
        return raw
    if raw[:2] == b"\x1f\x8b":  # gzip 魔数
        return gzip.decompress(raw).decode("utf-8", errors="ignore")
    return raw.decode("utf-8", errors="ignore")


def _root(text: str) -> ET.Element:
    cleaned = text.lstrip("\ufeff").strip()
    if not cleaned:
        raise ParseError("站点地图内容为空")
    try:
        return ET.fromstring(cleaned)
    except ET.ParseError:
        # 极少数站点会在 XML 前加 BOM/注释，退回标签定位
        start = cleaned.find("<urlset")
        if start < 0:
            start = cleaned.find("<sitemapindex")
        if start < 0:
            raise ParseError("无法识别的站点地图格式（既不是 urlset 也不是 sitemapindex）") from None
        return ET.fromstring(cleaned[start:])


def _locs(root: ET.Element) -> list[str]:
    """从 urlset 或 sitemapindex 中抽取全部 <loc>。"""
    found: list[str] = []
    for tag in ("url", "sitemap"):
        for node in root.iter(f"{_NS}{tag}"):
            loc = node.find(f"{_NS}loc")
            if loc is not None and loc.text:
                found.append(loc.text.strip())
    if not found:  # 不带命名空间的变体
        for tag in ("url", "sitemap"):
            for node in root.iter(tag):
                loc = node.find("loc")
                if loc is not None and loc.text:
                    found.append(loc.text.strip())
    return found


def is_index(text: bytes | str) -> bool:
    """判断是否为「子地图索引」而非「URL 清单」。"""
    return "<sitemapindex" in _decode(text)[:2000]


def parse_sitemap(raw: bytes | str) -> list[str]:
    """解析站点地图，返回其中的 URL 列表。

    - 对 ``<sitemapindex>`` 返回的是**子地图**地址（还需再解一层）；
    - 对 ``<urlset>`` 返回的是**详情页**地址。

    调用方可以用 :func:`is_index` 区分两者，也可以用 :func:`collect_detail_urls`
    一次性走完两层。
    """
    text = _decode(raw)
    urls = _locs(_root(text))
    if not urls:
        # 保底：命名空间写法超出预期时用正则兜底，宁可拿到 URL 也不要静默返回空
        urls = [match.strip() for match in _LOC_ANY.findall(text)]
    if not urls:
        raise ParseError("站点地图中未解析到任何 URL")
    return urls


def collect_detail_urls(
    index_xml: bytes | str,
    fetch_child,
    *,
    child_limit: int | None = None,
) -> list[dict[str, str]]:
    """走完 `索引 -> 子地图 -> 详情页` 两层，返回 ``[{url, child, lastmod}]``。

    :param fetch_child: 取子地图的回调，签名 ``(url: str) -> bytes | str``
    :param child_limit: 最多解析几个子地图（None 表示全部）
    """
    children = parse_sitemap(index_xml)
    if child_limit is not None:
        children = children[:child_limit]

    rows: list[dict[str, str]] = []
    for child in children:
        text = _decode(fetch_child(child))
        try:
            root = _root(text)
        except ParseError:
            continue
        for node in root.iter(f"{_NS}url"):
            loc = node.find(f"{_NS}loc")
            if loc is None or not loc.text:
                continue
            lastmod = node.find(f"{_NS}lastmod")
            rows.append(
                {
                    "url": loc.text.strip(),
                    "child": child,
                    "lastmod": (lastmod.text or "").strip() if lastmod is not None else "",
                }
            )
    if not rows:
        raise ParseError("两层解析后仍未获得任何详情页 URL")
    return rows


def sample_evenly(items: list[str], size: int) -> list[str]:
    """等距抽样，保证样本在整张地图上均匀铺开且结果可复现。

    为什么要抽样而不是抓全量：单个子地图就有 1 万条车源，14 个子地图约 14 万条。
    对目标站点保持礼貌、且面试演示只需要一个**统计上可用**的样本，
    因此按固定步长等距取，而不是"随便抓前 N 条"——后者会让样本严重偏向
    地图里排在前面的城市，得出错误的行情结论。
    """
    if size <= 0 or size >= len(items):
        return list(items)
    step = len(items) / size
    return [items[int(index * step)] for index in range(size)]


def detail_id(url: str) -> str | None:
    """从详情页 URL 中取出平台车源 ID，如 ``c172686863204199``。"""
    path = urlparse(url).path.rstrip("/")
    name = path.rsplit("/", 1)[-1]
    name = re.sub(r"\.(md|html?)$", "", name, flags=re.IGNORECASE)
    return name or None
