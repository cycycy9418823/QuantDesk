from datetime import date
from pathlib import Path

from quantdesk.trading_day_freshness import latest_expected_trade_date, trading_day_freshness


ROOT = Path(__file__).resolve().parents[1]


def test_latest_expected_trade_date_uses_latest_completed_bar():
    expected, reason = latest_expected_trade_date(date(2026, 6, 3))

    assert expected.isoformat() == "2026-06-02"
    assert reason == "regular_trading_day_latest_completed_bar"


def test_weekend_uses_previous_trading_day():
    expected, reason = latest_expected_trade_date(date(2026, 6, 7))

    assert expected.weekday() == 4
    assert reason == "weekend_or_holiday"


def test_trading_day_freshness_distinguishes_calendar_stale_from_trading_fresh():
    payload = trading_day_freshness(ROOT, today=date(2026, 6, 3))

    assert payload["latest_expected_trade_date"] == "2026-06-02"
    assert payload["data_fresh_for_latest_trading_day"] is True
    assert payload["status"] in {"fresh_for_latest_trading_day", "calendar_stale_but_trading_fresh"}

