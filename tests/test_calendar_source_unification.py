from __future__ import annotations

import tempfile
import unittest
import warnings
from datetime import date
from pathlib import Path

from quantdesk import market_clock
from quantdesk.config import BUILTIN_DEFAULT_CONFIG
from quantdesk.trading_calendar import (
    DEFAULT_TRADING_CALENDAR_CSV,
    is_a_share_trading_day,
    load_a_share_trading_calendar,
)


class CalendarSourceUnificationTests(unittest.TestCase):
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
        self.assertEqual(Path(cal["path"]), DEFAULT_TRADING_CALENDAR_CSV)
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
