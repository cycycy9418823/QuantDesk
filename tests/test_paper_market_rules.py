from __future__ import annotations

from pathlib import Path


def test_paper_lot_helpers_round_down_without_changing_default_behavior() -> None:
    from quantdesk.paper.market_rules import (
        has_minimum_lot,
        is_whole_lot,
        round_down_to_lot,
        round_notional_down_to_lot_quantity,
    )

    assert round_down_to_lot(345) == 300
    assert round_down_to_lot(99) == 0
    assert round_down_to_lot(345, lot_size=200) == 200
    assert round_notional_down_to_lot_quantity(10_000, 7) == 1400
    assert round_notional_down_to_lot_quantity(999, 10) == 0
    assert round_notional_down_to_lot_quantity(10_000, 7, lot_size=200) == 1400
    assert round_notional_down_to_lot_quantity(10_000, 0) == 0
    assert has_minimum_lot(100)
    assert not has_minimum_lot(99)
    assert is_whole_lot(100)
    assert is_whole_lot(200, lot_size=200)
    assert not is_whole_lot(100, lot_size=200)
    assert not is_whole_lot(0)


def test_paper_trade_block_reason_is_board_aware_and_handles_suspension() -> None:
    from quantdesk.paper.market_rules import paper_trade_block_reason

    assert "停牌" in paper_trade_block_reason(
        "BUY",
        {"suspended": True},
        symbol="000001",
        name="sample",
    )
    assert "涨停" in paper_trade_block_reason(
        "BUY",
        {"pct_chg": 9.9},
        symbol="000001",
        name="sample",
    )
    assert paper_trade_block_reason(
        "BUY",
        {"pct_chg": 10.0},
        symbol="300001",
        name="sample",
    ) == ""
    assert "涨停" in paper_trade_block_reason(
        "BUY",
        {"pct_chg": 5.0},
        symbol="600001",
        name="*ST sample",
    )
    assert "跌停" in paper_trade_block_reason(
        "SELL",
        {"change_pct": -9.9},
        symbol="000001",
        name="sample",
    )


def test_metadata_is_st_does_not_match_star_text() -> None:
    from quantdesk.paper.market_rules import metadata_is_st

    assert metadata_is_st({}, "*ST sample")
    assert metadata_is_st({}, "*ST样本")
    assert metadata_is_st({}, "ST样本")
    assert metadata_is_st({"is_st": True}, "sample")
    assert not metadata_is_st({}, "STAR sample")


def test_paper_trading_uses_shared_trade_block_helper() -> None:
    project_root = Path(__file__).resolve().parents[1]
    source = (project_root / "quantdesk/paper/paper_trading.py").read_text(encoding="utf-8")

    assert "from quantdesk.board_rules import" not in source
    assert "def _metadata_trade_block" not in source
    assert "paper_trade_block_reason" in source


def test_paper_modules_use_shared_lot_rounding_helper() -> None:
    project_root = Path(__file__).resolve().parents[1]
    checked = [
        project_root / "quantdesk/paper/auto_paper_portfolio.py",
        project_root / "quantdesk/paper/conditional_trade_plan.py",
        project_root / "quantdesk/paper/paper_trading.py",
        project_root / "quantdesk/paper/position_management_engine.py",
        project_root / "quantdesk/paper/strategy_trade_replay.py",
    ]

    for path in checked:
        source = path.read_text(encoding="utf-8")
        assert "// 100 * 100" not in source, path
        assert "// 100)" not in source, path
        assert "% 100" not in source, path


def test_backtest_paths_use_shared_notional_lot_rounding_helper() -> None:
    project_root = Path(__file__).resolve().parents[1]
    checked = [
        project_root / "quantdesk/backtest.py",
        project_root / "quantdesk/research/backtest_portfolio_simulation.py",
        project_root / "quantdesk/walk_forward.py",
    ]

    for path in checked:
        source = path.read_text(encoding="utf-8")
        assert "round_notional_down_to_lot_quantity" in source, path
        assert "math.floor(max_cash / executed_price / lot_size) * lot_size" not in source, path
        assert "math.floor(max_value / executed_price / lot_size) * lot_size" not in source, path
        assert "math.floor(excess_value / requested_price / lot_size) * lot_size" not in source, path
        assert "math.floor(max_notional_by_liquidity / executed_price / lot_size)" not in source, path
        assert "/ exec_price / 100) * 100" not in source, path


def test_auto_strategy_sandbox_uses_shared_paper_lot_helper() -> None:
    project_root = Path(__file__).resolve().parents[1]
    source = (project_root / "quantdesk/auto_strategy_sandbox.py").read_text(encoding="utf-8")

    assert "round_notional_down_to_lot_quantity" in source
    assert "position_cap_amount / price / 100" not in source
    assert "quantity < 100" not in source


def test_research_and_position_paths_use_shared_lot_helpers() -> None:
    project_root = Path(__file__).resolve().parents[1]
    checked = {
        project_root / "quantdesk/position_planner.py": ["round_notional_down_to_lot_quantity"],
        project_root / "quantdesk/risk_position.py": ["round_down_to_lot", "round_notional_down_to_lot_quantity"],
        project_root / "quantdesk/research/combined_score_backtest.py": ["round_notional_down_to_lot_quantity"],
        project_root / "quantdesk/research/strategy_viability_runner.py": ["round_notional_down_to_lot_quantity"],
    }

    forbidden = [
        "value // price // 100 * 100",
        "risk_amount / stop_distance / 100",
        "total_capital * max_position_pct / current_price / 100",
        "math.floor((float(initial_cash) * float(max_position_pct)) / entry_price",
        "math.floor(target_cash / entry / lot_size) * lot_size",
    ]
    for path, required_snippets in checked.items():
        source = path.read_text(encoding="utf-8")
        for snippet in required_snippets:
            assert snippet in source, path
        for snippet in forbidden:
            assert snippet not in source, path


def test_active_paper_copy_does_not_claim_universal_100_share_lot() -> None:
    project_root = Path(__file__).resolve().parents[1]
    checked = {
        project_root / "quantdesk/paper/auto_paper_portfolio.py": [
            "默认每只新候选模拟买入 100 股",
        ],
        project_root / "quantdesk/paper/strategy_trade_replay.py": [
            "T+1、100 股整数手",
            "最后一个买入信号不足 100 股整数手",
        ],
        project_root / "quantdesk/cn_a_share_rules.py": [
            "100 股整数手、T+1",
        ],
        project_root / "app/static/js/daily_research.js": [
            "100股观察盈亏估算",
        ],
    }

    for path, forbidden_snippets in checked.items():
        source = path.read_text(encoding="utf-8")
        for snippet in forbidden_snippets:
            assert snippet not in source, f"{path}: {snippet}"
