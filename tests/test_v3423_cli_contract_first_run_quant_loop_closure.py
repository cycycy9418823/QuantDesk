from __future__ import annotations

import csv
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import pandas as pd

from quantdesk.data_bootstrap import build_data_bootstrap
from quantdesk.operation_log import load_operation_log
from quantdesk.trading_calendar import resolve_target_complete_trade_date

try:
    from tests._isolated_project import IsolatedProject
    from tests._operation_log_isolation import isolate_operation_log_dir
except ModuleNotFoundError:  # pragma: no cover - unittest discovery adds tests/ directly
    from _isolated_project import IsolatedProject
    from _operation_log_isolation import isolate_operation_log_dir

isolate_operation_log_dir()

PROJECT_ROOT = Path(__file__).resolve().parents[1]
PYTHON = sys.executable


def run_qd(
    *args: str,
    root: Path | None = None,
    env: dict | None = None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [PYTHON, "scripts/qd.py", *args],
        cwd=root or PROJECT_ROOT,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        encoding="utf-8",
        errors="replace",
        timeout=120,
    )


def parse_stdout_json(proc: subprocess.CompletedProcess[str]) -> dict:
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError as exc:  # pragma: no cover - failure detail
        raise AssertionError(f"stdout is not JSON: {proc.stdout[:500]}\nstderr={proc.stderr}") from exc


def write_daily_csv(path: Path, rows: int = 180) -> None:
    dates = pd.date_range("2025-01-02", periods=rows, freq="B")
    closes = [10 + idx * 0.02 for idx in range(rows)]
    frame = pd.DataFrame(
        {
            "date": dates.strftime("%Y-%m-%d"),
            "open": [round(value * 0.99, 2) for value in closes],
            "high": [round(value * 1.02, 2) for value in closes],
            "low": [round(value * 0.98, 2) for value in closes],
            "close": closes,
            "prev_close": [None] + closes[:-1],
            "volume": [1_000_000 + idx for idx in range(rows)],
            "amount": [20_000_000 + idx * 1000 for idx in range(rows)],
            "adjust": ["raw"] * rows,
        }
    )
    frame.to_csv(path, index=False, encoding="utf-8-sig")


