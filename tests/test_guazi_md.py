"""瓜子详情页 Markdown 解析器测试（完全离线）。

这些用例锁住的是**业务口径**，不只是代码行为：

- 增程车的"综合续航"绝不能被当成纯电续航入库；
- HTTP 200 的验证页必须被识别为"没拿到数据"，而不是"字段缺失的合法页面"；
- 结构化字段（品牌/车系）优先于标题切分。

前两条如果失守，看板上的图会"看起来正常但结论错误"——这比报错难发现得多，
所以必须有测试兜住。
"""

from __future__ import annotations

import pytest

from usedcar_crawler.errors import ParseError
from usedcar_crawler.parsers import get_parser
from usedcar_crawler.parsers.guazi_md import (
    build_raw_record,
    extract_features,
    looks_like_detail_md,
    parse_detail_md,
    parse_range_from_text,
    resolve_line,
    validate_record_shape,
)

EV_SAMPLE = "guazi_md/detail_ev_enhanced.md"
FUEL_SAMPLE = "guazi_md/detail_fuel_sample.md"

# 风控页的真实特征：HTTP 200 + 一个 <div id="app"> 的 JS 空壳
CHALLENGE_PAGE = (
    '<!DOCTYPE html><html lang="en"><head><meta charset="UTF-8" />'
    '<title>瓜子二手车</title></head><body><div id="app"></div>'
    '<script type="module" src="https://sta.guazistatic.com/guazi-mall-ucenter/static/index.js"></script>'
    "</body></html>"
)


@pytest.fixture
def read_fixture(fixtures_dir):
    def _read(name: str) -> str:
        return (fixtures_dir / name).read_text(encoding="utf-8")

    return _read


# --------------------------------------------------------------------------- #
# 内容契约校验
# --------------------------------------------------------------------------- #
class TestContentContract:
    def test_real_detail_page_passes(self, read_fixture):
        assert looks_like_detail_md(read_fixture(EV_SAMPLE)) is True
        assert looks_like_detail_md(read_fixture(FUEL_SAMPLE)) is True

    def test_challenge_page_is_rejected(self):
        """最关键的一条：验证页是 HTTP 200，不能当成'解析不到字段的合法页面'。"""
        assert looks_like_detail_md(CHALLENGE_PAGE) is False

    @pytest.mark.parametrize("body", [None, "", "   ", "---\nshort\n---\n"])
    def test_empty_or_tiny_body_rejected(self, body):
        assert looks_like_detail_md(body) is False

    def test_html_page_without_frontmatter_rejected(self):
        assert looks_like_detail_md("<html><body>some content " + "x" * 300 + "</body></html>") is False

    def test_markdown_without_price_key_rejected(self):
        """结构变了（缺少价格键）要判为不可用，而不是带着空价格入库。"""
        body = "---\ntitle: x\n---\n" + "vehicle_core:\nid:c1\n" + "y" * 300
        assert looks_like_detail_md(body) is False


