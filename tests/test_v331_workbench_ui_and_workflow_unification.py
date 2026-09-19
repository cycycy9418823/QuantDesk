from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]


class WorkbenchUiWorkflowUnificationTests(unittest.TestCase):
    def test_qd_help_still_passes(self) -> None:
        completed = subprocess.run(
            [sys.executable, "scripts/qd.py", "help"],
            cwd=PROJECT_ROOT,
            text=True,
            encoding="utf-8",
            errors="replace",
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=60,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("data-health", completed.stdout)
        self.assertIn("schema-check", completed.stdout)


    def test_overview_missing_results_is_warning_not_error(self) -> None:
        from quantdesk.workbench_overview import build_workbench_overview

        with tempfile.TemporaryDirectory() as tmp:
            payload = build_workbench_overview(project_root=tmp)
        self.assertTrue(payload["ok"])
        self.assertIn(payload["status"], {"warning", "missing"})
        self.assertIn("next_actions", payload)

    def test_artifacts_api_groups_outputs(self) -> None:
        from quantdesk.workbench_overview import build_workbench_artifacts

        with tempfile.TemporaryDirectory() as tmp:
            payload = build_workbench_artifacts(project_root=tmp)
        self.assertTrue(payload["ok"])
        self.assertIn("数据健康", payload["groups"])
        self.assertIn("策略验证", payload["groups"])
        self.assertGreater(payload["missing_count"], 0)

    def test_next_actions_suggest_daily_retain_without_daily_data(self) -> None:
        from quantdesk.workbench_overview import build_workbench_next_actions

        with tempfile.TemporaryDirectory() as tmp:
            payload = build_workbench_next_actions(project_root=tmp)
        commands = " ".join(action.get("command", "") for action in payload["actions"])
        self.assertIn("daily-retain", commands)
        self.assertFalse(payload.get("network", True))


    def test_node_check_active_bundle(self) -> None:
        """The bundle the product actually serves must parse.

        Re-anchored from the retired app/static/app.js. Verified before the change
        that no test anywhere ran node --check against the active bundle, so
        retiring this gate outright would have left the served bundle with no
        syntax check at all.
        """
        if not shutil.which("node"):
            self.skipTest("node is not installed")
        completed = subprocess.run(
            ["node", "--check", "app/static/js/daily_research.js"],
            cwd=PROJECT_ROOT,
            text=True,
            encoding="utf-8",
            errors="replace",
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=120,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)

    def test_clean_zip_excludes_results(self) -> None:
        export_text = (PROJECT_ROOT / "export_project.ps1").read_text(encoding="utf-8")
        self.assertIn('"results"', export_text)
        self.assertIn('"reports"', export_text)
        self.assertIn('_maintenance', export_text)
        self.assertIn('*_cli_verify.json', export_text)
        zip_path = PROJECT_ROOT / "dist" / "quantdesk_clean.zip"
        if zip_path.exists():
            with zipfile.ZipFile(zip_path) as zf:
                names = zf.namelist()
            self.assertFalse(any(name.startswith("results/") for name in names))
            self.assertFalse(any(name.startswith("reports/") for name in names))

    def test_v330_test_file_still_present(self) -> None:
        self.assertTrue((PROJECT_ROOT / "tests" / "test_v330_consolidation_and_schema_contracts.py").exists())


if __name__ == "__main__":
    unittest.main()


