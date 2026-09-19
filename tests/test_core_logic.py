from __future__ import annotations

import json
import math
import os
import sys
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
os.environ.setdefault("QUANTDESK_TEST_QUIET_IO", "1")
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import daily_quant_report as dqr  # noqa: E402


def sample_history(days: int = 180, shock_from: int | None = None, shock: float = 0.0) -> pd.DataFrame:
    dates = pd.date_range("2024-01-01", periods=days, freq="D")
    base = np.linspace(8.0, 13.0, days)
    wave = np.sin(np.arange(days) / 3) * 0.85
    close = base + wave
    if shock_from is not None:
        close[shock_from:] = close[shock_from:] + shock
    open_ = close * 0.995
    high = close * 1.025
    low = close * 0.975
    volume = np.linspace(1_000_000, 1_400_000, days)
    amount = close * volume
    return pd.DataFrame(
        {
            "日期": dates,
            "开盘": open_,
            "最高": high,
            "最低": low,
            "收盘": close,
            "成交量": volume,
            "成交额": amount,
            "换手率": np.linspace(1.0, 2.0, days),
        }
    )


def sample_spot(change_pct: float = 1.2) -> pd.Series:
    return pd.Series({"涨跌幅": change_pct, "成交额": 180_000_000, "换手率": 2.5})


def sample_end_date(days: int = 180) -> str:
    return sample_history(days)["日期"].max().strftime("%Y%m%d")


def tradeable_backtest_history(days: int = 190) -> pd.DataFrame:
    dates = pd.date_range("2024-01-01", periods=days, freq="D")
    close = np.concatenate(
        [
            np.linspace(8.0, 10.0, 125),
            np.linspace(10.2, 15.0, 35),
            np.linspace(14.6, 7.6, days - 160),
        ]
    )
    open_ = close * 0.99
    high = np.maximum(open_, close) * 1.02
    low = np.minimum(open_, close) * 0.98
    volume = np.full(days, 1_000_000.0)
    if days > 126:
        volume[126 : min(days, 160)] = 3_000_000.0
    amount = close * volume
    change_pct = pd.Series(close).ffill().pct_change(fill_method=None).fillna(0) * 100
    return pd.DataFrame(
        {
            "日期": dates,
            "开盘": open_,
            "最高": high,
            "最低": low,
            "收盘": close,
            "成交量": volume,
            "成交额": amount,
            "涨跌幅": change_pct,
            "换手率": np.linspace(1.0, 2.0, days),
        }
    )


def zero_cost_config(**overrides) -> dqr.BacktestConfig:
    values = {
        "commission_rate": 0.0,
        "min_commission": 0.0,
        "stamp_duty_rate": 0.0,
        "slippage_bps": 0.0,
    }
    values.update(overrides)
    return dqr.BacktestConfig(**values)


def zero_cost_portfolio_config(**overrides) -> dqr.PortfolioBacktestConfig:
    values = {
        "commission_rate": 0.0,
        "min_commission": 0.0,
        "stamp_duty_rate": 0.0,
        "slippage_bps": 0.0,
        "rebalance": "daily",
    }
    values.update(overrides)
    return dqr.PortfolioBacktestConfig(**values)


def shift_history_dates(hist: pd.DataFrame, days: int) -> pd.DataFrame:
    shifted = hist.copy()
    shifted["日期"] = pd.to_datetime(shifted["日期"]) + pd.Timedelta(days=days)
    return shifted


def first_trade(result: dqr.BacktestResult, side: str, blocked: bool | None = None) -> dict:
    for trade in result.trades:
        if trade["side"] != side:
            continue
        if blocked is not None and bool(trade["blocked"]) != blocked:
            continue
        return trade
    raise AssertionError(f"No {side} trade found")


def test_artifact_dir(name: str) -> Path:
    path = ROOT / "results" / "test_artifacts" / name
    path.mkdir(parents=True, exist_ok=True)
    return path


test_artifact_dir.__test__ = False


class DataNormalizeTests(unittest.TestCase):
    def test_normalize_daily_renames_numeric_dates_and_turnover(self) -> None:
        raw = pd.DataFrame(
            {
                "date": ["2024-01-02", "bad-date", "2024-01-02", "2024-01-03"],
                "open": ["10", "11", "12", "13"],
                "high": ["11", "12", "13", "14"],
                "low": ["9", "10", "11", "12"],
                "close": ["10.5", None, "12.5", "13.5"],
                "volume": ["1000", "1000", "1200", "1300"],
                "amount": ["10000", "10000", "12000", "13000"],
                "turnover": [0.1, 0.2, 0.3, 0.4],
            }
        )

        normalized = dqr.normalize_daily(raw, "000001")

        self.assertEqual(list(normalized["股票代码"].unique()), ["000001"])
        self.assertEqual(len(normalized), 2)
        self.assertEqual(float(normalized.loc[0, "收盘"]), 12.5)
        self.assertEqual(float(normalized.loc[0, "换手率"]), 30.0)
        self.assertTrue(pd.api.types.is_datetime64_any_dtype(normalized["日期"]))
        self.assertTrue(pd.api.types.is_numeric_dtype(normalized["成交额"]))