# --------------------------------------------------------------------------- #
# 解析
# --------------------------------------------------------------------------- #
class TestParseDetail:
    def test_ev_sample_fields(self, read_fixture):
        doc = parse_detail_md(read_fixture(EV_SAMPLE), "https://www.guazi.com/car-detail/c1.md")
        assert doc.get("brand") == "鸿蒙智行"
        assert doc.get("series") == "问界M7"
        assert doc.get("full_payment") == "284700元"
        assert doc.get("guide_price") == "339800元"
        assert doc.get("first_register") == "2025-09"
        assert doc.get("mileage") == "5300公里"
        assert doc.get("city") == "苏州"
        assert doc.get("transfer_times") == "0次"

    def test_section_scoped_keys_disambiguate_type(self, read_fixture):
        """段落限定键：`energy` 段下的 `type` 才是能源类型。"""
        doc = parse_detail_md(read_fixture(EV_SAMPLE), "https://x/c1.md")
        assert doc.get("energy.type") == "新能源"
        assert resolve_line(doc) == "ev"

    def test_fuel_sample_fields(self, read_fixture):
        doc = parse_detail_md(read_fixture(FUEL_SAMPLE), "https://x/c2.md")
        assert doc.get("brand") == "本田"
        assert doc.get("series") == "英仕派"
        assert doc.get("emission_standard") == "国五"
        assert doc.get("engine") == "1.5T"
        assert resolve_line(doc) == "fuel"

    def test_dash_placeholder_treated_as_missing(self, read_fixture):
        """`production_type:-` 这类占位符必须当作缺失，不能当成有效值。"""
        doc = parse_detail_md(read_fixture(EV_SAMPLE), "https://x/c1.md")
        assert doc.get("production_type") is None

    def test_missing_frontmatter_raises(self):
        with pytest.raises(ParseError):
            parse_detail_md("no frontmatter here at all", "https://x/c1.md")

    def test_installment_section_does_not_pollute_fields(self, read_fixture):
        """分期方案的 `36期/48期` 等噪音不能进入字段表。"""
        doc = parse_detail_md(read_fixture(EV_SAMPLE), "https://x/c1.md")
        assert not any("期" in key for key in doc.fields)


# --------------------------------------------------------------------------- #
# 续航口径（业务红线）
# --------------------------------------------------------------------------- #
class TestRangeParsing:
    def test_extended_range_plug_in_hybrid_combined_range_is_dropped(self):
        """增程车的'综合续航'不是纯电续航——宁可缺失，不可给错值。"""
        text = "选装科技舒享包，CLTC综合续航1625km，鸿蒙座舱可见即可说"
        assert parse_range_from_text(text) == (None, None)

    def test_labelled_pure_electric_range_is_kept(self):
        assert parse_range_from_text("纯电续航 605 公里") == (605, "纯电")

    def test_cltc_range_on_pure_ev_is_kept_with_standard(self):
        assert parse_range_from_text("CLTC续航605km") == (605, "CLTC")

    def test_extended_car_with_explicit_pure_electric_range_keeps_it(self):
        km, standard = parse_range_from_text("增程版，纯电续航215公里，综合续航1200km")
        assert km == 215

    def test_out_of_range_value_is_rejected(self):
        assert parse_range_from_text("续航9999公里") == (None, None)

    def test_ev_sample_has_no_range(self, read_fixture):
        """回归：问界 M7 增程版只有综合续航，range_km 必须为空。"""
        doc = parse_detail_md(read_fixture(EV_SAMPLE), "https://x/c1.md")
        assert extract_features(doc)["range_raw"] is None

    # ---- 前缀式语序（数字在"续航"之前）------------------------------------ #
    # 瓜子的文案两种语序混用：只认后缀式会白白丢掉一半续航样本。
    def test_prefix_plain_range_is_kept(self):
        assert parse_range_from_text("610km续航，通勤够用") == (610, None)

    def test_prefix_labelled_range_reports_pure_electric(self):
        assert parse_range_from_text("215km纯电续航日常通勤一周一充") == (215, "纯电")

    def test_prefix_combined_range_is_dropped(self):
        """「1300km综合续航」是满油满电，同样不能当纯电续航。"""
        assert parse_range_from_text("210km纯电续航+1300km综合续航") == (210, "纯电")
        assert parse_range_from_text("1370km综合续航，一箱油跑到底") == (None, None)

    def test_model_name_with_changxuhang_is_not_a_range(self):
        """「长续航后驱版」是车型名，不是续航数字。"""
        assert parse_range_from_text("Model Y 2026款 长续航全轮驱动版") == (None, None)


# --------------------------------------------------------------------------- #
# 特征提取
# --------------------------------------------------------------------------- #
class TestFeatures:
    def test_battery_health_extracted_from_prose(self, read_fixture):
        doc = parse_detail_md(read_fixture(EV_SAMPLE), "https://x/c1.md")
        assert extract_features(doc)["battery_health_raw"] == "94%"

    def test_fuel_sample_has_no_battery_health(self, read_fixture):
        doc = parse_detail_md(read_fixture(FUEL_SAMPLE), "https://x/c2.md")
        assert extract_features(doc)["battery_health_raw"] is None


