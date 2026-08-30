from __future__ import annotations

import json
from pathlib import Path

from quantdesk.dashboard_candidate_cards import build_dashboard_cards
from quantdesk.dashboard_candidate_cards import render_summary_markdown

ROOT = Path(__file__).resolve().parents[1]


def test_dashboard_card_research_only_and_prohibited_usage():
    payload = build_dashboard_cards(root=ROOT)
    assert payload["research_only"] is True
    card = payload["cards"][0]
    for usage in ["live_trading", "broker_connection", "auto_execution"]:
        assert usage in payload["prohibited_usage"]
        assert usage in card["prohibited_usage"]


def test_dashboard_card_builder_repeatable():
    first = build_dashboard_cards(root=ROOT)
    second = build_dashboard_cards(root=ROOT)
    assert first["cards"][0]["candidate_id"] == second["cards"][0]["candidate_id"]
    assert first["candidate_count"] >= 1
    assert first["watchlist_count"] >= 5


def test_dashboard_card_contains_required_fields():
    payload = build_dashboard_cards(root=ROOT)
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


def test_dashboard_card_has_no_trading_language():
    payload = build_dashboard_cards(root=ROOT)
    text = json.dumps(payload, ensure_ascii=False).lower()
    forbidden = ["buy signal", "sell signal", "order instruction", "real-money allocation instruction:"]
    for term in forbidden:
        assert term not in text


def test_dashboard_allowed_usage_does_not_include_broker_or_execution():
    payload = build_dashboard_cards(root=ROOT)
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


def test_dashboard_summary_uses_metric_values_not_hardcoded_risk_text():
    payload = build_dashboard_cards(root=ROOT)
    markdown = render_summary_markdown(payload)
    card = payload["cards"][0]

    assert "-23%" not in markdown
    assert str(card["main_risk_source"]) in markdown
