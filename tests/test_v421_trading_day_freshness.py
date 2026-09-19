from datetime import date
from pathlib import Path

from quantdesk.trading_day_freshness import latest_expected_trade_date, trading_day_freshness


def test_latest_expected_trade_date_uses_latest_completed_bar():
    expected, reason = latest_expected_trade_date(date(2026, 6, 3))

    assert expected.isoformat() == "2026-06-02"
    assert reason == "regular_trading_day_latest_completed_bar"


def test_weekend_uses_previous_trading_day():
    expected, reason = latest_expected_trade_date(date(2026, 6, 7))

    assert expected.weekday() == 4
    assert reason == "weekend_or_holiday"


def test_trading_day_freshness_distinguishes_calendar_stale_from_trading_fresh(tmp_path: Path):
    daily = tmp_path / "data" / "daily"
    daily.mkdir(parents=True)
    (daily / "600000.csv").write_text(
        "date,symbol,open,high,low,close,volume\n2026-06-02,600000,1,1,1,1,100\n",
        encoding="utf-8",
    )

    payload = trading_day_freshness(tmp_path, today=date(2026, 6, 3))

    assert payload["latest_expected_trade_date"] == "2026-06-02"
    assert payload["data_fresh_for_latest_trading_day"] is True
    assert payload["status"] in {"fresh_for_latest_trading_day", "calendar_stale_but_trading_fresh"}

