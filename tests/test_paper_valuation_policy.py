from __future__ import annotations

from datetime import datetime
from pathlib import Path

from quantdesk.paper.adjustment_policy import (
    apply_paper_adjustment_policy,
    apply_suspension_valuation_policy,
    load_adjustment_factor_table,
)
from quantdesk.paper.paper_trading import update_paper_marks
from quantdesk.paper.position_management_engine import apply_conditional_execution_to_state


def test_paper_adjustment_policy_keeps_raw_cost_and_factor_reference(tmp_path: Path) -> None:
    meta = tmp_path / "data" / "metadata"
    meta.mkdir(parents=True)
    (meta / "adjustment_factors.csv").write_text(
        "symbol,trade_date,adj_factor,source\n"
        "600000,2026-01-02,1.0,unit\n"
        "600000,2026-06-30,2.0,unit\n",
        encoding="utf-8",
    )

    factors = load_adjustment_factor_table(meta)
    position = {
        "symbol": "600000",
        "entry_date": "2026-01-02",
        "entry_price": 10.0,
        "quantity": 100,
    }

    updated = apply_paper_adjustment_policy(
        position,
        latest_price=5.0,
        latest_date="2026-06-30",
        factors=factors,
    )

    assert updated["price_adjustment_status"] == "factor_reference_only"
    assert updated["price_adjustment_mode"] == "raw_price_recorded_shares"
    assert updated["entry_adj_factor"] == 1.0
    assert updated["latest_adj_factor"] == 2.0
    assert updated["factor_research_proxy"]["factor_ratio"] == 2.0
    assert updated["factor_research_proxy"]["included_in_equity"] is False
    assert "adjusted_avg_cost" not in updated
    assert "adjusted_quantity_for_valuation" not in updated
    assert updated["market_value"] == 500.0
    assert updated["unrealized_pnl"] == -500.0
    assert updated["return_pct"] == -50.0


def test_paper_adjustment_policy_warns_when_factor_missing(tmp_path: Path) -> None:
    factors = load_adjustment_factor_table(tmp_path)
    position = {"symbol": "600000", "entry_date": "2026-01-02", "entry_price": 10.0, "quantity": 100}

    updated = apply_paper_adjustment_policy(
        position,
        latest_price=12.0,
        latest_date="2026-06-30",
        factors=factors,
    )

    assert updated["price_adjustment_status"] == "missing_adj_factor"
    assert updated["price_adjustment_warning_level"] == "warning"
    assert updated["market_value"] == 1200.0
    assert updated["unrealized_pnl"] == 200.0


def test_adjustment_factor_loader_skips_malformed_rows_with_visible_warning(tmp_path: Path) -> None:
    meta = tmp_path / "data" / "metadata"
    meta.mkdir(parents=True)
    (meta / "adjustment_factors.csv").write_text(
        "symbol,trade_date,adj_factor,source\n"
        "600000,2026-01-02,1.0,unit\n"
        '"bad,row\n'
        "600000,2026-06-30,2.0,unit\n",
        encoding="utf-8-sig",
    )

    factors = load_adjustment_factor_table(meta)

    assert factors.factor_at_or_before("600000", "2026-06-30") == 1.0
    joined = "\n".join(factors.warnings)
    assert "csv_parse_failed:" in joined
    assert "csv_bad_lines_skipped:" in joined
    assert "adjustment_factor_load_failed:" not in joined


