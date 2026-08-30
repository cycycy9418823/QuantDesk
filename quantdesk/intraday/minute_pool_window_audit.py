"""Stage 2: windowed minute-pool quality audit (read-only, offline).

Task book section 6: extend the minute quality gate from a single
``target_date`` to a replay window, so the C (board) replay account can use
every locally-verified symbol-day instead of only the Top200 single-date pool.

Design constraints honoured here:

* **Zero standard drift** — every day is graded by the EXISTING
  :func:`quantdesk.intraday.intraday_quality_audit._audit_frame` logic via a
  read-once/slice-per-day loop.  A window grade equals what
  :func:`audit_intraday_file` would return for that symbol and date.
* **Read-only** — canonical ``data/intraday/1m`` files are never written;
  the only output is a snapshot under ``results/data_health/``.
* **Fail-closed consumers** — window coverage is symbol+date; anything not
  explicitly usable inside the window stays unverified/no-fill downstream.

Research Only / Paper Account Only / No Broker Route / Not Investment Advice.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Any, Iterable, Mapping

import pandas as pd

from quantdesk.intraday.intraday_quality_audit import (
    DISCLAIMER,
    INTRADAY_1M_DIR,
    _audit_frame,
    _display_path,
)
from quantdesk.intraday.intraday_schema import read_intraday_csv
from quantdesk.intraday.minute_pool_capability import classify_minute_replay_evidence
from quantdesk.shared import normalize_symbol, now_text, write_json
from quantdesk.trading_calendar import trade_days_between
from quantdesk.version import API_SCHEMA_VERSION, APP_VERSION

PROJECT_ROOT = Path(__file__).resolve().parents[2]
WINDOW_AUDIT_LATEST_JSON = (
    PROJECT_ROOT / "results" / "data_health" / "minute_pool_window_audit_latest.json"
)

USABLE_STANDARD = "existing_intraday_quality_audit_v1"

__all__ = [
    "audit_minute_pool_window",
    "load_window_verified_minute_pool",
    "window_pool_covers",
    "classify_minute_replay_evidence_windowed",
    "resolve_minute_replay_evidence",
    "WINDOW_AUDIT_LATEST_JSON",
]

_WINDOW_DAY_STATUSES = {"usable", "missing", "invalid", "partial", "insufficient"}


def _window_day(value: Any) -> str | None:
    """Return an ISO day for a snapshot field, or ``None`` when malformed."""

    text = str(value or "").strip()[:10]
    if len(text) != 10:
        return None
    try:
        date.fromisoformat(text)
    except ValueError:
        return None
    return text


def _window_snapshot_is_semantically_valid(payload: Mapping[str, Any]) -> bool:
    """Validate the minimum contract needed for symbol+date arbitration.

    The audit artifact is allowed to contain an empty ``symbols`` list.  That
    is a valid, authoritative (but non-passing) window rather than a missing
    snapshot.  Optional metadata is checked when present, while the core
    window, row, and status shapes are fail-closed.
    """

    if payload.get("task_type") != "minute_pool_window_audit":
        return False
    start = _window_day(payload.get("window_start"))
    end = _window_day(payload.get("window_end"))
    if not start or not end or start > end:
        return False
    rows = payload.get("symbols")
    if not isinstance(rows, list):
        return False
    criteria = payload.get("criteria")
    if isinstance(criteria, Mapping) and criteria.get("usable_standard") not in {
        None,
        USABLE_STANDARD,
    }:
        return False
    for count_key in ("total_symbols", "fully_usable_symbol_count"):
        count_value = payload.get(count_key)
        if count_value is not None and (
            isinstance(count_value, bool)
            or not isinstance(count_value, int)
            or count_value < 0
        ):
            return False
    trading_days = payload.get("trading_days")
    if trading_days is not None:
        if not isinstance(trading_days, list):
            return False
        for raw_day in trading_days:
            day = _window_day(raw_day)
            if day is None or not (start <= day <= end):
                return False
    seen: set[str] = set()
    for row in rows:
        if not isinstance(row, Mapping):
            return False
        symbol = normalize_symbol(row.get("symbol"))
        day_status = row.get("day_status")
        if not symbol or symbol in seen or not isinstance(day_status, Mapping):
            return False
        seen.add(symbol)
        for raw_day, status in day_status.items():
            day = _window_day(raw_day)
            if day is None or not (start <= day <= end):
                return False
            if str(status or "") not in _WINDOW_DAY_STATUSES:
                return False
    return True


def _empty_window_pool(path: Path, *, status: str, warning: str) -> dict[str, Any]:
    return {
        "passed": False,
        "snapshot_valid": False,
        "status": status,
        "window_start": "",
        "window_end": "",
        "snapshot_path": _display_path(path),
        "source_snapshot": "window_audit",
        "usable_by_symbol": {},
        "total_symbols": 0,
        "fully_usable_symbol_count": 0,
        "warnings": [warning],
    }


def _audit_symbol_days(
    symbol: str,
    days: list[str],
    *,
    intraday_dir: Path,
    encoding: str = "auto",
    price_basis: str = "",
) -> dict[str, str]:
    """Grade one symbol for every window day, reading its CSV exactly once.

    Returns ``{date: status}`` where status matches what
    :func:`audit_intraday_file` would produce for that (symbol, date).
    """

    path = intraday_dir / f"{symbol}.csv"
    if not path.exists():
        return {day: "missing" for day in days}
    payload = read_intraday_csv(path, default_symbol=symbol, target_date="", encoding=encoding)
    frame = payload.get("frame")
    if not isinstance(frame, pd.DataFrame) or frame.empty:
        return {day: "missing" for day in days}
    date_series = frame.get("date")
    if date_series is None:
        return {day: "missing" for day in days}
    date_keys = date_series.astype(str).str.slice(0, 10)
    statuses: dict[str, str] = {}
    for day in days:
        day_frame = frame.loc[date_keys == day]
        if day_frame.empty:
            statuses[day] = "missing"
            continue
        day_payload = dict(payload)
        audit = _audit_frame(
            day_frame.reset_index(drop=True),
            symbol=symbol,
            target_date=day,
            payload=day_payload,
            price_basis=price_basis,
        )
        statuses[day] = str(audit.get("status") or "invalid")
    return statuses


def _discover_symbols(intraday_dir: Path) -> list[str]:
    return list(
        dict.fromkeys(
            sorted(
                normalize_symbol(path.stem)
                for path in intraday_dir.glob("*.csv")
                if normalize_symbol(path.stem)
            )
        )
    )


def audit_minute_pool_window(
    *,
    window_start: str,
    window_end: str,
    symbols: Iterable[str] | None = None,
    intraday_dir: Path | str | None = None,
    calendar_path: Path | str | None = None,
    output_path: Path | str | None = None,
    write: bool = True,
    encoding: str = "auto",
    price_basis: str = "",
    max_exception_dates: int = 30,
) -> dict[str, Any]:
    """Grade every (symbol, trading-day) pair inside the window.

    Reads each canonical CSV once and slices per day; grading is delegated to
    the existing single-date auditor so the quality standard cannot drift.
    """

    directory = Path(intraday_dir) if intraday_dir is not None else INTRADAY_1M_DIR
    days = trade_days_between(
        window_start,
        window_end,
        calendar_path=Path(calendar_path) if calendar_path else None,
    )
    requested = list(
        dict.fromkeys(
            normalize_symbol(item)
            for item in (symbols or [])
            if normalize_symbol(item)
        )
    )
    pool_symbols = requested or _discover_symbols(directory)

    symbol_rows: list[dict[str, Any]] = []
    per_day_usable: dict[str, int] = {day: 0 for day in days}
    fully_usable = 0
    for symbol in pool_symbols:
        day_status = _audit_symbol_days(
            symbol,
            days,
            intraday_dir=directory,
            encoding=encoding,
            price_basis=price_basis,
        )
        usable_days = [day for day, status in day_status.items() if status == "usable"]
        not_usable = [day for day in days if day_status.get(day) != "usable"]
        for day in usable_days:
            per_day_usable[day] += 1
        all_usable = len(usable_days) == len(days) and bool(days)
        if all_usable:
            fully_usable += 1
        symbol_rows.append(
            {
                "symbol": symbol,
                "day_status": day_status,
                "usable_day_count": len(usable_days),
                "all_days_usable": all_usable,
                "not_usable_date_count": len(not_usable),
                "not_usable_dates": not_usable[: max(0, int(max_exception_dates))],
            }
        )

    payload: dict[str, Any] = {
        "ok": True,
        "schema_version": API_SCHEMA_VERSION,
        "app_version": APP_VERSION,
        "task_type": "minute_pool_window_audit",
        "generated_at": now_text(),
        "window_start": str(window_start)[:10],
        "window_end": str(window_end)[:10],
        "trading_days": days,
        "trading_day_count": len(days),
        "intraday_dir": _display_path(directory),
        "total_symbols": len(pool_symbols),
        "fully_usable_symbol_count": fully_usable,
        "per_day_usable_counts": per_day_usable,
        "symbols": symbol_rows,
        "criteria": {
            "usable_standard": USABLE_STANDARD,
            "grader": "quantdesk.intraday.intraday_quality_audit._audit_frame",
            "note": "window grade per day is identical to audit_intraday_file for that date",
        },
        "read_only": True,
        "network_used": False,
        "canonical_write": False,
        "research_only": True,
        "paper_account_only": True,
        "no_broker_route": True,
        "disclaimer": DISCLAIMER,
    }
    if write:
        target = Path(output_path) if output_path is not None else WINDOW_AUDIT_LATEST_JSON
        write_json(target, payload)
        payload["path"] = _display_path(target)
    return payload


def load_window_verified_minute_pool(
    *,
    snapshot_path: Path | str | None = None,
) -> dict[str, Any]:
    """Load the window audit snapshot as a fail-closed verified pool.

    ``snapshot_valid`` records whether the window artifact itself is valid;
    ``passed`` records whether it contains at least one usable symbol-day.
    These are intentionally separate so a valid empty window still has
    symbol+date arbitration authority over a legacy symbol-only pool.
    """

    path = Path(snapshot_path) if snapshot_path is not None else WINDOW_AUDIT_LATEST_JSON
    if not path.exists():
        return _empty_window_pool(
            path,
            status="missing",
            warning="window_audit_snapshot_missing_or_unreadable",
        )
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return _empty_window_pool(
            path,
            status="corrupt",
            warning="window_audit_snapshot_missing_or_unreadable",
        )
    if not isinstance(payload, dict) or not _window_snapshot_is_semantically_valid(payload):
        return _empty_window_pool(
            path,
            status="invalid",
            warning="window_audit_snapshot_semantically_invalid",
        )
    usable_by_symbol: dict[str, set[str]] = {}
    for row in payload.get("symbols") or []:
        if not isinstance(row, Mapping):
            # The semantic validator above makes this unreachable, but keep
            # the loader fail-closed if its contract changes independently.
            return _empty_window_pool(
                path,
                status="invalid",
                warning="window_audit_snapshot_semantically_invalid",
            )
        symbol = normalize_symbol(row.get("symbol"))
        if not symbol:
            return _empty_window_pool(
                path,
                status="invalid",
                warning="window_audit_snapshot_semantically_invalid",
            )
        day_status = row.get("day_status") if isinstance(row.get("day_status"), Mapping) else {}
        usable_days = {str(day)[:10] for day, status in day_status.items() if status == "usable"}
        if usable_days:
            usable_by_symbol[symbol] = usable_days
    return {
        "passed": bool(usable_by_symbol),
        "snapshot_valid": True,
        "status": "passed" if usable_by_symbol else "empty",
        "window_start": str(payload.get("window_start") or ""),
        "window_end": str(payload.get("window_end") or ""),
        "snapshot_path": _display_path(path),
        "source_snapshot": "window_audit",
        "generated_at": str(payload.get("generated_at") or ""),
        "usable_by_symbol": usable_by_symbol,
        "total_symbols": int(payload.get("total_symbols") or 0),
        "fully_usable_symbol_count": int(payload.get("fully_usable_symbol_count") or 0),
        "warnings": [str(item) for item in (payload.get("warnings") or []) if item],
    }


def window_pool_covers(pool: Mapping[str, Any] | None, symbol: str, trade_date: str) -> bool:
    """True only when (symbol, date) is explicitly usable inside the window."""

    if not isinstance(pool, Mapping) or pool.get("snapshot_valid") is not True:
        return False
    if not pool.get("passed"):
        return False
    normalized = normalize_symbol(symbol)
    day = str(trade_date or "")[:10]
    window_start = str(pool.get("window_start") or "")
    window_end = str(pool.get("window_end") or "")
    if not normalized or not day or not window_start or not window_end:
        return False
    if not (window_start <= day <= window_end):
        return False
    usable = pool.get("usable_by_symbol")
    if not isinstance(usable, Mapping):
        return False
    days = usable.get(normalized)
    return bool(days) and day in days


def resolve_minute_replay_evidence(
    symbol: str,
    trade_date: str,
    *,
    fill_model: str,
    window_pool: Mapping[str, Any] | None = None,
    symbol_pool: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Select windowed evidence when the snapshot can arbitrate this date.

    The legacy classifier is called directly for every fallback path and its
    returned dictionary is retained untouched under ``evidence``.  This keeps
    byte-level compatibility for consumers that still rely on the Top200
    symbol-only evidence contract while exposing the arbitration provenance in
    a separate outer envelope.
    """

    pool = window_pool if isinstance(window_pool, Mapping) else {}
    valid = bool(pool.get("snapshot_valid"))
    day = str(trade_date or "")[:10]
    window_start = str(pool.get("window_start") or "") if valid else ""
    window_end = str(pool.get("window_end") or "") if valid else ""
    in_window = bool(
        valid
        and str(fill_model or "") == "intraday_1m_close"
        and window_start
        and window_end
        and window_start <= day <= window_end
    )
    if in_window:
        evidence = classify_minute_replay_evidence_windowed(
            symbol,
            day,
            fill_model=fill_model,
            pool=pool,
        )
        decision_basis = "symbol_date"
    else:
        # Do not copy, patch, or normalize this result: fallback compatibility
        # is defined as the old classifier's dictionary byte-for-byte.
        evidence = classify_minute_replay_evidence(
            symbol,
            fill_model=fill_model,
            pool=symbol_pool,
        )
        decision_basis = "symbol_only_fallback"
    if decision_basis == "symbol_date":
        source_snapshot = evidence.get("pool_source_snapshot") or pool.get("source_snapshot") or ""
    else:
        source_snapshot = evidence.get("pool_source_snapshot") or ""
    return {
        "evidence": evidence,
        "pool_source_snapshot": source_snapshot,
        "window_start": window_start,
        "window_end": window_end,
        "decision_basis": decision_basis,
    }


