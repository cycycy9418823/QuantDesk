from __future__ import annotations

from pathlib import Path

import pandas as pd

from quantdesk.backtest_metrics import WindowMetrics, summarize_windows
from quantdesk.board_rules import BoardType, classify_board
from quantdesk.intraday_execution import simulate_intraday_plan
from quantdesk.paper import paper_trading
from quantdesk.risk.pre_trade_gate import PreTradeGateConfig, check_position_size, run_pre_trade_checks
from quantdesk.risk.smart_stop import compute_atr
from quantdesk.strategy_registry import get_strategy
from quantdesk.walk_forward import _simulate_trades


def test_st_board_rules_keep_st_board_type() -> None:
    rules = classify_board("600000", is_st=True)
    assert rules.board_type == BoardType.ST
    assert rules.limit_up_pct == 5.0


def test_compute_atr_allows_exact_period_rows() -> None:
    rows = [{"high": 11, "low": 9, "close": 10}] * 14
    assert compute_atr(rows, period=14) == 2.0


def test_walk_forward_executes_on_next_open_when_available() -> None:
    daily = pd.DataFrame(
        {
            "date": pd.to_datetime(["2026-01-01", "2026-01-02", "2026-01-05"]),
            "open": [100.0, 50.0, 70.0],
            "close": [100.0, 500.0, 700.0],
        }
    )
    signals = pd.DataFrame(
        {
            "date": pd.to_datetime(["2026-01-01", "2026-01-02"]),
            "signal": ["entry", "exit"],
        }
    )
    trades, _equity = _simulate_trades(daily, signals, cost_bps=0, slippage_bps=0, symbol="600000", strategy_name="demo")
    assert len(trades) == 1
    assert trades[0].entry_price == 50.0
    assert trades[0].exit_price == 70.0


def test_get_strategy_uses_yaml_override_before_builtin(tmp_path: Path) -> None:
    config = tmp_path / "strategy_demo.yaml"
    config.write_text(
        """
strategies:
  - strategy_id: ma_breakout
    name: YAML覆盖策略
    enabled: true
    entry_conditions:
      - field: close
        op: ">"
        value: 123
    exit_conditions: []
""".strip(),
        encoding="utf-8",
    )
    strategy = get_strategy("ma_breakout", path=config)
    assert strategy is not None
    assert strategy.name == "YAML覆盖策略"
    assert strategy.entry_conditions[0]["value"] == 123


def test_pre_trade_position_size_counts_existing_position() -> None:
    result = check_position_size(
        symbol="000001",
        order_amount=10_000,
        current_equity=100_000,
        current_positions={"000001": {"weight": 0.15}},
        config=PreTradeGateConfig(max_position_pct=0.20),
    )
    assert result.passed is False
    assert round(result.current_value, 4) == 0.25
    result_dict = result.to_dict()
    assert result_dict["is_real_order"] is False
    assert result_dict["research_only"] is True
    assert result_dict["paper_only"] is True
    assert result_dict["paper_trading_only"] is True
    assert result_dict["no_broker"] is True
    assert result_dict["no_real_orders"] is True
    assert result_dict["not_investment_advice"] is True
    assert result_dict["not_position_advice"] is True

    passed, results = run_pre_trade_checks(
        symbol="000001",
        order_amount=10_000,
        current_equity=100_000,
        current_positions={"000001": {"weight": 0.15, "industry": "银行"}},
        industry="银行",
        current_position_count=1,
        config=PreTradeGateConfig(max_position_pct=0.20),
    )
    assert passed is False
    assert any(item.check_name == "position_size" and item.passed is False for item in results)


