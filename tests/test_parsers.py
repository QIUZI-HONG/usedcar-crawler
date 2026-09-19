"""解析层测试：两条业务线的字段提取、脏数据处理、站点改版检测。"""

from __future__ import annotations

import pytest

from usedcar_crawler.errors import ParseError, SchemaDriftError
from usedcar_crawler.parsers import get_parser

SYNTHETIC_TEMPLATE = """
<div class="car-list">
  <div class="car-item">
    <h3 class="title">{title}</h3>
    <span class="price">{price}</span>
    <span class="reg-date">{reg}</span>
    {mileage}
  </div>
</div>
"""


def _spec(registry, key: str):
    return registry.get(key)


class TestEVParser:
    def test_parse_fixture(self, registry, fixture_html):
        spec = _spec(registry, "local_fixture_ev")
        html = fixture_html("ev_list.html")
        outcome = get_parser("ev").parse(html, spec, "local://fixtures/ev_list.html")

        assert outcome.cards_found == 4
        assert len(outcome.records) == 4
        # 4 张卡片中 1 张价格「面议」、1 张缺里程 -> 必需字段缺失率 25%，未触发改版
        assert not outcome.has_drift

        by_title = {record["title_raw"]: record for record in outcome.records}
        parser = get_parser("ev")

        model = parser.to_model(by_title["比亚迪 汉EV 2022款 605KM 尊享型"])
        assert model.brand == "比亚迪"
        assert model.model == "汉EV"
        assert float(model.price_wan) == 15.98
        assert float(model.new_car_price_wan) == 20.98
        assert model.retention_rate == 0.7617
        assert model.mileage_km == 32000
        assert (model.reg_year, model.reg_month) == (2022, 6)
        assert model.battery_type == "三元锂"
        assert model.range_km == 605
        assert model.range_standard == "CLTC"
        assert model.battery_health == 91.5
        assert model.fast_charge_kw == 110.0
        assert model.location_city == "广州"
        assert model.detail_url == "https://fixture.local/usedcar/100001.html"
        assert model.source_id == "100001"

    def test_price_negotiable_is_rejected_by_model(self, registry, fixture_html):
        """「面议」不是有效价格：必须在入库前被拦下，而不是存成 0。"""
        spec = _spec(registry, "local_fixture_ev")
        outcome = get_parser("ev").parse(fixture_html("ev_list.html"), spec, "local://x")
        record = next(r for r in outcome.records if r["title_raw"].startswith("特斯拉"))
        with pytest.raises(Exception) as exc_info:
            get_parser("ev").to_model(record)
        assert "price_wan" in str(exc_info.value)

    def test_missing_mileage_stays_none(self, registry, fixture_html):
        spec = _spec(registry, "local_fixture_ev")
        outcome = get_parser("ev").parse(fixture_html("ev_list.html"), spec, "local://x")
        record = next(r for r in outcome.records if r["title_raw"].startswith("小鹏"))
        model = get_parser("ev").to_model(record)
        assert model.mileage_km is None

    def test_vehicle_key_is_stable_and_platform_scoped(self, registry, fixture_html):
        spec = _spec(registry, "local_fixture_ev")
        parser = get_parser("ev")
        first = parser.parse(fixture_html("ev_list.html"), spec, "local://x")
        second = parser.parse(fixture_html("ev_list.html"), spec, "local://x")
        assert [r["vehicle_key"] for r in first.records] == [r["vehicle_key"] for r in second.records]
        assert all(key.startswith("local_fi") for key in (r["vehicle_key"] for r in first.records))


class TestFuelParser:
    def test_parse_fixture(self, registry, fixture_html):
        spec = _spec(registry, "local_fixture_fuel")
        outcome = get_parser("fuel").parse(fixture_html("fuel_list.html"), spec, "local://x")
        assert outcome.cards_found == 4
        assert not outcome.has_drift

        parser = get_parser("fuel")
        records = {record["title_raw"]: record for record in outcome.records}

        lavida = parser.to_model(records["大众 朗逸 2021款 1.5L 自动舒适版"])
        assert (lavida.brand, lavida.model) == ("大众", "朗逸")
        assert float(lavida.price_wan) == 6.80
        assert lavida.mileage_km == 58000
        assert (lavida.reg_year, lavida.reg_month) == (2021, 3)
        assert lavida.displacement_l == 1.5
        assert lavida.gearbox == "AT"
        assert lavida.emission_standard == "国六"
        assert lavida.location_city == "广州"

        camry = parser.to_model(records["丰田 凯美瑞 2019款 2.0G 豪华版"])
        assert camry.gearbox == "CVT"
        assert camry.emission_standard == "国六B"
        assert camry.displacement_l == 2.0

        bmw = parser.to_model(records["宝马 3系 2020款 325Li M运动套装"])
        assert bmw.gearbox == "AT"
        assert bmw.emission_standard == "国六A"

    def test_missing_price_is_rejected(self, registry, fixture_html):
        spec = _spec(registry, "local_fixture_fuel")
        outcome = get_parser("fuel").parse(fixture_html("fuel_list.html"), spec, "local://x")
        record = next(r for r in outcome.records if r["title_raw"].startswith("本田"))
        with pytest.raises(Exception):
            get_parser("fuel").to_model(record)


class TestStructuralChange:
    def test_container_selector_miss_raises_parse_error(self, registry):
        """站点改版导致容器选择器失效时，必须显式报错，而不是静默返回 0 条。"""
        spec = _spec(registry, "local_fixture_ev")
        html = "<html><body><div class='brand-new-layout'><p>改版后的结构</p></div></body></html>"
        with pytest.raises(ParseError) as exc_info:
            get_parser("ev").parse(html, spec, "local://x")
        assert "零命中" in str(exc_info.value)

    def test_required_field_drift_is_detected_and_alerts(self, registry):
        """必需字段（里程）选择器失效：缺失率 100% -> 判定改版并抛出 SchemaDriftError。"""
        spec = _spec(registry, "local_fixture_ev")
        html = "<div class='car-list'>" + "".join(
            SYNTHETIC_TEMPLATE.format(
                title=f"比亚迪 汉EV 2022款 60{i}KM",
                price=f"1{i}.98万",
                reg="2022年6月",
                mileage="",  # 结构变化：里程节点整块消失
            )
            for i in range(4)
        ) + "</div>"

        parser = get_parser("ev")
        outcome = parser.parse(html, spec, "local://x")
        assert outcome.has_drift
        assert "mileage" in outcome.drift_fields
        assert outcome.field_missing["mileage"] == 1.0
        assert outcome.worst_field_ratio >= 0.3
        with pytest.raises(SchemaDriftError):
            parser.ensure_quality(outcome, spec)

    def test_partial_drift_below_threshold_is_tolerated(self, registry):
        """个别卡片缺字段属正常噪声，不应误报改版。"""
        spec = _spec(registry, "local_fixture_ev")
        cards = [
            SYNTHETIC_TEMPLATE.format(title=f"比亚迪 汉EV 2022款 60{i}KM", price=f"1{i}.98万",
                                      reg="2022年6月", mileage='<span class="mileage">3.2万公里</span>')
            for i in range(9)
        ]
        cards.append(SYNTHETIC_TEMPLATE.format(title="蔚来 ES6 2022款", price="26.5万",
                                               reg="2022年9月", mileage=""))
        outcome = get_parser("ev").parse("<div class='car-list'>" + "".join(cards) + "</div>", spec, "local://x")
        assert not outcome.has_drift
        assert outcome.missing_ratio < 0.3
