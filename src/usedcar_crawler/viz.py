"""可视化模块：把库里的干净表变成一张可交互的行情看板。

设计取舍：

1. **聚合在后端算，前端只渲染**。所有统计（分布、分位、均值、热力矩阵）在 Python 侧
   算完再喂给前端，页面拿到的就是最终数字——与计划书"前端零计算"的约定一致，
   也避免把 Decimal/时区这类类型问题带进浏览器。
2. **单文件 HTML + 本地 ECharts**。产物是一个可以双击打开、断网也能看的文件，
   面试演示时不必先起服务；图库就近加载（``dashboard/echarts.min.js``），
   不依赖 CDN 可达性。
3. **口径写进图注**。每个图表都标注了它的数据口径与样本量，避免"看着好看但说不清"——
   二手车的保值率、续航、车龄都有多种口径，含糊的图比没有图更危险。
"""

from __future__ import annotations

import json
import shutil
from collections import Counter, defaultdict
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterable, Sequence

from .config import PROJECT_ROOT
from .logging_setup import get_logger
from .storage.repository import Repository, build_engine

log = get_logger("viz")

DASHBOARD_DIR = PROJECT_ROOT / "dashboard"
ECHARTS_FILE = "echarts.min.js"

PRICE_BUCKETS: list[tuple[float, float, str]] = [
    (0, 3, "3万以下"), (3, 5, "3-5万"), (5, 8, "5-8万"), (8, 12, "8-12万"),
    (12, 18, "12-18万"), (18, 25, "18-25万"), (25, 40, "25-40万"), (40, 1e9, "40万以上"),
]
AGE_BUCKETS: list[tuple[int, int, str]] = [
    (0, 1, "1年内"), (1, 2, "1-2年"), (2, 3, "2-3年"), (3, 5, "3-5年"),
    (5, 8, "5-8年"), (8, 11, "8-11年"), (11, 99, "11年以上"),
]
MILEAGE_BUCKETS: list[tuple[int, int, str]] = [
    (0, 10_000, "1万内"), (10_000, 30_000, "1-3万"), (30_000, 60_000, "3-6万"),
    (60_000, 100_000, "6-10万"), (100_000, 150_000, "10-15万"), (150_000, 10**9, "15万以上"),
]

EV_FIELD_LABELS = [
    ("brand", "品牌"), ("model", "车型"), ("price_wan", "售价"), ("new_car_price_wan", "新车指导价"),
    ("retention_rate", "保值率"), ("mileage_km", "里程"), ("reg_year", "上牌年份"),
    ("transfer_count", "过户次数"), ("location_city", "城市"),
    ("battery_type", "电池类型"), ("range_km", "标称续航"), ("battery_health", "电池健康度"),
]
FUEL_FIELD_LABELS = [
    ("brand", "品牌"), ("model", "车型"), ("price_wan", "售价"), ("new_car_price_wan", "新车指导价"),
    ("retention_rate", "保值率"), ("mileage_km", "里程"), ("reg_year", "上牌年份"),
    ("transfer_count", "过户次数"), ("location_city", "城市"),
    ("displacement_l", "排量"), ("gearbox", "变速箱"), ("emission_standard", "排放标准"),
]


# --------------------------------------------------------------------------- #
# 基础工具
# --------------------------------------------------------------------------- #
def _f(value: Any) -> float | None:
    """Decimal / str -> float；不可用时返回 None（缺失不冒充 0）。"""
    if value is None or value == "":
        return None
    if isinstance(value, Decimal):
        return float(value)
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _avg(values: Iterable[float | None]) -> float | None:
    clean = [v for v in values if v is not None]
    return round(sum(clean) / len(clean), 4) if clean else None


def _bucket(value: float | None, buckets: Sequence[tuple[float, float, str]]) -> str | None:
    if value is None:
        return None
    for low, high, label in buckets:
        if low <= value < high:
            return label
    return buckets[-1][2]


