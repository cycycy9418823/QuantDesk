"""One-command read-only gate summary for the extended data lifecycle.

This module aggregates the existing local diagnostics:

- Tushare queue status
- retry plan
- staged import pre-apply audit
- canonical post-import verification

It is intentionally read-only.  It never fetches network data, restarts
downloads, imports staged files, or writes canonical market data.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from quantdesk.extended_import_verifier import build_extended_import_verification
from quantdesk.extended_staged_data_importer import FULL_APPLY_COMMAND, FULL_AUDIT_COMMAND, build_extended_staged_import_audit
from quantdesk.shared import now_text, read_json, write_json, write_text
from quantdesk.tushare_minute_coverage import build_tushare_minute_coverage
from quantdesk.tushare_minute_quality import (
    load_latest_tushare_minute_quality_snapshot,
    summarize_completed_minute_quality_snapshot,
)
from quantdesk.tushare_minute_status import build_tushare_minute_status
from quantdesk.tushare_queue_status import build_tushare_queue_status
from quantdesk.tushare_retry_plan import build_tushare_retry_plan


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT_PATH = PROJECT_ROOT / "results" / "data_health" / "extended_data_gates_latest.json"
DEFAULT_REPORT_PATH = PROJECT_ROOT / "reports" / "extended_data_gates_latest.md"


def _root(project_root: str | Path | None) -> Path:
    return Path(project_root).resolve() if project_root else PROJECT_ROOT


def _rel(path: Path, root: Path) -> str:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except Exception:
        return path.as_posix()


def _queue_stage(queue_status: dict[str, Any]) -> str:
    queue = queue_status.get("queue") if isinstance(queue_status.get("queue"), dict) else {}
    return str(queue.get("current_stage") or "")


def _queue_stage_progress(queue_status: dict[str, Any]) -> dict[str, Any]:
    queue = queue_status.get("queue") if isinstance(queue_status.get("queue"), dict) else {}
    progress = queue.get("stage_progress") if isinstance(queue.get("stage_progress"), dict) else {}
    if not progress:
        progress = queue_status.get("stage_progress") if isinstance(queue_status.get("stage_progress"), dict) else {}
    return progress


def _queue_log_counts(queue_status: dict[str, Any]) -> dict[str, int]:
    summary = queue_status.get("log_summary") if isinstance(queue_status.get("log_summary"), dict) else {}
    keys = ("ok_count", "fail_count", "ssl_eof_count", "timeout_count", "traceback_count")
    counts: dict[str, int] = {}
    for key in keys:
        try:
            counts[key] = int(summary.get(key) or 0)
        except Exception:
            counts[key] = 0
    return counts


def _number_or_none(value: Any) -> float | int | None:
    if value in (None, ""):
        return None
    try:
        number = float(value)
    except Exception:
        return None
    return int(number) if number.is_integer() else number


def _int_or_none(value: Any) -> int | None:
    if value in (None, ""):
        return None
    try:
        return int(float(value))
    except Exception:
        return None


def _queue_log_freshness(queue_status: dict[str, Any]) -> dict[str, Any]:
    current_logs = queue_status.get("current_logs") if isinstance(queue_status.get("current_logs"), dict) else {}
    freshness: dict[str, Any] = {}
    for stream in ("stdout", "stderr"):
        item = current_logs.get(stream) if isinstance(current_logs.get(stream), dict) else {}
        freshness[f"{stream}_age_seconds"] = _number_or_none(item.get("age_seconds"))
        freshness[f"{stream}_modified_at"] = str(item.get("modified_at") or "")
        freshness[f"{stream}_path"] = str(item.get("path") or "")
    stdout_age = freshness.get("stdout_age_seconds")
    if isinstance(stdout_age, (int, float)):
        freshness["queue_liveness"] = "active" if stdout_age <= 900 else "stale"
    else:
        freshness["queue_liveness"] = "unknown"
    return freshness


def _queue_progress_since_previous(
    *,
    current_queue: dict[str, Any],
    previous_payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Compare current queue counters with the previous data-gates snapshot."""

    previous = previous_payload if isinstance(previous_payload, dict) else {}
    previous_chain = previous.get("gate_chain") if isinstance(previous.get("gate_chain"), dict) else {}
    previous_queue = previous_chain.get("queue") if isinstance(previous_chain.get("queue"), dict) else {}
    current_ok = _int_or_none(current_queue.get("ok_count"))
    current_fail = _int_or_none(current_queue.get("fail_count"))
    current_traceback = _int_or_none(current_queue.get("traceback_count"))
    previous_ok = _int_or_none(previous_queue.get("ok_count"))
    previous_fail = _int_or_none(previous_queue.get("fail_count"))
    previous_traceback = _int_or_none(previous_queue.get("traceback_count"))

    ok_delta = current_ok - previous_ok if current_ok is not None and previous_ok is not None else None
    fail_delta = current_fail - previous_fail if current_fail is not None and previous_fail is not None else None
    traceback_delta = (
        current_traceback - previous_traceback
        if current_traceback is not None and previous_traceback is not None
        else None
    )

    if ok_delta is None:
        progress_state = "unknown"
    elif ok_delta < 0 or (fail_delta is not None and fail_delta < 0) or (traceback_delta is not None and traceback_delta < 0):
        progress_state = "counter_reset_or_regressed"
    elif ok_delta > 0:
        progress_state = "advancing"
    elif fail_delta and fail_delta > 0:
        progress_state = "failures_increased_without_ok_growth"
    elif traceback_delta and traceback_delta > 0:
        progress_state = "tracebacks_increased_without_ok_growth"
    else:
        progress_state = "no_change"

    return {
        "progress_state": progress_state,
        "previous_generated_at": str(previous.get("generated_at") or ""),
        "previous_stage": str(previous_queue.get("current_stage") or ""),
        "current_stage": str(current_queue.get("current_stage") or ""),
        "previous_ok_count": previous_ok,
        "current_ok_count": current_ok,
        "ok_delta": ok_delta,
        "previous_fail_count": previous_fail,
        "current_fail_count": current_fail,
        "fail_delta": fail_delta,
        "previous_traceback_count": previous_traceback,
        "current_traceback_count": current_traceback,
        "traceback_delta": traceback_delta,
    }


