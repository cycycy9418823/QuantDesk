"""Append-only, reconstructable transactions for the opt-in strategy book.

A labelled opening balance preserves inherited history without inventing old
fills. Each later JSONL line is one whole daily transaction: ordered outcomes,
new intents, valuation evidence and a deterministic state patch. Replay checks
the hash chain, cash/quantity conservation and saved raw opening evidence.

The caller holds the existing portfolio lock. A second OS lock protects the
journal append. A complete transaction is fsynced before the caller atomically
writes its derived ledger. A torn final line fails closed; it is never trimmed
or silently ignored. This is local crash recovery, not a signed audit service.
"""

from __future__ import annotations

import copy
import hashlib
import json
import math
import os
from pathlib import Path
from typing import Any

from quantdesk.candidate_contract import normalize_strategy_type
from quantdesk.interprocess_lock import interprocess_file_lock
from quantdesk.paper.market_rules import order_quantity_rule
from quantdesk.paper.snapshot_open_fill import FILL_MODE_NEXT_OPEN_SNAPSHOT, aware_timestamp, evaluate_open_fill, next_trading_day
from quantdesk.shared import normalize_symbol

SCHEMA = "strategy_fill_transactions.v1"


class JournalError(ValueError):
    """Ledger/journal integrity failed; no automatic repair is safe."""


def _bytes(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                      allow_nan=False).encode("utf-8")


def state_digest(state: dict[str, Any]) -> str:
    return hashlib.sha256(_bytes(state)).hexdigest()


def _number(value: Any) -> float:
    if isinstance(value, bool):
        raise JournalError("journal_non_numeric_amount")
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise JournalError("journal_non_numeric_amount") from exc
    if not math.isfinite(parsed):
        raise JournalError("journal_non_finite_amount")
    return parsed


def _key(row: dict[str, Any]) -> tuple[str, str]:
    return normalize_symbol(row.get("symbol")), normalize_strategy_type(row.get("strategy_type"), "long")


def _positions(state: dict[str, Any]) -> dict[tuple[str, str], dict[str, Any]]:
    result = {}
    for row in state.get("positions") or []:
        if not isinstance(row, dict) or not _key(row)[0] or _key(row) in result:
            raise JournalError("journal_ambiguous_position")
        quantity = _number(row.get("quantity"))
        if quantity <= 0 or quantity != int(quantity):
            raise JournalError("journal_invalid_position_quantity")
        result[_key(row)] = row
    return result


def _orders(state: dict[str, Any]) -> dict[str, dict[str, Any]]:
    result = {}
    orders = state.get("pending_orders") or []
    if not isinstance(orders, list):
        raise JournalError("journal_pending_orders_not_list")
    for order in orders:
        if not isinstance(order, dict):
            raise JournalError("journal_pending_order_not_object")
        key = str(order.get("order_id") or "")
        if not key or key in result:
            raise JournalError("journal_ambiguous_pending_order")
        result[key] = order
    return result


def _validate_totals(state: dict[str, Any], *, checkpoint: bool = False) -> None:
    cash = _number(state.get("cash"))
    if cash < -0.005 or _number(state.get("initial_cash")) <= 0:
        raise JournalError("journal_invalid_cash")
    positions = _positions(state)
    _orders(state)
    market = sum(_number(row.get("market_value")) for row in positions.values())
    for key, expected in (("market_value", market), ("total_equity", cash + market)):
        if key in state and abs(_number(state[key]) - round(expected, 2)) > 0.011:
            raise JournalError(f"journal_{key}_mismatch")
    if checkpoint:
        return  # old positions are an explicitly unverified opening balance
    for row in positions.values():
        price = _number(row.get("latest_price"))
        quantity = _number(row.get("quantity"))
        valued_quantity = quantity
        if row.get("price_adjustment_status") == "adjusted":
            valued_quantity = _number(row.get("adjusted_quantity_for_valuation"))
            ratio = _number(row.get("adjustment_factor_ratio"))
            if ratio <= 0 or abs(valued_quantity - quantity * ratio) > 0.01:
                raise JournalError("journal_adjusted_quantity_mismatch")
        if abs(_number(row.get("market_value")) - round(price * valued_quantity, 2)) > 0.011:
            raise JournalError("journal_position_market_value_mismatch")


