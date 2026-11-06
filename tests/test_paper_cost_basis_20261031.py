"""Added positions use their recorded average for PnL, without changing exits."""
from __future__ import annotations

import copy
from pathlib import Path

import pandas as pd
import pytest

from quantdesk.paper import auto_paper_portfolio as paper
from quantdesk.paper.adjustment_policy import (
    AdjustmentFactorTable,
    apply_paper_adjustment_policy,
)
from tests.test_codex_audit_20261020_ledger import (
    TARGET,
    dump,
    isolated,
    state_with_position,
    update,
)


def _position() -> dict:
    return {
        "symbol": "600000", "name": "Synthetic added position",
        "strategy_type": "long", "quantity": 200,
        "entry_date": "2026-10-13", "entry_price": 10.0, "avg_cost": 15.0,
        "execution_price": 9.95, "entry_cash_out": 1000.0,
        "entry_cost_basis_includes_buy_fee": True,
        "latest_price": 10.0, "latest_date": "2026-10-14",
    }


def _factors(status: str, ratio: float = 1.0) -> AdjustmentFactorTable:
    rows = [] if status == "missing" else [
        (20261013, 1.0), (20261014 if status == "stale" else 20261019, ratio),
    ]
    return AdjustmentFactorTable(rows_by_symbol={"600000": rows})


@pytest.mark.parametrize("factor_status", ["missing", "stale", "fresh"])
@pytest.mark.parametrize("has_average", [True, False])
def test_marks_and_hold_actions_use_same_cost_without_hiding_factor_warnings(
    factor_status: str, has_average: bool,
) -> None:
    position = _position()
    if not has_average:
        position.pop("avg_cost")
    before = copy.deepcopy(position)
    expected_pnl = 1000.0 if has_average else 2000.0
    expected_return = 33.33 if has_average else 100.0
    factors = _factors(factor_status)
    valuation = apply_paper_adjustment_policy(
        position, latest_price=20.0, latest_date=TARGET, factors=factors,
    )
    marked = paper._mark_position(
        copy.deepcopy(position), 20.0, TARGET, "synthetic hold",
        adjustment_factors=factors,
        quote={"price": 20.0, "date": TARGET, "price_basis": "raw",
               "bar_valid": True, "source": "synthetic_fixture"},
    )
    for row in (valuation, marked):
        assert row["market_value"] == 4000.0
        assert row["unrealized_pnl"] == expected_pnl
        assert row["return_pct"] == row["unrealized_return_pct"] == expected_return
        for key in ("quantity", "entry_price", "entry_cash_out", "execution_price",
                    "entry_cost_basis_includes_buy_fee", "avg_cost"):
            assert row.get(key) == before.get(key)
        assert row["price_adjustment_status"] == {
            "missing": "missing_adj_factor", "stale": "stale_adj_factor", "fresh": "factor_reference_only",
        }[factor_status]
        assert row["price_adjustment_warning_level"] == "warning"
        assert row["price_adjustment_warning"]
        for action_fn in (paper._hold_action, paper._watch_exit_action):
            assert action_fn(row, 20.0, TARGET, "synthetic hold")["pnl"] == expected_pnl
    assert position == before


@pytest.mark.parametrize("has_average", [True, False])
def test_sale_profit_uses_average_but_cash_fees_and_original_cost_fields_are_preserved(
    has_average: bool,
) -> None:
    position = _position()
    if not has_average:
        position.pop("avg_cost")
    before = copy.deepcopy(position)
    sold = paper._sell_action(position, 20.0, TARGET, "synthetic exit")
    closed = paper._close_position(position, 20.0, TARGET, "synthetic exit")
    # The existing sell fee is 9 yuan. Only the PnL cost changes from 2000 to 3000.
    assert sold["pnl"] == closed["realized_pnl"] == (991.0 if has_average else 1991.0)
    for row in (sold, closed):
        assert row["gross_amount"] == 4000.0
        assert row["total_fee"] == 9.0
        assert row["net_amount"] == 3991.0
        assert row["quantity"] == 200
    assert closed["return_pct"] == (33.33 if has_average else 100.0)
    assert closed["entry_price"] == before["entry_price"]
    assert closed.get("avg_cost") == before.get("avg_cost")
    assert closed["entry_cash_out"] == before["entry_cash_out"]
    assert position == before


@pytest.mark.parametrize("empty_average", [None, 0.0, ""])
def test_legacy_empty_average_keeps_original_entry_cost(empty_average: object) -> None:
    position = {**_position(), "avg_cost": empty_average}
    marked = apply_paper_adjustment_policy(
        position, latest_price=20.0, latest_date=TARGET, factors=_factors("missing"),
    )
    assert marked["unrealized_pnl"] == 2000.0
    assert paper._hold_action(position, 20.0, TARGET, "legacy")["pnl"] == 2000.0
    assert paper._sell_action(position, 20.0, TARGET, "legacy")["pnl"] == 1991.0
    assert paper._close_position(position, 20.0, TARGET, "legacy")["realized_pnl"] == 1991.0


