from __future__ import annotations

import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from quantdesk import market_clock
from quantdesk.timing_advisor import advise_timing


class MarketClockTimezoneAndTimingTests(unittest.TestCase):
    def test_market_clock_normalizes_aware_time_to_asia_shanghai(self) -> None:
        current = market_clock.to_shanghai_datetime(datetime(2026, 5, 6, 1, 0, tzinfo=timezone.utc))
        self.assertEqual(current.tzinfo, market_clock.CN_TIMEZONE)
        self.assertEqual(current.hour, 9)

    def test_market_clock_treats_naive_time_as_asia_shanghai(self) -> None:
        current = market_clock.to_shanghai_datetime(datetime(2026, 5, 6, 9, 40))
        self.assertEqual(current.tzinfo, market_clock.CN_TIMEZONE)
        self.assertEqual(market_clock.get_market_session(current), "morning_session")

    def test_timing_next_review_uses_next_a_share_trading_day_after_close(self) -> None:
        advice = advise_timing(
            {
                "symbol": "000001",
                "name": "A",
                "trust_score": 80,
                "data_reliability_score": 90,
                "action_state": "条件满足后再评估",
            },
            now=datetime(2026, 5, 8, 15, 30),
        )
        self.assertIn("2026-05-11", advice.next_review_time)

    def test_timing_next_review_uses_next_a_share_trading_day_on_holiday(self) -> None:
        advice = advise_timing(
            {
                "symbol": "000001",
                "name": "A",
                "trust_score": 50,
                "data_reliability_score": 90,
                "action_state": "只观察",
            },
            now=datetime(2026, 5, 5, 10, 0),
        )
        self.assertIn("2026-05-06", advice.next_review_time)


if __name__ == "__main__":
    unittest.main()
