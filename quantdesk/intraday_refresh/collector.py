"""Minute-bar collection with fail-closed validation and a health manifest.

Collection windows (driven by the scheduled task, honoured by this module):
    09:35 first round, every 5 minutes to 11:30,
    13:05 resume,      every 5 minutes to 15:00,
    15:05 final check.
Outside a trading session - or on a non-trading day - the run reports
``closed`` and never contacts a provider.

Providers: mootdx 1-minute bars (frequency=8), with the existing raw Sina
minute source as a fallback for Shanghai/Shenzhen symbols. Rows are appended
per symbol with dedupe key (symbol, datetime); historical bars are never
rewritten. Every row must pass: positive prices, OHLC not inverted, datetime
not in the future, datetime not already present. Provider failure leaves the
existing data marked stale/blocked - never silently "fresh".
"""

from __future__ import annotations

import csv
import hashlib
import importlib
import inspect
import ipaddress
import json
import logging
import math
import os
import subprocess
import sys
import threading
import time
from collections.abc import Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Iterator
from zoneinfo import ZoneInfo

from quantdesk.interprocess_lock import interprocess_file_lock
from quantdesk.intraday.minute_storage import QUARANTINE_DIR, envelope_violation, minute_write_path
from quantdesk.intraday_refresh.scope import build_dynamic_scope  # noqa: E402
from quantdesk.intraday_refresh.snapshot import refresh_full_market_snapshot  # noqa: E402

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SHANGHAI = ZoneInfo("Asia/Shanghai")

LOG = logging.getLogger(__name__)

INTRADAY_DIR_NAME = "data/intraday/1m"
RESULTS_DIR_NAME = "results/intraday"
LOCK_RELATIVE = Path("data/intraday/1m/.locks/stage6_append_only_batch.lock")
DAILY_HEADER = "date,symbol,datetime,open,high,low,close,volume,amount,price_basis,source,updated_at"

SCHEMA_VERSION = "intraday_refresh_v1"
FRESH_MAX_LAG_SECONDS = 600  # 10 minutes during market hours

# Bounded round budget.  The scheduled task kills a round at its 10-minute
# ExecutionTimeLimit (event 329 / 0x41306 on 2026-09-10), so the collector must
# stop itself well before that and say so, instead of dying with no manifest.
# It must also end before the next 5-minute trigger: with 480 s on 2026-10-09
# rounds overlapped the following triggers, only 32 of 48 slots started and no
# round finished ready.  240 s leaves the rest of the 300 s slot for start-up,
# the daytime observers and the terminal manifest.
DEFAULT_MAX_ROUND_SECONDS = 240.0
DEFAULT_MAX_SYMBOL_SECONDS = 20.0
PROGRESS_EVERY_SYMBOLS = 10
PROVIDER_TIMEOUT_SECONDS = 8
MINUTE_OFFSET = 240  # One existing provider page, enough for the current session.
CURRENT_RUN_RELATIVE = Path("results/intraday/intraday_refresh_current.json")

MORNING_START = (9, 35)
MORNING_END = (11, 30)
AFTERNOON_START = (13, 5)
AFTERNOON_END = (15, 5)


# ---------------------------------------------------------------------------
# Session / trading-day logic
# ---------------------------------------------------------------------------


def _now_shanghai(now: datetime | None = None) -> datetime:
    current = now if now is not None else datetime.now(SHANGHAI)
    if current.tzinfo is None:
        return current.replace(tzinfo=SHANGHAI)
    return current.astimezone(SHANGHAI)


def _collector_source_fingerprint() -> str:
    """Hash of this collector module, recorded per attempt (B4.1)."""

    try:
        return hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    except OSError:
        return ""


def _load_trading_days(project_root: Path) -> set[str]:
    from quantdesk.trading_calendar import load_a_share_trading_calendar

    calendar = project_root / "data" / "calendar" / "a_share_trading_calendar.csv"
    lookup = load_a_share_trading_calendar(calendar).get("is_open_by_date") or {}
    return {day for day, is_open in lookup.items() if is_open}


def trading_day_today(project_root: Path | None = None, *, now: datetime | None = None) -> bool:
    from quantdesk.trading_calendar import is_trade_day

    root = Path(project_root or PROJECT_ROOT)
    current = _now_shanghai(now)
    calendar = root / "data/calendar/a_share_trading_calendar.csv"
    return is_trade_day(current.date(), calendar_path=calendar)


def market_session_for(now: datetime | None = None) -> str:
    """morning | lunch | afternoon | closed (A-share session clock)."""

    current = _now_shanghai(now)
    hm = (current.hour, current.minute)
    if (9, 30) <= hm <= (11, 30):
        return "morning"
    if (11, 30) < hm < (13, 0):
        return "lunch"
    if (13, 0) <= hm <= (15, 0):
        return "afternoon"
    return "closed"


def in_collection_window(now: datetime | None = None) -> bool:
    current = _now_shanghai(now)
    hm = (current.hour, current.minute)
    if MORNING_START <= hm <= MORNING_END:
        return True
    if AFTERNOON_START <= hm <= AFTERNOON_END:
        return True
    return False


# ---------------------------------------------------------------------------
# Locking
# ---------------------------------------------------------------------------


class _AlreadyRunning(RuntimeError):
    pass


@contextmanager
def _intraday_refresh_lock(root: Path) -> Iterator[Path]:
    """Hold the OS lock for the complete provider/write critical section."""

    lock_path = root / LOCK_RELATIVE
    try:
        with interprocess_file_lock(lock_path, timeout_seconds=0) as acquired:
            yield acquired
    except TimeoutError as exc:
        raise _AlreadyRunning(f"intraday_refresh_already_running:{lock_path}") from exc


# ---------------------------------------------------------------------------
# Minute-bar validation (fail-closed)
# ---------------------------------------------------------------------------


def parse_minute_datetime(value: object) -> datetime | None:
    """Accept TDX ``YYYY-MM-DD HH:MM`` as well as ``YYYY-MM-DD HH:MM:SS``."""

    text = str(value or "").strip().replace("T", " ")
    if not text:
        return None
    for fmt, size in (("%Y-%m-%d %H:%M:%S", 19), ("%Y-%m-%d %H:%M", 16)):
        token = text[:size]
        try:
            return datetime.strptime(token, fmt)
        except ValueError:
            continue
    return None


def validate_minute_bar(row: dict[str, Any], *, now: datetime, symbol: str) -> tuple[bool, str]:
    def _num(name: str) -> float | None:
        try:
            val = float(row.get(name))
        except (TypeError, ValueError):
            return None
        if not math.isfinite(val):
            return None
        return val

    for name in ("open", "high", "low", "close"):
        val = _num(name)
        if val is None or val <= 0:
            return False, f"invalid_minute_bar:{symbol}:{name}_not_positive"
    open_price, high, low, close = (
        _num("open"),
        _num("high"),
        _num("low"),
        _num("close"),
    )
    if None not in (open_price, high, low, close):
        # Both open and close must lie inside [low, high]. Keep the existing
        # reason priority when more than one envelope constraint fails.
        # Float serialization noise (39.690000000000005 vs 39.69) is not an
        # inverted bar; the shared tolerance is ~1e6 times below one tick.
        code = envelope_violation(open_price, high, low, close)
        if code:
            return False, f"invalid_minute_bar:{symbol}:{code}"
    dt_raw = str(row.get("datetime") or "").strip()
    dt = parse_minute_datetime(dt_raw)
    if dt is None:
        return False, f"invalid_minute_bar:{symbol}:datetime_unparseable"
    now_cmp = now
    if now_cmp.tzinfo is not None and dt.tzinfo is None:
        now_cmp = now_cmp.replace(tzinfo=None)
    if dt > now_cmp + timedelta(minutes=2):
        return False, f"invalid_minute_bar:{symbol}:future_datetime"
    # ``dt`` is naive while a caller's ``now`` is usually Shanghai-aware; the
    # comparison must use the same normalised value as the future check above,
    # otherwise every previous-session bar raises TypeError.
    if dt.strftime("%Y-%m-%d") != now_cmp.strftime("%Y-%m-%d") and dt > now_cmp - timedelta(days=1):
        pass  # bars from a previous session are legal (late backfill)
    return True, ""


# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------

MINUTE_COLUMNS = (
    "symbol",
    "datetime",
    "date",
    "time",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "amount",
    "source",
    "updated_at",
    "price_basis",
)
MINUTE_HEADER = ",".join(MINUTE_COLUMNS)

# Markets the raw Sina minute endpoint is verified to serve.  BSE (92xxxx) has no
# verified minute source in this project, so a BSE code must never be sent to the
# Shanghai/Shenzhen endpoint as a guess (2026-09-22 DATA-CAUSES minute cause §2).
_SINA_MINUTE_PREFIXES = {"6": "sh", "0": "sz", "3": "sz"}


def _sina_minute_prefix(symbol: str) -> str:
    """Return the Sina market prefix, or "" when no verified fallback exists."""

    if len(symbol) != 6 or not symbol.isdigit():
        return ""
    return _SINA_MINUTE_PREFIXES.get(symbol[0], "")


def _minute_datetime_key(value: object) -> str:
    text = str(value or "").strip().replace("T", " ")
    # Native ISO parsing keeps a complete archive scan inexpensive. Retain
    # the existing parser for its other accepted strptime spellings.
    for size in (19, 16):
        token = text[:size]
        if (
            len(token) == size and token[4] == "-" and token[7] == "-"
            and token[10] == " " and token[13] == ":"
            and (size == 16 or token[16] == ":")
        ):
            try:
                datetime.fromisoformat(token)
                return token if size == 19 else token + ":00"
            except ValueError:
                pass
    parsed = parse_minute_datetime(value)
    return parsed.strftime("%Y-%m-%d %H:%M:%S") if parsed is not None else ""


def _stored_minute_datetimes(path: Path) -> Iterator[str]:
    """Read timestamp identity by column name, including earlier archived bars."""

    try:
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            reader = csv.reader(handle)
            columns = next(reader, [])
            if columns.count("datetime") != 1:
                return
            column = columns.index("datetime")
            for row in reader:
                key = _minute_datetime_key(row[column]) if len(row) > column else ""
                if key:
                    yield key
    except FileNotFoundError:
        return


def _last_datetime_of(path: Path) -> str:
    # An allowed historical append can be older than an earlier physical row.
    return max(_stored_minute_datetimes(path), default="")


def load_recent_bars(path: Path) -> list[str]:
    """Load archived timestamp identities; older duplicates must also be found.

    The public name is retained for compatibility. A byte-tail window cannot
    guarantee idempotency when a provider repeats an older archived bar.
    """

    return list(_stored_minute_datetimes(path))


def _append_rows(path: Path, rows: list[dict[str, Any]], *, now_iso: str) -> tuple[int, list[str]]:
    """Append validated rows; dedupe on datetime; never rewrite history."""

    accepted, reasons, _latest = _append_rows_with_latest(path, rows, now_iso=now_iso)
    return accepted, reasons


