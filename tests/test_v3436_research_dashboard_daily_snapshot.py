from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

from quantdesk.daily_research_snapshot import (
    load_research_dashboard_snapshot,
    render_daily_research_snapshot_markdown,
)

try:
    from tests._isolated_project import IsolatedProject
except ModuleNotFoundError:  # pragma: no cover - unittest discovery adds tests/ directly
    from _isolated_project import IsolatedProject

PROJECT_ROOT = Path(__file__).resolve().parents[1]
PYTHON = Path(sys.executable)
TEST_TMP_ROOT = Path(tempfile.gettempdir()) / ".quantdesk_test_tmp"
DAILY_CLI_TIMEOUT_SECONDS = 300


def _dashboard_payload(*, generated_at: str | None = None) -> dict:
    return {
        "ok": True,
        "status": "warning",
        "task_type": "factor_research_dashboard",
        "generated_at": generated_at or datetime.now().isoformat(timespec="seconds"),
        "app_version": "3.4.3.6-research-dashboard-daily-snapshot",
        "dashboard_summary": {
            "readiness_level": "watch",
            "key_findings": ["Factor evaluation has usable factors."],
            "risk_flags": ["PIT universe not enabled."],
            "suggested_next_actions": ["Run factor-eval with updated daily data."],
            "missing_modules": [],
            "stale_modules": [],
        },
        "module_health": {
            "factor_evaluation": {"available": True, "status": "warning"},
            "factor_combination": {"available": True, "status": "passed"},
            "combined_score_backtest": {"available": True, "status": "warning"},
            "sector_rotation": {"available": True, "status": "warning"},
        },
        "factor_health_summary": {"usable_count": 3, "weak_count": 1},
        "backtest_summary": {"sharpe_ratio": 0.74, "max_drawdown": -0.113},
        "sector_rotation_summary": {
            "top_sectors_preview": [
                {"sector_name": "Tech", "heat_score": 1.2, "heat_status": "hot", "current_rank": 1}
            ]
        },
        "pit_status": {"available": True, "pit_universe": True},
        "output_paths": {
            "latest_json": "results/research_dashboard_latest.json",
            "latest_report": "reports/research_dashboard_report_latest.md",
        },
        "warnings": ["synthetic dashboard"],
        "disclaimer": "Research diagnostics only. Not investment advice.",
    }


class ResearchDashboardDailySnapshotTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        TEST_TMP_ROOT.mkdir(parents=True, exist_ok=True)
        cls._isolated_project = IsolatedProject(PROJECT_ROOT, temp_parent=TEST_TMP_ROOT).start()
        assert cls._isolated_project.root is not None
        cls.isolated_project_root = cls._isolated_project.root

    @classmethod
    def tearDownClass(cls) -> None:
        cls._isolated_project.cleanup()

    def test_snapshot_loading_missing_invalid_and_task_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            missing = load_research_dashboard_snapshot(root / "missing.json")
            self.assertEqual(missing["status"], "missing")
            self.assertIn("missing_research_dashboard", " ".join(missing["warnings"]))

            bad = root / "bad.json"
            bad.write_text("{bad", encoding="utf-8")
            invalid = load_research_dashboard_snapshot(bad)
            self.assertEqual(invalid["status"], "invalid")

            mismatch_path = root / "mismatch.json"
            payload = _dashboard_payload()
            payload["task_type"] = "wrong_task"
            mismatch_path.write_text(json.dumps(payload), encoding="utf-8")
            mismatch = load_research_dashboard_snapshot(mismatch_path)
            self.assertIn("task_type_mismatch", " ".join(mismatch["warnings"]))

    def test_stale_detection_and_summary_extraction(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "dashboard.json"
            old = (datetime.now() - timedelta(hours=72)).isoformat(timespec="seconds")
            path.write_text(json.dumps(_dashboard_payload(generated_at=old)), encoding="utf-8")
            snapshot = load_research_dashboard_snapshot(path, stale_hours=24)
        self.assertEqual(snapshot["status"], "stale")
        self.assertTrue(snapshot["stale"])
        self.assertEqual(snapshot["readiness_level"], "watch")
        self.assertEqual(snapshot["available_module_count"], 4)
        self.assertEqual(snapshot["usable_factor_count"], 3)
        self.assertEqual(snapshot["weak_factor_count"], 1)
        self.assertTrue(snapshot["pit_universe"])
        self.assertEqual(snapshot["pit_status"], "available")
        self.assertEqual(snapshot["backtest_sharpe"], 0.74)
        self.assertEqual(snapshot["top_sectors_preview"][0]["sector_name"], "Tech")

    def test_markdown_rendering(self) -> None:
        snapshot = load_research_dashboard_snapshot(Path("definitely_missing_dashboard.json"))
        text = render_daily_research_snapshot_markdown(snapshot)
        for token in ["Research Dashboard Snapshot", "Readiness", "PIT universe", "Disclaimer", "research-dashboard --summary true"]:
            self.assertIn(token, text)
        self.assertNotIn("????", text)
        self.assertNotIn("???", text)
        self.assertNotIn("\ufffd", text)

    def test_daily_cli_json_summary_includes_snapshot(self) -> None:
        completed = subprocess.run(
            [
                str(PYTHON),
                "scripts/qd.py",
                "daily",
                "--network",
                "false",
                "--with-guardrails",
                "true",
                "--json",
                "true",
                "--summary",
                "true",
            ],
            cwd=self.isolated_project_root,
            env=self._isolated_project.subprocess_env(),
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            timeout=DAILY_CLI_TIMEOUT_SECONDS,
        )
        self.assertEqual(completed.returncode, 0, completed.stdout[-1000:] + completed.stderr)
        payload = json.loads(completed.stdout)
        self.assertIn("output_paths", payload)
        self.assertIn("research_dashboard_status", payload)
        self.assertIn("pit_universe", payload)

        latest = json.loads(
            (self.isolated_project_root / "results" / "daily_workflow_latest.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertIn("research_dashboard_snapshot", latest)
        report = (
            self.isolated_project_root / "reports" / "daily_quant_report_latest.md"
        ).read_text(encoding="utf-8")
        self.assertIn("Research Dashboard Snapshot", report)

    def test_self_check_mentions_daily_research_snapshot(self) -> None:
        completed = subprocess.run(
            [str(PYTHON), "scripts/self_check.py"],
            cwd=PROJECT_ROOT,
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            timeout=180,
        )
        self.assertEqual(completed.returncode, 0, completed.stdout[-1000:] + completed.stderr)
        self.assertIn("daily_research_snapshot", completed.stdout)
        self.assertIn('"fail": 0', completed.stdout)

    def test_research_dashboard_and_pit_regressions(self) -> None:
        commands = [
            [str(PYTHON), "scripts/qd.py", "research-dashboard", "--json", "true", "--summary", "true", "--synthetic-test-data", "true"],
            [str(PYTHON), "scripts/qd.py", "pit-universe", "--json", "true", "--summary", "true"],
            [str(PYTHON), "scripts/qd.py", "sector-rotation", "--json", "true", "--summary", "true", "--synthetic-test-data", "true"],
            [str(PYTHON), "scripts/qd.py", "combined-score-backtest", "--json", "true", "--summary", "true", "--synthetic-test-data", "true"],
            [str(PYTHON), "scripts/qd.py", "factor-combine", "--json", "true", "--summary", "true", "--synthetic-test-data", "true"],
            [str(PYTHON), "scripts/qd.py", "factor-eval", "--json", "true", "--summary", "true", "--synthetic-test-data", "true"],
        ]
        for command in commands:
            completed = subprocess.run(
                command,
                cwd=self.isolated_project_root,
                env=self._isolated_project.subprocess_env(),
                text=True,
                encoding="utf-8",
                errors="replace",
                capture_output=True,
                timeout=180,
            )
            self.assertEqual(completed.returncode, 0, " ".join(command) + (completed.stdout or "")[-500:] + (completed.stderr or ""))
            json.loads(completed.stdout)


if __name__ == "__main__":
    unittest.main()