# --------------------------------------------------------------------------- #
# 原始记录契约
# --------------------------------------------------------------------------- #
class TestRawRecord:
    def test_record_shape_matches_list_page_contract(self, read_fixture):
        """详情页记录必须与列表页解析器同构，否则清洗/入库层无法复用。"""
        doc = parse_detail_md(read_fixture(EV_SAMPLE), "https://www.guazi.com/car-detail/c172686863204199.md")
        record = build_raw_record(doc, source_platform="guazi_md", raw_ref="data/raw/x.md.gz")
        assert record["_line"] == "ev"
        assert record["source_id"] == "c172686863204199"
        assert record["source_platform"] == "guazi_md"
        assert record["vehicle_key"].startswith("guazi_md_")
        assert record["raw_ref"] == "data/raw/x.md.gz"
        # 详情页独有的结构化字段
        assert record["brand_raw"] == "鸿蒙智行"
        assert record["model_raw"] == "问界M7"
        assert record["detail_url"].endswith(".html")

    def test_vehicle_key_is_stable_across_captures(self, read_fixture):
        """同一车源两次抓取必须得到同一个主键，这是幂等更新的前提。"""
        text = read_fixture(EV_SAMPLE)
        url = "https://www.guazi.com/car-detail/c172686863204199.md"
        key1 = build_raw_record(parse_detail_md(text, url), source_platform="guazi_md")["vehicle_key"]
        key2 = build_raw_record(parse_detail_md(text, url), source_platform="guazi_md")["vehicle_key"]
        assert key1 == key2

    def test_validate_shape_rejects_incomplete_record(self):
        with pytest.raises(ParseError):
            validate_record_shape({"vehicle_key": "k", "source_platform": "p"})


# --------------------------------------------------------------------------- #
# 端到端：原始记录 -> 领域模型
# --------------------------------------------------------------------------- #
class TestToModel:
    def test_ev_record_cleans_correctly(self, read_fixture):
        doc = parse_detail_md(read_fixture(EV_SAMPLE), "https://www.guazi.com/car-detail/c172686863204199.md")
        record = build_raw_record(doc, source_platform="guazi_md")
        model = get_parser("ev").to_model(record)

        assert model.brand == "鸿蒙智行"
        assert model.model == "问界M7"
        assert float(model.price_wan) == 28.47
        assert float(model.new_car_price_wan) == 33.98
        assert model.mileage_km == 5300
        assert (model.reg_year, model.reg_month) == (2025, 9)
        assert model.transfer_count == 0
        assert model.location_city == "苏州"
        assert model.battery_health == 94.0
        assert model.range_km is None          # 综合续航已按口径剔除
        assert 0.83 < model.retention_rate < 0.84

    def test_fuel_record_cleans_correctly(self, read_fixture):
        doc = parse_detail_md(read_fixture(FUEL_SAMPLE), "https://www.guazi.com/car-detail/c172523414170890.md")
        record = build_raw_record(doc, source_platform="guazi_md")
        model = get_parser("fuel").to_model(record)

        assert model.brand == "本田"
        assert model.model == "英仕派"
        assert float(model.price_wan) == 7.02
        assert model.mileage_km == 111500
        assert (model.reg_year, model.reg_month) == (2019, 8)
        assert model.transfer_count == 1
        assert model.displacement_l == 1.5
        assert model.gearbox == "AT"
        assert model.emission_standard == "国五"

    def test_brand_normalization_applies(self, read_fixture):
        """源站品牌名要过一遍归一，避免同一品牌在看板上被拆成两行。"""
        doc = parse_detail_md(read_fixture(FUEL_SAMPLE), "https://x/c2.md")
        doc.fields["brand"] = "BYD"
        record = build_raw_record(doc, source_platform="guazi_md")
        assert get_parser("fuel").to_model(record).brand == "比亚迪"