def test_requested_factor_symbols_filter_before_python_normalization(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from quantdesk.paper import adjustment_policy

    meta = tmp_path / "data" / "metadata"
    meta.mkdir(parents=True)
    noise = "".join(
        f"{index:06d},2026-01-02,1.0,unit\n"
        for index in range(1000)
    )
    (meta / "adjustment_factors.csv").write_text(
        "symbol,trade_date,adj_factor,source\n"
        + noise
        + "600000,2026-06-30,2.0,unit\n",
        encoding="utf-8",
    )

    calls = 0
    original = adjustment_policy._symbol

    def counting_symbol(value):
        nonlocal calls
        calls += 1
        return original(value)

    monkeypatch.setattr(adjustment_policy, "_symbol", counting_symbol)
    factors = adjustment_policy.load_adjustment_factor_table(
        meta,
        symbols=["600000"],
    )

    assert factors.factor_at_or_before("600000", "2026-06-30") == 2.0
    assert calls < 20


def test_suspended_position_uses_frozen_last_tradable_price() -> None:
    position = {
        "symbol": "600000",
        "quantity": 100,
        "latest_price": 9.8,
        "latest_date": "2026-06-28",
        "entry_price": 10.0,
    }
    quote = {
        "price": None,
        "date": "2026-07-01",
        "suspended": True,
        "suspend_type": "S",
        "suspend_reason": "unit test suspension",
    }

    updated = apply_suspension_valuation_policy(position, quote)

    assert updated["valuation_status"] == "suspended_frozen"
    assert updated["tradeability_status"] == "suspended"
    assert updated["tradable"] is False
    assert updated["can_sell"] is False
    assert updated["valuation_price_source"] == "last_tradable_price"
    assert updated["latest_price"] == 9.8
    assert updated["market_value"] == 980.0
    assert "suspended" in updated["valuation_warning"]


def test_conditional_position_management_marks_with_adjustment_factor(tmp_path: Path) -> None:
    meta = tmp_path / "data" / "metadata"
    meta.mkdir(parents=True)
    (meta / "adjustment_factors.csv").write_text(
        "symbol,trade_date,adj_factor,source\n"
        "600000,2026-01-02,1.0,unit\n"
        "600000,2026-06-30,2.0,unit\n",
        encoding="utf-8",
    )
    factors = load_adjustment_factor_table(meta)
    state = {
        "initial_cash": 100000.0,
        "cash": 99000.0,
        "positions": [
            {
                "symbol": "600000",
                "strategy_type": "long",
                "entry_date": "2026-01-02",
                "entry_price": 10.0,
                "avg_cost": 10.0,
                "quantity": 100,
                "latest_price": 10.0,
            }
        ],
        "closed_positions": [],
    }
    execution_payload = {
        "plan_id": "unit",
        "trade_date": "2026-06-30",
        "events": [],
        "no_fill": [],
        "blocked": [],
    }
    daily_dir = tmp_path / "data" / "daily"
    daily_dir.mkdir(parents=True)
    # Round 21 valuation gate: the close may enter valuation only when the
    # frame declares a raw price basis, matching canonical data/daily files.
    (daily_dir / "600000.csv").write_text(
        "date,close,adjust\n2026-06-30,5,raw\n", encoding="utf-8"
    )

    result = apply_conditional_execution_to_state(
        state,
        execution_payload,
        daily_dir=daily_dir,
        adjustment_factors=factors,
    )

    position = result["state"]["positions"][0]
    assert position["price_adjustment_status"] == "factor_reference_only"
    assert "adjusted_avg_cost" not in position
    assert "adjusted_quantity_for_valuation" not in position
    assert position["market_value"] == 500.0
    assert position["unrealized_pnl"] == -500.0


def test_manual_paper_marks_freeze_suspended_quote() -> None:
    portfolio = {
        "account": {"initial_cash": 100000.0, "cash": 99000.0},
        "positions": [
            {
                "symbol": "600000",
                "shares": 100,
                "buy_price": 10.0,
                "last_price": 9.8,
                "settled_price": 9.8,
            }
        ],
    }
    latest_prices = {
        "600000": {
            "price": None,
            "quote_date": "2026-07-01",
            "suspended": True,
            "suspend_type": "S",
            "suspend_reason": "unit suspension",
        }
    }

    updated = update_paper_marks(portfolio, latest_prices)

    row = updated["positions"][0]
    assert row["valuation_status"] == "suspended_frozen"
    assert row["tradeability_status"] == "suspended"
    assert row["tradable"] is False
    assert row["can_sell"] is False
    assert row["market_value"] == 980.0


def test_manual_paper_marks_label_stale_quote_valuation() -> None:
    portfolio = {
        "account": {"initial_cash": 100000.0, "cash": 99000.0},
        "positions": [
            {
                "symbol": "600000",
                "shares": 100,
                "buy_price": 10.0,
                "last_price": 9.8,
                "settled_price": 9.8,
            }
        ],
    }
    latest_prices = {
        "600000": {
            "price": 9.6,
            "quote_date": "2026-06-30",
            "source": "unit",
        }
    }

    updated = update_paper_marks(portfolio, latest_prices, now=datetime(2026, 7, 2, 16, 0))

    row = updated["positions"][0]
    assert row["settlement_status"] == "quote_stale"
    assert row["estimate_only"] is True
    assert row["valuation_status"] == "stale_quote_estimate"
    assert row["valuation_price_source"] == "stale_quote_price"
    assert row["tradeability_status"] == "quote_stale"
    assert row["tradable"] is False
    assert row["can_sell"] is False
    assert "stale" in row["valuation_warning"]
    assert row["market_value"] == 960.0
