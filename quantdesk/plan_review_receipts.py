"""Immutable, local evidence for expiry review and conditional-plan replay.

Reviewing input evidence is not simulation or ledger application.  This module
never changes a plan, minute file, applied execution ID, or paper account.
"""
from __future__ import annotations

import copy
import hashlib
import json
from datetime import date, datetime, time, timezone, timedelta
from pathlib import Path
from typing import Any, Mapping, Sequence

from quantdesk.csv_io import read_csv_frame
from quantdesk.interprocess_lock import interprocess_file_lock
from quantdesk.intraday.minute_storage import resolve_minute_read_path
from quantdesk.price_basis import classify_frame_price_basis
from quantdesk.shared import normalize_symbol

SCHEMA_VERSION = "plan_review_receipt_v2"
VALIDATOR_VERSION = "plan_review_evidence_v1"
STORE_PATH = Path("results/plan_review_receipts")
CN_TIMEZONE = timezone(timedelta(hours=8))
BOUNDARIES = {
    "research_only": True, "paper_only": True, "broker_route": False,
    "ledger_write": False, "market_data_write": False,
}


def _hash(value: Any) -> str:
    return hashlib.sha256(json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False,
    ).encode("utf-8")).hexdigest()


def _sha(value: Any) -> bool:
    return isinstance(value, str) and len(value) == 64 and all(c in "0123456789abcdef" for c in value)


def _day(value: Any) -> str:
    text = str(value or "")
    try:
        return date.fromisoformat(text).isoformat()
    except ValueError:
        return ""


def _timestamp(value: Any) -> datetime | None:
    try:
        result = datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
    except ValueError:
        return None
    return result.astimezone(CN_TIMEZONE) if result.tzinfo is not None else None


def _symbols(plan: Mapping[str, Any]) -> list[str]:
    rows = plan.get("symbols")
    if not isinstance(rows, list):
        return []
    return sorted({
        symbol for row in rows
        if (symbol := normalize_symbol(row.get("symbol") if isinstance(row, Mapping) else row))
    })


def _file_hash(path: Path) -> str | None:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


def _input_path(root: Path, value: Any) -> Path | None:
    try:
        path = (root / str(value or "")).resolve()
        path.relative_to(root)
    except (OSError, RuntimeError, ValueError):
        return None
    return path


def inspect_plan_minute_inputs(
    project_root: Path, plan: Mapping[str, Any], *, intraday_dir: Path | None = None,
) -> list[dict[str, Any]]:
    """Audit the actual original files and bind their bytes before date slicing."""
    from quantdesk.intraday.intraday_quality_audit import audit_intraday_file

    root = Path(project_root).resolve()
    base = Path(intraday_dir) if intraday_dir is not None else root / "data/intraday/1m"
    trade_date = _day(plan.get("trade_date"))
    records: list[dict[str, Any]] = []
    for symbol in _symbols(plan):
        requested_path = (base / f"{symbol}.csv").resolve()
        path = resolve_minute_read_path(requested_path).resolve()
        try:
            relative = path.relative_to(root).as_posix()
        except ValueError:
            raise ValueError("plan review inputs must be under the injected project root") from None
        before = _file_hash(path)
        reasons: list[str] = []
        basis = "mixed_or_unknown"
        audit: dict[str, Any] = {}
        if before is None:
            reasons.append("minute_file_missing_or_unreadable")
        else:
            try:
                original = read_csv_frame(path)
                # Minute files use price_basis; the common classifier consumes
                # adjust. This projection retains every original row/token.
                frame = original.assign(adjust=original["price_basis"]) if "price_basis" in original else original
                basis, basis_reason = classify_frame_price_basis(frame)
                if "price_basis" in original and "adjust" in original:
                    adjust_basis, adjust_reason = classify_frame_price_basis(original)
                    if adjust_reason or adjust_basis != basis:
                        basis, basis_reason = "mixed_or_unknown", "conflicting_price_basis_columns"
                if basis != "raw":
                    reasons.append(basis_reason or "minute_price_basis_not_raw")
                audit = audit_intraday_file(symbol, trade_date, intraday_dir=base)
                if audit.get("formal_execution_eligible") is not True:
                    reasons.extend(str(item) for item in (audit.get("session_reason_codes") or []))
                    reasons.append(f"minute_session_{audit.get('session_status') or audit.get('status') or 'invalid'}")
            except (OSError, ValueError, TypeError, KeyError) as exc:
                reasons.append(f"minute_input_invalid:{type(exc).__name__}")
            if _file_hash(path) != before:
                reasons.append("minute_input_changed_during_review")
        records.append({
            "symbol": symbol, "target_date": trade_date, "path": relative,
            "sha256": before, "price_basis": basis,
            "status": "passed" if not reasons else "evidence_insufficient",
            "target_date_rows": int(audit.get("row_count") or 0),
            "session_status": str(audit.get("session_status") or audit.get("status") or "missing"),
            "complete_session": audit.get("formal_execution_eligible") is True and not reasons,
            "reason_codes": sorted(set(reasons)),
        })
        if path != requested_path:
            records[-1]["requested_path"] = requested_path.relative_to(root).as_posix()
    return records


