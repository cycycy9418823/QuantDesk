from __future__ import annotations

import warnings
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from quantdesk.trading_calendar import DEFAULT_TRADING_CALENDAR_CSV, load_a_share_trading_calendar

CN_TIMEZONE_NAME = "Asia/Shanghai"
CN_TIMEZONE = ZoneInfo(CN_TIMEZONE_NAME)
MORNING_START = time(9, 30)
MORNING_END = time(11, 30)
AFTERNOON_START = time(13, 0)
AFTERNOON_END = time(15, 0)
PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CN_TRADING_CALENDAR_PATH = DEFAULT_TRADING_CALENDAR_CSV
LEGACY_CN_TRADING_CALENDAR_YAML = PROJECT_ROOT / "config" / "trading_calendar_cn.yaml"

DEFAULT_CALENDAR_NOTE = (
    "Weekends are non-trading days by default. Adjusted workdays are not "
    "treated as A-share trading days unless the local exchange calendar says so. "
    "Please keep data/calendar/a_share_trading_calendar.csv current."
)
CALENDAR_UNAVAILABLE_WARNING = (
    "Trading calendar data is unavailable. Market open/close detection may be "
    "degraded. Please provide a current CN trading calendar CSV or YAML file."
)

def to_shanghai_datetime(value: datetime | None = None) -> datetime:
    """Return a timezone-aware datetime normalized to Asia/Shanghai.

    Naive datetimes are treated as local China A-share market time. This keeps
    report, API, and paper-portfolio settlement logic independent from the host
    machine's timezone setting.
    """

    if value is None:
        return datetime.now(CN_TIMEZONE)
    if value.tzinfo is None:
        return value.replace(tzinfo=CN_TIMEZONE)
    return value.astimezone(CN_TIMEZONE)


def _as_datetime(value: datetime | None = None) -> datetime:
    return to_shanghai_datetime(value)


def _as_date(value: Any) -> date | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value).strip()
    for fmt in ("%Y-%m-%d", "%Y%m%d", "%Y/%m/%d"):
        try:
            source = text[:10] if fmt in {"%Y-%m-%d", "%Y/%m/%d"} else text[:8]
            return datetime.strptime(source, fmt).date()
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(text[:19]).date()
    except ValueError:
        return None


def _parse_simple_calendar_yaml(text: str) -> dict[str, Any]:
    calendar: dict[str, Any] = {"market": "CN_A_SHARE", "timezone": "Asia/Shanghai", "holidays": [], "notes": []}
    current_list: str | None = None
    for raw_line in text.splitlines():
        line = raw_line.split("#", 1)[0].rstrip()
        if not line.strip():
            continue
        stripped = line.strip()
        if stripped.endswith(":"):
            key = stripped[:-1].strip()
            if key in {"holidays", "notes"}:
                current_list = key
                calendar.setdefault(key, [])
            else:
                current_list = None
                calendar.setdefault(key, {})
            continue
        if stripped.startswith("- ") and current_list:
            calendar.setdefault(current_list, []).append(stripped[2:].strip().strip("\"'"))
            continue
        if ":" in stripped:
            key, value = stripped.split(":", 1)
            calendar[key.strip()] = value.strip().strip("\"'")
            current_list = None
    return calendar


