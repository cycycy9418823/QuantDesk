"""Narrow-table parquet storage for daily bars (tickflow 精华落地).

Design — store only what cannot be recomputed from base OHLCV:
  - adjusted OHLC (qfq/hfq) *and* raw (unadjusted) OHLC, kept side-by-side so
    limit-up/down detection can use true unadjusted prices;
  - volume / amount / turnover_rate;
  - the adjustment provenance columns (``adjust`` / ``adj_factor`` / ``source``).

Everything derived (MA / EMA / MACD / RSI / KDJ / ATR / 信号列 ...) is computed
on read by the indicator layer, never persisted.  This avoids the "add one
indicator -> rewrite every parquet" maintenance trap while keeping the on-disk
footprint small and the adjustment convention explicit on every row.

This module is storage-only: it normalizes via :mod:`quantdesk.enriched.normalize`,
asserts a single adjustment convention per frame, and round-trips parquet.  It
never downloads, trades, or computes indicators.
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd

from quantdesk.enriched.adjust import (
    ADJUST_UNKNOWN,
    adjust_of_frame,
    assert_adjust_consistency,
)
from quantdesk.enriched.normalize import normalize_daily_frame

#: Columns persisted to the narrow parquet table.  Anything not here is treated
#: as derived and recomputed on read.
NARROW_STORE_COLUMNS: tuple[str, ...] = (
    "date",
    "symbol",
    # adjusted OHLC (the working price series)
    "open",
    "high",
    "low",
    "close",
    # raw (unadjusted) OHLC — needed for true limit-up/down detection
    "raw_open",
    "raw_high",
    "raw_low",
    "raw_close",
    # volume / value / turnover
    "volume",
    "amount",
    "turnover_rate",
    # provenance / adjustment metadata
    "adjust",
    "adj_factor",
    "source",
    "updated_at",
)


def _present_columns(frame: pd.DataFrame) -> list[str]:
    return [column for column in NARROW_STORE_COLUMNS if column in frame.columns]


def to_narrow_frame(frame: pd.DataFrame, *, assert_single_adjust: bool = True) -> pd.DataFrame:
    """Normalize *frame* and project it onto the narrow storage schema.

    Raises :class:`AdjustConsistencyError` when *assert_single_adjust* is set
    and the frame mixes adjustment conventions (e.g. qfq and raw rows together),
    which would silently corrupt downstream price math.
    """

    normalized = normalize_daily_frame(frame)
    if assert_single_adjust and not normalized.empty:
        assert_adjust_consistency(normalized)
    keep = _present_columns(normalized)
    return normalized.loc[:, keep].reset_index(drop=True)


def write_narrow_parquet(
    frame: pd.DataFrame,
    path: Path | str,
    *,
    assert_single_adjust: bool = True,
) -> dict[str, object]:
    """Write *frame* to *path* as a narrow parquet table.

    Returns a small summary dict: ``rows``, ``adjust`` (the single convention
    detected), ``columns`` and ``path``.
    """

    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    narrow = to_narrow_frame(frame, assert_single_adjust=assert_single_adjust)
    narrow.to_parquet(target, index=False)
    return {
        "rows": int(len(narrow)),
        "adjust": adjust_of_frame(narrow) if not narrow.empty else ADJUST_UNKNOWN,
        "columns": list(narrow.columns),
        "path": str(target),
    }


def read_narrow_parquet(path: Path | str) -> pd.DataFrame:
    """Read a narrow parquet table back as a normalized daily frame."""

    target = Path(path)
    if not target.exists():
        return pd.DataFrame(columns=list(NARROW_STORE_COLUMNS))
    frame = pd.read_parquet(target)
    # Re-normalize on read so callers always get the canonical schema even if
    # the file was written by an older layout.
    return normalize_daily_frame(frame)


__all__ = [
    "NARROW_STORE_COLUMNS",
    "read_narrow_parquet",
    "to_narrow_frame",
    "write_narrow_parquet",
]
