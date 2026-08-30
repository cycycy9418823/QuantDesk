from __future__ import annotations


def estimate_slippage_bps(*, order_notional: float, daily_amount: float, volatility: float = 0.02, liquidity_score: float = 50) -> dict:
    participation = order_notional / daily_amount if daily_amount else 1
    bps = 5 + participation * 1000 + volatility * 100 + max(0, 50 - liquidity_score) * 0.2
    return {"estimated_slippage_bps": round(bps, 4), "participation": round(participation, 6), "assumption": "research estimate only"}
