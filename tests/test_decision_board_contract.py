from __future__ import annotations

import json
from pathlib import Path

from flask import Flask


def _profile():
    from quantdesk.trader_profile import default_research_paper_profile

    return default_research_paper_profile()


def _inputs() -> dict:
    return {
        "watchlist_payload": {
            "ok": True,
            "updated_at": "2026-07-10T16:00:00",
            "paper_tracking_summary": {"needs_review_count": 1, "data_gap_count": 0},
            "items": [
                {
                    "symbol": "000001",
                    "name": "样本一",
                    "entry_date": "2026-07-07",
                    "elapsed_trading_days": 3,
                    "return_since_added_pct": 2.5,
                    "review_status": "paper_tracking",
                    "review_label": "继续观察",
                    "review_reason": "后端复核状态",
                }
            ],
        },
        "portfolio_payload": {
            "ok": True,
            "exists": True,
            "generated_at": "2026-07-10T16:01:00",
            "schema_version": "3.7.0.6",
            "summary": {
                "initial_cash": 10_000.0,
                "cash": 8_800.0,
                "market_value": 1_260.0,
                "total_equity": 10_060.0,
                "total_return_pct": 0.6,
                "realized_pnl": 10.0,
                "unrealized_pnl": 50.0,
                "position_count": 1,
            },
            "positions": [
                {
                    "symbol": "000001",
                    "name": "样本一",
                    "entry_date": "2026-07-07",
                    "latest_date": "2026-07-10",
                    "entry_price": 10.0,
                    "latest_price": 10.5,
                    "quantity": 100,
                    "unrealized_pnl": 50.0,
                    "unrealized_return_pct": 5.0,
                    "management_state": "hold_review",
                    "hold_reason": "后端纸面复核结论",
                }
            ],
            "actions": [
                {
                    "decision_date": "2026-07-10",
                    "symbol": "000001",
                    "action": "hold",
                    "reason": "后端事件",
                    "quantity": 0,
                    "is_real_order": False,
                    "paper_only": True,
                    "no_broker": True,
                }
            ],
        },
        "equity_history_payload": {
            "ok": True,
            "items": [
                {"date": "2026-07-07", "total_return_pct": 0.0, "total_equity": 10_000.0},
                {"date": "2026-07-10", "total_return_pct": 0.6, "total_equity": 10_060.0},
            ],
        },
        "benchmark_payload": {
            "ok": True,
            "label": "沪深300",
            "items": [
                {"date": "2026-07-07", "close": 4000.0},
                {"date": "2026-07-08", "close": 4040.0},
                {"date": "2026-07-10", "close": 4080.0},
            ],
        },
        "profile": _profile(),
        "calendar_days": ["2026-07-07", "2026-07-08", "2026-07-09", "2026-07-10"],
        "strategy_version": "3.7.0.6-test",
        "source_paths": {
            "portfolio": "results/auto_paper_portfolio_latest.json",
            "watchlist": "state/paper_simulation_watchlist.json",
            "equity_history": "results/auto_paper_portfolio_history.json",
            "benchmark": "data/benchmark/hs300.csv",
        },
    }


def test_decision_board_owns_position_metrics_and_common_envelope() -> None:
    from quantdesk.decision_board import build_decision_board_snapshot

    payload = build_decision_board_snapshot(**_inputs())

    assert payload["ok"] is True
    assert set(payload["envelope"]) == {
        "as_of",
        "source",
        "coverage",
        "strategy_version",
        "risk",
        "event",
        "profile",
        "manifest",
    }
    position = payload["positions"][0]
    assert position["holding_days"] == 3
    assert position["holding_days_source"] == "a_share_calendar"
    assert position["unrealized_pnl"] == 50.0
    assert position["review_status"] == "hold_review"
    assert position["review_reason"] == "后端纸面复核结论"
    assert payload["account"]["total_equity"] == 10_060.0
    assert payload["envelope"]["profile"]["profile_id"] == "user_10k"
    assert payload["envelope"]["manifest"]["manifest_id"]


def test_decision_board_never_invents_missing_pnl_from_price_and_quantity() -> None:
    from quantdesk.decision_board import build_decision_board_snapshot

    inputs = _inputs()
    inputs["portfolio_payload"]["positions"][0].pop("unrealized_pnl")
    payload = build_decision_board_snapshot(**inputs)

    position = payload["positions"][0]
    assert position["entry_price"] == 10.0
    assert position["latest_price"] == 10.5
    assert position["quantity"] == 100
    assert position["unrealized_pnl"] is None
    assert position["metric_status"] == "degraded"
    assert "position_unrealized_pnl_missing:000001" in payload["warnings"]


