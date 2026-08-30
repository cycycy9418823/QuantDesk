from __future__ import annotations

import math


def estimate_impact_bps(
    order_notional: float,
    daily_amount: float,
    daily_volatility: float = 0.025,
    spread_bps: float = 5.0,
) -> float:
    """Estimate research-only market impact in basis points."""

    if order_notional <= 0:
        return 0.0
    if daily_amount <= 0 or not math.isfinite(daily_amount):
        return 100.0
    participation = order_notional / daily_amount
    if participation > 0.30:
        return 200.0
    eta = 0.142
    permanent = eta * max(daily_volatility, 0.0) * math.sqrt(max(participation, 0.0)) * 10_000
    temporary = max(spread_bps, 0.0) / 2
    return round(float(permanent + temporary), 4)
