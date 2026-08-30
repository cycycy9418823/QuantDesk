"""Unified A-share trade-cost service for paper-trading ledgers.

This module is the **single source of truth** for trade costs (commission,
stamp tax, transfer fee, slippage) and their cash impact across every paper
ledger.  It replaces the previously divergent fee helpers that lived in
``auto_paper_portfolio._auto_trade_costs`` and ``paper_trading._paper_fees``,
each of which computed costs slightly differently (and, in the conditional
execution path of ``position_management_engine``, were not applied at all).

Design notes
------------
- Built on :func:`quantdesk.cn_a_share_rules.calculate_cn_a_share_fees`, which
  already separates commission / stamp tax (sell-only) / transfer fee, plus a
  gross-based slippage term.
- The default parameters are deliberately chosen to match the historical
  ``_auto_trade_costs`` constants so that existing ledgers stay numerically
  stable when they are migrated onto this service.
- Pure functions, no IO, no imports from other ``paper`` modules (so it can be
  imported from anywhere in the paper package without circular imports).

It is for local paper-trading simulation only; it does not connect to brokers,
submit orders, or constitute investment advice.
"""

from __future__ import annotations

from dataclasses import dataclass

from quantdesk.cn_a_share_rules import calculate_cn_a_share_fees

# Defaults aligned with the historical auto-paper ledger constants
# (auto_paper_portfolio._AUTO_* ) so migrating callers keeps numbers stable.
DEFAULT_COMMISSION_RATE = 0.00025
DEFAULT_MIN_COMMISSION = 5.0
DEFAULT_STAMP_TAX_RATE = 0.0005   # sell-only
DEFAULT_TRANSFER_FEE_RATE = 0.0   # kept 0 to preserve historical numbers
DEFAULT_SLIPPAGE_BPS = 5.0

# Round 21 cost profiles. The historical ledger charged no transfer fee; that
# stays the default so existing ledger numbers never shift retroactively.
# ``cn_current`` reflects the CSDC schedule effective 2022-04-29: transfer fee
# 0.01 permille (0.001%) of gross, charged on BOTH buy and sell, no minimum,
# unified across SSE/SZSE/BSE A shares.
COST_PROFILE_LEGACY = "legacy"
COST_PROFILE_CN_CURRENT = "cn_current"
CN_CURRENT_TRANSFER_FEE_RATE = 0.00001

# Action / side tokens that represent a sell (stamp tax applies, fee reduces
# proceeds and realized pnl).  Everything else is treated as a buy.
_SELL_TOKENS = {
    "sell",
    "sell_all",
    "stop_loss",
    "take_profit",
    "reduce_half",
    "reduce",
    "trim",
    "exit",
    "close",
}

TRADE_COST_MODEL = "cn_a_share_unified"


@dataclass(frozen=True)
class TradeCostConfig:
    """Configurable trade-cost parameters.

    Defaults match the historical auto-paper ledger so existing behaviour is
    preserved unless a caller intentionally overrides them. ``cost_profile``
    is provenance metadata written onto every fee breakdown so ledger event
    rows can state which fee schedule produced their numbers.
    """

    commission_rate: float = DEFAULT_COMMISSION_RATE
    min_commission: float = DEFAULT_MIN_COMMISSION
    stamp_tax_rate: float = DEFAULT_STAMP_TAX_RATE
    transfer_fee_rate: float = DEFAULT_TRANSFER_FEE_RATE
    slippage_bps: float = DEFAULT_SLIPPAGE_BPS
    apply_min_commission: bool = True
    cost_profile: str = COST_PROFILE_LEGACY

    @classmethod
    def cn_current(cls, **overrides: object) -> "TradeCostConfig":
        """Current A-share fee schedule (verified 2026-08: CSDC 2022-04-29 notice)."""

        params: dict[str, object] = {
            "transfer_fee_rate": CN_CURRENT_TRANSFER_FEE_RATE,
            "cost_profile": COST_PROFILE_CN_CURRENT,
        }
        params.update(overrides)
        return cls(**params)  # type: ignore[arg-type]


def is_sell_side(side: object) -> bool:
    """Return True when *side* denotes a sell-type action."""

    return str(side or "").strip().lower() in _SELL_TOKENS


