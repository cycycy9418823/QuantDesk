"""fix(1d) #2: stats-validate must not pass on a stale file or a degenerate sample.

The 2026-10-09 20:03 production artefact said ``status=passed``. It was built
from ``results/backtest/latest.json`` (generated 2026-09-07) because the current
``backtest_latest.json`` carries no extractable returns and the loader silently
fell back to the older file. That sample had 189 daily returns with **no**
negative day and 2 trades, and the "actual" side of the permutation test was the
file's own ``annual_return`` (0.0217) compared against a compounded baseline of
the very same returns (0.0162) -- so it "beat" its own reshuffles.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest import mock

from quantdesk.research import backtest_statistical_validation as stats


def _production_like_equity_curve() -> list[dict[str, object]]:
    # 190 points, 8 small up-days, every other day flat: no negative day at all.
    up_days = {20, 40, 60, 80, 100, 120, 140, 160}
    equity = 100000.0
    rows = []
    for day in range(190):
        if day in up_days:
            equity *= 1.002
        rows.append({"date": f"2024-day-{day:03d}", "equity": round(equity, 4)})
    return rows


def test_primary_without_returns_reports_missing_instead_of_reading_stale_file(tmp_path: Path) -> None:
    backtest_dir = tmp_path / "results" / "backtest"
    backtest_dir.mkdir(parents=True)
    # Current file: metrics + BUY/SELL legs, but no extractable return series.
    (backtest_dir / "backtest_latest.json").write_text(
        json.dumps(
            {
                "generated_at": "2026-09-20T20:59:25",
                "metrics": {"annual_return": 0.0387},
                "trades": [{"side": "BUY", "executed_price": 10.0}],
            }
        ),
        encoding="utf-8",
    )
    # Stale file from an older run that happens to have an equity curve.
    (backtest_dir / "latest.json").write_text(
        json.dumps({"generated_at": "2026-09-07T00:30:04", "equity_curve": _production_like_equity_curve()}),
        encoding="utf-8",
    )
    with mock.patch.object(stats, "PROJECT_ROOT", tmp_path):
        result = stats.run_backtest_statistical_validation(iterations=50, seed=42, write=False)

    assert result["status"] == "missing"
    assert result["sample_size"] == 0
    assert "backtest_returns_missing" in result["warnings"]
    assert result["backtest_result_source"]["path"].endswith("backtest_latest.json")


def test_sample_without_a_negative_day_is_insufficient_not_passed() -> None:
    payload = {
        "metrics": {"annual_return": 0.021695120001048895, "number_of_trades": 2},
        "equity_curve": _production_like_equity_curve(),
    }
    result = stats.run_backtest_statistical_validation(payload, iterations=200, seed=42, write=False)

    assert result["sample_size"] == 189
    assert result["status"] == "insufficient_sample"
    assert "no_negative_return_in_sample" in result["insufficient_sample_reasons"]
    assert "trade_count_below_30" in result["insufficient_sample_reasons"]


def test_fewer_than_thirty_trades_is_insufficient_even_with_losses() -> None:
    returns = [0.03, -0.01] * 10  # 20 trades, has losses
    result = stats.run_backtest_statistical_validation({"trade_returns": returns}, iterations=50, seed=42, write=False)

    assert result["trade_count"] == 20
    assert result["status"] == "insufficient_sample"
    assert result["insufficient_sample_reasons"] == ["trade_count_below_30"]


def test_period_returns_without_a_trade_count_cannot_pass() -> None:
    returns = [0.004, -0.001, 0.003, -0.002] * 20
    result = stats.run_backtest_statistical_validation({"daily_returns": returns}, iterations=50, seed=42, write=False)

    assert result["trade_count"] is None
    assert result["status"] == "insufficient_sample"
    assert "trade_count_unknown" in result["insufficient_sample_reasons"]


def test_actual_metric_uses_the_baseline_aggregation_not_the_reported_annual_return() -> None:
    returns = [0.05, -0.01] * 20
    compounded = 1.0
    for value in returns:
        compounded *= 1.0 + value
    compounded -= 1.0
    payload = {"trade_returns": returns, "metrics": {"annual_return": 5.0}}
    result = stats.run_backtest_statistical_validation(payload, iterations=50, seed=42, write=False)

    assert abs(result["permutation_test"]["actual_metric"] - compounded) < 1e-6
    assert result["reported_metric"] == 5.0


def test_large_clean_sample_can_still_pass() -> None:
    returns = [0.05, -0.01] * 20  # 40 trades, wins larger than losses
    result = stats.run_backtest_statistical_validation({"trade_returns": returns}, iterations=200, seed=42, write=False)

    assert result["trade_count"] == 40
    assert result["status"] == "passed", result