def _choose_current_gate(
    *,
    queue_status: dict[str, Any],
    retry_plan: dict[str, Any],
    import_audit: dict[str, Any],
    import_verification: dict[str, Any],
) -> tuple[str, str, str]:
    queue_state = str(queue_status.get("status") or "").strip().lower()
    queue_suggested = str(queue_status.get("suggested_next_action") or "").strip()
    if queue_suggested == "continue_monitoring":
        return "warning", "download_queue", "continue_monitoring"
    if queue_state in {"running", "running_or_present"}:
        return "warning", "download_queue", queue_suggested or "inspect_logs"

    retry_gate = str(retry_plan.get("retry_gate") or "")
    if retry_gate in {"review_failures_before_retry", "inspect_traceback_before_retry"}:
        return "warning", "retry_plan", str(retry_plan.get("suggested_next_action") or "review_retry_plan")

    preapply_gate = str(import_audit.get("preapply_gate") or "")
    if preapply_gate == "wait_for_download_completion":
        return "warning", "download_queue", "wait_for_queue_then_rerun_audit"
    if preapply_gate in {"review_required", "no_staged_files_found"}:
        return "warning", "preapply_audit", str(import_audit.get("suggested_next_action") or "review_preapply_audit")
    if preapply_gate == "ready_for_bounded_apply":
        return "warning", "preapply_audit", "confirm_bounded_apply_with_user"
    if preapply_gate == "ready_for_full_apply":
        return "warning", "preapply_audit", "confirm_full_apply_with_user"

    post_gate = str(import_verification.get("post_import_gate") or "")
    if post_gate in {"wait_for_download_completion", "no_canonical_import_yet", "partial_canonical_import"}:
        return "warning", "post_import_verification", str(import_verification.get("suggested_next_action") or "verify_extended_import")
    if post_gate == "canonical_presence_verified":
        return "passed", "canonical_presence_verified", "run_data_readiness_and_market_intel"
    return "warning", "unknown", "python scripts/qd.py data-health --network false --summary true"