class IndicatorAndScoringTests(unittest.TestCase):
    def test_compute_base_features_matches_expected_moving_averages(self) -> None:
        hist = sample_history()
        feature = dqr.compute_base_features("000001", "测试股份", hist, sample_spot())

        self.assertIsNotNone(feature)
        assert feature is not None
        closes = hist["收盘"].astype(float)
        self.assertAlmostEqual(feature["ma5"], round(closes.tail(5).mean(), 3))
        self.assertAlmostEqual(feature["ma20"], round(closes.tail(20).mean(), 3))
        self.assertAlmostEqual(feature["ma60"], round(closes.tail(60).mean(), 3))
        self.assertTrue(math.isfinite(feature["atr14"]))
        self.assertTrue(math.isfinite(feature["rsi14"]))
        self.assertTrue(math.isfinite(feature["macd_dif"]))
        self.assertAlmostEqual(feature["boll_mid"], feature["ma20"], places=3)

    def test_future_price_changes_do_not_change_feature_snapshot_at_cutoff(self) -> None:
        hist = sample_history(190)
        changed_future = hist.copy()
        changed_future.loc[changed_future.index >= 150, "收盘"] = changed_future.loc[changed_future.index >= 150, "收盘"] * 5
        cutoff = hist.loc[149, "日期"]

        self.assertFalse(hist.loc[hist.index >= 150, "收盘"].equals(changed_future.loc[changed_future.index >= 150, "收盘"]))

        before = dqr.compute_base_features_as_of("000001", "测试股份", hist, sample_spot(), cutoff)
        after = dqr.compute_base_features_as_of("000001", "测试股份", changed_future, sample_spot(), cutoff)

        self.assertIsNotNone(before)
        self.assertIsNotNone(after)
        assert before is not None and after is not None
        for key in ["close", "ma20", "ma60", "ma120", "ret20", "ret60", "base_score"]:
            self.assertEqual(before[key], after[key])

    def test_finalize_scores_bounds_and_required_columns(self) -> None:
        feature_a = dqr.compute_base_features("000001", "测试A", sample_history(), sample_spot(change_pct=1.0))
        feature_b = dqr.compute_base_features("000002", "测试B", sample_history(shock_from=170, shock=-1.0), sample_spot(change_pct=-2.0))
        self.assertIsNotNone(feature_a)
        self.assertIsNotNone(feature_b)

        scored = dqr.finalize_scores(pd.DataFrame([feature_a, feature_b]))

        for column in ["score", "long_score", "board_score", "risk_pct", "long_signal", "board_signal"]:
            self.assertIn(column, scored.columns)
        self.assertTrue(scored["score"].between(0, 100).all())
        self.assertTrue(scored["long_score"].between(0, 100).all())
        self.assertTrue(scored["board_score"].between(0, 100).all())