def compute_trade_costs(
    *,
    side: object,
    gross: float,
    config: TradeCostConfig | None = None,
) -> dict[str, float | str]:
    """Compute the full cost breakdown for a single simulated fill.

    Parameters
    ----------
    side:
        Buy/sell token.  Sell-type tokens (see :data:`_SELL_TOKENS`) incur stamp
        tax; everything else is treated as a buy.
    gross:
        Notional value of the fill (price * quantity), in yuan.
    config:
        Optional :class:`TradeCostConfig`; defaults reproduce the historical
        auto-paper ledger numbers.

    Returns
    -------
    dict
        Keys: ``commission``, ``stamp_duty`` (alias ``stamp_tax``),
        ``transfer_fee``, ``slippage``, ``total_fee``, ``fee_model``.
        ``stamp_duty`` and ``stamp_tax`` carry the same value so both legacy
        callers (``_auto_trade_costs`` used ``stamp_duty``) and the cn-a-share
        rule layer (used ``stamp_tax``) keep working.
    """

    cfg = config or TradeCostConfig()
    fee_side = "sell" if is_sell_side(side) else "buy"
    fees = calculate_cn_a_share_fees(
        side=fee_side,
        amount=gross,
        commission_rate=cfg.commission_rate,
        min_commission=cfg.min_commission,
        stamp_tax_rate=cfg.stamp_tax_rate,
        transfer_fee_rate=cfg.transfer_fee_rate,
        apply_min_commission=cfg.apply_min_commission,
    )
    gross_value = max(float(gross or 0.0), 0.0)
    slippage = gross_value * max(float(cfg.slippage_bps or 0.0), 0.0) / 10_000.0
    total = float(fees["total_fee"]) + slippage
    stamp = float(fees["stamp_tax"])
    return {
        "commission": float(fees["commission"]),
        "stamp_duty": round(stamp, 4),
        "stamp_tax": round(stamp, 4),
        "transfer_fee": float(fees["transfer_fee"]),
        "slippage": round(slippage, 4),
        "total_fee": round(total, 4),
        "fee_model": TRADE_COST_MODEL,
        "cost_profile": str(cfg.cost_profile or COST_PROFILE_LEGACY),
    }


def buy_cash_outflow(
    *,
    price: float,
    quantity: float,
    config: TradeCostConfig | None = None,
) -> dict[str, float]:
    """Cash needed for a buy and the fee-inclusive cost basis.

    Returns ``gross``, ``total_fee``, ``cash_out`` (gross + fee) and
    ``cost_basis`` (== cash_out; buy fees are folded into the cost basis so that
    unrealized pnl reflects fee drag).
    """

    gross = float(price) * float(quantity)
    costs = compute_trade_costs(side="buy", gross=gross, config=config)
    fee = float(costs["total_fee"])
    cash_out = gross + fee
    return {
        "gross": round(gross, 4),
        "total_fee": fee,
        "cash_out": round(cash_out, 4),
        "cost_basis": round(cash_out, 4),
        "cost_profile": str(costs.get("cost_profile") or COST_PROFILE_LEGACY),
    }


def sell_cash_inflow(
    *,
    price: float,
    quantity: float,
    avg_cost: float,
    config: TradeCostConfig | None = None,
) -> dict[str, float]:
    """Cash received for a sell and the fee-net realized pnl.

    ``realized_pnl`` is net of the sell fee.  (Buy fees are assumed already
    folded into ``avg_cost`` via :func:`buy_cash_outflow`.)
    """

    gross = float(price) * float(quantity)
    costs = compute_trade_costs(side="sell", gross=gross, config=config)
    fee = float(costs["total_fee"])
    cash_in = gross - fee
    realized_pnl = (float(price) - float(avg_cost)) * float(quantity) - fee
    return {
        "gross": round(gross, 4),
        "total_fee": fee,
        "cash_in": round(cash_in, 4),
        "realized_pnl": round(realized_pnl, 4),
        "cost_profile": str(costs.get("cost_profile") or COST_PROFILE_LEGACY),
    }


__all__ = [
    "CN_CURRENT_TRANSFER_FEE_RATE",
    "COST_PROFILE_CN_CURRENT",
    "COST_PROFILE_LEGACY",
    "TRADE_COST_MODEL",
    "TradeCostConfig",
    "buy_cash_outflow",
    "compute_trade_costs",
    "is_sell_side",
    "sell_cash_inflow",
]
