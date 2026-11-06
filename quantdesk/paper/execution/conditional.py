"""Conditional paper execution coordination; preserves legacy step and failure order.

Call through auto_paper_portfolio for the existing outer lock and capital policy.
This module depends only on explicit per-call ports; it never imports that engine.
"""
from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

from .ports import ConditionalPorts


def run(
    *, ports: ConditionalPorts,
    initial_cash: float,
    quantity_per_stock: int,
    strategy: str,
    intraday_source: str,
    allow_daily_approx: bool,
    dry_run: bool,
    auto_strategy_path: Path | None,
    auto_strategy_payload: dict[str, Any] | None,
    plan_date: str,
    trade_date: str,
    lookback_days: int,
    allow_same_day_rerun: bool,
    risk_gate_shadow_enabled: bool,
    risk_gate_enforced: bool,
    due_plan_payload: dict[str, Any] | None = None,
    formal_candidate_batch_path: Path | str | None = None,
    formal_candidate_status: str = "",
    publish_next_plan: bool = True,
    review_target_date: str = "",
) -> dict[str, Any]:
    state = ports.state.load_state(initial_cash)
    candidate_input_source = (
        "formal_candidate_batch"
        if formal_candidate_status == "passed"
        else (
            f"formal_candidate_batch_{formal_candidate_status}"
            if formal_candidate_status
            else (
                "in_memory"
                if isinstance(auto_strategy_payload, dict)
                else "latest_file_fallback"
            )
        )
    )
    previous_positions = [dict(row) for row in state.get("positions") or []]
    previous_actions = list(state.get("daily_actions") or state.get("actions") or [])
    previous_total_equity = ports.valuation.total_equity_from_state(state)
    previous_valuation_state = copy.deepcopy(state)
    freshness_gate = (
        ports.review.evaluate_paper_review_freshness(state, target_date=review_target_date, daily_dir=ports.paths.daily_dir)
        if review_target_date else ports.review.evaluate_data_freshness()
    )
    candidate_intake = dict((auto_strategy_payload or {}).get("candidate_intake") or {})
    if not freshness_gate.get("can_review_yesterday_positions", True):
        decision_date = str(freshness_gate.get("target_complete_trade_date") or ports.clock.now_text())[:10]
        actions: list[dict[str, Any]] = []
        decision_review = ports.review.build_decision_review(previous_actions, decision_date, freshness_gate)
        enriched_positions = ports.review.enrich_positions_with_theme_map(state.get("positions") or [])
        state_for_latest = {**state, "positions": enriched_positions}
        yesterday_review = ports.review.build_yesterday_holding_review(previous_positions, enriched_positions, actions, decision_review, freshness_gate)
        summary = ports.valuation.summary(state_for_latest, actions)
        summary["previous_total_equity"] = round(previous_total_equity, 2)
        summary["daily_pnl"] = None
        summary["daily_return_pct"] = None
        summary["daily_pnl_status"] = "current_valuation_unverified"
        latest = {
            "ok": True,
            "exists": True,
            "mode": "update",
            "strategy": strategy,
            "generated_at": ports.clock.now_text(),
            "quantity_per_stock": ports.trading.round_down_to_lot(quantity_per_stock),
            "source": "conditional_position_management",
            "input_source": candidate_input_source,
            "candidate_intake": candidate_intake,
            "warnings": list(candidate_intake.get("reason_codes") or []),
            "task_type": "conditional_position_management",
            "scope": "low_price_included_only",
            "network": False,
            "full_scan": False,
            "user_triggered": True,
            "skipped": True,
            "review_mode": "stale_observation",
            "message": "暂无有效复盘：本地完整日线尚未更新到目标完整交易日，不生成新的条件交易计划，不更新自动虚拟组合估值。",
            "summary": summary,
            "positions": enriched_positions,
            "closed_positions": state.get("closed_positions") or [],
            # 陈旧观察也要把主账的减仓明细与**显式**历史差额带全：少了这两把键，
            # _write_auto_paper_latest() 的 reconcile 会用“已实现盈亏 − 明细”反推历史
            # 差额，把刚发生的半卖利润写成 legacy、把 realized_reductions 清空，摘要与
            # 对账随之不一致（T24-LATEST-REDUCTIONS-20260929）。
            "realized_reductions": state.get("realized_reductions") or [],
            "legacy_realized_adjustment": state.get("legacy_realized_adjustment", 0.0),
            **ports.valuation.paper_correction_fields(state),
            "actions": actions,
            "daily_actions": actions,
            "decision_review": decision_review,
            "yesterday_holding_review": yesterday_review,
            "decision_audit": ports.review.build_decision_audit(actions),
            "auto_candidate_count": 0,
            "intraday_swing_enabled": True,
            "intraday_source": intraday_source,
            "allow_daily_approx": bool(allow_daily_approx),
            "conditional_plan": {"summary": {"symbol_count": 0}, "path": ports.paths.display_path(ports.paths.conditional_plan_path())},
            "conditional_execution": {"summary": {"event_count": 0, "no_fill_count": 0, "missing_intraday_count": 0}, "path": ports.paths.display_path(ports.paths.conditional_execution_path())},
            "conditional_review": {
                "ok": False,
                "review_valid": False,
                "missing_intraday_count": 0,
                "explanation": "完整日线控制收益复盘，分钟线控制盘中触发复盘。",
                "reason": freshness_gate.get("reason") or "",
            },
            "data_freshness_gate": freshness_gate,
            "portfolio_review_valid": False,
            "conditional_plan_summary": {"symbol_count": 0},
            "conditional_execution_summary": {"event_count": 0, "no_fill_count": 0, "missing_intraday_count": 0},
            "dry_run": bool(dry_run),
            "disclaimer": ports.info.disclaimer,
            "broker_boundary": ports.info.broker_boundary,
        }
        stale_shadow_records: list[dict[str, Any]] = []
        stale_shadow_summary: dict[str, Any] | None = None
        if risk_gate_shadow_enabled:
            try:
                stale_shadow_summary = ports.risk.summarize_shadow_records(
                    stale_shadow_records,
                    as_of=decision_date,
                    risk_gate_enforced=risk_gate_enforced,
                )
                stale_shadow_summary["observation_reason"] = "stale_data_no_traffic"
            except Exception as exc:
                ports.risk.record_shadow_failure(
                    stale_shadow_records,
                    surface="auto_paper_stale_shadow_summary",
                    error=exc,
                    as_of=decision_date,
                    write_error=not dry_run,
                )
        if not dry_run:
            if stale_shadow_summary is not None:
                try:
                    ports.risk.write_shadow_summary(
                        stale_shadow_summary,
                        output_path=ports.paths.risk_gate_shadow_path(),
                    )
                except Exception as exc:
                    ports.risk.record_shadow_failure(
                        stale_shadow_records,
                        surface="auto_paper_stale_shadow_output",
                        error=exc,
                        as_of=decision_date,
                        write_error=True,
                    )
            latest = ports.publication.write_auto_paper_latest(latest)
            report_path = ports.publication.write_report(
                latest,
                risk_gate_shadow_summary=stale_shadow_summary,
            )
            latest["report_path"] = ports.paths.display_path(report_path)
            latest = ports.publication.write_auto_paper_latest(latest)
        return latest
    conditional_plan_path = ports.paths.conditional_plan_path()
    conditional_execution_path = ports.paths.conditional_execution_path()
    # Round-13 ordering fix: ``build_conditional_trade_plan`` always targets the
    # NEXT trading day (plan_date = today, trade_date = tomorrow). Simulating
    # that freshly built plan immediately was the root cause of the historical
    # evaluation_count=0: the update looked for minute evidence on a date that
    # had not happened yet. The update must replay the DUE plan -- the one
    # archived under today's trade date -- and only build and archive the next
    # plan, never simulate it.
    plan_payload = ports.plans.build_conditional_trade_plan(
        auto_strategy_payload=auto_strategy_payload,
        state=state,
        strategy=strategy,
        quantity_per_stock=quantity_per_stock,
        plan_date=plan_date,
        trade_date=trade_date,
        lookback_days=lookback_days,
        write=False,
        output_path=conditional_plan_path,
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
    due_trade_date = str(plan_payload.get("plan_date") or "")[:10]
    archive_root = ports.paths.archive_project_root()
    due_archive_path = (
        archive_root / "results" / "conditional_trade_plans_daily" / f"{due_trade_date}.json"
        if due_trade_date
        else None
    )
    due_loaded: dict[str, Any]
    if due_plan_payload is not None:
        # The caller (daily workflow orchestrator) already loaded the due plan
        # from the archive; trust its verified payload rather than re-reading.
        due_loaded = {
            "status": "passed",
            "exists": True,
            "plan_id": str(due_plan_payload.get("plan_id") or ""),
            "plan": due_plan_payload,
        }
    elif due_trade_date:
        due_loaded = ports.plans.load_archived_conditional_plan(archive_root, trade_date=due_trade_date)
    else:
        due_loaded = {
            "status": "blocked",
            "reason_codes": ["conditional_plan_archive_trade_date_invalid"],
        }
    due_plan_payload = (
        dict(due_loaded["plan"])
        if due_loaded.get("status") == "passed"
        and isinstance(due_loaded.get("plan"), dict)
        else None
    )
    due_source_plan = copy.deepcopy(due_plan_payload)
    replay_input_evidence = None
    due_plan_enrichment: dict[str, Any] = {
        "status": "not_applicable",
        "target_date": "",
        "formal_batch_path": "",
        "exact_bindable": 0,
        "enriched": 0,
        "existing_valid_envelope": 0,
        "rebound_existing_envelope": 0,
        "unchanged_unmatched": 0,
        "rejected_mismatch": 0,
        "rejections": [],
        "warnings": [],
        "reason_codes": [],
    }
    if due_plan_payload is not None:
        try:
            enriched = ports.plans.enrich_legacy_due_plan_candidate_envelopes(
                archive_root,
                due_plan_payload,
            )
        except Exception as exc:  # noqa: BLE001 - compatibility must fail closed
            enriched = {
                **due_plan_enrichment,
                "status": "blocked",
                "reason_codes": [
                    f"legacy_due_plan_enrichment_failed:{type(exc).__name__}:{exc}"
                ],
                "plan": copy.deepcopy(due_plan_payload),
            }
        due_plan_enrichment = {
            key: copy.deepcopy(value)
            for key, value in enriched.items()
            if key != "plan"
        }
        enriched_plan = enriched.get("plan")
        if isinstance(enriched_plan, dict):
            due_plan_payload = enriched_plan
    if due_plan_payload is not None:
        replay_input_evidence = ports.plans.inspect_plan_minute_inputs(archive_root, due_source_plan, intraday_dir=ports.paths.intraday_dir / "1m")
        execution_payload = ports.plans.simulate_conditional_plan_file(
            due_plan_payload,
            intraday_dir=ports.paths.intraday_dir,
            output_path=conditional_execution_path,
            intraday_source=intraday_source,
            allow_daily_approx=allow_daily_approx,
            trigger_model="minute_close",
            write=not dry_run,
        )
    else:
        # No due plan in the archive: nothing is due today, so the honest result
        # is an empty execution (no fake fills), not a simulation of tomorrow.
        execution_payload = {
            "ok": True,
            "skipped": True,
            "status": "no_due_plan",
            "plan_id": str(due_loaded.get("plan_id") or ""),
            "trade_date": due_trade_date,
            "summary": {
                "plan_count": 0,
                "event_count": 0,
                "no_fill_count": 0,
                "blocked_count": 0,
            },
            "events": [],
            "no_fill": [],
            "blocked": [],
            "message": "归档中没有到期计划，未执行盘中模拟。",
            "disclaimer": ports.info.disclaimer,
        }
        # 这个空结果也必须落盘。不写等于让下游无法区分"这一步没跑"和"今天
        # 没有到期计划"——前者是故障，后者是正常，而缺失文件长得像故障。
        if not dry_run:
            ports.publication.atomic_write_json(conditional_execution_path, execution_payload)
    next_plan_publish: dict[str, Any] | None = None
    if not publish_next_plan:
        next_plan_publish = {
            "status": "skipped",
            "published_latest": False,
            "reason_codes": ["next_plan_publish_disabled_by_caller"],
        }
    elif not dry_run and plan_payload.get("ok") is True:
        # The next plan is archived through the archive-aware publisher, which
        # archives the previous latest and the new plan under its trade date
        # before replacing latest; a bare write over latest would lose the
        # append-only record.
        next_plan_publish = ports.publication.publish_archived_conditional_plan(archive_root, plan_payload)
    decision_date = str(
        execution_payload.get("trade_date")
        or plan_payload.get("trade_date")
        or ports.clock.now_text()
    )[:10]
    adjustment_factors = ports.valuation.load_auto_adjustment_factors([row.get("symbol") for row in state.get("positions") or [] if isinstance(row, dict)])
    risk_gate_shadow_records: list[dict[str, Any]] = []
    apply_result = ports.plans.apply_conditional_execution_to_state(
        state,
        execution_payload,
        daily_dir=ports.paths.daily_dir,
        allow_same_day_rerun=allow_same_day_rerun,
        adjustment_factors=adjustment_factors,
        risk_gate_shadow_enabled=risk_gate_shadow_enabled,
        risk_gate_enforced=risk_gate_enforced,
        risk_gate_shadow_records=risk_gate_shadow_records,
        risk_gate_shadow_output_path=ports.paths.risk_gate_shadow_path(),
        risk_gate_shadow_error_path=ports.paths.risk_gate_shadow_error_path(),
        risk_gate_shadow_write_errors=not dry_run,
        sector_rotation_path=ports.paths.sector_rotation_path(),
        ledger_source_path=ports.paths.state_path,
        execution_source_path=conditional_execution_path,
        risk_gate_shadow_ledger_snapshot_dir=(
            ports.paths.results_dir
            / "risk_gate_shadow_ledger_inputs"
            / decision_date
            / str(execution_payload.get("plan_id") or "unknown_plan")
            if not dry_run
            else None
        ),
    )
    if apply_result.get("already_updated_today"):
        latest_state = apply_result.get("state") if isinstance(apply_result.get("state"), dict) else state
        actions = list(latest_state.get("daily_actions") or [])
    else:
        latest_state = apply_result.get("state") if isinstance(apply_result.get("state"), dict) else state
        actions = list(apply_result.get("actions") or [])
    replay_receipt: dict[str, Any] | None = None
    minute_evidence: dict[str, Any] | None = None
    if due_plan_payload is not None and due_archive_path is not None:
        minute_evidence = ports.review.intraday_gap_status(
            target_trade_date=due_trade_date,
            source="plans",
            plan_path=due_archive_path,
            write=False,
        )
        if dry_run:
            replay_receipt = ports.review.build_conditional_replay_receipt(
                execution_payload=execution_payload,
                apply_result=apply_result,
                persisted_state=ports.state.load_state(initial_cash),
                minute_evidence=minute_evidence,
                captured_at=ports.clock.now_text(),
            )
        elif apply_result.get("already_updated_today"):
            replay_receipt = ports.review.build_conditional_replay_receipt(
                execution_payload=execution_payload,
                apply_result=apply_result,
                persisted_state=ports.state.load_state(initial_cash),
                minute_evidence=minute_evidence,
                captured_at=ports.clock.now_text(),
            )
    decision_review = ports.review.build_decision_review(previous_actions, decision_date, freshness_gate)
    latest_state["positions"] = ports.review.enrich_positions_with_theme_map(latest_state.get("positions") or [])
    yesterday_review = ports.review.build_yesterday_holding_review(previous_positions, latest_state.get("positions") or [], actions, decision_review, freshness_gate)
    latest_state["decision_review"] = decision_review
    latest_state["yesterday_holding_review"] = yesterday_review
    latest_state["daily_actions"] = actions
    latest_state["actions"] = actions
    summary = ports.valuation.with_daily_pnl(ports.valuation.summary(latest_state, actions), valuation_date=decision_date,
                              previous_state=previous_valuation_state)
    latest = {
        "ok": True,
        "exists": True,
        "mode": "update",
        "strategy": strategy,
        "generated_at": ports.clock.now_text(),
        "quantity_per_stock": ports.trading.round_down_to_lot(quantity_per_stock),
        "source": "conditional_position_management",
        "valuation_date": decision_date,
        "input_source": candidate_input_source,
        "candidate_intake": candidate_intake,
        "warnings": list(candidate_intake.get("reason_codes") or []),
        "task_type": "conditional_position_management",
        "scope": "low_price_included_only",
        "network": False,
        "full_scan": False,
        "user_triggered": True,
        "already_updated_today": bool(apply_result.get("already_updated_today")),
        "due_plan_id": (
            str(due_plan_payload.get("plan_id") or "")
            if due_plan_payload is not None
            else ""
        ),
        "due_plan_enrichment": due_plan_enrichment,
        "replay_receipt": replay_receipt,
        "next_plan_publish": next_plan_publish,
        "summary": summary,
        "positions": latest_state.get("positions") or [],
        "closed_positions": latest_state.get("closed_positions") or [],
        "realized_reductions": latest_state.get("realized_reductions") or [],
        "legacy_realized_adjustment": latest_state.get("legacy_realized_adjustment", 0.0),
        **ports.valuation.paper_correction_fields(latest_state),
        "actions": actions,
        "daily_actions": actions,
        "decision_review": decision_review,
        "yesterday_holding_review": yesterday_review,
        "decision_audit": ports.review.build_decision_audit(actions),
        "auto_candidate_count": plan_payload.get("summary", {}).get("new_candidate_plan_count", 0),
        "intraday_swing_enabled": True,
        "intraday_source": intraday_source,
        "allow_daily_approx": bool(allow_daily_approx),
        "conditional_plan": {
            "plan_id": plan_payload.get("plan_id"),
            "plan_date": plan_payload.get("plan_date"),
            "trade_date": plan_payload.get("trade_date"),
            "source_data_cutoff": plan_payload.get("source_data_cutoff"),
            "summary": plan_payload.get("summary"),
            "path": ports.paths.display_path(conditional_plan_path),
        },
        "conditional_execution": {
            "plan_id": execution_payload.get("plan_id"),
            "trade_date": execution_payload.get("trade_date"),
            "summary": execution_payload.get("summary"),
            "path": ports.paths.display_path(conditional_execution_path),
        },
        "conditional_review": ports.review.conditional_review_summary(execution_payload, actions),
        "data_freshness_gate": freshness_gate,
        "portfolio_review_valid": bool(freshness_gate.get("can_review_yesterday_positions", True)),
        "conditional_plan_summary": plan_payload.get("summary", {}),
        "conditional_execution_summary": execution_payload.get("summary", {}),
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
    persisted_apply_state: dict[str, Any] | None = None
    if dry_run:
        ports.review.attach_bound_replay_review(
            latest, source_plan=due_source_plan, replay_receipt=replay_receipt,
            input_evidence=replay_input_evidence,
            applied_ids=list(latest_state.get("applied_execution_ids") or []), dry_run=True,
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
        compat_artifacts = ports.publication.write_conditional_compat_artifacts(plan_payload, execution_payload)
        latest["swing_trade_plans_path"] = ports.paths.display_path(ports.paths.swing_plans_path)
        latest["intraday_execution_path"] = ports.paths.display_path(ports.paths.intraday_execution_path)
        latest["swing_execution_report_path"] = ports.paths.display_path(compat_artifacts["report_path"])
        if not apply_result.get("already_updated_today"):
            persisted_apply_state = ports.state.write_auto_paper_state(latest_state)
        if (
            replay_receipt is None
            and due_plan_payload is not None
            and due_archive_path is not None
            and minute_evidence is not None
        ):
            receipt_apply_result = dict(apply_result)
            if persisted_apply_state is not None:
                receipt_apply_result["state"] = persisted_apply_state
            replay_receipt = ports.review.build_conditional_replay_receipt(
                execution_payload=execution_payload,
                apply_result=receipt_apply_result,
                persisted_state=ports.state.auto_paper_state_store().read(default={}),
                minute_evidence=minute_evidence,
                captured_at=ports.clock.now_text(),
            )
            latest["replay_receipt"] = replay_receipt
        ports.review.attach_bound_replay_review(
            latest, source_plan=due_source_plan, replay_receipt=replay_receipt,
            input_evidence=replay_input_evidence,
            applied_ids=list((persisted_apply_state or latest_state).get("applied_execution_ids") or []), dry_run=False,
        )
        if not apply_result.get("already_updated_today"):
            latest = ports.publication.write_auto_paper_latest(latest)
            history = ports.publication.append_history(latest)
            latest["history_path"] = ports.paths.display_path(ports.paths.history_path)
            latest["history_count"] = len(history.get("items") or [])
        else:
            latest["message"] = apply_result.get("message")
        report_path = ports.publication.write_report(
            latest,
            risk_gate_shadow_summary=risk_gate_shadow_summary,
        )
        latest["report_path"] = ports.paths.display_path(report_path)
        latest = ports.publication.write_auto_paper_latest(latest)
    return latest
