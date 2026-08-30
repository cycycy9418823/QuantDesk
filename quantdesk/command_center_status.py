"""Read-only command center status built from operation_log."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from quantdesk.daily_price_update_engine import infer_target_date
from quantdesk.operation_log import load_operation_log, summarize_recent_operations
from quantdesk.shared import now_text, read_json, write_json
from quantdesk.version import API_SCHEMA_VERSION, APP_VERSION, PACKAGE_VERSION

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT_PATH = PROJECT_ROOT / "results" / "command_center_status_latest.json"
DISCLAIMER = "仅用于本地命令状态排查，不联网、不调用 LLM、不构成投资建议。"

COMMAND_ALIASES = [
    "status",
    "fix-plan",
    "daily",
    "market-intel",
    "mainline-radar",
    "data-readiness",
    "data-gates",
    "tushare-queue-status",
    "tushare-minute-status",
    "tushare-minute-coverage",
    "tushare-minute-quality",
    "tushare-retry-plan",
    "audit-extended-import",
    "verify-extended-import",
    "self-check",
    "providers",
    "data-update",
    "dev-preflight",
    "daily-retain",
    "strategy-validate",
    "consistency-check",
    "feature-audit",
    "stats-validate",
    "param-sensitivity",
    "schema-check",
    "data-health",
    "observation-report",
    "agent-report-audit",
    "manifest-check",
    "archive-results",
    "llm-smoke",
    "network-smoke",
]
COMMAND_FIELD_KEYS = {"command", "suggested_next_action", "follow_up_command", "remediation_command"}
DATA_GATE_COMMANDS = ("data-gates", "data-health")
DATA_GATE_SUGGESTION_PREFIXES = (
    "continue_monitoring",
    "wait_for_queue",
    "restart_queue_after_user_approval",
    "inspect_logs",
    "inspect_traceback",
    "review_retry",
    "confirm_bounded_apply",
    "run_audit_extended_import",
    "run_data_readiness",
)
DATA_GATE_SAFE_COMMAND_PREFIXES = (
    "python scripts/qd.py tushare-queue-status",
    "python scripts/qd.py tushare-minute-status",
    "python scripts/qd.py tushare-minute-coverage",
    "python scripts/qd.py data-gates",
)


def _project_path(path: str | Path) -> Path:
    target = Path(path)
    if not target.is_absolute():
        target = PROJECT_ROOT / target
    return target


def _command_key(value: str) -> str:
    text = str(value or "").strip()
    if text.startswith("qd.py "):
        text = text.split(" ", 1)[1]
    return text.split()[0] if text else ""


def collect_latest_command_status(records: list[dict[str, Any]]) -> dict[str, Any]:
    latest: dict[str, dict[str, Any]] = {}
    for row in records:
        key = _command_key(str(row.get("command") or ""))
        if key in COMMAND_ALIASES:
            latest[key] = row
    return latest


def _int(value: Any, default: int = 0) -> int:
    try:
        if value in (None, ""):
            return default
        return int(float(value))
    except Exception:
        return default


def _bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {"1", "true", "yes", "y", "on"}


def _text_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item) for item in value if str(item).strip()]


def _int_dict(value: Any) -> dict[str, int]:
    if not isinstance(value, dict):
        return {}
    return {str(key): _int(item) for key, item in value.items() if str(key).strip()}


def _safe_suggested_next_action(value: Any, fallback: str = "python scripts/qd.py status") -> str:
    text = str(value or "").strip()
    if not text:
        return fallback
    if "python scripts/qd.py help" in text:
        return fallback
    if "market-intel" in text and "--scope full_a" in text and "--force true" not in text:
        return (
            "full_a 需要显式 --force true；日常排查请运行 "
            "python scripts/qd.py market-intel --network false --scope current_analysis_pool --price-limit 20"
        )
    return text


def _is_data_gate_suggestion(value: Any) -> bool:
    text = str(value or "").strip()
    return any(text.startswith(prefix) for prefix in DATA_GATE_SUGGESTION_PREFIXES) or any(
        text.startswith(prefix) for prefix in DATA_GATE_SAFE_COMMAND_PREFIXES
    )


def _active_data_gate_safe_command(row: dict[str, Any]) -> str:
    gate = row.get("data_gate_summary") if isinstance(row.get("data_gate_summary"), dict) else {}
    current_gate = str(gate.get("extended_data_current_gate") or "").strip()
    wait_reason = str(gate.get("extended_data_operator_wait_reason") or "").strip()
    if current_gate != "download_queue" and wait_reason != "download_queue_active":
        return ""
    safe_commands = gate.get("extended_data_safe_next_commands")
    if isinstance(safe_commands, list):
        for command in safe_commands:
            text = str(command or "").strip()
            if text:
                return text
    return "python scripts/qd.py tushare-queue-status --summary true"


def _active_data_gate_suggestion(commands: list[dict[str, Any]]) -> str:
    for command_name in DATA_GATE_COMMANDS:
        for row in commands:
            if row.get("command") != command_name:
                continue
            safe_command = _active_data_gate_safe_command(row)
            if safe_command:
                return safe_command
            suggested = str(row.get("suggested_next_action") or "").strip()
            if _is_data_gate_suggestion(suggested):
                return suggested
    return ""


def _current_target_date() -> str:
    try:
        target, _note = infer_target_date(None)
        return str(target or "")[:10]
    except Exception:
        return ""


def _target_date_from_payload(payload: dict[str, Any], fallback: str = "") -> str:
    summary = payload.get("summary") if isinstance(payload.get("summary"), dict) else {}
    freshness = payload.get("freshness") if isinstance(payload.get("freshness"), dict) else {}
    for value in (
        payload.get("target_date"),
        payload.get("effective_target_date"),
        payload.get("target_complete_trade_date"),
        summary.get("target_complete_trade_date"),
        summary.get("daily_price_target_date"),
        freshness.get("target_date"),
        freshness.get("target_complete_trade_date"),
        fallback,
    ):
        text = str(value or "").strip()
        if text and text not in {"-", "YYYY-MM-DD"}:
            return text[:10]
    return _current_target_date()


def _replace_target_tokens(text: str, target_date: str = "") -> str:
    rendered = str(text or "")
    target = str(target_date or "").strip() or _current_target_date()
    if "YYYY-MM-DD" in rendered:
        if target:
            rendered = rendered.replace("YYYY-MM-DD", target[:10])
        else:
            rendered = rendered.replace(" --target-date YYYY-MM-DD", "").replace("--target-date YYYY-MM-DD ", "")
    if "{target_date}" in rendered:
        rendered = rendered.replace("{target_date}", target[:10] if target else "target-date")
    return rendered


def _sanitize_command_fields(value: Any, target_date: str = "") -> Any:
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for key, item in value.items():
            if key in COMMAND_FIELD_KEYS and isinstance(item, str):
                out[key] = _replace_target_tokens(item, target_date)
            else:
                out[key] = _sanitize_command_fields(item, target_date)
        return out
    if isinstance(value, list):
        return [_sanitize_command_fields(item, target_date) for item in value]
    return value


def _daily_command_details(row: dict[str, Any]) -> dict[str, Any]:
    payload = read_json(PROJECT_ROOT / "results" / "daily_workflow_latest.json", {})
    payload = payload if isinstance(payload, dict) else {}
    target_date = _target_date_from_payload(payload)
    console = payload.get("daily_console_summary") if isinstance(payload.get("daily_console_summary"), dict) else {}
    guardrails = payload.get("guardrail_summary") if isinstance(payload.get("guardrail_summary"), dict) else {}
    report_path = console.get("report_path") or payload.get("report_path") or "reports/daily_quant_report_latest.md"
    result_path = console.get("result_path") or payload.get("latest_path") or "results/daily_workflow_latest.json"
    warning_count = _int(console.get("warning_count"), len(payload.get("warnings") or []))
    failed_count = _int(console.get("failed_count"), len(payload.get("errors") or []))
    status = row.get("status") or payload.get("status") or payload.get("workflow_status") or "missing"
    if status == "partial":
        status = "warning"
    suggested = ""
    actions = console.get("next_actions") if isinstance(console.get("next_actions"), list) else []
    remediation = console.get("remediation_actions") if isinstance(console.get("remediation_actions"), list) else []
    if not remediation:
        plan = payload.get("remediation_plan") if isinstance(payload.get("remediation_plan"), dict) else {}
        remediation = plan.get("actions") if isinstance(plan.get("actions"), list) else []
    remediation = _sanitize_command_fields(remediation, target_date)
    if actions and isinstance(actions[0], dict):
        suggested = _replace_target_tokens(str(actions[0].get("command") or ""), target_date)
    top_remediation = remediation[0] if remediation and isinstance(remediation[0], dict) else {}
    return {
        "last_status": status,
        "warning_count": warning_count,
        "failed_count": failed_count,
        "report_path": report_path,
        "result_path": result_path,
        "output_paths": [result_path, report_path],
        "remediation_actions": remediation[:3],
        "top_remediation": top_remediation,
        "suggested_next_action": suggested or "查看 daily 报告，或运行 python scripts/qd.py status 获取下一步建议。",
        "guardrail_warning_count": _int(guardrails.get("warning_count")),
        "guardrail_failed_count": _int(guardrails.get("failed_count")),
    }


def _remediation_details(path: str, fallback: str = "") -> dict[str, Any]:
    payload = read_json(PROJECT_ROOT / path, {})
    payload = payload if isinstance(payload, dict) else {}
    target_date = _target_date_from_payload(payload)
    actions = payload.get("remediation_actions") if isinstance(payload.get("remediation_actions"), list) else []
    if not actions:
        plan = payload.get("remediation_plan") if isinstance(payload.get("remediation_plan"), dict) else payload
        actions = plan.get("actions") if isinstance(plan.get("actions"), list) else []
    actions = _sanitize_command_fields(actions, target_date)
    top = actions[0] if actions and isinstance(actions[0], dict) else {}
    return {
        "remediation_actions": actions[:3],
        "top_remediation": top,
        "suggested_next_action": _replace_target_tokens(_safe_suggested_next_action(top.get("command") or fallback), target_date),
    }


def _latest_data_gates_summary() -> dict[str, Any]:
    """Return gate fields from the latest dedicated data-gates snapshot.

    Command center is often run after ``data-gates`` without a fresh
    ``data-health`` run.  Prefer the newer dedicated gate snapshot so the
    operator console does not show stale queue counters.
    """

    payload = read_json(PROJECT_ROOT / "results" / "data_health" / "extended_data_gates_latest.json", {})
    if not isinstance(payload, dict) or payload.get("task_type") != "extended_data_gates":
        return {}
    chain = payload.get("gate_chain") if isinstance(payload.get("gate_chain"), dict) else {}
    queue = chain.get("queue") if isinstance(chain.get("queue"), dict) else {}
    minute = chain.get("minute") if isinstance(chain.get("minute"), dict) else {}
    retry = chain.get("retry_plan") if isinstance(chain.get("retry_plan"), dict) else {}
    audit = chain.get("import_audit") if isinstance(chain.get("import_audit"), dict) else {}
    verify = chain.get("import_verification") if isinstance(chain.get("import_verification"), dict) else {}
    policy = payload.get("operator_action_policy") if isinstance(payload.get("operator_action_policy"), dict) else {}
    return {
        "data_gate_summary_source": "extended_data_gates_latest",
        "data_gate_summary_generated_at": payload.get("generated_at") or "",
        "tushare_queue_status": queue.get("status") or "",
        "tushare_queue_stage": queue.get("current_stage") or "",
        "extended_data_queue_stage_progress": queue.get("stage_position_label") or "",
        "extended_data_queue_stage_requested_count": _int(queue.get("stage_requested_count")),
        "extended_data_queue_stage_completed_count": _int(queue.get("stage_completed_count")),
        "extended_data_queue_stage_remaining_count": _int(queue.get("stage_remaining_count")),
        "extended_data_queue_stage_remaining_keys": _text_list(queue.get("stage_remaining_keys")),
        "extended_data_queue_stage_current_elapsed_seconds": queue.get("stage_current_elapsed_seconds"),
        "extended_data_queue_stage_last_event_age_seconds": queue.get("stage_last_event_age_seconds"),
        "extended_data_queue_stage_last_event_status": queue.get("stage_last_event_status") or "",
        "extended_data_current_gate": payload.get("current_gate") or "",
        "extended_data_gate_suggested_next_action": payload.get("suggested_next_action") or "",
        "extended_data_operator_wait_reason": payload.get("operator_wait_reason") or "",
        "extended_data_queue_ok_count": _int(queue.get("ok_count")),
        "extended_data_queue_fail_count": _int(queue.get("fail_count")),
        "extended_data_queue_ssl_eof_count": _int(queue.get("ssl_eof_count")),
        "extended_data_queue_timeout_count": _int(queue.get("timeout_count")),
        "extended_data_queue_traceback_count": _int(queue.get("traceback_count")),
        "extended_data_queue_stdout_age_seconds": queue.get("stdout_age_seconds"),
        "extended_data_queue_stderr_age_seconds": queue.get("stderr_age_seconds"),
        "extended_data_queue_liveness": queue.get("queue_liveness") or "",
        "extended_data_queue_progress_state": queue.get("progress_state") or "",
        "extended_data_queue_ok_delta": queue.get("ok_delta"),
        "extended_data_queue_fail_delta": queue.get("fail_delta"),
        "extended_data_queue_traceback_delta": queue.get("traceback_delta"),
        "tushare_minute_status": minute.get("status") or "",
        "tushare_minute_coverage_status": minute.get("coverage_status") or "",
        "tushare_minute_symbol_done_count": _int(minute.get("symbol_done_count")),
        "tushare_minute_symbol_count": _int(minute.get("symbol_count")),
        "tushare_minute_last_symbol": minute.get("last_symbol") or "",
        "tushare_minute_target_symbol_count": _int(minute.get("target_symbol_count")),
        "tushare_minute_current_available_count": _int(minute.get("current_available_count")),
        "tushare_minute_current_available_count_source": minute.get("current_available_count_source") or "",
        "tushare_minute_canonical_current_available_count": _int(minute.get("canonical_current_available_count")),
        "tushare_minute_staged_current_not_imported_count": _int(minute.get("staged_current_not_imported_count")),
        "tushare_minute_canonical_current_available_ratio": minute.get("canonical_current_available_ratio"),
        "tushare_minute_staged_current_not_imported_ratio": minute.get("staged_current_not_imported_ratio"),
        "tushare_minute_missing_or_stale_count": _int(minute.get("missing_or_stale_count")),
        "tushare_minute_current_available_ratio": minute.get("current_available_ratio"),
        "tushare_minute_canonical_file_count": _int(minute.get("canonical_file_count")),
        "tushare_minute_latest_completed_job_path": minute.get("latest_completed_job_path") or "",
        "tushare_minute_latest_completed_job_symbol_count": _int(minute.get("latest_completed_job_symbol_count")),
        "tushare_minute_latest_completed_job_canonical_current_count": _int(
            minute.get("latest_completed_job_canonical_current_count")
        ),
        "tushare_minute_latest_completed_job_missing_or_stale_count": _int(
            minute.get("latest_completed_job_missing_or_stale_count")
        ),
        "tushare_minute_latest_completed_quality_status": minute.get("latest_completed_quality_status") or "",
        "tushare_minute_latest_completed_quality_total_symbols": _int(
            minute.get("latest_completed_quality_total_symbols")
        ),
        "tushare_minute_latest_completed_quality_usable_count": _int(
            minute.get("latest_completed_quality_usable_count")
        ),
        "tushare_minute_latest_completed_quality_invalid_count": _int(
            minute.get("latest_completed_quality_invalid_count")
        ),
        "tushare_minute_latest_completed_quality_missing_count": _int(
            minute.get("latest_completed_quality_missing_count")
        ),
        "tushare_minute_latest_completed_quality_target_date": minute.get("latest_completed_quality_target_date") or "",
        "tushare_minute_latest_completed_quality_suggested_next_action": (
            minute.get("latest_completed_quality_suggested_next_action") or ""
        ),
        "tushare_minute_coverage_suggested_next_action": minute.get("suggested_next_action") or "",
        "extended_data_manual_review_required": _bool(policy.get("manual_review_required")),
        "extended_data_manual_review_reason": policy.get("manual_review_reason") or "",
        "extended_data_requires_user_confirmation": _bool(policy.get("requires_user_confirmation")),
        "extended_data_confirmation_required_command": policy.get("confirmation_required_command") or "",
        "extended_data_safe_next_commands": _text_list(policy.get("safe_next_commands")),
        "extended_data_blocked_without_confirmation": _text_list(policy.get("blocked_without_confirmation")),
        "tushare_retry_gate": retry.get("retry_gate") or "",
        "extended_import_preapply_gate": audit.get("preapply_gate") or "",
        "extended_import_domain_counts": _int_dict(audit.get("domain_counts")),
        "extended_import_missing_planned_domains": _text_list(audit.get("missing_planned_domains")),
        "extended_import_duplicate_destination_count": _int(audit.get("duplicate_destination_count")),
        "extended_import_existing_destination_count": _int(audit.get("existing_destination_count")),
        "extended_import_would_improve_domains": _text_list(audit.get("would_improve_domains")),
        "extended_import_max_files_per_rule": _int(audit.get("max_files_per_rule")),
        "extended_import_source_scan_limit_applied": _bool(audit.get("source_scan_limit_applied")),
        "extended_import_source_scan_limited_domains": _text_list(audit.get("source_scan_limited_domains")),
        "extended_import_source_scan_limited_rule_count": _int(audit.get("source_scan_limited_rule_count")),
        "extended_import_full_audit_command": audit.get("full_audit_command") or "",
        "extended_import_source_scan_full_audit_command": audit.get("source_scan_full_audit_command") or "",
        "extended_import_gate": verify.get("post_import_gate") or "",
        "extended_import_canonical_presence_ratio": verify.get("canonical_presence_ratio"),
    }


def _data_health_command_details(row: dict[str, Any]) -> dict[str, Any]:
    payload = read_json(PROJECT_ROOT / "results" / "data_health" / "data_health_dashboard_latest.json", {})
    if not isinstance(payload, dict) or not payload:
        payload = read_json(PROJECT_ROOT / "results" / "data_health_dashboard_latest.json", {})
    payload = payload if isinstance(payload, dict) else {}
    summary = payload.get("data_health_summary") if isinstance(payload.get("data_health_summary"), dict) else {}
    output_paths = payload.get("output_paths") if isinstance(payload.get("output_paths"), dict) else {}
    result_path = output_paths.get("dashboard_json") or "results/data_health/data_health_dashboard_latest.json"
    report_path = output_paths.get("report") or "reports/data_health_dashboard_report_latest.md"
    suggested = payload.get("suggested_next_action") or row.get("suggested_next_action") or ""
    gate_summary = {
        "tushare_queue_status": summary.get("tushare_queue_status") or "",
        "tushare_queue_stage": summary.get("tushare_queue_stage") or "",
        "extended_data_current_gate": summary.get("extended_data_current_gate") or "",
        "extended_data_gate_suggested_next_action": summary.get("extended_data_gate_suggested_next_action") or "",
        "extended_data_operator_wait_reason": summary.get("extended_data_operator_wait_reason") or "",
        "extended_data_queue_stage_progress": summary.get("extended_data_queue_stage_progress") or "",
        "extended_data_queue_stage_requested_count": _int(summary.get("extended_data_queue_stage_requested_count")),
        "extended_data_queue_stage_completed_count": _int(summary.get("extended_data_queue_stage_completed_count")),
        "extended_data_queue_stage_remaining_count": _int(summary.get("extended_data_queue_stage_remaining_count")),
        "extended_data_queue_stage_remaining_keys": _text_list(summary.get("extended_data_queue_stage_remaining_keys")),
        "extended_data_queue_stage_current_elapsed_seconds": summary.get("extended_data_queue_stage_current_elapsed_seconds"),
        "extended_data_queue_stage_last_event_age_seconds": summary.get("extended_data_queue_stage_last_event_age_seconds"),
        "extended_data_queue_stage_last_event_status": summary.get("extended_data_queue_stage_last_event_status") or "",
        "extended_data_queue_ok_count": _int(summary.get("extended_data_queue_ok_count")),
        "extended_data_queue_fail_count": _int(summary.get("extended_data_queue_fail_count")),
        "extended_data_queue_ssl_eof_count": _int(summary.get("extended_data_queue_ssl_eof_count")),
        "extended_data_queue_timeout_count": _int(summary.get("extended_data_queue_timeout_count")),
        "extended_data_queue_traceback_count": _int(summary.get("extended_data_queue_traceback_count")),
        "extended_data_queue_stdout_age_seconds": summary.get("extended_data_queue_stdout_age_seconds"),
        "extended_data_queue_stderr_age_seconds": summary.get("extended_data_queue_stderr_age_seconds"),
        "extended_data_queue_liveness": summary.get("extended_data_queue_liveness") or "",
        "extended_data_queue_progress_state": summary.get("extended_data_queue_progress_state") or "",
        "extended_data_queue_ok_delta": summary.get("extended_data_queue_ok_delta"),
        "extended_data_queue_fail_delta": summary.get("extended_data_queue_fail_delta"),
        "extended_data_queue_traceback_delta": summary.get("extended_data_queue_traceback_delta"),
        "tushare_minute_status": summary.get("tushare_minute_status") or "",
        "tushare_minute_coverage_status": summary.get("tushare_minute_coverage_status") or "",
        "tushare_minute_symbol_done_count": _int(summary.get("tushare_minute_symbol_done_count")),
        "tushare_minute_symbol_count": _int(summary.get("tushare_minute_symbol_count")),
        "tushare_minute_last_symbol": summary.get("tushare_minute_last_symbol") or "",
        "tushare_minute_target_symbol_count": _int(summary.get("tushare_minute_target_symbol_count")),
        "tushare_minute_current_available_count": _int(summary.get("tushare_minute_current_available_count")),
        "tushare_minute_current_available_count_source": summary.get("tushare_minute_current_available_count_source") or "",
        "tushare_minute_canonical_current_available_count": _int(
            summary.get("tushare_minute_canonical_current_available_count")
        ),
        "tushare_minute_staged_current_not_imported_count": _int(
            summary.get("tushare_minute_staged_current_not_imported_count")
        ),
        "tushare_minute_canonical_current_available_ratio": summary.get(
            "tushare_minute_canonical_current_available_ratio"
        ),
        "tushare_minute_staged_current_not_imported_ratio": summary.get(
            "tushare_minute_staged_current_not_imported_ratio"
        ),
        "tushare_minute_missing_or_stale_count": _int(summary.get("tushare_minute_missing_or_stale_count")),
        "tushare_minute_current_available_ratio": summary.get("tushare_minute_current_available_ratio"),
        "tushare_minute_canonical_file_count": _int(summary.get("tushare_minute_canonical_file_count")),
        "tushare_minute_latest_completed_job_path": summary.get("tushare_minute_latest_completed_job_path") or "",
        "tushare_minute_latest_completed_job_symbol_count": _int(
            summary.get("tushare_minute_latest_completed_job_symbol_count")
        ),
        "tushare_minute_latest_completed_job_canonical_current_count": _int(
            summary.get("tushare_minute_latest_completed_job_canonical_current_count")
        ),
        "tushare_minute_latest_completed_job_missing_or_stale_count": _int(
            summary.get("tushare_minute_latest_completed_job_missing_or_stale_count")
        ),
        "tushare_minute_latest_completed_quality_status": summary.get("tushare_minute_latest_completed_quality_status") or "",
        "tushare_minute_latest_completed_quality_total_symbols": _int(
            summary.get("tushare_minute_latest_completed_quality_total_symbols")
        ),
        "tushare_minute_latest_completed_quality_usable_count": _int(
            summary.get("tushare_minute_latest_completed_quality_usable_count")
        ),
        "tushare_minute_latest_completed_quality_invalid_count": _int(
            summary.get("tushare_minute_latest_completed_quality_invalid_count")
        ),
        "tushare_minute_latest_completed_quality_missing_count": _int(
            summary.get("tushare_minute_latest_completed_quality_missing_count")
        ),
        "tushare_minute_latest_completed_quality_target_date": (
            summary.get("tushare_minute_latest_completed_quality_target_date") or ""
        ),
        "tushare_minute_latest_completed_quality_suggested_next_action": (
            summary.get("tushare_minute_latest_completed_quality_suggested_next_action") or ""
        ),
        "tushare_minute_coverage_suggested_next_action": summary.get("tushare_minute_coverage_suggested_next_action") or "",
        "extended_data_manual_review_required": _bool(summary.get("extended_data_manual_review_required")),
        "extended_data_manual_review_reason": summary.get("extended_data_manual_review_reason") or "",
        "extended_data_requires_user_confirmation": _bool(summary.get("extended_data_requires_user_confirmation")),
        "extended_data_confirmation_required_command": summary.get("extended_data_confirmation_required_command") or "",
        "extended_data_safe_next_commands": _text_list(summary.get("extended_data_safe_next_commands")),
        "extended_data_blocked_without_confirmation": _text_list(summary.get("extended_data_blocked_without_confirmation")),
        "tushare_retry_gate": summary.get("tushare_retry_gate") or "",
        "extended_import_preapply_gate": summary.get("extended_import_preapply_gate") or "",
        "extended_import_domain_counts": _int_dict(summary.get("extended_import_domain_counts")),
        "extended_import_missing_planned_domains": _text_list(summary.get("extended_import_missing_planned_domains")),
        "extended_import_duplicate_destination_count": _int(summary.get("extended_import_duplicate_destination_count")),
        "extended_import_existing_destination_count": _int(summary.get("extended_import_existing_destination_count")),
        "extended_import_would_improve_domains": _text_list(summary.get("extended_import_would_improve_domains")),
        "extended_import_max_files_per_rule": _int(summary.get("extended_import_max_files_per_rule")),
        "extended_import_source_scan_limit_applied": _bool(summary.get("extended_import_source_scan_limit_applied")),
        "extended_import_source_scan_limited_domains": _text_list(summary.get("extended_import_source_scan_limited_domains")),
        "extended_import_source_scan_limited_rule_count": _int(summary.get("extended_import_source_scan_limited_rule_count")),
        "extended_import_full_audit_command": summary.get("extended_import_full_audit_command") or "",
        "extended_import_source_scan_full_audit_command": summary.get("extended_import_source_scan_full_audit_command") or "",
        "extended_import_gate": summary.get("extended_import_gate") or "",
        "extended_import_canonical_presence_ratio": summary.get("extended_import_canonical_presence_ratio"),
    }
    latest_gate_summary = _latest_data_gates_summary()
    if latest_gate_summary:
        gate_summary.update(latest_gate_summary)
        latest_gate_action = str(gate_summary.get("extended_data_gate_suggested_next_action") or "").strip()
        if _is_data_gate_suggestion(latest_gate_action):
            suggested = latest_gate_action
    return {
        "last_status": row.get("status") or payload.get("status") or "missing",
        "warning_count": len(payload.get("warnings") or row.get("warnings") or []),
        "failed_count": 1 if payload.get("status") == "failed" or row.get("status") == "failed" else 0,
        "report_path": report_path,
        "result_path": result_path,
        "output_paths": [result_path, report_path],
        "data_health_summary": summary,
        "data_gate_summary": gate_summary,
        "suggested_next_action": _replace_target_tokens(_safe_suggested_next_action(suggested, "python scripts/qd.py data-health --network false --summary true")),
    }


def build_command_center_status(
    *,
    log_path: str | Path = "results/operation_log/operation_timeline.jsonl",
    recent: int = 20,
) -> dict[str, Any]:
    records = load_operation_log(log_path)
    latest = collect_latest_command_status(records)
    commands: list[dict[str, Any]] = []
    for name in COMMAND_ALIASES:
        row = latest.get(name) or {}
        item = {
            "command": name,
            "last_run_at": row.get("finished_at") or row.get("started_at") or "",
            "last_status": row.get("status") or "missing",
            "output_paths": row.get("output_paths") or [],
            "warnings": row.get("warnings") or [],
            "warning_count": len(row.get("warnings") or []),
            "failed_count": 1 if row.get("status") == "failed" else 0,
            "failed_reason": row.get("failed_reason") or "",
            "suggested_next_action": _replace_target_tokens(row.get("suggested_next_action") or "运行 python scripts/qd.py help 查看推荐入口。"),
        }
        if name == "daily":
            item.update(_daily_command_details(row))
        elif name == "status":
            item.update(_remediation_details("results/status_latest.json", item["suggested_next_action"]))
        elif name == "fix-plan":
            item.update(_remediation_details("results/fix_plan_latest.json", item["suggested_next_action"]))
        elif name == "data-health":
            item.update(_data_health_command_details(row))
        elif name == "market-intel":
            payload = read_json(PROJECT_ROOT / "results" / "market_intelligence" / "market_intelligence_latest.json", {})
            payload = payload if isinstance(payload, dict) else {}
            target_date = _target_date_from_payload(payload)
            summary = payload.get("market_intelligence_summary") if isinstance(payload.get("market_intelligence_summary"), dict) else {}
            remediation_actions = payload.get("remediation_actions") if isinstance(payload.get("remediation_actions"), list) else []
            remediation_actions = _sanitize_command_fields(remediation_actions, target_date)
            item.update(
                {
                    "last_status": row.get("status") or payload.get("status") or "missing",
                    "warning_count": len(payload.get("warnings") or []),
                    "failed_count": 1 if payload.get("status") == "failed" else 0,
                    "report_path": payload.get("report_path") or "reports/market_intelligence_report_latest.md",
                    "result_path": payload.get("output_path") or "results/market_intelligence/market_intelligence_latest.json",
                    "output_paths": [
                        payload.get("output_path") or "results/market_intelligence/market_intelligence_latest.json",
                        payload.get("report_path") or "reports/market_intelligence_report_latest.md",
                    ],
                    "market_intelligence_summary": summary,
                    "remediation_actions": remediation_actions,
                    "top_remediation": remediation_actions[0] if remediation_actions else {},
                    "suggested_next_action": _replace_target_tokens(_safe_suggested_next_action(
                        payload.get("suggested_next_action"),
                        "python scripts/qd.py market-intel --target-date YYYY-MM-DD --price-limit 20 --network false",
                    ), target_date),
                }
            )
        commands.append(item)
    summary = summarize_recent_operations(records, recent=recent)
    current_failed = [row for row in commands if row.get("last_status") == "failed"]
    current_warnings = [row for row in commands if row.get("warnings") or row.get("last_status") == "warning"]
    current_suggested = _active_data_gate_suggestion(commands)
    if current_failed and not current_suggested:
        current_suggested = _safe_suggested_next_action(summary.get("suggested_next_action"), "")
    if not current_suggested:
        for row in commands:
            top = row.get("top_remediation") if isinstance(row.get("top_remediation"), dict) else {}
            command = top.get("command") if isinstance(top, dict) else ""
            if command:
                current_suggested = _safe_suggested_next_action(command, "")
                break
    if not current_suggested:
        current_suggested = "python scripts/qd.py status"
    status = "warning" if current_failed or current_warnings else "passed"
    warnings = [f"recent warning command: {item}" for item in (summary.get("warning_commands") or [])]
    warnings.extend(f"recent failed command: {item}" for item in (summary.get("failed_commands") or []))
    return {
        "schema_version": API_SCHEMA_VERSION,
        "app_version": APP_VERSION,
        "package_version": PACKAGE_VERSION,
        "api_schema_version": API_SCHEMA_VERSION,
        "status": status,
        "ok": True,
        "generated_at": now_text(),
        "network": False,
        "llm_enabled": False,
        "recent": int(recent),
        "recent_operation_count": summary.get("recent_count", 0),
        "recent_failed_commands": summary.get("failed_commands", []),
        "recent_warning_commands": summary.get("warning_commands", []),
        "warnings": warnings,
        "suggested_next_action": _replace_target_tokens(_safe_suggested_next_action(current_suggested)),
        "commands": commands,
        "disclaimer": DISCLAIMER,
    }


def summarize_command_status(payload: dict[str, Any]) -> dict[str, Any]:
    commands = payload.get("commands") if isinstance(payload.get("commands"), list) else []
    failed = [row for row in commands if row.get("last_status") == "failed"]
    warnings = [row for row in commands if row.get("warnings") or row.get("last_status") == "warning"]
    suggested = _safe_suggested_next_action(payload.get("suggested_next_action"), "")
    if not failed and not _is_data_gate_suggestion(suggested):
        for row in commands:
            top = row.get("top_remediation") if isinstance(row.get("top_remediation"), dict) else {}
            command = top.get("command") if isinstance(top, dict) else ""
            if command:
                suggested = _safe_suggested_next_action(command, "")
                break
        if suggested.startswith("可直接排查："):
            suggested = "python scripts/qd.py status"
    return {
        "recent_operation_count": int(payload.get("recent_operation_count") or 0),
        "failed_count": len(failed),
        "warning_count": len(warnings),
        "failed_commands": [row.get("command") for row in failed],
        "warning_commands": [row.get("command") for row in warnings],
        "suggested_next_action": _replace_target_tokens(_safe_suggested_next_action(suggested)),
    }


def write_command_center_status(payload: dict[str, Any], output_path: str | Path = DEFAULT_OUTPUT_PATH) -> Path:
    return write_json(_project_path(output_path), payload)
