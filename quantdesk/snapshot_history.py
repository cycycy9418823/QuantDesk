"""History-only full-market five-minute snapshot collection.

This module is deliberately separate from the existing intraday minute cache.
It writes one immutable artifact per market slot under ``data/snapshots`` and
never writes a ``latest`` file or calls a downstream research/AI pipeline.
Network access is opt-in at the call boundary; callers should inject a root
and a provider in tests and dry runs.
"""

from __future__ import annotations

import csv
import hashlib
import inspect
import io
import json
import math
import os
import re
import time as time_module
import subprocess
from collections.abc import Callable, Iterable, Mapping, Sequence
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from quantdesk.interprocess_lock import interprocess_file_lock
from quantdesk.market_clock import is_cn_a_share_trading_day, load_cn_trading_calendar
from quantdesk.shared import normalize_symbol

SHANGHAI = ZoneInfo("Asia/Shanghai")
SNAPSHOT_RELATIVE_DIR = Path("data/snapshots")
UNIVERSE_RELATIVE_PATH = Path("data/universe/a_share_symbols.csv")
LOCK_RELATIVE_PATH = SNAPSHOT_RELATIVE_DIR / ".locks/collector.lock"
SNAPSHOT_COLUMNS = (
    "snapshot_date",
    "snapshot_time",
    "symbol",
    "name",
    "price",
    "pct_chg",
    "change",
    "volume",
    "amount",
    "open",
    "pre_close",
    "high",
    "low",
    "quote_date",
    "quote_time",
    "source",
    "fetched_at",
    "price_basis",
)
LEGACY_SNAPSHOT_COLUMNS = SNAPSHOT_COLUMNS[:-1]
_ALIASES = {
    "代码": "symbol",
    "股票代码": "symbol",
    "code": "symbol",
    "证券代码": "symbol",
    "名称": "name",
    "股票名称": "name",
    "name": "name",
    "最新价": "price",
    "trade": "price",
    "close": "price",
    "current_price": "price",
    "涨跌幅": "pct_chg",
    "changepercent": "pct_chg",
    "涨跌额": "change",
    "pricechange": "change",
    "成交量": "volume",
    "volume": "volume",
    "成交额": "amount",
    "amount": "amount",
    "今开": "open",
    "开盘": "open",
    "open": "open",
    "昨收": "pre_close",
    "settlement": "pre_close",
    "prev_close": "pre_close",
    "最高": "high",
    "high": "high",
    "最低": "low",
    "low": "low",
    "时间戳": "quote_time",
    "time": "quote_time",
    "ticktime": "quote_time",
    "日期": "quote_date",
    "date": "quote_date",
}

DEFAULT_WINDOWS: tuple[tuple[time, time], ...] = (
    (time(9, 30), time(11, 30)),
    (time(13, 0), time(15, 0)),
)

# Bounded provider budget.  A 5-minute slot must never spend its whole window
# inside one provider chain: 2026-09-10 showed sina.spot_direct exhausting its
# retries during ReadTimeouts and the slot landing outside its own window.
PROVIDER_TOTAL_BUDGET_SECONDS = 120.0
SINA_DIRECT_PAGE_LIMIT = 80
SINA_DIRECT_ATTEMPTS = 2
# Full-A needs about 56 serial pages. Reserve enough of the existing 150-second
# worker allowance for normal responses, leaving time for the fallback sources.
SINA_DIRECT_MAX_SECONDS = 90.0
# Per-request ceiling inside the pagination; the effective timeout is always the
# smaller of this and whatever is left of the absolute deadline.
SINA_DIRECT_REQUEST_TIMEOUT_SECONDS = 8.0
SINA_HQ_BATCH_SIZE = 100
SINA_HQ_MAX_SECONDS = 90.0
SINA_HQ_REQUEST_TIMEOUT_SECONDS = 5.0
# Every source declares the window it must have in hand before it may be
# started.  akshare calls cannot be cancelled once begun.  The Sina pagination
# cannot be cancelled mid-request either, but it takes the leftover window as an
# argument and refuses the call itself when that window is too small, so it is
# not listed here.
AKSHARE_MAX_SECONDS = 6.0
PROVIDER_MINIMUM_BUDGET_SECONDS: dict[str, float] = {
    "eastmoney.spot_ak": AKSHARE_MAX_SECONDS,
    "sina.spot_ak": AKSHARE_MAX_SECONDS,
}
# Kept under its original name: these are the uncancellable entries.
UNCANCELLABLE_PROVIDER_MINIMUM_SECONDS = PROVIDER_MINIMUM_BUDGET_SECONDS
PROVIDER_BUDGET_TOO_SMALL_SOURCE = "provider_budget_exhausted"
# The AkShare-backed spot adapters take no ``budget_seconds`` and cannot be
# cancelled once started, so under a finite round budget they are explicitly
# skipped with evidence instead of being run unbounded (DSR-06).  The Sina
# pagination is budget-aware and is the only boundable member of the chain.
UNBOUNDED_PROVIDER_NAMES = frozenset({"eastmoney.spot_ak", "sina.spot_ak"})


class SnapshotProviderError(RuntimeError):
    """Provider-chain failure carrying per-source stage and timing evidence.

    The message stays ``snapshot_providers_failed:<codes>`` so existing
    reason codes and warnings are unchanged; ``provider_errors`` adds the
    stage-by-stage detail that used to be discarded.
    """

    def __init__(self, message: str, *, provider_errors: Sequence[Mapping[str, Any]]) -> None:
        super().__init__(message)
        self.provider_errors: list[dict[str, Any]] = [dict(item) for item in provider_errors]


def _elapsed_ms(started: float, clock: Callable[[], float]) -> int:
    return max(0, int(round((clock() - started) * 1000)))


def _sanitize_error_text(value: Any, limit: int = 160) -> str:
    """Keep the failure class and message but strip anything key-shaped."""

    text = " ".join(str(value or "").split())
    for marker in ("key=", "token=", "sendkey=", "password=", "secret="):
        position = text.lower().find(marker)
        while position >= 0:
            end = text.find("&", position)
            end = len(text) if end < 0 else end
            text = text[:position] + marker + "<redacted>" + text[end:]
            position = text.lower().find(marker, position + len(marker) + 10)
    if len(text) > limit:
        text = text[: limit - 3] + "..."
    return text


def _as_shanghai(value: datetime | None) -> datetime:
    current = value or datetime.now(tz=SHANGHAI)
    if current.tzinfo is None:
        return current.replace(tzinfo=SHANGHAI)
    return current.astimezone(SHANGHAI)


def expected_snapshot_slots(
    *,
    include_close: bool = False,
    windows: Sequence[tuple[time, time]] = DEFAULT_WINDOWS,
    interval_minutes: int = 5,
) -> list[str]:
    """Return regular five-minute start labels for each A-share session.

    Session end labels are excluded by default, yielding 48 regular labels
    (09:30..11:25 and 13:00..14:55).  ``include_close`` adds an optional
    15:00 close sample, making the endpoint convention explicit in metadata.
    """

    interval = max(1, int(interval_minutes))
    labels: list[str] = []
    for index, (start, end) in enumerate(windows):
        current = datetime.combine(date(2000, 1, 1), start)
        final = datetime.combine(date(2000, 1, 1), end)
        while current < final:
            labels.append(current.strftime("%H%M"))
            current += timedelta(minutes=interval)
        if include_close and index == len(windows) - 1:
            labels.append(final.strftime("%H%M"))
    return list(dict.fromkeys(labels))


