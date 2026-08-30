from __future__ import annotations

import hashlib
import math
from decimal import Decimal, InvalidOperation
from typing import Any

DAILY_HASH_FIELDS = ["symbol", "date", "open", "high", "low", "close", "volume", "amount"]
FUNDAMENTAL_HASH_FIELDS = ["symbol", "report_date", "revenue", "net_profit", "equity", "eps", "roe", "pe", "pb"]
SECTOR_HASH_FIELDS = ["symbol", "sector_name", "sector_type", "source"]


def normalize_hash_value(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        if math.isnan(value) or math.isinf(value):
            return ""
        return f"{value:.6f}"
    text = str(value).strip()
    if not text:
        return ""
    try:
        dec = Decimal(text)
    except (InvalidOperation, ValueError):
        return text
    if dec.is_finite():
        return f"{float(dec):.6f}"
    return ""


def compute_row_hash(row: dict, hash_fields: list[str]) -> str:
    payload = "|".join(normalize_hash_value(row.get(field)) for field in hash_fields)
    return hashlib.md5(payload.encode("utf-8")).hexdigest()[:16]
