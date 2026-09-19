from __future__ import annotations

import json
import importlib.util
import subprocess
import sys
import unittest
from pathlib import Path


try:
    from tests._operation_log_isolation import isolate_operation_log_dir
except ModuleNotFoundError:  # pragma: no cover - unittest discovery adds tests/ directly
    from _operation_log_isolation import isolate_operation_log_dir

isolate_operation_log_dir()
PROJECT_ROOT = Path(__file__).resolve().parents[1]
PYTHON = sys.executable


def run_command(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [PYTHON, *args],
        cwd=PROJECT_ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=90,
    )


def read_text(path: str) -> str:
    return (PROJECT_ROOT / path).read_text(encoding="utf-8", errors="ignore")


class V3424WindowsPythonEnvAndCommandConsistencyTests(unittest.TestCase):
    def test_check_python_env_json_contains_runtime_keys(self) -> None:
        proc = run_command("scripts/check_python_env.py", "--json", "true")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        payload = json.loads(proc.stdout)
        for key in (
            "executable",
            "version",
            "stdout_encoding",
            "path_python_available",
            "py_launcher_available",
            "filesystem_encoding",
            "locale_preferred_encoding",
            "utf8_mode",
            "cwd",
            "suggested_next_action",
        ):
            self.assertIn(key, payload)
        self.assertFalse(payload.get("network"))

    def test_qd_env_doctor_json_contract(self) -> None:
        proc = run_command("scripts/qd.py", "env-doctor", "--json", "true")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        payload = json.loads(proc.stdout)
        self.assertEqual(payload.get("task_type"), "python_env_doctor")
        self.assertIn("path_python_available", payload)
        self.assertIn("results/python_env_latest.json", str(payload.get("output_paths", {})))

    def test_qd_json_stdout_is_default_codepage_safe(self) -> None:
        proc = subprocess.run(
            [PYTHON, "scripts/qd.py", "env-doctor", "--json", "true"],
            cwd=PROJECT_ROOT,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=90,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertTrue(proc.stdout.strip())
        self.assertTrue(all(ord(char) < 128 for char in proc.stdout))
        payload = json.loads(proc.stdout)
        self.assertEqual(payload.get("task_type"), "python_env_doctor")

    def test_qd_operation_log_collects_warning_top_entries(self) -> None:
        spec = importlib.util.spec_from_file_location("qd_under_test", PROJECT_ROOT / "scripts" / "qd.py")
        self.assertIsNotNone(spec)
        module = importlib.util.module_from_spec(spec)
        self.assertIsNotNone(spec.loader)
        spec.loader.exec_module(module)

        warnings = module._collect_log_warnings(
            {
                "status": "warning",
                "warning_count": 3,
                "warnings_top": ["daily_required_columns_missing:000060.csv", "degraded_pit_price_csv_coverage"],
            }
        )
        self.assertEqual(
            warnings,
            ["daily_required_columns_missing:000060.csv", "degraded_pit_price_csv_coverage"],
        )
        self.assertEqual(
            module._collect_log_warnings({"status": "warning", "warning_count": 2, "warning_kind": "no_daily_data"}),
            ["no_daily_data=2"],
        )

    def test_qd_help_exposes_env_doctor_and_python_note(self) -> None:
        proc = run_command("scripts/qd.py", "--help")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("env-doctor --json true", proc.stdout)
        self.assertIn("QUANTDESK_PYTHON", proc.stdout)

    def test_windows_cmd_scripts_support_quantdesk_python_fallback(self) -> None:
        for path in ("run_tests.cmd", "self_check.cmd", "run_daily_smoke.cmd"):
            text = read_text(path)
            self.assertIn("QUANTDESK_PYTHON", text, path)
            self.assertIn(".venv\\Scripts\\python.exe", text, path)
            self.assertIn("set \"PY=python\"", text, path)
            self.assertTrue(
                "请激活 conda 环境，或设置 QUANTDESK_PYTHON 指向 python.exe" in text
                or "Activate the project environment or set QUANTDESK_PYTHON to python.exe" in text,
                path,
            )

    def test_agents_md_contains_current_qd_flow_and_env_doctor(self) -> None:
        text = read_text("AGENTS.md")
        for token in (
            "python scripts/qd.py env-doctor --json true",
            "python scripts/qd.py data-bootstrap --preset low_price_demo --network false --json true",
            "python scripts/qd.py build-features --json true",
            "python scripts/qd.py backtest --network false --json true",
            "python scripts/qd.py walk-forward --network false",
            "clean zip path",
        ):
            self.assertIn(token, text)

    def test_readme_documents_windows_python_choice(self) -> None:
        text = read_text("README.md")
        self.assertIn("Windows Python 选择", text)
        self.assertIn("文档里的 `python` 是占位命令", text)
        self.assertIn("QUANTDESK_PYTHON", text)
        self.assertIn("env-doctor --json true", text)

    def test_local_miniconda_path_is_example_not_forced_default(self) -> None:
        combined = "\n".join(
            read_text(path)
            for path in ("README.md", "AGENTS.md", "run_tests.cmd", "self_check.cmd", "run_daily_smoke.cmd", "scripts/check_python_env.py")
        )
        self.assertNotIn('set "PY=E:\\miniconda\\python.exe"', combined)
        self.assertNotIn("must use E:\\miniconda", combined.lower())
        self.assertNotIn("必须使用 E:\\miniconda", combined)
        self.assertIn("Example", combined)

    def test_self_check_mentions_python_launcher_env_as_warn_or_pass(self) -> None:
        proc = run_command("scripts/self_check.py")
        self.assertIn(proc.returncode, {0, 1})
        self.assertIn("python_launcher_env", proc.stdout)
        self.assertNotIn("FAIL python_launcher_env", proc.stdout)

    def test_version_metadata_is_current_or_v3424_family(self) -> None:
        from quantdesk.version import API_SCHEMA_VERSION, APP_VERSION, PACKAGE_VERSION, RELEASE_TITLE

        self.assertIn(PACKAGE_VERSION, {"3.4.2.4", "3.4.2.5", "3.5.1", "3.5.1.1", "3.5.2", "3.6.0", "3.6.1", "3.6.2", "3.7.0", "3.7.0.1", "3.7.0.2", "3.7.0.3", "3.7.0.4", "3.7.0.5", "3.7.0.6"})
        self.assertIn(API_SCHEMA_VERSION, {"3.4.2.4", "3.4.2.5", "3.5.1", "3.5.1.1", "3.5.2", "3.6.0", "3.6.1", "3.6.2", "3.7.0", "3.7.0.1", "3.7.0.2", "3.7.0.3", "3.7.0.4", "3.7.0.5", "3.7.0.6"})
        self.assertIn(
            APP_VERSION,
            {
                "3.4.2.4-windows-python-env-and-command-consistency",
                "3.4.2.5-cli-summary-output-and-operator-readability",
                "3.5.1-truthful-attribution-installer-and-structure-hardening",
                "3.5.1.1-release-staging-cleanup-and-attribution-stress-tests",
                "3.5.2-ui-home-and-strategy-recommender",
                "3.6.0-real-use-quant-platform-completion",
                "3.6.1-real-risk-alerts-and-smart-stop-integration",
                "3.6.2-real-similar-day-stress-and-style-attribution",
                "3.7.0-data-backbone",
                "3.7.0.1-strategy-viability-on-data-backbone",
                "3.7.0.2-strategy-pipeline-repair",
                "3.7.0.3-low-price-pipeline-finalization",
                "3.7.0.4-capital-lot-sensitivity-and-git-hygiene",
                "3.7.0.5-git-deletion-reconciliation",
                "3.7.0.6-strategy-regression-guard-and-report-hygiene",
            },
        )
        self.assertIn(
            RELEASE_TITLE,
            {
                "v3.4.2.4 Windows Python Env And Command Consistency",
                "v3.4.2.5 CLI Summary Output And Operator Readability",
                "v3.5.1 Truthful Attribution Installer And Structure Hardening",
                "v3.5.1.1 Release Staging Cleanup And Attribution Stress Tests",
                "v3.5.2 UI Home And Strategy Recommender",
                "v3.6.0 Real Use Quant Platform Completion",
                "v3.6.1 Real Risk Alerts And Smart Stop Integration",
                "v3.6.2 Real Similar Day Stress And Style Attribution",
                "v3.7.0 Data Backbone",
                "v3.7.0.1 Strategy Viability On Data Backbone",
                "v3.7.0.2 Strategy Pipeline Repair",
                "v3.7.0.3 Low Price Pipeline Finalization",
                "v3.7.0.4 Capital Lot Sensitivity And Git Hygiene",
                "v3.7.0.5 Git Deletion Reconciliation",
                "v3.7.0.6 Strategy Regression Guard and Report Hygiene",
            },
        )


if __name__ == "__main__":
    unittest.main()