def _append_rows_with_latest(
    path: Path, rows: list[dict[str, Any]], *, now_iso: str
) -> tuple[int, list[str], str]:
    """Share one archive scan between deduplication and freshness reporting."""

    requested_path = path
    path = minute_write_path(path, history_now=datetime.fromisoformat(now_iso.replace("Z", "+00:00")))
    scanned = path.stat() if path.is_file() else None
    exists_with_data = scanned is not None and scanned.st_size > 0
    needs_newline = False
    fieldnames = list(MINUTE_COLUMNS)
    if exists_with_data:
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            header_cols = next(csv.reader(handle), [])
        if len(header_cols) != len(MINUTE_COLUMNS) or set(header_cols) != set(MINUTE_COLUMNS):
            return 0, [f"minute_header_mismatch:{path.name}"], ""
        fieldnames = header_cols
        with path.open("rb") as handle:
            handle.seek(-1, os.SEEK_END)
            needs_newline = handle.read(1) not in {b"\n", b"\r"}

    seen: set[str] = set()
    quarantined: list[dict[str, Any]] = []
    if exists_with_data:
        checked_at = datetime.fromisoformat(now_iso.replace("Z", "+00:00"))
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            reader = csv.reader(handle)
            next(reader, None)
            for line_number, values in enumerate(reader, 2):
                if not values:
                    continue
                stored = dict(zip(fieldnames, values))
                if len(values) == len(fieldnames):
                    # File-level: the archive is not a raw archive of this
                    # symbol. Raw bars appended beside it would mix price
                    # bases or identities, so the whole file stays blocked.
                    if str(stored.get("price_basis") or "").strip().lower() != "raw":
                        return 0, [f"minute_stored_row_invalid:{path.name}:{line_number}:price_basis_not_raw"], ""
                    if stored.get("symbol") != path.stem:
                        return 0, [f"minute_stored_row_invalid:{path.name}:{line_number}:symbol_mismatch"], ""
                    _ok, reason = validate_minute_bar(stored, now=checked_at, symbol=path.stem)
                else:
                    reason = "column_count"
                if reason:
                    # Row-level: one damaged row (a torn tail, a bad price, an
                    # unparseable time) is recorded and isolated. It never
                    # deduplicates a new bar and never counts as freshness;
                    # the rest of the archive stays appendable (2026-10-09:
                    # 73 symbols were refused for a whole day by one row).
                    quarantined.append({
                        "line": line_number, "reason": reason.rsplit(":", 1)[-1],
                        "values": values[: len(fieldnames) + 2],
                    })
                    continue
                seen.add(_minute_datetime_key(stored.get("datetime")))
    latest = max(seen, default="")
    stored_latest = latest
    reject_reasons: list[str] = ([f"legacy_price_basis_unknown:{requested_path.name}"]
                                 if path != requested_path else [])
    if quarantined:
        reject_reasons.append(_record_quarantine(path, quarantined, now_iso=now_iso))
    accepted: list[dict[str, Any]] = []
    for row in rows:
        if str(row.get("price_basis") or "").strip().lower() != "raw":
            reject_reasons.append(f"minute_price_basis_not_raw:{row.get('symbol')}")
            continue
        if "adjust" in row and str(row["adjust"]).strip().lower() != "raw":
            reject_reasons.append(f"minute_price_basis_conflict:{row.get('symbol')}")
            continue
        dt = _minute_datetime_key(row.get("datetime"))
        if not dt:
            reject_reasons.append(f"invalid_minute_datetime:{row.get('symbol')}")
            continue
        if dt in seen:
            reject_reasons.append(f"duplicate_minute_bar:{row.get('symbol')}:{dt}")
            continue
        seen.add(dt)
        latest = max(latest, dt)
        accepted.append(row)
    if not accepted:
        return 0, reject_reasons, latest
    import io

    buffer = io.StringIO(newline="")
    if needs_newline:
        buffer.write("\n")
    writer = csv.DictWriter(
        buffer,
        fieldnames=fieldnames,
        extrasaction="ignore",
        lineterminator="\n",
    )
    if not exists_with_data:
        writer.writeheader()
    for row in accepted:
        writer.writerow(
            {
                "symbol": str(row.get("symbol") or ""),
                "datetime": str(row.get("datetime") or ""),
                "date": str(row.get("date") or ""),
                "time": str(row.get("time") or ""),
                "open": repr(float(row.get("open"))),
                "high": repr(float(row.get("high"))),
                "low": repr(float(row.get("low"))),
                "close": repr(float(row.get("close"))),
                "volume": "%.1f" % float(row.get("volume") or 0),
                "amount": "%.1f" % float(row.get("amount") or 0),
                "source": str(row.get("source") or ""),
                "updated_at": now_iso,
                "price_basis": str(row["price_basis"]).strip().lower(),
            }
        )
    try:
        _atomic_append_bytes(path, buffer.getvalue().encode("utf-8"), scanned=scanned if exists_with_data else None)
    except OSError as exc:
        # The archive is unchanged; this symbol's round is a storage failure,
        # not an aborted round for every other symbol.
        return 0, [*reject_reasons, f"minute_append_failed:{path.name}:{type(exc).__name__}"], stored_latest
    return len(accepted), reject_reasons, latest


def _atomic_append_bytes(path: Path, payload: bytes, *, scanned: os.stat_result | None, attempts: int = 5) -> None:
    """Old bytes unchanged + new rows, published in one ``os.replace``.

    A killed or failed round leaves the previous archive exactly as it was
    (an in-place append could leave half a line).  ``scanned`` is the stat
    taken before the archive was validated: if the file changed since, the
    validation no longer describes it and nothing is written.
    """

    import shutil

    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.append.tmp")
    try:
        with tmp.open("wb") as out:
            if scanned is not None:
                with path.open("rb") as source:
                    current = os.fstat(source.fileno())
                    if (current.st_size, current.st_mtime_ns) != (scanned.st_size, scanned.st_mtime_ns):
                        raise OSError("minute_archive_changed_during_round")
                    shutil.copyfileobj(source, out, 1024 * 1024)
            elif path.exists() and path.stat().st_size > 0:
                raise OSError("minute_archive_appeared_during_round")
            out.write(payload)
            out.flush()
            os.fsync(out.fileno())
        expected = (scanned.st_size if scanned is not None else 0) + len(payload)
        if tmp.stat().st_size != expected:
            raise OSError("minute_append_size_mismatch")
        for attempt in range(max(1, int(attempts))):
            try:
                os.replace(tmp, path)
                return
            except PermissionError:
                # Windows: a reader holding the archive without delete sharing.
                if attempt + 1 >= attempts:
                    raise
                time.sleep(0.05 * (attempt + 1))
    finally:
        tmp.unlink(missing_ok=True)


def _record_quarantine(path: Path, rows: list[dict[str, Any]], *, now_iso: str) -> str:
    """Write the isolated rows beside the archive; the archive is not touched."""

    from collections import Counter

    from quantdesk.utils import atomic_write_json

    reasons = Counter(str(item["reason"]) for item in rows)
    target = path.parent / QUARANTINE_DIR / f"{path.stem}.json"
    code = f"minute_stored_rows_quarantined:{path.name}:{len(rows)}"
    payload = {
        "schema_version": "minute_quarantine_v1",
        "symbol": path.stem,
        "archive": path.name,
        "checked_at": now_iso,
        "quarantined_count": len(rows),
        "reasons": dict(sorted(reasons.items())),
        "rows": rows[:200],
        "rows_truncated": len(rows) > 200,
        "contract": "Rows stay byte-for-byte in the archive; they are never used for "
                    "deduplication, freshness or prices. New bars append after them.",
    }
    try:
        previous = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        previous = {}
    if (previous.get("quarantined_count"), previous.get("reasons")) != (payload["quarantined_count"], payload["reasons"]):
        try:
            atomic_write_json(target, payload)
        except OSError:
            return code + ":record_write_failed"
    return code


# ---------------------------------------------------------------------------
# Provider
# ---------------------------------------------------------------------------


def _elapsed_ms(started: float, monotonic: Callable[[], float]) -> int:
    return max(0, int(round((monotonic() - started) * 1000)))


def _provider_timeout_seconds(
    *,
    symbol_budget_ms: int,
    round_deadline: float,
    monotonic: Callable[[], float],
    default: int = PROVIDER_TIMEOUT_SECONDS,
) -> float:
    """The time one provider call may take: the smallest allowance left.

    A per-symbol budget of 20 seconds is not a promise that a call gets 20
    seconds when the round has only 4 left, and a constant 8-second timeout
    respects neither budget (DSR-07).
    """

    candidates = [float(default)]
    if symbol_budget_ms:
        candidates.append(symbol_budget_ms / 1000.0)
    candidates.append(max(0.0, round_deadline - monotonic()))
    return max(0.0, min(candidates))


def _remaining_round_seconds(round_deadline: float, monotonic: Callable[[], float]) -> float:
    """What is actually left of the round for the next stage to spend."""

    return max(0.0, float(round_deadline) - monotonic())


def _fair_symbol_allowance(remaining_seconds: float, remaining_symbols: int, symbol_limit: float) -> float:
    """Reserve time for receipts and share the rest among every untried name."""
    remaining = max(0.0, float(remaining_seconds))
    reserve = min(5.0, remaining * 0.05)
    share = max(0.0, remaining - reserve) / max(1, int(remaining_symbols))
    limits = [share, float(PROVIDER_TIMEOUT_SECONDS)]
    if symbol_limit > 0:
        limits.append(float(symbol_limit))
    return min(limits)


def _accepts_timeout(fetcher: Callable[..., Any]) -> bool:
    """Legacy two-argument injected fetchers remain supported, without retries."""
    try:
        parameters = inspect.signature(fetcher).parameters
    except (TypeError, ValueError):
        return False
    return "timeout_seconds" in parameters or any(
        item.kind == inspect.Parameter.VAR_KEYWORD for item in parameters.values()
    )


def _record_stage_failure(
    manifest: dict[str, Any],
    exc: BaseException,
    *,
    current_stage: dict[str, Any],
    monotonic: Callable[[], float],
) -> None:
    """Name the stage that failed, not only the exception class (DSR-10).

    A round that aborts must say where it aborted and what it saw there: the
    stage, its duration, the masked error, and the symbol under a provider call.
    """

    name = str(current_stage.get("name") or "") or "unknown"
    entry: dict[str, Any] = {
        "stage": name,
        "outcome": "aborted",
        "error_type": type(exc).__name__,
        "error_stage": _failure_stage(exc),
        "message": _sanitize_error_text(exc),
    }
    http = getattr(exc, "http_diagnostics", None)
    if isinstance(http, Mapping):
        entry["http"] = dict(http)
    started = current_stage.get("started")
    if isinstance(started, (int, float)) and name != "unknown":
        entry["duration_ms"] = _elapsed_ms(float(started), monotonic)
    symbol = str(current_stage.get("symbol") or "")
    if symbol:
        entry["symbol"] = symbol
    manifest["stage_timings"].append(entry)
    manifest["reason_codes"].append(f"stage_aborted:{name}:{type(exc).__name__}")


def _sanitize_error_text(value: Any, limit: int = 160) -> str:
    """Keep the failure class and message, strip anything key-shaped."""

    text = " ".join(str(value or "").split())
    for marker in ("key=", "token=", "sendkey=", "password=", "secret="):
        position = text.lower().find(marker)
        while position >= 0:
            end = text.find("&", position)
            end = len(text) if end < 0 else end
            text = text[:position] + marker + "<redacted>" + text[end:]
            position = text.lower().find(marker, position + len(marker) + 10)
    return text[: limit - 3] + "..." if len(text) > limit else text


def _failure_stage(exc: BaseException) -> str:
    """Name the stage that failed, not just the exception class."""

    name = type(exc).__name__
    if name in {"ConnectionError", "ConnectTimeout", "NewConnectionError", "ProtocolError"}:
        return "connect"
    if name in {"ReadTimeout", "Timeout", "TimeoutError"}:
        return "timeout"
    if name in {"JSONDecodeError", "UnicodeDecodeError", "ValueError", "KeyError", "TypeError", "AttributeError"}:
        return "parse"
    if isinstance(exc, ConnectionError):
        return "connect"
    if isinstance(exc, TimeoutError):
        return "timeout"
    return "request"


def _exception_origin_text(exc: BaseException, *, depth: int = 4) -> str:
    """Surface the wrapped cause a provider hides behind a generic error.

    mootdx raises ``TdxFunctionCallError("calling function error")`` while the
    real socket/protocol failure lives in ``original_exception``.  Without it a
    reader only sees "calling function error" and cannot tell a dead upstream
    from a bad symbol, so the chain is walked (bounded) and recorded.
    """

    parts: list[str] = []
    seen: set[int] = set()
    current: Any = getattr(exc, "original_exception", None)
    if current is None:
        current = getattr(exc, "__cause__", None)
    if current is None:
        current = getattr(exc, "__context__", None)
    while current is not None and len(parts) < depth and id(current) not in seen:
        seen.add(id(current))
        parts.append(f"{type(current).__name__}: {_sanitize_error_text(current)}")
        nxt = getattr(current, "original_exception", None) or getattr(current, "__cause__", None)
        current = nxt
    return " | ".join(parts)


def _provider_exception_evidence(exc: BaseException, *, error_stage: str) -> dict[str, Any]:
    """Message plus the wrapped origin, so the real cause is not discarded."""

    message = _sanitize_error_text(exc)
    origin = _exception_origin_text(exc)
    evidence: dict[str, Any] = {
        "error_stage": error_stage,
        "error_type": type(exc).__name__,
        "message": f"{message} <- {origin}" if origin else message,
    }
    if origin:
        evidence["original_exception"] = origin
    return evidence


class _MootdxDeadlineAdapterError(RuntimeError):
    pass


class _MootdxDeadlineRestoreError(RuntimeError):
    pass


_NATIVE_SOCKET_PATCH_LOCK = threading.RLock()


def _native_time_left(deadline: float, monotonic: Callable[[], float]) -> float:
    left = deadline - monotonic()
    if left <= 0:
        raise TimeoutError("mootdx_symbol_deadline_exhausted")
    return left


