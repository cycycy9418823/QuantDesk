"""Local A-share trading calendar helper.

The module prefers ``data/calendar/a_share_trading_calendar.csv``.  If that
static CSV is missing or unreadable, QuantDesk falls back to a weekend-only
calendar and marks the risk clearly in ``calendar_info``.
"""

from __future__ import annotations

import csv
from datetime import date, datetime, timedelta
from functools import lru_cache
from pathlib import Path
from typing import Any

from quantdesk.shared import truthy

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_TRADING_CALENDAR_CSV = PROJECT_ROOT / "data" / "calendar" / "a_share_trading_calendar.csv"
WEEKEND_ONLY_NOTE = (
    "WARN: 当前使用 weekend_only 简化交易日历，未覆盖 A 股节假日和临时休市；"
    "历史回放结果仅可用于学习观察。"
)


def _as_date(value: Any) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value or "").strip()
    for fmt, size in (("%Y-%m-%d", 10), ("%Y/%m/%d", 10), ("%Y%m%d", 8)):
        try:
            return datetime.strptime(text[:size], fmt).date()
        except ValueError:
            continue
    return datetime.fromisoformat(text[:10]).date()


def _empty_calendar(calendar_path: Path, warning: str) -> dict[str, Any]:
    return {
        "calendar_quality": "weekend_only",
        "path": str(calendar_path),
        "is_open_by_date": {},
        "rows_by_date": {},
        "start_date": "",
        "end_date": "",
        "loaded_dates": 0,
        "weekend_only": True,
        "coverage_warning": warning,
        "note": WEEKEND_ONLY_NOTE,
    }


def _calendar_cache_key(path: Path | None = None) -> str:
    calendar_path = Path(path or DEFAULT_TRADING_CALENDAR_CSV)
    try:
        return str(calendar_path.expanduser().resolve())
    except Exception:
        return str(calendar_path)


@lru_cache(maxsize=16)
def _load_calendar_cached(calendar_path_text: str) -> dict[str, Any]:
    calendar_path = Path(calendar_path_text)
    if not calendar_path.exists():
        return _empty_calendar(
            calendar_path,
            "WARN: local A-share calendar CSV is missing; using weekend_only fallback.",
        )

    is_open_by_date: dict[str, bool] = {}
    rows_by_date: dict[str, dict[str, str]] = {}
    try:
        with calendar_path.open("r", encoding="utf-8-sig", newline="") as handle:
            for row in csv.DictReader(handle):
                day = str(row.get("date") or row.get("日期") or "").strip()[:10]
                if not day:
                    continue
                is_open = truthy(row.get("is_open") or row.get("open") or row.get("是否交易") or row.get("交易日"))
                is_open_by_date[day] = bool(is_open)
                raw_open = next((row[key] for key in ("is_open", "open", "是否交易", "交易日") if key in row), None)
                row_count = int(rows_by_date.get(day, {}).get("row_count", "0")) + 1
                rows_by_date[day] = {
                    "date": day,
                    "is_open": str(bool(is_open)).lower(),
                    "is_open_raw": "" if raw_open is None else str(raw_open),
                    "row_count": str(row_count),
                    "source": str(row.get("source") or "").strip(),
                    "note": str(row.get("note") or "").strip(),
                }
    except Exception as exc:  # noqa: BLE001
        return _empty_calendar(
            calendar_path,
            f"WARN: local A-share calendar CSV read failed: {exc}; using weekend_only fallback.",
        )

    dates = sorted(is_open_by_date)
    provisional = any(
        "provisional" in (row.get("source", "") + " " + row.get("note", "")).lower()
        for row in rows_by_date.values()
    )
    quality = "local_calendar_provisional" if provisional else "local_calendar"
    return {
        "calendar_quality": quality,
        "path": str(calendar_path),
        "is_open_by_date": is_open_by_date,
        "rows_by_date": rows_by_date,
        "start_date": dates[0] if dates else "",
        "end_date": dates[-1] if dates else "",
        "loaded_dates": len(dates),
        "weekend_only": False,
        "coverage_warning": "" if dates else "WARN: local A-share calendar CSV contains no usable dates.",
        "note": (
            "使用本地 A 股交易日历 CSV；如果 2027 年标记 provisional，表示该年度仍需后续核对官方安排。"
        ),
    }


def _load_calendar(path: Path | None = None) -> dict[str, Any]:
    return _load_calendar_cached(_calendar_cache_key(path))


def load_a_share_trading_calendar(path: Path | None = None) -> dict[str, Any]:
    """Load the default local A-share trading calendar CSV."""

    return _load_calendar(path)


def clear_calendar_cache() -> None:
    _load_calendar_cached.cache_clear()


def _coverage_warning(cal: dict[str, Any], *values: Any) -> str:
    if cal.get("weekend_only"):
        return str(cal.get("coverage_warning") or WEEKEND_ONLY_NOTE)
    start = str(cal.get("start_date") or "")
    end = str(cal.get("end_date") or "")
    if not start or not end:
        return "WARN: local A-share calendar CSV contains no usable dates."

    requested: list[date] = []
    for value in values:
        if not value:
            continue
        try:
            requested.append(_as_date(value))
        except Exception:
            continue
    if not requested:
        return str(cal.get("coverage_warning") or "")

    start_date = _as_date(start)
    end_date = _as_date(end)
    out_of_range = [day.isoformat() for day in requested if day < start_date or day > end_date]
    if out_of_range:
        return f"WARN: requested date outside local calendar coverage {start}..{end}: {', '.join(out_of_range)}"
    return str(cal.get("coverage_warning") or "")


