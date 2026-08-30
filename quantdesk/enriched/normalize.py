"""Single daily-bar column normalization entry point for QuantDesk.

This module unifies the previously divergent normalizers that lived in:
  - ``column_aliases.normalize_columns``           (16 canonical cols, code/name/估值)
  - ``daily_schema.normalize_daily_columns``       (7 cols, OHLCV only)
  - ``data.schema_normalizer.normalize_columns``   (11 cols, turnover=换手率)
  - ``daily_price_providers.normalize_daily_df``   (11 cols, only one with adjust)

The key reconciliation it performs:

1. **``turnover`` semantic conflict.**  In ``column_aliases`` the alias
   ``turnover`` mapped to *amount* (成交额); in ``schema_normalizer`` ``换手率``
   mapped to a distinct *turnover_rate* column.  This module keeps the two
   strictly separate: money value is always ``amount``; turnover *rate* is
   always ``turnover_rate``.  A bare English ``turnover`` header is treated as
   *amount* only when no explicit amount column is present (legacy behaviour),
   otherwise ignored, and is never silently merged into ``turnover_rate``.

2. **Adjustment provenance.**  Carries an explicit ``adjust`` column
   (``qfq`` / ``hfq`` / ``raw`` / ``""``-unknown) so downstream code never has
   to guess the price-adjustment convention.  See :mod:`quantdesk.enriched.adjust`.

It is read/compute only: it never downloads data or places orders.
"""

from __future__ import annotations

from typing import Any

import pandas as pd

from quantdesk.enriched.columns import (
    ALIAS_TO_CANONICAL,
    CANONICAL_NUMERIC_COLUMNS,
    normalize_adjust_token,
)


def _build_rename_map(columns: list[str]) -> dict[str, str]:
    """Resolve heterogeneous source headers to canonical names.

    Matching is case-insensitive for ASCII headers; Chinese aliases match
    verbatim.  A canonical target is only assigned once (first source column
    wins) so we never collapse two distinct source columns onto one target.
    """

    rename: dict[str, str] = {}
    taken: set[str] = set()
    # Canonical columns already present keep their slot.
    for col in columns:
        if col in CANONICAL_NUMERIC_COLUMNS or col in {"date", "symbol", "name", "adjust", "source", "updated_at"}:
            taken.add(col)

    for col in columns:
        if col in taken and col in {str(c) for c in columns if c == col}:
            # already canonical
            if col in ALIAS_TO_CANONICAL and ALIAS_TO_CANONICAL[col] == col:
                continue
        key = str(col).strip()
        canonical = ALIAS_TO_CANONICAL.get(key)
        if canonical is None:
            canonical = ALIAS_TO_CANONICAL.get(key.lower())
        if canonical is None:
            continue
        if canonical in taken:
            # target already filled by a more authoritative column; skip.
            continue
        if canonical != key:
            rename[col] = canonical
        taken.add(canonical)
    return rename


def normalize_daily_frame(
    df: pd.DataFrame | None,
    *,
    symbol: str | None = None,
    adjust: str | None = None,
) -> pd.DataFrame:
    """Return a copy of *df* with canonical column names and dtypes.

    Parameters
    ----------
    df:
        Raw daily-bar frame from any provider / CSV / parquet source.
    symbol:
        Optional symbol to stamp onto a ``symbol`` column when absent.
    adjust:
        Optional explicit adjustment token (``qfq``/``hfq``/``raw``).  When
        provided it overrides any inferred value; when omitted the existing
        ``adjust`` column (if any) is normalized and otherwise left blank
        (= unknown), never guessed.
    """

    if df is None or len(df) == 0:
        empty = pd.DataFrame(columns=["date", "symbol", "open", "high", "low", "close", "volume", "amount", "adjust"])
        return empty

    result = df.copy()
    rename = _build_rename_map(list(result.columns))
    if rename:
        result = result.rename(columns=rename)
    # Drop duplicate columns created by aliasing (keep first).
    result = result.loc[:, ~result.columns.duplicated()]

    if "date" in result.columns:
        result["date"] = pd.to_datetime(result["date"], errors="coerce")

    if "symbol" not in result.columns and symbol is not None:
        result["symbol"] = str(symbol)

    for col in CANONICAL_NUMERIC_COLUMNS:
        if col in result.columns:
            result[col] = pd.to_numeric(result[col], errors="coerce")

    # Adjustment provenance — explicit, never guessed.
    if adjust is not None:
        result["adjust"] = normalize_adjust_token(adjust)
    elif "adjust" in result.columns:
        result["adjust"] = result["adjust"].map(normalize_adjust_token)
    else:
        result["adjust"] = ""

    if "date" in result.columns:
        result = result.dropna(subset=["date"]).sort_values("date").reset_index(drop=True)

    return result


def normalized_value(row: Any, canonical: str, default: Any = None) -> Any:
    """Read a canonical field from a dict/Series using any known alias.

    Convenience for call sites that still receive un-normalized rows.
    """

    if canonical in (row.index if isinstance(row, pd.Series) else row):
        value = row[canonical] if isinstance(row, pd.Series) else row.get(canonical)
        if value is not None:
            return value
    # fall back to scanning aliases
    for alias, target in ALIAS_TO_CANONICAL.items():
        if target != canonical:
            continue
        container = row.index if isinstance(row, pd.Series) else row
        if alias in container:
            value = row[alias] if isinstance(row, pd.Series) else row.get(alias)
            if value is not None:
                return value
    return default


__all__ = ["normalize_daily_frame", "normalized_value"]
