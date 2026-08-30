from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parents[1]


class EngineeringDocsPackageGroupingLiteTests(unittest.TestCase):
    def test_backtest_loaders_import_and_legacy_reexport(self) -> None:
        from quantdesk import backtest
        from quantdesk.backtest_loaders import benchmark_total_return, load_benchmark_series, load_industry_map

        self.assertIs(backtest.benchmark_total_return, benchmark_total_return)
        self.assertIs(backtest.load_benchmark_series, load_benchmark_series)
        self.assertIs(backtest.load_industry_map, load_industry_map)
        self.assertAlmostEqual(benchmark_total_return(pd.Series([10.0, 12.0])), 0.2)

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            benchmark_csv = tmp_path / "benchmark.csv"
            benchmark_csv.write_text("date,close\n2024-01-02,10\n2024-01-03,11\n", encoding="utf-8")
            series = load_benchmark_series(benchmark_csv)
            self.assertEqual(series.name, "benchmark_close")
            self.assertEqual(len(series), 2)

            industry_csv = tmp_path / "industry.csv"
            industry_csv.write_text("symbol,industry\n000001,Finance\n", encoding="utf-8")
            self.assertEqual(load_industry_map(industry_csv).get("000001"), "Finance")

    def test_self_check_render_split(self) -> None:
        from quantdesk.self_check_render import render_self_check_output
        from scripts.self_check import result

        output, summary = render_self_check_output([result("PASS", "sample", "ok"), result("WARN", "note", "check")])
        self.assertIn("PASS sample: ok", output)
        self.assertIn('"warn": 1', output)
        self.assertEqual(summary["pass"], 1)
        self.assertEqual(summary["warn"], 1)
        self.assertEqual(summary["fail"], 0)

    def test_package_indexes_and_docs_exist(self) -> None:
        import quantdesk.agent
        import quantdesk.intraday
        import quantdesk.paper
        import quantdesk.research

        self.assertTrue(hasattr(quantdesk.research, "build_risk_attribution_report"))
        self.assertTrue(hasattr(quantdesk.intraday, "build_intraday_live_workbench"))
        self.assertTrue(hasattr(quantdesk.paper, "build_multi_strategy_account_book"))
        self.assertTrue(hasattr(quantdesk.agent, "validate_agent_allowed_output"))

        for relative in [
            "docs/index.md",
            "docs/quickstart.md",
            "docs/user_guide.md",
            "docs/architecture.md",
            "quantdesk/research/README.md",
            "quantdesk/intraday/README.md",
            "quantdesk/paper/README.md",
            "quantdesk/agent/README.md",
        ]:
            self.assertTrue((PROJECT_ROOT / relative).exists(), relative)

        faq_text = (PROJECT_ROOT / "docs" / "faq.md").read_text(encoding="utf-8", errors="ignore")
        self.assertGreaterEqual(sum(1 for line in faq_text.splitlines() if line.startswith("## ")), 30)
        readme_lines = (PROJECT_ROOT / "README.md").read_text(encoding="utf-8", errors="ignore").splitlines()
        self.assertLessEqual(len(readme_lines), 350)

    def test_qd_help_still_works(self) -> None:
        completed = subprocess.run(
            [sys.executable, "scripts/qd.py", "--help"],
            cwd=PROJECT_ROOT,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("backtest", completed.stdout)
        self.assertIn("risk-attribution", completed.stdout)
        self.assertIn("experiments", completed.stdout)

    def test_release_proof_lists_stage_d_files(self) -> None:
        verify_text = (PROJECT_ROOT / "scripts" / "verify_clean_zip.py").read_text(encoding="utf-8")
        export_text = (PROJECT_ROOT / "export_project.ps1").read_text(encoding="utf-8")
        for token in [
            "quantdesk/backtest_loaders.py",
            "quantdesk/self_check_render.py",
            "quantdesk/research/__init__.py",
            "docs/quickstart.md",
            "tests/test_v3451_engineering_docs_and_package_grouping_lite.py",
        ]:
            self.assertIn(token, verify_text)
            self.assertIn(token, export_text)

    def test_clean_zip_forbidden_runtime_dirs_remain_configured(self) -> None:
        verify_text = (PROJECT_ROOT / "scripts" / "verify_clean_zip.py").read_text(encoding="utf-8")
        for token in ['"results/"', '"reports/"', '"state/"', '"test_runtime/"', '"data/paper/"']:
            self.assertIn(token, verify_text)

    def test_config_version(self) -> None:
        from quantdesk.version import API_SCHEMA_VERSION, APP_VERSION, PACKAGE_VERSION

        self.assertIn(PACKAGE_VERSION, {"3.4.5.1", "3.5.0", "3.5.1", "3.5.1.1", "3.5.2", "3.6.0", "3.6.1", "3.6.2", "3.7.0", "3.7.0.1", "3.7.0.2", "3.7.0.3", "3.7.0.4", "3.7.0.5", "3.7.0.6"})
        self.assertIn(APP_VERSION, {"3.4.5.1-engineering-docs-and-package-grouping-lite", "3.5.0-battle-ready-research-and-portable-release", "3.5.1-truthful-attribution-installer-and-structure-hardening", "3.5.1.1-release-staging-cleanup-and-attribution-stress-tests", "3.5.2-ui-home-and-strategy-recommender", "3.6.0-real-use-quant-platform-completion", "3.6.1-real-risk-alerts-and-smart-stop-integration", "3.6.2-real-similar-day-stress-and-style-attribution", "3.7.0-data-backbone", "3.7.0.1-strategy-viability-on-data-backbone", "3.7.0.2-strategy-pipeline-repair", "3.7.0.3-low-price-pipeline-finalization", "3.7.0.4-capital-lot-sensitivity-and-git-hygiene", "3.7.0.5-git-deletion-reconciliation", "3.7.0.6-strategy-regression-guard-and-report-hygiene"})
        self.assertIn(API_SCHEMA_VERSION, {"3.4.5.1", "3.5.0", "3.5.1", "3.5.1.1", "3.5.2", "3.6.0", "3.6.1", "3.6.2", "3.7.0", "3.7.0.1", "3.7.0.2", "3.7.0.3", "3.7.0.4", "3.7.0.5", "3.7.0.6"})


if __name__ == "__main__":
    unittest.main()