class BacktestTests(unittest.TestCase):
    def test_backtest_returns_metrics_without_network(self) -> None:
        metrics = dqr.backtest({"000001": sample_history()})

        self.assertGreater(metrics.days, 0)
        self.assertTrue(math.isfinite(metrics.total_return))
        self.assertLessEqual(metrics.max_drawdown, 0)
        self.assertIn("signal_date", metrics.assumptions)
        self.assertIn("execution_date", metrics.assumptions)

    def test_backtest_signal_uses_next_row_position(self) -> None:
        frame = dqr.backtest_signal_frame(sample_history())

        shifted = frame["raw_signal"].shift(1)
        shifted_signal = shifted.where(shifted.notna(), False).astype(bool).astype(float)
        pd.testing.assert_series_equal(frame["position"], shifted_signal, check_names=False)

    def test_future_price_changes_do_not_change_past_signals_on_full_frame(self) -> None:
        hist = sample_history(190)
        changed_future = hist.copy()
        changed_future.loc[changed_future.index >= 150, "收盘"] = changed_future.loc[changed_future.index >= 150, "收盘"] * 5

        original = dqr.backtest_signal_frame(hist)
        changed = dqr.backtest_signal_frame(changed_future)

        pd.testing.assert_series_equal(original["raw_signal"].iloc[:150], changed["raw_signal"].iloc[:150])

    def test_future_price_changes_do_not_change_past_positions_on_full_frame(self) -> None:
        hist = sample_history(190)
        changed_future = hist.copy()
        changed_future.loc[changed_future.index >= 150, "收盘"] = changed_future.loc[changed_future.index >= 150, "收盘"] * 5

        original = dqr.backtest_signal_frame(hist)
        changed = dqr.backtest_signal_frame(changed_future)

        pd.testing.assert_series_equal(original["position"].iloc[:150], changed["position"].iloc[:150])

    def test_future_price_changes_do_not_change_base_features_at_cutoff(self) -> None:
        hist = sample_history(190)
        changed_future = hist.copy()
        changed_future.loc[changed_future.index >= 150, "收盘"] = changed_future.loc[changed_future.index >= 150, "收盘"] * 5
        cutoff = hist.loc[149, "日期"]

        self.assertFalse(hist.loc[hist.index >= 150, "收盘"].equals(changed_future.loc[changed_future.index >= 150, "收盘"]))

        original = dqr.compute_base_features_as_of("000001", "测试股份", hist, sample_spot(), cutoff)
        changed = dqr.compute_base_features_as_of("000001", "测试股份", changed_future, sample_spot(), cutoff)
        assert original is not None and changed is not None

        for key in ["close", "ma20", "ma60", "ma120", "ret20", "ret60", "base_score"]:
            self.assertEqual(original[key], changed[key])

    def test_default_backtest_assumptions_match_real_config(self) -> None:
        # Round 21: the default assumption payload mirrors the real
        # BacktestConfig instead of claiming zero costs the simulator never used.
        from quantdesk.backtest import BacktestConfig

        assumptions = dqr.default_backtest_assumptions()
        config = BacktestConfig()

        self.assertEqual(assumptions["commission_rate"], config.commission_rate)
        self.assertEqual(assumptions["stamp_duty_rate"], config.stamp_duty_rate)
        self.assertEqual(assumptions["slippage_bps"], config.slippage_bps)
        self.assertTrue(bool(assumptions["costs_implemented"]))
        warning = dqr.backtest_cost_warning(assumptions)
        self.assertIn("不能等同真实成交", warning)

    def test_backtest_cost_warning_is_explicit_when_costs_not_implemented(self) -> None:
        assumptions = {"costs_implemented": False}
        self.assertIn("未完全模拟真实交易成本", dqr.backtest_cost_warning(assumptions))

    def test_commission_reduces_return(self) -> None:
        hist = tradeable_backtest_history()
        no_cost = dqr.simulate_single_asset_backtest(hist, "000001", zero_cost_config())
        with_commission = dqr.simulate_single_asset_backtest(
            hist,
            "000001",
            zero_cost_config(commission_rate=0.002),
        )

        self.assertGreater(no_cost.metrics.number_of_trades, 0)
        self.assertLess(with_commission.metrics.final_equity, no_cost.metrics.final_equity)

    def test_slippage_reduces_return(self) -> None:
        hist = tradeable_backtest_history()
        no_slippage = dqr.simulate_single_asset_backtest(hist, "000001", zero_cost_config())
        with_slippage = dqr.simulate_single_asset_backtest(
            hist,
            "000001",
            zero_cost_config(slippage_bps=25),
        )

        self.assertLess(with_slippage.metrics.final_equity, no_slippage.metrics.final_equity)

    def test_stamp_duty_only_on_sell(self) -> None:
        result = dqr.simulate_single_asset_backtest(
            tradeable_backtest_history(),
            "000001",
            zero_cost_config(stamp_duty_rate=0.001),
        )
        buy = first_trade(result, "BUY", blocked=False)
        sell = first_trade(result, "SELL", blocked=False)

        self.assertEqual(buy["stamp_duty"], 0.0)
        self.assertGreater(sell["stamp_duty"], 0.0)

    def test_signal_executes_next_day_open(self) -> None:
        hist = tradeable_backtest_history()
        result = dqr.simulate_single_asset_backtest(hist, "000001", zero_cost_config())
        buy = first_trade(result, "BUY", blocked=False)
        execution_date = pd.to_datetime(buy["execution_date"])
        execution_row = hist[pd.to_datetime(hist["日期"]).eq(execution_date)].iloc[0]

        self.assertNotEqual(buy["signal_date"], buy["execution_date"])
        self.assertAlmostEqual(float(buy["requested_price"]), float(execution_row["开盘"]))
        self.assertNotAlmostEqual(float(buy["requested_price"]), float(hist[pd.to_datetime(hist["日期"]).eq(pd.to_datetime(buy["signal_date"]))].iloc[0]["收盘"]))

    def test_limit_up_blocks_buy(self) -> None:
        hist = tradeable_backtest_history()
        baseline = dqr.simulate_single_asset_backtest(hist, "000001", zero_cost_config())
        buy = first_trade(baseline, "BUY", blocked=False)
        changed = hist.copy()
        exec_mask = pd.to_datetime(changed["日期"]).eq(pd.to_datetime(buy["execution_date"]))
        exec_idx = changed.index[exec_mask][0]
        prev_close = float(changed.loc[exec_idx - 1, "收盘"])
        for col in ["开盘", "最高", "最低", "收盘"]:
            changed.loc[exec_mask, col] = prev_close * 1.10

        result = dqr.simulate_single_asset_backtest(changed, "000001", zero_cost_config())
        blocked_buy = first_trade(result, "BUY", blocked=True)

        self.assertIn("limit up", blocked_buy["blocked_reason"])

    def test_limit_down_blocks_sell(self) -> None:
        hist = tradeable_backtest_history()
        baseline = dqr.simulate_single_asset_backtest(hist, "000001", zero_cost_config())
        sell = first_trade(baseline, "SELL", blocked=False)
        changed = hist.copy()
        exec_mask = pd.to_datetime(changed["日期"]).eq(pd.to_datetime(sell["execution_date"]))
        exec_idx = changed.index[exec_mask][0]
        prev_close = float(changed.loc[exec_idx - 1, "收盘"])
        for col in ["开盘", "最高", "最低", "收盘"]:
            changed.loc[exec_mask, col] = prev_close * 0.89

        result = dqr.simulate_single_asset_backtest(changed, "000001", zero_cost_config())
        blocked_sell = first_trade(result, "SELL", blocked=True)

        self.assertIn("limit down", blocked_sell["blocked_reason"])

    def test_suspended_day_blocks_trade(self) -> None:
        hist = tradeable_backtest_history()
        baseline = dqr.simulate_single_asset_backtest(hist, "000001", zero_cost_config())
        buy = first_trade(baseline, "BUY", blocked=False)
        changed = hist.copy()
        changed.loc[pd.to_datetime(changed["日期"]).eq(pd.to_datetime(buy["execution_date"])), "成交量"] = 0

        result = dqr.simulate_single_asset_backtest(changed, "000001", zero_cost_config())
        blocked_buy = first_trade(result, "BUY", blocked=True)

        self.assertIn("suspended", blocked_buy["blocked_reason"])

    def test_trades_are_recorded(self) -> None:
        result = dqr.simulate_single_asset_backtest(tradeable_backtest_history(), "000001", zero_cost_config())
        required = {
            "date",
            "symbol",
            "side",
            "signal_date",
            "execution_date",
            "requested_price",
            "executed_price",
            "shares",
            "gross_amount",
            "commission",
            "stamp_duty",
            "slippage_cost",
            "total_cost",
            "reason",
            "blocked",
            "blocked_reason",
        }

        self.assertTrue(any(trade["side"] == "BUY" for trade in result.trades))
        self.assertTrue(any(trade["side"] == "SELL" for trade in result.trades))
        self.assertTrue(required.issubset(result.trades[0].keys()))

    def test_backtest_assumptions_mark_costs_implemented(self) -> None:
        result = dqr.simulate_single_asset_backtest(tradeable_backtest_history(), "000001")

        self.assertTrue(bool(result.assumptions["costs_implemented"]))
        self.assertEqual(result.assumptions["execution_date"], "T+1")
        self.assertEqual(result.assumptions["execution_price"], "next_open")

    def test_old_backtest_still_runs(self) -> None:
        metrics = dqr.backtest({"000001": tradeable_backtest_history()})

        self.assertGreater(metrics.days, 0)
        self.assertTrue(math.isfinite(metrics.final_equity))
        self.assertTrue(bool(metrics.assumptions["costs_implemented"]))

    def test_backtest_exports_trades_csv(self) -> None:
        result = dqr.simulate_single_asset_backtest(tradeable_backtest_history(), "000001", zero_cost_config())
        files = dqr.export_backtest_result(result, "000001", test_artifact_dir(self.id()))
        header = files["trades"].read_text(encoding="utf-8-sig").splitlines()[0]

        self.assertTrue(files["trades"].exists())
        self.assertIn("date", header)
        self.assertIn("commission", header)

    def test_backtest_exports_equity_curve_csv(self) -> None:
        result = dqr.simulate_single_asset_backtest(tradeable_backtest_history(), "000001", zero_cost_config())
        files = dqr.export_backtest_result(result, "000001", test_artifact_dir(self.id()))
        header = files["equity_curve"].read_text(encoding="utf-8-sig").splitlines()[0]

        self.assertTrue(files["equity_curve"].exists())
        self.assertIn("equity", header)

    def test_backtest_exports_assumptions_json(self) -> None:
        result = dqr.simulate_single_asset_backtest(tradeable_backtest_history(), "000001")
        files = dqr.export_backtest_result(result, "000001", test_artifact_dir(self.id()))
        payload = json.loads(files["assumptions"].read_text(encoding="utf-8"))

        self.assertTrue(files["assumptions"].exists())
        self.assertIn("commission_rate", payload)
        self.assertIn("stamp_duty_rate", payload)
        self.assertIn("slippage_bps", payload)

    def test_blocked_trades_exported(self) -> None:
        hist = tradeable_backtest_history()
        baseline = dqr.simulate_single_asset_backtest(hist, "000001", zero_cost_config())
        buy = first_trade(baseline, "BUY", blocked=False)
        changed = hist.copy()
        exec_mask = pd.to_datetime(changed["日期"]).eq(pd.to_datetime(buy["execution_date"]))
        exec_idx = changed.index[exec_mask][0]
        prev_close = float(changed.loc[exec_idx - 1, "收盘"])
        for col in ["开盘", "最高", "最低", "收盘"]:
            changed.loc[exec_mask, col] = prev_close * 1.10
        result = dqr.simulate_single_asset_backtest(changed, "000001", zero_cost_config())

        files = dqr.export_backtest_result(result, "000001", test_artifact_dir(self.id()))
        blocked = pd.read_csv(files["blocked_trades"])

        self.assertTrue(files["blocked_trades"].exists())
        self.assertGreaterEqual(len(blocked), 1)
        self.assertIn("blocked_reason", blocked.columns)

    def test_no_trade_backtest_exports_empty_headers(self) -> None:
        result = dqr.simulate_single_asset_backtest(sample_history(20), "000001", zero_cost_config())
        files = dqr.export_backtest_result(result, "000001", test_artifact_dir(self.id()))
        header = files["trades"].read_text(encoding="utf-8-sig").splitlines()[0]

        self.assertIn("blocked_reason", header)
        self.assertEqual(len(pd.read_csv(files["trades"])), 0)

    def test_benchmark_total_return_from_local_series(self) -> None:
        series = pd.Series([100.0, 110.0], index=pd.to_datetime(["2024-01-01", "2024-01-02"]))

        self.assertAlmostEqual(dqr.benchmark_total_return(series), 0.10)

    def test_load_benchmark_series_from_csv(self) -> None:
        path = test_artifact_dir(self.id()) / "benchmark.csv"
        pd.DataFrame(
            {
                "date": ["2024-01-03", "bad-date", "2024-01-01", "2024-01-02"],
                "close": [103, 999, 100, 101],
            }
        ).to_csv(path, index=False)

        series = dqr.load_benchmark_series(path)

        self.assertEqual(list(series.index.strftime("%Y-%m-%d")), ["2024-01-01", "2024-01-02", "2024-01-03"])
        self.assertEqual(float(series.iloc[0]), 100.0)

    def test_unit_tests_do_not_require_real_akshare(self) -> None:
        from quantdesk import data_providers

        with patch.object(data_providers, "load_akshare", side_effect=RuntimeError("no akshare")):
            metrics = dqr.backtest({"000001": tradeable_backtest_history()})

        self.assertFalse(hasattr(dqr, "ak"))
        self.assertGreater(metrics.days, 0)

    def test_portfolio_uses_single_cash_pool(self) -> None:
        histories = {"000001": tradeable_backtest_history(), "000002": tradeable_backtest_history()}
        config = zero_cost_portfolio_config(initial_cash=10_000, max_positions=2, max_position_pct=1.0)

        result = dqr.simulate_portfolio_backtest(histories, config)
        buys = [trade for trade in result.trades if trade["side"] == "BUY" and not trade["blocked"]]
        blocked = [trade for trade in result.trades if trade["side"] == "BUY" and trade["blocked"]]

        self.assertEqual(len(buys), 1)
        self.assertTrue(any("insufficient cash" in str(trade["blocked_reason"]) for trade in blocked))

    def test_portfolio_respects_max_positions(self) -> None:
        histories = {
            "000001": tradeable_backtest_history(),
            "000002": tradeable_backtest_history(),
            "000003": tradeable_backtest_history(),
        }
        config = zero_cost_portfolio_config(initial_cash=100_000, max_positions=1, max_position_pct=0.5)

        result = dqr.simulate_portfolio_backtest(histories, config)

        self.assertLessEqual(int(result.equity_curve["shares"].max()), 1)

    def test_portfolio_respects_max_position_pct(self) -> None:
        histories = {"000001": tradeable_backtest_history(), "000002": tradeable_backtest_history()}
        config = zero_cost_portfolio_config(initial_cash=100_000, max_positions=2, max_position_pct=0.2)

        result = dqr.simulate_portfolio_backtest(histories, config)
        weights = pd.to_numeric(result.positions["weight"], errors="coerce").dropna()

        self.assertTrue((weights <= 0.23).all())

    def test_portfolio_sells_release_cash(self) -> None:
        histories = {
            "000001": tradeable_backtest_history(),
            "000002": shift_history_dates(tradeable_backtest_history(), 70),
        }
        config = zero_cost_portfolio_config(initial_cash=20_000, max_positions=1, max_position_pct=1.0)

        result = dqr.simulate_portfolio_backtest(histories, config)
        sells = [trade for trade in result.trades if trade["side"] == "SELL" and not trade["blocked"]]
        buys = [trade for trade in result.trades if trade["side"] == "BUY" and not trade["blocked"]]

        self.assertGreaterEqual(len(sells), 1)
        self.assertGreaterEqual(len(buys), 2)
        sell_date = sells[0]["execution_date"]
        cash_before = float(result.equity_curve[result.equity_curve["date"] < sell_date].tail(1)["cash"].iloc[0])
        cash_after = float(result.equity_curve[result.equity_curve["date"].eq(sell_date)]["cash"].iloc[0])
        self.assertGreater(cash_after, cash_before)
        self.assertTrue(any(trade["execution_date"] > sell_date for trade in buys))

    def test_portfolio_limit_up_blocks_buy(self) -> None:
        hist = tradeable_backtest_history()
        baseline = dqr.simulate_portfolio_backtest({"000001": hist}, zero_cost_portfolio_config())
        buy = first_trade(baseline, "BUY", blocked=False)
        changed = hist.copy()
        exec_mask = pd.to_datetime(changed["日期"]).eq(pd.to_datetime(buy["execution_date"]))
        exec_idx = changed.index[exec_mask][0]
        prev_close = float(changed.loc[exec_idx - 1, "收盘"])
        for col in ["开盘", "最高", "最低", "收盘"]:
            changed.loc[exec_mask, col] = prev_close * 1.10

        result = dqr.simulate_portfolio_backtest({"000001": changed}, zero_cost_portfolio_config())
        blocked_buy = first_trade(result, "BUY", blocked=True)

        self.assertIn("limit up", blocked_buy["blocked_reason"])

    def test_portfolio_limit_down_blocks_sell(self) -> None:
        hist = tradeable_backtest_history()
        baseline = dqr.simulate_portfolio_backtest({"000001": hist}, zero_cost_portfolio_config())
        sell = next(
            trade
            for trade in baseline.trades
            if trade["side"] == "SELL" and not trade["blocked"] and "left target" in str(trade.get("reason", ""))
        )
        changed = hist.copy()
        exec_mask = pd.to_datetime(changed["日期"]).eq(pd.to_datetime(sell["execution_date"]))
        exec_idx = changed.index[exec_mask][0]
        prev_close = float(changed.loc[exec_idx - 1, "收盘"])
        for col in ["开盘", "最高", "最低", "收盘"]:
            changed.loc[exec_mask, col] = prev_close * 0.89

        result = dqr.simulate_portfolio_backtest({"000001": changed}, zero_cost_portfolio_config())
        blocked_sell = first_trade(result, "SELL", blocked=True)

        self.assertIn("limit down", blocked_sell["blocked_reason"])

    def test_portfolio_suspended_blocks_trade(self) -> None:
        hist = tradeable_backtest_history()
        baseline = dqr.simulate_portfolio_backtest({"000001": hist}, zero_cost_portfolio_config())
        buy = first_trade(baseline, "BUY", blocked=False)
        changed = hist.copy()
        changed.loc[pd.to_datetime(changed["日期"]).eq(pd.to_datetime(buy["execution_date"])), "成交量"] = 0

        result = dqr.simulate_portfolio_backtest({"000001": changed}, zero_cost_portfolio_config())
        blocked_buy = first_trade(result, "BUY", blocked=True)

        self.assertIn("suspended", blocked_buy["blocked_reason"])

    def test_portfolio_exports_positions_csv(self) -> None:
        result = dqr.simulate_portfolio_backtest({"000001": tradeable_backtest_history()}, zero_cost_portfolio_config())
        files = dqr.export_backtest_result(result, "portfolio", test_artifact_dir(self.id()))
        header = files["positions"].read_text(encoding="utf-8-sig").splitlines()[0]

        self.assertTrue(files["positions"].exists())
        self.assertIn("market_value", header)

    def test_portfolio_exports_rebalance_log_csv(self) -> None:
        result = dqr.simulate_portfolio_backtest({"000001": tradeable_backtest_history()}, zero_cost_portfolio_config())
        files = dqr.export_backtest_result(result, "portfolio", test_artifact_dir(self.id()))
        header = files["rebalance_log"].read_text(encoding="utf-8-sig").splitlines()[0]

        self.assertTrue(files["rebalance_log"].exists())
        self.assertIn("cash_before", header)

    def test_portfolio_summary_csv_contains_core_metrics(self) -> None:
        result = dqr.simulate_portfolio_backtest({"000001": tradeable_backtest_history()}, zero_cost_portfolio_config(max_positions=3))
        files = dqr.export_backtest_result(result, "portfolio", test_artifact_dir(self.id()))
        summary = pd.read_csv(files["summary"])

        metrics = set(summary["metric"])
        self.assertIn("final_equity", metrics)
        self.assertIn("total_return", metrics)
        self.assertIn("max_drawdown", metrics)
        self.assertIn("max_positions", metrics)

    def test_portfolio_no_future_data_in_ranking(self) -> None:
        histories = {
            "000001": tradeable_backtest_history(220),
            "000002": shift_history_dates(tradeable_backtest_history(220), 5),
        }
        changed = {symbol: hist.copy() for symbol, hist in histories.items()}
        cutoff = pd.Timestamp("2024-06-01")
        changed["000002"].loc[pd.to_datetime(changed["000002"]["日期"]) > cutoff, "收盘"] *= 5
        config = zero_cost_portfolio_config(initial_cash=100_000, max_positions=1, max_position_pct=0.5)

        original_result = dqr.simulate_portfolio_backtest(histories, config)
        changed_result = dqr.simulate_portfolio_backtest(changed, config)
        original_trades = pd.DataFrame(original_result.trades)
        changed_trades = pd.DataFrame(changed_result.trades)
        original_past = original_trades[pd.to_datetime(original_trades["execution_date"]) <= cutoff].reset_index(drop=True)
        changed_past = changed_trades[pd.to_datetime(changed_trades["execution_date"]) <= cutoff].reset_index(drop=True)

        pd.testing.assert_frame_equal(original_past, changed_past)

    def test_existing_single_asset_tests_still_pass(self) -> None:
        result = dqr.simulate_single_asset_backtest(tradeable_backtest_history(), "000001", zero_cost_config())

        self.assertGreater(result.metrics.days, 0)
        self.assertTrue(any(trade["side"] == "BUY" for trade in result.trades))


