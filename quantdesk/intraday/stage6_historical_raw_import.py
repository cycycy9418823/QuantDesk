"""Verdict-bound historical raw-minute migration through the Stage 6 lock.

This path exists for legacy Tushare files whose canonical CSV predates the
``price_basis`` column.  It is deliberately per-symbol: every apply has its own
plan binding, recovery backup, atomic replace, post-write audit, and rollback.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import os
import tempfile
from datetime import datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_EVEN
from pathlib import Path
from typing import Any, Mapping

from quantdesk.interprocess_lock import interprocess_file_lock
from quantdesk.intraday.canonical_write_guard import (
    atomic_restore_from_backup,
    ensure_path_outside_canonical,
    sha256_path,
)
from quantdesk.intraday.stage6_append_only_import import (
    CANONICAL_WRITE_LOCK_TIMEOUT_SECONDS,
    COMPARE_QUANTUM,
    canonical_intraday_write_lock_path,
)
from quantdesk.shared import normalize_symbol, now_text
from quantdesk.utils import atomic_write_json


SCHEMA_VERSION = "stage6_proven_historical_raw_import_v1"
PROVENANCE_SCHEMA_VERSION = "round17_tushare_raw_import_provenance_v1"
SOURCE_FAMILY = "tushare_proxy_stk_mins"
OHLC_TOLERANCE = Decimal("0.00000001")
BASE_COLUMNS = (
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
)
STAGING_REQUIRED_COLUMNS = frozenset((*BASE_COLUMNS, "price_basis", "price_only_intraday"))
COMPARE_COLUMNS = ("open", "high", "low", "close", "volume", "amount")


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sha256(path: Path) -> str:
    return sha256_path(path) if path.is_file() else ""


def _canonical_digest(value: Any) -> str:
    return _sha256_bytes(
        json.dumps(
            value,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    )


def _decimal(value: Any, *, quantum: Decimal | None = None) -> Decimal | None:
    try:
        number = Decimal(str(value).strip())
    except (InvalidOperation, ValueError):
        return None
    if not number.is_finite():
        return None
    if quantum is not None:
        number = number.quantize(quantum, rounding=ROUND_HALF_EVEN)
    return number


def _read_json(path: Path) -> Mapping[str, Any] | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, Mapping) else None


def _resolve_record_path(value: Any) -> Path:
    return Path(str(value or "")).resolve()


def _verdict_record(payload: Mapping[str, Any], symbol: str) -> Mapping[str, Any] | None:
    rows = payload.get("verdicts")
    if not isinstance(rows, list):
        return None
    matches = [
        row
        for row in rows
        if isinstance(row, Mapping) and normalize_symbol(row.get("symbol")) == symbol
    ]
    return matches[0] if len(matches) == 1 else None


def _validate_verdict_binding(
    provenance_manifest_path: Path,
    symbol: str,
) -> tuple[dict[str, Any], list[str]]:
    reasons: list[str] = []
    manifest = _read_json(provenance_manifest_path)
    if manifest is None:
        return {}, ["provenance_manifest_invalid"]
    if manifest.get("schema_version") != PROVENANCE_SCHEMA_VERSION:
        reasons.append("provenance_schema_mismatch")
    symbols = manifest.get("symbols")
    record = symbols.get(symbol) if isinstance(symbols, Mapping) else None
    if not isinstance(record, Mapping):
        return {}, [*reasons, "symbol_provenance_missing"]
    if normalize_symbol(record.get("symbol")) != symbol:
        reasons.append("provenance_symbol_mismatch")
    if str(record.get("verdict") or "") != "raw_proven":
        reasons.append("verdict_not_raw_proven")

    verdict_path = _resolve_record_path(record.get("verdict_path"))
    verdict_payload = _read_json(verdict_path)
    actual_verdict_sha = _sha256(verdict_path)
    if not verdict_payload or actual_verdict_sha != str(record.get("verdict_sha256") or ""):
        reasons.append("verdict_file_sha256_mismatch")
        verdict = None
    else:
        verdict = _verdict_record(verdict_payload, symbol)
    if verdict is None:
        reasons.append("verdict_record_missing_or_ambiguous")
        events: list[Mapping[str, Any]] = []
    else:
        if _canonical_digest(dict(verdict)) != str(record.get("verdict_record_sha256") or ""):
            reasons.append("verdict_record_sha256_mismatch")
        if str(verdict.get("verdict") or "") != "raw_proven":
            reasons.append("verdict_record_not_raw_proven")
        raw_events = verdict.get("events")
        events = [item for item in raw_events or [] if isinstance(item, Mapping)] if isinstance(raw_events, list) else []
        if not events or any(str(item.get("classification") or "") != "raw_match" for item in events):
            reasons.append("verdict_raw_event_evidence_invalid")
    if int(record.get("event_count") or 0) != len(events):
        reasons.append("provenance_event_count_mismatch")
    if int(record.get("raw_match_count") or 0) != len(events):
        reasons.append("provenance_raw_match_count_mismatch")

    paths = {
        name: _resolve_record_path(record.get(name))
        for name in ("original_source_path", "backup_copy_path", "staging_path")
    }
    expected_hashes = {
        "original_source_path": str(record.get("original_source_sha256") or ""),
        "backup_copy_path": str(record.get("backup_copy_sha256") or ""),
        "staging_path": str(record.get("staging_sha256") or ""),
    }
    actual_hashes = {name: _sha256(path) for name, path in paths.items()}
    for name in paths:
        if not actual_hashes[name] or actual_hashes[name] != expected_hashes[name]:
            reasons.append(name.replace("_path", "") + "_sha256_mismatch")
    if actual_hashes["original_source_path"] != actual_hashes["backup_copy_path"]:
        reasons.append("source_backup_copy_mismatch")

    return {
        "symbol": symbol,
        "verdict": str(record.get("verdict") or ""),
        "verdict_path": str(verdict_path),
        "verdict_sha256": actual_verdict_sha,
        "verdict_record_sha256": str(record.get("verdict_record_sha256") or ""),
        "event_count": len(events),
        "raw_match_count": sum(str(item.get("classification") or "") == "raw_match" for item in events),
        "events": [dict(item) for item in events],
        "original_source_path": str(paths["original_source_path"]),
        "original_source_sha256": actual_hashes["original_source_path"],
        "backup_copy_path": str(paths["backup_copy_path"]),
        "backup_copy_sha256": actual_hashes["backup_copy_path"],
        "staging_path": str(paths["staging_path"]),
        "staging_sha256": actual_hashes["staging_path"],
    }, list(dict.fromkeys(reasons))


def _read_csv_rows(path: Path) -> tuple[list[str], list[dict[str, str]], str]:
    if not path.is_file():
        return [], [], ""
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle)
            header = list(reader.fieldnames or [])
            rows = [
                {str(key): str(value or "") for key, value in raw.items() if key is not None}
                for raw in reader
            ]
    except (OSError, UnicodeError, csv.Error):
        return [], [], "csv_unreadable"
    return header, rows, ""


def _audit_staging(
    path: Path,
    symbol: str,
    *,
    require_staging_provenance: bool = True,
    require_tushare_source_family: bool = True,
) -> tuple[dict[str, Any], list[dict[str, str]]]:
    header, rows, read_error = _read_csv_rows(path)
    reasons: list[str] = [read_error] if read_error else []
    required = (
        STAGING_REQUIRED_COLUMNS
        if require_staging_provenance
        else frozenset((*BASE_COLUMNS, "price_basis"))
    )
    missing = sorted(required - set(header))
    if missing:
        reasons.append("staging_required_columns_missing")
    datetimes: set[str] = set()
    sources: set[str] = set()
    negative_amount_count = 0
    for row in rows:
        if normalize_symbol(row.get("symbol")) != symbol:
            reasons.append("staging_symbol_mismatch")
        dt_text = str(row.get("datetime") or "").strip()
        try:
            parsed = datetime.fromisoformat(dt_text)
        except ValueError:
            parsed = None
            reasons.append("staging_datetime_invalid")
        if dt_text in datetimes:
            reasons.append("staging_duplicate_datetime")
        datetimes.add(dt_text)
        if parsed is not None:
            if str(row.get("date") or "")[:10] != parsed.date().isoformat():
                reasons.append("staging_date_datetime_mismatch")
            if str(row.get("time") or "")[:8] != parsed.time().strftime("%H:%M:%S"):
                reasons.append("staging_time_datetime_mismatch")
        if str(row.get("price_basis") or "").strip().lower() != "raw":
            reasons.append("staging_price_basis_not_raw")
        if require_staging_provenance and str(row.get("price_only_intraday") or "").strip().lower() not in {"0", "false", "no", "n", "off"}:
            reasons.append("staging_price_only_provenance_not_false")
        source = str(row.get("source") or "").strip()
        if source:
            sources.add(source)
        values = {name: _decimal(row.get(name)) for name in (*COMPARE_COLUMNS,)}
        if any(values[name] is None for name in ("open", "high", "low", "close", "volume")):
            reasons.append("staging_ohlcv_nonfinite")
            continue
        if min(values[name] for name in ("open", "high", "low", "close")) <= 0:
            reasons.append("staging_nonpositive_price")
        if values["volume"] < 0:
            reasons.append("staging_negative_volume")
        if (
            values["high"] + OHLC_TOLERANCE < max(values[name] for name in ("open", "low", "close"))
            or values["low"] - OHLC_TOLERANCE > min(values[name] for name in ("open", "high", "close"))
        ):
            reasons.append("staging_invalid_ohlc_envelope")
        if values["amount"] is not None and values["amount"] < 0:
            negative_amount_count += 1
    if not rows:
        reasons.append("staging_rows_missing")
    if require_tushare_source_family and sources != {SOURCE_FAMILY}:
        reasons.append("staging_source_family_mismatch")
    if not sources:
        reasons.append("source_values_missing")
    unique_reasons = list(dict.fromkeys(item for item in reasons if item))
    return {
        "status": "passed" if not unique_reasons else "blocked",
        "row_count": len(rows),
        "unique_datetime_count": len(datetimes),
        "source_values": sorted(sources),
        "negative_amount_count": negative_amount_count,
        "reason_codes": unique_reasons,
    }, rows


def _same_market_values(left: Mapping[str, Any], right: Mapping[str, Any]) -> bool:
    """True when every compared column agrees within its quantum.

    Compare the Decimal difference rather than quantizing each side and matching
    the rounded text: independent rounding puts values that straddle a bucket
    edge into different buckets, so a 4E-9 drift was reported as a conflict
    against a 1E-6 tolerance. An unparsable or non-finite figure on either side
    is never proof of agreement.
    """

    for column in COMPARE_COLUMNS:
        left_number = _decimal(left.get(column))
        right_number = _decimal(right.get(column))
        if left_number is None or right_number is None:
            return False
        if abs(left_number - right_number) > COMPARE_QUANTUM[column]:
            return False
    return True


def _append_field(line: str, value: str) -> str:
    if line.endswith("\r\n"):
        return line[:-2] + "," + value + "\r\n"
    if line.endswith(("\n", "\r")):
        return line[:-1] + "," + value + line[-1]
    return line + "," + value


def _candidate_bytes(
    canonical_path: Path,
    canonical_header: list[str],
    canonical_rows: list[dict[str, str]],
    staging_rows: list[dict[str, str]],
    missing_datetimes: list[str],
) -> bytes:
    missing_set = set(missing_datetimes)
    if canonical_path.is_file():
        before = canonical_path.read_bytes()
        has_bom = before.startswith(b"\xef\xbb\xbf")
        text = before.decode("utf-8-sig")
        lines = text.splitlines(keepends=True)
        target_header = list(canonical_header)
        if "price_basis" not in target_header:
            target_header.append("price_basis")
            lines = [
                _append_field(line, "price_basis" if index == 0 else "raw")
                for index, line in enumerate(lines)
            ]
    else:
        has_bom = True
        target_header = [*BASE_COLUMNS, "price_basis"]
        output = io.StringIO(newline="")
        writer = csv.DictWriter(output, fieldnames=target_header, lineterminator="\n")
        writer.writeheader()
        lines = [output.getvalue()]

    if missing_set:
        output = io.StringIO(newline="")
        writer = csv.DictWriter(
            output,
            fieldnames=target_header,
            extrasaction="ignore",
            lineterminator="\n",
        )
        for row in staging_rows:
            if str(row.get("datetime") or "") not in missing_set:
                continue
            projected = {name: row.get(name, "") for name in target_header}
            projected["price_basis"] = "raw"
            writer.writerow(projected)
        if lines and lines[-1] and not lines[-1].endswith(("\n", "\r")):
            lines[-1] += "\n"
        lines.append(output.getvalue())
    encoding = "utf-8-sig" if has_bom else "utf-8"
    return "".join(lines).encode(encoding)


def _write_payload(path: Path | None, payload: dict[str, Any]) -> dict[str, Any]:
    if path is not None:
        atomic_write_json(path, payload)
        payload["output_path"] = str(path)
    return payload


def _build_plan(
    *,
    staging_dir: Path,
    canonical_dir: Path,
    provenance_manifest_path: Path,
    symbol: str,
) -> tuple[dict[str, Any], bytes | None]:
    normalized = normalize_symbol(symbol)
    staging_path = Path(staging_dir) / f"{normalized}.csv"
    canonical_path = Path(canonical_dir) / f"{normalized}.csv"
    binding, binding_reasons = _validate_verdict_binding(
        Path(provenance_manifest_path), normalized
    )
    reasons = list(binding_reasons)
    if binding and Path(binding.get("staging_path") or "").resolve() != staging_path.resolve():
        reasons.append("staging_path_binding_mismatch")
    quality, staging_rows = _audit_staging(staging_path, normalized)
    reasons.extend(quality["reason_codes"])

    canonical_header, canonical_rows, canonical_read_error = _read_csv_rows(canonical_path)
    if canonical_read_error:
        reasons.append("canonical_csv_unreadable")
    if canonical_path.is_file() and not set(BASE_COLUMNS) <= set(canonical_header):
        reasons.append("canonical_required_columns_missing")
    if "price_basis" in canonical_header and any(
        str(row.get("price_basis") or "").strip().lower() != "raw"
        for row in canonical_rows
    ):
        reasons.append("canonical_existing_price_basis_not_raw")
    canonical_by_time: dict[str, dict[str, str]] = {}
    for row in canonical_rows:
        dt_text = str(row.get("datetime") or "").strip()
        if normalize_symbol(row.get("symbol")) != normalized:
            reasons.append("canonical_symbol_mismatch")
        if not dt_text:
            reasons.append("canonical_datetime_invalid")
        elif dt_text in canonical_by_time:
            reasons.append("canonical_duplicate_datetime")
        canonical_by_time[dt_text] = row
    staging_by_time = {str(row.get("datetime") or "").strip(): row for row in staging_rows}
    conflicts = sorted(
        timestamp
        for timestamp in set(canonical_by_time) & set(staging_by_time)
        if not _same_market_values(canonical_by_time[timestamp], staging_by_time[timestamp])
    )
    if conflicts:
        reasons.append("canonical_staging_timestamp_conflict")
    missing_datetimes = sorted(set(staging_by_time) - set(canonical_by_time))
    unique_reasons = list(dict.fromkeys(item for item in reasons if item))
    candidate = None
    if not unique_reasons:
        candidate = _candidate_bytes(
            canonical_path,
            canonical_header,
            canonical_rows,
            staging_rows,
            missing_datetimes,
        )
    before_sha = _sha256(canonical_path)
    after_sha = _sha256_bytes(candidate) if candidate is not None else ""
    plan_binding = {
        "schema_version": SCHEMA_VERSION,
        "symbol": normalized,
        "staging_sha256": binding.get("staging_sha256") if binding else "",
        "canonical_sha256_before": before_sha,
        "proposed_canonical_sha256_after": after_sha,
        "verdict_record_sha256": binding.get("verdict_record_sha256") if binding else "",
        "canonical_rows_before": len(canonical_rows),
        "canonical_rows_after": len(canonical_rows) + len(missing_datetimes),
    }
    payload: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "task_type": "stage6_proven_historical_raw_import",
        "generated_at": now_text(),
        "symbol": normalized,
        "status": "ready" if not unique_reasons else "blocked",
        "quality_status": quality["status"],
        "reason_codes": unique_reasons,
        "staging_path": str(staging_path),
        "staging_sha256": binding.get("staging_sha256") if binding else _sha256(staging_path),
        "canonical_path": str(canonical_path),
        "canonical_sha256_before": before_sha,
        "proposed_canonical_sha256_after": after_sha,
        "canonical_rows_before": len(canonical_rows),
        "staging_rows": len(staging_rows),
        "missing_staging_rows": len(missing_datetimes),
        "canonical_rows_after": len(canonical_rows) + len(missing_datetimes),
        "conflicting_timestamp_count": len(conflicts),
        "conflicting_timestamp_samples": conflicts[:10],
        "staging_quality": quality,
        "verdict_binding": binding,
        "plan_digest": _canonical_digest(plan_binding) if not unique_reasons else "",
        "applied": False,
        "canonical_write": False,
        "research_only": True,
        "paper_only": True,
        "no_broker": True,
    }
    return payload, candidate


def build_proven_historical_intraday_import_plan(
    *,
    staging_dir: Path,
    canonical_dir: Path,
    provenance_manifest_path: Path,
    symbol: str,
    output_path: Path | None = None,
) -> dict[str, Any]:
    canonical = Path(canonical_dir)
    ensure_path_outside_canonical(Path(staging_dir), canonical, role="historical_raw_staging")
    ensure_path_outside_canonical(Path(provenance_manifest_path), canonical, role="historical_raw_provenance")
    ensure_path_outside_canonical(output_path, canonical, role="historical_raw_plan_output")
    payload, _candidate = _build_plan(
        staging_dir=Path(staging_dir),
        canonical_dir=canonical,
        provenance_manifest_path=Path(provenance_manifest_path),
        symbol=symbol,
    )
    return _write_payload(output_path, payload)


def _atomic_replace_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = tempfile.NamedTemporaryFile(
        mode="wb",
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".round17.tmp",
        delete=False,
    )
    temporary = Path(handle.name)
    try:
        with handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _replace_canonical_bytes(path: Path, data: bytes) -> None:
    _atomic_replace_bytes(path, data)


def apply_proven_historical_intraday_import(
    *,
    staging_dir: Path,
    canonical_dir: Path,
    provenance_manifest_path: Path,
    symbol: str,
    expected_plan_digest: str,
    recovery_backup_dir: Path,
    output_path: Path | None = None,
) -> dict[str, Any]:
    canonical = Path(canonical_dir)
    backup_dir = Path(recovery_backup_dir)
    ensure_path_outside_canonical(Path(staging_dir), canonical, role="historical_raw_staging")
    ensure_path_outside_canonical(Path(provenance_manifest_path), canonical, role="historical_raw_provenance")
    ensure_path_outside_canonical(backup_dir, canonical, role="historical_raw_backup")
    ensure_path_outside_canonical(output_path, canonical, role="historical_raw_apply_output")
    normalized = normalize_symbol(symbol)
    target = canonical / f"{normalized}.csv"
    canonical.mkdir(parents=True, exist_ok=True)
    with interprocess_file_lock(
        canonical_intraday_write_lock_path(canonical),
        timeout_seconds=CANONICAL_WRITE_LOCK_TIMEOUT_SECONDS,
    ):
        plan, candidate = _build_plan(
            staging_dir=Path(staging_dir),
            canonical_dir=canonical,
            provenance_manifest_path=Path(provenance_manifest_path),
            symbol=normalized,
        )
        if plan["status"] != "ready" or not candidate:
            return _write_payload(output_path, plan)
        if str(expected_plan_digest) != str(plan.get("plan_digest") or ""):
            plan.update(
                {
                    "status": "blocked",
                    "reason_codes": ["expected_plan_digest_mismatch"],
                }
            )
            return _write_payload(output_path, plan)

        before_exists = target.is_file()
        before_sha = _sha256(target)
        backup = backup_dir / f"{normalized}.csv"
        try:
            if before_exists:
                backup.parent.mkdir(parents=True, exist_ok=True)
                if backup.exists() and _sha256(backup) != before_sha:
                    raise ValueError("recovery_backup_conflict")
                if not backup.exists():
                    backup.write_bytes(target.read_bytes())
                if _sha256(backup) != before_sha:
                    raise ValueError("recovery_backup_sha256_mismatch")
            _replace_canonical_bytes(target, candidate)
            after_sha = _sha256(target)
            if after_sha != plan["proposed_canonical_sha256_after"]:
                raise ValueError("post_apply_sha256_mismatch")
            post_quality, post_rows = _audit_staging(
                target,
                normalized,
                require_staging_provenance=False,
                require_tushare_source_family=False,
            )
            if "price_basis" not in (_read_csv_rows(target)[0]) or post_quality["reason_codes"]:
                raise ValueError(
                    "post_apply_quality_failed:"
                    + ",".join(post_quality["reason_codes"])
                )
            if len(post_rows) != int(plan["canonical_rows_after"]):
                raise ValueError("post_apply_row_count_mismatch")
        except Exception as exc:  # noqa: BLE001 - exact per-symbol rollback contract
            rollback_errors: list[str] = []
            try:
                if before_exists:
                    atomic_restore_from_backup(target, backup, expected_sha256=before_sha)
                else:
                    target.unlink(missing_ok=True)
            except (OSError, ValueError) as rollback_exc:
                rollback_errors.append(str(rollback_exc))
            failed = {
                **plan,
                "status": "failed",
                "applied": False,
                "canonical_write": False,
                "reason_codes": ["historical_raw_symbol_transaction_failed"],
                "error": f"{type(exc).__name__}:{exc}"[:1000],
                "rollback_performed": not rollback_errors,
                "rollback_errors": rollback_errors,
                "canonical_sha256_restored": _sha256(target),
                "backup_path": str(backup) if before_exists else "",
            }
            return _write_payload(output_path, failed)

        completed = {
            **plan,
            "status": "completed",
            "applied": True,
            "canonical_write": True,
            "canonical_sha256_after": _sha256(target),
            "backup_path": str(backup) if before_exists else "",
            "rollback_performed": False,
            "completed_at": now_text(),
        }
        return _write_payload(output_path, completed)


__all__ = [
    "PROVENANCE_SCHEMA_VERSION",
    "SCHEMA_VERSION",
    "apply_proven_historical_intraday_import",
    "build_proven_historical_intraday_import_plan",
]
