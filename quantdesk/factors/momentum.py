from __future__ import annotations

import pandas as pd

MOMENTUM_FACTOR_COLUMNS = ["momentum_5d", "momentum_20d", "momentum_60d", "reversal_5d"]


def _with_warnings(frame: pd.DataFrame, warnings: list[str]) -> pd.DataFrame:
    frame.attrs["warnings"] = warnings
    return frame


def _sort_for_symbol(frame: pd.DataFrame) -> pd.DataFrame:
    if "symbol" in frame.columns and "date" in frame.columns:
        return frame.sort_values(["symbol", "date"]).copy()
    if "date" in frame.columns:
        return frame.sort_values("date").copy()
    return frame.copy()


def compute_momentum_factors(df: pd.DataFrame) -> pd.DataFrame:
    """Compute backward-looking momentum factors from close prices.

    The function never looks forward: momentum_Nd uses close[t] / close[t-N] - 1.
    Missing required fields return stable factor columns filled with NA and a
    warning in ``DataFrame.attrs["warnings"]`` instead of raising.
    """

    frame = _sort_for_symbol(df)
    warnings: list[str] = []
    for column in MOMENTUM_FACTOR_COLUMNS:
        frame[column] = pd.NA

    if "close" not in frame.columns:
        warnings.append("missing close column; momentum factors skipped")
        return _with_warnings(frame, warnings)

    frame["close"] = pd.to_numeric(frame["close"], errors="coerce")
    if "symbol" in frame.columns:
        close = frame.groupby("symbol", group_keys=False)["close"]
        frame["momentum_5d"] = close.transform(lambda s: s / s.shift(5) - 1)
        frame["momentum_20d"] = close.transform(lambda s: s / s.shift(20) - 1)
        frame["momentum_60d"] = close.transform(lambda s: s / s.shift(60) - 1)
    else:
        frame["momentum_5d"] = frame["close"] / frame["close"].shift(5) - 1
        frame["momentum_20d"] = frame["close"] / frame["close"].shift(20) - 1
        frame["momentum_60d"] = frame["close"] / frame["close"].shift(60) - 1
    frame["reversal_5d"] = -frame["momentum_5d"]
    return _with_warnings(frame, warnings)
