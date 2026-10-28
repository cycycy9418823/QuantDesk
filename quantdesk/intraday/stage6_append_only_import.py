"""Quality-audited append-only import for Stage 6 minute batches."""

from __future__ import annotations

import csv
import hashlib
import io
import os
import shutil
import threading
import uuid
from contextlib import contextmanager
from decimal import Decimal, InvalidOperation, ROUND_HALF_EVEN
from pathlib import Path
from typing import Any, Iterator, Mapping, Sequence

import pandas as pd

from quantdesk.interprocess_lock import interprocess_file_lock
from quantdesk.intraday.canonical_write_guard import (
    atomic_restore_from_backup,
    ensure_path_outside_canonical,
    path_is_within,
    sha256_path,
)
from quantdesk.intraday.intraday_quality_audit import audit_intraday_batch
from quantdesk.intraday.intraday_schema import STANDARD_COLUMNS, read_intraday_csv
from quantdesk.intraday.minute_bar_lifecycle import (
    LIFECYCLE_COLUMNS, LIFECYCLE_DIR, select_bar_observations,
    merge_bar_observation_logs, read_bar_observation_log,
)
from quantdesk.intraday.minute_storage import resolve_minute_read_path
from quantdesk.shared import normalize_symbol, now_text
from quantdesk.utils import atomic_write_json

COMPARE_COLUMNS = (
    "datetime",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "amount",
    "price_basis",
)
CANONICAL_APPEND_REQUIRED_COLUMNS = frozenset(
    {
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
        "price_basis",
        "source",
        "updated_at",
    }
)
COMPARE_QUANTUM = {
    "open": Decimal("0.00000001"),
    "high": Decimal("0.00000001"),
    "low": Decimal("0.00000001"),
    "close": Decimal("0.00000001"),
    "volume": Decimal("0.000001"),
    "amount": Decimal("0.000001"),
}
CANONICAL_WRITE_LOCK_NAME = "stage6_append_only_batch.lock"
STAGING_PRICE_ONLY_PROVENANCE_COLUMN = "price_only_intraday"
# The section 16 singleton channel serializes every canonical minute mutation,
# so a batch's legitimate wait scales with the number of concurrent batch
# workers times one hold (audit + preflight + commit + verify, including three
# SHA256 passes over multi-megabyte canonical files). The 60s library default
# assumes an uncontended lock and made a 4-worker chain fail at 895/914 with
# interprocess_file_lock_timeout on 2026-08-13. Waiting is not a correctness
# risk here - the holder still owns the full transaction - so the wait is sized
# for real contention instead of narrowing the lock. A genuinely stuck holder is
# still bounded by the scheduled task's own ExecutionTimeLimit.
CANONICAL_WRITE_LOCK_TIMEOUT_SECONDS = 900.0
_CANONICAL_TRANSACTION_LOCKS = threading.local()


def canonical_intraday_write_lock_path(canonical_dir: Path) -> Path:
    """Return the singleton lock shared by every canonical minute writer."""

    return Path(canonical_dir) / ".locks" / CANONICAL_WRITE_LOCK_NAME


@contextmanager
def canonical_intraday_write_transaction(canonical_dir: Path) -> Iterator[Path]:
    """Keep the singleton lock across several audited appends and their rollback."""

    canonical = Path(canonical_dir)
    key = canonical.resolve()
    owned = getattr(_CANONICAL_TRANSACTION_LOCKS, "paths", ())
    if key in owned:
        yield canonical
        return
    canonical.mkdir(parents=True, exist_ok=True)
    with interprocess_file_lock(
        canonical_intraday_write_lock_path(canonical),
        timeout_seconds=CANONICAL_WRITE_LOCK_TIMEOUT_SECONDS,
    ):
        _CANONICAL_TRANSACTION_LOCKS.paths = (*owned, key)
        try:
            yield canonical
        finally:
            _CANONICAL_TRANSACTION_LOCKS.paths = owned


