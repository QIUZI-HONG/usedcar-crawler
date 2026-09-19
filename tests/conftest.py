"""pytest 公共夹具。

原则：单元测试**不触网**、不写工作区数据目录。所有外部依赖（网络、数据库、通知）
都通过注入替身控制，保证 CI 上稳定可复现。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC = PROJECT_ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

FIXTURES = Path(__file__).resolve().parent / "fixtures"


@pytest.fixture(scope="session")
def project_root() -> Path:
    return PROJECT_ROOT


@pytest.fixture(scope="session")
def fixtures_dir() -> Path:
    return FIXTURES


@pytest.fixture(autouse=True)
def _quiet_logging():
    """测试期间把根日志级别压到 WARNING，避免噪声淹没失败信息。"""
    import logging

    logging.getLogger().setLevel(logging.WARNING)
    yield


@pytest.fixture
def fetch_cfg():
    """测试用取数配置：关闭快照、放开限速，保证测试快速且不写盘。"""
    from usedcar_crawler.config import get_settings

    return get_settings().fetch.model_copy(
        update={"snapshot_raw": False, "default_qps": 1000.0, "max_retries": 1, "backoff_base": 0.01}
    )


@pytest.fixture
def registry():
    from usedcar_crawler.sources.registry import get_registry

    return get_registry()


@pytest.fixture
def fixture_spec(registry):
    return registry.get("local_fixture_ev")


@pytest.fixture
def fixture_fetcher(fetch_cfg):
    from usedcar_crawler.fetcher import FetcherService

    return FetcherService(fetch_cfg, fixtures_dir=FIXTURES)


@pytest.fixture
def repo(tmp_path, project_root):
    from usedcar_crawler.config import get_settings
    from usedcar_crawler.storage.repository import Repository, build_engine

    db_path = (tmp_path / "test.db").as_posix()
    engine = build_engine(get_settings().database.model_copy(update={"url": f"sqlite:///{db_path}"}))
    repository = Repository(engine, settings=get_settings())
    repository.init_schema()
    return repository


@pytest.fixture
def fake_notifier():
    """记录告警调用的替身，用于断言"改版/失败必告警"。"""

    class FakeNotifier:
        def __init__(self) -> None:
            self.events: list[tuple[str, str]] = []

        def send(self, event, *, title, detail, metrics=None) -> bool:  # noqa: ANN001
            self.events.append((event, title))
            return True

    return FakeNotifier()


@pytest.fixture
def fixture_html():
    """按文件名读取夹具 HTML。"""

    def _load(name: str) -> str:
        return (FIXTURES / name).read_text(encoding="utf-8")

    return _load
