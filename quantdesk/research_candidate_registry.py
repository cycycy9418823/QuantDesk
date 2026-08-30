from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

from quantdesk.candidate_monitoring import (
    ALLOWED_USAGE,
    CANDIDATE_ID,
    PRIMARY_COMBO,
    PROHIBITED_USAGE,
    read_json,
    write_json,
    write_text,
)


def default_state() -> dict[str, list[str]]:
    return {
        "candidate_pool": [CANDIDATE_ID],
        "portfolio_watchlist": ["combo_60_40", "combo_70_30", "combo_80_20"],
        "strategy_watchlist": [
            "liquidity_resilience_defensive_top50_quarterly",
            "liquidity_resilience_defensive_top100_quarterly",
        ],
        "risk_overlay_watchlist": [],
        "paused": ["volume_dry_rebound"],
        "rejected": [
            "pure_momentum",
            "trend_continuation",
            "rsi_recovery",
            "low_vol_carry_stability_top50_quarterly",
            "low_vol_carry_stability_top100_quarterly",
            "combo_cash_timing_vol_spike_half",
            "quality reversal variants",
            "breadth variants",
        ],
    }


def build_registry(root: Path | None = None) -> dict[str, Any]:
    base = (root or Path(__file__).resolve().parents[1]).resolve()
    results = base / "results"
    card_path = results / "dashboard_candidate_card_portfolio_low_vol_rev120_latest.json"
    monitoring_path = results / "portfolio_candidate_monitoring_latest.json"
    spec_path = results / "portfolio_candidate_monitoring_spec_latest.json"
    state_path = results / "strategy_pool_state_latest.json"
    card = read_json(card_path)
    monitoring = read_json(monitoring_path)
    spec = read_json(spec_path)
    state = default_state()
    state_json = read_json(state_path) if state_path.exists() else {}
    if isinstance(state_json.get("final_state"), dict):
        final_state = state_json["final_state"]
        state["portfolio_watchlist"] = final_state.get("portfolio_watchlist", state["portfolio_watchlist"])
        state["strategy_watchlist"] = final_state.get("strategy_watchlist", state["strategy_watchlist"])
        state["risk_overlay_watchlist"] = final_state.get("risk_overlay_watchlist", state["risk_overlay_watchlist"])
        state["paused"] = final_state.get("disputed", state["paused"])
        state["rejected"] = final_state.get("rejected", state["rejected"])

    candidate = {
        "candidate_id": CANDIDATE_ID,
        "display_name": card.get("display_name", "Low-vol + Rev120 B Canonical Portfolio"),
        "status": "candidate_pool",
        "monitoring_status": monitoring.get("monitoring_status", card.get("latest_monitoring_status")),
        "primary_combo": PRIMARY_COMBO,
        "component_weights": card.get("component_weights", {"low_vol_canonical": 0.5, "rev120_B_canonical": 0.5}),
        "canonical_input_files": [
            "results/low_vol_canonical_daily_equity_latest.csv",
            "results/rev120_bcanonical_daily_equity_latest.csv",
            "results/portfolio_daily_equity_rebuild_latest.csv",
        ],
        "current_metrics": {
            "annualized_return": card.get("annualized_return"),
            "Sharpe": card.get("Sharpe"),
            "Sortino": monitoring.get("metrics", {}).get("Sortino"),
            "daily_MDD": card.get("daily_MDD"),
            "monthly_MDD": card.get("monthly_MDD"),
            "rolling_252d_return": monitoring.get("metrics", {}).get("rolling_252d_return"),
            "annualized_without_2024_2025": card.get("annualized_without_2024_2025"),
            "cost_stress_proxy": monitoring.get("metrics", {}).get("cost_stress_proxy", {}),
        },
        "risk_summary": {
            "main_risk_source": monitoring.get("risk_assessment", {}).get("max_risk_source")
            or monitoring.get("metrics", {}).get("main_risk_source")
            or card.get("main_risk"),
            "warnings": monitoring.get("warnings", []),
            "human_review_required_reasons": monitoring.get("human_review_required_reasons", []),
        },
        "warning_rules": spec.get("warning_conditions", []),
        "human_review_rules": spec.get("human_review_required_conditions", []),
        "allowed_usage": ALLOWED_USAGE,
        "prohibited_usage": PROHIBITED_USAGE,
        "source_reports": [
            "reports/portfolio_candidate_monitoring_latest.md",
            "reports/dashboard_candidate_card_portfolio_low_vol_rev120_latest.md",
            "reports/portfolio_low_vol_rev120_candidate_risk_disclaimer.md",
            "reports/portfolio_low_vol_rev120_candidate_usage_boundary.md",
        ],
        "next_review_trigger": card.get("next_review_trigger", []),
    }
    return {
        "registry_id": "research_candidate_registry",
        "research_only": True,
        "candidate_pool": [candidate],
        "portfolio_watchlist": state["portfolio_watchlist"],
        "strategy_watchlist": state["strategy_watchlist"],
        "risk_overlay_watchlist": state["risk_overlay_watchlist"],
        "paused": state["paused"],
        "rejected": state["rejected"],
        "usage_boundary": {
            "allowed_usage": ALLOWED_USAGE,
            "prohibited_usage": PROHIBITED_USAGE,
            "no_live_trading": True,
            "no_broker_connection": True,
            "no_auto_execution": True,
            "no_buy_sell_recommendation": True,
        },
        "source_files": [
            str(card_path.relative_to(base)).replace("\\", "/"),
            str(monitoring_path.relative_to(base)).replace("\\", "/"),
            str(spec_path.relative_to(base)).replace("\\", "/"),
            str(state_path.relative_to(base)).replace("\\", "/"),
        ],
        "created_at": datetime.now().isoformat(timespec="seconds"),
    }


def render_registry_markdown(registry: dict[str, Any]) -> str:
    candidate = registry["candidate_pool"][0]
    metrics = candidate["current_metrics"]
    return "\n".join(
        [
            "# Research Candidate Registry",
            "",
            "Research-only: `true`",
            "",
            "## Candidate Pool",
            "",
            f"- `{candidate['candidate_id']}`: {candidate['display_name']}",
            f"- monitoring_status: `{candidate['monitoring_status']}`",
            f"- primary_combo: `{candidate['primary_combo']}`",
            f"- annualized_return: `{metrics.get('annualized_return')}`",
            f"- Sharpe: `{metrics.get('Sharpe')}`",
            f"- daily_MDD: `{metrics.get('daily_MDD')}`",
            "",
            "## Watchlists",
            "",
            f"- portfolio_watchlist: `{registry['portfolio_watchlist']}`",
            f"- strategy_watchlist: `{registry['strategy_watchlist']}`",
            f"- risk_overlay_watchlist: `{registry['risk_overlay_watchlist']}`",
            "",
            "## Usage Boundary",
            "",
            "- research dashboard / paper research / simulation / risk monitoring / human review only",
            "- prohibited: live trading, broker connection, auto execution, buy/sell recommendation, real-money allocation instruction",
        ]
    ) + "\n"


def write_registry(root: Path | None = None) -> dict[str, Any]:
    base = (root or Path(__file__).resolve().parents[1]).resolve()
    registry = build_registry(base)
    write_json(base / "results" / "research_candidate_registry_latest.json", registry)
    write_text(base / "reports" / "research_candidate_registry_latest.md", render_registry_markdown(registry))
    return registry

