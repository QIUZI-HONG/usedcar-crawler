"""存储层：SQLAlchemy Core 实现，SQLite（开发/CI）与 MySQL（生产）双兼容。

三个设计要点：
1. **方言无关**：不使用任何 MySQL 专有 SQL，业务代码在 SQLite 上可完整回归测试；
2. **幂等 UPSERT**：同一车源重复抓取只更新、不新增，价格变化本身就是要追踪的业务信号；
3. **下架追踪**：靠 ``last_seen_at`` / ``missing_count`` 判定，避免超长 NOT IN 列表拖垮数据库。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Sequence

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    Integer,
    MetaData,
    Numeric,
    String,
    Table,
    Text,
    bindparam,
    create_engine,
    delete,
    func,
    insert,
    select,
    update,
)
from sqlalchemy.engine import Engine
from sqlalchemy.exc import SQLAlchemyError

from ..config import PROJECT_ROOT, DatabaseCfg, get_settings
from ..dedupe import make_match_key
from ..errors import StorageError
from ..logging_setup import get_logger
from ..models import EVVehicle, FuelVehicle
from ..parsers import get_parser
from ..pipeline.cleaners import safe_ratio

log = get_logger("storage")

metadata = MetaData()

def common_columns() -> list[Column]:
    """两条业务线共享列的**工厂函数**。

    必须每次返回新对象：SQLAlchemy 的 Column 与 Table 是多对一绑定，
    同一个 Column 实例挂到两张表上会直接报 "Column object 'id' already assigned"。
    """
    return [
        Column("id", Integer, primary_key=True, autoincrement=True),
        Column("vehicle_key", String(80), nullable=False, unique=True, index=True),
        Column("source_platform", String(40), nullable=False, index=True),
        Column("source_id", String(80)),
        Column("match_key", String(200), index=True),
        Column("title_raw", String(500)),
        Column("brand", String(50), index=True),
        Column("model", String(120)),
        Column("price_wan", Numeric(10, 2), index=True),
        Column("new_car_price_wan", Numeric(10, 2)),
        Column("retention_rate", Numeric(8, 4)),
        Column("mileage_km", Integer, index=True),
        Column("reg_year", Integer, index=True),
        Column("reg_month", Integer),
        Column("transfer_count", Integer),
        Column("location_city", String(40), index=True),
        Column("detail_url", String(700)),
        Column("raw_ref", String(300)),
        Column("captured_at", DateTime, nullable=False, index=True),
        Column("last_seen_at", DateTime, nullable=False, index=True),
        Column("missing_count", Integer, nullable=False, default=0),
        Column("is_deleted", Boolean, nullable=False, default=False),
    ]


def ev_columns() -> list[Column]:
    """新能源专属列。"""
    return [
        Column("battery_type", String(20)),
        Column("range_km", Integer),
        Column("range_standard", String(10)),
        Column("battery_health", Numeric(5, 1)),
        Column("fast_charge_kw", Numeric(6, 1)),
    ]


def fuel_columns() -> list[Column]:
    """燃油专属列。"""
    return [
        Column("displacement_l", Numeric(4, 1)),
        Column("gearbox", String(10)),
        Column("emission_standard", String(10)),
    ]


ev_vehicles = Table("ev_vehicles", metadata, *(common_columns() + ev_columns()))
fuel_vehicles = Table("fuel_vehicles", metadata, *(common_columns() + fuel_columns()))

crawl_log = Table(
    "crawl_log",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("business_line", String(10), nullable=False),
    Column("source_platform", String(40), nullable=False, index=True),
    Column("task_type", String(20), nullable=False),
    Column("status", String(10), nullable=False),
    Column("pages_fetched", Integer, nullable=False, default=0),
    Column("fetched_count", Integer, nullable=False, default=0),
    Column("parsed_count", Integer, nullable=False, default=0),
    Column("inserted_count", Integer, nullable=False, default=0),
    Column("updated_count", Integer, nullable=False, default=0),
    Column("dup_count", Integer, nullable=False, default=0),
    Column("error_count", Integer, nullable=False, default=0),
    Column("missing_ratio", Numeric(6, 4), nullable=False, default=0),
    Column("error_detail", Text),
    Column("started_at", DateTime, nullable=False, index=True),
    Column("finished_at", DateTime),
)

TABLE_BY_LINE: dict[str, Table] = {"ev": ev_vehicles, "fuel": fuel_vehicles}
MODEL_BY_LINE: dict[str, type] = {"ev": EVVehicle, "fuel": FuelVehicle}

_UPDATE_BIND_KEY = "b_vehicle_key"


def build_update_statement(table: Table):
    """构造可 executemany 的 UPDATE 语句。

    用独立绑定名 ``b_vehicle_key`` 做 WHERE 条件，避免与 VALUES 中的同名列冲突。
    """
    return (
        update(table)
        .where(table.c.vehicle_key == bindparam(_UPDATE_BIND_KEY))
        .values(
            **{
                column.name: bindparam(column.name)
                for column in table.columns
                if column.name not in ("id", "vehicle_key")
            }
        )
    )


@dataclass
class UpsertResult:
    inserted: int = 0
    updated: int = 0
    rejected: int = 0
    reject_samples: list[str] = field(default_factory=list)

    @property
    def total(self) -> int:
        return self.inserted + self.updated


def build_engine(cfg: DatabaseCfg | None = None) -> Engine:
    """按配置创建引擎；SQLite 场景自动确保父目录存在。"""
    cfg = cfg or get_settings().database
    url = cfg.url
    connect_args: dict[str, Any] = {}
    if url.startswith("sqlite"):
        raw_path = url.split("///")[-1]
        if raw_path and raw_path != ":memory:":
            db_path = Path(raw_path)
            if not db_path.is_absolute():
                db_path = PROJECT_ROOT / db_path
            db_path.parent.mkdir(parents=True, exist_ok=True)
            url = f"sqlite:///{db_path.as_posix()}"
        connect_args = {"check_same_thread": False}
    try:
        return create_engine(url, echo=cfg.echo, pool_pre_ping=True, connect_args=connect_args)
    except SQLAlchemyError as exc:  # pragma: no cover - 配置错误路径
        raise StorageError(f"数据库引擎创建失败：{exc}", context={"url": url}) from exc


class Repository:
    """车源与采集日志的读写入口。"""

    def __init__(self, engine: Engine, *, settings=None) -> None:
        self.engine = engine
        settings = settings or get_settings()
        self.batch_size = settings.storage.batch_size
        self.mark_missing_after = settings.storage.mark_missing_deleted_after
        self._update_stmts = {line: build_update_statement(table) for line, table in TABLE_BY_LINE.items()}

    # ---------------- 建表 ----------------
    def init_schema(self) -> None:
        """建表（幂等，启动时调用）。"""
        metadata.create_all(self.engine)
        log.debug("表结构已就绪")

    # ---------------- 车辆写入 ----------------
    def upsert_vehicles(self, line: str, records: Sequence[dict[str, Any]]) -> UpsertResult:
        """校验 → 归一 → UPSERT。

        :param records: 解析层产出的原始字典（含 ``*_raw`` 文案字段）
        :return: 插入 / 更新 / 被拒条数
        """
        if line not in TABLE_BY_LINE:
            raise StorageError(f"未知业务线：{line}")
        if not records:
            return UpsertResult()

        table = TABLE_BY_LINE[line]
        rows, result = self._validate(line, records)
        if not rows:
            return result
        # 关键：以"本轮开始时间"作为本轮标记，写入与下架判定必须用同一个基准，
        # 否则本轮刚写入的行会被自己误判成"未出现"而标记下架。
        run_start = datetime.now()
        self._prepare_rows(rows, seen_at=run_start)

        try:
            with self.engine.begin() as conn:
                existing = self._existing_keys(conn, table, [r["vehicle_key"] for r in rows])
                to_insert = [r for r in rows if r["vehicle_key"] not in existing]
                to_update = [{**r, _UPDATE_BIND_KEY: r["vehicle_key"]} for r in rows if r["vehicle_key"] in existing]

                for chunk in self._chunks(to_insert):
                    conn.execute(insert(table), chunk)
                for chunk in self._chunks(to_update):
                    conn.execute(self._update_stmts[line], chunk)
                self._refresh_missing(conn, table, run_start=run_start)
        except SQLAlchemyError as exc:
            raise StorageError(f"车源写入失败：{exc}", context={"line": line, "rows": len(rows)}) from exc

        result.inserted = len(to_insert)
        result.updated = len(to_update)
        log.info(
            "车源入库完成",
            extra={"line": line, "inserted": result.inserted, "updated": result.updated,
                   "rejected": result.rejected},
        )
        return result

    def _validate(self, line: str, records: Sequence[dict[str, Any]]) -> tuple[list[dict], UpsertResult]:
        """用 Pydantic 模型逐条校验；单条非法只丢弃该条，不中断整批。"""
        parser = get_parser(line)
        rows: list[dict] = []
        result = UpsertResult()
        for record in records:
            try:
                model = parser.to_model(record)
            except Exception as exc:  # noqa: BLE001 - 脏数据不应拖垮整批任务
                result.rejected += 1
                if len(result.reject_samples) < 5:
                    result.reject_samples.append(f"{record.get('title_raw')} -> {exc}")
                continue
            rows.append(model.model_dump())
        if result.rejected:
            log.warning(
                "存在校验未通过的数据，已丢弃",
                extra={"line": line, "rejected": result.rejected, "samples": result.reject_samples},
            )
        return rows, result

    @staticmethod
    def _prepare_rows(rows: list[dict], *, seen_at: datetime) -> None:
        """补全派生列（归并键、保值率）与生命周期列。"""
        for row in rows:
            row["last_seen_at"] = seen_at
            row["missing_count"] = 0
            row["is_deleted"] = False
            row["match_key"] = make_match_key(
                brand=row.get("brand"), model=row.get("model"), reg_year=row.get("reg_year"),
                mileage_km=row.get("mileage_km"), reg_month=row.get("reg_month"),
            )
            row["retention_rate"] = safe_ratio(row.get("price_wan"), row.get("new_car_price_wan"))

    def _existing_keys(self, conn, table: Table, keys: Iterable[str]) -> set[str]:
        keys = list(keys)
        found: set[str] = set()
        for chunk in self._chunks(keys):
            stmt = select(table.c.vehicle_key).where(table.c.vehicle_key.in_(chunk))
            found.update(row[0] for row in conn.execute(stmt))
        return found

    def _refresh_missing(self, conn, table: Table, *, run_start: datetime) -> None:
        """本轮未出现的车源：累计未见次数，达到阈值标记下架。"""
        conn.execute(
            update(table).where(table.c.last_seen_at < run_start).values(missing_count=table.c.missing_count + 1)
        )
        conn.execute(update(table).where(table.c.missing_count >= self.mark_missing_after).values(is_deleted=True))

    def _chunks(self, items: Sequence[Any]) -> Iterable[list[Any]]:
        for start in range(0, len(items), self.batch_size):
            yield list(items[start:start + self.batch_size])

    # ---------------- 日志 ----------------
    def log_crawl(self, payload: dict[str, Any]) -> None:
        """写采集日志；日志失败不影响主流程（但要留下痕迹）。"""
        record = {k: v for k, v in payload.items() if k in crawl_log.columns}
        try:
            with self.engine.begin() as conn:
                conn.execute(insert(crawl_log), [record])
        except SQLAlchemyError as exc:
            log.error("采集日志写入失败", extra={"err": str(exc)})

    # ---------------- 查询与统计 ----------------
    def query(self, line: str, *, limit: int = 1000, offset: int = 0, order_by: str = "captured_at",
              descending: bool = True, include_deleted: bool = False, **filters: Any) -> list[dict[str, Any]]:
        """按条件查询车源（导出与临时取数的底座）。"""
        table = TABLE_BY_LINE.get(line)
        if table is None:
            raise StorageError(f"未知业务线：{line}")
        stmt = select(table)
        if not include_deleted:
            stmt = stmt.where(table.c.is_deleted.is_(False))
        for column_name, value in filters.items():
            if value is None or column_name not in table.columns:
                continue
            column = table.columns[column_name]
            if isinstance(value, (tuple, list)) and len(value) == 2:
                stmt = stmt.where(column.between(value[0], value[1]))
            else:
                stmt = stmt.where(column == value)
        column = table.columns.get(order_by, table.c.captured_at)
        stmt = stmt.order_by(column.desc() if descending else column.asc()).limit(limit).offset(offset)
        with self.engine.connect() as conn:
            return [dict(row._mapping) for row in conn.execute(stmt)]

    def stats(self, line: str) -> dict[str, Any]:
        """单业务线概览指标，供 ``stats`` 命令与数据质量报告使用。"""
        table = TABLE_BY_LINE.get(line)
        if table is None:
            raise StorageError(f"未知业务线：{line}")
        with self.engine.connect() as conn:
            total = conn.execute(select(func.count()).select_from(table)).scalar_one()
            active = conn.execute(
                select(func.count()).select_from(table).where(table.c.is_deleted.is_(False))
            ).scalar_one()
            by_platform = conn.execute(
                select(table.c.source_platform, func.count())
                .group_by(table.c.source_platform)
                .order_by(func.count().desc())
            ).all()
            price_row = conn.execute(
                select(
                    func.avg(table.c.price_wan), func.min(table.c.price_wan),
                    func.max(table.c.price_wan), func.avg(table.c.retention_rate),
                ).where(table.c.is_deleted.is_(False))
            ).one()
            last_run = conn.execute(
                select(crawl_log.c.source_platform, crawl_log.c.status, crawl_log.c.finished_at)
                .where(crawl_log.c.business_line == line)
                .order_by(crawl_log.c.started_at.desc())
                .limit(1)
            ).first()
        return {
            "line": line,
            "total": total,
            "active": active,
            "deleted": total - active,
            "by_platform": {row[0]: row[1] for row in by_platform},
            "avg_price_wan": round(float(price_row[0]), 2) if price_row[0] is not None else None,
            "min_price_wan": float(price_row[1]) if price_row[1] is not None else None,
            "max_price_wan": float(price_row[2]) if price_row[2] is not None else None,
            "avg_retention_rate": round(float(price_row[3]), 4) if price_row[3] is not None else None,
            "last_run": dict(last_run._mapping) if last_run else None,
        }

    def recent_logs(self, limit: int = 20) -> list[dict[str, Any]]:
        stmt = select(crawl_log).order_by(crawl_log.c.started_at.desc()).limit(limit)
        with self.engine.connect() as conn:
            return [dict(row._mapping) for row in conn.execute(stmt)]

    def purge(self, line: str) -> int:
        """清空某业务线数据（仅用于自检与测试）。"""
        table = TABLE_BY_LINE.get(line)
        if table is None:
            raise StorageError(f"未知业务线：{line}")
        with self.engine.begin() as conn:
            return conn.execute(delete(table)).rowcount or 0
