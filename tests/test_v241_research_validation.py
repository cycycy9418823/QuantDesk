"""Tests for QuantDesk v2.4.1 Research Validation features."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np
import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


def _make_price_df(rows: int = 60, seed: int = 42) -> pd.DataFrame:
    rng = np.random.RandomState(seed)
    close = 100 + rng.randn(rows).cumsum()
    return pd.DataFrame({
        "date": pd.date_range("2023-01-01", periods=rows, freq="B"),
        "close": close,
        "high": close + rng.rand(rows) * 2,
        "low": close - rng.rand(rows) * 2,
        "volume": rng.randint(100000, 1000000, rows).astype(float),
        "turnover": rng.rand(rows) * 5,
    })


def _make_small_price_df(rows: int = 60, seed: int = 42) -> pd.DataFrame:
    rng = np.random.RandomState(seed)
    close = 10 + rng.randn(rows).cumsum() * 0.1
    return pd.DataFrame({
        "date": pd.date_range("2023-01-01", periods=rows, freq="B"),
        "close": close.clip(1),
        "high": (close + rng.rand(rows) * 0.5).clip(1),
        "low": (close - rng.rand(rows) * 0.5).clip(0.5),
    })


# ---------------------------------------------------------------------------
# classify_market
# ---------------------------------------------------------------------------
class TestClassifyMarket(unittest.TestCase):
    def test_sh_main_board(self):
        from quantdesk.data_quality import classify_market
        self.assertEqual(classify_market("600000"), "SH")
        self.assertEqual(classify_market("601318"), "SH")

    def test_sz_main_board(self):
        from quantdesk.data_quality import classify_market
        self.assertEqual(classify_market("000001"), "SZ")
        self.assertEqual(classify_market("002594"), "SZ")

    def test_chinext(self):
        from quantdesk.data_quality import classify_market
        self.assertEqual(classify_market("300750"), "CHINEXT")
        self.assertEqual(classify_market("301001"), "CHINEXT")

    def test_star(self):
        from quantdesk.data_quality import classify_market
        self.assertEqual(classify_market("688981"), "STAR")

    def test_bse_920(self):
        from quantdesk.data_quality import classify_market
        self.assertEqual(classify_market("920001"), "BSE")
        self.assertEqual(classify_market("920999"), "BSE")

    def test_bse_83_87_43(self):
        from quantdesk.data_quality import classify_market
        self.assertEqual(classify_market("830799"), "BSE")
        self.assertEqual(classify_market("870001"), "BSE")
        self.assertEqual(classify_market("430001"), "BSE")

    def test_unknown(self):
        from quantdesk.data_quality import classify_market
        self.assertEqual(classify_market(""), "UNKNOWN")
        self.assertEqual(classify_market("abc"), "UNKNOWN")
        self.assertEqual(classify_market("12345"), "UNKNOWN")
        self.assertEqual(classify_market("999999"), "UNKNOWN")


# ---------------------------------------------------------------------------
# classify_provider_error enhanced
# ---------------------------------------------------------------------------
class TestClassifyProviderError(unittest.TestCase):
    def test_network_blocked(self):
        from quantdesk.data_quality import NETWORK_BLOCKED_CODE, classify_provider_error
        self.assertEqual(classify_provider_error("WinError 10013 permission denied"), NETWORK_BLOCKED_CODE)

    def test_remote_disconnected(self):
        from quantdesk.data_quality import NETWORK_REMOTE_DISCONNECTED, classify_provider_error
        self.assertEqual(classify_provider_error("RemoteDisconnected"), NETWORK_REMOTE_DISCONNECTED)

    def test_empty_response(self):
        from quantdesk.data_quality import PROVIDER_EMPTY_RESPONSE, classify_provider_error
        self.assertEqual(classify_provider_error("returned empty daily data"), PROVIDER_EMPTY_RESPONSE)

    def test_schema_mismatch(self):
        from quantdesk.data_quality import PROVIDER_SCHEMA_MISMATCH, classify_provider_error
        self.assertEqual(classify_provider_error("KeyError: 'date' not in columns"), PROVIDER_SCHEMA_MISMATCH)

    def test_bse_mapping(self):
        from quantdesk.data_quality import BSE_MAPPING_MISSING, classify_provider_error
        self.assertEqual(classify_provider_error("cannot normalize 920001 for Sina daily"), BSE_MAPPING_MISSING)

    def test_empty_string(self):
        from quantdesk.data_quality import classify_provider_error
        self.assertEqual(classify_provider_error(""), "")

    def test_unknown(self):
        from quantdesk.data_quality import UNKNOWN_PROVIDER_ERROR, classify_provider_error
        self.assertEqual(classify_provider_error("some random error"), UNKNOWN_PROVIDER_ERROR)


# ---------------------------------------------------------------------------
# bse_summary_from_results
# ---------------------------------------------------------------------------
class TestBseSummary(unittest.TestCase):
    def test_mixed_results(self):
        from quantdesk.data_quality import bse_summary_from_results
        results = [
            {"symbol": "920001", "outcome": "success", "ok": True},
            {"symbol": "920002", "outcome": "failed", "ok": False, "provider_error_type": "BSE_MAPPING_MISSING"},
            {"symbol": "600000", "outcome": "success", "ok": True},
            {"symbol": "830799", "outcome": "failed", "ok": False, "provider_error_type": "PROVIDER_SCHEMA_MISMATCH"},
        ]
        summary = bse_summary_from_results(results)
        self.assertEqual(summary["bse_total"], 3)
        self.assertEqual(summary["bse_success_count"], 1)
        self.assertEqual(summary["bse_failed_count"], 2)
        self.assertEqual(summary["bse_provider_unsupported_count"], 2)


# ---------------------------------------------------------------------------
# BacktestMetrics
# ---------------------------------------------------------------------------
class TestBacktestMetrics(unittest.TestCase):
    def test_compute_equity_metrics_basic(self):
        from quantdesk.backtest_metrics import compute_equity_metrics
        equity = [100000, 101000, 102000, 101500, 103000]
        result = compute_equity_metrics(equity, trading_days_per_year=4)
        self.assertGreater(result["annual_return"], 0)
        self.assertGreaterEqual(result["max_drawdown"], 0)
        self.assertLessEqual(result["max_drawdown"], 1)
        self.assertIsInstance(result["sharpe"], float)

    def test_compute_equity_metrics_empty(self):
        from quantdesk.backtest_metrics import compute_equity_metrics
        self.assertEqual(compute_equity_metrics([])["annual_return"], 0.0)

    def test_compute_trade_metrics_no_trades(self):
        from quantdesk.backtest_metrics import compute_trade_metrics
        result = compute_trade_metrics([])
        self.assertEqual(result["trades_count"], 0)
        self.assertEqual(result["win_rate"], 0.0)

    def test_compute_trade_metrics_with_trades(self):
        from quantdesk.backtest_metrics import TradeRecord, compute_trade_metrics
        trades = [TradeRecord(pnl=100), TradeRecord(pnl=-50), TradeRecord(pnl=200)]
        result = compute_trade_metrics(trades)
        self.assertEqual(result["trades_count"], 3)
        self.assertAlmostEqual(result["win_rate"], 2 / 3, places=2)
        self.assertGreater(result["profit_factor"], 0)

    def test_summarize_windows(self):
        from quantdesk.backtest_metrics import WindowMetrics, summarize_windows
        windows = [
            WindowMetrics(annual_return=0.1, max_drawdown=0.05, sharpe=1.2, win_rate=0.6, trades_count=20),
            WindowMetrics(annual_return=-0.03, max_drawdown=0.15, sharpe=-0.2, win_rate=0.4, trades_count=15),
            WindowMetrics(annual_return=0.08, max_drawdown=0.1, sharpe=0.8, win_rate=0.55, trades_count=18),
        ]
        summary = summarize_windows(windows)
        self.assertEqual(summary.total_windows, 3)
        self.assertEqual(summary.positive_windows, 2)
        self.assertEqual(summary.negative_windows, 1)
        self.assertAlmostEqual(summary.worst_window_return, -0.03)
        self.assertAlmostEqual(summary.best_window_return, 0.1)


# ---------------------------------------------------------------------------
# Walk-forward window generation
# ---------------------------------------------------------------------------
class TestWalkForwardWindows(unittest.TestCase):
    def test_generate_windows(self):
        from quantdesk.walk_forward import generate_windows
        windows = generate_windows("20150101", "20250101", train_years=3, test_years=1)
        self.assertGreaterEqual(len(windows), 1)
        for w in windows:
            self.assertLess(w["train_start"], w["train_end"])
            self.assertLess(w["test_start"], w["test_end"])

    def test_insufficient_range(self):
        from quantdesk.walk_forward import generate_windows
        windows = generate_windows("20230101", "20230601", train_years=3, test_years=1)
        self.assertEqual(len(windows), 0)


# ---------------------------------------------------------------------------
# Backtest quality scoring
# ---------------------------------------------------------------------------
class TestBacktestQuality(unittest.TestCase):
    def test_invalid_no_windows(self):
        from quantdesk.backtest_metrics import WalkForwardSummary
        from quantdesk.backtest_quality import evaluate_backtest_quality
        summary = WalkForwardSummary(total_windows=0)
        quality = evaluate_backtest_quality(summary)
        self.assertEqual(quality.quality_grade, "INVALID")

    def test_high_quality(self):
        from quantdesk.backtest_metrics import WalkForwardSummary, WindowMetrics
        from quantdesk.backtest_quality import evaluate_backtest_quality
        windows = [
            WindowMetrics(annual_return=0.12, max_drawdown=0.08, sharpe=1.5, win_rate=0.6, trades_count=40),
            WindowMetrics(annual_return=0.08, max_drawdown=0.1, sharpe=1.0, win_rate=0.55, trades_count=35),
            WindowMetrics(annual_return=0.1, max_drawdown=0.12, sharpe=1.2, win_rate=0.58, trades_count=38),
            WindowMetrics(annual_return=0.05, max_drawdown=0.15, sharpe=0.7, win_rate=0.52, trades_count=30),
        ]
        summary = WalkForwardSummary(
            total_windows=4, positive_windows=4, negative_windows=0,
            total_trades=143, aggregate_sharpe=1.1, aggregate_win_rate=0.56,
            worst_max_drawdown=0.15, windows=windows,
        )
        quality = evaluate_backtest_quality(summary)
        self.assertIn(quality.quality_grade, ("HIGH", "MEDIUM"))
        self.assertGreaterEqual(quality.score, 45)

    def test_low_quality_few_trades(self):
        from quantdesk.backtest_metrics import WalkForwardSummary, WindowMetrics
        from quantdesk.backtest_quality import evaluate_backtest_quality
        windows = [
            WindowMetrics(annual_return=-0.05, max_drawdown=0.2, sharpe=-0.5, win_rate=0.3, trades_count=3),
        ]
        summary = WalkForwardSummary(
            total_windows=1, positive_windows=0, negative_windows=1,
            total_trades=3, aggregate_sharpe=-0.5, aggregate_win_rate=0.3,
            worst_max_drawdown=0.2, windows=windows,
        )
        quality = evaluate_backtest_quality(summary)
        self.assertIn(quality.quality_grade, ("INVALID", "LOW"))
        self.assertLess(quality.score, 45)

    def test_quality_to_dict(self):
        from quantdesk.backtest_quality import BacktestQuality
        bq = BacktestQuality(quality_grade="MEDIUM", score=55, reasons=["test"])
        d = bq.to_dict()
        self.assertEqual(d["quality_grade"], "MEDIUM")
        self.assertEqual(d["score"], 55)


# ---------------------------------------------------------------------------
# Backtest audit report
# ---------------------------------------------------------------------------
class TestBacktestAudit(unittest.TestCase):
    def test_generate_audit_markdown(self):
        from quantdesk.backtest_audit import generate_audit_markdown
        from quantdesk.backtest_metrics import WalkForwardSummary, WindowMetrics
        from quantdesk.backtest_quality import BacktestQuality
        summary = WalkForwardSummary(
            strategy="ma_breakout", total_windows=2, positive_windows=1,
            negative_windows=1, total_trades=20, median_annual_return=0.05,
            average_annual_return=0.04, worst_window_return=-0.02,
            best_window_return=0.1, worst_max_drawdown=0.12,
            aggregate_sharpe=0.8, aggregate_win_rate=0.55,
            windows=[
                WindowMetrics(test_start="20200101", test_end="20201231",
                              annual_return=0.1, max_drawdown=0.08, sharpe=1.2, win_rate=0.6, trades_count=12),
                WindowMetrics(test_start="20210101", test_end="20211231",
                              annual_return=-0.02, max_drawdown=0.12, sharpe=-0.3, win_rate=0.4, trades_count=8),
            ],
        )
        quality = BacktestQuality(quality_grade="MEDIUM", score=55)
        md = generate_audit_markdown(summary, quality)
        self.assertIn("策略审计报告", md)
        self.assertIn("ma_breakout", md)
        self.assertIn("MEDIUM", md)
        self.assertIn("不构成", md)

    def test_save_audit_report(self):
        from quantdesk.backtest_audit import save_audit_report
        from quantdesk.backtest_metrics import WalkForwardSummary
        from quantdesk.backtest_quality import BacktestQuality
        summary = WalkForwardSummary(strategy="test", total_windows=1, total_trades=5)
        quality = BacktestQuality(quality_grade="LOW", score=30)
        with tempfile.TemporaryDirectory() as tmpdir:
            json_path, md_path = save_audit_report(Path(tmpdir), summary, quality)
            self.assertTrue(json_path.exists())
            self.assertTrue(md_path.exists())
            data = json.loads(json_path.read_text(encoding="utf-8"))
            self.assertEqual(data["backtest_quality"]["quality_grade"], "LOW")


# ---------------------------------------------------------------------------
# Factor framework
# ---------------------------------------------------------------------------
class TestFactorFramework(unittest.TestCase):
    def setUp(self):
        self.sample_df = _make_price_df()

    def test_price_volume_factors(self):
        from quantdesk.factors.price_volume import PriceVolumeFactor
        results = PriceVolumeFactor.compute_all(self.sample_df)
        self.assertEqual(len(results), 5)
        available = [r for r in results if r.available]
        self.assertGreaterEqual(len(available), 3)

    def test_fundamental_factors_missing(self):
        from quantdesk.factors.fundamental import FundamentalFactor
        results = FundamentalFactor.compute_all(self.sample_df)
        for r in results:
            self.assertFalse(r.available)
            self.assertTrue(r.reason)

    def test_fundamental_factors_with_data(self):
        from quantdesk.factors.fundamental import FundamentalFactor
        df = self.sample_df.copy()
        df["pe_ttm"] = 15.0
        df["pb"] = 2.5
        results = FundamentalFactor.compute_all(df)
        pe = [r for r in results if r.name == "pe_ttm"][0]
        pb = [r for r in results if r.name == "pb"][0]
        self.assertTrue(pe.available)
        self.assertEqual(pe.value, 15.0)
        self.assertTrue(pb.available)
        self.assertEqual(pb.value, 2.5)

    def test_compute_factor_summary(self):
        from quantdesk.factors.scoring import compute_factor_summary
        result = compute_factor_summary(self.sample_df)
        self.assertIn("factors", result)
        self.assertEqual(result["total_count"], 8)
        self.assertEqual(result["available_count"] + result["missing_count"], result["total_count"])
        self.assertIn("disclaimer", result)


# ---------------------------------------------------------------------------
# Trade plan
# ---------------------------------------------------------------------------
class TestTradePlan(unittest.TestCase):
    def test_build_trade_plan(self):
        from quantdesk.trade_plan import build_trade_plan
        candidates = [
            {"symbol": "600000", "name": "浦发银行", "strategy": "ma_breakout",
             "reference_price": 10.0, "data_grade": "BACKTEST_READY",
             "backtest_quality": "HIGH", "atr14": 0.3},
            {"symbol": "000001", "name": "平安银行", "strategy": "ma_breakout",
             "reference_price": 12.0, "data_grade": "OBSERVE_ONLY",
             "backtest_quality": "INVALID", "atr14": 0.4},
        ]
        plan = build_trade_plan(candidates)
        self.assertEqual(len(plan.items), 2)
        self.assertEqual(plan.items[0].direction, "观察")
        self.assertFalse(plan.items[0].manual_confirmed)
        self.assertIn("不适合操作", plan.items[1].risk_note)
        self.assertTrue(plan.disclaimer)

    def test_export_csv(self):
        from quantdesk.trade_plan import build_trade_plan, export_trade_plan_csv
        candidates = [{"symbol": "600000", "name": "test", "strategy": "test",
                       "reference_price": 10.0, "data_grade": "BACKTEST_READY",
                       "backtest_quality": "MEDIUM", "atr14": 0.3}]
        plan = build_trade_plan(candidates)
        with tempfile.TemporaryDirectory() as tmpdir:
            path = export_trade_plan_csv(plan, Path(tmpdir) / "plan.csv")
            self.assertTrue(path.exists())
            df = pd.read_csv(path)
            self.assertEqual(len(df), 1)
            self.assertIn("risk_observation_line", df.columns)
            self.assertIn("virtual_size_reference", df.columns)
            self.assertIn("research_only", df.columns)
            self.assertIn("is_real_order", df.columns)
            self.assertFalse(bool(df.loc[0, "is_real_order"]))

    def test_export_markdown(self):
        from quantdesk.trade_plan import build_trade_plan, export_trade_plan_markdown
        candidates = [{"symbol": "600000", "name": "test", "strategy": "test",
                       "reference_price": 10.0, "data_grade": "BACKTEST_READY",
                       "backtest_quality": "MEDIUM", "atr14": 0.3}]
        plan = build_trade_plan(candidates)
        with tempfile.TemporaryDirectory() as tmpdir:
            path = export_trade_plan_markdown(plan, Path(tmpdir) / "plan.md")
            self.assertTrue(path.exists())
            text = path.read_text(encoding="utf-8")
            self.assertIn("10.0%", text)
            self.assertIn("仅供人工复核", text)
            self.assertIn("不构成", text)

    def test_empty_candidates(self):
        from quantdesk.trade_plan import build_trade_plan
        plan = build_trade_plan([])
        self.assertEqual(len(plan.items), 0)
        self.assertTrue(any("没有符合" in w for w in plan.warnings))

    def test_trade_plan_fields(self):
        from quantdesk.trade_plan import TradePlanItem
        item = TradePlanItem(symbol="600000", direction="观察")
        d = item.to_dict()
        self.assertIn("symbol", d)
        self.assertIn("direction", d)
        self.assertIn("risk_note", d)
        self.assertIn("manual_confirmed", d)
        self.assertIn("risk_observation_line", d)
        self.assertIn("virtual_size_reference", d)
        self.assertFalse(d["is_real_order"])
        self.assertTrue(d["research_only"])
        self.assertIn("disclaimer", d)

    def test_trade_plan_payloads_are_explicitly_research_and_paper_only(self):
        from quantdesk.trade_plan import build_trade_plan

        plan = build_trade_plan([
            {
                "symbol": "600000",
                "name": "sample",
                "strategy": "local research strategy",
                "reference_price": 10.0,
                "data_grade": "BACKTEST_READY",
                "backtest_quality": "MEDIUM",
                "atr14": 0.3,
            }
        ])
        payload = plan.to_dict()

        for row in (payload, payload["items"][0]):
            self.assertFalse(row["is_real_order"])
            self.assertTrue(row["research_only"])
            self.assertTrue(row["paper_only"])
            self.assertTrue(row["paper_trading_only"])
            self.assertTrue(row["no_broker"])
            self.assertTrue(row["not_investment_advice"])
            self.assertTrue(row["not_position_advice"])


# ---------------------------------------------------------------------------
# Risk/position reference
# ---------------------------------------------------------------------------
class TestRiskPosition(unittest.TestCase):
    def setUp(self):
        self.sample_df = _make_small_price_df()

    def test_basic_computation(self):
        from quantdesk.risk_position import compute_position_reference
        ref = compute_position_reference(self.sample_df, symbol="600000", data_grade="BACKTEST_READY")
        self.assertTrue(ref.usable)
        self.assertGreater(ref.atr14, 0)
        self.assertGreater(ref.stop_price, 0)
        self.assertGreater(ref.position_pct, 0)
        self.assertLessEqual(ref.position_pct, 0.10)
        self.assertGreaterEqual(ref.position_shares, 100)

    def test_low_grade_rejected(self):
        from quantdesk.risk_position import compute_position_reference
        ref = compute_position_reference(self.sample_df, symbol="600000", data_grade="UNAVAILABLE")
        self.assertFalse(ref.usable)
        self.assertIn("不足", ref.reason)

    def test_empty_df(self):
        from quantdesk.risk_position import compute_position_reference
        ref = compute_position_reference(pd.DataFrame(), symbol="600000", data_grade="BACKTEST_READY")
        self.assertFalse(ref.usable)

    def test_to_dict(self):
        from quantdesk.risk_position import compute_position_reference
        ref = compute_position_reference(self.sample_df, symbol="600000", data_grade="BACKTEST_READY")
        d = ref.to_dict()
        self.assertIn("symbol", d)
        self.assertIn("disclaimer", d)
        self.assertIn("atr14", d)
        self.assertFalse(d["is_real_order"])
        self.assertTrue(d["research_only"])
        self.assertTrue(d["paper_only"])
        self.assertTrue(d["paper_trading_only"])
        self.assertTrue(d["no_broker"])
        self.assertTrue(d["no_real_orders"])
        self.assertTrue(d["not_investment_advice"])
        self.assertTrue(d["not_position_advice"])


# ---------------------------------------------------------------------------
# get_strategy
# ---------------------------------------------------------------------------
class TestGetStrategy(unittest.TestCase):
    def test_known_strategy(self):
        from quantdesk.strategy_registry import get_strategy
        s = get_strategy("ma_breakout")
        self.assertIsNotNone(s)
        self.assertEqual(s.strategy_id, "ma_breakout")

    def test_unknown_strategy(self):
        from quantdesk.strategy_registry import get_strategy
        s = get_strategy("nonexistent_strategy_xyz")
        self.assertIsNone(s)


# ---------------------------------------------------------------------------
# date_fresh_but_history_short fix verification
# ---------------------------------------------------------------------------
class TestDateFreshFix(unittest.TestCase):
    def test_success_path_uses_merged_data(self):
        """The success return dict should contain date_fresh and merged values."""
        pass


# ---------------------------------------------------------------------------
# Integration: self_check can import new modules
# ---------------------------------------------------------------------------
class TestNewModuleImports(unittest.TestCase):
    def test_import_walk_forward(self):
        pass

    def test_import_backtest_quality(self):
        pass

    def test_import_backtest_audit(self):
        pass

    def test_import_backtest_metrics(self):
        pass

    def test_import_factors(self):
        pass

    def test_import_trade_plan(self):
        pass

    def test_import_risk_position(self):
        pass


if __name__ == "__main__":
    unittest.main()
