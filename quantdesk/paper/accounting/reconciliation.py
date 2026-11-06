"""Pure paper-account reconciliation and validation; no storage or execution."""
from __future__ import annotations

import copy
import math
from typing import Any

from quantdesk.paper.realized_corrections import inspect_realized_adjustment
from quantdesk.shared import normalize_symbol, safe_float


def number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        parsed = float(value)
        return parsed if math.isfinite(parsed) else None
    except (TypeError, ValueError, OverflowError):
        return None


def strategy_pnl_identity(data: dict[str, Any]) -> dict[str, Any]:
    """Compare declared PnL with equity without creating an adjustment entry."""

    required = ("initial_cash", "total_equity", "realized_pnl", "unrealized_pnl")
    amounts = {key: number(data.get(key)) for key in required}
    missing = [key for key, value in amounts.items() if value is None]
    result: dict[str, Any] = {
        "status": "missing", "difference": None, "missing_fields": missing,
        "equity_from_pnl": None, "tolerance": 0.01,
        "definition": "total_equity - (initial_cash + realized_pnl + unrealized_pnl)",
        "read_only": True,
    }
    if "realized_corrections" in data:
        correction = inspect_realized_adjustment(data)
        result["realized_adjustment"] = correction
        if correction["status"] == "invalid":
            return {**result, "status": "invalid", "reason_codes": correction["reason_codes"]}
    if missing:
        return result
    # Financing belongs to its declared accounting policy, not an inferred
    # adjustment of this cash-funded strategy book's historical PnL.
    financing = [key for key in ("credit_used", "simulated_interest")
                 if key in data and number(data[key]) != 0]
    if financing:
        return {**result, "status": "unverified", "unreconciled_fields": financing}
    expected = sum(amounts[key] for key in ("initial_cash", "realized_pnl", "unrealized_pnl"))
    difference = round(amounts["total_equity"] - expected, 2)
    return {**result, "status": "mismatch" if abs(difference) > 0.01 else "consistent",
            "equity_from_pnl": round(expected, 2), "difference": difference}


def strategy_state_errors(data: dict[str, Any]) -> list[str]:
    """Validate evidence without supplying money or normalizing account totals."""

    errors = []
    for key in ("cash", "initial_cash"):
        value = number(data.get(key))
        if value is None:
            errors.append(f"paper_state_{key}_missing_or_invalid")
        elif value < 0:
            errors.append(f"paper_state_{key}_negative")
    if data.get("ledger_read_status") in {"missing", "invalid", "recovery_available"}:
        errors.append("paper_ledger_requires_initialization_or_recovery")
    rows = data.get("positions")
    if not isinstance(rows, list):
        errors.append("paper_state_positions_not_list")
        rows = []
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            errors.append(f"paper_state_position_{index}_not_object")
            continue
        for key in ("quantity", "latest_price", "market_value"):
            if key in row and number(row[key]) is None:
                errors.append(f"paper_state_position_{index}_{key}_invalid")
        for key in ("unrealized_pnl", "entry_price", "adjusted_quantity_for_valuation"):
            if key in row and number(row[key]) is None:
                errors.append(f"paper_state_position_{index}_{key}_invalid")
        if (number(row.get("quantity")) or 0) < 0:
            errors.append(f"paper_state_position_{index}_negative_quantity")
    for key in ("closed_positions", "realized_reductions", "actions"):
        if key in data and (not isinstance(data[key], list) or any(not isinstance(row, dict) for row in data[key])):
            errors.append(f"paper_state_{key}_invalid")
    for key in ("market_value", "total_equity", "realized_pnl", "unrealized_pnl", "legacy_realized_adjustment"):
        if key in data and number(data[key]) is None:
            errors.append(f"paper_state_{key}_invalid")
    if "realized_corrections" in data:
        errors.extend(inspect_realized_adjustment(data)["reason_codes"])
    return errors


