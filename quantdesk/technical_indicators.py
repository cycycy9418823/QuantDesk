"""Technical indicator helpers for the local research UI.

The important correctness rule here is: missing or insufficient history stays
unknown.  These helpers must not backfill rolling windows, invent neutral
values such as RSI=50, or reuse the last close as a placeholder.  Downstream UI
code can decide how to render ``NaN`` as "数据缺失"; the indicator layer should
not fabricate a numeric value.
"""

from __future__ import annotations

import numpy as np
import pandas as pd


def _nan_series(index: pd.Index) -> pd.Series:
    return pd.Series(np.nan, index=index, dtype=float)


def _as_float_series(values: pd.Series) -> pd.Series:
    return pd.to_numeric(values, errors="coerce").astype(float)


def calculate_ma(close: pd.Series, periods: list[int] | None = None) -> dict[str, pd.Series]:
    """Calculate moving averages without backfilling incomplete windows."""

    if periods is None:
        periods = [5, 10, 20, 60]

    close = _as_float_series(close)
    result: dict[str, pd.Series] = {}
    for period in periods:
        if len(close) < period:
            result[f"ma{period}"] = _nan_series(close.index)
        else:
            result[f"ma{period}"] = close.rolling(window=period, min_periods=period).mean()
    return result


def calculate_macd(
    close: pd.Series,
    fast: int = 12,
    slow: int = 26,
    signal: int = 9,
) -> dict[str, pd.Series]:
    """Calculate MACD while keeping warm-up periods unknown.

    Returns DIF, DEA and MACD histogram (domestic convention: ``2*(DIF-DEA)``).
    """

    close = _as_float_series(close)
    if len(close) < slow:
        empty = _nan_series(close.index)
        return {"dif": empty.copy(), "dea": empty.copy(), "macd": empty.copy()}

    ema_fast = close.ewm(span=fast, adjust=False).mean()
    ema_slow = close.ewm(span=slow, adjust=False).mean()
    dif = ema_fast - ema_slow
    dif.iloc[: slow - 1] = np.nan
    dea = dif.ewm(span=signal, adjust=False).mean()
    macd_histogram = 2 * (dif - dea)
    return {"dif": dif, "dea": dea, "macd": macd_histogram}


def calculate_kdj(
    df: pd.DataFrame,
    n: int = 9,
    k_period: int = 3,
    d_period: int = 3,
) -> dict[str, pd.Series]:
    """Calculate KDJ without neutral-value fabrication."""

    if len(df) < n:
        empty = _nan_series(df.index)
        return {"k": empty.copy(), "d": empty.copy(), "j": empty.copy()}

    high = _as_float_series(df["high"])
    low = _as_float_series(df["low"])
    close = _as_float_series(df["close"])
    low_n = low.rolling(window=n, min_periods=n).min()
    high_n = high.rolling(window=n, min_periods=n).max()
    denominator = (high_n - low_n).where(lambda s: s != 0)
    rsv = (close - low_n) / denominator * 100
    k = rsv.ewm(com=k_period - 1, adjust=False).mean()
    d = k.ewm(com=d_period - 1, adjust=False).mean()
    j = 3 * k - 2 * d
    return {"k": k, "d": d, "j": j}


def calculate_rsi(close: pd.Series, periods: list[int] | None = None) -> dict[str, pd.Series]:
    """Calculate RSI for one or more periods without filling unknown values."""

    if periods is None:
        periods = [6, 12, 24]

    close = _as_float_series(close)
    result: dict[str, pd.Series] = {}
    delta = close.diff()
    for period in periods:
        if len(close) < period + 1:
            result[f"rsi{period}"] = _nan_series(close.index)
            continue

        # delta 首元素是 NaN（首日涨跌未知）。用 clip 而非 where(...,0.0)：clip 会
        # 保留 NaN，避免把"未知"当成 0 计入窗口，从而 (a) 让首个 RSI 正确落在
        # index=period（而非提前一天到 period-1），(b) 不系统性拉低早期 RSI。
        gain = delta.clip(lower=0.0).rolling(window=period, min_periods=period).mean()
        loss = (-delta).clip(lower=0.0).rolling(window=period, min_periods=period).mean()
        rsi = pd.Series(np.nan, index=close.index, dtype=float)

        valid_loss = loss > 0
        rs = gain[valid_loss] / loss[valid_loss]
        rsi.loc[valid_loss] = 100 - (100 / (1 + rs))
        rsi.loc[(loss == 0) & (gain > 0)] = 100.0
        rsi.loc[(loss > 0) & (gain == 0)] = 0.0
        result[f"rsi{period}"] = rsi
    return result


def calculate_volume_ma(volume: pd.Series, periods: list[int] | None = None) -> dict[str, pd.Series]:
    """Calculate volume moving averages without backfilling incomplete windows."""

    if periods is None:
        periods = [5, 20]

    volume = _as_float_series(volume)
    result: dict[str, pd.Series] = {}
    for period in periods:
        if len(volume) < period:
            result[f"vol_ma{period}"] = _nan_series(volume.index)
        else:
            result[f"vol_ma{period}"] = volume.rolling(window=period, min_periods=period).mean()
    return result


def calculate_returns(close: pd.Series, periods: list[int] | None = None) -> dict[str, pd.Series]:
    """Calculate percentage returns while preserving the leading unknown rows."""

    if periods is None:
        periods = [1, 5, 20]

    close = _as_float_series(close)
    result: dict[str, pd.Series] = {}
    for period in periods:
        if len(close) <= period:
            result[f"return_{period}d"] = _nan_series(close.index)
        else:
            result[f"return_{period}d"] = close.pct_change(periods=period, fill_method=None)
    return result


def calculate_volatility(close: pd.Series, period: int = 20) -> pd.Series:
    """Calculate rolling return volatility in percent."""

    close = _as_float_series(close)
    if len(close) <= period:
        return _nan_series(close.index)

    returns = close.pct_change(fill_method=None)
    return returns.rolling(window=period, min_periods=period).std() * 100


def get_all_indicators(df: pd.DataFrame) -> dict[str, list[float]]:
    """Calculate all technical indicators and return JSON-friendly lists."""

    if df.empty or len(df) < 2:
        return {}

    close = df["close"]
    indicators: dict[str, list[float]] = {}

    for key, values in calculate_ma(close).items():
        indicators[key] = values.tolist()

    for key, values in calculate_macd(close).items():
        indicators[key] = values.tolist()

    for key, values in calculate_kdj(df).items():
        indicators[key] = values.tolist()

    for key, values in calculate_rsi(close).items():
        indicators[key] = values.tolist()

    if "volume" in df.columns:
        for key, values in calculate_volume_ma(df["volume"]).items():
            indicators[key] = values.tolist()

    for key, values in calculate_returns(close).items():
        indicators[key] = values.tolist()

    indicators["volatility_20d"] = calculate_volatility(close).tolist()
    return indicators


def get_latest_indicator_values(df: pd.DataFrame) -> dict[str, float]:
    """Return latest finite indicator values for candidate cards."""

    indicators = get_all_indicators(df)
    if not indicators:
        return {}

    latest: dict[str, float] = {}
    for key, series in indicators.items():
        if isinstance(series, pd.Series) and len(series) > 0:
            value = series.iloc[-1]
        elif isinstance(series, (list, tuple)) and len(series) > 0:
            value = series[-1]
        elif isinstance(series, (int, float, np.integer, np.floating)):
            value = series
        else:
            continue
        if pd.notna(value) and np.isfinite(float(value)):
            latest[key] = float(value)
    return latest
