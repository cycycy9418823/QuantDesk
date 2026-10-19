"""Opt-in next-session opening-price evidence for the strategy paper book.

The default remains ``same_day_close``. This module never fetches quotes or
writes a ledger. An archived 09:30 row must explicitly declare raw prices,
its actual provider date/time and an aware fetch time. A filename, snapshot
date or provider name cannot substitute for any missing evidence.

The bounded observation window is [09:30, 09:35) Asia/Shanghai. The observed
``open`` is a paper fill proxy, not proof that an order could obtain liquidity
at that price. Existing board/date limit rules and costs remain authoritative.
"""

from __future__ import annotations

import copy
import csv
import hashlib
import io
import json
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any

import pandas as pd

from quantdesk.board_rules import limit_fill_check
from quantdesk.price_basis import validate_price_frame_set
from quantdesk.shared import normalize_symbol, utc_now_iso
from quantdesk.trading_calendar import load_a_share_trading_calendar

FILL_MODE_SAME_DAY_CLOSE = "same_day_close"
FILL_MODE_NEXT_OPEN_SNAPSHOT = "next_open_snapshot"
FILL_MODES = (FILL_MODE_SAME_DAY_CLOSE, FILL_MODE_NEXT_OPEN_SNAPSHOT)
OPEN_SNAPSHOT_SLOT = "0930"
FILL_JOURNAL_SCHEMA = "auto_paper_fills.v2"
SHANGHAI = timezone(timedelta(hours=8))


def resolve_strategy_fill_mode(explicit: str | None = None) -> tuple[str, list[str]]:
    """Resolve explicit/config mode; never silently ignore a misspelled mode."""

    warnings: list[str] = []
    value = explicit
    if value is None:
        try:
            from quantdesk.config import load_config

            value = (load_config().get("paper") or {}).get("strategy_fill_mode")
        except Exception as exc:  # bad config cannot opt into a new fill policy
            warnings.append(f"strategy_fill_mode_config_unreadable:{type(exc).__name__}")
    selected = str(value or "").strip() or FILL_MODE_SAME_DAY_CLOSE
    if selected not in FILL_MODES:
        raise ValueError(f"unknown paper.strategy_fill_mode: {selected!r}")
    return selected, warnings


def aware_timestamp(value: Any) -> datetime:
    """Parse an actual timezone-aware timestamp; never supply a missing zone."""

    parsed = value if isinstance(value, datetime) else datetime.fromisoformat(str(value))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("timezone_aware_timestamp_required")
    return parsed.astimezone(SHANGHAI)


def next_trading_day(day: str, calendar: dict[str, Any] | None = None) -> str:
    """Find the next session only through explicit, contiguous calendar rows."""

    current = date.fromisoformat(day)
    cal = calendar if calendar is not None else load_a_share_trading_calendar()
    lookup = cal.get("is_open_by_date") or {}
    if lookup.get(current.isoformat()) is not True:
        raise ValueError("signal_session_unverified")
    for _ in range(32):
        current += timedelta(days=1)
        flag = lookup.get(current.isoformat())
        if flag is True:
            return current.isoformat()
        if flag is not False:
            raise ValueError(f"next_session_unverified:{current.isoformat()}")
    raise ValueError("next_session_unverified:calendar_gap")


def _number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(str(value).strip())
    except (TypeError, ValueError):
        return None
    return number if number == number and abs(number) != float("inf") else None


def _row_frame(rows: list[dict[str, Any]]) -> pd.DataFrame:
    # This is an explicit schema projection, never a provider-based inference.
    projected = []
    for row in rows:
        explicit = str(row.get("price_basis") or "").strip()
        adjust = str(row.get("adjust") or "").strip()
        projected.append({**row, "adjust": explicit or adjust or "unknown"})
        if explicit and adjust and explicit != adjust:
            projected.append({**row, "adjust": adjust})
    return pd.DataFrame(projected)


def load_open_snapshot(snapshot_root: Path, fill_date: str) -> dict[str, Any]:
    """Read one immutable byte sample, retaining duplicates and source hashes."""

    fill_date = date.fromisoformat(fill_date).isoformat()
    path = Path(snapshot_root) / fill_date / f"{OPEN_SNAPSHOT_SLOT}.csv"
    result: dict[str, Any] = {"path": str(path), "sha256": "", "rows": {},
                              "duplicate_symbols": [], "status": "missing", "reason": "snapshot_missing"}
    if not path.is_file():
        return result
    try:
        raw = path.read_bytes()
        result["sha256"] = hashlib.sha256(raw).hexdigest()
        reader = csv.DictReader(io.StringIO(raw.decode("utf-8-sig")))
        if not reader.fieldnames or len(set(reader.fieldnames)) != len(reader.fieldnames):
            raise ValueError("ambiguous_csv_header")
        grouped: dict[str, list[dict[str, Any]]] = {}
        for row in reader:
            if None in row:
                raise ValueError("malformed_csv_row")
            code = normalize_symbol(row.get("symbol"))
            if code:
                grouped.setdefault(code, []).append(dict(row))
        result.update(rows={key: values[0] for key, values in grouped.items()},
                      grouped_rows=grouped,
                      duplicate_symbols=sorted(key for key, values in grouped.items() if len(values) != 1),
                      status="ok", reason="")
    except (OSError, UnicodeError, ValueError, csv.Error) as exc:
        result.update(status="unreadable", reason=f"snapshot_unreadable:{type(exc).__name__}")
    return result


