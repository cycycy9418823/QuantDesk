from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

try:
    from tests._release_scope import release_packaging_deferred
except ModuleNotFoundError:  # pragma: no cover - unittest discover top-level mode
    from _release_scope import release_packaging_deferred

PROJECT_ROOT = Path(__file__).resolve().parents[1]


class BattleReadyPortableReleaseTests(unittest.TestCase):
    def test_onboarding_config_generation(self) -> None:
        from quantdesk.onboarding import run_first_run_setup

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            payload = run_first_run_setup(
                config_path=root / "config.yaml",
                data_dir=root / "data",
                install_sample_data=True,
                network_enabled=False,
                profile="both",
                init_live_account=False,
                run_self_check=False,
            )
            self.assertTrue(payload["ok"])
            self.assertEqual(payload["task_type"], "first_run_wizard")
            self.assertFalse(payload["network"])
            text = (root / "config.yaml").read_text(encoding="utf-8")
            self.assertIn("network_enabled: false", text)
            self.assertIn("no_broker: true", text)
            self.assertIn("no_real_orders: true", text)

    def test_first_run_wizard_cli_json_summary(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            completed = subprocess.run(
                [
                    sys.executable,
                    "scripts/first_run_wizard.py",
                    "--config-path",
                    str(root / "config.yaml"),
                    "--data-dir",
                    str(root / "data"),
                    "--init-live-account",
                    "false",
                    "--run-self-check",
                    "false",
                    "--json",
                    "true",
                    "--summary",
                    "true",
                ],
                cwd=PROJECT_ROOT,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            payload = json.loads(completed.stdout)
            self.assertEqual(payload["task_type"], "first_run_wizard")
            self.assertFalse(payload["network"])
            self.assertEqual(payload["self_check_status"], "skipped")

    def test_sample_data_manifest_and_builder(self) -> None:
        manifest_path = PROJECT_ROOT / "assets" / "sample_data_manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        self.assertTrue(manifest["synthetic_sample_data"])
        self.assertFalse(manifest["network"])
        self.assertEqual(len(manifest["symbols"]), 30)
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "sample.zip"
            completed = subprocess.run(
                [sys.executable, "scripts/build_sample_data_package.py", "--output", str(output), "--json", "true"],
                cwd=PROJECT_ROOT,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            payload = json.loads(completed.stdout)
            self.assertTrue(payload["synthetic_sample_data"])
            self.assertTrue(output.exists())

    def test_build_and_release_docs_exist(self) -> None:
        required = [
            "build/QuantDesk.spec",
            "docs/video_scripts.md",
            "docs/videos/README.md",
            "docs/screenshots/README.md",
        ]
        if not release_packaging_deferred(PROJECT_ROOT):
            required.extend(
                [
                    "build/quantdesk_installer.nsi",
                    "build/build_portable_release.ps1",
                    "build/build_installer.ps1",
                ]
            )
        for relative in required:
            self.assertTrue((PROJECT_ROOT / relative).exists(), relative)
        # Release notes may live in the project root or be archived under
        # docs/history/ after repository slimming; accept either location.
        notes_path = PROJECT_ROOT / "RELEASE_NOTES_v3.5.0.md"
        if not notes_path.exists():
            notes_path = PROJECT_ROOT / "docs" / "history" / "RELEASE_NOTES_v3.5.0.md"
        self.assertTrue(notes_path.exists(), "RELEASE_NOTES_v3.5.0.md")
        notes = notes_path.read_text(encoding="utf-8")
        self.assertIn("does not connect to brokers", notes)
        self.assertIn("does not place real orders", notes)
        self.assertIn("PIT filtering is off by default", notes)

    def test_qd_first_run_and_wizard_help(self) -> None:
        completed = subprocess.run(
            [sys.executable, "scripts/qd.py", "--help"],
            cwd=PROJECT_ROOT,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("first-run", completed.stdout)
        self.assertIn("wizard", completed.stdout)

    def test_release_proof_lists_v350_files(self) -> None:
        verify_text = (PROJECT_ROOT / "scripts" / "verify_clean_zip.py").read_text(encoding="utf-8")
        export_text = (PROJECT_ROOT / "export_project.ps1").read_text(encoding="utf-8")
        for token in [
            "quantdesk/onboarding.py",
            "scripts/first_run_wizard.py",
            "build/QuantDesk.spec",
            "assets/sample_data_manifest.json",
            "tests/test_v350_battle_ready_portable_release.py",
        ]:
            self.assertIn(token, verify_text)
            self.assertIn(token, export_text)
        if not release_packaging_deferred(PROJECT_ROOT):
            self.assertIn("build/quantdesk_installer.nsi", verify_text)
            self.assertIn("build/quantdesk_installer.nsi", export_text)

    def test_safety_scan_strings_are_disclaimers_not_execution_paths(self) -> None:
        sensitive_files = [
            PROJECT_ROOT / "quantdesk" / "onboarding.py",
            PROJECT_ROOT / "scripts" / "first_run_wizard.py",
        ]
        installer = PROJECT_ROOT / "build" / "quantdesk_installer.nsi"
        if installer.exists():
            sensitive_files.append(installer)
        combined = "\n".join(path.read_text(encoding="utf-8", errors="ignore") for path in sensitive_files)
        self.assertNotIn("submit_order(", combined)
        self.assertNotIn("place_order(", combined)
        self.assertNotIn("api_key", combined.lower())
        self.assertNotIn("secret_key", combined.lower())

    def test_version(self) -> None:
        from quantdesk.version import API_SCHEMA_VERSION, APP_VERSION, PACKAGE_VERSION

        self.assertIn(PACKAGE_VERSION, {"3.6.0", "3.6.1", "3.6.2", "3.7.0", "3.7.0.1", "3.7.0.2", "3.7.0.3", "3.7.0.4", "3.7.0.5", "3.7.0.6"})
        self.assertIn(APP_VERSION, {"3.6.0-real-use-quant-platform-completion", "3.6.1-real-risk-alerts-and-smart-stop-integration", "3.6.2-real-similar-day-stress-and-style-attribution", "3.7.0-data-backbone", "3.7.0.1-strategy-viability-on-data-backbone", "3.7.0.2-strategy-pipeline-repair", "3.7.0.3-low-price-pipeline-finalization", "3.7.0.4-capital-lot-sensitivity-and-git-hygiene", "3.7.0.5-git-deletion-reconciliation", "3.7.0.6-strategy-regression-guard-and-report-hygiene"})
        self.assertIn(API_SCHEMA_VERSION, {"3.6.0", "3.6.1", "3.6.2", "3.7.0", "3.7.0.1", "3.7.0.2", "3.7.0.3", "3.7.0.4", "3.7.0.5", "3.7.0.6"})


if __name__ == "__main__":
    unittest.main()



