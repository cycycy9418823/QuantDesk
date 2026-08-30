from __future__ import annotations

from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from quantdesk.data_update_orchestrator import latest_daily_date


def is_weekend(day: date) -> bool:
    return day.weekday() >= 5


def previous_trading_day(day: date) -> date:
    current = day
    while is_weekend(current):
        current -= timedelta(days=1)
    return current


def latest_expected_trade_date(today: date | None = None) -> tuple[date, str]:
    today = today or datetime.now().date()
    if is_weekend(today):
        return previous_trading_day(today), "weekend_or_holiday"
    # Before a fresh provider close, yesterday's completed bar is acceptable.
    return previous_trading_day(today - timedelta(days=1)), "regular_trading_day_latest_completed_bar"


def trading_day_freshness(root: Path, today: date | None = None) -> dict[str, Any]:
    today = today or datetime.now().date()
    local_date, scanned = latest_daily_date(root)
    expected, reason = latest_expected_trade_date(today)
    if not local_date:
        status = "freshness_unknown_human_review"
        stale = None
    else:
        stale = 0
        current = local_date
        while current < expected:
            current += timedelta(days=1)
            if not is_weekend(current):
                stale += 1
        if local_date >= expected:
            status = "fresh_for_latest_trading_day"
        elif is_weekend(today):
            status = "market_closed_no_update_needed" if stale == 0 else "stale_by_trading_day"
        else:
            status = "stale_by_trading_day"
    calendar_stale = (today - local_date).days if local_date else None
    if calendar_stale and calendar_stale > 0 and status == "fresh_for_latest_trading_day":
        status = "calendar_stale_but_trading_fresh"
    return {
        "task_id": "v421_trading_day_freshness",
        "status": status,
        "latest_local_trade_date": local_date.isoformat() if local_date else "",
        "latest_expected_trade_date": expected.isoformat(),
        "trading_day_staleness": stale,
        "calendar_staleness_days": calendar_stale,
        "market_closed_today": is_weekend(today),
        "weekend_or_holiday": reason == "weekend_or_holiday",
        "data_fresh_for_latest_trading_day": status in {"fresh_for_latest_trading_day", "calendar_stale_but_trading_fresh", "market_closed_no_update_needed"},
        "daily_files_scanned": scanned,
        "reason": reason,
    }