def test_current_factor_ratio_preserves_average_cost_and_original_share_record() -> None:
    position = _position()
    marked = apply_paper_adjustment_policy(
        position, latest_price=10.0, latest_date=TARGET, factors=_factors("fresh", ratio=2.0),
    )
    assert marked["price_adjustment_status"] == "factor_reference_only"
    assert marked["factor_research_proxy"]["factor_ratio"] == 2
    assert marked["factor_research_proxy"]["included_in_equity"] is False
    assert "adjusted_quantity_for_valuation" not in marked
    assert marked["market_value"] == 2000.0
    assert marked["unrealized_pnl"] == -1000.0
    assert marked["quantity"] == 200
    assert marked["avg_cost"] == 15.0
    assert marked["entry_price"] == 10.0


def test_observed_cost_difference_is_reproduced_from_tiny_detached_records() -> None:
    opened = {**_position(), "quantity": 500, "entry_price": 9.4047, "avg_cost": 9.3186}
    exited = {**_position(), "symbol": "600611", "entry_price": 4.8024, "avg_cost": 5.0025}
    assert paper._hold_action(opened, 10.02, TARGET, "detached diagnostic")["pnl"] == 350.70
    sold = paper._sell_action(exited, 4.41, TARGET, "detached diagnostic")
    closed = paper._close_position(exited, 4.41, TARGET, "detached diagnostic")
    assert sold["pnl"] == closed["realized_pnl"] == -124.38
    assert sold["net_amount"] == closed["net_amount"] == 876.12


@pytest.mark.parametrize("price,average,expected_action", [
    (11.0, 15.0, "watch_exit"),  # Average cost alone must not introduce a stop.
    (8.9, 5.0, "sell"),  # An average-cost gain must not suppress the existing stop.
])
def test_full_dry_run_preserves_entry_based_exit_cash_and_historical_records(
    isolated: Path, price: float, average: float, expected_action: str,
) -> None:
    state = state_with_position(isolated, price=price)
    position = state["positions"][0]
    position.update(entry_price=10.0, avg_cost=average, quantity=200,
                    entry_cash_out=1000.0, market_value=price * 200,
                    unrealized_pnl=round((price - average) * 200, 2))
    historical = {"symbol": "600999", "quantity": 0, "exit_quantity": 100,
                  "entry_price": 10.1234, "avg_cost": 10.4567, "exit_price": 11.0,
                  "realized_pnl": 12.34, "return_pct": 1.18, "status": "closed"}
    state.update(cash=100000 - average * 200, closed_positions=[historical],
                 legacy_realized_adjustment=-20.0)
    path = isolated / "data/daily/600121.csv"
    path.parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame([
        {"date": day, "symbol": "600121", "open": price, "high": price + .1,
         "low": price - .1, "close": price, "volume": 1000, "amount": price * 1000,
         "pre_close": price, "pct_chg": 0.0, "adjust": "raw"}
        for day in ("2026-10-14", TARGET)
    ]).to_csv(path, index=False)

    def run(candidate_state: dict) -> dict:
        dump(paper.AUTO_PAPER_PORTFOLIO_STATE_JSON, paper.reconcile_paper_account(candidate_state))
        before = paper.AUTO_PAPER_PORTFOLIO_STATE_JSON.read_bytes()
        result = update()
        assert result["ok"] is True
        assert paper.AUTO_PAPER_PORTFOLIO_STATE_JSON.read_bytes() == before
        assert result["closed_positions"][0] == historical
        assert result["legacy_realized_adjustment"] == -20.0
        return result

    legacy_state = copy.deepcopy(state)
    legacy_state["positions"][0].pop("avg_cost")
    legacy = run(legacy_state)
    averaged = run(state)
    for result in (legacy, averaged):
        assert [row["action"] for row in result["actions"]] == [expected_action]
    assert [{k: v for k, v in row.items() if k != "pnl"} for row in legacy["actions"]] == [
        {k: v for k, v in row.items() if k != "pnl"} for row in averaged["actions"]
    ]
    for key in ("cash", "market_value", "total_equity"):
        assert legacy["summary"][key] == averaged["summary"][key]
    if expected_action == "watch_exit":
        assert averaged["positions"][0]["unrealized_pnl"] == -800.0
        assert averaged["positions"][0]["return_pct"] == -26.67
        assert averaged["actions"][0]["pnl"] == -800.0
    else:
        assert not averaged["positions"]
        assert averaged["closed_positions"][-1]["realized_pnl"] == 773.22
        assert averaged["actions"][0]["pnl"] == 773.22
