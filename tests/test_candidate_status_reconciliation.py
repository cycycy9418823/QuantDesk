"""Retained active contracts; original mixed tests remain in the retirement evidence archive."""


from __future__ import annotations


import json


from pathlib import Path


from quantdesk.dashboard_candidate_cards import build_dashboard_cards


def write_candidate_status_fixture(root: Path) -> None:
    results = root / "results"
    results.mkdir(parents=True, exist_ok=True)
    (results / "research_candidate_registry_latest.json").write_text(
        json.dumps(
            {
                "candidate_pool": [
                    {
                        "candidate_id": "portfolio_low_vol_rev120_candidate",
                        "display_name": "Low-vol + Rev120 B Canonical Portfolio",
                        "status": "candidate_pool",
                        "primary_combo": "combo_50_50",
                        "component_weights": {"low_vol_canonical": 0.5, "rev120_B_canonical": 0.5},
                        "current_metrics": {
                            "annualized_return": 0.12,
                            "Sharpe": 0.9,
                            "Sortino": 1.1,
                            "daily_MDD": -0.22,
                            "monthly_MDD": -0.18,
                            "rolling_252d_return": 0.1,
                        },
                        "risk_summary": {"main_risk_source": "rev120"},
                        "next_review_trigger": ["data truth gap review"],
                    }
                ],
                "portfolio_watchlist": [],
                "strategy_watchlist": [],
                "source_files": ["results/research_candidate_registry_latest.json"],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    (results / "portfolio_candidate_monitoring_refresh_latest.json").write_text(
        json.dumps(
            {
                "monitoring_status": "candidate_healthy",
                "warnings": [],
                "human_review_required_reasons": [],
                "generated_at": "2026-07-04T00:00:00+08:00",
                "metrics": {
                    "data_freshness_days": 1,
                    "annualized_return": 0.12,
                    "Sharpe": 0.9,
                    "Sortino": 1.1,
                    "daily_MDD": -0.22,
                    "monthly_MDD": -0.18,
                    "rolling_252d_return": 0.1,
                    "main_risk_source": "rev120",
                    "cost_stress_proxy": {
                        "cost_plus_50bps_per_rebalance": {"annualized_return": 0.08}
                    },
                },
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    (results / "strategy_pool_state_latest.json").write_text(
        json.dumps({"final_state": {}}, ensure_ascii=False),
        encoding="utf-8",
    )
    (results / "candidate_data_truth_final_verdict_latest.json").write_text(
        json.dumps(
            {
                "final_conclusion": "candidate_valid_but_data_risk_high",
                "recommended_monitoring_status": "candidate_watch_due_to_data_gaps",
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    (results / "data_gap_tracker_latest.json").write_text(
        json.dumps({"p0_gap_count": 1, "gap_count": 1}, ensure_ascii=False),
        encoding="utf-8",
    )
    (results / "candidate_status_reconciliation_latest.json").write_text(
        json.dumps(
            {
                "status": "candidate_kept_but_data_watch",
                "performance_monitoring_status": "candidate_healthy",
                "data_truth_status": "candidate_valid_but_data_risk_high",
                "display_status": "candidate_watch_due_to_data_gaps",
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


def test_candidate_status_and_dashboard_data_risk_fields(tmp_path: Path) -> None:
    write_candidate_status_fixture(tmp_path)

    rec = json.loads((tmp_path / "results/candidate_status_reconciliation_latest.json").read_text(encoding="utf-8"))
    cards = build_dashboard_cards(root=tmp_path)
    card = cards["cards"][0]
    assert rec["status"] == "candidate_kept_but_data_watch"
    assert card["performance_monitoring_status"] == "candidate_healthy"
    assert card["data_truth_status"] == "candidate_valid_but_data_risk_high"
    assert card["display_status"] == "candidate_watch_due_to_data_gaps"
    assert card["candidate_upgrade_blocked_by_data_gaps"] is True
