"""Read-only strategy-ledger validity, recovery identity and valuation evidence."""
from __future__ import annotations

import math
from datetime import date
from pathlib import Path
from typing import Any

from quantdesk.paper.state_store import PaperStateStore
from quantdesk.paper.accounting.reconciliation import number, strategy_pnl_identity, strategy_state_errors
from quantdesk.paper.realized_corrections import inspect_realized_adjustment

IDENTITY_FIELDS = ("ledger_id", "created_at", "initial_cash", "profile_id", "scenario_id", "strategy_fill_mode")


def day(value: Any) -> str:
    try:
        return date.fromisoformat(str(value or "")[:10]).isoformat()
    except ValueError:
        return ""


def strategy_initialization_evidence(state_path: Path, *, latest_path: Path, history_path: Path) -> bool:
    """An existing artifact proves an account existed even if its contents broke."""

    store = PaperStateStore(state_path, schema_version=None)
    return bool(state_path.exists() or latest_path.exists() or history_path.exists()
                or store._identity_path().exists() or store._backups()
                or list(state_path.parent.glob(f"{state_path.stem}.bad_*.json")))


def strategy_ledger_health(state: dict[str, Any], *, target_date: str,
                           latest: dict[str, Any] | None = None) -> dict[str, Any]:
    """Account freshness is evaluated independently for every original position."""

    reasons = strategy_state_errors(state)
    target = day(target_date)
    valuation_date = day(state.get("valuation_date"))
    if not target or valuation_date != target:
        reasons.append("strategy_valuation_date_not_target")
    if state.get("portfolio_review_valid") is False:
        reasons.append("strategy_portfolio_review_invalid")
    reconciliation = state.get("accounting_reconciliation")
    if isinstance(reconciliation, dict) and reconciliation.get("status") == "invalid":
        reasons.append("strategy_accounting_invalid")
    pnl_identity = strategy_pnl_identity(state)
    if pnl_identity["status"] == "mismatch":
        reasons.append("strategy_pnl_identity_mismatch")
    elif pnl_identity["status"] == "unverified":
        reasons.append("strategy_pnl_identity_unverified")
    elif pnl_identity["status"] == "missing" and any(key in state for key in ("realized_pnl", "unrealized_pnl")):
        reasons.append("strategy_pnl_identity_incomplete")
    rows = state.get("positions") if isinstance(state.get("positions"), list) else []
    stale_symbols = []
    value = sum(number(row.get("market_value")) or 0 for row in rows if isinstance(row, dict))
    for row in rows:
        if not isinstance(row, dict):
            continue
        symbol = str(row.get("symbol") or "unknown")
        if (day(row.get("latest_date")) != target or str(row.get("valuation_status") or "normal") != "normal"
                or (number(row.get("latest_price")) or 0) <= 0):
            stale_symbols.append(symbol)
        basis = row.get("valuation_price_basis")
        if basis is not None and basis != "raw":
            reasons.append(f"strategy_position_basis_untrusted:{symbol}")
        price, quantity, market = (number(row.get(key)) for key in ("latest_price", "quantity", "market_value"))
        if price is None or quantity is None or market is None:
            reasons.append(f"strategy_position_value_incomplete:{symbol}")
        # Preserve explicit historical adjusted records without allowing stale
        # derived quantities to override a new raw recorded-shares mark.
        adjusted_quantity = (number(row.get("adjusted_quantity_for_valuation"))
                             if row.get("price_adjustment_status") == "adjusted"
                             and row.get("valuation_quantity_basis") != "recorded_shares" else None)
        if price is not None and quantity is not None and market is not None:
            expected = price * (adjusted_quantity if adjusted_quantity is not None else quantity)
            if abs(market - round(expected, 2)) > 0.011:
                reasons.append(f"strategy_position_value_mismatch:{symbol}")
    if stale_symbols:
        reasons.append("strategy_position_valuation_stale_or_missing")
    for key, expected in (("market_value", value), ("total_equity", (number(state.get("cash")) or 0) + value)):
        if key in state and (number(state[key]) is None or abs(float(state[key]) - round(expected, 2)) > 0.011):
            reasons.append(f"strategy_account_{key}_mismatch")
    if latest is not None:
        if latest.get("ok") is not True:
            reasons.append("strategy_latest_failed")
        if day(latest.get("valuation_date")) != target:
            reasons.append("strategy_latest_date_not_target")
        if latest.get("portfolio_review_valid") is False:
            reasons.append("strategy_latest_review_invalid")
        summary = latest.get("summary") if isinstance(latest.get("summary"), dict) else latest
        for key in ("cash", "market_value", "total_equity"):
            if key not in summary:
                reasons.append(f"strategy_latest_{key}_missing")
            if key in summary and key in state and number(summary[key]) != number(state[key]):
                reasons.append(f"strategy_latest_{key}_mismatch")
        latest_positions = latest.get("positions")
        if isinstance(latest_positions, list) and latest_positions != rows:
            # Compare money/date evidence rather than display-only enrichment.
            fields = ("symbol", "quantity", "latest_price", "latest_date", "market_value")
            def project(items: list[Any]) -> list[tuple[str, ...]]:
                return sorted(tuple(str(row.get(key)) for key in fields) for row in items if isinstance(row, dict))
            if project(latest_positions) != project(rows):
                reasons.append("strategy_latest_positions_mismatch")
    return {"ok": not reasons, "status": "healthy" if not reasons else "invalid_or_stale",
            "target_date": target, "valuation_date": valuation_date,
            "pnl_identity": pnl_identity,
            "stale_symbols": sorted(set(stale_symbols)), "reason_codes": list(dict.fromkeys(reasons))}


def inspect_strategy_ledger(state_path: Path, *, target_date: str = "",
                            latest: dict[str, Any] | None = None) -> dict[str, Any]:
    """Inspect the canonical file as-is; a backup never makes this read green."""

    store = PaperStateStore(state_path, schema_version=None, validator=strategy_state_errors,
                            identity_fields=IDENTITY_FIELDS)
    result = store.read_snapshot()
    if result.status == "ready" and result.data is not None:
        health = strategy_ledger_health(result.data, target_date=target_date, latest=latest) if target_date else {
            "ok": True, "status": "ready", "reason_codes": []}
    else:
        health = {"ok": False, "status": result.status, "reason_codes": list(result.reason_codes)}
    return {**health, "read_status": result.status, "state_path": str(state_path),
            "state_sha256": result.source_sha256, "recovery_source": result.recovery_source,
            "recovery_sha256": result.recovery_sha256, "read_only": True}
