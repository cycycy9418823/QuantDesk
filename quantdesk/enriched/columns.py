"""Canonical daily-bar column schema for the unified enriched layer.

This module reconciles the four pre-existing column normalizers that diverged
across the codebase:

- ``quantdesk.column_aliases``        (16 canonical names, ``code``, treats
                                       ``turnover`` as an *amount* alias)
- ``quantdesk.daily_schema``          (7 names, OHLCV only, no symbol)
- ``quantdesk.data.schema_normalizer`` (11 names, ``symbol``, ``turnover`` ==
                                       turnover *rate*)
- ``daily_price_providers.DAILY_COLUMN_ALIASES`` (adds ``adjust`` / ``source``)

The most important conflict resolved here is the **``turnover`` collision**:

- In ``column_aliases`` it was an alias of *amount* (成交额).
- In ``schema_normalizer`` it was the standalone *turnover rate* (换手率).

We disambiguate by using two distinct canonical names and never the bare word
``turnover`` as a target:

- ``amount``        -> 成交额 (turnover value in yuan)
- ``turnover_rate`` -> 换手率 (turnover ratio, fraction or percent)

Any literal source column named exactly ``turnover`` is treated as *amount*
(matching the historically dominant ``column_aliases`` behaviour and the
provider CSVs), while ``换手率`` / ``turnover_rate`` map to ``turnover_rate``.
"""

from __future__ import annotations

from typing import Literal

AdjustmentMode = Literal["raw", "qfq", "hfq", "unknown"]

#: All recognized adjustment modes.  ``qfq`` = 前复权 (forward), ``hfq`` =
#: 后复权 (backward), ``raw`` = 不复权, ``unknown`` = not determinable.
ADJUSTMENT_MODES: tuple[AdjustmentMode, ...] = ("raw", "qfq", "hfq", "unknown")

#: Canonical daily-bar columns, in stable order.  This is the union of all four
#: legacy normalizers plus the explicit ``adjust`` provenance column.
CANONICAL_DAILY_COLUMNS: tuple[str, ...] = (
    "date",
    "symbol",
    "name",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "amount",
    "pct_chg",
    "change",
    "pre_close",
    "turnover_rate",
    # raw (unadjusted) prices kept alongside adjusted OHLC so limit-up/down
    # detection can use true unadjusted prices (tickflow精华: store raw + adj).
    "raw_open",
    "raw_high",
    "raw_low",
    "raw_close",
    # provenance / adjustment metadata
    "adjust",
    "adj_factor",
    "source",
    "updated_at",
)

#: Columns that must be present (after normalization) for a frame to be usable.
REQUIRED_DAILY_COLUMNS: tuple[str, ...] = ("date", "close")