def _validate_transition(before: dict[str, Any], after: dict[str, Any], events: list[dict[str, Any]]) -> None:
    _validate_totals(after)
    if before.get("initial_cash") != after.get("initial_cash"):
        raise JournalError("journal_initial_cash_changed")
    pending = copy.deepcopy(_orders(before))
    positions = copy.deepcopy(_positions(before))
    expected_cash = _number(before["cash"])
    seen_outcomes: set[str] = set()
    new_orders: set[str] = set()
    filled_buys: dict[tuple[str, str], dict[str, Any]] = {}
    sells: list[dict[str, Any]] = []
    transitions = [event for event in events if event.get("event_type") == "policy_transition"]
    if after.get("strategy_fill_mode") != FILL_MODE_NEXT_OPEN_SNAPSHOT or after.get("fill_policy") != "next_open":
        raise JournalError("journal_wrong_effective_policy")
    if before.get("strategy_fill_mode") != FILL_MODE_NEXT_OPEN_SNAPSHOT:
        old_header = {key: value for key, value in before.items() if key.startswith("fill_policy")}
        if (len(transitions) != 1 or transitions[0].get("from") != old_header
                or transitions[0].get("to") != "next_open" or transitions[0].get("scope") != "future_fills_only"
                or transitions[0].get("opening_state_sha256") != state_digest(before)):
            raise JournalError("journal_policy_transition_missing")
    elif transitions or before.get("fill_policy_transition") != after.get("fill_policy_transition"):
        raise JournalError("journal_policy_history_changed")
    for event in events:
        kind = event.get("event_type")
        if kind in {"valuation", "policy_transition"}:
            continue
        if kind == "order_submitted":
            order = event.get("order") or {}
            key = str(order.get("order_id") or "")
            if not key or key in pending or key in seen_outcomes or key in new_orders:
                raise JournalError("journal_duplicate_order_submission")
            aware_timestamp(order.get("created_at"))
            if (order.get("fill_mode") != FILL_MODE_NEXT_OPEN_SNAPSHOT or order.get("signal_price_basis") != "raw"
                    or order.get("side") not in {"buy", "sell"}
                    or order.get("signal_date") != after.get("snapshot_decision_date")
                    or order.get("created_at") != after.get("updated_at")
                    or next_trading_day(order["signal_date"], order.get("session_calendar_evidence") or {}) != order.get("fill_date")):
                raise JournalError("journal_unverified_order_submission")
            pending[key] = copy.deepcopy(order)
            new_orders.add(key)
            continue
        if kind != "order_outcome":
            raise JournalError("journal_unknown_event_type")
        order_id = str(event.get("order_id") or "")
        if order_id not in pending or order_id in seen_outcomes or order_id in new_orders:
            raise JournalError("journal_outcome_without_prior_intent")
        order = pending.pop(order_id)
        seen_outcomes.add(order_id)
        if order != event.get("order"):
            raise JournalError("journal_order_changed_at_fill")
        if event.get("status") not in {"filled", "rejected", "expired"}:
            raise JournalError("journal_invalid_outcome")
        if event.get("status") != "filled":
            if event.get("quantity") != 0 or event.get("fill_price") is not None or event.get("amounts"):
                raise JournalError("journal_unfilled_order_moves_cash")
            continue
        key = _key(order)
        quantity = _number(event.get("quantity"))
        price = _number(event.get("fill_price"))
        side = str(order.get("side") or "")
        held = positions.get(key)
        if quantity != _number(order.get("quantity")) or price <= 0 or quantity != int(quantity):
            raise JournalError("journal_fill_quantity_or_price_invalid")
        rule = order_quantity_rule(order.get("symbol"), quantity,
                                   held_shares=held.get("quantity") if held and side == "sell" else None,
                                   name=order.get("name"))
        if rule:
            raise JournalError(f"journal_invalid_order_lot:{rule}")
        evidence = event.get("evidence") or {}
        row = evidence.get("source_row") or {}
        if state_digest(row) != evidence.get("source_row_sha256"):
            raise JournalError("journal_source_row_hash_mismatch")
        snapshot = {"status": "ok", "path": evidence.get("snapshot_path"),
                    "sha256": evidence.get("snapshot_sha256"), "rows": {key[0]: row}}
        checked = evaluate_open_fill(order, snapshot, str(event.get("fill_date") or ""),
                                     calendar=order.get("session_calendar_evidence") or {},
                                     observed_at=event.get("recorded_at"))
        if not checked["ok"] or checked["price"] != price:
            raise JournalError("journal_fill_source_invalid")
        if event.get("side") != side or _key(event) != key or event.get("decision_date") != order.get("fill_date"):
            raise JournalError("journal_fill_identity_mismatch")
        amounts = event.get("amounts") or {}
        gross = _number(amounts.get("gross"))
        fee = _number(amounts.get("total_fee"))
        components = sum(_number(amounts.get(name)) for name in ("commission", "stamp_duty", "transfer_fee", "slippage"))
        if abs(gross - price * quantity) > 0.0002 or fee < 0 or abs(fee - components) > 0.0002:
            raise JournalError("journal_fee_arithmetic_mismatch")
        delta = _number(amounts.get("cash_delta"))
        expected_delta = -(gross + fee) if side == "buy" else gross - fee
        if abs(delta - expected_delta) > 0.0002:
            raise JournalError("journal_cash_delta_mismatch")
        expected_cash += delta
        if expected_cash < -0.0001:
            raise JournalError("journal_negative_cash_at_fill")
        if side == "buy":
            if held:
                raise JournalError("journal_duplicate_position_buy")
            positions[key] = {"quantity": int(quantity)}
            filled_buys[key] = event
        elif side == "sell":
            if not held or quantity != _number(held.get("quantity")):
                raise JournalError("journal_sell_exceeds_or_partially_changes_position")
            entry = str(held.get("entry_date") or held.get("buy_date") or held.get("first_buy_date") or "")[:10]
            if not entry or entry >= order.get("fill_date", ""):
                raise JournalError("journal_t_plus_one_violation")
            sells.append({"key": key, "event": event, "position": held})
            del positions[key]
        else:
            raise JournalError("journal_unknown_fill_side")
    actual_positions = _positions(after)
    if set(positions) != set(actual_positions) or any(
        _number(row.get("quantity")) != _number(actual_positions[key].get("quantity"))
        for key, row in positions.items()
    ):
        raise JournalError("journal_unexplained_position_change")
    if abs(round(expected_cash, 2) - _number(after["cash"])) > 0.001:
        raise JournalError("journal_unexplained_cash_change")
    if pending != _orders(after):
        raise JournalError("journal_unexplained_pending_order_change")
    for key, event in filled_buys.items():
        row = actual_positions[key]
        cost = -_number(event["amounts"]["cash_delta"])
        if (row.get("entry_date") != event["fill_date"] or row.get("execution_price") != event["fill_price"]
                or abs(_number(row.get("entry_cash_out")) - round(cost, 2)) > 0.011):
            raise JournalError("journal_buy_cost_basis_mismatch")
    for key, row in _positions(before).items():
        if key in actual_positions and key not in filled_buys:
            if row.get("entry_price") != actual_positions[key].get("entry_price"):
                raise JournalError("journal_unexplained_entry_cost_change")
    old_closed = before.get("closed_positions") or []
    closed = after.get("closed_positions") or []
    if closed[:len(old_closed)] != old_closed or len(closed) != len(old_closed) + len(sells):
        raise JournalError("journal_closed_history_rewritten")
    for item, row in zip(sells, closed[len(old_closed):]):
        event, held = item["event"], item["position"]
        net = _number(event["amounts"]["cash_delta"])
        pnl = net - _number(held["entry_price"]) * _number(held["quantity"])
        if (_key(row) != item["key"] or row.get("exit_date") != event["fill_date"]
                or row.get("exit_price") != event["fill_price"] or row.get("quantity") != event["quantity"]
                or abs(_number(row.get("net_amount")) - round(net, 2)) > 0.011
                or abs(_number(row.get("realized_pnl")) - round(pnl, 2)) > 0.011):
            raise JournalError("journal_sell_realization_mismatch")
    if before.get("realized_reductions", []) != after.get("realized_reductions", []):
        raise JournalError("journal_reductions_rewritten")


