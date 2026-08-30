from __future__ import annotations

from quantdesk.paper.trade_costs import (
    DEFAULT_COMMISSION_RATE,
    DEFAULT_MIN_COMMISSION,
    DEFAULT_STAMP_TAX_RATE,
    DEFAULT_TRANSFER_FEE_RATE,
    TRADE_COST_MODEL,
    TradeCostConfig,
    compute_trade_costs,
)


def estimate_china_a_fees(
    notional: float,
    *,
    side: str = "buy",
    commission_rate: float = DEFAULT_COMMISSION_RATE,
    min_commission: float = DEFAULT_MIN_COMMISSION,
    stamp_tax_rate: float = DEFAULT_STAMP_TAX_RATE,
    transfer_fee_rate: float = DEFAULT_TRANSFER_FEE_RATE,
) -> dict:
    """Estimate CN-A fees through the unified research/paper cost service.

    This helper is used by optional execution-realism diagnostics. It keeps the
    output focused on explicit fees only, so slippage is set to zero here and
    remains represented by the separate slippage-curve diagnostic.
    """

    costs = compute_trade_costs(
        side=side,
        gross=notional,
        config=TradeCostConfig(
            commission_rate=commission_rate,
            min_commission=min_commission,
            stamp_tax_rate=stamp_tax_rate,
            transfer_fee_rate=transfer_fee_rate,
            slippage_bps=0.0,
        ),
    )
    return {
        "side": side,
        "commission": costs["commission"],
        "stamp_tax": costs["stamp_tax"],
        "stamp_duty": costs["stamp_duty"],
        "transfer_fee": costs["transfer_fee"],
        "total_fee": costs["total_fee"],
        "fee_model": TRADE_COST_MODEL,
    }
