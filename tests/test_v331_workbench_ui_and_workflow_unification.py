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

    def test_workbench_overview_route_registered(self) -> None:
        server_text = (PROJECT_ROOT / "scripts" / "quant_app_server.py").read_text(encoding="utf-8")
        for route in (
            "/api/workbench/overview",
            "/api/workbench/command-center",
            "/api/workbench/artifacts",
            "/api/workbench/next-actions",
        ):
            self.assertIn(route, server_text)

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

    def test_app_js_contains_sidebar_collapse_key(self) -> None:
        text = (PROJECT_ROOT / "app" / "static" / "app.js").read_text(encoding="utf-8")
        self.assertIn("quantdesk.sidebar.collapsed", text)

    def test_app_js_contains_beginner_expert_mode_key(self) -> None:
        text = (PROJECT_ROOT / "app" / "static" / "app.js").read_text(encoding="utf-8")
        self.assertIn("quantdesk.ui.mode", text)
        self.assertIn("quantdesk.beginnerMode.enabled", text)

    def test_command_center_not_in_default_overview_section(self) -> None:
        text = (PROJECT_ROOT / "app" / "static" / "app.js").read_text(encoding="utf-8")
        self.assertIn('overview: ["workbenchOverviewSection"]', text)
        overview_line = next(line for line in text.splitlines() if line.strip().startswith("overview:"))
        self.assertNotIn("commandCenterSection", overview_line)

    def test_command_center_independent_entry_exists(self) -> None:
        html = (PROJECT_ROOT / "app" / "index.html").read_text(encoding="utf-8")
        js = (PROJECT_ROOT / "app" / "static" / "app.js").read_text(encoding="utf-8")
        self.assertIn('data-primary-tab="commandCenter"', html)
        self.assertIn('commandCenter: ["commandCenterSection"]', js)

    def test_artifact_center_entry_exists(self) -> None:
        html = (PROJECT_ROOT / "app" / "index.html").read_text(encoding="utf-8")
        js = (PROJECT_ROOT / "app" / "static" / "app.js").read_text(encoding="utf-8")
        self.assertIn('data-primary-tab="artifactCenter"', html)
        self.assertIn("artifactCenterSection", html)
        self.assertIn('artifactCenter: ["artifactCenterSection"]', js)

    def test_status_classes_support_common_statuses(self) -> None:
        css = (PROJECT_ROOT / "app" / "static" / "styles.css").read_text(encoding="utf-8")
        for status in ("passed", "warning", "failed", "missing", "skipped", "unavailable"):
            self.assertIn(f"status-{status}", css)

    def test_node_check_app_js(self) -> None:
        if not shutil.which("node"):
            self.skipTest("node is not installed")
        completed = subprocess.run(
            ["node", "--check", "app/static/app.js"],
            cwd=PROJECT_ROOT,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=60,
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


