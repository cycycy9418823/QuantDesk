from __future__ import annotations

from pathlib import Path
from typing import Any

from quantdesk.candidate_monitoring import (
    ALLOWED_USAGE,
    PROHIBITED_USAGE,
    fmt,
    pct,
    read_json,
    write_json,
    write_text,
)


def _build_watchlist_cards(state: dict[str, Any], registry: dict[str, Any]) -> list[dict[str, Any]]:
    watchlist_cards = []
    for item in state.get("portfolio_watchlist", registry.get("portfolio_watchlist", [])):
        watchlist_cards.append(
            {
                "watchlist_id": item,
                "type": "portfolio_watchlist",
                "why_not_candidate": "not primary after cost/risk review; combo_50_50 remains sole candidate",
                "main_risk": "less favorable robustness or monitoring priority than combo_50_50",
                "continue_research": False,
                "dashboard_monitoring_only": True,
                "watchlist_reason": "portfolio alternative kept for comparison only",
                "main_blocker": "not superior enough to replace combo_50_50",
                "next_action": "dashboard monitoring only",
                "stop_research_reason": "no new candidate upgrade in this phase",
            }
        )
    for item in state.get("strategy_watchlist", registry.get("strategy_watchlist", [])):
        watchlist_cards.append(
            {
                "watchlist_id": item,
                "type": "strategy_watchlist",
                "why_not_candidate": "defensive watchlist only; no portfolio enhancement without Sharpe/ex-2024-2025 deterioration",
                "main_risk": "overlap with low-vol style and cost sensitivity",
                "continue_research": False,
                "dashboard_monitoring_only": True,
                "watchlist_reason": "defensive low-vol family retained for observation",
                "main_blocker": "insufficient candidate-level independent alpha",
                "next_action": "dashboard monitoring only",
                "stop_research_reason": "no liquidity resilience parameter micro-tuning in this phase",
            }
        )
    return watchlist_cards


