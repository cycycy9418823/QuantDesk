"""Evidence-led resolution for quarantined Stage 6 intraday conflicts.

This module is deliberately narrower than the normal append-only importer.  It
implements the exceptional review branches approved in
``CODEX_TASK_quantdesk_ai_fund_manager_v1.md`` section 15.1 plus the
2026-07-19 user section 16 ruling. It never turns an unclassified
cross-provider disagreement into canonical market data.
"""

from __future__ import annotations

import csv
import hashlib
import io
import os
import shutil
import uuid
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Mapping, Sequence

import pandas as pd

from quantdesk.csv_io import read_csv_frame
from quantdesk.interprocess_lock import interprocess_file_lock
from quantdesk.intraday.canonical_write_guard import (
    atomic_restore_from_backup,
    ensure_path_outside_canonical,
    sha256_path,
)
from quantdesk.intraday.stage6_append_only_import import (
    CANONICAL_WRITE_LOCK_TIMEOUT_SECONDS,
    canonical_intraday_write_lock_path,
    _stage_frame,
)
from quantdesk.intraday.intraday_quality_audit import audit_intraday_file, assess_complete_session
from quantdesk.intraday.intraday_schema import STANDARD_COLUMNS, normalize_intraday_frame
from quantdesk.intraday.minute_bar_lifecycle import (
    LIFECYCLE_COLUMNS, LIFECYCLE_DIR, merge_bar_observation_logs, read_bar_observation_log,
    select_bar_observations,
)
from quantdesk.intraday.minute_storage import resolve_minute_read_path
from quantdesk.shared import normalize_symbol, now_text
from quantdesk.utils import atomic_write_json

SECTION_15_REFERENCE = (
    "CODEX_TASK_quantdesk_ai_fund_manager_v1.md section 15.1; "
    "2026-07-19 user ruling section 16"
)
QUARANTINE_EVIDENCE_STATUS = "redundant_alternative_source_evidence"
OHLC_COLUMNS = ("open", "high", "low", "close")
DEFAULT_MINIMUM_TOLERANCE = Decimal("0.02")
DEFAULT_RELATIVE_CLOSE_TOLERANCE = Decimal("0.003")
DEFAULT_EXCLUDED_TIMES = (
    "09:31:00",
    "09:32:00",
    "09:33:00",
    "09:34:00",
    "09:35:00",
    "13:01:00",
)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _frame_for_date(path: Path, symbol: str, target_date: str) -> pd.DataFrame:
    # Comparison hashes name physical files. Do not redirect one side to a
    # different schema segment while reporting the original file's digest.
    raw = read_csv_frame(path, encoding="utf-8-sig", dtype=str, keep_default_na=False)
    payload = normalize_intraday_frame(
        raw,
        default_symbol=normalize_symbol(symbol),
        target_date=str(target_date)[:10],
    )
    frame = payload.get("frame")
    if not isinstance(frame, pd.DataFrame):
        return pd.DataFrame()
    return frame.copy()


def _final_evidence_candidate(canonical_dir: Path, staging_path: Path, symbol: str,
                              target_date: str) -> tuple[Path, bytes]:
    """Copy independently validated events without giving legacy rows finality."""
    events, errors = _stage_frame(staging_path, symbol, target_date)
    if events is None or errors or "evidence_hash" not in events.columns:
        raise ValueError("final_minute_source_evidence_missing")
    target = canonical_dir / LIFECYCLE_DIR / f"{symbol}.csv"
    previous = read_bar_observation_log(target) if target.exists() else pd.DataFrame()
    merged = merge_bar_observation_logs(previous, events)
    known = set(previous["evidence_hash"]) if not previous.empty else set()
    added = merged.loc[~merged["evidence_hash"].isin(known)]
    original = target.read_bytes() if target.exists() else b""
    if added.empty:
        return target, original
    columns = list(previous.columns) if not previous.empty else STANDARD_COLUMNS + list(LIFECYCLE_COLUMNS)
    if set(STANDARD_COLUMNS + list(LIFECYCLE_COLUMNS)) - set(columns):
        raise ValueError("minute_header_mismatch")
    buffer = io.StringIO(newline="")
    writer = csv.DictWriter(buffer, fieldnames=columns, extrasaction="ignore", lineterminator="\n")
    if not original:
        writer.writeheader()
    writer.writerows(added.to_dict("records"))
    suffix = buffer.getvalue().encode("utf-8")
    separator = b"" if not original or original.endswith((b"\n", b"\r")) else b"\n"
    return target, original + separator + suffix