def test_decision_board_aligns_paper_and_benchmark_returns_in_backend() -> None:
    from quantdesk.decision_board import build_decision_board_snapshot

    payload = build_decision_board_snapshot(**_inputs())

    assert payload["return_series"]["status"] == "degraded"
    assert payload["return_series"]["items"] == [
        {"date": "2026-07-07", "paper_return_pct": 0.0, "benchmark_return_pct": 0.0},
        {"date": "2026-07-08", "paper_return_pct": None, "benchmark_return_pct": 1.0},
        {"date": "2026-07-09", "paper_return_pct": None, "benchmark_return_pct": None},
        {"date": "2026-07-10", "paper_return_pct": 0.6, "benchmark_return_pct": 2.0},
    ]
    assert payload["return_series"]["missing_valuation_dates"] == ["2026-07-08", "2026-07-09"]
    assert payload["envelope"]["coverage"]["aligned_return_point_count"] == 2
    assert payload["return_series"]["benchmark_label"] == "沪深300"


def test_decision_board_marks_profile_capital_mismatch_without_resetting_ledger() -> None:
    from quantdesk.decision_board import build_decision_board_snapshot

    inputs = _inputs()
    inputs["portfolio_payload"]["summary"]["initial_cash"] = 100_000.0
    payload = build_decision_board_snapshot(**inputs)

    assert payload["status"] == "degraded"
    assert payload["account"]["initial_cash"] == 100_000.0
    assert payload["envelope"]["profile"]["capital_alignment"] == "mismatch"
    assert "profile_capital_mismatch" in payload["warnings"]


def test_decision_board_exposes_all_research_and_paper_boundaries() -> None:
    from quantdesk.decision_board import REQUIRED_BOUNDARY_BADGES, build_decision_board_snapshot

    payload = build_decision_board_snapshot(**_inputs())

    assert payload["boundaries"]["badges"] == list(REQUIRED_BOUNDARY_BADGES)
    assert payload["boundaries"]["research_only"] is True
    assert payload["boundaries"]["paper_only"] is True
    assert payload["boundaries"]["no_broker"] is True
    assert payload["boundaries"]["live_execution_locked"] is True


def test_equity_history_accessor_normalizes_dates_and_is_read_only(tmp_path: Path, monkeypatch) -> None:
    from quantdesk.paper import auto_paper_portfolio as module

    history_path = tmp_path / "history.json"
    history_path.write_text(
        json.dumps(
            {
                "ok": True,
                "updated_at": "2026-07-10T16:00:00",
                "items": [
                    {"valuation_date": "2026-07-07", "generated_at": "2026-07-08T01:01:00", "cash": 10_000,
                     "market_value": 0, "total_equity": 10_000, "total_return_pct": 0, "portfolio_review_valid": True},
                    {"valuation_date": "2026-07-10", "generated_at": "2026-07-10T15:01:00", "cash": 10_060,
                     "market_value": 0, "total_equity": 10_060, "total_return_pct": 0.6, "portfolio_review_valid": True},
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    before = history_path.read_bytes()
    monkeypatch.setattr(module, "AUTO_PAPER_PORTFOLIO_HISTORY_JSON", history_path)

    payload = module.auto_paper_portfolio_equity_history(limit=250)

    assert [item["date"] for item in payload["items"]] == ["2026-07-07", "2026-07-10"]
    assert payload["latest"]["total_equity"] == 10_060.0
    assert payload["research_only"] is True
    assert payload["paper_only"] is True
    assert history_path.read_bytes() == before


def test_decision_board_route_is_get_only_and_returns_authoritative_payload() -> None:
    from scripts.server.routes.decision_board import DecisionBoardRouteDeps, create_decision_board_blueprint

    inputs = _inputs()
    app = Flask(__name__)
    app.register_blueprint(
        create_decision_board_blueprint(
            DecisionBoardRouteDeps(
                load_watchlist=lambda: inputs["watchlist_payload"],
                load_portfolio=lambda: inputs["portfolio_payload"],
                load_equity_history=lambda: inputs["equity_history_payload"],
                load_benchmark=lambda: inputs["benchmark_payload"],
                load_profile=lambda: inputs["profile"],
                load_calendar_days=lambda: inputs["calendar_days"],
                strategy_version=lambda: inputs["strategy_version"],
                source_paths=lambda: inputs["source_paths"],
            )
        )
    )

    response = app.test_client().get("/api/decision-board")
    post_response = app.test_client().post("/api/decision-board")
    payload = response.get_json()

    assert response.status_code == 200
    assert payload["positions"][0]["unrealized_pnl"] == 50.0
    assert payload["return_series"]["items"][-1]["benchmark_return_pct"] == 2.0
    assert post_response.status_code == 405