def _patch(before: dict[str, Any], after: dict[str, Any]) -> dict[str, Any]:
    return {"set": {key: copy.deepcopy(value) for key, value in after.items()
                    if key not in before or _bytes(before[key]) != _bytes(value)},
            "delete": sorted(key for key in before if key not in after)}


def _apply(before: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
    result = copy.deepcopy(before)
    for key in patch.get("delete") or []:
        result.pop(key, None)
    result.update(copy.deepcopy(patch.get("set") or {}))
    return result


def _seal(record: dict[str, Any]) -> dict[str, Any]:
    return {**record, "record_sha256": state_digest(record)}


def replay_journal(path: Path) -> dict[str, Any]:
    """Reconstruct the complete ledger without reading any mutable result file."""

    path = Path(path)
    if not path.exists():
        return {"records": [], "state": None, "previous_state": None, "head": ""}
    raw = path.read_bytes()
    if not raw or not raw.endswith(b"\n"):
        raise JournalError("journal_incomplete_tail")
    records = []
    state = previous = None
    previous_hash = ""
    seen_ids = set()
    try:
        for ordinal, line in enumerate(raw.decode("utf-8").splitlines()):
            record = json.loads(line)
            if not isinstance(record, dict):
                raise JournalError("journal_record_not_object")
            digest = record.get("record_sha256")
            body = {key: value for key, value in record.items() if key != "record_sha256"}
            if record.get("schema") != SCHEMA or digest != state_digest(body):
                raise JournalError("journal_record_hash_mismatch")
            if record.get("sequence") != ordinal or record.get("previous_hash") != previous_hash:
                raise JournalError("journal_chain_broken")
            identity = record.get("transaction_id")
            if not identity or identity in seen_ids:
                raise JournalError("journal_duplicate_transaction")
            aware_timestamp(record.get("recorded_at"))
            if ordinal == 0:
                if record.get("kind") != "opening_checkpoint":
                    raise JournalError("journal_missing_opening_checkpoint")
                state = copy.deepcopy(record["state"])
                _validate_totals(state, checkpoint=True)
            else:
                if record.get("kind") != "daily_transaction" or state_digest(state) != record.get("before_state_sha256"):
                    raise JournalError("journal_before_state_mismatch")
                previous = copy.deepcopy(state)
                state = _apply(state, record["state_patch"])
                _validate_transition(previous, state, record["events"])
                if state.get("strategy_journal_sequence") != ordinal or state.get("strategy_journal_transaction") != identity:
                    raise JournalError("journal_ledger_pointer_mismatch")
            if state_digest(state) != record.get("after_state_sha256"):
                raise JournalError("journal_after_state_mismatch")
            records.append(record)
            seen_ids.add(identity)
            previous_hash = digest
    except (KeyError, TypeError, UnicodeError, json.JSONDecodeError) as exc:
        raise JournalError("journal_unreadable_record") from exc
    return {"records": records, "state": state, "previous_state": previous, "head": previous_hash}


def validate_transaction(before: dict[str, Any], after: dict[str, Any], events: list[dict[str, Any]]) -> None:
    """Validate a preview using the exact conservation checks used on replay."""

    _validate_transition(before, after, events)


def recoverable_state(path: Path, current: dict[str, Any] | None) -> dict[str, Any]:
    """Recognize only an exact ledger or its single uncommitted predecessor."""

    replay = replay_journal(path)
    if not replay["records"]:
        return {**replay, "state": current, "status": "new"}
    if current is not None and state_digest(current) == state_digest(replay["state"]):
        return {**replay, "status": "aligned"}
    previous = replay["previous_state"]
    if current is not None and previous is not None and state_digest(current) == state_digest(previous):
        return {**replay, "status": "recovered_uncommitted_transaction"}
    opening = replay["records"][0]
    if current is None and not opening["ledger_existed"] and len(replay["records"]) <= 2:
        return {**replay, "status": "recovered_uncommitted_initialization"}
    raise JournalError("journal_ledger_conflict")


def _append(path: Path, record: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("ab") as handle:
        handle.write(_bytes(record) + b"\n")
        handle.flush()
        os.fsync(handle.fileno())


def commit_transaction(
    path: Path, *, before: dict[str, Any], after: dict[str, Any], events: list[dict[str, Any]],
    transaction_id: str, recorded_at: str, result: dict[str, Any], ledger_existed: bool,
) -> dict[str, Any]:
    """Durably append a validated daily transaction; the caller writes its state."""

    path = Path(path)
    aware_timestamp(recorded_at)
    with interprocess_file_lock(path.with_suffix(path.suffix + ".lock"), timeout_seconds=60.0):
        replay = replay_journal(path)
        records = replay["records"]
        for record in records:
            if record["transaction_id"] == transaction_id:
                raise JournalError("journal_transaction_already_recorded")
        if not records:
            _validate_totals(before, checkpoint=True)
            opening = _seal({"schema": SCHEMA, "kind": "opening_checkpoint", "sequence": 0,
                             "transaction_id": "opening_checkpoint", "recorded_at": recorded_at,
                             "previous_hash": "", "state": copy.deepcopy(before),
                             "after_state_sha256": state_digest(before), "ledger_existed": bool(ledger_existed),
                             "inherited_history_status": "legacy_opening_balance_unverified" if ledger_existed else "empty_book",
                             "paper_only": True, "is_real_order": False})
            # Validate the whole next transaction before even writing the checkpoint.
            head = opening["record_sha256"]
            sequence = 1
        else:
            if state_digest(before) != state_digest(replay["state"]):
                raise JournalError("journal_append_base_conflict")
            head, sequence = replay["head"], len(records)
            opening = None
        target = copy.deepcopy(after)
        target["strategy_journal_sequence"] = sequence
        target["strategy_journal_transaction"] = transaction_id
        _validate_transition(before, target, events)
        record = _seal({"schema": SCHEMA, "kind": "daily_transaction", "sequence": sequence,
                        "transaction_id": transaction_id, "recorded_at": recorded_at, "previous_hash": head,
                        "before_state_sha256": state_digest(before), "after_state_sha256": state_digest(target),
                        "state_patch": _patch(before, target), "events": copy.deepcopy(events),
                        "result": copy.deepcopy(result), "paper_only": True, "is_real_order": False})
        if opening is not None:
            _append(path, opening)
        _append(path, record)
        return {"state": target, "record": record}
