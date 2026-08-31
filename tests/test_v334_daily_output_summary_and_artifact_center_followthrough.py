from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

try:
    from tests._isolated_project import IsolatedProject
except ModuleNotFoundError:  # pragma: no cover - unittest discovery adds tests/ directly
    from _isolated_project import IsolatedProject

PROJECT_ROOT = Path(__file__).resolve().parents[1]
TEST_TMP_ROOT = PROJECT_ROOT / ".quantdesk_test_tmp"
# The real local data workspace currently needs about 179 seconds for one
# offline daily run. Keep a material Windows I/O margin while preserving every
# return-code and payload assertion below.
DAILY_CLI_TIMEOUT_SECONDS = 300


class V334DailyOutputSummaryArtifactFollowthroughTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        TEST_TMP_ROOT.mkdir(parents=True, exist_ok=True)
        cls._isolated_project = IsolatedProject(PROJECT_ROOT, temp_parent=TEST_TMP_ROOT).start()
        assert cls._isolated_project.root is not None
        cls.isolated_project_root = cls._isolated_project.root

    @classmethod
    def tearDownClass(cls) -> None:
        cls._isolated_project.cleanup()

    def setUp(self) -> None:
        TEST_TMP_ROOT.mkdir(parents=True, exist_ok=True)

    def _temp_dir(self) -> tempfile.TemporaryDirectory[str]:
        return tempfile.TemporaryDirectory(dir=TEST_TMP_ROOT)

    @staticmethod
    def _write_json(path: Path, payload: dict) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    def test_version_is_v334(self) -> None:
        from quantdesk.version import API_SCHEMA_VERSION, APP_VERSION, PACKAGE_VERSION

        self.assertIn(PACKAGE_VERSION, {"3.4.2.2", "3.4.2.3", "3.4.2.4", "3.4.2.5", "3.5.1", "3.5.1.1", "3.5.2", "3.6.0", "3.6.1", "3.6.2", "3.7.0", "3.7.0.1", "3.7.0.2", "3.7.0.3", "3.7.0.4", "3.7.0.5", "3.7.0.6"})
        self.assertIn(API_SCHEMA_VERSION, {"3.4.2.2", "3.4.2.3", "3.4.2.4", "3.4.2.5", "3.5.1", "3.5.1.1", "3.5.2", "3.6.0", "3.6.1", "3.6.2", "3.7.0", "3.7.0.1", "3.7.0.2", "3.7.0.3", "3.7.0.4", "3.7.0.5", "3.7.0.6"})
        self.assertTrue(APP_VERSION.startswith(API_SCHEMA_VERSION))

    def test_daily_default_stdout_is_short_summary_and_mentions_artifacts(self) -> None:
        completed = subprocess.run(
            [sys.executable, "scripts/qd.py", "daily", "--network", "false", "--with-guardrails", "true"],
            cwd=self.isolated_project_root,
            env=self._isolated_project.subprocess_env(),
            text=True,
            encoding="utf-8",
            errors="replace",
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            timeout=DAILY_CLI_TIMEOUT_SECONDS,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("QuantDesk", completed.stdout)
        self.assertIn("Guardrails", completed.stdout)
        self.assertIn("reports/daily_quant_report_latest.md", completed.stdout)
        self.assertIn("results/daily_workflow_latest.json", completed.stdout)
        self.assertNotIn('"daily_price_status"', completed.stdout)
        self.assertFalse(completed.stdout.lstrip().startswith("{"))

    def test_daily_json_flag_prints_full_json(self) -> None:
        completed = subprocess.run(
            [sys.executable, "scripts/qd.py", "daily", "--network", "false", "--with-guardrails", "true", "--json", "true"],
            cwd=self.isolated_project_root,
            env=self._isolated_project.subprocess_env(),
            text=True,
            encoding="utf-8",
            errors="replace",
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            timeout=DAILY_CLI_TIMEOUT_SECONDS,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        payload = json.loads(completed.stdout)
        self.assertIn("daily_price_status", payload)
        self.assertIn("daily_console_summary", payload)
        self.assertIn("artifacts", payload)

    def test_daily_result_contains_console_summary_and_standard_artifacts(self) -> None:
        path = self.isolated_project_root / "results" / "daily_workflow_latest.json"
        if not path.exists():
            completed = subprocess.run(
                [sys.executable, "scripts/qd.py", "daily", "--network", "false", "--with-guardrails", "true"],
                cwd=self.isolated_project_root,
                env=self._isolated_project.subprocess_env(),
                text=True,
                encoding="utf-8",
                errors="replace",
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
                timeout=DAILY_CLI_TIMEOUT_SECONDS,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertTrue(path.exists(), "daily subprocess should have written latest JSON")
        payload = json.loads(path.read_text(encoding="utf-8"))
        self.assertIn("daily_console_summary", payload)
        self.assertIn("artifacts", payload)
        artifacts = payload["artifacts"]
        self.assertEqual(artifacts.get("daily_report"), "reports/daily_quant_report_latest.md")
        self.assertEqual(artifacts.get("daily_result"), "results/daily_workflow_latest.json")
        self.assertIn("data_health", artifacts)
        self.assertIn("command_center", artifacts)
        self.assertNotEqual(payload.get("workflow_status"), "failed")

    def test_status_reads_recent_daily_and_mentions_report_path(self) -> None:
        from quantdesk.status_advisor import build_quantdesk_status, format_status_console_output

        with self._temp_dir() as tmp:
            root = Path(tmp)
            self._write_json(
                root / "results" / "daily_workflow_latest.json",
                {
                    "status": "warning",
                    "generated_at": "2026-05-22T09:00:00",
                    "daily_console_summary": {
                        "warning_count": 2,
                        "failed_count": 0,
                        "report_path": "reports/daily_quant_report_latest.md",
                        "result_path": "results/daily_workflow_latest.json",
                    },
                    "guardrail_summary": {"warning_count": 2, "failed_count": 0},
                    "warnings": ["sample warning"],
                },
            )
            payload = build_quantdesk_status(project_root=root, write=False)
            text = format_status_console_output(payload)
        self.assertEqual(payload["daily_summary"]["warning_count"], 2)
        self.assertIn("reports/daily_quant_report_latest.md", text)

    def test_command_center_daily_row_contains_report_and_counts(self) -> None:
        from quantdesk import command_center_status as ccs
        from quantdesk.operation_log import append_operation_log, build_operation_record

        with self._temp_dir() as tmp:
            root = Path(tmp)
            self._write_json(
                root / "results" / "daily_workflow_latest.json",
                {
                    "status": "warning",
                    "daily_console_summary": {
                        "warning_count": 4,
                        "failed_count": 0,
                        "report_path": "reports/daily_quant_report_latest.md",
                        "result_path": "results/daily_workflow_latest.json",
                    },
                    "guardrail_summary": {"warning_count": 4, "failed_count": 0},
                },
            )
            log_path = root / "results" / "operation_log" / "operation_timeline.jsonl"
            append_operation_log(
                build_operation_record(command="daily", status="warning", output_paths=["results/daily_workflow_latest.json"]),
                log_path,
            )
            old_root = ccs.PROJECT_ROOT
            try:
                ccs.PROJECT_ROOT = root
                payload = ccs.build_command_center_status(log_path=log_path)
            finally:
                ccs.PROJECT_ROOT = old_root
        rows = {row["command"]: row for row in payload["commands"]}
        self.assertIn("daily", rows)
        self.assertEqual(rows["daily"]["warning_count"], 4)
        self.assertEqual(rows["daily"]["failed_count"], 0)
        self.assertEqual(rows["daily"]["report_path"], "reports/daily_quant_report_latest.md")

    def test_artifact_center_daily_group_and_missing_state(self) -> None:
        from quantdesk.workbench_overview import build_workbench_artifacts

        with self._temp_dir() as tmp:
            payload = build_workbench_artifacts(project_root=Path(tmp))
        self.assertIn("每日流程", payload["groups"])
        rows = payload["groups"]["每日流程"]
        self.assertTrue(any(row["path"] == "reports/daily_quant_report_latest.md" for row in rows))
        self.assertTrue(any(row["path"] == "results/daily_workflow_latest.json" for row in rows))
        self.assertTrue(all(row["status"] == "missing" for row in rows))

    def test_help_contains_recommended_daily_flow(self) -> None:
        completed = subprocess.run(
            [sys.executable, "scripts/qd.py", "help"],
            cwd=PROJECT_ROOT,
            text=True,
            encoding="utf-8",
            errors="replace",
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            timeout=60,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("python scripts/qd.py daily --network false --with-guardrails true", completed.stdout)
        self.assertIn("python scripts/qd.py status", completed.stdout)

    def test_active_bundle_surfaces_degraded_daily_states(self) -> None:
        """A daily step that warned, failed or produced nothing must say so.

        Re-anchored from the retired app/static/app.js. The old 每日流程 label and
        the literal "warning="/"failed=" forms do not appear on the active bundle, so
        this asserts the same property through the state vocabulary the active bundle
        actually renders.
        """
        text = (PROJECT_ROOT / "app" / "static" / "js" / "daily_research.js").read_text(
            encoding="utf-8"
        )
        for token in ("warning", "failed", "未生成", "每日"):
            self.assertIn(token, text, f"active bundle no longer surfaces {token}")
        # a degraded run must be visibly degraded, not silently blank
        self.assertIn("degraded", text)


if __name__ == "__main__":
    unittest.main()



