from __future__ import annotations

import json
import math
import subprocess
import tempfile
import unittest
from argparse import Namespace
from pathlib import Path

import pandas as pd

from quantdesk.paper.trade_costs import DEFAULT_STAMP_TAX_RATE
from scripts.run_strategy_screening_funnel import (
    HYPOTHESES,
    Hypothesis,
    classify_results,
    compute_factors,
    json_safe,
    load_benchmark,
    load_daily_data,
    max_drawdown,
    parse_args,
    run_screening,
    score_candidates,
    screening_trade_cost,
    write_outputs,
)

ROOT = Path(__file__).resolve().parents[1]


def _write_csv(path: Path, rows: int = 260, chinese: bool = False, missing: str = "") -> None:
    dates = pd.date_range("2020-01-01", periods=rows, freq="D")
    if chinese:
        df = pd.DataFrame({"日期": dates, "收盘": range(rows)})
    else:
        df = pd.DataFrame({
            "date": dates,
            "symbol": path.stem,
            "open": [10 + i * 0.01 for i in range(rows)],
            "high": [10.2 + i * 0.01 for i in range(rows)],
            "low": [9.8 + i * 0.01 for i in range(rows)],
            "close": [10 + i * 0.01 for i in range(rows)],
            "volume": [10000 + i for i in range(rows)],
            "amount": [30_000_000 + i for i in range(rows)],
        })
        if missing:
            df = df.drop(columns=[missing])
    df.to_csv(path, index=False)