def build_dashboard_cards(
    *,
    root: Path | None = None,
    registry_path: Path | None = None,
    monitoring_path: Path | None = None,
    state_path: Path | None = None,
) -> dict[str, Any]:
    base = (root or Path(__file__).resolve().parents[1]).resolve()
    registry_path = registry_path or base / "results" / "research_candidate_registry_latest.json"
    monitoring_path = monitoring_path or base / "results" / "portfolio_candidate_monitoring_refresh_latest.json"
    state_path = state_path or base / "results" / "strategy_pool_state_latest.json"
    registry = read_json(registry_path)
    monitoring = read_json(monitoring_path)
    state = read_json(state_path).get("final_state", {})
    data_truth_path = base / "results" / "candidate_data_truth_final_verdict_latest.json"
    gap_path = base / "results" / "data_gap_tracker_latest.json"
    reconciliation_path = base / "results" / "candidate_status_reconciliation_latest.json"
    data_truth = read_json(data_truth_path) if data_truth_path.exists() else {}
    gaps = read_json(gap_path) if gap_path.exists() else {}
    reconciliation = read_json(reconciliation_path) if reconciliation_path.exists() else {}
    candidate_pool = registry.get("candidate_pool", [])
    if not isinstance(candidate_pool, list):
        candidate_pool = []
    watchlist_cards = _build_watchlist_cards(state, registry)
    if not candidate_pool:
        warnings = list(monitoring.get("warnings", []))
        warnings.append("candidate_pool_empty")
        return {
            "task_id": "dashboard_candidate_card_integration",
            "status": "completed_with_warnings",
            "cards": [],
            "watchlist_cards": watchlist_cards,
            "candidate_count": 0,
            "watchlist_count": len(watchlist_cards),
            "warnings": warnings,
            "risk_disclaimer_included": True,
            "research_only": True,
            "prohibited_usage": PROHIBITED_USAGE,
            "files_written": [
                "results/dashboard_candidate_cards_latest.json",
                "reports/dashboard_candidate_cards_latest.md",
                "reports/research_dashboard_candidate_summary_latest.md",
                "results/dashboard_watchlist_cards_latest.json",
                "reports/dashboard_watchlist_cards_latest.md",
            ],
        }
    candidate = candidate_pool[0]
    metrics = monitoring.get("metrics", {})
    monitoring_status = monitoring.get("monitoring_status", candidate.get("monitoring_status"))
    warning_count = len(monitoring.get("warnings", []))
    human_review_reasons = monitoring.get("human_review_required_reasons", [])
    freshness_days = metrics.get("data_freshness_days")
    data_badge = "fresh" if freshness_days is not None and freshness_days <= 45 else "stale_or_unknown"
    performance_monitoring_status = reconciliation.get("performance_monitoring_status") or monitoring_status
    health_badge = {
        "candidate_healthy": "healthy",
        "candidate_watch": "watch",
        "candidate_degraded": "degraded",
        "human_review_required": "human_review_required",
        "data_issue": "data_issue",
    }.get(performance_monitoring_status, "unknown")
    daily_mdd = metrics.get("daily_MDD", candidate["current_metrics"].get("daily_MDD"))
    risk_badge = "review" if human_review_reasons else ("elevated" if daily_mdd is not None and daily_mdd < -0.25 else "contained")
    confidence_level = "medium" if performance_monitoring_status == "candidate_healthy" else "low"
    data_truth_status = reconciliation.get("data_truth_status") or data_truth.get("final_conclusion", "unknown")
    display_status = (
        reconciliation.get("display_status")
        or data_truth.get("recommended_monitoring_status")
        or ("candidate_watch_due_to_data_gaps" if data_truth_status == "candidate_valid_but_data_risk_high" else monitoring_status)
    )
    p0_gap_count = int(gaps.get("p0_gap_count", 0) or 0)
    data_gap_count = int(gaps.get("gap_count", p0_gap_count) or 0)
    data_watch_due_to_gaps = data_truth_status == "candidate_valid_but_data_risk_high" or display_status == "candidate_watch_due_to_data_gaps" or p0_gap_count > 0
    data_risk_badge = "high_risk_watch" if data_watch_due_to_gaps else "controlled"
    card = {
        "candidate_id": candidate["candidate_id"],
        "display_name": candidate["display_name"],
        "status": candidate["status"],
        "monitoring_status": monitoring_status,
        "performance_monitoring_status": performance_monitoring_status,
        "data_truth_status": data_truth_status,
        "display_status": display_status,
        "data_risk_badge": data_risk_badge,
        "data_gap_count": data_gap_count,
        "p0_data_gap_count": p0_gap_count,
        "candidate_upgrade_blocked_by_data_gaps": data_watch_due_to_gaps,
        "next_data_action": "collect_real_metadata_and_rerun_data_truth_audit" if data_watch_due_to_gaps else "human_review",
        "primary_combo": candidate["primary_combo"],
        "component_weights": candidate["component_weights"],
        "annualized_return": metrics.get("annualized_return", candidate["current_metrics"].get("annualized_return")),
        "sharpe": metrics.get("Sharpe", candidate["current_metrics"].get("Sharpe")),
        "sortino": metrics.get("Sortino", candidate["current_metrics"].get("Sortino")),
        "daily_mdd": daily_mdd,
        "monthly_mdd": metrics.get("monthly_MDD", candidate["current_metrics"].get("monthly_MDD")),
        "rolling_252d_return": metrics.get("rolling_252d_return", candidate["current_metrics"].get("rolling_252d_return")),
        "cost_stress_50bps_annualized": (
            metrics.get("cost_stress_proxy", {})
            .get("cost_plus_50bps_per_rebalance", {})
            .get("annualized_return")
        ),
        "main_risk_source": metrics.get("main_risk_source") or candidate["risk_summary"].get("main_risk_source"),
        "warning_count": warning_count,
        "human_review_required": bool(human_review_reasons),
        "allowed_usage": ALLOWED_USAGE,
        "prohibited_usage": PROHIBITED_USAGE,
        "source_files": registry.get("source_files", []) + [str(monitoring_path.relative_to(base)).replace("\\", "/")],
        "next_review_trigger": candidate.get("next_review_trigger", []),
        "health_badge": health_badge,
        "risk_badge": risk_badge,
        "confidence_level": confidence_level,
        "last_refresh_time": monitoring.get("generated_at"),
        "data_freshness_badge": data_badge,
        "main_positive_points": [
            "Performance monitoring status is candidate_healthy" if performance_monitoring_status == "candidate_healthy" else "Monitoring is available for review",
            "Fixed 50/50 construction remains the only formal candidate",
            "Dashboard card is research-only and not a trading signal",
        ],
        "main_risk_points": [
            "Daily MDD remains material and must be monitored",
            "Maximum drawdown risk is mainly from the rev120 leg",
            "Cost stress can reduce annualized return",
            "Data truth audit found PIT/survivorship/adjustment/holdings evidence gaps",
        ],
        "human_review_reason": human_review_reasons,
        "research_only_banner": "Research-only monitoring display. No live trading, broker connection, auto execution, buy/sell recommendation, or real-money allocation instruction.",
    }
    return {
        "task_id": "dashboard_candidate_card_integration",
        "status": "completed",
        "cards": [card],
        "watchlist_cards": watchlist_cards,
        "candidate_count": 1,
        "watchlist_count": len(watchlist_cards),
        "risk_disclaimer_included": True,
        "research_only": True,
        "prohibited_usage": PROHIBITED_USAGE,
        "files_written": [
            "results/dashboard_candidate_cards_latest.json",
            "reports/dashboard_candidate_cards_latest.md",
            "reports/research_dashboard_candidate_summary_latest.md",
            "results/dashboard_watchlist_cards_latest.json",
            "reports/dashboard_watchlist_cards_latest.md",
        ],
    }


