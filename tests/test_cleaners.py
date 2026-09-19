"""字段清洗与标准化的单元测试。

这些是本项目最该被测透的函数：爬虫的 bug 大多不是"抓不到"，
而是"抓到了但数字是错的"（价格少个零、里程把万当成个）。
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from usedcar_crawler.pipeline import cleaners as cl


class TestPrice:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("15.98万", Decimal("15.98")),
            ("￥15.98万", Decimal("15.98")),
            ("15.98 万元", Decimal("15.98")),
            ("<em>15.98</em>万", Decimal("15.98")),
            ("15.98-16.50万", Decimal("15.98")),
            ("1,258,000", Decimal("125.80")),
            ("6.8万", Decimal("6.80")),
        ],
    )
    def test_parse_price_wan(self, raw: str, expected: Decimal) -> None:
        assert cl.parse_price_wan(raw) == expected

    @pytest.mark.parametrize("raw", ["面议", "暂无", "", None, "-", "待定", "abc"])
    def test_invalid_price_returns_none(self, raw) -> None:
        assert cl.parse_price_wan(raw) is None

    def test_zero_price_is_invalid(self) -> None:
        assert cl.parse_price_wan("0万") is None


class TestMileage:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [("3.2万公里", 32000), ("5.8万km", 58000), ("58000公里", 58000), ("0.8万公里", 8000), ("1.2万", 12000)],
    )
    def test_parse_mileage_km(self, raw: str, expected: int) -> None:
        assert cl.parse_mileage_km(raw) == expected

    def test_missing_mileage_is_none_not_zero(self) -> None:
        """缺失必须是 None：把缺失写成 0 会让"零公里准新车"污染统计。"""
        assert cl.parse_mileage_km("未知") is None
        assert cl.parse_mileage_km(None) is None


class TestDate:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("2022年6月", (2022, 6)),
            ("2021-03 上牌", (2021, 3)),
            ("2022/09", (2022, 9)),
            ("2023年1月上牌", (2023, 1)),
            ("2019款 豪华版", (2019, None)),
        ],
    )
    def test_parse_year_month(self, raw: str, expected: tuple) -> None:
        assert cl.parse_year_month(raw) == expected

    def test_sane_year_filters_dirty_data(self) -> None:
        assert cl.sane_year(1985) is None
        assert cl.sane_year(2200) is None
        assert cl.sane_year(2022) == 2022


class TestBrandModel:
    @pytest.mark.parametrize(
        ("title", "brand", "model"),
        [
            ("比亚迪 汉EV 2022款 605KM 尊享型", "比亚迪", "汉EV"),
            ("特斯拉 Model 3 2021标准续航后驱版", "特斯拉", "Model3"),
            ("大众 朗逸 2021款 1.5L 自动舒适版", "大众", "朗逸"),
            ("大众朗逸 2021款 1.5L 自动舒适版", "大众", "朗逸"),
            ("丰田 凯美瑞 2019款 2.0G 豪华版", "丰田", "凯美瑞"),
            ("宝马 3系 2020款 325Li M运动套装", "宝马", "3系"),
        ],
    )
    def test_split_brand_model(self, title: str, brand: str, model: str) -> None:
        assert cl.split_brand_model(title) == (brand, model)

    def test_unknown_brand_returns_none(self) -> None:
        brand, model = cl.split_brand_model("某小众品牌 X1 2020款")
        assert brand is None
        assert model is not None


class TestEVFields:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [("三元锂电池", "三元锂"), ("磷酸铁锂电池", "磷酸铁锂"), ("刀片电池", "磷酸铁锂"),
         ("钠离子电池", "钠离子"), ("NCM811", "三元锂"), ("未知型号", "未知")],
    )
    def test_normalize_battery(self, raw: str, expected: str) -> None:
        assert cl.normalize_battery(raw) == expected

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [("续航 CLTC 605km", (605, "CLTC")), ("NEDC续航 468公里", (468, "NEDC")),
         ("续航 500km", (500, None)), ("CLTC 706 公里", (706, "CLTC")),
         ("续航面议", (None, None))],
    )
    def test_parse_range(self, raw: str, expected: tuple) -> None:
        assert cl.parse_range(raw) == expected

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [("电池健康度 91.5%", 91.5), ("SOH 88%", 88.0), ("120%", None), ("暂无", None)],
    )
    def test_parse_percent(self, raw, expected) -> None:
        assert cl.parse_percent(raw) == expected


class TestFuelFields:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [("1.5L", 1.5), ("2.0T", 2.0), ("1.5T", 1.5), ("3.0升", 3.0), ("0.1L", None), ("未知", None)],
    )
    def test_parse_displacement(self, raw, expected) -> None:
        assert cl.parse_displacement(raw) == expected

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [("自动挡", "AT"), ("CVT无级变速", "CVT"), ("手自一体", "AT"), ("手动", "MT"),
         ("双离合", "DCT"), ("电动车单速", "单速"), ("外星科技", "未知")],
    )
    def test_normalize_gearbox(self, raw: str, expected: str) -> None:
        assert cl.normalize_gearbox(raw) == expected

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [("国六B", "国六B"), ("国六", "国六"), ("国五", "国五"), ("国六A", "国六A"), ("国七", "未知")],
    )
    def test_normalize_emission_longest_first(self, raw: str, expected: str) -> None:
        """长串优先：'国六B' 不能被 '国六' 抢先匹配。"""
        assert cl.normalize_emission(raw) == expected


class TestMisc:
    def test_clean_city(self) -> None:
        assert cl.clean_city("广州市") == "广州"
        assert cl.clean_city("深圳市") == "深圳"
        assert cl.clean_city("") is None

    def test_relative_url(self) -> None:
        assert cl.relative_url("https://a.com", "/x/1.html") == "https://a.com/x/1.html"
        assert cl.relative_url("https://a.com/", "https://b.com/1") == "https://b.com/1"
        assert cl.relative_url(None, None) is None

    def test_safe_ratio(self) -> None:
        assert cl.safe_ratio(Decimal("15.98"), Decimal("20.98")) == 0.7617
        assert cl.safe_ratio(10, 0) is None
        assert cl.safe_ratio(None, 10) is None

    def test_clean_text_collapses_whitespace(self) -> None:
        assert cl.clean_text("  比亚  迪\n 汉EV ") == "比亚 迪 汉EV"
        assert cl.clean_text("　") is None
