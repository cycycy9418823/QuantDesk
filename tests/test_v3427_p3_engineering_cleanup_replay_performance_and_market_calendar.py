from __future__ import annotations

import inspect
import json
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
import warnings
from pathlib import Path
from unittest import mock

import pandas as pd

from quantdesk import historical_replay_engine as replay
from quantdesk import market_clock
from scripts import self_check

try:
    from tests._operation_log_isolation import isolate_operation_log_dir
except ModuleNotFoundError:  # pragma: no cover - unittest discovery adds tests/ directly
    from _operation_log_isolation import isolate_operation_log_dir

isolate_operation_log_dir()

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _write_daily_csv(symbol: str, daily_dir: Path, dates: list[str]) -> None:
    rows = []
    for idx, day in enumerate(dates):
        close = 10.0 + idx * 0.02
        rows.append(
            {
                "date": day,
                "open": close - 0.05,
                "high": close + 0.10,
                "low": close - 0.12,
                "close": close,
                "volume": 100000 + idx,
                "amount": (100000 + idx) * close,
                "adjust": "raw",
            }
        )
    pd.DataFrame(rows).to_csv(daily_dir / f"{symbol}.csv", index=False, encoding="utf-8-sig")


def _business_dates(start: str, count: int) -> list[str]:
    return [str(day.date()) for day in pd.bdate_range(start=start, periods=count)]


