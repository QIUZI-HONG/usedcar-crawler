"""导出层：把库里的干净数据交付成业务方可直接使用的 Excel / CSV。

交付细节决定"数据能不能被人用起来"：
- xlsx 附第二个 sheet「字段说明」，取数的人不用来问字段含义；
- 数值列统一转 float，避免 Decimal 在 Excel 里变成文本；
- 行数超阈值自动切 CSV（大表用 Excel 打开对谁都是折磨）。
"""

from __future__ import annotations

import json
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterable, Sequence

from ..config import PROJECT_ROOT, ExportCfg, get_settings
from ..logging_setup import get_logger

log = get_logger("exporter")

# 导出列顺序（也是对外承诺的字段契约，与 docs/data-dictionary.md 一致）
COMMON_COLUMNS = [
    "vehicle_key", "source_platform", "title_raw", "brand", "model",
    "price_wan", "new_car_price_wan", "retention_rate", "mileage_km",
    "reg_year", "reg_month", "transfer_count", "location_city",
    "detail_url", "captured_at",
]
EV_COLUMNS = ["battery_type", "range_km", "range_standard", "battery_health", "fast_charge_kw"]
FUEL_COLUMNS = ["displacement_l", "gearbox", "emission_standard"]

COLUMN_LABELS: dict[str, str] = {
    "vehicle_key": "车源唯一键",
    "source_platform": "来源平台",
    "title_raw": "原始标题",
    "brand": "品牌",
    "model": "车型",
    "price_wan": "售价(万元)",
    "new_car_price_wan": "新车指导价(万元)",
    "retention_rate": "保值率",
    "mileage_km": "里程(公里)",
    "reg_year": "上牌年份",
    "reg_month": "上牌月份",
    "transfer_count": "过户次数",
    "location_city": "车辆所在地",
    "detail_url": "详情链接",
    "captured_at": "抓取时间",
    "battery_type": "电池类型",
    "range_km": "标称续航(公里)",
    "range_standard": "续航口径",
    "battery_health": "电池健康度SOH(%)",
    "fast_charge_kw": "快充功率(kW)",
    "displacement_l": "排量(升)",
    "gearbox": "变速箱",
    "emission_standard": "排放标准",
}


def columns_for(line: str) -> list[str]:
    extra = EV_COLUMNS if line == "ev" else FUEL_COLUMNS
    return COMMON_COLUMNS + extra


def _to_excel_friendly(value: Any) -> Any:
    """Decimal / datetime / dict 等类型转成 Excel 友好的值。"""
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%d %H:%M:%S")
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False)
    return value


def export_records(
    line: str,
    records: Sequence[dict[str, Any]],
    *,
    cfg: ExportCfg | None = None,
    fmt: str | None = None,
    tag: str = "",
) -> list[Path]:
    """把记录导出为文件，返回生成的文件路径列表。

    :param fmt: ``xlsx`` / ``csv`` / ``both``；默认按配置
    """
    settings = get_settings()
    cfg = cfg or settings.export
    formats = list(cfg.formats) if fmt in (None, "both") else [fmt]
    out_dir = Path(cfg.dir)
    if not out_dir.is_absolute():
        out_dir = PROJECT_ROOT / out_dir
    out_dir.mkdir(parents=True, exist_ok=True)

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    suffix = f"_{tag}" if tag else ""
    base_name = f"{line}_vehicles_{stamp}{suffix}"
    columns = columns_for(line)

    rows = [{key: _to_excel_friendly(record.get(key)) for key in columns} for record in records]
    labelled = [{COLUMN_LABELS.get(c, c): row[c] for c in columns} for row in rows]

    outputs: list[Path] = []
    try:
        import pandas as pd  # noqa: PLC0415 - 导出为可选重依赖，按需导入
    except ModuleNotFoundError:  # pragma: no cover
        raise RuntimeError("导出功能需要 pandas：pip install pandas openpyxl") from None

    frame = pd.DataFrame(labelled, columns=[COLUMN_LABELS.get(c, c) for c in columns])

    if "csv" in formats:
        csv_path = out_dir / f"{base_name}.csv"
        frame.to_csv(csv_path, index=False, encoding="utf-8-sig")  # BOM 让 Excel 正确识别中文
        outputs.append(csv_path)

    if "xlsx" in formats:
        if len(frame) > cfg.xlsx_max_rows:
            log.warning(
                "记录数超过 xlsx 阈值，已自动降级为 CSV",
                extra={"rows": len(frame), "threshold": cfg.xlsx_max_rows},
            )
        else:
            xlsx_path = out_dir / f"{base_name}.xlsx"
            with pd.ExcelWriter(xlsx_path, engine="openpyxl") as writer:
                frame.to_excel(writer, sheet_name="车源明细", index=False)
                _write_dictionary_sheet(writer, columns)
                _autofit(writer, frame)
            outputs.append(xlsx_path)

    log.info("导出完成", extra={"line": line, "rows": len(frame), "files": len(outputs)})
    return outputs


def _write_dictionary_sheet(writer: Any, columns: Iterable[str]) -> None:
    """在 Excel 中附带字段说明 sheet —— 让业务方自助理解数据。"""
    import pandas as pd  # noqa: PLC0415

    rows = [{"字段": COLUMN_LABELS.get(col, col), "字段名": col} for col in columns]
    pd.DataFrame(rows).to_excel(writer, sheet_name="字段说明", index=False)


def _autofit(writer: Any, frame: Any) -> None:
    """列宽自适应（中文字符按 2 个宽度估算）。"""
    worksheet = writer.sheets.get("车源明细")
    if worksheet is None:  # pragma: no cover
        return
    for index, column in enumerate(frame.columns, start=1):
        width = max(
            [len(str(column)) * 2] + [min(len(str(value)) * 2, 60) for value in frame[column].head(200)]
        )
        worksheet.column_dimensions[worksheet.cell(row=1, column=index).column_letter].width = max(12, width)


def build_summary(line: str, stats: dict[str, Any]) -> Path:
    """生成一页纸的数据质量摘要（Markdown），用于投递材料与周报。"""
    out_dir = PROJECT_ROOT / get_settings().export.dir
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{line}_summary_{date.today().isoformat()}.md"
    lines = [
        f"# {line.upper()} 业务线数据摘要（{date.today().isoformat()}）",
        "",
        "| 指标 | 数值 |",
        "| --- | --- |",
        f"| 总车源数 | {stats.get('total')} |",
        f"| 有效在售 | {stats.get('active')} |",
        f"| 已下架 | {stats.get('deleted')} |",
        f"| 平均售价（万元） | {stats.get('avg_price_wan')} |",
        f"| 价格区间（万元） | {stats.get('min_price_wan')} ~ {stats.get('max_price_wan')} |",
        f"| 平均保值率 | {stats.get('avg_retention_rate')} |",
        "",
        "## 数据来源分布",
        "",
    ]
    for platform, count in (stats.get("by_platform") or {}).items():
        lines.append(f"- {platform}: {count} 条")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path
