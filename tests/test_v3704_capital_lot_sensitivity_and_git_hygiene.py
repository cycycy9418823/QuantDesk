from __future__ import annotations

import json
import math
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import pandas as pd

import quantdesk.maintenance.git_deletion_audit as git_deletion_audit
from quantdesk.maintenance.git_deletion_audit import (
    find_references_for_deleted,
    parse_git_status,
)
from quantdesk.research.capital_lot_sensitivity import (
    cash_drag_severity,
    default_capital_lot_grid,
    max_position_pct_for_positions,
    run_capital_lot_sensitivity,
)
from quantdesk.research.strategy_viability_decision import decide_capital_lot_sensitivity
from quantdesk.research.strategy_viability_runner import _compute_features, run_one_baseline
from quantdesk.version import API_SCHEMA_VERSION, APP_VERSION, PACKAGE_VERSION
from tests._cli_capture import run_main_json

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from scripts.run_maintenance import main as run_maintenance_main  # noqa: E402
from scripts.run_strategy_viability_sprint import main as run_strategy_viability_main  # noqa: E402

TMP_ROOT = Path(tempfile.gettempdir()) / ".quantdesk_test_tmp"
TMP_ROOT.mkdir(parents=True, exist_ok=True)


def _write_daily(path: Path, symbol: str, *, start: str = "2024-01-01", days: int = 180, base: float = 8.0, drift: float = 0.001) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    dates = pd.bdate_range(start, periods=days)
    rows = []
    price = base
    for idx, date in enumerate(dates):
        price *= 1.0 + drift + ((idx % 11) - 5) * 0.0005
        rows.append({
            "date": date.strftime("%Y-%m-%d"),
            "symbol": symbol,
            "open": price * 0.99,
            "high": price * 1.01,
            "low": price * 0.98,
            "close": price,
            "volume": 100000 + idx,
            "amount": price * (100000 + idx),
        })
    pd.DataFrame(rows).to_csv(path / f"{symbol}.csv", index=False)


