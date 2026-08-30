"""Central timestamp helpers for QuantDesk.

The functions are intentionally dependency-light and keep the existing local
time string formats used by result JSON files.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Any


def current_local_datetime() -> datetime:
    return datetime.now()


def now_iso(current: datetime | None = None) -> str:
    return (current or current_local_datetime()).isoformat(timespec="seconds")


def now_text(current: datetime | None = None, *, sep: str = "T") -> str:
    return now_iso(current).replace("T", sep, 1)


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def today_text() -> str:
    return current_local_datetime().date().isoformat()


def parse_date_safe(value: Any) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value or "").strip()
    if not text:
        return None
    text = text[:10].replace("/", "-")
    try:
        return datetime.strptime(text, "%Y-%m-%d").date()
    except Exception:
        return None