def render_cards_markdown(payload: dict[str, Any]) -> str:
    if not payload.get("cards"):
        warnings = ", ".join(payload.get("warnings", [])) or "none"
        return "\n".join(
            [
                "# Dashboard Candidate Cards",
                "",
                "Research-only: `true`",
                "Risk disclaimer included: `true`",
                "",
                "No formal candidate cards are available because the candidate pool is empty.",
                f"Warnings: `{warnings}`",
                "",
                "Boundary: Research-only. Not trading advice. No live trading. No broker connection. No auto execution. No buy/sell recommendation. No real-money allocation instruction.",
            ]
        ) + "\n"
    card = payload["cards"][0]
    return "\n".join(
        [
            "# Dashboard Candidate Cards",
            "",
            "Research-only: `true`",
            "Risk disclaimer included: `true`",
            "",
            "| candidate | performance | data truth | display | annualized | Sharpe | daily MDD | P0 gaps |",
            "|---|---|---|---|---:|---:|---:|---:|",
            f"| {card['candidate_id']} | {card['performance_monitoring_status']} | {card['data_truth_status']} | {card['display_status']} | {pct(card['annualized_return'])} | {fmt(card['sharpe'])} | {pct(card['daily_mdd'])} | {card['p0_data_gap_count']} |",
            "",
            f"Health badge: `{card['health_badge']}`. Risk badge: `{card['risk_badge']}`. Confidence: `{card['confidence_level']}`.",
            f"Data risk badge: `{card['data_risk_badge']}`. Candidate upgrade blocked by data gaps: `{card['candidate_upgrade_blocked_by_data_gaps']}`.",
            "",
            "Boundary: Research-only. Not trading advice. No live trading. No broker connection. No auto execution. No buy/sell recommendation. No real-money allocation instruction.",
        ]
    ) + "\n"


