"""Materialize research-only tracking metrics from formal candidate batches.

This adapter is intentionally separate from the legacy ``data/tracking`` CSV
pipeline.  It consumes one verified, date-addressed formal candidate batch and
publishes an immutable date snapshot under ``results/candidate_tracking``.
It never reads the mutable candidate-batch ``latest`` pointer, never falls
back to the v420 CSV, and never mutates paper/account or market-data state.
"""

from __future__ import annotations

import csv
import hashlib
import json
import math
import os
import re
from datetime import date
from pathlib import Path
from typing import Any, Mapping

from quantdesk.daily_candidate_refresh import verify_formal_candidate_batch
from quantdesk.price_basis import PRICE_BASIS_RAW, normalize_price_basis
from quantdesk.shared import normalize_symbol
from quantdesk.utils import atomic_write_json

FORMAL_CANDIDATE_TRACKING_SCHEMA_VERSION = "formal_candidate_tracking_v1"
HORIZONS: tuple[int, ...] = (1, 3, 5, 10, 20)
PROJECT_ROOT = Path(__file__).resolve().parents[1]
DISCLAIMER = (
    "Research Only / Paper Account Only / No Broker Route / "
    "Live Execution Locked / Not Investment Advice / Not Position Advice."
)


def _strict_day(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    try:
        parsed = date.fromisoformat(text)
    except ValueError:
        return ""
    return parsed.isoformat() if text == parsed.isoformat() else ""


def _safe_run_id(value: Any) -> str:
    text = str(value or "").strip()
    if (
        not text
        or text in {".", ".."}
        or Path(text).name != text
        or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,199}", text)
    ):
        return ""
    return text


def _is_sha256(value: Any) -> bool:
    text = str(value or "").strip().lower()
    return len(text) == 64 and all(char in "0123456789abcdef" for char in text)


def _stable_hash(value: Any) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _finite_float(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _read_json_object(path: Path) -> tuple[dict[str, Any] | None, str]:
    if not path.exists():
        return None, "missing"
    try:
        text = path.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeError):
        return None, "unreadable"
    if not text.strip():
        return None, "empty"
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return None, "invalid_json"
    if not isinstance(payload, dict) or not payload:
        return None, "invalid_object"
    return payload, ""


def _exclusive_write_json(path: Path, payload: Mapping[str, Any]) -> None:
    """Create one immutable JSON file without replacing existing bytes."""

    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL)
    completed = False
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            descriptor = -1
            json.dump(
                dict(payload),
                handle,
                ensure_ascii=False,
                indent=2,
                allow_nan=False,
            )
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        completed = True
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if not completed:
            try:
                target.unlink(missing_ok=True)
            except OSError:
                pass


def _relative(root: Path, path: Path) -> str:
    return str(path.resolve().relative_to(root.resolve())).replace("\\", "/")


def _blocked(
    *,
    target_date: str,
    reason_codes: list[str],
    path: Path | None = None,
    batch_hash: str = "",
    run_id: str = "",
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "task_type": "formal_candidate_tracking",
        "schema_version": FORMAL_CANDIDATE_TRACKING_SCHEMA_VERSION,
        "status": "blocked",
        "applied": False,
        "target_date": target_date,
        "authoritative_chain_run_id": run_id,
        "batch_provenance_hash": batch_hash,
        "candidate_count": 0,
        "entries": [],
        "reason_codes": list(dict.fromkeys(reason_codes)),
        "warnings": list(dict.fromkeys(reason_codes)),
        "output_path": _relative(Path.cwd(), path) if path and path.exists() else "",
        "research_only": True,
        "paper_account_only": True,
        "broker_route": False,
        "live_execution": False,
        "paper_state_mutated": False,
        "market_data_mutated": False,
        "disclaimer": DISCLAIMER,
    }
    return payload


