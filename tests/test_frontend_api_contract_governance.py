from __future__ import annotations

import json
import math
import subprocess
import sys
import unittest
from pathlib import Path

from quantdesk.maintenance import frontend_api_contract

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


class FrontendApiContractGovernanceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.payload = frontend_api_contract.build_frontend_api_contract()

    def test_001_schema(self) -> None:
        payload = self.payload
        self.assertEqual(payload["task_type"], "frontend_api_contract")
        self.assertGreater(payload["frontend_endpoint_count"], 0)
        self.assertGreaterEqual(payload["frontend_source_count"], 2)
        self.assertGreaterEqual(payload["server_source_count"], 1)
        self.assertGreater(payload["server_exact_route_count"], 0)
        self.assertIn("frontend_sources", payload)
        self.assertIn("server_sources", payload)
        self.assertIn("frontend_missing_on_server", payload)
        self.assertIn("frontend_method_gaps", payload)
        self.assertIn("frontend_dynamic_api_reviews", payload)
        self.assertIn("server_unreferenced_by_frontend", payload)
        self.assertIn("server_unreferenced_route_reviews", payload)
        self.assertIn("server_unreferenced_usage_class_counts", payload)
        self.assertIn("notes", payload)

    def test_002_known_routes_are_detected(self) -> None:
        frontend_paths = set(self.payload["frontend_endpoint_methods"])
        server_paths = set(self.payload["server_endpoint_methods"])
        frontend_sources = set(self.payload["frontend_sources"])
        server_sources = set(self.payload["server_sources"])
        self.assertIn("/api/stock/${encodeURIComponent(candidate.symbol)}/detail${suffix}", frontend_paths)
        self.assertIn("/api/startup/update-status", frontend_paths)
        self.assertIn("/api/daily/overview", frontend_paths)
        self.assertIn("/api/daily/candidates", frontend_paths)
        self.assertIn("/api/stock-detail", server_paths)
        self.assertIn("/api/daily/quant-summary", server_paths)
        self.assertIn("/api/startup/update-status", server_paths)
        self.assertIn("/api/daily/overview", server_paths)
        self.assertIn("app/static/js/daily_research.js", frontend_sources)
        self.assertIn("app/templates/daily_research.html", frontend_sources)
        self.assertIn("scripts/quant_api_server.py", server_sources)

    def test_003_cli_summary_json(self) -> None:
        proc = subprocess.run(
            [
                sys.executable,
                "scripts/qd.py",
                "maintenance",
                "frontend-api-contract",
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
        self.assertEqual(payload["task_type"], "frontend_api_contract")
        self.assertIn("frontend_source_count", payload)
        self.assertIn("server_source_count", payload)
        self.assertIn("frontend_missing_on_server_count", payload)
        self.assertIn("frontend_dynamic_api_action_required_count", payload)
        self.assertIn("server_unreferenced_by_frontend_count", payload)
        self.assertIn("server_unreferenced_action_required_count", payload)

    def test_004_outputs_are_written(self) -> None:
        payload = frontend_api_contract.write_frontend_api_contract_outputs(self.payload)
        self.assertEqual(payload["output_paths"]["json"], "results/maintenance/frontend_api_contract_latest.json")
        self.assertTrue((ROOT / "results" / "maintenance" / "frontend_api_contract_latest.json").exists())
        self.assertTrue((ROOT / "reports" / "maintenance" / "frontend_api_contract_latest.md").exists())

    def test_005_docs_and_self_check_marker_exist(self) -> None:
        docs = (ROOT / "docs" / "script_entrypoint_inventory.md").read_text(encoding="utf-8")
        self.assertIn("frontend-api-contract", docs)
        self_check = (ROOT / "scripts" / "self_check.py").read_text(encoding="utf-8")
        self.assertIn("check_frontend_api_contract_governance", self_check)

    def test_006_json_has_no_nan_or_infinity(self) -> None:
        _assert_json_safe(self.payload)

    def test_007_contract_is_static_and_read_only(self) -> None:
        text = (ROOT / "quantdesk" / "maintenance" / "frontend_api_contract.py").read_text(encoding="utf-8")
        forbidden = ["git clean", "git restore", "Remove-Item", "shutil.rmtree", ".unlink(", "urlopen(", "requests."]
        for token in forbidden:
            with self.subTest(token=token):
                self.assertNotIn(token, text)

    def test_008_unreferenced_routes_are_classified_not_flat_warnings(self) -> None:
        reviews = {item["path"]: item for item in self.payload["server_unreferenced_route_reviews"]}
        self.assertIn("/api/daily/quant-summary", reviews)
        self.assertEqual(reviews["/api/daily/quant-summary"]["usage_class"], "daily_research_summary_read_endpoint")
        self.assertFalse(reviews["/api/daily/quant-summary"]["action_required"])
        self.assertIn("/api/stock-detail", reviews)
        self.assertEqual(reviews["/api/stock-detail"]["usage_class"], "compatibility_alias")
        self.assertFalse(reviews["/api/stock-detail"]["action_required"])
        self.assertIn("/api/theme-map/generate-import-draft", reviews)
        self.assertEqual(reviews["/api/theme-map/generate-import-draft"]["usage_class"], "compatibility_alias")
        self.assertFalse(reviews["/api/theme-map/generate-import-draft"]["action_required"])
        self.assertIn("/api/theme-map/update", reviews)
        self.assertEqual(reviews["/api/theme-map/update"]["usage_class"], "compatibility_alias")
        self.assertFalse(reviews["/api/theme-map/update"]["action_required"])
        self.assertIn("/api/paper-simulation/equity-history", reviews)
        paper_history = reviews["/api/paper-simulation/equity-history"]
        self.assertEqual(paper_history["usage_class"], "decision_board_component_read_endpoint")
        self.assertEqual(paper_history["recommendation"], "keep_as_direct_paper_history_diagnostic")
        self.assertFalse(paper_history["action_required"])
        self.assertGreater(self.payload["server_unreferenced_by_frontend_count"], 0)
        self.assertEqual(self.payload["server_unreferenced_action_required_count"], 0)
        self.assertEqual(self.payload["status"], "passed")
        self.assertNotIn("server_unreferenced_routes_need_ui_or_archive_review", self.payload["warnings"])
        self.assertIn("server_routes_not_referenced_by_frontend_classified", self.payload["notes"])

    def test_009_dynamic_daily_research_detail_route_is_covered_by_prefix(self) -> None:
        reviews = self.payload["frontend_dynamic_api_reviews"]
        self.assertGreater(len(reviews), 0)
        detail = [item for item in reviews if "/api/stock/${" in item["raw_url"]]
        self.assertTrue(detail)
        self.assertEqual(detail[0]["coverage_status"], "covered_by_server_prefix")
        self.assertFalse(detail[0]["action_required"])
        self.assertEqual(self.payload["frontend_dynamic_api_action_required_count"], 0)

    def test_010_unknown_unreferenced_actions_still_require_review(self) -> None:
        reviews = frontend_api_contract.review_unreferenced_server_routes(
            [
                {
                    "path": "/api/new-unreviewed-action",
                    "method": "POST",
                    "route_type": "exact",
                    "source": "scripts/example.py",
                    "line": 10,
                    "pattern": "/api/new-unreviewed-action",
                },
                {
                    "path": "/api/new-unreviewed-read",
                    "method": "GET",
                    "route_type": "exact",
                    "source": "scripts/example.py",
                    "line": 11,
                    "pattern": "/api/new-unreviewed-read",
                },
            ]
        )
        self.assertTrue(all(item["action_required"] for item in reviews))


if __name__ == "__main__":
    unittest.main()
