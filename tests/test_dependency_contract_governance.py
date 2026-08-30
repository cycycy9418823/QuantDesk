from __future__ import annotations

import json
import math
import subprocess
import sys
import unittest
from pathlib import Path

from quantdesk.maintenance import dependency_contract

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


class DependencyContractGovernanceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.payload = dependency_contract.build_dependency_contract()

    def test_001_schema(self) -> None:
        payload = self.payload
        self.assertEqual(payload["task_type"], "dependency_contract")
        self.assertIn("groups", payload)
        self.assertIn("runtime", payload["groups"])
        self.assertIn("dev", payload["groups"])
        self.assertIn("data", payload["groups"])
        self.assertIn("optional_data_packages_in_runtime", payload)

    def test_002_existing_dependency_files_are_aligned(self) -> None:
        payload = self.payload
        self.assertEqual(payload["missing_files"], [])
        self.assertEqual(payload["groups"]["runtime"]["status"], "passed")
        self.assertEqual(payload["groups"]["dev"]["status"], "passed")
        self.assertEqual(payload["groups"]["data"]["status"], "passed")
        self.assertEqual(payload["optional_data_packages_in_runtime"], [])
        self.assertEqual(payload["dev_packages_in_runtime"], [])

    def test_003_cli_summary_json(self) -> None:
        proc = subprocess.run(
            [
                sys.executable,
                "scripts/qd.py",
                "maintenance",
                "dependency-contract",
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
        self.assertEqual(payload["task_type"], "dependency_contract")
        self.assertIn("group_statuses", payload)
        self.assertEqual(payload["group_statuses"]["runtime"], "passed")

    def test_004_outputs_are_written(self) -> None:
        payload = dependency_contract.write_dependency_contract_outputs(self.payload)
        self.assertEqual(payload["output_paths"]["json"], "results/maintenance/dependency_contract_latest.json")
        self.assertTrue((ROOT / "results" / "maintenance" / "dependency_contract_latest.json").exists())
        self.assertTrue((ROOT / "reports" / "maintenance" / "dependency_contract_latest.md").exists())

    def test_005_docs_and_self_check_marker_exist(self) -> None:
        docs = (ROOT / "docs" / "script_entrypoint_inventory.md").read_text(encoding="utf-8")
        self.assertIn("dependency-contract", docs)
        self_check = (ROOT / "scripts" / "self_check.py").read_text(encoding="utf-8")
        self.assertIn("check_dependency_contract_governance", self_check)

    def test_006_json_has_no_nan_or_infinity(self) -> None:
        _assert_json_safe(self.payload)

    def test_007_contract_is_static_and_read_only(self) -> None:
        text = (ROOT / "quantdesk" / "maintenance" / "dependency_contract.py").read_text(encoding="utf-8")
        forbidden = ["pip install", "subprocess.run", "urlopen(", "requests.", "git clean", "git restore", "Remove-Item", "shutil.rmtree", ".unlink("]
        for token in forbidden:
            with self.subTest(token=token):
                self.assertNotIn(token, text)


if __name__ == "__main__":
    unittest.main()