def snapshot_price_gate(snapshot: dict[str, Any], symbols: list[str]) -> dict[str, Any]:
    """Validate the complete requested set before evaluating any opening fill."""

    grouped = snapshot.get("grouped_rows") or {}
    rows = snapshot.get("rows") or {}
    frames = {code: _row_frame(grouped.get(code) or ([rows[code]] if code in rows else []))
              for code in dict.fromkeys(normalize_symbol(item) for item in symbols)}
    return validate_price_frame_set(frames, consumer="paper_execution")


def _evidence(snapshot: dict[str, Any], row: dict[str, Any] | None) -> dict[str, Any]:
    original = copy.deepcopy(row or {})
    return {
        "snapshot_path": str(snapshot.get("path") or ""),
        "snapshot_sha256": str(snapshot.get("sha256") or ""),
        "snapshot_slot": str(original.get("snapshot_time") or ""),
        "snapshot_date": str(original.get("snapshot_date") or ""),
        "quote_date": str(original.get("quote_date") or ""),
        "quote_time": str(original.get("quote_time") or ""),
        "source": str(original.get("source") or ""),
        "fetched_at": str(original.get("fetched_at") or ""),
        "price_basis": str(original.get("price_basis") or original.get("adjust") or ""),
        "open": _number(original.get("open")),
        "pre_close": _number(original.get("pre_close")),
        "volume": _number(original.get("volume")),
        "source_row": original,
        "source_row_sha256": hashlib.sha256(json.dumps(original, ensure_ascii=False, sort_keys=True,
                                                       separators=(",", ":")).encode("utf-8")).hexdigest(),
    }


def evaluate_open_fill(
    order: dict[str, Any], snapshot: dict[str, Any], fill_date: str, *,
    calendar: dict[str, Any] | None = None, observed_at: str | datetime | None = None,
) -> dict[str, Any]:
    """Check source chronology and limits without changing cash or positions."""

    side = str(order.get("side") or "")
    code = normalize_symbol(order.get("symbol"))
    row = (snapshot.get("rows") or {}).get(code)
    evidence = _evidence(snapshot, row)

    def reject(reason: str, limits: dict[str, Any] | None = None) -> dict[str, Any]:
        return {"ok": False, "status": "rejected", "reason_code": reason,
                "reason": f"开盘纸面成交证据不满足：{reason}；本单不成交、不顺延。",
                "price": None, "evidence": evidence, "limits": limits or {}}

    if side not in {"buy", "sell"} or not code:
        return reject("invalid_order_identity")
    try:
        execution_date = date.fromisoformat(fill_date)
        signal_date = date.fromisoformat(str(order.get("signal_date") or ""))
        if str(order.get("fill_date") or "") != fill_date or next_trading_day(signal_date.isoformat(), calendar) != fill_date:
            return reject("not_next_trading_session")
        created = aware_timestamp(order.get("created_at"))
        opening = datetime.combine(execution_date, time(9, 30), SHANGHAI)
        signal_close = datetime.combine(signal_date, time(15), SHANGHAI)
        if not signal_close <= created < opening:
            return reject("decision_not_recorded_before_open")
    except (TypeError, ValueError):
        return reject("order_chronology_unverified")
    if snapshot.get("status") != "ok":
        return reject(str(snapshot.get("reason") or "snapshot_missing"))
    if code in (snapshot.get("duplicate_symbols") or []):
        return reject("duplicate_snapshot_symbol")
    if row is None:
        return reject("symbol_missing_in_snapshot")
    if not snapshot.get("sha256") or not snapshot.get("path"):
        return reject("snapshot_provenance_missing")
    if str(row.get("snapshot_date") or "") != fill_date or str(row.get("snapshot_time") or "") != OPEN_SNAPSHOT_SLOT:
        return reject("snapshot_slot_or_date_mismatch")
    basis = snapshot_price_gate(snapshot, [code])
    if not basis["eligible"]:
        return reject("snapshot_price_basis_unverified")
    if str(row.get("quote_date") or "") != fill_date:
        return reject("quote_date_missing_or_stale")
    if not str(row.get("source") or "").strip():
        return reject("quote_source_missing")
    try:
        clock = time.fromisoformat(str(row.get("quote_time") or ""))
        if clock.tzinfo is not None:
            return reject("quote_time_format_invalid")
        quoted = datetime.combine(execution_date, clock, SHANGHAI)
        fetched = aware_timestamp(row.get("fetched_at"))
        observed = aware_timestamp(observed_at or utc_now_iso())
        end = opening + timedelta(minutes=5)
        if not opening <= quoted <= fetched < end or observed < fetched:
            return reject("quote_or_fetch_outside_open_window")
    except (TypeError, ValueError):
        return reject("quote_timestamp_unverified")
    volume, open_px, pre_close = (_number(row.get(key)) for key in ("volume", "open", "pre_close"))
    if volume is None or volume <= 0:
        return reject("suspended_or_no_volume")
    if str(row.get("suspended") or row.get("is_suspended") or "").lower() in {"1", "true", "yes"}:
        return reject("suspended_or_no_volume")
    if open_px is None or open_px <= 0:
        return reject("open_price_invalid")
    if pre_close is None or pre_close <= 0:
        return reject("limit_unverifiable")
    reference = _number(order.get("reference_price"))
    if reference is None or reference <= 0 or abs(pre_close - reference) > 0.011:
        return reject("opening_reference_changed_corporate_action_or_source_unresolved")
    check = limit_fill_check(code, side, price=open_px, trade_date=fill_date, pre_close=pre_close,
                             name=row.get("name") or order.get("name"))
    if not check["ok"]:
        return reject(str(check["code"]), dict(check.get("limits") or {}))
    return {"ok": True, "status": "filled", "reason_code": "", "reason": "",
            "price": open_px, "evidence": evidence, "limits": dict(check.get("limits") or {}),
            "fill_quality": "raw_open_paper_proxy", "liquidity_verified": False}


