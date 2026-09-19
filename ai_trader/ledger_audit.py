"""Read-only reconciliation of recorded AI-ledger amounts, with visible residuals."""
from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any, Mapping

from ai_trader.execution_evidence import audit_trade_evidence

ZERO = Decimal("0")
HALF_MONEY_UNIT = Decimal("0.00005")


def _decimal(value: Any) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = Decimal(str(value))
        return number if number.is_finite() else None
    except (InvalidOperation, ValueError, TypeError):
        return None


def _sum(values: list[Decimal | None]) -> Decimal | None:
    return None if any(value is None for value in values) else sum(values, ZERO)


def audit_ledger(ledger: Mapping[str, Any]) -> dict[str, Any]:
    """Explain differences without changing balances, trades, fees or precision.

    Tolerances follow the existing four-place cash/mark and six-place average
    cost storage. A residual within that bound remains in the result; it is not
    rounded away or represented as exact equality.
    """
    if ledger.get("ledger_id") != "ai_trader_100k":
        return {"schema": "ai_ledger_audit_v1", "read_only": True,
                "status": "foreign_ledger", "ledger_id": ledger.get("ledger_id"),
                "checks": [], "historical_backfill": False}
    trades = [row for row in (ledger.get("trades") or []) if isinstance(row, dict)]
    positions = [row for row in (ledger.get("positions") or []) if isinstance(row, dict)]
    checks: list[dict[str, Any]] = []

    def check(name: str, recorded: Any, expected: Decimal | None,
              tolerance: Decimal = HALF_MONEY_UNIT, **extra: Any) -> None:
        actual = _decimal(recorded)
        delta = None if actual is None or expected is None else actual - expected
        if delta is None:
            status = "unavailable"
        elif delta == ZERO:
            status = "exact"
        elif abs(delta) <= tolerance:
            status = "rounding_residual"
        else:
            status = "requires_review"
        checks.append({
            "name": name, "status": status,
            "recorded": None if actual is None else str(actual),
            "recomputed": None if expected is None else str(expected),
            "delta": None if delta is None else str(delta),
            "tolerance": str(tolerance), **extra,
        })

    cash_movements = _sum([_decimal(row.get("cash_delta")) for row in trades])
    initial = _decimal(ledger.get("initial_cash"))
    expected_cash = None if initial is None or cash_movements is None else initial + cash_movements
    check("cash_from_recorded_trades", ledger.get("cash"), expected_cash,
          HALF_MONEY_UNIT * (len(trades) + 1),
          coverage="initial_cash + recorded trade cash_delta; other movements are not inferred")
    sells = [row for row in trades if row.get("side") == "sell"]
    check("realized_pnl_from_recorded_exits", ledger.get("realized_pnl"),
          _sum([_decimal(row.get("realized_pnl")) for row in sells]),
          HALF_MONEY_UNIT * (len(sells) + 1))

    for index, trade in enumerate(trades):
        price = _decimal(trade.get("price"))
        qty = _decimal(trade.get("quantity"))
        fee = _decimal(trade.get("fee"))
        expected = None
        if price is not None and qty is not None and fee is not None:
            if trade.get("side") == "buy":
                expected = -(price * qty + fee)
            elif trade.get("side") == "sell":
                expected = price * qty - fee
        # The logged raw price has four decimal places, hence quantity scales
        # its possible quantisation. Do not silently use today's CSV instead.
        tolerance = HALF_MONEY_UNIT * (abs(qty) + 1) if qty is not None else ZERO
        check("trade_cash_components", trade.get("cash_delta"), expected, tolerance,
              trade_index=index, symbol=trade.get("symbol"),
              basis="recorded quantity * recorded raw price and recorded total fee")

    for index, pos in enumerate(positions):
        qty, price, avg = (_decimal(pos.get(key)) for key in ("quantity", "market_price", "avg_cost"))
        value = None if qty is None or price is None else qty * price
        pnl = None if qty is None or price is None or avg is None else (price - avg) * qty
        mark_tolerance = HALF_MONEY_UNIT * (abs(qty) + 1) if qty is not None else ZERO
        check("position_market_value", pos.get("market_value"), value, mark_tolerance,
              position_index=index, symbol=pos.get("symbol"), stale_mark=bool(pos.get("stale_mark")))
        check("position_unrealized_pnl", pos.get("unrealized_pnl"), pnl,
              mark_tolerance + (abs(qty) * Decimal("0.0000005") if qty is not None else ZERO),
              position_index=index, symbol=pos.get("symbol"))

    total_value = _sum([_decimal(row.get("market_value")) for row in positions])
    total_unrealized = _sum([_decimal(row.get("unrealized_pnl")) for row in positions])
    check("market_value_from_positions", ledger.get("market_value"), total_value,
          HALF_MONEY_UNIT * (len(positions) + 1))
    check("unrealized_pnl_from_positions", ledger.get("unrealized_pnl"), total_unrealized,
          HALF_MONEY_UNIT * (len(positions) + 1))
    cash = _decimal(ledger.get("cash"))
    check("equity_from_cash_and_positions", ledger.get("equity"),
          None if cash is None or total_value is None else cash + total_value,
          HALF_MONEY_UNIT * (len(positions) + 2))
    evidence = audit_trade_evidence(list(ledger.get("trades") or []))
    statuses = {row["status"] for row in checks}
    status = "requires_review" if "requires_review" in statuses else (
        "incomplete" if "unavailable" in statuses else (
            "rounding_residuals" if "rounding_residual" in statuses else "exact"
        )
    )
    amount_status = status
    invalid_rows = len(list(ledger.get("trades") or [])) - len(trades) + len(list(ledger.get("positions") or [])) - len(positions)
    if evidence["counts"]["invalid"] or invalid_rows:
        status = "requires_review"
    elif evidence["counts"]["legacy_unverified"] and status == "exact":
        status = "incomplete_evidence"
    return {
        "schema": "ai_ledger_audit_v1", "ledger_id": ledger.get("ledger_id"),
        "as_of": ledger.get("as_of"), "read_only": True, "status": status,
        "amount_status": amount_status, "invalid_record_count": invalid_rows,
        "checks": checks, "execution_evidence": evidence, "historical_backfill": False,
        "rounding_policy": {
            "cash_mark_equity_decimal_places": 4,
            "average_cost_decimal_places": 6,
            "method": "existing Python round; decimal audit of recorded values",
            "precision_changed": False,
            "tolerance_rule": "half stored unit per rounded component; price uncertainty scaled by quantity",
            "residuals_always_visible": True,
        },
    }
