"""Reconstruct closed-position net returns from complete simulated fill records.

This only describes an existing simulation. It never changes its fills or cash.
One sample is a symbol's complete flat-to-flat position episode. Partial exits
remain one episode, and an unclosed position is not a completed trade sample.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Iterable


@dataclass
class _Episode:
    shares: float = 0.0
    cash_paid: float = 0.0
    cash_received: float = 0.0
    fees: float = 0.0
    slippage: float = 0.0
    entry_date: str = ""
    fill_indices: list[int] = field(default_factory=list)


def _number(row: dict[str, Any], key: str) -> float:
    value = row.get(key)
    if isinstance(value, bool):
        raise ValueError(f"invalid_{key}")
    number = float(value)
    if not math.isfinite(number) or number < 0:
        raise ValueError(f"invalid_{key}")
    return number


def closed_trade_return_evidence(trades: Iterable[dict[str, Any]]) -> dict[str, Any]:
    """Return all completed, cash-cost-inclusive episodes without preview limits."""
    rows = list(trades)
    episodes: dict[str, _Episode] = {}
    invalid_symbols: set[str] = set()
    closed: list[dict[str, Any]] = []
    warnings: list[str] = []
    cost_warnings: list[str] = []
    executed_count = 0
    blocked_count = 0
    ordered = sorted(enumerate(rows), key=lambda pair: (
        str(pair[1].get("execution_date") or pair[1].get("date") or ""), pair[0]
    ))
    for index, row in ordered:
        if row.get("blocked"):
            blocked_count += 1
            continue
        symbol = str(row.get("symbol") or "").strip()
        side = str(row.get("side") or "").lower()
        date = str(row.get("execution_date") or row.get("date") or "")
        if row.get("cost_coverage_complete") is False or str(row.get("cost_schedule") or "").startswith("partial_"):
            cost_warnings.append(f"fill_{index}_cost_schedule_incomplete:{row.get('cost_schedule') or 'unknown'}")
        try:
            shares = _number(row, "shares")
            gross = _number(row, "gross_amount")
            commission = _number(row, "commission")
            stamp = _number(row, "stamp_duty")
            transfer = _number(row, "transfer_fee")
            slippage = _number(row, "slippage_cost") if row.get("slippage_cost") is not None else 0.0
            if not symbol or not date or side not in {"buy", "sell"} or shares <= 0 or gross <= 0:
                raise ValueError("invalid_fill_identity_or_amount")
        except (TypeError, ValueError) as exc:
            warnings.append(f"fill_{index}_excluded:{exc}")
            cost_warnings.append(f"fill_{index}_net_cash_evidence_unverified")
            invalid_symbols.add(symbol)
            continue
        executed_count += 1
        if symbol in invalid_symbols:
            continue
        episode = episodes.setdefault(symbol, _Episode(entry_date=date))
        cash_fees = commission + stamp + transfer
        if side == "sell" and shares > episode.shares + 1e-8:
            warnings.append(f"fill_{index}_unmatched_sell:{symbol}")
            invalid_symbols.add(symbol)
            continue
        episode.fill_indices.append(index)
        episode.fees += cash_fees
        # Executed prices/gross already contain slippage. Keep it as an audit
        # component, never subtract it a second time from the net cash result.
        episode.slippage += slippage
        if side == "buy":
            episode.shares += shares
            episode.cash_paid += gross + cash_fees
        else:
            episode.shares -= shares
            episode.cash_received += gross - cash_fees
            if abs(episode.shares) <= 1e-8:
                pnl = episode.cash_received - episode.cash_paid
                closed.append({
                    "symbol": symbol,
                    "entry_date": episode.entry_date,
                    "exit_date": date,
                    "cash_paid": episode.cash_paid,
                    "cash_received": episode.cash_received,
                    "cash_fees": episode.fees,
                    "slippage_in_executed_prices": episode.slippage,
                    "net_pnl": pnl,
                    "return": pnl / episode.cash_paid,
                    "fill_indices": episode.fill_indices,
                })
                del episodes[symbol]
    # If any later fill of a symbol cannot be reconstructed, do not silently
    # retain a selected subset and call the complete return evidence reliable.
    return {
        "trade_returns": [row["return"] for row in closed],
        "closed_trades": closed,
        "closed_trade_count": len(closed),
        "executed_fill_count": executed_count,
        "blocked_fill_count": blocked_count,
        "open_positions_excluded": sorted(symbol for symbol, episode in episodes.items() if episode.shares > 0),
        "return_evidence": {
            "version": "closed_position_net_cash_v1",
            "unit": "flat_to_flat_symbol_position",
            "complete_fill_history": not warnings,
            "cost_coverage_complete": not cost_warnings,
            "cash_fees_included": not warnings,
            "slippage_included_once_via_execution_price": True,
            "portfolio_return_equivalent": False,
            "preview_truncation_applied": False,
            "warnings": warnings + cost_warnings,
        },
    }
