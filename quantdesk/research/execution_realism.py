from __future__ import annotations

from quantdesk.platform_common import base_payload, clean_number, finalize_outputs, simple_markdown
from quantdesk.research.fees_china_a import estimate_china_a_fees
from quantdesk.research.limit_up_fill_model import limit_up_fill_probability
from quantdesk.research.slippage_curve import estimate_slippage_bps


def compute_partial_fill(order_qty: float, daily_volume: float, participation_rate: float = 0.01) -> dict:
    max_fill = max(0.0, clean_number(daily_volume) * participation_rate)
    filled = min(clean_number(order_qty), max_fill)
    unfilled = max(0.0, clean_number(order_qty) - filled)
    return {"filled_qty": filled, "unfilled_qty": unfilled, "fill_ratio": filled / clean_number(order_qty, 1) if order_qty else 0}


def build_execution_realism_report(config: dict | None = None) -> dict:
    cfg = dict(config or {})
    partial = compute_partial_fill(5000, 100000, float(cfg.get("max_participation_rate") or 0.01))
    payload = base_payload("execution_realism")
    payload.update({
        "enabled": bool(cfg.get("execution_realism", True)),
        "default_backtest_unchanged": True,
        "partial_fill": partial,
        "limit_up_fill": limit_up_fill_probability("sealed_limit_up"),
        "dynamic_slippage": estimate_slippage_bps(order_notional=100000, daily_amount=5000000, volatility=0.03, liquidity_score=60),
        "fees": estimate_china_a_fees(100000, side="sell"),
        "execution_realism_assumptions": ["optional model", "not true market fill proof"],
        "items": [partial],
    })
    return finalize_outputs(payload, "execution_realism", simple_markdown("Execution Realism", payload, "items"))
