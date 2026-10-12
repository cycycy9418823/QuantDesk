"""Delisting liquidation for paper portfolios (Phase 3 退市链路).

A long-standing gap (flagged by the audit): when a held symbol delists, the
paper ledgers had **no liquidation path**.  The position kept being marked at
its last stale close forever, inflating portfolio equity and hiding the loss.

This module closes that gap.  Given a portfolio's positions and the PIT
delist data (:mod:`quantdesk.data_truth.pit_st_delist_loader`), it identifies
held symbols that are delisted as of a given date and produces explicit
liquidation actions:

- value the position at a conservative liquidation price (last tradable close
  if known, otherwise the position's own recorded price marked down), and
- move it out of the live positions into ``closed_positions`` with a clear
  ``exit_reason`` so the equity curve reflects reality.

It is deliberately conservative and side-effect free: it takes plain data in
and returns plain data out, so it can be wired into either ledger (manual or
auto) without coupling them.  Research / paper use only; never places orders.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from quantdesk.paper.market_rules import paper_only_boundary
from quantdesk.data_truth.pit_st_delist_loader import (
    StDelistData,
    load_st_delist_data,
)


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None or value == "":
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _norm_symbol(value: Any) -> str:
    text = "".join(ch for ch in str(value or "") if ch.isdigit())
    return text.zfill(6)[-6:] if text else ""


#: Kept for API compatibility only. Batch 1A: a haircut is an invented price
#: (charter §2: 不编造行情), so it is no longer applied; a delisted holding with
#: no observed last-tradable price is marked NO_PRICE_PENDING_MANUAL instead.
DEFAULT_DELIST_HAIRCUT = 0.25
NO_PRICE_PENDING_MANUAL = "no_price_pending_manual"


@dataclass
class LiquidationAction:
    """One delisting liquidation decision for a single held position."""

    symbol: str
    name: str
    quantity: int
    liquidation_price: float | None
    proceeds: float | None
    last_trade_date: str
    delist_date: str
    reason: str
    price_basis: str  # "last_tradable_close" | NO_PRICE_PENDING_MANUAL

    @property
    def pending_manual(self) -> bool:
        return self.liquidation_price is None

    def as_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "name": self.name,
            "quantity": self.quantity,
            "liquidation_price": None if self.liquidation_price is None else round(self.liquidation_price, 4),
            "proceeds": None if self.proceeds is None else round(self.proceeds, 2),
            "last_trade_date": self.last_trade_date,
            "delist_date": self.delist_date,
            "exit_reason": self.reason,
            "price_basis": self.price_basis,
            "action": "delist_pending_manual" if self.pending_manual else "delist_liquidation",
            **paper_only_boundary(),
        }


@dataclass
class LiquidationResult:
    """Outcome of scanning a set of positions for delisting liquidation."""

    as_of: str
    actions: list[LiquidationAction] = field(default_factory=list)
    coverage: str = "absent"
    warnings: list[str] = field(default_factory=list)

    @property
    def liquidated_symbols(self) -> set[str]:
        return {a.symbol for a in self.actions}

    def as_dict(self) -> dict[str, Any]:
        return {
            "as_of": self.as_of,
            "coverage": self.coverage,
            "liquidated_count": len(self.actions),
            "actions": [a.as_dict() for a in self.actions],
            "warnings": list(self.warnings),
        }


def _position_quantity(position: dict[str, Any]) -> int:
    for key in ("quantity", "shares", "qty"):
        if key in position:
            try:
                return int(float(position.get(key) or 0))
            except (TypeError, ValueError):
                return 0
    return 0


def _position_mark(position: dict[str, Any]) -> float:
    for key in ("latest_price", "last_price", "close", "avg_cost", "entry_price", "buy_price"):
        val = _safe_float(position.get(key), 0.0)
        if val > 0:
            return val
    return 0.0


def scan_positions_for_delisting(
    positions: list[dict[str, Any]],
    *,
    as_of: str,
    data: StDelistData | None = None,
    last_close_lookup: dict[str, float] | None = None,
    haircut: float = DEFAULT_DELIST_HAIRCUT,
) -> LiquidationResult:
    """Identify held positions that are delisted as of *as_of*.

    Parameters
    ----------
    positions:
        Live position dicts (manual or auto ledger shape).
    as_of:
        Decision date ``YYYY-MM-DD``.  A position is liquidated if its symbol
        has a delist date on/before this date.
    data:
        PIT ST/delist data; loaded from local CSV if not supplied.
    last_close_lookup:
        Optional ``{symbol: last_tradable_close}`` to value liquidation more
        precisely.  When absent, the position's own mark is haircut.
    haircut:
        Fractional markdown applied when no explicit last-tradable price is
        available (default 25%).
    """

    pit = data if data is not None else load_st_delist_data()
    lookup = {_norm_symbol(k): float(v) for k, v in (last_close_lookup or {}).items()}
    result = LiquidationResult(as_of=str(as_of)[:10], coverage=pit.delist_source)

    if pit.delist_source == "absent":
        # Honest: with no delist data we cannot liquidate anything.  Surface a
        # warning so callers know survivorship bias is *not* handled, instead
        # of silently treating every held name as still-listed.
        result.warnings.append(
            "delist_data_absent:cannot_detect_delisted_holdings:survivorship_bias_unhandled"
        )
        return result

    for position in positions:
        symbol = _norm_symbol(position.get("symbol") or position.get("code"))
        if not symbol:
            continue
        record = pit.delist_record(symbol)
        if record is None or not record.is_delisted_on(result.as_of):
            continue

        quantity = _position_quantity(position)
        if quantity <= 0:
            continue

        name = str(position.get("name") or symbol)
        explicit = lookup.get(symbol)
        if explicit is not None and explicit > 0:
            price: float | None = explicit
            basis = "last_tradable_close"
            proceeds: float | None = explicit * quantity
            reason = (
                f"退市清算：{name}({symbol}) 已于 {record.delist_date} 退市"
                f"（最后可交易日 {record.effective_last_trade_date}），"
                f"按最后可交易收盘价 {explicit:.4f} 清仓，移出持仓。"
            )
        else:
            # No observed price: never invent one (the old path booked 75% of the
            # stale mark as a fill). The holding stays, frozen, for a person.
            price = None
            basis = NO_PRICE_PENDING_MANUAL
            proceeds = None
            reason = (
                f"退市待人工：{name}({symbol}) 已于 {record.delist_date} 退市"
                f"（最后可交易日 {record.effective_last_trade_date}），没有可用的成交价，"
                "不编造清算价；持仓冻结，等所有者确认清算价后再记账。"
            )
        result.actions.append(
            LiquidationAction(
                symbol=symbol,
                name=name,
                quantity=quantity,
                liquidation_price=price,
                proceeds=proceeds,
                last_trade_date=record.effective_last_trade_date,
                delist_date=record.delist_date,
                reason=reason,
                price_basis=basis,
            )
        )

    return result


def apply_delisting_liquidation(
    state: dict[str, Any],
    *,
    as_of: str,
    data: StDelistData | None = None,
    last_close_lookup: dict[str, float] | None = None,
    haircut: float = DEFAULT_DELIST_HAIRCUT,
    cash_key: str = "cash",
    positions_key: str = "positions",
    closed_key: str = "closed_positions",
) -> dict[str, Any]:
    """Apply delisting liquidation to a portfolio *state* dict (pure, returns new).

    Moves delisted holdings from ``positions`` to ``closed_positions`` and
    credits liquidation proceeds to ``cash``.  Returns a result payload with the
    updated state and the actions taken.  If no delist data is available, the
    state is returned unchanged with a warning (never silently drops holdings).
    """

    portfolio = dict(state or {})
    positions = [dict(p) for p in portfolio.get(positions_key) or [] if isinstance(p, dict)]
    scan = scan_positions_for_delisting(
        positions,
        as_of=as_of,
        data=data,
        last_close_lookup=last_close_lookup,
        haircut=haircut,
    )

    if not scan.actions:
        for _bk, _bv in paper_only_boundary().items():
            portfolio.setdefault(_bk, _bv)
        return {
            "ok": True,
            "liquidated": 0,
            "coverage": scan.coverage,
            "warnings": scan.warnings,
            "state": portfolio,
            "actions": [],
            **paper_only_boundary(),
        }

    liquidated = scan.liquidated_symbols
    cash = _safe_float(portfolio.get(cash_key), 0.0)
    closed = list(portfolio.get(closed_key) or [])
    remaining: list[dict[str, Any]] = []

    action_by_symbol = {a.symbol: a for a in scan.actions}
    pending: list[str] = []
    for position in positions:
        symbol = _norm_symbol(position.get("symbol") or position.get("code"))
        action = action_by_symbol.get(symbol)
        if action is None:
            remaining.append(position)
            continue
        if action.pending_manual:
            # No price evidence: the position stays, frozen and labelled; no cash moves.
            remaining.append({
                **position,
                "management_state": "delist_pending_manual",
                "tradable": False,
                "can_sell": False,
                "delist_date": action.delist_date,
                "delist_status": NO_PRICE_PENDING_MANUAL,
                "valuation_warning": "delisted: no tradable price; held at last mark until the owner confirms",
            })
            pending.append(symbol)
            continue
        cash += float(action.proceeds or 0.0)
        avg_cost = _safe_float(
            position.get("avg_cost") or position.get("entry_price") or position.get("buy_price"),
            action.liquidation_price,
        )
        realized = (action.liquidation_price - avg_cost) * action.quantity
        closed.append(
            {
                **position,
                "exit_date": scan.as_of,
                "exit_price": round(action.liquidation_price, 4),
                "exit_quantity": action.quantity,
                "realized_pnl": round(realized, 2),
                "exit_reason": action.reason,
                "status": "closed",
                "management_state": "delisted",
                "delist_date": action.delist_date,
                "price_basis": action.price_basis,
                **paper_only_boundary(),
            }
        )

    portfolio[positions_key] = remaining
    portfolio[closed_key] = closed
    portfolio[cash_key] = round(cash, 2)
    for _bk, _bv in paper_only_boundary().items():
        portfolio.setdefault(_bk, _bv)
    warnings = list(scan.warnings) + [f"delist_pending_manual:{symbol}" for symbol in pending]

    return {
        "ok": True,
        "liquidated": len(scan.actions) - len(pending),
        "pending_manual": len(pending),
        "coverage": scan.coverage,
        "warnings": warnings,
        "state": portfolio,
        "actions": [a.as_dict() for a in scan.actions],
        "liquidated_symbols": sorted(liquidated - set(pending)),
        "pending_manual_symbols": sorted(pending),
        **paper_only_boundary(),
    }


__all__ = [
    "DEFAULT_DELIST_HAIRCUT",
    "NO_PRICE_PENDING_MANUAL",
    "LiquidationAction",
    "LiquidationResult",
    "apply_delisting_liquidation",
    "scan_positions_for_delisting",
]
