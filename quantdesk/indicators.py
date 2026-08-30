from __future__ import annotations

from typing import Any, Mapping

import numpy as np
import pandas as pd

COLUMN_ALIASES = {
    "date": ("date", "日期", "trade_date", "datetime", "time"),
    "open": ("open", "开盘", "开盘价", "open_price"),
    "high": ("high", "最高", "最高价"),
    "low": ("low", "最低", "最低价"),
    "close": ("close", "收盘", "收盘价", "latest_close", "last_close", "price", "current_price"),
    "volume": ("volume", "成交量", "vol", "latest_volume", "turnover_volume"),
    "amount": ("amount", "amount_yuan", "成交额", "amount_value"),
}


def _config_value(config: Mapping[str, Any] | None, key: str, default: Any) -> Any:
    if not isinstance(config, Mapping):
        return default
    value = config.get(key, default)
    return default if value is None else value


def _coerce_numeric(series: pd.Series | None) -> pd.Series | None:
    """Force a Series into numpy-backed ``float64`` with NaN for non-numeric.

    v2.6.7 stability patch: ``pd.to_numeric(errors="coerce")`` can return a
    ``Float64`` nullable dtype when the source is already nullable, and any
    later ``.replace(0, pd.NA)`` against such a column may promote the whole
    series to ``object`` dtype.  Both shapes cause ``pandas.errors.DataError:
    No numeric types to aggregate`` inside ``ewm`` / ``rolling`` aggregations
    used by KDJ / RSI / BOLL / ATR.  Forcing numpy ``float64`` here keeps
    every downstream rolling / ewm call safe.
    """

    if series is None:
        return None
    coerced = pd.to_numeric(series, errors="coerce")
    try:
        return coerced.astype("float64")
    except (TypeError, ValueError):
        # Defensive: if astype refuses (e.g. exotic extension dtype), fall back
        # to a numpy.float64 array via .to_numpy(dtype=float).
        return pd.Series(coerced.to_numpy(dtype="float64", na_value=np.nan), index=series.index, name=series.name)


def _series(frame: pd.DataFrame, *names: str) -> pd.Series | None:
    for name in names:
        if name in frame.columns:
            return _coerce_numeric(frame[name])
    return None


def _resolve_series(frame: pd.DataFrame, aliases: tuple[str, ...]) -> tuple[pd.Series | None, str | None]:
    for name in aliases:
        if name in frame.columns:
            return _coerce_numeric(frame[name]), name
    return None, None


def _ensure_numeric(series: pd.Series | None) -> pd.Series | None:
    return _coerce_numeric(series)


def _safe_ratio(numerator: pd.Series, denominator: pd.Series) -> pd.Series:
    """Divide two numeric series and keep numpy ``float64`` dtype.

    Replaces a zero denominator with ``np.nan`` (numpy NaN, *not* ``pd.NA``)
    so the resulting dtype stays ``float64``; replaces ``+inf`` / ``-inf``
    with NaN so downstream rolling / ewm calls never raise.
    """

    safe_denom = denominator.replace(0, np.nan)
    ratio = numerator / safe_denom
    return pd.to_numeric(ratio, errors="coerce").replace([np.inf, -np.inf], np.nan)


def _wilder_rsi(close: pd.Series, window: int) -> pd.Series:
    delta = close.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1 / window, adjust=False, min_periods=window).mean()
    avg_loss = loss.ewm(alpha=1 / window, adjust=False, min_periods=window).mean()
    rs = _safe_ratio(avg_gain, avg_loss)
    return 100 - 100 / (1 + rs)


def _history_warnings(df: pd.DataFrame, columns: tuple[str, ...]) -> list[str]:
    warnings: list[str] = []
    for column in columns:
        if column not in df.columns:
            warnings.append(f"缺少指标字段：{column}")
    return warnings


