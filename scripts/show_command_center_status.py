from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from quantdesk.command_center_status import (  # noqa: E402
    build_command_center_status,
    summarize_command_status,
    write_command_center_status,
)
from quantdesk.operation_log import log_operation  # noqa: E402
from quantdesk.shared import now_text  # noqa: E402


def _truthy(value: Any) -> bool:
    return str(value).strip().lower() in {"1", "true", "yes", "y", "on"}


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Show QuantDesk local command center status.")
    parser.add_argument("--output", default="results/command_center_status_latest.json")
    parser.add_argument("--recent", type=int, default=20)
    parser.add_argument("--json", nargs="?", const="true", default="false")
    parser.add_argument("--summary", nargs="?", const="true", default="false", help="Accepted for qd.py consistency; JSON output is already bounded.")
    parser.add_argument("--network", default="false", help="Accepted for qd.py consistency; command-center is read-only.")
    return parser.parse_args(argv)


def run(args: argparse.Namespace) -> dict[str, Any]:
    payload = build_command_center_status(recent=args.recent)
    write_command_center_status(payload, args.output)
    payload["output_path"] = args.output
    return payload


def _top_remediation(payload: dict[str, Any]) -> dict[str, Any]:
    for row in payload.get("commands", []) or []:
        if not isinstance(row, dict) or row.get("command") != "data-health":
            continue
        gate = row.get("data_gate_summary") if isinstance(row.get("data_gate_summary"), dict) else {}
        current_gate = str(gate.get("extended_data_current_gate") or "").strip()
        wait_reason = str(gate.get("extended_data_operator_wait_reason") or "").strip()
        if current_gate != "download_queue" and wait_reason != "download_queue_active":
            continue
        safe_commands = gate.get("extended_data_safe_next_commands") if isinstance(gate.get("extended_data_safe_next_commands"), list) else []
        command = str(safe_commands[0]).strip() if safe_commands else "python scripts/qd.py tushare-queue-status --summary true"
        blocked_commands = (
            gate.get("extended_data_blocked_without_confirmation")
            if isinstance(gate.get("extended_data_blocked_without_confirmation"), list)
            else []
        )
        return {
            "title": "扩展数据下载队列仍在运行",
            "warning_kind": "download_queue_active",
            "command": command,
            "network_required": False,
            "blocked_without_confirmation": [str(item) for item in blocked_commands if str(item).strip()],
            "why": "download_queue_active; use read-only data-gates monitoring before any update/import/apply command.",
        }
    for row in payload.get("commands", []) or []:
        if isinstance(row, dict) and isinstance(row.get("top_remediation"), dict) and row.get("top_remediation"):
            return row["top_remediation"]
    return {}


def _int(value: Any) -> int:
    try:
        return int(value or 0)
    except Exception:
        return 0


def _optional_int_text(value: Any) -> str:
    if value is None:
        return ""
    text = str(value).strip()
    if not text:
        return ""
    try:
        return str(int(float(text)))
    except Exception:
        return ""


