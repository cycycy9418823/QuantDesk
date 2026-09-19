"""CSV trading-calendar source unification (offline fixture based).

The unified source is ``data/calendar/a_share_trading_calendar.csv``. That file
is a generated local data asset, not a tracked source file, so these tests build
a complete deterministic calendar with the project's own offline builder and
point the default loader paths at it. This proves the CSV-preferred logic and
the agreement between ``market_clock`` and ``trading_calendar`` without relying
on whatever happens to be present on the machine.

Whether the real default CSV exists and covers the required range stays a
real-site item (`scripts/self_check.py` / the shipped asset), not something this
offline test fabricates.
"""

from __future__ import annotations

import shutil
import tempfile
import unittest
import warnings
from datetime import date
from pathlib import Path
from unittest import mock

from quantdesk import market_clock, trading_calendar
from quantdesk.config import BUILTIN_DEFAULT_CONFIG
from quantdesk.trading_calendar import (
    is_a_share_trading_day,
    load_a_share_trading_calendar,
)
from scripts.build_trading_calendar import build_offline_calendar, write_calendar


def _write_offline_calendar(path: Path) -> None:
    rows = build_offline_calendar("2020-01-01", "2027-12-31", source="offline")
    write_calendar(rows, path)


class CalendarSourceUnificationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._tmp = tempfile.mkdtemp(prefix="qd_calendar_source_")
        cls.fixture = (
            Path(cls._tmp) / "data" / "calendar" / "a_share_trading_calendar.csv"
        )
        _write_offline_calendar(cls.fixture)
        cls._patchers = [
            mock.patch.object(trading_calendar, "DEFAULT_TRADING_CALENDAR_CSV", cls.fixture),
            mock.patch.object(market_clock, "DEFAULT_CN_TRADING_CALENDAR_PATH", cls.fixture),
        ]
        for patcher in cls._patchers:
            patcher.start()
        trading_calendar.clear_calendar_cache()

    @classmethod
    def tearDownClass(cls) -> None:
        for patcher in reversed(cls._patchers):
            patcher.stop()
        trading_calendar.clear_calendar_cache()
        shutil.rmtree(cls._tmp, ignore_errors=True)

    def test_default_config_uses_csv_calendar(self) -> None:
        self.assertEqual(
            BUILTIN_DEFAULT_CONFIG["calendar"]["trading_calendar_path"],
            "data/calendar/a_share_trading_calendar.csv",
        )

    def test_market_clock_prefers_csv_and_matches_trading_calendar(self) -> None:
        cal = market_clock.load_cn_trading_calendar()
        self.assertEqual(cal.get("source"), "csv")
        for day in (date(2026, 5, 5), date(2026, 5, 6), date(2026, 5, 9)):
            self.assertEqual(
                market_clock.is_cn_a_share_trading_day(day, cal),
                is_a_share_trading_day(day),
                day.isoformat(),
            )

    def test_csv_calendar_loader_exposes_default_path(self) -> None:
        cal = load_a_share_trading_calendar()
        self.assertEqual(Path(cal["path"]).name, "a_share_trading_calendar.csv")
        self.assertTrue(Path(cal["path"]).exists())
        self.assertGreater(int(cal.get("loaded_dates") or 0), 0)

    def test_yaml_is_optional_and_builtin_fallback_warns(self) -> None:
        from scripts import self_check

        self.assertFalse(any(path.name == "trading_calendar_cn.yaml" for path in self_check.required_files()))
        with tempfile.TemporaryDirectory() as tmp, warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            cal = market_clock.load_cn_trading_calendar(Path(tmp) / "missing.yaml")
        self.assertEqual(cal.get("source"), "unavailable")
        self.assertTrue(any("unavailable" in str(item.message).lower() for item in caught))


if __name__ == "__main__":
    unittest.main()