def _operator_wait_reason(
    *,
    current_gate: str,
    queue_freshness: dict[str, Any],
    retry_plan: dict[str, Any],
    import_audit: dict[str, Any],
    import_verification: dict[str, Any],
) -> str:
    """Return a stable operator-facing reason for the active gate."""

    if current_gate == "download_queue":
        liveness = str(queue_freshness.get("queue_liveness") or "unknown").strip().lower()
        if liveness == "active":
            return "download_queue_active"
        if liveness == "stale":
            return "download_queue_stale"
        return "download_queue_unknown"

    if current_gate == "retry_plan":
        retry_gate = str(retry_plan.get("retry_gate") or "").strip()
        if retry_gate == "inspect_traceback_before_retry":
            return "retry_traceback_review_required"
        if retry_gate == "review_failures_before_retry":
            return "retry_review_required"
        return retry_gate or "retry_plan_review_required"

    if current_gate == "preapply_audit":
        preapply_gate = str(import_audit.get("preapply_gate") or "").strip()
        if preapply_gate == "ready_for_bounded_apply":
            return "ready_for_bounded_apply"
        if preapply_gate == "ready_for_full_apply":
            return "ready_for_full_apply"
        if preapply_gate == "no_staged_files_found":
            return "no_staged_files_found"
        return "preapply_review_required"

    if current_gate == "post_import_verification":
        post_gate = str(import_verification.get("post_import_gate") or "").strip()
        if post_gate == "partial_canonical_import":
            return "canonical_import_partial"
        if post_gate == "no_canonical_import_yet":
            return "staged_data_not_imported"
        return post_gate or "post_import_verification_required"

    if current_gate == "canonical_presence_verified":
        return "canonical_presence_verified"

    return current_gate or "unknown"


def _dedupe_text(items: list[str]) -> list[str]:
    return [item for item in dict.fromkeys(str(item).strip() for item in items) if item]


def _operator_action_policy(
    *,
    current_gate: str,
    operator_wait_reason: str,
    import_audit: dict[str, Any],
) -> dict[str, Any]:
    """Return safe next commands and explicit confirmation requirements."""

    blocked_without_confirmation = [
        "python scripts/qd.py import-extended-data --apply true",
        # Keep the legacy bounded-looking apply form blocked too; the approved
        # full apply command is surfaced separately as FULL_APPLY_COMMAND.
        "python scripts/qd.py import-extended-data --apply true --summary true",
        FULL_APPLY_COMMAND,
        "python scripts/qd.py import-extended-data --apply true --allow-running-queue true",
    ]
    safe_next_commands: list[str] = []
    confirmation_required_command = ""
    requires_user_confirmation = False
    manual_review_required = False
    manual_review_reason = ""

    if current_gate == "download_queue":
        safe_next_commands.extend(
            [
                "python scripts/qd.py tushare-queue-status --summary true",
                "python scripts/qd.py tushare-minute-status --summary true",
                "python scripts/qd.py tushare-minute-coverage --summary true",
                "python scripts/qd.py data-gates --summary true",
            ]
        )
        if operator_wait_reason in {"download_queue_stale", "download_queue_unknown"}:
            safe_next_commands.append("python scripts/qd.py tushare-retry-plan --summary true")
            manual_review_required = True
            manual_review_reason = operator_wait_reason
    elif current_gate == "retry_plan":
        safe_next_commands.extend(
            [
                "python scripts/qd.py tushare-retry-plan --summary true",
                "python scripts/qd.py data-gates --summary true",
            ]
        )
        manual_review_required = True
        manual_review_reason = operator_wait_reason
    elif current_gate == "preapply_audit":
        safe_next_commands.extend(
            [
                FULL_AUDIT_COMMAND,
                "python scripts/qd.py data-gates --summary true",
            ]
        )
        manual_review_required = True
        manual_review_reason = operator_wait_reason
        if operator_wait_reason in {"ready_for_bounded_apply", "ready_for_full_apply"}:
            requires_user_confirmation = True
            confirmation_required_command = str(import_audit.get("apply_command_after_review") or "").strip()
    elif current_gate == "post_import_verification":
        safe_next_commands.extend(
            [
                "python scripts/qd.py verify-extended-import --summary true",
                "python scripts/qd.py data-gates --summary true",
            ]
        )
    elif current_gate == "canonical_presence_verified":
        safe_next_commands.extend(
            [
                "python scripts/qd.py data-readiness --summary true",
                "python scripts/qd.py market-intel --summary true",
            ]
        )
    else:
        safe_next_commands.append("python scripts/qd.py data-health --network false --summary true")
        manual_review_required = True
        manual_review_reason = operator_wait_reason

    return {
        "safe_next_commands": _dedupe_text(safe_next_commands),
        "manual_review_required": manual_review_required,
        "manual_review_reason": manual_review_reason,
        "requires_user_confirmation": requires_user_confirmation,
        "confirmation_required_command": confirmation_required_command,
        "blocked_without_confirmation": _dedupe_text(blocked_without_confirmation),
        "policy_note": "Read-only commands are safe for agents; import --apply requires explicit user confirmation.",
    }


