from __future__ import annotations

import json
from pathlib import Path

import pandas as pd

from quantdesk.candidate_monitoring import (
    CANDIDATE_ID,
    PROHIBITED_USAGE,
    refresh_candidate_monitoring,
)
from quantdesk.research_candidate_registry import build_registry

ROOT = Path(__file__).resolve().parents[1]


def _write_current_files(root: Path) -> dict[str, Path]:
    results = root / "results"
    results.mkdir(parents=True, exist_ok=True)
    card = {
        "display_name": "Deterministic Candidate Fixture",
        "component_weights": {"low_vol_canonical": 0.5, "rev120_B_canonical": 0.5},
        "annualized_return": 0.08,
        "Sharpe": 0.7,
        "daily_MDD": -0.1,
        "monthly_MDD": -0.08,
        "annualized_without_2024_2025": 0.07,
        "next_review_trigger": ["fixture_review"],
    }
    monitoring = {
        "monitoring_status": "candidate_watch",
        "metrics": {
            "Sortino": 0.9,
            "rolling_252d_return": 0.06,
            "main_risk_source": "rev120",
            "cost_stress_proxy": {
                "cost_plus_50bps_per_rebalance": {"annualized_return": 0.05}
            },
        },
        "warnings": [],
        "human_review_required_reasons": [],
    }
    spec = {
        "warning_conditions": ["fixture_warning"],
        "human_review_required_conditions": ["fixture_review"],
    }
    state = {"final_state": {"candidate_pool": [CANDIDATE_ID]}}
    for name, payload in (
        ("dashboard_candidate_card_portfolio_low_vol_rev120_latest.json", card),
        ("portfolio_candidate_monitoring_latest.json", monitoring),
        ("portfolio_candidate_monitoring_spec_latest.json", spec),
        ("strategy_pool_state_latest.json", state),
    ):
        (results / name).write_text(json.dumps(payload), encoding="utf-8")

    registry_path = results / "research_candidate_registry_latest.json"
    registry_path.write_text(
        json.dumps(build_registry(root), ensure_ascii=False),
        encoding="utf-8",
    )
    dates = pd.bdate_range("2025-01-02", periods=300)
    row_numbers = pd.Series(range(len(dates)))
    cash_flags = row_numbers.mod(50).eq(0)
    low_returns = pd.Series(0.0008, index=row_numbers.index).mask(
        row_numbers.mod(7).eq(0), -0.0004
    )
    rev_returns = pd.Series(0.0010, index=row_numbers.index).mask(
        row_numbers.mod(11).eq(0), -0.0007
    )
    low_returns = low_returns.mask(cash_flags, 0.0)
    rev_returns = rev_returns.mask(cash_flags, 0.0)
    combo_returns = 0.5 * low_returns + 0.5 * rev_returns
    combo_equity = (1.0 + combo_returns).cumprod()
    portfolio_path = results / "portfolio_daily_equity_rebuild_latest.csv"
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
    ).to_csv(portfolio_path, index=False)
    low_path = results / "low_vol_canonical_daily_equity_latest.csv"
    rev_path = results / "rev120_bcanonical_daily_equity_latest.csv"
    pd.DataFrame({"date": dates, "daily_return": low_returns}).to_csv(
        low_path, index=False
    )
    pd.DataFrame({"date": dates, "daily_return": rev_returns}).to_csv(
        rev_path, index=False
    )
    return {
        "registry": registry_path,
        "portfolio": portfolio_path,
        "low_vol": low_path,
        "rev120": rev_path,
    }


def test_registry_json_schema(tmp_path):
    _write_current_files(tmp_path)
    registry = build_registry(tmp_path)
    assert registry["registry_id"] == "research_candidate_registry"
    assert registry["research_only"] is True
    assert registry["candidate_pool"]
    candidate = registry["candidate_pool"][0]
    assert candidate["candidate_id"] == CANDIDATE_ID
    for usage in PROHIBITED_USAGE:
        assert usage in candidate["prohibited_usage"]


def test_monitoring_refresh_reads_current_files(tmp_path):
    _write_current_files(tmp_path)
    payload = refresh_candidate_monitoring(root=tmp_path)
    assert payload["status"] == "completed"
    assert payload["monitoring_status"] in {
        "candidate_healthy",
        "candidate_watch",
        "candidate_degraded",
        "human_review_required",
        "data_issue",
    }
    assert payload["research_only"] is True


def test_missing_input_graceful_fail(tmp_path):
    payload = refresh_candidate_monitoring(
        root=tmp_path,
        registry_path=tmp_path / "missing_registry.json",
        portfolio_csv=tmp_path / "missing_portfolio.csv",
        low_vol_csv=tmp_path / "missing_low.csv",
        rev120_csv=tmp_path / "missing_rev.csv",
    )
    assert payload["status"] == "data_issue"
    assert payload["monitoring_status"] == "data_issue"
    assert "missing required input files" in payload["failure_reasons"]


def test_monitoring_csv_inputs_handle_bom_and_surface_bad_line_warning(tmp_path):
    registry = tmp_path / "registry.json"
    registry.write_text(
        json.dumps({
            "candidate_pool": [
                {
                    "candidate_id": CANDIDATE_ID,
                    "current_metrics": {
                        "cost_stress_proxy": {
                            "cost_plus_50bps_per_rebalance": {"annualized_return": 0.12}
                        }
                    },
                }
            ]
        }),
        encoding="utf-8",
    )
    portfolio = tmp_path / "portfolio.csv"
    portfolio.write_text(
        "\ufeffportfolio_name,date,equity,daily_return,low_vol_daily_return,rev120_daily_return,cash_flag\n"
        "combo_50_50,2026-07-01,1.00,0.00,0.00,0.00,True\n"
        "combo_50_50,2026-07-02,1.01,0.01,0.004,0.006,False\n",
        encoding="utf-8",
    )
    low_vol = tmp_path / "low.csv"
    low_vol.write_text(
        "date,equity,daily_return\n"
        "2026-07-01,1.00,0.00\n"
        "2026-07-01,bad,extra,column\n"
        "2026-07-02,1.004,0.004\n",
        encoding="utf-8",
    )
    rev120 = tmp_path / "rev.csv"
    rev120.write_text(
        "date,equity,daily_return\n2026-07-01,1.00,0.00\n2026-07-02,1.006,0.006\n",
        encoding="utf-8",
    )

    payload = refresh_candidate_monitoring(
        root=ROOT,
        registry_path=registry,
        portfolio_csv=portfolio,
        low_vol_csv=low_vol,
        rev120_csv=rev120,
    )

    assert payload["status"] == "completed"
    assert payload["as_of_date"] == "2026-07-02"
    assert any(item.startswith("low_vol_csv:csv_parse_failed:") for item in payload["warnings"])
    assert any(item.startswith("low_vol_csv:csv_bad_lines_skipped:") for item in payload["warnings"])


def test_cash_day_return_rule_current_portfolio(tmp_path):
    paths = _write_current_files(tmp_path)
    df = pd.read_csv(paths["portfolio"])
    combo = df[df["portfolio_name"] == "combo_50_50"]
    bad = combo[combo["cash_flag"].astype(bool) & (combo["daily_return"].abs() > 1e-12)]
    assert len(bad) == 0


def test_no_buy_sell_rebalance_instruction_in_refresh(tmp_path):
    _write_current_files(tmp_path)
    payload = refresh_candidate_monitoring(root=tmp_path)
    text = json.dumps(payload, ensure_ascii=False).lower()
    forbidden = ["buy recommendation", "sell recommendation", "rebalance instruction", "broker_order"]
    for term in forbidden:
        assert term not in text
