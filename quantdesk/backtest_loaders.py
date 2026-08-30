from __future__ import annotations

import math
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from quantdesk.csv_io import read_csv_frame
from quantdesk.shared import normalize_symbol


def _safe_number(value: Any, default: float = np.nan) -> float:
    try:
        number = float(value)
        if math.isfinite(number):
            return number
    except (TypeError, ValueError):
        pass
    return default


def benchmark_total_return(
    benchmark_series: pd.Series | None,
    *,
    start_date: Any | None = None,
    end_date: Any | None = None,
) -> float:
    """Return local benchmark total return, optionally aligned to a date window."""

    if benchmark_series is None:
        return np.nan
    series = pd.Series(benchmark_series).dropna()
    if (
        (start_date is not None or end_date is not None)
        and len(series)
        and not isinstance(series.index, pd.RangeIndex)
        and not pd.api.types.is_numeric_dtype(series.index)
    ):
        index_dates = pd.to_datetime(series.index, errors="coerce")
        if not pd.isna(index_dates).all():
            mask = pd.Series(True, index=series.index)
            if start_date is not None:
                start = pd.to_datetime(start_date, errors="coerce")
                if pd.notna(start):
                    mask &= index_dates >= start
            if end_date is not None:
                end = pd.to_datetime(end_date, errors="coerce")
                if pd.notna(end):
                    mask &= index_dates <= end
            series = series.loc[mask.to_numpy()]
    if len(series) < 2:
        return np.nan
    first = _safe_number(series.iloc[0])
    last = _safe_number(series.iloc[-1])
    if not math.isfinite(first) or first == 0 or not math.isfinite(last):
        return np.nan
    return float(last / first - 1)


def benchmark_window_from_equity_curve(equity_curve: pd.DataFrame) -> tuple[pd.Timestamp | None, pd.Timestamp | None]:
    """Return the valid equity date window used for benchmark alignment."""

    if "date" not in equity_curve.columns or "equity" not in equity_curve.columns or equity_curve.empty:
        return None, None
    valid_equity = pd.to_numeric(equity_curve["equity"], errors="coerce").notna()
    dates = pd.to_datetime(equity_curve.loc[valid_equity, "date"], errors="coerce").dropna()
    if dates.empty:
        return None, None
    return pd.Timestamp(dates.min()), pd.Timestamp(dates.max())


def load_benchmark_series(path: str | Path | None) -> pd.Series:
    """Load a local benchmark CSV with date and close columns."""

    if not path:
        return pd.Series(dtype=float, name="benchmark_close")
    frame = read_csv_frame(path)
    required = {"date", "close"}
    missing = required - set(frame.columns)
    if missing:
        raise ValueError(f"benchmark csv missing columns: {', '.join(sorted(missing))}")
    frame = frame[["date", "close"]].copy()
    frame["date"] = pd.to_datetime(frame["date"], errors="coerce")
    frame["close"] = pd.to_numeric(frame["close"], errors="coerce")
    frame = frame.dropna(subset=["date", "close"]).sort_values("date")
    frame = frame.drop_duplicates("date", keep="last")
    return pd.Series(frame["close"].to_numpy(dtype=float), index=frame["date"], name="benchmark_close")


def load_industry_map(path: str | Path | None) -> dict[str, str]:
    if not path:
        return {}
    frame = read_csv_frame(path)
    required = {"symbol", "industry"}
    missing = required - set(frame.columns)
    if missing:
        raise ValueError(f"industry map csv missing columns: {', '.join(sorted(missing))}")
    result: dict[str, str] = {}
    for _, row in frame[["symbol", "industry"]].dropna().iterrows():
        symbol = normalize_symbol(row["symbol"])
        industry = str(row["industry"]).strip()
        if symbol and industry:
            result[symbol] = industry
    return result
