"""A-share specific backward-looking factors.

These complement the generic momentum / volatility / liquidity factors with
A-share microstructure signals that the report flagged as missing:

- ``limit_up_freq_20d`` / ``limit_down_freq_20d``: how often the name hit the
  daily price limit in the trailing 20 sessions. The ±10% main-board limit is
  approximated by a |9.5%| close-to-close move (STAR/ChiNext 20% names are
  under-counted, which is fine for a relative cross-sectional factor).
- ``turnover_decay_20d``: short-vs-long turnover trend (ma5/ma20 - 1). Negative
  means activity is fading — useful to separate fading speculation from genuine
  momentum.

All factors are backward-looking, local-research only, and do not constitute
investment advice.
"""

from __future__ import annotations

import pandas as pd

ASHARE_FACTOR_COLUMNS = ["limit_up_freq_20d", "limit_down_freq_20d", "turnover_decay_20d"]
LIMIT_UP_RET = 0.095
LIMIT_DOWN_RET = -0.095


def _with_warnings(frame: pd.DataFrame, warnings: list[str]) -> pd.DataFrame:
    frame.attrs["warnings"] = warnings
    return frame


def _sort_for_symbol(frame: pd.DataFrame) -> pd.DataFrame:
    if "symbol" in frame.columns and "date" in frame.columns:
        return frame.sort_values(["symbol", "date"]).copy()
    if "date" in frame.columns:
        return frame.sort_values("date").copy()
    return frame.copy()


def _returns(series: pd.Series) -> pd.Series:
    return series.ffill().pct_change(fill_method=None)


def compute_ashare_factors(df: pd.DataFrame) -> pd.DataFrame:
    """Add A-share microstructure factors (limit-up/down frequency, turnover decay)."""

    frame = _sort_for_symbol(df)
    warnings: list[str] = []
    for column in ASHARE_FACTOR_COLUMNS:
        frame[column] = pd.NA

    if "close" not in frame.columns:
        warnings.append("missing close column; a-share factors skipped")
        return _with_warnings(frame, warnings)
    frame["close"] = pd.to_numeric(frame["close"], errors="coerce")

    grouped = "symbol" in frame.columns
    if grouped:
        returns = frame.groupby("symbol", group_keys=False)["close"].transform(_returns)
    else:
        returns = _returns(frame["close"])

    # Mask undefined-return rows so a leading bar is not scored as "not limit-up".
    up = (returns >= LIMIT_UP_RET).astype(float).where(returns.notna())
    down = (returns <= LIMIT_DOWN_RET).astype(float).where(returns.notna())

    turn: pd.Series | None
    if "turnover" in frame.columns:
        frame["turnover"] = pd.to_numeric(frame["turnover"], errors="coerce")
        turn = frame["turnover"]
    elif "volume" in frame.columns:
        warnings.append("missing turnover column; turnover_decay_20d uses volume proxy")
        frame["volume"] = pd.to_numeric(frame["volume"], errors="coerce")
        turn = frame["volume"]
    else:
        warnings.append("missing turnover and volume; turnover_decay_20d skipped")
        turn = None

    if grouped:
        symbols = frame["symbol"]
        frame["limit_up_freq_20d"] = up.groupby(symbols, group_keys=False).transform(
            lambda s: s.rolling(20, min_periods=5).mean()
        )
        frame["limit_down_freq_20d"] = down.groupby(symbols, group_keys=False).transform(
            lambda s: s.rolling(20, min_periods=5).mean()
        )
        if turn is not None:
            ma5 = turn.groupby(symbols, group_keys=False).transform(lambda s: s.rolling(5, min_periods=2).mean())
            ma20 = turn.groupby(symbols, group_keys=False).transform(lambda s: s.rolling(20, min_periods=5).mean())
            frame["turnover_decay_20d"] = (ma5 / ma20.where(ma20 > 0)) - 1.0
    else:
        frame["limit_up_freq_20d"] = up.rolling(20, min_periods=5).mean()
        frame["limit_down_freq_20d"] = down.rolling(20, min_periods=5).mean()
        if turn is not None:
            ma5 = turn.rolling(5, min_periods=2).mean()
            ma20 = turn.rolling(20, min_periods=5).mean()
            frame["turnover_decay_20d"] = (ma5 / ma20.where(ma20 > 0)) - 1.0

    return _with_warnings(frame, warnings)


__all__ = ["compute_ashare_factors", "ASHARE_FACTOR_COLUMNS", "LIMIT_UP_RET", "LIMIT_DOWN_RET"]
