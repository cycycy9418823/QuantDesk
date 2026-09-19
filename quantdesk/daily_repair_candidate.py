"""Prepare an offline daily-CSV repair candidate without applying it.

Only independent, all-NUL physical records may be quarantined. Every other
original byte is retained. Supplied CSVs are evidence, never instructions to
fetch or write anything. A ready candidate still requires a separate, locked
compare-and-swap application by the canonical daily writer.
"""

from __future__ import annotations

import csv
import hashlib
import math
import re
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

import pandas as pd

from quantdesk.data_truth.daily_bar_validity import daily_bar_validity_from_row
from quantdesk.price_basis import classify_frame_price_basis

DAILY_COLUMNS = (
    "date", "symbol", "open", "high", "low", "close", "volume", "amount",
    "change", "pct_chg", "adjust", "source", "updated_at",
)
_CALENDAR_COLUMNS = ("date", "is_open", "source", "note")
_NUMERIC_COLUMNS = ("open", "high", "low", "close", "volume", "amount", "change", "pct_chg")


@dataclass(frozen=True)
class RepairCandidate:
    candidate_bytes: bytes | None
    manifest: dict[str, Any]


@dataclass(frozen=True)
class _Record:
    values: dict[str, str]
    raw: bytes
    offset: int


@dataclass(frozen=True)
class _Table:
    header: bytes
    records: tuple[_Record, ...]
    quarantined: tuple[dict[str, Any], ...]

    def retained_bytes(self) -> bytes:
        return self.header + b"".join(row.raw for row in self.records)


class _Rejected(ValueError):
    pass


def _sha256(content: bytes | None) -> str | None:
    return hashlib.sha256(content).hexdigest() if isinstance(content, bytes) else None


def _date(value: str, role: str) -> date:
    if not isinstance(value, str) or re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value) is None:
        raise _Rejected(f"{role}_invalid_date")
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise _Rejected(f"{role}_invalid_date") from exc


def _read_table(
    content: bytes,
    *,
    columns: tuple[str, ...],
    role: str,
    allow_nul_records: bool = False,
) -> _Table:
    if not content:
        raise _Rejected(f"{role}_empty")
    records: list[_Record] = []
    quarantined: list[dict[str, Any]] = []
    header = b""
    offset = 0
    for number, raw in enumerate(content.splitlines(keepends=True), start=1):
        body = raw.removesuffix(b"\r\n").removesuffix(b"\n").removesuffix(b"\r")
        if allow_nul_records and header and body and not body.strip(b"\x00"):
            quarantined.append({
                "line_number": number, "byte_offset": offset,
                "byte_length": len(raw), "nul_byte_count": len(body),
                "sha256": _sha256(raw),
            })
        else:
            if b"\x00" in raw:
                raise _Rejected(f"{role}_nul_in_record:{number}")
            try:
                text = raw.decode("utf-8-sig" if number == 1 else "utf-8")
                # One physical line must be a complete CSV record. In
                # particular, a NUL-only line inside an open quote is never
                # mistaken for an independent record that can be removed.
                parsed = list(csv.reader([text], strict=True))
            except (UnicodeError, csv.Error) as exc:
                raise _Rejected(f"{role}_csv_unreadable:{number}") from exc
            if len(parsed) != 1 or len(parsed[0]) != len(columns):
                raise _Rejected(f"{role}_column_mismatch:{number}")
            if not header:
                if tuple(parsed[0]) != columns:
                    raise _Rejected(f"{role}_header_mismatch")
                header = raw
            else:
                records.append(_Record(dict(zip(columns, parsed[0])), raw, offset))
        offset += len(raw)
    if not records:
        raise _Rejected(f"{role}_no_records")
    return _Table(header, tuple(records), tuple(quarantined))


