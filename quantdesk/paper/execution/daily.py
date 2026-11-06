"""Daily paper execution coordination; preserves legacy step and failure order.

Call through auto_paper_portfolio for the existing outer lock and capital policy.
This module depends only on explicit per-call ports; it never imports that engine.
"""
from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

from .ports import DailyPorts


def run(
    *, ports: DailyPorts,
    mode: str = "init",
    initial_cash: float = 10_000.0,
    quantity_per_stock: int = 100,
    strategy: str = "all",
    enable_intraday_swing: bool = False,
    intraday_source: str = "1m",
    allow_daily_approx: bool = False,
    dry_run: bool = False,
    force_reset: bool = False,
    auto_strategy_path: Path | None = None,
    build_conditional_plan: bool = False,
    simulate_conditional_plan: bool = False,
    plan_date: str = "",
    trade_date: str = "",
    lookback_days: int = 250,
    allow_same_day_rerun: bool = False,
    auto_strategy_payload: dict[str, Any] | None = None,
    risk_gate_shadow_enabled: bool = True,
    risk_gate_enforced: bool = False,
    due_plan_payload: dict[str, Any] | None = None,
    formal_candidate_batch: dict[str, Any] | None = None,
    formal_candidate_batch_path: Path | str | None = None,
    formal_candidate_status: str = "",
    publish_next_plan: bool = True,
    review_target_date: str = "",
    candidate_mode: str = "formal",
    strategy_fill_mode: str | None = None,
    snapshot_root: Path | None = None,
    fill_journal_path: Path | None = None,
) -> dict[str, Any]:
    """Initialize or update the persistent automatic paper portfolio."""

    fill_mode, fill_mode_warnings = ports.open_fill.resolve_strategy_fill_mode(strategy_fill_mode)
    if fill_mode == ports.open_fill.FILL_MODE_NEXT_OPEN_SNAPSHOT and (
        enable_intraday_swing or build_conditional_plan or simulate_conditional_plan or risk_gate_enforced
    ):
        return ports.trading.snapshot_failure("snapshot_mode_incompatible_with_conditional_or_enforced_risk_path")
    ports.risk.unrecorded.clear()  # one run's unrecorded shadow failures, not a previous run's
    mode = "update" if str(mode) == "update" else "init"
    quantity = ports.trading.round_down_to_lot(quantity_per_stock)
    if candidate_mode == "legacy_diagnostic":
        diagnostic = ports.plans.build_conditional_trade_plan(
            auto_strategy_payload=auto_strategy_payload, candidate_mode=candidate_mode,
            strategy=strategy, quantity_per_stock=quantity_per_stock,
            plan_date=plan_date, trade_date=trade_date, lookback_days=lookback_days,
            write=False, auto_strategy_path=auto_strategy_path or ports.paths.strategy_path,
            state_path=ports.paths.state_path, daily_dir=ports.paths.daily_dir,
            daily_feature_path=ports.paths.daily_features_path, low_price_universe_path=None,
            project_root=ports.paths.archive_project_root(),
        )
        diagnostic.update({"mode": "diagnostic", "dry_run": True, "input_source": "legacy_diagnostic", "ledger_write": False})
        return diagnostic
    if candidate_mode != "formal":
        raise ValueError("candidate_mode must be formal or legacy_diagnostic")
    supplied = auto_strategy_payload if isinstance(auto_strategy_payload, dict) else {}
    formal_candidate_batch = formal_candidate_batch if formal_candidate_batch is not None else supplied.get("formal_candidate_batch")
    formal_candidate_batch_path = formal_candidate_batch_path or supplied.get("formal_candidate_batch_path")
    formal_candidate_status = formal_candidate_status or str(supplied.get("formal_candidate_status") or "")
    candidate_target = str(plan_date or review_target_date or (formal_candidate_batch or {}).get("target_date") or ports.candidates.infer_target_date()[0])[:10]
    intake = ports.candidates.load_formal_candidate_intake(
        ports.paths.archive_project_root(), target_date=candidate_target, batch=formal_candidate_batch,
        batch_path=formal_candidate_batch_path, status=formal_candidate_status,
    )
    if supplied and "formal_candidate_batch" not in supplied:
        intake["reason_codes"] = [*intake.get("reason_codes", []), "legacy_candidate_payload_ignored"]
    auto_payload = ports.candidates.formal_candidate_strategy_payload(intake)
    auto_strategy_input_source = str(intake["input_source"])
    formal_candidate_batch_path = intake.get("path") or None
    formal_candidate_status = str(intake.get("status") or "blocked")
    review_target_date = review_target_date or candidate_target
    plan_date = plan_date or candidate_target
    candidates = ports.candidates.candidate_rows_from_strategy_payload(auto_payload, strategy=strategy)
    decision_date = str(auto_payload.get("generated_at") or ports.clock.now_text())[:10]
    candidate_source_for_risk = (
        formal_candidate_batch_path
        or auto_strategy_path
        or ports.paths.strategy_path
    )

    def _risk_candidate(row: dict[str, Any]) -> dict[str, Any]:
        envelope = row.get("candidate_envelope") if isinstance(row, dict) else None
        original = envelope.get("candidate") if isinstance(envelope, dict) else None
        return original if isinstance(original, dict) else row
    if mode == "init" and ports.paths.state_path.exists() and not force_reset and not dry_run:
        return {
            "ok": False,
            "exists": True,
            "error": "portfolio_exists",
            "message": "自动虚拟组合已存在。初始化会重置当前组合并丢失连续复盘历史。请改用‘更新今日虚拟组合’。如确需重置，请使用 --force-reset。",
            "state_path": str(ports.paths.state_path),
            "disclaimer": ports.info.disclaimer,
            "broker_boundary": ports.info.broker_boundary,
        }
    if mode == "init" and ports.state.strategy_book_existed() and not ports.paths.state_path.exists() and not force_reset:
        return ports.state.invalid_paper_account_payload(ports.state.load_state(initial_cash))
    loaded_update_state: dict[str, Any] | None = None
    if fill_mode == ports.open_fill.FILL_MODE_NEXT_OPEN_SNAPSHOT:
        return ports.dispatch.snapshot(
            mode=mode, initial_cash=initial_cash, quantity=quantity, strategy=strategy,
            decision_date=candidate_target, candidates=candidates, intake=intake,
            dry_run=dry_run, force_reset=force_reset, snapshot_root=snapshot_root, journal_path=fill_journal_path,
        )
    if mode == "update":
        loaded_update_state = ports.state.load_state(initial_cash)
        if loaded_update_state.get("strategy_fill_mode") == ports.open_fill.FILL_MODE_NEXT_OPEN_SNAPSHOT:
            return ports.trading.snapshot_failure("snapshot_ledger_requires_explicit_snapshot_mode")
        reconciliation = loaded_update_state.get("accounting_reconciliation")
        if (
            isinstance(reconciliation, dict)
            and str(reconciliation.get("status") or "").lower() == "invalid"
        ):
            return ports.state.invalid_paper_account_payload(loaded_update_state)
    if build_conditional_plan:
        state_for_plan = loaded_update_state or ports.state.load_state(initial_cash)
        payload = ports.plans.build_conditional_trade_plan(
            auto_strategy_payload=auto_payload,
            state=state_for_plan,
            strategy=strategy,
            quantity_per_stock=quantity_per_stock,
            plan_date=plan_date,
            trade_date=trade_date,
            lookback_days=lookback_days,
            write=not dry_run,
            output_path=ports.paths.conditional_plan_path(),
            auto_strategy_path=Path(
                (formal_candidate_batch_path if formal_candidate_status == "passed" else None)
                or auto_strategy_path
                or ports.paths.strategy_path
            ),
            state_path=ports.paths.state_path,
            daily_dir=ports.paths.daily_dir,
            daily_feature_path=ports.paths.daily_features_path,
            low_price_universe_path=None,
            project_root=ports.paths.archive_project_root(),
        )
        payload.update({
            "task_type": "conditional_position_management",
            "scope": "formal_candidates_and_positions",
            "network": False,
            "full_scan": False,
            "user_triggered": True,
            "dry_run": bool(dry_run),
            "input_source": auto_strategy_input_source,
            "candidate_intake": {key: value for key, value in intake.items() if key != "batch"},
        })
        return payload
    if simulate_conditional_plan:
        plan_payload = ports.plans.conditional_trade_plan_latest(ports.paths.conditional_plan_path())
        payload = ports.plans.simulate_conditional_plan_file(
            plan_payload,
            intraday_dir=ports.paths.intraday_dir,
            output_path=ports.paths.conditional_execution_path(),
            intraday_source=intraday_source,
            allow_daily_approx=allow_daily_approx,
            trigger_model="minute_close",
            write=not dry_run,
        )
        payload.update({
            "task_type": "conditional_position_management",
            "scope": "low_price_included_only",
            "network": False,
            "full_scan": False,
            "user_triggered": True,
            "dry_run": bool(dry_run),
        })
        return payload
    if mode == "update" and enable_intraday_swing:
        return ports.dispatch.conditional(
            initial_cash=initial_cash,
            quantity_per_stock=quantity_per_stock,
            strategy=strategy,
            intraday_source=intraday_source,
            allow_daily_approx=allow_daily_approx,
            dry_run=dry_run,
            auto_strategy_path=auto_strategy_path,
            auto_strategy_payload=auto_payload,
            plan_date=plan_date,
            trade_date=trade_date,
            lookback_days=lookback_days,
            allow_same_day_rerun=allow_same_day_rerun,
            risk_gate_shadow_enabled=risk_gate_shadow_enabled,
            risk_gate_enforced=risk_gate_enforced,
            due_plan_payload=due_plan_payload,
            formal_candidate_batch_path=formal_candidate_batch_path,
            formal_candidate_status=formal_candidate_status,
            publish_next_plan=publish_next_plan,
            review_target_date=review_target_date,
        )
    state = (
        ports.state.empty_state(initial_cash)
        if mode == "init"
        else loaded_update_state or ports.state.load_state(initial_cash)
    )
    previous_valuation_state = copy.deepcopy(state)
    delist_liquidation = {
        "ok": True,
        "liquidated": 0,
        "coverage": "not_checked",
        "warnings": [],
        "actions": [],
    }
    if mode == "update":
        liquidation_payload = ports.trading.apply_delisting_liquidation(state, as_of=decision_date)
        if isinstance(liquidation_payload, dict):
            state = liquidation_payload.get("state", state)
            delist_liquidation = {key: value for key, value in liquidation_payload.items() if key != "state"}
    positions = list(state.get("positions") or [])
    closed_positions = list(state.get("closed_positions") or [])
    previous_positions = [dict(row) for row in positions]
    previous_actions = list(state.get("daily_actions") or state.get("actions") or [])
    actions: list[dict[str, Any]] = []
    actions.extend(delist_liquidation.get("actions") or [])
    warnings: list[str] = [str(item) for item in state.get("warnings") or []] + list(intake.get("reason_codes") or [])
    warnings.extend(fill_mode_warnings)
    cash_value = ports.trading.safe_float(state.get("cash"), None)
    if cash_value is None:
        cash = 0.0
        warnings.append("funded_ledger_cash_invalid: paper cash was set to zero instead of initial-capital fallback")
    else:
        cash = float(cash_value)
    candidate_by_key = {ports.trading.position_key(row): row for row in candidates}
    existing_keys = {ports.trading.position_key(row) for row in positions}
    swing_plans: list[dict[str, Any]] = []
    intraday_executions: list[dict[str, Any]] = []
    adjustment_factors = ports.valuation.load_auto_adjustment_factors([row.get("symbol") for row in list(positions) + list(candidates) if isinstance(row, dict)])
    risk_gate_shadow_records: list[dict[str, Any]] = []
    shadow_ledger_event_ordinal = 0

    def bind_shadow_pretrade_ledger(
        shadow_state: dict[str, Any],
        *,
        surface: str,
        symbol: Any,
    ) -> Path:
        nonlocal shadow_ledger_event_ordinal
        if dry_run:
            return ports.paths.state_path
        shadow_ledger_event_ordinal += 1
        return ports.risk.write_shadow_ledger_snapshot_once(
            ports.paths.results_dir
            / "risk_gate_shadow_ledger_inputs"
            / decision_date
            / surface,
            shadow_state,
            event_ordinal=shadow_ledger_event_ordinal,
            symbol=ports.trading.normalize_symbol(symbol),
            base_ledger_source_path=ports.paths.state_path,
            execution_source_path=None,
        )

    def record_legacy_shadow_block(candidate: dict[str, Any], reason: str, surface: str) -> None:
        if not risk_gate_shadow_enabled:
            return
        try:
            risk_gate_shadow_records.append(
                ports.risk.build_legacy_blocked_shadow_record(
                    candidate=candidate,
                    as_of=decision_date,
                    reason=reason,
                    surface=surface,
                    risk_gate_enforced=risk_gate_enforced,
                )
            )
        except Exception as exc:
            ports.risk.record_shadow_failure(
                risk_gate_shadow_records,
                surface=f"{surface}_legacy_block_record",
                error=exc,
                as_of=decision_date,
                write_error=not dry_run,
            )

    if mode == "update":
        updated_positions: list[dict[str, Any]] = []

        def record_daily_exit(position: dict[str, Any], price: float, reason: str,
                              quote: dict[str, Any], extra: dict[str, Any] | None = None) -> None:
            nonlocal cash
            quantity_error = ports.trading.order_quantity_rule(position.get("symbol"), position["sellable_quantity"],
                held_shares=position["quantity"], name=position.get("name"))
            if quantity_error:
                blocked_reason = f"可卖股份不符合板块数量规则，保留持仓：{quantity_error}"
                actions.append(ports.trading.block_action(position, blocked_reason, decision_date))
                updated_positions.append(ports.valuation.mark_position(dict(position), price, decision_date, blocked_reason,
                    adjustment_factors=adjustment_factors, quote=quote))
                return
            action, sold, rest = ports.trading.sell_available_position(position, price, decision_date, reason)
            action.update(extra or {})
            actions.append(action)
            cash += sold["net_amount"]
            if rest is None:
                closed_positions.append(sold)
            else:
                state.setdefault("realized_reductions", []).append(sold)
                remainder_reason = "已退出可卖股份；当日新增股份仍受T+1限制，保留纸面持仓。"
                updated_positions.append(ports.valuation.mark_position(rest, price, decision_date, remainder_reason,
                    adjustment_factors=adjustment_factors, quote=quote))

        for position in positions:
            position = ports.trading.normalize_position_for_t1(position, decision_date)
            key = ports.trading.position_key(position)
            candidate = candidate_by_key.get(key)
            if candidate:
                position["sector"] = position.get("sector") or candidate.get("sector") or "未知板块"
                position["primary_theme"] = position.get("primary_theme") or candidate.get("primary_theme") or "未知题材"
            if position["sellable_quantity"] <= 0:
                quote = ports.valuation.latest_daily_quote(position.get("symbol"), decision_date=decision_date)
                warnings.extend(ports.valuation.quote_warnings(position.get("symbol"), quote))
                latest_price = (
                    ports.trading.safe_float(quote.get("price"), None)
                    or ports.trading.safe_float(position.get("latest_price"), None)
                    or ports.trading.safe_float(position.get("entry_price"), 0.0)
                    or 0.0
                )
                reason = "A股 T+1 规则：当前没有已核验可卖股份，保留纸面持仓，不假设卖出成交。"
                updated = ports.valuation.mark_position(
                    dict(position),
                    latest_price,
                    decision_date,
                    reason,
                    adjustment_factors=adjustment_factors,
                    quote=quote,
                )
                action = ports.trading.hold_action(updated, latest_price, decision_date, reason)
                action["t_plus_one_sell_blocked"] = True
                actions.append(action)
                updated_positions.append(updated)
                continue
            if enable_intraday_swing and ports.trading.is_long_strategy(position):
                swing_quantity = position["sellable_quantity"]
                plan, execution = ports.trading.simulate_swing_for_row(
                    {**position, **(candidate or {})},
                    holding=True,
                    quantity=swing_quantity,
                    intraday_source=intraday_source,
                )
                swing_plans.append(plan)
                intraday_executions.append(execution)
                swing_action = ports.trading.swing_action_from_execution(
                    execution=execution,
                    row=position,
                    decision_date=decision_date,
                    fallback_action="sell_plan",
                )
                if execution.get("triggered") and execution.get("action") == "sell":
                    latest_price = ports.trading.safe_float(execution.get("trigger_price"), None) or ports.trading.safe_float(position.get("latest_price"), 0.0) or 0.0
                    reason = str(execution.get("reason") or "盘中长线波段计划触发模拟卖出")
                    record_daily_exit(position, latest_price, reason,
                        ports.valuation.latest_daily_quote(position.get("symbol"), decision_date=decision_date), {
                        "trigger_time": execution.get("trigger_time"),
                        "trigger_price": execution.get("trigger_price"),
                        "trigger_type": execution.get("trigger_type"),
                        "is_reliable": bool(execution.get("is_reliable")),
                        "execution_source": "intraday_swing",
                        "fill_model": str(execution.get("fill_model") or "daily_approx"),
                        "fill_model_provenance_complete": True,
                    })
                    continue
                actions.append(swing_action)
            quote = ports.valuation.latest_daily_quote(position.get("symbol"), decision_date=decision_date)
            warnings.extend(ports.valuation.quote_warnings(position.get("symbol"), quote))
            latest_price = (
                ports.trading.safe_float(quote.get("price"), None)
                or ports.trading.candidate_price(candidate or {})
                or ports.trading.safe_float(position.get("latest_price"), None)
                or ports.trading.safe_float(position.get("entry_price"), 0.0)
                or 0.0
            )
            entry_price = ports.trading.safe_float(position.get("entry_price"), latest_price) or latest_price
            if latest_price <= 0:
                reason = "本地日线缺失或最新价无效，保留旧持仓并阻断今日估值更新。"
                actions.append(ports.trading.block_action(position, reason, decision_date))
                updated_positions.append(ports.valuation.mark_position(dict(position), latest_price, decision_date, reason,
                    adjustment_factors=adjustment_factors, quote=quote))
                continue
            entry_date = str(position.get("entry_date") or "")[:10]
            if entry_date and entry_date >= decision_date and latest_price > 0 and entry_price > 0 and latest_price <= entry_price * 0.92:
                reason = "A股 T+1 规则：纸面持仓为当日新建，风险复核不假设同日卖出成交。"
                updated = ports.valuation.mark_position(
                    dict(position),
                    latest_price,
                    decision_date,
                    reason,
                    adjustment_factors=adjustment_factors,
                    quote=quote,
                )
                action = ports.trading.hold_action(updated, latest_price, decision_date, reason)
                action["t_plus_one_sell_blocked"] = True
                actions.append(action)
                updated_positions.append(updated)
                continue
            if latest_price > 0 and entry_price > 0 and latest_price <= entry_price * 0.92:
                # An exit is a real fill: it needs today's tradeable bar. A
                # suspended / missing / limit-down / untrusted-basis day must
                # not be booked off the carried old mark.
                rejection = ports.trading.fill_tradability_rejection(
                    quote, "sell", require_quote=True, fill_price=latest_price,
                    symbol=ports.trading.normalize_symbol(position.get("symbol")), name=position.get("name"),
                )
                if rejection:
                    actions.append(ports.trading.block_action(position, rejection, decision_date))
                    updated_positions.append(ports.valuation.mark_position(dict(position), latest_price, decision_date, rejection,
                        adjustment_factors=adjustment_factors, quote=quote))
                    continue
                reason = "触发自动虚拟组合风险复核：最新价较模拟买入价回撤超过 8%，执行纸面退出。"
                record_daily_exit(position, latest_price, reason, quote)
                continue
            reason = "仍在自动策略沙盒候选中，已持仓不重复买入，继续纸面持仓。" if candidate else "未进入今日自动候选，进入退出观察，不直接删除持仓。"
            updated = ports.valuation.mark_position(
                dict(position),
                latest_price,
                decision_date,
                reason,
                adjustment_factors=adjustment_factors,
                quote=quote,
            )
            if candidate:
                action = ports.trading.hold_action(updated, latest_price, decision_date, reason)
                action["already_held"] = True
                actions.append(action)
            else:
                updated["watch_status"] = "watch_exit"
                actions.append(ports.trading.watch_exit_action(updated, latest_price, decision_date, reason))
            updated_positions.append(updated)
        positions = updated_positions
        existing_keys = {ports.trading.position_key(row) for row in positions}

    for candidate in candidates:
        key = ports.trading.position_key(candidate)
        if key in existing_keys:
            continue
        # Structural check first: a quantity below the board's lot can never
        # fill, whatever the price evidence says (also covers the swing path,
        # whose own check only knew the 100-share main-board lot).
        early_lot = ports.trading.lot_size_for_symbol(candidate.get("symbol"), name=candidate.get("name"))
        if quantity < early_lot:
            reason = f"默认数量 {quantity} 股不足该股票板块整手（{early_lot} 股），未生成纸面持仓。"
            actions.append(ports.trading.block_action(candidate, reason, decision_date))
            record_legacy_shadow_block(candidate, reason, "auto_paper_daily_buy")
            continue
        # The strategy book requires raw execution evidence, including when a
        # research signal was produced with a known adjusted basis.
        basis_rejection = ports.trading.basis_fill_rejection(candidate.get("symbol"))
        if basis_rejection:
            actions.append(ports.trading.block_action(candidate, basis_rejection, decision_date))
            record_legacy_shadow_block(candidate, basis_rejection, "auto_paper_daily_buy")
            continue
        # A new book entry is a real fill too: a zero-volume suspension or a
        # one-word limit-up board cannot be bought at the open.
        buy_quote = ports.valuation.latest_daily_quote(candidate.get("symbol"), decision_date=decision_date)
        warnings.extend(ports.valuation.quote_warnings(candidate.get("symbol"), buy_quote))
        buy_rejection = ports.trading.fill_tradability_rejection(
            buy_quote, "buy", require_quote=True, fill_price=buy_quote.get("price"),
            symbol=ports.trading.normalize_symbol(candidate.get("symbol")), name=candidate.get("name"),
        )
        if buy_rejection:
            actions.append(ports.trading.block_action(candidate, buy_rejection, decision_date))
            record_legacy_shadow_block(candidate, buy_rejection, "auto_paper_daily_buy")
            continue
        if enable_intraday_swing and mode == "update" and ports.trading.is_long_strategy(candidate):
            plan, execution = ports.trading.simulate_swing_for_row(
                candidate,
                holding=False,
                quantity=quantity,
                intraday_source=intraday_source,
            )
            swing_plans.append(plan)
            intraday_executions.append(execution)
            if not execution.get("triggered") or execution.get("action") != "buy":
                actions.append(ports.trading.swing_action_from_execution(
                    execution=execution,
                    row=candidate,
                    decision_date=decision_date,
                    fallback_action="buy_plan",
                ))
                continue
            price = ports.trading.safe_float(execution.get("trigger_price"), None)
            if price is None or price <= 0:
                reason = "盘中计划触发但缺少有效触发价格，未生成模拟买入。"
                actions.append(ports.trading.block_action(candidate, reason, decision_date))
                record_legacy_shadow_block(candidate, reason, "auto_paper_intraday_swing_buy")
                continue
            if quantity < 100:
                reason = "不足 100 股整数手，未生成模拟买入。"
                actions.append(ports.trading.block_action(candidate, reason, decision_date))
                record_legacy_shadow_block(candidate, reason, "auto_paper_intraday_swing_buy")
                continue
            cost_info = ports.trading.buy_cash_outflow(price=price, quantity=quantity)
            cash_out = ports.trading.safe_float(cost_info.get("cash_out"), price * quantity) or price * quantity
            if cash_out > cash:
                reason = "现金不足以按盘中触发价模拟买入 100 股。"
                actions.append(ports.trading.block_action(candidate, reason, decision_date))
                record_legacy_shadow_block(candidate, reason, "auto_paper_intraday_swing_buy")
                continue
            if risk_gate_shadow_enabled:
                try:
                    shadow_state = {
                        **state,
                        "cash": cash,
                        "positions": copy.deepcopy(positions),
                    }
                    shadow_ledger_source = bind_shadow_pretrade_ledger(
                        shadow_state,
                        surface="auto_paper_intraday_swing_buy",
                        symbol=candidate.get("symbol"),
                    )
                    risk_gate_shadow_records.append(
                        ports.risk.evaluate_shadow_risk_order(
                            state=shadow_state,
                            candidate=_risk_candidate(candidate),
                            side="buy",
                            reference_price=price,
                            requested_quantity=quantity,
                            as_of=decision_date,
                            daily_dir=ports.paths.daily_dir,
                            candidate_source_path=candidate_source_for_risk,
                            ledger_source_path=shadow_ledger_source,
                            sector_rotation_path=ports.paths.sector_rotation_path(),
                            price_source_path=(
                                ports.paths.intraday_dir
                                / str(intraday_source or "1m")
                                / f"{ports.trading.normalize_symbol(candidate.get('symbol'))}.csv"
                            ),
                            price_data_cutoff=str(
                                execution.get("trade_date") or decision_date
                            )[:10],
                            risk_gate_enforced=risk_gate_enforced,
                            surface="auto_paper_intraday_swing_buy",
                        )
                    )
                except Exception as exc:
                    ports.risk.record_shadow_failure(
                        risk_gate_shadow_records,
                        surface="auto_paper_intraday_swing_buy",
                        error=exc,
                        as_of=decision_date,
                        write_error=not dry_run,
                    )
            cash -= cash_out
            position = ports.trading.make_position(candidate, quantity, price, decision_date, cost_info)
            position.update({
                "entry_reason": "长线波段计划在分钟线中触发，按本地纸面交易规则模拟买入 100 股。",
                "trigger_time": execution.get("trigger_time"),
                "trigger_price": execution.get("trigger_price"),
                "trigger_type": execution.get("trigger_type"),
            })
            positions.append(position)
            existing_keys.add(key)
            action = ports.trading.buy_action(candidate, quantity, price, decision_date, cash)
            action.update({
                "gross_amount": round(ports.trading.safe_float(cost_info.get("gross"), price * quantity) or price * quantity, 2),
                "total_fee": round(ports.trading.safe_float(cost_info.get("total_fee"), 0.0) or 0.0, 2),
                "cash_out": round(cash_out, 2),
                "cost_profile": str(cost_info.get("cost_profile") or "legacy"),
                "entry_reason": "长线波段计划在分钟线中触发，按本地纸面交易规则模拟买入 100 股。",
                "trigger_time": execution.get("trigger_time"),
                "trigger_price": execution.get("trigger_price"),
                "trigger_type": execution.get("trigger_type"),
                "is_reliable": bool(execution.get("is_reliable")),
                "execution_source": "intraday_swing",
                "fill_model": str(execution.get("fill_model") or "daily_approx"),
                "fill_model_provenance_complete": True,
            })
            actions.append(action)
            continue
        price = ports.trading.safe_float(buy_quote.get("price"), None)
        if price is None or price <= 0:
            reason = "缺少可用模拟价格，未进入自动虚拟组合。"
            actions.append(ports.trading.block_action(candidate, reason, decision_date))
            record_legacy_shadow_block(candidate, reason, "auto_paper_daily_buy")
            continue
        board_lot = ports.trading.lot_size_for_symbol(candidate.get("symbol"), name=candidate.get("name"))
        if quantity < board_lot:
            reason = f"默认数量 {quantity} 股不足该股票板块整手（{board_lot} 股），未生成纸面持仓。"
            actions.append(ports.trading.block_action(candidate, reason, decision_date))
            record_legacy_shadow_block(candidate, reason, "auto_paper_daily_buy")
            continue
        cost_info = ports.trading.buy_cash_outflow(price=price, quantity=quantity)
        cash_out = ports.trading.safe_float(cost_info.get("cash_out"), price * quantity) or price * quantity
        if cash_out > cash:
            reason = f"现金不足以模拟买入 {quantity} 股，已阻断。"
            actions.append(ports.trading.block_action(candidate, reason, decision_date))
            record_legacy_shadow_block(candidate, reason, "auto_paper_daily_buy")
            continue
        if risk_gate_shadow_enabled:
            try:
                shadow_state = {
                    **state,
                    "cash": cash,
                    "positions": copy.deepcopy(positions),
                }
                shadow_ledger_source = bind_shadow_pretrade_ledger(
                    shadow_state,
                    surface="auto_paper_daily_buy",
                    symbol=candidate.get("symbol"),
                )
                risk_gate_shadow_records.append(
                    ports.risk.evaluate_shadow_risk_order(
                        state=shadow_state,
                        candidate=_risk_candidate(candidate),
                        side="buy",
                        reference_price=price,
                        requested_quantity=quantity,
                        as_of=decision_date,
                        daily_dir=ports.paths.daily_dir,
                        candidate_source_path=candidate_source_for_risk,
                        ledger_source_path=shadow_ledger_source,
                        sector_rotation_path=ports.paths.sector_rotation_path(),
                        risk_gate_enforced=risk_gate_enforced,
                        surface="auto_paper_daily_buy",
                    )
                )
            except Exception as exc:
                ports.risk.record_shadow_failure(
                    risk_gate_shadow_records,
                    surface="auto_paper_daily_buy",
                    error=exc,
                    as_of=decision_date,
                    write_error=not dry_run,
                )
        cash -= cash_out
        position = ports.trading.make_position(candidate, quantity, price, decision_date, cost_info)
        price_evidence = {"execution_price_basis": "raw", "execution_price_date": buy_quote.get("date"),
            "execution_price_source": buy_quote.get("source_file") or buy_quote.get("source"),
            "execution_price_source_sha256": buy_quote.get("source_sha256"),
            "valuation_price_basis": "raw", "valuation_price_date": buy_quote.get("date"),
            "valuation_price_source": buy_quote.get("source_file") or buy_quote.get("source")}
        position.update(price_evidence)
        positions.append(position)
        existing_keys.add(key)
        action = ports.trading.buy_action(candidate, quantity, price, decision_date, cash)
        action.update(price_evidence)
        action.update({
            "gross_amount": round(ports.trading.safe_float(cost_info.get("gross"), price * quantity) or price * quantity, 2),
            "total_fee": round(ports.trading.safe_float(cost_info.get("total_fee"), 0.0) or 0.0, 2),
            "cash_out": round(cash_out, 2),
            "cost_profile": str(cost_info.get("cost_profile") or "legacy"),
        })
        actions.append(action)

    freshness_gate = (
        ports.review.evaluate_paper_review_freshness(state, target_date=review_target_date, daily_dir=ports.paths.daily_dir)
        if review_target_date else ports.review.evaluate_data_freshness()
    )
    decision_review = ports.review.build_decision_review(previous_actions, decision_date, freshness_gate)
    positions = ports.review.enrich_positions_with_theme_map(positions)
    yesterday_review = ports.review.build_yesterday_holding_review(previous_positions, positions, actions, decision_review, freshness_gate)
    warnings = list(dict.fromkeys(str(item) for item in warnings))
    summary_for_state = ports.valuation.summary({**state, "cash": round(cash, 2), "positions": positions, "closed_positions": closed_positions}, actions)
    valuation_valid = bool(freshness_gate.get("can_review_yesterday_positions", True)) and all(
        str(row.get("latest_date") or "")[:10] == decision_date
        and str(row.get("valuation_status") or "normal") == "normal"
        for row in positions
    )
    if mode == "update":
        summary_for_state = ports.valuation.with_daily_pnl(
            summary_for_state, valuation_date=decision_date,
            previous_state=previous_valuation_state,
            valuation_valid=valuation_valid,
        )
    trade_actions = {"buy", "add", "breakout_add", "reduce_half", "take_profit", "sell", "sell_all", "stop_loss"}
    state.update({
        "schema_version": ports.info.api_schema_version,
        "app_version": ports.info.app_version,
        "updated_at": ports.clock.now_text(),
        "valuation_date": decision_date,
        "daily_pnl_reference": ports.valuation.daily_pnl_reference(summary_for_state, decision_date),
        "initial_cash": ports.trading.safe_float(state.get("initial_cash"), initial_cash) or initial_cash,
        "cash": round(cash, 2),
        "total_return_pct": summary_for_state["total_return_pct"],
        "last_trade_date": (max((str(row.get("decision_date") or "")[:10] for row in actions if str(row.get("action") or "") in trade_actions), default="") or state.get("last_trade_date", "")),
        "positions": positions,
        "closed_positions": closed_positions,
        "actions": actions,
        "daily_actions": actions,
        "decision_review": decision_review,
        "yesterday_holding_review": yesterday_review,
        "delist_liquidation": delist_liquidation,
        "data_freshness_gate": freshness_gate,
        "portfolio_review_valid": valuation_valid,
        "warnings": warnings,
        "disclaimer": ports.info.disclaimer,
        "broker_boundary": ports.info.broker_boundary,
    })
    latest = {
        "ok": True,
        "exists": True,
        "mode": mode,
        "strategy": strategy,
        "generated_at": ports.clock.now_text(),
        "quantity_per_stock": quantity,
        "source": "formal_candidate_daily_batch",
        "valuation_date": decision_date,
        "input_source": auto_strategy_input_source,
        "candidate_intake": {key: value for key, value in intake.items() if key != "batch"},
        "summary": summary_for_state,
        "positions": positions,
        "closed_positions": closed_positions,
        "realized_reductions": state.get("realized_reductions") or [],
        "legacy_realized_adjustment": state.get("legacy_realized_adjustment", 0.0),
        **ports.valuation.paper_correction_fields(state),
        "actions": actions,
        "daily_actions": actions,
        "decision_review": decision_review,
        "yesterday_holding_review": yesterday_review,
        "delist_liquidation": delist_liquidation,
        "data_freshness_gate": freshness_gate,
        "portfolio_review_valid": valuation_valid,
        "decision_audit": ports.review.build_decision_audit(actions),
        "auto_strategy_generated_at": auto_payload.get("generated_at"),
        "auto_candidate_count": len(candidates),
        "intraday_swing_enabled": bool(enable_intraday_swing),
        "intraday_source": intraday_source,
        "allow_daily_approx": bool(allow_daily_approx),
        "swing_plan_count": len(swing_plans),
        "intraday_execution_count": len(intraday_executions),
        "warnings": warnings,
        "swing_plans": swing_plans,
        "intraday_executions": intraday_executions,
        "dry_run": bool(dry_run),
        "disclaimer": ports.info.disclaimer,
        "broker_boundary": ports.info.broker_boundary,
    }
    risk_gate_shadow_summary: dict[str, Any] | None = None
    if risk_gate_shadow_enabled:
        try:
            risk_gate_shadow_summary = ports.risk.summarize_shadow_records(
                risk_gate_shadow_records,
                as_of=decision_date,
                risk_gate_enforced=risk_gate_enforced,
            )
        except Exception as exc:
            ports.risk.record_shadow_failure(
                risk_gate_shadow_records,
                surface="auto_paper_shadow_summary",
                error=exc,
                as_of=decision_date,
                write_error=not dry_run,
            )
    if not dry_run:
        if risk_gate_shadow_summary is not None:
            try:
                ports.risk.write_shadow_summary(
                    risk_gate_shadow_summary,
                    output_path=ports.paths.risk_gate_shadow_path(),
                )
            except Exception as exc:
                ports.risk.record_shadow_failure(
                    risk_gate_shadow_records,
                    surface="auto_paper_shadow_output",
                    error=exc,
                    as_of=decision_date,
                    write_error=not dry_run,
                )
        if enable_intraday_swing:
            swing_artifacts = ports.publication.write_intraday_swing_artifacts(
                swing_plans,
                intraday_executions,
                enabled=enable_intraday_swing,
                source=intraday_source,
                allow_daily_approx=allow_daily_approx,
            )
            latest["swing_trade_plans_path"] = str(ports.paths.swing_plans_path.relative_to(ports.paths.project_root))
            latest["intraday_execution_path"] = str(ports.paths.intraday_execution_path.relative_to(ports.paths.project_root))
            latest["swing_execution_report_path"] = str(swing_artifacts["report_path"].relative_to(ports.paths.project_root))
        ports.state.write_auto_paper_state(state)
        latest = ports.publication.write_auto_paper_latest(latest)
        if mode == "init" and force_reset and ports.paths.history_path.exists():
            ports.paths.history_path.unlink()
        history = ports.publication.append_history(latest)
        report_path = ports.publication.write_report(
            latest,
            risk_gate_shadow_summary=risk_gate_shadow_summary,
        )
        latest["history_path"] = ports.paths.display_path(ports.paths.history_path)
        latest["report_path"] = ports.paths.display_path(report_path)
        latest["history_count"] = len(history.get("items") or [])
        latest = ports.publication.write_auto_paper_latest(latest)
    return latest