def _sha256(path: Path) -> str:
    if not path.exists() or not path.is_file():
        return ""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _date_text(row: Mapping[str, Any]) -> str:
    date_value = str(row.get("date") or "").strip()[:10]
    if date_value:
        return date_value
    return str(row.get("datetime") or "").strip()[:10]


def _read_canonical_rows(path: Path, target_date: str) -> tuple[list[str], list[dict[str, str]], str]:
    if not path.exists():
        return [], [], ""
    target_rows: list[dict[str, str]] = []
    latest_date = ""
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        header = list(reader.fieldnames or [])
        for raw in reader:
            row = {str(key): str(value or "") for key, value in raw.items() if key is not None}
            row_date = _date_text(row)
            if row_date > latest_date:
                latest_date = row_date
            if row_date == target_date:
                target_rows.append(row)
    return header, target_rows, latest_date


def _decimal_text(value: Any, *, quantum: Decimal | None = None) -> str:
    if value is None:
        return ""
    try:
        if bool(pd.isna(value)):
            return ""
    except (TypeError, ValueError):
        pass
    text = str(value).strip()
    if not text:
        return ""
    try:
        number = Decimal(text)
    except InvalidOperation:
        return text
    if not number.is_finite():
        return text.lower()
    if quantum is not None:
        number = number.quantize(quantum, rounding=ROUND_HALF_EVEN)
    normalized = format(number.normalize(), "f")
    return "0" if normalized in {"-0", ""} else normalized


def _comparable_rows(rows: Sequence[Mapping[str, Any]]) -> list[tuple[str, ...]]:
    if rows and any("evidence_hash" in row for row in rows):
        rows = select_bar_observations(pd.DataFrame(rows)).to_dict("records")
    comparable: list[tuple[str, ...]] = []
    for row in rows:
        values: list[str] = []
        for column in COMPARE_COLUMNS:
            raw = row.get(column)
            if column == "datetime":
                parsed = pd.to_datetime(raw, errors="coerce")
                values.append("" if pd.isna(parsed) else parsed.strftime("%Y-%m-%d %H:%M:%S"))
            else:
                values.append(
                    _decimal_text(raw, quantum=COMPARE_QUANTUM.get(column))
                )
        comparable.append(tuple(values))
    return sorted(comparable)


def _staging_provenance_errors(path: Path) -> list[str]:
    if not path.exists() or not path.is_file():
        return []
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle)
            header = list(reader.fieldnames or [])
            if STAGING_PRICE_ONLY_PROVENANCE_COLUMN not in header:
                return ["staging_price_only_provenance_missing"]
            values = {
                str(row.get(STAGING_PRICE_ONLY_PROVENANCE_COLUMN) or "")
                .strip()
                .lower()
                for row in reader
            }
    except (OSError, UnicodeError, csv.Error):
        return ["staging_price_only_provenance_unreadable"]
    true_values = {"1", "true", "yes", "y", "on"}
    false_values = {"0", "false", "no", "n", "off"}
    if values & true_values:
        return ["staging_price_only_provenance_true"]
    if not values or not values <= false_values:
        return ["staging_price_only_provenance_invalid"]
    return []


