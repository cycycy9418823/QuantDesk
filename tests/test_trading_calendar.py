from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from quantdesk.trading_calendar import (
    calendar_info,
    clear_calendar_cache,
    is_trade_day,
    previous_trade_day,
    trade_days_between,
)


class TradingCalendarModuleSmokeTests(unittest.TestCase):
    def test_local_calendar_csv_controls_trade_days(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "calendar.csv"
            path.write_text(
                "date,is_open,source,note\n"
                "2026-01-02,true,test,\n"
                "2026-01-03,false,test,\n"
                "2026-01-05,true,test,\n",
                encoding="utf-8",
            )
            clear_calendar_cache()
            self.assertTrue(is_trade_day("2026-01-02", calendar_path=path))
            self.assertFalse(is_trade_day("2026-01-03", calendar_path=path))
            self.assertEqual(previous_trade_day("2026-01-05", calendar_path=path), "2026-01-02")
            self.assertEqual(trade_days_between("2026-01-02", "2026-01-05", calendar_path=path), ["2026-01-02", "2026-01-05"])
            self.assertEqual(calendar_info(path)["calendar_quality"], "local_calendar")

    def test_missing_calendar_is_visible_weekend_only_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            missing = Path(tmp) / "missing.csv"
            clear_calendar_cache()
            info = calendar_info(missing, target_date="2026-01-02")
            self.assertTrue(info["weekend_only"])
            self.assertIn("weekend_only", info["calendar_quality"])
            self.assertIn("missing", info["coverage_warning"])


if __name__ == "__main__":
    unittest.main()
