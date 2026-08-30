from __future__ import annotations

import json
import math
import subprocess
import sys
import unittest
from pathlib import Path

from quantdesk.maintenance import script_inventory

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


class ScriptInventoryGovernanceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.payload = script_inventory.build_script_inventory()

    def test_001_inventory_schema(self) -> None:
        payload = self.payload
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["task_type"], "script_inventory")
        self.assertGreater(payload["script_count"], 100)
        self.assertGreater(payload["qd_command_count"], 20)
        self.assertIn("counts_by_category", payload)
        self.assertIn("risk_sections", payload)
        self.assertIn("reference_summary", payload)
        self.assertIn("dependency_inventory", payload)
        self.assertIn("documentation_inventory", payload)

    def test_002_qd_targets_exist(self) -> None:
        payload = self.payload
        self.assertEqual(payload["missing_qd_target_scripts"], [])

    def test_003_versioned_raw_scripts_are_flagged(self) -> None:
        payload = self.payload
        self.assertGreater(payload["versioned_raw_script_count"], 0)
        names = {item["name"] for item in payload["records"]}
        self.assertTrue(any(name.startswith("run_v") for name in names))
        self.assertIn("versioned_raw_scripts_present", payload["warnings"])

    def test_004_reference_audit_splits_raw_scripts(self) -> None:
        payload = self.payload
        self.assertIn("archive_review_candidate_count", payload)
        self.assertIn("referenced_raw_script_count", payload)
        self.assertIn("counts_by_reference_group", payload)
        self.assertEqual(
            payload["reference_summary"]["archive_review_candidate_count"],
            payload["archive_review_candidate_count"],
        )
        self.assertGreater(payload["reference_summary"]["source_file_count"], 100)

    def test_005_outputs_are_written(self) -> None:
        payload = script_inventory.write_script_inventory_outputs()
        self.assertTrue((ROOT / "results" / "maintenance" / "script_inventory_latest.json").exists())
        self.assertTrue((ROOT / "reports" / "maintenance" / "script_inventory_latest.md").exists())
        self.assertEqual(payload["output_paths"]["json"], "results/maintenance/script_inventory_latest.json")

    def test_006_cli_summary_json(self) -> None:
        proc = subprocess.run(
            [sys.executable, "scripts/qd.py", "maintenance", "script-inventory", "--json", "true", "--summary", "true"],
            cwd=ROOT,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=120,
            check=True,
        )
        payload = json.loads(proc.stdout)
        self.assertEqual(payload["task_type"], "script_inventory")
        self.assertGreater(payload["versioned_raw_script_count"], 0)
        self.assertIn("reference_summary", payload)
        self.assertIn("risk_sections", payload)

    def test_007_json_has_no_nan_or_infinity(self) -> None:
        _assert_json_safe(self.payload)

    def test_008_docs_and_self_check_marker_exist(self) -> None:
        docs = (ROOT / "docs" / "script_entrypoint_inventory.md").read_text(encoding="utf-8")
        self.assertIn("script-inventory", docs)
        self.assertIn("Script governance", docs)
        self.assertIn("archive review candidate", docs)
        self_check = (ROOT / "scripts" / "self_check.py").read_text(encoding="utf-8")
        self.assertIn("check_script_inventory_governance", self_check)

    def test_009_inventory_is_read_only(self) -> None:
        text = (ROOT / "quantdesk" / "maintenance" / "script_inventory.py").read_text(encoding="utf-8")
        forbidden = ["git clean", "git restore", "Remove-Item", "shutil.rmtree", ".unlink("]
        for token in forbidden:
            with self.subTest(token=token):
                self.assertNotIn(token, text)

    def test_010_frontend_coupling_points_to_active_daily_research_assets(self) -> None:
        sections = {
            item["risk"]: item for item in self.payload["risk_sections"]
        }
        coupling = sections["frontend_backend_coupling"]["evidence"]
        self.assertEqual(coupling["active_template"], "app/templates/daily_research.html")
        self.assertEqual(coupling["active_bundle"], "app/static/js/daily_research.js")
        self.assertNotIn("app/static/app.js", json.dumps(coupling, ensure_ascii=False))


if __name__ == "__main__":
    unittest.main()