class DataHealthTests(unittest.TestCase):
    def setUp(self) -> None:
        dqr.reset_data_health()
        run_id = time.time_ns()
        self.test_symbol = str(800000 + (run_id % 100000)).zfill(6)
        self.test_cache_dir = ROOT / "results" / "test_daily_cache" / f"{self.id().replace('.', '_')}_{run_id}"

    def tearDown(self) -> None:
        dqr.reset_data_health()

    def cache_dir(self) -> Path:
        path = self.test_cache_dir
        path.mkdir(parents=True, exist_ok=True)
        return path

    def run_fetch_in_test_cache(self, symbol: str, hist: pd.DataFrame, hist_error: Exception | None = None, sina_error: Exception | None = None) -> pd.DataFrame:
        old_daily_dir = dqr.DAILY_DIR
        dqr.DAILY_DIR = self.cache_dir()
        try:
            with patch.object(dqr, "fetch_daily_eastmoney", side_effect=hist_error, return_value=hist):
                with patch.object(dqr, "fetch_daily_sina", side_effect=sina_error, return_value=hist):
                    return dqr.fetch_daily(symbol, "20240101", sample_end_date(), "qfq", 1, 0, name="测试股份")
        finally:
            dqr.DAILY_DIR = old_daily_dir

    def test_eastmoney_success_status_is_ok(self) -> None:
        self.run_fetch_in_test_cache(self.test_symbol, sample_history())
        health = dqr.data_health_frame()
        row = health.iloc[-1]

        self.assertEqual(row["effective_source"], "eastmoney")
        self.assertTrue(bool(row["fresh_fetch_success"]))
        self.assertFalse(bool(row["cache_present"]))
        self.assertFalse(bool(row["cache_merged"]))
        self.assertFalse(bool(row["served_from_cache"]))
        self.assertFalse(bool(row["fallback_used"]))
        self.assertEqual(row["warning_level"], "OK")

    def test_eastmoney_success_with_cache_marks_cache_merged(self) -> None:
        old_daily_dir = dqr.DAILY_DIR
        dqr.DAILY_DIR = self.cache_dir()
        cache_path = dqr.DAILY_DIR / f"{self.test_symbol}_qfq.parquet"
        dqr.save_table(sample_history(150), cache_path, quiet=True)
        try:
            with patch.object(dqr, "fetch_daily_eastmoney", return_value=sample_history()):
                result = dqr.fetch_daily(self.test_symbol, "20240101", sample_end_date(), "qfq", 1, 0, name="缓存合并股份")
        finally:
            dqr.DAILY_DIR = old_daily_dir
        row = dqr.data_health_frame().iloc[-1]

        self.assertFalse(result.empty)
        self.assertEqual(row["effective_source"], "eastmoney")
        self.assertTrue(bool(row["fresh_fetch_success"]))
        self.assertTrue(bool(row["cache_present"]))
        self.assertTrue(bool(row["cache_merged"]))
        self.assertFalse(bool(row["served_from_cache"]))
        self.assertEqual(row["warning_level"], "OK")

    def test_eastmoney_failure_sina_success_marks_fallback(self) -> None:
        self.run_fetch_in_test_cache(self.test_symbol, sample_history(), hist_error=RuntimeError("eastmoney down"))
        row = dqr.data_health_frame().iloc[-1]

        self.assertEqual(row["effective_source"], "sina")
        self.assertTrue(bool(row["fresh_fetch_success"]))
        self.assertTrue(bool(row["fallback_used"]))
        self.assertEqual(row["fallback_from"], "eastmoney")
        self.assertEqual(row["fallback_to"], "sina")
        self.assertEqual(row["warning_level"], "INFO")

    def test_eastmoney_empty_sina_success_marks_fallback(self) -> None:
        old_daily_dir = dqr.DAILY_DIR
        dqr.DAILY_DIR = self.cache_dir()
        try:
            with patch.object(dqr, "fetch_daily_eastmoney", return_value=pd.DataFrame()):
                with patch.object(dqr, "fetch_daily_sina", return_value=sample_history()):
                    dqr.fetch_daily(self.test_symbol, "20240101", sample_end_date(), "qfq", 1, 0, name="空表备用股份")
        finally:
            dqr.DAILY_DIR = old_daily_dir
        row = dqr.data_health_frame().iloc[-1]

        self.assertEqual(row["effective_source"], "sina")
        self.assertTrue(bool(row["fresh_fetch_success"]))
        self.assertTrue(bool(row["fallback_used"]))
        self.assertIn("empty", str(row["error_message"]))

    def test_both_sources_fail_with_cache_marks_cache_used(self) -> None:
        old_daily_dir = dqr.DAILY_DIR
        dqr.DAILY_DIR = self.cache_dir()
        cache_path = dqr.DAILY_DIR / f"{self.test_symbol}_qfq.parquet"
        dqr.save_table(sample_history(), cache_path, quiet=True)
        try:
            with patch.object(dqr, "fetch_daily_eastmoney", side_effect=RuntimeError("eastmoney down")):
                with patch.object(dqr, "fetch_daily_sina", side_effect=RuntimeError("sina down")):
                    result = dqr.fetch_daily(self.test_symbol, "20240101", sample_end_date(), "qfq", 1, 0, name="缓存股份")
        finally:
            dqr.DAILY_DIR = old_daily_dir
        row = dqr.data_health_frame().iloc[-1]

        self.assertFalse(result.empty)
        self.assertEqual(row["effective_source"], "cache")
        self.assertFalse(bool(row["fresh_fetch_success"]))
        self.assertTrue(bool(row["cache_present"]))
        self.assertTrue(bool(row["served_from_cache"]))
        self.assertEqual(row["warning_level"], "WARNING")

    def test_eastmoney_empty_sina_empty_with_cache_serves_cache(self) -> None:
        old_daily_dir = dqr.DAILY_DIR
        dqr.DAILY_DIR = self.cache_dir()
        cache_path = dqr.DAILY_DIR / f"{self.test_symbol}_qfq.parquet"
        dqr.save_table(sample_history(), cache_path, quiet=True)
        try:
            with patch.object(dqr, "fetch_daily_eastmoney", return_value=pd.DataFrame()):
                with patch.object(dqr, "fetch_daily_sina", return_value=pd.DataFrame()):
                    result = dqr.fetch_daily(self.test_symbol, "20240101", sample_end_date(), "qfq", 1, 0, name="空表缓存股份")
        finally:
            dqr.DAILY_DIR = old_daily_dir
        row = dqr.data_health_frame().iloc[-1]

        self.assertFalse(result.empty)
        self.assertEqual(row["effective_source"], "cache")
        self.assertTrue(bool(row["served_from_cache"]))
        self.assertIn(row["warning_level"], ["WARNING", "ERROR"])

    def test_eastmoney_empty_sina_empty_without_cache_is_error(self) -> None:
        old_daily_dir = dqr.DAILY_DIR
        dqr.DAILY_DIR = self.cache_dir()
        try:
            with patch.object(dqr, "fetch_daily_eastmoney", return_value=pd.DataFrame()):
                with patch.object(dqr, "fetch_daily_sina", return_value=pd.DataFrame()):
                    result = dqr.fetch_daily(self.test_symbol, "20240101", sample_end_date(), "qfq", 1, 0, name="空表无缓存股份")
        finally:
            dqr.DAILY_DIR = old_daily_dir
        row = dqr.data_health_frame().iloc[-1]

        self.assertTrue(result.empty)
        self.assertEqual(row["effective_source"], "none")
        self.assertEqual(row["warning_level"], "ERROR")

    def test_no_data_marks_error(self) -> None:
        self.run_fetch_in_test_cache(
            self.test_symbol,
            pd.DataFrame(),
            hist_error=RuntimeError("eastmoney down"),
            sina_error=RuntimeError("sina down"),
        )
        row = dqr.data_health_frame().iloc[-1]

        self.assertFalse(bool(row["fresh_fetch_success"]))
        self.assertFalse(bool(row["served_from_cache"]))
        self.assertEqual(row["warning_level"], "ERROR")

    def test_stale_cache_is_warning_or_error(self) -> None:
        dqr.reset_data_health()
        dqr.record_data_health(
            symbol="000001",
            name="测试股份",
            adjust="qfq",
            requested_source="eastmoney",
            effective_source="eastmoney",
            merged=sample_history(10),
            end_date="20240131",
            cache_present=True,
            cache_merged=False,
            served_from_cache=False,
            fresh_fetch_success=True,
            fallback_used=False,
            fallback_from="",
            fallback_to="",
            error_message="",
        )
        row = dqr.data_health_frame().iloc[-1]
        self.assertIn(row["warning_level"], ["WARNING", "ERROR"])

    def test_data_health_summary_counts_levels_cache_fallback_and_stale(self) -> None:
        dqr.record_data_health(
            symbol="000010",
            name="正常股份",
            adjust="qfq",
            requested_source="eastmoney",
            effective_source="eastmoney",
            merged=sample_history(),
            end_date=sample_end_date(),
            cache_present=False,
            cache_merged=False,
            served_from_cache=False,
            fresh_fetch_success=True,
            fallback_used=False,
            fallback_from="",
            fallback_to="",
            error_message="",
        )
        dqr.record_data_health(
            symbol="000011",
            name="备用股份",
            adjust="qfq",
            requested_source="eastmoney",
            effective_source="sina",
            merged=sample_history(),
            end_date=sample_end_date(),
            cache_present=False,
            cache_merged=False,
            served_from_cache=False,
            fresh_fetch_success=True,
            fallback_used=True,
            fallback_from="eastmoney",
            fallback_to="sina",
            error_message="eastmoney failed",
        )
        dqr.record_data_health(
            symbol="000012",
            name="无数据股份",
            adjust="qfq",
            requested_source="eastmoney",
            effective_source="none",
            merged=pd.DataFrame(),
            end_date=sample_end_date(),
            cache_present=True,
            cache_merged=False,
            served_from_cache=True,
            fresh_fetch_success=False,
            fallback_used=True,
            fallback_from="eastmoney",
            fallback_to="sina",
            error_message="network failed",
        )

        health = dqr.data_health_frame()
        summary = dqr.data_health_summary(health)

        self.assertEqual(summary["total"], 3)
        self.assertEqual(summary["fresh_fetch_success"], 2)
        self.assertEqual(summary["ok"], 1)
        self.assertEqual(summary["info"], 1)
        self.assertEqual(summary["error"], 1)
        self.assertEqual(summary["cache_present"], 1)
        self.assertEqual(summary["served_from_cache"], 1)
        self.assertEqual(summary["fallback_used"], 2)
        self.assertEqual(summary["no_data"], 1)
        self.assertIn("stale_days", health.columns)

    def test_data_health_csv_can_be_written_with_header_when_empty(self) -> None:
        path = ROOT / "results" / "test_data_health.csv"
        path.parent.mkdir(parents=True, exist_ok=True)

        written = dqr.save_csv_safely(dqr.data_health_frame(), path)
        header = written.read_text(encoding="utf-8-sig").splitlines()[0]

        self.assertIn("symbol", header)
        self.assertIn("warning_level", header)

    def test_data_quality_risk_note_mentions_warning(self) -> None:
        note = dqr.data_quality_risk_note({"warning": 1, "error": 0})

        self.assertIn("本报告存在数据质量警告", note)

    def test_error_data_is_invalid_and_excluded_from_candidates(self) -> None:
        features = pd.DataFrame(
            [
                {"code": "000001", "score": 90, "long_score": 90, "board_score": 80},
                {"code": "000002", "score": 88, "long_score": 88, "board_score": 78},
            ]
        )
        health = pd.DataFrame(
            [
                {"symbol": "000001", "warning_level": "OK", "effective_source": "eastmoney", "latest_date": "2024-06-28", "served_from_cache": False, "fallback_used": False},
                {"symbol": "000002", "warning_level": "ERROR", "effective_source": "none", "latest_date": "", "served_from_cache": False, "fallback_used": False},
            ]
        )

        attached = dqr.attach_data_health(features, health)
        candidates = attached[(attached["score"] >= 60) & (~attached["data_invalid"])]

        self.assertEqual(list(candidates["code"]), ["000001"])
        self.assertTrue(bool(attached.loc[attached["code"].eq("000002"), "data_invalid"].iloc[0]))

    def test_warning_data_gets_risk_flag(self) -> None:
        features = pd.DataFrame([{"code": "000001", "score": 90}])
        health = pd.DataFrame(
            [{"symbol": "000001", "warning_level": "WARNING", "effective_source": "cache", "latest_date": "2024-06-01", "served_from_cache": True, "fallback_used": False}]
        )

        attached = dqr.attach_data_health(features, health)

        self.assertEqual(attached.loc[0, "data_warning_level"], "WARNING")
        self.assertIn("缓存", attached.loc[0, "data_risk_flag"])

    def test_info_fallback_data_gets_backup_source_flag(self) -> None:
        features = pd.DataFrame([{"code": "000001", "score": 90}])
        health = pd.DataFrame(
            [{"symbol": "000001", "warning_level": "INFO", "effective_source": "sina", "latest_date": "2024-06-28", "served_from_cache": False, "fallback_used": True}]
        )

        attached = dqr.attach_data_health(features, health)

        self.assertEqual(attached.loc[0, "data_warning_level"], "INFO")
        self.assertIn("备用数据源", attached.loc[0, "data_risk_flag"])


if __name__ == "__main__":
    unittest.main()
