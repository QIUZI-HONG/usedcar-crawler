"""离线全链路自检（``usedcar-crawler selftest``）。

价值：不需要网络、不需要真实站点，就能验证"夹具 -> 解析 -> 清洗 -> 校验 -> 去重 -> 入库 -> 导出"
整条链路是通的。这就是 CI 里跑的那条命令，也是演示给面试官看的"一键跑通"入口。

自检内容：
1. 两条业务线各解析一页夹具；
2. 打印关键字段标准化结果（价格/里程/SOH/排量/排放）；
3. 第二次写入必须全部走 UPDATE —— 验证幂等性（爬虫最容易出的生产事故就是重复插入）；
4. 导出 Excel/CSV 并校验文件确实生成；
5. 打印统计指标。
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from .config import PROJECT_ROOT, DatabaseCfg, get_settings
from .fetcher import FetcherService
from .logging_setup import get_logger, new_trace_id
from .notify import Notifier
from .pipeline.exporter import export_records
from .runner import CrawlRunner, TaskSpec
from .storage.repository import Repository, build_engine

log = get_logger("selftest")

FIXTURES_DIR = PROJECT_ROOT / "tests" / "fixtures"


def run_selftest(*, verbose: bool = False) -> int:
    """执行自检，返回 0 表示全部通过。"""
    new_trace_id()
    settings = get_settings()
    failures: list[str] = []
    with tempfile.TemporaryDirectory(prefix="usedcar_selftest_") as tmp:
        tmp_path = Path(tmp)
        fetch_cfg = settings.fetch.model_copy(
            update={"snapshot_raw": False, "respect_robots": True, "max_retries": 1}
        )
        fetcher = FetcherService(fetch_cfg, fixtures_dir=FIXTURES_DIR)
        db_cfg = DatabaseCfg(url=f"sqlite:///{(tmp_path / 'selftest.db').as_posix()}")
        engine = build_engine(db_cfg)
        repo = Repository(engine, settings=settings)
        try:
            failures = _run_stages(
                settings=settings, fetcher=fetcher, repo=repo, tmp_path=tmp_path, verbose=verbose
            )
        finally:
            # Windows 上必须先释放 SQLite 文件句柄，否则临时目录清理会因文件占用而失败
            engine.dispose()

    print()
    if failures:
        print("自检未通过：")
        for item in failures:
            print(f"  ✗ {item}")
        return 1
    print("✓ 自检全部通过：解析 / 清洗 / 校验 / 去重 / 幂等入库 / 导出 均正常")
    return 0


def _run_stages(*, settings, fetcher, repo: Repository, tmp_path: Path, verbose: bool) -> list[str]:
    """执行四个自检阶段，返回失败项列表。"""
    failures: list[str] = []
    runner = CrawlRunner(fetcher=fetcher, repo=repo, notifier=Notifier(), registry=_fixture_registry())

    print("=" * 78)
    print("阶段 1/4  解析与清洗（离线夹具）")
    print("=" * 78)
    for line in ("ev", "fuel"):
        logs = runner.run(TaskSpec(line=line, pages=1, task_type="selftest", fixture=True))
        for item in logs:
            print(f"  [{line}] 源={item.source_platform} 抓到={item.fetched_count} "
                  f"解析={item.parsed_count} 新增={item.inserted_count} "
                  f"缺失率={item.missing_ratio:.1%} 错误={item.error_count}")
            if item.fetched_count == 0:
                failures.append(f"{line} 未解析出任何数据")
            if item.status == "failed":
                failures.append(f"{line} 采集失败：{item.error_detail[:200]}")

    _print_samples(repo, "ev", [
        ("标题", "title_raw"), ("品牌", "brand"), ("车型", "model"),
        ("售价(万)", "price_wan"), ("新车价(万)", "new_car_price_wan"), ("保值率", "retention_rate"),
        ("里程", "mileage_km"), ("上牌", "reg_year"), ("电池", "battery_type"),
        ("续航", "range_km"), ("续航口径", "range_standard"), ("电池健康度", "battery_health"),
        ("城市", "location_city"),
    ], verbose=verbose)
    _print_samples(repo, "fuel", [
        ("标题", "title_raw"), ("品牌", "brand"), ("车型", "model"),
        ("售价(万)", "price_wan"), ("里程", "mileage_km"), ("上牌年", "reg_year"),
        ("排量(L)", "displacement_l"), ("变速箱", "gearbox"), ("排放", "emission_standard"),
        ("城市", "location_city"),
    ], verbose=verbose)

    print()
    print("=" * 78)
    print("阶段 2/4  幂等性验证（重复写入必须走 UPDATE）")
    print("=" * 78)
    for line in ("ev", "fuel"):
        logs = runner.run(TaskSpec(line=line, pages=1, task_type="selftest", fixture=True))
        for item in logs:
            print(f"  [{line}] 第二次: 新增={item.inserted_count} 更新={item.updated_count} "
                  f"去重={item.dup_count}")
            if item.inserted_count != 0:
                failures.append(f"{line} 重复抓取产生了 {item.inserted_count} 条新增，幂等性被破坏")
            if item.updated_count == 0:
                failures.append(f"{line} 重复抓取未触发任何更新，UPSERT 可能未生效")

    print()
    print("=" * 78)
    print("阶段 3/4  导出交付（Excel / CSV）")
    print("=" * 78)
    for line in ("ev", "fuel"):
        records = repo.query(line)
        if not records:
            failures.append(f"{line} 无数据可导出")
            continue
        try:
            files = export_records(line, records, fmt="both", tag="selftest")
        except Exception as exc:  # noqa: BLE001 - 自检需给出可读结论而非堆栈
            failures.append(f"{line} 导出失败：{exc}")
            print(f"  导出失败：{exc}")
            continue
        for path in files:
            ok = path.exists() and path.stat().st_size > 0
            print(f"  [{line}] {path.name} 大小={path.stat().st_size}B 可读={ok}")
            if not ok:
                failures.append(f"{line} 导出文件为空：{path}")
            # 导出产物属临时验证，随手清理，避免污染工作区
            path.unlink(missing_ok=True)

    print()
    print("=" * 78)
    print("阶段 4/4  统计指标")
    print("=" * 78)
    for line in ("ev", "fuel"):
        stats = repo.stats(line)
        print(f"  [{line}] 总数={stats['total']} 在售={stats['active']} "
              f"均价(万)={stats['avg_price_wan']} 保值率={stats['avg_retention_rate']} "
              f"来源={stats['by_platform']}")
    return failures


def _fixture_registry():
    """只保留离线样本源，确保自检不触网。"""
    from .sources.registry import SourceRegistry, get_registry  # noqa: PLC0415

    specs = [spec for spec in get_registry().all() if spec.tier == "fixture"]
    return SourceRegistry(specs)


def _print_samples(repo: Repository, line: str, fields: list[tuple[str, str]], *, verbose: bool = False) -> None:
    records = repo.query(line, limit=2 if not verbose else 10)
    if not records:
        print(f"  [{line}] 无样本数据")
        return
    print(f"\n  ---- {line.upper()} 标准化字段样本 ----")
    for index, record in enumerate(records, start=1):
        print(f"  第 {index} 条：")
        for label, key in fields:
            print(f"      {label:<10} = {record.get(key)}")