def _stage_frame(path: Path, symbol: str, target_date: str) -> tuple[pd.DataFrame | None, list[str]]:
    path = resolve_minute_read_path(path)
    provenance_errors = _staging_provenance_errors(path)
    if provenance_errors:
        return None, provenance_errors
    payload = read_intraday_csv(path, default_symbol=symbol, target_date=target_date)
    frame = payload.get("frame")
    errors = [str(item) for item in list(payload.get("errors") or []) if str(item)]
    if not isinstance(frame, pd.DataFrame) or frame.empty:
        return None, errors or ["staging_target_date_missing"]
    observed_dates = sorted(
        {
            str(item)[:10]
            for item in frame.get("date", pd.Series(dtype=str)).astype(str).tolist()
            if str(item).strip()
        }
    )
    if observed_dates != [target_date]:
        return None, ["staging_contains_non_target_dates"]
    if "evidence_hash" in frame.columns:
        # The audit reads the latest view; import preserves every event needed
        # to reconstruct that view and its historical as-of evidence.
        try:
            events = read_bar_observation_log(Path(payload["path"]))
        except (OSError, ValueError, KeyError) as exc:
            return None, [str(exc)]
        events["revision"] = pd.to_numeric(events["revision"], errors="raise")
        stamps = pd.to_datetime(events["datetime"], errors="coerce")
        frame = events.loc[stamps.dt.strftime("%Y-%m-%d") == target_date].copy()
        # date/time are compatibility columns, not signed event identity. Derive
        # them from the signed datetime rather than trusting an unrelated date.
        frame["date"] = stamps.loc[frame.index].dt.strftime("%Y-%m-%d")
        frame["time"] = stamps.loc[frame.index].dt.strftime("%H:%M:%S")
        return frame.sort_values(["datetime", "revision"], kind="stable").reset_index(drop=True), errors
    return frame.sort_values("datetime").reset_index(drop=True), errors


def _quality_gate(
    symbols: Sequence[str],
    audit: Mapping[str, Any],
) -> tuple[bool, dict[str, Mapping[str, Any]]]:
    by_symbol = {
        normalize_symbol(item.get("symbol")): item
        for item in list(audit.get("items") or [])
        if isinstance(item, Mapping) and normalize_symbol(item.get("symbol"))
    }
    passed = True
    for symbol in symbols:
        item = by_symbol.get(symbol)
        if not item:
            passed = False
            continue
        if item.get("complete_session") is not True:
            passed = False
        if item.get("formal_execution_eligible") is not True:
            passed = False
        if str(item.get("price_basis") or "").lower() != "raw":
            passed = False
    return passed, by_symbol


def _identical_non_execution_batch(
    symbols: Sequence[str], *, staging: Path, canonical: Path, target_date: str,
    audit_by_symbol: Mapping[str, Mapping[str, Any]],
) -> list[dict[str, Any]] | None:
    """A byte-identical no-op may preserve zero-volume observations, never fill."""
    items = []
    for symbol in symbols:
        audit_item = audit_by_symbol.get(symbol, {})
        if set(audit_item.get("session_reason_codes", [])) != {"zero_volume_no_trade_evidence"}:
            return None
        stage_path = resolve_minute_read_path(staging / f"{symbol}.csv")
        target_path = resolve_minute_read_path(canonical / f"{symbol}.csv")
        frame, errors = _stage_frame(stage_path, symbol, target_date)
        if frame is None or errors or "evidence_hash" not in frame.columns or not target_path.is_file():
            return None
        try:
            prior = read_bar_observation_log(target_path)
        except (OSError, ValueError):
            return None
        prior = prior.loc[prior["datetime"].str[:10] == target_date]
        if set(prior["evidence_hash"]) != set(frame["evidence_hash"]):
            return None
        digest = _sha256(target_path)
        items.append({"symbol": symbol, "target_date": target_date,
                      "operation": "unchanged_identical_date", "formal_execution_eligible": False,
                      "quality_status": "warning", "reason_codes": ["zero_volume_no_trade_evidence"],
                      "canonical_path": str(target_path), "staging_path": str(stage_path),
                      "canonical_sha256_before": digest, "canonical_sha256_after": digest,
                      "staging_sha256": _sha256(stage_path)})
    return items


def _append_candidate(
    target: Path,
    candidate: Path,
    header: Sequence[str],
    stage: pd.DataFrame,
) -> None:
    original = target.read_bytes()
    buffer = io.StringIO(newline="")
    writer = csv.DictWriter(
        buffer,
        fieldnames=list(header),
        extrasaction="ignore",
        lineterminator="\n",
    )
    for row in stage.to_dict(orient="records"):
        writer.writerow({column: row.get(column, "") for column in header})
    suffix = buffer.getvalue().encode("utf-8")
    separator = b"" if not original or original.endswith((b"\n", b"\r")) else b"\n"
    candidate.parent.mkdir(parents=True, exist_ok=True)
    candidate.write_bytes(original + separator + suffix)


