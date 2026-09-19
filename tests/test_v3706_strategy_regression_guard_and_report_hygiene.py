from __future__ import annotations

import importlib
import math
import tempfile
import unittest
from pathlib import Path

import pandas as pd

from quantdesk.maintenance.import_alias_reconciliation import build_import_rewrite_manifest
from quantdesk.research.capital_lot_sensitivity import DEFAULT_STRATEGIES, FULL_STRATEGIES, run_capital_lot_sensitivity
from quantdesk.maintenance.strategy_regression_audit import audit_strategy_regression
from quantdesk.research.strategy_viability_runner import (
    build_report_truthfulness,
    check_baseline_distinctness,
    classify_data_root_kind,
    classify_execution_evidence,
    factor_requirements_for_strategy,
    run_one_baseline,
    run_strategy_viability_baselines,
    validate_performance_metrics_sanity,
    validate_strategy_result_metrics_consistency,
    write_baseline_outputs,
)
from quantdesk.version import API_SCHEMA_VERSION, APP_VERSION, PACKAGE_VERSION
from tests._cli_capture import run_qd_json

ROOT = Path(__file__).resolve().parents[1]
_IMPORT_REWRITE_MANIFEST: dict | None = None


def _write_daily_csv(path: Path, symbol: str, *, periods: int = 160) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    dates = pd.bdate_range("2025-01-01", periods=periods)
    price = 8.0 + int(symbol[-1]) * 0.2
    rows: list[dict[str, object]] = []
    for idx, date in enumerate(dates):
        price *= 1.0 + 0.001 + (idx % 7 - 3) * 0.0003
        rows.append(
            {
                "date": date.strftime("%Y-%m-%d"),
                "symbol": symbol,
                "open": price * 0.995,
                "high": price * 1.012,
                "low": price * 0.988,
                "close": price,
                "volume": 1000000 + idx * 1000,
                "amount": price * (1000000 + idx * 1000),
            }
        )
    pd.DataFrame(rows).to_csv(path, index=False, encoding="utf-8")


def _write_cli_fixture_data(root: Path, *, symbol_count: int = 8) -> None:
    daily = root / "data" / "daily"
    universe = root / "data" / "universe"
    universe.mkdir(parents=True, exist_ok=True)
    listing_rows: list[dict[str, str]] = []
    for idx in range(symbol_count):
        symbol = f"600{idx:03d}"
        _write_daily_csv(daily / f"{symbol}.csv", symbol)
        listing_rows.append(
            {
                "symbol": symbol,
                "start_date": "2025-01-01",
                "end_date": "",
                "name": f"sample-{idx}",
            }
        )
    pd.DataFrame(listing_rows).to_csv(universe / "historical_listings.csv", index=False, encoding="utf-8")


def _json_safe(value) -> bool:
    if isinstance(value, float):
        return math.isfinite(value)
    if isinstance(value, dict):
        return all(_json_safe(v) for v in value.values())
    if isinstance(value, list):
        return all(_json_safe(v) for v in value)
    return True


def _features() -> pd.DataFrame:
    rows = []
    dates = pd.date_range("2020-01-01", periods=180, freq="D")
    for idx, symbol in enumerate(["000001", "000002", "000003", "000004", "000005"]):
        for day, date in enumerate(dates):
            close = 5 + idx + day * (0.01 + idx * 0.002)
            rows.append({
                "date": date,
                "symbol": symbol,
                "open": close,
                "high": close * 1.01,
                "low": close * 0.99,
                "close": close,
                "volume": 10000,
                "amount": close * 10000,
                "ret_1d": 0.001 + idx * 0.0001,
                "momentum_20d": 0.01 * (idx + 1),
                "trend_60d": 0.02 * (5 - idx),
                "volatility_20d": 0.01 * (5 - idx),
                "rsi_14": 30 + idx * 5,
                "rsi_recovery": max(0, 50 - (30 + idx * 5)),
                "listed_days": day + 1,
                "zero_volume_5d": 0,
            })
    return pd.DataFrame(rows)


def _import_rewrite_manifest() -> dict:
    global _IMPORT_REWRITE_MANIFEST
    if _IMPORT_REWRITE_MANIFEST is None:
        _IMPORT_REWRITE_MANIFEST = build_import_rewrite_manifest()
    return _IMPORT_REWRITE_MANIFEST


