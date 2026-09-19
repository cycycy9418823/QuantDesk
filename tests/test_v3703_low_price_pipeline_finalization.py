from __future__ import annotations

import json
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path

import pandas as pd
from quantdesk.point_in_time_universe import (
    SCHEMA_COLUMNS,
    is_tradable_at,
    load_historical_listings,
    summarize_pit_universe_status,
)

from quantdesk.research.strategy_pipeline_repair import get_low_price_strategy_viability_config, load_benchmark_series
from quantdesk.research.strategy_viability_runner import (
    CostConfig,
    _compute_features,
    load_price_panel,
    run_one_baseline,
    run_strategy_viability_baselines,
)
from quantdesk.version import API_SCHEMA_VERSION, APP_VERSION, PACKAGE_VERSION
from scripts.build_combined_score import main as build_combined_score_main
from scripts.build_historical_listings import build_historical_listings
from scripts.run_combined_score_backtest import main as run_combined_score_backtest_main
from scripts.run_data_universe import main as run_data_universe_main
from scripts.qd import print_help as print_qd_help
from scripts.validate_historical_listings import validate_historical_listings
from tests._cli_capture import run_main_json

ROOT = Path(__file__).resolve().parents[1]
TMP_ROOT = Path(tempfile.gettempdir()) / ".quantdesk_test_tmp"
TMP_ROOT.mkdir(parents=True, exist_ok=True)


def _write_daily(path: Path, symbol: str, prices: list[float], *, start: str = "2020-01-01", include_symbol: bool = True) -> None:
    dates = pd.bdate_range(start, periods=len(prices))
    rows = []
    for idx, price in enumerate(prices):
        row = {
            "date": dates[idx].strftime("%Y-%m-%d"),
            "open": price,
            "high": price * 1.02,
            "low": price * 0.98,
            "close": price,
            "volume": 100000,
            "amount": price * 100000,
        }
        if include_symbol:
            row["symbol"] = symbol
        rows.append(row)
    pd.DataFrame(rows).to_csv(path / f"{symbol}.csv", index=False)