def _write_payload(path: Path | None, payload: dict[str, Any]) -> dict[str, Any]:
    if path is not None:
        atomic_write_json(path, payload)
        payload["output_path"] = str(path)
    return payload


def _transaction_volume_preflight(canonical: Path) -> dict[str, Any]:
    """Prove the external transaction parent shares the canonical device."""

    result: dict[str, Any] = {
        "status": "blocked",
        "canonical_path": "",
        "transaction_parent": "",
        "canonical_device": None,
        "transaction_parent_device": None,
        "reason_code": "",
        "error": "",
    }
    try:
        resolved_canonical = canonical.resolve(strict=True)
        transaction_parent = resolved_canonical.parent
    except (OSError, RuntimeError) as exc:
        result.update(
            {
                "reason_code": "canonical_transaction_volume_check_failed",
                "error": f"{type(exc).__name__}:{exc}"[:1000],
            }
        )
        return result
    try:
        transaction_parent_inside_canonical = path_is_within(
            transaction_parent,
            canonical,
        )
    except (OSError, RuntimeError) as exc:
        result.update(
            {
                "reason_code": "canonical_transaction_volume_check_failed",
                "error": f"{type(exc).__name__}:{exc}"[:1000],
            }
        )
        return result
    if transaction_parent_inside_canonical:
        result.update(
            {
                "reason_code": "canonical_transaction_parent_overlaps_canonical",
                "error": "canonical_transaction_parent_inside_canonical",
            }
        )
        return result
    try:
        canonical_device = int(resolved_canonical.stat().st_dev)
        transaction_parent_device = int(transaction_parent.stat().st_dev)
    except (OSError, TypeError, ValueError) as exc:
        result.update(
            {
                "reason_code": "canonical_transaction_volume_check_failed",
                "error": f"{type(exc).__name__}:{exc}"[:1000],
            }
        )
        return result

    result.update(
        {
            "canonical_path": str(resolved_canonical),
            "transaction_parent": str(transaction_parent),
            "canonical_device": canonical_device,
            "transaction_parent_device": transaction_parent_device,
        }
    )
    if canonical_device != transaction_parent_device:
        result["reason_code"] = "canonical_transaction_volume_mismatch"
        result["error"] = (
            f"canonical_device={canonical_device};"
            f"transaction_parent_device={transaction_parent_device}"
        )
        return result
    result["status"] = "passed"
    return result


def _record_transaction_cleanup_failure(
    base: dict[str, Any],
    transaction: Path,
    error: BaseException,
) -> None:
    warning = "append_transaction_cleanup_failed"
    warnings = [str(item) for item in list(base.get("warnings") or [])]
    if warning not in warnings:
        warnings.append(warning)
    base.update(
        {
            "cleanup_status": "failed",
            "cleanup_warning": warning,
            "pending_cleanup_path": str(transaction),
            "cleanup_error": f"{type(error).__name__}:{error}"[:1000],
            "warnings": warnings,
        }
    )
    if base.get("status") == "failed":
        base["recovery_transaction_path"] = str(transaction)


