from __future__ import annotations

import json
import math
import tempfile
import unittest
from pathlib import Path

import pandas as pd

from quantdesk.data.schema_normalizer import normalize_daily_csv, scan_daily_schema
from quantdesk.research.factor_combiner import build_combined_score
from quantdesk.research.strategy_pipeline_repair import (
    board_aware_limit_rate,
    build_pit_mode_status,
    build_price_coverage_universe,
    build_repair_rerun_report,
    build_strategy_pipeline_precheck,
    compute_benchmark_comparison,
    default_strategy_repair_config,
    infer_listing_window_from_csv,
    is_tradable_at_price_coverage,
)
from quantdesk.research.strategy_viability_runner import run_strategy_viability_baselines
from quantdesk.version import API_SCHEMA_VERSION, APP_VERSION, PACKAGE_VERSION
from tests._cli_capture import run_qd_json

ROOT = Path(__file__).resolve().parents[1]


def _write_daily(path: Path, symbol: str = "600000", *, chinese: bool = False, bad: bool = False) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    dates = pd.bdate_range("2025-01-01", periods=130)
    rows = []
    price = 10.0
    for idx, date in enumerate(dates):
        price *= 1.0 + 0.001 + (idx % 5 - 2) * 0.0002
        if chinese:
            rows.append({"日期": date.strftime("%Y-%m-%d"), "股票代码": symbol, "开盘": price, "最高": price * 1.01, "最低": price * 0.99, "收盘": price, "成交量": 1000000, "成交额": price * 1000000})
        elif bad:
            rows.append({"symbol": symbol, "open": price})
        else:
            rows.append({"date": date.strftime("%Y-%m-%d"), "symbol": symbol, "open": price, "high": price * 1.01, "low": price * 0.99, "close": price, "volume": 1000000, "amount": price * 1000000})
    pd.DataFrame(rows).to_csv(path, index=False, encoding="utf-8")
    return path


def _assert_json_safe(value):
    if isinstance(value, dict):
        for item in value.values():
            _assert_json_safe(item)
    elif isinstance(value, list):
        for item in value:
            _assert_json_safe(item)
    elif isinstance(value, float):
        assert math.isfinite(value)


