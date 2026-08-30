from __future__ import annotations

import importlib
import json
import math
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

from quantdesk.maintenance import git_deletion_audit
from quantdesk.maintenance import import_alias_reconciliation as alias_recon
from quantdesk.version import API_SCHEMA_VERSION, APP_VERSION, PACKAGE_VERSION

try:
    from tests._operation_log_isolation import isolate_operation_log_dir
except ModuleNotFoundError:  # pragma: no cover - unittest discovery adds tests/ directly
    from _operation_log_isolation import isolate_operation_log_dir

isolate_operation_log_dir()

ROOT = Path(__file__).resolve().parents[1]
REPRESENTATIVE_MODULES = [
    "paper_trading",
    "paper_review",
    "strategy_trade_replay",
    "conditional_trade_plan",
    "intraday_providers",
    "intraday_data_workbench",
    "agent_policy_guard",
    "agent_evidence_pack",
    "backtest_statistical_validation",
    "feature_quality_audit",
    "parameter_sensitivity_audit",
]


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


class V3705GitDeletionReconciliationTests(unittest.TestCase):
    def test_001_version(self) -> None:
        self.assertIn(PACKAGE_VERSION, {"3.7.0.5", "3.7.0.6"})
        self.assertIn(APP_VERSION, {"3.7.0.5-git-deletion-reconciliation", "3.7.0.6-strategy-regression-guard-and-report-hygiene"})
        self.assertIn(API_SCHEMA_VERSION, {"3.7.0.5", "3.7.0.6"})

    def test_002_moved_modules_exists(self) -> None:
        import quantdesk

        moved = getattr(quantdesk, "_MOVED_MODULES", {})
        self.assertIsInstance(moved, dict)
        self.assertGreater(len(moved), 0)

    def test_003_representative_deleted_alias_targets_exist(self) -> None:
        import quantdesk

        moved = getattr(quantdesk, "_MOVED_MODULES", {})
        for name in REPRESENTATIVE_MODULES:
            with self.subTest(name=name):
                self.assertIn(name, moved)
                importlib.import_module(moved[name])

    def test_004_import_quantdesk_old_aliases(self) -> None:
        for name in REPRESENTATIVE_MODULES:
            with self.subTest(name=name):
                module = importlib.import_module(f"quantdesk.{name}")
                self.assertTrue(module.__name__.startswith("quantdesk."))

    def test_005_from_quantdesk_old_aliases(self) -> None:
        package = importlib.import_module("quantdesk")
        for name in REPRESENTATIVE_MODULES:
            with self.subTest(name=name):
                self.assertIs(getattr(package, name), importlib.import_module(f"quantdesk.{name}"))

    def test_006_current_status_scope_is_deleted_only(self) -> None:
        payload = alias_recon.get_current_git_deletion_status()
        self.assertIn("deleted_tracked_files", payload)
        self.assertEqual(payload["deleted_tracked_count"], len(payload["deleted_tracked_files"]))
        for excluded in payload["excluded_non_deleted_migration_candidates"]:
            self.assertNotIn(excluded, payload["deleted_module_names"])

    def test_007_alias_reconciliation_all_current_deleted_covered(self) -> None:
        payload = alias_recon.check_alias_coverage()
        self.assertEqual(payload["alias_import_fail_count"], 0)
        self.assertEqual(payload["from_import_fail_count"], 0)
        self.assertEqual(payload["failed_aliases"], [])
        self.assertEqual(payload["uncovered_deleted_modules"], [])
        self.assertIn(
            payload["recommendation"],
            {
                "all_deleted_modules_shim_covered",
                "all_deleted_modules_resolved_or_retired",
                "all_deleted_modules_retired_or_unreferenced",
            },
        )

    def test_008_scan_only_excludes_shim_definition(self) -> None:
        scan = alias_recon.scan_old_import_references()
        for item in scan["old_import_references"]:
            self.assertNotEqual(item["file"], "quantdesk/__init__.py")
        self.assertEqual(scan["rewrite_candidates"], [])

    def test_009_rewrite_report_has_remaining_reason(self) -> None:
        payload = alias_recon.write_rewrite_outputs(alias_recon.run_import_rewrite_dry_run())
        self.assertIn("remaining_reasons", payload)
        self.assertIn("old_import_references_before", payload)
        self.assertGreaterEqual(payload["old_import_references_before"], payload["old_import_references_after"])

    def test_010_git_audit_shim_covered_not_high_risk(self) -> None:
        payload = git_deletion_audit.run_git_deletion_audit()
        self.assertEqual(payload["high_risk_deleted_files"], [])
        self.assertEqual(payload["restore_required_files"], [])
        # Coverage-completeness check (not a count check): once the legacy
        # root-module migration is fully committed, the working tree may have
        # zero deleted Python modules, which is the healthy end state. What
        # matters is that any deleted module that DOES appear is shim-covered or
        # retired -- never high-risk, restore-required, or a broken alias.
        self.assertEqual(payload.get("broken_alias_deleted_files") or [], [])
        deleted_count = int(payload.get("deleted_tracked_count") or 0)
        if deleted_count and payload.get("deleted_file_details"):
            # Some Python modules were deleted in this working tree; they must be
            # accounted for (shim-covered or retired), not left unclassified.
            self.assertEqual(int(payload.get("unknown_risk_count") or 0), 0)

    def test_011_git_audit_classification_fields(self) -> None:
        payload = git_deletion_audit.run_git_deletion_audit()
        self.assertIn("docs_only_references", payload)
        self.assertIn("runtime_import_references", payload)
        self.assertIn("shim_definition_references", payload)
        self.assertIn(
            payload["recommendation"],
            {
                "safe_to_continue_after_alias_reconciliation",
                "manual_review_required",
            },
        )

    def test_012_broken_alias_is_high_risk(self) -> None:
        with mock.patch.object(alias_recon, "_load_moved_modules", return_value={"paper_trading": "quantdesk.missing_target"}):
            payload = alias_recon.check_alias_coverage(["paper_trading"])
        self.assertTrue(payload["failed_aliases"])
        self.assertEqual(payload["recommendation"], "fix_broken_aliases_only")

    def test_013_audit_does_not_restore_or_clean(self) -> None:
        text = (ROOT / "quantdesk" / "maintenance" / "git_deletion_audit.py").read_text(encoding="utf-8")
        alias_text = (ROOT / "quantdesk" / "maintenance" / "import_alias_reconciliation.py").read_text(encoding="utf-8")
        combined = text + alias_text
        self.assertNotIn('["git", "restore"', combined)
        self.assertNotIn('["git", "clean"', combined)

    def test_014_docs_only_reference_not_runtime_high_risk(self) -> None:
        payload = git_deletion_audit.run_git_deletion_audit()
        self.assertEqual(payload["high_risk_deleted_files"], [])
        self.assertIsInstance(payload["docs_only_references"], list)

    def test_015_cli_import_alias_reconciliation_json(self) -> None:
        cp = subprocess.run(
            [sys.executable, "scripts/qd.py", "maintenance", "import-alias-reconciliation", "--json", "true", "--summary", "true"],
            cwd=ROOT,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=True,
        )
        payload = json.loads(cp.stdout)
        self.assertEqual(payload["task_type"], "import_alias_reconciliation")
        self.assertEqual(payload["alias_import_fail_count"], 0)

    def test_016_cli_import_alias_scan_only_json(self) -> None:
        cp = subprocess.run(
            [sys.executable, "scripts/qd.py", "maintenance", "import-alias-reconciliation", "--scan-only", "true", "--json", "true", "--summary", "true"],
            cwd=ROOT,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=True,
        )
        payload = json.loads(cp.stdout)
        self.assertEqual(payload["rewrite_candidate_count"], 0)
        self.assertEqual(payload["uncovered_deleted_module_count"], 0)

    def test_017_cli_git_deletion_audit_json(self) -> None:
        cp = subprocess.run(
            [sys.executable, "scripts/qd.py", "maintenance", "git-deletion-audit", "--json", "true", "--summary", "true"],
            cwd=ROOT,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=True,
        )
        payload = json.loads(cp.stdout)
        self.assertEqual(payload["task_type"], "git_deletion_audit")
        self.assertEqual(payload["high_risk_deleted_files"], [])
        self.assertEqual(payload["manual_review_required"], False)

    def test_018_json_has_no_nan_or_infinity(self) -> None:
        payload = alias_recon.run_import_alias_reconciliation(scan_only=True)
        _assert_json_safe(payload)

    def test_019_reports_are_written(self) -> None:
        alias_recon.run_import_alias_reconciliation(scan_only=True)
        self.assertTrue((ROOT / "results" / "git_deletion_current_status_latest.json").exists())
        self.assertTrue((ROOT / "reports" / "git_deletion_current_status_latest.md").exists())
        self.assertTrue((ROOT / "results" / "import_rewrite_latest.json").exists())
        self.assertTrue((ROOT / "reports" / "import_rewrite_latest.md").exists())

    def test_020_self_check_marker_present(self) -> None:
        text = (ROOT / "scripts" / "self_check.py").read_text(encoding="utf-8")
        self.assertIn("check_v3705_git_deletion_reconciliation", text)

    def test_021_verify_clean_zip_knows_v3705(self) -> None:
        text = (ROOT / "scripts" / "verify_clean_zip.py").read_text(encoding="utf-8")
        self.assertIn("3.7.0.5", text)
        self.assertIn("docs/import_alias_reconciliation.md", text)

    def test_022_docs_exist(self) -> None:
        # Release notes may live in the project root or be archived under
        # docs/history/ after repository slimming; accept either location.
        self.assertTrue(
            (ROOT / "RELEASE_NOTES_v3.7.0.5.md").exists()
            or (ROOT / "docs" / "history" / "RELEASE_NOTES_v3.7.0.5.md").exists()
        )
        self.assertTrue((ROOT / "docs" / "import_alias_reconciliation.md").exists())

    def test_023_self_check_fail_zero(self) -> None:
        cp = subprocess.run([sys.executable, "scripts/self_check.py"], cwd=ROOT, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True)
        self.assertIn('"fail": 0', cp.stdout)


if __name__ == "__main__":
    unittest.main()