def build_extended_data_gates(
    *,
    project_root: str | Path | None = None,
    max_files_per_rule: int = 20,
    previous_payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build the read-only extended-data gate summary."""

    root = _root(project_root)
    queue_status = build_tushare_queue_status(project_root=root, include_processes=True)
    minute_status = build_tushare_minute_status(project_root=root, include_processes=False)
    minute_coverage = build_tushare_minute_coverage(project_root=root)
    minute_quality = load_latest_tushare_minute_quality_snapshot(project_root=root)
    retry_plan = build_tushare_retry_plan(project_root=root)
    import_audit = build_extended_staged_import_audit(project_root=root, max_files_per_rule=max_files_per_rule)
    import_verification = build_extended_import_verification(project_root=root)
    return build_extended_data_gates_from_payloads(
        queue_status=queue_status,
        minute_status=minute_status,
        minute_coverage=minute_coverage,
        minute_quality=minute_quality,
        retry_plan=retry_plan,
        import_audit=import_audit,
        import_verification=import_verification,
        previous_payload=previous_payload,
    )


def build_extended_data_gates_from_payloads(
    *,
    queue_status: dict[str, Any],
    minute_status: dict[str, Any] | None = None,
    minute_coverage: dict[str, Any] | None = None,
    minute_quality: dict[str, Any] | None = None,
    retry_plan: dict[str, Any],
    import_audit: dict[str, Any],
    import_verification: dict[str, Any],
    previous_payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build the gate summary from already-computed read-only diagnostics."""

    status, current_gate, suggested = _choose_current_gate(
        queue_status=queue_status,
        retry_plan=retry_plan,
        import_audit=import_audit,
        import_verification=import_verification,
    )
    warnings: list[str] = []
    minute_status_payload = minute_status if isinstance(minute_status, dict) else {}
    minute_coverage_payload = minute_coverage if isinstance(minute_coverage, dict) else {}
    minute_quality_payload = minute_quality if isinstance(minute_quality, dict) else {}
    for payload in (
        queue_status,
        minute_status_payload,
        minute_coverage_payload,
        minute_quality_payload,
        retry_plan,
        import_audit,
        import_verification,
    ):
        warnings.extend(str(item) for item in (payload.get("warnings") or []) if item)
    queue_counts = _queue_log_counts(queue_status)
    queue_freshness = _queue_log_freshness(queue_status)
    queue_stage_progress = _queue_stage_progress(queue_status)
    queue_payload = {
        "status": queue_status.get("status"),
        "current_stage": _queue_stage(queue_status),
        "current_stage_status": queue_stage_progress.get("current_stage_status") or "",
        "stage_progress": queue_stage_progress,
        "stage_position_label": queue_stage_progress.get("stage_position_label") or "",
        "stage_requested_count": queue_stage_progress.get("requested_stage_count") or 0,
        "stage_completed_count": queue_stage_progress.get("completed_stage_count") or 0,
        "stage_remaining_count": queue_stage_progress.get("remaining_stage_count") or 0,
        "stage_remaining_keys": queue_stage_progress.get("remaining_stage_keys") or [],
        "stage_current_elapsed_seconds": queue_stage_progress.get("current_stage_elapsed_seconds"),
        "stage_last_event_age_seconds": queue_stage_progress.get("last_stage_event_age_seconds"),
        "stage_last_event_status": queue_stage_progress.get("last_stage_event_status") or "",
        **queue_counts,
        **queue_freshness,
        "suggested_next_action": queue_status.get("suggested_next_action"),
    }
    queue_progress = _queue_progress_since_previous(
        current_queue=queue_payload,
        previous_payload=previous_payload,
    )
    queue_payload.update(
        {
            "progress_state": queue_progress.get("progress_state"),
            "ok_delta": queue_progress.get("ok_delta"),
            "fail_delta": queue_progress.get("fail_delta"),
            "traceback_delta": queue_progress.get("traceback_delta"),
        }
    )
    operator_wait_reason = _operator_wait_reason(
        current_gate=current_gate,
        queue_freshness=queue_freshness,
        retry_plan=retry_plan,
        import_audit=import_audit,
        import_verification=import_verification,
    )
    operator_action_policy = _operator_action_policy(
        current_gate=current_gate,
        operator_wait_reason=operator_wait_reason,
        import_audit=import_audit,
    )
    minute_log = minute_status_payload.get("log_summary") if isinstance(minute_status_payload.get("log_summary"), dict) else {}
    minute_last = minute_log.get("last_symbol_done") if isinstance(minute_log.get("last_symbol_done"), dict) else {}
    minute_target = minute_coverage_payload.get("target_pool") if isinstance(minute_coverage_payload.get("target_pool"), dict) else {}
    minute_job = minute_coverage_payload.get("minute_job") if isinstance(minute_coverage_payload.get("minute_job"), dict) else {}
    completed_minute_job = (
        minute_coverage_payload.get("latest_completed_job")
        if isinstance(minute_coverage_payload.get("latest_completed_job"), dict)
        else {}
    )
    completed_quality = summarize_completed_minute_quality_snapshot(
        minute_quality_payload,
        expected_job_path=str(completed_minute_job.get("path") or ""),
    )
    minute_payload = {
        "status": minute_status_payload.get("status") or "",
        "coverage_status": minute_coverage_payload.get("status") or "",
        "symbol_done_count": minute_log.get("symbol_done_count") or 0,
        "symbol_count": minute_log.get("symbol_count") or 0,
        "last_symbol": minute_last.get("symbol") or "",
        "rate_limit_count": minute_log.get("rate_limit_count") or 0,
        "permission_count": minute_log.get("permission_count") or 0,
        "traceback_count": minute_log.get("traceback_count") or 0,
        "target_symbol_count": minute_target.get("symbol_count") or 0,
        "target_pool_path": minute_target.get("path") or "",
        "job_path": minute_job.get("path") or "",
        "target_end": minute_job.get("target_end") or "",
        "current_available_count": minute_coverage_payload.get("current_available_count") or 0,
        "current_available_count_source": minute_coverage_payload.get("current_available_count_source") or "",
        "canonical_current_available_count": minute_coverage_payload.get("canonical_current_available_count") or 0,
        "staged_current_not_imported_count": minute_coverage_payload.get("staged_current_not_imported_count") or 0,
        "canonical_current_available_ratio": minute_coverage_payload.get("canonical_current_available_ratio") or 0.0,
        "staged_current_not_imported_ratio": minute_coverage_payload.get("staged_current_not_imported_ratio") or 0.0,
        "missing_or_stale_count": minute_coverage_payload.get("missing_or_stale_count") or 0,
        "current_available_ratio": minute_coverage_payload.get("current_available_ratio") or 0.0,
        "canonical_file_count": minute_coverage_payload.get("canonical_file_count") or 0,
        "latest_completed_job_path": completed_minute_job.get("path") or "",
        "latest_completed_job_symbol_count": completed_minute_job.get("symbol_count") or 0,
        "latest_completed_job_canonical_current_count": completed_minute_job.get("canonical_current_count") or 0,
        "latest_completed_job_missing_or_stale_count": completed_minute_job.get("missing_or_stale_count") or 0,
        "latest_completed_job_available_ratio": completed_minute_job.get("current_available_ratio") or 0.0,
        "latest_completed_quality_status": completed_quality.get("status") or "missing",
        "latest_completed_quality_snapshot_path": completed_quality.get("snapshot_path") or "",
        "latest_completed_quality_job_matches": bool(completed_quality.get("job_matches_latest_completed")),
        "latest_completed_quality_target_date": completed_quality.get("target_date") or "",
        "latest_completed_quality_total_symbols": completed_quality.get("total_symbols") or 0,
        "latest_completed_quality_usable_count": completed_quality.get("usable_count") or 0,
        "latest_completed_quality_partial_count": completed_quality.get("partial_count") or 0,
        "latest_completed_quality_insufficient_count": completed_quality.get("insufficient_count") or 0,
        "latest_completed_quality_invalid_count": completed_quality.get("invalid_count") or 0,
        "latest_completed_quality_missing_count": completed_quality.get("missing_count") or 0,
        "latest_completed_quality_usable_ratio": completed_quality.get("usable_ratio") or 0.0,
        "latest_completed_quality_suggested_next_action": completed_quality.get("suggested_next_action") or "",
        "suggested_next_action": minute_coverage_payload.get("suggested_next_action")
        or minute_status_payload.get("suggested_next_action")
        or "",
    }
    return {
        "task_type": "extended_data_gates",
        "schema_version": "1.0",
        "status": status,
        "current_gate": current_gate,
        "suggested_next_action": suggested,
        "operator_wait_reason": operator_wait_reason,
        "operator_action_policy": operator_action_policy,
        "queue_progress_since_previous": queue_progress,
        "gate_chain": {
            "queue": queue_payload,
            "minute": minute_payload,
            "retry_plan": {
                "status": retry_plan.get("status"),
                "retry_gate": retry_plan.get("retry_gate"),
                "fail_event_count": (retry_plan.get("summary") or {}).get("fail_event_count") if isinstance(retry_plan.get("summary"), dict) else 0,
                "retry_candidate_groups": len(retry_plan.get("retry_candidates") or []),
                "suggested_next_action": retry_plan.get("suggested_next_action"),
            },
            "import_audit": {
                "status": import_audit.get("status"),
                "preapply_gate": import_audit.get("preapply_gate"),
                "planned_file_count": import_audit.get("planned_file_count"),
                "domain_counts": import_audit.get("domain_counts") if isinstance(import_audit.get("domain_counts"), dict) else {},
                "missing_planned_domains": import_audit.get("missing_planned_domains") or [],
                "duplicate_destination_count": import_audit.get("duplicate_destination_count") or 0,
                "existing_destination_count": import_audit.get("existing_destination_count") or 0,
                "would_improve_domains": import_audit.get("would_improve_domains") or [],
                "max_files_per_rule": import_audit.get("max_files_per_rule") or 0,
                "source_scan_limit_applied": bool(import_audit.get("source_scan_limit_applied")),
                "source_scan_limited_domains": import_audit.get("source_scan_limited_domains") or [],
                "source_scan_limited_rule_count": import_audit.get("source_scan_limited_rule_count") or 0,
                "full_audit_command": import_audit.get("full_audit_command") or FULL_AUDIT_COMMAND,
                "source_scan_full_audit_command": import_audit.get("source_scan_full_audit_command") or "",
                "apply_command_after_review": import_audit.get("apply_command_after_review"),
                "suggested_next_action": import_audit.get("suggested_next_action"),
            },
            "import_verification": {
                "status": import_verification.get("status"),
                "post_import_gate": import_verification.get("post_import_gate"),
                "canonical_presence_ratio": import_verification.get("canonical_presence_ratio"),
                "missing_canonical_domains": import_verification.get("missing_canonical_domains") or [],
                "suggested_next_action": import_verification.get("suggested_next_action"),
            },
        },
        "warnings": sorted(dict.fromkeys(warnings)),
        "generated_at": now_text(),
        "research_only": True,
        "paper_only": True,
        "no_network_fetch": True,
        "no_canonical_writes": True,
        "no_broker_route": True,
    }


def render_extended_data_gates_report(payload: dict[str, Any]) -> str:
    chain = payload.get("gate_chain") if isinstance(payload.get("gate_chain"), dict) else {}
    lines = [
        "# Extended Data Gates",
        "",
        "Research Only / Read-Only / No Network Fetch / No Canonical Writes / No Broker Route",
        "",
        f"- status: `{payload.get('status') or '-'}`",
        f"- current_gate: `{payload.get('current_gate') or '-'}`",
        f"- suggested_next_action: `{payload.get('suggested_next_action') or '-'}`",
        f"- operator_wait_reason: `{payload.get('operator_wait_reason') or '-'}`",
        "",
        "## Operator Action Policy",
        "",
    ]
    policy = payload.get("operator_action_policy") if isinstance(payload.get("operator_action_policy"), dict) else {}
    lines.extend(
        [
            f"- requires_user_confirmation: `{policy.get('requires_user_confirmation')}`",
            f"- manual_review_required: `{policy.get('manual_review_required')}`",
            f"- manual_review_reason: `{policy.get('manual_review_reason') or '-'}`",
            f"- confirmation_required_command: `{policy.get('confirmation_required_command') or '-'}`",
            f"- policy_note: {policy.get('policy_note') or '-'}",
            "",
            "Safe next commands:",
            "",
        ]
    )
    safe_commands = policy.get("safe_next_commands") if isinstance(policy.get("safe_next_commands"), list) else []
    lines.extend([f"- `{command}`" for command in safe_commands] or ["- none"])
    lines.extend(
        [
            "",
            "Blocked without confirmation:",
            "",
        ]
    )
    blocked = policy.get("blocked_without_confirmation") if isinstance(policy.get("blocked_without_confirmation"), list) else []
    lines.extend([f"- `{command}`" for command in blocked] or ["- none"])
    lines.extend(
        [
            "",
            "## Gate Chain",
            "",
            "| gate | status | key state | suggested_next_action |",
            "| --- | --- | --- | --- |",
        ]
    )
    queue = chain.get("queue") if isinstance(chain.get("queue"), dict) else {}
    minute = chain.get("minute") if isinstance(chain.get("minute"), dict) else {}
    retry = chain.get("retry_plan") if isinstance(chain.get("retry_plan"), dict) else {}
    audit = chain.get("import_audit") if isinstance(chain.get("import_audit"), dict) else {}
    verify = chain.get("import_verification") if isinstance(chain.get("import_verification"), dict) else {}
    lines.extend(
        [
            f"| queue | {queue.get('status') or '-'} | stage={queue.get('current_stage') or '-'}, progress={queue.get('stage_position_label') or '-'}, remaining={queue.get('stage_remaining_count', 0)}, stage_elapsed={queue.get('stage_current_elapsed_seconds')} | {queue.get('suggested_next_action') or '-'} |",
            f"| queue_counts | {queue.get('status') or '-'} | ok={queue.get('ok_count', 0)}, fail={queue.get('fail_count', 0)}, ssl_eof={queue.get('ssl_eof_count', 0)}, timeout={queue.get('timeout_count', 0)}, traceback={queue.get('traceback_count', 0)} | {queue.get('suggested_next_action') or '-'} |",
            f"| queue_progress | {queue.get('status') or '-'} | state={queue.get('progress_state') or '-'}, ok_delta={queue.get('ok_delta')}, fail_delta={queue.get('fail_delta')}, traceback_delta={queue.get('traceback_delta')} | {queue.get('suggested_next_action') or '-'} |",
            f"| queue_logs | {queue.get('status') or '-'} | stdout_age={queue.get('stdout_age_seconds')}, stderr_age={queue.get('stderr_age_seconds')} | {queue.get('suggested_next_action') or '-'} |",
            f"| minute | {minute.get('status') or '-'} | downloaded={minute.get('symbol_done_count', 0)}/{minute.get('symbol_count', 0)}, available={minute.get('current_available_count', 0)}/{minute.get('target_symbol_count', 0)}, canonical={minute.get('canonical_current_available_count', 0)}, staged_pending_import={minute.get('staged_current_not_imported_count', 0)}, missing_or_stale={minute.get('missing_or_stale_count', 0)}, last={minute.get('last_symbol') or '-'} | {minute.get('suggested_next_action') or '-'} |",
            f"| minute_completed_quality | {minute.get('latest_completed_quality_status') or '-'} | usable={minute.get('latest_completed_quality_usable_count', 0)}/{minute.get('latest_completed_quality_total_symbols', 0)}, invalid={minute.get('latest_completed_quality_invalid_count', 0)}, missing={minute.get('latest_completed_quality_missing_count', 0)}, target={minute.get('latest_completed_quality_target_date') or '-'} | {minute.get('latest_completed_quality_suggested_next_action') or '-'} |",
            f"| retry_plan | {retry.get('status') or '-'} | gate={retry.get('retry_gate') or '-'}, fail={retry.get('fail_event_count', 0)} | {retry.get('suggested_next_action') or '-'} |",
            f"| import_audit | {audit.get('status') or '-'} | preapply={audit.get('preapply_gate') or '-'}, planned={audit.get('planned_file_count', 0)} | {audit.get('suggested_next_action') or '-'} |",
            f"| staged_domains | {audit.get('status') or '-'} | counts={audit.get('domain_counts') or {}}, missing={audit.get('missing_planned_domains') or []} | {audit.get('suggested_next_action') or '-'} |",
            f"| import_verification | {verify.get('status') or '-'} | post={verify.get('post_import_gate') or '-'}, ratio={verify.get('canonical_presence_ratio')} | {verify.get('suggested_next_action') or '-'} |",
            "",
            "## Full Staged Audit",
            "",
            f"- full_audit_command: `{audit.get('full_audit_command') or '-'}`",
            f"- source_scan_full_audit_command: `{audit.get('source_scan_full_audit_command') or '-'}`",
            "",
            "## Warnings",
            "",
        ]
    )
    warnings = payload.get("warnings") if isinstance(payload.get("warnings"), list) else []
    lines.extend([f"- {warning}" for warning in warnings[:100]] or ["- none"])
    lines.append("")
    return "\n".join(lines)


def write_extended_data_gates_outputs(
    *,
    project_root: str | Path | None = None,
    output_path: str | Path = DEFAULT_OUTPUT_PATH,
    report_path: str | Path = DEFAULT_REPORT_PATH,
    max_files_per_rule: int = 20,
) -> dict[str, Any]:
    root = _root(project_root)
    output = Path(output_path)
    report = Path(report_path)
    if not output.is_absolute():
        output = root / output
    if not report.is_absolute():
        report = root / report
    previous_payload = read_json(output, {}) if output.exists() else {}
    payload = build_extended_data_gates(
        project_root=root,
        max_files_per_rule=max_files_per_rule,
        previous_payload=previous_payload if isinstance(previous_payload, dict) else {},
    )
    payload["output_path"] = _rel(output, root)
    payload["report_path"] = _rel(report, root)
    write_json(output, payload)
    write_text(report, render_extended_data_gates_report(payload))
    return payload