def _load_formal_batch(
    root: Path, *, target_date: str, authoritative_chain_run_id: str
) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    """Read exactly one dated batch and bind it to the supplied chain run."""

    path = root / "results" / "candidate_refresh" / "daily" / f"{target_date}.json"
    batch, read_error = _read_json_object(path)
    if read_error == "missing":
        return None, _blocked(
            target_date=target_date,
            run_id=authoritative_chain_run_id,
            reason_codes=["formal_candidate_batch_missing"],
        )
    if read_error:
        return None, _blocked(
            target_date=target_date,
            run_id=authoritative_chain_run_id,
            reason_codes=[f"formal_candidate_batch_{read_error}"],
        )
    assert batch is not None
    reasons = verify_formal_candidate_batch(batch)
    if _strict_day(batch.get("target_date")) != target_date:
        reasons.append("formal_candidate_batch_target_date_path_mismatch")
    provenance = batch.get("provenance")
    provenance = provenance if isinstance(provenance, Mapping) else {}
    source_run_id = _safe_run_id(provenance.get("source_run_id"))
    if source_run_id != authoritative_chain_run_id:
        reasons.append("formal_candidate_batch_source_run_mismatch")
    declared_hash = str(provenance.get("hash") or "").strip().lower()
    if not _is_sha256(declared_hash):
        reasons.append("formal_candidate_batch_hash_invalid")
    if reasons:
        return None, _blocked(
            target_date=target_date,
            run_id=authoritative_chain_run_id,
            batch_hash=declared_hash,
            reason_codes=list(dict.fromkeys(reasons)),
        )
    return batch, {}


def _load_raw_history(
    root: Path, symbol: str
) -> tuple[list[dict[str, Any]], list[str], str]:
    """Read one raw canonical daily file without writing or adjusting prices."""

    path = root / "data" / "daily" / f"{symbol}.csv"
    warnings: list[str] = []
    if not path.is_file():
        return [], [f"daily_history_missing:{symbol}"], "missing"
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle)
            fieldnames = list(reader.fieldnames or [])
            if "date" not in fieldnames or "close" not in fieldnames:
                return [], [f"daily_history_schema_invalid:{symbol}"], "blocked"
            if "adjust" not in fieldnames:
                return [], [f"daily_history_price_basis_unknown:{symbol}"], "blocked"
            raw_rows = list(reader)
    except (OSError, UnicodeError, csv.Error) as exc:
        return [], [f"daily_history_unreadable:{symbol}:{type(exc).__name__}"], "blocked"

    basis_values: set[str] = set()
    parsed: list[dict[str, Any]] = []
    for row in raw_rows:
        adjustment = str(row.get("adjust") or "").strip()
        if not adjustment:
            basis_values.add("mixed_or_unknown")
        else:
            basis_values.add(normalize_price_basis(adjustment))
        day = _strict_day(str(row.get("date") or "")[:10])
        close = _finite_float(row.get("close"))
        if not day:
            warnings.append(f"daily_history_date_invalid:{symbol}")
            continue
        if close is None or close <= 0:
            warnings.append(f"daily_history_close_invalid:{symbol}:{day}")
            continue
        row_symbol = normalize_symbol(row.get("symbol") or symbol)
        if row_symbol and row_symbol != symbol:
            warnings.append(f"daily_history_symbol_mismatch:{symbol}:{day}")
            continue
        parsed.append({"date": day, "close": close})

    if basis_values != {PRICE_BASIS_RAW}:
        reason = (
            f"daily_history_price_basis_mixed:{symbol}"
            if len(basis_values) > 1
            else f"daily_history_price_basis_not_raw:{symbol}"
        )
        return [], list(dict.fromkeys(warnings + [reason])), "blocked"

    by_date: dict[str, dict[str, Any]] = {}
    duplicate_date = False
    for row in parsed:
        if row["date"] in by_date:
            duplicate_date = True
            warnings.append(f"daily_history_duplicate_date:{symbol}:{row['date']}")
        by_date[row["date"]] = row
    if duplicate_date:
        return [], list(dict.fromkeys(warnings)), "blocked"
    rows = [by_date[key] for key in sorted(by_date)]
    if not rows:
        return [], list(dict.fromkeys(warnings + [f"daily_history_no_valid_rows:{symbol}"])), "missing"
    return rows, list(dict.fromkeys(warnings)), "passed"


