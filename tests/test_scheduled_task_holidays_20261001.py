"""Deterministic holiday delegation and invisible console contract tests."""
from __future__ import annotations
import base64
from datetime import datetime
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("scheduled_task_test_subject", ROOT / "scripts/run_scheduled_task.py")
subject = importlib.util.module_from_spec(spec)
spec.loader.exec_module(subject)


class ScheduledTaskHolidayTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        path = self.root / "data/calendar/a_share_trading_calendar.csv"
        path.parent.mkdir(parents=True)
        path.write_text("date,is_open,source,note\n2026-09-30,true,test,\n"
                        "2026-10-01,false,test,\n2026-10-02,false,test,\n"
                        "2026-10-03,false,test,\n2026-10-08,true,test,\n", encoding="utf-8")
        self.action = {"executable": r"C:\Windows\System32\cmd.exe",
                       "arguments": '/d /s /c ""E:\\测试 路径\\run.cmd" --flag true"',
                       "working_directory": str(self.root)}
        self.runner = Mock(return_value=subprocess.CompletedProcess([], 0))

    def run_on(self, day, policy="trading_day"):
        return subject.run_task(self.root, "QuantDesk Test", policy, self.action,
                                now=datetime.fromisoformat(day+"T10:00:00+08:00"), runner=self.runner)

    def records(self):
        return [json.loads(line) for line in subject.task_log_path(self.root, "QuantDesk Test").read_text(encoding="utf-8").splitlines()]

    def test_national_holiday_never_starts_original_action(self):
        for day in ("2026-10-01", "2026-10-02"):
            self.assertEqual(self.run_on(day), 0)
        self.runner.assert_not_called()
        self.assertEqual([r["status"] for r in self.records()], ["skipped_non_trading_day"]*2)

    def test_normal_days_delegate(self):
        for day in ("2026-09-30", "2026-10-08"):
            self.assertEqual(self.run_on(day), 0)
        self.assertEqual(self.runner.call_count, 2)

    def test_weekend_closed(self):
        self.assertEqual(self.run_on("2026-10-03"), 0)
        self.runner.assert_not_called()

    def test_daily_backup_continues_during_holiday(self):
        self.assertEqual(self.run_on("2026-10-01", "daily"), 0)
        self.runner.assert_called_once()

    def test_missing_calendar_is_explicit_fallback(self):
        (self.root / "data/calendar/a_share_trading_calendar.csv").unlink()
        self.assertEqual(self.run_on("2026-10-01"), 0)
        self.runner.assert_called_once()
        self.assertEqual(self.records()[0]["status"], "calendar_warning")
        self.assertIn("missing", self.records()[0]["warning"])

    def test_outside_coverage_has_warning(self):
        self.assertEqual(self.run_on("2026-10-09"), 0)
        self.assertIn("outside", self.records()[0]["warning"])

    def test_timezone_uses_shanghai_date(self):
        self.assertEqual(subject.run_task(self.root, "QuantDesk Test", "trading_day", self.action,
            now=datetime.fromisoformat("2026-09-30T17:00:00+00:00"), runner=self.runner), 0)
        self.runner.assert_not_called()

    def test_child_exit_code_is_preserved(self):
        self.runner.return_value.returncode = 5
        self.assertEqual(self.run_on("2026-09-30"), 5)
        self.assertEqual(self.records()[-1]["status"], "failed")

    def test_start_error_is_recorded(self):
        self.runner.side_effect = OSError("not available")
        self.assertEqual(self.run_on("2026-09-30"), 2)
        self.assertEqual(self.records()[-1]["error_type"], "OSError")

    def test_exact_windows_arguments_and_hidden_console(self):
        self.run_on("2026-09-30")
        args, kwargs = self.runner.call_args
        self.assertFalse(kwargs["shell"])
        self.assertEqual(kwargs["cwd"], str(self.root))
        self.assertEqual(kwargs["stderr"], subprocess.STDOUT)
        if os.name == "nt":
            self.assertEqual(args[0], subprocess.list2cmdline([self.action["executable"]])+" "+self.action["arguments"])
            self.assertEqual(kwargs["creationflags"], subprocess.CREATE_NEW_CONSOLE)
            self.assertTrue(kwargs["startupinfo"].dwFlags & subprocess.STARTF_USESHOWWINDOW)
            self.assertEqual(kwargs["startupinfo"].wShowWindow, subprocess.SW_HIDE)

    def test_log_name_cannot_escape_log_directory(self):
        path = subject.task_log_path(self.root, "../../../../escape")
        self.assertEqual(path.parent, self.root/"logs/ops/scheduled_tasks")
        self.assertNotIn("..", path.name)

    def test_actions_roundtrip_unicode_and_quotes(self):
        text = base64.b64encode(json.dumps(self.action, ensure_ascii=False).encode()).decode()
        self.assertEqual(subject.decode_action(text), self.action)

    def test_invalid_action_never_reaches_business(self):
        for value in ([], {}, {**self.action, "other": 1}, {**self.action, "arguments": None},
                      {**self.action, "executable": "x\x00y"}):
            text = base64.b64encode(json.dumps(value).encode()).decode()
            with self.assertRaises(ValueError):
                subject.decode_action(text)

    def test_invalid_cli_action_leaves_error_log(self):
        code = subject.main(["--task-name", "bad", "--policy", "trading_day", "--project-root", str(self.root), "--action-base64", "!"])
        self.assertEqual(code, 2)
        self.assertIn('"invalid_action"', subject.task_log_path(self.root, "bad").read_text())

    def test_collector_reads_open_flag_instead_of_date_presence(self):
        from quantdesk.intraday_refresh.collector import trading_day_today, _load_trading_days
        for day, expected in (("2026-09-30", True), ("2026-10-01", False),
                              ("2026-10-02", False), ("2026-10-08", True)):
            with self.subTest(day=day):
                self.assertEqual(trading_day_today(self.root, now=datetime.fromisoformat(day+"T10:00:00")), expected)
        self.assertEqual(_load_trading_days(self.root), {"2026-09-30", "2026-10-08"})

    def test_calendar_with_only_closed_dates_never_falls_back_to_weekday(self):
        from quantdesk.intraday_refresh.collector import trading_day_today
        (self.root / "data/calendar/a_share_trading_calendar.csv").write_text(
            "date,is_open\n2026-10-01,false\n", encoding="utf-8")
        self.assertFalse(trading_day_today(self.root, now=datetime(2026, 10, 1, 10)))

    def test_collector_holiday_contacts_no_provider(self):
        from unittest.mock import patch
        from quantdesk.intraday_refresh import collector
        fetcher = Mock(side_effect=AssertionError("provider must not run on holiday"))
        with patch.object(collector, "refresh_full_market_snapshot") as snapshot:
            result = collector.collect_intraday_refresh(self.root, network=True,
                now=datetime(2026, 10, 1, 10), write_manifest=False, fetcher=fetcher)
        self.assertEqual(result["status"], "closed")
        self.assertEqual(result["run_state"], "skipped")
        self.assertEqual(result["reason_codes"], ["non_trading_day"])
        snapshot.assert_not_called()
        fetcher.assert_not_called()
        self.assertFalse((self.root / "results").exists())


if __name__ == "__main__":
    unittest.main()
