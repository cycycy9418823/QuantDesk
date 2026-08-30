from __future__ import annotations

import pandas as pd

LIQUIDITY_FACTOR_COLUMNS = [
    "turnover_proxy_20d",
    "amihud_illiquidity_20d",
    "volume_price_corr_20d",
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


def compute_liquidity_factors(df: pd.DataFrame) -> pd.DataFrame:
    """Compute backward-looking liquidity proxy factors."""

    frame = _sort_for_symbol(df)
    warnings: list[str] = []
    for column in LIQUIDITY_FACTOR_COLUMNS:
        frame[column] = pd.NA

    if "close" not in frame.columns or "volume" not in frame.columns:
        warnings.append("missing close or volume column; liquidity factors skipped")
        return _with_warnings(frame, warnings)

    for column in ("close", "volume", "amount"):
        if column in frame.columns:
            frame[column] = pd.to_numeric(frame[column], errors="coerce")

    if "symbol" in frame.columns:
        returns = frame.groupby("symbol", group_keys=False)["close"].transform(
            lambda s: s.ffill().pct_change(fill_method=None)
        )
        frame["turnover_proxy_20d"] = frame.groupby("symbol", group_keys=False)["volume"].transform(lambda s: s.rolling(20, min_periods=5).mean())
    else:
        returns = frame["close"].ffill().pct_change(fill_method=None)
        frame["turnover_proxy_20d"] = frame["volume"].rolling(20, min_periods=5).mean()

    denom = frame["amount"] if "amount" in frame.columns else frame["volume"]
    if "amount" not in frame.columns:
        warnings.append("missing amount column; amihud_illiquidity_20d uses volume fallback")
    amihud_raw = returns.abs() / denom.where(denom > 0)
    if "symbol" in frame.columns:
        frame["amihud_illiquidity_20d"] = amihud_raw.groupby(frame["symbol"], group_keys=False).transform(lambda s: s.rolling(20, min_periods=5).mean())
        corr = pd.Series(pd.NA, index=frame.index, dtype="Float64")
        for _, idx in frame.groupby("symbol", sort=False).groups.items():
            group = frame.loc[idx]
            group_returns = group["close"].ffill().pct_change(fill_method=None)
            group_corr = group["volume"].rolling(20, min_periods=5).corr(group_returns)
            corr.loc[idx] = group_corr.to_numpy()
        frame["volume_price_corr_20d"] = corr
    else:
        frame["amihud_illiquidity_20d"] = amihud_raw.rolling(20, min_periods=5).mean()
        frame["volume_price_corr_20d"] = frame["volume"].rolling(20, min_periods=5).corr(
            frame["close"].ffill().pct_change(fill_method=None)
        )

    return _with_warnings(frame, warnings)