def _entry_tracking(
    root: Path,
    *,
    candidate: Mapping[str, Any],
    target_date: str,
) -> tuple[dict[str, Any], list[str]]:
    symbol = normalize_symbol(candidate.get("symbol"))
    warnings: list[str] = []
    history, history_warnings, history_status = _load_raw_history(root, symbol)
    warnings.extend(history_warnings)
    target_row = next((row for row in history if row["date"] == target_date), None)
    local_entry = target_row["close"] if target_row else None
    declared_entry = _finite_float(candidate.get("close"))
    if (
        local_entry is not None
        and declared_entry is not None
        and abs(local_entry - declared_entry) > max(1e-8, abs(local_entry) * 1e-6)
    ):
        warnings.append(f"candidate_close_mismatch_local_daily:{symbol}")
    future = [row for row in history if row["date"] > target_date]
    horizons: dict[str, dict[str, Any]] = {}
    for horizon in HORIZONS:
        key = f"{horizon}d"
        if history_status == "blocked":
            horizons[key] = {"return": None, "status": "blocked", "as_of": None}
            continue
        if target_row is None:
            horizons[key] = {"return": None, "status": "missing", "as_of": None}
            continue
        if len(future) < horizon:
            horizons[key] = {
                "return": None,
                "status": "pending",
                "as_of": future[-1]["date"] if future else target_date,
            }
            continue
        observed = future[horizon - 1]
        result = (observed["close"] - local_entry) / local_entry if local_entry else None
        horizons[key] = {
            "return": result if result is not None and math.isfinite(result) else None,
            "status": "mature" if result is not None and math.isfinite(result) else "missing",
            "as_of": observed["date"],
        }

    mature_count = sum(item["status"] == "mature" for item in horizons.values())
    if history_status == "blocked":
        sample_status = "blocked"
    elif target_row is None:
        sample_status = "missing"
    elif mature_count == len(HORIZONS):
        sample_status = "complete"
    elif mature_count:
        sample_status = "partial"
    else:
        sample_status = "pending"
    if any(item["status"] == "pending" for item in horizons.values()):
        warnings.append(f"horizon_samples_pending:{symbol}")
    hold_5 = horizons["5d"]["return"]
    hit_flag = None if hold_5 is None else int(hold_5 > 0)
    entry = {
        "symbol": symbol,
        "name": str(candidate.get("name") or symbol),
        "strategy_type": str(candidate.get("strategy_type") or ""),
        "strategy_label": str(candidate.get("strategy_label") or ""),
        "rank": candidate.get("rank"),
        "score": candidate.get("score"),
        "candidate_source": str(candidate.get("candidate_source") or ""),
        "target_date": target_date,
        "candidate": dict(candidate),
        "entry_price": local_entry,
        "entry_price_source": "local_daily_target_date_close" if local_entry is not None else None,
        "price_basis": PRICE_BASIS_RAW if history_status == "passed" else "mixed_or_unknown",
        "sample_status": sample_status,
        "horizons": horizons,
        "hit_flag": hit_flag,
        "warnings": list(dict.fromkeys(warnings)),
    }
    return entry, warnings


def _artifact_projection(payload: Mapping[str, Any]) -> dict[str, Any]:
    excluded = {
        "artifact_hash",
        "publish_status",
        "applied",
        "dated_path",
        "latest_path",
        "output_path",
    }
    return {key: value for key, value in payload.items() if key not in excluded}