class _DeadlineSocket:
    """Bound each native connect/send/recv to the same absolute deadline."""

    _LOCAL_FIELDS = frozenset({"_socket", "_deadline", "_monotonic", "_timeout",
                               "_diagnostics", "_closed_by_owner"})

    def __init__(self, sock: Any, deadline: float, monotonic: Callable[[], float],
                 diagnostics: dict[str, Any]) -> None:
        self._socket = sock
        self._deadline = deadline
        self._monotonic = monotonic
        self._timeout: float | None = None
        self._diagnostics = diagnostics
        self._closed_by_owner = False

    def __getattr__(self, name: str) -> Any:
        return getattr(self._socket, name)

    def __setattr__(self, name: str, value: Any) -> None:
        # The parser updates traffic counters on this object; keep them on the
        # real TrafficStatSocket, alongside its existing socket attributes.
        if name in self._LOCAL_FIELDS:
            object.__setattr__(self, name, value)
        else:
            setattr(self._socket, name, value)

    def _apply_timeout(self) -> None:
        left = _native_time_left(self._deadline, self._monotonic)
        self._socket.settimeout(left if self._timeout is None else min(left, self._timeout))

    def settimeout(self, value: float | None) -> None:
        self._timeout = value
        self._apply_timeout()

    def _io(self, operation: str, *args: Any, **kwargs: Any) -> Any:
        try:
            self._apply_timeout()
            self._diagnostics["native_socket_io_count"] = self._diagnostics.get("native_socket_io_count", 0) + 1
            result = getattr(self._socket, operation)(*args, **kwargs)
            _native_time_left(self._deadline, self._monotonic)
            return result
        except TimeoutError:
            self._diagnostics["native_deadline_operation"] = operation
            raise

    def connect(self, address: Any) -> Any:
        # Socket timeouts do not bound DNS resolution. The inspected native
        # source uses numeric server IPs; reject an unbounded hostname lookup.
        ipaddress.ip_address(str(address[0]))
        return self._io("connect", address)

    def send(self, *args: Any, **kwargs: Any) -> Any:
        return self._io("send", *args, **kwargs)

    def sendall(self, *args: Any, **kwargs: Any) -> Any:
        return self._io("sendall", *args, **kwargs)

    def recv(self, *args: Any, **kwargs: Any) -> Any:
        return self._io("recv", *args, **kwargs)

    def recv_into(self, *args: Any, **kwargs: Any) -> Any:
        return self._io("recv_into", *args, **kwargs)

    def shutdown(self, *args: Any, **kwargs: Any) -> Any:
        if not self._closed_by_owner:
            return self._socket.shutdown(*args, **kwargs)
        return None

    def close(self) -> None:
        if not self._closed_by_owner:
            self._socket.close()
            self._closed_by_owner = True


@contextmanager
def _mootdx_deadline_sockets(
    quotes_module: Any, *, deadline: float, monotonic: Callable[[], float],
    sockets: list[_DeadlineSocket], diagnostics: dict[str, Any],
) -> Iterator[None]:
    """Intercept only this construction thread, restoring the library symbol.

    mootdx has no socket injection argument. Its inspected Python backend
    creates TrafficStatSocket in BaseSocketClient.connect. Other threads keep
    the original socket factory; competing collector patches wait within the
    same deadline. No worker/heartbeat thread is started or left behind.
    """
    api = getattr(quotes_module, "TdxHq_API", None)
    connect = getattr(api, "connect", None)
    if api is None and getattr(quotes_module, "__spec__", None) is None:
        # Preserve manually injected, no-I/O provider modules used by legacy
        # callers/tests. Only this explicit injection seam is cooperative.
        diagnostics["native_deadline_enforcement"] = "cooperative_injected_factory"
        yield
        return
    module_name = str(getattr(connect, "__module__", ""))
    module = importlib.import_module(module_name) if module_name else None
    if module is None or not callable(getattr(module, "TrafficStatSocket", None)):
        diagnostics["native_deadline_enforcement"] = "unavailable"
        raise _MootdxDeadlineAdapterError("native_socket_factory_unavailable")
    if (getattr(quotes_module, "__spec__", None) is not None
            and getattr(connect, "__globals__", None) is not vars(module)):
        diagnostics["native_deadline_enforcement"] = "unavailable"
        raise _MootdxDeadlineAdapterError("native_connect_globals_unverified")
    config = getattr(quotes_module, "config", None)
    if (getattr(quotes_module, "__spec__", None) is not None
            and not callable(getattr(config, "bestip", None))):
        diagnostics["native_deadline_enforcement"] = "unavailable"
        raise _MootdxDeadlineAdapterError("native_config_discovery_guard_unavailable")

    wait_started = monotonic()
    acquired = _NATIVE_SOCKET_PATCH_LOCK.acquire(timeout=_native_time_left(deadline, monotonic))
    diagnostics["socket_patch_lock_wait_ms"] = _elapsed_ms(wait_started, monotonic)
    if not acquired:
        raise TimeoutError("mootdx_socket_adapter_lock_deadline_exhausted")
    original = None
    original_bestip = None
    patches: list[tuple[Any, str, Any, Any]] = []
    active = True
    owner = threading.get_ident()

    def socket_factory(*args: Any, **kwargs: Any) -> Any:
        if not active or threading.get_ident() != owner:
            return original(*args, **kwargs)
        _native_time_left(deadline, monotonic)
        sock = _DeadlineSocket(original(*args, **kwargs), deadline, monotonic, diagnostics)
        sockets.append(sock)
        return sock

    def no_implicit_discovery(*args: Any, **kwargs: Any) -> Any:
        if not active or threading.get_ident() != owner:
            return original_bestip(*args, **kwargs)
        # BaseQuotes always calls config.setup(), even with bestip=False.
        # Missing/invalid config otherwise triggers its own unbounded probes,
        # outside this socket path. Block that branch for the whole construct,
        # including a config disappearing after any earlier read.
        diagnostics["native_discovery_blocked"] = True
        raise _MootdxDeadlineAdapterError("implicit_native_server_discovery_disabled")

    try:
        _native_time_left(deadline, monotonic)
        original = module.TrafficStatSocket
        module.TrafficStatSocket = socket_factory
        patches.append((module, "TrafficStatSocket", original, socket_factory))
        if callable(getattr(config, "bestip", None)):
            original_bestip = config.bestip
            config.bestip = no_implicit_discovery
            patches.append((config, "bestip", original_bestip, no_implicit_discovery))
            diagnostics["native_implicit_discovery"] = "disabled_for_this_thread"
        diagnostics.update(native_deadline_enforcement="socket_io_absolute_deadline",
                           socket_patch_restored=False, native_socket_module=module_name)
        yield
    finally:
        # Even a failed restoration must leave our callable inert. Never
        # silently overwrite a third party's concurrent replacement.
        active = False
        try:
            restore_errors = []
            for target, name, before, replacement in reversed(patches):
                try:
                    if getattr(target, name) is not replacement:
                        reason = ("native_socket_factory_changed_during_call" if name == "TrafficStatSocket"
                                  else "native_config_discovery_guard_changed_during_call")
                        raise _MootdxDeadlineRestoreError(reason)
                    setattr(target, name, before)
                except Exception as exc:
                    restore_errors.append(_sanitize_error_text(exc))
            if restore_errors:
                raise _MootdxDeadlineRestoreError("; ".join(restore_errors))
            if patches:
                diagnostics["socket_patch_restored"] = True
        except Exception as exc:
            diagnostics["socket_patch_restore_error"] = _sanitize_error_text(exc)
            raise
        finally:
            _NATIVE_SOCKET_PATCH_LOCK.release()


def _mootdx_minute_rows_once(
    symbol: str,
    *,
    offset: int,
    now: datetime,
    client_factory: Callable[[], Any] | None = None,
    monotonic: Callable[[], float] = time.monotonic,
    timeout_seconds: float = PROVIDER_TIMEOUT_SECONDS,
) -> tuple[list[dict[str, Any]], str, dict[str, Any]]:
    """Fetch one symbol's minute bars, keeping per-stage failure evidence.

    The caller owns retries and their shared deadline. Library retries stay
    disabled so reconnects cannot restart the time allowance; final exceptions
    remain visible instead of being converted to an unexplained empty frame.

    ``timeout_seconds`` is the round's actual allowance for this symbol, not a
    constant: a call must never be given more time than the budgets it runs
    inside have left (DSR-07).
    """

    diagnostics: dict[str, Any] = {
        "stages": [], "error_stage": "", "error_type": "", "message": "", "rows": 0,
        "timeout_seconds": float(timeout_seconds),
    }
    client: Any = None
    native_sockets: list[_DeadlineSocket] = []
    provider_error = ""
    close_error = ""
    deadline = monotonic() + max(0.0, float(timeout_seconds))

    def remaining() -> float:
        return _native_time_left(deadline, monotonic)

    def request_timeout() -> None:
        left = remaining()
        # mootdx -> TdxHq_API -> socket. Reset the connected socket before
        # requesting bars so connection time cannot be granted again.
        current_client = client
        for _ in range(3):
            if current_client is None:
                break
            setter = getattr(current_client, "settimeout", None)
            if callable(setter):
                setter(left)
                break
            current_client = getattr(current_client, "client", None)
        diagnostics["request_timeout_seconds"] = left

    def _stage(name: str, outcome: str, started: float) -> None:
        diagnostics["stages"].append(
            {"stage": name, "outcome": outcome, "duration_ms": _elapsed_ms(started, monotonic)}
        )

    try:
        started = monotonic()
        try:
            remaining()
            if client_factory is not None:
                diagnostics["native_deadline_enforcement"] = "cooperative_injected_factory"
                client = client_factory()
            else:
                quotes_module = importlib.import_module("mootdx.quotes")
                quotes = quotes_module.Quotes
                # Import, lock wait and every handshake all consume this allowance.
                # Recompute here, never reuse a timeout calculated before import.
                with _mootdx_deadline_sockets(
                    quotes_module, deadline=deadline, monotonic=monotonic,
                    sockets=native_sockets, diagnostics=diagnostics,
                ):
                    client = quotes.factory(
                        market="std", timeout=remaining(), heartbeat=False, bestip=False,
                        auto_retry=False, raise_exception=True,
                    )
            remaining()
        except Exception as exc:  # noqa: BLE001 - surfaced as stage evidence
            _stage("connect", "failed", started)
            diagnostics.update(_provider_exception_evidence(exc, error_stage="connect"))
            provider_error = f"provider_failed:mootdx:{type(exc).__name__}"
        else:
            _stage("connect", "ok", started)

        if not provider_error:
            started = monotonic()
            try:
                request_timeout()
                if (len(symbol) == 6 and symbol.isascii() and symbol.isdigit()
                        and symbol.startswith(("92", "4", "8"))):
                    # mootdx 0.11.x infers Shanghai for 92xxxx. Use the same
                    # connected transport and deadline with explicit BJ=2.
                    count = min(int(offset), 800)
                    diagnostics["native_request"] = {
                        "method": "get_security_bars", "market": 2, "symbol": symbol,
                        "frequency": 8, "start": 0, "count": count,
                    }
                    getter = getattr(getattr(client, "client", None), "get_security_bars", None)
                    if not callable(getter):
                        raise RuntimeError("mootdx BSE transport does not expose get_security_bars")
                    frame = getter(8, 2, symbol, 0, count)
                else:
                    frame = client.bars(symbol=symbol, frequency=8, offset=offset)
                remaining()
            except Exception as exc:  # noqa: BLE001
                _stage("request", "failed", started)
                diagnostics.update(_provider_exception_evidence(exc, error_stage=_failure_stage(exc)))
                provider_error = f"provider_failed:mootdx:{type(exc).__name__}"
            else:
                _stage("request", "ok", started)

        rows: list[dict[str, Any]] = []
        if not provider_error:
            started = monotonic()
            try:
                if frame is None or len(frame) == 0:
                    _stage("parse", "empty", started)
                    diagnostics.update(
                        error_stage="empty", error_type="EmptyResponse",
                        message="provider_returned_no_rows",
                    )
                    provider_error = "provider_no_rows:mootdx"
                else:
                    # Native bars bypass mootdx.to_data(), whose volume
                    # conversion copies vol as-is without scaling.
                    records = (
                        [{**rec, "volume": rec.get("vol", rec.get("volume"))} for rec in frame]
                        if isinstance(frame, list) else frame.to_dict("records")
                    )
                    for rec in records:
                        dt = rec.get("datetime")
                        if dt is None:
                            continue
                        norm = parse_minute_datetime(str(dt))
                        if norm is None:
                            continue
                        rows.append(
                            {
                                "symbol": symbol,
                                "datetime": norm.strftime("%Y-%m-%d %H:%M:%S"),
                                "date": norm.strftime("%Y-%m-%d"),
                                "time": norm.strftime("%H:%M:%S"),
                                "open": float(rec.get("open") or 0),
                                "high": float(rec.get("high") or 0),
                                "low": float(rec.get("low") or 0),
                                "close": float(rec.get("close") or 0),
                                "volume": float(rec.get("volume") or 0),
                                "amount": float(rec["amount"]) if rec.get("amount") is not None else float("nan"),
                                "price_basis": "raw",
                                "source": "mootdx_minute",
                            }
                        )
                    if not rows:
                        _stage("parse", "empty", started)
                        diagnostics.update(
                            error_stage="parse", error_type="EmptyParse",
                            message="no_usable_minute_records",
                        )
                        provider_error = "provider_no_rows:mootdx"
                    else:
                        diagnostics["rows"] = len(rows)
                        _stage("parse", "ok", started)
            except Exception as exc:  # noqa: BLE001
                _stage("parse", "failed", started)
                diagnostics.update(_provider_exception_evidence(exc, error_stage="parse"))
                rows = []
                provider_error = f"provider_parse_failed:mootdx:{type(exc).__name__}"

    finally:
        close = getattr(client, "close", None)
        if callable(close):
            try:
                close()
            except Exception as exc:  # noqa: BLE001 - close failures are reported too
                close_error = f"provider_close_failed:mootdx:{type(exc).__name__}"
                diagnostics["close_error"] = close_error
        for sock in native_sockets:
            try:
                sock.close()
            except Exception as exc:  # noqa: BLE001 - factory failures can precede client assignment
                close_error = f"provider_close_failed:mootdx:{type(exc).__name__}"
                diagnostics.setdefault("socket_close_errors", []).append(_sanitize_error_text(exc))
                diagnostics["close_error"] = close_error
        diagnostics["native_socket_count"] = len(native_sockets)
        diagnostics["native_sockets_closed"] = all(sock._closed_by_owner for sock in native_sockets)
    _ = now

    if provider_error:
        return [], provider_error, diagnostics
    if close_error:
        return [], close_error, diagnostics
    if monotonic() >= deadline:
        diagnostics.update(error_stage="timeout", error_type="TimeoutError", message="mootdx_symbol_deadline_exhausted")
        return [], "provider_failed:mootdx:TimeoutError", diagnostics
    return rows, "", diagnostics


