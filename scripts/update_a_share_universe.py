from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import sys
import time
_ENTRY_STARTED_MONOTONIC = time.monotonic()
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Sequence, TYPE_CHECKING
from zoneinfo import ZoneInfo

if TYPE_CHECKING:
    import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from quantdesk.config import load_config  # noqa: E402
from quantdesk.utils import atomic_write_json  # noqa: E402
from quantdesk.process_deadline import ProcessDeadlineResult  # noqa: E402

DATA_DIR = PROJECT_ROOT / "data"
DEFAULT_UNIVERSE_PATH = DATA_DIR / "universe" / "a_share_symbols.csv"
DEFAULT_STATUS_PATH = DATA_DIR / "cache" / "a_share_universe_status.json"
DEFAULT_FALLBACK_SYMBOLS = ["600000", "000001", "600519", "300750"]
UNIVERSE_SOURCE_PROVIDERS = (
    "stock_info_a_code_name", "baostock.query_stock_basic", "stock_zh_a_spot", "stock_zh_a_spot_em",
)
DEFAULT_ROSTER_PROVIDERS = ("sina.spot_direct", "baostock.query_stock_basic", "stock_info_a_code_name")
SHANGHAI = ZoneInfo("Asia/Shanghai")
# Minimum window reserved for each source still queued behind the current one.
# Without it the first two windows consume the whole shared budget and the last
# fallback is never attempted (2026-10-14: stock_info_a_code_name got 0.0s).
# The 35s total budget is unchanged; only the split is bounded.
_MIN_SOURCE_SLICE = 4.0
# The parent's own prepare/reap/accounting cost per source. It is charged to the
# source's window so the reservation survives real orchestration overhead
# instead of depending on an exact-fit split.
_PARENT_OVERHEAD_SECONDS = 0.5

# The failure classes a per-source receipt must be able to tell apart. Kept as
# data so a new upstream shape is one line, not a rewrite.
FAILURE_CLASSES = (
    "connect_or_http_failure",
    "protocol_or_schema_change",
    "pagination_incomplete",
    "time_budget_exhausted",
    "empty_roster",
    "date_or_symbol_validation_failed",
    "not_attempted_budget",
    "unknown_failure",
)


def read_csv_frame(path: str | Path, **kwargs: Any) -> Any:
    """Keep parent CSV loading out of cold provider-worker imports."""
    from quantdesk.csv_io import read_csv_frame as read

    return read(path, **kwargs)


def now_text() -> str:
    return datetime.now(SHANGHAI).isoformat(timespec="seconds")


def normalize_symbol(value: object) -> str:
    text = str(value or "").strip()
    digits = "".join(ch for ch in text if ch.isdigit())
    if len(digits) >= 6:
        return digits[-6:]
    if digits:
        return digits.zfill(6)
    return ""


def _add_symbol(rows: list[dict[str, str]], symbol: object, name: object = "", source: str = "") -> None:
    normalized = normalize_symbol(symbol)
    if not normalized:
        return
    if any(row["symbol"] == normalized for row in rows):
        return
    rows.append({"symbol": normalized, "name": str(name or "").strip(), "source": source or "unknown"})


def normalize_universe_frame(df: pd.DataFrame, source: str) -> list[dict[str, str]]:
    if df is None or df.empty:
        return []
    code_candidates = ["symbol", "code", "代码", "股票代码", "证券代码"]
    name_candidates = ["name", "名称", "股票简称", "证券简称"]
    code_col = next((column for column in code_candidates if column in df.columns), "")
    name_col = next((column for column in name_candidates if column in df.columns), "")
    if not code_col:
        return []
    rows: list[dict[str, str]] = []
    seen: set[str] = set()
    for _, row in df.iterrows():
        symbol = normalize_symbol(row.get(code_col))
        if symbol and symbol not in seen:
            rows.append({"symbol": symbol, "name": str(row.get(name_col) or "") if name_col else "", "source": source})
            seen.add(symbol)
    return rows


class UniverseSourceRows(list):
    """Observed source membership is evidence even when no roster is published."""

    def __init__(self, rows: Sequence[dict[str, str]] = (), *, complete: bool = False) -> None:
        super().__init__(rows)
        self.complete = bool(complete)
        self.attempts: list[dict[str, Any]] = []
        self.source_snapshots: list[dict[str, Any]] = []


