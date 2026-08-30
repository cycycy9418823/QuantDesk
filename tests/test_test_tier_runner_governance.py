from __future__ import annotations

import json
import math
import re
import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

from quantdesk.maintenance import test_tier_runner

try:
    from tests._operation_log_isolation import isolate_operation_log_dir
except ModuleNotFoundError:  # pragma: no cover - unittest discovery adds tests/ directly
    from _operation_log_isolation import isolate_operation_log_dir

isolate_operation_log_dir()

ROOT = Path(__file__).resolve().parents[1]


def _assert_json_safe(value):
    if isinstance(value, float):
        assert not math.isnan(value)
        assert not math.isinf(value)
    elif isinstance(value, dict):
        for child in value.values():
            _assert_json_safe(child)
    elif isinstance(value, list):
        for child in value:
            _assert_json_safe(child)


class TestTierRunnerGovernanceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.payload = test_tier_runner.build_test_tier_runner_payload(
            tier="tier0_fast_unit",
            limit=2,
            dry_run=True,
        )

    def test_001_schema(self) -> None:
        payload = self.payload
        self.assertEqual(payload["task_type"], "test_tier_runner")
        self.assertTrue(payload["dry_run"])
        self.assertIn("selected_files", payload)
        self.assertIn("command", payload)
        self.assertIn("command_kind", payload)
        self.assertIn("counts_by_tier", payload)

    def test_002_tier3_requires_explicit_flag(self) -> None:
        payload = test_tier_runner.build_test_tier_runner_payload(
            tier="tier3_release_or_long",
            dry_run=True,
            include_tier3=False,
        )
        self.assertEqual(payload["selected_file_count"], 0)
        self.assertIn("tier3_requires_include_tier3_true", payload["warnings"])

    def test_002b_legacy_versioned_tests_require_explicit_flag(self) -> None:
        payload = test_tier_runner.build_test_tier_runner_payload(
            tier="legacy_versioned_acceptance",
            dry_run=True,
            include_legacy=False,
        )
        self.assertEqual(payload["selected_file_count"], 0)
        self.assertIn("legacy_tier_requires_include_legacy_true", payload["warnings"])

        selected = test_tier_runner.build_test_tier_runner_payload(
            tier="legacy_versioned_acceptance",
            limit=3,
            dry_run=True,
            include_legacy=True,
        )
        self.assertGreater(selected["selected_file_count"], 0)
        self.assertTrue(all(re.match(r"test_v\d", Path(item["path"]).name) for item in selected["selected_files"]))

    def test_002c_tier0_default_excludes_versioned_acceptance_tests(self) -> None:
        payload = test_tier_runner.build_test_tier_runner_payload(
            tier="tier0_fast_unit",
            limit=80,
            dry_run=True,
        )
        self.assertTrue(all(not re.match(r"test_v\d", Path(item["path"]).name) for item in payload["selected_files"]))

    def test_003_cli_summary_json(self) -> None:
        proc = subprocess.run(
            [
                sys.executable,
                "scripts/qd.py",
                "maintenance",
                "test-tier-runner",
                "--json",
                "true",
                "--summary",
                "true",
                "--tier",
                "tier0_fast_unit",
                "--limit",
                "2",
                "--dry-run",
                "true",
            ],
            cwd=ROOT,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=120,
            check=True,
        )
        payload = json.loads(proc.stdout)
        self.assertEqual(payload["task_type"], "test_tier_runner")
        self.assertTrue(payload["dry_run"])
        self.assertIn("command", payload)

    def test_003b_cli_accepts_short_tier_alias(self) -> None:
        proc = subprocess.run(
            [
                sys.executable,
                "scripts/qd.py",
                "maintenance",
                "test-tier-runner",
                "--json",
                "true",
                "--summary",
                "true",
                "--tier",
                "tier0",
                "--limit",
                "1",
                "--dry-run",
                "true",
            ],
            cwd=ROOT,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=120,
            check=True,
        )
        payload = json.loads(proc.stdout)
        self.assertEqual(payload["tier"], "tier0_fast_unit")
        self.assertGreaterEqual(payload["selected_file_count"], 1)

    def test_003c_direct_script_accepts_positional_tier_alias(self) -> None:
        proc = subprocess.run(
            [
                sys.executable,
                "scripts/run_test_tier_runner.py",
                "tier0",
                "--json",
                "true",
                "--summary",
                "true",
                "--limit",
                "1",
                "--dry-run",
                "true",
            ],
            cwd=ROOT,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=120,
            check=True,
        )
        payload = json.loads(proc.stdout)
        self.assertEqual(payload["tier"], "tier0_fast_unit")
        self.assertGreaterEqual(payload["selected_file_count"], 1)

    def test_004_outputs_are_written(self) -> None:
        payload = test_tier_runner.write_test_tier_runner_outputs(self.payload)
        self.assertEqual(payload["output_paths"]["json"], "results/maintenance/test_tier_runner_latest.json")
        self.assertTrue((ROOT / "results" / "maintenance" / "test_tier_runner_latest.json").exists())
        self.assertTrue((ROOT / "reports" / "maintenance" / "test_tier_runner_latest.md").exists())

    def test_005_docs_and_self_check_marker_exist(self) -> None:
        docs = (ROOT / "docs" / "script_entrypoint_inventory.md").read_text(encoding="utf-8")
        self.assertIn("test-tier-runner", docs)
        self_check = (ROOT / "scripts" / "self_check.py").read_text(encoding="utf-8")
        self.assertIn("check_test_tier_runner_governance", self_check)

    def test_006_json_has_no_nan_or_infinity(self) -> None:
        _assert_json_safe(self.payload)

    def test_007_runner_is_read_only(self) -> None:
        text = (ROOT / "quantdesk" / "maintenance" / "test_tier_runner.py").read_text(encoding="utf-8")
        forbidden = ["git clean", "git restore", "Remove-Item", "shutil.rmtree", ".unlink("]
        for token in forbidden:
            with self.subTest(token=token):
                self.assertNotIn(token, text)

    def test_008_mixed_runner_selection_executes_as_separate_commands(self) -> None:
        command_kind, commands = test_tier_runner._runner_commands(
            ["tests/test_pytest_style.py"],
            ["tests.test_unittest_style"],
        )

        self.assertEqual(command_kind, "mixed")
        self.assertEqual([item["kind"] for item in commands], ["pytest", "unittest"])

    def test_009_mixed_runner_processes_are_aggregated(self) -> None:
        class DummyProcess:
            returncode = 0
            stdout = "ok"
            stderr = ""

        with patch("quantdesk.maintenance.test_tier_runner.subprocess.run", return_value=DummyProcess()):
            status, aggregate, processes, warnings = test_tier_runner._run_runner_commands(
                [
                    {"kind": "pytest", "command": [sys.executable, "-m", "pytest", "tests/test_a.py", "-q"]},
                    {"kind": "unittest", "command": [sys.executable, "-m", "unittest", "tests.test_b", "-v"]},
                ],
                timeout_seconds=30,
                project_root=ROOT,
            )

        self.assertEqual(status, "passed")
        self.assertEqual(aggregate["returncode"], 0)
        self.assertEqual(len(processes), 2)
        self.assertEqual(warnings, [])


if __name__ == "__main__":
    unittest.main()
