from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from quantdesk.command_center_status import build_command_center_status
from quantdesk.daily_data_retention import build_retention_plan, collect_daily_retention_snapshot
from quantdesk.operation_log import append_operation_log, build_operation_record
from quantdesk.research.parameter_sensitivity_audit import (
    build_parameter_sensitivity_report,
    build_parameter_grid,
    compute_robustness_score,
    detect_overfit_parameter_hotspots,
    run_parameter_sensitivity_audit,
)
from quantdesk.result_archive import build_archive_plan
from quantdesk.strategy_observation_contract import build_strategy_observation_context

PROJECT_ROOT = Path(__file__).resolve().parents[1]


class ParameterSensitivityAuditTests(unittest.TestCase):
    def test_build_parameter_grid_generates_finite_combinations(self) -> None:
        grid = build_parameter_grid({"ma_short": [5, 7], "score_threshold": [60, 65]}, max_combinations=50, random_seed=42)
        self.assertEqual(grid["parameter_count"], 2)
        self.assertEqual(grid["combination_count"], 4)
        self.assertEqual(len(grid["combinations"]), 4)

    def test_max_combinations_limits_grid(self) -> None:
        grid = build_parameter_grid(
            {"a": [1, 2, 3], "b": [1, 2, 3], "c": [1, 2, 3]},
            max_combinations=5,
            random_seed=42,
        )
        self.assertEqual(grid["original_combination_count"], 27)
        self.assertEqual(grid["combination_count"], 5)
        self.assertTrue(grid["warnings"])

    def test_random_seed_reproducible(self) -> None:
        params = {"a": [1, 2, 3, 4], "b": [10, 20, 30]}
        left = build_parameter_grid(params, max_combinations=6, random_seed=7)
        right = build_parameter_grid(params, max_combinations=6, random_seed=7)
        self.assertEqual(left["combinations"], right["combinations"])

    def test_detect_overfit_hotspots_identifies_narrow_peak(self) -> None:
        rows = [
            {
                "parameters": {"p": index},
                "annual_return": 1.0 if index == 0 else 0.0,
                "max_drawdown": -0.1,
                "sharpe": 0.5,
                "trade_count": 20,
                "window_count": 20,
            }
            for index in range(20)
        ]
        warnings = detect_overfit_parameter_hotspots(rows)
        self.assertIn("narrow_peak", {item["type"] for item in warnings})

    def test_compute_robustness_score_bounds(self) -> None:
        score = compute_robustness_score(
            {
                "annual_return": {"mean": 0.08, "std": 0.02},
                "max_drawdown": {"mean": -0.1, "std": 0.01},
                "sharpe": {"mean": 0.6, "std": 0.1},
            },
            [{"type": "narrow_peak"}],
        )
        self.assertGreaterEqual(score, 0.0)
        self.assertLessEqual(score, 1.0)

    def test_insufficient_sample_returns_warning(self) -> None:
        payload = run_parameter_sensitivity_audit(
            symbol="600744",
            target_date="2026-05-19",
            network=False,
            config_payload={"parameters": {"ma_short": [5]}},
            backtest_result={},
            write=False,
        )
        self.assertEqual(payload["status"], "warning")
        self.assertTrue(any(item["type"].startswith("insufficient") for item in payload["overfit_warnings"]))

    def test_parameter_sensitivity_report_humanizes_visible_metrics(self) -> None:
        payload = run_parameter_sensitivity_audit(
            symbol="600744",
            target_date="2026-05-19",
            network=False,
            config_payload={"parameters": {"ma_short": [5, 7], "score_threshold": [60, 65]}},
            parameter_results=[
                {
                    "parameters": {"ma_short": 5, "score_threshold": 60},
                    "metrics": {
                        "annual_return": 0.08,
                        "max_drawdown": -0.04,
                        "sharpe": 0.6,
                        "win_rate": 0.55,
                        "trade_count": 12,
                        "window_count": 12,
                    },
                },
                {
                    "parameters": {"ma_short": 7, "score_threshold": 65},
                    "metrics": {
                        "annual_return": 0.05,
                        "max_drawdown": -0.06,
                        "sharpe": 0.4,
                        "win_rate": 0.5,
                        "trade_count": 12,
                        "window_count": 12,
                    },
                },
            ],
            write=False,
        )

        report = build_parameter_sensitivity_report(payload)

        self.assertIn("参数敏感性审计", report)
        self.assertIn("参数稳定性指标", report)
        self.assertIn("状态：", report)
        self.assertIn("均值：", report)
        self.assertIn("波动：", report)
        for raw in (
            "- status:",
            "- symbol:",
            "- target_date:",
            "- parameter_count:",
            "- combination_count:",
            "- robustness_score:",
            "- source_mode:",
            "- warning_count:",
            "mean=`",
            "std=`",
            "min=`",
            "max=`",
            "status=`",
        ):
            self.assertNotIn(raw, report)

    def test_param_sensitivity_cli_exists_and_network_false(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = Path(tmp) / "results"
            report_dir = Path(tmp) / "reports"
            completed = subprocess.run(
                [
                    sys.executable,
                    str(PROJECT_ROOT / "scripts" / "run_parameter_sensitivity_audit.py"),
                    "--symbol",
                    "600744",
                    "--target-date",
                    "2026-05-19",
                    "--network",
                    "false",
                    "--max-combinations",
                    "10",
                    "--seed",
                    "42",
                    "--output-dir",
                    str(output_dir),
                    "--report-dir",
                    str(report_dir),
                ],
                cwd=PROJECT_ROOT,
                text=True,
                encoding="utf-8",
                errors="replace",
                capture_output=True,
                check=False,
            )
            payload = json.loads((output_dir / "parameter_sensitivity_latest.json").read_text(encoding="utf-8"))
            report_text = (report_dir / "parameter_sensitivity_report_latest.md").read_text(encoding="utf-8")
        self.assertEqual(completed.returncode, 0, completed.stderr + completed.stdout)
        self.assertIn("本命令不主动抓行情", completed.stdout)
        self.assertFalse(payload["network"])
        self.assertIn("robustness_score", payload)
        self.assertIn("不构成投资建议", report_text)

    def test_qd_help_contains_param_sensitivity(self) -> None:
        completed = subprocess.run(
            [sys.executable, str(PROJECT_ROOT / "scripts" / "qd.py"), "help"],
            cwd=PROJECT_ROOT,
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            check=False,
        )
        self.assertEqual(completed.returncode, 0)
        self.assertIn("param-sensitivity", completed.stdout)

    def test_cli_does_not_modify_default_strategy_config(self) -> None:
        config_path = PROJECT_ROOT / "config" / "default.yaml"
        before = config_path.read_text(encoding="utf-8")
        with tempfile.TemporaryDirectory() as tmp:
            completed = subprocess.run(
                [
                    sys.executable,
                    str(PROJECT_ROOT / "scripts" / "run_parameter_sensitivity_audit.py"),
                    "--symbol",
                    "600744",
                    "--target-date",
                    "2026-05-19",
                    "--network",
                    "false",
                    "--output-dir",
                    str(Path(tmp) / "results"),
                    "--report-dir",
                    str(Path(tmp) / "reports"),
                ],
                cwd=PROJECT_ROOT,
                text=True,
                encoding="utf-8",
                errors="replace",
                capture_output=True,
                check=False,
            )
        self.assertEqual(completed.returncode, 0, completed.stderr + completed.stdout)
        self.assertEqual(config_path.read_text(encoding="utf-8"), before)

    def test_observation_context_contains_parameter_sensitivity_summary(self) -> None:
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
            portfolio_payload={"summary": {}, "positions": []},
            parameter_sensitivity_payload={
                "status": "warning",
                "robustness_score": 0.4,
                "combination_count": 10,
                "warnings": [],
                "overfit_warnings": [{"type": "narrow_peak", "message": "仅少数组合表现较好，可能存在参数挑选偏差。"}],
            },
        )
        self.assertIn("parameter_sensitivity_summary", context)
        self.assertIn("当前策略结果对参数较敏感", "\n".join(context["warnings"]))

    def test_daily_retention_manifest_records_parameter_summary(self) -> None:
        with mock.patch("quantdesk.daily_data_retention.read_json") as mocked_read:
            def fake_read(path: object, default: object = None) -> object:
                if "parameter_sensitivity_latest.json" in str(path):
                    return {
                        "status": "warning",
                        "robustness_score": 0.62,
                        "combination_count": 12,
                        "warnings": ["bounded audit"],
                        "overfit_warnings": [{"type": "parameter_cliff", "message": "相邻参数组合结果跳变较大。"}],
                    }
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
        self.assertEqual(snapshot["parameter_sensitivity_summary"]["status"], "warning")
        self.assertEqual(snapshot["manifest"]["parameter_sensitivity_status"], "warning")
        self.assertEqual(snapshot["manifest"]["robustness_score"], 0.62)

    def test_archive_plan_contains_parameter_outputs(self) -> None:
        plan = build_archive_plan(target_date="2026-05-19", dry_run=True)
        sources = "\n".join(item["source"] for item in plan["entries"])
        self.assertIn("results/parameter_sensitivity_latest.json", sources)
        self.assertIn("reports/parameter_sensitivity_report_latest.md", sources)

    def test_command_center_can_show_param_sensitivity_status(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            log_path = Path(tmp) / "operation_timeline.jsonl"
            append_operation_log(build_operation_record(command="param-sensitivity", status="warning"), log_path)
            payload = build_command_center_status(log_path=log_path)
        row = next(item for item in payload["commands"] if item["command"] == "param-sensitivity")
        self.assertEqual(row["last_status"], "warning")

    def test_clean_zip_rules_exclude_parameter_outputs_and_real_config(self) -> None:
        export_text = (PROJECT_ROOT / "export_project.ps1").read_text(encoding="utf-8")
        self.assertIn('"results"', export_text)
        self.assertIn('"reports"', export_text)
        self.assertIn("parameter_sensitivity.json", export_text)
        self.assertTrue((PROJECT_ROOT / "config" / "parameter_sensitivity.example.json").exists())
        self.assertTrue((PROJECT_ROOT / "docs" / "parameter_sensitivity_audit.md").exists())


if __name__ == "__main__":
    unittest.main()


