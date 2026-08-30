from __future__ import annotations

import json
import math
import subprocess
import sys
import unittest
from pathlib import Path

from quantdesk.maintenance import coverage_contract

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


class CoverageContractGovernanceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.payload = coverage_contract.build_coverage_contract(dry_run=True)

    def test_001_schema(self) -> None:
        payload = self.payload
        self.assertEqual(payload["task_type"], "coverage_contract")
        self.assertIn("command", payload)
        self.assertIn("coverage", payload)
        self.assertIn("coverage_xml", payload)
        self.assertIn("pytest_cov_available", payload)
        self.assertIn("require_pytest_cov", payload)
        self.assertTrue(payload["dry_run"])

    def test_002_command_uses_pytest_cov_and_bounded_tests(self) -> None:
        command = " ".join(self.payload["command"])
        self.assertIn("--cov=quantdesk/maintenance", command)
        self.assertIn("--cov-report=xml:reports/coverage/coverage.xml", command)
        self.assertIn("tests/test_frontend_api_contract_governance.py", command)
        self.assertIn("tests/test_dependency_contract_governance.py", command)

    def test_003_cli_summary_json_dry_run(self) -> None:
        proc = subprocess.run(
            [
                sys.executable,
                "scripts/qd.py",
                "maintenance",
                "coverage-contract",
                "--json",
                "true",
                "--summary",
                "true",
                "--dry-run",
                "true",
                "--require-pytest-cov",
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
        self.assertEqual(payload["task_type"], "coverage_contract")
        self.assertTrue(payload["dry_run"])
        self.assertTrue(payload["require_pytest_cov"])
        self.assertIn("line_percent", payload)

    def test_004_outputs_are_written(self) -> None:
        payload = coverage_contract.write_coverage_contract_outputs(self.payload)
        self.assertEqual(payload["output_paths"]["json"], "results/maintenance/coverage_contract_latest.json")
        self.assertTrue((ROOT / "results" / "maintenance" / "coverage_contract_latest.json").exists())
        self.assertTrue((ROOT / "reports" / "maintenance" / "coverage_contract_latest.md").exists())

    def test_005_docs_and_self_check_marker_exist(self) -> None:
        docs = (ROOT / "docs" / "script_entrypoint_inventory.md").read_text(encoding="utf-8")
        self.assertIn("coverage-contract", docs)
        self_check = (ROOT / "scripts" / "self_check.py").read_text(encoding="utf-8")
        self.assertIn("check_coverage_contract_governance", self_check)

    def test_006_json_has_no_nan_or_infinity(self) -> None:
        _assert_json_safe(self.payload)

    def test_007_contract_is_static_until_command_runs(self) -> None:
        text = (ROOT / "quantdesk" / "maintenance" / "coverage_contract.py").read_text(encoding="utf-8")
        forbidden = ["enable-network", "--network true", "git push", "urlopen(", "requests."]
        for token in forbidden:
            with self.subTest(token=token):
                self.assertNotIn(token, text.lower())


if __name__ == "__main__":
    unittest.main()
