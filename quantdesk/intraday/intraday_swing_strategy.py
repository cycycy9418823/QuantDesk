"""Intraday swing plan generator (v2.7.8).

Generates next-trading-day execution plans from T-day close data.  Plans
specify concrete price zones for pullback buy, breakout buy, take profit,
and stop loss.  They are consumed by the intraday execution engine which
replays minute-bar data in chronological order.

This module does NOT connect to brokers, does NOT place real orders, and
does NOT constitute investment advice.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

import pandas as pd

from quantdesk.daily_schema import normalize_daily_columns

DISCLAIMER = "仅用于策略学习与模拟验证，不构成投资建议。"


def build_swing_plan(
    symbol: str,
    daily_df: pd.DataFrame,
    *,
    name: str = "",
    plan_date: str = "",
    trade_date: str = "",
    quantity: int = 100,
    no_chase_pct: float = 6.0,
    stop_loss_pct: float = -5.0,
    take_profit_pct: float = 8.0,
) -> dict[str, Any]:
    """Build a next-day swing execution plan from T-day daily bars.

    The plan is generated using only data available at T-day close.  It must
    NOT use any T+1 information.
    """

    df = normalize_daily_columns(daily_df)
    if df.empty or "close" not in df.columns:
        return {"symbol": symbol, "name": name, "ok": False, "reason": "日线数据不足。", "disclaimer": DISCLAIMER}

    df = df.dropna(subset=["close"]).sort_values("date" if "date" in df.columns else df.columns[0]).reset_index(drop=True)
    if len(df) < 20:
        return {"symbol": symbol, "name": name, "ok": False, "reason": "历史行数不足 20，无法生成波段计划。", "disclaimer": DISCLAIMER}

    latest = df.iloc[-1]
    ref_close = float(latest["close"])
    ma5 = float(df["close"].rolling(5).mean().iloc[-1]) if len(df) >= 5 else ref_close
    ma10 = float(df["close"].rolling(10).mean().iloc[-1]) if len(df) >= 10 else ref_close
    ma20 = float(df["close"].rolling(20).mean().iloc[-1])
    recent_high = float(df["high"].tail(10).max()) if "high" in df.columns else ref_close * 1.05
    recent_low = float(df["low"].tail(10).min()) if "low" in df.columns else ref_close * 0.95

    support_price = round(max(ma20, recent_low), 4)
    resistance_price = round(recent_high, 4)
    no_chase_above = round(ref_close * (1 + no_chase_pct / 100), 4)
    stop_loss_price = round(ref_close * (1 + stop_loss_pct / 100), 4)
    take_profit_low = round(ref_close * (1 + take_profit_pct / 100 * 0.8), 4)
    take_profit_high = round(ref_close * (1 + take_profit_pct / 100), 4)

    # Pullback buy zone: between support and slightly above
    pullback_low = round(support_price, 4)
    pullback_high = round(min(support_price * 1.02, ref_close * 0.99), 4)

    # Breakout buy zone: above resistance
    breakout_low = round(resistance_price * 0.99, 4)
    breakout_high = round(resistance_price * 1.015, 4)

    buy_zones = []
    if ref_close > ma20:
        buy_zones.append({
            "type": "pullback_buy",
            "price_low": pullback_low,
            "price_high": pullback_high,
            "reason": f"回踩 MA20({round(ma20,2)}) 上方支撑区域，仍保持长线趋势观察",
        })
    buy_zones.append({
        "type": "breakout_buy",
        "price_low": breakout_low,
        "price_high": breakout_high,
        "reason": f"放量突破近期压力位({round(resistance_price,2)})，进入趋势跟随模拟",
    })

    sell_zones = [
        {
            "type": "take_profit",
            "price_low": take_profit_low,
            "price_high": take_profit_high,
            "reason": "接近短期压力区，模拟止盈或减仓",
        },
        {
            "type": "stop_loss",
            "price_low": 0.0,
            "price_high": stop_loss_price,
            "reason": "跌破关键支撑，趋势假设失效",
        },
    ]

    if not plan_date:
        plan_date = str(latest.get("date", ""))[:10] if "date" in df.columns else datetime.now().strftime("%Y-%m-%d")
    if not trade_date:
        trade_date = ""  # Caller should fill with next trading day

    return {
        "ok": True,
        "symbol": symbol,
        "name": name,
        "strategy_type": "long_swing",
        "plan_date": plan_date,
        "trade_date": trade_date,
        "reference_close": ref_close,
        "ma5": round(ma5, 4),
        "ma10": round(ma10, 4),
        "ma20": round(ma20, 4),
        "support_price": support_price,
        "resistance_price": resistance_price,
        "buy_zones": buy_zones,
        "sell_zones": sell_zones,
        "no_chase_above": no_chase_above,
        "quantity": quantity,
        "valid_for_date": trade_date,
        "disclaimer": DISCLAIMER,
    }
