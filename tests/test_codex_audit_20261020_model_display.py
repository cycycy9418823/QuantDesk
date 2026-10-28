"""Failed transport, accepted judgment, committed plan and paper fills are distinct."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from ai_trader import dashboard


@pytest.mark.parametrize("status,phase,category", [(410, "response_body", "provider_gone"),
    (None, "response_headers", "response_headers_timeout"), (None, "tcp", "connect_timeout")])
def test_model_failure_is_not_a_model_or_backup_judgment(tmp_path: Path, status, phase: str, category: str) -> None:
    state = tmp_path / "state/ai_trader"
    state.mkdir(parents=True)
    plan = {"plan_id": "fixture-plan", "decision_date": "2026-10-19", "committed": True,
        "decision_mode": "hold_only", "intents": [], "llm": {"used": False, "model": "requested-model",
            "unavailable_reason": "transport:TimeoutError", "transport": {"http_status": status, "error_category": "timeout"},
            "transport_detail": {"requested_model": "requested-model", "failure_phase": phase,
                "response_headers_received": status is not None, "request_written": True,
                "started_at": "2026-10-19T20:20:00+08:00", "finished_at": "2026-10-19T20:25:00+08:00"}}}
    (state / "plan_latest.json").write_text(json.dumps(plan), encoding="utf-8")
    payload = dashboard.build_dashboard_payload(state_dir=state, results_dir=tmp_path / "results")
    assert payload["llm"]["actual_response_model"] is None
    assert payload["llm"]["requested_model"] == "requested-model"
    assert payload["llm"]["failure_category"] == category
    assert payload["llm"]["judgment_status"] == "not_received"
    assert payload["llm"]["decision_label"] == "未取得新判断，维持持仓"
    assert payload["operation_stages"]["plan_committed"] is True
    assert payload["operation_stages"]["paper_fill_count"] == 0


def test_invalid_answer_is_received_but_not_accepted(tmp_path: Path) -> None:
    state = tmp_path / "state"; state.mkdir()
    (state / "plan_latest.json").write_text(json.dumps({"decision_mode": "hold_only", "committed": False,
        "llm": {"used": False, "unavailable_reason": "unparseable_json",
            "response_evidence": {"response_model": "returned-model", "text_status": "received"}}}), encoding="utf-8")
    payload = dashboard.build_dashboard_payload(state_dir=state, results_dir=tmp_path / "results")
    assert payload["llm"]["actual_response_model"] == "returned-model"
    assert payload["llm"]["judgment_status"] == "rejected"
    assert payload["operation_stages"]["plan_committed"] is False


def test_real_nested_response_receipt_format_is_understood(tmp_path: Path) -> None:
    from ai_trader.llm_transport import describe_model_request
    result = describe_model_request({"used": False, "unavailable_reason": "transport:HTTPError",
        "response_evidence": {"requested_model": "requested", "response_model": None,
            "transport": {"http_status": 410, "error_category": "http_error"},
            "transport_detail": {"response_headers_received": True, "started_at": "2026-10-19T20:20:00+08:00"}}})
    assert result["failure_category"] == "provider_gone"
    assert result["requested_model"] == "requested"
    assert result["actual_response_model"] is None


def test_default_profile_is_a_template_not_the_funded_book(tmp_path: Path, monkeypatch) -> None:
    from flask import Flask
    from scripts.server.routes import decision_board as routes
    from quantdesk.trader_profile import default_research_paper_profile

    deps = routes.DecisionBoardRouteDeps(load_watchlist=lambda: {},
        load_portfolio=lambda: {"initial_cash": 100000, "cash": 100000, "market_value": 0,
            "total_equity": 100000, "total_return_pct": 0, "positions": [], "actions": []},
        load_equity_history=lambda: {}, load_benchmark=lambda: {},
        load_profile=default_research_paper_profile, load_calendar_days=lambda: [],
        strategy_version=lambda: "fixture", source_paths=lambda: {})
    monkeypatch.setattr(routes, "evaluate_data_chain_consumer_gate", lambda: {"allowed": True})
    app = Flask(__name__); app.register_blueprint(routes.create_decision_board_blueprint(deps))
    response = app.test_client().get("/api/decision-board")
    assert response.status_code == 200
    result = response.get_json()
    profile = result["envelope"]["profile"]
    assert profile["paper_capital"] == 100000
    assert profile["configured_template_capital"] == 10000
    assert profile["capital_source"] == "existing_strategy_ledger"
    assert profile["max_total_exposure_pct"] == 0.8
    assert profile["max_single_position_pct"] == 0.2
    assert "profile_capital_mismatch" not in result["warnings"]
    import hashlib
    encoded = json.dumps(profile, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    assert result["envelope"]["manifest"]["input_hashes"]["profile"] == hashlib.sha256(encoded.encode()).hexdigest()
