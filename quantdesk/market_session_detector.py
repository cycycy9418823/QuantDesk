from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any

from quantdesk.daily_open_workflow import md_table, write_json, write_md
from quantdesk.market_clock import to_shanghai_datetime
from quantdesk.trading_day_freshness import latest_expected_trade_date, trading_day_freshness


CN_A_SHARE_HOLIDAYS_2026 = {
    date(2026, 1, 1),
    date(2026, 1, 2),
    date(2026, 2, 16),
    date(2026, 2, 17),
    date(2026, 2, 18),
    date(2026, 2, 19),
    date(2026, 2, 20),
    date(2026, 4, 6),
    date(2026, 5, 1),
    date(2026, 5, 4),
    date(2026, 5, 5),
    date(2026, 6, 19),
    date(2026, 9, 25),
    date(2026, 10, 1),
    date(2026, 10, 2),
    date(2026, 10, 5),
    date(2026, 10, 6),
    date(2026, 10, 7),
}


@dataclass(frozen=True)
class SessionWindows:
    before_open_end: time = time(9, 29)
    morning_end: time = time(11, 30)
    lunch_end: time = time(13, 0)
    afternoon_end: time = time(14, 50)
    near_close_end: time = time(15, 10)
    post_close_end: time = time(18, 0)


def is_trading_day(day: date) -> bool:
    return day.weekday() < 5 and day not in CN_A_SHARE_HOLIDAYS_2026


def previous_trading_day(day: date) -> date:
    current = day
    while not is_trading_day(current):
        current -= timedelta(days=1)
    return current


def detect_market_session(root: Path, now: datetime | None = None, windows: SessionWindows | None = None) -> dict[str, Any]:
    # Normalize to Asia/Shanghai: a naive value is treated as Shanghai local
    # time and an aware value is converted, so an aware UTC instant just after
    # Shanghai midnight is classified under the correct local day/session.
    now = to_shanghai_datetime(now)
    windows = windows or SessionWindows()
    today = now.date()
    if not is_trading_day(today):
        session = "non_trading_day"
        update_mode = "skip_update"
    else:
        current = now.time()
        if current <= windows.before_open_end:
            session = "before_open"
            update_mode = "check_previous_trading_day"
        elif current <= windows.morning_end:
            session = "morning_session"
            update_mode = "intraday_snapshot_update"
        elif current <= windows.lunch_end:
            session = "lunch_break"
            update_mode = "intraday_snapshot_update"
        elif current <= windows.afternoon_end:
            session = "afternoon_session"
            update_mode = "intraday_snapshot_update"
        elif current <= windows.near_close_end:
            session = "near_close"
            update_mode = "intraday_snapshot_update"
        elif current <= windows.post_close_end:
            session = "post_close"
            update_mode = "post_close_daily_update"
        else:
            session = "after_hours"
            update_mode = "post_close_daily_update"
    fresh = trading_day_freshness(root, today=today)
    if session in {"post_close", "after_hours"}:
        expected = today
        reason = "post_close_today_completed_bar"
    else:
        expected, reason = latest_expected_trade_date(today)
    latest_local = fresh.get("latest_local_trade_date", "")
    data_fresh = bool(latest_local and latest_local >= expected.isoformat())
    freshness_status = "fresh_for_latest_trading_day" if data_fresh else fresh.get("status")
    return {
        "task_id": "v424_market_session",
        "status": "completed",
        "current_local_time": now.isoformat(timespec="seconds"),
        "is_trading_day": is_trading_day(today),
        "session": session,
        "recommended_update_mode": update_mode,
        "latest_local_trade_date": latest_local,
        "expected_latest_completed_trade_date": expected.isoformat(),
        "expected_reason": reason,
        "freshness_status": freshness_status,
        "data_fresh_for_latest_trading_day": data_fresh,
        "research_only": True,
        "no_broker": True,
        "no_live_trading": True,
        "no_auto_execution": True,
        "no_buy_sell_advice": True,
        "no_real_money_allocation": True,
    }


def write_market_session(root: Path, payload: dict[str, Any]) -> dict[str, Any]:
    write_json(root / "results/data_update/v424_market_session_latest.json", payload)
    write_md(
        root / "reports/data_update/v424_market_session_latest.md",
        "# v424 Market Session\n\n"
        + md_table(
            [payload],
            [
                "current_local_time",
                "is_trading_day",
                "session",
                "recommended_update_mode",
                "latest_local_trade_date",
                "expected_latest_completed_trade_date",
                "freshness_status",
            ],
        ),
    )
    return payload