def fill_journal_row(
    order: dict[str, Any], *, status: str, decision_date: str, reason_code: str = "",
    reason: str = "", price: float | None = None, quantity: int = 0,
    evidence: dict[str, Any] | None = None, limits: dict[str, Any] | None = None,
    amounts: dict[str, Any] | None = None, recorded_at: str | None = None,
) -> dict[str, Any]:
    """A nested event; durability and state reconstruction live in the journal."""

    return {
        "schema": FILL_JOURNAL_SCHEMA, "event_type": "order_outcome",
        "order_id": str(order.get("order_id") or ""), "order": copy.deepcopy(order),
        "recorded_at": recorded_at or utc_now_iso(), "ledger": "auto_paper_portfolio",
        "fill_mode": FILL_MODE_NEXT_OPEN_SNAPSHOT, "status": status,
        "side": str(order.get("side") or ""), "symbol": normalize_symbol(order.get("symbol")),
        "strategy_type": str(order.get("strategy_type") or "long"),
        "signal_date": str(order.get("signal_date") or ""),
        "fill_date": str(order.get("fill_date") or ""), "decision_date": decision_date,
        "reason_code": reason_code, "reason": reason, "quantity": int(quantity),
        "fill_price": price, "evidence": evidence or {}, "limits": limits or {},
        "amounts": amounts or {}, "paper_only": True, "is_real_order": False,
        "fill_quality": "raw_open_paper_proxy" if status == "filled" else "no_fill",
        "liquidity_verified": False,
    }


def make_pending_order(
    side: str, row: dict[str, Any], *, signal_date: str, fill_date: str, quantity: int,
    reason: str, reference_price: float | None, candidate: dict[str, Any] | None = None,
    recorded_at: str | None = None,
) -> dict[str, Any]:
    code = normalize_symbol(row.get("symbol"))
    strategy_type = str(row.get("strategy_type") or "long")
    stamp = recorded_at or utc_now_iso()
    aware_timestamp(stamp)
    if side not in {"buy", "sell"} or not code or isinstance(quantity, bool) or quantity <= 0 or int(quantity) != quantity:
        raise ValueError("invalid_pending_order")
    order = {
        "order_id": f"{side}:{code}:{strategy_type}:{signal_date}", "side": side,
        "symbol": code, "name": str(row.get("name") or code), "strategy_type": strategy_type,
        "signal_date": signal_date, "fill_date": fill_date, "quantity": int(quantity),
        "reason": reason, "reference_price": reference_price, "created_at": stamp,
        "fill_mode": FILL_MODE_NEXT_OPEN_SNAPSHOT, "status": "pending",
        "signal_price_basis": "raw", "paper_only": True, "is_real_order": False,
    }
    if candidate is not None:
        order["candidate"] = copy.deepcopy(candidate)
    return order
