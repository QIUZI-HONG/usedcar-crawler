"""去重与归并测试：幂等性与跨源归并的边界。"""

from __future__ import annotations

from usedcar_crawler.dedupe import dedupe_by_key, make_match_key, make_vehicle_key, split_upsert


class TestVehicleKey:
    def test_same_source_id_produces_same_key(self) -> None:
        assert make_vehicle_key("guazi_fuel", "200001") == make_vehicle_key("guazi_fuel", "200001")

    def test_different_platform_never_collides(self) -> None:
        """同一车源 ID 在不同平台必须产生不同键，否则会互相覆盖。"""
        assert make_vehicle_key("guazi_fuel", "200001") != make_vehicle_key("autohome_fuel", "200001")

    def test_fallback_when_source_id_missing(self) -> None:
        key = make_vehicle_key("x", None, fallback="宝马 3系|23.99万|4.1万公里")
        assert key.startswith("x_")
        assert key == make_vehicle_key("x", None, fallback="宝马 3系|23.99万|4.1万公里")

    def test_key_prefix_identifies_platform(self) -> None:
        assert make_vehicle_key("dongchedi_ev", "9").startswith("dongched")


class TestMatchKey:
    def test_match_key_groups_same_car_across_platforms(self) -> None:
        left = make_match_key(brand="大众", model="朗逸", reg_year=2021, mileage_km=58_000, reg_month=3)
        right = make_match_key(brand="大众", model="朗逸", reg_year=2021, mileage_km=57_200, reg_month=3)
        assert left == right  # 同一里程分段内视为同一台车

    def test_match_key_separates_different_mileage_bucket(self) -> None:
        left = make_match_key(brand="大众", model="朗逸", reg_year=2021, mileage_km=58_000)
        right = make_match_key(brand="大众", model="朗逸", reg_year=2021, mileage_km=95_000)
        assert left != right

    def test_match_key_requires_core_dimensions(self) -> None:
        """核心维度缺失时宁可不归并，也不做错误的跨源匹配。"""
        assert make_match_key(brand=None, model="朗逸", reg_year=2021, mileage_km=1000) is None
        assert make_match_key(brand="大众", model="朗逸", reg_year=None, mileage_km=1000) is None


class TestBatchDedupe:
    def test_duplicate_keeps_last_occurrence(self) -> None:
        records = [
            {"vehicle_key": "a", "price_wan": 10},
            {"vehicle_key": "b", "price_wan": 20},
            {"vehicle_key": "a", "price_wan": 11},
        ]
        deduped, dropped = dedupe_by_key(records)
        assert dropped == 1
        assert len(deduped) == 2
        assert next(r for r in deduped if r["vehicle_key"] == "a")["price_wan"] == 11

    def test_records_without_key_are_dropped(self) -> None:
        deduped, dropped = dedupe_by_key([{"vehicle_key": None}, {"vehicle_key": "a"}])
        assert dropped == 1
        assert len(deduped) == 1

    def test_empty_input(self) -> None:
        assert dedupe_by_key([]) == ([], 0)


class TestSplitUpsert:
    def test_split(self) -> None:
        candidates = [{"vehicle_key": "a"}, {"vehicle_key": "b"}, {"vehicle_key": "c"}]
        to_insert, to_update = split_upsert(candidates, existing_keys={"a", "c"})
        assert [r["vehicle_key"] for r in to_insert] == ["b"]
        assert {r["vehicle_key"] for r in to_update} == {"a", "c"}