def _finite_decimal(value: str, *, role: str, day: str, name: str) -> Decimal:
    # Retained history and overlapping evidence use the same numeric
    # contract. Downstream bar checks also require a finite float.
    try:
        exact = Decimal(value)
        numeric = float(value)
    except (InvalidOperation, ValueError, TypeError, OverflowError) as exc:
        raise _Rejected(f"{role}_nonfinite_or_invalid:{day}:{name}") from exc
    if not exact.is_finite() or not math.isfinite(numeric):
        raise _Rejected(f"{role}_nonfinite_or_invalid:{day}:{name}")
    return exact


def _validate_daily(table: _Table, *, symbol: str, role: str) -> None:
    # Classify ALL records before looking at dates, ordering, or duplicates.
    # No provider/source inference and no normalizing away unknown rows.
    frame = pd.DataFrame([row.values for row in table.records], columns=DAILY_COLUMNS)
    basis, reason = classify_frame_price_basis(frame)
    if basis != "raw" or reason:
        raise _Rejected(f"{role}_price_basis_rejected:{basis}:{reason}")
    dates: list[str] = []
    for record in table.records:
        row = record.values
        day = row["date"]
        _date(day, role)
        if row["symbol"] != symbol:
            raise _Rejected(f"{role}_symbol_mismatch:{day}")
        if not row["source"].strip():
            raise _Rejected(f"{role}_source_missing:{day}")
        for name in _NUMERIC_COLUMNS:
            value = row[name]
            # These three fields are optional metadata in existing daily
            # CSVs. Preserve empty cells; never manufacture zeros for them.
            if not value and name in {"amount", "change", "pct_chg"}:
                continue
            numeric = _finite_decimal(value, role=role, day=day, name=name)
            if name == "amount" and numeric < 0:
                raise _Rejected(f"{role}_negative_amount:{day}")
        valid, reason = daily_bar_validity_from_row(row)
        if not valid:
            raise _Rejected(f"{role}_invalid_bar:{day}:{reason}")
        dates.append(day)
    if len(set(dates)) != len(dates):
        raise _Rejected(f"{role}_duplicate_dates")
    if dates != sorted(dates):
        raise _Rejected(f"{role}_unordered_dates")


def _calendar_evidence(content: bytes | None, days: list[str]) -> dict[str, Any]:
    evidence: dict[str, Any] = {
        "status": "missing", "sha256": _sha256(content),
        "verified": False, "missing_dates": list(days),
        "unverified_dates": [], "expected_session_dates": [], "reasons": [],
    }
    if content is None:
        evidence["reasons"] = ["calendar_missing"]
        return evidence
    try:
        table = _read_table(content, columns=_CALENDAR_COLUMNS, role="calendar")
        by_date: dict[str, dict[str, str]] = {}
        for record in table.records:
            row = record.values
            day = row["date"]
            _date(day, "calendar")
            if day in by_date:
                raise _Rejected("calendar_duplicate_dates")
            by_date[day] = row
    except _Rejected as exc:
        evidence.update(status="unverified", reasons=[str(exc)])
        return evidence
    missing = [day for day in days if day not in by_date]
    unverified: list[str] = []
    sessions: list[str] = []
    for day in days:
        if day not in by_date:
            continue
        row = by_date[day]
        flag = row["is_open"].strip().lower()
        is_open = flag in {"true", "1"}
        # This is the existing persisted provider contract, not an inferred
        # weekday calendar. Provisional rows OUTSIDE the requested interval
        # do not lower the evidence quality of the requested interval.
        trusted = (
            flag in {"true", "false", "1", "0"}
            and row["source"] == "baostock.query_trade_dates"
            and row["note"] == ("trading_day" if is_open else "non_trading_day")
        )
        if not trusted:
            unverified.append(day)
        elif is_open:
            sessions.append(day)
    reasons = []
    if missing:
        reasons.append("calendar_coverage_incomplete")
    if unverified:
        reasons.append("calendar_rows_unverified")
    evidence.update(
        status="verified" if not reasons else "unverified", verified=not reasons,
        missing_dates=missing, unverified_dates=unverified,
        expected_session_dates=sessions, reasons=reasons,
    )
    return evidence


