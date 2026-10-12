from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import pytest

from quantdesk.data_truth.pit_st_delist_loader import DelistRecord, StDelistData
from quantdesk.paper import auto_paper_portfolio, delist_liquidation, paper_trading, position_management_engine


@pytest.fixture(autouse=True)
def isolated_paper_paths_and_clock(tmp_path: Path, monkeypatch):
    source_root = Path(__file__).resolve().parents[1]
    for name, value in list(vars(auto_paper_portfolio).items()):
        if isinstance(value, Path) and value.is_relative_to(source_root):
            monkeypatch.setattr(auto_paper_portfolio, name, tmp_path / value.relative_to(source_root))
    monkeypatch.setattr(auto_paper_portfolio, "infer_target_date", lambda target_date=None: (target_date or "2026-06-18", "fixture"))
    # These accounting/lot/T+1 cases keep their existing freshness fixture at
    # the position-specific adapter introduced by the formal intake change.
    monkeypatch.setattr(auto_paper_portfolio, "evaluate_paper_review_freshness", lambda _state, **_kwargs: auto_paper_portfolio.evaluate_data_freshness())


def test_paper_trading_blocks_limit_and_suspended_metadata(tmp_path: Path) -> None:
    path = tmp_path / "paper.json"

    blocked_buy = paper_trading.paper_buy("000001", "sample", 10, 100, path=path, metadata={"pct_chg": 9.9})
    assert blocked_buy["ok"] is False

    bought = paper_trading.paper_buy("000001", "sample", 10, 100, path=path, metadata={"trade_date": "2026-06-18"})
    assert bought["ok"] is True

    blocked_sell = paper_trading.paper_sell("000001", 9, 100, path=path, metadata={"trade_date": "2026-06-19", "pct_chg": -9.9})
    assert blocked_sell["ok"] is False

    blocked_suspended = paper_trading.paper_sell("000001", 9, 100, path=path, metadata={"trade_date": "2026-06-19", "suspended": True})
    assert blocked_suspended["ok"] is False


def test_paper_trading_limit_metadata_is_board_aware(tmp_path: Path) -> None:
    path = tmp_path / "paper.json"

    chinext_buy = paper_trading.paper_buy("300001", "sample", 10, 100, path=path, metadata={"pct_chg": 10.0})
    assert chinext_buy["ok"] is True

    st_buy = paper_trading.paper_buy("600001", "*ST sample", 10, 100, path=path, metadata={"pct_chg": 5.0})
    assert st_buy["ok"] is False


def test_paper_trading_uses_board_lot_size_for_star_market(tmp_path: Path) -> None:
    path = tmp_path / "paper.json"

    too_small = paper_trading.paper_buy(
        "688001",
        "sample",
        10,
        100,
        path=path,
        metadata={"trade_date": "2026-06-18", "pct_chg": 0.0},
    )
    assert too_small["ok"] is False
    assert "200" in too_small["message"]

    whole_star_lot = paper_trading.paper_buy(
        "688001",
        "sample",
        10,
        200,
        path=path,
        metadata={"trade_date": "2026-06-18", "pct_chg": 0.0},
    )
    assert whole_star_lot["ok"] is True

    partial_star_sell = paper_trading.paper_sell(
        "688001",
        11,
        100,
        path=path,
        metadata={"trade_date": "2026-06-19", "pct_chg": 0.0},
    )
    assert partial_star_sell["ok"] is False
    assert "200" in partial_star_sell["message"]

    full_star_sell = paper_trading.paper_sell(
        "688001",
        11,
        200,
        path=path,
        metadata={"trade_date": "2026-06-19", "pct_chg": 0.0},
    )
    assert full_star_sell["ok"] is True