def _decimal(value: Any) -> Decimal:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return Decimal("NaN")
    return result


def compare_intraday_sources(
    canonical_path: Path,
    staging_path: Path,
    *,
    symbol: str,
    target_date: str,
    minimum_tolerance: Decimal | float | str = DEFAULT_MINIMUM_TOLERANCE,
    relative_close_tolerance: Decimal | float | str = DEFAULT_RELATIVE_CLOSE_TOLERANCE,
    excluded_times: Sequence[str] = DEFAULT_EXCLUDED_TIMES,
) -> dict[str, Any]:
    """Compare shared minutes under the section 16 calibrated tolerance.

    ``large_diff`` means ``abs(diff) > max(0.02, 0.003 * close)``. The
    09:31-09:35 opening window and 13:01 boundary bar are excluded by the
    ruling because providers legitimately disagree on those bars.
    """

    canonical_path = Path(canonical_path)
    staging_path = Path(staging_path)
    normalized_symbol = normalize_symbol(symbol)
    date_text = str(target_date)[:10]
    minimum_tolerance_decimal = _decimal(minimum_tolerance)
    relative_tolerance_decimal = _decimal(relative_close_tolerance)
    left = _frame_for_date(canonical_path, normalized_symbol, date_text)
    right = _frame_for_date(staging_path, normalized_symbol, date_text)

    def by_time(frame: pd.DataFrame) -> dict[str, Mapping[str, Any]]:
        rows: dict[str, Mapping[str, Any]] = {}
        if frame.empty:
            return rows
        for row in frame.to_dict(orient="records"):
            timestamp = pd.to_datetime(row.get("datetime"), errors="coerce")
            if pd.isna(timestamp):
                continue
            rows[timestamp.strftime("%H:%M:%S")] = row
        return rows

    canonical_rows = by_time(left)
    staging_rows = by_time(right)
    common_times = sorted(set(canonical_rows) & set(staging_rows))
    excluded = {str(item) for item in excluded_times}
    compared_times = [item for item in common_times if item not in excluded]
    large_items: list[dict[str, Any]] = []
    max_diff = {column: Decimal("0") for column in OHLC_COLUMNS}
    for time_text in compared_times:
        differences: dict[str, Decimal] = {}
        invalid_numeric = False
        canonical_close = _decimal(canonical_rows[time_text].get("close"))
        staging_close = _decimal(staging_rows[time_text].get("close"))
        close_reference = max(
            abs(value)
            for value in (canonical_close, staging_close)
            if value.is_finite()
        ) if canonical_close.is_finite() or staging_close.is_finite() else Decimal("NaN")
        effective_tolerance = (
            max(
                minimum_tolerance_decimal,
                relative_tolerance_decimal * close_reference,
            )
            if close_reference.is_finite()
            else minimum_tolerance_decimal
        )
        for column in OHLC_COLUMNS:
            left_value = _decimal(canonical_rows[time_text].get(column))
            right_value = _decimal(staging_rows[time_text].get(column))
            if not left_value.is_finite() or not right_value.is_finite():
                invalid_numeric = True
                difference = Decimal("Infinity")
            else:
                difference = abs(left_value - right_value)
                max_diff[column] = max(max_diff[column], difference)
            differences[column] = difference
        if invalid_numeric or any(value > effective_tolerance for value in differences.values()):
            large_items.append(
                {
                    "time": time_text,
                    "effective_tolerance": str(effective_tolerance),
                    "close_reference": str(close_reference),
                    "absolute_differences": {
                        key: str(value) for key, value in differences.items()
                    },
                }
            )
    return {
        "contract_reference": SECTION_15_REFERENCE,
        "symbol": normalized_symbol,
        "target_date": date_text,
        "canonical_path": str(canonical_path),
        "staging_path": str(staging_path),
        "canonical_sha256": _sha256(canonical_path) if canonical_path.exists() else "",
        "staging_sha256": _sha256(staging_path) if staging_path.exists() else "",
        "minimum_absolute_tolerance": float(minimum_tolerance_decimal),
        "relative_close_tolerance": float(relative_tolerance_decimal),
        "excluded_times": sorted(excluded),
        "canonical_timestamp_count": len(canonical_rows),
        "staging_timestamp_count": len(staging_rows),
        "common_timestamp_count": len(common_times),
        "compared_timestamp_count": len(compared_times),
        "canonical_only_times": sorted(set(canonical_rows) - set(staging_rows)),
        "staging_only_times": sorted(set(staging_rows) - set(canonical_rows)),
        "large_diff_count": len(large_items),
        "large_diff_timestamps": [item["time"] for item in large_items],
        "large_differences": large_items,
        "max_absolute_difference": {
            key: float(value) for key, value in max_diff.items()
        },
    }