class StrategyPipelineRepairV3702Tests(unittest.TestCase):
    _shared_tmp: tempfile.TemporaryDirectory | None = None
    _shared_root: Path | None = None
    _shared_baseline_payload: dict | None = None

    @classmethod
    def setUpClass(cls) -> None:
        cls._shared_tmp = tempfile.TemporaryDirectory()
        cls._shared_root = Path(cls._shared_tmp.name)
        daily = cls._shared_root / "data" / "daily"
        for idx in range(12):
            _write_daily(daily / f"600{idx:03d}.csv", f"600{idx:03d}")

    @classmethod
    def tearDownClass(cls) -> None:
        if cls._shared_tmp is not None:
            cls._shared_tmp.cleanup()
        cls._shared_tmp = None
        cls._shared_root = None
        cls._shared_baseline_payload = None

    @classmethod
    def shared_baseline_payload(cls) -> dict:
        if cls._shared_root is None:
            raise AssertionError("shared test root is not initialized")
        if cls._shared_baseline_payload is None:
            cls._shared_baseline_payload = run_strategy_viability_baselines({"data_root": str(cls._shared_root / "data"), "max_symbols": 12, "sample_seed": 7})
        return cls._shared_baseline_payload

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.daily = self.root / "data" / "daily"
        self.daily.mkdir(parents=True, exist_ok=True)

    def _seed_daily(self, count: int = 12) -> None:
        for idx in range(12):
            if idx >= count:
                break
            _write_daily(self.daily / f"600{idx:03d}.csv", f"600{idx:03d}")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_001_version(self):
        self.assertIn(PACKAGE_VERSION, {"3.7.0.2", "3.7.0.3", "3.7.0.4", "3.7.0.5", "3.7.0.6"})
        self.assertIn(API_SCHEMA_VERSION, {"3.7.0.2", "3.7.0.3", "3.7.0.4", "3.7.0.5", "3.7.0.6"})
        self.assertIn(APP_VERSION, {"3.7.0.2-strategy-pipeline-repair", "3.7.0.3-low-price-pipeline-finalization", "3.7.0.4-capital-lot-sensitivity-and-git-hygiene", "3.7.0.5-git-deletion-reconciliation", "3.7.0.6-strategy-regression-guard-and-report-hygiene"})

    def test_002_strict_missing_historical_listings_fails(self):
        status = build_pit_mode_status("strict", data_root=self.root / "data")
        self.assertFalse(status["ok"])
        self.assertEqual(status["status"], "failed")

    def test_003_price_coverage_missing_historical_listings_warns(self):
        status = build_pit_mode_status("price_coverage", data_root=self.root / "data")
        self.assertTrue(status["ok"])
        self.assertEqual(status["survivorship_bias_risk"], "high")

    def test_004_off_mode_debug_only(self):
        status = build_pit_mode_status("off", data_root=self.root / "data")
        self.assertTrue(status["debug_only"])

    def test_005_price_coverage_infers_window(self):
        self._seed_daily(1)
        window = infer_listing_window_from_csv(self.daily / "600000.csv")
        self.assertEqual(window["symbol"], "600000")
        self.assertLess(window["first_date"], window["last_date"])

    def test_006_price_coverage_tradable(self):
        self._seed_daily(1)
        coverage = build_price_coverage_universe(self.root / "data")
        self.assertTrue(is_tradable_at_price_coverage("600000", "2025-03-03", coverage))

    def test_007_survivorship_high_without_history(self):
        self._seed_daily(1)
        payload = build_strategy_pipeline_precheck({"data_root": str(self.root / "data")})
        self.assertIn(payload["pipeline_repair_summary"]["survivorship_bias_risk"], {"high", "unknown"})

    def test_008_chinese_columns_normalize(self):
        path = _write_daily(self.daily / "600999.csv", "600999", chinese=True)
        frame, meta = normalize_daily_csv(path)
        self.assertFalse(frame.empty)
        self.assertTrue(meta["has_chinese_columns"])

    def test_009_missing_optional_fields_do_not_fail(self):
        self._seed_daily(1)
        frame, meta = normalize_daily_csv(self.daily / "600000.csv")
        self.assertFalse(frame.empty)
        self.assertIn("missing_optional_pct_chg", meta["warnings"])

    def test_010_symbol_missing_inferred_from_filename(self):
        path = self.daily / "601111.csv"
        pd.DataFrame({"date": ["2025-01-01"], "close": [10.0]}).to_csv(path, index=False)
        frame, meta = normalize_daily_csv(path)
        self.assertEqual(frame["symbol"].iloc[0], "601111")
        self.assertTrue(meta["inferred_symbol"])

    def test_011_bad_csv_quarantined(self):
        path = _write_daily(self.daily / "bad.csv", "600888", bad=True)
        frame, meta = normalize_daily_csv(path)
        self.assertTrue(frame.empty)
        self.assertTrue(meta["quarantined"])

    def test_011b_bad_csv_rows_are_skipped_with_visible_warning(self):
        path = self.daily / "600777.csv"
        path.write_text(
            "\n".join(
                [
                    "date,symbol,open,high,low,close,volume,amount",
                    "2025-01-01,600777,10,11,9,10.5,1000,10500",
                    "bad,row,with,too,many,columns,for,this,csv,boom",
                    "2025-01-02,600777,10.5,11.5,10,11,1000,11000",
                ]
            ),
            encoding="utf-8-sig",
        )

        frame, meta = normalize_daily_csv(path)

        self.assertFalse(frame.empty)
        self.assertEqual(len(frame), 2)
        self.assertFalse(meta["quarantined"])
        self.assertTrue(any("csv_bad_lines_skipped" in item for item in meta["warnings"]))

    def test_012_schema_report_invalid_files(self):
        self._seed_daily(1)
        _write_daily(self.daily / "bad.csv", "600888", bad=True)
        payload = scan_daily_schema(self.root / "data")
        self.assertIn("invalid_files", payload)

    def test_013_factor_weights_single(self):
        panel = pd.DataFrame({"date": ["2025-01-01", "2025-01-01"], "symbol": ["600000", "600001"], "momentum_20d": [1.0, 2.0]})
        payload = build_combined_score(panel, ["momentum_20d"], manual_weights={"momentum_20d": 1.0}, neutralize_industry=False, neutralize_market_cap=False)
        self.assertEqual(payload["raw_weights"]["momentum_20d"], 1.0)

    def test_014_factor_weights_negative(self):
        panel = pd.DataFrame({"date": ["2025-01-01", "2025-01-01"], "symbol": ["600000", "600001"], "volatility_20d": [1.0, 2.0]})
        payload = build_combined_score(panel, ["volatility_20d"], manual_weights={"volatility_20d": -1.0}, neutralize_industry=False, neutralize_market_cap=False)
        self.assertLess(payload["normalized_weights"]["volatility_20d"], 0)

    def test_015_factor_weights_multi(self):
        panel = pd.DataFrame({"date": ["2025-01-01", "2025-01-01"], "symbol": ["600000", "600001"], "momentum_20d": [1.0, 2.0], "volatility_20d": [2.0, 1.0]})
        payload = build_combined_score(panel, ["momentum_20d", "volatility_20d"], manual_weights={"momentum_20d": 0.6, "volatility_20d": -0.4}, neutralize_industry=False, neutralize_market_cap=False)
        self.assertEqual(set(payload["factor_names"]), {"momentum_20d", "volatility_20d"})

    def test_016_unknown_factor_error_cli(self):
        payload = run_qd_json(["factor-combine", "--weights", "unknown=1", "--synthetic-test-data", "true", "--json", "true", "--summary", "true"], check=False)
        self.assertFalse(payload["ok"])

    def test_017_zero_cross_section_not_normal_success(self):
        payload = build_combined_score(pd.DataFrame(), ["momentum_20d"])
        self.assertEqual(payload["status"], "warning")
        self.assertTrue(payload["insufficient_cross_section"])

    def test_018_default_config(self):
        cfg = default_strategy_repair_config()
        self.assertEqual(cfg["max_positions"], 20)
        self.assertEqual(cfg["rebalance"], "monthly")
        self.assertNotEqual(cfg["universe"], "code_sorted_first_80")

    def test_019_random_sample_seed_recorded(self):
        payload = self.shared_baseline_payload()
        self.assertEqual(payload["strategy_config"]["sample_seed"], 7)

    def test_020_benchmark_missing_warns(self):
        bench = compute_benchmark_comparison(0.1, self.root / "data")
        self.assertFalse(bench["available"])
        self.assertTrue(bench["warnings"])

    def test_021_benchmark_outputs_excess_when_available(self):
        idx = self.root / "data" / "index" / "000905.csv"
        _write_daily(idx, "000905")
        bench = compute_benchmark_comparison(0.1, self.root / "data")
        values = list(bench["comparisons"].values())
        self.assertTrue(any(item["excess_return"] is not None for item in values))

    def test_022_board_aware_limit_rates(self):
        self.assertEqual(board_aware_limit_rate("600000")["limit_rate"], 0.10)
        self.assertEqual(board_aware_limit_rate("300750")["limit_rate"], 0.20)
        self.assertEqual(board_aware_limit_rate("688001")["limit_rate"], 0.20)
        self.assertEqual(board_aware_limit_rate("600001", "ST sample")["limit_rate"], 0.05)
        self.assertTrue(board_aware_limit_rate("123456")["warnings"])

    def test_023_qd_strategy_pipeline_json(self):
        self._seed_daily(1)
        payload = run_qd_json(["strategy-pipeline", "precheck", "--data-root", str(self.root / "data"), "--json", "true", "--summary", "true"])
        self.assertEqual(payload["task_type"], "strategy_pipeline_precheck")

    def test_024_qd_factor_combine_weights_json(self):
        payload = run_qd_json(["factor-combine", "--weights", "momentum_20d=1.0", "--synthetic-test-data", "true", "--json", "true", "--summary", "true"])
        self.assertEqual(payload["task_type"], "factor_combination")
        self.assertIn("normalized_weights", payload)

    def test_025_qd_combined_score_backtest_pit_mode_json(self):
        self._seed_daily(1)
        payload = run_qd_json(["combined-score-backtest", "--pit-mode", "price_coverage", "--daily-dir", str(self.daily), "--json", "true", "--summary", "true"])
        self.assertEqual(payload["task_type"], "combined_score_backtest")
        self.assertEqual(payload["pit_mode"], "price_coverage")

    def test_026_strategy_report_contains_six_rows(self):
        baseline = self.shared_baseline_payload()
        if self._shared_root is None:
            raise AssertionError("shared test root is not initialized")
        payload = build_repair_rerun_report(baseline, data_root=self._shared_root / "data")
        self.assertEqual(payload["strategy_count"], 6)

    def test_027_gross_net_metrics_exist(self):
        payload = self.shared_baseline_payload()
        metrics = payload["baselines"][0]["metrics"]
        self.assertIn("annual_return_gross", metrics)
        self.assertIn("annual_return_net", metrics)

    def test_028_json_no_nan(self):
        self._seed_daily(1)
        payload = build_strategy_pipeline_precheck({"data_root": str(self.root / "data")})
        _assert_json_safe(payload)

    def test_029_partial_daily_isolated(self):
        self._seed_daily(1)
        payload = build_strategy_pipeline_precheck({"data_root": str(self.root / "data")})
        self.assertTrue(payload["partial_daily"]["isolated"])

    def test_030_self_check_v3702_marker_present(self):
        text = (ROOT / "scripts" / "self_check.py").read_text(encoding="utf-8")
        self.assertIn("v3702_strategy_pipeline_repair", text)


if __name__ == "__main__":
    unittest.main()