class V3423CliContractFirstRunQuantLoopTests(unittest.TestCase):
    def test_status_json_contract(self) -> None:
        proc = run_qd("status", "--json", "true")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        payload = parse_stdout_json(proc)
        self.assertIn("status", payload)
        self.assertEqual(payload.get("network"), False)

    def test_fix_plan_json_contract(self) -> None:
        proc = run_qd("fix-plan", "--json", "true")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        payload = parse_stdout_json(proc)
        self.assertIn("actions", payload)
        self.assertEqual(payload.get("network"), False)

    def test_walk_forward_accepts_network_false(self) -> None:
        proc = run_qd("walk-forward", "--network", "false", "--symbols", "600000", "--start-date", "20200101", "--end-date", "20260522")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertNotIn("unrecognized arguments", proc.stderr.lower())

    def test_backtest_no_daily_data_returns_warning(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            proc = run_qd("backtest", "--network", "false", "--json", "true", "--symbols", "600000", "--daily-dir", tmp)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        payload = parse_stdout_json(proc)
        self.assertEqual(payload.get("status"), "warning")
        self.assertEqual(payload.get("warning_kind"), "no_daily_data")
        self.assertNotIn(payload.get("status"), {"passed", "skipped"})

    def test_backtest_with_fixture_daily_generates_latest(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            daily = Path(tmp)
            write_daily_csv(daily / "000001.csv")
            proc = run_qd("backtest", "--network", "false", "--json", "true", "--symbols", "000001", "--daily-dir", str(daily))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        payload = parse_stdout_json(proc)
        self.assertIn("metrics", payload)
        self.assertIn("trades", payload)
        self.assertIn("assumptions", payload)
        self.assertTrue((PROJECT_ROOT / "results" / "backtest" / "backtest_latest.json").exists())

    def test_screen_missing_features_warning(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            proc = run_qd(
                "screen",
                "--json",
                "true",
                "--data",
                str(root / "missing_features.csv"),
                "--json-output",
                str(root / "screen.json"),
                "--output",
                str(root / "screen.md"),
            )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        payload = parse_stdout_json(proc)
        self.assertEqual(payload.get("status"), "warning")
        self.assertEqual(payload.get("warning_kind"), "missing_features")

    def test_screen_with_features_writes_latest_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            features = root / "features.csv"
            with features.open("w", encoding="utf-8-sig", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=["symbol", "name", "close", "row_count"])
                writer.writeheader()
                writer.writerow({"symbol": "000001", "name": "Alpha", "close": "10.5", "row_count": "160"})
            out_json = root / "screen.json"
            proc = run_qd("screen", "--json", "true", "--data", str(features), "--json-output", str(out_json), "--output", str(root / "screen.md"))
            self.assertEqual(proc.returncode, 0, proc.stderr)
            payload = parse_stdout_json(proc)
            self.assertEqual(payload.get("status"), "passed")
            self.assertTrue(out_json.exists())
            self.assertEqual(json.loads(out_json.read_text(encoding="utf-8"))["total_matched"], 1)

    def test_default_target_date_for_2026_05_23(self) -> None:
        resolved = resolve_target_complete_trade_date(current="2026-05-23T10:00:00")
        self.assertEqual(resolved["target_complete_trade_date"], "2026-05-22")

    def test_data_bootstrap_offline_payload_marks_universe_only(self) -> None:
        # The builder writes the demo universe even when the report write is off,
        # so redirect the universe path into a temp root: the test must never
        # touch the real data/universe sink.
        with tempfile.TemporaryDirectory() as tmp:
            isolated_universe = Path(tmp) / "data" / "universe" / "low_price_learning_symbols.csv"
            with mock.patch(
                "quantdesk.data_bootstrap.low_price_universe_path",
                return_value=isolated_universe,
            ):
                payload = build_data_bootstrap(symbols="600000", network=False, write=False)
        self.assertFalse(payload.get("daily_ready"))
        self.assertEqual(payload.get("bootstrap_mode"), "offline_universe_only")
        self.assertEqual(payload.get("next_required_step"), "network_data_update_or_manual_import")
        self.assertIn(payload.get("status"), {"warning", "initialized"})

    def test_operation_log_has_one_record_per_qd_command(self) -> None:
        from _operation_log_isolation import operation_log_timeline_path

        timeline = operation_log_timeline_path()
        before = len(load_operation_log(timeline))
        # Run the qd.py subprocesses in a disposable source clone with empty
        # runtime dirs so data-bootstrap writes its demo universe inside the
        # clone, never into the real project tree.
        with IsolatedProject(PROJECT_ROOT, temp_parent=Path(tempfile.gettempdir())) as project:
            env = project.subprocess_env()
            first = run_qd(
                "data-bootstrap", "--preset", "low_price_demo", "--network", "false", "--json", "true",
                root=project.root, env=env,
            )
            second = run_qd(
                "data-health", "--symbols", "600000", "--network", "false", "--json", "true", "--max-symbols", "1",
                root=project.root, env=env,
            )
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertEqual(second.returncode, 0, second.stderr)
        after = load_operation_log(timeline)
        new_commands = [row.get("command") for row in after[before:]]
        self.assertGreaterEqual(new_commands.count("data-bootstrap"), 1)
        self.assertGreaterEqual(new_commands.count("data-health"), 1)
        self.assertEqual([row.get("command") for row in after[-2:]], ["data-bootstrap", "data-health"])

    def test_clean_zip_runtime_exclusions_include_quant_loop_state(self) -> None:
        gitignore = (PROJECT_ROOT / ".gitignore").read_text(encoding="utf-8")
        export = (PROJECT_ROOT / "export_project.ps1").read_text(encoding="utf-8")
        for token in ("dist/", "results/", "reports/", "data/daily/", "data/intraday/", "data/universe/", "data/features/", "logs/", "state/", "*.zip"):
            self.assertIn(token, gitignore)
        for token in ("data\\daily", "data\\intraday", "data\\universe", "data\\features", "logs", "state"):
            self.assertIn(token, export)


if __name__ == "__main__":
    unittest.main()