def _data_gate_console_lines(payload: dict[str, Any]) -> list[str]:
    commands = payload.get("commands") if isinstance(payload.get("commands"), list) else []
    data_health = next((row for row in commands if isinstance(row, dict) and row.get("command") == "data-health"), {})
    gate = data_health.get("data_gate_summary") if isinstance(data_health.get("data_gate_summary"), dict) else {}
    current_gate = str(gate.get("extended_data_current_gate") or "").strip()
    if not current_gate:
        return []
    lines = [
        (
            f"data-gates: {current_gate} / "
            f"{gate.get('extended_data_queue_liveness') or 'unknown'} / "
            f"ok={_int(gate.get('extended_data_queue_ok_count'))} / "
            f"fail={_int(gate.get('extended_data_queue_fail_count'))} / "
            f"ssl_eof={_int(gate.get('extended_data_queue_ssl_eof_count'))} / "
            f"timeout={_int(gate.get('extended_data_queue_timeout_count'))} / "
            f"traceback={_int(gate.get('extended_data_queue_traceback_count'))} / "
            f"stdout_age={_int(gate.get('extended_data_queue_stdout_age_seconds'))}s / "
            f"stderr_age={_int(gate.get('extended_data_queue_stderr_age_seconds'))}s"
        )
    ]
    source = str(gate.get("data_gate_summary_source") or "").strip()
    generated_at = str(gate.get("data_gate_summary_generated_at") or "").strip()
    if source or generated_at:
        lines.append(f"data-gates source: {source or '-'} / generated_at={generated_at or '-'}")
    progress = str(gate.get("extended_data_queue_progress_state") or "").strip()
    if progress:
        lines.append(
            f"data-gates progress: {progress} / "
            f"ok_delta={gate.get('extended_data_queue_ok_delta')} / "
            f"fail_delta={gate.get('extended_data_queue_fail_delta')} / "
            f"traceback_delta={gate.get('extended_data_queue_traceback_delta')}"
        )
    stage_progress = str(gate.get("extended_data_queue_stage_progress") or "").strip()
    if stage_progress:
        remaining_keys = gate.get("extended_data_queue_stage_remaining_keys")
        remaining_text = ", ".join(str(item) for item in remaining_keys) if isinstance(remaining_keys, list) else "-"
        stage_parts = [
            f"data-gates queue stage: {stage_progress}",
            f"completed={_int(gate.get('extended_data_queue_stage_completed_count'))}",
            f"remaining={_int(gate.get('extended_data_queue_stage_remaining_count'))}",
            f"next={remaining_text or '-'}",
        ]
        stage_elapsed = _optional_int_text(gate.get("extended_data_queue_stage_current_elapsed_seconds"))
        if stage_elapsed:
            stage_parts.append(f"stage_elapsed={stage_elapsed}s")
        stage_event_age = _optional_int_text(gate.get("extended_data_queue_stage_last_event_age_seconds"))
        if stage_event_age:
            stage_parts.append(f"stage_event_age={stage_event_age}s")
        lines.append(" / ".join(stage_parts))
    minute_target = _int(gate.get("tushare_minute_target_symbol_count"))
    if minute_target:
        lines.append(
            "data-gates minute pool: "
            f"downloaded={_int(gate.get('tushare_minute_symbol_done_count'))}/{_int(gate.get('tushare_minute_symbol_count'))}, "
            f"available={_int(gate.get('tushare_minute_current_available_count'))}/{minute_target}, "
            f"canonical={_int(gate.get('tushare_minute_canonical_current_available_count'))}, "
            f"staged_pending_import={_int(gate.get('tushare_minute_staged_current_not_imported_count'))}, "
            f"missing_or_stale={_int(gate.get('tushare_minute_missing_or_stale_count'))}, "
            f"last={gate.get('tushare_minute_last_symbol') or '-'}"
        )
    completed_minute_total = _int(gate.get("tushare_minute_latest_completed_job_symbol_count"))
    if completed_minute_total:
        lines.append(
            "data-gates completed minute job: "
            f"available={_int(gate.get('tushare_minute_latest_completed_job_canonical_current_count'))}/{completed_minute_total}, "
            f"missing_or_stale={_int(gate.get('tushare_minute_latest_completed_job_missing_or_stale_count'))}, "
            f"canonical_files={_int(gate.get('tushare_minute_canonical_file_count'))}"
        )
    completed_quality_total = _int(gate.get("tushare_minute_latest_completed_quality_total_symbols"))
    if completed_quality_total:
        lines.append(
            "data-gates completed minute quality: "
            f"status={gate.get('tushare_minute_latest_completed_quality_status') or '-'}, "
            f"usable={_int(gate.get('tushare_minute_latest_completed_quality_usable_count'))}/{completed_quality_total}, "
            f"invalid={_int(gate.get('tushare_minute_latest_completed_quality_invalid_count'))}, "
            f"missing={_int(gate.get('tushare_minute_latest_completed_quality_missing_count'))}, "
            f"target={gate.get('tushare_minute_latest_completed_quality_target_date') or '-'}"
        )
    domain_counts = gate.get("extended_import_domain_counts") if isinstance(gate.get("extended_import_domain_counts"), dict) else {}
    if domain_counts:
        domain_text = ", ".join(f"{key}={domain_counts[key]}" for key in sorted(domain_counts))
        lines.append(f"data-gates staged domains: {domain_text}")
    if gate.get("extended_import_source_scan_limit_applied"):
        limited_domains = gate.get("extended_import_source_scan_limited_domains")
        limited_text = ", ".join(str(item) for item in limited_domains) if isinstance(limited_domains, list) else "-"
        lines.append(
            "data-gates staged domain counts are bounded: "
            f"max_files_per_rule={_int(gate.get('extended_import_max_files_per_rule'))}, "
            f"limited_domains={limited_text or '-'}"
        )
        full_audit_command = str(gate.get("extended_import_source_scan_full_audit_command") or "").strip()
        if not full_audit_command:
            full_audit_command = str(gate.get("extended_import_full_audit_command") or "").strip()
        if full_audit_command:
            lines.append(f"data-gates full audit: {full_audit_command}")
    missing_domains = (
        gate.get("extended_import_missing_planned_domains")
        if isinstance(gate.get("extended_import_missing_planned_domains"), list)
        else []
    )
    if missing_domains:
        lines.append("data-gates missing staged domains: " + ", ".join(str(item) for item in missing_domains))
    reason = str(gate.get("extended_data_operator_wait_reason") or "").strip()
    if reason:
        lines.append(f"data-gates reason: {reason}")
    if gate.get("extended_data_manual_review_required"):
        manual_reason = str(gate.get("extended_data_manual_review_reason") or "-").strip()
        lines.append(f"data-gates manual review: {manual_reason}")
    if gate.get("extended_data_requires_user_confirmation"):
        command = str(gate.get("extended_data_confirmation_required_command") or "-").strip()
        lines.append(f"data-gates confirmation required: {command}")
    safe_commands = gate.get("extended_data_safe_next_commands") if isinstance(gate.get("extended_data_safe_next_commands"), list) else []
    if safe_commands:
        lines.append(f"data-gates safe next: {safe_commands[0]}")
    blocked_commands = (
        gate.get("extended_data_blocked_without_confirmation")
        if isinstance(gate.get("extended_data_blocked_without_confirmation"), list)
        else []
    )
    if blocked_commands:
        lines.append(f"data-gates blocked without confirmation: {blocked_commands[0]}")
    suggested = str(gate.get("extended_data_gate_suggested_next_action") or "").strip()
    if suggested:
        lines.append(f"data-gates next: {suggested}")
    return lines