class LowPricePipelineFinalizationV3703Tests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(dir=TMP_ROOT)
        self.root = Path(self.tmp.name)
        self.data = self.root / "data"
        self.daily = self.data / "daily"
        self.daily.mkdir(parents=True)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_001_version(self) -> None:
        self.assertIn(PACKAGE_VERSION, {"3.7.0.3", "3.7.0.4", "3.7.0.5", "3.7.0.6"})
        self.assertIn(APP_VERSION, {"3.7.0.3-low-price-pipeline-finalization", "3.7.0.4-capital-lot-sensitivity-and-git-hygiene", "3.7.0.5-git-deletion-reconciliation", "3.7.0.6-strategy-regression-guard-and-report-hygiene"})
        self.assertIn(API_SCHEMA_VERSION, {"3.7.0.3", "3.7.0.4", "3.7.0.5", "3.7.0.6"})

    def test_002_build_listings_from_temp_csv(self) -> None:
        _write_daily(self.daily, "600001", [8.0, 8.2, 8.3], include_symbol=False)
        output = self.data / "universe" / "historical_listings.csv"
        payload = build_historical_listings(data_root=self.data, output_path=output, write=True, network=False)
        self.assertTrue(output.exists())
        self.assertGreater(payload["row_count"], 0)
        frame = pd.read_csv(output, dtype=str)
        self.assertTrue(set(SCHEMA_COLUMNS).issubset(frame.columns))
        self.assertEqual(frame.iloc[0]["list_date"], frame.iloc[0]["first_price_date"])
        self.assertIn("not the official listing date", frame.iloc[0]["notes"])

    def test_003_validate_generated_listings_warning(self) -> None:
        _write_daily(self.daily, "600002", [9.0, 9.1, 9.2])
        output = self.data / "universe" / "historical_listings.csv"
        build_historical_listings(data_root=self.data, output_path=output, write=True)
        payload = validate_historical_listings(output)
        self.assertTrue(payload["ok"])
        self.assertIn(payload["status"], {"warning", "passed"})
        self.assertEqual(payload["pit_quality"], "degraded")
        self.assertEqual(payload["survivorship_bias_risk"], "high")

    def test_004_missing_date_skipped(self) -> None:
        pd.DataFrame([{"symbol": "600003", "close": 8.0}]).to_csv(self.daily / "600003.csv", index=False)
        payload = build_historical_listings(data_root=self.data, output_path=self.data / "universe" / "historical_listings.csv", write=True)
        self.assertEqual(payload["row_count"], 0)
        self.assertFalse(payload["ok"])
        self.assertTrue(payload["skipped_files"])

    def test_005_missing_close_skipped(self) -> None:
        pd.DataFrame([{"date": "2020-01-01", "symbol": "600004"}]).to_csv(self.daily / "600004.csv", index=False)
        payload = build_historical_listings(data_root=self.data, output_path=self.data / "universe" / "historical_listings.csv", write=True)
        self.assertEqual(payload["row_count"], 0)
        self.assertFalse(payload["ok"])

    def test_006_symbol_missing_inferred(self) -> None:
        _write_daily(self.daily, "600005", [8.0, 8.1], include_symbol=False)
        output = self.data / "universe" / "historical_listings.csv"
        build_historical_listings(data_root=self.data, output_path=output, write=True)
        frame = pd.read_csv(output, dtype=str)
        self.assertEqual(frame.iloc[0]["symbol"], "600005")

    def test_007_pit_load_and_status_not_missing(self) -> None:
        _write_daily(self.daily, "600006", [8.0, 8.1])
        output = self.data / "universe" / "historical_listings.csv"
        build_historical_listings(data_root=self.data, output_path=output, write=True)
        listings, warnings = load_historical_listings(output)
        status = summarize_pit_universe_status(listings, output)
        self.assertFalse(any("missing_historical_listings" in item for item in warnings))
        self.assertTrue(status["historical_listings_exists"])
        self.assertGreater(status["historical_listings_row_count"], 0)
        self.assertEqual(status["pit_quality"], "degraded")
        self.assertEqual(status["survivorship_bias_risk"], "high")

    def test_008_is_tradable_at_dates(self) -> None:
        _write_daily(self.daily, "600007", [8.0, 8.1], start="2020-01-02")
        output = self.data / "universe" / "historical_listings.csv"
        build_historical_listings(data_root=self.data, output_path=output, write=True)
        listings, _ = load_historical_listings(output)
        before, _meta = is_tradable_at("600007", "2019-12-31", listings)
        after, _meta = is_tradable_at("600007", "2020-01-03", listings)
        self.assertFalse(before)
        self.assertTrue(after)

    def test_009_low_price_config(self) -> None:
        cfg = get_low_price_strategy_viability_config()
        self.assertEqual(cfg["initial_cash"], 10000)
        self.assertEqual(cfg["max_positions"], 10)
        self.assertEqual(cfg["max_position_pct"], 0.10)
        self.assertEqual(cfg["rebalance"], "monthly")
        self.assertEqual(cfg["price_filter"]["max_price"], 15.0)
        self.assertNotEqual(cfg["max_positions"], 5)
        self.assertNotEqual(cfg["rebalance"], "weekly")
        self.assertEqual(cfg["universe_mode"], "full_eligible_universe")

    def test_009b_strategy_viability_default_stamp_tax_uses_current_a_share_rate(self) -> None:
        self.assertEqual(CostConfig().stamp_tax_bps, 5.0)

    def test_010_price_filter_uses_signal_date_not_latest(self) -> None:
        prices = [8.0] * 160 + [30.0] * 40
        _write_daily(self.daily, "600008", prices)
        panel, warnings = load_price_panel(self.data)
        self.assertFalse(panel.empty, warnings)
        features = _compute_features(panel)
        cfg = get_low_price_strategy_viability_config()
        row = run_one_baseline(features, "momentum_20d", top_n=1, strategy_config=cfg)
        selected_any = any("600008" in item.get("symbols", []) for item in row["selection_preview"])
        self.assertTrue(selected_any)
        self.assertLessEqual(row["price_filter"]["max_price"], 15.0)

    def test_011_price_filter_excludes_signal_price_above_max(self) -> None:
        _write_daily(self.daily, "600009", [16.0] * 200)
        panel, _warnings = load_price_panel(self.data)
        features = _compute_features(panel)
        row = run_one_baseline(features, "momentum_20d", top_n=1, strategy_config=get_low_price_strategy_viability_config())
        self.assertEqual(row["metrics"]["trade_count"], 0)

    def test_012_lot_size_unfilled(self) -> None:
        _write_daily(self.daily, "600010", [14.8] * 200)
        panel, _warnings = load_price_panel(self.data)
        features = _compute_features(panel)
        row = run_one_baseline(features, "momentum_20d", top_n=1, strategy_config=get_low_price_strategy_viability_config())
        self.assertGreater(row["metrics"]["unfilled_due_to_lot_size_count"], 0)
        self.assertFalse(row["metrics"]["has_fractional_shares"])

    def test_013_random_sample_seed_recorded(self) -> None:
        payload = run_strategy_viability_baselines({"data_root": self.data, "profile": "low_price", "max_symbols": 1, "sample_seed": 123})
        self.assertEqual(payload["sample_seed"], 123)

    def test_014_strategy_run_summary_low_price(self) -> None:
        for idx in range(12):
            _write_daily(self.daily, f"600{idx:03d}", [8.0 + idx * 0.01] * 180)
        payload = run_strategy_viability_baselines({"data_root": self.data, "profile": "low_price", "max_symbols": 12})
        self.assertEqual(payload["profile"], "low_price")
        self.assertEqual(payload["max_positions"], 10)
        self.assertEqual(payload["rebalance"], "monthly")
        self.assertTrue(payload["price_filter_enabled"])

    def test_015_benchmark_missing_warns(self) -> None:
        status = load_benchmark_series(self.data)
        self.assertFalse(status["available"])
        self.assertTrue(status["warnings"])

    def test_016_benchmark_available_outputs_return(self) -> None:
        _write_daily(self.daily, "000905", [10.0, 11.0])
        status = load_benchmark_series(self.data)
        self.assertTrue(status["available"])

    def test_017_st_name_detected(self) -> None:
        symbols = self.data / "universe"
        symbols.mkdir(parents=True)
        pd.DataFrame([{"symbol": "600011", "name": "*ST Sample"}]).to_csv(symbols / "a_share_symbols.csv", index=False)
        _write_daily(self.daily, "600011", [8.0, 8.1])
        output = symbols / "historical_listings.csv"
        build_historical_listings(data_root=self.data, output_path=output, write=True)
        frame = pd.read_csv(output, dtype=str)
        self.assertEqual(frame.iloc[0]["is_st_current"], "true")

    def test_018_json_no_nan(self) -> None:
        _write_daily(self.daily, "600012", [8.0] * 180)
        payload = run_strategy_viability_baselines({"data_root": self.data, "profile": "low_price", "max_symbols": 1})
        json.dumps(payload, allow_nan=False)

    def test_019_partial_daily_not_used(self) -> None:
        partial = self.data / "intraday" / "partial_daily"
        partial.mkdir(parents=True)
        _write_daily(partial, "600013", [1.0, 1.1])
        payload = build_historical_listings(data_root=self.data, output_path=self.data / "universe" / "historical_listings.csv", write=True)
        self.assertEqual(payload["row_count"], 0)

    def test_020_qd_data_universe_build_json(self) -> None:
        payload = run_main_json(run_data_universe_main, ["build-listings", "--data-root", str(self.data), "--network", "false", "--write", "false", "--json", "true", "--summary", "true"], check=False)
        self.assertEqual(payload["task_type"], "build_historical_listings")

    def test_021_qd_data_universe_validate_json(self) -> None:
        _write_daily(self.daily, "600014", [8.0, 8.1])
        output = self.data / "universe" / "historical_listings.csv"
        build_historical_listings(data_root=self.data, output_path=output, write=True)
        payload = run_main_json(run_data_universe_main, ["validate-listings", "--path", str(output), "--json", "true", "--summary", "true"])
        self.assertEqual(payload["task_type"], "validate_historical_listings")

    def test_022_tmp_root_exists(self) -> None:
        self.assertTrue(TMP_ROOT.exists())

    def test_023_build_payload_contract(self) -> None:
        _write_daily(self.daily, "600015", [8.0, 8.1])
        payload = build_historical_listings(data_root=self.data, output_path=self.data / "universe" / "historical_listings.csv", write=False)
        self.assertEqual(payload["task_type"], "build_historical_listings")
        self.assertEqual(payload["source"], "price_csv_coverage")
        self.assertEqual(payload["confidence"], "low_to_medium")

    def test_024_validate_payload_contract(self) -> None:
        _write_daily(self.daily, "600016", [8.0, 8.1])
        output = self.data / "universe" / "historical_listings.csv"
        build_historical_listings(data_root=self.data, output_path=output, write=True)
        payload = validate_historical_listings(output)
        self.assertEqual(payload["task_type"], "validate_historical_listings")
        self.assertIsInstance(payload["source_distribution"], dict)
        self.assertIsInstance(payload["confidence_distribution"], dict)

    def test_025_no_network_default(self) -> None:
        payload = build_historical_listings(data_root=self.data, output_path=self.data / "universe" / "historical_listings.csv", write=False)
        self.assertFalse(payload["network"])

    def test_026_write_false_does_not_create_csv(self) -> None:
        _write_daily(self.daily, "600017", [8.0, 8.1])
        output = self.data / "universe" / "historical_listings.csv"
        build_historical_listings(data_root=self.data, output_path=output, write=False)
        self.assertFalse(output.exists())

    def test_027_price_filter_summary_counts(self) -> None:
        _write_daily(self.daily, "600018", [8.0] * 180)
        payload = run_strategy_viability_baselines({"data_root": self.data, "profile": "low_price", "max_symbols": 1})
        self.assertIn("average_price_filtered_universe_count", payload)
        self.assertIn("min_price_filtered_universe_count", payload)
        self.assertIn("max_price_filtered_universe_count", payload)

    def test_028_cash_drag_summary(self) -> None:
        _write_daily(self.daily, "600019", [8.0] * 180)
        payload = run_strategy_viability_baselines({"data_root": self.data, "profile": "low_price", "max_symbols": 1})
        self.assertIn("average_cash_drag", payload)
        self.assertIn("invested_ratio_avg", payload)
        self.assertIn("final_cash", payload)

    def test_029_six_strategies_present(self) -> None:
        for idx in range(12):
            _write_daily(self.daily, f"601{idx:03d}", [8.0 + idx * 0.01] * 180)
        payload = run_strategy_viability_baselines({"data_root": self.data, "profile": "low_price", "max_symbols": 12})
        self.assertEqual(payload["baseline_count"], 6)
        self.assertIn("rsi_recovery_weekly_control", {row["name"] for row in payload["baselines"]})

    def test_030_st_filter_quality_degraded(self) -> None:
        _write_daily(self.daily, "600020", [8.0] * 180)
        payload = run_strategy_viability_baselines({"data_root": self.data, "profile": "low_price", "max_symbols": 1})
        self.assertTrue(payload["st_filter_enabled"])
        self.assertFalse(payload["historical_st_period_available"])
        self.assertEqual(payload["st_filter_quality"], "degraded")

    def test_031_combined_score_profile_cli_json(self) -> None:
        payload = run_main_json(run_combined_score_backtest_main, ["--profile", "low_price", "--pit-mode", "price_coverage", "--synthetic-test-data", "true", "--json", "true", "--summary", "true"])
        self.assertEqual(payload["task_type"], "combined_score_backtest")
        self.assertEqual(payload["profile"], "low_price")
        self.assertEqual(payload["pit_mode"], "price_coverage")

    def test_032_factor_combine_weight_smoke(self) -> None:
        payload = run_main_json(build_combined_score_main, ["--weights", "momentum_20d=1.0", "--synthetic-test-data", "true", "--json", "true", "--summary", "true"])
        self.assertEqual(payload["task_type"], "factor_combination")

    def test_033_factor_combine_negative_weight_smoke(self) -> None:
        payload = run_main_json(build_combined_score_main, ["--weights", "volatility_20d=-1.0", "--synthetic-test-data", "true", "--json", "true", "--summary", "true"])
        self.assertEqual(payload["task_type"], "factor_combination")

    def test_034_factor_combine_multi_weight_smoke(self) -> None:
        payload = run_main_json(build_combined_score_main, ["--weights", "momentum_20d=0.6,volatility_20d=-0.4", "--synthetic-test-data", "true", "--json", "true", "--summary", "true"])
        self.assertEqual(payload["task_type"], "factor_combination")

    def test_035_schema_columns_include_v3703_fields(self) -> None:
        self.assertIn("first_price_date", SCHEMA_COLUMNS)
        self.assertIn("last_price_date", SCHEMA_COLUMNS)
        self.assertIn("confidence", SCHEMA_COLUMNS)
        self.assertIn("notes", SCHEMA_COLUMNS)

    def test_036_no_fractional_shares(self) -> None:
        _write_daily(self.daily, "600021", [5.0] * 180)
        panel, _warnings = load_price_panel(self.data)
        row = run_one_baseline(_compute_features(panel), "momentum_20d", top_n=1, strategy_config=get_low_price_strategy_viability_config())
        self.assertFalse(row["metrics"]["has_fractional_shares"])

    def test_037_price_filter_evaluated_on_signal_date(self) -> None:
        cfg = get_low_price_strategy_viability_config()
        self.assertEqual(cfg["price_filter"]["evaluated_on"], "signal_date")
        self.assertTrue(cfg["price_filter"]["no_lookahead"])

    def test_038_general_profile_still_available(self) -> None:
        payload = run_strategy_viability_baselines({"data_root": self.data, "profile": "general", "max_symbols": 1})
        self.assertEqual(payload["profile"], "general")

    def test_039_sourceportable_forbidden_runtime_hint(self) -> None:
        forbidden = "data/universe/historical_listings.csv"
        verify_text = (ROOT / "scripts" / "verify_clean_zip.py").read_text(encoding="utf-8")
        self.assertIn("data/universe/", verify_text)
        self.assertTrue(forbidden.startswith("data/universe/"))

    def test_040_release_notes_exist(self) -> None:
        # Release notes may live in the project root or be archived under
        # docs/history/ after repository slimming; accept either location.
        self.assertTrue(
            (ROOT / "RELEASE_NOTES_v3.7.0.3.md").exists()
            or (ROOT / "docs" / "history" / "RELEASE_NOTES_v3.7.0.3.md").exists()
        )
        self.assertTrue((ROOT / "docs" / "low_price_strategy_viability.md").exists())

    def test_041_qd_help_mentions_data_universe(self) -> None:
        stdout = StringIO()
        with redirect_stdout(stdout):
            print_qd_help()
        self.assertIn("data-universe", stdout.getvalue())

    def test_042_self_check_marker_present(self) -> None:
        text = (ROOT / "scripts" / "self_check.py").read_text(encoding="utf-8")
        self.assertIn("check_v3703_low_price_pipeline_finalization", text)


if __name__ == "__main__":
    unittest.main()