def test_paper_trading_blocks_limit_up_from_local_daily_when_metadata_missing(tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / "paper.json"
    daily_dir = tmp_path / "daily"
    daily_dir.mkdir()
    monkeypatch.setattr(paper_trading, "DAILY_DIR", daily_dir, raising=False)
    pd.DataFrame(
        [
            {"date": "2026-06-18", "close": 10.0, "pct_chg": 2.0, "vol": 1000},
            {"date": "2026-06-19", "close": 11.0, "pct_chg": 9.9, "vol": 1000},
        ]
    ).to_csv(daily_dir / "000001.csv", index=False)

    blocked = paper_trading.paper_buy("000001", "sample", 11, 100, path=path)

    assert blocked["ok"] is False
    assert "涨停" in blocked["message"]


def test_paper_trading_blocks_limit_down_from_local_daily_when_metadata_missing(tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / "paper.json"
    daily_dir = tmp_path / "daily"
    daily_dir.mkdir()
    monkeypatch.setattr(paper_trading, "DAILY_DIR", daily_dir, raising=False)
    bought = paper_trading.paper_buy("000002", "sample", 10, 100, path=path, metadata={"trade_date": "2026-06-18"})
    assert bought["ok"] is True
    pd.DataFrame(
        [
            {"date": "2026-06-18", "close": 10.0, "pct_chg": 0.0, "vol": 1000},
            {"date": "2026-06-19", "close": 9.0, "pct_chg": -9.9, "vol": 1000},
        ]
    ).to_csv(daily_dir / "000002.csv", index=False)

    blocked = paper_trading.paper_sell("000002", 9, 100, path=path, metadata={"trade_date": "2026-06-19"})

    assert blocked["ok"] is False
    assert "跌停" in blocked["message"]


def test_paper_trading_daily_reader_closes_csv_handle(tmp_path: Path, monkeypatch) -> None:
    daily_dir = tmp_path / "daily"
    daily_dir.mkdir()
    daily_path = daily_dir / "000001.csv"
    daily_path.write_text(
        "date,close,pct_chg,vol\n"
        "2026-06-19,11.0,9.9,1000\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(paper_trading, "DAILY_DIR", daily_dir, raising=False)

    original_open = Path.open
    opened_handles = []

    def tracking_open(path: Path, *args, **kwargs):
        handle = original_open(path, *args, **kwargs)
        if path == daily_path:
            opened_handles.append(handle)
        return handle

    monkeypatch.setattr(Path, "open", tracking_open)

    metadata = paper_trading._augment_trade_metadata_from_local_daily(
        "000001",
        {},
        "2026-06-19",
    )

    assert metadata["pct_chg"] == 9.9
    assert opened_handles
    assert all(handle.closed for handle in opened_handles)


def test_auto_close_position_realized_pnl_uses_sell_net_amount() -> None:
    closed = auto_paper_portfolio._close_position(
        {"symbol": "000001", "quantity": 100, "entry_price": 10.0},
        latest_price=11.0,
        decision_date="2026-06-19",
        exit_reason="test",
    )

    assert closed["commission"] == 5.0
    assert closed["stamp_duty"] == 0.55
    assert closed["slippage"] == 0.55
    assert closed["realized_pnl"] == 93.9
    assert closed["entry_cost_basis_includes_buy_fee"] is False
    assert closed["entry_cost_basis_status"] == "legacy_or_unknown_entry_price"


def test_auto_paper_reconciliation_recomputes_detail_totals_and_preserves_legacy_adjustment() -> None:
    state = {
        "cash": 100.0,
        "market_value": 999.0,
        "total_equity": 1099.0,
        "realized_pnl": -6.0,
        "unrealized_pnl": 999.0,
        "positions": [
            {
                "symbol": "600611",
                "quantity": 100,
                "market_value": 200.0,
                "unrealized_pnl": 3.0,
                "management_state": "normal",
                "watch_status": "watch_exit",
            }
        ],
        "closed_positions": [{"symbol": "600272", "realized_pnl": 4.0}],
    }

    reconciled = auto_paper_portfolio.reconcile_paper_account(state)

    assert reconciled["market_value"] == 200.0
    assert reconciled["unrealized_pnl"] == 3.0
    assert reconciled["total_equity"] == 300.0
    assert reconciled["realized_pnl"] == -6.0
    assert reconciled["legacy_realized_adjustment"] == -10.0
    assert reconciled["positions"][0]["management_state"] == "normal"
    assert reconciled["positions"][0]["watch_status"] == "normal"
    assert "paper_account_top_level_drift_detected" in reconciled["warnings"]
    assert state["positions"][0]["watch_status"] == "watch_exit"
    assert "legacy_realized_adjustment" not in state


def test_latest_snapshot_writer_persists_canonical_position_statuses(tmp_path: Path, monkeypatch) -> None:
    latest_path = tmp_path / "auto_paper_portfolio_latest.json"
    monkeypatch.setattr(auto_paper_portfolio, "AUTO_PAPER_PORTFOLIO_LATEST_JSON", latest_path)
    snapshot = {
        "cash": 100.0,
        "positions": [
            {
                "symbol": "600611",
                "quantity": 100,
                "market_value": 200.0,
                "unrealized_pnl": 3.0,
                "management_state": "normal",
                "watch_status": "watch_exit",
            }
        ],
        "closed_positions": [],
    }

    normalized = auto_paper_portfolio._write_auto_paper_latest(snapshot)

    persisted = json.loads(latest_path.read_text(encoding="utf-8"))
    assert normalized["positions"][0]["watch_status"] == "normal"
    assert persisted["positions"][0]["watch_status"] == "normal"
    assert snapshot["positions"][0]["watch_status"] == "watch_exit"


def test_auto_paper_reconciliation_marks_malformed_structure_invalid() -> None:
    state = {
        "cash": 100.0,
        "positions": {"symbol": "600000", "market_value": 20.0},
        "closed_positions": ["not-a-closed-position"],
    }

    reconciled = auto_paper_portfolio.reconcile_paper_account(state)

    detail = reconciled["accounting_reconciliation"]
    assert detail["status"] == "invalid"
    assert detail["reason"] == "positions_not_list"
    assert detail["invalid_reason_codes"] == [
        "positions_not_list",
        "closed_positions_row_0_not_object",
    ]
    assert "paper_account_invalid" in reconciled["warnings"]
    assert state["positions"] == {"symbol": "600000", "market_value": 20.0}


def test_auto_paper_reconciliation_marks_unparseable_detail_numbers_invalid() -> None:
    reconciled = auto_paper_portfolio.reconcile_paper_account(
        {
            "cash": 100.0,
            "positions": [
                {
                    "symbol": "600000",
                    "quantity": 100,
                    "market_value": "not-a-number",
                    "unrealized_pnl": 2.0,
                }
            ],
            "closed_positions": [{"symbol": "600001", "realized_pnl": "NaN"}],
        }
    )

    detail = reconciled["accounting_reconciliation"]
    assert detail["status"] == "invalid"
    assert detail["invalid_reason_codes"] == [
        "positions_row_0_market_value_not_numeric",
        "closed_positions_row_0_realized_pnl_not_numeric",
    ]
    assert "paper_account_invalid:positions_row_0_market_value_not_numeric" in reconciled["warnings"]


def test_auto_paper_update_fails_closed_without_writing_malformed_state(
    tmp_path: Path,
    monkeypatch,
) -> None:
    state_path = tmp_path / "state" / "auto_paper_portfolio.json"
    state_path.parent.mkdir(parents=True)
    malformed = {
        "initial_cash": 100_000.0,
        "cash": 90_000.0,
        "positions": "truncated",
        "closed_positions": [],
    }
    state_path.write_text(json.dumps(malformed), encoding="utf-8")
    original = state_path.read_bytes()
    monkeypatch.setattr(auto_paper_portfolio, "AUTO_PAPER_PORTFOLIO_STATE_JSON", state_path)
    monkeypatch.setattr(
        auto_paper_portfolio,
        "evaluate_data_freshness",
        lambda: {
            "ok": True,
            "target_complete_trade_date": "2026-06-18",
            "can_review_yesterday_positions": True,
            "can_update_portfolio_valuation": True,
        },
    )
    monkeypatch.setattr(
        auto_paper_portfolio,
        "apply_delisting_liquidation",
        lambda state, **_kwargs: {
            "ok": True,
            "liquidated": 0,
            "coverage": "fixture",
            "warnings": [],
            "actions": [],
            "state": state,
        },
    )
    monkeypatch.setattr(
        auto_paper_portfolio,
        "_write_auto_paper_state",
        lambda _state: (_ for _ in ()).throw(
            AssertionError("malformed state reached the writer")
        ),
    )

    payload = auto_paper_portfolio._run_auto_paper_portfolio_unlocked(
        mode="update",
        auto_strategy_payload={
            "generated_at": "2026-06-18T15:30:00",
            "long_candidates": [],
            "board_candidates": [],
        },
        risk_gate_shadow_enabled=False,
    )

    assert payload["ok"] is False
    assert payload["status"] == "failed"
    assert payload["error"] == "paper_account_invalid"
    assert "positions_not_list" in payload["reason_codes"]
    assert state_path.read_bytes() == original


def test_auto_paper_state_writer_persists_reconciled_summary(tmp_path, monkeypatch) -> None:
    state_path = tmp_path / "state" / "auto_paper_portfolio.json"
    monkeypatch.setattr(auto_paper_portfolio, "AUTO_PAPER_PORTFOLIO_STATE_JSON", state_path)

    auto_paper_portfolio._write_auto_paper_state(
        {
            "cash": 50.0,
            "positions": [{"symbol": "000001", "market_value": 125.0, "unrealized_pnl": -2.5}],
            "closed_positions": [{"symbol": "000002", "realized_pnl": 4.0}],
            "actions": [],
            "realized_pnl": 999.0,
        }
    )

    persisted = json.loads(state_path.read_text(encoding="utf-8"))
    assert persisted["market_value"] == 125.0
    assert persisted["unrealized_pnl"] == -2.5
    assert persisted["total_equity"] == 175.0
    assert persisted["realized_pnl"] == 999.0
    assert persisted["legacy_realized_adjustment"] == 995.0


def test_auto_paper_summary_includes_explicit_legacy_realized_adjustment() -> None:
    summary = auto_paper_portfolio._summary(
        {
            "cash": 100.0,
            "initial_cash": 100.0,
            "positions": [],
            "closed_positions": [{"realized_pnl": 4.0}],
            "legacy_realized_adjustment": -10.0,
        },
        [],
    )

    assert summary["realized_pnl"] == -6.0
    assert summary["legacy_realized_adjustment"] == -10.0


def test_paper_trading_fees_use_unified_service_with_slippage() -> None:
    fees = paper_trading._paper_fees("SELL", 1100.0)

    assert fees["fee_model"] == "cn_a_share_unified"
    assert fees["commission"] == 5.0
    assert fees["stamp_duty"] == 0.55
    assert fees["slippage"] == 0.55
    assert fees["total_fee"] == 6.1


def test_manual_paper_trading_payloads_carry_explicit_no_real_order_boundary(tmp_path: Path) -> None:
    path = tmp_path / "paper.json"

    bought = paper_trading.paper_buy("000001", "sample", 10, 100, path=path, metadata={"trade_date": "2026-06-18"})
    buy_portfolio = bought["portfolio"]
    buy_position = buy_portfolio["positions"][0]
    buy_trade = buy_portfolio["trades"][0]

    sold = paper_trading.paper_sell("000001", 11, 100, path=path, metadata={"trade_date": "2026-06-19"})
    sell_portfolio = sold["portfolio"]
    sell_trade = sell_portfolio["trades"][-1]

    for row in (bought, buy_portfolio, buy_position, buy_trade, sold, sell_portfolio, sell_trade):
        assert row["is_real_order"] is False
        assert row["research_only"] is True
        assert row["paper_only"] is True
        assert row["no_broker"] is True


def test_auto_new_position_cost_basis_includes_buy_fee() -> None:
    position = auto_paper_portfolio._make_position(
        {"symbol": "000001", "name": "sample", "strategy_type": "long"},
        quantity=100,
        price=10.0,
        decision_date="2026-06-18",
    )

    assert position["execution_price"] == 10.0
    assert position["entry_total_fee"] == 5.5
    assert position["entry_cash_out"] == 1005.5
    assert position["entry_price"] == 10.055
    assert position["entry_cost_basis_includes_buy_fee"] is True
    assert position["unrealized_pnl"] == -5.5


def test_auto_paper_actions_carry_explicit_no_real_order_boundary() -> None:
    candidate = {"symbol": "000001", "name": "sample", "strategy_type": "long"}
    position = auto_paper_portfolio._make_position(candidate, quantity=100, price=10.0, decision_date="2026-06-18")
    actions = [
        auto_paper_portfolio._buy_action(candidate, 100, 10.0, "2026-06-18", 98_994.5),
        auto_paper_portfolio._block_action(candidate, "blocked", "2026-06-18"),
        auto_paper_portfolio._hold_action(position, 10.0, "2026-06-19", "hold"),
        auto_paper_portfolio._sell_action(position, 11.0, "2026-06-19", "risk review"),
        auto_paper_portfolio._close_position(position, 11.0, "2026-06-19", "risk review"),
    ]

    for action in actions:
        assert action["is_real_order"] is False
        assert action["research_only"] is True
        assert action["paper_only"] is True
        assert action["no_broker"] is True
    assert actions[3]["total_fee"] == 6.1
    assert actions[3]["pnl"] == 88.4
    assert actions[4]["entry_cost_basis_status"] == "fee_inclusive"


def test_auto_portfolio_blocks_star_candidate_when_default_quantity_is_below_board_lot(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from tests.test_b5_formal_candidate_intake import dump, make_formal_batch
    from tests.test_daily_candidate_refresh import _strategy_payload

    candidate = _strategy_payload("2026-06-18")["standard_candidates"][0]
    candidate.update(symbol="688001", name="sample", close=10.0)
    batch = make_formal_batch("2026-06-18", candidate=candidate)
    batch_path = tmp_path / "results/candidate_refresh/daily/2026-06-18.json"
    state_dir = tmp_path / "state"
    results_dir = tmp_path / "results"
    daily_dir = tmp_path / "data" / "daily"
    state_dir.mkdir(parents=True)
    results_dir.mkdir(parents=True)
    daily_dir.mkdir(parents=True)
    dump(batch_path, batch)
    monkeypatch.setattr(auto_paper_portfolio, "STATE_DIR", state_dir)
    monkeypatch.setattr(auto_paper_portfolio, "RESULTS_DIR", results_dir)
    monkeypatch.setattr(auto_paper_portfolio, "DAILY_DIR", daily_dir)
    monkeypatch.setattr(auto_paper_portfolio, "AUTO_PAPER_PORTFOLIO_STATE_JSON", state_dir / "auto_paper_portfolio.json")
    monkeypatch.setattr(auto_paper_portfolio, "AUTO_PAPER_PORTFOLIO_LATEST_JSON", results_dir / "auto_paper_portfolio_latest.json")
    monkeypatch.setattr(auto_paper_portfolio, "AUTO_PAPER_PORTFOLIO_HISTORY_JSON", results_dir / "auto_paper_portfolio_history.json")
    monkeypatch.setattr(
        auto_paper_portfolio,
        "evaluate_data_freshness",
        lambda: {
            "ok": True,
            "target_complete_trade_date": "2026-06-18",
            "can_review_yesterday_positions": True,
            "can_update_portfolio_valuation": True,
        },
    )

    payload = auto_paper_portfolio._run_auto_paper_portfolio_unlocked(
        mode="init",
        dry_run=True,
        force_reset=True,
        enable_intraday_swing=False,
        quantity_per_stock=100,
        formal_candidate_batch=batch,
        formal_candidate_batch_path=batch_path,
        formal_candidate_status="passed",
        auto_strategy_payload={
            "generated_at": "2026-06-18T15:30:00",
            "long_candidates": [
                {
                    "symbol": "688001",
                    "name": "sample",
                    "strategy_type": "long",
                    "source_item": {"close": 10.0, "latest_date": "2026-06-18"},
                }
            ],
            "board_candidates": [],
        },
    )

    assert payload["positions"] == []
    assert payload["daily_actions"][0]["action"] == "block"
    assert payload["daily_actions"][0]["symbol"] == "688001"
    assert "200" in payload["daily_actions"][0]["reason"]


def test_auto_portfolio_same_day_position_is_not_sold(tmp_path: Path, monkeypatch) -> None:
    state_dir = tmp_path / "state"
    results_dir = tmp_path / "results"
    daily_dir = tmp_path / "data" / "daily"
    state_dir.mkdir(parents=True)
    results_dir.mkdir(parents=True)
    daily_dir.mkdir(parents=True)
    monkeypatch.setattr(auto_paper_portfolio, "STATE_DIR", state_dir)
    monkeypatch.setattr(auto_paper_portfolio, "RESULTS_DIR", results_dir)
    monkeypatch.setattr(auto_paper_portfolio, "DAILY_DIR", daily_dir)
    monkeypatch.setattr(auto_paper_portfolio, "AUTO_PAPER_PORTFOLIO_STATE_JSON", state_dir / "auto_paper_portfolio.json")
    monkeypatch.setattr(auto_paper_portfolio, "AUTO_PAPER_PORTFOLIO_LATEST_JSON", results_dir / "auto_paper_portfolio_latest.json")
    monkeypatch.setattr(auto_paper_portfolio, "AUTO_PAPER_PORTFOLIO_HISTORY_JSON", results_dir / "auto_paper_portfolio_history.json")
    monkeypatch.setattr(
        auto_paper_portfolio,
        "evaluate_data_freshness",
        lambda: {
            "ok": True,
            "target_complete_trade_date": "2026-06-18",
            "can_review_yesterday_positions": True,
            "can_update_portfolio_valuation": True,
        },
    )
    (state_dir / "auto_paper_portfolio.json").write_text(
        json.dumps(
            {
                "initial_cash": 100000,
                "cash": 90000,
                "positions": [
                    {
                        "symbol": "000001",
                        "name": "sample",
                        "strategy_type": "long",
                        "quantity": 100,
                        "entry_date": "2026-06-18",
                        "entry_price": 10.0,
                        "latest_price": 10.0,
                    }
                ],
                "closed_positions": [],
                "daily_actions": [],
            }
        ),
        encoding="utf-8",
    )
    pd.DataFrame([{"date": "2026-06-18", "close": 8.0}]).to_csv(daily_dir / "000001.csv", index=False)

    payload = auto_paper_portfolio._run_auto_paper_portfolio_unlocked(
        mode="update",
        enable_intraday_swing=False,
        auto_strategy_payload={"generated_at": "2026-06-18T15:30:00", "long_candidates": [], "board_candidates": []},
    )

    assert payload["positions"][0]["symbol"] == "000001"
    assert payload["closed_positions"] == []
    assert any(action.get("t_plus_one_sell_blocked") for action in payload["daily_actions"])


def test_auto_portfolio_daily_quote_exposes_csv_warnings(tmp_path: Path, monkeypatch) -> None:
    state_dir = tmp_path / "state"
    results_dir = tmp_path / "results"
    daily_dir = tmp_path / "data" / "daily"
    state_dir.mkdir(parents=True)
    results_dir.mkdir(parents=True)
    daily_dir.mkdir(parents=True)
    monkeypatch.setattr(auto_paper_portfolio, "STATE_DIR", state_dir)
    monkeypatch.setattr(auto_paper_portfolio, "RESULTS_DIR", results_dir)
    monkeypatch.setattr(auto_paper_portfolio, "DAILY_DIR", daily_dir)
    monkeypatch.setattr(auto_paper_portfolio, "AUTO_PAPER_PORTFOLIO_STATE_JSON", state_dir / "auto_paper_portfolio.json")
    monkeypatch.setattr(auto_paper_portfolio, "AUTO_PAPER_PORTFOLIO_LATEST_JSON", results_dir / "auto_paper_portfolio_latest.json")
    monkeypatch.setattr(auto_paper_portfolio, "AUTO_PAPER_PORTFOLIO_HISTORY_JSON", results_dir / "auto_paper_portfolio_history.json")
    monkeypatch.setattr(
        auto_paper_portfolio,
        "evaluate_data_freshness",
        lambda: {
            "ok": True,
            "target_complete_trade_date": "2026-06-18",
            "can_review_yesterday_positions": True,
            "can_update_portfolio_valuation": True,
        },
    )
    (state_dir / "auto_paper_portfolio.json").write_text(
        json.dumps(
            {
                "initial_cash": 100000,
                "cash": 90000,
                "positions": [
                    {
                        "symbol": "000001",
                        "name": "sample",
                        "strategy_type": "long",
                        "quantity": 100,
                        "entry_date": "2026-06-17",
                        "entry_price": 10.0,
                        "latest_price": 10.0,
                    }
                ],
                "closed_positions": [],
                "daily_actions": [],
            }
        ),
        encoding="utf-8",
    )
    (daily_dir / "000001.csv").write_text(
        "date,close\n"
        "2026-06-18,10.5\n"
        "2026-06-19,11.0,unexpected_extra_field\n",
        encoding="utf-8",
    )

    payload = auto_paper_portfolio._run_auto_paper_portfolio_unlocked(
        mode="update",
        enable_intraday_swing=False,
        auto_strategy_payload={"generated_at": "2026-06-18T15:30:00", "long_candidates": [], "board_candidates": []},
    )

    assert payload["positions"][0]["latest_price"] == 10.5
    assert any("csv_bad_lines_skipped" in item for item in payload["warnings"])


def test_auto_portfolio_same_day_position_skips_intraday_swing_sell(tmp_path: Path, monkeypatch) -> None:
    state_dir = tmp_path / "state"
    results_dir = tmp_path / "results"
    daily_dir = tmp_path / "data" / "daily"
    state_dir.mkdir(parents=True)
    results_dir.mkdir(parents=True)
    daily_dir.mkdir(parents=True)
    monkeypatch.setattr(auto_paper_portfolio, "STATE_DIR", state_dir)
    monkeypatch.setattr(auto_paper_portfolio, "RESULTS_DIR", results_dir)
    monkeypatch.setattr(auto_paper_portfolio, "DAILY_DIR", daily_dir)
    monkeypatch.setattr(auto_paper_portfolio, "AUTO_PAPER_PORTFOLIO_STATE_JSON", state_dir / "auto_paper_portfolio.json")
    monkeypatch.setattr(auto_paper_portfolio, "AUTO_PAPER_PORTFOLIO_LATEST_JSON", results_dir / "auto_paper_portfolio_latest.json")
    monkeypatch.setattr(auto_paper_portfolio, "AUTO_PAPER_PORTFOLIO_HISTORY_JSON", results_dir / "auto_paper_portfolio_history.json")
    monkeypatch.setattr(
        auto_paper_portfolio,
        "evaluate_data_freshness",
        lambda: {
            "ok": True,
            "target_complete_trade_date": "2026-06-18",
            "can_review_yesterday_positions": True,
            "can_update_portfolio_valuation": True,
        },
    )
    monkeypatch.setattr(
        auto_paper_portfolio,
        "_simulate_swing_for_row",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("same-day T+1 holding should not simulate a sell")),
    )
    monkeypatch.setattr(
        auto_paper_portfolio,
        "build_conditional_trade_plan",
        lambda **_kwargs: {
            "plan_id": "same-day-t1-plan",
            "plan_date": "2026-06-18",
            "trade_date": "2026-06-18",
            "summary": {"new_candidate_plan_count": 0, "symbol_count": 1},
            "symbols": [],
        },
    )
    # Round 13 起 update 只重放归档里到期的计划：没有到期计划就不会调用
    # simulate_conditional_plan_file，本用例也就拿不到那笔待拦的 stop_loss。
    # 因此这里必须提供一份"已到期"的归档计划。
    monkeypatch.setattr(
        auto_paper_portfolio,
        "load_archived_conditional_plan",
        lambda *_args, **_kwargs: {
            "status": "passed",
            "exists": True,
            "plan_id": "same-day-t1-plan",
            "plan": {
                "plan_id": "same-day-t1-plan",
                "plan_date": "2026-06-17",
                "trade_date": "2026-06-18",
                "symbols": [],
            },
        },
    )
    monkeypatch.setattr(
        auto_paper_portfolio,
        "simulate_conditional_plan_file",
        lambda *_args, **_kwargs: {
            "plan_id": "same-day-t1-plan",
            "trade_date": "2026-06-18",
            "summary": {"event_count": 1, "no_fill_count": 0, "missing_intraday_count": 0},
            "events": [
                {
                    "plan_id": "same-day-t1-plan",
                    "symbol": "000001",
                    "name": "sample",
                    "strategy_type": "long",
                    "action": "stop_loss",
                    "trigger_price": 8.0,
                    "quantity": 100,
                    "is_reliable": True,
                    "trade_date": "2026-06-18",
                }
            ],
            "no_fill": [],
            "blocked": [],
        },
    )
    (state_dir / "auto_paper_portfolio.json").write_text(
        json.dumps(
            {
                "initial_cash": 100000,
                "cash": 90000,
                "positions": [
                    {
                        "symbol": "000001",
                        "name": "sample",
                        "strategy_type": "long",
                        "quantity": 100,
                        "entry_date": "2026-06-18",
                        "entry_price": 10.0,
                        "latest_price": 10.0,
                    }
                ],
                "closed_positions": [],
                "daily_actions": [],
            }
        ),
        encoding="utf-8",
    )
    pd.DataFrame([{"date": "2026-06-18", "close": 8.0}]).to_csv(daily_dir / "000001.csv", index=False)

    payload = auto_paper_portfolio._run_auto_paper_portfolio_unlocked(
        mode="update",
        enable_intraday_swing=True,
        dry_run=True,
        trade_date="2026-06-18",
        auto_strategy_payload={"generated_at": "2026-06-18T15:30:00", "long_candidates": [], "board_candidates": []},
    )

    assert payload["positions"][0]["symbol"] == "000001"
    assert payload["closed_positions"] == []
    assert any(action.get("t_plus_one_sell_blocked") for action in payload["daily_actions"])


def test_auto_portfolio_applies_delist_liquidation_summary_without_state_recursion(tmp_path: Path, monkeypatch) -> None:
    state_dir = tmp_path / "state"
    results_dir = tmp_path / "results"
    daily_dir = tmp_path / "data" / "daily"
    state_dir.mkdir(parents=True)
    results_dir.mkdir(parents=True)
    daily_dir.mkdir(parents=True)
    monkeypatch.setattr(auto_paper_portfolio, "STATE_DIR", state_dir)
    monkeypatch.setattr(auto_paper_portfolio, "RESULTS_DIR", results_dir)
    monkeypatch.setattr(auto_paper_portfolio, "DAILY_DIR", daily_dir)
    monkeypatch.setattr(auto_paper_portfolio, "AUTO_PAPER_PORTFOLIO_STATE_JSON", state_dir / "auto_paper_portfolio.json")
    monkeypatch.setattr(auto_paper_portfolio, "AUTO_PAPER_PORTFOLIO_LATEST_JSON", results_dir / "auto_paper_portfolio_latest.json")
    monkeypatch.setattr(auto_paper_portfolio, "AUTO_PAPER_PORTFOLIO_HISTORY_JSON", results_dir / "auto_paper_portfolio_history.json")
    monkeypatch.setattr(
        auto_paper_portfolio,
        "evaluate_data_freshness",
        lambda: {
            "ok": True,
            "target_complete_trade_date": "2026-06-18",
            "can_review_yesterday_positions": True,
            "can_update_portfolio_valuation": True,
        },
    )

    def fake_liquidation(state: dict, *, as_of: str) -> dict:
        updated = dict(state)
        updated["cash"] = 90100.0
        updated["positions"] = []
        updated["closed_positions"] = [
            {"symbol": "000002", "status": "closed", "exit_reason": "delisted"}
        ]
        return {
            "ok": True,
            "liquidated": 1,
            "coverage": "local_csv",
            "warnings": [],
            "state": updated,
            "actions": [{"action": "delist_liquidation", "symbol": "000002", "is_real_order": False}],
        }

    monkeypatch.setattr(auto_paper_portfolio, "apply_delisting_liquidation", fake_liquidation)
    (state_dir / "auto_paper_portfolio.json").write_text(
        json.dumps(
            {
                "initial_cash": 100000,
                "cash": 90000,
                "positions": [{"symbol": "000002", "name": "delisted", "quantity": 100, "entry_price": 10.0}],
                "closed_positions": [],
                "daily_actions": [],
            }
        ),
        encoding="utf-8",
    )

    payload = auto_paper_portfolio._run_auto_paper_portfolio_unlocked(
        mode="update",
        enable_intraday_swing=False,
        auto_strategy_payload={"generated_at": "2026-06-18T15:30:00", "long_candidates": [], "board_candidates": []},
    )

    assert payload["positions"] == []
    assert payload["closed_positions"][0]["symbol"] == "000002"
    assert payload["delist_liquidation"]["liquidated"] == 1
    assert "state" not in payload["delist_liquidation"]
    assert any(action.get("action") == "delist_liquidation" for action in payload["daily_actions"])


def test_delist_liquidation_payloads_are_explicitly_paper_only() -> None:
    data = StDelistData(
        delist_records={
            "000002": DelistRecord(
                symbol="000002",
                name="delisted",
                delist_date="2026-06-18",
                last_trade_date="2026-06-17",
                delist_reason="test",
            )
        },
        delist_source="local_csv",
    )

    result = delist_liquidation.apply_delisting_liquidation(
        {
            "cash": 1000.0,
            "positions": [{"symbol": "000002", "name": "delisted", "quantity": 100, "avg_cost": 10.0, "last_price": 8.0}],
            "closed_positions": [],
        },
        as_of="2026-06-19",
        data=data,
        # 1A: liquidation needs an observed last-tradable close (no invented 75% price).
        last_close_lookup={"000002": 8.0},
    )

    for row in [result, result["state"], result["actions"][0], result["state"]["closed_positions"][0]]:
        assert row["is_real_order"] is False
        assert row["research_only"] is True
        assert row["paper_only"] is True
        assert row["no_broker"] is True

    pending = delist_liquidation.apply_delisting_liquidation(
        {"cash": 1000.0, "positions": [{"symbol": "000002", "quantity": 100, "avg_cost": 10.0}],
         "closed_positions": []},
        as_of="2026-06-19",
        data=data,
    )
    for row in [pending, pending["state"], pending["actions"][0]]:
        assert row["is_real_order"] is False and row["paper_only"] is True and row["no_broker"] is True


def test_conditional_execution_applies_unified_trade_costs() -> None:
    bought = position_management_engine.apply_conditional_execution_to_state(
        {"initial_cash": 10000.0, "cash": 10000.0, "positions": [], "closed_positions": []},
        {
            "plan_id": "fee-test-buy",
            "trade_date": "2026-06-19",
            "events": [
                {
                    "action": "buy",
                    "symbol": "000001",
                    "name": "sample",
                    "strategy_type": "long",
                    "is_reliable": True,
                    "trigger_price": 10.0,
                    "quantity": 100,
                    "trade_date": "2026-06-19",
                }
            ],
        },
    )

    buy_state = bought["state"]
    assert buy_state["cash"] == 8994.5
    assert buy_state["positions"][0]["avg_cost"] == 10.055
    assert buy_state["actions"][0]["total_fee"] == 5.5
    assert buy_state["actions"][0]["cash_out"] == 1005.5

    sold = position_management_engine.apply_conditional_execution_to_state(
        {
            "initial_cash": 10000.0,
            "cash": 0.0,
            "positions": [
                {
                    "symbol": "000001",
                    "name": "sample",
                    "strategy_type": "long",
                    "quantity": 100,
                    "sellable_quantity": 100,
                    "avg_cost": 10.055,
                    "entry_price": 10.055,
                }
            ],
            "closed_positions": [],
        },
        {
            "plan_id": "fee-test-sell",
            "trade_date": "2026-06-20",
            "events": [
                {
                    "action": "sell_all",
                    "symbol": "000001",
                    "name": "sample",
                    "strategy_type": "long",
                    "is_reliable": True,
                    "trigger_price": 11.0,
                    "quantity": 100,
                    "trade_date": "2026-06-20",
                }
            ],
        },
    )

    sell_state = sold["state"]
    assert sell_state["cash"] == 1093.9
    assert sell_state["actions"][0]["total_fee"] == 6.1
    assert sell_state["actions"][0]["cash_in"] == 1093.9
    assert sell_state["actions"][0]["pnl"] == 88.4
    assert sell_state["closed_positions"][0]["realized_pnl"] == 88.4


def test_conditional_execution_blocks_same_day_entry_without_sellable_quantity() -> None:
    applied = position_management_engine.apply_conditional_execution_to_state(
        {
            "initial_cash": 10000.0,
            "cash": 0.0,
            "positions": [
                {
                    "symbol": "000001",
                    "name": "sample",
                    "strategy_type": "long",
                    "quantity": 100,
                    "entry_date": "2026-06-19",
                    "entry_price": 10.0,
                }
            ],
            "closed_positions": [],
        },
        {
            "plan_id": "same-day-t1",
            "trade_date": "2026-06-19",
            "events": [
                {
                    "action": "stop_loss",
                    "symbol": "000001",
                    "name": "sample",
                    "strategy_type": "long",
                    "trigger_price": 9.0,
                    "quantity": 100,
                    "is_reliable": True,
                    "trade_date": "2026-06-19",
                }
            ],
            "no_fill": [],
            "blocked": [],
        },
        daily_dir=Path("__missing_daily__"),
    )

    assert applied["state"]["closed_positions"] == []
    action = applied["actions"][0]
    assert action["action"] == "block"
    assert action["quantity"] == 0
    assert action["t_plus_one_sell_blocked"] is True
    position = applied["state"]["positions"][0]
    assert position["sellable_quantity"] == 0
    assert position["frozen_quantity"] == 100


def test_conditional_execution_daily_valuation_exposes_csv_warnings(tmp_path: Path) -> None:
    daily_dir = tmp_path / "daily"
    daily_dir.mkdir()
    (daily_dir / "000001.csv").write_text(
        "date,close,adjust\n"
        "2026-06-19,12.0,raw\n"
        "2026-06-20,13.0,raw,unexpected_extra_field\n",
        encoding="utf-8",
    )

    applied = position_management_engine.apply_conditional_execution_to_state(
        {
            "initial_cash": 10000.0,
            "cash": 9000.0,
            "positions": [
                {
                    "symbol": "000001",
                    "name": "sample",
                    "strategy_type": "long",
                    "quantity": 100,
                    "sellable_quantity": 100,
                    "avg_cost": 10.0,
                    "entry_price": 10.0,
                    "latest_price": 10.0,
                }
            ],
            "closed_positions": [],
        },
        {"plan_id": "valuation-warning", "trade_date": "2026-06-19", "events": [], "no_fill": [], "blocked": []},
        daily_dir=daily_dir,
    )

    position = applied["state"]["positions"][0]
    assert position["latest_price"] == 12.0
    assert any("csv_bad_lines_skipped" in item for item in applied["state"]["warnings"])