def main(argv: list[str] | None = None) -> int:
    started_at = now_text()
    start_time = time.perf_counter()
    args = _parse_args(argv)
    try:
        payload = run(args)
        summary = summarize_command_status(payload)
        if _truthy(args.json):
            print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
            console_output = False
        else:
            console_output = True
        if not console_output:
            log_operation(
                command="command-center",
                args=sys.argv[1:] if argv is None else argv,
                network=False,
                llm_enabled=False,
                status="warning" if summary.get("failed_count") or summary.get("warning_count") else "passed",
                started_at=started_at,
                start_time=start_time,
                output_paths=[args.output],
                warnings=summary.get("warning_commands"),
                suggested_next_action=_top_remediation(payload).get("command") or summary.get("suggested_next_action"),
            )
            return 0
        print("QuantDesk 命令中心")
        print(f"最近运行命令数量: {summary.get('recent_operation_count')}")
        print(f"最近失败命令: {', '.join(summary.get('failed_commands') or []) or '-'}")
        print(f"最近 warning: {', '.join(summary.get('warning_commands') or []) or '-'}")
        print(f"建议下一步: {summary.get('suggested_next_action')}")
        for line in _data_gate_console_lines(payload):
            print(line)
        daily_row = next((row for row in payload.get("commands", []) if row.get("command") == "daily"), {})
        if daily_row:
            print(f"daily: {daily_row.get('last_status') or 'missing'} / warning_count={daily_row.get('warning_count', 0)} / failed_count={daily_row.get('failed_count', 0)}")
            print(f"daily report: {daily_row.get('report_path') or '未生成'}")
            print(f"daily result: {daily_row.get('result_path') or '未生成'}")
        market_row = next((row for row in payload.get("commands", []) if row.get("command") == "market-intel"), {})
        if market_row:
            summary = market_row.get("market_intelligence_summary") if isinstance(market_row.get("market_intelligence_summary"), dict) else {}
            print(
                "market-intel: "
                f"{market_row.get('last_status') or 'missing'} / "
                f"hot_sectors={summary.get('hot_sector_count', 0)} / "
                f"candidates={summary.get('candidate_count', 0)}"
            )
            print(f"market report: {market_row.get('report_path') or '未生成'}")
        top = _top_remediation(payload)
        if top:
            print(f"remediation: {top.get('title') or '-'}")
            print(f"fix command: {top.get('command') or '-'}")
        print(f"status json: {args.output}")
        print("命令中心只读取本地 operation_log，不联网、不调用 LLM。")
        log_operation(
            command="command-center",
            args=sys.argv[1:] if argv is None else argv,
            network=False,
            llm_enabled=False,
            status="warning" if summary.get("failed_count") or summary.get("warning_count") else "passed",
            started_at=started_at,
            start_time=start_time,
            output_paths=[args.output],
            warnings=summary.get("warning_commands"),
            suggested_next_action=top.get("command") or summary.get("suggested_next_action"),
        )
        return 0
    except Exception as exc:  # noqa: BLE001
        log_operation(
            command="command-center",
            args=sys.argv[1:] if argv is None else argv,
            network=False,
            llm_enabled=False,
            status="failed",
            started_at=started_at,
            start_time=start_time,
            failed_reason=str(exc),
            suggested_next_action="检查 results/operation_log 是否可读写后重试。",
        )
        print(f"command-center failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