def _provider_error_text(exc: BaseException) -> str:
    """Keep upstream diagnosis without persisting proxy credentials or keys."""
    message = " ".join(str(exc).split())
    message = re.sub(r"(https?://)[^\s/@]+@", r"\1<redacted>@", message, flags=re.IGNORECASE)
    message = re.sub(r"((?:key|token|sendkey|password|secret|api_key)=)[^&\s]+",
                     r"\1<redacted>", message, flags=re.IGNORECASE)
    return f"{type(exc).__name__}:{message}"[:500]


# Ordered token groups. Order matters: a network timeout is a connection
# outcome even though the word "timeout" appears in it, and an explicit HTTP
# failure is named before the generic page/pagination tokens can swallow it.
_CONNECT_TOKENS = (
    "connectionerror", "connecttimeout", "readtimeout", "connectionreset",
    "connectionaborted", "proxyerror", "sslerror", "remotedisconnected",
    "httperror", "http 4", "http 5", "status_code", "getaddrinfo",
    "name or service", "max retries", "tunnel connection", "remote end closed",
)
_PROTOCOL_TOKENS = (
    "not a list", "not an object", "invalid response", "unexpected response",
    "schema", "keyerror", "attributeerror", "missing column", "protocol",
    "jsondecode",
)
_EMPTY_TOKENS = ("empty_or_invalid_symbols", "empty_roster", "rows_empty", "no rows")
_PAGINATION_TOKENS = (
    "pagination", "terminal_page", "unconsumed_page", "truncat", "page ",
    "incomplete_roster",
)
_SYMBOL_TOKENS = (
    "invalid_symbols", "invalid_symbol", "symbol_mismatch", "attribution",
    "invalid_rows", "not_object",
)
_DEADLINE_TOKENS = ("deadline", "total_budget_exhausted", "budget_exhausted")


def _class_from_text(text: Any) -> str:
    """Classify one piece of source-supplied text. "" means "no known cause"."""
    lowered = str(text or "").lower()
    if not lowered:
        return ""
    for tokens, name in (
        (_CONNECT_TOKENS, "connect_or_http_failure"),
        (_PROTOCOL_TOKENS, "protocol_or_schema_change"),
        (_EMPTY_TOKENS, "empty_roster"),
        (_PAGINATION_TOKENS, "pagination_incomplete"),
        (_SYMBOL_TOKENS, "date_or_symbol_validation_failed"),
        (_DEADLINE_TOKENS, "time_budget_exhausted"),
    ):
        if any(token in lowered for token in tokens):
            return name
    return ""


def _classify_source_failure(attempt: dict[str, Any], error: str) -> str:
    """Map one source attempt onto the failure class the receipt must name.

    Evidence order, so a generic wrapper never overwrites a real cause:

    1. ``provider_warning`` - the source's own stated reason (HTTP status, page
       failure, non-list response). This is evidence, not a guess.
    2. an explicit parent deadline (``timeout_kind`` / ``deadline_exhausted``) -
       budget exhaustion and nothing else.
    3. the recorded error text.

    A bare ``"timeout"`` in a message is NOT budget exhaustion: ``ConnectTimeout``
    and ``ReadTimeout`` are network outcomes and stay connection failures. An
    unknown reason stays ``unknown_failure`` rather than being guessed.
    """
    if str(attempt.get("status") or "") == "not_attempted":
        return "not_attempted_budget"
    from_warning = _class_from_text(attempt.get("provider_warning"))
    if from_warning:
        return from_warning
    if attempt.get("deadline_exhausted") or attempt.get("timeout_kind"):
        return "time_budget_exhausted"
    text = str(error or attempt.get("error") or "")
    if "provider_deadline_exhausted" in text.lower():
        return "time_budget_exhausted"
    from_error = _class_from_text(text)
    if from_error:
        return from_error
    return "unknown_failure"