def _apply_audited_intraday_batch_locked(
    *,
    staging_dir: Path,
    canonical_dir: Path,
    symbols: Sequence[str],
    target_date: str,
    run_id: str,
    quality_audit: Mapping[str, Any] | None = None,
    output_path: Path | None = None,
    apply: bool = True,
) -> dict[str, Any]:
    """Audit and atomically append one complete Stage 6 batch.

    CODEX_TASK_quantdesk_ai_fund_manager_v1.md section 14.3 is the governing
    exception: only new dates/new symbols may be imported; existing rows are
    never rewritten, trimmed, reordered, or overwritten. Any quality failure or
    symbol/date conflict quarantines the whole batch in staging.
    """

    staging = Path(staging_dir)
    canonical = Path(canonical_dir)
    normalized_symbols = sorted(
        {normalize_symbol(item) for item in symbols if normalize_symbol(item)}
    )
    date_text = str(target_date)[:10]
    provided_audit = dict(quality_audit or {})
    audit = dict(
        audit_intraday_batch(
            normalized_symbols,
            date_text,
            intraday_dir=staging,
            write=False,
            run_id=run_id,
        )
    )
    quality_passed, audit_by_symbol = _quality_gate(normalized_symbols, audit)
    provided_quality_passed = True
    if quality_audit is not None:
        provided_quality_passed, _provided_by_symbol = _quality_gate(
            normalized_symbols,
            provided_audit,
        )
    quality_passed = quality_passed and provided_quality_passed
    base: dict[str, Any] = {
        "schema_version": 1,
        "contract_reference": "CODEX_TASK_quantdesk_ai_fund_manager_v1.md section 14.3",
        "run_id": str(run_id),
        "target_date": date_text,
        "generated_at": now_text(),
        "staging_dir": str(staging),
        "canonical_dir": str(canonical),
        "requested_symbols": normalized_symbols,
        "requested_symbol_count": len(normalized_symbols),
        "quality_status": "passed" if quality_passed else "blocked",
        "quality_summary": dict(audit.get("summary") or {}),
        "provided_quality_summary": dict(provided_audit.get("summary") or {}),
        "applied": False,
        "quarantined": False,
        "appended_symbol_count": 0,
        "unchanged_symbol_count": 0,
        "conflict_symbols": [],
        "reason_codes": [],
        "items": [],
    }
    if not normalized_symbols or not quality_passed:
        identical = (_identical_non_execution_batch(normalized_symbols, staging=staging, canonical=canonical,
                                                     target_date=date_text, audit_by_symbol=audit_by_symbol)
                     if normalized_symbols and provided_quality_passed else None)
        if identical:
            base.update(status="completed", applied=False, quality_status="warning", items=identical,
                        unchanged_symbol_count=len(identical), formal_execution_eligible=False,
                        reason_codes=["unchanged_non_execution_evidence"])
            return _write_payload(output_path, base)
        base.update(
            {
                "status": "blocked",
                "quarantined": True,
                "reason_codes": [
                    "empty_batch" if not normalized_symbols else "batch_quality_audit_failed"
                ],
            }
        )
        return _write_payload(output_path, base)

    planned: list[dict[str, Any]] = []
    conflicts: list[str] = []
    for symbol in normalized_symbols:
        stage_path = resolve_minute_read_path(staging / f"{symbol}.csv")
        target_path = canonical / f"{symbol}.csv"
        original_target = target_path
        legacy_latest_date = ""
        frame, stage_errors = _stage_frame(stage_path, symbol, date_text)
        if frame is not None and "evidence_hash" in frame.columns:
            header, prior_rows, legacy_latest_date = _read_canonical_rows(target_path, date_text)
            if (target_path.exists() and CANONICAL_APPEND_REQUIRED_COLUMNS <= set(header)
                    and "evidence_hash" not in header
                    and ((canonical / LIFECYCLE_DIR / f"{symbol}.csv").is_file()
                         or not prior_rows or _comparable_rows(prior_rows) == _comparable_rows(frame.to_dict("records")))):
                # Keep legacy archive bytes intact. New final evidence has its
                # own schema segment; it cannot be appended under an old header.
                target_path = canonical / LIFECYCLE_DIR / f"{symbol}.csv"
            elif not target_path.exists() and (canonical / LIFECYCLE_DIR / f"{symbol}.csv").is_file():
                target_path = canonical / LIFECYCLE_DIR / f"{symbol}.csv"
        audit_item = audit_by_symbol.get(symbol) or {}
        item: dict[str, Any] = {
            "symbol": symbol,
            "target_date": date_text,
            "staging_path": str(stage_path),
            "canonical_path": str(target_path),
            "staging_sha256": _sha256(stage_path),
            "canonical_sha256_before": _sha256(target_path),
            "canonical_sha256_after": "",
            "quality_status": "passed",
            "quality_reason_codes": list(audit_item.get("session_reason_codes") or []),
            "source": "",
            "retrieved_at": "",
            "operation": "blocked",
            "reason_codes": list(stage_errors),
        }
        if frame is None:
            conflicts.append(symbol)
            planned.append(item)
            continue
        sources = sorted({str(value) for value in frame["source"].dropna().tolist() if str(value)})
        retrieved = sorted(
            {str(value) for value in frame["updated_at"].dropna().tolist() if str(value)}
        )
        item["source"] = ",".join(sources)
        item["retrieved_at"] = retrieved[-1] if retrieved else ""
        header, existing_target_rows, latest_date = _read_canonical_rows(
            target_path, date_text
        )
        if "evidence_hash" in header and CANONICAL_APPEND_REQUIRED_COLUMNS <= set(header):
            try:
                read_bar_observation_log(target_path)
            except (OSError, ValueError) as exc:
                item.update(operation="conflict_existing_date", reason_codes=["canonical_symbol_date_conflict", str(exc)])
                conflicts.append(symbol)
                planned.append(item)
                continue
        if not target_path.exists():
            item["operation"] = (
                "append_missing_historical_date" if original_target != target_path
                and original_target.exists() and legacy_latest_date > date_text
                else "append_new_date" if original_target != target_path and original_target.exists()
                else "append_new_symbol")
            if original_target != target_path and original_target.is_file():
                item["legacy_segment_path"] = str(original_target)
                item["legacy_segment_sha256"] = _sha256(original_target)
        elif CANONICAL_APPEND_REQUIRED_COLUMNS - set(header):
            item["operation"] = "conflict_invalid_canonical_header"
            item["reason_codes"] = ["canonical_header_missing_required_fields"]
            item["missing_canonical_header_fields"] = sorted(
                CANONICAL_APPEND_REQUIRED_COLUMNS - set(header)
            )
            conflicts.append(symbol)
        elif existing_target_rows:
            try:
                if "evidence_hash" in header and "evidence_hash" in frame.columns:
                    previous = pd.DataFrame(existing_target_rows)
                    merged = merge_bar_observation_logs(previous, frame)
                    extra = merged.loc[~merged["evidence_hash"].isin(previous["evidence_hash"])]
                    item["operation"] = "append_bar_revisions" if not extra.empty else "unchanged_identical_date"
                    if not extra.empty:
                        frame = extra.reset_index(drop=True)
                elif _comparable_rows(existing_target_rows) == _comparable_rows(frame.to_dict("records")):
                    item["operation"] = "unchanged_identical_date"
                else:
                    raise ValueError("canonical_symbol_date_conflict")
            except ValueError as exc:
                item["operation"] = "conflict_existing_date"
                item["reason_codes"] = list(dict.fromkeys(["canonical_symbol_date_conflict", str(exc)]))
                conflicts.append(symbol)
        elif latest_date and latest_date > date_text:
            # Stage 6 task-book section 14.3 permits a missing date to be
            # appended but forbids rewriting/reordering existing rows. Keep the
            # canonical byte prefix intact and append this older gap date at EOF;
            # readers normalize/filter by target date before quality auditing.
            item["operation"] = "append_missing_historical_date"
        elif not header:
            item["operation"] = "conflict_invalid_canonical_header"
            item["reason_codes"] = ["canonical_header_missing"]
            conflicts.append(symbol)
        else:
            item["operation"] = "append_new_date"
        planned.append({**item, "_frame": frame, "_header": header})

    public_items = [
        {key: value for key, value in item.items() if not key.startswith("_")}
        for item in planned
    ]
    base["items"] = public_items
    if conflicts:
        base.update(
            {
                "status": "blocked",
                "quarantined": True,
                "conflict_symbols": sorted(set(conflicts)),
                "reason_codes": ["batch_contains_canonical_conflict"],
            }
        )
        return _write_payload(output_path, base)

    append_items = [
        item for item in planned if str(item.get("operation") or "").startswith("append_")
    ]
    unchanged_items = [
        item for item in planned if item.get("operation") == "unchanged_identical_date"
    ]
    if not apply:
        base.update(
            {
                "status": "ready",
                "unchanged_symbol_count": len(unchanged_items),
                "planned_append_symbol_count": len(append_items),
            }
        )
        return _write_payload(output_path, base)

    canonical.mkdir(parents=True, exist_ok=True)
    volume_preflight = _transaction_volume_preflight(canonical)
    base["transaction_volume_preflight"] = volume_preflight
    if volume_preflight["status"] != "passed":
        base.update(
            {
                "status": "blocked",
                "quarantined": True,
                "reason_codes": [str(volume_preflight["reason_code"])],
                "error": str(volume_preflight.get("error") or "")[:1000],
            }
        )
        return _write_payload(output_path, base)

    # Keep transaction files beside (but outside) canonical storage so every
    # replace stays on the verified same filesystem without inheriting deep
    # run paths.
    transaction = (
        Path(str(volume_preflight["transaction_parent"]))
        / f".append_transaction_{uuid.uuid4().hex}"
    )
    candidates = transaction / "candidates"
    backups = transaction / "backups"
    transaction.mkdir(parents=True, exist_ok=False)
    moved_originals: list[tuple[Path, Path]] = []
    new_targets: list[Path] = []
    retain_transaction = False
    transaction_failed = False
    try:
        for item in append_items:
            symbol = str(item["symbol"])
            target = Path(item["canonical_path"])
            candidate = candidates / f"{symbol}.csv"
            candidate.parent.mkdir(parents=True, exist_ok=True)
            if target.exists():
                _append_candidate(
                    target,
                    candidate,
                    list(item.get("_header") or []),
                    item["_frame"],
                )
            else:
                candidate.parent.mkdir(parents=True, exist_ok=True)
                columns = list(STANDARD_COLUMNS) + [key for key in LIFECYCLE_COLUMNS if key in item["_frame"].columns]
                item["_frame"][columns].to_csv(
                    candidate,
                    index=False,
                    encoding="utf-8-sig",
                    lineterminator="\n",
                )

        backups.mkdir(parents=True, exist_ok=True)
        for item in append_items:
            symbol = str(item["symbol"])
            target = Path(item["canonical_path"])
            target.parent.mkdir(parents=True, exist_ok=True)
            candidate = candidates / f"{symbol}.csv"
            if target.exists():
                backup = backups / f"{symbol}.csv"
                os.replace(target, backup)
                moved_originals.append((target, backup))
            else:
                new_targets.append(target)
            os.replace(candidate, target)
    except Exception as exc:  # noqa: BLE001 - rollback is part of the contract
        rollback_errors: list[str] = []
        for target in reversed(new_targets):
            try:
                target.unlink(missing_ok=True)
            except OSError as rollback_exc:
                rollback_errors.append(f"{target}:{rollback_exc}")
        for target, backup in reversed(moved_originals):
            try:
                symbol = target.stem
                expected_hash = next(
                    (
                        str(item.get("canonical_sha256_before") or "")
                        for item in base["items"]
                        if item.get("symbol") == symbol
                    ),
                    "",
                )
                atomic_restore_from_backup(
                    target,
                    backup,
                    expected_sha256=expected_hash,
                )
                if expected_hash and sha256_path(target) != expected_hash:
                    raise ValueError(f"rollback_hash_verification_failed:{symbol}")
            except (OSError, ValueError) as rollback_exc:
                rollback_errors.append(f"{target}:{rollback_exc}")
        retain_transaction = bool(rollback_errors)
        transaction_failed = True
        base.update(
            {
                "status": "failed",
                "quarantined": True,
                "reason_codes": ["append_transaction_failed"],
                "error": f"{type(exc).__name__}:{exc}"[:1000],
                "rollback_performed": not rollback_errors,
                "rollback_verified": not rollback_errors,
                "rollback_errors": rollback_errors,
                "recovery_transaction_path": (
                    str(transaction) if rollback_errors else ""
                ),
            }
        )
    finally:
        if not retain_transaction:
            try:
                if transaction.exists():
                    shutil.rmtree(transaction)
                if transaction.exists():
                    raise OSError("transaction_cleanup_incomplete")
            except Exception as cleanup_exc:  # noqa: BLE001 - surface pending cleanup
                _record_transaction_cleanup_failure(base, transaction, cleanup_exc)

    if transaction_failed:
        return _write_payload(output_path, base)

    by_symbol = {str(item["symbol"]): item for item in base["items"]}
    for item in append_items:
        symbol = str(item["symbol"])
        by_symbol[symbol]["canonical_sha256_after"] = _sha256(
            Path(item["canonical_path"])
        )
    for item in unchanged_items:
        symbol = str(item["symbol"])
        by_symbol[symbol]["canonical_sha256_after"] = by_symbol[symbol][
            "canonical_sha256_before"
        ]
    base.update(
        {
            "status": "completed",
            "applied": True,
            "quarantined": False,
            "appended_symbol_count": len(append_items),
            "unchanged_symbol_count": len(unchanged_items),
            "completed_at": now_text(),
            "rollback_performed": False,
        }
    )
    return _write_payload(output_path, base)