#: canonical name -> recognized source aliases (case-insensitive match).
#: Order within each list is priority order when multiple aliases are present.
#:
#: NOTE on ``turnover``: it is intentionally listed only under ``amount`` (its
#: historically dominant meaning in this codebase + provider CSVs).  The
#: turnover *rate* is captured separately under ``turnover_rate`` via 换手率 /
#: ``turnover_rate`` so the two never collide.
COLUMN_ALIASES: dict[str, list[str]] = {
    "date": ["date", "日期", "trade_date", "datetime", "time", "交易日期"],
    "symbol": ["symbol", "code", "ts_code", "股票代码", "代码", "证券代码", "ticker"],
    "name": ["name", "名称", "stock_name", "股票名称"],
    "open": ["open", "开盘", "开盘价", "open_price"],
    "high": ["high", "最高", "最高价"],
    "low": ["low", "最低", "最低价"],
    "close": [
        "close", "收盘", "收盘价", "price", "latest_close", "last_close",
        "current_price",
    ],
    "volume": ["volume", "成交量", "vol", "成交量(股)", "latest_volume", "turnover_volume"],
    # ``turnover`` here means 成交额 (value), matching column_aliases + CSVs.
    "amount": [
        "amount", "成交额", "turnover", "money", "成交额(元)", "turnover_amount",
        "amount_yuan", "amount_value",
    ],
    "pct_chg": ["pct_chg", "涨跌幅", "change_pct", "pct_change"],
    "change": ["change", "涨跌额", "涨跌", "price_change", "change_amount"],
    "pre_close": ["pre_close", "昨收", "prev_close", "previous_close", "昨收价"],
    # turnover *rate* (换手率) — kept strictly separate from ``amount``.
    "turnover_rate": ["turnover_rate", "换手率", "turnover_ratio", "hsl"],
    "raw_open": ["raw_open", "不复权开盘"],
    "raw_high": ["raw_high", "不复权最高"],
    "raw_low": ["raw_low", "不复权最低"],
    "raw_close": ["raw_close", "不复权收盘", "raw_price"],
    "adjust": ["adjust", "复权", "adjustment", "adj_mode"],
    "adj_factor": ["adj_factor", "复权因子", "adjustment_factor", "factor"],
    "source": ["source", "数据源", "data_source", "data_effective_source"],
    "updated_at": ["updated_at", "更新时间", "fetched_at"],
}

# Build a flat lower-cased alias -> canonical lookup once at import time.
_ALIAS_TO_CANONICAL: dict[str, str] = {}
for _canon, _aliases in COLUMN_ALIASES.items():
    for _alias in _aliases:
        _ALIAS_TO_CANONICAL.setdefault(_alias.lower(), _canon)


# Public alias for callers that want the flat lookup table directly.
ALIAS_TO_CANONICAL: dict[str, str] = _ALIAS_TO_CANONICAL


#: Canonical columns that should be coerced to numeric (float) on normalization.
CANONICAL_NUMERIC_COLUMNS: tuple[str, ...] = (
    "open", "high", "low", "close", "volume", "amount",
    "pct_chg", "change", "pre_close", "turnover_rate",
    "raw_open", "raw_high", "raw_low", "raw_close", "adj_factor",
)


def normalize_adjust_token(value: object) -> AdjustmentMode:
    """Normalize a free-form adjustment marker into a canonical mode token.

    Accepts the many spellings seen across providers / CSVs and collapses them
    to one of :data:`ADJUSTMENT_MODES` (``raw``/``qfq``/``hfq``/``unknown``).
    An empty / unrecognized value becomes ``"unknown"`` (never silently ``raw``)
    so that callers can tell "explicitly unadjusted" apart from "we don't know".
    """

    text = str(value or "").strip().lower()
    if text in {"qfq", "前复权", "forward", "forward_adjusted", "qfq_factor"}:
        return "qfq"
    if text in {"hfq", "后复权", "backward", "back_adjusted", "back"}:
        return "hfq"
    if text in {"raw", "none", "不复权", "no", "unadjusted", "原始"}:
        return "raw"
    return "unknown"


def canonical_name(column: str) -> str | None:
    """Return the canonical name for *column*, or ``None`` if unrecognized.

    Matching is case-insensitive.  An already-canonical name maps to itself.
    """

    if not column:
        return None
    key = str(column).strip().lower()
    if key in _ALIAS_TO_CANONICAL:
        return _ALIAS_TO_CANONICAL[key]
    # An exact canonical name (e.g. passed through twice) maps to itself.
    if str(column).strip() in CANONICAL_DAILY_COLUMNS:
        return str(column).strip()
    return None


__all__ = [
    "ADJUSTMENT_MODES",
    "ALIAS_TO_CANONICAL",
    "CANONICAL_DAILY_COLUMNS",
    "CANONICAL_NUMERIC_COLUMNS",
    "COLUMN_ALIASES",
    "REQUIRED_DAILY_COLUMNS",
    "AdjustmentMode",
    "canonical_name",
    "normalize_adjust_token",
]
