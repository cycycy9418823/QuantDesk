from __future__ import annotations

import copy
import json
import math
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from quantdesk.research import backtest_statistical_validation as stats
from quantdesk.research import feature_quality_audit as features

PROJECT_ROOT = Path(__file__).resolve().parents[1]


class BacktestStatisticalValidationAndFeatureQualityAuditTests(unittest.TestCase):
    def test_feature_quality_audit_computes_missing_rate(self) -> None:
        rows = [
            {"symbol": "600744", "date": "2026-05-18", "close": 10, "ma20": 9},
            {"symbol": "600744", "date": "2026-05-19", "close": "", "ma20": 9.5},
        ]
        result = features.audit_feature_quality(rows, write=False)
        self.assertEqual(result["features"]["close"]["missing_rate"], 0.5)

    def test_feature_quality_audit_detects_nan_and_inf(self) -> None:
        rows = [
            {"symbol": "600744", "date": "2026-05-18", "close": math.nan, "ma20": "inf"},
            {"symbol": "600744", "date": "2026-05-19", "close": 10, "ma20": 9.5},
        ]
        result = features.audit_feature_quality(rows, write=False)
        self.assertGreaterEqual(result["features"]["close"]["nan_count"], 1)
        self.assertEqual(result["features"]["ma20"]["inf_count"], 1)

    def test_high_missing_rate_warns(self) -> None:
        rows = [
            {"symbol": "600744", "date": "2026-05-18", "close": ""},
            {"symbol": "600744", "date": "2026-05-19", "close": ""},
            {"symbol": "600744", "date": "2026-05-20", "close": 10},
        ]
        result = features.audit_feature_quality(rows, write=False, missing_threshold=0.3)
        self.assertEqual(result["features"]["close"]["status"], "warning")
        self.assertTrue(any(item["type"] == "high_missing_rate" for item in result["warning_items"]))

    def test_feature_quality_audit_network_false_does_not_call_network(self) -> None:
        result = features.audit_feature_quality([{"symbol": "600744", "date": "2026-05-19", "close": 10}], network=False, write=False)
        self.assertFalse(result["network"])

    def test_feature_quality_audit_can_bound_quick_local_file_scan(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            daily_dir = Path(tmp) / "daily"
            daily_dir.mkdir()
            for symbol in ("000001", "000002", "000003"):
                (daily_dir / f"{symbol}.csv").write_text(
                    f"symbol,date,open,high,low,close,volume\n{symbol},2026-07-10,10,11,9,10.5,100\n",
                    encoding="utf-8",
                )

            result = features.audit_feature_quality(
                input_dir=daily_dir,
                max_files=2,
                network=False,
                write=False,
            )

        self.assertEqual(result["review_scope"], "sampled")
        self.assertEqual(result["checked_file_count"], 2)
        self.assertEqual(result["unchecked_file_count"], 1)
        self.assertTrue(result["full_scan_required_for_formal_conclusion"])
        self.assertEqual(result["symbol_count"], 2)

    def test_raw_daily_ohlcv_not_flagged_for_missing_derived_features(self) -> None:
        # Raw data/daily rows carry only OHLCV/amount/volume/pct_chg. They must
        # be audited as raw and NOT reported as missing ma20/volatility etc.
        rows = [
            {"symbol": "600000", "date": "2026-06-25", "open": 8.5, "high": 8.7,
             "low": 8.4, "close": 8.6, "volume": 1000, "amount": 8600, "pct_chg": 0.5},
            {"symbol": "600000", "date": "2026-06-26", "open": 8.6, "high": 8.8,
             "low": 8.5, "close": 8.7, "volume": 1100, "amount": 9500, "pct_chg": 1.1},
        ]
        result = features.audit_feature_quality(rows, write=False, missing_threshold=0.3)
        self.assertEqual(result["table_kind"], "raw_daily_ohlcv")
        # Derived fields are not part of a raw-daily audit at all.
        self.assertNotIn("ma20", result["features"])
        self.assertNotIn("volatility_20_pct", result["features"])
        # No high-missing warning should be raised for derived families.
        flagged = {item.get("feature") for item in result["warning_items"]}
        self.assertFalse(flagged & {"ma5", "ma10", "ma20", "ma60", "volatility_20_pct", "amount_ma20"})
        # Complete OHLCV rows should not warn at all.
        self.assertEqual(result["status"], "passed")

    def test_derived_feature_table_flags_missing_ma20(self) -> None:
        # Derived feature-table rows (ma5 marker present) with empty ma20 SHOULD warn.
        rows = [
            {"symbol": "600000", "date": "2026-06-25", "close": 8.6, "ma5": 8.5, "ma20": ""},
            {"symbol": "600000", "date": "2026-06-26", "close": 8.7, "ma5": 8.6, "ma20": ""},
        ]
        result = features.audit_feature_quality(rows, write=False, missing_threshold=0.3)
        self.assertEqual(result["table_kind"], "derived_feature_table")
        self.assertEqual(result["features"]["ma20"]["status"], "warning")
        self.assertTrue(
            any(item["type"] == "high_missing_rate" and item.get("feature") == "ma20"
                for item in result["warning_items"])
        )

    def test_permutation_test_fixed_seed_reproducible(self) -> None:
        returns = [0.01, -0.02, 0.03, 0.01, -0.01, 0.02, 0.04, -0.01, 0.02, 0.01]
        left = stats.permutation_test_signals(returns, iterations=50, seed=42)
        right = stats.permutation_test_signals(returns, iterations=50, seed=42)
        self.assertEqual(left["p_value"], right["p_value"])
        self.assertEqual(left["random_p95"], right["random_p95"])

    def test_permutation_baseline_matches_actual_metric_scale(self) -> None:
        # 回归：置换基线与 actual 必须同口径。此前 actual 用复利累计收益、
        # 基线用单期收益均值，量纲不一致导致 p 值恒 ≈ 0.002 的虚假显著性。
        # 全正收益时 sign-shuffle 不改变序列，基线复利收益必须等于 actual 复利收益。
        returns = [0.02] * 12
        compounded = 1.0
        for value in returns:
            compounded *= 1.0 + value
        compounded -= 1.0
        result = stats.permutation_test_signals(
            returns, iterations=100, seed=42, actual_metric=compounded
        )
        # 修复后：基线也做复利聚合 → random_mean == compounded（≈0.268），而非旧的单期均值 0.02。
        self.assertAlmostEqual(result["random_mean"], compounded, places=6)
        self.assertAlmostEqual(result["actual_metric"], compounded, places=6)
        # 全正序列下 actual 不显著高于基线，p 值不应是退化的 ~1/(iters+1)。
        self.assertGreater(result["p_value"], 0.5)

    def test_insufficient_sample_returns_warning(self) -> None:
        result = stats.run_backtest_statistical_validation({"returns": [0.01, -0.01]}, iterations=20, seed=42, write=False)
        self.assertEqual(result["status"], "warning")
        self.assertIn("insufficient_sample", result["warnings"])

    def test_extract_returns_from_equity_curve(self) -> None:
        payload = {
            "equity_curve": [
                {"date": "2026-01-01", "equity": 100000},
                {"date": "2026-01-02", "equity": 101000},
                {"date": "2026-01-03", "equity": 99990},
            ]
        }
        returns = stats._extract_returns(payload)
        self.assertEqual(len(returns), 2)
        self.assertAlmostEqual(returns[0], 0.01)
        self.assertAlmostEqual(returns[1], -0.01)

    def test_default_loader_falls_back_to_extractable_backtest_latest(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            backtest_dir = root / "results" / "backtest"
            backtest_dir.mkdir(parents=True)
            (backtest_dir / "backtest_latest.json").write_text(
                json.dumps({"metrics": {"total_return": 0.02}, "trades": []}),
                encoding="utf-8",
            )
            equity_curve = [{"date": f"2026-01-{day:02d}", "equity": 100000 + day * 100} for day in range(1, 11)]
            (backtest_dir / "latest.json").write_text(json.dumps({"equity_curve": equity_curve}), encoding="utf-8")
            with mock.patch.object(stats, "PROJECT_ROOT", root):
                result = stats.run_backtest_statistical_validation(iterations=20, seed=42, write=False)
        self.assertEqual(result["sample_size"], 9)
        self.assertNotIn("backtest_result_missing", result["warnings"])

    def test_bootstrap_confidence_intervals_return_ranges(self) -> None:
        returns = [0.01, -0.02, 0.03, 0.01, -0.01, 0.02, 0.04, -0.01, 0.02, 0.01]
        result = stats.bootstrap_metric_confidence_intervals(returns, iterations=30, seed=42)
        self.assertEqual(len(result["annual_return_ci"]), 2)
        self.assertEqual(len(result["sharpe_ci"]), 2)

    def test_stats_validation_does_not_modify_backtest_result(self) -> None:
        payload = {"returns": [0.01, -0.02, 0.03, 0.01, -0.01, 0.02, 0.04, -0.01]}
        before = copy.deepcopy(payload)
        stats.run_backtest_statistical_validation(payload, iterations=20, seed=42, write=False)
        self.assertEqual(payload, before)

    def test_qd_help_contains_new_commands(self) -> None:
        qd_text = (PROJECT_ROOT / "scripts" / "qd.py").read_text(encoding="utf-8")
        self.assertIn("feature-audit", qd_text)
        self.assertIn("stats-validate", qd_text)

    def test_feature_audit_cli_network_false(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            daily_dir = Path(tmp) / "daily"
            daily_dir.mkdir()
            (daily_dir / "600744.csv").write_text("symbol,date,close,ma20\n600744,2026-05-19,10,9\n", encoding="utf-8")
            completed = subprocess.run(
                [
                    sys.executable,
                    str(PROJECT_ROOT / "scripts" / "run_feature_quality_audit.py"),
                    "--symbols",
                    "600744",
                    "--target-date",
                    "2026-05-19",
                    "--input-dir",
                    str(daily_dir),
                    "--output-dir",
                    str(Path(tmp)),
                    "--network",
                    "false",
                ],
                cwd=PROJECT_ROOT,
                text=True,
                encoding="utf-8",
                errors="replace",
                capture_output=True,
                check=False,
            )
        self.assertEqual(completed.returncode, 0, completed.stderr + completed.stdout)
        self.assertIn("不主动联网", completed.stdout)

    def test_stats_validate_cli_network_false(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            backtest_path = Path(tmp) / "backtest.json"
            backtest_path.write_text(json.dumps({"returns": [0.01, -0.01, 0.02, -0.02, 0.03, 0.01, -0.01, 0.02]}), encoding="utf-8")
            completed = subprocess.run(
                [
                    sys.executable,
                    str(PROJECT_ROOT / "scripts" / "run_backtest_statistical_validation.py"),
                    "--symbol",
                    "600744",
                    "--target-date",
                    "2026-05-19",
                    "--backtest-result",
                    str(backtest_path),
                    "--network",
                    "false",
                    "--iterations",
                    "20",
                    "--seed",
                    "42",
                ],
                cwd=PROJECT_ROOT,
                text=True,
                encoding="utf-8",
                errors="replace",
                capture_output=True,
                check=False,
            )
        self.assertEqual(completed.returncode, 0, completed.stderr + completed.stdout)
        self.assertIn("不主动联网", completed.stdout)

    def test_feature_audit_empty_data_returns_warning_exit_zero(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            missing_daily = Path(tmp) / "missing_daily"
            completed = subprocess.run(
                [
                    sys.executable,
                    str(PROJECT_ROOT / "scripts" / "run_feature_quality_audit.py"),
                    "--symbols",
                    "600744,002115",
                    "--target-date",
                    "2026-05-19",
                    "--input-dir",
                    str(missing_daily),
                    "--output-dir",
                    str(Path(tmp)),
                    "--network",
                    "false",
                ],
                cwd=PROJECT_ROOT,
                text=True,
                encoding="utf-8",
                errors="replace",
                capture_output=True,
                check=False,
            )
            payload = json.loads((Path(tmp) / "feature_quality_audit_latest.json").read_text(encoding="utf-8"))
        self.assertEqual(completed.returncode, 0, completed.stderr + completed.stdout)
        self.assertEqual(payload["status"], "warning")
        self.assertEqual(payload["row_count"], 0)
        self.assertTrue(payload["insufficient_data"])
        self.assertIn("suggested_next_action", payload)
        self.assertEqual(payload["sources"], [])
        self.assertFalse(payload["network"])

    def test_observation_context_contains_feature_and_stats_summary(self) -> None:
        from quantdesk.strategy_observation_contract import build_strategy_observation_context

        context = build_strategy_observation_context(
            symbol="600744",
            target_date="2026-05-19",
            data_health_payload={"status": "ok"},
            provider_payload={},
            strategy_payload={},
            workflow_payload={},
            conditional_plan_payload={},
            execution_payload={},
            intraday_payload={},
            portfolio_payload={},
            validation_payload={},
            consistency_payload={},
            feature_quality_payload={"status": "warning", "row_count": 2, "feature_count": 3, "features": {"ma20": {"missing_rate": 0.5}}},
            statistical_validation_payload={"status": "warning", "sample_size": 2, "permutation_test": {"status": "warning", "p_value": None}, "bootstrap_ci": {"status": "warning"}, "warnings": ["insufficient_sample"]},
        )
        self.assertIn("feature_quality_summary", context)
        self.assertIn("statistical_validation_summary", context)
        self.assertIn("当前统计样本不足，不能据此判断策略稳定性。", context["warnings"])

    def test_daily_retain_manifest_can_record_feature_and_stats_summary(self) -> None:
        from quantdesk.daily_data_retention import build_retention_plan, collect_daily_retention_snapshot

        with mock.patch("quantdesk.daily_data_retention.read_json") as mocked_read:
            def fake_read(path: object, default: object = None) -> object:
                text = str(path)
                if "feature_quality_audit_latest.json" in text:
                    return {"status": "warning", "row_count": 2, "feature_count": 3, "features": {"ma20": {"missing_rate": 0.5}}}
                if "backtest_statistical_validation_latest.json" in text:
                    return {"status": "warning", "sample_size": 2, "permutation_test": {"status": "warning", "p_value": None}, "bootstrap_ci": {"status": "warning"}, "warnings": ["insufficient_sample"]}
                return {}

            mocked_read.side_effect = fake_read
            plan = build_retention_plan(
                symbols="600744",
                target_date="2026-05-19",
                include_provider_smoke=False,
                include_strategy_validation=False,
                include_observation_report=False,
            )
            snapshot = collect_daily_retention_snapshot(plan)
        manifest = snapshot["manifest"]
        self.assertEqual(manifest["feature_quality_status"], "warning")
        self.assertEqual(manifest["statistical_validation_status"], "warning")

    def test_clean_zip_excludes_new_outputs(self) -> None:
        export_text = (PROJECT_ROOT / "export_project.ps1").read_text(encoding="utf-8")
        self.assertIn('"results"', export_text)
        self.assertIn('"reports"', export_text)


if __name__ == "__main__":
    unittest.main()
