"""站点地图解析与抽样测试（完全离线）。"""

from __future__ import annotations

import pytest

from usedcar_crawler.errors import ParseError
from usedcar_crawler.sources.sitemap import (
    collect_detail_urls,
    detail_id,
    is_index,
    parse_sitemap,
    sample_evenly,
)

URLSET = """<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url><loc>https://www.guazi.com/car-detail/c1.md</loc><lastmod>2026-09-19</lastmod></url>
  <url><loc>https://www.guazi.com/car-detail/c2.md</loc><lastmod>2026-09-19</lastmod></url>
  <url><loc>https://www.guazi.com/car-detail/c3.md</loc><lastmod>2026-09-18</lastmod></url>
</urlset>
"""

INDEX = """<?xml version="1.0" encoding="UTF-8"?>
<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <sitemap><loc>https://www.guazi.com/guazisou/cardetail/pc_cardetail_md_1.xml</loc></sitemap>
  <sitemap><loc>https://www.guazi.com/guazisou/cardetail/pc_cardetail_md_2.xml</loc></sitemap>
</sitemapindex>
"""


class TestParseSitemap:
    def test_parses_urlset(self):
        assert parse_sitemap(URLSET) == [
            "https://www.guazi.com/car-detail/c1.md",
            "https://www.guazi.com/car-detail/c2.md",
            "https://www.guazi.com/car-detail/c3.md",
        ]

    def test_parses_sitemapindex(self):
        urls = parse_sitemap(INDEX)
        assert len(urls) == 2
        assert urls[0].endswith("pc_cardetail_md_1.xml")

    def test_index_detection(self):
        assert is_index(INDEX) is True
        assert is_index(URLSET) is False

    def test_bom_and_leading_comment_tolerated(self):
        messy = "\ufeff<!-- generated -->\n" + URLSET
        assert len(parse_sitemap(messy)) == 3

    def test_namespaceless_variant_supported(self):
        plain = "<urlset><url><loc>https://x/a.md</loc></url></urlset>"
        assert parse_sitemap(plain) == ["https://x/a.md"]

    @pytest.mark.parametrize("bad", ["", "   ", "<urlset></urlset>", "not xml at all"])
    def test_unparsable_input_raises(self, bad):
        with pytest.raises(ParseError):
            parse_sitemap(bad)

    def test_gzip_payload_supported(self):
        import gzip

        assert parse_sitemap(gzip.compress(URLSET.encode("utf-8"))) == parse_sitemap(URLSET)


class TestSampleEvenly:
    def test_returns_all_when_sample_ge_size(self):
        items = [str(i) for i in range(5)]
        assert sample_evenly(items, 99) == items

    def test_sample_size_honoured(self):
        items = [str(i) for i in range(1000)]
        assert len(sample_evenly(items, 10)) == 10

    def test_is_deterministic(self):
        items = [str(i) for i in range(1000)]
        assert sample_evenly(items, 25) == sample_evenly(items, 25)

    def test_spread_across_whole_range_not_just_head(self):
        """必须等距铺开，否则样本会严重偏向地图里排前的城市，得出错误行情结论。"""
        items = [str(i) for i in range(1000)]
        picked = [int(v) for v in sample_evenly(items, 10)]
        assert picked == sorted(picked)
        assert picked[0] < 100
        assert picked[-1] > 800
        # 相邻样本间距应大致均匀
        gaps = [b - a for a, b in zip(picked, picked[1:])]
        assert max(gaps) - min(gaps) <= 1

    def test_no_duplicates(self):
        items = [str(i) for i in range(37)]
        picked = sample_evenly(items, 9)
        assert len(set(picked)) == len(picked)


class TestCollectDetailUrls:
    def test_walks_two_levels_and_carries_lastmod(self):
        children = {
            "https://www.guazi.com/guazisou/cardetail/pc_cardetail_md_1.xml": URLSET,
            "https://www.guazi.com/guazisou/cardetail/pc_cardetail_md_2.xml": URLSET,
        }
        rows = collect_detail_urls(INDEX, lambda url: children[url])
        assert len(rows) == 6
        assert rows[0]["url"].endswith("c1.md")
        assert rows[0]["lastmod"] == "2026-09-19"
        assert rows[0]["child"].endswith("_1.xml")

    def test_child_limit_respected(self):
        children = {
            "https://www.guazi.com/guazisou/cardetail/pc_cardetail_md_1.xml": URLSET,
            "https://www.guazi.com/guazisou/cardetail/pc_cardetail_md_2.xml": URLSET,
        }
        rows = collect_detail_urls(INDEX, lambda url: children[url], child_limit=1)
        assert len(rows) == 3

    def test_total_failure_raises(self):
        with pytest.raises(ParseError):
            collect_detail_urls(INDEX, lambda url: "<urlset></urlset>")


class TestDetailId:
    @pytest.mark.parametrize(
        ("url", "expected"),
        [
            ("https://www.guazi.com/car-detail/c172686863204199.md", "c172686863204199"),
            ("https://www.guazi.com/car-detail/c172686863204199.html", "c172686863204199"),
            ("https://www.guazi.com/car-detail/c172686863204199", "c172686863204199"),
        ],
    )
    def test_extracts_platform_id(self, url, expected):
        assert detail_id(url) == expected