def _verify_tracking_artifact(payload: Mapping[str, Any]) -> list[str]:
    reasons: list[str] = []
    if payload.get("schema_version") != FORMAL_CANDIDATE_TRACKING_SCHEMA_VERSION:
        reasons.append("candidate_tracking_schema_invalid")
    declared = str(payload.get("artifact_hash") or "").lower()
    if not _is_sha256(declared):
        reasons.append("candidate_tracking_artifact_hash_invalid")
    elif declared != _stable_hash(_artifact_projection(payload)):
        reasons.append("candidate_tracking_artifact_hash_mismatch")
    if not _strict_day(payload.get("target_date")):
        reasons.append("candidate_tracking_target_date_invalid")
    if not _is_sha256(payload.get("batch_provenance_hash")):
        reasons.append("candidate_tracking_batch_hash_invalid")
    if not _safe_run_id(payload.get("authoritative_chain_run_id")):
        reasons.append("candidate_tracking_run_id_invalid")
    return list(dict.fromkeys(reasons))


def _publish(
    root: Path, *, target_date: str, artifact: Mapping[str, Any]
) -> dict[str, Any]:
    base = root / "results" / "candidate_tracking"
    dated = base / "daily" / f"{target_date}.json"
    latest = base / "latest.json"
    expected_hash = str(artifact.get("artifact_hash") or "").lower()

    existing, read_error = _read_json_object(dated)
    if read_error not in {"", "missing"}:
        return {
            "status": "blocked",
            "applied": False,
            "publish_status": "blocked",
            "reason_codes": ["candidate_tracking_dated_snapshot_unreadable"],
        }
    if existing is not None:
        existing_reasons = _verify_tracking_artifact(existing)
        if existing_reasons or str(existing.get("artifact_hash") or "").lower() != expected_hash:
            return {
                "status": "blocked",
                "applied": False,
                "publish_status": "blocked",
                "reason_codes": ["candidate_tracking_dated_snapshot_conflict"],
            }
        dated_status = "idempotent"
    else:
        try:
            _exclusive_write_json(dated, artifact)
        except FileExistsError:
            raced, raced_error = _read_json_object(dated)
            if (
                raced_error
                or raced is None
                or _verify_tracking_artifact(raced)
                or str(raced.get("artifact_hash") or "").lower() != expected_hash
            ):
                return {
                    "status": "blocked",
                    "applied": False,
                    "publish_status": "blocked",
                    "reason_codes": ["candidate_tracking_dated_snapshot_conflict"],
                }
            dated_status = "idempotent"
        except (OSError, TypeError, ValueError) as exc:
            return {
                "status": "blocked",
                "applied": False,
                "publish_status": "blocked",
                "reason_codes": [f"candidate_tracking_dated_snapshot_write_failed:{type(exc).__name__}"],
            }
        else:
            dated_status = "created"

    latest_existing, latest_error = _read_json_object(latest)
    if latest_error not in {"", "missing"}:
        return {
            "status": "blocked",
            "applied": False,
            "publish_status": "blocked",
            "reason_codes": ["candidate_tracking_latest_unreadable"],
        }
    if latest_existing is not None:
        latest_reasons = _verify_tracking_artifact(latest_existing)
        if latest_reasons:
            return {
                "status": "blocked",
                "applied": False,
                "publish_status": "blocked",
                "reason_codes": ["candidate_tracking_latest_invalid"],
            }
        latest_date = _strict_day(latest_existing.get("target_date"))
        latest_hash = str(latest_existing.get("artifact_hash") or "").lower()
        if latest_date > target_date:
            return {
                "status": "blocked",
                "applied": False,
                "publish_status": "blocked",
                "reason_codes": ["candidate_tracking_latest_regression"],
            }
        if latest_date == target_date and latest_hash != expected_hash:
            return {
                "status": "blocked",
                "applied": False,
                "publish_status": "blocked",
                "reason_codes": ["candidate_tracking_latest_same_date_conflict"],
            }
    latest_same = bool(
        latest_existing
        and _strict_day(latest_existing.get("target_date")) == target_date
        and str(latest_existing.get("artifact_hash") or "").lower() == expected_hash
    )
    if not latest_same:
        try:
            atomic_write_json(latest, artifact)
        except RuntimeError as exc:
            return {
                "status": "blocked",
                "applied": False,
                "publish_status": "blocked",
                "reason_codes": [f"candidate_tracking_latest_write_failed:{type(exc).__name__}"],
            }
    return {
        "status": str(artifact.get("status") or "completed"),
        "applied": True,
        "publish_status": "idempotent" if dated_status == "idempotent" and latest_same else "created",
        "dated_path": _relative(root, dated),
        "latest_path": _relative(root, latest),
        "output_path": _relative(root, dated),
        "reason_codes": [],
    }


