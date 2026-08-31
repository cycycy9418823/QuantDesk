from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from quantdesk import daily_data_retention as retention
from quantdesk import reproducibility_manifest as manifest_mod
from quantdesk.version import API_SCHEMA_VERSION, APP_VERSION, PACKAGE_VERSION

PROJECT_ROOT = Path(__file__).resolve().parents[1]


class DailyRetentionAndManifestTests(unittest.TestCase):
    def test_build_retention_plan_defaults_network_false(self) -> None:
        plan = retention.build_retention_plan(symbols="600744", target_date="2026-05-19")
        self.assertFalse(plan["network"])
        self.assertFalse(plan["llm_enabled"])
        self.assertFalse(plan["full_scan"])
        self.assertEqual(plan["symbols"], ["600744"])

    def test_missing_symbols_samples_low_price_included_only_with_limit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            universe = Path(tmp) / "low_price_learning_symbols.csv"
            universe.write_text(
                "symbol,name,included\n600001,A,true\n600002,B,false\n600003,C,yes\n600004,D,true\n",
                encoding="utf-8",
            )
            plan = retention.build_retention_plan(
                symbols="",
                target_date="2026-05-19",
                max_symbols=2,
                low_price_universe_path=universe,
            )
        self.assertEqual(plan["symbol_source"], "low_price_included_sample")
        self.assertEqual(plan["symbols"], ["600001", "600003"])
        self.assertFalse(plan["full_scan"])

    def test_network_false_provider_smoke_receives_network_false(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan = retention.build_retention_plan(
                symbols="600744",
                target_date="2026-05-19",
                network=False,
                output_dir=Path(tmp) / "daily_retention",
                include_strategy_validation=False,
                include_observation_report=False,
            )
            with mock.patch.object(retention, "run_provider_network_smoke", return_value={"summary": {"skipped": 4}, "steps": []}) as smoke:
                snapshot = retention.collect_daily_retention_snapshot(plan)
        self.assertFalse(snapshot["network"])
        smoke.assert_called_once()
        self.assertFalse(smoke.call_args.kwargs["network"])

    def test_daily_retention_writes_manifest_and_summary(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "daily_retention"
            plan = retention.build_retention_plan(
                symbols="600744",
                target_date="2026-05-19",
                output_dir=out,
                include_provider_smoke=False,
                include_strategy_validation=False,
                include_observation_report=False,
            )
            snapshot = retention.collect_daily_retention_snapshot(plan)
            manifest_path = retention.write_retention_manifest(snapshot, Path(tmp) / "daily_retention_manifest.json")
            summary_path = retention.write_retention_summary(snapshot, Path(tmp) / "daily_retention_summary.md")
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            self.assertTrue(manifest_path.exists())
            self.assertTrue(summary_path.exists())
            self.assertEqual(manifest["app_version"], APP_VERSION)
            self.assertEqual(manifest["package_version"], PACKAGE_VERSION)
            self.assertEqual(manifest["api_schema_version"], API_SCHEMA_VERSION)
            self.assertEqual(manifest["target_date"], "2026-05-19")
            self.assertEqual(manifest["symbols"], ["600744"])
            self.assertIn("calendar_quality", manifest)
            self.assertIn("strategy_validation", manifest)

    def test_manifest_does_not_contain_api_key_or_provider_secret_content(self) -> None:
        manifest = manifest_mod.build_reproducibility_manifest(
            target_date="2026-05-19",
            symbols=["600744"],
            commands=["daily-retain", "--api_key=SECRET_VALUE"],
            inputs=["config/llm_config.json", "config/provider_secrets.json"],
            outputs=[],
            warnings=["api_key should redact"],
        )
        text = json.dumps(manifest, ensure_ascii=False)
        self.assertNotIn("SECRET_VALUE", text)
        self.assertNotIn("provider_secret_value", text)
        self.assertIn("[redacted]", text)

    def test_compute_file_hash_is_stable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "sample.txt"
            path.write_text("same content", encoding="utf-8")
            first = manifest_mod.compute_file_hash(path)
            second = manifest_mod.compute_file_hash(path)
        self.assertTrue(first["exists"])
        self.assertEqual(first["sha256"], second["sha256"])

    def test_compare_manifests_detects_version_or_input_difference(self) -> None:
        left = manifest_mod.build_reproducibility_manifest(target_date="2026-05-19", symbols=["600744"], app_version="x") if False else manifest_mod.build_reproducibility_manifest(target_date="2026-05-19", symbols=["600744"])
        right = dict(left)
        right["app_version"] = "different"
        result = manifest_mod.compare_manifests(left, right)
        self.assertFalse(result["ok"])
        self.assertTrue(any(item["field"] == "app_version" for item in result["differences"]))

    def test_qd_help_contains_daily_retain_and_manifest_check(self) -> None:
        text = (PROJECT_ROOT / "scripts" / "qd.py").read_text(encoding="utf-8")
        self.assertIn("daily-retain", text)
        self.assertIn("manifest-check", text)

    def test_manifest_check_cli_is_local_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            manifest = manifest_mod.build_reproducibility_manifest(target_date="2026-05-19", symbols=["600744"])
            path = Path(tmp) / "manifest.json"
            path.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
            completed = subprocess.run(
                [sys.executable, str(PROJECT_ROOT / "scripts" / "check_reproducibility_manifest.py"), "--manifest", str(path), "--strict", "false"],
                cwd=PROJECT_ROOT,
                text=True,
                encoding="utf-8",
                errors="replace",
                capture_output=True,
                check=False,
            )
        self.assertEqual(completed.returncode, 0, completed.stderr + completed.stdout)
        self.assertIn("不联网", completed.stdout)

    def test_clean_zip_rules_exclude_daily_retention_outputs(self) -> None:
        export_text = (PROJECT_ROOT / "export_project.ps1").read_text(encoding="utf-8")
        self.assertIn('"results"', export_text)
        self.assertIn('"reports"', export_text)
        self.assertIn('"data\\calendar"', export_text)

    def test_docs_are_wired(self) -> None:
        """Retention and the reproducibility manifest stay documented.

        Split from the retired app/index.html half: 每日数据留存 and 复现 manifest do
        not exist on the active desk, so those assertions retired rather than being
        replaced by a weaker token. The README and workflow-doc halves are active and
        keep the guarantee.
        """
        readme = (PROJECT_ROOT / "README.md").read_text(encoding="utf-8")
        docs = (PROJECT_ROOT / "docs" / "daily_data_retention_workflow.md").read_text(
            encoding="utf-8"
        )
        self.assertIn("Daily Data Retention And Reproducibility Manifest", readme)
        self.assertIn("daily-retain", docs)


if __name__ == "__main__":
    unittest.main()


