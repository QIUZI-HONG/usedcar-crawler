"""配置管理：YAML 为基础层，环境变量为最高优先级覆盖层。

优先级（高 -> 低）：真实环境变量 > ``.env`` 文件 > ``config/settings.yaml`` > 代码默认值。
密钥（数据库密码、代理）只允许来自环境变量，仓库内不落任何凭据。
"""

from __future__ import annotations

import functools
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator
from pydantic_settings import BaseSettings, PydanticBaseSettingsSource, SettingsConfigDict

ENV_PREFIX = "UCC_"
PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG_DIR = PROJECT_ROOT / "config"


class AppCfg(BaseModel):
    name: str = "usedcar-crawler"
    env: Literal["dev", "prod"] = "dev"
    timezone: str = "Asia/Shanghai"


class LoggingCfg(BaseModel):
    level: str = "INFO"
    # 字段名避开 BaseModel.json（否则 pydantic 会告警），对外配置键同名以保持可读
    json_lines: bool = Field(default=False, alias="json")
    dir: str = "logs"
    retention_days: int = 14

    model_config = ConfigDict(populate_by_name=True)


class DatabaseCfg(BaseModel):
    url: str = "sqlite:///./data/usedcar.db"
    echo: bool = False
    pool_size: int = 5
    init_schema: bool = True


class FetchCfg(BaseModel):
    default_qps: float = Field(default=0.5, gt=0, le=5)
    request_timeout: int = Field(default=30, gt=0)
    max_retries: int = Field(default=3, ge=0, le=10)
    backoff_base: float = Field(default=2.0, ge=0.1)
    respect_robots: bool = True
    tier_ladder: list[Literal["http", "stealth", "dynamic"]] = ["http", "stealth", "dynamic"]
    snapshot_raw: bool = True
    raw_dir: str = "data/raw"
    proxy: str = ""
    adaptive: bool = True
    adaptive_storage: str = ".scrapling_checkpoints/selectors.db"
    drift_missing_ratio: float = Field(default=0.3, ge=0, le=1)


class NotifyCfg(BaseModel):
    enabled: bool = False
    webhook_url: str = ""
    on_events: list[str] = ["source_failed", "schema_drift", "compliance_blocked"]


class StorageCfg(BaseModel):
    batch_size: int = Field(default=500, gt=0)
    mark_missing_deleted_after: int = Field(default=3, ge=1)


class ExportCfg(BaseModel):
    dir: str = "exports"
    formats: list[Literal["xlsx", "csv"]] = ["xlsx", "csv"]
    xlsx_max_rows: int = 50_000


class ScheduleJobCfg(BaseModel):
    name: str
    line: Literal["ev", "fuel", "all"] = "all"
    cron: str = "0 0 3 * * *"
    pages: int = Field(default=10, ge=1)
    sources: list[str] = []


class ScheduleCfg(BaseModel):
    enabled: bool = True
    jobs: list[ScheduleJobCfg] = []


class Settings(BaseSettings):
    """运行时配置聚合对象。"""

    model_config = SettingsConfigDict(
        env_prefix=ENV_PREFIX,
        env_nested_delimiter="__",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app: AppCfg = AppCfg()
    logging: LoggingCfg = LoggingCfg()
    database: DatabaseCfg = DatabaseCfg()
    fetch: FetchCfg = FetchCfg()
    notify: NotifyCfg = NotifyCfg()
    storage: StorageCfg = StorageCfg()
    export: ExportCfg = ExportCfg()
    schedule: ScheduleCfg = ScheduleCfg()

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        """把 YAML 插到最低优先级，保证环境变量始终能覆盖。"""
        yaml_source = _YamlSettingsSource(settings_cls, DEFAULT_CONFIG_DIR / "settings.yaml")
        return (init_settings, env_settings, dotenv_settings, yaml_source, file_secret_settings)


class _YamlSettingsSource(PydanticBaseSettingsSource):
    """最小实现的 YAML 配置源（不依赖 pydantic-settings 的子类可用性）。"""

    def __init__(self, settings_cls: type[BaseSettings], path: Path) -> None:
        super().__init__(settings_cls)
        self._path = Path(path)
        self._data: dict[str, Any] = {}
        if self._path.exists():
            raw = yaml.safe_load(self._path.read_text(encoding="utf-8")) or {}
            if not isinstance(raw, dict):
                raise ValueError(f"配置文件格式错误，顶层必须是映射：{self._path}")
            self._data = raw

    def get_field_value(self, field: Any, field_name: str) -> tuple[Any, str, bool]:  # noqa: D102
        return self._data.get(field_name), field_name, False

    def __call__(self) -> dict[str, Any]:
        return self._data


@functools.lru_cache(maxsize=1)
def get_settings() -> Settings:
    """进程内单例，避免重复解析 YAML。"""
    return Settings()


def reload_settings() -> Settings:
    """测试或热更新场景下强制重新加载。"""
    get_settings.cache_clear()
    return get_settings()