def _normalized_management_state(row: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    normalized = dict(row)
    management_state = str(normalized.get("management_state") or "").strip()
    watch_status = str(normalized.get("watch_status") or "").strip()
    canonical = management_state or watch_status
    if not canonical:
        canonical = "closed" if str(normalized.get("status") or "").strip() == "closed" else "normal"
    mismatch = bool(management_state and watch_status and management_state != watch_status)
    normalized["management_state"] = canonical
    normalized["watch_status"] = canonical
    return normalized, mismatch


_PAPER_POSITION_NUMERIC_FIELDS = ("quantity", "market_value", "unrealized_pnl")
_PAPER_CLOSED_NUMERIC_FIELDS = (
    "quantity",
    "market_value",
    "unrealized_pnl",
    "realized_pnl",
)
_PAPER_TOP_LEVEL_NUMERIC_FIELDS = (
    "initial_cash",
    "cash",
    "market_value",
    "unrealized_pnl",
    "realized_pnl",
    "total_equity",
    "legacy_realized_adjustment",
)


def _parseable_paper_number(value: Any) -> bool:
    """Reject booleans and non-finite/non-numeric ledger values."""

    return not isinstance(value, bool) and safe_float(value, None) is not None


def _paper_state_shape_errors(state: Any) -> list[str]:
    """Return deterministic shape/value errors without mutating a paper state."""

    if not isinstance(state, dict):
        return ["paper_state_not_object"]

    errors: list[str] = []
    if state.get("ledger_read_status") in {"missing", "invalid", "recovery_available"}:
        errors.append("paper_ledger_requires_initialization_or_recovery")
        errors.extend(str(item) for item in state.get("ledger_read_reason_codes") or [])
    for key, numeric_fields in (
        ("positions", _PAPER_POSITION_NUMERIC_FIELDS),
        ("closed_positions", _PAPER_CLOSED_NUMERIC_FIELDS),
        ("realized_reductions", _PAPER_CLOSED_NUMERIC_FIELDS),
    ):
        if key not in state:
            continue
        rows = state.get(key)
        if not isinstance(rows, list):
            errors.append(f"{key}_not_list")
            continue
        for index, row in enumerate(rows):
            if not isinstance(row, dict):
                errors.append(f"{key}_row_{index}_not_object")
                continue
            for field in numeric_fields:
                if field in row and not _parseable_paper_number(row.get(field)):
                    errors.append(f"{key}_row_{index}_{field}_not_numeric")

    for field in _PAPER_TOP_LEVEL_NUMERIC_FIELDS:
        if field in state and not _parseable_paper_number(state.get(field)):
            errors.append(f"{field}_not_numeric")
    return errors


def _paper_correction_fields(state: dict[str, Any]) -> dict[str, Any]:
    evidence = inspect_realized_adjustment(state, legacy_base=safe_float(state.get("legacy_realized_adjustment"), 0.0))
    return {"realized_corrections": copy.deepcopy(state.get("realized_corrections", [])),
        "realized_corrections_total": evidence["realized_corrections_total"],
        "effective_realized_adjustment": evidence["effective_realized_adjustment"],
        "realized_correction_evidence": evidence}


def reconcile_paper_account(state: dict[str, Any]) -> dict[str, Any]:
    """Return a detail-derived paper-account state without mutating the input.

    Historical ledgers may contain a realized-PnL amount that differs from the
    sum of closed-position rows.  That difference is retained explicitly as a
    legacy adjustment; all other account totals are rebuilt from current detail.
    """

    invalid_reason_codes = _paper_state_shape_errors(state)
    if isinstance(state, dict) and "realized_corrections" in state:
        invalid_reason_codes.extend(inspect_realized_adjustment(state)["reason_codes"])
    reconciled = copy.deepcopy(state if isinstance(state, dict) else {})
    positions: list[dict[str, Any]] = []
    status_mismatches: list[str] = []
    raw_positions = reconciled.get("positions")
    if not isinstance(raw_positions, list):
        raw_positions = []
    for raw in raw_positions:
        if not isinstance(raw, dict):
            continue
        row, mismatch = _normalized_management_state(raw)
        positions.append(row)
        if mismatch:
            status_mismatches.append(normalize_symbol(row.get("symbol")) or "unknown")

    raw_closed_positions = reconciled.get("closed_positions")
    if not isinstance(raw_closed_positions, list):
        raw_closed_positions = []
    closed_positions = [dict(row) for row in raw_closed_positions if isinstance(row, dict)]
    raw_reductions = reconciled.get("realized_reductions")
    if not isinstance(raw_reductions, list):
        raw_reductions = []
    realized_reductions = [dict(row) for row in raw_reductions if isinstance(row, dict)]
    cash = safe_float(reconciled.get("cash"), 0.0) or 0.0
    market_value = sum(safe_float(row.get("market_value"), 0.0) or 0.0 for row in positions)
    unrealized_pnl = sum(safe_float(row.get("unrealized_pnl"), 0.0) or 0.0 for row in positions)
    closed_realized_pnl = sum(
        safe_float(row.get("realized_pnl"), 0.0) or 0.0
        for row in closed_positions
    )
    partial_realized_pnl = sum(
        safe_float(row.get("realized_pnl"), 0.0) or 0.0
        for row in realized_reductions
    )
    detail_realized_pnl = closed_realized_pnl + partial_realized_pnl
    explicit_adjustment = safe_float(reconciled.get("legacy_realized_adjustment"), None)
    if explicit_adjustment is None:
        historical_realized = safe_float(reconciled.get("realized_pnl"), None)
        explicit_adjustment = (
            historical_realized - detail_realized_pnl
            if historical_realized is not None
            else 0.0
        )

    correction = inspect_realized_adjustment(reconciled, legacy_base=explicit_adjustment)
    effective_adjustment = safe_float(correction["effective_realized_adjustment"], explicit_adjustment)
    totals = {
        "market_value": round(market_value, 2),
        "unrealized_pnl": round(unrealized_pnl, 2),
        "realized_pnl": round(detail_realized_pnl + effective_adjustment, 2),
        "total_equity": round(cash + market_value, 2),
    }
    pnl_identity = strategy_pnl_identity({**reconciled, **totals})
    drift_fields = [
        key
        for key, value in totals.items()
        if key in reconciled
        and abs((safe_float(reconciled.get(key), 0.0) or 0.0) - value) > 0.01
    ]

    warnings = [str(item) for item in reconciled.get("warnings") or []]
    if drift_fields:
        warnings.extend(
            [
                "paper_account_top_level_drift_detected",
                f"paper_account_top_level_drift_fields:{','.join(drift_fields)}",
            ]
        )
    if status_mismatches:
        warnings.append(
            "paper_position_status_mismatch_reconciled:"
            + ",".join(sorted(set(status_mismatches)))
        )
    if abs(explicit_adjustment) > 0.005:
        warnings.append("paper_account_legacy_realized_adjustment_preserved")
    if pnl_identity["status"] != "consistent":
        warnings.append("paper_account_pnl_identity_" + pnl_identity["status"])
    if invalid_reason_codes:
        warnings.append("paper_account_invalid")
        warnings.extend(f"paper_account_invalid:{reason}" for reason in invalid_reason_codes)

    reconciled.update(totals)
    reconciled["cash"] = round(cash, 2)
    reconciled["positions"] = positions
    reconciled["closed_positions"] = closed_positions
    reconciled["realized_reductions"] = realized_reductions
    reconciled["position_count"] = len(positions)
    reconciled["legacy_realized_adjustment"] = round(explicit_adjustment, 2)
    if "realized_corrections" in reconciled and correction["status"] != "invalid":
        reconciled["realized_corrections_total"] = correction["realized_corrections_total"]
        reconciled["effective_realized_adjustment"] = correction["effective_realized_adjustment"]
    reconciled["warnings"] = list(dict.fromkeys(warnings))
    reconciled["accounting_reconciliation"] = {
        "status": (
            "invalid"
            if invalid_reason_codes
            else "unverified"
            if pnl_identity["status"] != "consistent"
            else "reconciled"
            if drift_fields or status_mismatches
            else "consistent"
        ),
        "reason": invalid_reason_codes[0] if invalid_reason_codes else "",
        "invalid_reason_codes": invalid_reason_codes,
        "detail_market_value": totals["market_value"],
        "detail_unrealized_pnl": totals["unrealized_pnl"],
        "closed_realized_pnl": round(closed_realized_pnl, 2),
        "partial_realized_pnl": round(partial_realized_pnl, 2),
        "legacy_realized_adjustment": round(explicit_adjustment, 2),
        "realized_corrections_total": correction["realized_corrections_total"],
        "effective_realized_adjustment": correction["effective_realized_adjustment"],
        "realized_correction_evidence": correction,
        "pnl_identity": pnl_identity,
        "drift_fields": drift_fields,
        "status_mismatch_symbols": sorted(set(status_mismatches)),
    }
    return reconciled
