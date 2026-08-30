from __future__ import annotations

import json
import math
import subprocess
import sys
import unittest
from pathlib import Path

from quantdesk.maintenance import pytest_suite_runner

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


class PytestSuiteRunnerGovernanceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.payload = pytest_suite_runner.build_pytest_suite_runner_payload(
            suite="all",
            limit=2,
            dry_run=True,
        )

    def test_001_schema_and_suite_names(self) -> None:
        payload = self.payload
        self.assertEqual(payload["task_type"], "pytest_suite_runner")
        self.assertEqual(payload["suite"], "all")
        self.assertTrue(payload["dry_run"])
        self.assertEqual(
            payload["available_suites"],
            ["core", "api", "research", "v3_v4_governance", "slow"],
        )
        self.assertIn("counts_by_suite", payload)
        self.assertIn("tests_by_suite", payload)
        self.assertEqual(len(payload["suite_runs"]), 5)

    def test_002_all_dry_run_selects_each_suite(self) -> None:
        by_suite = {item["suite"]: item for item in self.payload["suite_runs"]}
        for suite in pytest_suite_runner.SUITE_ORDER:
            self.assertIn(suite, by_suite)
            self.assertGreater(by_suite[suite]["selected_file_count"], 0)
            self.assertFalse(by_suite[suite]["process"]["executed"])

    def test_003_classifier_places_representative_files(self) -> None:
        by_path = {item.path: item for item in pytest_suite_runner.discover_pytest_suite_files(ROOT)}
        self.assertEqual(by_path["tests/test_daily_research_phase1.py"].suite, "slow")
        self.assertEqual(by_path["tests/test_backtest_module.py"].suite, "slow")
        self.assertEqual(by_path["tests/test_frontend_api_contract_governance.py"].suite, "v3_v4_governance")
        self.assertEqual(by_path["tests/test_v370_data_provider_chain_and_quality.py"].suite, "v3_v4_governance")

    def test_004_qd_cli_summary_json(self) -> None:
        proc = subprocess.run(
            [
                sys.executable,
                "scripts/qd.py",
                "maintenance",
                "pytest-suite-runner",
                "--suite",
                "core",
                "--limit",
                "2",
                "--dry-run",
                "true",
                "--json",
                "true",
                "--summary",
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
        self.assertEqual(payload["task_type"], "pytest_suite_runner")
        self.assertEqual(payload["suite"], "core")
        self.assertTrue(payload["dry_run"])
        self.assertEqual(len(payload["suite_runs"]), 1)

    def test_005_direct_script_accepts_v3_v4_alias(self) -> None:
        proc = subprocess.run(
            [
                sys.executable,
                "scripts/run_pytest_suite_runner.py",
                "v3-v4",
                "--limit",
                "1",
                "--dry-run",
                "true",
                "--json",
                "true",
                "--summary",
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
        self.assertEqual(payload["suite"], "v3_v4_governance")
        self.assertEqual(len(payload["suite_runs"]), 1)
        self.assertGreaterEqual(payload["suite_runs"][0]["selected_file_count"], 1)

    def test_006_outputs_are_written(self) -> None:
        payload = pytest_suite_runner.write_pytest_suite_runner_outputs(self.payload)
        self.assertEqual(payload["output_paths"]["json"], "results/maintenance/pytest_suite_runner_latest.json")
        self.assertTrue((ROOT / "results" / "maintenance" / "pytest_suite_runner_latest.json").exists())
        self.assertTrue((ROOT / "reports" / "maintenance" / "pytest_suite_runner_latest.md").exists())

    def test_007_json_has_no_nan_or_infinity(self) -> None:
        _assert_json_safe(self.payload)

    def test_008_runner_is_read_only(self) -> None:
        text = (ROOT / "quantdesk" / "maintenance" / "pytest_suite_runner.py").read_text(encoding="utf-8")
        forbidden = ["git clean", "git restore", "Remove-Item", "shutil.rmtree", ".unlink("]
        for token in forbidden:
            with self.subTest(token=token):
                self.assertNotIn(token, text)

    def test_009_docs_mention_pytest_suite_runner(self) -> None:
        docs = (ROOT / "docs" / "development_test_guide.md").read_text(encoding="utf-8")
        inventory = (ROOT / "docs" / "script_entrypoint_inventory.md").read_text(encoding="utf-8")
        qd = (ROOT / "scripts" / "qd.py").read_text(encoding="utf-8")
        self_check = (ROOT / "scripts" / "self_check.py").read_text(encoding="utf-8")
        self.assertIn("pytest-suite-runner", docs)
        self.assertIn("pytest-suite-runner", inventory)
        self.assertIn("pytest_suite_runner_latest.json", qd)
        self.assertIn("check_pytest_suite_runner_governance", self_check)


if __name__ == "__main__":
    unittest.main()