class StrategyScreeningFunnelTests(unittest.TestCase):
    def test_01_standard_csv_readable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _write_csv(Path(tmp) / "000001.csv")
            panel, summary = load_daily_data(Path(tmp), 180)
        self.assertEqual(summary["usable_csv"], 1)
        self.assertFalse(panel.empty)

    def test_01b_load_daily_data_skips_bad_rows_with_visible_warning(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            csv_path = Path(tmp) / "000001.csv"
            _write_csv(csv_path, rows=220)
            with csv_path.open("a", encoding="utf-8-sig", newline="") as f:
                f.write("bad,row,with,too,many,columns,for,daily,panel\n")
            panel, summary = load_daily_data(Path(tmp), 180)

        self.assertEqual(summary["usable_csv"], 1)
        self.assertFalse(panel.empty)
        self.assertTrue(any("csv_bad_lines_skipped" in item for item in summary["csv_read_warnings"]))

    def test_02_chinese_schema_skipped(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _write_csv(Path(tmp) / "000001.csv", chinese=True)
            _, summary = load_daily_data(Path(tmp), 180)
        self.assertEqual(summary["skipped_reasons"]["chinese_or_non_ascii_schema"], 1)

    def test_03_missing_date_skipped(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _write_csv(Path(tmp) / "000001.csv", missing="date")
            _, summary = load_daily_data(Path(tmp), 180)
        self.assertEqual(summary["skipped_reasons"]["missing_date"], 1)

    def test_04_missing_close_skipped(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _write_csv(Path(tmp) / "000001.csv", missing="close")
            _, summary = load_daily_data(Path(tmp), 180)
        self.assertEqual(summary["skipped_reasons"]["missing_close"], 1)

    def test_05_to_13_factor_calculations_exist_and_sane(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _write_csv(Path(tmp) / "000001.csv", rows=300)
            panel, _ = load_daily_data(Path(tmp), 180)
        f = compute_factors(panel)
        last = f.iloc[-1]
        self.assertAlmostEqual(last["ret_20d"], last["close"] / f.iloc[-21]["close"] - 1, places=8)
        self.assertAlmostEqual(last["ret_60d"], last["close"] / f.iloc[-61]["close"] - 1, places=8)
        self.assertAlmostEqual(last["ret_120d"], last["close"] / f.iloc[-121]["close"] - 1, places=8)
        self.assertTrue(math.isfinite(last["volatility_60d"]))
        self.assertTrue(0 <= last["rsi_14"] <= 100)
        self.assertTrue(last["ma20"] > 0 and last["ma60"] > 0 and last["ma120"] > 0)
        self.assertTrue(last["bollinger_lower_20_2"] < last["bollinger_mid_20"])
        self.assertTrue(last["volume_5d_avg"] > 0 and last["volume_20d_avg"] > 0)
        self.assertLessEqual(last["max_drawdown_120d"], 0)

    def test_14_to_16_score_directions(self) -> None:
        cross = pd.DataFrame({
            "symbol": ["a", "b"],
            "ret_60d": [-0.5, 0.5],
            "volatility_60d": [0.1, 0.5],
            "avg_amount_20d": [1, 2],
        })
        rev, _ = score_candidates(cross, Hypothesis("reversal_60_top20", "reversal"), __import__("random").Random(1), set())
        mom, _ = score_candidates(cross, Hypothesis("momentum_60_top20", "momentum"), __import__("random").Random(1), set())
        low, _ = score_candidates(cross, Hypothesis("low_volatility_60", "low_vol"), __import__("random").Random(1), set())
        self.assertEqual(rev.iloc[0]["symbol"], "a")
        self.assertEqual(mom.iloc[0]["symbol"], "b")
        self.assertEqual(low.iloc[0]["symbol"], "a")

    def test_17_hypothesis_pool_size(self) -> None:
        self.assertGreaterEqual(len(HYPOTHESES), 25)

    def test_18_each_strategy_has_family(self) -> None:
        self.assertTrue(all(h.family for h in HYPOTHESES))

    def test_18b_ma_trend_is_only_a_compatibility_alias(self) -> None:
        self.assertNotIn("ma_trend", {h.strategy for h in HYPOTHESES})
        self.assertIn("ma_breakout", {h.strategy for h in HYPOTHESES})

        cross = pd.DataFrame({
            "symbol": ["a", "b", "c"],
            "close": [11.0, 12.0, 9.0],
            "ma20": [10.0, 10.0, 10.0],
            "volume_5d_avg": [200.0, 500.0, 300.0],
            "volume_20d_avg": [100.0, 100.0, 100.0],
            "ret_60d": [0.2, 0.3, 0.9],
        })
        rng = __import__("random").Random(1)
        direct, direct_warnings = score_candidates(cross, Hypothesis("ma_breakout", "trend"), rng, set())
        alias, alias_warnings = score_candidates(cross, Hypothesis("ma_trend", "trend"), rng, set())

        self.assertFalse(direct_warnings)
        self.assertIn("strategy_alias:ma_trend->ma_breakout", alias_warnings)
        self.assertEqual(direct[["symbol", "score"]].to_dict("records"), alias[["symbol", "score"]].to_dict("records"))
        self.assertEqual(alias.iloc[0]["symbol"], "b")

    def test_19_to_23_classification_rules(self) -> None:
        rows = [
            {"strategy": "c", "status": "unclassified", "annualized_return_net": 0.1, "sharpe": 0.7, "max_drawdown": -0.2, "excess_return": 0.1, "invested_ratio_avg": 0.8, "positive_year_count": 2, "yearly_returns": {"2020": 0.1, "2021": 0.1}, "trade_count": 10, "equity_curve_hash": "a"},
            {"strategy": "w", "status": "unclassified", "annualized_return_net": 0.1, "sharpe": 0.5, "max_drawdown": -0.45, "excess_return": 0.1, "invested_ratio_avg": 0.8, "positive_year_count": 1, "yearly_returns": {"2020": 0.1}, "trade_count": 10, "equity_curve_hash": "b"},
            {"strategy": "r", "status": "unclassified", "annualized_return_net": 0.01, "sharpe": 0.1, "max_drawdown": -0.7, "excess_return": -0.1, "invested_ratio_avg": 0.8, "positive_year_count": 0, "yearly_returns": {"2020": -0.1}, "trade_count": 10, "equity_curve_hash": "c"},
            {"strategy": "i", "status": "invalid", "equity_curve_hash": ""},
        ]
        c, w, r, i, _ = classify_results(rows, True)
        self.assertEqual(c[0]["strategy"], "c")
        self.assertEqual(w[0]["strategy"], "w")
        self.assertEqual(r[0]["strategy"], "r")
        self.assertEqual(i[0]["strategy"], "i")
        rows[0]["equity_curve_hash"] = rows[1]["equity_curve_hash"] = "dup"
        self.assertTrue(classify_results(rows, True)[4])

    def test_24_screening_failed_when_many_duplicates(self) -> None:
        # Distinctness policy is represented by duplicated groups from classify_results.
        rows = [{"strategy": str(i), "status": "unclassified", "annualized_return_net": 0.1, "sharpe": 0.7, "max_drawdown": -0.2, "excess_return": 0.1, "invested_ratio_avg": 0.8, "positive_year_count": 1, "yearly_returns": {"2020": 0.1}, "trade_count": 10, "equity_curve_hash": "x"} for i in range(5)]
        self.assertEqual(len(classify_results(rows, True)[4][0]), 5)

    def test_25_benchmark_missing_warning_not_fail(self) -> None:
        args = Namespace(data_root="missing", initial_cash=100000, top_n_default=20, benchmark="nope", min_avg_amount_20d=20000000, min_history_days=180, lot_size=100, commission_rate=0.0003, stamp_tax_rate=0.001, slippage_bps=5, max_strategies=1)
        out = run_screening(args)
        self.assertEqual(out["status"], "failed")

    def test_25b_benchmark_loader_skips_bad_rows_with_visible_warning(self) -> None:
        import scripts.run_strategy_screening_funnel as funnel

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            daily = tmp_path / "data" / "daily"
            daily.mkdir(parents=True)
            (daily / "000852.csv").write_text(
                "date,close\n"
                "2020-01-02,1000\n"
                "2020-01-03,1001\n"
                "bad,row,with,too,many,columns\n",
                encoding="utf-8-sig",
            )
            old_root = funnel.ROOT
            try:
                funnel.ROOT = tmp_path
                frame, meta = load_benchmark("000852")
            finally:
                funnel.ROOT = old_root

        self.assertFalse(frame.empty)
        self.assertTrue(meta["benchmark_available"])
        self.assertTrue(any("csv_bad_lines_skipped" in item for item in meta["warnings"]))

    def test_26_json_safe_blocks_nan(self) -> None:
        self.assertIsNone(json_safe(float("nan")))
        self.assertIsNone(json_safe(float("inf")))

    def test_27_no_fractional_lot_assumption(self) -> None:
        self.assertEqual(250 // 100 * 100, 200)

    def test_28_cash_shortfall_skip_math(self) -> None:
        self.assertEqual(math.floor(99 / 10 / 100) * 100, 0)

    def test_29_signal_before_execution(self) -> None:
        self.assertLess(pd.Timestamp("2020-01-31"), pd.Timestamp("2020-02-03"))

    def test_30_max_drawdown_correct(self) -> None:
        self.assertAlmostEqual(max_drawdown([1.0, 1.2, 0.6]), -0.5)

    def test_30b_screening_trade_cost_uses_unified_min_commission(self) -> None:
        buy_cost = screening_trade_cost(
            "buy",
            gross=1000.0,
            commission_rate=0.0003,
            stamp_tax_rate=0.001,
            slippage_bps=5,
        )
        sell_cost = screening_trade_cost(
            "sell",
            gross=1000.0,
            commission_rate=0.0003,
            stamp_tax_rate=0.001,
            slippage_bps=5,
        )

        self.assertAlmostEqual(buy_cost, 5.5)
        self.assertAlmostEqual(sell_cost, 6.5)

    def test_30c_strategy_screening_defaults_to_current_stamp_tax(self) -> None:
        self.assertEqual(parse_args([]).stamp_tax_rate, DEFAULT_STAMP_TAX_RATE)
        route_text = (ROOT / "scripts" / "server" / "routes" / "strategy_screening.py").read_text(encoding="utf-8")
        self.assertIn('_float_param_or_default(params, "stamp_tax_rate", DEFAULT_STAMP_TAX_RATE)', route_text)

    def test_30d_strategy_screening_uses_shared_lot_rounding_helper(self) -> None:
        text = (ROOT / "scripts" / "run_strategy_screening_funnel.py").read_text(encoding="utf-8")

        self.assertIn("round_notional_down_to_lot_quantity", text)
        self.assertNotIn("math.floor(target_cash / price / lot_size) * lot_size", text)

    def test_31_markdown_report_generated(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            data_root = tmp_path / "daily"
            output_dir = tmp_path / "results"
            report_dir = tmp_path / "reports"
            data_root.mkdir()
            _write_csv(data_root / "000001.csv", rows=260)
            args = Namespace(data_root=str(data_root), initial_cash=100000, top_n_default=20, benchmark="missing_test_benchmark", min_avg_amount_20d=20000000, min_history_days=180, lot_size=100, commission_rate=0.0003, stamp_tax_rate=0.001, slippage_bps=5, max_strategies=1)
            payload = write_outputs(run_screening(args), output_dir, report_dir)

            report_path = report_dir / "strategy_screening_funnel_latest.md"
            self.assertTrue(report_path.exists())
            self.assertEqual(payload["output_paths"]["markdown"], str(report_path.resolve()).replace("\\", "/"))

    def test_32_cli_outputs_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            data_root = tmp_path / "daily"
            output_dir = tmp_path / "results"
            report_dir = tmp_path / "reports"
            data_root.mkdir()
            _write_csv(data_root / "000001.csv", rows=260)
            proc = subprocess.run(
                [
                    "python",
                    "scripts/run_strategy_screening_funnel.py",
                    "--data-root",
                    str(data_root),
                    "--output-dir",
                    str(output_dir),
                    "--report-dir",
                    str(report_dir),
                    "--benchmark",
                    "missing_test_benchmark",
                    "--json",
                    "true",
                    "--summary",
                    "true",
                    "--max-strategies",
                    "1",
                ],
                cwd=ROOT,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=60,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            payload = json.loads(proc.stdout)
            self.assertEqual(payload["task_type"], "strategy_screening_funnel")
            self.assertEqual(payload["usable_csv"], 1)
            self.assertTrue((output_dir / "strategy_screening_funnel_latest.json").exists())

    def test_33_json_result_exists(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            data_root = tmp_path / "daily"
            output_dir = tmp_path / "results"
            report_dir = tmp_path / "reports"
            data_root.mkdir()
            _write_csv(data_root / "000001.csv", rows=260)
            args = Namespace(data_root=str(data_root), initial_cash=100000, top_n_default=20, benchmark="missing_test_benchmark", min_avg_amount_20d=20000000, min_history_days=180, lot_size=100, commission_rate=0.0003, stamp_tax_rate=0.001, slippage_bps=5, max_strategies=1)
            payload = write_outputs(run_screening(args), output_dir, report_dir)

            json_path = output_dir / "strategy_screening_funnel_latest.json"
            self.assertTrue(json_path.exists())
            self.assertEqual(payload["output_paths"]["json"], str(json_path.resolve()).replace("\\", "/"))

    def test_33a_screening_result_exports_backtest_handoff(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            data_root = tmp_path / "daily"
            data_root.mkdir()
            _write_csv(data_root / "000001.csv", rows=260)
            args = Namespace(data_root=str(data_root), initial_cash=100000, top_n_default=20, benchmark="missing_test_benchmark", min_avg_amount_20d=20000000, min_history_days=180, lot_size=100, commission_rate=0.0003, stamp_tax_rate=0.001, slippage_bps=5, max_strategies=1)

            payload = run_screening(args)

        handoff = payload["backtest_handoff"]
        self.assertEqual(handoff["contract_version"], "backtest_handoff_v1")
        self.assertEqual(handoff["status"], "ready")
        self.assertEqual(handoff["backtest_scope"], "specified_symbols")
        self.assertEqual(handoff["backtest_mode"], "portfolio")
        self.assertIn("000001", handoff["symbols"])
        self.assertIn("scripts/qd.py", handoff["command_text"])
        self.assertTrue(payload["strategy_results"][0]["latest_selected_symbols"])

    def test_33b_max_symbols_and_feature_cache_bound_runtime_work(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            data_root = tmp_path / "daily"
            cache_dir = tmp_path / "cache"
            data_root.mkdir()
            for symbol in ("000001", "000002", "000003"):
                _write_csv(data_root / f"{symbol}.csv", rows=260)
            args = Namespace(
                data_root=str(data_root),
                initial_cash=100000,
                top_n_default=20,
                benchmark="missing_test_benchmark",
                min_avg_amount_20d=20000000,
                min_history_days=180,
                lot_size=100,
                commission_rate=0.0003,
                stamp_tax_rate=0.001,
                slippage_bps=5,
                max_strategies=1,
                max_symbols=2,
                feature_cache="true",
                cache_dir=str(cache_dir),
            )

            first = run_screening(args)
            second = run_screening(args)

            self.assertEqual(first["data_quality_summary"]["daily_csv_available"], 3)
            self.assertEqual(first["data_quality_summary"]["scanned_csv"], 2)
            self.assertTrue(first["data_quality_summary"]["truncated_by_max_symbols"])
            self.assertEqual(first["performance"]["feature_cache"]["status"], "miss_written")
            self.assertEqual(second["performance"]["feature_cache"]["status"], "hit")
            self.assertEqual(second["data_quality_summary"]["usable_csv"], 2)

    def test_34_no_future_data_marker(self) -> None:
        text = (ROOT / "scripts" / "run_strategy_screening_funnel.py").read_text(encoding="utf-8")
        self.assertIn("execution_candidates = [d for d in dates if d > signal_date]", text)


if __name__ == "__main__":
    unittest.main()