def _assert_no_conflicts(original: _Table, supplemental: _Table) -> None:
    existing = {record.values["date"]: record.values for record in original.records}
    for record in supplemental.records:
        row = record.values
        day = row["date"]
        if day not in existing:
            continue
        for name in _NUMERIC_COLUMNS:
            before, incoming = existing[day][name], row[name]
            if before and incoming:
                equal = (
                    _finite_decimal(before, role="original", day=day, name=name)
                    == _finite_decimal(incoming, role="supplemental", day=day, name=name)
                )
            else:
                equal = before == incoming
            if not equal:
                raise _Rejected(f"supplemental_history_conflict:{day}:{name}")


def _assemble(original: _Table, additions: list[_Record]) -> tuple[bytes, list[dict[str, Any]]]:
    output = bytearray(original.header)
    retained = [{
        "original_offset": 0, "candidate_offset": 0,
        "byte_length": len(original.header), "sha256": _sha256(original.header),
    }]
    tagged = [(record, True) for record in original.records]
    tagged.extend((record, False) for record in additions)
    # Original order was validated; merging new dates never reorders or
    # reserializes a retained original record.
    for record, is_original in sorted(tagged, key=lambda item: item[0].values["date"]):
        if output and not output.endswith((b"\n", b"\r")):
            output.extend(b"\n")
        offset = len(output)
        output.extend(record.raw)
        if is_original:
            retained.append({
                "original_offset": record.offset, "candidate_offset": offset,
                "byte_length": len(record.raw), "sha256": _sha256(record.raw),
            })
    candidate = bytes(output)
    preserved = b"".join(
        candidate[item["candidate_offset"]:item["candidate_offset"] + item["byte_length"]]
        for item in retained
    )
    if preserved != original.retained_bytes():
        raise _Rejected("original_history_preservation_failed")
    return candidate, retained


