"""Pipeline-owned, date-verified full-A close refresh.

The live-list path only accepts today's completed session. Each symbol is
bound to its own dated Sina HQ quote before a raw bar or valuation is written.
If the list endpoint is unavailable, the existing dated HQ endpoint can supply
raw OHLCV for the local universe. Valuation fields remain explicitly missing.
List and quote requests are batched and paced; neither fetches stock history.

Families:
  1. bars_and_valuation - Sina list plus dated HQ batches; raw append-only
     history with atomic file publication. Anomalies are review labels, never
     inferred suspension evidence.
  2. benchmarks - reuses quantdesk.data_truth.benchmark_incremental_update.
  3. announcements - project news provider for the pool in
     config/data_refresh_news_pool.txt (one request per symbol, 2s pacing).
  4. quarterly - earnings (yjbb) + disclosure schedule, only in
     disclosure-season months (1,4,7,8,10), paced small pages.

Health report: results/data_refresh/data_refresh_latest.json (+ dated copy).
Direct invocation requires a current Pipeline writer lease in addition to
network authorization. Offline invocation writes nothing. Research only.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import math
import os
import sys
import time
from contextvars import ContextVar
from datetime import date, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

import requests  # noqa: E402

from quantdesk.data_truth.daily_bar_validity import daily_bar_validity_from_row  # noqa: E402
from quantdesk.interprocess_lock import interprocess_file_lock  # noqa: E402
from quantdesk.process_deadline import ProcessDeadlineResult, run_process_tasks_until_deadline  # noqa: E402
from quantdesk.universe_refresh import discover_daily_universe_symbols  # noqa: E402
from scripts.run_data_close_pipeline import (  # noqa: E402
    MarketWriterLease, MarketWriterUnauthorized, _market_refresh_progress_path,
)

DAILY_DIR = PROJECT_ROOT / "data" / "daily"
META_DIR = PROJECT_ROOT / "data" / "metadata"
VAL_DIR = META_DIR / "valuation_snapshots"
EVIDENCE_DIR = META_DIR / "suspension_evidence"
RESULTS_DIR = PROJECT_ROOT / "results" / "data_refresh"
NEWS_POOL_FILE = PROJECT_ROOT / "config" / "data_refresh_news_pool.txt"
UNIVERSE_FILE = PROJECT_ROOT / "data" / "universe" / "a_share_symbols.csv"
DAILY_HEADER = "date,symbol,open,high,low,close,volume,amount,change,pct_chg,adjust,source,updated_at"

UA = "Mozilla/5.0 QuantDesk research client"
SINA_URL = "https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/Market_Center.getHQNodeData"
DISCLOSURE_MONTHS = {1, 4, 7, 8, 10}
UNIVERSE_MISMATCH_WARN_THRESHOLD = 30
# One bounded re-check per symbol the list snapshot silently dropped (9/21:
# 689009 sat in the local roster and in no HQ request at all).  The cap keeps a
# list outage from turning into a thousands-request sweep, which this project
# requires explicit authorization for; codes above the cap are recorded as not
# attempted instead of being skipped without a trace.
UNIVERSE_DIFF_REQUERY_LIMIT = 50
SHANGHAI = ZoneInfo("Asia/Shanghai")
_OPTIONAL_DEADLINE: ContextVar[float | None] = ContextVar("daily_refresh_deadline", default=None)
# bars_and_valuation's own deadline. On 2026-10-09 the 15:40 run had written
# every bar by 15:47:31 and was still killed at the stage's 900 s limit, so the
# whole run read "failed". Bars stop with evidence before the budget is gone;
# the optional families stop earlier still, leaving time for the final report.
_BARS_DEADLINE: ContextVar[float | None] = ContextVar("daily_refresh_bars_deadline", default=None)
BARS_PUBLISH_RESERVE_SECONDS = 90.0
OPTIONAL_PUBLISH_RESERVE_SECONDS = 60.0
_DISCOVERY_SNAPSHOT_ROOTS: tuple[Path, ...] = ()


def _now_iso() -> str:
    return datetime.now(SHANGHAI).isoformat(timespec="seconds")


def _remaining_refresh_seconds() -> float | None:
    deadline = _OPTIONAL_DEADLINE.get()
    return None if deadline is None else max(0.0, deadline - time.monotonic())


def _bars_time_left() -> float | None:
    deadline = _BARS_DEADLINE.get()
    return None if deadline is None else deadline - time.monotonic()


def _bars_expired() -> bool:
    left = _bars_time_left()
    return left is not None and left <= 0


def _reserve(budget: float, reserve: float) -> float:
    """A reserve that scales down with a small budget (never the whole budget)."""

    return min(float(reserve), max(0.0, float(budget)) * 0.10)


def _optional_fetch_worker(task: dict[str, Any], context: dict[str, Any]) -> Any:
    """Fetch in a killable process; only the parent owns canonical publication."""
    if task.get("delay_seconds"):
        time.sleep(float(task["delay_seconds"]))
    kind = task["kind"]
    if kind == "news":
        from quantdesk.providers.news_provider import _fetch_announce_eastmoney

        return _fetch_announce_eastmoney(task["symbol"], 50)
    if kind == "benchmarks":
        from quantdesk.data_truth.benchmark_incremental_update import default_index_fetcher, update_benchmark_files

        # This updater writes only inside the parent's disposable staged root.
        return update_benchmark_files(Path(task["staged_root"]), fetcher=default_index_fetcher, target_date=task["target"])
    import akshare as ak

    year = task["year"]
    if kind == "earnings":
        errors = []
        for period in (f"{year}0630", f"{year}0331", f"{year - 1}1231"):
            try:
                frame = ak.stock_yjbb_em(date=period)
                if frame is not None and len(frame) > 0:
                    return {
                        "rows": len(frame), "content": frame.to_csv(index=False).encode("utf-8-sig"),
                        "filename": f"earnings_report_{period[:4]}Q{((int(period[4:6]) - 1) // 3) + 1}.csv",
                    }
            except Exception as exc:  # each pre-existing period is attempted at most once
                errors.append({"period": period, "error_type": type(exc).__name__})
        return {"rows": 0, "status": "no_fresh_earnings_rows", "errors": errors}
    if kind == "disclosure":
        frame = ak.stock_report_disclosure(market="沪深京", period=f"{year}半年报")
        if frame is not None and len(frame) > 0:
            return {"rows": len(frame), "content": frame.to_csv(index=False).encode("utf-8-sig"),
                    "filename": f"disclosure_schedule_{year}H1.csv"}
        return {"rows": 0, "status": "no_fresh_disclosure_rows"}
    raise ValueError("unknown_optional_family")


def _run_optional_fetches(tasks: list[dict[str, Any]]) -> ProcessDeadlineResult:
    remaining = _remaining_refresh_seconds()
    if remaining is None:
        raise RuntimeError("optional_fetch_requires_deadline")
    return run_process_tasks_until_deadline(
        _optional_fetch_worker, tasks, context={}, max_workers=1, timeout_seconds=remaining,
    )


def _optional_fetch_evidence(batch: ProcessDeadlineResult, names: list[str]) -> dict[str, Any]:
    unfinished = [names[index] for index in batch.unfinished_indices]
    return {
        "timed_out": batch.timed_out, "unfinished": unfinished,
        # Once a worker starts, the pool cannot prove whether an unfinished
        # request was in flight or still queued. Never label all as unattempted.
        "not_attempted": unfinished if not batch.worker_pids else [],
        "unfinished_attempt_state": "unknown" if unfinished and batch.worker_pids else "not_started",
        "fetch_errors": {names[index]: error.split(":", 1)[0] for index, error in batch.failures.items()},
        "reason_codes": ["optional_fetch_deadline_exhausted"] if batch.timed_out else
                        ["optional_fetch_worker_incomplete"] if unfinished else [],
    }


def _save_refresh_progress(report: dict[str, Any], *, lease: MarketWriterLease, current_family: str | None) -> None:
    progress = {
        **report, "schema_version": "daily_refresh_progress_v1", "current_family": current_family,
        "progress_recorded_at": _now_iso(), "finished_at": report.get("finished_at"),
    }
    _atomic_bytes(
        _market_refresh_progress_path(RESULTS_DIR, report["pipeline_run_id"]),
        json.dumps(progress, ensure_ascii=False, indent=2, allow_nan=False).encode("utf-8"), lease=lease,
    )


def _complete_live_session(target: str, now: datetime | None = None) -> bool:
    from quantdesk.market_clock import is_cn_a_share_trading_day, load_cn_trading_calendar

    current = now or datetime.now(SHANGHAI)
    if current.tzinfo is None:
        current = current.replace(tzinfo=SHANGHAI)
    current = current.astimezone(SHANGHAI)
    return (
        target == current.date().isoformat()
        and (current.hour, current.minute) >= (15, 5)
        and is_cn_a_share_trading_day(current.date(), calendar=load_cn_trading_calendar())
    )


def _atomic_bytes(path: Path, content: bytes, *, lease: MarketWriterLease) -> None:
    lease.assert_active()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        with tmp.open("wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        lease.assert_active()
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


class _DatedQuotes(dict[str, dict[str, Any]]):
    def __init__(self) -> None:
        super().__init__()
        self.batch_diagnostics: list[dict[str, Any]] = []


def _dated_close_quotes(symbols: list[str]) -> dict[str, dict[str, Any]]:
    """Sina list has no session date: bind each row to its own dated HQ quote.

    This adds one paced batch per 100 symbols; it never fetches per-stock
    history. The price/volume match below prevents transferring a date from
    an unrelated or changed quote onto the list snapshot.
    """
    from quantdesk.data_providers import fetch_spot_sina_batch
    from quantdesk.notifications.redaction import redact_text

    quotes = _DatedQuotes()
    quotes.not_requested = []
    for start in range(0, len(symbols), 100):
        pending = symbols[start:start + 100]
        if _bars_expired():
            # No new request once bars' own deadline is gone; the rest stays
            # an explicit "not requested" gap, never a silent absence.
            quotes.not_requested.extend(symbols[start:])
            quotes.batch_diagnostics.append({"batch_start": start, "reason": "bars_deadline_exhausted",
                                             "not_requested_count": len(symbols) - start})
            break
        # One bounded retry of missing rows; do not request successful rows twice.
        for attempt in (1, 2):
            if attempt == 2 and _bars_expired():
                break
            requested = set(pending)
            left = _bars_time_left()
            timeout = 10 if left is None else max(1.0, min(10.0, left))
            result = fetch_spot_sina_batch(pending, batch_size=100, timeout=timeout)
            for row in result.rows:
                code = str(row.get("symbol") or "")[-6:]
                if code not in requested:
                    continue
                if code in quotes:
                    quotes[code] = {}  # ambiguous duplicate, never pick one
                else:
                    quotes[code] = row
            pending = sorted(requested - set(quotes))
            quotes.batch_diagnostics.append({
                "batch_start": start, "attempt": attempt,
                "requested_count": len(requested), "received_count": len(result.rows),
                "missing_symbols": pending,
                "provider_errors": [redact_text(str(error))[:500] for error in result.provider_errors],
                "warnings": [redact_text(str(warning))[:500] for warning in result.warnings],
            })
            if not pending or attempt == 2:
                break
            time.sleep(0.8)
        if start + 100 < len(symbols):
            time.sleep(0.8)
    return quotes


def _requery_universe_diff(
    symbols: list[str], *, lease: MarketWriterLease,
) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
    """Re-check the symbols the list snapshot dropped, one bounded pass each.

    The list endpoint is the primary source, but a code it silently omits would
    otherwise never be requested by anything (9/21: 689009 was in the local
    roster and in no HQ request).  Each difference code goes through the
    existing dated-HQ path exactly once and whatever happens is written to the
    receipt, so a dropped symbol can never be silent again.  Codes above
    ``UNIVERSE_DIFF_REQUERY_LIMIT`` are recorded as not attempted rather than
    turned into an unauthorized full-universe sweep.
    """

    quotes = _DatedQuotes()
    outcomes: dict[str, dict[str, Any]] = {}
    for index, code in enumerate(symbols[:UNIVERSE_DIFF_REQUERY_LIMIT]):
        lease.assert_active()
        if _bars_expired():
            for later in symbols[index:UNIVERSE_DIFF_REQUERY_LIMIT]:
                outcomes[later] = {"attempted": False, "reason": "bars_deadline_exhausted",
                                   "quote_date": "", "quote_time": "",
                                   "note": "bars 自身截止时间已到，本轮未补查；缺口保留。"}
            break
        single = _dated_close_quotes([code])
        for entry in getattr(single, "batch_diagnostics", None) or []:
            quotes.batch_diagnostics.append(
                {**entry, "scope": "universe_diff_requery", "symbol": code}
            )
        quote = single.get(code) or {}
        if quote:
            quotes[code] = quote
            outcomes[code] = {
                "attempted": True, "reason": "",
                "quote_date": quote.get("date"), "quote_time": quote.get("time"),
            }
        else:
            outcomes[code] = {
                "attempted": True, "reason": "universe_diff_requery_no_quote",
                "quote_date": "", "quote_time": "",
                "note": "列表未返回该代码，单只补查也未取得报价；不写日线、不作排除。",
            }
    for code in symbols[UNIVERSE_DIFF_REQUERY_LIMIT:]:
        outcomes[code] = {
            "attempted": False, "reason": "universe_diff_requery_over_limit",
            "quote_date": "", "quote_time": "",
            "note": f"差集超过单轮上限 {UNIVERSE_DIFF_REQUERY_LIMIT}，本轮不请求，缺口保留。",
        }
    return quotes, outcomes


def _dated_quote_target_reason(
    quote: dict[str, Any], target: str, *, observed_at: datetime | None = None,
) -> str:
    """Check a dated HQ quote is usable as the target day's closing evidence.

    This is the list-independent half of ``_close_quote_reason``: it enforces
    the date, completed-session and not-in-the-future rules.  The list-versus-HQ
    value comparison stays in ``_close_quote_reason``, because a symbol the list
    snapshot dropped has no list row to compare against.
    """

    if quote.get("date") != target:
        return "quote_date_missing" if not quote.get("date") else "quote_date_mismatch"
    try:
        quoted_at = datetime.fromisoformat(f"{quote['date']}T{quote['time']}")
    except (KeyError, TypeError, ValueError):
        return "quote_timestamp_invalid"
    if quoted_at.tzinfo is None:
        quoted_at = quoted_at.replace(tzinfo=SHANGHAI)
    quoted_at = quoted_at.astimezone(SHANGHAI)
    if quoted_at.date().isoformat() != target:
        return "quote_date_mismatch"
    if (quoted_at.hour, quoted_at.minute) < (15, 0):
        return "quote_before_close"
    current = observed_at or datetime.now(SHANGHAI)
    if current.tzinfo is None:
        current = current.replace(tzinfo=SHANGHAI)
    # HQ timestamps describe quote updates, which continue after 15:30.
    # Keep same-day/after-close checks and reject future timestamps instead.
    if quoted_at > current:
        return "quote_timestamp_in_future"
    return ""


def _close_quote_reason(
    item: dict[str, Any], quote: dict[str, Any], target: str,
    *, observed_at: datetime | None = None,
) -> str:
    reason = _dated_quote_target_reason(quote, target, observed_at=observed_at)
    if reason:
        return reason
    for list_key, quote_key in (("open", "open"), ("high", "high"), ("low", "low"), ("trade", "price"), ("volume", "volume")):
        try:
            left, right = float(item[list_key]), float(quote[quote_key])
        except (KeyError, TypeError, ValueError):
            return f"quote_value_missing:{list_key}"
        if not math.isfinite(left) or not math.isfinite(right):
            return f"quote_value_nonfinite:{list_key}"
        if not math.isclose(left, right, rel_tol=1e-8, abs_tol=1e-6):
            return f"quote_value_mismatch:{list_key}"
    return ""


def _close_quote_matches(item: dict[str, Any], quote: dict[str, Any], target: str) -> bool:
    return not _close_quote_reason(item, quote, target)


def _display_path(path: Path) -> str:
    try:
        return path.relative_to(PROJECT_ROOT).as_posix()
    except ValueError:
        return str(path)


def _resolve_target_date() -> str:
    try:
        from quantdesk.trading_calendar import resolve_target_complete_trade_date

        resolved = resolve_target_complete_trade_date()
        if isinstance(resolved, dict):
            raw = str(resolved.get("effective_target_date") or resolved.get("target_complete_trade_date") or "").strip()
        else:
            raw = str(resolved or "").strip()
        digits = "".join(ch for ch in raw if ch.isdigit())
        if len(digits) == 8:
            return f"{digits[:4]}-{digits[4:6]}-{digits[6:]}"
    except Exception:  # noqa: BLE001
        pass
    return datetime.now().strftime("%Y-%m-%d")


def _fmt_price(value: Any) -> str:
    s = str(value).strip()
    if s in ("", "None", "null"):
        return ""
    try:
        number = float(s)
        return repr(number) if math.isfinite(number) else ""
    except (TypeError, ValueError):
        return ""


def _fmt_float1(value: Any) -> str:
    try:
        number = float(value)
        return "%.1f" % number if math.isfinite(number) else ""
    except (TypeError, ValueError):
        return ""


def _fnum(value: Any) -> float:
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return 0.0


def _load_universe_symbols() -> list[str]:
    if not UNIVERSE_FILE.is_file():
        return []
    out: list[str] = []
    try:
        for line in UNIVERSE_FILE.read_text(encoding="utf-8-sig").splitlines()[1:]:
            sym = line.split(",")[0].strip()
            if sym.isdigit() and len(sym) == 6:
                out.append(sym)
    except OSError:
        return []
    return out


class _ListRows(list):
    """The list snapshot plus how its pagination actually ended."""

    def __init__(self) -> None:
        super().__init__()
        self.pagination: dict[str, Any] = {
            "complete": False, "pages_completed": 0, "end": "", "failed_page": None,
            "page_errors": [], "reason_codes": [],
        }


def _page_error(exc: Exception, status: int | None) -> str:
    """Name a page failure: HTTP status (e.g. 456 rate limit), timeout, connect, decode."""

    if status is not None and status != 200:
        return f"http_{status}"
    name = type(exc).__name__
    if "Timeout" in name:
        return "timeout"
    if "Connection" in name:
        return "connect"
    if isinstance(exc, ValueError):  # JSONDecodeError and other undecodable bodies
        return "decode"
    return f"request:{name}"


def fetch_market_snapshot(session: requests.Session) -> list[dict]:
    """Sina list pages. A page that still fails after its retries is a failure.

    Before 2026-10-12 a page that failed three times was taken as "end of the
    list": 2026-10-08 15:40 kept 900 rows and silently lost 4670 symbols. Now
    the page's failure (HTTP status, timeout, connect, decode) is recorded and
    ``pagination.complete`` stays False, so the caller can fill the gap.
    """

    rows = _ListRows()
    evidence = rows.pagination
    page = 1
    while page <= 80:
        if _bars_expired():
            evidence.update(end="deadline", failed_page=page)
            evidence["reason_codes"].append(f"list_deadline_exhausted:page={page}")
            break
        payload = None
        failure = ""
        for attempt in range(1, 4):
            status: int | None = None
            left = _bars_time_left()
            if left is not None and left <= 0:
                failure = failure or "deadline"
                break
            try:
                resp = session.get(
                    SINA_URL,
                    params={
                        "page": page,
                        "num": 100,
                        "sort": "symbol",
                        "asc": 1,
                        "node": "hs_a",
                        "symbol": "",
                        "_s_r_a": "init",
                    },
                    headers={"User-Agent": UA, "Referer": "https://finance.sina.com.cn/"},
                    timeout=25 if left is None else max(1.0, min(25.0, left)),
                )
                status = getattr(resp, "status_code", None)
                if isinstance(status, int) and status != 200:
                    raise RuntimeError(f"http_status_{status}")
                resp.encoding = "GBK"
                payload = resp.json()
                failure = ""
                break
            except Exception as exc:  # noqa: BLE001 - recorded per page, never silent
                failure = _page_error(exc, status if isinstance(status, int) else None)
                evidence["page_errors"].append({"page": page, "attempt": attempt, "error": failure})
                if attempt < 3:
                    time.sleep(3 * attempt)
        if failure:
            evidence.update(end="page_failed", failed_page=page)
            evidence["reason_codes"].append(f"list_page_failed:page={page}:{failure}")
            break
        if payload is None or (isinstance(payload, list) and not payload):
            # Sina answers past the last page with null / an empty list.
            evidence.update(complete=True, end="empty_page")
            break
        if not isinstance(payload, list):
            evidence.update(end="invalid_page", failed_page=page)
            evidence["reason_codes"].append(f"list_page_invalid:page={page}")
            break
        rows.extend(payload)
        evidence["pages_completed"] += 1
        if len(payload) < 100:
            # A short page is normally the last one. The caller still checks
            # the result against the local roster; a gap is never assumed away.
            evidence.update(complete=True, end="short_page")
            break
        page += 1
        time.sleep(0.8)
    if page > 80 and not evidence["end"]:
        evidence.update(end="page_limit")
        evidence["reason_codes"].append("list_page_limit_reached")
    return rows


def _append_daily_row(
    path: Path, row: str, *, header: str, lease: MarketWriterLease,
) -> tuple[bool, str]:
    """Validate the complete original frame, then atomically append its bytes."""
    import pandas as pd
    from quantdesk.price_basis import classify_frame_price_basis

    lease.assert_active()
    columns = next(csv.reader([header]))
    parsed = next(csv.reader([row]))
    if len(parsed) != len(columns):
        return False, f"daily_row_column_mismatch:{path.name}"
    incoming = dict(zip(columns, parsed))
    target = incoming["date"]
    try:
        if date.fromisoformat(target).isoformat() != target:
            raise ValueError(target)
    except ValueError:
        return False, f"daily_target_invalid:{path.name}"
    if incoming.get("adjust") != "raw" or incoming.get("symbol") != path.stem:
        return False, f"daily_incoming_contract_invalid:{path.name}"
    valid, reason = daily_bar_validity_from_row(incoming)
    if not valid:
        return False, f"daily_incoming_invalid:{path.name}:{reason}"
    before = path.read_bytes() if path.exists() else b""
    if before:
        try:
            all_rows = list(csv.reader(io.StringIO(before.decode("utf-8-sig"))))
        except (UnicodeError, csv.Error):
            return False, f"daily_csv_unreadable:{path.name}"
        if not all_rows or all_rows[0] != columns:
            return False, f"daily_header_mismatch:{path.name}"
        if any(len(values) != len(columns) for values in all_rows[1:]):
            return False, f"daily_history_column_mismatch:{path.name}"
        frame = pd.DataFrame(all_rows[1:], columns=columns)
        basis, reason = classify_frame_price_basis(frame)
        if basis != "raw":
            return False, f"daily_history_basis_rejected:{path.name}:{basis}:{reason}"
        # No filtering, sorting or deduplication may hide a bad original row.
        dates = frame["date"].tolist()
        try:
            if any(date.fromisoformat(day).isoformat() != day for day in dates):
                raise ValueError("noncanonical date")
        except ValueError:
            return False, f"daily_history_date_invalid:{path.name}"
        if len(set(dates)) != len(dates):
            return False, f"daily_history_duplicate_dates:{path.name}"
        if set(frame["symbol"]) != {path.stem}:
            return False, f"daily_history_symbol_mismatch:{path.name}"
        if target in dates:
            valid, reason = daily_bar_validity_from_row(frame.iloc[dates.index(target)].to_dict())
            if not valid:
                return False, f"daily_existing_target_invalid:{path.name}:{reason}"
            return False, f"daily_target_already_present:{path.name}:{target}"
        if any(day > target for day in dates):
            return False, f"daily_append_would_backdate:{path.name}:{target}"
    extra = io.StringIO(newline="")
    writer = csv.writer(extra, lineterminator="\n")
    if not before:
        writer.writerow(columns)
    writer.writerow(parsed)
    separator = b"\n" if before and not before.endswith((b"\n", b"\r")) else b""
    _atomic_bytes(path, before + separator + extra.getvalue().encode("utf-8"), lease=lease)
    return True, ""


def family_bars_and_valuation(
    target: str, network: bool, *, lease: MarketWriterLease,
) -> dict[str, Any]:
    if not network:
        return {"ok": False, "status": "skipped_network_false"}
    lease.assert_active()
    if not _complete_live_session(target):
        return {"ok": False, "status": "blocked", "reason": "live_snapshot_requires_today_completed_session"}
    session = requests.Session()
    try:
        rows = fetch_market_snapshot(session)
    finally:
        session.close()
    pagination = dict(getattr(rows, "pagination", {}) or {})
    list_row_count = len(rows)
    roster = _load_universe_symbols()
    discovery = discover_daily_universe_symbols(
        PROJECT_ROOT, target, known_symbols=[*roster, *(str(row.get("code") or "") for row in rows)],
        snapshot_roots=_DISCOVERY_SNAPSHOT_ROOTS,
    )
    request_universe = list(dict.fromkeys([*roster, *discovery["symbols"]]))
    # A list whose pagination did not end cleanly is truncated, not complete.
    list_truncated = bool(rows) and bool(pagination) and pagination.get("complete") is not True
    dated_only = not rows
    quotes: dict[str, dict[str, Any]] | None = None
    if dated_only:
        # The dated HQ endpoint supplies its own OHLCV and session timestamp.
        # A list outage must not prevent using that existing independent path.
        if not request_universe:
            return {"ok": False, "status": "failed", "reason": "snapshot_empty_and_universe_missing", "fetched": 0}
        lease.assert_active()
        quotes = _dated_close_quotes(request_universe)
        rows = []
        for code in request_universe:
            quote = quotes.get(code) or {}
            if not quote:
                continue
            price, previous = _fnum(quote.get("price")), _fnum(quote.get("prev_close"))
            rows.append({
                "code": code, "name": quote.get("name"),
                "open": quote.get("open"), "high": quote.get("high"), "low": quote.get("low"),
                "trade": quote.get("price"), "volume": quote.get("volume"), "amount": quote.get("amount"),
                "pricechange": price - previous if previous > 0 else None,
                "changepercent": (price / previous - 1) * 100 if previous > 0 else None,
            })
    from collections import Counter

    counts = Counter(str(item.get("code") or "").strip() for item in rows)
    symbols = sorted(code for code in counts if len(code) == 6 and code.isdigit())
    lease.assert_active()
    if quotes is None:
        quotes = _dated_close_quotes(symbols)
    now = _now_iso()
    stats = {"appended": 0, "already_had_bar": 0, "created_files": 0,
             "suspended_verified": 0, "flagged_for_review": 0, "append_failed": 0}
    review: dict[str, Any] = {}
    warnings: list[str] = (["snapshot_list_empty:using_dated_hq", "valuation_not_updated:list_unavailable"]
                           if dated_only else [])
    warnings.extend(discovery["warnings"])
    if discovery["symbols"]:
        warnings.append(f"universe_discovery_new_request_targets:{len(discovery['symbols'])}")
    universe = request_universe
    missing = sorted(set(universe) - set(symbols))
    if not universe:
        warnings.append("universe_file_missing_or_unreadable:" + str(UNIVERSE_FILE))
    if missing:
        warnings.append(f"universe_snapshot_missing:{len(missing)}")
    if len(symbols) < 5000:
        warnings.append(f"full_a_snapshot_low_coverage:{len(symbols)}")

    # A truncated list (a page failed, the deadline hit, ...) is not the whole
    # market. 2026-10-08 15:40: 900 list rows, 4670 roster codes missing, the
    # single-code re-check stopped at 50 and 4620 symbols got no bar at all.
    # The missing codes now go through the same batched dated-HQ path the
    # empty-list fallback uses (100 codes per request, no per-stock history).
    gap_fill: dict[str, Any] = {}
    if list_truncated and missing:
        lease.assert_active()
        warnings.append(
            f"snapshot_list_truncated:{list_row_count}_rows:"
            + ",".join(pagination.get("reason_codes") or ["pagination_incomplete"])[:300]
        )
        gap_quotes = _dated_close_quotes(missing)
        not_requested = set(getattr(gap_quotes, "not_requested", None) or [])
        filled: list[str] = []
        for code in missing:
            quote = gap_quotes.get(code) or {}
            if not quote:
                continue
            price, previous = _fnum(quote.get("price")), _fnum(quote.get("prev_close"))
            rows.append({
                "code": code, "name": quote.get("name"),
                "open": quote.get("open"), "high": quote.get("high"), "low": quote.get("low"),
                "trade": quote.get("price"), "volume": quote.get("volume"), "amount": quote.get("amount"),
                "pricechange": price - previous if previous > 0 else None,
                "changepercent": (price / previous - 1) * 100 if previous > 0 else None,
                "_gap_fill": True,
            })
            quotes[code] = quote
            counts[code] += 1
            filled.append(code)
        main_diagnostics = getattr(quotes, "batch_diagnostics", None)
        if isinstance(main_diagnostics, list):
            main_diagnostics.extend(
                {**entry, "scope": "list_gap_fill"} for entry in getattr(gap_quotes, "batch_diagnostics", None) or []
            )
        symbols = sorted(set(symbols) | set(filled))
        still_missing = sorted(set(universe) - set(symbols))
        gap_fill = {
            "attempted": len(missing) - len(not_requested), "filled": len(filled),
            "no_quote": len([c for c in still_missing if c not in not_requested]),
            "not_requested_deadline": len(not_requested),
            "still_missing_sample": still_missing[:20],
        }
        warnings.append(f"list_gap_filled_by_dated_hq:{len(filled)}/{len(missing)}")
        warnings.append("valuation_partial:list_truncated")
        missing = still_missing

    # The list is the primary source; a code it silently drops (9/21: 689009)
    # ended up in no request at all.  Every difference code gets one bounded
    # re-check through the same dated-HQ path, and the outcome is kept.  The
    # empty-list fallback already requested the whole roster, so it needs none;
    # a truncated list's gap was just requested in batches above.
    diff_quotes: dict[str, dict[str, Any]] = {}
    diff_outcomes: dict[str, dict[str, Any]] = {}
    if missing and not dated_only and not list_truncated:
        diff_quotes, diff_outcomes = _requery_universe_diff(missing, lease=lease)
        main_diagnostics = getattr(quotes, "batch_diagnostics", None)
        if isinstance(main_diagnostics, list):
            main_diagnostics.extend(getattr(diff_quotes, "batch_diagnostics", None) or [])
        failed_diff = sorted(code for code, row in diff_outcomes.items() if row.get("reason"))
        if failed_diff:
            warnings.append(
                "universe_diff_requery_failed:"
                f"{len(failed_diff)}:{','.join(failed_diff[:8])}"
            )

    val_fields = ["symbol", "name", "close", "pct_chg", "pe", "pb", "total_mv_raw", "circ_mv_raw",
                  "turnover_rate", "volume", "amount", "trade_date", "source", "provider",
                  "total_mv_unit", "pe_semantics", "quote_date", "quote_time"]
    valuations = io.StringIO(newline="")
    valuation_writer = csv.writer(valuations, lineterminator="\n")
    valuation_writer.writerow(val_fields)
    accepted_quotes = 0
    unprocessed: list[str] = []
    for position, item in enumerate(rows):
        lease.assert_active()
        if _bars_expired():
            # Stop with evidence: these codes were not checked or written in
            # this run and stay missing (the slow path may still fill them).
            unprocessed = [str(other.get("code") or "").strip() for other in rows[position:]]
            break
        code = str(item.get("code") or "").strip()
        if not code.isdigit() or len(code) != 6:
            continue
        gap_item = bool(item.get("_gap_fill"))
        quote = quotes.get(code) or {}
        reason = "duplicate_list_symbol" if counts[code] != 1 else (
            "duplicate_or_missing_hq_quote" if not quote else _close_quote_reason(item, quote, target))
        if reason:
            review[code] = {"reason": reason, "quote_date": quote.get("date"),
                            "quote_time": quote.get("time"),
                            "note": "未获得本交易日收盘证据，不写日线或估值，不认定停牌。"}
            if reason.startswith("quote_value_"):
                review[code]["compared_fields"] = {
                    left: {"list": str(item.get(left)), "dated_quote": str(quote.get(right))}
                    for left, right in (("open", "open"), ("high", "high"),
                                        ("low", "low"), ("trade", "price"), ("volume", "volume"))
                }
            continue
        accepted_quotes += 1
        if not dated_only and not gap_item:
            valuation_writer.writerow([
                code, str(item.get("name") or ""), _fmt_price(item.get("trade")),
                _fmt_price(item.get("changepercent")), _fmt_price(item.get("per")), _fmt_price(item.get("pb")),
                _fmt_price(item.get("mktcap")), _fmt_price(item.get("nmc")), _fmt_price(item.get("turnoverratio")),
                _fmt_float1(item.get("volume")), _fmt_float1(item.get("amount")), target,
                "sina.Market_Center.getHQNodeData+dated_hq", "sina_spot_direct", "wan_yuan", "dynamic",
                quote["date"], quote["time"],
            ])
        valid, reason = daily_bar_validity_from_row({
            "open": item.get("open"), "high": item.get("high"), "low": item.get("low"),
            "close": item.get("trade"), "volume": item.get("volume"),
        })
        if not valid:
            review[code] = {"reason": reason, "quote_date": quote["date"],
                            "note": "异常或零成交行情；无独立停牌证据，不写日线、不作排除。"}
            continue
        row = io.StringIO(newline="")
        csv.writer(row, lineterminator="").writerow([
            target, code, _fmt_price(item.get("open")), _fmt_price(item.get("high")),
            _fmt_price(item.get("low")), _fmt_price(item.get("trade")), _fmt_float1(item.get("volume")),
            _fmt_float1(item.get("amount")), _fmt_price(item.get("pricechange")),
            _fmt_price(item.get("changepercent")), "raw",
            "sina_dated_hq_close" if dated_only else "sina_dated_hq_list_gap_fill" if gap_item else "sina_spot_close", now,
        ])
        path = DAILY_DIR / f"{code}.csv"
        was_new = not path.exists()
        with interprocess_file_lock(DAILY_DIR / ".locks" / f"{code}.lock"):
            appended, reason = _append_daily_row(path, row.getvalue(), header=DAILY_HEADER, lease=lease)
        if not appended and reason.startswith("daily_target_already_present:"):
            stats["already_had_bar"] += 1
        elif not appended:
            stats["append_failed"] += 1
            warnings.append(reason)
            review[code] = {"reason": reason, "note": "原日线保留，未追加。"}
        else:
            stats["appended"] += 1
            stats["created_files"] += int(was_new)
    # Difference symbols have no list row to compare against, so their quotes are
    # validated on the date/close rules only and appended through the same
    # writer and per-symbol lock as every other daily row.
    for code in sorted(diff_quotes):
        lease.assert_active()
        quote = diff_quotes[code]
        outcome = diff_outcomes.setdefault(code, {"attempted": True, "reason": ""})
        if _bars_expired():
            unprocessed.append(code)
            outcome["reason"] = "bars_deadline_exhausted"
            continue
        reason = _dated_quote_target_reason(quote, target)
        if reason:
            outcome["reason"] = f"universe_diff_requery:{reason}"
            outcome["quote_date"] = quote.get("date")
            outcome["quote_time"] = quote.get("time")
            outcome["note"] = "补查报价不是目标日期的收盘证据；不写日线、不作排除。"
            continue
        valid, reason = daily_bar_validity_from_row({
            "open": quote.get("open"), "high": quote.get("high"), "low": quote.get("low"),
            "close": quote.get("price"), "volume": quote.get("volume"),
        })
        if not valid:
            outcome["reason"] = f"universe_diff_requery:{reason}"
            outcome["note"] = "补查行情异常或零成交；不写日线、不作排除。"
            continue
        price, previous = _fnum(quote.get("price")), _fnum(quote.get("prev_close"))
        row = io.StringIO(newline="")
        csv.writer(row, lineterminator="").writerow([
            target, code, _fmt_price(quote.get("open")), _fmt_price(quote.get("high")),
            _fmt_price(quote.get("low")), _fmt_price(quote.get("price")),
            _fmt_float1(quote.get("volume")), _fmt_float1(quote.get("amount")),
            _fmt_price(price - previous if previous > 0 else None),
            _fmt_price((price / previous - 1) * 100 if previous > 0 else None), "raw",
            "sina_dated_hq_universe_diff", now,
        ])
        path = DAILY_DIR / f"{code}.csv"
        was_new = not path.exists()
        with interprocess_file_lock(DAILY_DIR / ".locks" / f"{code}.lock"):
            appended, reason = _append_daily_row(path, row.getvalue(), header=DAILY_HEADER, lease=lease)
        if appended:
            stats["appended"] += 1
            stats["created_files"] += int(was_new)
            outcome["appended"] = True
        elif reason.startswith("daily_target_already_present:"):
            stats["already_had_bar"] += 1
            outcome["appended"] = False
            outcome["note"] = "本地当日日线已存在，未重复写入。"
        else:
            stats["append_failed"] += 1
            outcome["reason"] = f"universe_diff_requery:{reason}"
            warnings.append(reason)
    stats["flagged_for_review"] = len(review)
    val_path = VAL_DIR / f"{target.replace('-', '')}.csv"
    if accepted_quotes and not dated_only:
        _atomic_bytes(val_path, valuations.getvalue().encode("utf-8"), lease=lease)
    if review or missing:
        warnings.append(f"unverified_symbols:{len(review)}:missing_snapshot:{len(missing)}")
    # Deliberately separate from suspension_evidence: this is not exclusion proof.
    review_path = META_DIR / "daily_refresh_review" / f"{target.replace('-', '')}.json"
    _atomic_bytes(review_path, json.dumps({
        "target_date": target, "generated_at": now, "pipeline_run_id": lease.run_id,
        "source": "sina_dated_hq_fallback" if dated_only else "sina_list_and_dated_hq", "verified_exclusions": {},
        "flagged_for_review": review, "missing_in_snapshot": missing,
        "universe_diff_requery": diff_outcomes,
        "universe_diff_requery_limit": UNIVERSE_DIFF_REQUERY_LIMIT,
        "universe_discovery": discovery,
        "quote_batch_diagnostics": getattr(quotes, "batch_diagnostics", []),
        "contract": "Missing or invalid data remains missing; no suspension is inferred.",
    }, ensure_ascii=False, indent=2, allow_nan=False).encode("utf-8"), lease=lease)
    usable = stats["appended"] + stats["already_had_bar"]
    not_requested = list(getattr(quotes, "not_requested", None) or [])
    deadline_hit = bool(unprocessed or not_requested)
    if deadline_hit:
        warnings.append(f"bars_deadline_exhausted:unprocessed={len(unprocessed)}:not_requested={len(not_requested)}")
    status = ("partial" if usable and deadline_hit else "completed_with_warnings" if usable and warnings
              else "completed" if usable else "failed")
    return {
        "ok": bool(usable), "status": status,
        "list_rows": list_row_count, "list_pagination": pagination, "list_truncated": list_truncated,
        "list_gap_fill": gap_fill,
        "universe_discovery": discovery,
        "roster_symbol_count": len(roster), "request_universe_count": len(request_universe),
        "bars_deadline": {"hit": deadline_hit, "unprocessed_count": len(unprocessed),
                          "unprocessed_sample": unprocessed[:20], "not_requested_count": len(not_requested)},
        "snapshot_rows": len(rows), "dated_close_quotes": accepted_quotes, "bars": stats,
        "valuation_snapshot": _display_path(val_path) if accepted_quotes and not dated_only else "",
        "quote_source": "sina_dated_hq_fallback" if dated_only else "sina_list_and_dated_hq",
        "review_evidence": _display_path(review_path), "suspension_evidence": "", "warnings": warnings,
        "universe_diff_requery": {
            "attempted": len([code for code, row in diff_outcomes.items() if row.get("attempted")]),
            "appended": len([code for code, row in diff_outcomes.items() if row.get("appended")]),
            "failed": len([code for code, row in diff_outcomes.items() if row.get("reason")]),
            "limit": UNIVERSE_DIFF_REQUERY_LIMIT,
        },
    }


def family_benchmarks(
    network: bool, *, lease: MarketWriterLease, target_date: str | None = None,
) -> dict[str, Any]:
    if not network:
        return {"ok": False, "status": "skipped_network_false"}
    lease.assert_active()
    try:
        from quantdesk.data_truth.benchmark_incremental_update import (
            BENCHMARK_SPECS,
            default_index_fetcher,
            update_benchmark_files,
        )
        from tempfile import TemporaryDirectory

        # The legacy updater writes internally. Publish its audited staged
        # bytes only while the Pipeline still owns this run.
        with TemporaryDirectory(prefix="quantdesk-benchmark-") as temp:
            staged_root = Path(temp)
            staged_dir = staged_root / "data" / "benchmark"
            staged_dir.mkdir(parents=True)
            for spec in BENCHMARK_SPECS:
                source = PROJECT_ROOT / "data" / "benchmark" / spec.filename
                if source.is_file():
                    (staged_dir / spec.filename).write_bytes(source.read_bytes())
            target = target_date or lease.target
            if _OPTIONAL_DEADLINE.get() is None:
                result = update_benchmark_files(
                    staged_root, fetcher=default_index_fetcher, target_date=target,
                )
            else:
                batch = _run_optional_fetches([{"kind": "benchmarks", "staged_root": str(staged_root), "target": target}])
                lease.assert_active()
                if 0 not in batch.results:
                    error_type = batch.failures.get(0, "").split(":", 1)[0]
                    return {
                        "ok": False, "status": "error" if error_type else "skipped" if batch.timed_out and not batch.worker_pids else "incomplete",
                        "error_type": error_type,
                        "target_date": target, "target_covered": None,
                        "freshness_status": "assessment_incomplete" if batch.worker_pids else "not_assessed",
                        "indices": [], **_optional_fetch_evidence(batch, ["benchmarks"]),
                    }
                result = batch.results[0]
            lease.assert_active()
            for item in result.get("results", []):
                if item.get("status") == "appended":
                    filename = str(item["path"])
                    if filename not in {spec.filename for spec in BENCHMARK_SPECS}:
                        raise ValueError("unexpected_benchmark_filename")
                    _atomic_bytes(PROJECT_ROOT / "data" / "benchmark" / filename,
                                  (staged_dir / filename).read_bytes(), lease=lease)
        lease.assert_active()
        statuses = [str(r.get("status")) for r in result.get("results", [])]
        reasons = sorted({str(code) for item in result.get("results", [])
                          for code in item.get("reason_codes", [])})
        if result.get("target_covered") is not True:
            reasons.append("benchmark_target_not_covered")
        return {
            "ok": result.get("target_covered") is True and bool(statuses) and all(
                s in ("updated", "appended", "up_to_date", "skipped_existing_run") for s in statuses
            ),
            "status": result.get("status"),
            "target_date": result.get("target_date"),
            "target_covered": result.get("target_covered"),
            "freshness_status": result.get("freshness_status"),
            "reason_codes": reasons,
            "coverage": {"requested": len(result.get("results", [])),
                         "target_covered": sum(item.get("target_covered") is True for item in result.get("results", []))},
            "indices": [
                {**r, "appended": r.get("appended_count", 0)}
                for r in result.get("results", [])
            ],
        }
    except MarketWriterUnauthorized:
        raise
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "status": "error", "error_type": type(exc).__name__,
                "target_date": target_date or getattr(lease, "target", None),
                "target_covered": None, "freshness_status": "not_assessed",
                "reason_codes": [f"benchmark_update_failed:{type(exc).__name__}"], "indices": []}


def family_news(network: bool, *, lease: MarketWriterLease) -> dict[str, Any]:
    if not network:
        return {"ok": False, "status": "skipped_network_false"}
    lease.assert_active()
    if not NEWS_POOL_FILE.exists():
        return {"ok": False, "status": "news_pool_file_missing", "path": str(NEWS_POOL_FILE)}
    symbols = [
        line.strip() for line in NEWS_POOL_FILE.read_text(encoding="utf-8").splitlines() if line.strip().isdigit()
    ]
    if not symbols:
        return {"ok": False, "status": "news_pool_empty"}
    try:
        from quantdesk.providers.news_provider import _fetch_announce_eastmoney
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "status": "import_error", "error": repr(exc)[:150]}
    refreshed = 0
    failures: list[str] = []
    details: list[dict[str, str]] = []
    batch = None
    if _OPTIONAL_DEADLINE.get() is not None:
        batch = _run_optional_fetches([
            {"kind": "news", "symbol": sym, "delay_seconds": 2.0 if index else 0.0}
            for index, sym in enumerate(symbols)
        ])
        lease.assert_active()
    for index, sym in enumerate(symbols):
        lease.assert_active()
        if batch is not None and index in batch.unfinished_indices:
            continue
        if batch is not None and index in batch.failures:
            failures.append(sym)
            details.append({"symbol": sym, "reason": batch.failures[index].split(":", 1)[0]})
            continue
        try:
            items = batch.results[index] if batch is not None else _fetch_announce_eastmoney(sym, 50)
            lease.assert_active()
            if items:
                content = io.StringIO(newline="")
                writer = csv.DictWriter(content, fieldnames=["title", "content", "published", "source", "url"])
                writer.writeheader()
                writer.writerows(items)
                path = PROJECT_ROOT / "data" / "news" / f"{datetime.now(SHANGHAI):%Y%m%d}_{sym}.csv"
                _atomic_bytes(path, content.getvalue().encode("utf-8-sig"), lease=lease)
                refreshed += 1
            else:
                failures.append(sym)
                details.append({"symbol": sym, "reason": "no_fresh_news_rows"})
        except MarketWriterUnauthorized:
            raise
        except Exception as exc:  # noqa: BLE001
            failures.append(sym)
            details.append({"symbol": sym, "reason": type(exc).__name__})
        if batch is None:
            time.sleep(2.0)
    evidence = _optional_fetch_evidence(batch, symbols) if batch is not None else {}
    complete = not failures and not evidence.get("unfinished")
    status = "completed" if complete else "skipped" if batch is not None and batch.timed_out and not batch.worker_pids else "partial"
    return {"ok": complete, "status": status,
            "pool_size": len(symbols), "refreshed": refreshed, "failures": failures[:10],
            "failure_details": details, "existing_cache_preserved": True, **evidence}


def family_quarterly(network: bool, force: bool, *, lease: MarketWriterLease) -> dict[str, Any]:
    if not network:
        return {"ok": False, "status": "skipped_network_false"}
    lease.assert_active()
    if not force and datetime.now(SHANGHAI).month not in DISCLOSURE_MONTHS:
        return {"ok": True, "status": "skipped_offseason"}
    if _OPTIONAL_DEADLINE.get() is not None:
        year = datetime.now(SHANGHAI).year
        names = ["earnings", "disclosure"]
        batch = _run_optional_fetches([
            {"kind": "earnings", "year": year},
            {"kind": "disclosure", "year": year, "delay_seconds": 2.0},
        ])
        lease.assert_active()
        out = {"ok": False, "status": "skipped" if batch.timed_out and not batch.worker_pids else "partial_or_missing",
               **_optional_fetch_evidence(batch, names)}
        allowed = {f"earnings_report_{year}Q2.csv", f"earnings_report_{year}Q1.csv",
                   f"earnings_report_{year - 1}Q4.csv", f"disclosure_schedule_{year}H1.csv"}
        for index, result in batch.results.items():
            if result.get("rows", 0) > 0:
                filename = result["filename"]
                if filename not in allowed:
                    raise ValueError("unexpected_quarterly_filename")
                dest = META_DIR / filename
                _atomic_bytes(dest, result["content"], lease=lease)
                out[names[index]] = {"rows": result["rows"], "file": str(dest.relative_to(PROJECT_ROOT))}
            else:
                out[names[index]] = result
        out["ok"] = all(out.get(name, {}).get("rows", 0) > 0 for name in names)
        if out["ok"]:
            out["status"] = "completed"
        return out
    try:
        import akshare as ak
    except ImportError:
        return {"ok": False, "status": "akshare_not_installed"}
    out: dict[str, Any] = {"ok": True}
    try:
        year = datetime.now(SHANGHAI).year
        period_candidates = [f"{year}0630", f"{year}0331", f"{year - 1}1231"]
        for period in period_candidates:
            try:
                lease.assert_active()
                df = ak.stock_yjbb_em(date=period)
                if df is not None and len(df) > 0:
                    dest = META_DIR / f"earnings_report_{period[:4]}Q{((int(period[4:6]) - 1) // 3) + 1}.csv"
                    _atomic_bytes(dest, df.to_csv(index=False).encode("utf-8-sig"), lease=lease)
                    out["earnings"] = {"rows": len(df), "file": str(dest.relative_to(PROJECT_ROOT))}
                    break
            except MarketWriterUnauthorized:
                raise
            except Exception:  # noqa: BLE001
                continue
        time.sleep(2)
        lease.assert_active()
        df2 = ak.stock_report_disclosure(market="沪深京", period=f"{year}半年报")
        if df2 is not None and len(df2) > 0:
            dest2 = META_DIR / f"disclosure_schedule_{year}H1.csv"
            _atomic_bytes(dest2, df2.to_csv(index=False).encode("utf-8-sig"), lease=lease)
            out["disclosure"] = {"rows": len(df2), "file": str(dest2.relative_to(PROJECT_ROOT))}
        if "earnings" not in out or "disclosure" not in out:
            out["ok"] = False
            out["status"] = "partial_or_missing"
    except MarketWriterUnauthorized:
        raise
    except Exception as exc:  # noqa: BLE001
        out["ok"] = False
        out["error"] = repr(exc)[:200]
    return out


def main(argv: list[str] | None = None) -> int:
    global _DISCOVERY_SNAPSHOT_ROOTS
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--enable-network", action="store_true")
    parser.add_argument("--network", default="false")
    parser.add_argument("--force-quarterly", action="store_true")
    parser.add_argument("--target-date", default="")
    # Retained syntax only; notification delivery belongs to the Pipeline.
    parser.add_argument("--enable-alert-network", action="store_true")
    parser.add_argument("--alert-push", action="store_true")
    parser.add_argument("--alert-dry-run", default="true")
    parser.add_argument("--no-alerts", action="store_true")
    parser.add_argument("--json", default="true")
    parser.add_argument("--pipeline-run-id", default="")
    parser.add_argument("--max-refresh-seconds", type=float, default=None)
    parser.add_argument("--refresh-attempt-id", default="")
    parser.add_argument("--discovery-snapshot-root", type=Path, action="append", default=[],
                        help="Explicit read-only project root for same-day verified discovery snapshots.")
    args = parser.parse_args(argv)
    _DISCOVERY_SNAPSHOT_ROOTS = tuple(args.discovery_snapshot_root)
    if args.max_refresh_seconds is not None and (not math.isfinite(args.max_refresh_seconds) or args.max_refresh_seconds < 0):
        parser.error("--max-refresh-seconds must be finite and nonnegative")
    from quantdesk.shared import truthy

    network = bool(args.enable_network or truthy(args.network))
    target = args.target_date or _resolve_target_date()
    report: dict[str, Any] = {
        "task_id": "daily_data_refresh", "run_at": _now_iso(), "target_date": target,
        "pipeline_run_id": str(args.pipeline_run_id or "").strip(), "network": network,
        "refresh_attempt_id": args.refresh_attempt_id, "max_refresh_seconds": args.max_refresh_seconds,
        "research_only": True, "no_broker": True, "families": {}, "overall_ok": False,
        "status": "planned_offline" if not network else "blocked", "canonical_write": False,
        "notes": ["already_had_bar counts pre-existing target bars, not this run's downloads"],
    }
    printed = dict(report)
    if not network:
        report["reason_codes"] = ["network_not_authorized:no_writes"]
        printed = dict(report)
    else:
        started = time.monotonic()
        budget = args.max_refresh_seconds
        # Bars get their own deadline; the optional families stop earlier than
        # the whole budget, so the final report is always written in time.
        deadline_token = _OPTIONAL_DEADLINE.set(
            None if budget is None else started + budget - _reserve(budget, OPTIONAL_PUBLISH_RESERVE_SECONDS))
        bars_token = _BARS_DEADLINE.set(
            None if budget is None else started + budget - _reserve(budget, BARS_PUBLISH_RESERVE_SECONDS))
        try:
            with MarketWriterLease(PROJECT_ROOT, run_id=report["pipeline_run_id"], target=target) as lease:
                if not _complete_live_session(target):
                    raise MarketWriterUnauthorized("live_snapshot_requires_today_completed_session")
                families = {
                    "bars_and_valuation": lambda: family_bars_and_valuation(target, True, lease=lease),
                    "benchmarks": lambda: family_benchmarks(True, lease=lease, target_date=target),
                    "news": lambda: family_news(True, lease=lease),
                    "quarterly": lambda: family_quarterly(True, args.force_quarterly, lease=lease),
                }
                report["status"] = "running"
                for name, run in families.items():
                    lease.assert_active()
                    remaining = _remaining_refresh_seconds()
                    if remaining is not None and remaining <= 0:
                        report["families"][name] = {
                            "ok": False, "status": "skipped", "attempted": False,
                            "started_at": None, "finished_at": None, "reason_codes": ["refresh_budget_exhausted"],
                        }
                        if name == "benchmarks":
                            report["families"][name].update(target_date=target, target_covered=None, freshness_status="not_assessed")
                        _save_refresh_progress(report, lease=lease, current_family=None)
                        continue
                    family_started = _now_iso()
                    report["families"][name] = {"ok": False, "status": "running", "started_at": family_started, "finished_at": None}
                    _save_refresh_progress(report, lease=lease, current_family=name)
                    try:
                        report["families"][name] = run()
                    except MarketWriterUnauthorized:
                        raise
                    except Exception as exc:  # family failure is visible, never fabricated
                        report["families"][name] = {"ok": False, "status": "failed", "error_type": type(exc).__name__}
                    family = report["families"][name]
                    skipped = str(family.get("status", "")).startswith("skipped")
                    family.update(attempted=not skipped, started_at=None if skipped else family_started,
                                  finished_at=None if skipped else _now_iso())
                    _save_refresh_progress(report, lease=lease, current_family=None)
                bars = report["families"]["bars_and_valuation"]
                all_ok = all(v.get("ok") for v in report["families"].values())
                skipped_families = [name for name, family in report["families"].items() if str(family.get("status", "")).startswith("skipped")]
                report.update({
                    "overall_ok": all_ok and not skipped_families, "canonical_write": True,
                    "skipped_families": skipped_families, "finished_at": _now_iso(),
                    "elapsed_seconds": round(time.monotonic() - started, 1),
                    # Bars stopped by their own deadline wrote some bars but not
                    # all: that is "partial", never relabelled as warnings.
                    "status": "failed" if not bars.get("ok") else "partial" if bars.get("status") == "partial"
                              else "completed" if all_ok and not skipped_families and not bars.get("warnings") else "completed_with_warnings",
                })
                benchmark = report["families"]["benchmarks"]
                report["benchmark_evidence"] = {
                    key: benchmark.get(key) for key in (
                        "status", "target_date", "target_covered", "freshness_status", "coverage", "reason_codes"
                    )
                }
                report["reason_codes"] = [f"benchmarks:{code}" for code in benchmark.get("reason_codes", [])]
                for name, family in report["families"].items():
                    if name != "benchmarks":
                        report["reason_codes"].extend(f"{name}:{code}" for code in family.get("reason_codes", []))
                if args.enable_alert_network or args.alert_push or not truthy(args.alert_dry_run):
                    report["notes"].append("notification_flags_ignored:use_pipeline_health_alert")
                content = json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False).encode("utf-8")
                latest = RESULTS_DIR / "data_refresh_latest.json"
                _atomic_bytes(RESULTS_DIR / f"data_refresh_{target}.json", content, lease=lease)
                _atomic_bytes(latest, content, lease=lease)
                _save_refresh_progress(report, lease=lease, current_family=None)
                printed = {**report, "artifact_path": _display_path(latest),
                           "artifact_sha256": "sha256:" + hashlib.sha256(content).hexdigest()}
        except (MarketWriterUnauthorized, OSError) as exc:
            report.update({"status": "blocked", "overall_ok": False,
                           "reason_codes": [str(exc) if isinstance(exc, MarketWriterUnauthorized) else "writer_io_failure"]})
            printed = dict(report)
        finally:
            _BARS_DEADLINE.reset(bars_token)
            _OPTIONAL_DEADLINE.reset(deadline_token)
    if truthy(args.json):
        print(json.dumps(printed, ensure_ascii=False, indent=2, allow_nan=False))
    return (0 if report["status"] in {"completed", "completed_with_warnings", "planned_offline"}
            else 5 if report["status"] == "blocked" else 4 if report["status"] == "partial" else 1)


if __name__ == "__main__":
    raise SystemExit(main())