def classify_minute_replay_evidence_windowed(
    symbol: str,
    trade_date: str,
    *,
    fill_model: str,
    pool: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Window-aware three-state minute-evidence classifier.

    Mirrors :func:`quantdesk.intraday.minute_pool_capability.classify_minute_replay_evidence`
    but matches on symbol+date coverage.  Outside-window or未通过 dates stay
    ``local_available_unverified_pool`` — never a silent upgrade.
    """

    normalized = normalize_symbol(symbol)
    model = str(fill_model or "")
    if model == "missing_intraday":
        status = "missing_intraday"
        can_use = False
    elif model == "intraday_1m_close" and window_pool_covers(pool, normalized, trade_date):
        status = "verified_completed_pool"
        can_use = True
    elif model == "intraday_1m_close":
        status = "local_available_unverified_pool"
        can_use = True
    else:
        status = "not_intraday_fill_model"
        can_use = False
    pool_map = pool if isinstance(pool, Mapping) else {}
    return {
        "status": status,
        "symbol": normalized,
        "trade_date": str(trade_date or "")[:10],
        "fill_model": model,
        "can_use_for_research_replay": bool(can_use),
        "verified_completed_pool": status == "verified_completed_pool",
        "pool_status": pool_map.get("status") or "missing",
        "pool_source_snapshot": pool_map.get("source_snapshot") or "window_audit",
        "pool_window_start": pool_map.get("window_start") or "",
        "pool_window_end": pool_map.get("window_end") or "",
        "pool_snapshot_path": pool_map.get("snapshot_path") or "",
        "pool_warnings": [str(item) for item in (pool_map.get("warnings") or [])],
        "research_only": True,
        "paper_only": True,
    }
