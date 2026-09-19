"""领域模型：解析结果必须通过模型校验才能进入存储层。

为什么用 Pydantic 而不是裸 dict：
- 缺字段、类型错、数值越界在**入库前**暴露，而不是在报表里变成脏数据；
- 模型即数据契约，与 ``docs/data-dictionary.md`` 一一对应，减少沟通成本。
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .pipeline import cleaners as cl

BusinessLine = Literal["ev", "fuel"]


class VehicleBase(BaseModel):
    """两条业务线共享的车源字段。"""

    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    # --- 主键与溯源 ---
    vehicle_key: str = Field(min_length=8, max_length=64)
    source_platform: str
    source_id: str | None = None
    raw_ref: str | None = None

    # --- 车型 ---
    title_raw: str | None = None
    brand: str | None = None
    model: str | None = None

    # --- 价格与车况 ---
    price_wan: Decimal | None = Field(default=None, ge=0)
    new_car_price_wan: Decimal | None = Field(default=None, ge=0)
    mileage_km: int | None = Field(default=None, ge=0, le=1_500_000)
    reg_year: int | None = None
    reg_month: int | None = Field(default=None, ge=1, le=12)
    transfer_count: int | None = Field(default=None, ge=0, le=30)

    # --- 位置与链接 ---
    location_city: str | None = None
    detail_url: str | None = None

    # --- 元数据 ---
    captured_at: datetime = Field(default_factory=datetime.now)
    is_deleted: bool = False

    @field_validator("brand", "model", "location_city", "detail_url", "title_raw", mode="before")
    @classmethod
    def _clean_text_fields(cls, value: object) -> object:
        return cl.clean_text(value)

    @field_validator("reg_year", mode="before")
    @classmethod
    def _sane_year(cls, value: object) -> object:
        return cl.sane_year(value if isinstance(value, int) else cl.parse_int(value))

    @property
    def retention_rate(self) -> float | None:
        """保值率（现价 / 新车指导价）。"""
        return cl.safe_ratio(self.price_wan, self.new_car_price_wan)


class EVVehicle(VehicleBase):
    """业务线 A：新能源二手车。"""

    line: Literal["ev"] = "ev"
    battery_type: str | None = None
    range_km: int | None = Field(default=None, ge=0, le=2000)
    range_standard: str | None = None
    battery_health: float | None = Field(default=None, gt=0, le=100)
    fast_charge_kw: float | None = Field(default=None, ge=0, le=1000)

    @model_validator(mode="after")
    def _require_price(self) -> "EVVehicle":
        # 价格是行情分析的最小可用集，缺失即视为无效数据
        if self.price_wan is None or self.price_wan <= 0:
            raise ValueError("price_wan 缺失或非正数，拒绝入库")
        return self


class FuelVehicle(VehicleBase):
    """业务线 B：燃油二手车。"""

    line: Literal["fuel"] = "fuel"
    displacement_l: float | None = Field(default=None, ge=0.6, le=8.0)
    gearbox: str | None = None
    emission_standard: str | None = None

    @model_validator(mode="after")
    def _require_price(self) -> "FuelVehicle":
        if self.price_wan is None or self.price_wan <= 0:
            raise ValueError("price_wan 缺失或非正数，拒绝入库")
        return self


class CrawlLog(BaseModel):
    """一次采集任务的运行记录，对应存储层 ``crawl_log`` 表。"""

    model_config = ConfigDict(str_strip_whitespace=True)

    # both：一次采集同时产出油/电两条线（详情页源按车辆能源类型分流）
    business_line: Literal["ev", "fuel", "both"]
    source_platform: str
    task_type: Literal["daily_incr", "weekly_full", "retry", "selftest", "replay"] = "daily_incr"
    status: Literal["success", "partial", "failed"] = "success"
    pages_fetched: int = 0
    fetched_count: int = 0
    parsed_count: int = 0
    inserted_count: int = 0
    updated_count: int = 0
    dup_count: int = 0
    error_count: int = 0
    missing_ratio: float = 0.0
    error_detail: str = ""
    started_at: datetime = Field(default_factory=datetime.now)
    finished_at: datetime | None = None

    @property
    def elapsed_seconds(self) -> float | None:
        if self.finished_at is None:
            return None
        return round((self.finished_at - self.started_at).total_seconds(), 2)


MODEL_BY_LINE: dict[str, type[VehicleBase]] = {"ev": EVVehicle, "fuel": FuelVehicle}