def prepare_daily_repair_candidate(
    *,
    symbol: str,
    original_bytes: bytes,
    expected_sha256: str,
    start_date: str,
    target_date: str,
    calendar_bytes: bytes | None = None,
    supplemental_bytes: bytes | None = None,
) -> RepairCandidate:
    """Return a candidate and evidence; never read, fetch, or apply anything.

    Daily inputs use the canonical 13-column schema. Calendar input uses
    ``date,is_open,source,note`` with persisted Baostock trade-date evidence.
    With insufficient evidence only the structural candidate is returned;
    supplemental records are inserted only when the entire requested interval
    can be verified. No state here means the original file was repaired.
    """
    manifest: dict[str, Any] = {
        "schema_version": "daily_repair_candidate.v1", "symbol": symbol,
        "start_date": start_date, "target_date": target_date,
        "status": "blocked", "ok": False, "reasons": [],
        "canonical_write": False, "network_effective": False, "applied": False,
        "repair_complete": False, "original_status": "not_assessed",
        "original_sha256": _sha256(original_bytes), "expected_sha256": expected_sha256,
        "original_sha256_matches_expected": False,
        "original_size_bytes": len(original_bytes) if isinstance(original_bytes, bytes) else None,
        "candidate_sha256": None, "candidate_size_bytes": None,
        "calendar_sha256": _sha256(calendar_bytes),
        "supplemental_sha256": _sha256(supplemental_bytes),
        "quarantined_records": [], "retained_original_segments": [],
        "history_preserved": False, "retained_history_sha256": None,
        "added_dates": [], "available_supplemental_dates": [], "missing_dates": None,
        "coverage_verified": False, "coverage_complete": False,
        "supplemental_evidence": "not_supplied",
    }
    try:
        if not isinstance(original_bytes, bytes) or any(
            value is not None and not isinstance(value, bytes)
            for value in (calendar_bytes, supplemental_bytes)
        ):
            raise _Rejected("input_bytes_required")
        manifest["original_status"] = "damaged" if b"\x00" in original_bytes else "not_assessed"
        if not isinstance(symbol, str) or re.fullmatch(r"[0-9]{6}", symbol) is None:
            raise _Rejected("invalid_symbol")
        if not isinstance(expected_sha256, str) or re.fullmatch(r"[0-9a-fA-F]{64}", expected_sha256) is None:
            raise _Rejected("invalid_expected_sha256")
        if expected_sha256.lower() != manifest["original_sha256"]:
            raise _Rejected("original_hash_mismatch")
        manifest["original_sha256_matches_expected"] = True
        first, last = _date(start_date, "start"), _date(target_date, "target")
        if first > last:
            raise _Rejected("reversed_date_range")
        original = _read_table(
            original_bytes, columns=DAILY_COLUMNS, role="original", allow_nul_records=True,
        )
        manifest["quarantined_records"] = list(original.quarantined)
        _validate_daily(original, symbol=symbol, role="original")
        if not original.quarantined:
            manifest["original_status"] = "structurally_valid"
            raise _Rejected("no_isolated_nul_records")
        days = [(first + timedelta(days=index)).isoformat() for index in range((last - first).days + 1)]
        calendar = _calendar_evidence(calendar_bytes, days)
        manifest["calendar"] = calendar
        manifest["coverage_verified"] = calendar["verified"]
        reasons = list(calendar["reasons"])
        supplemental = None
        if supplemental_bytes is None:
            reasons.append("supplemental_missing")
        else:
            supplemental = _read_table(supplemental_bytes, columns=DAILY_COLUMNS, role="supplemental")
            _validate_daily(supplemental, symbol=symbol, role="supplemental")
            _assert_no_conflicts(original, supplemental)
            manifest["supplemental_evidence"] = "caller_supplied_local_csv"
            manifest["supplemental_sources"] = sorted({row.values["source"] for row in supplemental.records})
        existing = {record.values["date"] for record in original.records}
        available = {
            record.values["date"]: record for record in supplemental.records
            if start_date <= record.values["date"] <= target_date
        } if supplemental is not None else {}
        manifest["available_supplemental_dates"] = sorted(available)
        additions: list[_Record] = []
        if calendar["verified"]:
            expected = set(calendar["expected_session_dates"])
            closed = set(days) - expected
            if (existing | set(available)) & closed:
                raise _Rejected("bar_on_verified_closed_session")
            missing = sorted(expected - existing)
            manifest["missing_dates"] = missing
            if not expected:
                reasons.append("requested_interval_has_no_open_sessions")
            if supplemental is not None and not available:
                reasons.append("supplemental_no_rows_in_requested_interval")
            if set(missing) - set(available):
                reasons.append("supplemental_missing_sessions")
            if not reasons:
                additions = [available[day] for day in missing]
        candidate, retained = _assemble(original, additions)
        checked = _read_table(candidate, columns=DAILY_COLUMNS, role="candidate")
        _validate_daily(checked, symbol=symbol, role="candidate")
        manifest.update(
            status="candidate_ready" if not reasons else "structural_candidate_only",
            ok=not reasons, reasons=reasons, candidate_sha256=_sha256(candidate),
            candidate_size_bytes=len(candidate), history_preserved=True,
            retained_history_sha256=_sha256(original.retained_bytes()),
            retained_original_segments=retained, candidate_price_basis="raw",
            coverage_complete=not reasons,
            added_dates=[record.values["date"] for record in additions],
            missing_dates=[] if not reasons else manifest["missing_dates"],
        )
        return RepairCandidate(candidate, manifest)
    except _Rejected as exc:
        manifest.update(
            status="blocked", ok=False, reasons=[str(exc)],
            coverage_complete=False, added_dates=[],
            candidate_sha256=None, candidate_size_bytes=None,
        )
        return RepairCandidate(None, manifest)


__all__ = ["DAILY_COLUMNS", "RepairCandidate", "prepare_daily_repair_candidate"]
