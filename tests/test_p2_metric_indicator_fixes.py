from __future__ import annotations

import math

import pandas as pd

from quantdesk.backtest import BacktestConfig, BacktestMetrics, metrics_from_equity
from quantdesk.backtest_assumptions import backtest_assumptions_from_config, summary_rows_from_metrics
from quantdesk.backtest_metrics import compute_equity_metrics
from quantdesk.indicators import add_indicators
from quantdesk.stock_screener import ScreenCondition, screen_stocks
from quantdesk.walk_forward import _simulate_trades


def test_backtest_default_stamp_duty_warning_and_daily_positive_alias() -> None:
    config = BacktestConfig()
    assumptions = backtest_assumptions_from_config(config)
    legacy_config = BacktestConfig(cost_profile="legacy_pre_20230828")
    legacy_assumptions = backtest_assumptions_from_config(legacy_config)
    metrics = BacktestMetrics(
        annual_return=0.0,
        total_return=0.0,
        max_drawdown=0.0,
        sharpe=0.0,
        win_rate=0.25,
        days=3,
    )

    assert config.stamp_duty_rate == 0.0005
    assert assumptions["stamp_duty_rate_warning"] == ""
    assert legacy_config.stamp_duty_rate == 0.001
    assert "pre-2023-08-28" in legacy_assumptions["stamp_duty_rate_warning"]
    assert metrics.daily_positive_rate == 0.25
    rows = summary_rows_from_metrics(metrics, assumptions)
    metric_names = set(rows["metric"].astype(str))
    assert "daily_positive_rate" in metric_names
    assert "win_rate" not in metric_names


def test_equity_sharpe_subtracts_risk_free_rate() -> None:
    equity = [100.0, 101.0, 100.0, 102.0, 103.0]

    no_rf = compute_equity_metrics(equity, risk_free_annual=0.0)
    with_rf = compute_equity_metrics(equity)

    assert with_rf["sharpe"] < no_rf["sharpe"]


def test_core_backtest_metrics_reuse_equity_metric_conventions() -> None:
    equity = [100.0, 101.0, 100.0, 102.0, 103.0]
    expected = compute_equity_metrics(equity)
    actual = metrics_from_equity(
        pd.DataFrame({"equity": equity}),
        trades=[],
        assumptions={},
        initial_cash=equity[0],
    )

    assert actual.annual_return == expected["annual_return"]
    assert actual.max_drawdown == -expected["max_drawdown"]
    assert actual.sharpe == expected["sharpe"]
    assert actual.sortino == expected["sortino"]


def test_equity_metrics_include_sortino_ratio() -> None:
    equity = [100.0, 101.0, 99.0, 103.0, 102.0, 105.0]

    metrics = compute_equity_metrics(equity, risk_free_annual=0.0)

    assert "sortino" in metrics
    assert math.isfinite(metrics["sortino"])


def test_indicators_use_wilder_atr_and_population_bollinger_std() -> None:
    frame = pd.DataFrame(
        {
            "date": pd.date_range("2026-01-01", periods=25),
            "open": [10.0] * 25,
            "high": [11.0] * 25,
            "low": [9.0] * 25,
            "close": [10.0 + index * 0.1 for index in range(25)],
            "volume": [1000] * 25,
        }
    )

    out = add_indicators(frame)

    assert out["boll_upper"].iloc[:19].isna().all()
    assert out["atr14"].iloc[:13].isna().all()
    assert math.isfinite(float(out["atr14"].iloc[13]))


def test_walk_forward_trade_pnl_deducts_entry_exit_cost_and_stamp_duty() -> None:
    daily = pd.DataFrame(
        {
            "date": pd.to_datetime(["2026-01-01", "2026-01-02", "2026-01-05"]),
            "open": [10.0, 10.0, 11.0],
            "close": [10.0, 10.0, 11.0],
            "high": [10.1, 10.1, 11.1], "low": [9.9, 9.9, 10.9],
            "volume": 1000, "adjust": "raw",
        }
    )
    signals = pd.DataFrame(
        {
            "date": pd.to_datetime(["2026-01-01", "2026-01-02"]),
            "signal": ["entry", "exit"],
        }
    )

    trades, _equity = _simulate_trades(daily, signals, cost_bps=10.0, slippage_bps=0.0, initial_cash=10000.0)

    assert len(trades) == 1
    assert round(trades[0].cost, 2) == 23.85
    assert round(trades[0].pnl, 2) == 876.15


