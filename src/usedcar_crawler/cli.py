"""命令行入口。

设计：所有能力都通过 CLI 暴露，便于接 CI / 定时任务 / 运维手册落地，
而不是"只能在自己电脑上跑一次的脚本"。
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from pathlib import Path

from .config import PROJECT_ROOT, get_settings
from .errors import CrawlerError
from .fetcher import FetcherService
from .logging_setup import get_logger, setup_logging
from .notify import Notifier
from .pipeline.exporter import build_summary, export_records
from .runner import CrawlRunner, TaskSpec, probe_source
from .sources.registry import get_registry
from .storage.repository import Repository, build_engine

log = get_logger("cli")


def _bootstrap_logging() -> None:
    settings = get_settings()
    setup_logging(
        level=settings.logging.level,
        json_mode=settings.logging.json_lines,
        log_dir=PROJECT_ROOT / settings.logging.dir,
        retention_days=settings.logging.retention_days,
    )


def _build_repo() -> Repository:
    settings = get_settings()
    repo = Repository(build_engine(settings.database), settings=settings)
    repo.init_schema()
    return repo


# --------------------------------------------------------------------------- #
# 子命令
# --------------------------------------------------------------------------- #
def cmd_run(args: argparse.Namespace) -> int:
    task = TaskSpec(
        line=args.line,
        sources=args.source or [],
        pages=args.pages,
        task_type=args.task_type,
        adaptive=args.adaptive,
        fixture=args.fixture,
    )
    logs = CrawlRunner().run(task)
    failed = [item for item in logs if item.status == "failed"]
    _print_table(
        ["来源", "状态", "页数", "抓到", "入库(新/更)", "去重", "缺失率", "耗时(s)"],
        [
            [item.source_platform, item.status, item.pages_fetched, item.fetched_count,
             f"{item.inserted_count}/{item.updated_count}", item.dup_count,
             f"{item.missing_ratio:.1%}", item.elapsed_seconds]
            for item in logs
        ],
    )
    return 1 if failed else 0


def cmd_crawl(args: argparse.Namespace) -> int:
    """详情页型数据源采集（站点地图 -> 逐条详情页，真实网络请求）。"""
    from .pipeline.detail_runner import DetailCrawlRunner, DetailTaskSpec  # noqa: PLC0415

    task = DetailTaskSpec(
        line=args.line,
        sources=args.source or [],
        maps=args.maps,
        sample=args.sample,
        task_type=args.task_type,
        resume=not args.fresh,
        cooldown_seconds=args.cooldown,
        max_cooldowns=args.max_cooldowns,
    )
    logs = DetailCrawlRunner().run(task)
    _print_table(
        ["业务线", "来源", "状态", "取子图", "抓到", "入库(新/更)", "去重", "缺失率", "耗时(s)"],
        [
            [item.business_line, item.source_platform, item.status, item.pages_fetched,
             item.fetched_count, f"{item.inserted_count}/{item.updated_count}", item.dup_count,
             f"{item.missing_ratio:.1%}", item.elapsed_seconds]
            for item in logs
        ],
    )
    for item in logs:
        if item.error_detail:
            print(f"\n[{item.business_line}] {item.error_detail}")
    return 1 if all(item.status == "failed" for item in logs) else 0


def cmd_replay(args: argparse.Namespace) -> int:
    """raw 层回放：把已落盘的原始响应重新解析入库（离线，不发起网络请求）。"""
    from .pipeline.detail_runner import DetailCrawlRunner, ReplaySpec  # noqa: PLC0415

    task = ReplaySpec(
        line=args.line,
        sources=args.source or [],
        raw_dir=args.raw_dir,
        pattern=args.pattern,
        task_type=args.task_type,
    )
    logs = DetailCrawlRunner().replay(task)
    _print_table(
        ["业务线", "来源", "状态", "raw文件", "解析", "入库(新/更)", "去重", "缺失率"],
        [
            [item.business_line, item.source_platform, item.status, item.pages_fetched,
             item.parsed_count, f"{item.inserted_count}/{item.updated_count}", item.dup_count,
             f"{item.missing_ratio:.1%}"]
            for item in logs
        ],
    )
    for item in logs:
        if item.error_detail:
            print(f"\n[{item.business_line}] {item.error_detail}")
    return 1 if all(item.status == "failed" for item in logs) else 0


def cmd_viz(args: argparse.Namespace) -> int:
    """生成行情看板（单文件 HTML，离线可打开）。"""
    from .viz import build_dashboard  # noqa: PLC0415

    path, summary = build_dashboard(limit=args.limit)
    print(f"看板已生成：{path}")
    _print_table(
        ["指标", "数值"],
        [
            ["车源总量", summary["total"]],
            ["新能源", summary["ev"]],
            ["燃油", summary["fuel"]],
            ["覆盖品牌", summary["brands"]],
            ["覆盖城市", summary["cities"]],
            ["平均售价(万元)", summary["avg_price"]],
            ["平均保值率", f"{summary['avg_retention'] * 100:.1f}%" if summary["avg_retention"] else "—"],
            ["文件大小(KB)", summary["size_kb"]],
        ],
    )
    return 0


def cmd_probe(args: argparse.Namespace) -> int:
    spec = get_registry().get(args.source)
    if not spec.runnable:
        print(
            f"警告：源 {spec.key} 处于不可采集状态（enabled={spec.enabled}, "
            f"robots={spec.compliance.robots_status}）。probe 仅用于选择器校准，不会写入数据库。",
            file=sys.stderr,
        )
    report = probe_source(spec, page=args.page, fixture=args.fixture)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if report["missing_required"]:
        print(f"\n未命中的必需字段：{', '.join(report['missing_required'])}", file=sys.stderr)
        return 2
    return 0


def cmd_export(args: argparse.Namespace) -> int:
    repo = _build_repo()
    records = repo.query(args.line, limit=args.limit, include_deleted=args.include_deleted)
    if not records:
        print(f"{args.line} 业务线暂无数据，请先执行 run 命令", file=sys.stderr)
        return 1
    files = export_records(args.line, records, fmt=args.format, tag=args.tag or "")
    for path in files:
        print(path)
    return 0


def cmd_stats(args: argparse.Namespace) -> int:
    repo = _build_repo()
    lines = ["ev", "fuel"] if args.line == "all" else [args.line]
    for line in lines:
        stats = repo.stats(line)
        print(json.dumps({k: v for k, v in stats.items() if k != "last_run"}, ensure_ascii=False, indent=2))
        if args.summary:
            print(f"摘要已生成：{build_summary(line, stats)}")
    return 0


def cmd_logs(args: argparse.Namespace) -> int:
    repo = _build_repo()
    rows = repo.recent_logs(args.limit)
    if not rows:
        print("暂无采集日志")
        return 0
    _print_table(
        ["时间", "业务线", "来源", "状态", "抓到", "入库(新/更)", "错误", "缺失率"],
        [
            [str(row["started_at"])[:19], row["business_line"], row["source_platform"], row["status"],
             row["fetched_count"], f"{row['inserted_count']}/{row['updated_count']}",
             row["error_count"], f"{float(row['missing_ratio']):.1%}"]
            for row in rows
        ],
    )
    return 0


def cmd_compliance(args: argparse.Namespace) -> int:
    """合规巡检：打印各源的 robots 档案；``--verify`` 联网实时复核。"""
    from .compliance import ComplianceReport, verify_all  # noqa: PLC0415
    from .fetcher import RobotsGate  # noqa: PLC0415

    registry = get_registry()
    specs = registry.all(include_disabled=True)
    if args.verify:
        targets = [spec for spec in specs if spec.enabled]
        reports = verify_all(targets, gate=RobotsGate(enabled=True))
        skipped = [spec for spec in specs if not spec.enabled]
        for spec in skipped:
            reports.append(
                ComplianceReport(key=spec.key, name=spec.name, line=spec.line, enabled=False,
                                 recorded_status=spec.compliance.robots_status,
                                 reason=spec.compliance.note)
            )
    else:
        reports = [
            ComplianceReport(key=spec.key, name=spec.name, line=spec.line, enabled=spec.enabled,
                             recorded_status=spec.compliance.robots_status,
                             reason=spec.compliance.note)
            for spec in specs
        ]

    order = {"ev": 0, "both": 0, "fuel": 1}
    reports.sort(key=lambda item: (order.get(item.line, 9), item.key))
    _print_table(
        ["来源", "业务线", "启用", "档案状态", "实时核查", "结论"],
        [
            [item.key, item.line, "是" if item.enabled else "否",
             item.recorded_status, item.live_status, item.verdict]
            for item in reports
        ],
    )
    print()
    for item in reports:
        if item.reason:
            print(f"- {item.key}: {item.reason}")
    blocked = [item for item in reports if item.verdict.startswith("禁止")]
    if blocked:
        print(f"\n共 {len(blocked)} 个源禁止抓取，已从采集范围中排除（配置里 enabled=false）。")
    if not args.verify:
        print("\n提示：加 --verify 可联网实时复核 robots.txt（仅请求 robots.txt 本身，不抓列表页）。")
    return 0


def cmd_selftest(args: argparse.Namespace) -> int:
    """离线全链路自检：夹具 -> 解析 -> 清洗 -> 入库 -> 导出，零网络请求。"""
    from .selftest import run_selftest  # noqa: PLC0415 - 避免无谓的导入开销

    return run_selftest(verbose=args.verbose)


def cmd_schedule(args: argparse.Namespace) -> int:
    from .scheduler import start_scheduler  # noqa: PLC0415

    return start_scheduler(run_once=args.once, job_name=args.job)


def cmd_initdb(args: argparse.Namespace) -> int:
    repo = _build_repo()
    repo.init_schema()
    print(f"表结构已就绪：{get_settings().database.url}")
    return 0


def cmd_notify_test(args: argparse.Namespace) -> int:
    """验证告警通道是否打通（上线前必做一步）。"""
    notifier = Notifier()
    if not notifier.enabled:
        print("未配置 webhook（UCC_NOTIFY__WEBHOOK_URL），当前为日志模式")
        return 0
    ok = notifier.send("source_failed", title="告警通道连通性测试", detail="来自 usedcar-crawler 的测试消息")
    print("推送成功" if ok else "推送失败，请检查 webhook 配置")
    return 0 if ok else 1


# --------------------------------------------------------------------------- #
# 输出辅助
# --------------------------------------------------------------------------- #
def _display_width(text: str) -> int:
    return sum(2 if ord(char) > 0x2E80 else 1 for char in str(text))


def _print_table(headers: list[str], rows: list[list[object]]) -> None:
    """极简对齐表格（中文按双宽计算），避免为一个 CLI 引入表格依赖。"""
    table = [[str(cell) for cell in row] for row in rows]
    widths = [
        max(_display_width(headers[i]), *(_display_width(row[i]) for row in table)) if table else _display_width(headers[i])
        for i in range(len(headers))
    ]

    def render(cells: list[str]) -> str:
        return "  ".join(cell + " " * (widths[i] - _display_width(cell)) for i, cell in enumerate(cells))

    print(render(headers))
    print("-" * (sum(widths) + 2 * (len(headers) - 1)))
    for row in table:
        print(render(row))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="usedcar-crawler",
        description="二手车行情采集平台（EV / FUEL 双业务线，基于 Scrapling）",
    )
    parser.add_argument("--version", action="version", version="usedcar-crawler 0.1.0")
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="执行采集任务")
    run.add_argument("--line", default="all", choices=["ev", "fuel", "all"])
    run.add_argument("--source", action="append", help="指定采集源 key，可重复；缺省为该业务线全部源")
    run.add_argument("--pages", type=int, default=1, help="翻页数（默认 1，先小步验证再放量）")
    run.add_argument("--task-type", default="daily_incr", choices=["daily_incr", "weekly_full", "retry"])
    run.add_argument("--adaptive", action="store_true", help="启用选择器自适应（站点改版后使用）")
    run.add_argument("--fixture", action="store_true", help="使用离线样本源（零网络）")
    run.set_defaults(func=cmd_run)

    crawl = sub.add_parser("crawl", help="详情页型源采集（站点地图 -> 逐条详情页，真实请求）")
    crawl.add_argument("--line", default="all", choices=["ev", "fuel", "all"])
    crawl.add_argument("--source", action="append", help="指定采集源 key，可重复")
    crawl.add_argument("--maps", type=int, default=0, help="取前几个子地图；0 表示用源配置的 max_pages")
    crawl.add_argument("--sample", type=int, default=0, help="每个子地图采样多少条；0 表示用源配置的 detail_sample")
    crawl.add_argument("--task-type", default="daily_incr", choices=["daily_incr", "weekly_full", "retry"])
    crawl.add_argument("--fresh", action="store_true", help="忽略断点文件，重新采集（默认续爬）")
    crawl.add_argument("--cooldown", type=float, default=None, help="覆盖源配置的冷却秒数（配额紧张时调大）")
    crawl.add_argument("--max-cooldowns", type=int, default=None, help="覆盖源配置的冷却次数上限（调大=更耐心）")
    crawl.set_defaults(func=cmd_crawl)

    replay = sub.add_parser("replay", help="raw 层回放：重解析已落盘原始响应并入库（离线）")
    replay.add_argument("--line", default="all", choices=["ev", "fuel", "all"])
    replay.add_argument("--source", action="append", help="指定采集源 key，可重复")
    replay.add_argument("--raw-dir", default=None, help="raw 文件目录；缺省 data/captured/<source>")
    replay.add_argument("--pattern", default="*.md", help="raw 文件匹配模式（默认 *.md）")
    replay.add_argument("--task-type", default="replay", choices=["daily_incr", "weekly_full", "retry", "replay"])
    replay.set_defaults(func=cmd_replay)

    viz = sub.add_parser("viz", help="生成行情看板（单文件 HTML，离线可打开）")
    viz.add_argument("--limit", type=int, default=100_000)
    viz.set_defaults(func=cmd_viz)

    probe = sub.add_parser("probe", help="选择器现场校准：报告各选择器命中数")
    probe.add_argument("--source", required=True)
    probe.add_argument("--page", type=int, default=1)
    probe.add_argument("--fixture", action="store_true")
    probe.set_defaults(func=cmd_probe)

    export = sub.add_parser("export", help="导出干净表为 Excel / CSV")
    export.add_argument("--line", default="ev", choices=["ev", "fuel"])
    export.add_argument("--format", default="both", choices=["xlsx", "csv", "both"])
    export.add_argument("--limit", type=int, default=100_000)
    export.add_argument("--tag", default="")
    export.add_argument("--include-deleted", action="store_true", help="包含已下架车源")
    export.set_defaults(func=cmd_export)

    stats = sub.add_parser("stats", help="查看业务线统计指标")
    stats.add_argument("--line", default="all", choices=["ev", "fuel", "all"])
    stats.add_argument("--summary", action="store_true", help="同时生成 Markdown 摘要")
    stats.set_defaults(func=cmd_stats)

    logs = sub.add_parser("logs", help="查看最近的采集日志")
    logs.add_argument("--limit", type=int, default=20)
    logs.set_defaults(func=cmd_logs)

    selftest = sub.add_parser("selftest", help="离线全链路自检（不需网络）")
    selftest.add_argument("--verbose", action="store_true")
    selftest.set_defaults(func=cmd_selftest)

    compliance = sub.add_parser("compliance", help="合规巡检：robots.txt 档案与实时复核")
    compliance.add_argument("--verify", action="store_true", help="联网实时复核 robots.txt")
    compliance.set_defaults(func=cmd_compliance)

    schedule = sub.add_parser("schedule", help="启动常驻调度")
    schedule.add_argument("--once", action="store_true", help="立即执行一次后退出（用于验证配置）")
    schedule.add_argument("--job", default=None, help="只运行指定任务名")
    schedule.set_defaults(func=cmd_schedule)

    initdb = sub.add_parser("initdb", help="初始化数据库表结构")
    initdb.set_defaults(func=cmd_initdb)

    notify = sub.add_parser("notify-test", help="测试告警 Webhook 连通性")
    notify.set_defaults(func=cmd_notify_test)
    return parser


def main(argv: list[str] | None = None) -> int:
    _bootstrap_logging()
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except CrawlerError as exc:
        log.error("任务失败：%s", exc)
        return 2
    except KeyboardInterrupt:  # pragma: no cover
        log.warning("已被用户中断")
        return 130


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