def load_cn_trading_calendar(path: str | Path | None = None) -> dict[str, Any]:
    """Load the local A-share trading calendar without network access.

    Source order is CSV, then legacy YAML, then an explicit degraded payload.
    QuantDesk no longer uses hard-coded holiday sets as a production fallback.
    """

    calendar_path = DEFAULT_CN_TRADING_CALENDAR_PATH if path is None else Path(path)
    if not calendar_path.is_absolute():
        calendar_path = PROJECT_ROOT / calendar_path
    if calendar_path.suffix.lower() == ".csv" and calendar_path.exists():
        loaded_csv = load_a_share_trading_calendar(calendar_path)
        return {
            "market": "CN_A_SHARE",
            "timezone": "Asia/Shanghai",
            "holidays": sorted(
                day for day, is_open in (loaded_csv.get("is_open_by_date") or {}).items() if not is_open
            ),
            "notes": [],
            "calendar_note": str(loaded_csv.get("note") or DEFAULT_CALENDAR_NOTE),
            "path": str(calendar_path),
            "source": "csv",
            "calendar_quality": loaded_csv.get("calendar_quality") or "local_calendar",
            "is_open_by_date": loaded_csv.get("is_open_by_date") or {},
            "rows_by_date": loaded_csv.get("rows_by_date") or {},
            "warnings": [loaded_csv.get("coverage_warning")] if loaded_csv.get("coverage_warning") else [],
        }

    legacy_path = calendar_path if calendar_path.suffix.lower() in {".yaml", ".yml"} else LEGACY_CN_TRADING_CALENDAR_YAML
    if legacy_path.exists():
        if calendar_path != legacy_path:
            warnings.warn(
                "A-share calendar CSV is missing; falling back to legacy config/trading_calendar_cn.yaml.",
                RuntimeWarning,
                stacklevel=2,
            )
        text = legacy_path.read_text(encoding="utf-8")
        try:
            import yaml  # type: ignore
        except ImportError:
            loaded = _parse_simple_calendar_yaml(text)
        else:
            loaded = yaml.safe_load(text) or {}
            if not isinstance(loaded, dict):
                loaded = {}
        holidays = {str(item)[:10] for item in loaded.get("holidays", []) if str(item).strip()}
        notes = loaded.get("notes") or []
        note_text = "; ".join(str(item) for item in notes if str(item).strip()) or DEFAULT_CALENDAR_NOTE
        return {
            "market": loaded.get("market", "CN_A_SHARE"),
            "timezone": loaded.get("timezone", "Asia/Shanghai"),
            "holidays": sorted(holidays),
            "notes": notes,
            "calendar_note": note_text,
            "path": str(legacy_path),
            "source": "legacy_yaml",
            "calendar_quality": "legacy_yaml",
            "is_open_by_date": {},
            "rows_by_date": {},
            "warnings": ["A-share calendar CSV is missing; using legacy YAML fallback."],
        }

    warnings.warn(CALENDAR_UNAVAILABLE_WARNING, RuntimeWarning, stacklevel=2)
    return {
        "market": "CN_A_SHARE",
        "timezone": "Asia/Shanghai",
        "holidays": [],
        "notes": [],
        "calendar_note": CALENDAR_UNAVAILABLE_WARNING,
        "path": str(calendar_path),
        "source": "unavailable",
        "calendar_quality": "unavailable",
        "is_open_by_date": {},
        "rows_by_date": {},
        "warnings": [CALENDAR_UNAVAILABLE_WARNING],
    }

def is_weekend(value: date | datetime) -> bool:
    target = value.date() if isinstance(value, datetime) else value
    return target.weekday() >= 5


def is_cn_market_holiday(value: date | datetime, calendar: dict[str, Any] | None = None) -> bool:
    target = value.date() if isinstance(value, datetime) else value
    cal = calendar or load_cn_trading_calendar()
    lookup = cal.get("is_open_by_date") if isinstance(cal.get("is_open_by_date"), dict) else {}
    if target.isoformat() in lookup:
        return not bool(lookup[target.isoformat()])
    return target.isoformat() in {str(item)[:10] for item in cal.get("holidays", [])}


def is_cn_a_share_trading_day(value: date | datetime, calendar: dict[str, Any] | None = None) -> bool:
    """Return whether the date is an A-share trading day using the local calendar.

    Weekends are always non-trading days. Adjusted workdays are not treated as
    exchange trading days unless the exchange calendar explicitly says so in a
    future enhancement.
    """

    target = value.date() if isinstance(value, datetime) else value
    cal = calendar or load_cn_trading_calendar()
    lookup = cal.get("is_open_by_date") if isinstance(cal.get("is_open_by_date"), dict) else {}
    if target.isoformat() in lookup:
        return bool(lookup[target.isoformat()])
    if is_weekend(target):
        return False
    return not is_cn_market_holiday(target, cal)


def is_likely_trading_day(value: date | datetime) -> bool:
    return is_cn_a_share_trading_day(value)


def previous_cn_a_share_trading_day(value: date | datetime, calendar: dict[str, Any] | None = None) -> date:
    target = value.date() if isinstance(value, datetime) else value
    current = target - timedelta(days=1)
    while not is_cn_a_share_trading_day(current, calendar):
        current -= timedelta(days=1)
    return current


def next_cn_a_share_trading_day(value: date | datetime, calendar: dict[str, Any] | None = None) -> date:
    target = value.date() if isinstance(value, datetime) else value
    current = target + timedelta(days=1)
    while not is_cn_a_share_trading_day(current, calendar):
        current += timedelta(days=1)
    return current


def previous_likely_trading_day(value: date) -> date:
    return previous_cn_a_share_trading_day(value)


