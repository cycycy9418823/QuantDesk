from __future__ import annotations

import json
import math
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from quantdesk.research.factor_research_dashboard import (
    build_factor_research_dashboard,
    build_research_dashboard_summary,
    build_synthetic_research_payloads,
    load_latest_research_payloads,
    summarize_backtest,
    summarize_combined_score,
    summarize_factor_health,
    summarize_module_health,
    summarize_sector_rotation,
    write_research_dashboard_outputs,
)

try:
    from tests._operation_log_isolation import isolate_operation_log_dir
except ModuleNotFoundError:  # pragma: no cover - unittest discovery adds tests/ directly
    from _operation_log_isolation import isolate_operation_log_dir

isolate_operation_log_dir()

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _assert_json_safe(value):
    if isinstance(value, dict):
        for item in value.values():
            _assert_json_safe(item)
    elif isinstance(value, list):
        for item in value:
            _assert_json_safe(item)
    elif isinstance(value, float):
        assert math.isfinite(value)


class FactorResearchDashboardTest(unittest.TestCase):
    def test_payload_loading_missing_and_invalid_are_warnings(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            factor = root / "factor.json"
            factor.write_text(json.dumps({"task_type": "factor_evaluation", "status": "passed", "generated_at": "2026-05-24T10:00:00"}), encoding="utf-8")
            bad = root / "bad.json"
            bad.write_text("{not-json", encoding="utf-8")
            loaded = load_latest_research_payloads({
                "inputs": {
                    "factor_health_json": str(factor),
                    "combined_score_json": str(root / "missing_score.json"),
                    "combined_score_backtest_json": str(bad),
                    "sector_rotation_json": str(root / "missing_sector.json"),
                }
            })
            self.assertTrue(loaded["payloads"]["factor_evaluation"]["available"])
            self.assertFalse(loaded["payloads"]["factor_combination"]["available"])
            self.assertFalse(loaded["payloads"]["combined_score_backtest"]["available"])
            self.assertTrue(any("missing_combined_score" in item for item in loaded["warnings"]))
            self.assertTrue(any("invalid_json" in item for item in loaded["warnings"]))

    def test_module_health_missing_stale_and_generated_at_warning(self) -> None:
        payloads = build_synthetic_research_payloads()
        payloads["payloads"]["factor_evaluation"]["payload"]["generated_at"] = "2000-01-01T00:00:00"
        payloads["payloads"]["factor_combination"]["payload"].pop("generated_at", None)
        payloads["payloads"]["sector_rotation"] = {"available": False, "path": "missing.json", "payload": {}, "warnings": ["missing_sector_rotation"]}
        health = summarize_module_health(payloads, stale_hours=1)
        self.assertEqual(health["module_health"]["factor_evaluation"]["status"], "stale")
        self.assertEqual(health["module_health"]["factor_combination"]["status"], "warning")
        self.assertEqual(health["module_health"]["sector_rotation"]["status"], "missing")

    def test_factor_health_summary(self) -> None:
        payload = build_synthetic_research_payloads()["payloads"]["factor_evaluation"]["payload"]
        summary = summarize_factor_health(payload)
        self.assertEqual(summary["factor_count"], 3)
        self.assertEqual(summary["strong_count"], 1)
        self.assertEqual(summary["usable_count"], 1)
        self.assertEqual(summary["weak_count"], 1)
        self.assertTrue(summary["best_factors"])
        self.assertIn("missing_factor_health", summarize_factor_health(None)["main_warnings"])

    def test_combined_score_summary(self) -> None:
        payload = build_synthetic_research_payloads()["payloads"]["factor_combination"]["payload"]
        summary = summarize_combined_score(payload)
        self.assertEqual(summary["factor_count"], 3)
        self.assertEqual(summary["scored_count"], 300)
        self.assertEqual(summary["top_candidates_preview"][0]["symbol"], "000001")
        self.assertIn("score_panel_unavailable", " ".join(summary["main_warnings"]))

    def test_backtest_summary_long_short_warning(self) -> None:
        payload = build_synthetic_research_payloads()["payloads"]["combined_score_backtest"]["payload"]
        payload["config"]["long_short"] = True
        summary = summarize_backtest(payload)
        self.assertAlmostEqual(summary["net_return"], 0.12)
        self.assertAlmostEqual(summary["sharpe_ratio"], 0.74)
        self.assertTrue(any("long_short_research_only" in item for item in summary["main_warnings"]))

    def test_sector_rotation_summary(self) -> None:
        payload = build_synthetic_research_payloads()["payloads"]["sector_rotation"]["payload"]
        summary = summarize_sector_rotation(payload)
        self.assertEqual(summary["sector_count"], 3)
        self.assertEqual(summary["hot_sector_count"], 1)
        self.assertEqual(summary["improving_sector_count"], 2)
        self.assertTrue(summary["top_sectors_preview"])

    def test_dashboard_summary_degrades_on_missing_and_has_pit_risk(self) -> None:
        module_health = {
            "factor_evaluation": {"available": True, "status": "passed"},
            "factor_combination": {"available": False, "status": "missing"},
            "combined_score_backtest": {"available": True, "status": "stale", "stale": True},
            "sector_rotation": {"available": True, "status": "warning"},
        }
        summary = build_research_dashboard_summary({
            "module_health": module_health,
            "factor_health_summary": {"usable_count": 2, "weak_count": 1},
            "combined_score_summary": {"scored_count": 0},
            "backtest_summary": {"rebalance_count": 0, "sharpe_ratio": 0.1, "max_drawdown": -0.1},
            "sector_rotation_summary": {"hot_sector_count": 0, "improving_sector_count": 0, "top_sectors_preview": []},
        })
        self.assertEqual(summary["readiness_level"], "watch")
        self.assertIn("factor_combination", summary["missing_modules"])
        self.assertTrue(any("survivorship" in item.lower() for item in summary["risk_flags"]))
        self.assertTrue(summary["suggested_next_actions"])

    def test_json_schema_and_markdown_report(self) -> None:
        payload = build_factor_research_dashboard(build_synthetic_research_payloads(), synthetic_payloads=True)
        self.assertEqual(payload["task_type"], "factor_research_dashboard")
        self.assertIn("module_health", payload)
        self.assertIn("dashboard_summary", payload)
        self.assertIn("output_paths", payload)
        _assert_json_safe(payload)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            payload = write_research_dashboard_outputs(payload, output_dir=root / "results", report_dir=root / "reports")
            latest = Path(payload["output_paths"]["latest_json"])
            report = Path(payload["output_paths"]["latest_report"])
            self.assertTrue(latest.exists())
            self.assertTrue(report.exists())
            loaded = json.loads(latest.read_text(encoding="utf-8"))
            self.assertIn("module_health", loaded)
            text = report.read_text(encoding="utf-8")
            for token in ("Module Health", "Factor Health", "Combined Score", "Backtest", "Sector Rotation", "Risk Flags", "Disclaimer"):
                self.assertIn(token, text)
            self.assertNotIn("????", text)
            self.assertNotIn("\ufffd", text)

    def test_cli_summary_and_qd_integration(self) -> None:
        command = [
            sys.executable,
            str(PROJECT_ROOT / "scripts" / "run_factor_research_dashboard.py"),
            "--json",
            "true",
            "--summary",
            "true",
            "--synthetic-test-data",
            "true",
        ]
        completed = subprocess.run(command, cwd=PROJECT_ROOT, text=True, capture_output=True, timeout=60)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        summary = json.loads(completed.stdout)
        self.assertEqual(summary["task_type"], "factor_research_dashboard")
        self.assertIn("output_paths", summary)
        self.assertIn("warning_count", summary)
        help_completed = subprocess.run([sys.executable, str(PROJECT_ROOT / "scripts" / "qd.py"), "--help"], cwd=PROJECT_ROOT, text=True, capture_output=True, timeout=60)
        self.assertIn("research-dashboard", help_completed.stdout)
        qd = subprocess.run(
            [sys.executable, str(PROJECT_ROOT / "scripts" / "qd.py"), "research-dashboard", "--json", "true", "--summary", "true", "--synthetic-test-data", "true"],
            cwd=PROJECT_ROOT,
            text=True,
            capture_output=True,
            timeout=60,
        )
        self.assertEqual(qd.returncode, 0, qd.stderr)
        self.assertEqual(json.loads(qd.stdout)["task_type"], "factor_research_dashboard")


if __name__ == "__main__":
    unittest.main()