def calendar_info(
    path: Path | None = None,
    *,
    target_date: Any = None,
    start_date: Any = None,
    end_date: Any = None,
) -> dict[str, Any]:
    cal = _load_calendar(path)
    return {
        "calendar_quality": cal["calendar_quality"],
        "path": cal["path"],
        "loaded_dates": int(cal.get("loaded_dates") or 0),
        "start_date": cal.get("start_date") or "",
        "end_date": cal.get("end_date") or "",
        "weekend_only": bool(cal.get("weekend_only")),
        "coverage_warning": _coverage_warning(cal, target_date, start_date, end_date),
        "note": cal.get("note") or "",
    }


def is_trade_day(value: Any, *, calendar_path: Path | None = None) -> bool:
    target = _as_date(value)
    cal = _load_calendar(calendar_path)
    lookup = cal.get("is_open_by_date") or {}
    if target.isoformat() in lookup:
        return bool(lookup[target.isoformat()])
    return target.weekday() < 5


def is_a_share_trading_day(value: Any, *, calendar_path: Path | None = None) -> bool:
    return is_trade_day(value, calendar_path=calendar_path)


def previous_trade_day(value: Any, *, calendar_path: Path | None = None) -> str:
    current = _as_date(value) - timedelta(days=1)
    guard = 0
    while not is_trade_day(current, calendar_path=calendar_path):
        current -= timedelta(days=1)
        guard += 1
        if guard > 370:
            raise RuntimeError("trading calendar lookup exceeded 370 days")
    return current.isoformat()


def previous_a_share_trading_day(value: Any, *, calendar_path: Path | None = None) -> str:
    return previous_trade_day(value, calendar_path=calendar_path)


def next_trade_day(value: Any, *, calendar_path: Path | None = None) -> str:
    current = _as_date(value) + timedelta(days=1)
    guard = 0
    while not is_trade_day(current, calendar_path=calendar_path):
        current += timedelta(days=1)
        guard += 1
        if guard > 370:
            raise RuntimeError("trading calendar lookup exceeded 370 days")
    return current.isoformat()


def trade_days_between(start_date: Any, end_date: Any, *, calendar_path: Path | None = None) -> list[str]:
    start = _as_date(start_date)
    end = _as_date(end_date)
    if end < start:
        return []
    days: list[str] = []
    current = start
    while current <= end:
        if is_trade_day(current, calendar_path=calendar_path):
            days.append(current.isoformat())
        current += timedelta(days=1)
    return days



def resolve_target_complete_trade_date(value: Any = None, *, current: Any = None, calendar_path: Path | None = None) -> dict[str, Any]:
    """Resolve the latest complete A-share trading date for local workflows.

    Defaults use the most recent fully closed trading day. Explicit dates are
    preserved, but warnings describe future, non-trading, or not-yet-closed
    dates so CLI payloads do not imply complete data when that is uncertain.
    """

    warnings: list[str] = []
    source = "explicit" if str(value or "").strip() else "inferred"
    now = current if isinstance(current, datetime) else None
    if now is None:
        if isinstance(current, date):
            now = datetime.combine(current, datetime.min.time())
        elif current:
            try:
                now = datetime.fromisoformat(str(current)[:19])
            except ValueError:
                now = datetime.now()
        else:
            now = datetime.now()
    today = now.date()
    market_close = datetime.strptime("15:00", "%H:%M").time()

    if value:
        try:
            target = _as_date(value)
        except Exception:
            target = today
            warnings.append(f"invalid target date {value}; using {target.isoformat()} for local checks")
        if target > today:
            warnings.append(f"target_date {target.isoformat()} is in the future; local data may be unavailable")
        if not is_a_share_trading_day(target, calendar_path=calendar_path):
            warnings.append(f"target_date {target.isoformat()} is not an A-share trading day")
        if target == today and is_a_share_trading_day(today, calendar_path=calendar_path) and now.time() < market_close:
            warnings.append(f"target_date {target.isoformat()} is before market close; complete daily bars may not be ready")
    else:
        if is_a_share_trading_day(today, calendar_path=calendar_path) and now.time() >= market_close:
            target = today
        else:
            # Before today's close (or on a non-trading day) the latest COMPLETE
            # bar is the most recent trading day STRICTLY BEFORE today — today's
            # bar does not exist yet. previous_a_share_trading_day(today) already
            # steps back from today, so on a trading day before close it yields
            # yesterday, and on a weekend/holiday it yields the last trading day.
            # (The previous `today + 1 day` form wrongly included today itself,
            # which made the data-health layer demand a not-yet-produced bar and
            # flag the freshest completed bar as stale.)
            target = _as_date(previous_a_share_trading_day(today, calendar_path=calendar_path))
        if target != today:
            warnings.append(f"default target date uses the latest complete A-share trading day: {target.isoformat()}")

    note = "user specified target_date" if source == "explicit" else "latest complete A-share trading date inferred from local calendar"
    return {
        "target_complete_trade_date": target.isoformat(),
        "effective_target_date": target.isoformat(),
        "target_date": target.isoformat(),
        "target_date_source": source,
        "target_date_note": note,
        "target_date_warnings": warnings,
        "warnings": warnings,
    }

__all__ = [
    "DEFAULT_TRADING_CALENDAR_CSV",
    "WEEKEND_ONLY_NOTE",
    "calendar_info",
    "clear_calendar_cache",
    "is_trade_day",
    "is_a_share_trading_day",
    "load_a_share_trading_calendar",
    "next_trade_day",
    "previous_a_share_trading_day",
    "previous_trade_day",
    "resolve_target_complete_trade_date",
    "trade_days_between",
]