def _baostock_completion_evidence(
    result: Any, before: tuple[object, int | None, object], *, page_size: int,
) -> tuple[bool, str]:
    """SDK False also means failed pagination, sometimes with error_code == 0."""
    if str(getattr(result, "error_code", "")) != "0":
        return False, "provider_error"
    data = getattr(result, "data", None)
    cursor = getattr(result, "cur_row_num", None)
    page_text = str(getattr(result, "cur_page_num", ""))
    size_text = str(getattr(result, "per_page_count", ""))
    before_page, before_count, before_cursor = before
    if (not isinstance(data, list) or type(cursor) is not int
            or not re.fullmatch(r"[0-9]+", page_text)
            or not re.fullmatch(r"[0-9]+", str(before_page))
            or not re.fullmatch(r"[0-9]+", size_text)):
        return False, "unverified_pagination_state"
    page, prior_page = int(page_text), int(str(before_page))
    if page < 1 or prior_page < 1 or int(size_text) != page_size:
        return False, "invalid_pagination_metadata"
    count = len(data)
    if cursor != count:
        return False, "unconsumed_page"
    # An empty/blank failed continuation leaves the exhausted full page intact.
    if count == page_size:
        return False, "full_page_without_terminal_response"
    if before_count == page_size and before_cursor == page_size:
        if count == 0 and cursor == 0 and page == prior_page + 1:
            return True, "successful_empty_terminal_page"
        return False, "terminal_page_not_advanced"
    if (page, count, cursor) != (prior_page, before_count, before_cursor):
        return False, "unverified_terminal_transition"
    if count == 0:
        return (True, "successful_empty_initial_page") if page == 1 else (False, "unverified_empty_page")
    if count < page_size:
        return True, "consumed_short_terminal_page"
    # The SDK also documents a > page-size, single-response/no-pagination path.
    return True, "consumed_unpaged_response"


def _universe_source_worker(provider: str, context: dict[str, Any]) -> dict[str, Any]:
    """Only fetch in a killable worker; canonical publication stays in parent."""
    def phase(name: str) -> None:
        if context.get("report_phases"):
            print("QD_ROSTER_PHASE " + json.dumps({"phase": name, "at": time.monotonic(), "pid": os.getpid()}),
                  file=sys.stderr, flush=True)

    phase("provider_import")
    if provider == "sina.spot_direct":
        from quantdesk.universe_refresh import build_default_fetchers
        import requests

        def request_get(url: str, **kwargs: Any) -> Any:
            remaining = float(context["fetch_deadline_monotonic"]) - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("roster_source_deadline_exhausted")
            kwargs["timeout"] = min(float(kwargs.get("timeout", 5)), remaining)
            return requests.get(url, **kwargs)

        # Membership needs no market-clock-derived quote date. Only identities
        # and terminal pagination evidence leave this worker.
        fetch = build_default_fetchers(request_get=request_get, clock=lambda: "", retries=0, timeout_seconds=5.0)["sina"]
        phase("provider_call")
        frame = fetch(target_date=datetime.now(SHANGHAI).date().isoformat())
        phase("normalize")
        complete = frame.attrs.get("pagination_complete") is True
        return {"rows": normalize_universe_frame(frame, provider), "response_complete": complete,
                "pagination_complete": complete, "completion_evidence": "terminal_page",
                "observed_pages": frame.attrs.get("observed_pages"), "expected_pages": frame.attrs.get("expected_pages"),
                "provider_status": frame.attrs.get("provider_status"),
                "provider_warning": _provider_error_text(RuntimeError(frame.attrs["provider_warning"])) if frame.attrs.get("provider_warning") else ""}
    if provider == "baostock.query_stock_basic":
        import baostock as bs  # type: ignore
        from baostock.common.contants import BAOSTOCK_PER_PAGE_COUNT  # type: ignore
        import pandas as pd

        phase("provider_call")
        login = bs.login()
        if str(login.error_code) != "0":
            raise RuntimeError(f"baostock_login:{login.error_code}")
        try:
            result = bs.query_stock_basic()
            if str(result.error_code) != "0":
                raise RuntimeError(f"baostock_query:{result.error_code}")
            records = []
            while True:
                page_data = getattr(result, "data", None)
                before = (getattr(result, "cur_page_num", None),
                          len(page_data) if isinstance(page_data, list) else None,
                          getattr(result, "cur_row_num", None))
                if not result.next():
                    break
                record = dict(zip(result.fields, result.get_row_data()))
                # Exclude indices/funds and explicitly delisted instruments.
                if record.get("type") == "1" and record.get("status") == "1":
                    records.append({"code": record.get("code"), "name": record.get("code_name")})
            complete, evidence = _baostock_completion_evidence(result, before, page_size=BAOSTOCK_PER_PAGE_COUNT)
            phase("normalize")
            return {"rows": normalize_universe_frame(pd.DataFrame(records), provider),
                    "response_complete": complete, "pagination_complete": complete,
                    "completion_evidence": evidence,
                    "provider_warning": "" if complete else _provider_error_text(
                        RuntimeError(f"baostock_response_incomplete:{evidence}"))}
        finally:
            phase("provider_cleanup")
            bs.logout()
    if provider not in UNIVERSE_SOURCE_PROVIDERS:
        raise ValueError("unapproved_universe_provider")
    import akshare as ak  # type: ignore

    phase("provider_call")
    frame = getattr(ak, provider)()
    phase("normalize")
    return {"rows": normalize_universe_frame(frame, provider), "response_complete": True,
            "completion_evidence": "provider_return"}


