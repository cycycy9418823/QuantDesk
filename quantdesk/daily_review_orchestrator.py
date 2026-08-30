"""Fail-closed coordination for one offline evening research review."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable
from uuid import uuid4

from quantdesk.interprocess_lock import interprocess_file_lock
from quantdesk.paper.stage4_final_evaluator import (
    validate_stage4_final_evaluation_pair as _validate_stage4_final_pair,
)
from quantdesk.utils import atomic_write_json

RECEIPT_SCHEMA_VERSION = 1
REVIEW_TRIGGERS = {"manual", "scheduled_daily_review"}
KNOWN_STATES = {"running", "daily_completed", "completed", "retryable_failed"}
STAGE4_EVIDENCE_SCHEMA_VERSION = "stage4_maintenance_evidence_v1"
STAGE4_ACCOUNT_REPLAY_STATES = {
    "not_authorized",
    "observed_real_pending_maintenance_gates",
    "enabled_real",
}
STAGE4_MAINTENANCE_STATES = {
    "maintenance_window_incomplete",
    "maintenance_window_ready",
}
STAGE4_TERMINAL_STATES = {
    "completed",
    "terminal_replay_blocked",
    "terminal_replay_ready",
    "closed_inconclusive",
}
DISCLAIMER = (
    "Research Only / Paper Account Only / No Broker Route / Live Execution Locked / "
    "Not Investment Advice / Not Position Advice."
)

CommandRunner = Callable[[], int]
CandidateTrackingRunner = Callable[[], int]
Stage4Readiness = Callable[[Path, str], bool]
Stage4Satisfied = Callable[[Path, str], bool]
Clock = Callable[[], datetime]


def build_review_event_key(target_date: str, authoritative_chain_run_id: str) -> str:
    day = str(target_date or "").strip()
    run_id = str(authoritative_chain_run_id or "").strip()
    if not day or not run_id:
        raise ValueError("target_date and authoritative_chain_run_id are required")
    parsed = datetime.fromisoformat(day)
    if parsed.date().isoformat() != day:
        raise ValueError("target_date must be an ISO calendar date")
    return f"review:{day}:{run_id}"


def review_receipt_path(
    project_root: Path,
    target_date: str,
    authoritative_chain_run_id: str,
) -> Path:
    event_key = build_review_event_key(target_date, authoritative_chain_run_id)
    digest = hashlib.sha256(event_key.encode("utf-8")).hexdigest()[:20]
    return (
        Path(project_root).resolve()
        / "state"
        / "daily_review_runs"
        / f"{target_date}-{digest}.json"
    )


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _clock(source: datetime | Clock | None) -> Clock:
    if callable(source):
        return lambda: _as_utc(source())
    if isinstance(source, datetime):
        fixed = _as_utc(source)
        return lambda: fixed
    return lambda: datetime.now(timezone.utc)


def _timestamp(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return _as_utc(datetime.fromisoformat(text.replace("Z", "+00:00")))
    except ValueError:
        return None


def _read_json_object(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"daily_review_receipt_unreadable:{type(exc).__name__}") from exc
    if not isinstance(payload, dict):
        raise RuntimeError("daily_review_receipt_invalid_object")
    return payload


def _validate_receipt(
    receipt: dict[str, Any],
    *,
    event_key: str,
    target_date: str,
    chain_run_id: str,
) -> None:
    if receipt.get("schema_version") != RECEIPT_SCHEMA_VERSION:
        raise RuntimeError("daily_review_receipt_schema_invalid")
    if receipt.get("event_key") != event_key:
        raise RuntimeError("daily_review_receipt_event_key_mismatch")
    if receipt.get("target_date") != target_date:
        raise RuntimeError("daily_review_receipt_target_date_mismatch")
    if receipt.get("authoritative_chain_run_id") != chain_run_id:
        raise RuntimeError("daily_review_receipt_chain_run_id_mismatch")
    trigger = receipt.get("trigger")
    if trigger is not None and str(trigger) not in REVIEW_TRIGGERS:
        raise RuntimeError("daily_review_receipt_trigger_invalid")
    state = receipt.get("state")
    if state not in KNOWN_STATES:
        raise RuntimeError("daily_review_receipt_state_invalid")

    timestamp_fields = (
        "claimed_at",
        "lease_expires_at",
        "daily_completed_at",
        "candidate_tracking_claimed_at",
        "candidate_tracking_completed_at",
        "stage4_claimed_at",
        "stage4_lease_expires_at",
        "stage4_completed_at",
    )
    for field in timestamp_fields:
        raw_value = receipt.get(field)
        if raw_value not in (None, "") and _timestamp(raw_value) is None:
            raise RuntimeError("daily_review_receipt_timestamp_invalid")

    claimed_at = _timestamp(receipt.get("claimed_at"))
    if not receipt.get("claim_id") or claimed_at is None:
        raise RuntimeError("daily_review_receipt_claim_invalid")

    daily_completed_at = _timestamp(receipt.get("daily_completed_at"))
    daily_completed = daily_completed_at is not None
    daily_returncode = receipt.get("daily_returncode")
    if daily_completed != (daily_returncode == 0):
        raise RuntimeError("daily_review_receipt_daily_completion_inconsistent")
    if daily_completed_at is not None and claimed_at > daily_completed_at:
        raise RuntimeError("daily_review_receipt_timestamp_order_invalid")

    active_step = receipt.get("active_step")
    lease_expires_at = _timestamp(receipt.get("lease_expires_at"))
    stage4_claimed_at = _timestamp(receipt.get("stage4_claimed_at"))
    stage4_lease_expires_at = _timestamp(receipt.get("stage4_lease_expires_at"))
    stage4_completed_at = _timestamp(receipt.get("stage4_completed_at"))
    if state != "running":
        if active_step is not None:
            raise RuntimeError("daily_review_receipt_active_step_not_cleared")
        if receipt.get("lease_expires_at") not in {None, ""}:
            raise RuntimeError("daily_review_receipt_lease_not_cleared")
        if receipt.get("stage4_lease_expires_at") not in {None, ""}:
            raise RuntimeError("daily_review_receipt_stage4_lease_not_cleared")

    if state == "running":
        if active_step not in {"daily", "candidate_tracking", "stage4"}:
            raise RuntimeError("daily_review_receipt_active_step_invalid")
        if (
            receipt.get("failed_step") is not None
            or receipt.get("reconciliation_required") is not False
            or receipt.get("returncode") is not None
        ):
            raise RuntimeError("daily_review_receipt_running_fields_invalid")
        if lease_expires_at is None:
            raise RuntimeError("daily_review_receipt_lease_invalid")
        if active_step == "daily" and lease_expires_at <= claimed_at:
            raise RuntimeError("daily_review_receipt_lease_invalid")
        if active_step == "daily" and daily_completed:
            raise RuntimeError("daily_review_receipt_running_daily_inconsistent")
        if active_step == "daily" and any(
            value is not None
            for value in (
                receipt.get("candidate_tracking_claimed_at"),
                receipt.get("candidate_tracking_completed_at"),
                receipt.get("candidate_tracking_returncode"),
                stage4_claimed_at,
                stage4_lease_expires_at,
                stage4_completed_at,
                receipt.get("stage4_returncode"),
            )
        ):
            raise RuntimeError("daily_review_receipt_running_daily_inconsistent")
        if active_step == "candidate_tracking":
            if not daily_completed:
                raise RuntimeError("daily_review_receipt_running_tracking_inconsistent")
            tracking_claimed_at = _timestamp(
                receipt.get("candidate_tracking_claimed_at")
            )
            if tracking_claimed_at is None or lease_expires_at is None:
                raise RuntimeError("daily_review_receipt_tracking_claim_invalid")
            if lease_expires_at <= tracking_claimed_at:
                raise RuntimeError("daily_review_receipt_tracking_lease_invalid")
            if any(
                value is not None
                for value in (
                    receipt.get("candidate_tracking_completed_at"),
                    receipt.get("candidate_tracking_returncode"),
                    stage4_claimed_at,
                    stage4_lease_expires_at,
                    stage4_completed_at,
                    receipt.get("stage4_returncode"),
                )
            ):
                raise RuntimeError("daily_review_receipt_running_tracking_inconsistent")
        if active_step == "stage4":
            if not daily_completed:
                raise RuntimeError("daily_review_receipt_running_stage4_inconsistent")
            if stage4_claimed_at is None:
                raise RuntimeError("daily_review_receipt_stage4_claim_invalid")
            if stage4_lease_expires_at is None:
                raise RuntimeError("daily_review_receipt_stage4_lease_invalid")
            if stage4_lease_expires_at != lease_expires_at:
                raise RuntimeError("daily_review_receipt_stage4_lease_mismatch")
            if stage4_lease_expires_at <= stage4_claimed_at:
                raise RuntimeError("daily_review_receipt_stage4_lease_invalid")
            if daily_completed_at > stage4_claimed_at:
                raise RuntimeError("daily_review_receipt_timestamp_order_invalid")
            if stage4_completed_at is not None or receipt.get("stage4_returncode") is not None:
                raise RuntimeError("daily_review_receipt_running_stage4_inconsistent")
    elif state == "daily_completed":
        if not daily_completed:
            raise RuntimeError("daily_review_receipt_daily_completion_missing")
        if any(
            value is not None
            for value in (
                stage4_claimed_at,
                stage4_completed_at,
                receipt.get("stage4_returncode"),
            )
        ) or receipt.get("failed_step") is not None or receipt.get(
            "reconciliation_required"
        ) is not False or receipt.get("returncode") != 0:
            raise RuntimeError("daily_review_receipt_daily_completed_fields_invalid")
    elif state == "completed":
        if not daily_completed or stage4_completed_at is None:
            raise RuntimeError("daily_review_receipt_completion_missing")
        if receipt.get("stage4_returncode") != 0:
            raise RuntimeError("daily_review_receipt_stage4_completion_inconsistent")
        if receipt.get("failed_step") is not None or receipt.get(
            "reconciliation_required"
        ) is not False or receipt.get("returncode") != 0:
            raise RuntimeError("daily_review_receipt_completion_fields_invalid")
        if stage4_claimed_at is None:
            if not str(receipt.get("stage4_skipped_reason") or "").strip():
                raise RuntimeError("daily_review_receipt_stage4_claim_invalid")
        elif (
            daily_completed_at > stage4_claimed_at
            or stage4_claimed_at > stage4_completed_at
        ):
            raise RuntimeError("daily_review_receipt_timestamp_order_invalid")
    elif state == "retryable_failed":
        failed_step = receipt.get("failed_step")
        if failed_step not in {"daily", "candidate_tracking", "stage4"}:
            raise RuntimeError("daily_review_receipt_failed_step_invalid")
        if failed_step == "daily":
            if daily_completed or receipt.get("reconciliation_required") is not True:
                raise RuntimeError("daily_review_receipt_daily_failure_inconsistent")
            if any(
                value is not None
                for value in (
                    stage4_claimed_at,
                    stage4_completed_at,
                    receipt.get("stage4_returncode"),
                )
            ):
                raise RuntimeError("daily_review_receipt_daily_failure_inconsistent")
        elif failed_step == "candidate_tracking":
            if (
                not daily_completed
                or daily_returncode != 0
                or receipt.get("reconciliation_required") is not False
                or receipt.get("candidate_tracking_completed_at") is not None
                or not isinstance(receipt.get("candidate_tracking_returncode"), int)
                or receipt.get("candidate_tracking_returncode") == 0
                or receipt.get("returncode") != receipt.get("candidate_tracking_returncode")
                or stage4_claimed_at is not None
                or stage4_completed_at is not None
                or receipt.get("stage4_returncode") is not None
            ):
                raise RuntimeError("daily_review_receipt_tracking_failure_inconsistent")
        else:
            if (
                not daily_completed
                or receipt.get("reconciliation_required") is not False
                or stage4_claimed_at is None
                or stage4_completed_at is not None
                or not isinstance(receipt.get("stage4_returncode"), int)
                or receipt.get("stage4_returncode") == 0
                or receipt.get("returncode") != receipt.get("stage4_returncode")
            ):
                raise RuntimeError("daily_review_receipt_stage4_failure_inconsistent")
            if daily_completed_at > stage4_claimed_at:
                raise RuntimeError("daily_review_receipt_timestamp_order_invalid")


def _read_receipt(
    path: Path,
    *,
    event_key: str,
    target_date: str,
    chain_run_id: str,
) -> dict[str, Any]:
    if not path.exists():
        return {}
    receipt = _read_json_object(path)
    _validate_receipt(
        receipt,
        event_key=event_key,
        target_date=target_date,
        chain_run_id=chain_run_id,
    )
    return receipt


def _write_receipt(path: Path, payload: dict[str, Any]) -> None:
    atomic_write_json(path, payload)


def _receipts_for_date(
    project_root: Path, target_date: str
) -> list[tuple[Path, dict[str, Any]]]:
    directory = Path(project_root).resolve() / "state" / "daily_review_runs"
    receipts: list[tuple[Path, dict[str, Any]]] = []
    for path in sorted(directory.glob(f"{target_date}-*.json")):
        payload = _read_json_object(path)
        run_id = str(payload.get("authoritative_chain_run_id") or "")
        event_key = str(payload.get("event_key") or "")
        _validate_receipt(
            payload,
            event_key=event_key,
            target_date=target_date,
            chain_run_id=run_id,
        )
        if event_key != build_review_event_key(target_date, run_id):
            raise RuntimeError("daily_review_receipt_event_key_mismatch")
        receipts.append((path, payload))
    return receipts


def _select_date_receipt(
    project_root: Path,
    target_date: str,
    current_path: Path,
) -> tuple[Path, dict[str, Any]]:
    receipts = _receipts_for_date(project_root, target_date)
    ambiguous = [
        item
        for item in receipts
        if (
            item[1].get("state") == "running"
            and item[1].get("active_step") == "daily"
        )
        or (
            item[1].get("state") == "retryable_failed"
            and item[1].get("failed_step") == "daily"
            and item[1].get("reconciliation_required") is True
        )
    ]
    if len(ambiguous) > 1:
        raise RuntimeError("daily_review_date_receipt_conflict")
    if ambiguous:
        return ambiguous[0]

    daily_completed = [
        item for item in receipts if _timestamp(item[1].get("daily_completed_at"))
    ]
    if len(daily_completed) > 1:
        raise RuntimeError("daily_review_date_receipt_conflict")
    if daily_completed:
        return daily_completed[0]
    for item in receipts:
        if item[0] == current_path:
            return item
    return current_path, {}


def _reconcile_expired_prior_daily_receipts(
    project_root: Path,
    current_target_date: str,
    clock: Clock,
) -> list[Path]:
    """Fail-closed sweep for abandoned daily claims from earlier dates.

    A later natural event may be the first process that can observe an old
    lease.  Reconcile only an expired ``running/daily`` receipt under its own
    date lock; never replay the old child or manufacture a shadow observation.
    """

    directory = Path(project_root).resolve() / "state" / "daily_review_runs"
    if not directory.is_dir():
        return []
    try:
        current_day = datetime.fromisoformat(current_target_date).date()
    except ValueError as exc:
        raise RuntimeError("daily_review_receipt_target_date_invalid") from exc

    reconciled: list[Path] = []
    for path in sorted(directory.glob("????-??-??-*.json")):
        match = re.match(r"^(\d{4}-\d{2}-\d{2})-[^/]+\.json$", path.name)
        if not match:
            continue
        receipt_date = match.group(1)
        try:
            receipt_day = datetime.fromisoformat(receipt_date).date()
        except ValueError as exc:
            raise RuntimeError("daily_review_receipt_target_date_invalid") from exc
        if receipt_day >= current_day:
            continue

        date_lock_path = directory / f"{receipt_date}.lock"
        lock = interprocess_file_lock(date_lock_path, timeout_seconds=0)
        try:
            lock.__enter__()
        except TimeoutError:
            # A live owner keeps authority over that date.  The next natural
            # run may retry the sweep after the owner releases the lock.
            continue
        try:
            receipts = _receipts_for_date(project_root, receipt_date)
            ambiguous = [
                item
                for item in receipts
                if (
                    item[1].get("state") == "running"
                    and item[1].get("active_step") == "daily"
                )
                or (
                    item[1].get("state") == "retryable_failed"
                    and item[1].get("failed_step") == "daily"
                    and item[1].get("reconciliation_required") is True
                )
            ]
            if len(ambiguous) > 1 or (
                ambiguous
                and any(
                    item[1].get("state") == "completed"
                    or (
                        item[1].get("state") == "running"
                        and item[1].get("active_step") != "daily"
                    )
                    for item in receipts
                )
            ):
                raise RuntimeError("daily_review_date_receipt_conflict")
            if not ambiguous:
                continue
            selected_path, receipt = ambiguous[0]
            if (
                receipt.get("state") != "running"
                or receipt.get("active_step") != "daily"
            ):
                continue
            lease = _timestamp(receipt.get("lease_expires_at"))
            if lease is None:
                raise RuntimeError("daily_review_receipt_lease_invalid")
            now = _as_utc(clock())
            if lease > now:
                continue
            _manual_reconciliation(receipt, selected_path, now)
            reconciled.append(selected_path)
        finally:
            lock.__exit__(None, None, None)
    return reconciled


def _safe_run(runner: CommandRunner) -> tuple[int, str]:
    try:
        return int(runner()), ""
    except Exception as exc:
        return 99, f"{type(exc).__name__}:{exc}"[:1000]


def _stage4_runtime_error(payload: dict[str, Any]) -> bool:
    reasons = payload.get("reason_codes")
    if not isinstance(reasons, list) or any(not isinstance(item, str) for item in reasons):
        return True
    return bool(
        "stage4_replay_adapter_error" in reasons
        or str(payload.get("adapter_error_type") or "").strip()
        or str(payload.get("runtime_error") or "").strip()
        or str(payload.get("error") or "").strip()
    )


def _stage4_milestone_dates(project_root: Path) -> tuple[str, str] | None:
    path = (
        Path(project_root).resolve()
        / "results"
        / "data_health"
        / "round7_forward_pit_readiness_latest.json"
    )
    try:
        payload = _read_json_object(path)
    except RuntimeError:
        return None
    milestones = payload.get("milestones")
    if not isinstance(milestones, list):
        return None

    def milestone_date(prefix: str) -> str:
        for item in milestones:
            if isinstance(item, dict) and str(
                item.get("relative_to_t0") or ""
            ).startswith(prefix):
                value = str(item.get("calendar_date") or "")
                try:
                    return datetime.fromisoformat(value).date().isoformat()
                except ValueError:
                    return ""
        return ""

    t0_plus_8 = milestone_date("T0+8")
    t0_plus_18 = milestone_date("T0+18")
    return (t0_plus_8, t0_plus_18) if t0_plus_8 and t0_plus_18 else None


def _validate_stage4_payload(
    payload: dict[str, Any],
    target_date: str,
    *,
    t0_plus_8_date: str,
    t0_plus_18_date: str,
) -> tuple[bool, str]:
    if payload.get("schema_version") != STAGE4_EVIDENCE_SCHEMA_VERSION:
        return False, "stage4_evidence_schema_invalid"
    if payload.get("task_type") != "stage4_maintenance_evidence" or payload.get(
        "as_of"
    ) != target_date:
        return False, "stage4_evidence_identity_invalid"
    try:
        normalized_date = datetime.fromisoformat(target_date).date().isoformat()
    except ValueError:
        return False, "stage4_evidence_identity_invalid"
    if normalized_date != target_date or target_date < t0_plus_8_date:
        return False, "stage4_evidence_before_t0_plus_8"
    if _stage4_runtime_error(payload):
        return False, "stage4_evidence_runtime_error"
    for field, expected in (
        ("synthetic_data_used", False),
        ("conclusion_drawn", False),
        ("research_only", True),
        ("paper_only", True),
        ("no_broker", True),
        ("is_real_order", False),
    ):
        if payload.get(field) is not expected:
            return False, f"stage4_evidence_{field}_invalid"
    account_replay = payload.get("account_replay")
    if not isinstance(account_replay, str) or account_replay not in (
        STAGE4_ACCOUNT_REPLAY_STATES
    ):
        return False, "stage4_evidence_account_replay_invalid"
    if account_replay in {"observed_real_pending_maintenance_gates", "enabled_real"}:
        account_results = payload.get("account_results")
        if (
            not isinstance(account_results, dict)
            or account_results.get("quality_gate_passed") is not True
        ):
            return False, "stage4_evidence_account_results_invalid"
        if account_replay == "enabled_real":
            allowed_account_milestones = {"T0+4", "T0+8"}
            if target_date >= t0_plus_18_date:
                allowed_account_milestones.add("T0+18")
            if (
                payload.get("formal_evidence_eligible") is not True
                or account_results.get("as_of") != target_date
                or account_results.get("milestone") not in allowed_account_milestones
            ):
                return False, "stage4_evidence_account_results_invalid"
            for field in (
                "account_candidate_counts",
                "account_gate_pass_counts",
                "account_would_fill_counts",
            ):
                counts = account_results.get(field)
                if (
                    not isinstance(counts, dict)
                    or set(counts) != {"A", "B", "C"}
                    or any(
                        isinstance(value, bool)
                        or not isinstance(value, int)
                        or value < 0
                        for value in counts.values()
                    )
                ):
                    return False, "stage4_evidence_account_results_invalid"
        elif payload.get("formal_evidence_eligible") is not False:
            return False, "stage4_evidence_account_results_invalid"
    status = str(payload.get("status") or "")
    allowed = (
        STAGE4_TERMINAL_STATES
        if target_date >= t0_plus_18_date
        else STAGE4_MAINTENANCE_STATES
    )
    if status not in allowed:
        return False, "stage4_evidence_status_window_invalid"
    return True, ""


def validate_stage4_evidence_pair(
    project_root: Path, target_date: str
) -> tuple[bool, str]:
    milestones = _stage4_milestone_dates(project_root)
    if milestones is None:
        return False, "stage4_milestones_missing_or_invalid"
    base = Path(project_root).resolve() / "results" / "data_health"
    dated = base / f"stage4_maintenance_evidence_{target_date}.json"
    latest = base / "stage4_maintenance_evidence_latest.json"
    try:
        dated_payload = _read_json_object(dated)
        latest_payload = _read_json_object(latest)
    except RuntimeError:
        return False, "stage4_evidence_missing_or_unreadable"
    if dated_payload != latest_payload:
        return False, "stage4_evidence_latest_mismatch"
    return _validate_stage4_payload(
        dated_payload,
        target_date,
        t0_plus_8_date=milestones[0],
        t0_plus_18_date=milestones[1],
    )


def validate_stage4_final_evaluation_pair(
    project_root: Path, target_date: str
) -> tuple[bool, str]:
    return _validate_stage4_final_pair(project_root, target_date)


def validate_stage4_terminal_evidence_pair(
    project_root: Path, target_date: str
) -> tuple[bool, str]:
    maintenance_valid, maintenance_reason = validate_stage4_evidence_pair(
        project_root, target_date
    )
    if not maintenance_valid:
        return False, maintenance_reason
    final_valid, final_reason = validate_stage4_final_evaluation_pair(
        project_root, target_date
    )
    if not final_valid:
        return False, final_reason
    base = Path(project_root).resolve() / "results" / "data_health"
    try:
        maintenance = _read_json_object(
            base / f"stage4_maintenance_evidence_{target_date}.json"
        )
        final = _read_json_object(
            base / f"stage4_final_evaluation_{target_date}.json"
        )
    except RuntimeError:
        return False, "stage4_terminal_evidence_missing_or_unreadable"
    if (
        final.get("terminal_status") != maintenance.get("status")
        or final.get("maintenance_disposition")
        != maintenance.get("maintenance_disposition")
    ):
        return False, "stage4_terminal_evidence_cross_artifact_mismatch"
    return True, ""


def stage4_t0_plus_8_evidence_satisfied(
    project_root: Path,
    through_date: str | None = None,
) -> bool:
    root = Path(project_root).resolve()
    milestones = _stage4_milestone_dates(root)
    if milestones is None:
        return False
    t0_plus_8, t0_plus_18 = milestones
    base = root / "results" / "data_health"
    for path in sorted(base.glob("stage4_maintenance_evidence_*.json")):
        match = re.fullmatch(
            r"stage4_maintenance_evidence_(\d{4}-\d{2}-\d{2})\.json",
            path.name,
        )
        evidence_date = match.group(1) if match else ""
        if (
            not match
            or evidence_date < t0_plus_8
            or (through_date is not None and evidence_date > through_date)
        ):
            continue
        try:
            payload = _read_json_object(path)
        except RuntimeError:
            continue
        valid, _ = _validate_stage4_payload(
            payload,
            match.group(1),
            t0_plus_8_date=t0_plus_8,
            t0_plus_18_date=t0_plus_18,
        )
        if valid and payload.get("account_replay") != "not_authorized":
            return True
    return False


def stage4_evidence_satisfied(project_root: Path, target_date: str) -> bool:
    milestones = _stage4_milestone_dates(project_root)
    if milestones is None:
        return False
    _t0_plus_8, t0_plus_18 = milestones
    if target_date >= t0_plus_18:
        terminal_date = t0_plus_18
        valid, _reason = validate_stage4_terminal_evidence_pair(
            project_root, terminal_date
        )
        return valid
    return stage4_t0_plus_8_evidence_satisfied(
        project_root,
        through_date=target_date,
    )


def stage4_t0_plus_8_ready(project_root: Path, target_date: str) -> bool:
    path = (
        Path(project_root).resolve()
        / "results"
        / "data_health"
        / "round7_forward_pit_readiness_latest.json"
    )
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return False
    milestones = payload.get("milestones") if isinstance(payload, dict) else None
    eligible = payload.get("eligible_forward_days") if isinstance(payload, dict) else None
    if not isinstance(milestones, list) or not isinstance(eligible, list):
        return False
    milestone = next(
        (
            item
            for item in milestones
            if isinstance(item, dict)
            and str(item.get("relative_to_t0") or "").startswith("T0+8")
        ),
        None,
    )
    if not isinstance(milestone, dict) or milestone.get("status") != "achieved":
        return False
    milestone_date = str(milestone.get("calendar_date") or "")
    required = int(milestone.get("required_consecutive_trading_days") or 9)
    eligible_dates = {
        str(item.get("date") or "")
        for item in eligible
        if isinstance(item, dict)
        and str(item.get("date") or "") <= str(target_date)
    }
    return bool(
        milestone_date
        and target_date >= milestone_date
        and len(eligible_dates) >= required
    )


def _result(
    receipt: dict[str, Any],
    *,
    receipt_path: Path,
    daily_executed: bool,
    stage4_executed: bool,
    reason_code: str,
) -> dict[str, Any]:
    return {
        **receipt,
        "receipt_path": str(receipt_path),
        "daily_executed": daily_executed,
        "stage4_executed": stage4_executed,
        "reason_code": reason_code,
        "disclaimer": DISCLAIMER,
    }


def _manual_reconciliation(
    receipt: dict[str, Any], path: Path, current: datetime
) -> dict[str, Any]:
    failed = {
        **receipt,
        "state": "retryable_failed",
        "active_step": None,
        "failed_step": "daily",
        "reconciliation_required": True,
        "lease_expires_at": None,
        "updated_at": current.isoformat(),
    }
    _write_receipt(path, failed)
    return failed


def _complete_with_existing_stage4(
    receipt: dict[str, Any],
    path: Path,
    current: datetime,
    *,
    skipped_reason: str,
) -> dict[str, Any]:
    completed = {
        **receipt,
        "state": "completed",
        "active_step": None,
        "failed_step": None,
        "reconciliation_required": False,
        "stage4_claimed_at": None,
        "stage4_lease_expires_at": None,
        "stage4_completed_at": current.isoformat(),
        "stage4_returncode": 0,
        "stage4_skipped_reason": skipped_reason,
        "returncode": 0,
        "error": "",
        "lease_expires_at": None,
        "updated_at": current.isoformat(),
    }
    _write_receipt(path, completed)
    return completed


def run_evening_review(
    *,
    project_root: Path,
    target_date: str,
    authoritative_chain_run_id: str,
    daily_runner: CommandRunner,
    candidate_tracking_runner: CandidateTrackingRunner | None = None,
    stage4_runner: CommandRunner,
    stage4_ready: Stage4Readiness = stage4_t0_plus_8_ready,
    stage4_satisfied: Stage4Satisfied = stage4_evidence_satisfied,
    trigger: str = "manual",
    now: datetime | Clock | None = None,
    lease_seconds: int = 7200,
) -> dict[str, Any]:
    """Consume one chain event with one automatic daily attempt at most."""

    if lease_seconds < 1:
        raise ValueError("lease_seconds must be positive")
    normalized_trigger = str(trigger or "manual").strip()
    if normalized_trigger not in REVIEW_TRIGGERS:
        raise ValueError("trigger must be manual or scheduled_daily_review")
    root = Path(project_root).resolve()
    event_key = build_review_event_key(target_date, authoritative_chain_run_id)
    path = review_receipt_path(root, target_date, authoritative_chain_run_id)
    date_lock_path = root / "state" / "daily_review_runs" / f"{target_date}.lock"
    clock = _clock(now)
    try:
        _reconcile_expired_prior_daily_receipts(root, target_date, clock)
        with interprocess_file_lock(date_lock_path, timeout_seconds=0):
            path, receipt = _select_date_receipt(root, target_date, path)
            selected_run_id = str(
                receipt.get("authoritative_chain_run_id")
                or authoritative_chain_run_id
            )
            selected_event_key = str(receipt.get("event_key") or event_key)
            if receipt.get("state") == "completed":
                return _result(
                    receipt,
                    receipt_path=path,
                    daily_executed=False,
                    stage4_executed=False,
                    reason_code=(
                        "event_already_completed"
                        if selected_run_id == authoritative_chain_run_id
                        else "date_already_reviewed"
                    ),
                )
            if receipt.get("state") == "retryable_failed" and receipt.get(
                "failed_step"
            ) == "daily":
                return _result(
                    receipt,
                    receipt_path=path,
                    daily_executed=False,
                    stage4_executed=False,
                    reason_code=(
                        "daily_completion_ambiguous_manual_reconciliation_required"
                    ),
                )
            if receipt.get("state") == "running" and receipt.get(
                "active_step"
            ) == "daily":
                failed = _manual_reconciliation(receipt, path, clock())
                return _result(
                    failed,
                    receipt_path=path,
                    daily_executed=False,
                    stage4_executed=False,
                    reason_code=(
                        "daily_completion_ambiguous_manual_reconciliation_required"
                    ),
                )
            if receipt.get("state") == "retryable_failed" and receipt.get(
                "failed_step"
            ) == "candidate_tracking":
                # A tracking failure is retryable without replaying the already
                # completed daily child.  A caller that does not provide the
                # hook cannot safely advance this receipt, so leave it visible.
                if candidate_tracking_runner is None:
                    return _result(
                        receipt,
                        receipt_path=path,
                        daily_executed=False,
                        stage4_executed=False,
                        reason_code="candidate_tracking_runner_missing",
                    )
                receipt = {
                    **receipt,
                    "state": "daily_completed",
                    "active_step": None,
                    "failed_step": None,
                    "candidate_tracking_returncode": None,
                    "returncode": 0,
                    "error": "",
                    "reconciliation_required": False,
                    "updated_at": clock().isoformat(),
                }
                _write_receipt(path, receipt)
            
            daily_done = bool(receipt.get("daily_completed_at"))
            daily_executed = False
            claim_id = uuid4().hex
            if not daily_done:
                claimed_at = clock()
                running = {
                    "schema_version": RECEIPT_SCHEMA_VERSION,
                    "event_key": selected_event_key,
                    "target_date": target_date,
                    "authoritative_chain_run_id": selected_run_id,
                    "trigger": normalized_trigger,
                    "state": "running",
                    "active_step": "daily",
                    "claim_id": claim_id,
                    "claimed_at": claimed_at.isoformat(),
                    "lease_expires_at": (
                        claimed_at + timedelta(seconds=lease_seconds)
                    ).isoformat(),
                    "daily_completed_at": None,
                    "daily_returncode": None,
                    "candidate_tracking_claimed_at": None,
                    "candidate_tracking_completed_at": None,
                    "candidate_tracking_returncode": None,
                    "stage4_claimed_at": None,
                    "stage4_lease_expires_at": None,
                    "stage4_completed_at": None,
                    "stage4_returncode": None,
                    "failed_step": None,
                    "returncode": None,
                    "reconciliation_required": False,
                    "error": "",
                    "updated_at": claimed_at.isoformat(),
                }
                _write_receipt(path, running)
                daily_executed = True
                daily_returncode, daily_error = _safe_run(daily_runner)
                finished_at = clock()
                if daily_returncode != 0:
                    failed = {
                        **running,
                        "state": "retryable_failed",
                        "active_step": None,
                        "failed_step": "daily",
                        "daily_returncode": daily_returncode,
                        "returncode": daily_returncode,
                        "error": daily_error,
                        "reconciliation_required": True,
                        "lease_expires_at": None,
                        "updated_at": finished_at.isoformat(),
                    }
                    _write_receipt(path, failed)
                    return _result(
                        failed,
                        receipt_path=path,
                        daily_executed=True,
                        stage4_executed=False,
                        reason_code="daily_failed_manual_reconciliation_required",
                    )
                receipt = {
                    **running,
                    "state": "daily_completed",
                    "active_step": None,
                    "daily_completed_at": finished_at.isoformat(),
                    "daily_returncode": 0,
                    "returncode": 0,
                    "lease_expires_at": None,
                    "stage4_lease_expires_at": None,
                    "updated_at": finished_at.isoformat(),
                }
                _write_receipt(path, receipt)

            if candidate_tracking_runner is not None and not receipt.get(
                "candidate_tracking_completed_at"
            ):
                tracking_claimed_at = clock()
                tracking_lease = tracking_claimed_at + timedelta(seconds=lease_seconds)
                tracking_running = {
                    **receipt,
                    "state": "running",
                    "active_step": "candidate_tracking",
                    "candidate_tracking_claimed_at": tracking_claimed_at.isoformat(),
                    "candidate_tracking_completed_at": None,
                    "candidate_tracking_returncode": None,
                    "lease_expires_at": tracking_lease.isoformat(),
                    "failed_step": None,
                    "reconciliation_required": False,
                    "returncode": None,
                    "error": "",
                    "updated_at": tracking_claimed_at.isoformat(),
                }
                _write_receipt(path, tracking_running)
                tracking_returncode, tracking_error = _safe_run(
                    candidate_tracking_runner
                )
                finished_at = clock()
                if tracking_returncode != 0:
                    failed = {
                        **tracking_running,
                        "state": "retryable_failed",
                        "active_step": None,
                        "failed_step": "candidate_tracking",
                        "candidate_tracking_returncode": tracking_returncode,
                        "returncode": tracking_returncode,
                        "error": tracking_error,
                        "lease_expires_at": None,
                        "updated_at": finished_at.isoformat(),
                    }
                    _write_receipt(path, failed)
                    return _result(
                        failed,
                        receipt_path=path,
                        daily_executed=daily_executed,
                        stage4_executed=False,
                        reason_code="candidate_tracking_failed_retryable",
                    )
                receipt = {
                    **tracking_running,
                    "state": "daily_completed",
                    "active_step": None,
                    "candidate_tracking_completed_at": finished_at.isoformat(),
                    "candidate_tracking_returncode": 0,
                    "returncode": 0,
                    "lease_expires_at": None,
                    "updated_at": finished_at.isoformat(),
                }
                _write_receipt(path, receipt)

            if stage4_satisfied(root, target_date):
                milestones = _stage4_milestone_dates(root)
                terminal_target = bool(
                    milestones is not None and target_date >= milestones[1]
                )
                completed = _complete_with_existing_stage4(
                    receipt,
                    path,
                    clock(),
                    skipped_reason=(
                        "t0_plus_18_evidence_already_satisfied"
                        if terminal_target
                        else "t0_plus_8_evidence_already_satisfied"
                    ),
                )
                return _result(
                    completed,
                    receipt_path=path,
                    daily_executed=daily_executed,
                    stage4_executed=False,
                    reason_code="event_completed",
                )
            if not stage4_ready(root, target_date):
                return _result(
                    receipt,
                    receipt_path=path,
                    daily_executed=daily_executed,
                    stage4_executed=False,
                    reason_code=(
                        "stage4_not_ready"
                        if daily_executed
                        else (
                            "daily_already_completed"
                            if selected_run_id == authoritative_chain_run_id
                            else "date_already_reviewed"
                        )
                    ),
                )

            stage4_claimed_at = clock()
            stage4_lease = stage4_claimed_at + timedelta(seconds=lease_seconds)
            stage4_running = {
                **receipt,
                "state": "running",
                "active_step": "stage4",
                "claim_id": claim_id,
                "stage4_claimed_at": stage4_claimed_at.isoformat(),
                "stage4_lease_expires_at": stage4_lease.isoformat(),
                "stage4_completed_at": None,
                "stage4_returncode": None,
                "stage4_skipped_reason": None,
                "lease_expires_at": stage4_lease.isoformat(),
                "updated_at": stage4_claimed_at.isoformat(),
                "failed_step": None,
                "reconciliation_required": False,
                "returncode": None,
                "error": "",
            }
            _write_receipt(path, stage4_running)
            stage4_returncode, stage4_error = _safe_run(stage4_runner)
            finished_at = clock()
            if stage4_returncode != 0:
                failed = {
                    **stage4_running,
                    "state": "retryable_failed",
                    "active_step": None,
                    "failed_step": "stage4",
                    "stage4_returncode": stage4_returncode,
                    "returncode": stage4_returncode,
                    "error": stage4_error,
                    "lease_expires_at": None,
                    "stage4_lease_expires_at": None,
                    "updated_at": finished_at.isoformat(),
                }
                _write_receipt(path, failed)
                return _result(
                    failed,
                    receipt_path=path,
                    daily_executed=daily_executed,
                    stage4_executed=True,
                    reason_code="stage4_failed_retryable",
                )
            completed = {
                **stage4_running,
                "state": "completed",
                "active_step": None,
                "stage4_completed_at": finished_at.isoformat(),
                "stage4_returncode": 0,
                "returncode": 0,
                "error": "",
                "lease_expires_at": None,
                "stage4_lease_expires_at": None,
                "updated_at": finished_at.isoformat(),
            }
            _write_receipt(path, completed)
            return _result(
                completed,
                receipt_path=path,
                daily_executed=daily_executed,
                stage4_executed=True,
                reason_code="event_completed",
            )
    except TimeoutError:
        receipts = _receipts_for_date(root, target_date)
        if receipts:
            path, receipt = receipts[0]
        else:
            receipt = {}
        return _result(
            receipt,
            receipt_path=path,
            daily_executed=False,
            stage4_executed=False,
            reason_code="date_review_already_running",
        )


__all__ = [
    "DISCLAIMER",
    "build_review_event_key",
    "review_receipt_path",
    "_reconcile_expired_prior_daily_receipts",
    "run_evening_review",
    "stage4_evidence_satisfied",
    "stage4_t0_plus_8_evidence_satisfied",
    "stage4_t0_plus_8_ready",
    "validate_stage4_evidence_pair",
    "validate_stage4_final_evaluation_pair",
    "validate_stage4_terminal_evidence_pair",
]
