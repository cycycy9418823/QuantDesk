"""Unified daily-bar column normalization (v2.7.3).

This module provides a single entry point to rename heterogeneous Chinese /
English column names from various CSV / Parquet sources into a stable internal
schema used by the low-price report, strategy signals, and data-confidence
modules.
"""

from __future__ import annotations

import pandas as pd

# Mapping: target column -> list of recognized source names (case-insensitive)
_COLUMN_MAP: dict[str, list[str]] = {
    "date": ["date", "日期", "trade_date"],
    "open": ["open", "开盘"],
    "high": ["high", "最高"],
    "low": ["low", "最低"],
    "close": ["close", "收盘"],
    "volume": ["volume", "vol", "成交量"],
    "amount": ["amount", "turnover", "turnover_amount", "amount_yuan", "成交额", "成交额(元)"],
}


def normalize_daily_columns(df: pd.DataFrame) -> pd.DataFrame:
    """Return a copy of *df* with columns renamed to the internal schema.

    Only columns that exist in the input and match a known alias are renamed.
    Columns already using the target name are kept as-is.  Unknown columns are
    passed through unchanged.
    """

    if df.empty:
        return df.copy()
    result = df.copy()
    lower_map: dict[str, str] = {}
    for target, aliases in _COLUMN_MAP.items():
        for alias in aliases:
            lower_map[alias.lower()] = target

    rename: dict[str, str] = {}
    existing_lower = {col.lower(): col for col in result.columns}
    for lower_name, original_name in existing_lower.items():
        target = lower_map.get(lower_name)
        if target and target not in result.columns and original_name != target:
            rename[original_name] = target

    if rename:
        result = result.rename(columns=rename)

    # Coerce numeric columns
    for col in ("open", "high", "low", "close", "volume", "amount"):
        if col in result.columns:
            result[col] = pd.to_numeric(result[col], errors="coerce")

    return result


def has_amount_field(df: pd.DataFrame) -> bool:
    return "amount" in df.columns and df["amount"].notna().any()


def has_volume_field(df: pd.DataFrame) -> bool:
    return "volume" in df.columns and df["volume"].notna().any()
