from __future__ import annotations

import json
import math
import subprocess
import sys
import unittest
from pathlib import Path

from quantdesk.maintenance import test_discovery_diagnostics

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


class TestDiscoveryDiagnosticsGovernanceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.payload = test_discovery_diagnostics.build_test_discovery_diagnostics(
            sample_limit=0,
            discover_timeout_seconds=10,
            include_probes=False,
            include_timed_test_probes=False,
        )

    def test_001_schema(self) -> None:
        payload = self.payload
        self.assertEqual(payload["task_type"], "test_discovery_diagnostics")
        self.assertIn("import_discovery_probe", payload)
        self.assertIn("counts_by_tier", payload)
        self.assertIn("recommendations", payload)
        self.assertIn("timeout_probe_reviews", payload)
        self.assertIn("failed_probe_reviews", payload)
        self.assertIn("timed_test_probe_reviews", payload)
        self.assertIn("timed_test_probe_count", payload)
        self.assertIn("resolved_timeout_paths", payload)
        self.assertIn("unresolved_timeout_paths", payload)
        self.assertIn("resolved_timeout_count", payload)
        self.assertIn("unresolved_timeout_count", payload)
        self.assertGreater(payload["test_file_count"], 100)

    def test_002_import_discovery_probe_is_recorded(self) -> None:
        probe = self.payload["import_discovery_probe"]
        self.assertIn(probe["status"], {"passed", "failed", "timeout"})
        self.assertIn("duration_seconds", probe)
        self.assertIn("command", probe)

    def test_003_cli_summary_json(self) -> None:
        proc = subprocess.run(
            [
                sys.executable,
                "scripts/qd.py",
                "maintenance",
                "test-discovery-diagnostics",
                "--json",
                "true",
                "--summary",
                "true",
                "--sample-limit",
                "0",
                "--discover-timeout-seconds",
                "10",
                "--timed-test-timeout-seconds",
                "2",
                "--no-probes",
                "--no-timed-test-probes",
                "--include-legacy",
                "false",
            ],
            cwd=ROOT,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=120,
            check=True,
        )
        payload = json.loads(proc.stdout)
        self.assertEqual(payload["task_type"], "test_discovery_diagnostics")
        self.assertIn("import_discovery_probe", payload)
        self.assertFalse(payload.get("include_legacy"))

    def test_004_outputs_are_written(self) -> None:
        payload = test_discovery_diagnostics.write_test_discovery_diagnostics_outputs(self.payload)
        self.assertEqual(payload["output_paths"]["json"], "results/maintenance/test_discovery_diagnostics_latest.json")
        self.assertTrue((ROOT / "results" / "maintenance" / "test_discovery_diagnostics_latest.json").exists())
        self.assertTrue((ROOT / "reports" / "maintenance" / "test_discovery_diagnostics_latest.md").exists())

    def test_005_docs_and_self_check_marker_exist(self) -> None:
        docs = (ROOT / "docs" / "script_entrypoint_inventory.md").read_text(encoding="utf-8")
        self.assertIn("test-discovery-diagnostics", docs)
        self_check = (ROOT / "scripts" / "self_check.py").read_text(encoding="utf-8")
        self.assertIn("check_test_discovery_diagnostics_governance", self_check)

    def test_006_json_has_no_nan_or_infinity(self) -> None:
        _assert_json_safe(self.payload)

    def test_007_diagnostic_is_read_only(self) -> None:
        text = (ROOT / "quantdesk" / "maintenance" / "test_discovery_diagnostics.py").read_text(encoding="utf-8")
        forbidden = ["git clean", "git restore", "Remove-Item", "shutil.rmtree", ".unlink("]
        for token in forbidden:
            with self.subTest(token=token):
                self.assertNotIn(token, text)

    def test_008_per_file_probe_uses_discover_semantics(self) -> None:
        command = test_discovery_diagnostics._probe_command("tests/test_v11_ui_workbench.py")
        self.assertIn("discover", command)
        self.assertIn("-s", command)
        self.assertIn("tests", command)
        self.assertIn("-p", command)
        self.assertIn("test_v11_ui_workbench.py", command)

    def test_008b_filtered_discovery_excludes_legacy_by_default(self) -> None:
        payload = test_discovery_diagnostics._discover_filtered_unittest_cases(include_legacy=False)
        self.assertTrue(payload["module_count"] > 0)
        self.assertEqual(payload["failure_count"], 0)
        self.assertIn("unittest_case_count", payload)

    def test_009_timed_test_probe_uses_discover_pattern(self) -> None:
        command = test_discovery_diagnostics._timed_test_probe_command("tests/test_v11_ui_workbench.py")
        self.assertIn("scripts/run_unittest_timing_probe.py", command)
        self.assertIn("--pattern", command)
        self.assertIn("test_v11_ui_workbench.py", command)

    def test_010_timeout_resolution_counts_are_self_consistent(self) -> None:
        payload = test_discovery_diagnostics.build_test_discovery_diagnostics(
            sample_limit=0,
            discover_timeout_seconds=10,
            include_probes=False,
            include_timed_test_probes=False,
        )
        resolved = set(payload["resolved_timeout_paths"])
        unresolved = set(payload["unresolved_timeout_paths"])
        all_timeouts = set(payload["timeout_paths"])
        self.assertFalse(resolved & unresolved)
        self.assertEqual(resolved | unresolved, all_timeouts)
        self.assertEqual(payload["resolved_timeout_count"], len(resolved))
        self.assertEqual(payload["unresolved_timeout_count"], len(unresolved))

    def test_011_timed_probe_nearly_complete_timeout_is_aggregate(self) -> None:
        probe = test_discovery_diagnostics.TimedTestProbe(
            path="tests/test_backtest_module.py",
            tier="tier3_release_or_long",
            test_count=291,
            line_count=2731,
            timeout_seconds=24.0,
            status="timeout",
            duration_seconds=24.0,
            returncode=None,
            started_test_count=286,
            completed_test_count=285,
            timed_out_test_id="test_backtest_module.BacktestModuleTests.test_example",
            slowest_tests=[{"id": "slow", "seconds": 7.5}],
            command=[],
            stdout_tail="",
            stderr_tail="",
            categories=[],
        )

        review = test_discovery_diagnostics._timed_probe_review(probe)

        self.assertEqual(review["suspected_source"], "aggregate_runtime_exceeds_timed_probe_budget")
        self.assertIn("last started method", review["next_action"])

    def test_012_tier3_timed_probe_uses_release_sized_budget_floor(self) -> None:
        timeout = test_discovery_diagnostics._effective_timed_test_timeout_seconds(
            {"tier": "tier3_release_or_long", "test_count": 291, "line_count": 2731},
            24.0,
        )

        self.assertGreaterEqual(timeout, 45.0)

    def test_013_probe_subprocesses_pin_utf8_stdio(self) -> None:
        text = (ROOT / "quantdesk" / "maintenance" / "test_discovery_diagnostics.py").read_text(encoding="utf-8")
        timing_probe = (ROOT / "scripts" / "run_unittest_timing_probe.py").read_text(encoding="utf-8")

        self.assertIn("PYTHONIOENCODING", text)
        self.assertIn('encoding="utf-8"', text)
        self.assertIn("sys.stdout.reconfigure", timing_probe)


if __name__ == "__main__":
    unittest.main()