def _csv_fields(line: bytes) -> list[str]:
    text = line.decode("utf-8-sig").rstrip("\r\n")
    return next(csv.reader([text]))


def _field_spans(line: bytes) -> list[tuple[int, int]]:
    """Return byte spans for CSV fields, preserving quotes and line endings."""

    content_end = len(line.rstrip(b"\r\n"))
    spans: list[tuple[int, int]] = []
    start = 0
    quoted = False
    index = 0
    while index < content_end:
        value = line[index]
        if value == 34:  # quote
            if quoted and index + 1 < content_end and line[index + 1] == 34:
                index += 2
                continue
            quoted = not quoted
        elif value == 44 and not quoted:  # comma
            spans.append((start, index))
            start = index + 1
        index += 1
    spans.append((start, content_end))
    return spans


def _row_date(fields: Sequence[str], header: Sequence[str]) -> str:
    if "date" in header:
        return str(fields[header.index("date")])[:10]
    if "datetime" in header:
        return str(fields[header.index("datetime")])[:10]
    return ""


def _replace_target_basis_bytes(content: bytes, target_date: str) -> tuple[bytes, int]:
    lines = content.splitlines(keepends=True)
    if not lines:
        raise ValueError("canonical_csv_empty")
    header = _csv_fields(lines[0])
    if "price_basis" not in header:
        raise ValueError("canonical_price_basis_column_missing")
    basis_index = header.index("price_basis")
    changed = 0
    output = [lines[0]]
    for line in lines[1:]:
        fields = _csv_fields(line)
        if len(fields) != len(header):
            raise ValueError("canonical_csv_row_width_mismatch")
        if _row_date(fields, header) != target_date:
            output.append(line)
            continue
        if str(fields[basis_index]).strip().lower() != "unknown":
            raise ValueError("target_price_basis_not_unknown")
        spans = _field_spans(line)
        start, end = spans[basis_index]
        field = line[start:end]
        if field.count(b"unknown") != 1:
            raise ValueError("target_price_basis_bytes_ambiguous")
        output.append(line[:start] + field.replace(b"unknown", b"raw", 1) + line[end:])
        changed += 1
    if changed == 0:
        raise ValueError("target_date_rows_missing")
    return b"".join(output), changed


def _non_label_sha256(content: bytes) -> str:
    lines = content.splitlines(keepends=True)
    if not lines:
        return hashlib.sha256(content).hexdigest()
    header = _csv_fields(lines[0])
    if "price_basis" not in header:
        return hashlib.sha256(content).hexdigest()
    index = header.index("price_basis")
    normalized = [lines[0]]
    for line in lines[1:]:
        spans = _field_spans(line)
        if len(spans) != len(header):
            raise ValueError("canonical_csv_row_width_mismatch")
        start, end = spans[index]
        normalized.append(line[:start] + b"<PRICE_BASIS>" + line[end:])
    return hashlib.sha256(b"".join(normalized)).hexdigest()