def add_indicators(frame: pd.DataFrame, config: Mapping[str, Any] | None = None) -> pd.DataFrame:
    """Return a copy with common local technical indicators.

    The helper tolerates missing fields and never downloads data.
    """

    df = frame.copy()
    warnings: list[str] = []
    # v2.6.7: every core column is coerced to numpy float64 *before* any
    # rolling / ewm / KDJ / BOLL / ATR / RSI / MACD computation runs.  This
    # keeps pd.NA-containing object dtypes from leaking into pandas
    # aggregations that would otherwise raise
    # ``pandas.errors.DataError: No numeric types to aggregate``.
    close = _series(df, *COLUMN_ALIASES["close"])
    high = _series(df, *COLUMN_ALIASES["high"])
    low = _series(df, *COLUMN_ALIASES["low"])
    volume = _series(df, *COLUMN_ALIASES["volume"])
    amount = _series(df, *COLUMN_ALIASES["amount"])
    for canonical, series in {
        "open": _series(df, *COLUMN_ALIASES["open"]),
        "high": high,
        "low": low,
        "close": close,
        "volume": volume,
        "amount": amount,
    }.items():
        if series is not None and canonical not in df.columns:
            df[canonical] = series

    if close is None:
        warnings.append("缺少收盘价字段，无法计算趋势类指标")
    else:
        close = close.astype(float)
        for window in (5, 10, 20, 60, 120):
            df[f"ma{window}"] = close.rolling(window, min_periods=1).mean()
        df["ema12"] = close.ewm(span=12, adjust=False).mean()
        df["ema26"] = close.ewm(span=26, adjust=False).mean()
        df["macd_dif"] = df["ema12"] - df["ema26"]
        df["macd_dea"] = df["macd_dif"].ewm(span=9, adjust=False).mean()
        df["macd_histogram"] = (df["macd_dif"] - df["macd_dea"]) * 2
        df["previous_macd_histogram"] = df["macd_histogram"].shift(1)
        for window in (6, 14):
            df[f"rsi{window}"] = _wilder_rsi(close, window)
        mid = close.rolling(20, min_periods=20).mean()
        std = close.rolling(20, min_periods=20).std(ddof=0)
        df["boll_mid"] = mid
        df["boll_upper"] = mid + 2 * std
        df["boll_lower"] = mid - 2 * std
        df["high_20d"] = close.rolling(20, min_periods=1).max()
        df["low_20d"] = close.rolling(20, min_periods=1).min()
        df["high_60d"] = close.rolling(60, min_periods=1).max()
        df["low_60d"] = close.rolling(60, min_periods=1).min()
        df["vol20"] = close.ffill().pct_change(fill_method=None).rolling(20, min_periods=2).std()
        df["previous_close"] = close.shift(1)

    if high is not None and low is not None and close is not None:
        high = high.astype(float)
        low = low.astype(float)
        kdj_n = int(_config_value(config, "kdj_n", 9) or 9)
        kdj_k_smooth = int(_config_value(config, "kdj_k_smooth", 3) or 3)
        kdj_d_smooth = int(_config_value(config, "kdj_d_smooth", 3) or 3)
        low_min = low.rolling(kdj_n, min_periods=1).min()
        high_max = high.rolling(kdj_n, min_periods=1).max()
        # v2.6.7: use np.nan (numpy NaN) rather than pd.NA as the divide-by-zero
        # sentinel so the rsv series stays numpy float64.  Then force-coerce the
        # final rsv once more before ewm to defend against any unexpected dtype
        # promotion in older pandas versions.
        rsv_denom = (high_max - low_min).replace(0, np.nan)
        rsv = (close - low_min) / rsv_denom * 100
        rsv = pd.to_numeric(rsv, errors="coerce").replace([np.inf, -np.inf], np.nan)
        df["kdj_k"] = rsv.ewm(com=kdj_k_smooth - 1, adjust=False).mean()
        df["kdj_d"] = df["kdj_k"].ewm(com=kdj_d_smooth - 1, adjust=False).mean()
        df["kdj_j"] = 3 * df["kdj_k"] - 2 * df["kdj_d"]
        previous_close = close.shift(1)
        tr = pd.concat([(high - low).abs(), (high - previous_close).abs(), (low - previous_close).abs()], axis=1).max(axis=1)
        df["atr14"] = tr.ewm(alpha=1 / 14, adjust=False, min_periods=14).mean()
    else:
        warnings.append("缺少最高价、最低价或收盘价字段，无法计算 KDJ / ATR")

    if volume is not None:
        df["volume_ma20"] = volume.rolling(20, min_periods=1).mean()
        df["volume_ratio_20"] = _safe_ratio(volume, df["volume_ma20"])
    if amount is not None:
        df["amount_ma20"] = amount.rolling(20, min_periods=1).mean()
        df["amount_ratio_20"] = _safe_ratio(amount, df["amount_ma20"])
        df["amount_ratio"] = df["amount_ratio_20"]
    if "amount_ratio_20" not in df.columns and "volume_ratio_20" in df.columns:
        df["amount_ratio_20"] = df["volume_ratio_20"]
    if "activity_ratio_20" not in df.columns:
        if "amount_ratio_20" in df.columns:
            df["activity_ratio_20"] = df["amount_ratio_20"]
        elif "volume_ratio_20" in df.columns:
            df["activity_ratio_20"] = df["volume_ratio_20"]
    if "activity_value" not in df.columns:
        if amount is not None:
            df["activity_value"] = amount
            df["activity_source"] = "amount"
        elif volume is not None:
            df["activity_value"] = volume
            df["activity_source"] = "volume"
    if "activity_avg_20" not in df.columns:
        if "amount_ma20" in df.columns:
            df["activity_avg_20"] = df["amount_ma20"]
        elif "volume_ma20" in df.columns:
            df["activity_avg_20"] = df["volume_ma20"]
    if "activity_ratio_20" in df.columns and "amount_ratio_20" not in df.columns:
        df["amount_ratio_20"] = df["activity_ratio_20"]

    if warnings:
        existing = list(df.attrs.get("indicator_warnings", []))
        df.attrs["indicator_warnings"] = existing + warnings
        df["indicator_warnings"] = "；".join(existing + warnings)
    else:
        df.attrs.setdefault("indicator_warnings", [])
        if "indicator_warnings" not in df.columns:
            df["indicator_warnings"] = ""

    return df



def latest_indicator_snapshot(frame: pd.DataFrame) -> dict[str, Any]:
    if frame.empty:
        return {}
    enhanced = add_indicators(frame)
    row = enhanced.iloc[-1].to_dict()
    snapshot = {key: (None if pd.isna(value) else value) for key, value in row.items()}
    warnings = list(enhanced.attrs.get("indicator_warnings", []))
    if warnings:
        snapshot["indicator_warnings"] = warnings
    return snapshot
