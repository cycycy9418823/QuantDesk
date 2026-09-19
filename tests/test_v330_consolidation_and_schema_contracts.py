from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from quantdesk.command_center_status import build_command_center_status
from quantdesk.operation_log import append_operation_log, build_operation_record
from quantdesk.result_archive import build_archive_plan
from quantdesk.result_payload_validator import (
    validate_no_secret_leak,
    validate_result_payload,
)
from quantdesk.research_daily_pipeline import render_research_daily_pipeline_markdown
from quantdesk.schema_contracts import build_base_result, normalize_status

try:
    from tests._operation_log_isolation import isolate_operation_log_dir
except ModuleNotFoundError:  # pragma: no cover - unittest discovery adds tests/ directly
    from _operation_log_isolation import isolate_operation_log_dir

isolate_operation_log_dir()

PROJECT_ROOT = Path(__file__).resolve().parents[1]


class ConsolidationAndSchemaContractsTests(unittest.TestCase):
    def test_build_base_result_contains_common_fields(self) -> None:
        payload = build_base_result(status="ok", target_date="2026-05-19", symbols="600744")
        for key in ("schema_version", "app_version", "status", "warnings", "disclaimer"):
            self.assertIn(key, payload)
        self.assertEqual(payload["status"], "passed")
        self.assertEqual(payload["symbols"], ["600744"])
        self.assertIsInstance(payload["warnings"], list)

    def test_normalize_status_aliases(self) -> None:
        self.assertEqual(normalize_status("ok"), "passed")
        self.assertEqual(normalize_status("error"), "failed")
        self.assertEqual(normalize_status("not_available"), "unavailable")

    def test_validate_result_payload_detects_missing_schema_version(self) -> None:
        payload = {"app_version": "x", "status": "passed", "warnings": [], "disclaimer": "local only"}
        result = validate_result_payload(payload)
        self.assertEqual(result["status"], "warning")
        self.assertIn("missing_schema_version", result["warnings"])

    def test_validate_result_payload_detects_illegal_status(self) -> None:
        payload = {"schema_version": "3.3.0", "app_version": "x", "status": "great", "warnings": [], "disclaimer": "x"}
        result = validate_result_payload(payload, strict=True)
        self.assertEqual(result["status"], "failed")
        self.assertIn("illegal_status", result["errors"])

    def test_validate_result_payload_detects_warnings_not_list(self) -> None:
        payload = {"schema_version": "3.3.0", "app_version": "x", "status": "passed", "warnings": "oops", "disclaimer": "x"}
        result = validate_result_payload(payload)
        self.assertEqual(result["status"], "warning")
        self.assertIn("warnings_not_list", result["warnings"])

    def test_secret_leak_detection_masks_values(self) -> None:
        secret = "abcd1234SECRET5678"
        result = validate_no_secret_leak({"api_key": secret, "nested": {"token": "tok_test_123456"}})
        text = json.dumps(result, ensure_ascii=False)
        self.assertFalse(result["ok"])
        self.assertGreaterEqual(result["secret_leak_count"], 2)
        self.assertNotIn(secret, text)
        self.assertIn("abcd****5678", text)

    def test_schema_check_cli_exists_and_help_mentions_it(self) -> None:
        self.assertTrue((PROJECT_ROOT / "scripts" / "check_result_schemas.py").exists())
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
        self.assertIn("schema-check", completed.stdout)

    def test_schema_check_network_false_missing_results_warns_not_fail(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            output = root / "schema_validation_latest.json"
            completed = subprocess.run(
                [
                    sys.executable,
                    str(PROJECT_ROOT / "scripts" / "check_result_schemas.py"),
                    "--results-dir",
                    str(root / "missing_results"),
                    "--reports-dir",
                    str(root / "reports"),
                    "--output",
                    str(output),
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
            payload = json.loads(output.read_text(encoding="utf-8"))
        self.assertEqual(completed.returncode, 0, completed.stderr + completed.stdout)
        self.assertFalse(payload["network"])
        self.assertEqual(payload["status"], "warning")
        self.assertGreater(payload["missing_count"], 0)

    def test_schema_check_qd_accepts_common_json_and_summary_flags(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            output = root / "schema_validation_latest.json"
            completed = subprocess.run(
                [
                    sys.executable,
                    str(PROJECT_ROOT / "scripts" / "qd.py"),
                    "schema-check",
                    "--results-dir",
                    str(root / "missing_results"),
                    "--reports-dir",
                    str(root / "reports"),
                    "--output",
                    str(output),
                    "--network",
                    "false",
                    "--json",
                    "true",
                    "--summary",
                    "true",
                ],
                cwd=PROJECT_ROOT,
                text=True,
                encoding="utf-8",
                errors="replace",
                capture_output=True,
                check=False,
                timeout=60,
            )
            payload = json.loads(output.read_text(encoding="utf-8"))
            stdout_payload = json.loads(completed.stdout)

        self.assertEqual(completed.returncode, 0, completed.stderr + completed.stdout)
        self.assertEqual(payload["status"], "warning")
        self.assertEqual(stdout_payload["status"], "warning")
        self.assertNotIn("unrecognized arguments", completed.stderr)

    def test_schema_check_strict_secret_leak_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            results = root / "results"
            results.mkdir()
            (results / "strategy_validation_latest.json").write_text(
                json.dumps(
                    {
                        "schema_version": "3.3.0",
                        "app_version": "x",
                        "status": "passed",
                        "warnings": [],
                        "disclaimer": "local only",
                        "api_key": "abcd1234SECRET5678",
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            output = root / "schema_validation_latest.json"
            completed = subprocess.run(
                [
                    sys.executable,
                    str(PROJECT_ROOT / "scripts" / "check_result_schemas.py"),
                    "--results-dir",
                    str(results),
                    "--reports-dir",
                    str(root / "reports"),
                    "--output",
                    str(output),
                    "--network",
                    "false",
                    "--strict",
                    "true",
                ],
                cwd=PROJECT_ROOT,
                text=True,
                encoding="utf-8",
                errors="replace",
                capture_output=True,
                check=False,
            )
            payload = json.loads(output.read_text(encoding="utf-8"))
        self.assertEqual(completed.returncode, 1)
        self.assertEqual(payload["status"], "failed")
        self.assertGreater(payload["secret_leak_count"], 0)

    def test_command_center_can_show_schema_check_status(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            log_path = Path(tmp) / "operation_timeline.jsonl"
            append_operation_log(build_operation_record(command="schema-check", status="warning"), log_path)
            payload = build_command_center_status(log_path=log_path)
        row = next(item for item in payload["commands"] if item["command"] == "schema-check")
        self.assertEqual(row["last_status"], "warning")

    def test_archive_plan_contains_schema_validation_outputs(self) -> None:
        plan = build_archive_plan(target_date="2026-05-19", dry_run=True)
        sources = "\n".join(item["source"] for item in plan["entries"])
        self.assertIn("results/schema_validation_latest.json", sources)
        self.assertIn("reports/schema_validation_report_latest.md", sources)

    def test_research_daily_pipeline_report_humanizes_step_status(self) -> None:
        report = render_research_daily_pipeline_markdown(
            {
                "status": "warning",
                "research_only": True,
                "failed_steps": ["risk_stat"],
                "warning_steps": ["factor_ic"],
                "steps": [
                    {
                        "name": "factor_ic",
                        "status": "warning",
                        "duration_seconds": 1.234,
                        "warnings": ["sample small"],
                    },
                    {
                        "name": "risk_stat",
                        "status": "failed",
                        "duration_seconds": 2.345,
                        "warnings": [],
                        "error": "missing equity file",
                    },
                ],
                "prohibited_usage": ["broker_connection", "auto_execution"],
                "disclaimer": "Research only; no broker connection.",
            }
        )

        self.assertIn("研究日流程", report)
        self.assertIn("状态：", report)
        self.assertIn("失败步骤：", report)
        self.assertIn("警告步骤：", report)
        self.assertIn("耗时：", report)
        self.assertIn("警告数：", report)
        self.assertIn("错误：", report)
        for raw in (
            "- status:",
            "- research_only:",
            "- failed_steps:",
            "- warning_steps:",
            "status=`",
            "duration=",
            "warnings=",
            "error:",
            "## Steps",
            "## Prohibited Usage",
        ):
            self.assertNotIn(raw, report)

    def test_schema_docs_and_examples_exist(self) -> None:
        self.assertTrue((PROJECT_ROOT / "docs" / "schema_contracts.md").exists())
        self.assertTrue((PROJECT_ROOT / "docs" / "api_payload_schema.md").exists())
        examples = sorted((PROJECT_ROOT / "docs" / "schema_examples").glob("*.json"))
        self.assertGreaterEqual(len(examples), 3)
        for path in examples[:3]:
            payload = json.loads(path.read_text(encoding="utf-8"))
            self.assertIn("schema_version", payload)
            self.assertNotIn("api_key", json.dumps(payload, ensure_ascii=False).lower())

    def test_clean_zip_rules_exclude_schema_validation_outputs(self) -> None:
        export_text = (PROJECT_ROOT / "export_project.ps1").read_text(encoding="utf-8")
        self.assertIn('"results"', export_text)
        self.assertIn('"reports"', export_text)
        self.assertTrue((PROJECT_ROOT / "docs" / "schema_examples" / "data_health_dashboard.example.json").exists())


if __name__ == "__main__":
    unittest.main()