def _counter_to_rows(counter: Counter, *, top: int | None = None) -> list[dict[str, Any]]:
    items = counter.most_common(top) if top else counter.most_common()
    return [{"name": str(name), "n": count} for name, count in items if name not in (None, "", "未知")]


def _age_of(reg_year: Any, reg_month: Any) -> float | None:
    """车龄（年，保留一位小数），用于热力图与保值率曲线分箱。"""
    year = _f(reg_year)
    if not year:
        return None
    month = _f(reg_month) or 6
    today = date.today()
    months = (today.year - year) * 12 + (today.month - month)
    if months < 0:
        return None
    return round(months / 12, 1)


# --------------------------------------------------------------------------- #
# 聚合
# --------------------------------------------------------------------------- #
def summarize_line(line: str, records: Sequence[dict[str, Any]]) -> dict[str, Any]:
    """单业务线的全部统计指标。"""
    prices = [_f(r.get("price_wan")) for r in records]
    retentions = [_f(r.get("retention_rate")) for r in records]
    mileages = [_f(r.get("mileage_km")) for r in records]
    ages = [_age_of(r.get("reg_year"), r.get("reg_month")) for r in records]

    brands = Counter(r.get("brand") for r in records)
    cities = Counter(r.get("location_city") for r in records)
    price_dist = Counter(_bucket(p, PRICE_BUCKETS) for p in prices)
    age_dist = Counter(_bucket(a, AGE_BUCKETS) for a in ages)

    # 品牌均价：只保留样本量 >= 3 的品牌，避免单价品牌污染排行
    by_brand_price: dict[str, list[float]] = defaultdict(list)
    by_brand_ret: dict[str, list[float]] = defaultdict(list)
    for record in records:
        brand = record.get("brand")
        if not brand:
            continue
        price = _f(record.get("price_wan"))
        if price:
            by_brand_price[brand].append(price)
        retention = _f(record.get("retention_rate"))
        if retention:
            by_brand_ret[brand].append(retention)

    brand_price_rank = sorted(
        (
            {"name": brand, "avg": round(sum(v) / len(v), 2), "n": len(v)}
            for brand, v in by_brand_price.items() if len(v) >= 3
        ),
        key=lambda item: item["avg"], reverse=True,
    )
    brand_retention_rank = sorted(
        (
            {"name": brand, "avg": round(sum(v) / len(v) * 100, 1), "n": len(v)}
            for brand, v in by_brand_ret.items() if len(v) >= 2
        ),
        key=lambda item: item["avg"], reverse=True,
    )

    # 车龄 x 里程 热力矩阵（行=车龄段，列=里程段）
    heat: dict[tuple[int, int], int] = defaultdict(int)
    for record in records:
        age_label = _bucket(_age_of(record.get("reg_year"), record.get("reg_month")), AGE_BUCKETS)
        mileage_label = _bucket(_f(record.get("mileage_km")), MILEAGE_BUCKETS)
        if age_label is None or mileage_label is None:
            continue
        heat[(AGE[index_of(AGE, age_label)], MILE[index_of(MILE, mileage_label)])] += 1

    # 车龄 -> 平均保值率（保值率趋势，按 1 年粒度归并）
    age_retention: dict[int, list[float]] = defaultdict(list)
    for record in records:
        age = _age_of(record.get("reg_year"), record.get("reg_month"))
        retention = _f(record.get("retention_rate"))
        if age is None or retention is None:
            continue
        age_retention[min(int(age), 12)].append(retention)

    summary: dict[str, Any] = {
        "line": line,
        "count": len(records),
        "brands": len([b for b in brands if b]),
        "cities": len([c for c in cities if c]),
        "avg_price": _avg(prices),
        "median_price": round(sorted([p for p in prices if p])[len([p for p in prices if p]) // 2], 2)
        if any(prices) else None,
        "min_price": min([p for p in prices if p], default=None),
        "max_price": max([p for p in prices if p], default=None),
        "avg_retention": _avg(retentions),
        "avg_mileage": round(_avg(mileages) or 0) if any(mileages) else None,
        "avg_age": _avg(ages),
        "top_brands": _counter_to_rows(brands, top=10),
        "top_cities": _counter_to_rows(cities, top=10),
        "price_dist": [
            {"name": label, "n": price_dist.get(label, 0)} for _, _, label in PRICE_BUCKETS
        ],
        "age_dist": [
            {"name": label, "n": age_dist.get(label, 0)} for _, _, label in AGE_BUCKETS
        ],
        "brand_price_rank": brand_price_rank[:10],
        "brand_retention_rank": brand_retention_rank[:10],
        "age_retention": [
            {"age": age, "rate": round(sum(v) / len(v) * 100, 1), "n": len(v)}
            for age, v in sorted(age_retention.items())
        ],
        "heatmap": {
            "x": [label for _, _, label in MILEAGE_BUCKETS],
            "y": [label for _, _, label in AGE_BUCKETS],
            "data": [[x, y, n] for (y, x), n in heat.items()],
        },
        "field_completeness": [
            {
                "field": field, "label": label,
                "rate": round(sum(1 for r in records if r.get(field) not in (None, "")) / len(records) * 100, 1)
                if records else 0.0,
            }
            for field, label in (EV_FIELD_LABELS if line == "ev" else FUEL_FIELD_LABELS)
        ],
    }
    if line == "ev":
        summary.update(_ev_specifics(records))
    else:
        summary.update(_fuel_specifics(records))
    return summary


AGE = [label for _, _, label in AGE_BUCKETS]
MILE = [label for _, _, label in MILEAGE_BUCKETS]


def index_of(labels: Sequence[str], value: str) -> int:
    return labels.index(value) if value in labels else 0


def _ev_specifics(records: Sequence[dict[str, Any]]) -> dict[str, Any]:
    """新能源线专属统计：电池类型价格分布、续航-价格散点、电池健康度分布。"""
    battery_price: dict[str, list[float]] = defaultdict(list)
    for record in records:
        battery = record.get("battery_type") or "未知"
        price = _f(record.get("price_wan"))
        if price:
            battery_price[battery].append(price)

    scatter = [
        [_f(r.get("range_km")), _f(r.get("price_wan")), r.get("brand") or "", r.get("model") or ""]
        for r in records
        if _f(r.get("range_km")) and _f(r.get("price_wan"))
    ]

    health = [_f(r.get("battery_health")) for r in records]
    health_buckets = [(0, 80, "<80%"), (80, 85, "80-85%"), (85, 90, "85-90%"),
                      (90, 95, "90-95%"), (95, 101, "95%+")]
    health_dist = Counter(_bucket(h, health_buckets) for h in health)
    range_standards = Counter(r.get("range_standard") or "未标注" for r in records if _f(r.get("range_km")))

    return {
        "battery_price": sorted(
            (
                {"name": name, "avg": round(sum(v) / len(v), 2), "n": len(v)}
                for name, v in battery_price.items()
            ),
            key=lambda item: item["n"], reverse=True,
        ),
        "range_price_scatter": [row for row in scatter if row[0] is not None],
        "range_standard_dist": _counter_to_rows(range_standards),
        "health_dist": [
            {"name": label, "n": health_dist.get(label, 0)} for _, _, label in health_buckets
        ],
        "health_avg": _avg(health),
        "health_n": len([h for h in health if h is not None]),
    }


def _fuel_specifics(records: Sequence[dict[str, Any]]) -> dict[str, Any]:
    """燃油线专属统计：排放标准、变速箱、排量分布。"""
    emission = Counter(r.get("emission_standard") for r in records)
    gearbox = Counter(r.get("gearbox") for r in records)
    disp = [_f(r.get("displacement_l")) for r in records]
    disp_buckets = [(0, 1.0, "1.0L以下"), (1.0, 1.6, "1.0-1.6L"), (1.6, 2.0, "1.6-2.0L"),
                    (2.0, 3.0, "2.0-3.0L"), (3.0, 10, "3.0L以上")]
    disp_dist = Counter(_bucket(d, disp_buckets) for d in disp)
    return {
        "emission_dist": _counter_to_rows(emission),
        "gearbox_dist": _counter_to_rows(gearbox),
        "displacement_dist": [
            {"name": label, "n": disp_dist.get(label, 0)} for _, _, label in disp_buckets
        ],
        "displacement_avg": _avg(disp),
    }


def build_payload(repo: Repository, *, limit: int = 100_000) -> dict[str, Any]:
    """从库里读出干净表并算出看板所需的全部数据。"""
    ev_records = repo.query("ev", limit=limit)
    fuel_records = repo.query("fuel", limit=limit)
    logs = repo.recent_logs(limit=20)

    ev_summary = summarize_line("ev", ev_records)
    fuel_summary = summarize_line("fuel", fuel_records)

    payload = {
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "source_note": "瓜子二手车 · 车源详情 Markdown 通道（robots.txt 显式放行）",
        "kpi": {
            "total": len(ev_records) + len(fuel_records),
            "ev": len(ev_records),
            "fuel": len(fuel_records),
            "brands": len({r.get("brand") for r in ev_records + fuel_records if r.get("brand")}),
            "cities": len({r.get("location_city") for r in ev_records + fuel_records if r.get("location_city")}),
            "avg_price": _avg([_f(r.get("price_wan")) for r in ev_records + fuel_records]),
            "avg_retention": _avg([_f(r.get("retention_rate")) for r in ev_records + fuel_records]),
        },
        "compare": _compare_rows(ev_summary, fuel_summary),
        "ev": ev_summary,
        "fuel": fuel_summary,
        "ev_rows": _table_rows("ev", ev_records),
        "fuel_rows": _table_rows("fuel", fuel_records),
        "logs": [_log_row(row) for row in logs],
    }
    return payload


def _compare_rows(ev: dict[str, Any], fuel: dict[str, Any]) -> list[dict[str, Any]]:
    """油电对比：两条线在同一口径下的并排指标。"""
    def pct(value: float | None) -> str:
        return f"{value * 100:.1f}%" if value is not None else "—"

    return [
        {"label": "车源样本量", "ev": f"{ev['count']} 条", "fuel": f"{fuel['count']} 条"},
        {"label": "平均售价", "ev": f"{ev['avg_price']} 万", "fuel": f"{fuel['avg_price']} 万"},
        {"label": "平均保值率", "ev": pct(ev["avg_retention"]), "fuel": pct(fuel["avg_retention"])},
        {"label": "平均车龄",
         "ev": f"{ev['avg_age']} 年" if ev["avg_age"] is not None else "—",
         "fuel": f"{fuel['avg_age']} 年" if fuel["avg_age"] is not None else "—"},
        {"label": "平均里程",
         "ev": f"{ev['avg_mileage']:,} km" if ev["avg_mileage"] is not None else "—",
         "fuel": f"{fuel['avg_mileage']:,} km" if fuel["avg_mileage"] is not None else "—"},
        {"label": "覆盖品牌", "ev": f"{ev['brands']} 个", "fuel": f"{fuel['brands']} 个"},
    ]


def _table_rows(line: str, records: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    """明细表数据（带格式化后的展示字段，前端只负责渲染）。"""
    rows: list[dict[str, Any]] = []
    for record in records:
        row: dict[str, Any] = {
            "brand": record.get("brand") or "—",
            "model": record.get("model") or "—",
            "title": record.get("title_raw") or "",
            "price": _f(record.get("price_wan")),
            "new_price": _f(record.get("new_car_price_wan")),
            "retention": round(_f(record.get("retention_rate")) * 100, 1)
            if _f(record.get("retention_rate")) is not None else None,
            "mileage": record.get("mileage_km"),
            "reg": f"{record.get('reg_year') or '—'}-{int(record.get('reg_month') or 0):02d}"
            if record.get("reg_year") else "—",
            "city": record.get("location_city") or "—",
            "transfer": record.get("transfer_count"),
            "url": record.get("detail_url") or "",
        }
        if line == "ev":
            row.update({
                "battery": record.get("battery_type") or "—",
                "range": record.get("range_km"),
                "range_std": record.get("range_standard") or "—",
                "soh": _f(record.get("battery_health")),
            })
        else:
            row.update({
                "disp": _f(record.get("displacement_l")),
                "gearbox": record.get("gearbox") or "—",
                "emission": record.get("emission_standard") or "—",
            })
        rows.append(row)
    return rows


def _log_row(row: dict[str, Any]) -> dict[str, Any]:
    started, finished = row.get("started_at"), row.get("finished_at")
    seconds = round((finished - started).total_seconds(), 1) if started and finished else None
    return {
        "time": str(started)[:19] if started else "-",
        "line": row.get("business_line"),
        "source": row.get("source_platform"),
        "status": row.get("status"),
        "maps": row.get("pages_fetched"),
        "fetched": row.get("fetched_count"),
        "parsed": row.get("parsed_count"),
        "new": row.get("inserted_count"),
        "updated": row.get("updated_count"),
        "dup": row.get("dup_count"),
        "err": row.get("error_count"),
        "missing": round(float(row.get("missing_ratio") or 0) * 100, 1),
        "seconds": seconds,
        "detail": (row.get("error_detail") or "")[:300],
    }


# --------------------------------------------------------------------------- #
# 渲染
# --------------------------------------------------------------------------- #
def render_html(payload: dict[str, Any]) -> str:
    """把聚合结果注入模板，产出单文件看板。"""
    template = (Path(__file__).parent / "dashboard_template.html").read_text(encoding="utf-8")
    return template.replace("/*__DATA__*/", json.dumps(payload, ensure_ascii=False))


def build_dashboard(
    *,
    repo: Repository | None = None,
    out_dir: Path | None = None,
    limit: int = 100_000,
) -> tuple[Path, dict[str, Any]]:
    """生成看板文件，返回 (HTML 路径, 数据摘要)。"""
    repo = repo or Repository(build_engine(), settings=None)
    out_dir = out_dir or DASHBOARD_DIR
    out_dir.mkdir(parents=True, exist_ok=True)

    payload = build_payload(repo, limit=limit)
    html_path = out_dir / "index.html"
    html_path.write_text(render_html(payload), encoding="utf-8")
    _ensure_echarts(out_dir)

    summary = {
        "html": str(html_path),
        "total": payload["kpi"]["total"],
        "ev": payload["kpi"]["ev"],
        "fuel": payload["kpi"]["fuel"],
        "brands": payload["kpi"]["brands"],
        "cities": payload["kpi"]["cities"],
        "avg_price": payload["kpi"]["avg_price"],
        "avg_retention": payload["kpi"]["avg_retention"],
        "size_kb": round(html_path.stat().st_size / 1024, 1),
    }
    log.info("看板已生成", extra={"path": str(html_path), "total": summary["total"]})
    return html_path, summary


def _ensure_echarts(out_dir: Path) -> None:
    """把 ECharts 放到看板同级目录，使产物可离线打开。

    找不到本地副本时给出明确提示，而不是让页面静默白屏。
    """
    target = out_dir / ECHARTS_FILE
    if target.exists():
        return
    for candidate in (
        PROJECT_ROOT / "assets" / ECHARTS_FILE,
        PROJECT_ROOT / "dashboard" / ECHARTS_FILE,
    ):
        if candidate.exists() and candidate != target:
            shutil.copyfile(candidate, target)
            return
    log.warning(
        "未找到本地 echarts.min.js，看板将尝试从 CDN 加载；建议把 echarts.min.js 放入 dashboard/ 以支持离线演示",
        extra={"expected": str(target)},
    )
