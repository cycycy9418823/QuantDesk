from __future__ import annotations

import math

import pandas as pd

VOLATILITY_FACTOR_COLUMNS = [
    "realized_volatility_20d",
    "parkinson_volatility_20d",
    "intraday_range_ratio_20d",
]


def _with_warnings(frame: pd.DataFrame, warnings: list[str]) -> pd.DataFrame:
    frame.attrs["warnings"] = warnings
    return frame


def _sort_for_symbol(frame: pd.DataFrame) -> pd.DataFrame:
    if "symbol" in frame.columns and "date" in frame.columns:
        return frame.sort_values(["symbol", "date"]).copy()
    if "date" in frame.columns:
        return frame.sort_values("date").copy()
    return frame.copy()


def compute_volatility_factors(df: pd.DataFrame) -> pd.DataFrame:
    """Compute backward-looking volatility diagnostics from OHLC data."""

    frame = _sort_for_symbol(df)
    warnings: list[str] = []
    for column in VOLATILITY_FACTOR_COLUMNS:
        frame[column] = pd.NA

    if "close" not in frame.columns:
        warnings.append("missing close column; volatility factors skipped")
        return _with_warnings(frame, warnings)

    for column in ("close", "high", "low"):
        if column in frame.columns:
            frame[column] = pd.to_numeric(frame[column], errors="coerce")

    if "symbol" in frame.columns:
        returns = frame.groupby("symbol", group_keys=False)["close"].transform(
            lambda s: s.ffill().pct_change(fill_method=None)
        )
        frame["realized_volatility_20d"] = returns.groupby(frame["symbol"], group_keys=False).transform(lambda s: s.rolling(20, min_periods=5).std())
    else:
        returns = frame["close"].ffill().pct_change(fill_method=None)
        frame["realized_volatility_20d"] = returns.rolling(20, min_periods=5).std()

    if {"high", "low"}.issubset(frame.columns):
        high = frame["high"].where(frame["high"] > 0)
        low = frame["low"].where(frame["low"] > 0)
        log_range = (high / low).apply(lambda value: math.log(value) if pd.notna(value) and value > 0 else float("nan"))
        park = pd.to_numeric((log_range**2) / (4 * math.log(2)), errors="coerce")
        range_ratio = (frame["high"] - frame["low"]) / frame["close"].where(frame["close"] != 0)
        if "symbol" in frame.columns:
            frame["parkinson_volatility_20d"] = park.groupby(frame["symbol"], group_keys=False).transform(lambda s: s.rolling(20, min_periods=5).mean().pow(0.5))
            frame["intraday_range_ratio_20d"] = range_ratio.groupby(frame["symbol"], group_keys=False).transform(lambda s: s.rolling(20, min_periods=5).mean())
        else:
            frame["parkinson_volatility_20d"] = park.rolling(20, min_periods=5).mean().pow(0.5)
            frame["intraday_range_ratio_20d"] = range_ratio.rolling(20, min_periods=5).mean()
    else:
        warnings.append("missing high/low columns; Parkinson and range factors skipped")

    return _with_warnings(frame, warnings)
