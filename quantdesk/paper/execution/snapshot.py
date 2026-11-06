"""Snapshot paper execution coordination; preserves legacy step and failure order.

Call through auto_paper_portfolio for the existing outer lock and capital policy.
This module depends only on explicit per-call ports; it never imports that engine.
"""
from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

from .ports import SnapshotPorts


def run(
    *, ports: SnapshotPorts, mode: str, initial_cash: float, quantity: int, strategy: str,
    decision_date: str, candidates: list[dict[str, Any]], intake: dict[str, Any],
    dry_run: bool, force_reset: bool, snapshot_root: Path | None, journal_path: Path | None,
) -> dict[str, Any]:
    """Opt-in after-close workflow; legacy matching never enters this path."""

    snapshot_root = Path(snapshot_root) if snapshot_root is not None else ports.paths.project_root / "data/snapshots"
    journal_path = Path(journal_path) if journal_path is not None else ports.paths.state_path.with_suffix(".fills.jsonl")
    ledger_existed = ports.paths.state_path.exists()
    if force_reset and (ledger_existed or journal_path.exists()):
        return ports.trading.snapshot_failure("snapshot_reset_requires_new_ledger_and_journal")
    try:
        # Do not let the legacy store recover a backup behind the journal's back.
        current = json.loads(ports.paths.state_path.read_text(encoding="utf-8")) if ledger_existed else None
        if current is not None and (not isinstance(current, dict) or ports.state.paper_state_shape_errors(current)):
            return ports.trading.snapshot_failure("snapshot_ledger_invalid")
        recovered = ports.journal.recoverable_state(journal_path, current)
        if not recovered["records"] and current and current.get("pending_orders"):
            return ports.trading.snapshot_failure("snapshot_pending_orders_have_no_journal_proof")
        if current and current.get("strategy_fill_mode") == ports.open_fill.FILL_MODE_NEXT_OPEN_SNAPSHOT and not recovered["records"]:
            return ports.trading.snapshot_failure("snapshot_journal_missing_for_active_ledger")
        state = copy.deepcopy(recovered["state"] if recovered["state"] is not None else ports.valuation.reconcile_paper_account(ports.state.empty_state(initial_cash)))
        if any(str(row.get("entry_date") or row.get("buy_date") or "")[:10] > decision_date
               for row in state.get("positions") or []):
            return ports.trading.snapshot_failure("snapshot_position_entry_is_in_future")
        transaction_id = f"update:{decision_date}"
        if recovered["records"] and recovered["records"][-1]["transaction_id"] == transaction_id:
            result = copy.deepcopy(recovered["records"][-1]["result"])
            result.update(idempotent_replay=True, journal_recovery_status=recovered["status"], dry_run=bool(dry_run))
            if not dry_run:
                if recovered["status"].startswith("recovered_"):
                    ports.state.auto_paper_state_store().write(state)
                return ports.publication.publish_snapshot_result(result)
            return result
        if str(state.get("snapshot_decision_date") or "") > decision_date:
            return ports.trading.snapshot_failure("snapshot_decision_date_would_go_backwards")
        recorded_at = ports.clock.utc_now_iso()
        observed = ports.open_fill.aware_timestamp(recorded_at)
        calendar = ports.clock.load_a_share_trading_calendar(ports.paths.project_root / "data/calendar/a_share_trading_calendar.csv")
        next_date = ports.open_fill.next_trading_day(decision_date, calendar)
        lower = ports.clock.datetime.combine(ports.clock.date.fromisoformat(decision_date), ports.clock.time(15), ports.open_fill.SHANGHAI)
        upper = ports.clock.datetime.combine(ports.clock.date.fromisoformat(next_date), ports.clock.time(9, 30), ports.open_fill.SHANGHAI)
        if not lower <= observed < upper:
            return ports.trading.snapshot_failure("snapshot_signal_recording_window_closed")
        if not dry_run and recovered["status"].startswith("recovered_"):
            ports.state.auto_paper_state_store().write(state)
    except (OSError, ValueError, TypeError) as exc:
        return ports.trading.snapshot_failure(f"snapshot_journal_or_clock_invalid:{exc}")

    before = copy.deepcopy(state)
    positions = copy.deepcopy(state.get("positions") or [])
    closed = copy.deepcopy(state.get("closed_positions") or [])
    cash = float(state["cash"])
    events: list[dict[str, Any]] = []
    actions: list[dict[str, Any]] = []
    warnings = [*state.get("warnings", []), *intake.get("reason_codes", []),
                "next_open_snapshot_opt_in_unvalidated", "raw_open_paper_proxy_liquidity_unverified"]
    if state.get("strategy_fill_mode") != ports.open_fill.FILL_MODE_NEXT_OPEN_SNAPSHOT:
        old_header = {key: value for key, value in state.items() if key.startswith("fill_policy")}
        transition = {"from": old_header, "to": ports.info.policy_next_open, "recorded_at": recorded_at,
                      "opening_state_sha256": ports.journal.state_digest(before),
                      "scope": "future_fills_only", "inherited_history_status": "legacy_unverified" if ledger_existed else "empty_book"}
        # An explicit, journaled policy transition; old rows and opening state stay intact.
        state.update(ports.trading.policy_header(ports.info.policy_next_open))
        state["fill_policy_transition"] = transition
        state["fill_policy_scope"] = "future_fills_since_opening_checkpoint"
        events.append({"event_type": "policy_transition", **transition})
        if ledger_existed:
            warnings.append("inherited_strategy_history_not_reconstructed_or_relabelled")
    state["strategy_fill_mode"] = ports.open_fill.FILL_MODE_NEXT_OPEN_SNAPSHOT
    pending = copy.deepcopy(state.get("pending_orders") or [])
    remaining: list[dict[str, Any]] = []
    due = [row for row in pending if str(row.get("fill_date") or "") == decision_date]
    snapshot = ports.open_fill.load_open_snapshot(snapshot_root, decision_date) if due else {}
    basis_gate = ports.open_fill.snapshot_price_gate(snapshot, [row["symbol"] for row in due]) if due else {}
    # Preserve signal order within each side; opening sells release cash before buys.
    for order in sorted(pending, key=lambda item: 0 if item.get("side") == "sell" else 1):
        fill_date = str(order.get("fill_date") or "")
        if fill_date > decision_date:
            remaining.append(order)
            continue
        if fill_date < decision_date:
            evaluated = {"ok": False, "status": "expired", "reason_code": "missed_only_fill_session",
                         "reason": "已错过唯一成交日；不补做历史成交、不顺延。"}
        else:
            evaluated = ports.open_fill.evaluate_open_fill(order, snapshot, decision_date,
                                                      calendar=calendar, observed_at=recorded_at)
            if evaluated["ok"] and not basis_gate.get("eligible"):
                evaluated.update(ok=False, status="rejected", reason_code="snapshot_universe_price_basis_blocked",
                                 reason="请求集合中存在未知或非原始价格；整组不成交。", price=None)
        key = ports.trading.position_key(order)
        position = next((row for row in positions if ports.trading.position_key(row) == key), None)
        requested = ports.trading.safe_float(order.get("quantity"), None)
        error = ""
        if evaluated["ok"]:
            if requested is None or isinstance(order.get("quantity"), bool) or requested != int(requested):
                error = "invalid_order_quantity"
            else:
                error = ports.trading.order_quantity_rule(order["symbol"], int(requested),
                                             held_shares=position.get("quantity") if position and order["side"] == "sell" else None,
                                             name=order.get("name"))
            if not error and order["side"] == "buy" and position:
                error = "already_held_at_open"
            if not error and order["side"] == "sell":
                if not position or int(requested) != ports.trading.safe_int(position.get("quantity"), 0):
                    error = "sell_quantity_does_not_match_holding"
                elif int(requested) > ports.trading.normalize_position_for_t1(position, decision_date)["sellable_quantity"]:
                    error = "t_plus_one_sell_blocked"
            if error:
                evaluated.update(ok=False, status="rejected", reason_code=error, reason=error, price=None)
        amounts: dict[str, Any] = {}
        price = evaluated.get("price") if evaluated["ok"] else None
        if evaluated["ok"]:
            q = int(requested)
            costs = ports.trading.compute_trade_costs(side=order["side"], gross=price * q)
            if order["side"] == "buy":
                flow = ports.trading.buy_cash_outflow(price=price, quantity=q)
                if flow["cash_out"] > cash:
                    evaluated.update(ok=False, status="rejected", reason_code="insufficient_open_cash",
                                     reason="开盘价与原费用合计超过可用现金，本单不成交。", price=None)
                    price = None
                else:
                    cash -= flow["cash_out"]
                    position = ports.trading.make_position(order.get("candidate") or order, q, price, decision_date, flow)
                    position.update(entry_fill_policy=ports.info.policy_next_open, entry_fill_mode=ports.open_fill.FILL_MODE_NEXT_OPEN_SNAPSHOT,
                                    entry_fill_evidence=copy.deepcopy(evaluated["evidence"]),
                                    entry_order_id=order["order_id"], zero_pnl_explanation="")
                    positions.append(position)
                    action = ports.trading.buy_action(order, q, price, decision_date, cash)
                    amounts = {**costs, "gross": flow["gross"], "cash_delta": -flow["cash_out"]}
            else:
                sold = ports.trading.close_position(position, price, decision_date, order.get("reason") or "次日开盘纸面退出")
                sold.update(exit_fill_policy=ports.info.policy_next_open, exit_fill_mode=ports.open_fill.FILL_MODE_NEXT_OPEN_SNAPSHOT,
                            exit_fill_evidence=copy.deepcopy(evaluated["evidence"]), exit_order_id=order["order_id"])
                unit_cost = ports.trading.safe_float(position.get("avg_cost") or position.get("entry_price"), price) or price
                flow = ports.trading.sell_cash_inflow(price=price, quantity=q, avg_cost=unit_cost)
                cash += flow["cash_in"]
                closed.append(sold)
                positions.remove(position)
                action = ports.trading.sell_action(position, price, decision_date, order.get("reason") or "次日开盘纸面退出")
                amounts = {**costs, "gross": flow["gross"], "cash_delta": flow["cash_in"]}
            if evaluated["ok"]:
                action.update(order_id=order["order_id"], signal_date=order["signal_date"],
                              fill_date=decision_date, fill_model=ports.open_fill.FILL_MODE_NEXT_OPEN_SNAPSHOT,
                              fill_policy=ports.info.policy_next_open, fill_evidence=copy.deepcopy(evaluated["evidence"]),
                              total_fee=costs["total_fee"], fill_quality="raw_open_paper_proxy", liquidity_verified=False)
                actions.append(action)
        if not evaluated["ok"]:
            action = ports.trading.block_action(order, str(evaluated.get("reason") or evaluated["reason_code"]), decision_date)
            action.update(order_id=order["order_id"], status=evaluated["status"], reason_code=evaluated["reason_code"])
            actions.append(action)
            warnings.append(f"{order['order_id']}:{evaluated['reason_code']}")
        events.append(ports.open_fill.fill_journal_row(order, status=evaluated["status"], decision_date=decision_date,
                                                 reason_code=evaluated.get("reason_code", ""), reason=evaluated.get("reason", ""),
                                                 price=price, quantity=int(requested) if evaluated["ok"] else 0,
                                                 evidence=evaluated.get("evidence"), limits=evaluated.get("limits"),
                                                 amounts=amounts, recorded_at=recorded_at))

    symbols = [ports.trading.normalize_symbol(row.get("symbol")) for row in [*positions, *candidates]]
    quotes = ports.valuation.snapshot_daily_quotes(symbols, decision_date)
    factors = ports.valuation.load_auto_adjustment_factors(symbols)
    candidate_keys = {ports.trading.position_key(row) for row in candidates}
    pending_keys = {ports.trading.position_key(row) for row in remaining}
    calendar_evidence = {"is_open_by_date": {key: value for key, value in calendar["is_open_by_date"].items()
                                            if decision_date <= key <= next_date}}

    def queue(side: str, row: dict[str, Any], q: int, price: float, reason: str) -> None:
        order = ports.open_fill.make_pending_order(side, row, signal_date=decision_date, fill_date=next_date,
                                              quantity=q, reason=reason, reference_price=price,
                                              candidate=row if side == "buy" else None, recorded_at=recorded_at)
        order["session_calendar_evidence"] = copy.deepcopy(calendar_evidence)
        order["signal_evidence"] = copy.deepcopy(quotes[ports.trading.normalize_symbol(row["symbol"])])
        remaining.append(order)
        pending_keys.add(ports.trading.position_key(order))
        events.append({"event_type": "order_submitted", "order": copy.deepcopy(order)})
        actions.append({**ports.trading.block_action(row, reason, decision_date), "action": f"pending_{side}",
                        "action_label": "待下一交易日开盘核验", "quantity": q,
                        "order_id": order["order_id"], "fill_date": next_date, "price": None,
                        "signal_reference_price": price, "status": "pending"})

    marked = []
    valuation_valid = True
    for position in positions:
        quote = quotes[ports.trading.position_key(position)[0]]
        warnings.extend(quote["warnings"])
        if not quote["valid"]:
            valuation_valid = False
            marked.append(position)
            actions.append(ports.trading.block_action(position, "当日原始价格缺失或未核验，保留旧估值；不生成卖出意向。", decision_date))
            continue
        price = float(quote["price"])
        reason = "仍在正式候选，继续纸面观察。" if ports.trading.position_key(position) in candidate_keys else "未进入正式候选，保留退出观察。"
        marked_position = ports.valuation.mark_position(copy.deepcopy(position), price, decision_date, reason,
                                         adjustment_factors=factors, quote=quote)
        marked_position["zero_pnl_explanation"] = ""
        marked.append(marked_position)
        events.append({"event_type": "valuation", "symbol": position["symbol"],
                       "strategy_type": position["strategy_type"], "quote": quote})
        entry = ports.trading.safe_float(position.get("entry_price"), None)
        if entry and price <= entry * 0.92 and ports.trading.position_key(position) not in pending_keys:
            queue("sell", position, int(position["quantity"]), price,
                  "沿用收盘回撤超过 8% 的复核规则；只记录下一交易日开盘卖出意向。")
        else:
            actions.append(ports.trading.hold_action(marked_position, price, decision_date, reason))
    positions = marked
    held_keys = {ports.trading.position_key(row) for row in positions}
    for candidate in candidates:
        key = ports.trading.position_key(candidate)
        if key in held_keys or key in pending_keys:
            continue
        quote = quotes[key[0]]
        warnings.extend(quote["warnings"])
        rule = ports.trading.order_quantity_rule(candidate["symbol"], quantity, name=candidate.get("name"))
        reference = ports.trading.candidate_price(candidate)
        if (not quote["valid"] or rule or reference is None
                or abs(reference - float(quote["price"] or 0)) > 0.0001):
            actions.append(ports.trading.block_action(candidate, rule or "正式候选参考价与当日原始价格证据未对齐，不记录待成交意向。", decision_date))
            continue
        queue("buy", candidate, quantity, float(quote["price"]),
              "正式候选只生成待成交意向；现金与涨跌停于下一交易日真实开盘证据到达后检查。")

    state.update(cash=round(cash, 2), positions=positions, closed_positions=closed, pending_orders=remaining,
                 updated_at=recorded_at, snapshot_decision_date=decision_date, valuation_date=decision_date,
                 portfolio_review_valid=valuation_valid, actions=actions, daily_actions=actions,
                 warnings=list(dict.fromkeys(warnings)))
    state = ports.valuation.reconcile_paper_account(state)
    summary = ports.valuation.with_daily_pnl(ports.valuation.summary(state, actions), valuation_date=decision_date, previous_state=before,
                              valuation_valid=valuation_valid, calendar=calendar)
    state["daily_pnl_reference"] = ports.valuation.daily_pnl_reference(summary, decision_date)
    if any(event.get("status") == "filled" for event in events):
        state["last_trade_date"] = decision_date
    result = {"ok": True, "exists": True, "mode": mode, "strategy": strategy, "generated_at": recorded_at,
              "valuation_date": decision_date, "strategy_fill_mode": ports.open_fill.FILL_MODE_NEXT_OPEN_SNAPSHOT,
              "strategy_journal_transaction": transaction_id, "fill_journal_path": str(journal_path),
              "fill_policy": ports.info.policy_next_open, "fill_policy_scope": state["fill_policy_scope"],
              "validation_status": "研究输出·未验证", "summary": summary, "positions": positions,
              "closed_positions": closed, "realized_reductions": state.get("realized_reductions", []),
              "legacy_realized_adjustment": state.get("legacy_realized_adjustment", 0),
              **ports.valuation.paper_correction_fields(state),
              "pending_orders": remaining, "actions": actions, "daily_actions": actions,
              "decision_audit": ports.review.build_decision_audit(actions), "warnings": state["warnings"],
              "portfolio_review_valid": valuation_valid, "source": "formal_candidate_daily_batch",
              "input_source": str(intake.get("input_source") or ""), "auto_candidate_count": len(candidates),
              "candidate_intake": {key: value for key, value in intake.items() if key != "batch"},
              "snapshot_outcomes": [event for event in events if event["event_type"] == "order_outcome"],
              "dry_run": bool(dry_run), "journal_recovery_status": recovered["status"],
              "disclaimer": ports.info.disclaimer, "broker_boundary": ports.info.broker_boundary, **ports.trading.paper_action_boundary()}
    # Even a preview must satisfy the same economic checks; it simply writes nothing.
    try:
        ports.journal.validate_transaction(before, state, events)
        if not dry_run:
            committed = ports.journal.commit_transaction(journal_path, before=before, after=state, events=events,
                                                         transaction_id=transaction_id, recorded_at=recorded_at,
                                                         result=result, ledger_existed=ledger_existed)
            ports.state.auto_paper_state_store().write(committed["state"])
            result = ports.publication.publish_snapshot_result(result)
    except (OSError, ValueError, TypeError) as exc:
        failure = ports.trading.snapshot_failure(f"snapshot_transaction_incomplete:{exc}")
        failure["journal_may_require_recovery"] = not dry_run
        return failure
    return result