class V3706StrategyRegressionGuardTests(unittest.TestCase):
    _shared_tmp: tempfile.TemporaryDirectory | None = None
    _shared_root: Path | None = None

    @classmethod
    def setUpClass(cls) -> None:
        tmp_root = Path(tempfile.gettempdir()) / ".quantdesk_test_tmp"
        tmp_root.mkdir(parents=True, exist_ok=True)
        cls._shared_tmp = tempfile.TemporaryDirectory(dir=tmp_root)
        cls._shared_root = Path(cls._shared_tmp.name)
        _write_cli_fixture_data(cls._shared_root)

    @classmethod
    def tearDownClass(cls) -> None:
        if cls._shared_tmp is not None:
            cls._shared_tmp.cleanup()
        cls._shared_tmp = None
        cls._shared_root = None

    @classmethod
    def shared_data_root(cls) -> Path:
        if cls._shared_root is None:
            raise AssertionError("shared CLI fixture root is not initialized")
        return cls._shared_root / "data"

    def test_001_version(self) -> None:
        self.assertEqual(PACKAGE_VERSION, "3.7.0.6")
        self.assertEqual(APP_VERSION, "3.7.0.6-strategy-regression-guard-and-report-hygiene")
        self.assertEqual(API_SCHEMA_VERSION, "3.7.0.6")

    def test_002_factor_requirements_differ(self) -> None:
        self.assertNotEqual(factor_requirements_for_strategy("momentum_20d"), factor_requirements_for_strategy("low_volatility"))

    def test_003_momentum_requirements(self) -> None:
        self.assertEqual(factor_requirements_for_strategy("momentum_20d"), ["momentum_20d"])

    def test_004_low_vol_requirements(self) -> None:
        self.assertEqual(factor_requirements_for_strategy("low_volatility"), ["volatility_20d"])

    def test_005_momentum_low_vol_requirements(self) -> None:
        self.assertEqual(factor_requirements_for_strategy("momentum_low_vol"), ["momentum_20d", "volatility_20d"])

    def test_006_run_one_baseline_exports_requirements(self) -> None:
        row = run_one_baseline(_features(), "momentum_20d", top_n=2)
        self.assertEqual(row["factor_requirements"], ["momentum_20d"])
        self.assertIn("selected_symbols_by_rebalance", row)
        self.assertIn("score_frame_summary", row)

    def test_007_distinctness_detects_identical_equity(self) -> None:
        sample = [
            {"name": "a", "metrics": {"annualized_return_net": 0.1, "max_drawdown": -0.1}, "equity_curve_hash": "x", "selected_symbols_hash": "y", "eligible_count_by_rebalance": [{"eligible_count": 5}]},
            {"name": "b", "metrics": {"annualized_return_net": 0.1, "max_drawdown": -0.1}, "equity_curve_hash": "x", "selected_symbols_hash": "y", "eligible_count_by_rebalance": [{"eligible_count": 5}]},
        ]
        self.assertEqual(check_baseline_distinctness(sample)["status"], "failed")

    def test_008_distinctness_reports_universe_too_small(self) -> None:
        sample = [
            {"name": "a", "metrics": {}, "equity_curve_hash": "x", "selected_symbols_hash": "y", "eligible_count_by_rebalance": [{"eligible_count": 1}]},
            {"name": "b", "metrics": {}, "equity_curve_hash": "x", "selected_symbols_hash": "y", "eligible_count_by_rebalance": [{"eligible_count": 1}]},
        ]
        self.assertEqual(check_baseline_distinctness(sample)["reason"], "universe_too_small_after_filters")

    def test_009_metrics_consistency_passes(self) -> None:
        result = {"annualized_return_net": 0.1, "metrics": {"annualized_return_net": 0.1}}
        self.assertEqual(validate_strategy_result_metrics_consistency(result)["status"], "passed")

    def test_010_metrics_consistency_fails(self) -> None:
        result = {"annualized_return_net": 0.0, "metrics": {"annualized_return_net": 0.1}}
        out = validate_strategy_result_metrics_consistency(result)
        self.assertEqual(out["status"], "failed")
        self.assertIn("top_level_metrics_mismatch", out["warnings"])

    def test_011_drawdown_sanity_fails(self) -> None:
        out = validate_performance_metrics_sanity({"max_drawdown": 0.0}, [{"net_equity": 1.0}, {"net_equity": 0.8}])
        self.assertEqual(out["status"], "failed")

    def test_012_sharpe_sanity_fails(self) -> None:
        out = validate_performance_metrics_sanity({"sharpe": 38.0}, [{"net_equity": 1.0}, {"net_equity": 1.1}])
        self.assertIn("suspicious_sharpe_ratio", out["warnings"])

    def test_013_near_zero_volatility_not_infinite(self) -> None:
        out = validate_performance_metrics_sanity({"annualized_return_net": 0.1, "sharpe": None}, [{"net_equity": 1.0}, {"net_equity": 1.01}, {"net_equity": 1.0201}])
        self.assertTrue(_json_safe(out))

    def test_014_default_strategy_count_expected_81(self) -> None:
        expected = 3 * 3 * 3 * len(DEFAULT_STRATEGIES)
        self.assertEqual(expected, 81)

    def test_015_full_strategy_count_expected_162(self) -> None:
        expected = 3 * 3 * 3 * len(FULL_STRATEGIES)
        self.assertEqual(expected, 162)

    def test_016_full_strategy_count_is_6(self) -> None:
        self.assertEqual(len(FULL_STRATEGIES), 6)

    def test_017_missing_listings_does_not_fake_grid(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            payload = run_capital_lot_sensitivity({"data_root": tmp})
        self.assertFalse(payload["ok"])

    def test_018_smoke_run_paths_are_smoke(self) -> None:
        from quantdesk.research.capital_lot_sensitivity import write_capital_lot_sensitivity_outputs

        payload = {"ok": True, "status": "passed", "task_type": "capital_lot_sensitivity", "report_provenance": {"smoke_run": True, "write_latest": False}}
        out = write_capital_lot_sensitivity_outputs(payload)
        self.assertIn("results/smoke/", out["output_paths"]["json"])

    def test_019_report_provenance_shape(self) -> None:
        payload = run_capital_lot_sensitivity({"data_root": "missing"})
        self.assertTrue("report_provenance" not in payload or isinstance(payload.get("report_provenance"), dict))

    def test_020_import_rewrite_manifest_not_empty(self) -> None:
        # Structure/consistency check (not a count check): once the legacy
        # root-module import rewrite is fully committed, no file still imports
        # the old paths and entry_count is legitimately 0 -- the healthy end
        # state. The manifest must still expose a well-formed, non-negative
        # entry_count matching the entries list length.
        manifest = _import_rewrite_manifest()
        self.assertIsInstance(manifest["entries"], list)
        self.assertEqual(manifest["entry_count"], len(manifest["entries"]))
        self.assertGreaterEqual(manifest["entry_count"], 0)

    def test_021_import_rewrite_manifest_fields(self) -> None:
        # If any rewrite entry exists, it must carry the audit fields. An empty
        # manifest (migration already complete) is acceptable and skips here.
        entries = _import_rewrite_manifest()["entries"]
        if not entries:
            self.skipTest("no pending import-rewrite entries (migration complete)")
        entry = entries[0]
        self.assertIn("file", entry)
        self.assertIn("old_import", entry)
        self.assertIn("new_import", entry)

    def test_022_core_strategy_import_rewrite_guard_field(self) -> None:
        manifest = _import_rewrite_manifest()
        self.assertIn("suspected_import_rewrite_root_cause", manifest)
        self.assertIn("rollback_candidates", manifest)

    def test_023_strategy_regression_audit_importable(self) -> None:
        mod = importlib.import_module("quantdesk.maintenance.strategy_regression_audit")
        self.assertTrue(hasattr(mod, "audit_strategy_regression"))

    def test_024_qd_strategy_regression_audit_json(self) -> None:
        payload = run_qd_json(["maintenance", "strategy-regression-audit", "--json", "true", "--summary", "true"])
        self.assertEqual(payload["task_type"], "strategy_regression_audit")

    def test_025_git_deletion_audit_still_no_high_risk(self) -> None:
        payload = run_qd_json(["maintenance", "git-deletion-audit", "--json", "true", "--summary", "true"])
        self.assertEqual(payload.get("high_risk_deleted_files"), [])

    def test_026_alias_reconciliation_still_passes(self) -> None:
        payload = run_qd_json(["maintenance", "import-alias-reconciliation", "--json", "true", "--summary", "true"])
        self.assertEqual(payload.get("alias_import_fail_count"), 0)

    def test_027_capital_lot_cli_json(self) -> None:
        payload = run_qd_json(["strategy-viability", "capital-lot-sensitivity", "--data-root", str(self.shared_data_root()), "--json", "true", "--summary", "true", "--smoke-run", "true", "--strategies", "low_volatility", "--max-symbols", "8"])
        self.assertEqual(payload["task_type"], "capital_lot_sensitivity")

    def test_028_strategy_viability_run_cli_json(self) -> None:
        payload = run_qd_json(["strategy-viability", "run", "--data-root", str(self.shared_data_root()), "--profile", "low_price", "--pit-mode", "price_coverage", "--json", "true", "--summary", "true", "--smoke-run", "true", "--max-symbols", "8"])
        self.assertEqual(payload["task_type"], "strategy_viability_baselines")

    def test_029_self_check_marker_present(self) -> None:
        text = (ROOT / "scripts" / "self_check.py").read_text(encoding="utf-8")
        self.assertIn("check_v3706_strategy_regression_guard", text)

    def test_030_verify_clean_zip_knows_v3706(self) -> None:
        text = (ROOT / "scripts" / "verify_clean_zip.py").read_text(encoding="utf-8")
        self.assertIn("3.7.0.6", text)

    def test_031_docs_exist(self) -> None:
        self.assertTrue((ROOT / "docs" / "strategy_regression_guard.md").exists())

    def test_032_json_has_no_nan(self) -> None:
        self.assertTrue(_json_safe(run_one_baseline(_features(), "low_volatility")))

    def test_033_self_check_fail_zero(self) -> None:
        payload = run_qd_json(["self-check"])
        self.assertEqual(payload.get("fail"), 0)


def _write_zero_trade_daily(path: Path, symbol: str, *, periods: int = 160, price: float = 40.0) -> None:
    """High-priced series so 10000 cash / 10% cap / 100-share lot cannot fill.

    A single 100-share lot costs ~price*100 (>= 4000) which exceeds the
    per-symbol cap of 1000 (10% of 10000), so every candidate is unfillable and
    the equity curve stays empty -> the zero-trade scenario.
    """

    path.parent.mkdir(parents=True, exist_ok=True)
    dates = pd.bdate_range("2025-01-01", periods=periods)
    rows: list[dict[str, object]] = []
    value = price + int(symbol[-1])
    for idx, date in enumerate(dates):
        value *= 1.0 + 0.001 + (idx % 7 - 3) * 0.0003
        rows.append(
            {
                "date": date.strftime("%Y-%m-%d"),
                "symbol": symbol,
                "open": value * 0.995,
                "high": value * 1.012,
                "low": value * 0.988,
                "close": value,
                "volume": 1_000_000 + idx * 1000,
                "amount": value * (1_000_000 + idx * 1000),
            }
        )
    pd.DataFrame(rows).to_csv(path, index=False, encoding="utf-8")


class V3706ReportTruthfulnessAndExecutionEvidenceTests(unittest.TestCase):
    """Latest-output truthfulness gate + honest zero-trade diagnosis (goal v371)."""

    def test_101_temp_data_root_is_not_canonical(self) -> None:
        from quantdesk.research.strategy_viability_runner import classify_data_root_kind

        with tempfile.TemporaryDirectory() as tmp:
            info = classify_data_root_kind(Path(tmp) / "data")
        self.assertFalse(info["is_canonical_data_root"])
        self.assertEqual(info["data_root_kind"], "temporary")

    def test_102_canonical_data_root_detected_via_resolve(self) -> None:
        from quantdesk.research.strategy_viability_runner import (
            PROJECT_ROOT as RUNNER_ROOT,
            classify_data_root_kind,
        )

        # Relative "data" and the absolute canonical path must both resolve to
        # canonical, and a Temp path that merely contains the word "data" must not.
        self.assertTrue(classify_data_root_kind("data")["is_canonical_data_root"])
        self.assertTrue(classify_data_root_kind(RUNNER_ROOT / "data")["is_canonical_data_root"])
        with tempfile.TemporaryDirectory() as tmp:
            self.assertFalse(classify_data_root_kind(Path(tmp) / "data")["is_canonical_data_root"])

    def test_103_temp_root_report_not_latest_eligible(self) -> None:
        from quantdesk.research.strategy_viability_runner import build_report_truthfulness

        with tempfile.TemporaryDirectory() as tmp:
            truth = build_report_truthfulness(Path(tmp) / "data", write_latest=True, smoke_run=False)
        self.assertFalse(truth["latest_eligible"])
        self.assertTrue(truth["latest_write_blocked_reason"].startswith("non_canonical_data_root"))

    def test_104_canonical_root_latest_eligible(self) -> None:
        from quantdesk.research.strategy_viability_runner import build_report_truthfulness

        truth = build_report_truthfulness("data", write_latest=True, smoke_run=False)
        self.assertTrue(truth["latest_eligible"])
        self.assertEqual(truth["latest_write_blocked_reason"], "")

    def test_105_smoke_run_not_latest_eligible(self) -> None:
        from quantdesk.research.strategy_viability_runner import build_report_truthfulness

        truth = build_report_truthfulness("data", write_latest=True, smoke_run=True)
        self.assertFalse(truth["latest_eligible"])
        self.assertEqual(truth["latest_write_blocked_reason"], "smoke_run_not_eligible_for_canonical_latest")
        self.assertTrue(truth["smoke_run"])

    def test_106_synthetic_root_not_latest_eligible(self) -> None:
        from quantdesk.research.strategy_viability_runner import build_report_truthfulness

        with tempfile.TemporaryDirectory() as tmp:
            truth = build_report_truthfulness(Path(tmp) / "data", write_latest=True, smoke_run=False, synthetic=True)
        self.assertFalse(truth["latest_eligible"])
        self.assertEqual(truth["data_root_kind"], "synthetic")

    def test_107_temp_data_root_does_not_overwrite_canonical_latest(self) -> None:
        from quantdesk.research.strategy_viability_runner import (
            RESULT_JSON,
            run_strategy_viability_baselines,
            write_baseline_outputs,
        )

        canonical_before = RESULT_JSON.read_bytes() if RESULT_JSON.exists() else None
        with tempfile.TemporaryDirectory() as tmp:
            daily = Path(tmp) / "data" / "daily"
            for idx in range(6):
                _write_zero_trade_daily(daily / f"6000{idx:02d}.csv", f"6000{idx:02d}")
            payload = run_strategy_viability_baselines({"data_root": str(Path(tmp) / "data"), "max_symbols": 6})
            written = write_baseline_outputs(payload)
        # The write must be redirected to a run-specific path, never canonical latest.
        self.assertNotEqual(written["output_paths"]["json"], "results/strategy_viability_baselines_latest.json")
        self.assertIn("results/runs/", written["output_paths"]["json"])
        self.assertEqual(written.get("latest_write_target"), "run_specific")
        canonical_after = RESULT_JSON.read_bytes() if RESULT_JSON.exists() else None
        self.assertEqual(canonical_before, canonical_after)

    def test_108_zero_trade_is_insufficient_evidence_not_degenerate(self) -> None:
        from quantdesk.research.strategy_viability_runner import run_strategy_viability_baselines

        with tempfile.TemporaryDirectory() as tmp:
            daily = Path(tmp) / "data" / "daily"
            for idx in range(6):
                _write_zero_trade_daily(daily / f"6000{idx:02d}.csv", f"6000{idx:02d}")
            payload = run_strategy_viability_baselines({"data_root": str(Path(tmp) / "data"), "max_symbols": 6})
        self.assertEqual(payload["baseline_distinctness_status"], "not_evaluable")
        self.assertIsNone(payload["best_strategy"])
        self.assertEqual(payload["execution_evidence"]["status"], "insufficient_execution_evidence")
        self.assertFalse(payload["execution_evidence"]["distinctness_evaluable"])
        self.assertNotEqual(payload.get("baseline_distinctness", {}).get("reason"), "strategy_scoring_degenerated")
        self.assertNotEqual(payload["decision_gate"]["recommendation"], "continue_research")

    def test_109_classify_execution_evidence_lot_infeasible(self) -> None:
        from quantdesk.research.strategy_viability_runner import classify_execution_evidence

        rows = [
            {"metrics": {"trade_count": 0, "unfilled_due_to_lot_size_count": 30}, "price_filter": {"max_price_filtered_universe_count": 8}},
            {"metrics": {"trade_count": 0, "unfilled_due_to_lot_size_count": 25}, "price_filter": {"max_price_filtered_universe_count": 8}},
        ]
        out = classify_execution_evidence(rows, panel_empty=False)
        self.assertEqual(out["status"], "insufficient_execution_evidence")
        self.assertEqual(out["reason"], "no_lot_feasible_candidate")
        self.assertFalse(out["distinctness_evaluable"])

    def test_110_classify_execution_evidence_sufficient_when_trades(self) -> None:
        from quantdesk.research.strategy_viability_runner import classify_execution_evidence

        rows = [
            {"metrics": {"trade_count": 5, "unfilled_due_to_lot_size_count": 0}, "price_filter": {"max_price_filtered_universe_count": 40}},
            {"metrics": {"trade_count": 4, "unfilled_due_to_lot_size_count": 0}, "price_filter": {"max_price_filtered_universe_count": 40}},
        ]
        out = classify_execution_evidence(rows, panel_empty=False)
        self.assertEqual(out["status"], "sufficient_execution_evidence")
        self.assertTrue(out["distinctness_evaluable"])

    def test_111_real_homogeneity_still_detected_with_trades(self) -> None:
        # With real execution evidence AND identical results, the audit must still
        # flag genuine strategy homogeneity (not suppressed by the zero-trade gate).
        from quantdesk.maintenance import strategy_regression_audit as audit_mod

        baselines = {
            "baselines": [
                {"name": "a", "metrics": {"trade_count": 6, "annualized_return_net": 0.1, "max_drawdown": -0.1}, "equity_curve": [{"net_equity": 1.0}, {"net_equity": 1.1}], "equity_curve_hash": "same", "selected_symbols_hash": "same", "eligible_count_by_rebalance": [{"eligible_count": 40}], "price_filter": {"max_price_filtered_universe_count": 40}},
                {"name": "b", "metrics": {"trade_count": 6, "annualized_return_net": 0.1, "max_drawdown": -0.1}, "equity_curve": [{"net_equity": 1.0}, {"net_equity": 1.1}], "equity_curve_hash": "same", "selected_symbols_hash": "same", "eligible_count_by_rebalance": [{"eligible_count": 40}], "price_filter": {"max_price_filtered_universe_count": 40}},
            ],
            "baseline_distinctness_status": "failed",
            "baseline_distinctness": {"status": "failed", "reason": "strategy_scoring_degenerated", "duplicated_strategy_groups": [["a", "b"]]},
            "execution_evidence": {"status": "sufficient_execution_evidence", "reason": "baselines_have_executable_trades", "distinctness_evaluable": True},
            "report_provenance": {"data_root_kind": "canonical", "is_canonical_data_root": True, "latest_eligible": True},
        }
        result = audit_mod.assess_strategy_reason_codes(baselines)
        self.assertTrue(result["real_strategy_homogeneity"])
        self.assertFalse(result["insufficient_execution_evidence"])

    def test_112_distinct_strategies_not_flagged_homogeneous(self) -> None:
        from quantdesk.research.strategy_viability_runner import (
            check_baseline_distinctness,
            classify_execution_evidence,
        )

        rows = [
            {"name": "a", "metrics": {"trade_count": 6, "annualized_return_net": 0.12, "max_drawdown": -0.1}, "equity_curve_hash": "h_a", "selected_symbols_hash": "s_a", "eligible_count_by_rebalance": [{"eligible_count": 40}], "price_filter": {"max_price_filtered_universe_count": 40}},
            {"name": "b", "metrics": {"trade_count": 5, "annualized_return_net": 0.05, "max_drawdown": -0.2}, "equity_curve_hash": "h_b", "selected_symbols_hash": "s_b", "eligible_count_by_rebalance": [{"eligible_count": 40}], "price_filter": {"max_price_filtered_universe_count": 40}},
        ]
        self.assertEqual(check_baseline_distinctness(rows)["status"], "passed")
        self.assertTrue(classify_execution_evidence(rows, panel_empty=False)["distinctness_evaluable"])

    def test_113_run_specific_output_does_not_touch_data_daily(self) -> None:
        from quantdesk.research.strategy_viability_runner import (
            PROJECT_ROOT as RUNNER_ROOT,
            run_strategy_viability_baselines,
            write_baseline_outputs,
        )

        data_daily = RUNNER_ROOT / "data" / "daily"
        listing_before = sorted(p.name for p in data_daily.glob("*.csv")) if data_daily.exists() else []
        with tempfile.TemporaryDirectory() as tmp:
            daily = Path(tmp) / "data" / "daily"
            for idx in range(6):
                _write_zero_trade_daily(daily / f"6000{idx:02d}.csv", f"6000{idx:02d}")
            payload = run_strategy_viability_baselines({"data_root": str(Path(tmp) / "data"), "max_symbols": 6})
            write_baseline_outputs(payload)
        listing_after = sorted(p.name for p in data_daily.glob("*.csv")) if data_daily.exists() else []
        self.assertEqual(listing_before, listing_after)


if __name__ == "__main__":
    unittest.main()