def _run_roster_source_process(
    worker: Callable[..., Any], tasks: Sequence[str], *, context: dict[str, Any],
    max_workers: int, timeout_seconds: float,
) -> ProcessDeadlineResult:
    """One source owns one phase; its cleanup consumes the same absolute budget."""
    import subprocess

    if worker is not _universe_source_worker or len(tasks) != 1 or max_workers != 1:
        raise ValueError("roster_worker_requires_one_source")
    started = time.monotonic()
    deadline = min(started + max(0.0, timeout_seconds), float(context["deadline_monotonic"]))
    allowance = max(0.0, deadline - started)
    if allowance <= 0:
        return ProcessDeadlineResult({}, {0: "total_budget_exhausted"}, [0], True, [])
    cleanup_budget = min(1.0, allowance / 5.0)
    fetch_deadline = deadline - cleanup_budget
    script = Path(__file__).resolve()
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
    child = subprocess.Popen(
        [sys.executable, "-B", str(script), "--source-worker"], cwd=str(script.parents[1]), env=env,
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, encoding="utf-8", errors="replace", creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    timed_out = False
    try:
        stdout, stderr = child.communicate(json.dumps({"provider": tasks[0], "network": True,
            "context": {**context, "fetch_deadline_monotonic": fetch_deadline, "report_phases": True}}),
            timeout=max(0.0, fetch_deadline - time.monotonic()))
    except subprocess.TimeoutExpired:
        timed_out = True
        try:
            child.kill()
        except ProcessLookupError:
            pass
        try:
            stdout, stderr = child.communicate(timeout=max(0.0, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            stdout, stderr = "", ""
    phases = []
    for line in stderr.splitlines():
        if line.startswith("QD_ROSTER_PHASE "):
            try:
                phases.append(json.loads(line[len("QD_ROSTER_PHASE "):]))
            except (ValueError, TypeError):
                continue
    evidence = {"runtime_pid": child.pid, "return_code": child.returncode, "process_reaped": child.returncode is not None,
                "deadline_monotonic": deadline, "fetch_deadline_monotonic": fetch_deadline,
                "cleanup_budget_seconds": cleanup_budget, "duration_seconds": time.monotonic() - started,
                "last_phase": phases[-1]["phase"] if phases else "startup", "phase_events": phases}
    # A stopped child may already have reported the real upstream failure. That
    # diagnosis must survive the timeout: replacing it with a generic deadline
    # reason is exactly what left the 2026-10-14 roster receipt unable to say
    # whether a source never connected or was merely slow. Same contract as the
    # minute HTTP worker (test_minute_http_failure_budget_20261013).
    recovered: dict[str, Any] | None = None
    try:
        parsed = json.loads(stdout)
        if isinstance(parsed, dict):
            recovered = parsed
    except (ValueError, TypeError):
        recovered = None

    if child.returncode is None:
        payload = {"worker_error": "worker_not_reaped_within_phase", "timeout_kind": "worker_not_reaped"}
    elif timed_out or child.returncode == 124 or time.monotonic() >= deadline:
        timeout_kind = (
            "worker_watchdog_exit_124" if child.returncode == 124
            else "parent_communicate_timeout" if timed_out
            else "parent_deadline_passed"
        )
        child_error = str((recovered or {}).get("worker_error") or "").strip()
        payload = dict(recovered or {})
        if isinstance(payload.get("rows"), list):
            payload["partial_row_count"] = len(payload["rows"])
        # A timed-out child is never a roster source, whatever it managed to say.
        payload.pop("rows", None)
        payload.pop("response_complete", None)
        payload.pop("pagination_complete", None)
        payload["timeout_kind"] = timeout_kind
        payload["deadline_exhausted"] = True
        if child_error:
            payload["worker_error"] = child_error
            payload["child_error"] = child_error
        else:
            payload["worker_error"] = "provider_deadline_exhausted"
    elif recovered is not None:
        payload = recovered
        if child.returncode and not payload.get("worker_error"):
            payload["worker_error"] = f"worker_exit:{child.returncode}"
    else:
        payload = {"worker_error": f"worker_unparsable_output:return_code_{child.returncode}"}
    payload["stdout_recovered"] = recovered is not None
    payload["process_evidence"] = evidence
    return ProcessDeadlineResult({0: payload}, {}, [], timed_out, [child.pid])


def _universe_worker_main() -> int:
    """Private stdin/stdout protocol; no CSV or status writes in this process."""
    import contextlib
    import threading

    watchdog = None
    try:
        request = json.load(sys.stdin)
        if request.get("network") is not True:
            raise ValueError("network_not_authorized")
        context = request["context"]
        remaining = float(context["fetch_deadline_monotonic"]) - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("worker_startup_exhausted_phase")
        # Starts before pandas, requests, SDK, or the provider helper imports.
        # It also caps a real interpreter behind a Windows venv launcher.
        watchdog = threading.Timer(remaining, os._exit, args=(124,))
        watchdog.daemon = True
        watchdog.start()
        with contextlib.redirect_stdout(sys.stderr):
            payload = _universe_source_worker(str(request["provider"]), context)
        code = 0
    except Exception as exc:
        payload, code = {"worker_error": _provider_error_text(exc)}, 1
    try:
        print(json.dumps(payload, ensure_ascii=False, default=str), flush=True)
    finally:
        if watchdog is not None:
            watchdog.cancel()
    return code


def fetch_universe_from_akshare(
    *, budget_seconds: float = 35.0, minimum_rows: int = 5000,
    providers: Sequence[str] | None = None, clock: Callable[[], float] = time.monotonic,
    runner: Callable[..., Any] | None = None,
) -> tuple[UniverseSourceRows, str, dict[str, str]]:
    """Bound each source so one stalled endpoint cannot consume all retries.

    Two independent responses are sought when time permits. Partial lists are
    retained as discovery evidence, never promoted to a complete roster.
    """
    order = list(providers if providers is not None else DEFAULT_ROSTER_PROVIDERS)
    runner = runner or _run_roster_source_process
    deadline = clock() + max(0.0, min(90.0, float(budget_seconds)))
    result = UniverseSourceRows()
    provider_errors: dict[str, str] = {}
    best_provider = ""
    successes = 0
    for index, provider in enumerate(order):
        left = max(0.0, deadline - clock())
        if providers is not None:
            # Preserve explicit diagnostic source orders; normal operation no
            # longer rotates four cold SDKs through quarter-sized windows.
            allowance = min(20.0 if provider == "sina.spot_direct" else 12.0, left / max(1, len(order) - index))
        elif index == 0:
            # The primary source keeps its original window. Taking the
            # reservation out of it crushed cold starts on small budgets (a 10s
            # CLI budget left it 2.6s instead of the baseline 5.2s).
            allowance = min(20.0, left * 0.65)
        else:
            cap = 12.0 if provider == "baostock.query_stock_basic" else 15.0
            queued = len(order) - index - 1
            reserved = 0.0
            if queued and left >= _MIN_SOURCE_SLICE * (queued + 1):
                # Set a real window aside for every source still queued, but only
                # when this source can still get a real window of its own.
                reserved = min(_MIN_SOURCE_SLICE * queued, left - _MIN_SOURCE_SLICE)
            # Charge the parent's own prepare/reap/accounting to this source, and
            # hand any positive remainder to the last queued source rather than
            # requiring another full slice (which is what starved it).
            usable = max(0.0, left - reserved - _PARENT_OVERHEAD_SECONDS)
            allowance = min(cap, usable) if usable > 0 else 0.0
        attempt: dict[str, Any] = {"provider": provider, "budget_seconds": allowance}
        result.attempts.append(attempt)
        if allowance <= 0:
            attempt.update(status="not_attempted", error="total_budget_exhausted")
            attempt["failure_class"] = "not_attempted_budget"
            provider_errors[provider] = "total_budget_exhausted"
            continue
        started = clock()
        try:
            batch = runner(_universe_source_worker, [provider], context={"deadline_monotonic": min(deadline, started + allowance)}, max_workers=1,
                           timeout_seconds=allowance)
            if batch.failures or batch.unfinished_indices or 0 not in batch.results:
                raise RuntimeError(batch.failures.get(0) or "provider_deadline_exhausted")
            payload = batch.results[0]
            # Explicit runner injection retains the old list contract for SDK
            # fixtures. A direct paginated result always needs terminal proof.
            metadata = payload if isinstance(payload, dict) else {}
            if metadata.get("process_evidence"):
                attempt["process_evidence"] = metadata["process_evidence"]
            if metadata.get("provider_warning"):
                attempt["provider_warning"] = metadata["provider_warning"]
            # Keep the child's own diagnosis and the timeout facts on the attempt
            # so the receipt names the failure class instead of one generic
            # string for every stalled source.
            for key in ("timeout_kind", "deadline_exhausted", "child_error",
                        "partial_row_count", "stdout_recovered"):
                if key in metadata:
                    attempt[key] = metadata[key]
            if metadata.get("worker_error"):
                raise RuntimeError(str(metadata["worker_error"]))
            rows = metadata.get("rows") if metadata else payload
            if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
                raise ValueError("provider_invalid_rows")
            if clock() >= deadline:
                raise TimeoutError("total_budget_exhausted")
            # The worker normalizes live source rows. No cache is admitted here.
            codes = sorted({str(row.get("symbol") or "") for row in rows})
            if not codes:
                # An empty response and a non-empty but unusable one are
                # different facts and must not share one diagnosis.
                raise ValueError("provider_empty_or_invalid_symbols")
            invalid = [code for code in codes if len(code) != 6 or not code.isdigit()]
            if invalid:
                raise ValueError("provider_invalid_symbols:" + ",".join(invalid[:3]))
            if any(row.get("source") != provider for row in rows):
                raise ValueError("provider_attribution_mismatch")
            response_complete = (metadata.get("response_complete") is True if metadata else provider in UNIVERSE_SOURCE_PROVIDERS)
            if provider == "sina.spot_direct":
                response_complete = response_complete and metadata.get("pagination_complete") is True
            complete = response_complete and len(codes) >= minimum_rows
            observed_at = now_text()
            result.source_snapshots.append({
                "source": provider, "observed_at": observed_at, "symbols": codes,
                "row_count": len(codes), "status": "complete" if complete else "partial",
                "response_complete": response_complete, "pagination_complete": metadata.get("pagination_complete"),
                "completion_evidence": metadata.get("completion_evidence", "provider_return"),
                "observed_pages": metadata.get("observed_pages"), "expected_pages": metadata.get("expected_pages"),
                "symbols_sha256": hashlib.sha256(json.dumps(codes, separators=(",", ":")).encode()).hexdigest(),
                "contract": "Observed current source membership; not dated price or suspension evidence.",
            })
            attempt.update(status="complete" if complete else "partial", row_count=len(codes), response_complete=response_complete)
            if (complete, len(rows)) > (result.complete, len(result)):
                result[:] = rows
                result.complete = complete
                best_provider = provider
            if complete:
                successes += 1
            else:
                provider_errors[provider] = (f"incomplete_roster:{len(codes)}<{minimum_rows}" if len(codes) < minimum_rows
                                             else "incomplete_roster:pagination_or_response_unfinished")
        except Exception as exc:
            # The runner joins/terminates its own workers before returning.
            error = _provider_error_text(exc)
            provider_errors[provider] = error
            attempt.update(status="failed", error=error)
        attempt["duration_seconds"] = max(0.0, clock() - started)
        if attempt.get("status") != "complete":
            attempt["failure_class"] = _classify_source_failure(attempt, provider_errors.get(provider, ""))
        if (attempt.get("process_evidence") or {}).get("process_reaped") is False:
            break
        if successes >= 2:
            break
    return result, best_provider, provider_errors


def load_local_fallback_universe(config: dict[str, Any] | None = None) -> list[dict[str, str]]:
    cfg = config if config is not None else load_config()
    data_cfg = cfg.get("data", {}) if isinstance(cfg.get("data"), dict) else {}
    rows: list[dict[str, str]] = []

    daily_dir = PROJECT_ROOT / str(data_cfg.get("daily_cache_dir") or "data/daily")
    if not daily_dir.is_absolute():
        daily_dir = PROJECT_ROOT / daily_dir
    if daily_dir.exists():
        for path in sorted(daily_dir.glob("*.csv")):
            _add_symbol(rows, path.stem, "", "local_daily_cache")

    for status_name in ["update_status.json", "spot_snapshot_meta.json"]:
        status_path = PROJECT_ROOT / "data" / "cache" / status_name
        if not status_path.exists():
            continue
        try:
            raw = json.loads(status_path.read_text(encoding="utf-8"))
        except Exception:
            continue
        for item in raw.get("symbols", []) or raw.get("results", []) or []:
            if isinstance(item, dict):
                _add_symbol(rows, item.get("symbol") or item.get("code"), item.get("name", ""), f"cache_{status_name}")

    for pattern in ["reports/*_candidates.csv", "reports/*_long_candidates.csv", "reports/*_watchlist.csv"]:
        for path in sorted(PROJECT_ROOT.glob(pattern), key=lambda item: item.stat().st_mtime, reverse=True)[:5]:
            df = read_csv_frame(path, dtype=str)
            if any(item.startswith("csv_read_failed:") for item in list(df.attrs.get("warnings") or [])):
                continue
            for _, row in df.iterrows():
                _add_symbol(rows, row.get("symbol") or row.get("code") or row.get("股票代码"), row.get("name") or row.get("名称"), f"report_{path.name}")

    for symbol in data_cfg.get("fallback_symbols") or DEFAULT_FALLBACK_SYMBOLS:
        _add_symbol(rows, symbol, "", "config_fallback_symbols")
    if not rows:
        for symbol in DEFAULT_FALLBACK_SYMBOLS:
            _add_symbol(rows, symbol, "", "default_fallback_symbols")
    return rows


def resolve_output_paths(config: dict[str, Any] | None, output: str = "", status_path: str = "") -> tuple[Path, Path]:
    cfg = config if config is not None else load_config()
    data_cfg = cfg.get("data", {}) if isinstance(cfg.get("data"), dict) else {}
    output_path = Path(output or str(data_cfg.get("a_share_universe_path") or DEFAULT_UNIVERSE_PATH))
    status = Path(status_path or str(data_cfg.get("a_share_universe_status_path") or DEFAULT_STATUS_PATH))
    if not output_path.is_absolute():
        output_path = PROJECT_ROOT / output_path
    if not status.is_absolute():
        status = PROJECT_ROOT / status
    return output_path, status


def write_universe_csv(path: Path, rows: list[dict[str, str]], *, limit: int = 0, deadline_monotonic: float | None = None) -> None:
    import pandas as pd

    path.parent.mkdir(parents=True, exist_ok=True)
    output_rows = sorted(rows, key=lambda item: item["symbol"])
    if limit and limit > 0:
        output_rows = output_rows[:limit]
    df = pd.DataFrame(output_rows, columns=["symbol", "name", "source"])
    df["updated_at"] = now_text()
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("w", encoding="utf-8-sig", newline="") as handle:
            df.to_csv(handle, index=False)
            handle.flush()
            os.fsync(handle.fileno())
        if deadline_monotonic is not None and time.monotonic() >= deadline_monotonic:
            raise TimeoutError("roster_publication_deadline_exhausted")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def run_update_universe(args: argparse.Namespace, *, started_monotonic: float | None = None) -> dict[str, Any]:
    started = time.monotonic() if started_monotonic is None else started_monotonic
    budget = max(0.0, min(90.0, float(getattr(args, "budget_seconds", 35))))
    deadline = started + budget
    config = load_config(args.config or None)
    output_path, status_path = resolve_output_paths(config, args.output, args.status_path)
    configured_output, _ = resolve_output_paths(config)
    limited = bool(args.limit and args.limit > 0)
    protected_outputs = {configured_output.resolve(), DEFAULT_UNIVERSE_PATH.resolve()}
    preview_output = bool(args.output) and output_path.resolve() not in protected_outputs
    limit_blocked = limited and not args.dry_run and not preview_output
    if limited and preview_output and not args.status_path:
        status_path = output_path.with_name(output_path.name + ".status.json")
    started_at = now_text()
    previous = read_csv_frame(output_path, dtype=str) if output_path.is_file() else []
    before_sha = hashlib.sha256(output_path.read_bytes()).hexdigest() if output_path.is_file() else ""
    minimum_rows = max(5000, math.ceil(len(previous) * 0.95))
    rows, provider_used, provider_errors = fetch_universe_from_akshare(
        budget_seconds=max(0.0, deadline - time.monotonic() - min(2.0, budget / 10)), minimum_rows=minimum_rows,
    )
    attempts = list(getattr(rows, "attempts", []))
    source_snapshots = list(getattr(rows, "source_snapshots", []))
    warnings: list[str] = []
    fresh_roster = rows.complete and len(rows) >= minimum_rows and time.monotonic() < deadline
    fetched_count = len(rows)
    if not fresh_roster:
        warnings.append("fresh_universe_unavailable:existing_roster_bytes_and_updated_at_preserved")
    if limit_blocked:
        warnings.append("limited_roster_publish_refused:use_dry_run_or_explicit_nondefault_output")
    if limited:
        rows = rows[: args.limit]
    published = fresh_roster and not args.dry_run and not limit_blocked
    if published:
        try:
            write_universe_csv(output_path, rows, deadline_monotonic=deadline)
        except TimeoutError:
            published = fresh_roster = False
            warnings.append("roster_publication_deadline_exhausted:existing_roster_preserved")
    if limit_blocked:
        result_status = "limit_refused_preserved_existing"
    elif not fresh_roster:
        result_status = "failed_preserved_existing"
    elif limited:
        result_status = "preview_dry_run" if args.dry_run else "preview_written"
    else:
        result_status = "dry_run" if args.dry_run else "refreshed"
    status = {
        "schema_version": "universe_update.v2",
        "started_at": started_at,
        "finished_at": now_text(),
        "provider_used": provider_used,
        "provider_errors": provider_errors,
        # One class per failed source, so the receipt separates "never connected"
        # from "answered too slowly" without re-deriving it from log text.
        "provider_error_classes": {
            str(item.get("provider") or ""): str(item.get("failure_class") or "unknown_failure")
            for item in attempts
            if item.get("failure_class")
        },
        "row_count": len(rows) if fresh_roster and not limit_blocked else len(previous),
        "fetched_row_count": fetched_count,
        "minimum_complete_rows": minimum_rows,
        "source_attempts": attempts,
        "source_snapshots": source_snapshots,
        "published": bool(published),
        "full_roster_published": bool(published and not limited),
        "preview": limited,
        "status": result_status,
        "previous_roster_sha256": before_sha,
        "roster_sha256": hashlib.sha256(output_path.read_bytes()).hexdigest() if output_path.is_file() else "",
        "output_path": str(output_path),
        "status_path": str(status_path),
        "dry_run": bool(args.dry_run),
        "budget_seconds": budget,
        "duration_seconds": max(0.0, time.monotonic() - started),
        "warnings": warnings,
        "ok": fresh_roster and not limit_blocked,
    }
    if not args.dry_run:
        atomic_write_json(status_path, status)
    return status


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Update local A-share universe CSV for QuantDesk.")
    parser.add_argument("--config", default="", help="Optional config path.")
    parser.add_argument("--output", default="", help="Output CSV path, default data/universe/a_share_symbols.csv.")
    parser.add_argument("--status-path", default="", help="Universe update status JSON path.")
    parser.add_argument("--limit", type=int, default=0,
                        help="Preview at most N rows; requires --dry-run or an explicit non-default --output.")
    parser.add_argument("--dry-run", action="store_true", help="Resolve universe without writing files.")
    parser.add_argument("--budget-seconds", type=float, default=35.0, help="Shared bounded source-refresh budget (max 90s).")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    status = run_update_universe(args, started_monotonic=_ENTRY_STARTED_MONOTONIC if __name__ == "__main__" else None)
    print(
        f"update_a_share_universe finished: ok={status['ok']} provider={status['provider_used']} "
        f"rows={status['row_count']} output={status['output_path']}"
    )
    if status.get("warnings"):
        for warning in status["warnings"]:
            print(f"WARN {warning}")
    return 0 if status.get("ok") else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(_universe_worker_main() if sys.argv[1:] == ["--source-worker"] else main())