def recent_likely_trading_day(value: date) -> date:
    current = value
    while not is_cn_a_share_trading_day(current):
        current -= timedelta(days=1)
    return current


def get_market_session(now: datetime | None = None, calendar: dict[str, Any] | None = None) -> str:
    current = _as_datetime(now)
    today = current.date()
    if is_weekend(today):
        return "non_trading_day"
    if is_cn_market_holiday(today, calendar):
        return "market_holiday"
    current_time = current.time()
    if current_time < MORNING_START:
        return "pre_open"
    if MORNING_START <= current_time < MORNING_END:
        return "morning_session"
    if MORNING_END <= current_time < AFTERNOON_START:
        return "lunch_break"
    if AFTERNOON_START <= current_time < AFTERNOON_END:
        return "afternoon_session"
    return "after_close"


def expected_settlement_date(now: datetime | None = None, calendar: dict[str, Any] | None = None) -> date | None:
    current = _as_datetime(now)
    today = current.date()
    session = get_market_session(current, calendar)
    if session in {"non_trading_day", "market_holiday"}:
        return previous_cn_a_share_trading_day(today + timedelta(days=1), calendar)
    if session == "after_close":
        return today
    return previous_cn_a_share_trading_day(today, calendar)


def can_settle_with_quote(
    quote_date: Any,
    now: datetime | None = None,
    calendar: dict[str, Any] | None = None,
) -> tuple[bool, str]:
    quote = _as_date(quote_date)
    if quote is None:
        return False, "no_quote"

    current = _as_datetime(now)
    today = current.date()
    session = get_market_session(current, calendar)
    expected = expected_settlement_date(current, calendar)

    if session in {"market_holiday", "non_trading_day"}:
        if expected and quote == expected:
            return True, "market_holiday_using_last_trading_day" if session == "market_holiday" else "using_last_trading_day"
        return False, "quote_stale"

    if session != "after_close" and is_cn_a_share_trading_day(today, calendar) and quote == today:
        return False, "market_not_closed"
    if expected and quote < expected:
        previous = previous_cn_a_share_trading_day(today, calendar) if session == "after_close" else None
        if session == "after_close" and previous and quote == previous:
            return False, "waiting_for_quote_update"
        return False, "quote_stale"
    if session == "after_close" and quote == today:
        return True, "settled_after_close"
    return True, "using_last_trading_day"


def settlement_status_from_quote(
    quote_date: Any,
    now: datetime | None = None,
    calendar: dict[str, Any] | None = None,
) -> dict[str, Any]:
    current = _as_datetime(now)
    quote = _as_date(quote_date)
    cal = calendar or load_cn_trading_calendar()
    session = get_market_session(current, cal)
    expected = expected_settlement_date(current, cal)
    can_settle, reason = can_settle_with_quote(quote, current, cal)
    is_holiday = session == "market_holiday"

    if quote is None:
        status = "no_quote"
        message = "当前报告缺少行情日期，无法确认结算状态。"
    elif reason == "market_not_closed":
        status = "waiting_for_close"
        message = "今日尚未收盘，暂不使用今日最终收盘价结算。"
    elif reason == "waiting_for_quote_update":
        status = "waiting_for_quote_update"
        message = "今日已收盘，但行情数据尚未更新到今日。"
    elif reason == "quote_stale":
        status = "quote_stale"
        message = "当前行情日期早于预期结算日，请检查数据源。"
    elif reason == "settled_after_close":
        status = "settled_after_close"
        message = "已使用今日收盘价结算。"
    elif reason == "market_holiday_using_last_trading_day":
        status = "market_holiday_using_last_trading_day"
        message = "当前为 A 股休市日，使用最近交易日完整日线。"
    else:
        status = "using_last_trading_day"
        message = "当前为非交易日或尚未收盘，使用最近交易日完整日线。"

    return {
        "can_settle": bool(can_settle),
        "market_session": session,
        "expected_settlement_date": expected.isoformat() if expected else "",
        "quote_date": quote.isoformat() if quote else "",
        "status": status,
        "message": message,
        "is_market_holiday": bool(is_holiday),
        "calendar_note": str(cal.get("calendar_note") or DEFAULT_CALENDAR_NOTE),
        "calendar_source": cal.get("source") or "unknown",
        "calendar_quality": cal.get("calendar_quality") or "unknown",
        "calendar_warnings": cal.get("warnings") if isinstance(cal.get("warnings"), list) else [],
    }
