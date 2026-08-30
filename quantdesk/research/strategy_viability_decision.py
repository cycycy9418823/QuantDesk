from __future__ import annotations

from typing import Any


def decide_capital_lot_sensitivity(payload: dict[str, Any]) -> dict[str, Any]:
    rows = payload.get("results") or []
    warnings = list(payload.get("warnings") or [])
    if not rows:
        return {
            "decision": "pipeline_still_blocked",
            "confidence": "low",
            "reasons": ["no_sensitivity_results"],
            "blocking_risks": warnings,
            "next_recommended_version": "v3.7.1-repair-strategy-pipeline",
        }
    by_cash: dict[int, list[dict[str, Any]]] = {}
    for row in rows:
        by_cash.setdefault(int(row.get("initial_cash") or 0), []).append(row)
    best_by_cash: dict[int, dict[str, Any]] = {}
    for cash, group in by_cash.items():
        best_by_cash[cash] = max(group, key=lambda r: float(r.get("sharpe") or -999))
    all_weak = all(
        float(row.get("sharpe") or 0) < 0.6
        or float(row.get("max_drawdown") or 0) < -0.40
        or float(row.get("annualized_return_net") or 0) < 0.08
        for row in best_by_cash.values()
    )
    low_cash = best_by_cash.get(10000, {})
    high_cash_candidates = [best_by_cash.get(50000, {}), best_by_cash.get(100000, {})]
    high_best = max(high_cash_candidates, key=lambda r: float(r.get("invested_ratio_avg") or 0)) if high_cash_candidates else {}
    invested_improved = float(high_best.get("invested_ratio_avg") or 0) >= float(low_cash.get("invested_ratio_avg") or 0) + 0.15
    unfilled_improved = float(high_best.get("unfilled_due_to_lot_size_count") or 0) <= float(low_cash.get("unfilled_due_to_lot_size_count") or 0) * 0.75
    high_quality_combo = any(
        float(row.get("annualized_return_net") or 0) > 0.08 and float(row.get("sharpe") or 0) > 0.6
        for row in rows
    )
    reasons: list[str] = []
    if not payload.get("historical_listings_exists", True):
        return {
            "decision": "pipeline_still_blocked",
            "confidence": "low",
            "reasons": ["historical_listings_missing"],
            "blocking_risks": warnings,
            "next_recommended_version": "v3.7.1-repair-strategy-pipeline",
        }
    if all_weak and not invested_improved:
        decision = "switch_strategy_direction"
        reasons.append("all_cash_levels_remain_weak_and_lot_constraint_not_primary")
    elif all_weak:
        decision = "low_price_direction_rejected"
        reasons.append("best_strategy_still_weak_after_capital_grid")
    elif invested_improved and unfilled_improved:
        decision = "low_price_needs_more_capital"
        reasons.append("larger_capital_improves_deployment_and_reduces_unfilled_lots")
    elif high_quality_combo:
        decision = "low_price_refine_needed"
        reasons.append("some_parameter_combinations_pass_basic_research_thresholds")
    else:
        decision = "low_price_direction_rejected"
        reasons.append("no_stable_research_edge_detected")
    confidence = "medium"
    blocking_risks: list[str] = []
    if payload.get("survivorship_bias_risk") == "high":
        confidence = "medium"
        blocking_risks.append("survivorship_bias_risk_high")
    if not payload.get("benchmark_available", False):
        confidence = "low"
        blocking_risks.append("benchmark_missing")
    if payload.get("pit_quality") == "degraded":
        confidence = "medium" if confidence != "low" else "low"
        blocking_risks.append("degraded_pit_price_csv_coverage")
    if float((payload.get("lot_constraint_summary") or {}).get("avg_invested_ratio_by_cash", {}).get("100000", 1.0) or 0) < 0.70:
        blocking_risks.append("capital_deployment_issue")
    next_map = {
        "low_price_direction_rejected": "v3.7.1-new-strategy-direction-screening",
        "low_price_needs_more_capital": "v3.7.1-low-price-capital-aware-refinement",
        "low_price_refine_needed": "v3.7.1-low-price-factor-refinement",
        "switch_strategy_direction": "v3.7.1-new-strategy-direction-screening",
        "pipeline_still_blocked": "v3.7.1-repair-strategy-pipeline",
    }
    return {
        "decision": decision,
        "confidence": confidence,
        "reasons": reasons,
        "blocking_risks": blocking_risks,
        "next_recommended_version": next_map.get(decision, "v3.7.1-new-strategy-direction-screening"),
    }

