from datetime import datetime
from pathlib import Path

from quantdesk.market_session_detector import detect_market_session


ROOT = Path(__file__).resolve().parents[1]


def test_market_session_detector_classifies_sessions():
    morning = detect_market_session(ROOT, now=datetime(2026, 6, 3, 10, 0))
    lunch = detect_market_session(ROOT, now=datetime(2026, 6, 3, 12, 0))
    after = detect_market_session(ROOT, now=datetime(2026, 6, 3, 19, 0))

    assert morning["session"] == "morning_session"
    assert lunch["session"] == "lunch_break"
    assert after["session"] == "after_hours"
    assert morning["expected_latest_completed_trade_date"]


def test_market_session_detector_handles_holiday_not_just_weekday():
    payload = detect_market_session(ROOT, now=datetime(2026, 10, 1, 10, 0))

    assert payload["session"] == "non_trading_day"
    assert payload["is_trading_day"] is False