def _replace_target_date_bytes(
    canonical_content: bytes,
    staging_content: bytes,
    target_date: str,
) -> bytes:
    canonical_lines = canonical_content.splitlines(keepends=True)
    staging_lines = staging_content.splitlines(keepends=True)
    if not canonical_lines or not staging_lines:
        raise ValueError("replacement_csv_empty")
    canonical_header = _csv_fields(canonical_lines[0])
    staging_header = _csv_fields(staging_lines[0])
    projection: list[int] | None = None
    if canonical_header != staging_header:
        staging_only = set(staging_header) - set(canonical_header)
        if (
            staging_only <= set(LIFECYCLE_COLUMNS) | {"price_only_intraday"}
            and set(canonical_header) <= set(staging_header)
            and len(set(staging_header)) == len(staging_header)
        ):
            projection = [staging_header.index(column) for column in canonical_header]
        else:
            raise ValueError("replacement_header_mismatch")
    indexes = [
        index
        for index, line in enumerate(canonical_lines[1:], start=1)
        if _row_date(_csv_fields(line), canonical_header) == target_date
    ]
    latest_hashes: set[str] | None = None
    if "evidence_hash" in canonical_header:
        raise ValueError("signed_minute_event_requires_explicit_revision")
    if "evidence_hash" in staging_header:
        events = pd.DataFrame(list(csv.DictReader(io.StringIO(staging_content.decode("utf-8-sig")))))
        latest_hashes = set(select_bar_observations(events)["evidence_hash"])
    stage_rows: list[bytes] = []
    for line in staging_lines[1:]:
        fields = _csv_fields(line)
        if _row_date(fields, staging_header) != target_date:
            continue
        if latest_hashes is not None and fields[staging_header.index("evidence_hash")] not in latest_hashes:
            continue
        if projection is None:
            stage_rows.append(line)
            continue
        spans = _field_spans(line)
        if len(spans) != len(staging_header):
            raise ValueError("replacement_staging_row_width_mismatch")
        ending = b"\r\n" if line.endswith(b"\r\n") else (b"\n" if line.endswith(b"\n") else b"")
        stage_rows.append(
            b",".join(line[spans[index][0] : spans[index][1]] for index in projection)
            + ending
        )
    if not indexes or not stage_rows:
        raise ValueError("replacement_target_rows_missing")
    if indexes != list(range(indexes[0], indexes[-1] + 1)):
        raise ValueError("replacement_target_rows_non_contiguous")
    return b"".join(
        canonical_lines[: indexes[0]]
        + stage_rows
        + canonical_lines[indexes[-1] + 1 :]
    )


def _audit_reasons(audit: Mapping[str, Any]) -> list[str]:
    reasons = [str(item) for item in list(audit.get("session_reason_codes") or [])]
    if str(audit.get("status") or "") in {"missing", "invalid", "insufficient"} and not reasons:
        reasons.append("canonical_file_quality_invalid")
    return list(dict.fromkeys(reasons))


