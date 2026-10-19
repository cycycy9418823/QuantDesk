from __future__ import annotations

import math
from pathlib import Path

import pandas as pd
import pytest

from quantdesk.backtest_metrics import downside_sortino
from quantdesk.research.backtest_overfitting import deflated_sharpe_ratio, minimum_track_record_length
from quantdesk.research.backtest_return_evidence import closed_trade_return_evidence
from quantdesk.research.risk_stat_validation import _annualized_metrics, _load_equity_csv
from quantdesk.research.backtest_statistical_validation import run_backtest_statistical_validation


def test_constant_losses_have_downside_risk() -> None:
    assert downside_sortino([-.01, -.01, -.01]) == pytest.approx(-math.sqrt(252))
    assert downside_sortino([.01, -.01, .02]) == pytest.approx(math.sqrt(252) * (.02 / 3) / math.sqrt(.0001 / 3))
    assert downside_sortino([.01, .02]) is None


def test_dsr_is_probability_with_observation_units() -> None:
    assert deflated_sharpe_ratio([0.0], 252) == pytest.approx(.5)
    assert .83 < deflated_sharpe_ratio([1.0], 252, skew=0, kurt=3) < .85
    annual = deflated_sharpe_ratio([.4, 1.2, .9], 504)
    per_period = deflated_sharpe_ratio([value / math.sqrt(252) for value in [.4, 1.2, .9]], 504, periods_per_year=1)
    assert 0 <= annual <= 1
    assert annual == pytest.approx(per_period)
    assert math.isnan(deflated_sharpe_ratio([1.0], 1))


def test_minimum_track_record_does_not_confuse_years_and_days() -> None:
    assert minimum_track_record_length(1.0, skew=0, kurt=3) == 3
    assert minimum_track_record_length(.5, skew=0, kurt=3) == 11
    assert minimum_track_record_length(0.0) is None


def test_cash_equity_scale_does_not_change_annualized_return() -> None:
    returns = pd.Series([.01, -.02, .03])
    equity = (1 + returns).cumprod()
    assert _annualized_metrics(returns, equity) == _annualized_metrics(returns, equity * 100_000)


def _fill(side: str, shares: int, gross: float, *, date: str = "2026-10-12", **extra: object) -> dict:
    return {"symbol": "600000", "side": side, "shares": shares, "gross_amount": gross,
            "commission": 5.0, "stamp_duty": .5 if side == "sell" else 0.0,
            "transfer_fee": .02, "slippage_cost": 1.0, "date": date, **extra}


def test_net_round_trip_counts_partial_exits_once_and_slippage_once() -> None:
    evidence = closed_trade_return_evidence([
        _fill("buy", 200, 2000, date="2026-10-09"),
        _fill("sell", 100, 1100), _fill("sell", 100, 1200),
        _fill("buy", 100, 1000, blocked=True),
        _fill("buy", 100, 900, symbol="000001"),
    ])
    assert evidence["closed_trade_count"] == 1
    assert evidence["blocked_fill_count"] == 1
    assert evidence["open_positions_excluded"] == ["000001"]
    assert evidence["trade_returns"] == pytest.approx([(2300 - 11.04 - 2005.02) / 2005.02])
    assert evidence["return_evidence"]["complete_fill_history"]


def test_missing_cost_or_entry_cannot_become_net_trade_sample() -> None:
    orphan = closed_trade_return_evidence([_fill("sell", 100, 1100)])
    assert not orphan["return_evidence"]["complete_fill_history"]
    assert orphan["trade_returns"] == []
    broken = _fill("buy", 100, 1000)
    del broken["commission"]
    assert not closed_trade_return_evidence([broken])["return_evidence"]["complete_fill_history"]


def test_return_evidence_is_not_cut_at_two_hundred_display_rows() -> None:
    fills = []
    for index in range(150):
        fills.extend([_fill("buy", 100, 1000), _fill("sell", 100, 1010)])
    evidence = closed_trade_return_evidence(fills)
    assert len(evidence["trade_returns"]) == 150
    assert evidence["executed_fill_count"] == 300


def test_zero_closed_positions_cannot_fall_back_to_buy_count_and_equity() -> None:
    result = run_backtest_statistical_validation({
        "trade_returns": [], "closed_trade_count": 0, "closed_trades": [],
        "return_evidence": {"complete_fill_history": True, "cost_coverage_complete": True},
        "metrics": {"number_of_trades": 40},
        "equity_curve": [{"equity": 100 * (1.02 ** index)} for index in range(41)]},
        iterations=40, write=False)
    assert result["status"] == "missing"
    assert result["sample_size"] == result["trade_count"] == 0
    assert result["backtest_result_source"]["return_source"] == "trade_returns"


def test_invalid_or_mismatched_authoritative_returns_cannot_shrink_denominator() -> None:
    for values, count in (([.02, -.001] * 20 + [None], 41), ([.02, -.001] * 20, 45)):
        result = run_backtest_statistical_validation({"trade_returns": values,
            "closed_trade_count": count, "return_evidence": {"complete_fill_history": True}},
            iterations=40, write=False)
        assert result["status"] == "insufficient_sample"
        assert result["trade_count"] == count
        assert "closed_trade_count_return_count_mismatch" in result["insufficient_sample_reasons"]


def test_malformed_fill_retains_partial_cost_evidence() -> None:
    broken = _fill("buy", 100, 1000, cost_coverage_complete=False,
                   cost_schedule="partial_historical_transfer_schedule_unavailable_before_20150801")
    del broken["commission"]
    evidence = closed_trade_return_evidence([broken])["return_evidence"]
    assert evidence["cost_coverage_complete"] is False
    assert evidence["cash_fees_included"] is False
    assert any("20150801" in reason for reason in evidence["warnings"])


def test_equity_returns_are_derived_after_sorting_and_first_value_is_not_a_return(tmp_path: Path) -> None:
    path = tmp_path / "equity.csv"
    path.write_text("date,equity\n2026-10-12,121\n2026-10-08,100\n2026-10-09,110\n", encoding="utf-8")
    frame, warnings = _load_equity_csv(path)
    assert list(frame["daily_return"].dropna()) == pytest.approx([.1, .1])
    assert (1 + frame["daily_return"].dropna()).prod() - 1 == pytest.approx(.21)
    assert pd.isna(frame["daily_return"].iloc[0])
    assert "daily_return_derived_from_equity" in warnings


@pytest.mark.parametrize("text,warning", [
    ("date,equity\n2026-10-12,100\n2026-10-12,110\n", "invalid_or_duplicate_equity_date"),
    ("date,equity\nunknown,100\n2026-10-12,110\n", "invalid_or_duplicate_equity_date"),
    ("date,equity\n2026-10-09,100\n2026-10-12,\n", "invalid_equity_value"),
    ("date,equity,daily_return\n2026-10-09,100,0\n2026-10-12,110,\n", "bad_numeric_values:1"),
])
def test_ambiguous_equity_series_is_not_silently_filled_or_deduplicated(tmp_path: Path, text: str, warning: str) -> None:
    path = tmp_path / "equity.csv"
    path.write_text(text, encoding="utf-8")
    frame, warnings = _load_equity_csv(path)
    assert frame.empty
    assert warning in warnings
