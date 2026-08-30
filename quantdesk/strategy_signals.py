"""Local-only strategy signal helpers for paper simulation.

The helpers in this module only create virtual learning signals from local
daily bars. They do not connect to brokers, do not place orders, and do not
represent investment advice.
"""

from __future__ import annotations

from typing import Any

import pandas as pd

from quantdesk.utils import safe_float


def is_st_or_delisting(name: str) -> bool:
    text = str(name or "").upper()
    return "ST" in text or "退" in text


def limit_rate_for_symbol(symbol: str, name: str = "") -> float:
    code = str(symbol or "")
    if is_st_or_delisting(name):
        return 0.05
    if code.startswith(("300", "301", "688")):
        return 0.20
    return 0.10


def add_signal_columns(frame: pd.DataFrame) -> pd.DataFrame:
    df = frame.copy()
    if "close" not in df.columns:
        return df
    df["close"] = pd.to_numeric(df["close"], errors="coerce")
    if "amount" in df.columns:
        df["amount"] = pd.to_numeric(df["amount"], errors="coerce")
        df["amount_ma20"] = df["amount"].rolling(20, min_periods=20).mean()
    if "volume" in df.columns:
        df["volume"] = pd.to_numeric(df["volume"], errors="coerce")
        df["volume_ma20"] = df["volume"].rolling(20, min_periods=20).mean()
    df["ma20"] = df["close"].rolling(20, min_periods=20).mean()
    df["prev_close"] = df["close"].shift(1)
    df["close_vs_ma20_pct"] = (df["close"] / df["ma20"] - 1.0) * 100.0
    return df


def liquidity_status_from_row(row: dict[str, Any]) -> str:
    amount_ma20 = safe_float(row.get("amount_ma20"))
    if amount_ma20 is None:
        return "unknown"
    if amount_ma20 >= 30_000_000:
        return "ok"
    if amount_ma20 >= 10_000_000:
        return "low"
    return "very_low"


def long_signal_from_row(row: dict[str, Any], *, symbol: str, name: str = "") -> dict[str, Any] | None:
    """Return the shared long-learning entry signal for one enriched daily row."""

    if is_st_or_delisting(name):
        return None
    close = safe_float(row.get("close"))
    ma20 = safe_float(row.get("ma20"))
    close_vs_ma20_pct = safe_float(row.get("close_vs_ma20_pct"))
    if close is None or ma20 is None or close_vs_ma20_pct is None:
        return None
    liquidity = liquidity_status_from_row(row)
    if close > ma20 and 0 <= close_vs_ma20_pct <= 20 and liquidity != "very_low":
        return {
            "symbol": symbol,
            "name": name,
            "signal_date": str(row.get("date") or row.get("latest_date") or "")[:10],
            "strategy": "long",
            "signal_type": "virtual_entry",
            "reason": "长线规则满足：收盘价高于20日均线且偏离不超过20%。",
            "liquidity_status": liquidity,
            "close": close,
            "ma20": ma20,
            "close_vs_ma20_pct": close_vs_ma20_pct,
        }
    return None


def board_signal_from_row(row: dict[str, Any], *, symbol: str, name: str = "") -> dict[str, Any] | None:
    """Return the shared board-learning entry signal for one enriched daily row."""

    if is_st_or_delisting(name):
        return None
    close = safe_float(row.get("close"))
    prev_close = safe_float(row.get("prev_close"))
    if close is None or prev_close is None or prev_close <= 0:
        return None
    liquidity = liquidity_status_from_row(row)
    pct = close / prev_close - 1.0
    rate = limit_rate_for_symbol(symbol, name)
    if pct >= rate * 0.92 and liquidity != "very_low":
        return {
            "symbol": symbol,
            "name": name,
            "signal_date": str(row.get("date") or row.get("latest_date") or "")[:10],
            "strategy": "board",
            "signal_type": "virtual_entry",
            "reason": "打板近似规则满足：日线接近涨停或涨停，仅用于学习模拟。",
            "liquidity_status": liquidity,
            "pct_change": pct,
            "limit_rate": rate,
            "limit_up_like": True,
            "close": close,
            "prev_close": prev_close,
        }
    return None


def generate_long_signals(frame: pd.DataFrame, *, symbol: str, name: str = "") -> list[dict[str, Any]]:
    """Generate long-learning virtual entry signals from local daily bars."""

    if is_st_or_delisting(name):
        return []
    df = add_signal_columns(frame)
    signals: list[dict[str, Any]] = []
    for _, row in df.iterrows():
        signal = long_signal_from_row(row.to_dict(), symbol=symbol, name=name)
        if signal:
            signals.append(signal)
    return signals


def generate_board_signals(frame: pd.DataFrame, *, symbol: str, name: str = "") -> list[dict[str, Any]]:
    """Generate board-learning virtual entry signals from local daily bars.

    This is a daily-bar approximation. It cannot see intraday order queues,
    sealed volume, or intraday break details.
    """

    if is_st_or_delisting(name):
        return []
    df = add_signal_columns(frame)
    signals: list[dict[str, Any]] = []
    for _, row in df.iterrows():
        signal = board_signal_from_row(row.to_dict(), symbol=symbol, name=name)
        if signal:
            signals.append(signal)
    return signals


def evaluate_exit_signals(
    row: dict[str, Any],
    *,
    entry_price: float,
    holding_days: int,
    max_holding_days: int,
    stop_loss_pct: float,
    take_profit_pct: float,
) -> tuple[bool, str]:
    close = safe_float(row.get("close"))
    ma20 = safe_float(row.get("ma20"))
    if close is None or entry_price <= 0:
        return True, "数据异常，触发风险复核离场模拟。"
    pnl_pct = (close / entry_price - 1.0) * 100.0
    if holding_days >= max_holding_days:
        return True, "达到模拟持有天数。"
    if pnl_pct <= stop_loss_pct:
        return True, "触发模拟风险线。"
    if pnl_pct >= take_profit_pct:
        return True, "触发模拟收益观察线。"
    if ma20 is not None and close < ma20:
        return True, "收盘价跌破20日均线，观察假设失效。"
    return False, ""
