from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from quantdesk.extended_data_gates import (  # noqa: E402
    DEFAULT_OUTPUT_PATH,
    DEFAULT_REPORT_PATH,
    write_extended_data_gates_outputs,
)


def _truthy(value: Any) -> bool:
    return str(value).strip().lower() in {"1", "true", "yes", "y", "on"}


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Show the read-only extended-data gate chain: queue -> retry -> audit -> verification."
    )
    parser.add_argument("--project-root", default=str(PROJECT_ROOT), help="Project root to inspect; mainly for tests.")
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT_PATH))
    parser.add_argument("--report", default=str(DEFAULT_REPORT_PATH))
    parser.add_argument("--max-files-per-rule", type=int, default=20)
    parser.add_argument("--json", nargs="?", const="true", default="false")
    parser.add_argument("--summary", nargs="?", const="true", default="false")
    parser.add_argument("--network", default="false", help="Accepted for qd.py consistency; this command never fetches network data.")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    payload = write_extended_data_gates_outputs(
        project_root=args.project_root,
        output_path=args.output,
        report_path=args.report,
        max_files_per_rule=args.max_files_per_rule,
    )
    if _truthy(args.json):
        print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
        return 0

    chain = payload.get("gate_chain") if isinstance(payload.get("gate_chain"), dict) else {}
    policy = payload.get("operator_action_policy") if isinstance(payload.get("operator_action_policy"), dict) else {}
    queue = chain.get("queue") if isinstance(chain.get("queue"), dict) else {}
    minute = chain.get("minute") if isinstance(chain.get("minute"), dict) else {}
    retry = chain.get("retry_plan") if isinstance(chain.get("retry_plan"), dict) else {}
    audit = chain.get("import_audit") if isinstance(chain.get("import_audit"), dict) else {}
    verify = chain.get("import_verification") if isinstance(chain.get("import_verification"), dict) else {}
    print("QuantDesk extended data gates")
    print(f"status: {payload.get('status')}")
    print(f"current_gate: {payload.get('current_gate')}")
    print(f"suggested_next_action: {payload.get('suggested_next_action')}")
    print(f"operator_wait_reason: {payload.get('operator_wait_reason')}")
    print(f"manual_review_required: {policy.get('manual_review_required')}")
    print(f"manual_review_reason: {policy.get('manual_review_reason') or '-'}")
    print(f"requires_user_confirmation: {policy.get('requires_user_confirmation')}")
    print(f"confirmation_required_command: {policy.get('confirmation_required_command') or '-'}")
    safe_commands = policy.get("safe_next_commands") if isinstance(policy.get("safe_next_commands"), list) else []
    if safe_commands:
        print(f"safe_next_command: {safe_commands[0]}")
    print(f"queue_status: {queue.get('status') or '-'}")
    print(f"queue_stage: {queue.get('current_stage') or '-'}")
    print(f"queue_stage_progress: {queue.get('stage_position_label') or '-'}")
    print(f"queue_stage_completed_count: {queue.get('stage_completed_count', 0)}")
    print(f"queue_stage_remaining_count: {queue.get('stage_remaining_count', 0)}")
    remaining = queue.get("stage_remaining_keys") if isinstance(queue.get("stage_remaining_keys"), list) else []
    print(f"queue_stage_remaining_keys: {', '.join(str(item) for item in remaining) or '-'}")
    print(f"queue_stage_current_elapsed_seconds: {queue.get('stage_current_elapsed_seconds')}")
    print(f"queue_stage_last_event_age_seconds: {queue.get('stage_last_event_age_seconds')}")
    print(f"queue_ok_count: {queue.get('ok_count', 0)}")
    print(f"queue_fail_count: {queue.get('fail_count', 0)}")
    print(f"queue_ssl_eof_count: {queue.get('ssl_eof_count', 0)}")
    print(f"queue_timeout_count: {queue.get('timeout_count', 0)}")
    print(f"queue_traceback_count: {queue.get('traceback_count', 0)}")
    print(f"queue_liveness: {queue.get('queue_liveness') or 'unknown'}")
    print(f"queue_progress_state: {queue.get('progress_state') or 'unknown'}")
    print(f"queue_ok_delta: {queue.get('ok_delta')}")
    print(f"queue_fail_delta: {queue.get('fail_delta')}")
    print(f"queue_traceback_delta: {queue.get('traceback_delta')}")
    print(f"queue_stdout_age_seconds: {queue.get('stdout_age_seconds')}")
    print(f"queue_stderr_age_seconds: {queue.get('stderr_age_seconds')}")
    print(f"minute_status: {minute.get('status') or '-'}")
    print(f"minute_coverage_status: {minute.get('coverage_status') or '-'}")
    print(f"minute_symbol_done_count: {minute.get('symbol_done_count', 0)}")
    print(f"minute_symbol_count: {minute.get('symbol_count', 0)}")
    print(f"minute_last_symbol: {minute.get('last_symbol') or '-'}")
    print(f"minute_target_symbol_count: {minute.get('target_symbol_count', 0)}")
    print(f"minute_current_available_count: {minute.get('current_available_count', 0)}")
    print(f"minute_current_available_count_source: {minute.get('current_available_count_source') or '-'}")
    print(f"minute_canonical_current_available_count: {minute.get('canonical_current_available_count', 0)}")
    print(f"minute_staged_current_not_imported_count: {minute.get('staged_current_not_imported_count', 0)}")
    print(f"minute_missing_or_stale_count: {minute.get('missing_or_stale_count', 0)}")
    print(f"minute_current_available_ratio: {minute.get('current_available_ratio', 0.0)}")
    print(f"minute_canonical_current_available_ratio: {minute.get('canonical_current_available_ratio', 0.0)}")
    print(f"minute_staged_current_not_imported_ratio: {minute.get('staged_current_not_imported_ratio', 0.0)}")
    print(f"minute_canonical_file_count: {minute.get('canonical_file_count', 0)}")
    print(f"minute_latest_completed_job: {minute.get('latest_completed_job_path') or '-'}")
    print(
        "minute_latest_completed_job_canonical_current: "
        f"{minute.get('latest_completed_job_canonical_current_count', 0)}/"
        f"{minute.get('latest_completed_job_symbol_count', 0)}"
    )
    print(f"minute_latest_completed_quality_status: {minute.get('latest_completed_quality_status') or '-'}")
    print(
        "minute_latest_completed_quality_usable: "
        f"{minute.get('latest_completed_quality_usable_count', 0)}/"
        f"{minute.get('latest_completed_quality_total_symbols', 0)}"
    )
    print(f"minute_latest_completed_quality_invalid_count: {minute.get('latest_completed_quality_invalid_count', 0)}")
    print(f"minute_latest_completed_quality_missing_count: {minute.get('latest_completed_quality_missing_count', 0)}")
    print(f"minute_latest_completed_quality_target_date: {minute.get('latest_completed_quality_target_date') or '-'}")
    print(f"minute_suggested_next_action: {minute.get('suggested_next_action') or '-'}")
    print(f"retry_gate: {retry.get('retry_gate') or '-'}")
    print(f"preapply_gate: {audit.get('preapply_gate') or '-'}")
    domain_counts = audit.get("domain_counts") if isinstance(audit.get("domain_counts"), dict) else {}
    if domain_counts:
        print("staged_domain_counts: " + ", ".join(f"{key}={domain_counts[key]}" for key in sorted(domain_counts)))
    print(f"staged_domain_counts_bounded: {bool(audit.get('source_scan_limit_applied'))}")
    print(f"staged_domain_max_files_per_rule: {audit.get('max_files_per_rule') or 0}")
    limited_domains = audit.get("source_scan_limited_domains") if isinstance(audit.get("source_scan_limited_domains"), list) else []
    print(f"staged_domain_limited_domains: {', '.join(str(item) for item in limited_domains) or '-'}")
    print(f"staged_domain_full_audit_command: {audit.get('full_audit_command') or '-'}")
    print(f"staged_domain_source_scan_full_audit_command: {audit.get('source_scan_full_audit_command') or '-'}")
    missing_domains = audit.get("missing_planned_domains") if isinstance(audit.get("missing_planned_domains"), list) else []
    print(f"missing_planned_domains: {', '.join(str(item) for item in missing_domains) or '-'}")
    print(f"post_import_gate: {verify.get('post_import_gate') or '-'}")
    print(f"canonical_presence_ratio: {verify.get('canonical_presence_ratio')}")
    print(f"output: {payload.get('output_path')}")
    print(f"report: {payload.get('report_path')}")
    print("read-only / research-only / no network fetch / no canonical writes / no broker")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
