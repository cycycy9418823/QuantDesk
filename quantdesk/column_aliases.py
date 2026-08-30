"""Centralized column-name aliases for A-share data.

AKShare and other providers may return DataFrames with Chinese or English
column names depending on the API version.  This module provides a single
mapping so downstream code can use the English keys regardless of source.
"""

from __future__ import annotations

from typing import Any

import pandas as pd

# Canonical English → list of known Chinese / legacy aliases
_COLUMN_FORWARD: dict[str, list[str]] = {
    "code": ["代码", "code", "symbol", "ts_code", "股票代码", "证券代码", "ticker"],
    "name": ["名称", "name", "stock_name", "股票名称"],
    "date": ["日期", "date", "trade_date", "datetime", "time", "交易日期"],
    "open": ["开盘", "open", "开盘价", "open_price"],
    "high": ["最高", "high", "最高价"],
    "low": ["最低", "low", "最低价"],
    "close": ["收盘", "close", "收盘价", "price", "latest_close", "last_close", "current_price"],
    "volume": ["成交量", "volume", "vol", "成交量(股)", "latest_volume", "turnover_volume"],
    "amount": ["成交额", "amount", "turnover", "money", "成交额(元)", "turnover_amount", "amount_yuan", "amount_value"],
    "pct_chg": ["涨跌幅", "pct_chg", "change_pct", "pct_change"],
    "change": ["涨跌额", "涨跌", "change", "price_change"],
    "pre_close": ["昨收", "pre_close", "prev_close"],
    "sector": ["板块", "sector", "industry", "行业"],
    "pe": ["市盈率", "pe", "pe_ttm"],
    "pb": ["市净率", "pb"],
    "market_cap": ["总市值", "market_cap", "total_mv"],
}

# Chinese → English flat map (for direct rename_dict usage)
COLUMN_FORWARD_MAP: dict[str, str] = {
    alias: canonical
    for canonical, aliases in _COLUMN_FORWARD.items()
    for alias in aliases
    if alias != canonical
}

# English → Chinese flat map (first Chinese alias for display)
COLUMN_REVERSE_MAP: dict[str, str] = {
    canonical: aliases[0]
    for canonical, aliases in _COLUMN_FORWARD.items()
    if aliases and aliases[0] != canonical
}


def normalize_columns(df: pd.DataFrame) -> pd.DataFrame:
    """Rename DataFrame columns to canonical English names."""
    mapping: dict[str, str] = {}
    for canonical, aliases in _COLUMN_FORWARD.items():
        for alias in aliases:
            if alias in df.columns and alias != canonical:
                mapping[alias] = canonical
                break
    return df.rename(columns=mapping)


def find_column(df: pd.DataFrame, canonical: str) -> str | None:
    """Return the actual column name in *df* that maps to *canonical*."""
    aliases = _COLUMN_FORWARD.get(canonical, [canonical])
    for alias in aliases:
        if alias in df.columns:
            return alias
    return None


def get_value(row: Any, canonical: str, default: Any = None) -> Any:
    """Read a value from a Series/dict/row using any known alias for *canonical*."""
    aliases = _COLUMN_FORWARD.get(canonical, [canonical])
    if isinstance(row, pd.Series):
        for alias in aliases:
            if alias in row.index:
                return row.get(alias, default)
    elif isinstance(row, dict):
        for alias in aliases:
            if alias in row:
                return row.get(alias, default)
    return default
