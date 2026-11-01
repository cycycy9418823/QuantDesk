"""Read-only identity audit for the strategy paper ledger (auto_paper_portfolio).

Checks, without writing any ledger field:

1. balance:      total_equity == cash + sum(position.market_value)
                 total_equity == cash + sum(latest_price * quantity)
                 state.market_value == sum(position.market_value)
2. pnl identity: total_equity == initial_cash + realized_pnl + unrealized_pnl
3. per position: avg_cost vs entry_price gap and its money impact
                 recorded unrealized_pnl vs entry-basis and avg-basis values
4. per closed:   gross_amount - commission == net_amount
                 gross_amount - total_fee == net_amount
                 realized_pnl vs (exit_price - entry_price) * exit_quantity
                 realized_pnl vs net_amount - avg_cost * exit_quantity
                 realized_pnl vs net_amount - entry_price * exit_quantity
5. decomposition: does the PnL identity difference equal the sum of the
                 entry/avg cost-basis conflicts?  A residual near zero means
                 the numerical differences happen to offset. This alone does
                 not establish the correct accounting or authorize a repair.

Exit code: 0 when the PnL identity reconciles (|difference| <= tolerance) and
both balance checks hold; 1 otherwise.  ``--json-out`` writes the structured
report; nothing else is written.  Read-only: never repairs or mutates state.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime, timezone
from decimal import Decimal, DecimalException, ROUND_HALF_EVEN
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from quantdesk.product_integrity import cost_basis_conflict_evidence  # noqa: E402

DEFAULT_STATE = PROJECT_ROOT / "state" / "auto_paper_portfolio.json"
CENT = Decimal("0.01")
TOLERANCE = Decimal("0.01")


def _decimal(value: Any) -> Decimal | None:
    """Parse a finite ledger number into Decimal; None when absent/invalid."""

    if isinstance(value, bool) or value is None:
        return None
    try:
        parsed = Decimal(str(value))
    except (DecimalException, ValueError):
        return None
    return parsed if parsed.is_finite() else None


def _money(value: Decimal | None) -> Decimal | None:
    return value.quantize(CENT, rounding=ROUND_HALF_EVEN) if value is not None else None


def _number(value: Decimal | None) -> float | None:
    return float(value) if value is not None else None


def _diff(left: Decimal | None, right: Decimal | None) -> Decimal | None:
    return _money(left - right) if left is not None and right is not None else None


def _rows(state: dict[str, Any], key: str) -> list[dict[str, Any]]:
    raw = state.get(key)
    if not isinstance(raw, list):
        return []
    return [row for row in raw if isinstance(row, dict)]


def _sum_known(rows: list[dict[str, Any]], field: str, *, complete: bool = True) -> Decimal | None:
    values = [_decimal(row.get(field)) for row in rows]
    if not complete or any(value is None for value in values):
        return None
    return sum(values, Decimal(0))


def _check_balance(state: dict[str, Any]) -> dict[str, Any]:
    cash = _decimal(state.get("cash"))
    equity = _decimal(state.get("total_equity"))
    positions = _rows(state, "positions")
    raw_positions = state.get("positions")
    complete = isinstance(raw_positions, list) and len(raw_positions) == len(positions)
    detail_mv = _sum_known(positions, "market_value", complete=complete)
    price_mv = Decimal(0)
    price_rows = 0
    for row in positions:
        price = _decimal(row.get("latest_price"))
        quantity = _decimal(row.get("quantity"))
        if price is not None and quantity is not None:
            price_mv += price * quantity
            price_rows += 1
    if not complete or price_rows != len(positions):
        price_mv = None
    reported_mv = _decimal(state.get("market_value"))
    checks = {
        "equity_equals_cash_plus_detail_market_value": _diff(equity, (cash + detail_mv) if cash is not None and detail_mv is not None else None),
        "equity_equals_cash_plus_price_times_quantity": _diff(equity, (cash + price_mv) if cash is not None and price_mv is not None else None),
        "reported_market_value_equals_detail_sum": _diff(reported_mv, _money(detail_mv)),
    }
    return {
        "cash": _number(cash),
        "total_equity": _number(equity),
        "market_value_reported": _number(reported_mv),
        "market_value_detail_sum": _number(_money(detail_mv)),
        "market_value_latest_price_times_quantity": _number(_money(price_mv)),
        "positions_priced": price_rows,
        "positions_total": len(positions),
        "diffs": {key: _number(value) for key, value in checks.items()},
        "ok": all(value is not None and abs(value) <= TOLERANCE for value in checks.values()),
        "tolerance": float(TOLERANCE),
    }


def _check_pnl_identity(state: dict[str, Any]) -> dict[str, Any]:
    initial = _decimal(state.get("initial_cash"))
    realized = _decimal(state.get("realized_pnl"))
    unrealized = _decimal(state.get("unrealized_pnl"))
    equity = _decimal(state.get("total_equity"))
    components_known = all(value is not None for value in (initial, realized, unrealized, equity))
    equity_from_pnl = _money(initial + realized + unrealized) if components_known else None
    difference = _diff(equity, equity_from_pnl)
    return {
        "definition": "total_equity - (initial_cash + realized_pnl + unrealized_pnl)",
        "initial_cash": _number(initial),
        "realized_pnl": _number(realized),
        "unrealized_pnl": _number(unrealized),
        "total_equity": _number(equity),
        "equity_from_pnl": _number(equity_from_pnl),
        "difference": _number(difference),
        "tolerance": float(TOLERANCE),
        "ok": difference is not None and abs(difference) <= TOLERANCE,
    }


def _position_row(row: dict[str, Any], index: int) -> dict[str, Any]:
    symbol = str(row.get("symbol") or "")
    quantity = _decimal(row.get("quantity"))
    avg_cost = _decimal(row.get("avg_cost"))
    entry_price = _decimal(row.get("entry_price"))
    latest_price = _decimal(row.get("latest_price"))
    market_value = _decimal(row.get("market_value"))
    recorded = _decimal(row.get("unrealized_pnl"))
    adjusted_avg = _decimal(row.get("adjusted_avg_cost"))
    missing = [
        field
        for field, value in (
            ("quantity", quantity),
            ("avg_cost", avg_cost),
            ("entry_price", entry_price),
            ("latest_price", latest_price),
            ("market_value", market_value),
            ("unrealized_pnl", recorded),
        )
        if value is None
    ]
    avg_basis = entry_basis = price_basis = cost_gap = None
    if not missing:
        avg_basis = _money(market_value - avg_cost * quantity)
        entry_basis = _money(market_value - entry_price * quantity)
        price_basis = _money((latest_price - entry_price) * quantity)
        cost_gap = _money((avg_cost - entry_price) * quantity)
    recorded_vs_avg = _diff(recorded, avg_basis)
    recorded_vs_entry = _diff(recorded, entry_basis)
    entry_vs_avg = _money(entry_price - avg_cost) if None not in (avg_cost, entry_price) else None
    if missing:
        status = "missing_fields"
    elif abs(recorded_vs_avg or Decimal(0)) <= TOLERANCE:
        status = "matches_avg_cost"
    elif abs(recorded_vs_entry or Decimal(0)) <= TOLERANCE:
        status = "matches_entry_price"
    else:
        status = "matches_neither"
    return {
        "index": index,
        "symbol": symbol,
        "name": row.get("name"),
        "quantity": _number(quantity),
        "latest_price": _number(latest_price),
        "market_value": _number(market_value),
        "avg_cost": _number(avg_cost),
        "adjusted_avg_cost": _number(adjusted_avg),
        "entry_price": _number(entry_price),
        "entry_minus_avg_cost": _number(entry_vs_avg),
        # (avg_cost - entry_price) * quantity: positive means the entry-price
        # basis understates the real (fee-inclusive, post-add) cost.
        "cost_basis_gap_amount": _number(cost_gap),
        "recorded_unrealized_pnl": _number(recorded),
        "unrealized_pnl_avg_cost_basis": _number(avg_basis),
        "unrealized_pnl_entry_price_basis": _number(entry_basis),
        "unrealized_pnl_price_only": _number(price_basis),
        "recorded_minus_avg_basis": _number(recorded_vs_avg),
        "recorded_minus_entry_basis": _number(recorded_vs_entry),
        "canonical_avg_basis_delta": _number(_diff(avg_basis, recorded)),
        "status": status,
        "missing_fields": missing,
    }


def _closed_row(row: dict[str, Any], index: int) -> dict[str, Any]:
    symbol = str(row.get("symbol") or "")
    quantity = _decimal(row.get("exit_quantity"))
    if quantity is None:
        quantity = _decimal(row.get("quantity"))
    avg_cost = _decimal(row.get("avg_cost"))
    entry_price = _decimal(row.get("entry_price"))
    exit_price = _decimal(row.get("exit_price"))
    if exit_price is None:
        exit_price = _decimal(row.get("latest_price"))
    gross = _decimal(row.get("gross_amount"))
    commission = _decimal(row.get("commission"))
    total_fee = _decimal(row.get("total_fee"))
    net_amount = _decimal(row.get("net_amount"))
    recorded = _decimal(row.get("realized_pnl"))
    missing = [
        field
        for field, value in (
            ("exit_quantity", quantity),
            ("avg_cost", avg_cost),
            ("entry_price", entry_price),
            ("exit_price", exit_price),
            ("gross_amount", gross),
            ("net_amount", net_amount),
            ("realized_pnl", recorded),
        )
        if value is None
    ]
    gross_minus_commission = _diff(gross, _money(net_amount + commission) if None not in (net_amount, commission) else None)
    fee_ok = _diff(gross, _money(net_amount + total_fee) if None not in (net_amount, total_fee) else None)
    price_pnl = _money((exit_price - entry_price) * quantity) if None not in (exit_price, entry_price, quantity) else None
    avg_basis = _money(net_amount - avg_cost * quantity) if None not in (net_amount, avg_cost, quantity) else None
    entry_basis = _money(net_amount - entry_price * quantity) if None not in (net_amount, entry_price, quantity) else None
    recorded_vs_avg = _diff(recorded, avg_basis)
    recorded_vs_entry = _diff(recorded, entry_basis)
    if missing:
        status = "missing_fields"
    elif abs(recorded_vs_avg or Decimal(0)) <= TOLERANCE:
        status = "matches_avg_cost"
    elif abs(recorded_vs_entry or Decimal(0)) <= TOLERANCE:
        status = "matches_entry_price"
    else:
        status = "matches_neither"
    return {
        "index": index,
        "symbol": symbol,
        "name": row.get("name"),
        "entry_date": row.get("entry_date"),
        "exit_date": row.get("exit_date"),
        "exit_quantity": _number(quantity),
        "entry_price": _number(entry_price),
        "avg_cost": _number(avg_cost),
        "exit_price": _number(exit_price),
        "gross_amount": _number(gross),
        "commission": _number(commission),
        "total_fee": _number(total_fee),
        "net_amount": _number(net_amount),
        "gross_minus_commission_minus_net": _number(gross_minus_commission),
        "gross_minus_total_fee_minus_net": _number(fee_ok),
        "fees_consistent": fee_ok is not None and abs(fee_ok) <= TOLERANCE,
        "realized_pnl_recorded": _number(recorded),
        "price_only_pnl": _number(price_pnl),
        "recorded_minus_price_only_pnl": _number(_diff(recorded, price_pnl)),
        "realized_pnl_avg_cost_basis": _number(avg_basis),
        "realized_pnl_entry_price_basis": _number(entry_basis),
        "recorded_minus_avg_basis": _number(recorded_vs_avg),
        "recorded_minus_entry_basis": _number(recorded_vs_entry),
        "canonical_avg_basis_delta": _number(_diff(avg_basis, recorded)),
        "status": status,
        "missing_fields": missing,
    }


def _check_reductions(state: dict[str, Any]) -> dict[str, Any]:
    rows = _rows(state, "realized_reductions")
    items = []
    for index, row in enumerate(rows):
        quantity = _decimal(row.get("quantity"))
        avg_cost = _decimal(row.get("avg_cost"))
        net_amount = _decimal(row.get("net_amount"))
        recorded = _decimal(row.get("realized_pnl"))
        basis = _money(net_amount - avg_cost * quantity) if None not in (quantity, avg_cost, net_amount) else None
        items.append(
            {
                "index": index,
                "symbol": str(row.get("symbol") or ""),
                "quantity": _number(quantity),
                "avg_cost": _number(avg_cost),
                "net_amount": _number(net_amount),
                "realized_pnl_recorded": _number(recorded),
                "realized_pnl_avg_cost_basis": _number(basis),
                "canonical_avg_basis_delta": _number(_diff(basis, recorded)),
            }
        )
    raw = state.get("realized_reductions")
    complete = isinstance(raw, list) and len(raw) == len(rows)
    recorded_sum = _money(_sum_known(rows, "realized_pnl", complete=complete))
    return {"count": len(rows), "rows": items, "recorded_realized_pnl_sum": _number(recorded_sum),
            "complete": complete and recorded_sum is not None}


def _sum_deltas(rows: list[dict[str, Any]]) -> Decimal:
    total = Decimal(0)
    for row in rows:
        value = _decimal(row.get("canonical_avg_basis_delta"))
        if value is not None:
            total += value
    return total


def audit(state_path: Path) -> dict[str, Any]:
    source_bytes = state_path.read_bytes()
    state = json.loads(source_bytes.decode("utf-8-sig"))
    balance = _check_balance(state)
    identity = _check_pnl_identity(state)
    positions = [_position_row(row, index) for index, row in enumerate(_rows(state, "positions"))]
    closed = [_closed_row(row, index) for index, row in enumerate(_rows(state, "closed_positions"))]
    reductions = _check_reductions(state)
    known_conflicts = cost_basis_conflict_evidence(state)

    position_delta = _sum_deltas(positions)
    closed_delta = _sum_deltas(closed)
    reduction_delta = _sum_deltas(reductions["rows"])
    identity_difference = _decimal(identity["difference"])
    delta_rows = positions + closed + reductions["rows"]
    delta_complete = all(_decimal(row.get("canonical_avg_basis_delta")) is not None for row in delta_rows)
    canonical_difference = (
        _money(identity_difference - position_delta - closed_delta - reduction_delta)
        if identity_difference is not None and delta_complete
        else None
    )

    raw_positions = state.get("positions")
    raw_closed = state.get("closed_positions")
    position_rows = _rows(state, "positions")
    closed_rows = _rows(state, "closed_positions")
    open_unrealized_sum = _money(_sum_known(position_rows, "unrealized_pnl",
        complete=isinstance(raw_positions, list) and len(raw_positions) == len(position_rows)))
    closed_realized_sum = _money(_sum_known(closed_rows, "realized_pnl",
        complete=isinstance(raw_closed, list) and len(raw_closed) == len(closed_rows)))
    legacy_adjustment = _decimal(state.get("legacy_realized_adjustment"))
    reduction_recorded_sum = _decimal(reductions["recorded_realized_pnl_sum"])
    detail_realized = (_money(closed_realized_sum + reduction_recorded_sum + legacy_adjustment)
                       if None not in (closed_realized_sum, reduction_recorded_sum, legacy_adjustment) else None)

    consistency = {
        "top_level_unrealized_equals_position_sum": _number(_diff(_decimal(state.get("unrealized_pnl")), open_unrealized_sum)),
        "top_level_realized_equals_detail_sum": _number(_diff(_decimal(state.get("realized_pnl")), detail_realized)),
        "closed_realized_sum": _number(closed_realized_sum),
        "legacy_realized_adjustment": _number(legacy_adjustment),
        "detail_realized_with_legacy_adjustment": _number(detail_realized),
    }

    conflicts_open = [row for row in positions if row["status"] == "matches_entry_price"]
    conflicts_closed = [row for row in closed if row["status"] == "matches_entry_price"]
    unexplained = [
        row for row in positions if row["status"] == "matches_neither"
    ] + [row for row in closed if row["status"] == "matches_neither"]
    missing = [row for row in positions + closed if row["status"] == "missing_fields"]

    cost_basis_explains = (
        canonical_difference is not None
        and abs(canonical_difference) <= TOLERANCE
        and bool(conflicts_open or conflicts_closed or reduction_delta)
        and not unexplained
        and not missing and reductions["complete"]
    )
    detail_differences = (
        consistency["top_level_unrealized_equals_position_sum"],
        consistency["top_level_realized_equals_detail_sum"],
    )
    details_match = all(value is not None and abs(value) <= float(TOLERANCE) for value in detail_differences)
    if unexplained or missing or not reductions["complete"] or open_unrealized_sum is None or detail_realized is None:
        status = "unexplained"
    elif identity["ok"] and balance["ok"] and details_match and all(row["fees_consistent"] for row in closed):
        status = "consistent"
    elif cost_basis_explains and balance["ok"]:
        status = "cost_basis_conflict"
    else:
        status = "mismatch"

    warnings: list[str] = []
    if state.get("credit_used") not in (None, 0, 0.0) or state.get("simulated_interest") not in (None, 0, 0.0):
        warnings.append("financing_or_interest_policy_not_audited")
    if _decimal(state.get("partial_realized_pnl")) not in (None, 0):
        warnings.append("partial_realized_pnl_field_ignored_audit_uses_realized_reductions_rows")

    return {
        "schema": "paper_identity_audit_v1",
        "state_path": str(state_path),
        "state_sha256": hashlib.sha256(source_bytes).hexdigest(),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "state_meta": {
            "schema_version": state.get("schema_version"),
            "updated_at": state.get("updated_at"),
            "valuation_date": state.get("valuation_date"),
            "position_count_declared": state.get("position_count"),
            "closed_positions_count": len(closed),
        },
        "balance": balance,
        "pnl_identity": identity,
        "positions": {
            "count": len(positions),
            "conflict_count_entry_basis": len(conflicts_open),
            "rows": positions,
            "canonical_avg_basis_delta_sum": _number(position_delta),
        },
        "closed_positions": {
            "count": len(closed),
            "conflict_count_entry_basis": len(conflicts_closed),
            "fee_inconsistent_count": sum(1 for row in closed if not row["fees_consistent"]),
            "rows": closed,
            "canonical_avg_basis_delta_sum": _number(closed_delta),
        },
        "realized_reductions": {**reductions, "canonical_avg_basis_delta_sum": _number(reduction_delta)},
        "top_level_consistency": consistency,
        "decomposition": {
            "reported_identity_difference": _number(identity_difference),
            "position_cost_basis_delta_sum": _number(position_delta),
            "closed_cost_basis_delta_sum": _number(closed_delta),
            "reduction_cost_basis_delta_sum": _number(reduction_delta),
            "delta_rows_missing": sum(1 for row in delta_rows if _decimal(row.get("canonical_avg_basis_delta")) is None),
            "delta_sums_are_partial": not delta_complete,
            "residual_after_cost_basis_unification": _number(canonical_difference),
            "cost_basis_differences_arithmetically_match": cost_basis_explains,
        },
        "known_cost_field_candidates": {
            "reported_difference": known_conflicts["reported_pnl_difference"],
            "candidate_delta_sum": known_conflicts["candidate_adjustment_sum"],
            "arithmetic_match": known_conflicts["explains_reported_difference"],
            "candidates": known_conflicts["candidates"],
            "rows_with_unknown_cost_fields": known_conflicts["unknown_count"],
            "historical_cashflows_verified": False,
        },
        "explanations": {
            "open_positions_using_entry_price": [
                {"symbol": row["symbol"], "delta": row["canonical_avg_basis_delta"]} for row in conflicts_open
            ],
            "closed_positions_using_entry_price": [
                {"symbol": row["symbol"], "delta": row["canonical_avg_basis_delta"]} for row in conflicts_closed
            ],
            "unexplained_rows": [{"symbol": row["symbol"]} for row in unexplained],
            "rows_with_missing_fields": [{"symbol": row["symbol"], "missing": row["missing_fields"]} for row in missing],
        },
        "warnings": warnings,
        "verdict": {
            "status": status,
            "identity_ok": identity["ok"],
            "balance_ok": balance["ok"],
            "repairable_by_avg_cost_unification": None,
        },
        "read_only": True,
    }


def render(report: dict[str, Any]) -> str:
    identity = report["pnl_identity"]
    balance = report["balance"]
    decomposition = report["decomposition"]
    lines = [
        "PAPER LEDGER IDENTITY AUDIT (read-only)",
        f"state            : {report['state_path']}",
        f"schema / updated : {report['state_meta']['schema_version']} / {report['state_meta']['updated_at']}",
        f"valuation_date   : {report['state_meta']['valuation_date']}",
        "",
        "[1] BALANCE CHECKS",
        f"    cash={_fmt(balance['cash'])}  equity={_fmt(balance['total_equity'])}  "
        f"market_value={_fmt(balance['market_value_reported'])}",
        f"    equity - (cash + sum market_value)    : {_fmt(balance['diffs']['equity_equals_cash_plus_detail_market_value'])}  "
        f"{'OK' if balance['diffs']['equity_equals_cash_plus_detail_market_value'] is not None and abs(balance['diffs']['equity_equals_cash_plus_detail_market_value']) <= 0.01 else 'UNVERIFIED_OR_MISMATCH'}",
        f"    equity - (cash + sum price*quantity)  : {_fmt(balance['diffs']['equity_equals_cash_plus_price_times_quantity'])}",
        f"    reported market_value - detail sum    : {_fmt(balance['diffs']['reported_market_value_equals_detail_sum'])}",
        f"    balance_ok: {balance['ok']}",
        "",
        "[2] PNL IDENTITY",
        f"    equity={_fmt(identity['total_equity'])}  initial={_fmt(identity['initial_cash'])}  "
        f"realized={_fmt(identity['realized_pnl'])}  unrealized={_fmt(identity['unrealized_pnl'])}",
        f"    equity_from_pnl={_fmt(identity['equity_from_pnl'])}  difference={_fmt(identity['difference'])}  "
        f"OK={identity['ok']}",
        "",
        "[3] POSITION COST BASIS",
    ]
    for row in report["positions"]["rows"]:
        if row["status"] == "matches_avg_cost" and abs(row["recorded_minus_avg_basis"] or 0) <= 0.01:
            continue
        lines.append(
            f"    {row['symbol']:<8} qty={row['quantity']} avg={row['avg_cost']} entry={row['entry_price']} "
            f"gap={_fmt(row['entry_minus_avg_cost'])} impact={_fmt(row['cost_basis_gap_amount'])} "
            f"recorded={_fmt(row['recorded_unrealized_pnl'])} avg_basis={_fmt(row['unrealized_pnl_avg_cost_basis'])} "
            f"status={row['status']}"
        )
    if report["positions"]["conflict_count_entry_basis"] == 0:
        lines.append("    no entry-price-basis conflicts")
    elif report["positions"]["conflict_count_entry_basis"] > 0:
        lines.append(f"    entry-price-basis conflicts: {report['positions']['conflict_count_entry_basis']}")
        for item in report["explanations"]["open_positions_using_entry_price"]:
            lines.append(f"      - {item['symbol']}: canonical delta {_fmt(item['delta'])}")
    lines.extend(["", "[4] CLOSED POSITION CHECKS"])
    for row in report["closed_positions"]["rows"]:
        if row["status"] == "matches_avg_cost" and row["fees_consistent"]:
            continue
        lines.append(
            f"    {row['symbol']:<8} qty={row['exit_quantity']} avg={row['avg_cost']} entry={row['entry_price']} "
            f"exit={row['exit_price']} gross={_fmt(row['gross_amount'])} fees_ok={row['fees_consistent']} "
            f"recorded={_fmt(row['realized_pnl_recorded'])} avg_basis={_fmt(row['realized_pnl_avg_cost_basis'])} "
            f"delta={_fmt(row['canonical_avg_basis_delta'])} status={row['status']}"
        )
    if report["closed_positions"]["conflict_count_entry_basis"] == 0:
        lines.append("    no entry-price-basis conflicts")
    lines.extend(
        [
            f"    fee-inconsistent rows: {report['closed_positions']['fee_inconsistent_count']}",
            "",
            "[5] TOP-LEVEL VS DETAIL SUMS",
            f"    unrealized: top - position sum = {_fmt(report['top_level_consistency']['top_level_unrealized_equals_position_sum'])}",
            f"    realized  : top - detail sum   = {_fmt(report['top_level_consistency']['top_level_realized_equals_detail_sum'])}",
            "",
            "[6] DIFFERENCE DECOMPOSITION",
            f"    reported identity difference          : {_fmt(decomposition['reported_identity_difference'])}",
            f"    + open position cost-basis deltas     : {_fmt(decomposition['position_cost_basis_delta_sum'])}",
            f"    + closed position cost-basis deltas   : {_fmt(decomposition['closed_cost_basis_delta_sum'])}",
            f"    + realized reduction deltas           : {_fmt(decomposition['reduction_cost_basis_delta_sum'])}",
            f"    partial sums / missing rows           : {decomposition['delta_sums_are_partial']} / {decomposition['delta_rows_missing']}",
            f"    = residual after avg-cost unification : {_fmt(decomposition['residual_after_cost_basis_unification'])}",
            f"    cost-basis differences numerically match: {decomposition['cost_basis_differences_arithmetically_match']} (not proof of cause)",
            f"    known candidate delta sum              : {_fmt(report['known_cost_field_candidates']['candidate_delta_sum'])}",
            f"    known candidates match difference     : {report['known_cost_field_candidates']['arithmetic_match']} (cash flows unverified)",
            "",
            "[7] VERDICT",
            f"    status={report['verdict']['status']}  identity_ok={report['verdict']['identity_ok']}  "
            f"balance_ok={report['verdict']['balance_ok']}  "
            f"repairable_by_avg_cost_unification={report['verdict']['repairable_by_avg_cost_unification']}",
        ]
    )
    for warning in report["warnings"]:
        lines.append(f"    warning: {warning}")
    if report["explanations"]["unexplained_rows"]:
        lines.append(f"    unexplained rows: {report['explanations']['unexplained_rows']}")
    if report["explanations"]["rows_with_missing_fields"]:
        lines.append(f"    rows with missing fields: {report['explanations']['rows_with_missing_fields']}")
    return "\n".join(lines)


def _fmt(value: float | None) -> str:
    return f"{value:+.2f}" if value is not None else "n/a"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--state", type=Path, default=DEFAULT_STATE, help=f"paper ledger JSON (default: {DEFAULT_STATE})")
    parser.add_argument("--json-out", type=Path, default=None, help="write the structured audit report to this path")
    parser.add_argument("--quiet", action="store_true", help="suppress the human-readable report")
    args = parser.parse_args(argv)

    if args.json_out is not None:
        try:
            aliases_input = args.json_out.resolve() == args.state.resolve()
            if not aliases_input and args.json_out.exists() and args.state.exists():
                aliases_input = args.json_out.samefile(args.state)
        except OSError:
            print("ERROR: audit output identity could not be checked", file=sys.stderr)
            return 2
        if aliases_input:
            print("ERROR: audit output must not overwrite the input ledger", file=sys.stderr)
            return 2
    if not args.state.is_file():
        print(f"ERROR: state file not found: {args.state}", file=sys.stderr)
        return 2
    try:
        report = audit(args.state)
    except (json.JSONDecodeError, OSError) as exc:
        print(f"ERROR: cannot audit {args.state}: {exc}", file=sys.stderr)
        return 2

    if not args.quiet:
        print(render(report))
    if args.json_out is not None:
        args.json_out.parent.mkdir(parents=True, exist_ok=True)
        args.json_out.write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        print(f"\nJSON report written: {args.json_out}")
    # Even a consistent result means only these recorded amounts reconcile; it
    # does not establish that original cash flows or corporate actions exist.
    return 0 if report["verdict"]["status"] == "consistent" else 1


if __name__ == "__main__":
    raise SystemExit(main())
