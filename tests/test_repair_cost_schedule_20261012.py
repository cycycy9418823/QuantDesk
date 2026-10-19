from __future__ import annotations

import pytest

from quantdesk.backtest import BacktestConfig, cost_assumption_fields, trade_costs_for
from quantdesk.research.backtest_return_evidence import closed_trade_return_evidence
from quantdesk.research.backtest_statistical_validation import bootstrap_metric_confidence_intervals


@pytest.mark.parametrize("day,stamp,transfer", [
    ("2022-04-28", 10.0, .20),
    ("2022-04-29", 10.0, .10),
    ("2023-08-27", 10.0, .10),
    ("2023-08-28", 5.0, .10),
])
def test_costs_use_actual_execution_date(day: str, stamp: float, transfer: float) -> None:
    costs = trade_costs_for("sell", 10_000, BacktestConfig(historical_costs=True), trade_date=day)
    assert costs["stamp_duty"] == pytest.approx(stamp)
    assert costs["transfer_fee"] == pytest.approx(transfer)
    assert cost_assumption_fields(costs)["cost_coverage_complete"] is True
    buy = trade_costs_for("buy", 10_000, BacktestConfig(historical_costs=True), trade_date=day)
    assert buy["stamp_duty"] == 0
    assert buy["transfer_fee"] == pytest.approx(transfer)


def test_dated_costs_require_date_and_respect_custom_fixed_rates() -> None:
    with pytest.raises(ValueError, match="require_execution_date"):
        trade_costs_for("sell", 10_000, BacktestConfig(historical_costs=True))
    costs = trade_costs_for("sell", 10_000, BacktestConfig(historical_costs=True, stamp_duty_rate=.003, transfer_fee_rate=.00008), trade_date="2022-04-28")
    assert costs["stamp_duty"] == 30
    assert costs["transfer_fee"] == pytest.approx(.8)
    # Existing library callers still get their stated fixed research assumptions.
    assert trade_costs_for("sell", 10_000, BacktestConfig())["stamp_duty"] == 5


def test_partial_history_cannot_look_like_complete_cost_evidence() -> None:
    fills = []
    for side, gross, day in (("buy", 10_000, "2014-01-02"), ("sell", 11_000, "2014-01-03")):
        costs = trade_costs_for(side, gross, BacktestConfig(historical_costs=True), trade_date=day)
        fills.append({"symbol": "600000", "side": side, "shares": 1000, "gross_amount": gross,
                      "date": day, "commission": costs["commission"], "stamp_duty": costs["stamp_duty"],
                      "transfer_fee": costs["transfer_fee"], "slippage_cost": 0,
                      **cost_assumption_fields(costs)})
    evidence = closed_trade_return_evidence(fills)
    assert evidence["closed_trade_count"] == 1
    assert evidence["return_evidence"]["complete_fill_history"] is True
    assert evidence["return_evidence"]["cost_coverage_complete"] is False
    assert any("20150801" in value for value in evidence["return_evidence"]["warnings"])


@pytest.mark.parametrize("slippage", ["bad", float("inf"), -1, True])
def test_invalid_cost_annotation_is_reported_instead_of_crashing(slippage: object) -> None:
    result = closed_trade_return_evidence([{"symbol": "600000", "side": "buy", "shares": 100,
        "gross_amount": 1000, "date": "2026-10-12", "commission": 5, "stamp_duty": 0,
        "transfer_fee": .01, "slippage_cost": slippage}])
    assert result["return_evidence"]["complete_fill_history"] is False


def test_unidentifiable_annual_interval_is_null_even_without_sample() -> None:
    assert bootstrap_metric_confidence_intervals([])["annual_return_ci"] == [None, None]