def _native_retry_allowed(diagnostics: Mapping[str, Any]) -> bool:
    """A failed adapter/discovery guard must survive both retry layers."""
    return not (
        diagnostics.get("socket_patch_restore_error") or diagnostics.get("native_discovery_blocked")
        or diagnostics.get("native_deadline_enforcement") == "unavailable"
    )


def _mootdx_minute_rows(
    symbol: str,
    *,
    offset: int,
    now: datetime,
    client_factory: Callable[[], Any] | None = None,
    monotonic: Callable[[], float] = time.monotonic,
    timeout_seconds: float = PROVIDER_TIMEOUT_SECONDS,
    max_attempts: int = 3,
) -> tuple[list[dict[str, Any]], str, dict[str, Any]]:
    """Retry short connection/request failures within one symbol allowance."""
    deadline = monotonic() + max(0.0, float(timeout_seconds))
    attempts: list[dict[str, Any]] = []
    result: tuple[list[dict[str, Any]], str, dict[str, Any]] = (
        [], "provider_failed:mootdx:TimeoutError",
        {"stages": [], "error_stage": "timeout", "error_type": "TimeoutError",
         "message": "mootdx_symbol_deadline_exhausted", "rows": 0},
    )
    attempt_limit = max(1, min(3, int(max_attempts)))
    for attempt in range(1, attempt_limit + 1):
        left = deadline - monotonic()
        if left <= 0:
            break
        result = _mootdx_minute_rows_once(
            symbol, offset=offset, now=now, client_factory=client_factory,
            monotonic=monotonic, timeout_seconds=left,
        )
        rows, error, diagnostics = result
        attempts.append({"attempt": attempt, "error": error, **diagnostics})
        if not error or not error.startswith("provider_failed:mootdx:"):
            break
        if diagnostics.get("error_stage") not in {"connect", "request", "timeout"}:
            break
        if not _native_retry_allowed(diagnostics):
            break
        delay = 0.1 * attempt
        if attempt == attempt_limit or deadline - monotonic() <= delay:
            break
        time.sleep(delay)
    rows, error, diagnostics = result
    return rows, error, {**diagnostics, "attempt_count": len(attempts), "attempts": attempts}


_SINA_HTTP_MAX_BODY_BYTES = 1024 * 1024
# This small stdlib-only child is the sole owner of the HTTP socket. A parent
# timeout can terminate DNS/TLS/header/body waits, and its own watchdog also
# bounds the request if the collector itself exits. No work thread is abandoned
# in the collector. Python 3.10+ monotonic clocks share the same OS reference.
_SINA_HTTP_PROGRAM = r'''
import hashlib
import http.client
import json
import os
import sys
import threading
import time
import urllib.request

def emit(value):
    print(json.dumps(value, ensure_ascii=True, separators=(",", ":")), flush=True)

def run():
    url, user_agent, cutoff, body_limit = sys.argv[1:]
    cutoff, body_limit = float(cutoff), int(body_limit)
    evidence = {"phase": "open", "received_bytes": None}
    response = None
    watchdog = None
    result = {"event": "result", "ok": False}
    try:
        left = cutoff - time.monotonic()
        if left <= 0:
            raise TimeoutError("sina_http_deadline_exhausted_before_open")
        watchdog = threading.Timer(left, lambda: os._exit(124))
        watchdog.daemon = True
        watchdog.start()
        emit({"event": "stage", "http": dict(evidence)})
        request = urllib.request.Request(url, headers={"User-Agent": user_agent})
        response = urllib.request.urlopen(request, timeout=left)
        evidence.update(phase="read", status=getattr(response, "status", None))
        headers = response.headers
        length_text = headers.get("Content-Length")
        length = int(length_text) if length_text is not None else None
        chunked = str(headers.get("Transfer-Encoding", "")).lower().strip() == "chunked"
        evidence.update(content_length=length, transfer_encoding="chunked" if chunked else "not_chunked")
        if length is not None and (length < 0 or length > body_limit):
            raise ValueError("sina_http_content_length_out_of_bounds")
        emit({"event": "stage", "http": dict(evidence)})
        body = response.read(body_limit + 1)
        evidence["received_bytes"] = len(body)
        if len(body) > body_limit:
            raise ValueError("sina_http_body_too_large")
        # read(n) may silently return a short fixed-length response. Reject it
        # even if its prefix happens to be a syntactically complete JSON list.
        if not chunked and length is not None and len(body) < length:
            raise http.client.IncompleteRead(body, length - len(body))
        if not chunked and length is not None and len(body) != length:
            raise ValueError("sina_http_content_length_mismatch")
        evidence["phase"] = "decode"
        # Preserve UTF-8; silently dropping corrupt bytes can alter a price.
        text = body.decode("utf-8", errors="strict")
        if time.monotonic() >= cutoff:
            raise TimeoutError("sina_http_deadline_exhausted_after_read")
        result.update(ok=True, text=text)
    except Exception as exc:
        result.update(error_type=type(exc).__name__, message=str(exc)[:1200])
        if isinstance(exc, http.client.IncompleteRead):
            chain = []
            current, seen = exc, set()
            for _ in range(4):
                if current is None or id(current) in seen:
                    break
                seen.add(id(current))
                if isinstance(current, http.client.IncompleteRead):
                    chain.append({"partial_bytes": len(current.partial), "expected_bytes": current.expected,
                                  "partial_sha256": hashlib.sha256(current.partial).hexdigest()})
                current = current.__cause__ or current.__context__
            # Chunked readers can hide the final truncated chunk in a cause.
            # These partial buffers need not be disjoint: never invent a total.
            evidence.update(received_bytes=len(exc.partial) if len(chain) == 1 and current is None else None,
                            reported_partial_bytes=len(exc.partial), incomplete_read_chain=chain,
                            incomplete_chain_truncated=current is not None,
                            incomplete_expected_bytes=exc.expected,
                            partial_sha256=hashlib.sha256(exc.partial).hexdigest())
        cause = getattr(exc, "reason", None) or exc.__cause__ or exc.__context__
        if cause is not None:
            result["original_exception"] = (type(cause).__name__ + ": " + str(cause))[:1200]
    finally:
        if response is not None:
            try:
                response.close()
            except Exception as exc:
                evidence["response_close_error"] = type(exc).__name__
                if result.get("ok"):
                    result = {"event": "result", "ok": False,
                              "error_type": type(exc).__name__, "message": "sina_http_response_close_failed"}
        result["http"] = evidence
        # The watchdog remains active through response cleanup and output.
        emit(result)
        if watchdog is not None:
            watchdog.cancel()

if __name__ == "__main__":
    run()
'''


class _SinaHTTPFailure(RuntimeError):
    def __init__(self, error_type: str, message: str, http: Mapping[str, Any], *, origin: str = "") -> None:
        super().__init__(_sanitize_error_text(message))
        self.error_type = error_type
        self.http = dict(http)
        self.origin = _sanitize_error_text(origin)


# An OS cleanup failure must not spawn additional sockets in later symbols or
# rounds. Retain the actual child handle until its exit can be confirmed.
@dataclass
class _SinaHTTPPending:
    process: Any
    output_drained: bool = False


_SINA_HTTP_UNREAPED: _SinaHTTPPending | None = None


def _sina_http_pending_failure(
    *, deadline: float, monotonic: Callable[[], float],
) -> _SinaHTTPFailure | None:
    global _SINA_HTTP_UNREAPED
    if _SINA_HTTP_UNREAPED is None:
        return None
    pending = _SINA_HTTP_UNREAPED
    exited = False
    cleanup_error = ""
    interrupted = None
    try:
        exited = pending.process.poll() is not None
        if exited and not pending.output_drained:
            # POSIX communicate(timeout=0) can expire before reading any EOF.
            # Borrow a small positive slice of the caller's existing budget.
            left = min(0.05, max(0.0, deadline - monotonic()))
            if left <= 0:
                raise TimeoutError("sina_http_pending_cleanup_budget_exhausted")
            pending.process.communicate(timeout=left)
            pending.output_drained = True
        if exited and pending.output_drained:
            _SINA_HTTP_UNREAPED = None
            return None
    except BaseException as exc:
        cleanup_error = f"{type(exc).__name__}: {_sanitize_error_text(exc)}"
        if not isinstance(exc, Exception):
            interrupted = exc
    http = {
        "phase": "cleanup", "cleanup": "unconfirmed", "stop_collection": True,
        "process_exit": "confirmed" if exited else "unconfirmed",
        "output_drain": "confirmed" if pending.output_drained else "unconfirmed",
        "cleanup_error": cleanup_error,
        "process_id": pending.process.pid, "deadline_enforcement": "process_watchdog",
    }
    if interrupted is not None:
        setattr(interrupted, "http_diagnostics", http)
        raise interrupted
    return _SinaHTTPFailure("HTTPWorkerCleanupError", "sina_http_worker_cleanup_unconfirmed", http)


def _sina_http_frames(output: bytes) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """Keep complete progress records, but never accept a partial final record."""
    evidence: dict[str, Any] = {}
    final = None
    if len(output) > _SINA_HTTP_MAX_BODY_BYTES * 6 + 65536:
        return evidence, final
    for line in output.splitlines(keepends=True):
        if not line.endswith(b"\n"):
            return evidence, None
        try:
            item = json.loads(line)
        except (ValueError, UnicodeDecodeError):
            return evidence, None
        if not isinstance(item, dict) or item.get("event") not in {"stage", "result"} or final is not None:
            return evidence, None
        if isinstance(item.get("http"), dict):
            evidence.update(item["http"])
        if item["event"] == "result":
            final = item
    return evidence, final