class V3427ReplayPerformanceAndCalendarTests(unittest.TestCase):
    synthetic_elapsed_seconds: float | None = None

    def test_historical_replay_source_has_no_per_signal_tempfile_csv_path(self) -> None:
        source = inspect.getsource(replay)
        self.assertNotIn("TemporaryDirectory", source)
        self.assertNotIn("tempfile", source)
        self.assertNotIn("_prepare_signal_daily_dir", source)
        self.assertNotIn(".to_csv(", source)
        self.assertIn("slice_history_until_signal_date", source)
        self.assertIn("_load_daily_histories", source)

    def test_historical_replay_output_compatible_and_no_future_daily_rows(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            daily_dir = root / "daily"
            intraday_dir = root / "intraday"
            daily_dir.mkdir()
            intraday_dir.mkdir()
            _write_daily_csv(
                "600000",
                daily_dir,
                ["2026-05-13", "2026-05-14", "2026-05-15", "2026-05-18", "2026-05-19"],
            )

            frame, slice_warnings = replay.get_daily_slice("600000", "2026-05-14", source_dir=daily_dir)
            self.assertFalse(frame.empty)
            self.assertLessEqual(frame["date"].max().strftime("%Y-%m-%d"), "2026-05-14")
            self.assertTrue(any("truncated in memory" in item or "截断" in item for item in slice_warnings))

            with (
                mock.patch.object(replay, "RESULTS_DIR", root / "results"),
                mock.patch.object(replay, "REPORTS_DIR", root / "reports"),
                mock.patch.object(replay, "STATE_DIR", root / "state"),
            ):
                payload = replay.run_historical_intraday_replay(
                    "2026-05-14",
                    "2026-05-14",
                    strategy="long",
                    symbols=["600000"],
                    only_low_price=False,
                    dry_run=False,
                    daily_dir=daily_dir,
                    intraday_dir=intraday_dir,
                    output_path=root / "results" / "historical_intraday_replay_latest.json",
                    state_path=root / "state" / "historical_replay_account.json",
                )

            self.assertTrue(payload.get("ok"))
            self.assertEqual(payload.get("daily_slice_mode"), "in_memory_preloaded")
            self.assertEqual(payload.get("daily_history_load_mode"), "preload_once")
            self.assertEqual(payload.get("daily_history_loaded_symbol_count"), 1)
            self.assertIn("summary", payload)
            self.assertIn("daily_results", payload)
            self.assertTrue((root / "results" / "historical_intraday_replay_latest.json").exists())
            day = (payload.get("daily_results") or [{}])[0]
            self.assertEqual(day.get("source_data_cutoff"), "2026-05-14")
            self.assertEqual(day.get("anti_future_leak_check"), "passed")

    def test_historical_replay_report_humanizes_verified_minute_pool_counts(self) -> None:
        payload = {
            "start_date": "2026-07-01",
            "end_date": "2026-07-03",
            "use_intraday_provider": False,
            "summary": {
                "calendar_quality": "csv",
                "calendar_coverage_warning": "",
                "trade_day_count": 1,
                "plan_count": 3,
                "available_intraday_count": 2,
                "verified_completed_intraday_count": 1,
                "local_unverified_intraday_count": 1,
                "missing_intraday_count": 1,
                "reliable_event_count": 1,
                "no_fill_count": 2,
                "total_return_pct": 0,
            },
            "daily_results": [
                {
                    "signal_date": "2026-07-02",
                    "trade_date": "2026-07-03",
                    "source_data_cutoff": "2026-07-02",
                    "anti_future_leak_check": "passed",
                    "plan_count": 3,
                    "execution_summary": {
                        "available_intraday_count": 2,
                        "verified_completed_intraday_count": 1,
                        "local_unverified_intraday_count": 1,
                        "missing_intraday_count": 1,
                        "event_count": 1,
                        "no_fill_count": 2,
                    },
                }
            ],
        }

        report = replay._build_report(payload)

        self.assertIn("已验证分钟池可先用于覆盖内股票", report)
        self.assertIn("缺分钟线次数：1", report)
        self.assertIn("未成交/未触发数量：2", report)
        self.assertNotIn("missing_intraday：", report)
        self.assertNotIn("no_fill 数量", report)

    def test_synthetic_replay_smoke_uses_in_memory_preload(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            daily_dir = root / "daily"
            intraday_dir = root / "intraday"
            daily_dir.mkdir()
            intraday_dir.mkdir()
            dates = _business_dates("2025-01-02", 250)
            symbols = [f"60{i:04d}" for i in range(100)]
            for symbol in symbols:
                _write_daily_csv(symbol, daily_dir, dates)

            started = time.perf_counter()
            payload = replay.run_historical_intraday_replay(
                "2025-02-03",
                "2025-03-28",
                strategy="long",
                symbols=symbols,
                only_low_price=False,
                dry_run=True,
                max_days=20,
                daily_dir=daily_dir,
                intraday_dir=intraday_dir,
            )
            self.__class__.synthetic_elapsed_seconds = time.perf_counter() - started

        self.assertTrue(payload.get("ok"))
        self.assertEqual(payload.get("daily_slice_mode"), "in_memory_preloaded")
        self.assertEqual(payload.get("daily_history_loaded_symbol_count"), 100)
        self.assertEqual(len(payload.get("daily_results") or []), 20)
        self.assertEqual(payload.get("anti_future_leak_check"), "passed")

    def test_market_clock_does_not_expose_builtin_holidays(self) -> None:
        source = inspect.getsource(market_clock)
        self.assertNotIn("BUILTIN_CN_HOLIDAYS", source)
        self.assertFalse(hasattr(market_clock, "BUILTIN_CN_HOLIDAYS"))

    def test_market_clock_calendar_source_order_csv_yaml_unavailable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            csv_path = root / "calendar.csv"
            csv_path.write_text(
                "date,is_open,source,note\n2026-05-05,false,test,\n2026-05-06,true,test,\n",
                encoding="utf-8-sig",
            )
            csv_cal = market_clock.load_cn_trading_calendar(csv_path)
            self.assertEqual(csv_cal.get("source"), "csv")
            self.assertFalse(market_clock.is_cn_a_share_trading_day(pd.Timestamp("2026-05-05").date(), csv_cal))

            yaml_path = root / "trading_calendar_cn.yaml"
            yaml_path.write_text(
                "market: CN_A_SHARE\ntimezone: Asia/Shanghai\nholidays:\n  - 2026-05-05\nnotes:\n  - legacy test calendar\n",
                encoding="utf-8",
            )
            with mock.patch.object(market_clock, "LEGACY_CN_TRADING_CALENDAR_YAML", yaml_path):
                yaml_cal = market_clock.load_cn_trading_calendar(root / "missing.csv")
            self.assertEqual(yaml_cal.get("source"), "legacy_yaml")
            self.assertIn("legacy", " ".join(str(item) for item in yaml_cal.get("warnings") or []).lower())

            with (
                mock.patch.object(market_clock, "LEGACY_CN_TRADING_CALENDAR_YAML", root / "missing.yaml"),
                warnings.catch_warnings(record=True) as caught,
            ):
                warnings.simplefilter("always")
                unavailable = market_clock.load_cn_trading_calendar(root / "missing.csv")
            self.assertEqual(unavailable.get("source"), "unavailable")
            text = json.dumps(unavailable, ensure_ascii=False) + " ".join(str(item.message) for item in caught)
            self.assertIn("Trading calendar data is unavailable", text)
            self.assertNotIn("????", text)
            self.assertNotIn("???", text)
            self.assertNotIn("\ufffd", text)

    def test_self_check_exposes_market_calendar_source(self) -> None:
        check = self_check.check_market_calendar_source()
        text = f"{check.status} {check.name} {check.detail}"
        self.assertIn(check.status, {"PASS", "WARN"})
        self.assertIn("market_calendar_source", text)
        self.assertNotIn("????", text)
        self.assertNotIn("???", text)
        self.assertNotIn("\ufffd", text)

    def test_git_does_not_track_release_zip_files(self) -> None:
        if not shutil.which("git"):
            self.skipTest("git is not available")
        proc = subprocess.run(
            ["git", "ls-files"],
            cwd=PROJECT_ROOT,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        if proc.returncode != 0:
            self.skipTest("not running in a git worktree")
        tracked_zip = [line for line in proc.stdout.splitlines() if line.lower().endswith(".zip")]
        tracked_dist = [line for line in proc.stdout.splitlines() if line.startswith("dist/")]
        self.assertEqual(tracked_zip, [])
        self.assertEqual(tracked_dist, [])

    def test_summary_contract_sample_still_returns_compact_json(self) -> None:
        proc = subprocess.run(
            [
                sys.executable,
                "scripts/qd.py",
                "data-health",
                "--network",
                "false",
                "--json",
                "true",
                "--summary",
                "true",
            ],
            cwd=PROJECT_ROOT,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        payload = json.loads(proc.stdout)
        self.assertIn("output_paths", payload)
        text = json.dumps(payload, ensure_ascii=False)
        self.assertNotIn("????", text)
        self.assertNotIn("???", text)
        self.assertNotIn("\ufffd", text)


if __name__ == "__main__":
    unittest.main()