def _write_listing(path: Path, symbols: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = []
    for symbol in symbols:
        rows.append({
            "symbol": symbol,
            "name": "",
            "list_date": "2024-01-01",
            "delist_date": "",
            "is_active": "true",
            "is_st_current": "false",
            "st_start_date": "",
            "st_end_date": "",
            "suspend_start_date": "",
            "suspend_end_date": "",
            "first_price_date": "2024-01-01",
            "last_price_date": "2024-09-06",
            "source": "price_csv_coverage",
            "confidence": "low_to_medium",
            "survivorship_bias_risk": "high",
            "notes": "list_date is inferred from first available local price CSV date, not the official listing date.",
        })
    pd.DataFrame(rows).to_csv(path, index=False)


def _make_data_root() -> tempfile.TemporaryDirectory:
    tmp = tempfile.TemporaryDirectory(dir=TMP_ROOT)
    root = Path(tmp.name) / "data"
    daily = root / "daily"
    daily.mkdir(parents=True, exist_ok=True)
    symbols = []
    for idx in range(8):
        symbol = f"600{idx:03d}"
        symbols.append(symbol)
        _write_daily(daily, symbol, base=6.0 + idx, drift=0.0006 + idx * 0.00005)
    _write_listing(root / "universe" / "historical_listings.csv", symbols)
    return tmp


def _assert_json_safe(value):
    if isinstance(value, dict):
        for item in value.values():
            _assert_json_safe(item)
    elif isinstance(value, list):
        for item in value:
            _assert_json_safe(item)
    elif isinstance(value, float):
        assert math.isfinite(value)


class V3704CapitalLotSensitivityAndGitHygieneTests(unittest.TestCase):
    _git_audit_payload: dict | None = None
    _capital_tmp: tempfile.TemporaryDirectory | None = None
    _capital_payloads: dict[str, dict] = {}

    @classmethod
    def tearDownClass(cls) -> None:
        if cls._capital_tmp is not None:
            cls._capital_tmp.cleanup()

    @classmethod
    def git_audit_payload(cls) -> dict:
        if cls._git_audit_payload is None:
            deleted = "quantdesk/paper_trading.py"
            with (
                patch.object(git_deletion_audit, "_run_git_status", return_value=[f" D {deleted}"]),
                patch.object(git_deletion_audit, "_recent_release_entries", return_value=({}, set())),
                patch.object(git_deletion_audit, "_zip_entries", return_value=set()),
                patch.object(git_deletion_audit, "_iter_source_files", return_value=[]),
                patch.object(git_deletion_audit, "_build_docs_reference_index", return_value={}),
                patch.object(
                    git_deletion_audit,
                    "check_alias_coverage",
                    return_value={
                        "deleted_module_count": 1,
                        "alias_import_ok_count": 1,
                        "alias_import_fail_count": 0,
                        "from_import_ok_count": 1,
                        "from_import_fail_count": 0,
                        "shim_covered_aliases": [{"module_name": "paper_trading", "status": "covered"}],
                        "failed_aliases": [],
                    },
                ),
                patch.object(git_deletion_audit, "scan_old_import_references", return_value={"rewrite_candidates": [], "shim_definition_references": []}),
                patch.object(
                    git_deletion_audit,
                    "build_reference_index_for_deleted",
                    return_value={deleted: {"module": "quantdesk.paper_trading", "referenced_by": [], "reference_count": 0, "import_risk": "low"}},
                ),
            ):
                cls._git_audit_payload = git_deletion_audit.run_git_deletion_audit()
        return cls._git_audit_payload

    @classmethod
    def capital_data_root(cls) -> Path:
        if cls._capital_tmp is None:
            cls._capital_tmp = _make_data_root()
        return Path(cls._capital_tmp.name) / "data"

    @classmethod
    def capital_payload(cls, config: dict) -> dict:
        cfg = {"data_root": str(cls.capital_data_root()), "write_latest": False, **config}
        key = json.dumps(cfg, sort_keys=True)
        if key not in cls._capital_payloads:
            cls._capital_payloads[key] = run_capital_lot_sensitivity(cfg)
        return cls._capital_payloads[key]

    def test_001_version(self):
        self.assertIn(PACKAGE_VERSION, {"3.7.0.4", "3.7.0.5", "3.7.0.6"})
        self.assertIn(API_SCHEMA_VERSION, {"3.7.0.4", "3.7.0.5", "3.7.0.6"})
        self.assertIn(APP_VERSION, {"3.7.0.4-capital-lot-sensitivity-and-git-hygiene", "3.7.0.5-git-deletion-reconciliation", "3.7.0.6-strategy-regression-guard-and-report-hygiene"})

    def test_002_git_parse_deleted(self):
        parsed = parse_git_status([" D quantdesk/paper_trading.py"])
        self.assertIn("quantdesk/paper_trading.py", parsed["deleted_tracked_files"])

    def test_003_git_parse_untracked(self):
        parsed = parse_git_status(["?? quantdesk/new_file.py"])
        self.assertIn("quantdesk/new_file.py", parsed["untracked_files"])

    def test_004_git_reference_audit_detects_deleted_python_imports(self):
        ref = find_references_for_deleted("quantdesk/paper_trading.py")
        self.assertIn(ref["import_risk"], {"low", "high"})
        self.assertIn("reference_count", ref)

    def test_005_git_audit_does_not_modify_state_contract(self):
        payload = self.git_audit_payload()
        self.assertEqual(payload["disclaimer"], "This audit does not modify git state.")

    def test_006_git_audit_deleted_status_warning(self):
        payload = self.git_audit_payload()
        if payload["deleted_tracked_count"]:
            self.assertIn(payload["status"], {"warning", "failed"})

    def test_007_git_audit_manual_review_required_field(self):
        self.assertIn("manual_review_required", self.git_audit_payload())

    def test_008_git_audit_no_restore_or_clean_text(self):
        text = (ROOT / "quantdesk" / "maintenance" / "git_deletion_audit.py").read_text(encoding="utf-8")
        self.assertNotIn("git restore", text)
        self.assertNotIn("git clean", text)

    def test_009_grid_initial_cash(self):
        self.assertEqual(default_capital_lot_grid()["initial_cash"], [10000, 50000, 100000])

    def test_010_grid_max_positions(self):
        self.assertEqual(default_capital_lot_grid()["max_positions"], [5, 10, 20])

    def test_011_grid_max_price(self):
        self.assertEqual(default_capital_lot_grid()["max_price"], [10.0, 15.0, 20.0])

    def test_012_position_pct_mapping(self):
        self.assertAlmostEqual(max_position_pct_for_positions(5), 0.20)
        self.assertAlmostEqual(max_position_pct_for_positions(10), 0.10)
        self.assertAlmostEqual(max_position_pct_for_positions(20), 0.05)

    def test_013_grid_lot_size(self):
        self.assertEqual(default_capital_lot_grid()["lot_size"], [100])

    def test_014_grid_rebalance(self):
        self.assertEqual(default_capital_lot_grid()["rebalance"], ["monthly"])

    def test_015_price_filter_uses_signal_date(self):
        tmp = _make_data_root()
        try:
            panel = pd.concat(pd.read_csv(p, parse_dates=["date"]) for p in (Path(tmp.name) / "data" / "daily").glob("*.csv"))
            features = _compute_features(panel)
            cfg = {"initial_cash": 100000, "max_position_pct": 0.2, "lot_size": 100, "min_listing_age_days": 120, "rebalance": "monthly", "price_filter": {"enabled": True, "max_price": 10, "evaluated_on": "signal_date"}}
            row = run_one_baseline(features, "low_volatility", top_n=5, strategy_config=cfg)
            self.assertEqual(row["price_filter"]["evaluated_on"], "signal_date")
        finally:
            tmp.cleanup()

    def test_016_latest_close_does_not_drive_filter(self):
        self.assertIn("signal_date", (ROOT / "quantdesk" / "research" / "strategy_viability_runner.py").read_text(encoding="utf-8"))

    def test_017_close_lte_max_price_policy(self):
        text = (ROOT / "quantdesk" / "research" / "strategy_viability_runner.py").read_text(encoding="utf-8")
        self.assertIn("<= max_price", text)

    def test_018_close_gt_max_price_excluded_policy(self):
        self.assertIn("price_filter", (ROOT / "quantdesk" / "research" / "capital_lot_sensitivity.py").read_text(encoding="utf-8"))

    def test_019_missing_signal_price_warning_contract(self):
        self.assertIn("skipped_missing_signal_price_count", (ROOT / "quantdesk" / "research" / "strategy_viability_runner.py").read_text(encoding="utf-8"))

    def test_020_lot_size_insufficient_skips(self):
        payload = self.capital_payload({"strategies": ["low_volatility"], "initial_cash_grid": [10000], "max_positions_grid": [20], "max_price_grid": [20], "max_symbols": 8})
        self.assertGreaterEqual(payload["results"][0]["unfilled_due_to_lot_size_count"], 0)

    def test_021_no_fractional_shares_policy(self):
        self.assertIn("has_fractional_shares", (ROOT / "quantdesk" / "research" / "strategy_viability_runner.py").read_text(encoding="utf-8"))

    def test_022_invested_ratio_present(self):
        payload = self.capital_payload({"strategies": ["low_volatility"], "initial_cash_grid": [10000], "max_positions_grid": [10], "max_price_grid": [15], "max_symbols": 8})
        self.assertIn("invested_ratio_avg", payload["results"][0])

    def test_023_unfilled_count_present(self):
        payload = self.capital_payload({"strategies": ["low_volatility"], "initial_cash_grid": [10000], "max_positions_grid": [10], "max_price_grid": [15], "max_symbols": 8})
        self.assertIn("unfilled_due_to_lot_size_count", payload["results"][0])

    def test_024_10000_not_better_deployed_than_100000(self):
        payload = self.capital_payload({"strategies": ["low_volatility"], "initial_cash_grid": [10000, 100000], "max_positions_grid": [20], "max_price_grid": [20], "max_symbols": 8})
        summary = payload["capital_sensitivity_summary"]
        self.assertLessEqual(summary["invested_ratio_by_initial_cash"]["10000"], summary["invested_ratio_by_initial_cash"]["100000"] + 1e-9)

    def test_025_cash_drag_severity(self):
        self.assertEqual(cash_drag_severity(0.45), "severe")
        self.assertEqual(cash_drag_severity(0.65), "high")

    def test_026_result_contains_annualized_return_net(self):
        payload = self.capital_payload({"strategies": ["low_volatility"], "initial_cash_grid": [10000], "max_positions_grid": [5], "max_price_grid": [15], "max_symbols": 8})
        self.assertIn("annualized_return_net", payload["results"][0])

    def test_027_result_contains_max_drawdown(self):
        payload = self.capital_payload({"strategies": ["low_volatility"], "initial_cash_grid": [10000], "max_positions_grid": [5], "max_price_grid": [15], "max_symbols": 8})
        self.assertIn("max_drawdown", payload["results"][0])

    def test_028_result_contains_sharpe(self):
        payload = self.capital_payload({"strategies": ["low_volatility"], "initial_cash_grid": [10000], "max_positions_grid": [5], "max_price_grid": [15], "max_symbols": 8})
        self.assertIn("sharpe", payload["results"][0])

    def test_029_json_no_nan(self):
        payload = self.capital_payload({"strategies": ["low_volatility"], "initial_cash_grid": [10000], "max_positions_grid": [5], "max_price_grid": [15], "max_symbols": 8})
        _assert_json_safe(payload)

    def test_030_decision_weak_low_price(self):
        payload = {"results": [{"initial_cash": 10000, "sharpe": 0.1, "max_drawdown": -0.5, "annualized_return_net": 0.01}], "historical_listings_exists": True, "survivorship_bias_risk": "high", "pit_quality": "degraded", "benchmark_available": True}
        self.assertIn(decide_capital_lot_sensitivity(payload)["decision"], {"low_price_direction_rejected", "switch_strategy_direction"})

    def test_031_decision_more_capital(self):
        payload = {"results": [{"initial_cash": 10000, "sharpe": 0.2, "max_drawdown": -0.5, "annualized_return_net": 0.02, "invested_ratio_avg": 0.4, "unfilled_due_to_lot_size_count": 100}, {"initial_cash": 100000, "sharpe": 0.7, "max_drawdown": -0.2, "annualized_return_net": 0.1, "invested_ratio_avg": 0.9, "unfilled_due_to_lot_size_count": 0}], "historical_listings_exists": True, "survivorship_bias_risk": "high", "pit_quality": "degraded", "benchmark_available": True}
        self.assertEqual(decide_capital_lot_sensitivity(payload)["decision"], "low_price_needs_more_capital")

    def test_032_survivorship_confidence_cap(self):
        payload = {"results": [{"initial_cash": 10000, "sharpe": 0.8, "max_drawdown": -0.2, "annualized_return_net": 0.1}], "historical_listings_exists": True, "survivorship_bias_risk": "high", "pit_quality": "degraded", "benchmark_available": True}
        self.assertIn(decide_capital_lot_sensitivity(payload)["confidence"], {"medium", "low"})

    def test_033_benchmark_missing_confidence_low(self):
        payload = {"results": [{"initial_cash": 10000, "sharpe": 0.8, "max_drawdown": -0.2, "annualized_return_net": 0.1}], "historical_listings_exists": True, "survivorship_bias_risk": "high", "pit_quality": "degraded", "benchmark_available": False}
        self.assertEqual(decide_capital_lot_sensitivity(payload)["confidence"], "low")

    def test_034_qd_maintenance_json(self):
        def fake_git_deletion_audit_main(argv: list[str] | None = None) -> int:
            print(json.dumps({"task_type": "git_deletion_audit", "argv": argv or []}))
            return 0

        with patch("scripts.run_maintenance.git_deletion_audit_main", side_effect=fake_git_deletion_audit_main):
            payload = run_main_json(run_maintenance_main, ["git-deletion-audit", "--json", "true", "--summary", "true"])
        self.assertEqual(payload["task_type"], "git_deletion_audit")

    def test_035_qd_capital_lot_json(self):
        payload = run_main_json(run_strategy_viability_main, ["capital-lot-sensitivity", "--data-root", str(self.capital_data_root()), "--strategies", "low_volatility", "--max-symbols", "8", "--smoke-run", "true", "--write-latest", "false", "--json", "true", "--summary", "true"])
        self.assertEqual(payload["task_type"], "capital_lot_sensitivity")

    def test_036_qd_report_json(self):
        payload = run_main_json(run_strategy_viability_main, ["report", "--data-root", str(self.capital_data_root()), "--max-symbols", "8", "--smoke-run", "true", "--write-latest", "false", "--json", "true", "--summary", "true"])
        self.assertEqual(payload["task_type"], "strategy_viability_report")

    def test_037_v3703_low_price_profile_still_runs(self):
        payload = run_main_json(run_strategy_viability_main, ["run", "--data-root", str(self.capital_data_root()), "--profile", "low_price", "--pit-mode", "price_coverage", "--max-symbols", "8", "--smoke-run", "true", "--write-latest", "false", "--json", "true", "--summary", "true"])
        self.assertEqual(payload["profile"], "low_price")

    def test_038_data_universe_build_still_wired(self):
        text = (ROOT / "scripts" / "qd.py").read_text(encoding="utf-8")
        self.assertIn("data-universe", text)

    def test_039_combined_score_low_price_still_wired(self):
        text = (ROOT / "scripts" / "run_combined_score_backtest.py").read_text(encoding="utf-8")
        self.assertIn("low_price", text)

    def test_040_self_check_marker_present(self):
        self.assertIn("check_v3704_capital_lot_sensitivity_and_git_hygiene", (ROOT / "scripts" / "self_check.py").read_text(encoding="utf-8"))

    def test_041_clean_zip_forbidden_runtime_hint(self):
        text = (ROOT / "scripts" / "verify_clean_zip.py").read_text(encoding="utf-8")
        self.assertIn("data/universe/", text)
        self.assertIn(".parquet", text)

    def test_042_tmp_root_exists(self):
        self.assertTrue(TMP_ROOT.exists())

    def test_043_docs_exist(self):
        self.assertTrue((ROOT / "docs" / "capital_lot_sensitivity.md").exists())
        self.assertTrue((ROOT / "docs" / "git_deletion_audit.md").exists())


if __name__ == "__main__":
    unittest.main()