def render_summary_markdown(payload: dict[str, Any]) -> str:
    if not payload.get("cards"):
        warnings = ", ".join(payload.get("warnings", [])) or "none"
        return "\n".join(
            [
                "# Research Dashboard Candidate Summary",
                "",
                "## Candidate Overview",
                "",
                "- candidate_count: `0`",
                "- display status: `candidate_pool_empty`",
                f"- warnings: `{warnings}`",
                "",
                "## Risk Summary",
                "",
                "- No candidate metrics are available to summarize.",
                "- Missing candidate data is shown as a degraded research state, not as a clean result.",
                "- Research display only.",
                "",
                "## Usage Boundary",
                "",
                "- No live trading.",
                "- No broker connection.",
                "- No auto execution.",
                "- No buy/sell recommendation.",
                "- No real-money allocation instruction.",
            ]
        ) + "\n"
    card = payload["cards"][0]
    return "\n".join(
        [
            "# Research Dashboard Candidate Summary",
            "",
            "## Candidate Overview",
            "",
            f"- candidate_id: `{card['candidate_id']}`",
            f"- primary combo: `{card['primary_combo']}`",
            f"- current status: `{card['monitoring_status']}`",
            f"- performance monitoring status: `{card['performance_monitoring_status']}`",
            f"- data truth status: `{card['data_truth_status']}`",
            f"- display status: `{card['display_status']}`",
            f"- data risk badge: `{card['data_risk_badge']}`",
            f"- P0 data gaps: `{card['p0_data_gap_count']}`",
            f"- health badge: `{card['health_badge']}`",
            f"- risk badge: `{card['risk_badge']}`",
            f"- confidence level: `{card['confidence_level']}`",
            f"- annualized_return: `{pct(card['annualized_return'])}`",
            f"- Sharpe: `{fmt(card['sharpe'])}`",
            f"- Sortino: `{fmt(card['sortino'])}`",
            f"- daily MDD: `{pct(card['daily_mdd'])}`",
            f"- monthly MDD: `{pct(card['monthly_mdd'])}`",
            f"- rolling 252d return: `{pct(card['rolling_252d_return'])}`",
            "",
            "## Component Summary",
            "",
            "- low-vol canonical",
            "- rev120 B canonical",
            "- fixed 50/50 research portfolio construction",
            "",
            "## Risk Summary",
            "",
            f"- Daily MDD: `{pct(card['daily_mdd'])}`; monitor this as a risk observation, not a trading instruction.",
            f"- Main risk source: `{card.get('main_risk_source') or 'unknown'}`.",
            f"- Cost stress annualized reference: `{pct(card['cost_stress_50bps_annualized'])}`.",
            "- Data truth audit found high evidence gaps; do not upgrade beyond research review until resolved.",
            "- Future performance is not guaranteed.",
            "- Research display only.",
            "",
            "## Usage Boundary",
            "",
            "- No live trading.",
            "- No broker connection.",
            "- No auto execution.",
            "- No buy/sell recommendation.",
            "- No real-money allocation instruction.",
        ]
    ) + "\n"


def render_watchlist_markdown(payload: dict[str, Any]) -> str:
    lines = [
        "# Dashboard Watchlist Cards",
        "",
        "Boundary: Research-only. Not trading advice. No live trading. No broker connection. No auto execution. No buy/sell recommendation. No real-money allocation instruction.",
        "",
    ]
    for card in payload.get("watchlist_cards", []):
        lines.extend(
            [
                f"## {card['watchlist_id']}",
                f"- type: `{card['type']}`",
                f"- why not candidate: {card['why_not_candidate']}",
                f"- main risk: {card['main_risk']}",
                f"- main blocker: {card['main_blocker']}",
                f"- next action: {card['next_action']}",
                f"- stop research reason: {card['stop_research_reason']}",
                f"- continue research: `{card['continue_research']}`",
                f"- dashboard monitoring only: `{card['dashboard_monitoring_only']}`",
                "",
            ]
        )
    return "\n".join(lines)


def write_dashboard_cards(root: Path | None = None) -> dict[str, Any]:
    base = (root or Path(__file__).resolve().parents[1]).resolve()
    payload = build_dashboard_cards(root=base)
    write_json(base / "results" / "dashboard_candidate_cards_latest.json", payload)
    write_text(base / "reports" / "dashboard_candidate_cards_latest.md", render_cards_markdown(payload))
    write_text(base / "reports" / "research_dashboard_candidate_summary_latest.md", render_summary_markdown(payload))
    watch_payload = {
        "task_id": "dashboard_candidate_card_integration",
        "status": "completed",
        "portfolio_watchlist": [c["watchlist_id"] for c in payload["watchlist_cards"] if c["type"] == "portfolio_watchlist"],
        "strategy_watchlist": [c["watchlist_id"] for c in payload["watchlist_cards"] if c["type"] == "strategy_watchlist"],
        "watchlist_cards": payload["watchlist_cards"],
        "research_only": True,
        "prohibited_usage": PROHIBITED_USAGE,
    }
    write_json(base / "results" / "dashboard_watchlist_cards_latest.json", watch_payload)
    write_text(base / "reports" / "dashboard_watchlist_cards_latest.md", render_watchlist_markdown(watch_payload))
    return payload