def _slot_for(now: datetime, *, include_close: bool) -> str | None:
    current = now.time().replace(second=0, microsecond=0)
    regular = expected_snapshot_slots(include_close=include_close)
    # A caller may invoke the one-shot collector a few seconds after a slot.
    # Floor to the five-minute label, but never silently cross the lunch break.
    minute = (current.minute // 5) * 5
    candidate = f"{current.hour:02d}{minute:02d}"
    return candidate if candidate in regular else None


def _read_trading_day(root: Path, target: date) -> tuple[bool, str, list[str]]:
    warnings: list[str] = []
    for path in (
        root / "data/calendar/a_share_trading_calendar.csv",
        root / "config/trading_calendar_cn.yaml",
    ):
        if not path.is_file():
            continue
        try:
            calendar = load_cn_trading_calendar(path)
            if calendar["source"] == "csv":
                if target.isoformat() in calendar["is_open_by_date"]:
                    return bool(calendar["is_open_by_date"][target.isoformat()]), "csv", warnings
                warnings.append("calendar_csv_target_missing")
                continue
            warnings.append("calendar_legacy_yaml")
            return is_cn_a_share_trading_day(target, calendar), "legacy_yaml", warnings
        except (OSError, ValueError, TypeError, KeyError):
            warnings.append("calendar_unreadable")
    return target.weekday() < 5, "unavailable", [*warnings, "calendar_unavailable"]


def _clean_value(value: Any) -> Any:
    if value is None or str(value).strip().lower() in {
        "nan", "nat", "<na>", "none", "null", "inf", "-inf", "infinity", "-infinity",
    }:
        return ""
    return value


# A slot is relabelled ``partial`` only by evidence against the slot itself: an
# unknown roster, a roster gap, an unverified source, provider timestamps that
# are not usable, or a fetch that landed outside the slot.  A missing price is a
# counted warning (``snapshot_price_missing:<n>`` plus ``price_missing_symbols``)
# and no longer relabels the slot: 2026-09-21 lost 12 of 5564 tickers to a zero
# price while every symbol was present with a usable timestamp, and filing all
# 48 slots as partial hid a fully collected day (S3.4-r).
SLOT_BLOCKING_WARNINGS = frozenset({
    "snapshot_symbols_missing",
    "universe_unavailable",
    "snapshot_source_unknown",
    "fetch_completed_outside_requested_slot",
    "fetch_outside_slot",
})
SLOT_BLOCKING_WARNING_PREFIXES = ("snapshot_quote_time_",)


def _slot_blocked_by_warnings(warnings: Iterable[str]) -> bool:
    """True when a warning is evidence against this slot, not just its quotes."""

    for warning in warnings:
        if warning in SLOT_BLOCKING_WARNINGS or warning.startswith(SLOT_BLOCKING_WARNING_PREFIXES):
            return True
    return False


def _valid_price(value: Any) -> bool:
    try:
        price = float(value)
        return math.isfinite(price) and price > 0
    except (ValueError, TypeError):
        return False


def _quote_field_quality(rows: Sequence[Mapping[str, Any]], trade_date: str) -> dict[str, Any]:
    """Field evidence independent of archive/slot completion conventions."""

    missing_date = sum(not str(row.get("quote_date") or "").strip() for row in rows)
    wrong_date = sum(bool(str(row.get("quote_date") or "").strip())
                     and str(row.get("quote_date")) != trade_date for row in rows)
    missing_time = sum(not str(row.get("quote_time") or "").strip() for row in rows)
    unknown_basis = sum(str(row.get("price_basis") or "").lower() != "raw" for row in rows)
    missing_price = sum(not _valid_price(row.get("price")) for row in rows)
    return {
        "quote_date_missing_count": missing_date,
        "quote_date_mismatch_count": wrong_date,
        "quote_time_missing_count": missing_time,
        "price_basis_unverified_count": unknown_basis,
        "price_missing_count": missing_price,
        "quality_status": "partial" if not rows or any((missing_date, wrong_date, missing_time,
                                                        unknown_basis, missing_price)) else "complete",
        "quote_date_basis": "provider_date_or_empty",
        "price_basis_contract": "explicit_provider_declaration_or_empty",
    }


def _load_symbols(root: Path, symbols: Iterable[Any] | None) -> list[str]:
    source: Iterable[Any]
    if symbols is not None:
        source = symbols
    else:
        path = root / UNIVERSE_RELATIVE_PATH
        if not path.is_file():
            return []
        try:
            with path.open(encoding="utf-8-sig", newline="") as handle:
                source = [row.get("symbol") or row.get("代码") or row.get("code") for row in csv.DictReader(handle)]
        except (OSError, csv.Error):
            return []
    normalized = {normalize_symbol(item) for item in source}
    return sorted(item for item in normalized if len(item) == 6 and item.isdigit())


def _records(payload: Any) -> list[Mapping[str, Any]]:
    if payload is None:
        return []
    if hasattr(payload, "to_dict"):
        try:
            payload = payload.to_dict("records")
        except TypeError:
            payload = payload.to_dict()
    if isinstance(payload, Mapping):
        for key in ("rows", "data", "records", "quotes"):
            if key in payload:
                return _records(payload[key])
        return [payload]
    if isinstance(payload, Sequence) and not isinstance(payload, (str, bytes, bytearray)):
        return [row for row in payload if isinstance(row, Mapping)]
    return []


def normalize_snapshot_rows(
    payload: Any,
    *,
    source: str,
    fetched_at: datetime,
    trade_date: str,
    slot_time: str,
) -> list[dict[str, Any]]:
    """Normalize provider records without manufacturing missing quote values."""

    stamp = _as_shanghai(fetched_at).isoformat(timespec="seconds")
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in _records(payload):
        mapped: dict[str, Any] = {}
        for key, value in raw.items():
            name = _ALIASES.get(str(key).strip(), str(key).strip())
            mapped[name] = _clean_value(value)
        symbol = normalize_symbol(mapped.get("symbol"))
        if len(symbol) != 6 or not symbol.isdigit() or symbol in seen:
            continue
        seen.add(symbol)
        row: dict[str, Any] = {
            "snapshot_date": trade_date,
            "snapshot_time": slot_time,
            "symbol": symbol,
            "name": str(mapped.get("name") or ""),
            "price": mapped.get("price", ""),
            "pct_chg": mapped.get("pct_chg", ""),
            "change": mapped.get("change", ""),
            "volume": mapped.get("volume", ""),
            "amount": mapped.get("amount", ""),
            "open": mapped.get("open", ""),
            "pre_close": mapped.get("pre_close", ""),
            "high": mapped.get("high", ""),
            "low": mapped.get("low", ""),
            "quote_date": str(mapped.get("quote_date") or ""),
            "quote_time": str(mapped.get("quote_time") or ""),
            "source": source,
            "fetched_at": stamp,
            "price_basis": str(mapped.get("price_basis") or ""),
        }
        rows.append(row)
    rows.sort(key=lambda row: row["symbol"])
    return rows


SINA_DIRECT_BUDGET_REFUSED_SOURCE = "sina.spot_direct:budget_below_minimum"


def _sina_direct_fetch(
    *,
    clock: Callable[[], float] = time_module.monotonic,
    budget_seconds: float = SINA_DIRECT_MAX_SECONDS,
) -> tuple[list[dict[str, Any]], str]:
    """Fetch Sina's paginated full-A spot endpoint on explicit demand.

    Pagination is bounded three times: by ``SINA_DIRECT_PAGE_LIMIT`` pages, by an
    absolute wall-clock deadline, and by giving every single request a timeout
    equal to whatever is left of that deadline. A shorter caller allowance is
    respected rather than rejected merely because it is below the default cap.
    """

    import requests

    if budget_seconds <= 0:
        return [], SINA_DIRECT_BUDGET_REFUSED_SOURCE

    url = "https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/Market_Center.getHQNodeData"
    rows: list[dict[str, Any]] = []
    # The endpoint's own cap survives a larger chain budget: this source is
    # never allowed to keep the whole shared window for itself.
    deadline = clock() + min(max(0.0, budget_seconds), SINA_DIRECT_MAX_SECONDS)
    pages_read = 0
    for page in range(1, SINA_DIRECT_PAGE_LIMIT + 1):
        now = clock()
        if now >= deadline:
            raise TimeoutError(f"sina.spot_direct:page_budget_exhausted:{pages_read}")
        payload: Any = None
        for attempt in (1, SINA_DIRECT_ATTEMPTS):
            if attempt > 1:
                # Never start a retry the remaining budget cannot pay for.
                if clock() >= deadline:
                    raise TimeoutError(f"sina.spot_direct:page_budget_exhausted:{pages_read}")
                time_module.sleep(0.3)
            # The allowance is recomputed per request, so a slow first attempt
            # cannot hand the retry the budget the first one already spent.
            left = deadline - clock()
            if left <= 0:
                raise TimeoutError(f"sina.spot_direct:page_budget_exhausted:{pages_read}")
            response = requests.get(
                url,
                params={"page": page, "num": 100, "sort": "symbol", "asc": 1, "node": "hs_a", "symbol": "", "_s_r_a": "init"},
                headers={"User-Agent": "Mozilla/5.0 QuantDesk snapshot collector"},
                timeout=max(0.1, min(SINA_DIRECT_REQUEST_TIMEOUT_SECONDS, left)),
            )
            response.raise_for_status()
            payload = response.json()
            if not isinstance(payload, list):
                raise ValueError(f"sina.spot_direct:invalid_page:{page}")
            if len(payload) == 100 or attempt == SINA_DIRECT_ATTEMPTS:
                break
        if not payload:
            break
        pages_read += 1
        # This live quote endpoint supplies unadjusted exchange prices. Keep
        # date/time exactly as supplied (ticktime alone does not prove a date).
        rows.extend(dict(item, price_basis="raw") for item in payload if isinstance(item, Mapping))
        if len(payload) < 100:
            break
    if not rows:
        raise RuntimeError("sina.spot_direct:empty")
    return rows, "sina.spot_direct"


# Only an explicit "too many requests" status proves rate limiting.  429 is the
# standard code; a provider-specific non-standard code (e.g. Sina's 456) is not
# generalised here - it keeps its raw status in the evidence and falls through
# to the generic bucket so the reason is not over-inferred.
RATE_LIMITED_HTTP_STATUSES = frozenset({429})
# 401/403 prove the caller was refused but not why (hard block vs throttling),
# so they get their own label instead of being folded into rate limiting.
ACCESS_DENIED_HTTP_STATUSES = frozenset({401, 403})


def _http_status(exc: BaseException) -> int | None:
    """Best-effort HTTP status from a requests-style provider exception."""

    for candidate in (
        getattr(getattr(exc, "response", None), "status_code", None),
        getattr(exc, "status", None),
        getattr(exc, "code", None),
    ):
        try:
            value = int(candidate)
        except (TypeError, ValueError):
            continue
        if value > 0:
            return value
    return None


def _response_format_hint(exc: BaseException) -> str:
    """Flag a response that arrived as HTML instead of the expected JSON.

    Two real spellings must be covered, not just the stdlib stand-in:

    * ``json.JSONDecodeError`` / ``requests`` expose the parsed body as ``.doc``;
    * AkShare's Sina spot adapter uses ``akshare.utils.demjson`` whose
      ``JSONDecodeError`` carries no body but says
      ``Can not decode value starting with character '<'`` - the exact text
      captured for the 2026-09-11 14:55 A0 failure.

    Either way the format is labelled ``html_or_non_json`` rather than reported
    as a bare parse error.
    """

    if type(exc).__name__ not in {"JSONDecodeError", "UnicodeDecodeError"}:
        return ""
    body = str(getattr(exc, "doc", "") or "")
    if body.lstrip().startswith("<"):
        return "html_or_non_json"
    text = str(exc)
    if "starting with character '<'" in text or 'starting with character "<"' in text:
        return "html_or_non_json"
    return ""


def _failure_stage(exc: BaseException) -> str:
    """Classify a provider failure into the stage that actually failed.

    Names are used first because the requests hierarchy (``ReadTimeout``,
    ``ConnectionError``...) does not map onto the builtin exception classes.
    An explicit throttling status takes precedence over the generic
    ``request`` bucket so the evidence distinguishes blocking from bugs.
    """

    status = _http_status(exc)
    if status in RATE_LIMITED_HTTP_STATUSES:
        return "rate_limited"
    if status in ACCESS_DENIED_HTTP_STATUSES:
        return "access_denied"
    name = type(exc).__name__
    if name in {"ConnectionError", "ConnectTimeout", "NewConnectionError", "ProtocolError"}:
        return "connect"
    if name in {"ReadTimeout", "Timeout", "timeout", "TimeoutError"}:
        return "timeout"
    if name in {"JSONDecodeError", "UnicodeDecodeError", "ValueError", "KeyError", "TypeError"}:
        return "parse"
    if isinstance(exc, ConnectionError):
        return "connect"
    if isinstance(exc, TimeoutError):
        return "timeout"
    if isinstance(exc, (UnicodeDecodeError, KeyError)):
        return "parse"
    return "request"


def _invoke_provider(call: Callable[..., Any], budget_seconds: float) -> Any:
    """Hand the remaining window to providers that declare one.

    A budget-aware provider (the Sina pagination) must be told how much of the
    chain's deadline is left, or it will happily spend its own default window
    after the caller's budget is already gone.  Providers that take no budget -
    including test doubles - keep being called with no arguments.
    """

    try:
        parameters = inspect.signature(call).parameters
    except (TypeError, ValueError):
        return call()
    if "budget_seconds" in parameters:
        return call(budget_seconds=budget_seconds)
    return call()


def _provider_is_boundable(name: str) -> bool:
    """Whether the named chain source can be told how much time it may use.

    Budget-awareness is a property of the source implementation, not of whatever
    callable a test patches in: ``sina.spot_direct`` is the only source that
    accepts a ``budget_seconds`` window and is trusted to respect it.  The
    AkShare-backed adapters cannot be cancelled once begun, so under a finite
    chain deadline they are explicitly skipped with evidence instead of being
    started unbounded (DSR-06).
    """

    return name not in UNBOUNDED_PROVIDER_NAMES


def _provider_stage(
    name: str,
    call: Callable[[], Any],
    *,
    clock: Callable[[], float] = time_module.monotonic,
    budget_seconds: float | None = None,
) -> tuple[Any, dict[str, Any]]:
    """Run one provider call, returning its result plus stage evidence.

    On failure the evidence records the source, the stage that failed, the
    exception type and a sanitized message, and how long the call took.
    """

    started = clock()
    try:
        value = call() if budget_seconds is None else _invoke_provider(call, budget_seconds)
    except Exception as exc:  # noqa: BLE001 - every provider failure is reported
        evidence = {
            "source": name,
            "stage": _failure_stage(exc),
            "outcome": "failed",
            "error_type": type(exc).__name__,
            "message": _sanitize_error_text(exc),
            "duration_ms": _elapsed_ms(started, clock),
        }
        status = _http_status(exc)
        if status is not None:
            evidence["http_status"] = status
        hint = _response_format_hint(exc)
        if hint:
            evidence["response_format"] = hint
        if isinstance(exc, SnapshotProviderError):
            # Do not launch another source while a timed-out child is still
            # unaccounted for. Otherwise retain its PID/deadline evidence when
            # the alternate source succeeds.
            if any(item.get("process_reaped") is False for item in exc.provider_errors):
                raise
            return ProviderRows([], source=name, provider_errors=exc.provider_errors), evidence
        return None, evidence
    return value, {
        "source": name,
        "stage": "request",
        "outcome": "ok",
        "error_type": "",
        "message": "",
        "duration_ms": _elapsed_ms(started, clock),
    }


class ProviderRows(list):
    """Provider rows carrying the chain evidence that produced them.

    The public contract stays ``(rows, source)`` and callers still receive a real
    list (``isinstance(x, list)``, ``len``, unpacking).  The difference is that a
    fallback which succeeds after an earlier source failed keeps that failure
    attached - here and in the archive's ``failure_detail`` - instead of dropping
    it the moment coverage completes.
    """

    def __init__(
        self,
        rows: Iterable[Any] = (),
        *,
        source: str = "",
        warnings: Sequence[str] = (),
        provider_errors: Sequence[Mapping[str, Any]] = (),
    ) -> None:
        super().__init__(rows)
        self.source = str(source or "")
        self.warnings = [str(item) for item in warnings]
        self.provider_errors = [dict(item) for item in provider_errors]

    def __repr__(self) -> str:
        # The evidence has to be visible in logs and tracebacks, not only as an
        # attribute nobody prints.
        return (
            f"ProviderRows(source={self.source!r}, rows={len(self)}, "
            f"warnings={self.warnings!r}, provider_errors={self.provider_errors!r})"
        )


def _evidence_rows(
    rows: Sequence[Mapping[str, Any]],
    *,
    source: str,
    warnings: Sequence[str],
    provider_errors: Sequence[Mapping[str, Any]],
) -> ProviderRows:
    """The "no clean provider" rows: data plus the chain evidence behind it.

    The public ``(rows, source)`` contract is kept - callers index ``[1]`` for
    the source - while the failures that preceded the data stay attached.
    """

    return ProviderRows(
        rows, source=source, warnings=warnings, provider_errors=provider_errors,
    )


def _sina_hq_symbol_key(value: Any) -> str | None:
    """Keep the requested exchange when adapting the existing HQ client."""
    text = str(value or "").strip().lower()
    if re.fullmatch(r"(?:sh|sz|bj)\d{6}", text):
        return text
    suffix = re.fullmatch(r"(\d{6})\.(sh|sz|bj)", text)
    if suffix:
        return suffix.group(2) + suffix.group(1)
    if not re.fullmatch(r"\d{6}", text):
        return None
    if text.startswith(("4", "8", "92")):
        return "bj" + text
    if text.startswith(("6", "900")):
        return "sh" + text
    if text.startswith(("000", "001", "002", "003", "200", "300", "301", "302")):
        return "sz" + text
    return None


def _sina_hq_fetch(
    symbols: Sequence[str],
    *,
    clock: Callable[[], float] = time_module.monotonic,
    budget_seconds: float = SINA_HQ_MAX_SECONDS,
    batch_size: int | None = None,
) -> tuple[ProviderRows, str]:
    """Use the already supported dated HQ source, one bounded batch at a time."""
    source = "sina.hq_batch"
    deadline = clock() + min(SINA_HQ_MAX_SECONDS, max(0.0, budget_seconds))
    keys = list(dict.fromkeys(key for value in symbols if (key := _sina_hq_symbol_key(value))))
    rows: list[dict[str, Any]] = []
    warnings: list[str] = []
    evidence: list[dict[str, Any]] = []
    if len(keys) < len(set(symbols)):
        warnings.append("sina.hq_batch:unrecognized_or_duplicate_request_identity")
    if keys and clock() < deadline:
        from quantdesk.data_providers import fetch_spot_sina_batch

        width = max(1, min(500, int(batch_size or SINA_HQ_BATCH_SIZE)))
        for start in range(0, len(keys), width):
            remaining = deadline - clock()
            if remaining <= 0:
                break
            chunk = keys[start:start + width]
            result = None
            for attempt in (1, 2):
                remaining = deadline - clock()
                if remaining <= 0:
                    break
                result = fetch_spot_sina_batch(
                    chunk, batch_size=len(chunk),
                    timeout=min(SINA_HQ_REQUEST_TIMEOUT_SECONDS, remaining),
                )
                if result.rows or not result.provider_errors:
                    break
                evidence.append({
                    "source": source, "stage": "request", "outcome": "failed",
                    "batch_start": start, "attempt": attempt, "error_type": "BatchRequestError",
                    "message": "; ".join(_sanitize_error_text(value) for value in result.provider_errors),
                })
                warnings.append(f"sina.hq_batch:request_failed:{start}:attempt_{attempt}")
            if result is None:
                break
            received: set[str] = set()
            rejected = 0
            for raw in result.rows:
                # The common parser is permissive. Only this batch's complete
                # provider identities may become rows in its snapshot.
                key = str(raw.get("symbol") or "").strip().lower()
                if key not in chunk or key in received:
                    rejected += 1
                    continue
                quote_date = str(raw.get("date") or "").strip()
                quote_time = str(raw.get("time") or "").strip()
                try:
                    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", quote_date) or not re.fullmatch(r"\d{2}:\d{2}:\d{2}", quote_time):
                        raise ValueError("missing_provider_datetime")
                    datetime.fromisoformat(f"{quote_date}T{quote_time}")
                except ValueError:
                    rejected += 1
                    continue
                row = dict(raw, symbol=key[2:], date=quote_date, time=quote_time)
                # HQ volumes are shares and amounts are yuan already. Its
                # movement fields are derived only from real positive prices.
                try:
                    price, previous = float(raw.get("price")), float(raw.get("prev_close"))
                    if all(math.isfinite(value) and value > 0 for value in (price, previous)):
                        row.update(change=price - previous, pct_chg=(price / previous - 1.0) * 100.0)
                except (TypeError, ValueError):
                    pass
                received.add(key)
                rows.append(row)
            missing = len(set(chunk) - received)
            errors = [_sanitize_error_text(value) for value in result.provider_errors]
            warnings.extend(f"sina.hq_batch:{_sanitize_error_text(value)}" for value in result.warnings)
            if errors or rejected or missing:
                warnings.append(f"sina.hq_batch:batch_incomplete:{start}:{len(received)}/{len(chunk)}")
                evidence.append({
                    "source": source, "stage": "batch", "outcome": "partial" if received else "failed",
                    "batch_start": start, "requested_count": len(chunk), "returned_count": len(received),
                    "rejected_count": rejected, "missing_count": missing,
                    "error_type": "BatchIncomplete", "message": "; ".join(errors) or "missing_or_invalid_quote",
                })
    if keys and clock() >= deadline:
        warnings.append("sina.hq_batch:budget_exhausted")
        evidence.append({"source": source, "stage": "timeout", "outcome": "budget_exhausted",
                         "error_type": "BudgetExhausted", "message": "hq_shared_deadline_exhausted",
                         "returned_count": len(rows), "requested_count": len(keys)})
    return ProviderRows(rows, source=source, warnings=warnings, provider_errors=evidence), source


def resilient_network_fetch(
    symbols: Sequence[str],
    *,
    budget_seconds: float = PROVIDER_TOTAL_BUDGET_SECONDS,
    clock: Callable[[], float] = time_module.monotonic,
    primary: Callable[..., Any] | None = None,
    secondary: Callable[..., Any] | None = None,
    trade_date: str = "",
) -> tuple[ProviderRows, str]:
    """Try two bounded quote endpoints before archiving one real capture.

    HQ receives at most 30 seconds and at most one third of the shared budget;
    empty, failed or incomplete HQ responses leave the rest for the list
    endpoint. Responses are never merged across capture times. Every failed
    attempt remains attached even when the alternate endpoint succeeds.
    """

    allowance = min(150.0, max(0.0, float(budget_seconds)))
    deadline = clock() + allowance
    expected = {normalize_symbol(value) for value in symbols}
    evidence: list[dict[str, Any]] = []
    warnings: list[str] = []
    best: tuple[list[Mapping[str, Any]], str] = ([], "sina.hq_batch")
    best_score = (-1, -1)

    def hq(*, budget_seconds: float) -> Any:
        return _sina_hq_fetch(symbols, clock=clock, budget_seconds=budget_seconds, batch_size=500)

    def direct(*, budget_seconds: float) -> Any:
        return _sina_direct_fetch(clock=clock, budget_seconds=budget_seconds)

    sources = (("sina.hq_batch", primary or hq), ("sina.spot_direct", secondary or direct))
    for index, (name, fetch) in enumerate(sources):
        remaining = max(0.0, deadline - clock())
        window = min(30.0, allowance / 3.0, remaining) if index == 0 else remaining
        if window <= 0:
            evidence.append({"source": name, "stage": "skipped", "outcome": "budget_exhausted",
                             "error_type": "BudgetExhausted", "message": "shared_snapshot_deadline"})
            continue
        result, stage = _provider_stage(name, fetch, clock=clock, budget_seconds=window)
        stage["budget_seconds"] = window
        payload, source, notices, details = _fetch_result(result) if result is not None else ([], name, [], [])
        records = _records(payload)
        observed = {normalize_symbol(row.get("symbol") or row.get("code") or row.get("代码"))
                    for row in records}
        coverage = len(expected & observed) if expected else len(observed)
        usable = set()
        stale_dates = 0
        for row in records:
            code = normalize_symbol(row.get("symbol") or row.get("code") or row.get("代码"))
            quoted_date = str(row.get("quote_date") or row.get("date") or row.get("日期") or "")
            stale = bool(trade_date and quoted_date and quoted_date != trade_date)
            stale_dates += int(stale)
            if not stale and _valid_price(row.get("price", row.get("trade", row.get("最新价")))):
                usable.add(code)
        usable_coverage = len(expected & usable) if expected else len(usable)
        stage.update(row_count=len(records), coverage_count=coverage, expected_symbol_count=len(expected),
                     usable_quote_count=usable_coverage, non_current_quote_date_count=stale_dates)
        if records and not usable:
            stage.update(outcome="unusable", error_type="NoUsableQuotes", message="no_positive_current_or_undated_quote")
        elif records and (not expected or expected <= observed):
            stage["outcome"] = "ok"
        elif records:
            stage.update(outcome="partial", error_type="IncompleteCoverage",
                         message=f"returned {coverage}/{len(expected)} requested identities")
        elif stage.get("outcome") == "ok":
            stage.update(outcome="empty", error_type="EmptyResponse", message="provider_returned_no_usable_rows")
        if stage.get("duration_ms", 0) / 1000.0 > window or clock() > deadline:
            stage.update(outcome="overrun", error_type="BudgetExhausted", message="shared_snapshot_deadline")
        evidence.extend([stage, *details])
        warnings.extend(notices)
        score = (usable_coverage, coverage)
        if records and score > best_score:
            best, best_score = (records, source or name), score
        if stage["outcome"] == "ok":
            break
        warnings.append(f"{name}:{stage['outcome']}:{coverage}/{len(expected)}")
    rows, source = best
    return ProviderRows(rows, source=source, warnings=warnings, provider_errors=evidence), source


def default_network_fetch(
    symbols: Sequence[str] = (),
    *,
    clock: Callable[[], float] = time_module.monotonic,
    budget_seconds: float | None = PROVIDER_TOTAL_BUDGET_SECONDS,
) -> Any:
    """Check coverage before accepting a provider's pagination as complete.

    Keep the best real partial response if all providers remain incomplete;
    no data is invented or merged across capture times.  Every source, retry and
    page runs against one absolute deadline: the leftover window is handed to
    budget-aware providers, a source whose declared minimum does not fit is
    never started, and a source that returns late is reported as an overrun.
    The failure evidence of every source is preserved even when a later source
    succeeds.
    """

    failures: list[str] = []
    provider_errors: list[dict[str, Any]] = []
    expected = set(symbols)
    best: tuple[Any, str] | None = None
    best_coverage = -1
    started = clock()
    deadline = None if budget_seconds is None else started + max(0.0, budget_seconds)
    # Even the process-bounded mode must not hand HQ a new full budget after
    # slow pagination. Leave the existing 150-second worker time to serialize.
    hq_deadline = started + PROVIDER_TOTAL_BUDGET_SECONDS if deadline is None else deadline
    below_minimum_skips = 0

    def hq(*, budget_seconds: float = SINA_HQ_MAX_SECONDS) -> Any:
        return _sina_hq_fetch(
            symbols, clock=clock,
            budget_seconds=min(budget_seconds, max(0.0, hq_deadline - clock())),
        )

    def consider(payload: Any, source: str) -> bool:
        nonlocal best, best_coverage
        records = _records(payload)
        observed = {normalize_symbol(row.get("symbol") or row.get("code") or row.get("代码")) for row in records}
        coverage = len(expected & observed) if expected else len(observed)
        if records and coverage > best_coverage:
            best, best_coverage = (payload, source), coverage
        if records and (not expected or expected <= observed):
            return True
        failures.append(f"{source}:incomplete:{coverage}/{len(expected)}")
        return False

    chain: tuple[tuple[str, Callable[[], Any]], ...] = (
        ("sina.spot_direct", _sina_direct_fetch),
        *((("sina.hq_batch", hq),) if symbols else ()),
        ("eastmoney.spot_ak", lambda: _akshare_provider("fetch_spot_eastmoney")),
        ("sina.spot_ak", lambda: _akshare_provider("fetch_spot_sina")),
    )
    for name, call in chain:
        minimum = PROVIDER_MINIMUM_BUDGET_SECONDS.get(name, 0.0)
        now = clock()
        if deadline is not None and now >= deadline:
            # The round's window is already gone: every remaining source is
            # unattempted and the deadline is the reason, not the source's
            # boundability.  This is a terminal round, so the chain raises.
            provider_errors.append({
                "source": name, "stage": "skipped", "outcome": "budget_exhausted",
                "error_type": "BudgetExhausted", "message": "provider_budget_exhausted", "duration_ms": 0,
            })
            failures.append(f"{name}:budget_exhausted")
            continue
        # A source that cannot accept the chain's budget cannot be stopped by the
        # chain's deadline.  Starting it inside a finite-budget round lets it
        # consume time the round no longer has (DSR-06), so it is explicitly
        # skipped with evidence instead of being run unbounded.  In explicit
        # unbounded mode (budget_seconds=None) it is called normally.
        if deadline is not None and not _provider_is_boundable(name):
            provider_errors.append({
                "source": name, "stage": "skipped", "outcome": "budget_exhausted",
                "error_type": "UnboundedProvider",
                "message": "provider_unbounded_skipped_in_finite_budget",
                "duration_ms": 0,
            })
            failures.append(f"{name}:unbounded_source_skipped")
            below_minimum_skips += 1
            continue
        remaining = None if deadline is None else deadline - now
        if remaining is not None and remaining < minimum:
            # A source that cannot fit in what is left is never started.
            provider_errors.append({
                "source": name, "stage": "skipped", "outcome": "budget_exhausted",
                "error_type": "BudgetExhausted",
                "message": f"provider_budget_below_minimum:{round(remaining, 3)}<{minimum}",
                "duration_ms": 0,
            })
            failures.append(f"{name}:budget_exhausted")
            below_minimum_skips += 1
            continue
        result, evidence = _provider_stage(name, call, clock=clock, budget_seconds=remaining)
        payload = result[0] if isinstance(result, tuple) and len(result) == 2 else result
        reported_source = str(result[1]) if isinstance(result, tuple) and len(result) == 2 else ""
        if isinstance(payload, ProviderRows):
            failures.extend(payload.warnings)
            provider_errors.extend(dict(item) for item in payload.provider_errors)
        if evidence["outcome"] == "ok" and reported_source == SINA_DIRECT_BUDGET_REFUSED_SOURCE:
            # A source that refused the window it was handed is not a source
            # that answered with nothing: it never ran, and says so.
            evidence.update(
                outcome="budget_exhausted", error_type="BudgetExhausted",
                message=f"provider_budget_below_minimum:{round(remaining, 3)}",
            )
            provider_errors.append(evidence)
            failures.append(f"{name}:budget_exhausted")
            below_minimum_skips += 1
            continue
        # A provider that returns is not automatically a provider that kept its
        # budget: re-check the elapsed time against what it was allowed.
        if (
            evidence["outcome"] == "ok"
            and remaining is not None
            and (evidence["duration_ms"] / 1000.0) > remaining
        ):
            evidence.update(
                outcome="budget_overrun",
                message=f"provider_completed_after_deadline:{evidence['duration_ms']}ms",
            )
        provider_errors.append(evidence)
        if evidence["outcome"] == "budget_overrun":
            failures.append(f"{name}:budget_overrun")
            # Late data is kept only as diagnostic/partial material.
            consider(payload, name)
            continue
        if evidence["outcome"] != "ok":
            failures.append(f"{name}:{evidence['error_type']}")
            continue
        if consider(payload, name):
            return ProviderRows(
                _records(payload), source=name, warnings=failures,
                provider_errors=provider_errors,
            ), name
        if name == "sina.hq_batch" and best is not None and "sina.hq_batch:budget_exhausted" in failures:
            # Preserve real partial rows instead of losing them to a subsequent
            # unbounded adapter and the outer worker's hard timeout.
            for skipped in ("eastmoney.spot_ak", "sina.spot_ak"):
                provider_errors.append({"source": skipped, "stage": "skipped", "outcome": "budget_exhausted",
                                        "error_type": "BudgetExhausted", "message": "hq_deadline_partial_preserved", "duration_ms": 0})
            break
    if best is not None:
        return _evidence_rows(
            _records(best[0]), source=best[1],
            warnings=["snapshot_provider_coverage_incomplete", *failures],
            provider_errors=provider_errors,
        ), best[1]
    if below_minimum_skips:
        # The whole window was smaller than every remaining provider's declared
        # budget - or the unbounded sources were skipped outright - so the chain
        # reports the skipped/refused chain with its evidence instead of
        # claiming an attempted provider failed.
        reason = f"snapshot_provider_budget_too_small:{round(max(0.0, deadline - clock()), 3)}"
        return _evidence_rows(
            [], source=PROVIDER_BUDGET_TOO_SMALL_SOURCE,
            warnings=[reason, *failures], provider_errors=provider_errors,
        ), PROVIDER_BUDGET_TOO_SMALL_SOURCE
    raise SnapshotProviderError(
        "snapshot_providers_failed:" + ",".join(failures[:6]),
        provider_errors=provider_errors,
    )


def _akshare_provider(function_name: str) -> Any:
    """Import akshare-backed providers lazily and call the named one."""

    from quantdesk import data_providers

    provider = getattr(data_providers, function_name)
    return provider()


def fetch_snapshot_in_process(
    symbols: Sequence[str] = (), *, network: bool = False, budget_seconds: float = 150.0,
    trade_date: str = "",
) -> ProviderRows:
    """Run the dated HQ-first quote chain with interruptible provider stages.

    Only the parent collector writes an archive. The child returns quote data
    over stdout and is terminated if the whole request exceeds its allowance.
    The original deadline includes process setup/imports and child cleanup;
    both endpoints share what remains. HQ has a separate interruptible phase
    so a slow stream cannot consume the time reserved for the list fallback.
    The session date is only a comparison target, never a value used to fill
    a missing provider quote date.
    """
    if not network:
        return ProviderRows([], source="offline", warnings=["network_not_authorized"], provider_errors=[])
    return _fetch_snapshot_worker(symbols, budget_seconds=budget_seconds, trade_date=trade_date, mode="chain")


def _fetch_snapshot_worker(
    symbols: Sequence[str], *, budget_seconds: float, trade_date: str, mode: str,
    outer_deadline: float | None = None,
) -> ProviderRows:
    """Fetch one chain or HQ phase, including startup and reaping in its budget."""
    import sys

    if mode not in {"chain", "hq"}:
        raise ValueError("unknown_snapshot_worker_mode")
    source = "sina.hq_batch" if mode == "hq" else "existing_provider_chain"
    allowance = min(30.0 if mode == "hq" else 240.0, max(0.0, float(budget_seconds)))
    started = time_module.monotonic()
    deadline = started + allowance
    if outer_deadline is not None:
        deadline = min(deadline, outer_deadline)
        allowance = max(0.0, deadline - started)
    # Leave time to kill/reap inside this phase, never add a new five-second
    # cleanup timeout after it. The child receives both absolute deadlines.
    cleanup_budget = min(1.0, allowance / 5.0)
    fetch_deadline = deadline - cleanup_budget
    source_root = Path(__file__).resolve().parents[1]
    if allowance <= 0:
        raise SnapshotProviderError("snapshot_worker_budget_exhausted", provider_errors=[{
            "source": source, "stage": "skipped", "outcome": "budget_exhausted",
            "error_type": "BudgetExhausted", "duration_ms": 0,
        }])
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(filter(None, [str(source_root), env.get("PYTHONPATH", "")]))
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    child = subprocess.Popen(
        [sys.executable, "-B", "-m", "quantdesk.snapshot_history"], cwd=str(source_root), env=env,
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, encoding="utf-8", errors="replace",
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    timed_out = False
    try:
        stdout, stderr = child.communicate(
            json.dumps({
                "network": True, "symbols": list(symbols), "deadline_monotonic": deadline,
                "fetch_deadline_monotonic": fetch_deadline, "worker_mode": mode,
                "trade_date": trade_date or _as_shanghai(None).date().isoformat(),
            }, ensure_ascii=False),
            timeout=max(0.0, fetch_deadline - time_module.monotonic()),
        )
    except subprocess.TimeoutExpired:
        timed_out = True
        try:
            child.kill()
        except ProcessLookupError:  # the HQ watchdog may have exited first
            pass
        try:
            stdout, stderr = child.communicate(timeout=max(0.0, deadline - time_module.monotonic()))
        except subprocess.TimeoutExpired:
            stdout, stderr = "", ""
    duration = int(round((time_module.monotonic() - started) * 1000))
    stage = {"source": source, "stage": "isolated_fetch", "duration_ms": duration,
             "runtime_pid": child.pid, "budget_seconds": allowance, "return_code": child.returncode,
             "worker_mode": mode, "process_reaped": child.returncode is not None,
             "deadline_monotonic": deadline, "fetch_deadline_monotonic": fetch_deadline,
             "cleanup_budget_seconds": cleanup_budget}
    if child.returncode is None:
        stage.update(outcome="failed", error_type="ProcessCleanupTimeout", message="worker_not_reaped_within_phase")
        raise SnapshotProviderError("snapshot_worker_cleanup_deadline_exhausted", provider_errors=[stage])
    if timed_out or child.returncode == 124 or time_module.monotonic() >= deadline:
        stage.update(outcome="failed", error_type="TimeoutError", message="provider_worker_deadline_exhausted")
        raise SnapshotProviderError("snapshot_worker_deadline_exhausted", provider_errors=[stage])
    try:
        result = json.loads(stdout)
        if not isinstance(result, dict):
            raise ValueError("worker_result_not_object")
    except (ValueError, TypeError) as exc:
        stage.update(outcome="failed", error_type=type(exc).__name__, message=_sanitize_error_text(stderr or exc))
        raise SnapshotProviderError("snapshot_worker_invalid_result", provider_errors=[stage]) from exc
    if child.returncode != 0 or not result.get("ok"):
        stage.update(outcome="failed", error_type=str(result.get("error_type") or "ProviderError"),
                     message=_sanitize_error_text(result.get("message") or stderr))
        raise SnapshotProviderError("snapshot_worker_provider_failed", provider_errors=[*result.get("provider_errors", []), stage])
    stage.update(outcome="ok", error_type="", message="")
    return ProviderRows(_records(result.get("rows")), source=str(result.get("source") or "unknown_provider"),
                        warnings=list(result.get("warnings") or []), provider_errors=[*result.get("provider_errors", []), stage])



def _call_fetcher(fetcher: Callable[..., Any], now: datetime, symbols: Sequence[str]) -> Any:
    try:
        signature = inspect.signature(fetcher)
    except (TypeError, ValueError):
        signature = None
    if signature is not None and len(signature.parameters) == 0:
        return fetcher()
    if signature is not None:
        if "symbols" in signature.parameters:
            return fetcher(symbols=symbols)
        if "universe" in signature.parameters:
            return fetcher(universe=symbols)
        for name in ("now", "fetched_at", "at"):
            if name in signature.parameters:
                return fetcher(**{name: now})
    try:
        return fetcher()
    except TypeError:
        return fetcher(now)


def _fetch_result(result: Any) -> tuple[Any, str, list[str], list[dict[str, Any]]]:
    warnings: list[str] = []
    provider_errors: list[dict[str, Any]] = []
    source = "unknown_provider"
    if isinstance(result, tuple) and len(result) == 2:
        payload, source = result
        # ``ProviderRows`` carries the chain evidence of a fallback success that
        # followed a failure; it must reach the archive, not be dropped here.
        if isinstance(payload, ProviderRows):
            provider_errors = [dict(item) for item in payload.provider_errors]
            warnings = [str(item) for item in payload.warnings]
    elif isinstance(result, ProviderRows):
        payload, source = result, result.source or source
        warnings = list(result.warnings)
        provider_errors = [dict(item) for item in result.provider_errors]
    elif isinstance(result, Mapping):
        payload = result.get("rows", result.get("data", result.get("records", result)))
        source = str(result.get("source") or source)
        warnings = [str(item) for item in result.get("warnings") or []]
        provider_errors = [dict(item) for item in result.get("provider_errors") or []]
    else:
        payload = result
    return payload, str(source), warnings, provider_errors


def _relative(root: Path, target: Path) -> str:
    try:
        return str(target.relative_to(root)).replace("\\", "/")
    except ValueError:
        return str(target).replace("\\", "/")


def _write_csv(path: Path, rows: Sequence[Mapping[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    with temp.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(SNAPSHOT_COLUMNS), extrasaction="ignore")
        writer.writeheader()
        writer.writerows({key: row.get(key, "") for key in SNAPSHOT_COLUMNS} for row in rows)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temp, path)


def _write_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temp.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temp, path)


def _universe_hash(symbols: Sequence[str]) -> str:
    return hashlib.sha256(json.dumps(sorted(symbols), separators=(",", ":")).encode("utf-8")).hexdigest()


def _unknown_code_version() -> dict[str, Any]:
    return {
        "status": "unknown", "git_head": "unknown", "dirty_count": "unknown",
        "captured_at": "", "reason_code": "not_recorded_at_collection",
    }


def _capture_code_version(captured_at: datetime) -> dict[str, Any]:
    """Record the executing checkout, never the injected archive directory."""

    version = _unknown_code_version()
    version["captured_at"] = captured_at.isoformat(timespec="seconds")
    options = {
        "cwd": Path(__file__).resolve().parents[1],
        "capture_output": True, "text": True, "encoding": "utf-8",
        "errors": "replace", "check": True, "timeout": 5,
    }
    try:
        head = subprocess.run(
            ["git", "--no-optional-locks", "rev-parse", "--verify", "HEAD"], **options,
        ).stdout.strip()
        if len(head) not in {40, 64} or any(char not in "0123456789abcdef" for char in head):
            raise ValueError("git_head_invalid")
        version["git_head"] = head
        entries = subprocess.run(
            ["git", "--no-optional-locks", "status", "--porcelain=v1", "-z", "--untracked-files=all"],
            **options,
        ).stdout.split("\0")
        dirty_count = 0
        index = 0
        while index < len(entries):
            entry = entries[index]
            if entry:
                if len(entry) < 3 or entry[2] != " ":
                    raise ValueError("git_status_invalid")
                dirty_count += 1
                # Porcelain -z gives renamed/copied entries one extra path.
                if "R" in entry[:2] or "C" in entry[:2]:
                    index += 1
            index += 1
        version.update(status="recorded", dirty_count=dirty_count, reason_code="")
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        version["reason_code"] = f"git_unavailable:{type(exc).__name__}"
    return version


def _stored_code_version(metadata: Mapping[str, Any]) -> dict[str, Any]:
    version = _unknown_code_version()
    stored = metadata.get("code_version")
    if isinstance(stored, Mapping):
        version.update(stored)
    return version


# Every status a written attempt sidecar may carry.  A late capture and a
# partial archive are attempts too: they are evidence of what happened, not
# clean collections, so the manifest reader must read them instead of filing
# them as invalid sidecars.
ATTEMPT_STATUS_PROVIDER_FAILED = "provider_failed"
ATTEMPT_STATUS_PROVIDER_EMPTY = "provider_empty"
ATTEMPT_STATUS_PARTIAL_ARCHIVE = "partial_archive"
ATTEMPT_READABLE_STATUSES = frozenset({
    ATTEMPT_STATUS_PROVIDER_FAILED,
    ATTEMPT_STATUS_PROVIDER_EMPTY,
    ATTEMPT_STATUS_PARTIAL_ARCHIVE,
})


def _read_attempts(
    root: Path, trade_date: str, expected_slots: Sequence[str],
) -> tuple[dict[str, list[dict[str, Any]]], dict[str, str]]:
    attempts: dict[str, list[dict[str, Any]]] = {}
    invalid: dict[str, str] = {}
    directory = root / SNAPSHOT_RELATIVE_DIR / trade_date / "attempts"
    for path in sorted(directory.glob("*.json")):
        try:
            if path.stat().st_size > 64 * 1024:
                raise ValueError("attempt_size_limit")
            item = json.loads(path.read_text(encoding="utf-8"))
            if (
                not isinstance(item, dict)
                or item.get("schema_version") != "a0_snapshot_attempt_v1"
                or item.get("trade_date") != trade_date
                or item.get("status") not in ATTEMPT_READABLE_STATUSES
                or not isinstance(item.get("reason_codes"), list)
                or not item["reason_codes"]
            ):
                raise ValueError("attempt_schema_invalid")
            slot = item.get("slot_time")
            if slot not in expected_slots:
                continue
            item["attempt_path"] = _relative(root, path)
            item["code_version"] = _stored_code_version(item)
            attempts.setdefault(slot, []).append(item)
        except (OSError, UnicodeError, ValueError, TypeError) as exc:
            invalid[_relative(root, path)] = f"{type(exc).__name__}:{exc}"[:200]
    return attempts, invalid


def _record_failed_attempt(
    base: dict[str, Any], root: Path, manifest_path: Path,
    expected_symbols: Sequence[str], include_close: bool,
    as_of: datetime | None = None,
    *,
    attempt_status: str | None = None,
    write_manifest: bool = True,
) -> dict[str, Any]:
    """Keep each failed fetch separately; retries never replace its evidence.

    ``attempt_status`` files the sidecar under the attempt's own truth (a late
    capture, a partial archive) instead of the caller's final status, and
    ``write_manifest=False`` lets a caller that already refreshed the day
    manifest avoid writing it a second time.
    """

    base["status"] = str(attempt_status or base.get("status") or "attempt_unclassified")
    directory = manifest_path.parent / "attempts"
    try:
        # ``_write_json`` creates the attempts directory; only the numbering is
        # read here, and an absent directory simply yields no numbers.
        numbers = [
            int(path.stem.rsplit("-", 1)[-1])
            for path in directory.glob(f"{base['slot_time']}-*.json")
            if path.stem.rsplit("-", 1)[-1].isdigit()
        ]
        path = directory / f"{base['slot_time']}-{max(numbers, default=0) + 1:06d}.json"
        base["attempt_path"] = _relative(root, path)
        attempt = {key: base[key] for key in (
            "trade_date", "slot_time", "status", "reason_codes", "warnings",
            "source", "fetched_at", "row_count", "expected_symbol_count",
            "network_attempted", "attempt_started_at", "attempt_completed_at",
            "code_version", "attempt_path", "pipeline_input", "ai_input", "latest_overwrite",
            "failure_detail", "stage_timings", "quote_time_states",
        )}
        attempt["schema_version"] = "a0_snapshot_attempt_v1"
        if not any(str(code).strip() for code in attempt["reason_codes"]):
            # A partial archive can keep warnings without ever recording a
            # reason code (one usable row still flags the stale rows).  The
            # sidecar must stay readable by ``_read_attempts``, so the
            # attempt's own recorded warnings become its reasons: nothing is
            # invented, and the caller's own result payload is left untouched.
            attempt["reason_codes"] = [
                str(warning) for warning in attempt["warnings"] if str(warning).strip()
            ] or ["attempt_reason_unrecorded"]
        # The sidecar goes through the same atomic writer as every other
        # artifact: the monotonically numbered name is what keeps earlier
        # attempts, and the collector holds the lock while it is written.
        _write_json(path, attempt)
        base["attempt_written"] = True
        if write_manifest:
            day_manifest = _manifest(
                root, base["trade_date"], include_close=include_close,
                expected_symbols=expected_symbols, as_of=as_of,
            )
            _write_json(manifest_path, day_manifest)
            base["missing_slots"] = day_manifest["missing_slots"]
            base["day_summary"] = _day_summary_view(day_manifest)
    except OSError as exc:
        base.update(
            status="archive_write_failed",
            reason_codes=[*base["reason_codes"], f"attempt_evidence_write_failed:{type(exc).__name__}"],
        )
    return base


# A quote may sit slightly ahead of the collector's own clock (provider /
# collector clock skew).  This is the explicit, measurable allowance; nothing
# wider is used, because a 300-second window let a 10:04:59 quote be filed and
# reported as a 10:00 snapshot.
QUOTE_TIME_CLOCK_SKEW_SECONDS = 5


def _quote_time_state(
    row: Mapping[str, Any],
    trade_date: str,
    *,
    fetched_at: datetime | None = None,
) -> str:
    """Classify a row's provider quote time against the capture that holds it.

    Returns ``usable`` / ``stale`` / ``future`` / ``missing`` / ``unparseable``.

    The verdict is driven by the provider's own timestamp, never by the
    collector's ``fetched_at`` standing in for it:

    * ``missing``     - the provider sent no quote time at all.
    * ``unparseable`` - the provider sent something that is not a timestamp.
    * ``stale``       - the quote is dated before the trade date being filed,
      so it cannot be an observation of this session.
    * ``future``      - the quote is stamped after the recorded capture
      completion plus the clock-skew allowance. This detects disagreement
      between recorded clocks; it does not establish which clock is wrong.
    * ``usable``      - a same-day quote within the recorded completion bound;
      it does not establish an intraday maximum-age guarantee.

    A provider that supplies only a clock time (Sina's ``ticktime`` does) has
    that time-of-day anchored to the trade date being filed - the collector's
    own calendar, which is not what is being verified.  The clock time itself
    must still pass the not-after-fetch check above, so a quote that belongs to
    a later capture is rejected no matter how it is dated.
    """

    raw_date = str(row.get("quote_date") or "").strip()
    raw_time = str(row.get("quote_time") or "").strip()
    if not raw_time:
        return "missing"
    if "-" in raw_time or "/" in raw_time:
        stamp = raw_time
    elif raw_date:
        stamp = f"{raw_date[:10]} {raw_time}"
    else:
        stamp = f"{str(trade_date)[:10]} {raw_time}"
    parsed: datetime | None = None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y/%m/%d %H:%M:%S", "%Y/%m/%d %H:%M"):
        try:
            parsed = datetime.strptime(stamp[:19], fmt)
            break
        except ValueError:
            continue
    if parsed is None:
        return "unparseable"
    if parsed.date().isoformat() < str(trade_date)[:10]:
        return "stale"
    if fetched_at is not None:
        observed = _as_shanghai(fetched_at).replace(tzinfo=None)
        if parsed > observed + timedelta(seconds=QUOTE_TIME_CLOCK_SKEW_SECONDS):
            return "future"
    return "usable"


def _slot_evidence(root: Path, trade_date: str, slot: str) -> dict[str, Any]:
    target = root / SNAPSHOT_RELATIVE_DIR / trade_date / f"{slot}.csv"
    metadata_path = target.with_suffix(".json")
    if not target.exists() and not metadata_path.exists():
        return {"status": "missing", "reason": "not_collected"}
    if not target.is_file() or not metadata_path.is_file():
        return {"status": "incomplete", "reason": "archive_pair_incomplete"}
    try:
        if target.stat().st_size > 64 * 1024 * 1024 or metadata_path.stat().st_size > 2 * 1024 * 1024:
            raise ValueError("archive_size_limit")
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        content = target.read_bytes()
        reader = csv.DictReader(io.StringIO(content.decode("utf-8-sig")))
        rows = list(reader)
        if (not isinstance(metadata, dict) or not rows
                or reader.fieldnames not in (list(SNAPSHOT_COLUMNS), list(LEGACY_SNAPSHOT_COLUMNS))):
            raise ValueError("archive_schema_invalid")
        expected = metadata.get("expected_symbols")
        if not isinstance(expected, list) or expected != _load_symbols(root, expected):
            raise ValueError("archive_universe_invalid")
        if metadata.get("universe_sha256") != _universe_hash(expected):
            raise ValueError("archive_universe_hash_mismatch")
        if metadata.get("csv_sha256") != hashlib.sha256(content).hexdigest():
            raise ValueError("archive_csv_hash_mismatch")
        if (
            metadata.get("schema_version") != "a0_snapshot_history_v1"
            or metadata.get("trade_date") != trade_date
            or metadata.get("slot_time") != slot
            or metadata.get("snapshot_path") != _relative(root, target)
            or metadata.get("row_count") != len(rows)
            or metadata.get("expected_symbol_count") != len(expected)
            or not metadata.get("source")
        ):
            raise ValueError("archive_metadata_mismatch")
        fetched = datetime.fromisoformat(metadata["fetched_at"])
        if fetched.tzinfo is None:
            raise ValueError("archive_timestamp_timezone_missing")
        observed = {row["symbol"] for row in rows}
        if len(observed) != len(rows) or any(len(symbol) != 6 or not symbol.isdigit() for symbol in observed):
            raise ValueError("archive_symbol_invalid")
        if any(
            row["snapshot_date"] != trade_date or row["snapshot_time"] != slot
            or row["fetched_at"] != metadata["fetched_at"] or row["source"] != metadata["source"]
            for row in rows
        ):
            raise ValueError("archive_row_evidence_mismatch")
        missing = sorted(set(expected) - observed)
        covered = len(set(expected) & observed)
        if (
            metadata.get("missing_symbols") != missing
            or metadata.get("coverage_count") != covered
            or metadata.get("coverage_ratio") != (covered / len(expected) if expected else None)
            or metadata.get("unexpected_symbols") != sorted(observed - set(expected))
        ):
            raise ValueError("archive_coverage_mismatch")
        quote_time_ok = all(
            _quote_time_state(row, trade_date, fetched_at=fetched) == "usable" for row in rows
        )
        # One rule, both sides: a slot counts as complete when nothing blocks it.
        # A price gap is counted (``snapshot_price_missing:<n>``) but no longer
        # excludes a fully collected slot; the only floor kept is that a capture
        # with no usable price at all is still not a collection (S3.4-r).
        complete = bool(
            expected and not missing
            and metadata.get("status") == "written"
            and not _slot_blocked_by_warnings(metadata.get("warnings") or [])
            and metadata["source"] != "unknown_provider"
            and _as_shanghai(fetched).date().isoformat() == trade_date
            and _slot_for(_as_shanghai(fetched), include_close=slot == "1500") == slot
            and quote_time_ok
            and any(_valid_price(row["price"]) for row in rows)
        )
        if metadata.get("status") not in {"written", "partial"}:
            raise ValueError("archive_status_invalid")
        # A quote is only usable when its price is a real number AND its own
        # provider timestamp could have been observed by this capture (see
        # ``_quote_time_state``).  The archive's own recorded ``fetched_at`` is
        # the reference, so an archive written before this check existed is
        # re-judged instead of being trusted.
        time_states = {
            state: 0 for state in ("usable", "stale", "future", "missing", "unparseable")
        }
        valid_quotes = 0
        for row in rows:
            state = _quote_time_state(row, trade_date, fetched_at=fetched)
            time_states[state] += 1
            if state == "usable" and _valid_price(row["price"]):
                valid_quotes += 1
        observed_at = _as_shanghai(fetched)
        fetch_outside_slot = bool(
            observed_at.date().isoformat() != trade_date
            or _slot_for(observed_at, include_close=slot == "1500") != slot
        )
        quality = {
            "expected_symbol_count": len(expected),
            "row_count": len(rows),
            "roster_coverage_count": covered,
            "roster_coverage_ratio": (covered / len(expected)) if expected else None,
            "valid_quote_count": valid_quotes,
            "valid_quote_ratio": (valid_quotes / len(rows)) if rows else None,
            "invalid_quote_count": len(rows) - valid_quotes,
            "missing_symbol_count": len(missing),
            "quote_time_states": time_states,
            "quote_time_outside_slot": any(
                time_states[state] for state in ("stale", "future", "missing", "unparseable")
            ),
            # The capture itself landed outside the slot it is filed under: real
            # evidence, but not a collection of this slot (DSR-05).
            "fetch_outside_slot": fetch_outside_slot,
            "source": metadata["source"],
            **_quote_field_quality(rows, trade_date),
        }
        return {"status": "complete" if complete else "partial", "metadata": metadata, "quality": quality}
    except (OSError, UnicodeError, ValueError, TypeError, KeyError, csv.Error) as exc:
        return {"status": "invalid", "reason": f"{type(exc).__name__}:{exc}"[:200]}


_DAY_SUMMARY_KEYS = (
    "trade_date",
    "as_of",
    "expected_slot_count",
    "elapsed_slot_count",
    "pending_slot_count",
    "collected_slot_count",
    "collected_slot_ratio",
    "complete_ratio",
    "completed_slot_count",
    "partial_slot_count",
    "missing_slot_count",
    "missing_elapsed_slot_count",
    "missing_pending_slot_count",
    "missing_slots_include_pending",
    "archived_slot_count",
    "roster_coverage_ratio",
    "valid_quote_ratio",
    "field_verified_complete_slot_count",
    "field_partial_slot_count",
    "quote_date_missing_count",
    "price_basis_unverified_count",
)


def _day_summary_view(day_manifest: Mapping[str, Any]) -> dict[str, Any]:
    """The compact, non-interchangeable day rates attached to each result."""

    return {key: day_manifest.get(key) for key in _DAY_SUMMARY_KEYS}


def _slot_elapsed(trade_date: str, slot: str, reference: datetime) -> bool:
    """True when a slot label is already due at ``reference``.

    A slot becomes "elapsed" the moment its label starts; slots that have not
    started yet are pending and must never be reported as collection failures.
    """

    day = str(trade_date)[:10]
    reference_day = _as_shanghai(reference).date().isoformat()
    if day < reference_day:
        return True
    if day > reference_day:
        return False
    try:
        slot_at = time(int(str(slot)[:2]), int(str(slot)[2:4]))
    except (TypeError, ValueError, IndexError):
        return True
    return _as_shanghai(reference).time() >= slot_at


def _manifest(
    root: Path,
    trade_date: str,
    *,
    include_close: bool,
    expected_symbols: Sequence[str],
    as_of: datetime | None = None,
) -> dict[str, Any]:
    day_root = root / SNAPSHOT_RELATIVE_DIR / trade_date
    expected = expected_snapshot_slots(include_close=include_close)
    evidence = {slot: _slot_evidence(root, trade_date, slot) for slot in expected}
    completed = [slot for slot, item in evidence.items() if item["status"] == "complete"]
    partial = [slot for slot, item in evidence.items() if item["status"] == "partial"]
    missing = [slot for slot in expected if slot not in completed and slot not in partial]
    invalid = {slot: item["reason"] for slot, item in evidence.items() if item["status"] in {"invalid", "incomplete"}}
    attempts, invalid_attempts = _read_attempts(root, trade_date, expected)
    archived_universes = {
        slot: {"symbols": item["metadata"]["expected_symbols"], "sha256": item["metadata"]["universe_sha256"]}
        for slot, item in evidence.items() if "metadata" in item
    }
    frozen = next(iter(archived_universes.values()), {"symbols": list(expected_symbols), "sha256": _universe_hash(expected_symbols)})

    # --- Slot collection / coverage / quote quality are reported separately.
    # ``missing_slots`` keeps its original meaning (every expected slot that is
    # not archived) for field compatibility; the elapsed/pending split below is
    # what tells a reader whether a gap was a collection failure or simply a
    # slot that had not happened yet.
    reference = _as_shanghai(as_of)
    elapsed = [slot for slot in expected if _slot_elapsed(trade_date, slot, reference)]
    pending = [slot for slot in expected if slot not in elapsed]
    # A capture whose quotes or whose own fetch time belong outside the slot it
    # is filed under is archived evidence, but it is not a collection of that
    # slot: it must not inflate the collected rate (DSR-05).  Both sides are
    # read from the archive itself, so an already-archived capture is caught too.
    late = [
        slot
        for slot, item in evidence.items()
        if item.get("quality", {}).get("quote_time_outside_slot")
        or item.get("quality", {}).get("fetch_outside_slot")
        or "fetch_completed_outside_requested_slot" in (item.get("metadata", {}).get("warnings") or [])
    ]
    collected = [slot for slot in [*completed, *partial] if slot not in late]
    collected_elapsed = [slot for slot in collected if slot in elapsed]
    missing_elapsed = [slot for slot in missing if slot in elapsed]
    slot_quality = {
        slot: item["quality"] for slot, item in evidence.items() if "quality" in item
    }
    archived_quality = list(slot_quality.values())
    roster_num = sum(item["roster_coverage_count"] for item in archived_quality)
    roster_den = sum(item["expected_symbol_count"] for item in archived_quality)
    quote_num = sum(item["valid_quote_count"] for item in archived_quality)
    quote_den = sum(item["row_count"] for item in archived_quality)
    field_verified = [slot for slot in completed if slot_quality[slot]["quality_status"] == "complete"]
    return {
        "schema_version": "a0_snapshot_history_v1",
        "trade_date": trade_date,
        "as_of": reference.isoformat(timespec="seconds"),
        "slot_convention": "regular_start_labels" if not include_close else "regular_start_labels_plus_1500_close",
        "expected_slots": expected,
        "completed_slots": completed,
        "partial_slots": partial,
        "invalid_slots": invalid,
        "missing_slots": missing,
        "missing_slot_reasons": {
            slot: ([invalid[slot]] if slot in invalid else attempts[slot][-1]["reason_codes"]
                   if slot in attempts else ["not_collected"])
            for slot in missing
        },
        "slot_attempts": attempts,
        "invalid_attempts": invalid_attempts,
        "slot_code_versions": {
            slot: _stored_code_version(item["metadata"])
            for slot, item in evidence.items() if "metadata" in item
        },
        "expected_slot_count": len(expected),
        "completed_slot_count": len(completed),
        "partial_slot_count": len(partial),
        "missing_slot_count": len(expected) - len(completed) - len(partial),
        "expected_symbol_count": len(frozen["symbols"]),
        "universe_sha256": frozen["sha256"],
        "slot_universes": archived_universes,
        "complete_ratio": len(completed) / len(expected) if expected else None,
        # Separate, non-interchangeable rates.
        "elapsed_slots": elapsed,
        "elapsed_slot_count": len(elapsed),
        "pending_slots": pending,
        "pending_slot_count": len(pending),
        "collected_slots": collected_elapsed,
        "collected_slot_count": len(collected_elapsed),
        "collected_slot_ratio": (len(collected_elapsed) / len(elapsed)) if elapsed else None,
        "late_slots": sorted(slot for slot in late if slot in elapsed),
        "late_slot_count": len([slot for slot in late if slot in elapsed]),
        "late_slot_reason": "quote_time_or_fetch_outside_requested_slot",
        "missing_elapsed_slots": missing_elapsed,
        "missing_elapsed_slot_count": len(missing_elapsed),
        "missing_pending_slot_count": len(pending),
        "missing_slots_include_pending": bool(pending),
        "roster_coverage_count": roster_num,
        "roster_coverage_expected_count": roster_den,
        "roster_coverage_ratio": (roster_num / roster_den) if roster_den else None,
        "valid_quote_count": quote_num,
        "quoted_row_count": quote_den,
        "valid_quote_ratio": (quote_num / quote_den) if quote_den else None,
        "archived_slot_count": len(archived_quality),
        "slot_quality": slot_quality,
        "field_verified_complete_slots": field_verified,
        "field_verified_complete_slot_count": len(field_verified),
        "field_partial_slot_count": len(archived_quality) - len(field_verified),
        "quote_date_missing_count": sum(item["quote_date_missing_count"] for item in archived_quality),
        "price_basis_unverified_count": sum(item["price_basis_unverified_count"] for item in archived_quality),
        "writer": "a0_history_only_snapshot_collector",
        "pipeline_input": False,
        "ai_input": False,
        "latest_overwrite": False,
    }


def collect_market_snapshot(
    project_root: str | Path,
    *,
    network: bool = False,
    now: datetime | None = None,
    clock: Callable[[], datetime] | None = None,
    fetcher: Callable[..., Any] | None = None,
    symbols: Iterable[Any] | None = None,
    include_close: bool = False,
    slot_time: str | None = None,
    dry_run: bool = False,
    lock: bool = True,
    provenance: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Collect one slot, returning a truthful result without downstream side effects."""

    root = Path(project_root).expanduser().resolve()
    current = _as_shanghai(now)
    trade_date = current.date().isoformat()
    expected_symbols = _load_symbols(root, symbols)
    base: dict[str, Any] = {
        "schema_version": "a0_snapshot_history_v1",
        "trade_date": trade_date,
        "slot_time": str(slot_time or ""),
        "network_authorized": bool(network),
        "network_attempted": False,
        "fetched_at": "",
        "source": "",
        "row_count": 0,
        "expected_symbol_count": len(expected_symbols),
        "missing_symbols": [],
        "missing_slots": expected_snapshot_slots(include_close=include_close),
        "written": False,
        "snapshot_path": "",
        "metadata_path": "",
        "manifest_path": "",
        "attempt_path": "",
        "attempt_written": False,
        "reason_codes": [],
        "warnings": [],
        "failure_detail": {},
        "stage_timings": [],
        "quote_time_states": {},
        "pipeline_input": False,
        "ai_input": False,
        "latest_overwrite": False,
        "producer_provenance": dict(provenance or {}),
    }
    if dry_run:
        base.update(status="dry_run", reason_codes=["dry_run_no_provider_or_write"])
        return base
    if not network:
        base.update(status="offline", reason_codes=["network_not_authorized"])
        return base
    trading_day, calendar_source, calendar_warnings = _read_trading_day(root, current.date())
    base["calendar_source"] = calendar_source
    base["warnings"].extend(calendar_warnings)
    if not trading_day:
        base.update(status="closed", reason_codes=["non_trading_day"])
        return base
    inferred_slot = _slot_for(current, include_close=include_close)
    selected_slot = str(slot_time or inferred_slot or "")
    if selected_slot not in expected_snapshot_slots(include_close=include_close):
        base.update(status="closed", slot_time=selected_slot, reason_codes=["outside_snapshot_schedule"])
        return base
    if slot_time and selected_slot != inferred_slot:
        base.update(status="closed", slot_time=selected_slot, reason_codes=["historical_slot_backfill_not_allowed"])
        return base
    base["slot_time"] = selected_slot
    day_root = root / SNAPSHOT_RELATIVE_DIR / trade_date
    target = day_root / f"{selected_slot}.csv"
    metadata_path = day_root / f"{selected_slot}.json"
    manifest_path = day_root / "manifest.json"
    base.update(
        snapshot_path=_relative(root, target),
        metadata_path=_relative(root, metadata_path),
        manifest_path=_relative(root, manifest_path),
    )
    lock_context = interprocess_file_lock(root / LOCK_RELATIVE_PATH, timeout_seconds=0) if lock else None
    try:
        if lock_context is None:
            return _collect_locked(
                base, root, current, expected_symbols, target, metadata_path,
                manifest_path, include_close, fetcher, clock,
            )
        with lock_context:
            return _collect_locked(
                base, root, current, expected_symbols, target, metadata_path,
                manifest_path, include_close, fetcher, clock,
            )
    except TimeoutError:
        base.update(status="already_running", reason_codes=["collector_lock_busy"])
        return base


def _collect_locked(
    base: dict[str, Any],
    root: Path,
    current: datetime,
    expected_symbols: Sequence[str],
    target: Path,
    metadata_path: Path,
    manifest_path: Path,
    include_close: bool,
    fetcher: Callable[..., Any] | None,
    clock: Callable[[], datetime] | None,
) -> dict[str, Any]:
    existing = _slot_evidence(root, str(base["trade_date"]), str(base["slot_time"]))
    if existing["status"] != "missing":
        if existing["status"] in {"complete", "partial"}:
            metadata = existing["metadata"]
            day_manifest = _manifest(
                root, base["trade_date"], include_close=include_close,
                expected_symbols=metadata["expected_symbols"], as_of=current,
            )
            _write_json(manifest_path, day_manifest)
            base.update(
                status="already_exists" if existing["status"] == "complete" else "partial",
                reason_codes=["slot_already_archived"], row_count=metadata["row_count"],
                source=metadata["source"], fetched_at=metadata["fetched_at"],
                missing_symbols=metadata["missing_symbols"], warnings=metadata.get("warnings", []),
                missing_slots=day_manifest["missing_slots"],
                day_summary=_day_summary_view(day_manifest),
                code_version=_stored_code_version(metadata),
            )
        else:
            base.update(
                status="archive_incomplete" if existing["status"] == "incomplete" else "archive_invalid",
                reason_codes=[existing["reason"]],
            )
        return base
    started_at = _as_shanghai(clock() if clock is not None else None)
    base.update(
        network_attempted=True,
        attempt_started_at=started_at.isoformat(timespec="seconds"),
        code_version=_capture_code_version(started_at),
    )
    fetch_started = time_module.monotonic()
    try:
        result = _call_fetcher(fetcher or default_network_fetch, current, expected_symbols)
        payload, source, warnings, fetch_provider_errors = _fetch_result(result)
    except Exception as exc:  # noqa: BLE001 - preserve provider failure evidence
        provider_errors = list(getattr(exc, "provider_errors", []) or [])
        base.update(
            status="provider_failed", reason_codes=[f"provider_failed:{type(exc).__name__}"],
            warnings=[*base["warnings"], f"{type(exc).__name__}:{exc}"[:400]],
            attempt_completed_at=_as_shanghai(clock() if clock is not None else None).isoformat(timespec="seconds"),
            stage_timings=[
                {"stage": "provider_fetch", "source": "-", "outcome": "failed",
                 "duration_ms": int(round((time_module.monotonic() - fetch_started) * 1000))},
            ],
            failure_detail={
                "stage": "provider_fetch",
                "error_type": type(exc).__name__,
                "message": _sanitize_error_text(exc),
                "provider_errors": provider_errors,
                "sources_failed": [item.get("source") for item in provider_errors if item.get("outcome") != "ok"],
                "sources_not_attempted": [
                    item.get("source") for item in provider_errors if item.get("outcome") == "budget_exhausted"
                ],
            },
        )
        return _record_failed_attempt(
            base, root, manifest_path, expected_symbols, include_close, as_of=current,
        )
    fetch_time = _as_shanghai(clock() if clock is not None else None)
    base["stage_timings"] = [
        {"stage": "provider_fetch", "source": source, "outcome": "ok",
         "duration_ms": int(round((time_module.monotonic() - fetch_started) * 1000))},
    ]
    if fetch_provider_errors:
        # Whatever the fetcher's shape, the per-source evidence reaches the
        # archive: a successful fallback keeps the failures that preceded it.
        base["failure_detail"] = {
            "stage": "provider_fetch",
            "error_type": "",
            "message": "",
            "provider_errors": [dict(item) for item in fetch_provider_errors],
            "sources_failed": [
                item.get("source") for item in fetch_provider_errors if item.get("outcome") != "ok"
            ],
            "sources_not_attempted": [
                item.get("source")
                for item in fetch_provider_errors
                if item.get("outcome") == "budget_exhausted"
            ],
        }
    rows = normalize_snapshot_rows(
        payload,
        source=source,
        fetched_at=fetch_time,
        trade_date=str(base["trade_date"]),
        slot_time=str(base["slot_time"]),
    )
    observed = {str(row["symbol"]) for row in rows}
    missing_symbols = sorted(set(expected_symbols) - observed)
    warnings = list(dict.fromkeys([*base["warnings"], *warnings]))
    if not expected_symbols:
        warnings.append("universe_unavailable")
    if missing_symbols:
        warnings.append("snapshot_symbols_missing")
    price_missing_symbols = sorted(
        {str(row["symbol"]) for row in rows if not _valid_price(row["price"])}
    )
    if price_missing_symbols:
        warnings.append(f"snapshot_price_missing:{len(price_missing_symbols)}")
    quote_states: dict[str, int] = {}
    for row in rows:
        state = _quote_time_state(row, str(base["trade_date"]), fetched_at=fetch_time)
        quote_states[state] = quote_states.get(state, 0) + 1
    for state, count in sorted(quote_states.items()):
        if state != "usable" and count:
            # Stale / future / missing provider time is visible in the artifact,
            # never smoothed over by the collector's own fetched_at.
            warnings.append(f"snapshot_quote_time_{state}:{count}")
    if source == "unknown_provider":
        warnings.append("snapshot_source_unknown")
    outside_requested_slot = (
        fetch_time.date() != current.date()
        or _slot_for(fetch_time, include_close=include_close) != base["slot_time"]
    )
    if outside_requested_slot:
        warnings.append("fetch_completed_outside_requested_slot")
    base.update(
        fetched_at=fetch_time.isoformat(timespec="seconds"),
        attempt_completed_at=fetch_time.isoformat(timespec="seconds"),
        source=source,
        row_count=len(rows),
        missing_symbols=missing_symbols,
        warnings=warnings,
        quote_time_states=quote_states,
    )
    if not rows:
        base.update(status="provider_empty", reason_codes=["provider_returned_no_usable_rows"])
        if fetch_provider_errors and all(
            item.get("outcome") == "budget_exhausted" for item in fetch_provider_errors
        ):
            base["reason_codes"] = [*base["reason_codes"], "provider_budget_exhausted"]
        return _record_failed_attempt(
            base, root, manifest_path, expected_symbols, include_close, as_of=current,
        )
    # A capture that finished outside its slot keeps all of its data, but it is
    # filed as this slot's *failed* capture: the warning above, the archive's
    # own ``fetched_at`` and ``fetch_outside_slot`` keep it out of the collected
    # rate (DSR-05) while nothing is discarded.
    usable_rows = quote_states.get("usable", 0)
    if not usable_rows:
        # No row carries a timestamp this slot could have observed.  The capture
        # is still archived as this slot's failed capture - the data is kept -
        # but it can never be read back as a complete or collected slot.
        base["reason_codes"] = [*base["reason_codes"], "provider_quotes_not_usable_for_slot"]
    status = "partial" if _slot_blocked_by_warnings(warnings) else "written"
    metadata = {
        "schema_version": "a0_snapshot_history_v1",
        "snapshot_path": _relative(root, target),
        "trade_date": base["trade_date"],
        "slot_time": base["slot_time"],
        "fetched_at": base["fetched_at"],
        "attempt_started_at": base["attempt_started_at"],
        "code_version": base["code_version"],
        "source": source,
        "row_count": len(rows),
        "expected_symbol_count": len(expected_symbols),
        "expected_symbols": list(expected_symbols),
        "universe_sha256": _universe_hash(expected_symbols),
        "missing_symbols": missing_symbols,
        "price_missing_symbols": price_missing_symbols,
        "unexpected_symbols": sorted(observed - set(expected_symbols)),
        "coverage_count": len(observed & set(expected_symbols)),
        "coverage_ratio": (len(observed & set(expected_symbols)) / len(expected_symbols)) if expected_symbols else None,
        "warnings": warnings,
        "status": status,
        "quote_time_states": quote_states,
        "calendar_source": base["calendar_source"],
        "pipeline_input": False,
        "ai_input": False,
        "latest_overwrite": False,
        "producer_provenance": dict(base.get("producer_provenance") or {}),
        **_quote_field_quality(rows, base["trade_date"]),
    }
    if status == "partial" or base.get("failure_detail"):
        # A capture that fell back to a secondary source keeps the failure
        # evidence next to the data whether or not the slot itself is relabelled:
        # a written slot whose primary source failed must still say which stage
        # failed, from which source, with which error and how long it took
        # (S3.4-r; without this, a non-blocking source warning would hide it).
        metadata["stage_timings"] = list(base.get("stage_timings") or [])
        if base.get("failure_detail"):
            metadata["failure_detail"] = dict(base["failure_detail"])
    try:
        _write_csv(target, rows)
        metadata["csv_sha256"] = hashlib.sha256(target.read_bytes()).hexdigest()
        _write_json(metadata_path, metadata)
        day_manifest = _manifest(
            root, base["trade_date"], include_close=include_close,
            expected_symbols=expected_symbols, as_of=current,
        )
        _write_json(manifest_path, day_manifest)
        if status == "partial":
            # A partial capture is an attempt too: it keeps its own sidecar so a
            # later retry can never overwrite the record of what happened.
            _record_failed_attempt(
                base, root, manifest_path, expected_symbols, include_close,
                as_of=current, attempt_status=ATTEMPT_STATUS_PARTIAL_ARCHIVE,
                write_manifest=False,
            )
    except OSError as exc:
        base.update(status="archive_write_failed", reason_codes=[f"archive_write_failed:{type(exc).__name__}"])
        return base
    base.update(status=status, written=True)
    base["missing_slots"] = day_manifest["missing_slots"]
    base["day_summary"] = _day_summary_view(day_manifest)
    return base


def snapshot_day_summary(
    project_root: str | Path,
    trade_date: str,
    *,
    include_close: bool = False,
    symbols: Iterable[Any] | None = None,
    as_of: datetime | None = None,
) -> dict[str, Any]:
    """Read the dated health summary without changing any artifact.

    ``as_of`` fixes the reference instant used to split elapsed slots from
    pending ones; it defaults to the current Shanghai time.
    """

    root = Path(project_root).expanduser().resolve()
    expected_symbols = _load_symbols(root, symbols)
    payload = _manifest(
        root, str(trade_date)[:10], include_close=include_close,
        expected_symbols=expected_symbols, as_of=as_of,
    )
    payload["manifest_path"] = _relative(root, root / SNAPSHOT_RELATIVE_DIR / str(trade_date)[:10] / "manifest.json")
    return payload


__all__ = [
    "DEFAULT_WINDOWS",
    "PROVIDER_TOTAL_BUDGET_SECONDS",
    "SNAPSHOT_COLUMNS",
    "SNAPSHOT_RELATIVE_DIR",
    "SnapshotProviderError",
    "collect_market_snapshot",
    "default_network_fetch",
    "fetch_snapshot_in_process",
    "expected_snapshot_slots",
    "normalize_snapshot_rows",
    "snapshot_day_summary",
]


def _snapshot_worker_main() -> int:
    import contextlib
    import sys

    watchdog = None
    try:
        request_payload = json.load(sys.stdin)
        if request_payload.get("network") is not True:
            raise ValueError("network_not_authorized")
        mode = request_payload.get("worker_mode", "chain")
        if mode not in {"chain", "hq"}:
            raise ValueError("unknown_snapshot_worker_mode")
        with contextlib.redirect_stdout(sys.stderr):
            # Python 3.10+ monotonic clocks share the same OS reference across
            # processes, so imports cannot grant the child a fresh allowance.
            deadline = request_payload.get("deadline_monotonic")
            if deadline is None:
                deadline = time_module.monotonic() + PROVIDER_TOTAL_BUDGET_SECONDS
            deadline = min(float(deadline), float(request_payload.get("fetch_deadline_monotonic", deadline)))
            deadline = min(deadline, time_module.monotonic() + (30.0 if mode == "hq" else PROVIDER_TOTAL_BUDGET_SECONDS))
            remaining = max(0.0, deadline - time_module.monotonic())
            symbols = request_payload.get("symbols") or ()
            trade_date = str(request_payload.get("trade_date") or _as_shanghai(None).date().isoformat())
            if remaining <= 0:
                raise SnapshotProviderError("snapshot_worker_budget_exhausted", provider_errors=[])
            import threading

            # Start before any lazy provider import/call. Both the HQ child
            # and the chain interpreter exit even if a Windows venv launcher
            # is killed first. Raising in a thread cannot cancel urllib.read().
            watchdog = threading.Timer(max(0.0, deadline - time_module.monotonic()), os._exit, args=(124,))
            watchdog.daemon = True
            watchdog.start()
            if mode == "hq":
                value = _sina_hq_fetch(symbols, budget_seconds=max(0.0, deadline - time_module.monotonic()), batch_size=500)
            else:
                def isolated_hq(*, budget_seconds: float) -> ProviderRows:
                    return _fetch_snapshot_worker(
                        symbols, budget_seconds=budget_seconds, trade_date=trade_date,
                        mode="hq", outer_deadline=deadline,
                    )

                value = resilient_network_fetch(
                    symbols, budget_seconds=max(0.0, deadline - time_module.monotonic()),
                    trade_date=trade_date, primary=isolated_hq,
                )
            rows, source, warnings, provider_errors = _fetch_result(value)
        output = {"ok": True, "rows": _records(rows), "source": source,
                  "warnings": warnings, "provider_errors": provider_errors}
        code = 0
    except Exception as exc:  # the parent records failure and publishes no quotes
        output = {"ok": False, "error_type": type(exc).__name__, "message": _sanitize_error_text(exc),
                  "provider_errors": list(getattr(exc, "provider_errors", []) or [])}
        code = 1
    try:
        print(json.dumps(output, ensure_ascii=False, default=str), flush=True)
    finally:
        if watchdog is not None:
            watchdog.cancel()
    return code


if __name__ == "__main__":
    raise SystemExit(_snapshot_worker_main())