def _resolve_stage6_intraday_conflicts_locked(
    *,
    canonical_dir: Path,
    staging_dir: Path,
    symbols: Sequence[str],
    target_date: str,
    run_id: str,
    backup_dir: Path,
    output_path: Path | None = None,
    apply: bool = True,
) -> dict[str, Any]:
    """Apply only the bounded, evidence-approved section 15.1 branches.

    A batch containing an unclassified disagreement is kept entirely zero-write.
    This conservative fourth outcome is necessary when the observed evidence does
    not satisfy any of the three authorized mutation/closure branches.
    """

    canonical_dir = Path(canonical_dir)
    staging_dir = Path(staging_dir)
    date_text = str(target_date)[:10]
    normalized_symbols = sorted(
        {normalize_symbol(item) for item in symbols if normalize_symbol(item)}
    )
    items: list[dict[str, Any]] = []
    candidates: dict[str, bytes] = {}
    evidence_candidates: dict[str, tuple[Path, bytes]] = {}
    for symbol in normalized_symbols:
        canonical_path = canonical_dir / f"{symbol}.csv"
        staging_path = resolve_minute_read_path(staging_dir / f"{symbol}.csv")
        canonical_audit = audit_intraday_file(
            symbol, date_text, intraday_dir=canonical_dir
        )
        staging_audit = audit_intraday_file(
            symbol, date_text, intraday_dir=staging_dir
        )
        comparison = compare_intraday_sources(
            canonical_path,
            staging_path,
            symbol=symbol,
            target_date=date_text,
        )
        physical = read_csv_frame(canonical_path, encoding="utf-8-sig", dtype=str, keep_default_na=False)
        signed = any(column in physical.columns for column in (
            "evidence_hash", "revision", "supersedes_hash", "revision_reason",
            "is_final", "finality", "bar_start", "bar_end", "fetched_at"))
        legacy = canonical_path.is_file() and not signed
        if legacy:
            geometry = assess_complete_session(_frame_for_date(canonical_path, symbol, date_text),
                                               symbol=symbol, target_date=date_text)
            canonical_audit = {**canonical_audit, **geometry,
                               "session_status": geometry["status"],
                               "session_reason_codes": geometry["reason_codes"]}
        reasons = _audit_reasons(canonical_audit)
        real_quality_reasons = [
            item for item in reasons if item not in {"price_basis_not_raw", "legacy_bar_lifecycle_unverified"}
        ]
        staging_passed = staging_audit.get("formal_execution_eligible") is True
        if canonical_audit.get("formal_execution_eligible") is True:
            decision = "audit_passed_zero_write"
        elif signed:
            # A label patch or target-date replacement would rewrite signed
            # history. Only the append importer may accept explicit revisions.
            decision = "unclassified_cross_source_mismatch"
            reasons = list(dict.fromkeys([*reasons, "signed_minute_event_requires_explicit_revision"]))
        elif (
            set(reasons) - {"legacy_bar_lifecycle_unverified"} == {"price_basis_not_raw"}
            and comparison["large_diff_count"] == 0
            and staging_passed
        ):
            decision = "bounded_price_basis_label_repair"
        elif real_quality_reasons and staging_passed:
            decision = "bounded_target_date_replacement"
        elif legacy and not real_quality_reasons and staging_passed and comparison["large_diff_count"] == 0:
            decision = "bounded_final_evidence_segment"
        else:
            decision = "unclassified_cross_source_mismatch"
        before = canonical_path.read_bytes() if canonical_path.exists() else b""
        item: dict[str, Any] = {
            "symbol": symbol,
            "target_date": date_text,
            "decision": decision,
            "quarantine_status": QUARANTINE_EVIDENCE_STATUS,
            "canonical_audit": canonical_audit,
            "staging_audit": staging_audit,
            "canonical_reason_codes": reasons,
            "real_quality_reason_codes": real_quality_reasons,
            **comparison,
            "canonical_sha256_before": hashlib.sha256(before).hexdigest() if before else "",
            "canonical_sha256_after": "",
            "non_label_sha256_before": _non_label_sha256(before) if before else "",
            "non_label_sha256_after": "",
            "backup_path": "",
            "post_repair_audit": {},
            "applied": False,
        }
        comparison_artifact = Path(output_path).parent / (
            f"cross_source_compare_{symbol}_{date_text}.json"
        ) if output_path is not None else Path(backup_dir) / str(run_id) / (
            f"cross_source_compare_{symbol}_{date_text}.json"
        )
        comparison_payload = {
            key: value
            for key, value in comparison.items()
            if key not in {"comparison_artifact_path"}
        }
        atomic_write_json(comparison_artifact, comparison_payload)
        item["comparison_artifact_path"] = str(comparison_artifact)
        if decision in {"bounded_price_basis_label_repair", "bounded_target_date_replacement", "bounded_final_evidence_segment"}:
            try:
                if decision == "bounded_price_basis_label_repair":
                    candidate, changed_rows = _replace_target_basis_bytes(before, date_text)
                    candidates[symbol] = candidate
                    item["changed_label_row_count"] = changed_rows
                elif decision == "bounded_target_date_replacement":
                    candidates[symbol] = _replace_target_date_bytes(before, staging_path.read_bytes(), date_text)
                evidence_candidates[symbol] = _final_evidence_candidate(canonical_dir, staging_path, symbol, date_text)
                item["evidence_segment_path"] = str(evidence_candidates[symbol][0])
                item["evidence_source_path"] = str(staging_path)
                item["evidence_source_sha256"] = _sha256(staging_path)
                item["legacy_finality_promoted"] = False
            except (OSError, ValueError) as exc:
                candidates.pop(symbol, None)
                evidence_candidates.pop(symbol, None)
                item["decision"] = "unclassified_cross_source_mismatch"
                item["canonical_reason_codes"] = [*reasons, str(exc)]
        items.append(item)

    has_unclassified = any(
        item["decision"] == "unclassified_cross_source_mismatch" for item in items
    )
    mutation_items = [item for item in items if item["symbol"] in candidates or item["symbol"] in evidence_candidates]
    payload: dict[str, Any] = {
        "schema_version": 1,
        "contract_reference": SECTION_15_REFERENCE,
        "historical_note": (
            "Canonical 2026-07-14 rows predate the append-only gate; no blame and "
            "no rollback. The new gate exposed the conflict before overwrite."
        ),
        "run_id": str(run_id),
        "target_date": date_text,
        "generated_at": now_text(),
        "requested_symbols": normalized_symbols,
        "quarantine_status": QUARANTINE_EVIDENCE_STATUS,
        "items": items,
        "applied": False,
        "rollback_performed": False,
        "reason_codes": [],
    }
    if has_unclassified:
        payload.update(
            {
                "status": "external_review_required",
                "reason_codes": ["observed_evidence_matches_no_authorized_section15_branch"],
            }
        )
    elif not apply:
        payload["status"] = "ready"
    elif not mutation_items:
        payload.update({"status": "completed", "applied": True})
    else:
        retained_backups = Path(backup_dir) / str(run_id)
        transaction = retained_backups / f".transaction-{uuid.uuid4().hex}"
        transaction.mkdir(parents=True, exist_ok=False)
        rollback_errors: list[str] = []
        writes: list[dict[str, Any]] = []
        try:
            for item in mutation_items:
                symbol = str(item["symbol"])
                changes = []
                if symbol in candidates:
                    changes.append((canonical_dir / f"{symbol}.csv", candidates[symbol], "backup_path"))
                if symbol in evidence_candidates:
                    target, content = evidence_candidates[symbol]
                    changes.append((target, content, "evidence_backup_path"))
                for target, content, backup_key in changes:
                    relative = target.relative_to(canonical_dir)
                    backup = retained_backups / relative
                    expected = sha256_path(target) if target.exists() else ""
                    if expected:
                        backup.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(target, backup)
                        if sha256_path(backup) != expected:
                            raise ValueError(f"backup_hash_verification_failed:{symbol}")
                        item[backup_key] = str(backup)
                    candidate_path = transaction / relative
                    candidate_path.parent.mkdir(parents=True, exist_ok=True)
                    candidate_path.write_bytes(content)
                    writes.append({"target": target, "candidate": candidate_path, "backup": backup,
                                   "expected_sha256": expected, "item": item})
            for write in writes:
                write["target"].parent.mkdir(parents=True, exist_ok=True)
                os.replace(write["candidate"], write["target"])
            for item in mutation_items:
                symbol = str(item["symbol"])
                target = canonical_dir / f"{symbol}.csv"
                after_audit = audit_intraday_file(
                    symbol, date_text, intraday_dir=canonical_dir
                )
                item["post_repair_audit"] = after_audit
                if after_audit.get("formal_execution_eligible") is not True:
                    raise ValueError(f"post_repair_quality_failed:{symbol}")
                after = target.read_bytes()
                item["canonical_sha256_after"] = hashlib.sha256(after).hexdigest()
                item["non_label_sha256_after"] = _non_label_sha256(after)
                if item.get("evidence_segment_path"):
                    item["evidence_segment_sha256"] = sha256_path(Path(item["evidence_segment_path"]))
                if item["decision"] == "bounded_price_basis_label_repair" and (
                    item["non_label_sha256_before"] != item["non_label_sha256_after"]
                ):
                    raise ValueError(f"non_label_bytes_changed:{symbol}")
                item["applied"] = True
            payload.update(
                {
                    "status": "completed",
                    "applied": True,
                    "completed_at": now_text(),
                }
            )
        except Exception as exc:  # noqa: BLE001 - explicit rollback guard
            for write in reversed(writes):
                item = write["item"]
                target = write["target"]
                expected_hash = write["expected_sha256"]
                try:
                    if not expected_hash:
                        target.unlink(missing_ok=True)
                        restored_hash = ""
                    elif target.exists() and sha256_path(target) == expected_hash:
                        restored_hash = expected_hash
                    elif write["backup"].is_file():
                        restored_hash = atomic_restore_from_backup(
                            target,
                            write["backup"],
                            expected_sha256=expected_hash,
                        )
                    else:
                        raise ValueError("rollback_backup_missing")
                    item["rollback_verified"] = restored_hash == expected_hash
                    if target == canonical_dir / f"{item['symbol']}.csv":
                        item["canonical_sha256_restored"] = restored_hash
                    if not item["rollback_verified"]:
                        raise ValueError("rollback_hash_verification_failed")
                except (OSError, ValueError) as rollback_exc:
                    item["rollback_verified"] = False
                    rollback_errors.append(f"{item['symbol']}:{rollback_exc}")
            for item in mutation_items:
                item["applied"] = False
                item["post_repair_audit"] = {}
                item["canonical_sha256_after"] = ""
                item["non_label_sha256_after"] = ""
                item["evidence_segment_sha256"] = ""
            payload.update(
                {
                    "status": "failed",
                    "applied": False,
                    "rollback_performed": not rollback_errors,
                    "rollback_errors": rollback_errors,
                    "reason_codes": ["section15_transaction_failed"],
                    "error": f"{type(exc).__name__}:{exc}"[:1000],
                }
            )
        finally:
            shutil.rmtree(transaction, ignore_errors=True)

    if output_path is not None:
        atomic_write_json(Path(output_path), payload)
        payload["output_path"] = str(output_path)
    return payload


