"""存储层测试：幂等 UPSERT、脏数据拒收、下架追踪、查询与统计。"""

from __future__ import annotations

from datetime import datetime

import pytest

from usedcar_crawler.config import StorageCfg, get_settings
from usedcar_crawler.pipeline import cleaners as cl
from usedcar_crawler.storage.repository import Repository


def _record(title: str, source_id: str, price: str, mileage: str = "3.2万公里", *, source="local_fixture_ev"):
    """构造解析层风格的原始字典（模拟真实解析产物）。"""
    price_value = cl.parse_price_wan(price)
    return {
        "vehicle_key": f"testkey_{source_id}",
        "source_platform": source,
        "source_id": source_id,
        "title_raw": title,
        "price_raw": price,
        "new_car_price_raw": "20.98万",
        "reg_date_raw": "2022年6月",
        "mileage_raw": mileage,
        "city_raw": "广州市",
        "detail_url": f"https://fixture.local/usedcar/{source_id}.html",
        "battery_raw": "三元锂",
        "range_raw": "CLTC 605km",
        "battery_health_raw": "91.5%",
        "fast_charge_raw": "110kW",
        "captured_at": datetime.now(),
        "_expected_price": price_value,
    }


class TestUpsert:
    def test_first_insert(self, repo: Repository) -> None:
        result = repo.upsert_vehicles("ev", [
            _record("比亚迪 汉EV 2022款", "1", "15.98万"),
            _record("小鹏 P7 2023款", "2", "18.80万"),
        ])
        assert (result.inserted, result.updated, result.rejected) == (2, 0, 0)

    def test_second_run_updates_not_inserts(self, repo: Repository) -> None:
        """幂等性：重复抓取只更新，绝不重复插入（否则报表会翻倍）。"""
        repo.upsert_vehicles("ev", [_record("比亚迪 汉EV 2022款", "1", "15.98万")])
        second = repo.upsert_vehicles("ev", [_record("比亚迪 汉EV 2022款", "1", "15.20万")])

        assert (second.inserted, second.updated) == (0, 1)
        rows = repo.query("ev")
        assert len(rows) == 1
        assert float(rows[0]["price_wan"]) == 15.20  # 价格被更新为最新值

    def test_invalid_price_is_rejected_not_stored(self, repo: Repository) -> None:
        result = repo.upsert_vehicles("ev", [
            _record("比亚迪 汉EV 2022款", "1", "15.98万"),
            _record("特斯拉 Model 3", "2", "面议"),
        ])
        assert result.rejected == 1
        assert result.inserted == 1
        assert len(repo.query("ev")) == 1

    def test_reject_samples_are_recorded(self, repo: Repository) -> None:
        result = repo.upsert_vehicles("ev", [_record("特斯拉 Model 3", "2", "面议")])
        assert result.reject_samples and "price_wan" in result.reject_samples[0]

    def test_derived_columns_are_filled(self, repo: Repository) -> None:
        repo.upsert_vehicles("ev", [_record("比亚迪 汉EV 2022款", "1", "15.98万")])
        row = repo.query("ev")[0]
        assert float(row["retention_rate"]) == 0.7617
        assert row["match_key"] == "比亚迪|汉EV|2022|06|6"
        assert row["missing_count"] == 0
        assert bool(row["is_deleted"]) is False

    def test_empty_input_is_noop(self, repo: Repository) -> None:
        result = repo.upsert_vehicles("ev", [])
        assert result.total == 0

    def test_unknown_line_raises(self, repo: Repository) -> None:
        from usedcar_crawler.errors import StorageError

        with pytest.raises(StorageError):
            repo.upsert_vehicles("truck", [])


class TestMissingTracking:
    def test_unseen_rows_are_flagged_then_marked_deleted(self) -> None:
        """本轮未出现的车源：先累计未见次数，达到阈值再标记下架。"""
        settings = get_settings().model_copy(
            update={"storage": StorageCfg(batch_size=100, mark_missing_deleted_after=1)}
        )
        from usedcar_crawler.storage.repository import build_engine

        engine = build_engine(settings.database.model_copy(update={"url": "sqlite:///:memory:"}))
        repo = Repository(engine, settings=settings)
        repo.init_schema()

        first = _record("比亚迪 汉EV 2022款", "1", "15.98万")
        stale = _record("小鹏 P7 2023款", "2", "18.80万")
        # 第一次：两条都在
        repo.upsert_vehicles("ev", [first, stale])
        # 把 stale 的 last_seen_at 回拨，模拟"上一轮抓到的旧数据"
        with engine.begin() as conn:
            from usedcar_crawler.storage.repository import ev_vehicles

            conn.execute(
                ev_vehicles.update()
                .where(ev_vehicles.c.vehicle_key == stale["vehicle_key"])
                .values(last_seen_at=datetime(2000, 1, 1))
            )
        # 第二次：只抓到 first
        repo.upsert_vehicles("ev", [first])

        stale_row = next(r for r in repo.query("ev", include_deleted=True) if r["source_id"] == "2")
        fresh_row = next(r for r in repo.query("ev", include_deleted=True) if r["source_id"] == "1")
        assert stale_row["missing_count"] >= 1
        assert bool(stale_row["is_deleted"]) is True
        # 本轮抓到的数据绝不能被误伤
        assert bool(fresh_row["is_deleted"]) is False
        assert fresh_row["missing_count"] == 0
        assert len(repo.query("ev")) == 1  # 默认过滤已下架


class TestQueryAndStats:
    @pytest.fixture(autouse=True)
    def _seed(self, repo: Repository):
        repo.upsert_vehicles("ev", [
            _record("比亚迪 汉EV 2022款", "1", "15.98万"),
            _record("小鹏 P7 2023款", "2", "18.80万"),
            _record("蔚来 ES6 2022款", "3", "26.50万"),
        ])
        self.repo = repo

    def test_query_with_filter(self) -> None:
        rows = self.repo.query("ev", brand="比亚迪")
        assert len(rows) == 1
        assert rows[0]["model"] == "汉EV"

    def test_query_with_range_filter(self) -> None:
        rows = self.repo.query("ev", price_wan=(16, 20))
        assert [r["source_id"] for r in rows] == ["2"]

    def test_query_limit_and_order(self) -> None:
        rows = self.repo.query("ev", limit=2, order_by="price_wan", descending=True)
        assert [float(r["price_wan"]) for r in rows] == [26.50, 18.80]

    def test_stats(self) -> None:
        stats = self.repo.stats("ev")
        assert stats["total"] == 3
        assert stats["active"] == 3
        assert stats["by_platform"] == {"local_fixture_ev": 3}
        assert stats["avg_price_wan"] == pytest.approx(20.43, abs=0.01)

    def test_crawl_log_roundtrip(self) -> None:
        self.repo.log_crawl({
            "business_line": "ev", "source_platform": "local_fixture_ev", "task_type": "selftest",
            "status": "success", "fetched_count": 4, "inserted_count": 3,
            "started_at": datetime.now(), "finished_at": datetime.now(),
            "unknown_column": "应被忽略",
        })
        logs = self.repo.recent_logs(1)
        assert logs[0]["source_platform"] == "local_fixture_ev"
        assert logs[0]["inserted_count"] == 3