def test_paper_trading_applies_t1_and_basic_fees(tmp_path: Path) -> None:
    path = tmp_path / "paper.json"
    bought = paper_trading.paper_buy("000001", "平安银行", 10, 100, path=path, metadata={"trade_date": "2026-06-18"})
    assert bought["ok"] is True
    portfolio = paper_trading.load_paper_portfolio(path)
    assert portfolio["positions"][0]["available_shares"] == 0
    assert portfolio["account"]["initial_cash"] == 10_000.0
    assert portfolio["account"]["profile_id"] == "user_10k"
    assert portfolio["account"]["scenario_id"] == "user_10k"
    assert portfolio["account"]["cash"] == 8994.5
    assert portfolio["trades"][-1]["commission"] == 5.0
    assert portfolio["trades"][-1]["stamp_duty"] == 0.0
    assert portfolio["trades"][-1]["slippage"] == 0.5
    assert portfolio["trades"][-1]["total_fee"] == 5.5

    same_day = paper_trading.paper_sell("000001", 11, 100, path=path, metadata={"trade_date": "2026-06-18"})
    assert same_day["ok"] is False
    assert "T+1" in same_day["message"]

    sold = paper_trading.paper_sell("000001", 11, 100, path=path, metadata={"trade_date": "2026-06-19"})
    assert sold["ok"] is True
    portfolio = paper_trading.load_paper_portfolio(path)
    assert portfolio["positions"] == []
    assert round(portfolio["account"]["cash"], 3) == 10088.4
    assert portfolio["trades"][-1]["total_fee"] == 6.1
    assert portfolio["trades"][-1]["stamp_duty"] == 0.55
    assert portfolio["trades"][-1]["slippage"] == 0.55


def test_intraday_stop_loss_missing_high_defaults_to_safe_trigger() -> None:
    plan = {
        "symbol": "000001",
        "sell_zones": [{"type": "stop_loss", "reason": "缺 price_high 时保守触发"}],
        "buy_zones": [],
        "quantity": 100,
    }
    intraday = pd.DataFrame([{"datetime": "2026-06-18 10:00:00", "high": 10.0, "low": 9.5, "close": 9.8}])
    result = simulate_intraday_plan(plan, intraday, holding=True)
    assert result["triggered"] is True
    assert result["trigger_type"] == "stop_loss"


def test_window_summary_rounds_fractional_win_count() -> None:
    summary = summarize_windows([WindowMetrics(win_rate=0.333, trades_count=3)])
    assert summary.aggregate_win_rate == 1 / 3


def test_evaluate_conditions_exit_uses_or_semantics() -> None:
    from quantdesk.strategy_conditions import evaluate_conditions

    conditions = [
        {"field": "close", "op": "<", "target_field": "ma20", "explain": "跌破MA20"},
        {"field": "ma20", "op": "<", "target_field": "ma60", "explain": "MA20跌破MA60"},
    ]
    # Only the first condition is satisfied (close < ma20), the second is not.
    row = {"close": 9.0, "ma20": 10.0, "ma60": 9.5}
    all_mode = evaluate_conditions(row, conditions)
    any_mode = evaluate_conditions(row, conditions, mode="any")

    assert all_mode["all_matched"] is False   # AND: not all satisfied -> would hold
    assert any_mode["any_matched"] is True
    assert any_mode["triggered"] is True       # OR: one satisfied -> exit fires


def test_ma_breakout_exit_triggers_on_single_condition() -> None:
    strategy = get_strategy("ma_breakout")
    assert strategy is not None
    from quantdesk.strategy_conditions import evaluate_conditions

    # close < ma20 holds but ma20 > ma60 (second exit rule fails). Under OR the
    # position should still exit on the broken MA20 instead of waiting for the
    # slow MA20<MA60 cross.
    row = {"close": 9.0, "ma20": 10.0, "ma60": 9.0}
    result = evaluate_conditions(row, list(strategy.exit_conditions), mode="any")
    assert result["triggered"] is True


def test_offline_news_is_neutral_and_allows_high_trust_tier() -> None:
    from quantdesk.trust_engine import assess_stock_trust

    row = {
        "code": "000001", "name": "样例银行", "score": 92,
        "data_warning_level": "OK", "data_freshness_status": "latest_complete_daily_bar",
        "news_warning_level": "OK", "news_status": "样例模式（未联网）",
        "close": 10.5, "ma20": 10.0, "ma60": 9.0,
        "risk_pct": 4.2, "amount_yi": 2.0, "amount_ratio": 1.3, "change_pct": 1.2,
    }
    a = assess_stock_trust(row, {"temp_score": 80})
    # Offline news is neutral (70), not the old 45 penalty,
    assert a.news_reliability_score == 70
    # so a strong offline candidate is no longer capped at 65 nor stuck low.
    assert a.trust_score > 65
    assert a.action_state in {"条件满足后再评估", "深度复核"}
