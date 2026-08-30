from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd

from quantdesk.candidate_monitoring import (
    ALLOWED_USAGE,
    CANDIDATE_ID,
    PROHIBITED_USAGE,
    refresh_candidate_monitoring,
)
from scripts.run_candidate_monitoring_failure_injection import run_failure_injection


def _write_core_inputs(root: Path) -> dict[str, Path]:
    """Create the complete deterministic input set without runtime artifacts."""

    results = root / "results"
    results.mkdir(parents=True, exist_ok=True)
    dates = pd.bdate_range("2025-01-02", periods=300)
    row_numbers = np.arange(len(dates))
    low_returns = np.where(row_numbers % 7 == 0, -0.0004, 0.0008)
    rev_returns = np.where(row_numbers % 11 == 0, -0.0007, 0.0010)
    cash_flags = row_numbers % 50 == 0
    low_returns = np.where(cash_flags, 0.0, low_returns)
    rev_returns = np.where(cash_flags, 0.0, rev_returns)
    combo_returns = 0.5 * low_returns + 0.5 * rev_returns
    combo_equity = np.cumprod(1.0 + combo_returns)

    paths = {
        "registry": results / "research_candidate_registry_latest.json",
        "portfolio": results / "portfolio_daily_equity_rebuild_latest.csv",
        "low_vol": results / "low_vol_canonical_daily_equity_latest.csv",
        "rev120": results / "rev120_bcanonical_daily_equity_latest.csv",
        "monitoring": results / "portfolio_candidate_monitoring_refresh_latest.json",
        "dashboard": results / "dashboard_candidate_cards_latest.json",
    }
    paths["registry"].write_text(
        json.dumps(
            {
                "registry_id": "research_candidate_registry",
                "research_only": True,
                "candidate_pool": [
                    {
                        "candidate_id": CANDIDATE_ID,
                        "allowed_usage": ALLOWED_USAGE,
                        "prohibited_usage": PROHIBITED_USAGE,
                        "source_reports": ["deterministic_test_fixture"],
                        "next_review_trigger": ["fixture_review"],
                        "current_metrics": {
                            "cost_stress_proxy": {
                                "cost_plus_50bps_per_rebalance": {
                                    "annualized_return": 0.08
                                }
                            }
                        },
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    pd.DataFrame(
        {
            "portfolio_name": "combo_50_50",
            "date": dates,
            "equity": combo_equity,
            "daily_return": combo_returns,
            "low_vol_daily_return": low_returns,
            "rev120_daily_return": rev_returns,
            "cash_flag": cash_flags,
        }
    ).to_csv(paths["portfolio"], index=False)
    pd.DataFrame(
        {
            "date": dates,
            "equity": np.cumprod(1.0 + low_returns),
            "daily_return": low_returns,
        }
    ).to_csv(paths["low_vol"], index=False)
    pd.DataFrame(
        {
            "date": dates,
            "equity": np.cumprod(1.0 + rev_returns),
            "daily_return": rev_returns,
        }
    ).to_csv(paths["rev120"], index=False)
    paths["monitoring"].write_text(
        json.dumps(
            {
                "candidate_id": CANDIDATE_ID,
                "research_only": True,
                "monitoring_status": "candidate_watch",
                "prohibited_usage": PROHIBITED_USAGE,
                "warnings": [],
                "human_review_required_reasons": [],
                "metrics": {
                    "annualized_return": 0.08,
                    "Sharpe": 0.7,
                    "Sortino": 0.9,
                    "daily_MDD": -0.1,
                    "monthly_MDD": -0.08,
                },
            }
        ),
        encoding="utf-8",
    )
    paths["dashboard"].write_text(
        json.dumps(
            {
                "research_only": True,
                "prohibited_usage": PROHIBITED_USAGE,
                "cards": [
                    {
                        "candidate_id": CANDIDATE_ID,
                        "monitoring_status": "candidate_watch",
                        "allowed_usage": ALLOWED_USAGE,
                        "prohibited_usage": PROHIBITED_USAGE,
                        "source_files": ["deterministic_test_fixture"],
                        "annualized_return": 0.08,
                        "sharpe": 0.7,
                        "sortino": 0.9,
                        "daily_mdd": -0.1,
                        "monthly_mdd": -0.08,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    return paths


def test_bad_numeric_graceful_fail(tmp_path):
    paths = _write_core_inputs(tmp_path)
    frame = pd.read_csv(paths["portfolio"])
    combo_idx = frame.index[frame["portfolio_name"].eq("combo_50_50")][20]
    frame.loc[combo_idx, "daily_return"] = np.inf
    frame.to_csv(paths["portfolio"], index=False)
    payload = refresh_candidate_monitoring(
        registry_path=paths["registry"],
        portfolio_csv=paths["portfolio"],
        low_vol_csv=paths["low_vol"],
        rev120_csv=paths["rev120"],
    )
    assert payload["monitoring_status"] == "human_review_required"
    assert payload["monitoring_status"] != "candidate_healthy"
    assert "unexpected NaN / infinite values" in payload["failure_reasons"]


def test_missing_file_does_not_mark_healthy(tmp_path):
    payload = refresh_candidate_monitoring(
        registry_path=tmp_path / "missing.json",
        portfolio_csv=tmp_path / "missing.csv",
        low_vol_csv=tmp_path / "missing_low.csv",
        rev120_csv=tmp_path / "missing_rev.csv",
    )
    assert payload["monitoring_status"] == "data_issue"
    assert payload["monitoring_status"] != "candidate_healthy"


def test_failure_injection_suite_does_not_mark_bad_inputs_healthy(tmp_path):
    _write_core_inputs(tmp_path)
    payload = run_failure_injection(tmp_path)
    assert payload["status"] == "passed"
    assert payload["failed_count"] == 0
    for scenario in payload["scenarios"]:
        assert scenario["not_candidate_healthy"] is True