def _evidence_projection(receipt: Mapping[str, Any]) -> dict[str, Any]:
    return {key: receipt.get(key) for key in (
        "kind", "plan_id", "trade_date", "plan_hash", "validator_version", "inputs",
        "status", "simulation_enabled", "replay_receipt", "reason_codes",
    )}


def _seal(receipt: dict[str, Any]) -> dict[str, Any]:
    receipt["evidence_key"] = _hash(_evidence_projection(receipt))
    receipt["receipt_hash"] = _hash({key: value for key, value in receipt.items() if key != "receipt_hash"})
    return receipt


def build_expiry_review_receipt(
    project_root: Path, plan: Mapping[str, Any], *, checked_at: str,
    simulation_enabled: bool = False, intraday_dir: Path | None = None,
) -> dict[str, Any]:
    """Record what was checked now, including honest missing-data outcomes."""
    from quantdesk.daily_candidate_refresh import _formal_plan_reasons

    plan_copy = copy.deepcopy(dict(plan))
    target = _day(plan_copy.get("trade_date"))
    captured = _timestamp(checked_at)
    if captured is None or not target or not str(plan_copy.get("plan_id") or "").strip():
        raise ValueError("plan review requires a plan identity, exact trade date and aware checked_at")
    due = captured >= datetime.combine(date.fromisoformat(target), time(15), CN_TIMEZONE)
    inputs = inspect_plan_minute_inputs(project_root, plan_copy, intraday_dir=intraday_dir) if due else []
    reasons = list(_formal_plan_reasons(plan_copy, as_of=target, role="due")) if due else ["plan_not_due"]
    if due:
        reasons.extend(f"{row['symbol']}:{reason}" for row in inputs for reason in row["reason_codes"])
        if not inputs:
            reasons.append("plan_symbols_missing")
    status = "not_due" if not due else ("evidence_insufficient" if reasons else "passed")
    return _seal({
        "task_type": "conditional_plan_review_receipt", "schema_version": SCHEMA_VERSION,
        "validator_version": VALIDATOR_VERSION, "kind": "expiry_review", "book_id": "strategy_paper",
        "plan_id": str(plan_copy["plan_id"]), "trade_date": target,
        "execution_id": f"{plan_copy['plan_id']}:{target}", "plan_hash": _hash(plan_copy),
        "checked_at": captured.isoformat(), "status": status,
        "review_completed": due, "review_passed": status == "passed",
        "simulation_enabled": bool(simulation_enabled), "simulation_executed": False,
        "simulation_status": "not_run" if simulation_enabled else "disabled",
        "inputs": inputs, "reason_codes": sorted(set(reasons)),
        "reconstruction": "retrospective_reconstruction" if captured.date().isoformat() > target else "same_session_review",
        "boundaries": dict(BOUNDARIES), "disclaimer": "纸面 · 未验证；本记录只证明所列复核，不是成交。",
    })


