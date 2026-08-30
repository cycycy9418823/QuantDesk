from __future__ import annotations

import json
import math
import subprocess
import sys
import unittest
from pathlib import Path

from quantdesk.maintenance import data_backtest_contract

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


class DataBacktestContractGovernanceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.payload = data_backtest_contract.build_data_backtest_contract()

    def test_001_schema(self) -> None:
        payload = self.payload
        self.assertEqual(payload["task_type"], "data_backtest_contract")
        self.assertIn("backtest", payload)
        self.assertIn("retention_manifest", payload)
        self.assertIn("recommendations", payload)

    def test_002_backtest_contract_fields_are_audited(self) -> None:
        backtest = self.payload["backtest"]
        self.assertIn("missing_assumption_fields", backtest)
        self.assertIn("missing_assumption_metadata_fields", backtest)
        self.assertIn("artifact_records", backtest)
        if backtest["exists"]:
            self.assertIn("commission_rate", backtest["cost_fields"])
            self.assertIn("survivorship_bias_warning", backtest["risk_metadata"])

    def test_003_cli_summary_json(self) -> None:
        proc = subprocess.run(
            [
                sys.executable,
                "scripts/qd.py",
                "maintenance",
                "data-backtest-contract",
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
        self.assertEqual(payload["task_type"], "data_backtest_contract")
        self.assertIn("backtest_status", payload)
        self.assertIn("manifest_status", payload)

    def test_004_outputs_are_written(self) -> None:
        payload = data_backtest_contract.write_data_backtest_contract_outputs(self.payload)
        self.assertEqual(payload["output_paths"]["json"], "results/maintenance/data_backtest_contract_latest.json")
        self.assertTrue((ROOT / "results" / "maintenance" / "data_backtest_contract_latest.json").exists())
        self.assertTrue((ROOT / "reports" / "maintenance" / "data_backtest_contract_latest.md").exists())

    def test_005_docs_and_self_check_marker_exist(self) -> None:
        docs = (ROOT / "docs" / "script_entrypoint_inventory.md").read_text(encoding="utf-8")
        self.assertIn("data-backtest-contract", docs)
        self_check = (ROOT / "scripts" / "self_check.py").read_text(encoding="utf-8")
        self.assertIn("check_data_backtest_contract_governance", self_check)

    def test_006_json_has_no_nan_or_infinity(self) -> None:
        _assert_json_safe(self.payload)

    def test_007_contract_is_static_and_read_only(self) -> None:
        text = (ROOT / "quantdesk" / "maintenance" / "data_backtest_contract.py").read_text(encoding="utf-8")
        forbidden = ["simulate_backtest", "run_backtest", "data-update", "urlopen(", "requests.", "git clean", "git restore", "Remove-Item", "shutil.rmtree", ".unlink("]
        for token in forbidden:
            with self.subTest(token=token):
                self.assertNotIn(token, text)


if __name__ == "__main__":
    unittest.main()
