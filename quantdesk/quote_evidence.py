"""Read-only quote identity, clock and freshness checks for a selected stock."""
from __future__ import annotations

import math
import re
from datetime import date, datetime, time, timedelta
from typing import Any, Mapping

from quantdesk.market_clock import to_shanghai_datetime

LIVE_SESSIONS = {"morning_session", "afternoon_session"}
# Our collection record may be written a moment before the provider assembles
# its own timestamp, so small clock skew is expected and tolerated. A clock
# hours away from the dated collection record is not a plausible quote clock.
CLOCK_SKEW_TOLERANCE = timedelta(seconds=120)
# Closing prints can arrive a few minutes after the 15:00 close.
CLOSING_SNAPSHOT_START, CLOSING_SNAPSHOT_END = time(14, 55), time(15, 10)


def quote_number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (ValueError, TypeError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def quote_datetime(value: Any) -> datetime | None:
    """Parse a provider datetime; a clock without a date is not a datetime."""
    text = str(value or "").strip()
    if not re.match(r"^\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}", text):
        return None
    try:
        return to_shanghai_datetime(datetime.fromisoformat(text.replace("Z", "+00:00")))
    except ValueError:
        return None


def quote_time_of_day(value: Any) -> time | None:
    """Parse a provider HH:MM[:SS] clock. A clock alone carries no date."""
    match = re.fullmatch(r"(\d{1,2}):(\d{2})(?::(\d{2}))?", str(value or "").strip())
    if not match:
        return None
    hour, minute, second = int(match.group(1)), int(match.group(2)), int(match.group(3) or 0)
    if hour > 23 or minute > 59 or second > 59:
        return None
    return time(hour, minute, second)


def provider_quote_datetime(row: Mapping[str, Any]) -> datetime | None:
    timestamp = quote_datetime(row.get("quote_timestamp")) or quote_datetime(row.get("quote_time"))
    if timestamp is not None:
        return timestamp
    return quote_datetime(str(row.get("quote_date") or "") + " " + str(row.get("quote_time") or ""))


def inspect_quote(
    row: Mapping[str, Any],
    *,
    symbol: str,
    now: datetime,
    session: str,
    expected_trade_date: date | None,
    max_age_seconds: float = 60.0,
) -> dict[str, Any]:
    """Keep an old/missing timestamp visible instead of replacing it with now.

    Some providers (e.g. the Sina spot snapshot) publish only a clock such as
    "15:05:09" with no date. When our own record carries a dated collection
    timestamp, the display date is derived from that record and reported as
    inferred -- never from now() or a file mtime of an unrelated file. The
    inference is labelled, and a clock far away from its own collection record
    is rejected instead of being accepted as a fresh quote.
    """
    current = to_shanghai_datetime(now)
    price = quote_number(row.get("price"))
    timestamp = provider_quote_datetime(row)
    collected = quote_datetime(row.get("updated_at"))
    timestamp_inferred = False
    if timestamp is None and collected is not None:
        clock = quote_time_of_day(row.get("quote_time"))
        if clock is not None:
            timestamp = datetime.combine(collected.date(), clock, tzinfo=collected.tzinfo)
            timestamp_inferred = True
    actual_symbol = str(row.get("symbol") or "").strip()
    source = str(row.get("source") or "").strip()
    price_valid = bool(price is not None and price > 0)
    reason_codes: list[str] = []
    status, label = "missing", "报价缺失"
    usable = False
    age = (current - timestamp).total_seconds() if timestamp else None
    # A provider that omits `available` still returned a priced row; only an
    # explicit False means the source says the security has no quote.
    if not price_valid or row.get("available") is False:
        reason_codes.append("quote_price_missing_or_invalid")
    elif actual_symbol != symbol:
        status, label = "invalid_identity", "报价证券不匹配"
        reason_codes.append("quote_symbol_mismatch")
    elif not source:
        status, label = "unverified_source", "报价来源未记录"
        reason_codes.append("quote_source_missing")
    elif timestamp is None:
        status, label = "unverified_time", "报价日期未核实（来源只给了时刻，且无采集时间）"
        reason_codes.append("quote_timestamp_missing")
    elif str(row.get("quote_date") or "").strip() not in {"", "nan", "None", timestamp.date().isoformat()}:
        status, label = "invalid_time", "报价日期与时间不一致"
        reason_codes.append("quote_date_timestamp_conflict")
    # A provider clock can run slightly ahead of the local clock, so a small
    # lead is tolerated; a clearly future date/time is still rejected.
    elif timestamp > current + CLOCK_SKEW_TOLERANCE:
        status, label = "invalid_time", "报价时间晚于当前时间"
        reason_codes.append("quote_timestamp_in_future")
    elif collected and collected > current + CLOCK_SKEW_TOLERANCE:
        status, label = "invalid_time", "采集时间晚于当前时间"
        reason_codes.append("quote_collection_in_future")
    elif collected and timestamp > collected + CLOCK_SKEW_TOLERANCE:
        # A quote clock ahead of our own dated collection record is not
        # plausible. The reverse direction is normal write-out latency, so an
        # older quote is still judged by the session rules below.
        status, label = "invalid_time", "报价时间晚于采集记录"
        reason_codes.append("quote_clock_after_collection")
    elif session in LIVE_SESSIONS:
        if timestamp.date() != current.date() or age is None or age > max_age_seconds:
            status, label = "stale", "报价已延迟，等待更新"
            reason_codes.append("quote_stale_during_session")
        else:
            status, label, usable = "live", "盘中报价", True
    elif session == "lunch_break":
        usable = timestamp.date() == current.date() and time(11, 25) <= timestamp.time() <= time(11, 31)
        status, label = ("session_snapshot", "午间休市快照") if usable else ("stale", "午间报价偏旧")
        if not usable:
            reason_codes.append("quote_not_latest_session_snapshot")
    elif expected_trade_date and timestamp.date() == expected_trade_date and (
            CLOSING_SNAPSHOT_START <= timestamp.time() <= CLOSING_SNAPSHOT_END):
        status, label, usable = "session_snapshot", "最近交易日收盘附近快照", True
        if timestamp_inferred:
            label = "最近交易日收盘附近快照（日期按采集记录推定）"
    else:
        status, label = "stale", "历史报价，等待更新"
        reason_codes.append("quote_not_latest_session_snapshot")
    if timestamp_inferred:
        reason_codes.append("quote_date_inferred_from_collection")
    basis = str(row.get("price_basis") or "").strip()
    raw_verified = basis == "raw"
    if price_valid and not raw_verified:
        reason_codes.append("quote_price_basis_unverified")
    if usable and timestamp_inferred:
        message = "报价只有时刻没有日期；显示日期按本地采集记录推定，未采信来源日期。"
    elif usable:
        message = "报价日期不从今天或文件修改时间补造；价格触及日线参考位仍需收盘确认。"
    else:
        message = "当前报价不能作为实时信号依据；日线参考和历史图仍可查看。"
    return {
        "status": status,
        "label": label,
        "price_valid": price_valid,
        "usable_latest_quote": usable,
        "comparable_to_raw_daily": usable and raw_verified,
        "price_basis": basis or "mixed_or_unknown",
        "quote_timestamp": timestamp.isoformat(timespec="seconds") if timestamp else "",
        "quote_timestamp_inferred": timestamp_inferred,
        "collection_timestamp": collected.isoformat(timespec="seconds") if collected else "",
        "age_seconds": round(age, 1) if age is not None else None,
        "checked_at": current.isoformat(timespec="seconds"),
        "source": source,
        "session": session,
        "market_active": session in LIVE_SESSIONS,
        "reason_codes": reason_codes,
        "message": message,
    }