def resolve_stage6_intraday_conflicts(
    *,
    canonical_dir: Path,
    staging_dir: Path,
    symbols: Sequence[str],
    target_date: str,
    run_id: str,
    backup_dir: Path,
    output_path: Path | None = None,
    apply: bool = True,
) -> dict[str, Any]:
    """Run the bounded resolver under the singleton canonical write lock."""

    canonical = Path(canonical_dir)
    ensure_path_outside_canonical(
        Path(staging_dir),
        canonical,
        role="conflict_staging_dir",
    )
    ensure_path_outside_canonical(
        Path(backup_dir) / str(run_id),
        canonical,
        role="conflict_backup_dir",
    )
    ensure_path_outside_canonical(
        Path(output_path) if output_path is not None else None,
        canonical,
        role="conflict_output",
    )
    if not apply:
        return _resolve_stage6_intraday_conflicts_locked(
            canonical_dir=canonical_dir,
            staging_dir=staging_dir,
            symbols=symbols,
            target_date=target_date,
            run_id=run_id,
            backup_dir=backup_dir,
            output_path=output_path,
            apply=False,
        )
    canonical.mkdir(parents=True, exist_ok=True)
    with interprocess_file_lock(
        canonical_intraday_write_lock_path(canonical),
        timeout_seconds=CANONICAL_WRITE_LOCK_TIMEOUT_SECONDS,
    ):
        return _resolve_stage6_intraday_conflicts_locked(
            canonical_dir=canonical,
            staging_dir=staging_dir,
            symbols=symbols,
            target_date=target_date,
            run_id=run_id,
            backup_dir=backup_dir,
            output_path=output_path,
            apply=True,
        )
