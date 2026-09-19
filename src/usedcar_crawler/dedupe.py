"""去重与归并。

两层去重：
1. **主键去重**：``vehicle_key = MD5(平台 + 车源ID)``，同平台同车源永远一条记录，
   重复抓取走 UPDATE（价格变化就是业务价值本身）；
2. **跨源归并**：``match_key = 品牌|车型|年款|里程段|上牌月``，把不同平台上的同一台车
   归到一个可比的组，为"比价"与"价格合理性校验"提供基础。
"""

from __future__ import annotations

import hashlib

from .pipeline.cleaners import clean_text

MILEAGE_BUCKET_KM = 5_000  # 里程分段粒度


def _md5(*parts: object) -> str:
    payload = "|".join("" if p is None else str(p) for p in parts)
    return hashlib.md5(payload.encode("utf-8")).hexdigest()


def make_vehicle_key(source_platform: object, source_id: object, *, fallback: object = None) -> str:
    """生成车源主键。

    优先使用平台车源 ID；无 ID 时退化为"标题+价格+里程"的指纹，保证幂等可重跑。
    """
    platform = clean_text(source_platform) or "unknown"
    sid = clean_text(source_id)
    if sid is None and fallback is not None:
        sid = clean_text(fallback)
    return f"{platform[:8]}_{_md5(platform, sid)}"


def make_match_key(
    *,
    brand: object,
    model: object,
    reg_year: object,
    mileage_km: object,
    reg_month: object = None,
) -> str | None:
    """生成跨源同车归并键；关键维度缺失时返回 None（宁可不归并，不可错归并）。"""
    brand_text = clean_text(brand)
    model_text = clean_text(model)
    if brand_text is None or model_text is None or reg_year is None:
        return None
    bucket = "na"
    if isinstance(mileage_km, int) and mileage_km >= 0:
        bucket = str(mileage_km // MILEAGE_BUCKET_KM)
    month = f"{int(reg_month):02d}" if isinstance(reg_month, int) else "na"
    return f"{brand_text}|{model_text}|{int(reg_year)}|{month}|{bucket}"


def dedupe_by_key(records: list[dict], key: str = "vehicle_key") -> tuple[list[dict], int]:
    """批内去重，保留后出现的记录（后出现的通常信息更完整）。

    :return: (去重后记录, 被丢弃条数)
    """
    if not records:
        return [], 0
    seen: dict[str, dict] = {}
    for record in records:
        value = record.get(key)
        if value is None:
            continue
        seen[value] = record
    return list(seen.values()), len(records) - len(seen)


def split_upsert(candidates: list[dict], existing_keys: set[str], key: str = "vehicle_key") -> tuple[list[dict], list[dict]]:
    """按主键是否已存在，拆分为 (待插入, 待更新) 两批。"""
    to_insert, to_update = [], []
    for record in candidates:
        (to_update if record.get(key) in existing_keys else to_insert).append(record)
    return to_insert, to_update