def materialize_formal_candidate_tracking(
    project_root: Path,
    *,
    target_date: str,
    authoritative_chain_run_id: str,
) -> dict[str, Any]:
    """Materialize one verified formal candidate batch into dated metrics.

    ``target_date`` and ``authoritative_chain_run_id`` are required bindings;
    malformed or mismatched inputs return a blocked payload and write nothing.
    Performance is measured from the local raw close on ``target_date`` to the
    close after N subsequent local daily rows.  An immature horizon remains
    ``return=None`` and is labelled ``pending``.
    """

    root = Path(project_root).resolve()
    normalized_date = _strict_day(target_date)
    run_id = _safe_run_id(authoritative_chain_run_id)
    if not normalized_date:
        return _blocked(
            target_date="",
            run_id=run_id,
            reason_codes=["formal_candidate_tracking_target_date_invalid"],
        )
    if not run_id:
        return _blocked(
            target_date=normalized_date,
            reason_codes=["formal_candidate_tracking_run_id_invalid"],
        )

    batch, blocked = _load_formal_batch(
        root,
        target_date=normalized_date,
        authoritative_chain_run_id=run_id,
    )
    if batch is None:
        return blocked

    provenance = batch.get("provenance")
    provenance = provenance if isinstance(provenance, Mapping) else {}
    batch_hash = str(provenance.get("hash") or "").lower()
    candidates = batch.get("candidates")
    candidates = candidates if isinstance(candidates, list) else []
    entries: list[dict[str, Any]] = []
    warnings: list[str] = [
        f"formal_batch:{reason}"
        for reason in (batch.get("reason_codes") or [])
        if str(reason)
    ]
    for candidate in candidates:
        if not isinstance(candidate, Mapping):
            warnings.append("formal_batch_candidate_not_object")
            continue
        entry, entry_warnings = _entry_tracking(
            root,
            candidate=candidate,
            target_date=normalized_date,
        )
        entries.append(entry)
        warnings.extend(entry_warnings)
    if not entries:
        warnings.append("formal_batch_no_candidates")

    artifact: dict[str, Any] = {
        "task_type": "formal_candidate_tracking",
        "schema_version": FORMAL_CANDIDATE_TRACKING_SCHEMA_VERSION,
        "status": "completed_with_warnings" if warnings else "completed",
        "target_date": normalized_date,
        "authoritative_chain_run_id": run_id,
        "batch_provenance_hash": batch_hash,
        "candidate_count": len(entries),
        "entries": entries,
        "horizons": [f"{horizon}d" for horizon in HORIZONS],
        "return_definition": "(raw_close_T_plus_N - raw_close_T) / raw_close_T",
        "price_basis": PRICE_BASIS_RAW,
        "warnings": list(dict.fromkeys(warnings)),
        "research_only": True,
        "paper_account_only": True,
        "broker_route": False,
        "live_execution": False,
        "paper_state_mutated": False,
        "market_data_mutated": False,
        "disclaimer": DISCLAIMER,
    }
    artifact["artifact_hash"] = _stable_hash(_artifact_projection(artifact))
    published = _publish(root, target_date=normalized_date, artifact=artifact)
    result = dict(artifact)
    result.update(published)
    return result


__all__ = [
    "FORMAL_CANDIDATE_TRACKING_SCHEMA_VERSION",
    "HORIZONS",
    "materialize_formal_candidate_tracking",
]
