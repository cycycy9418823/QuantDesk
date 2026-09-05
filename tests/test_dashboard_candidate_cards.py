from __future__ import annotations

import json
from pathlib import Path

from quantdesk.dashboard_candidate_cards import build_dashboard_cards
from quantdesk.dashboard_candidate_cards import render_summary_markdown

ROOT = Path(__file__).resolve().parents[1]


def _populated_inputs(tmp_path: Path) -> dict[str, Path]:
    """Write a deterministic, fully-populated set of builder inputs.

    These tests used to read the real results/research_candidate_registry_latest.json,
    which is pipeline output: it is gitignored, it is not a fixture, and it is absent
    in a fresh checkout. Copying it in or running the workflow to create it would make
    the suite depend on generated state. The builder already accepts injected paths, so
    the inputs are constructed here instead - deterministic, and exercising the same
    code path as production.
    """
    registry = tmp_path / "registry.json"
    monitoring = tmp_path / "monitoring.json"
    state = tmp_path / "state.json"

    registry.write_text(json.dumps({
        "candidate_pool": [{
            "candidate_id": "combo_50_50",
            "display_name": "Fixed 50/50 combo",
            "status": "candidate",
            "monitoring_status": "candidate_healthy",
            "primary_combo": "rev120_plus_mom20",
            "component_weights": {"rev120": 0.5, "mom20": 0.5},
            "current_metrics": {
                "annualized_return": 0.1234,
                "Sharpe": 0.91,
                "Sortino": 1.22,
                "daily_MDD": -0.1831,
                "monthly_MDD": -0.0912,
                "rolling_252d_return": 0.1102,
            },
            "risk_summary": {"main_risk_source": "rev120_leg_drawdown"},
            "next_review_trigger": ["monthly_metric_refresh"],
        }],
        "portfolio_watchlist": [f"alt_{i}" for i in range(1, 7)],
        "strategy_watchlist": ["strategy_alt_a", "strategy_alt_b"],
        "source_files": ["results/research_candidate_registry_latest.json"],
    }, ensure_ascii=False), encoding="utf-8")

    monitoring.write_text(json.dumps({
        "monitoring_status": "candidate_healthy",
        "generated_at": "2026-08-28T16:43:00+08:00",
        "warnings": [],
        "human_review_required_reasons": [],
        "metrics": {
            "annualized_return": 0.1234,
            "Sharpe": 0.91,
            "Sortino": 1.22,
            "daily_MDD": -0.1831,
            "monthly_MDD": -0.0912,
            "rolling_252d_return": 0.1102,
            "data_freshness_days": 3,
            "main_risk_source": "rev120_leg_drawdown",
            "cost_stress_proxy": {
                "cost_plus_50bps_per_rebalance": {"annualized_return": 0.0871},
            },
        },
    }, ensure_ascii=False), encoding="utf-8")

    state.write_text(json.dumps({
        "final_state": {"portfolio_watchlist": [f"alt_{i}" for i in range(1, 7)]},
    }, ensure_ascii=False), encoding="utf-8")

    return {"registry_path": registry, "monitoring_path": monitoring, "state_path": state}


def _build(tmp_path: Path) -> dict:
    return build_dashboard_cards(root=tmp_path, **_populated_inputs(tmp_path))


def test_dashboard_card_research_only_and_prohibited_usage(tmp_path: Path):
    payload = _build(tmp_path)
    assert payload["research_only"] is True
    card = payload["cards"][0]
    for usage in ["live_trading", "broker_connection", "auto_execution"]:
        assert usage in payload["prohibited_usage"]
        assert usage in card["prohibited_usage"]


def test_dashboard_card_builder_repeatable(tmp_path: Path):
    inputs = _populated_inputs(tmp_path)
    first = build_dashboard_cards(root=tmp_path, **inputs)
    second = build_dashboard_cards(root=tmp_path, **inputs)
    assert first["cards"][0]["candidate_id"] == second["cards"][0]["candidate_id"]
    assert first["candidate_count"] >= 1
    assert first["watchlist_count"] >= 5


def test_dashboard_card_contains_required_fields(tmp_path: Path):
    payload = _build(tmp_path)
    card = payload["cards"][0]
    required = {
        "candidate_id",
        "display_name",
        "status",
        "monitoring_status",
        "primary_combo",
        "component_weights",
        "annualized_return",
        "sharpe",
        "sortino",
        "daily_mdd",
        "monthly_mdd",
        "rolling_252d_return",
        "cost_stress_50bps_annualized",
        "main_risk_source",
        "warning_count",
        "human_review_required",
        "allowed_usage",
        "prohibited_usage",
        "source_files",
        "next_review_trigger",
        "health_badge",
        "risk_badge",
        "confidence_level",
        "last_refresh_time",
        "data_freshness_badge",
        "main_positive_points",
        "main_risk_points",
        "human_review_reason",
        "research_only_banner",
    }
    assert required.issubset(card)


def test_dashboard_card_has_no_trading_language(tmp_path: Path):
    payload = _build(tmp_path)
    text = json.dumps(payload, ensure_ascii=False).lower()
    forbidden = ["buy signal", "sell signal", "order instruction", "real-money allocation instruction:"]
    for term in forbidden:
        assert term not in text


def test_dashboard_allowed_usage_does_not_include_broker_or_execution(tmp_path: Path):
    payload = _build(tmp_path)
    allowed = set(payload["cards"][0]["allowed_usage"])
    assert "broker_connection" not in allowed
    assert "auto_execution" not in allowed


def test_dashboard_card_empty_candidate_pool_degrades(tmp_path: Path):
    registry = tmp_path / "registry.json"
    monitoring = tmp_path / "monitoring.json"
    state = tmp_path / "state.json"
    registry.write_text('{"candidate_pool": [], "portfolio_watchlist": [], "strategy_watchlist": []}', encoding="utf-8")
    monitoring.write_text('{"monitoring_status": "data_issue", "warnings": ["missing candidate"]}', encoding="utf-8")
    state.write_text('{"final_state": {}}', encoding="utf-8")

    payload = build_dashboard_cards(
        root=tmp_path,
        registry_path=registry,
        monitoring_path=monitoring,
        state_path=state,
    )

    assert payload["status"] == "completed_with_warnings"
    assert payload["candidate_count"] == 0
    assert payload["cards"] == []
    assert payload["warnings"]


def test_dashboard_summary_uses_metric_values_not_hardcoded_risk_text(tmp_path: Path):
    payload = _build(tmp_path)
    markdown = render_summary_markdown(payload)
    card = payload["cards"][0]

    assert "-23%" not in markdown
    assert str(card["main_risk_source"]) in markdown