def test_walk_forward_trade_costs_apply_minimum_commission() -> None:
    daily = pd.DataFrame(
        {
            "date": pd.to_datetime(["2026-01-01", "2026-01-02", "2026-01-05"]),
            "open": [10.0, 10.0, 11.0],
            "close": [10.0, 10.0, 11.0],
            "high": [10.1, 10.1, 11.1], "low": [9.9, 9.9, 10.9],
            "volume": 1000, "adjust": "raw",
        }
    )
    signals = pd.DataFrame(
        {
            "date": pd.to_datetime(["2026-01-01", "2026-01-02"]),
            "signal": ["entry", "exit"],
        }
    )

    trades, _equity = _simulate_trades(daily, signals, cost_bps=1.0, slippage_bps=0.0, initial_cash=10000.0)

    assert len(trades) == 1
    assert round(trades[0].cost, 2) == 14.95
    assert round(trades[0].pnl, 2) == 885.05


def test_walk_forward_respects_max_position_pct() -> None:
    daily = pd.DataFrame(
        {
            "date": pd.to_datetime(["2026-01-01", "2026-01-02", "2026-01-05"]),
            "open": [10.0, 10.0, 11.0],
            "close": [10.0, 10.0, 11.0],
            "high": [10.1, 10.1, 11.1], "low": [9.9, 9.9, 10.9],
            "volume": 1000, "adjust": "raw",
        }
    )
    signals = pd.DataFrame(
        {
            "date": pd.to_datetime(["2026-01-01", "2026-01-02"]),
            "signal": ["entry", "exit"],
        }
    )

    trades, _equity = _simulate_trades(
        daily,
        signals,
        cost_bps=0.0,
        slippage_bps=0.0,
        initial_cash=10000.0,
        max_position_pct=0.5,
    )

    assert len(trades) == 1
    assert trades[0].quantity == 500


def test_screen_stocks_warns_for_missing_condition_fields() -> None:
    result = screen_stocks([{"symbol": "000001"}], [ScreenCondition("pe_ttm", "<", 30)])

    assert "excluded_by_missing_field:pe_ttm:1_stocks" in result.warnings


def test_screen_stocks_missing_field_warning_counts_full_scan_when_limited() -> None:
    result = screen_stocks(
        [
            {"symbol": "000001", "pe_ttm": 10},
            {"symbol": "000002"},
            {"symbol": "000003", "pe_ttm": 20},
            {"symbol": "000004"},
        ],
        [ScreenCondition("pe_ttm", "<", 30)],
        limit=1,
    )

    assert len(result.matched) == 1
    assert "结果超过 1 条，已截断" in result.warnings
    assert "excluded_by_missing_field:pe_ttm:2_stocks" in result.warnings


def test_rsi_first_value_not_shifted_by_fabricated_zero_delta() -> None:
    # 首日涨跌未知（diff 首元素 NaN），不得当成 0 参与 warmup。正确的 RSI
    # 需要 period 个真实价差，故首个非 NaN 值应落在 index=period（而非 period-1）。
    from quantdesk import technical_indicators

    period = 6
    close = pd.Series([float(10 + i) for i in range(period + 4)], dtype=float)
    rsi = technical_indicators.calculate_rsi(close, [period])[f"rsi{period}"]

    # 前 period 个必须是 NaN（含被 diff 置 NaN 的首日 + warmup 未满窗）。
    assert rsi.iloc[:period].isna().all()
    # 第 period 个（index=period）应产出首个有效 RSI。
    assert not math.isnan(rsi.iloc[period])
    # 全程上涨 → 首个有效 RSI 应为 100（无下跌，loss=0）。
    assert rsi.iloc[period] == 100.0
