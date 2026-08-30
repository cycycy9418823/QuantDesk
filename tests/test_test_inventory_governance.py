from __future__ import annotations

import json
import math
import re
import subprocess
import sys
import unittest
from pathlib import Path

from quantdesk.maintenance import test_inventory

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


class TestInventoryGovernanceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.payload = test_inventory.build_test_inventory()

    def test_001_inventory_schema(self) -> None:
        payload = self.payload
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["task_type"], "test_inventory")
        self.assertGreater(payload["test_file_count"], 100)
        self.assertGreater(payload["test_method_count"], 100)
        self.assertIn("counts_by_tier", payload)
        self.assertIn("counts_by_category", payload)
        self.assertIn("records", payload)
        self.assertIn("tier_runner_commands", payload)
        self.assertIn("large_file_split_candidates", payload)
        self.assertIn("counts_by_runner", payload)

    def test_002_tiers_are_present(self) -> None:
        counts = self.payload["counts_by_tier"]
        self.assertIn("tier0_fast_unit", counts)
        self.assertIn("tier2_cli_or_filesystem", counts)
        self.assertIn("tier3_release_or_long", counts)
        self.assertIn("legacy_versioned_acceptance", counts)

    def test_002b_versioned_acceptance_tests_are_legacy_tier(self) -> None:
        records = self.payload["records"]
        versioned = [record for record in records if re.match(r"test_v\d", record["name"])]
        self.assertGreater(len(versioned), 100)
        self.assertTrue(all(record["tier"] == "legacy_versioned_acceptance" for record in versioned))

    def test_003_cli_summary_json(self) -> None:
        proc = subprocess.run(
            [sys.executable, "scripts/qd.py", "maintenance", "test-inventory", "--json", "true", "--summary", "true"],
            cwd=ROOT,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=120,
            check=True,
        )
        payload = json.loads(proc.stdout)
        self.assertEqual(payload["task_type"], "test_inventory")
        self.assertIn("counts_by_tier", payload)
        self.assertIn("tier_runner_commands", payload)
        self.assertIn("counts_by_runner", payload)

    def test_004_outputs_are_written(self) -> None:
        payload = test_inventory.write_test_inventory_outputs()
        self.assertEqual(payload["output_paths"]["json"], "results/maintenance/test_inventory_latest.json")
        self.assertTrue((ROOT / "results" / "maintenance" / "test_inventory_latest.json").exists())
        self.assertTrue((ROOT / "reports" / "maintenance" / "test_inventory_latest.md").exists())

    def test_005_docs_and_self_check_marker_exist(self) -> None:
        docs = (ROOT / "docs" / "script_entrypoint_inventory.md").read_text(encoding="utf-8")
        self.assertIn("test-inventory", docs)
        self_check = (ROOT / "scripts" / "self_check.py").read_text(encoding="utf-8")
        self.assertIn("check_test_inventory_governance", self_check)

    def test_006_json_has_no_nan_or_infinity(self) -> None:
        _assert_json_safe(self.payload)

    def test_007_inventory_is_read_only(self) -> None:
        text = (ROOT / "quantdesk" / "maintenance" / "test_inventory.py").read_text(encoding="utf-8")
        forbidden = ["git clean", "git restore", "Remove-Item", "shutil.rmtree", ".unlink("]
        for token in forbidden:
            with self.subTest(token=token):
                self.assertNotIn(token, text)


if __name__ == "__main__":
    unittest.main()
