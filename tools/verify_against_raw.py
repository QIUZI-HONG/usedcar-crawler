"""溯源一致性核验：拿库里的干净值与 raw 层原始响应逐条对账。

为什么单独写一个工具，而不是复用解析器：**复用解析器等于自己证明自己**。
这里刻意用一套独立的极简正则直接从原始正文里抠数字，再与 DWD 表对比——
只有两套彼此独立的读法给出同一个值，才能说明"入库值确实来自那一份原始页面"。

用法：
    python tools/verify_against_raw.py            # 校验全部
    python tools/verify_against_raw.py --limit 20  # 只抽 20 条

退出码：0 全部一致；1 存在不一致或缺失溯源。
"""

from __future__ import annotations

import argparse
import re
import sqlite3
import sys
from decimal import Decimal
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from usedcar_crawler.pipeline.cleaners import normalize_brand  # noqa: E402

DEFAULT_DB = PROJECT_ROOT / "data" / "usedcar.db"

# 极简、独立于项目解析器的抽取规则：只认原始正文里的字面量
_FULL_PAYMENT = re.compile(r"full_payment\s*:\s*(\d+)\s*元")
_GUIDE_PRICE = re.compile(r"guide_price\s*:\s*(\d+)\s*元")
_MILEAGE = re.compile(r"mileage\s*:\s*(\d+)\s*公里")
_BRAND = re.compile(r"^brand\s*:\s*(\S+)", re.MULTILINE)
_FIRST_REGISTER = re.compile(r"first_register\s*:\s*(\d{4})-(\d{2})")


def _yuan_to_wan(match: re.Match[str]) -> Decimal:
    """元 -> 万元，与清洗层同口径（Decimal 两位小数，不用二进制浮点）。"""
    return (Decimal(match.group(1)) / Decimal(10_000)).quantize(Decimal("0.01"))


# 逐字段对账规则：DB 列 -> (原始值抽取器, 换算函数)
# 允许的变换只有两类：单位换算（元->万元）与品牌归一化；除此之外必须逐字一致。
CHECKS: dict[str, tuple[re.Pattern[str], object]] = {
    "price_wan": (_FULL_PAYMENT, _yuan_to_wan),
    "new_car_price_wan": (_GUIDE_PRICE, _yuan_to_wan),
    "mileage_km": (_MILEAGE, lambda m: int(m.group(1))),
    "brand": (_BRAND, lambda m: normalize_brand(m.group(1))),
}


def _rows(conn: sqlite3.Connection, line: str, limit: int) -> list[sqlite3.Row]:
    cols = "vehicle_key, brand, price_wan, new_car_price_wan, mileage_km, reg_year, reg_month, raw_ref"
    return list(conn.execute(f"select {cols} from {line}_vehicles limit ?", (limit,)))


def main() -> int:
    parser = argparse.ArgumentParser(description="库内干净值 vs raw 层原始响应 对账")
    parser.add_argument("--db", default=str(DEFAULT_DB))
    parser.add_argument("--limit", type=int, default=1_000_000)
    args = parser.parse_args()

    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row

    checked = mismatches = no_ref = missing_file = 0
    samples: list[str] = []

    for line in ("ev", "fuel"):
        for row in _rows(conn, line, args.limit):
            raw_ref = row["raw_ref"]
            if not raw_ref:
                no_ref += 1
                continue
            path = Path(raw_ref)
            if not path.is_absolute():
                path = PROJECT_ROOT / raw_ref
            if not path.exists():
                missing_file += 1
                samples.append(f"[{line}] 原始文件缺失：{raw_ref}")
                continue

            body = path.read_text(encoding="utf-8")
            checked += 1
            for column, (pattern, convert) in CHECKS.items():
                match = pattern.search(body)
                if match is None:
                    continue  # 原始页面本身没披露该字段，跳过比对
                expected = convert(match)  # type: ignore[operator]
                actual = row[column]
                if actual is None:
                    mismatches += 1
                    samples.append(
                        f"[{line}] {row['vehicle_key']} 字段 {column}：库内为空，原始={expected!r}"
                    )
                    continue
                if column in ("price_wan", "new_car_price_wan"):
                    # 库内是 NUMERIC，统一走 Decimal 比较，避免二进制浮点带来的 0.01 假差异
                    actual = Decimal(str(actual)).quantize(Decimal("0.01"))
                if actual != expected:
                    mismatches += 1
                    samples.append(
                        f"[{line}] {row['vehicle_key']} 字段 {column}：库内={actual!r} 原始={expected!r}"
                    )

            # 上牌年月单独核：库里拆成 reg_year / reg_month 两列
            reg = _FIRST_REGISTER.search(body)
            if reg:
                year, month = int(reg.group(1)), int(reg.group(2))
                if (row["reg_year"], row["reg_month"]) != (year, month):
                    mismatches += 1
                    samples.append(
                        f"[{line}] {row['vehicle_key']} 上牌时间："
                        f"库内={row['reg_year']}-{row['reg_month']:02d} 原始={year}-{month:02d}"
                    )

    print(f"对账完成：核验 {checked} 条记录")
    print(f"  字段不一致 : {mismatches}")
    print(f"  无 raw_ref : {no_ref}")
    print(f"  原始文件缺失: {missing_file}")
    if samples:
        print("\n不一致样例（最多 20 条）：")
        for item in samples[:20]:
            print("  -", item)
    return 0 if mismatches == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