def apply_audited_intraday_batch(
    *,
    staging_dir: Path,
    canonical_dir: Path,
    symbols: Sequence[str],
    target_date: str,
    run_id: str,
    quality_audit: Mapping[str, Any] | None = None,
    output_path: Path | None = None,
    apply: bool = True,
) -> dict[str, Any]:
    """Run one audited append while holding the singleton canonical write lock.

    The 2026-07-19 user ruling section 16 requires every canonical minute
    mutation to share one transaction channel.  For apply runs the lock covers
    canonical preflight, candidate construction, commit, verification, and any
    rollback; acquiring it only around ``os.replace`` permits stale whole-file
    candidates to lose a concurrent append.
    """

    canonical = Path(canonical_dir)
    ensure_path_outside_canonical(
        Path(staging_dir),
        canonical,
        role="append_staging_dir",
    )
    ensure_path_outside_canonical(
        Path(output_path) if output_path is not None else None,
        canonical,
        role="append_output",
    )
    if not apply:
        return _apply_audited_intraday_batch_locked(
            staging_dir=staging_dir,
            canonical_dir=canonical_dir,
            symbols=symbols,
            target_date=target_date,
            run_id=run_id,
            quality_audit=quality_audit,
            output_path=output_path,
            apply=False,
        )
    canonical.mkdir(parents=True, exist_ok=True)
    if canonical.resolve() in getattr(_CANONICAL_TRANSACTION_LOCKS, "paths", ()):
        return _apply_audited_intraday_batch_locked(
            staging_dir=staging_dir,
            canonical_dir=canonical,
            symbols=symbols,
            target_date=target_date,
            run_id=run_id,
            quality_audit=quality_audit,
            output_path=output_path,
            apply=True,
        )
    with interprocess_file_lock(
        canonical_intraday_write_lock_path(canonical),
        timeout_seconds=CANONICAL_WRITE_LOCK_TIMEOUT_SECONDS,
    ):
        return _apply_audited_intraday_batch_locked(
            staging_dir=staging_dir,
            canonical_dir=canonical,
            symbols=symbols,
            target_date=target_date,
            run_id=run_id,
            quality_audit=quality_audit,
            output_path=output_path,
            apply=True,
        )


def build_proven_historical_intraday_import_plan(**kwargs: Any) -> dict[str, Any]:
    """Expose the verdict-bound historical migration through Stage 6."""

    from quantdesk.intraday.stage6_historical_raw_import import (
        build_proven_historical_intraday_import_plan as implementation,
    )

    return implementation(**kwargs)


def apply_proven_historical_intraday_import(**kwargs: Any) -> dict[str, Any]:
    """Apply one verdict-bound historical migration through the Stage 6 lock."""

    from quantdesk.intraday.stage6_historical_raw_import import (
        apply_proven_historical_intraday_import as implementation,
    )

    return implementation(**kwargs)