def _sina_http_get_text(
    url: str, *, timeout: float, monotonic: Callable[[], float] = time.monotonic,
    popen_factory: Callable[..., Any] | None = None,
) -> tuple[str, dict[str, Any]]:
    """One HTTP request; launch, request, cancellation and reap share a budget."""
    global _SINA_HTTP_UNREAPED
    started = monotonic()
    deadline = started + max(0.0, float(timeout))
    pending = _sina_http_pending_failure(deadline=deadline, monotonic=monotonic)
    if pending is not None:
        raise pending
    http: dict[str, Any] = {"phase": "launch", "deadline_enforcement": "process_watchdog",
                            "granted_seconds": max(0.0, float(timeout)), "cleanup": "not_started"}
    if not math.isfinite(timeout) or timeout <= 0:
        raise _SinaHTTPFailure("TimeoutError", "sina_http_no_budget", http)
    # Reserve inside, not after, the caller's allowance. Stdlib process launch
    # itself is synchronous; an OS-delayed launch is recorded and immediately
    # cancelled, never labelled an on-time response.
    reserve = min(0.2, timeout * 0.1)
    cutoff = deadline - reserve
    http["cleanup_reserve_seconds"] = reserve
    from quantdesk.version import PACKAGE_VERSION
    process = None
    pending_state = None
    output = b""
    failure: tuple[str, str] | None = None
    interrupted: BaseException | None = None
    try:
        if monotonic() >= cutoff:
            raise TimeoutError("sina_http_deadline_exhausted_before_launch")
        factory = popen_factory or subprocess.Popen
        process = factory(
            [sys.executable, "-I", "-S", "-B", "-c", _SINA_HTTP_PROGRAM,
             url, f"QuantDesk/{PACKAGE_VERSION}", str(cutoff), str(_SINA_HTTP_MAX_BODY_BYTES)],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            close_fds=True, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        pending_state = _SinaHTTPPending(process)
        _SINA_HTTP_UNREAPED = pending_state
        http.update(process_id=process.pid, cleanup="pending")
        left = cutoff - monotonic()
        if left <= 0:
            raise TimeoutError("sina_http_deadline_exhausted_during_launch")
        output, _ = process.communicate(timeout=left)
        pending_state.output_drained = True
        if process.returncode == 124:
            failure = ("TimeoutError", "sina_http_child_total_deadline_exhausted")
        elif process.returncode != 0:
            failure = ("HTTPWorkerError", "sina_http_worker_failed")
    except subprocess.TimeoutExpired as exc:
        output = exc.output or b""
        failure = ("TimeoutError", "sina_http_total_deadline_exhausted")
    except Exception as exc:
        failure = (type(exc).__name__, _sanitize_error_text(exc))
    except BaseException as exc:
        interrupted = exc
    finally:
        if process is not None:
            if pending_state is None:
                pending_state = _SinaHTTPPending(process)
                _SINA_HTTP_UNREAPED = pending_state
            try:
                if process.poll() is None:
                    process.kill()  # Only this Popen child; never a PID/name search.
                    http["terminated"] = True
                if http.get("terminated") or not pending_state.output_drained:
                    drained, _ = process.communicate(timeout=max(0.0, deadline - monotonic()))
                    output = drained or output
                    pending_state.output_drained = True
                if process.poll() is None:
                    raise TimeoutError("sina_http_worker_exit_unconfirmed")
                http.update(cleanup="confirmed", process_exit="confirmed", output_drain="confirmed")
                if _SINA_HTTP_UNREAPED is pending_state:
                    _SINA_HTTP_UNREAPED = None
            except BaseException as exc:
                http["cleanup_error"] = _sanitize_error_text(exc)
                http["cleanup_error_type"] = type(exc).__name__
                try:
                    exited = process.poll() is not None
                except BaseException as poll_exc:
                    exited = False
                    http["cleanup_poll_error_type"] = type(poll_exc).__name__
                    if not isinstance(poll_exc, Exception):
                        interrupted = poll_exc
                drained = pending_state is not None and pending_state.output_drained
                http.update(process_exit="confirmed" if exited else "unconfirmed",
                            output_drain="confirmed" if drained else "unconfirmed",
                            cleanup="confirmed" if exited and drained else "unconfirmed")
                if not (exited and drained):
                    _SINA_HTTP_UNREAPED = pending_state
                    http["stop_collection"] = True
                    failure = ("HTTPWorkerCleanupError", "sina_http_worker_cleanup_unconfirmed")
                elif _SINA_HTTP_UNREAPED is pending_state:
                    _SINA_HTTP_UNREAPED = None
                if not isinstance(exc, Exception):
                    interrupted = exc
    progress, result = _sina_http_frames(output)
    http = {**progress, **http, "phase": progress.get("phase", http["phase"]),
            "elapsed_ms": _elapsed_ms(started, monotonic), "deadline_exceeded": monotonic() >= deadline}
    if result is not None and result.get("ok") is not True:
        http["child_error"] = {
            key: _sanitize_error_text(result[key])
            for key in ("error_type", "message", "original_exception") if key in result
        }
    if interrupted is not None:
        setattr(interrupted, "http_diagnostics", http)
        raise interrupted
    if failure is None and monotonic() >= deadline:
        failure = ("TimeoutError", "sina_http_deadline_exhausted_after_cleanup")
    if failure is not None:
        raise _SinaHTTPFailure(*failure, http)
    if result is None:
        raise _SinaHTTPFailure("HTTPWorkerProtocolError", "sina_http_worker_result_incomplete", http)
    if result.get("ok") is not True:
        raise _SinaHTTPFailure(str(result.get("error_type") or "HTTPWorkerError"),
                               str(result.get("message") or "sina_http_request_failed"), http,
                               origin=str(result.get("original_exception") or ""))
    if not isinstance(result.get("text"), str):
        raise _SinaHTTPFailure("HTTPWorkerProtocolError", "sina_http_worker_text_missing", http)
    return result["text"], http


def _sina_minute_rows(
    symbol: str, *, offset: int, now: datetime,
    monotonic: Callable[[], float] = time.monotonic,
    timeout_seconds: float = PROVIDER_TIMEOUT_SECONDS,
) -> tuple[list[dict[str, Any]], str, dict[str, Any]]:
    """Read this source's dated raw OHLCV; absent turnover stays absent."""
    deadline = monotonic() + max(0.0, float(timeout_seconds))
    now = _now_shanghai(now)
    sina_prefix = _sina_minute_prefix(symbol)
    started = monotonic()
    http: dict[str, Any] = {}
    try:
        if not sina_prefix:
            raise ValueError("sina_minute_market_unsupported")
        import pandas as pd
        from quantdesk.intraday.intraday_providers import _normalize_provider_frame

        prefix = sina_prefix
        url = ("https://quotes.sina.cn/cn/api/json_v2.php/CN_MarketData.getKLineData"
               f"?symbol={prefix}{symbol}&scale=1&ma=no&datalen=240")
        left = deadline - monotonic()
        if left <= 0:
            raise TimeoutError("minute_provider_deadline_exhausted")
        text, http = _sina_http_get_text(url, timeout=left, monotonic=monotonic)
        payload = json.loads(text)
        records = []
        rejected: list[str] = []
        for row in payload if isinstance(payload, list) else []:
            if not isinstance(row, Mapping):
                rejected.append("invalid_record")
                continue
            stamp = str(row.get("day") or row.get("datetime") or "")
            # The generic normalizer accepts time-only observations. This raw
            # K-line source must supply its own date; never fill in today.
            if parse_minute_datetime(stamp) is None:
                rejected.append("missing_full_datetime")
                continue
            try:
                values = {key: float(row[key]) for key in ("open", "high", "low", "close", "volume")}
            except (KeyError, TypeError, ValueError):
                rejected.append("missing_ohlcv")
                continue
            if not all(math.isfinite(value) for value in values.values()) or values["volume"] < 0:
                rejected.append("invalid_ohlcv")
                continue
            record = {"symbol": symbol, "datetime": stamp, **values}
            valid, reason = validate_minute_bar(record, now=now, symbol=symbol)
            if not valid:
                rejected.append(reason)
                continue
            records.append(record)
        result = _normalize_provider_frame(
            "sina_intraday", symbol, now.strftime("%Y-%m-%d"), pd.DataFrame(records),
            source="sina_intraday", price_basis="raw",
        )
        frame = result.get("data")
        if monotonic() >= deadline:
            raise TimeoutError("minute_provider_deadline_exhausted")
        if (result.get("status") != "ok" or result.get("price_only_intraday")
                or result.get("price_basis") != "raw"
                or result.get("ohlcv_complete") is not True or frame is None or frame.empty):
            raise ValueError("sina_minute_ohlcv_unavailable")
        rows = frame.tail(max(1, int(offset))).to_dict("records")
        return rows, "", {
            "provider": "sina_intraday", "rows": len(rows), "error_stage": "", "error_type": "", "message": "",
            "http": http,
            "rejected_rows": len(rejected), "rejection_reasons": sorted(set(rejected)),
            "stages": [{"stage": "request", "provider": "sina_intraday", "outcome": "ok",
                        "duration_ms": _elapsed_ms(started, monotonic)}],
        }
    except Exception as exc:  # noqa: BLE001 - keep the real failure stage and cause
        evidence = _provider_exception_evidence(exc, error_stage=_failure_stage(exc))
        if isinstance(exc, _SinaHTTPFailure):
            http = exc.http
            stage = ("cleanup" if http.get("cleanup") == "unconfirmed" else
                     "timeout" if exc.error_type == "TimeoutError" else
                     "parse" if exc.error_type in {"UnicodeDecodeError", "ValueError"} else "request")
            evidence.update(error_type=exc.error_type, error_stage=stage)
            if exc.origin:
                evidence["original_exception"] = exc.origin
                evidence["message"] += " <- " + exc.origin
        return [], f"provider_failed:sina_intraday:{evidence['error_type']}", {
            "provider": "sina_intraday", "rows": 0,
            **evidence, "http": http, "stop_collection": bool(http.get("stop_collection")),
            "stages": [{"stage": "request", "provider": "sina_intraday", "outcome": "failed",
                        "duration_ms": _elapsed_ms(started, monotonic)}],
        }


def _live_minute_rows(
    symbol: str, *, offset: int, now: datetime,
    monotonic: Callable[[], float] = time.monotonic,
    timeout_seconds: float = PROVIDER_TIMEOUT_SECONDS,
    preferred_provider: str = "mootdx",
) -> tuple[list[dict[str, Any]], str, dict[str, Any]]:
    """Bound both sources together and reuse proven source health this round."""
    deadline = monotonic() + max(0.0, float(timeout_seconds))
    pending = _sina_http_pending_failure(deadline=deadline, monotonic=monotonic)
    if pending is not None:
        return [], "provider_failed:sina_intraday:HTTPWorkerCleanupError", {
            "error_type": pending.error_type, "error_stage": "cleanup", "message": str(pending),
            "http": pending.http, "stop_collection": True,
        }
    timeout_seconds = max(0.0, deadline - monotonic())
    if timeout_seconds <= 0:
        return [], "provider_failed:deadline_exhausted", {
            "error_type": "TimeoutError", "error_stage": "timeout",
            "message": "minute_provider_deadline_exhausted_after_http_cleanup",
        }
    supported = bool(_sina_minute_prefix(symbol))
    if preferred_provider == "sina_intraday" and supported:
        # A prior symbol proved the native source failed and Sina worked. Avoid
        # spending that same connection timeout for every remaining symbol.
        rows, error, diagnostics = _sina_minute_rows(
            symbol, offset=offset, now=now, monotonic=monotonic,
            timeout_seconds=max(0.0, timeout_seconds * 0.8),
        )
        first = {"provider": "sina_intraday", "error": error, **diagnostics}
        if not error or diagnostics.get("stop_collection"):
            return rows, error, {**diagnostics, "routing": "round_sina_first", "attempts": [first]}
        left = deadline - monotonic()
        if left <= 0:
            return [], error, {**diagnostics, "routing": "round_sina_first", "attempts": [first]}
        rows, native_error, native_diagnostics = _mootdx_minute_rows(
            symbol, offset=offset, now=now, monotonic=monotonic,
            timeout_seconds=left, max_attempts=1,
        )
        return rows, native_error, {
            **native_diagnostics, "provider": "mootdx", "routing": "round_sina_first",
            "fallback_from": "sina_intraday", "primary_error": error,
            "attempts": [first, {"provider": "mootdx", "error": native_error, **native_diagnostics}],
        }

    native_allowance = min(2.0, timeout_seconds / 3) if supported else timeout_seconds
    rows, error, diagnostics = _mootdx_minute_rows(
        symbol, offset=offset, now=now, monotonic=monotonic,
        timeout_seconds=native_allowance, max_attempts=1 if supported else 3,
    )
    if not error or not supported or monotonic() >= deadline:
        return rows, error, diagnostics
    primary = {"provider": "mootdx", "error": error, **diagnostics}
    fallback_started = monotonic()
    fallback_rows, fallback_error, fallback_diagnostics = _sina_minute_rows(
        symbol, offset=offset, now=now, monotonic=monotonic,
        timeout_seconds=max(0.0, deadline - monotonic()),
    )
    attempts = [primary, {"provider": "sina_intraday", "error": fallback_error, **fallback_diagnostics}]
    if fallback_diagnostics.get("stop_collection"):
        return [], fallback_error, {**fallback_diagnostics, "attempts": attempts}
    if not fallback_error:
        return fallback_rows, "", {
            **fallback_diagnostics, "fallback_from": "mootdx", "primary_error": error,
            "primary_error_stage": diagnostics.get("error_stage") or "",
            "attempts": attempts,
            "stages": [*diagnostics.get("stages", []), {"stage": "fallback", "outcome": "ok",
                        "duration_ms": _elapsed_ms(fallback_started, monotonic)}],
        }
    diagnostics = {
        **diagnostics,
        "fallback_error": f"{fallback_diagnostics.get('error_type')}:{fallback_diagnostics.get('message')}",
        "attempts": attempts,
    }
    left = deadline - monotonic()
    if left > 0 and _native_retry_allowed(diagnostics):
        retry_rows, retry_error, retry_diag = _mootdx_minute_rows(
            symbol, offset=offset, now=now, monotonic=monotonic, timeout_seconds=left, max_attempts=2,
        )
        return retry_rows, retry_error, {**retry_diag, "fallback_error": diagnostics["fallback_error"],
                                        "prior_attempts": diagnostics["attempts"]}
    return [], error, diagnostics


_BUILTIN_MINUTE_FETCHERS = (_mootdx_minute_rows, _sina_minute_rows, _live_minute_rows)


def _prepare_minute_sources(
    *, deadline: float, monotonic: Callable[[], float],
) -> list[dict[str, Any]]:
    """Charge cold imports to the round before assigning any symbol a share."""
    entries: list[dict[str, Any]] = []
    sources = (("mootdx", _mootdx_minute_rows, _BUILTIN_MINUTE_FETCHERS[0]),
               ("sina_intraday", _sina_minute_rows, _BUILTIN_MINUTE_FETCHERS[1]))
    for provider, fetcher, original in sources:
        if fetcher is not original:
            continue  # An injected provider owns its own initialization.
        started = monotonic()
        entry: dict[str, Any] = {"provider": provider, "outcome": "ok"}
        try:
            if monotonic() >= deadline:
                raise TimeoutError("round_budget_exhausted:provider_initialization")
            if provider == "mootdx":
                importlib.import_module("mootdx.quotes").Quotes
            else:
                importlib.import_module("pandas")
                importlib.import_module("quantdesk.intraday.intraday_providers")
            if monotonic() >= deadline:
                raise TimeoutError("round_budget_exhausted:provider_initialization")
        except Exception as exc:  # noqa: BLE001 - the other source may still be available
            entry.update(outcome="failed", **_provider_exception_evidence(exc, error_stage="initialize"))
        entry["duration_ms"] = _elapsed_ms(started, monotonic)
        entries.append(entry)
    return entries


# ---------------------------------------------------------------------------
# Main entry
# ---------------------------------------------------------------------------


def _unpack_fetch_result(value: Any) -> tuple[list[dict[str, Any]], str, dict[str, Any]]:
    """Accept the legacy ``(rows, error)`` contract plus richer diagnostics.

    Injected test fetchers and older callers return two-tuples; the real
    provider additionally returns the per-stage evidence collected above.
    """

    if value is None:
        return [], "", {}
    if isinstance(value, Mapping):
        rows = value.get("rows") or []
        return list(rows), str(value.get("error") or ""), dict(value.get("diagnostics") or {})
    if isinstance(value, tuple):
        if len(value) >= 3:
            return list(value[0] or []), str(value[1] or ""), dict(value[2] or {})
        if len(value) == 2:
            return list(value[0] or []), str(value[1] or ""), {}
        return [], "", {}
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return list(value), "", {}
    return [], "", {}


def _reason_stage(reason: str) -> str:
    text = str(reason or "")
    if text.startswith("provider_no_rows") or text.startswith("provider_empty"):
        return "empty"
    if text.startswith("provider_parse_failed"):
        return "parse"
    if text.startswith("provider_close_failed"):
        return "close"
    if text.startswith("network_not_authorized"):
        return "skipped"
    tail = text.rsplit(":", 1)[-1]
    if tail in {"ReadTimeout", "Timeout", "TimeoutError", "ConnectTimeout"}:
        return "timeout"
    if tail in {"ConnectionError", "NewConnectionError", "ProtocolError"}:
        return "connect"
    return "request"


def _read_current_run(root: Path) -> dict[str, Any]:
    try:
        payload = json.loads((root / CURRENT_RUN_RELATIVE).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _write_run_marker(root: Path, manifest: dict[str, Any]) -> None:
    """Progress evidence for a round that may be killed before it finishes."""

    from quantdesk.utils import atomic_write_json

    atomic_write_json(root / CURRENT_RUN_RELATIVE, manifest)


# ERROR_INVALID_PARAMETER is what Windows OpenProcess reports when the PID
# does not exist.  Every other failure (access denied, unknown) means the
# liveness could not be determined and must not be reported as death.
_WINDOWS_ERROR_INVALID_PARAMETER = 87
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000


def _process_alive(pid: int) -> bool:
    """Whether a PID is still running (cross-platform, no signal sent).

    Returns ``True`` whenever liveness cannot be determined, so an
    unanswerable query never fabricates a dead process or a terminal
    ``abandoned`` marker.  Only a definitive "does not exist" yields ``False``.
    """

    if pid <= 0:
        return False
    if os.name == "nt":
        try:
            import ctypes

            kernel32 = ctypes.windll.kernel32
            handle = kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
        except Exception:  # noqa: BLE001 - liveness check must never raise
            return True
        if handle:
            try:
                kernel32.CloseHandle(handle)
            except Exception:  # noqa: BLE001 - closing failure must not flip liveness
                pass
            return True
        try:
            error = int(kernel32.GetLastError())
        except Exception:  # noqa: BLE001 - unknown error number -> cannot determine
            return True
        if error == _WINDOWS_ERROR_INVALID_PARAMETER:
            return False
        return True
    try:
        os.kill(int(pid), 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        # Ambiguous (e.g. platform-specific error); do not claim death.
        return True
    return True


def reconcile_abandoned_round(
    project_root: Path | None = None,
    *,
    now: datetime | None = None,
    process_alive: Callable[[int], bool] = _process_alive,
    write: bool = True,
) -> dict[str, Any]:
    """Publish a terminal state for a previous round whose process is gone.

    A round killed by the task scheduler (2026-09-11 15:05, exit 0x41306) wrote
    a ``running`` marker and then disappeared.  Because it was the last
    invocation of the day, nothing overwrote it and ``current``/``latest`` kept
    showing a dead process as running.  Any later invocation - the next round,
    a manual run, or ``--reconcile-only`` - calls this first, so the stale
    marker becomes an explicit ``abandoned`` terminal record instead of an
    apparently-live round.  Read-only when ``write=False``.
    """

    root = Path(project_root or PROJECT_ROOT)
    current_now = _now_shanghai(now)
    marker = _read_current_run(root)
    if not marker or marker.get("run_state") != "running":
        return {"reconciled": False, "reason": "no_running_marker"}
    pid = int(marker.get("runtime_pid") or 0)
    if pid <= 0:
        # No PID to prove the process is gone; the ordinary ``previous_run``
        # handling in the next round covers this case.
        return {"reconciled": False, "reason": "runtime_pid_unknown"}
    if process_alive(pid):
        return {"reconciled": False, "reason": "process_alive", "runtime_pid": pid}
    rid = str(marker.get("run_id") or "")

    def _abandoned(source: dict[str, Any]) -> dict[str, Any]:
        result = dict(source)
        result["run_state"] = "abandoned"
        if result.get("status") in {"ready", "started", "running", "", None}:
            result["status"] = "aborted"
        result["finished_at"] = result.get("finished_at") or current_now.strftime("%Y-%m-%dT%H:%M:%S")
        result["reason_codes"] = list(
            dict.fromkeys([*(result.get("reason_codes") or []), "previous_run_process_gone"])
        )
        return result

    if not write:
        return {
            "reconciled": True, "run_id": rid, "runtime_pid": pid,
            "written": False, "manifest": _abandoned(marker),
        }
    try:
        with _intraday_refresh_lock(root):
            # Re-read inside the lock (MIN-01): another round may have completed
            # between the liveness check above and lock acquisition.  Publish
            # only when the marker is still exactly the same running round, so a
            # stale reconcile can never write an old round over a newer one.
            locked = _read_current_run(root)
            if not locked or locked.get("run_state") != "running":
                return {"reconciled": False, "reason": "marker_changed_before_lock"}
            if (
                str(locked.get("run_id") or "") != rid
                or int(locked.get("runtime_pid") or 0) != pid
            ):
                return {
                    "reconciled": False,
                    "reason": "marker_changed_before_lock",
                    "run_id": str(locked.get("run_id") or ""),
                }
            _write_manifests(
                root,
                rid or f"abandoned-{current_now.strftime('%Y%m%dT%H%M%S')}",
                _abandoned(locked),
                touch_current=True,
            )
    except _AlreadyRunning:
        return {"reconciled": False, "reason": "active_round_holds_lock", "runtime_pid": pid}
    return {"reconciled": True, "run_id": rid, "runtime_pid": pid, "written": True}


def collect_intraday_refresh(
    project_root: Path | None = None,
    *,
    network: bool = False,
    now: datetime | None = None,
    fetcher: Callable[..., Any] | None = None,
    scope_builder: Callable[..., dict[str, Any]] | None = None,
    snapshot_fetcher: Callable[[], tuple[Any, str]] | None = None,
    run_id: str = "",
    attempt_id: str = "",
    scheduler_instance: str = "",
    write_manifest: bool = True,
    trading_day: bool | None = None,
    max_round_seconds: float = DEFAULT_MAX_ROUND_SECONDS,
    max_symbol_seconds: float = DEFAULT_MAX_SYMBOL_SECONDS,
    monotonic: Callable[[], float] = time.monotonic,
    read_ai_minute_focus: bool = False,
) -> dict[str, Any]:
    """One intraday refresh round. Returns the manifest payload.

    The round is self-bounded by ``max_round_seconds`` so it can never be
    killed by the task's own execution limit without leaving evidence.
    """

    root = Path(project_root or PROJECT_ROOT)
    current = _now_shanghai(now)
    rid = run_id or current.strftime("intraday-%Y%m%dT%H%M%S")
    session = market_session_for(current)
    is_trading_day = trading_day_today(root, now=current) if trading_day is None else trading_day

    if write_manifest:
        # Close a previous round whose process is gone before doing anything
        # else, so a killed last-round marker (2026-09-11 15:05) can never keep
        # ``current``/``latest`` showing a dead PID as running.
        try:
            reconcile_abandoned_round(root, now=current)
        except Exception:  # noqa: BLE001 - reconciliation must not break a round
            LOG.warning("intraday_reconcile_abandoned_failed")

    # The budgets the current round is actually running inside.  The default
    # provider is built from them, so a call is never given more time than the
    # round and the symbol both have left.
    budget_clock: dict[str, float] = {"symbol_ms": 0.0, "round_deadline": 1e9}
    source_route = {"preferred_provider": "mootdx", "evidence_symbol": "", "reason": ""}

    def _default_fetch(
        symbol: str, offset: int, *, timeout_seconds: float | None = None,
    ) -> tuple[list[dict[str, Any]], str, dict[str, Any]]:
        rows, error, diagnostics = _live_minute_rows(
            symbol,
            offset=offset,
            now=current,
            monotonic=monotonic,
            timeout_seconds=timeout_seconds if timeout_seconds is not None else _provider_timeout_seconds(
                symbol_budget_ms=int(budget_clock["symbol_ms"]),
                round_deadline=float(budget_clock["round_deadline"]),
                monotonic=monotonic,
            ),
            preferred_provider=source_route["preferred_provider"],
        )
        used_route = dict(source_route)
        if not error and diagnostics.get("fallback_from") == "mootdx":
            if diagnostics.get("primary_error_stage") in {"connect", "request", "timeout"}:
                source_route.update(preferred_provider="sina_intraday", evidence_symbol=symbol,
                                    reason=str(diagnostics.get("primary_error") or ""))
        elif not error and diagnostics.get("fallback_from") == "sina_intraday":
            source_route.update(preferred_provider="mootdx", evidence_symbol=symbol,
                                reason=str(diagnostics.get("primary_error") or ""))
        return rows, error, {**diagnostics, "round_source_route": used_route}

    fetch = fetcher or _default_fetch
    fetch_accepts_timeout = _accepts_timeout(fetch)
    round_started = monotonic()

    manifest: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "run_id": rid,
        "attempt_id": attempt_id or f"{rid}#1",
        "run_state": "started",
        "target_date": current.strftime("%Y-%m-%d"),
        "as_of": current.strftime("%H:%M:%S"),
        "run_started_at": current.strftime("%Y-%m-%dT%H:%M:%S"),
        "finished_at": "",
        "runtime_pid": os.getpid(),
        "source_fingerprint": _collector_source_fingerprint(),
        "scheduler_instance": scheduler_instance or "unknown",
        "round_budget_seconds": float(max_round_seconds),
        "symbol_budget_seconds": float(max_symbol_seconds),
        "round_duration_ms": 0,
        "stage_timings": [],
        "not_attempted_symbols": [],
        "not_attempted_reasons": {},
        "budget_allocation": "remaining_round_shared_across_untried_symbols",
        "ai_minute_focus_read_enabled": read_ai_minute_focus is True,
        "previous_run": {},
        "market_session": session if is_trading_day else "closed",
        "network": network,
        "requested_count": 0,
        "fresh_count": 0,
        "partial_count": 0,
        "missing_count": 0,
        "stale_count": 0,
        "appended_rows": 0,
        "latest_bar_time": "",
        "max_lag_seconds": 0,
        "status": "ready",
        "provider_status": [],
        "reason_codes": [],
        "source_hash": "",
        "research_only": True,
        "no_broker": True,
        "snapshot": {},
    }

    if not is_trading_day:
        # A non-trading day reports its state and contacts nothing - no
        # snapshot, no scope, no provider.  It must still not take over the
        # shared files from a round that owns them (DSR-09).
        manifest["status"] = "closed"
        manifest["run_state"] = "skipped"
        manifest["finished_at"] = current.isoformat(timespec="seconds")
        manifest["reason_codes"] = ["non_trading_day"]
        if write_manifest:
            try:
                with _intraday_refresh_lock(root):
                    _write_manifests(root, rid, manifest)
            except _AlreadyRunning:
                manifest["reason_codes"].append("shared_state_owned_by_active_round")
                _write_manifests(root, rid, manifest, touch_current=False)
        return manifest

    if not in_collection_window(current):
        # After 15:05 or before 09:35 on a trading day we still report state,
        # but never fetch (the 15:05 round is the last collector invocation).
        manifest["status"] = "closed"
        manifest["run_state"] = "skipped"
        manifest["finished_at"] = current.isoformat(timespec="seconds")
        manifest["reason_codes"] = ["outside_collection_window"]
        if write_manifest:
            try:
                with _intraday_refresh_lock(root):
                    _write_manifests(root, rid, manifest)
            except _AlreadyRunning:
                manifest["reason_codes"].append("shared_state_owned_by_active_round")
                _write_manifests(root, rid, manifest, touch_current=False)
        return manifest

    # The shared ``current`` marker and the shared ``latest`` file are
    # single-writer resources.  Every round that may write them - including the
    # read-only offline/closed reports - must own the same interprocess lock,
    # and every terminal write must happen before that lock is released
    # (DSR-09).  A second instance may only leave its own busy record.
    wants_provider_work = manifest["status"] != "closed" and bool(network)
    lock_guard = None
    lock_owned = False
    if write_manifest or wants_provider_work:
        # A round that will fetch must hold the lock even when it is not asked
        # to write manifests: two collectors hitting the provider at once is
        # exactly what the lock is for.
        try:
            lock_guard = _intraday_refresh_lock(root)
            lock_guard.__enter__()
            lock_owned = True
        except _AlreadyRunning as exc:
            if wants_provider_work:
                manifest["status"] = "already_running"
                manifest["run_state"] = "skipped"
                manifest["reason_codes"].append(str(exc))
                if write_manifest:
                    _write_busy_attempt(root, rid, manifest)
                return manifest
            # A read-only report keeps its own status, but it must not take over
            # the shared files from the round that is actually running.
            manifest["reason_codes"].append("shared_state_owned_by_active_round")

    aborted: BaseException | None = None
    current_stage: dict[str, Any] = {"name": "", "symbol": "", "started": 0.0}

    def _begin_stage(name: str, symbol: str = "") -> None:
        current_stage.update({"name": name, "symbol": symbol, "started": monotonic()})

    try:
        # A previous round that never wrote a terminal manifest was killed by
        # something outside this process.  Record it; never let the stale
        # ``latest`` file pass itself off as this round's result.  Judging a
        # round abandoned is itself a shared-state decision, so it is only made
        # by the round that owns the lock.
        previous = _read_current_run(root) if lock_owned else {}
        if previous and previous.get("run_id") != rid and previous.get("run_state") == "running":
            manifest["previous_run"] = {
                "run_id": previous.get("run_id"),
                "status": previous.get("status"),
                "as_of": previous.get("as_of"),
                "attempted_symbols": len(previous.get("provider_status") or []),
                "appended_rows": previous.get("appended_rows"),
            }
            manifest["reason_codes"].append(f"previous_run_abandoned:{previous.get('run_id')}")

        if write_manifest and lock_owned and wants_provider_work:
            # Progress evidence for a round that may be killed mid-flight.  A
            # round that will not fetch never writes it: it would otherwise
            # leave a "running" marker it has no terminal state to resolve.
            manifest["run_state"] = "running"
            _write_run_marker(root, manifest)

        round_deadline = round_started + max(0.0, float(max_round_seconds))
        budget_clock["round_deadline"] = round_deadline
        budget_exhausted = False

        _begin_stage("snapshot")
        snapshot_started = monotonic()
        snapshot_payload = refresh_full_market_snapshot(
            root,
            network=wants_provider_work,
            now=current,
            fetcher=snapshot_fetcher,
            # Scope expansion must leave most of the round for its actual
            # minute targets. A valid synced A0 slot avoids this fetch entirely.
            budget_seconds=min(60.0, _remaining_round_seconds(round_deadline, monotonic)),
            clock=monotonic,
        )
        snapshot_finished = monotonic()
        manifest["stage_timings"].append(
            {"stage": "snapshot", "outcome": "ok" if snapshot_payload.get("ok") else "failed",
             "duration_ms": _elapsed_ms(snapshot_started, monotonic),
             "budget_overrun": snapshot_finished > round_deadline}
        )
        manifest["snapshot"] = {
            "ok": snapshot_payload.get("ok"),
            "status": snapshot_payload.get("status"),
            "row_count": snapshot_payload.get("row_count") or 0,
            "mover_count": snapshot_payload.get("mover_count") or 0,
            "path": snapshot_payload.get("path") or "",
            "source": snapshot_payload.get("source") or "",
            "reason_codes": list(snapshot_payload.get("reason_codes") or []),
            "error": snapshot_payload.get("error") or "",
        }
        for key in ("published", "pagination", "reused", "reused_at", "archive_path", "archive_sha256",
                    "archive_slot", "archive_row_count", "archive_quality", "source_fetched_at",
                    "capture_age_seconds", "max_quote_age_seconds", "excluded_quote_count"):
            if key in snapshot_payload:
                manifest["snapshot"][key] = snapshot_payload[key]
        if wants_provider_work and snapshot_finished > round_deadline:
            # The snapshot spent the whole round: scope and the provider loop
            # must not start on a budget that is already gone.
            budget_exhausted = True
            manifest["status"] = "timed_out"
            manifest["reason_codes"].append("round_budget_exhausted:snapshot_over_budget")

        extra_symbols = list(snapshot_payload.get("movers") or [])
        _begin_stage("scope")
        scope_started = monotonic()
        if budget_exhausted:
            scope = {"symbols": [], "warnings": []}
        elif scope_builder is None:
            scope = build_dynamic_scope(root, now=current, extra_symbols=extra_symbols,
                                        read_ai_minute_focus=read_ai_minute_focus)
        else:
            try:
                scope = scope_builder(root, extra_symbols=extra_symbols)
            except TypeError:
                scope = scope_builder(root)
        scope_finished = monotonic()
        manifest["stage_timings"].append(
            {"stage": "scope",
             "outcome": "skipped" if budget_exhausted else "ok",
             "duration_ms": _elapsed_ms(scope_started, monotonic),
             "budget_overrun": not budget_exhausted and scope_finished > round_deadline}
        )
        manifest["reason_codes"].extend(f"scope:{w}" for w in scope.get("warnings", []))
        symbols = scope.get("symbols") or []
        manifest["ai_minute_focus"] = [
            {"symbol": entry["symbol"], "provenance": entry["focus_provenance"]}
            for entry in symbols if entry.get("focus_provenance")
        ]
        critical = {s["symbol"] for s in symbols if s.get("priority") == "critical"}
        manifest["requested_count"] = len(symbols)
        if wants_provider_work and not budget_exhausted and scope_finished > round_deadline:
            budget_exhausted = True
            manifest["status"] = "timed_out"
            manifest["reason_codes"].append("round_budget_exhausted:scope_over_budget")

        now_iso = current.strftime("%Y-%m-%dT%H:%M:%S")
        if manifest["status"] != "closed" and not network:
            manifest["status"] = "planned_offline"
            manifest["run_state"] = "skipped"
            manifest["reason_codes"].append("network_not_authorized")

        elif manifest["status"] != "closed" and symbols and not budget_exhausted:
            intraday_dir = root / INTRADAY_DIR_NAME
            intraday_dir.mkdir(parents=True, exist_ok=True)
            provider_failed = 0
            provider_started = monotonic()
            deadline = round_deadline
            max_symbol_ms = max(0, int(float(max_symbol_seconds) * 1000))
            # Publish the live budgets to the default provider.  Without this the
            # caller's ``max_symbol_seconds`` only ever produced a post-hoc
            # ``symbol_budget_exceeded`` note; the call itself could still block
            # for its own constant timeout (DSR-07).
            budget_clock["symbol_ms"] = float(max_symbol_ms)
            budget_clock["round_deadline"] = deadline
            if fetcher is None and _live_minute_rows is _BUILTIN_MINUTE_FETCHERS[2]:
                _begin_stage("provider_initialization")
                initialized = monotonic()
                entries = _prepare_minute_sources(deadline=deadline, monotonic=monotonic)
                manifest["provider_initialization"] = entries
                manifest["stage_timings"].append({
                    "stage": "provider_initialization",
                    "outcome": "failed" if any(row["outcome"] == "failed" for row in entries) else "ok",
                    "duration_ms": _elapsed_ms(initialized, monotonic),
                    "budget_overrun": monotonic() >= deadline,
                })
            for index, entry in enumerate(symbols):
                symbol = entry["symbol"]
                symbol_started = monotonic()
                allowance = _fair_symbol_allowance(
                    max(0.0, deadline - symbol_started), len(symbols) - index,
                    float(max_symbol_seconds),
                )
                if symbol_started >= deadline or allowance < 0.001:
                    # Stop ourselves with evidence instead of running into the
                    # task's 10-minute kill (which writes nothing at all).
                    manifest["status"] = "timed_out"
                    manifest["not_attempted_symbols"] = [item["symbol"] for item in symbols[index:]]
                    manifest["not_attempted_reasons"] = {
                        code: "round_budget_exhausted" for code in manifest["not_attempted_symbols"]
                    }
                    manifest["reason_codes"].append(
                        "round_budget_exhausted:"
                        f"{len(manifest['not_attempted_symbols'])}_symbols_not_attempted"
                    )
                    break
                symbol_deadline = symbol_started + allowance
                budget_clock["symbol_ms"] = allowance * 1000.0
                _begin_stage("provider", symbol)
                path = intraday_dir / f"{symbol}.csv"
                fetched = (fetch(symbol, MINUTE_OFFSET, timeout_seconds=allowance)
                           if fetch_accepts_timeout else fetch(symbol, MINUTE_OFFSET))
                rows, err, diagnostics = _unpack_fetch_result(
                    fetched
                )
                duration_ms = _elapsed_ms(symbol_started, monotonic)
                if not err and (monotonic() >= deadline or monotonic() >= symbol_deadline):
                    rows = []
                    err = "provider_failed:deadline_exhausted"
                    diagnostics = {**diagnostics, "error_stage": "timeout", "error_type": "TimeoutError",
                                   "message": "Provider response arrived after its allowed window; nothing appended."}
                if err:
                    manifest["provider_status"].append({
                        "symbol": symbol,
                        "status": "failed",
                        "reason": err,
                        "error_stage": diagnostics.get("error_stage") or _reason_stage(err),
                        "error_type": diagnostics.get("error_type") or err.rsplit(":", 1)[-1],
                        "message": diagnostics.get("message") or "",
                        "stages": list(diagnostics.get("stages") or []),
                        "diagnostics": diagnostics,
                        "duration_ms": duration_ms,
                        "granted_budget_seconds": allowance,
                        "priority": entry.get("priority"),
                    })
                    provider_failed += 1
                    if diagnostics.get("stop_collection"):
                        manifest["status"] = "partial"
                        manifest["not_attempted_symbols"] = [item["symbol"] for item in symbols[index + 1:]]
                        manifest["not_attempted_reasons"] = {
                            code: "http_worker_cleanup_unconfirmed" for code in manifest["not_attempted_symbols"]
                        }
                        manifest["reason_codes"].append("minute_http_worker_cleanup_unconfirmed")
                        break
                    if not _sina_minute_prefix(symbol):
                        # No verified fallback for this market: keep the failure a
                        # visible gap instead of letting it look recoverable.
                        manifest["reason_codes"].append(
                            f"minute_fallback_unavailable:{symbol}"
                        )
                    if max_symbol_ms and duration_ms > max_symbol_ms:
                        manifest["reason_codes"].append(
                            f"symbol_budget_exceeded:{symbol}:{duration_ms}ms"
                        )
                else:
                    valid_rows = []
                    validation_reason = ""
                    ignored_other_day_rows = 0
                    for row in rows:
                        ok, reason = validate_minute_bar(row, now=current, symbol=symbol)
                        if ok and str(row.get("price_basis") or "").strip().lower() != "raw":
                            ok, reason = False, f"minute_price_basis_not_raw:{symbol}"
                        if ok and "adjust" in row and str(row["adjust"]).strip().lower() != "raw":
                            ok, reason = False, f"minute_price_basis_conflict:{symbol}"
                        if ok and _minute_datetime_key(row.get("datetime"))[:10] != current.strftime("%Y-%m-%d"):
                            ignored_other_day_rows += 1
                            continue
                        if ok:
                            valid_rows.append(row)
                        else:
                            manifest["reason_codes"].append(reason)
                            validation_reason = validation_reason or reason
                    accepted, rejects, newest = _append_rows_with_latest(path, valid_rows, now_iso=now_iso)
                    actual_path = minute_write_path(path, history_now=current)
                    manifest["appended_rows"] += accepted
                    manifest["reason_codes"].extend(rejects[:3])
                    quarantine_reason = next(
                        (reason for reason in rejects if reason.startswith("minute_stored_rows_quarantined:")), ""
                    )
                    if quarantine_reason and quarantine_reason not in rejects[:3]:
                        manifest["reason_codes"].append(quarantine_reason)
                    storage_reason = next(
                        (reason for reason in rejects if reason.startswith((
                            "minute_header_mismatch:", "minute_stored_row_invalid:", "minute_append_failed:",
                        ))), ""
                    )
                    receipt_status = "ok"
                    receipt_reason = ""
                    error_stage = diagnostics.get("error_stage") or ""
                    error_type = ""
                    if storage_reason:
                        receipt_status, receipt_reason = "storage_blocked", storage_reason
                        error_stage, error_type = "storage", storage_reason.split(":", 1)[0]
                    elif not valid_rows:
                        # A successful request is not proof of usable data, even
                        # when an older archive supplies a recent timestamp.
                        receipt_status = "validation_failed" if rows else "empty"
                        receipt_reason = validation_reason or f"provider_no_rows:{symbol}"
                        error_stage = "validation" if rows else "empty"
                        error_type = "NoValidMinuteBars" if rows else "EmptyResponse"
                        if not validation_reason:
                            manifest["reason_codes"].append(receipt_reason)
                    manifest["provider_status"].append({
                        "symbol": symbol,
                        "status": receipt_status,
                        "reason": receipt_reason,
                        "appended": accepted,
                        "quarantined_rows": int(quarantine_reason.split(":")[2]) if quarantine_reason else 0,
                        "latest_bar_time": newest,
                        "storage_path": str(actual_path.relative_to(root)),
                        "legacy_price_basis_unknown": actual_path != path,
                        "requested_bar_limit": MINUTE_OFFSET,
                        "ignored_other_day_rows": ignored_other_day_rows,
                        "error_stage": error_stage,
                        "error_type": error_type,
                        "stages": list(diagnostics.get("stages") or []),
                        "diagnostics": diagnostics,
                        "duration_ms": duration_ms,
                        "granted_budget_seconds": allowance,
                        "priority": entry.get("priority"),
                    })
                    if max_symbol_ms and duration_ms > max_symbol_ms:
                        manifest["reason_codes"].append(
                            f"symbol_budget_exceeded:{symbol}:{duration_ms}ms"
                        )
                    if newest:
                        manifest["latest_bar_time"] = max(manifest["latest_bar_time"], newest)
                if write_manifest and (index + 1) % PROGRESS_EVERY_SYMBOLS == 0:
                    manifest["run_state"] = "running"
                    _write_run_marker(root, manifest)
            if monotonic() > deadline and manifest["status"] != "timed_out":
                # The budget can be blown by the last symbol (or by the opaque
                # snapshot call); that must not be reported as a clean round.
                manifest["status"] = "timed_out"
                manifest["not_attempted_symbols"] = list(manifest["not_attempted_symbols"])
                manifest["reason_codes"].append(
                    "round_budget_exhausted:all_symbols_attempted_over_budget"
                )
            manifest["provider_failed_count"] = provider_failed
            manifest["stage_timings"].append({
                "stage": "provider",
                "outcome": manifest["status"] if manifest["status"] in {"timed_out"} else (
                    "failed" if provider_failed and provider_failed == len(symbols) else "ok"
                ),
                "duration_ms": _elapsed_ms(provider_started, monotonic),
                "symbols_attempted": len(manifest["provider_status"]),
                "symbols_requested": len(symbols),
            })
            if provider_failed and provider_failed == len(symbols):
                # Every provider call failed.  A round that already stopped
                # itself on its own budget keeps ``timed_out``: the two states
                # are not interchangeable and the round must not be relabelled
                # once it has declared its budget exhausted (DSR-07).
                manifest["reason_codes"].append("provider_all_failed")
                if manifest["status"] != "timed_out":
                    manifest["status"] = "blocked"
        elif budget_exhausted:
            # The round ran out of budget before the provider stage: the scope it
            # never reached is recorded as untried, not as a clean round.
            manifest["not_attempted_symbols"] = [item["symbol"] for item in symbols]
            manifest["not_attempted_reasons"] = {
                code: "round_budget_exhausted_before_provider" for code in manifest["not_attempted_symbols"]
            }

        # The round budget is unconditional.  An empty scope, or a stage that
        # overran without the provider loop ever running, must not be able to
        # finish "ready" after the budget is gone.
        if (
            manifest["status"] not in {"closed", "already_running", "aborted", "timed_out"}
            and monotonic() > round_deadline
        ):
            manifest["status"] = "timed_out"
            manifest["reason_codes"].append(
                "round_budget_exhausted:round_over_budget_before_terminal_write"
            )

        # Freshness evaluation (session-aware; lunch break never reports stale).
        _evaluate_freshness(manifest, current=current, critical=critical, session=manifest["market_session"])
    except BaseException as exc:
        # A catchable failure must still leave this round's own terminal state.
        # Without this, the previous round's ``latest`` is all a reader finds and
        # nothing says this round failed.
        aborted = exc
        manifest["run_state"] = "aborted"
        if manifest.get("status") in {"ready", "started"}:
            manifest["status"] = "aborted"
        manifest["reason_codes"].append(f"round_aborted:{type(exc).__name__}")
        # Only the failing stage is missing from the record; name it, its
        # duration and its symbol instead of leaving just the exception class.
        _record_stage_failure(
            manifest, exc, current_stage=current_stage, monotonic=monotonic
        )
        raise
    finally:
        manifest["round_duration_ms"] = _elapsed_ms(round_started, monotonic)
        manifest["finished_at"] = _now_shanghai(None).strftime("%Y-%m-%dT%H:%M:%S")
        if manifest["run_state"] in {"running", "started"}:
            manifest["run_state"] = "completed"
        # The terminal write happens while this round still owns the lock, so a
        # successor that legally acquires the lock next can never be overwritten
        # by a straggler (DSR-09).  Failures are raised only on the clean path:
        # an abort must report its own exception, not a write error.
        try:
            if write_manifest:
                _write_manifests(root, rid, manifest, touch_current=lock_owned)
        except OSError:
            if aborted is None:
                raise
            LOG.warning("intraday_round_aborted_manifest_write_failed")
        finally:
            if lock_guard is not None:
                lock_guard.__exit__(None, None, None)
    return manifest


def _evaluate_freshness(manifest: dict[str, Any], *, current: datetime, critical: set[str], session: str) -> None:
    if session in ("closed", "lunch"):
        # Freshness is not actionable outside trading hours, so nothing is
        # flagged stale.  The counts are still facts about this round: leaving
        # the initial ``ready`` / ``missing_count=0`` values in place lets a
        # round that wrote nothing look complete (2026-09-22: 82 ok / 8 missing
        # was published as ``ready``).  Same aggregation as the trading-hours
        # branch below, minus the stale verdicts.
        manifest["stale_count"] = 0
        fresh = 0
        missing = 0
        for entry in manifest["provider_status"]:
            if entry.get("status") == "ok":
                fresh += 1
            else:
                missing += 1
        manifest["fresh_count"] = fresh
        manifest["missing_count"] = missing
        if missing and manifest["status"] not in (
            "already_running", "blocked", "closed", "timed_out", "planned_offline",
        ):
            manifest["status"] = "partial"
        return
    stale = 0
    fresh = 0
    partial = 0
    missing = 0
    critical_missing: list[str] = []
    for entry in manifest["provider_status"]:
        if entry.get("status") != "ok":
            missing += 1
            if entry.get("symbol") in critical:
                critical_missing.append(entry["symbol"])
            continue
        latest = str(entry.get("latest_bar_time") or "")
        lag = None
        if latest:
            latest_dt = parse_minute_datetime(latest)
            if latest_dt is None:
                lag = None
            else:
                lag = max(0.0, (current - latest_dt.replace(tzinfo=SHANGHAI)).total_seconds())
        if lag is None:
            stale += 1
        elif lag > FRESH_MAX_LAG_SECONDS:
            stale += 1
            manifest["max_lag_seconds"] = max(manifest["max_lag_seconds"], int(lag))
        else:
            fresh += 1
            manifest["max_lag_seconds"] = max(manifest["max_lag_seconds"], int(lag))
    manifest["fresh_count"] = fresh
    manifest["stale_count"] = stale
    manifest["partial_count"] = partial
    manifest["missing_count"] = missing
    if critical_missing:
        manifest["reason_codes"].append("critical_scope_missing:" + ",".join(critical_missing[:8]))
    # ``timed_out`` is a distinct terminal state: the round stopped itself on
    # its own budget, so freshness must be reported without relabelling it as
    # ready/partial/stale.  ``planned_offline`` never contacted a provider at
    # all, so there is no freshness to judge.
    if manifest["status"] not in ("closed", "already_running", "blocked", "timed_out", "planned_offline"):
        if missing == 0 and stale == 0:
            manifest["status"] = "ready"
        elif critical_missing or stale:
            manifest["status"] = "stale" if fresh else "blocked"
        else:
            manifest["status"] = "partial"


def _write_busy_attempt(root: Path, run_id: str, manifest: dict[str, Any]) -> None:
    """Record a refused round without touching the active round's evidence."""

    directory = root / RESULTS_DIR_NAME / "busy_attempts"
    payload = {
        "schema_version": SCHEMA_VERSION,
        "record": "intraday_busy_attempt",
        "run_id": run_id,
    }
    for key in ("run_state", "status", "as_of", "target_date", "requested_count", "reason_codes"):
        payload[key] = manifest.get(key)
    try:
        directory.mkdir(parents=True, exist_ok=True)
        (directory / f"{run_id}.json").write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except OSError:
        pass


def _write_manifests(
    root: Path, run_id: str, manifest: dict[str, Any], *, touch_current: bool = True
) -> None:
    """Write this round's terminal state.

    ``touch_current=False`` means the caller does not own the interprocess lock:
    it may record its own run directory, but the shared ``latest`` and
    ``current`` files belong to the round that does (DSR-09).

    Every file is written independently.  ``current`` is what tells a reader
    whether a round is still alive, so one failed write must never be allowed to
    skip it - the failures are reported after the writable files are updated
    (DSR-10).
    """

    results_dir = root / RESULTS_DIR_NAME
    runs_dir = results_dir / "runs" / run_id
    targets: list[Path] = [runs_dir / "manifest.json"]
    if touch_current:
        targets.append(results_dir / "intraday_refresh_latest.json")
        targets.append(root / CURRENT_RUN_RELATIVE)
    failures: list[str] = []
    for path in targets:
        # Re-serialise per target so a later file still carries this round's
        # terminal state *and* the note that an earlier write failed.
        manifest["write_failures"] = list(failures)
        blob = json.dumps(manifest, ensure_ascii=False, indent=2)
        try:
            if path == root / CURRENT_RUN_RELATIVE:
                # The marker must stay atomic: a half-written marker is
                # indistinguishable from a dead round.
                _write_run_marker(root, manifest)
            else:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(blob, encoding="utf-8")
        except OSError as exc:
            failures.append(f"{path.name}:{type(exc).__name__}")
    if failures:
        manifest["write_failures"] = list(failures)
        raise OSError("intraday_manifest_write_failed:" + ",".join(failures))