def bind_plan_replay_receipt(
    project_root: Path, plan: Mapping[str, Any], replay_receipt: Mapping[str, Any],
    *, checked_at: str, intraday_dir: Path | None = None,
    input_evidence: Sequence[Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """Bind an existing v1 simulation receipt without changing its old API."""
    from quantdesk.daily_candidate_refresh import verify_conditional_replay_receipt

    receipt = build_expiry_review_receipt(
        project_root, plan, checked_at=checked_at, simulation_enabled=True, intraday_dir=intraday_dir,
    )
    nested = copy.deepcopy(dict(replay_receipt))
    errors = verify_conditional_replay_receipt(nested)
    if nested.get("plan_id") != receipt["plan_id"] or nested.get("trade_date") != receipt["trade_date"]:
        errors.append("replay_plan_identity_mismatch")
    if input_evidence is not None and list(input_evidence) != receipt["inputs"]:
        errors.append("replay_input_changed_during_execution")
    receipt.update({
        "kind": "intraday_replay", "replay_receipt": nested,
        "simulation_executed": not errors and receipt["review_completed"],
        "simulation_status": "executed" if not errors and receipt["review_completed"] else "unverified",
    })
    if errors:
        receipt["reason_codes"] = sorted(set([*receipt["reason_codes"], *errors]))
        receipt["status"] = "evidence_insufficient"
        receipt["review_passed"] = False
    return _seal(receipt)


def verify_plan_review_receipt(
    receipt: Mapping[str, Any], *, plan: Mapping[str, Any],
    project_root: Path | None = None, as_of: str = "", verify_current_inputs: bool = True,
) -> list[str]:
    """Validate identity, content, time and actual current source bytes."""
    from quantdesk.daily_candidate_refresh import _formal_plan_reasons, verify_conditional_replay_receipt

    if not isinstance(receipt, Mapping) or receipt.get("schema_version") != SCHEMA_VERSION:
        return ["legacy_unbound"]
    errors: list[str] = []
    target = _day(plan.get("trade_date"))
    plan_id = str(plan.get("plan_id") or "")
    if receipt.get("task_type") != "conditional_plan_review_receipt":
        errors.append("receipt_task_type_invalid")
    if receipt.get("validator_version") != VALIDATOR_VERSION:
        errors.append("receipt_validator_version_invalid")
    if receipt.get("kind") not in {"expiry_review", "intraday_replay"}:
        errors.append("receipt_kind_invalid")
    if receipt.get("book_id") != "strategy_paper":
        errors.append("receipt_book_identity_invalid")
    if (not target or not plan_id or receipt.get("plan_id") != plan_id
            or receipt.get("trade_date") != target or receipt.get("execution_id") != f"{plan_id}:{target}"):
        errors.append("receipt_plan_identity_mismatch")
    try:
        if receipt.get("plan_hash") != _hash(dict(plan)):
            errors.append("receipt_plan_hash_mismatch")
        if not _sha(receipt.get("evidence_key")) or receipt.get("evidence_key") != _hash(_evidence_projection(receipt)):
            errors.append("receipt_evidence_key_mismatch")
        if not _sha(receipt.get("receipt_hash")) or receipt.get("receipt_hash") != _hash({key: value for key, value in receipt.items() if key != "receipt_hash"}):
            errors.append("receipt_hash_mismatch")
    except (ValueError, TypeError):
        errors.append("receipt_hash_invalid")
    checked = _timestamp(receipt.get("checked_at"))
    if checked is None:
        errors.append("receipt_checked_at_invalid")
    elif target:
        due = checked >= datetime.combine(date.fromisoformat(target), time(15), CN_TIMEZONE)
        if receipt.get("review_completed") is not due:
            errors.append("receipt_due_state_mismatch")
        if _day(as_of) and checked.date().isoformat() > as_of:
            errors.append("receipt_checked_after_as_of")
    else:
        due = False
    status = receipt.get("status")
    if status not in {"passed", "evidence_insufficient", "not_due"}:
        errors.append("receipt_status_invalid")
    if receipt.get("review_passed") is not (status == "passed"):
        errors.append("receipt_passed_flag_mismatch")
    if receipt.get("boundaries") != BOUNDARIES:
        errors.append("receipt_boundary_invalid")
    inputs = receipt.get("inputs")
    if not isinstance(inputs, list) or any(not isinstance(item, Mapping) for item in inputs):
        errors.append("receipt_inputs_invalid")
        inputs = []
    expected_symbols = _symbols(plan) if receipt.get("review_completed") is True else []
    if sorted(str(item.get("symbol") or "") for item in inputs) != expected_symbols:
        errors.append("receipt_input_symbols_mismatch")
    root = Path(project_root).resolve() if project_root is not None else None
    input_parents: set[Path] = set()
    for item in inputs:
        if item.get("target_date") != target:
            errors.append("receipt_input_date_mismatch")
        if item.get("status") not in {"passed", "evidence_insufficient"} or not isinstance(item.get("reason_codes"), list):
            errors.append("receipt_input_status_invalid")
        if item.get("sha256") is not None and not _sha(item.get("sha256")):
            errors.append("receipt_input_hash_invalid")
        if item.get("status") == "passed" and (
            not _sha(item.get("sha256")) or item.get("price_basis") != "raw"
            or item.get("complete_session") is not True or item.get("reason_codes")
        ):
            errors.append("receipt_input_not_formal")
        if root is not None and verify_current_inputs:
            path = _input_path(root, item.get("path"))
            if path is None or path == root:
                errors.append("receipt_input_path_invalid")
            elif _file_hash(path) != item.get("sha256"):
                errors.append("receipt_input_changed")
            elif path.name != f"{item.get('symbol')}.csv":
                errors.append("receipt_input_path_invalid")
            else:
                requested = _input_path(root, item.get("requested_path")) if item.get("requested_path") else path
                try:
                    selected = resolve_minute_read_path(requested).resolve() if requested is not None else None
                except (OSError, RuntimeError, ValueError):
                    selected = None
                if requested is None or requested.name != path.name or selected != path:
                    errors.append("receipt_input_path_invalid")
                else:
                    input_parents.add(requested.parent)
    if status == "passed" and (
        not receipt.get("review_completed") or not inputs or receipt.get("reason_codes")
        or any(item.get("status") != "passed" for item in inputs)
    ):
        errors.append("receipt_passed_without_evidence")
    if status == "passed" and target:
        errors.extend(_formal_plan_reasons(plan, as_of=target, role="due"))
    if root is not None and verify_current_inputs and inputs and not errors:
        if len(input_parents) != 1:
            errors.append("receipt_input_directory_mismatch")
        else:
            current = inspect_plan_minute_inputs(root, plan, intraday_dir=next(iter(input_parents)))
            if current != inputs:
                errors.append("receipt_input_evidence_mismatch")
    if not isinstance(receipt.get("reason_codes"), list):
        errors.append("receipt_reason_codes_invalid")
    if receipt.get("kind") == "intraday_replay":
        nested = receipt.get("replay_receipt")
        nested_errors = verify_conditional_replay_receipt(nested if isinstance(nested, Mapping) else {})
        if receipt.get("simulation_executed") is True:
            errors.extend(nested_errors)
            if not isinstance(nested, Mapping) or nested.get("plan_id") != plan_id or nested.get("trade_date") != target:
                errors.append("replay_plan_identity_mismatch")
        elif status == "passed":
            errors.append("receipt_replay_not_executed")
    elif receipt.get("simulation_executed") is not False:
        errors.append("receipt_review_is_not_simulation")
    return sorted(set(errors))


def publish_plan_review_receipt(
    project_root: Path, receipt: Mapping[str, Any], *, plan: Mapping[str, Any],
) -> dict[str, Any]:
    """Persist one evidence version exactly once; never overwrite history."""
    from quantdesk.daily_candidate_refresh import _exclusive_write_json, _read_json_strict

    root = Path(project_root).resolve()
    errors = verify_plan_review_receipt(receipt, plan=plan, project_root=root)
    if errors:
        return {"status": "blocked", "reason_codes": errors, "receipt": dict(receipt)}
    key = hashlib.sha256(str(receipt["plan_id"]).encode("utf-8")).hexdigest()[:24]
    path = root / STORE_PATH / str(receipt["trade_date"]) / key / f"{receipt['evidence_key']}.json"
    try:
        with interprocess_file_lock(path.parent / ".publish.lock", timeout_seconds=0):
            existing, error = _read_json_strict(path)
            if error not in {"", "missing"}:
                return {"status": "blocked", "reason_codes": ["receipt_archive_unreadable"], "path": str(path)}
            if existing is not None:
                invalid = verify_plan_review_receipt(existing, plan=plan, project_root=root)
                if invalid or existing.get("evidence_key") != receipt["evidence_key"]:
                    return {"status": "blocked", "reason_codes": ["receipt_archive_conflict", *invalid], "path": str(path)}
                return {"status": "idempotent", "reason_codes": [], "path": str(path), "receipt": existing}
            _exclusive_write_json(path, receipt)
    except (OSError, TimeoutError) as exc:
        return {"status": "blocked", "reason_codes": [f"receipt_archive_write_failed:{type(exc).__name__}"], "path": str(path)}
    return {"status": "completed", "reason_codes": [], "path": str(path), "receipt": dict(receipt)}


def load_plan_review_receipts(project_root: Path) -> dict[str, Any]:
    """Read immutable records; callers validate each against its actual plan."""
    from quantdesk.daily_candidate_refresh import _read_json_strict

    root = Path(project_root).resolve()
    result: dict[str, Any] = {"review_receipts": [], "replay_receipts": [], "reason_codes": []}
    base = root / STORE_PATH
    if not base.is_dir():
        return result
    for path in sorted(base.glob("*/*/*.json")):
        payload, error = _read_json_strict(path)
        if error or payload is None:
            result["reason_codes"].append(f"receipt_archive_unreadable:{path.relative_to(root).as_posix()}")
            continue
        plan_key = hashlib.sha256(str(payload.get("plan_id") or "").encode("utf-8")).hexdigest()[:24]
        if (path.stem != payload.get("evidence_key") or path.parent.name != plan_key
                or path.parent.parent.name != payload.get("trade_date")):
            result["reason_codes"].append(f"receipt_archive_path_mismatch:{path.relative_to(root).as_posix()}")
            continue
        bucket = "replay_receipts" if payload.get("kind") == "intraday_replay" else "review_receipts"
        result[bucket].append(payload)
    return result


def review_archived_plan(
    project_root: Path, *, trade_date: str, checked_at: str,
    simulation_enabled: bool = False, write: bool = False,
) -> dict[str, Any]:
    """Review the requested due archive only; never backfill old days in a scan."""
    from quantdesk.daily_candidate_refresh import load_archived_conditional_plan

    loaded = load_archived_conditional_plan(project_root, trade_date=trade_date)
    if loaded.get("status") != "passed":
        return {"status": loaded.get("status") or "blocked", "reason_codes": loaded.get("reason_codes") or [], "receipt": None}
    receipt = build_expiry_review_receipt(
        project_root, loaded["plan"], checked_at=checked_at, simulation_enabled=simulation_enabled,
    )
    if write and receipt["review_completed"]:
        return publish_plan_review_receipt(project_root, receipt, plan=loaded["plan"])
    return {"status": "read_only", "reason_codes": [], "receipt": receipt}


def plan_review_snapshot(
    project_root: Path, *, trade_date: str, checked_at: str,
    applied_execution_ids: Sequence[str] = (), simulation_enabled: bool = False,
    write: bool = False, review_current: bool = True,
    extra_replay_receipts: Sequence[Mapping[str, Any]] = (),
    additional_reason_codes: Sequence[str] = (),
) -> dict[str, Any]:
    """The daily/paper adapter: one current review, then a strictly read-only scan."""
    from quantdesk.daily_candidate_refresh import scan_unreplayed_archived_plans

    current = review_archived_plan(
        project_root, trade_date=trade_date, checked_at=checked_at,
        simulation_enabled=simulation_enabled, write=write,
    ) if review_current else {"status": "not_requested", "reason_codes": [], "receipt": None}
    stored = load_plan_review_receipts(project_root)
    reviews = list(stored["review_receipts"])
    replayed = list(stored["replay_receipts"])
    if not write and isinstance(current.get("receipt"), Mapping):
        reviews.append(current["receipt"])
    replayed.extend(extra_replay_receipts)
    checked = _timestamp(checked_at)
    target = _day(trade_date)
    scan_as_of = checked.date().isoformat() if checked is not None else trade_date
    include_today = bool(checked and checked.time().replace(tzinfo=None) >= time(15))
    scan = scan_unreplayed_archived_plans(
        project_root, as_of=scan_as_of, applied_execution_ids=applied_execution_ids,
        review_receipts=reviews, replay_receipts=replayed, include_as_of=include_today,
    )
    summary = dict(scan.get("plan_review_summary") or {"status": "blocked", "reason_codes": scan.get("reason_codes") or []})
    summary["target_trade_date"] = target
    warnings = list(stored["reason_codes"]) + list(current.get("reason_codes") or []) + list(additional_reason_codes)
    if warnings:
        summary["reason_codes"] = sorted(set([*summary.get("reason_codes", []), *warnings]))
        summary["status"] = "attention"
    result = {
        "plan_review_summary": summary, "plan_review_items": scan.get("plan_review_items") or [],
        "plan_review_receipt_archive": {key: current.get(key) for key in ("status", "path", "reason_codes")},
    }
    if write:
        from quantdesk.utils import atomic_write_json

        try:
            atomic_write_json(Path(project_root) / STORE_PATH / "latest.json", result)
        except (OSError, ValueError) as exc:
            summary["status"] = "attention"
            summary["reason_codes"] = [*summary.get("reason_codes", []), f"receipt_summary_write_failed:{type(exc).__name__}"]
    return result


def load_plan_review_snapshot(project_root: Path) -> dict[str, Any]:
    """Read the published review view; never review inputs or write on GET."""
    from quantdesk.shared import read_json_strict

    payload, error = read_json_strict(Path(project_root) / STORE_PATH / "latest.json")
    if error or not isinstance(payload, dict):
        return {"plan_review_summary": {"status": "missing" if error == "missing" else "blocked",
                                        "reason_codes": [f"plan_review_snapshot_{error or 'invalid'}"]},
                "plan_review_items": []}
    return {key: payload.get(key) for key in ("plan_review_summary", "plan_review_items", "plan_review_receipt_archive")}
