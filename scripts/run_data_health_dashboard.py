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

from quantdesk.cli_summary import build_cli_summary, format_human_summary  # noqa: E402
from quantdesk.daily_data_chain import latest_daily_data_chain  # noqa: E402
from quantdesk.daily_scheduler_status import daily_data_scheduler_status  # noqa: E402
from quantdesk.data_chain_consumer_gate import evaluate_data_chain_consumer_gate  # noqa: E402
from quantdesk.data_freshness_summary import build_data_freshness_summary  # noqa: E402
from quantdesk.data_grade_trend import build_data_grade_trend_summary  # noqa: E402
from quantdesk.data_health_dashboard import (  # noqa: E402
    _snapshot_status_with_chain,
    build_formal_daily_chain_status,
    build_minute_universe_coverage,
)
from quantdesk.evidence_freshness import build_evidence_freshness  # noqa: E402
from quantdesk.extended_data_commands import FULL_AUDIT_COMMAND  # noqa: E402
from quantdesk.extended_data_gates import (  # noqa: E402
    build_extended_data_gates_from_payloads,
    render_extended_data_gates_report,
)
from quantdesk.extended_data_readiness import write_extended_data_readiness_outputs  # noqa: E402
from quantdesk.extended_import_verifier import write_extended_import_verification_outputs  # noqa: E402
from quantdesk.extended_staged_data_importer import build_extended_staged_import_audit  # noqa: E402
from quantdesk.intraday.intraday_coverage_dashboard import build_intraday_coverage_summary  # noqa: E402
from quantdesk.operation_log import log_operation  # noqa: E402
from quantdesk.provider_failure_stats import collect_provider_failure_stats  # noqa: E402
from quantdesk.remediation_advisor import build_remediation_plan  # noqa: E402
from quantdesk.research.parameter_sensitivity_audit import summarize_parameter_sensitivity  # noqa: E402
from quantdesk.scope_policy import resolve_scope, scope_fields  # noqa: E402
from quantdesk.shared import now_text, read_json, short_error_text, truthy, write_json, write_text  # noqa: E402
from quantdesk.symbol_display_utils import (  # noqa: E402
    build_symbols_omitted_note,
    format_symbol_preview,
    safe_symbol_count,
)
from quantdesk.trading_calendar import resolve_target_complete_trade_date  # noqa: E402
from quantdesk.tushare_minute_coverage import write_tushare_minute_coverage_outputs  # noqa: E402
from quantdesk.tushare_minute_quality import (  # noqa: E402
    load_latest_tushare_minute_quality_snapshot,
    summarize_completed_minute_quality_snapshot,
)
from quantdesk.tushare_minute_status import write_tushare_minute_status_outputs  # noqa: E402
from quantdesk.tushare_queue_status import write_tushare_queue_status_outputs  # noqa: E402
from quantdesk.tushare_retry_plan import write_tushare_retry_plan_outputs  # noqa: E402
from quantdesk.version import API_SCHEMA_VERSION, APP_VERSION, PACKAGE_VERSION  # noqa: E402

REPORT_PATH = PROJECT_ROOT / "reports" / "data_health_dashboard_report_latest.md"
PARAMETER_SENSITIVITY_LATEST_JSON = PROJECT_ROOT / "results" / "parameter_sensitivity_latest.json"
DISCLAIMER = "仅用于本地数据健康诊断和学习研究，不构成投资建议。"


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run QuantDesk local data health dashboard.")
    parser.add_argument("--symbols", default="", help="Comma-separated symbols. Empty means low-price included=true sample only.")
    parser.add_argument("--scope", default="", help="Optional qd.py scope label. current_analysis_pool keeps the existing safe scoped pool.")
    parser.add_argument("--target-date", default="", help="Target date in YYYY-MM-DD.")
    parser.add_argument("--network", default="false", help="Defaults to false. This command does not fetch data.")
    parser.add_argument("--max-symbols", type=int, default=80)
    parser.add_argument("--include-freshness", default="true")
    parser.add_argument("--include-intraday", default="true")
    parser.add_argument("--include-provider-stats", default="true")
    parser.add_argument("--include-grade-trend", default="true")
    parser.add_argument("--json", default="false", help="Print full result JSON to stdout.")
    parser.add_argument("--summary", nargs="?", const="true", default="false", help="Print compact operator summary while still writing full latest JSON.")
    parser.add_argument("--verbose", default="false", help="Print full legacy details to stdout.")
    parser.add_argument("--output-dir", default="results/data_health")
    return parser.parse_args(argv)


def _project_path(path: str | Path) -> Path:
    target = Path(path)
    if not target.is_absolute():
        target = PROJECT_ROOT / target
    return target


def _rel(path: str | Path) -> str:
    target = _project_path(path)
    try:
        return str(target.relative_to(PROJECT_ROOT)).replace("\\", "/")
    except Exception:
        return str(target)


def _status_from_parts(parts: list[dict[str, Any]]) -> str:
    statuses = {str(part.get("status") or "missing").lower() for part in parts if isinstance(part, dict)}
    if "failed" in statuses:
        return "failed"
    if "warning" in statuses or "missing" in statuses or "partial" in statuses:
        return "warning"
    return "passed"


def _operation_log_status(status: Any) -> str:
    """Map dashboard status to the operation-log contract without hiding failures."""

    normalized = str(status or "failed").lower()
    if normalized in {"failed", "warning", "passed"}:
        return normalized
    if normalized in {"missing", "partial", "skipped"}:
        return "warning"
    return "failed"


def _merge_evidence_reason_codes(
    existing: list[str] | None,
    evidence_reasons: list[str] | None,
    evidence_status: str,
) -> list[str]:
    """Always retain evidence reason codes, regardless of the base verdict."""

    merged = [str(item) for item in (existing or []) if str(item)]
    merged.extend(str(item) for item in (evidence_reasons or []) if str(item))
    return list(dict.fromkeys(merged))


#: Latest-chain statuses that must degrade the persisted snapshot verdict.
_CHAIN_FAILED_STATUSES = frozenset({"abandoned", "blocked", "failed", "superseded_contract"})
_CHAIN_UNFINISHED_STATUSES = frozenset({"running", "partial", "not_run"})

#: Scheduler probe statuses that mean natural runs cannot be trusted.
_SCHEDULER_BROKEN_STATUSES = frozenset(
    {"missing", "unmanaged", "disabled", "misconfigured"}
)
_SCHEDULER_DEGRADED_STATUSES = frozenset(
    {"unavailable", "last_run_failed", "last_run_unknown", "not_supported"}
)


def _reconcile_status_with_chain(
    status: str,
    *,
    daily_chain: dict[str, Any],
    scheduler: dict[str, Any],
) -> tuple[str, list[str]]:
    """Degrade the persisted snapshot verdict to match chain/scheduler reality.

    The component `parts` can all be green while the latest chain attempt is
    blocked or the scheduler is misconfigured. Persisting `passed` in that case
    made the cached dashboard contradict the live state, so the snapshot itself
    must carry the degrade instead of relying on the serve-time overlay.
    """

    chain_status = str((daily_chain or {}).get("status") or "").lower()
    scheduler_status = str((scheduler or {}).get("status") or "").lower()
    reasons: list[str] = []

    # The verdict itself comes from the single canonical rule in
    # quantdesk.data_health_dashboard, so the CLI snapshot and the served
    # overlay cannot drift into two different status vocabularies. This
    # wrapper only adds the human-readable reason codes.
    resolved = _snapshot_status_with_chain(
        dict(daily_chain or {}), {"status": status}, dict(scheduler or {})
    )
    if chain_status in _CHAIN_FAILED_STATUSES or chain_status in _CHAIN_UNFINISHED_STATUSES:
        reasons.append(f"latest_daily_chain_{chain_status}")
    if (
        scheduler_status in _SCHEDULER_BROKEN_STATUSES
        or scheduler_status in _SCHEDULER_DEGRADED_STATUSES
    ):
        reasons.append(f"daily_data_scheduler_{scheduler_status}")
    return resolved or "passed", reasons


def _provider_failure_summary(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "status": payload.get("status") or "missing",
        "failed_count": int(payload.get("failed_count") or 0),
        "skipped_count": int(payload.get("skipped_count") or 0),
        "offline_skipped_count": int(payload.get("offline_skipped_count") or 0),
        "failures_by_provider": payload.get("failures_by_provider") if isinstance(payload.get("failures_by_provider"), dict) else {},
        "failures_by_type": payload.get("failures_by_type") if isinstance(payload.get("failures_by_type"), dict) else {},
        "skipped_by_type": payload.get("skipped_by_type") if isinstance(payload.get("skipped_by_type"), dict) else {},
    }


def build_daily_chain_snapshot_overlay(
    daily_chain: dict[str, Any],
    scheduler: dict[str, Any],
) -> dict[str, Any]:
    """Persist the same chain/scheduler contract consumed by the port-8000 UI."""

    gate = evaluate_data_chain_consumer_gate(
        daily_chain,
        project_root=PROJECT_ROOT,
    )
    return {
        "daily_data_scheduler": scheduler,
        "formal_daily_chain_status": build_formal_daily_chain_status(
            daily_chain,
            scheduler=scheduler,
        ),
        "downstream_consumer_gate": {
            key: gate.get(key)
            for key in (
                "status",
                "allowed",
                "authoritative",
                "run_id",
                "target_date",
                "reasons",
                "chain_status",
            )
        },
    }


def _load_parameter_sensitivity_summary() -> dict[str, Any]:
    payload = read_json(PARAMETER_SENSITIVITY_LATEST_JSON, {})
    if isinstance(payload, dict) and payload:
        summary = summarize_parameter_sensitivity(payload)
        return {
            "status": summary.get("status") or "unknown",
            "robustness_score": summary.get("robustness_score"),
            "combination_count": summary.get("combination_count"),
            "warning_count": summary.get("warning_count"),
            "top_overfit_warnings": summary.get("top_overfit_warnings") or [],
            "report_path": "reports/parameter_sensitivity_report_latest.md",
            "disclaimer": summary.get("disclaimer"),
        }
    return {
        "status": "missing",
        "robustness_score": None,
        "combination_count": 0,
        "warning_count": 0,
        "top_overfit_warnings": [],
        "suggested_next_action": "运行 python scripts/qd.py param-sensitivity --symbol 600744 --network false",
    }


def _choose_suggested_next_action(
    *,
    freshness: dict[str, Any],
    intraday: dict[str, Any],
    provider_stats: dict[str, Any],
    extended_data_readiness: dict[str, Any],
    tushare_retry_plan: dict[str, Any],
    extended_import_verification: dict[str, Any],
    extended_data_gates: dict[str, Any] | None = None,
) -> str:
    """Choose the operator next action, prioritizing active data gates."""

    gates = extended_data_gates if isinstance(extended_data_gates, dict) else {}
    gates_current = str(gates.get("current_gate") or "").strip()
    gates_wait_reason = str(gates.get("operator_wait_reason") or "").strip()
    gates_action = str(gates.get("suggested_next_action") or "").strip()
    if gates_current == "download_queue" or gates_wait_reason == "download_queue_active":
        return gates_action or "continue_monitoring"

    import_gate = str(extended_import_verification.get("post_import_gate") or "").strip()
    import_action = str(extended_import_verification.get("suggested_next_action") or "").strip()
    if import_gate == "wait_for_download_completion" and import_action:
        return import_action
    if import_gate in {"no_canonical_import_yet", "partial_canonical_import"} and import_action:
        return import_action

    retry_gate = str(tushare_retry_plan.get("retry_gate") or "").strip()
    if retry_gate == "wait_for_queue_completion":
        return "wait_for_queue_then_audit_and_import"
    if retry_gate in {"review_failures_before_retry", "inspect_traceback_before_retry"}:
        retry_action = str(tushare_retry_plan.get("suggested_next_action") or "").strip()
        return retry_action or "review_retry_plan_then_restart_with_user_approval"

    readiness_action = str(extended_data_readiness.get("suggested_next_action") or "").strip()
    if readiness_action in {"wait_for_queue_then_rerun_audit", FULL_AUDIT_COMMAND}:
        return readiness_action

    if freshness.get("status") == "warning":
        return freshness.get("suggested_next_action") or "python scripts/qd.py data-health --network false --summary true"
    if intraday.get("status") == "warning":
        return intraday.get("suggested_next_action") or "python scripts/qd.py data-health --network false --summary true"
    if provider_stats.get("status") == "warning":
        return provider_stats.get("suggested_next_action") or "python scripts/qd.py providers --network false"
    return "python scripts/qd.py status"


def build_dashboard_report(payload: dict[str, Any]) -> str:
    freshness = payload.get("freshness") if isinstance(payload.get("freshness"), dict) else {}
    intraday = payload.get("intraday") if isinstance(payload.get("intraday"), dict) else {}
    provider = payload.get("provider_failure_stats") if isinstance(payload.get("provider_failure_stats"), dict) else {}
    grade = payload.get("data_grade_trend") if isinstance(payload.get("data_grade_trend"), dict) else {}
    parameter = payload.get("parameter_sensitivity_summary") if isinstance(payload.get("parameter_sensitivity_summary"), dict) else {}
    readiness = payload.get("extended_data_readiness") if isinstance(payload.get("extended_data_readiness"), dict) else {}
    readiness_capabilities = readiness.get("capabilities") if isinstance(readiness.get("capabilities"), dict) else {}
    queue_status = payload.get("tushare_queue_status") if isinstance(payload.get("tushare_queue_status"), dict) else {}
    queue = queue_status.get("queue") if isinstance(queue_status.get("queue"), dict) else {}
    log_summary = queue_status.get("log_summary") if isinstance(queue_status.get("log_summary"), dict) else {}
    minute_status = payload.get("tushare_minute_status") if isinstance(payload.get("tushare_minute_status"), dict) else {}
    minute_log = minute_status.get("log_summary") if isinstance(minute_status.get("log_summary"), dict) else {}
    minute_last = minute_log.get("last_symbol_done") if isinstance(minute_log.get("last_symbol_done"), dict) else {}
    minute_coverage = payload.get("tushare_minute_coverage") if isinstance(payload.get("tushare_minute_coverage"), dict) else {}
    minute_target = minute_coverage.get("target_pool") if isinstance(minute_coverage.get("target_pool"), dict) else {}
    completed_minute = minute_coverage.get("latest_completed_job") if isinstance(minute_coverage.get("latest_completed_job"), dict) else {}
    minute_quality = payload.get("tushare_minute_quality") if isinstance(payload.get("tushare_minute_quality"), dict) else {}
    completed_quality = payload.get("tushare_minute_completed_quality") if isinstance(payload.get("tushare_minute_completed_quality"), dict) else {}
    retry_plan = payload.get("tushare_retry_plan") if isinstance(payload.get("tushare_retry_plan"), dict) else {}
    retry_summary = retry_plan.get("summary") if isinstance(retry_plan.get("summary"), dict) else {}
    data_gates = payload.get("extended_data_gates") if isinstance(payload.get("extended_data_gates"), dict) else {}
    import_audit = payload.get("extended_import_audit") if isinstance(payload.get("extended_import_audit"), dict) else {}
    import_verification = payload.get("extended_import_verification") if isinstance(payload.get("extended_import_verification"), dict) else {}
    health_summary = payload.get("data_health_summary") if isinstance(payload.get("data_health_summary"), dict) else {}
    safe_next_commands = (
        health_summary.get("extended_data_safe_next_commands")
        if isinstance(health_summary.get("extended_data_safe_next_commands"), list)
        else []
    )
    blocked_without_confirmation = (
        health_summary.get("extended_data_blocked_without_confirmation")
        if isinstance(health_summary.get("extended_data_blocked_without_confirmation"), list)
        else []
    )
    warnings = payload.get("warnings") if isinstance(payload.get("warnings"), list) else []
    return "\n".join(
        [
            "# QuantDesk Data Health Dashboard",
            "",
            DISCLAIMER,
            "",
            f"- status: `{payload.get('status') or '-'}`",
            f"- target_date: `{payload.get('target_date') or '-'}`",
            f"- network: `{payload.get('network')}`",
            f"- freshness: `{freshness.get('status') or 'missing'}` stale=`{freshness.get('stale_count', 0)}` missing=`{freshness.get('missing_count', 0)}`",
            f"- intraday coverage: `{intraday.get('coverage_rate', 0)}` covered=`{intraday.get('covered_count', 0)}` / `{intraday.get('symbol_count', 0)}`",
            f"- provider failures: `{provider.get('failed_count', 0)}` skipped=`{provider.get('skipped_count', 0)}`",
            f"- data_grade_counts: `{grade.get('grade_counts') or {}}`",
            f"- parameter_sensitivity: `{parameter.get('status') or 'missing'}` robustness_score=`{parameter.get('robustness_score')}` combinations=`{parameter.get('combination_count', 0)}`",
            f"- extended_data_readiness: `{readiness.get('status') or 'missing'}` mainline=`{readiness_capabilities.get('mainline_analysis') or 'degraded'}` moneyflow=`{readiness_capabilities.get('moneyflow_context') or 'degraded'}`",
            f"- tushare_queue: `{queue_status.get('status') or 'missing'}` stage=`{queue.get('current_stage') or '-'}` ok=`{log_summary.get('ok_count', 0)}` fail=`{log_summary.get('fail_count', 0)}`",
            f"- tushare_minute_status: `{minute_status.get('status') or 'missing'}` done=`{minute_log.get('symbol_done_count', 0)}`/`{minute_log.get('symbol_count', 0)}` last=`{minute_last.get('symbol') or '-'}` traceback=`{minute_log.get('traceback_count', 0)}` action=`{minute_status.get('suggested_next_action') or '-'}`",
            (
                "- tushare_minute_warning_policy: `running_with_warnings means continue monitoring; "
                "no automatic restart without user confirmation`"
                if str(minute_status.get("status") or "").strip() == "running_with_warnings"
                else "- tushare_minute_warning_policy: `-`"
            ),
            f"- tushare_minute_coverage: `{minute_coverage.get('status') or 'missing'}` available=`{minute_coverage.get('current_available_count', 0)}` target=`{minute_target.get('symbol_count', 0)}` canonical=`{minute_coverage.get('canonical_current_available_count', 0)}` staged_pending_import=`{minute_coverage.get('staged_current_not_imported_count', 0)}` missing_or_stale=`{minute_coverage.get('missing_or_stale_count', 0)}`",
            f"- tushare_minute_latest_completed: `{completed_minute.get('path') or '-'}` canonical_current=`{completed_minute.get('canonical_current_count', 0)}`/`{completed_minute.get('symbol_count', 0)}`",
            f"- tushare_minute_latest_completed_quality: `{completed_quality.get('status') or minute_quality.get('status') or 'missing'}` usable=`{completed_quality.get('usable_count', 0)}`/`{completed_quality.get('total_symbols', 0)}` invalid=`{completed_quality.get('invalid_count', 0)}` missing=`{completed_quality.get('missing_count', 0)}` target=`{completed_quality.get('target_date') or '-'}` action=`{completed_quality.get('suggested_next_action') or '-'}`",
            f"- tushare_retry_plan: `{retry_plan.get('status') or 'missing'}` gate=`{retry_plan.get('retry_gate') or '-'}` fail_events=`{retry_summary.get('fail_event_count', 0)}` candidates=`{len(retry_plan.get('retry_candidates') or [])}`",
            f"- extended_data_gates: `{data_gates.get('status') or 'missing'}` current_gate=`{data_gates.get('current_gate') or '-'}` suggested=`{data_gates.get('suggested_next_action') or '-'}`",
            f"- extended_import_audit: `{import_audit.get('status') or 'missing'}` preapply_gate=`{import_audit.get('preapply_gate') or '-'}` planned=`{import_audit.get('planned_file_count', 0)}`",
            f"- extended_import_full_audit_command: `{import_audit.get('full_audit_command') or '-'}`",
            f"- extended_import_source_scan_full_audit_command: `{import_audit.get('source_scan_full_audit_command') or '-'}`",
            f"- extended_import_verification: `{import_verification.get('status') or 'missing'}` gate=`{import_verification.get('post_import_gate') or '-'}` canonical_ratio=`{import_verification.get('canonical_presence_ratio')}`",
            f"- extended_data_safe_next: `{safe_next_commands[0] if safe_next_commands else '-'}`",
            f"- extended_data_blocked_without_confirmation: `{blocked_without_confirmation[0] if blocked_without_confirmation else '-'}`",
            "",
            "## Suggested Next Action",
            "",
            str(payload.get("suggested_next_action") or "-"),
            "",
            "## Warnings",
            *(f"- {item}" for item in warnings[:80]),
        ]
    )


def write_dashboard_report(payload: dict[str, Any], report_path: str | Path = REPORT_PATH) -> Path:
    target = _project_path(report_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(build_dashboard_report(payload), encoding="utf-8")
    return target


def run(args: argparse.Namespace) -> dict[str, Any]:
    output_dir = _project_path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    target_info = resolve_target_complete_trade_date(args.target_date or None)
    target_date = str(target_info.get("target_complete_trade_date") or target_info.get("target_date") or now_text()[:10])
    network = truthy(args.network)
    scope_summary = resolve_scope(symbols=args.symbols, scope=args.scope or "", max_symbols=args.max_symbols, project_root=PROJECT_ROOT)
    selected_symbols_list = list(scope_summary.get("symbols") or [])
    selected_symbols = ",".join(selected_symbols_list)
    parts: list[dict[str, Any]] = []

    freshness: dict[str, Any] = {"status": "skipped", "warnings": []}
    if truthy(args.include_freshness):
        freshness = build_data_freshness_summary(
            selected_symbols,
            target_date=target_date,
            network=network,
            max_symbols=args.max_symbols,
            write=True,
            output_path=output_dir / "data_freshness_summary_latest.json",
        )
        freshness.update(scope_fields(scope_summary))
        freshness["selected_symbols"] = selected_symbols_list
        freshness["symbols_checked"] = selected_symbols_list
        parts.append(freshness)

    intraday: dict[str, Any] = {"status": "skipped", "warnings": []}
    if truthy(args.include_intraday):
        intraday = build_intraday_coverage_summary(
            selected_symbols,
            target_date=target_date,
            network=network,
            max_symbols=args.max_symbols,
            write=True,
            output_path=output_dir / "intraday_coverage_summary_latest.json",
        )
        intraday.update(scope_fields(scope_summary))
        intraday["selected_symbols"] = selected_symbols_list
        intraday["symbols_checked"] = selected_symbols_list
        parts.append(intraday)

    provider_stats: dict[str, Any] = {"status": "skipped", "warnings": []}
    if truthy(args.include_provider_stats):
        provider_stats = collect_provider_failure_stats(
            network=network,
            write=True,
            output_path=output_dir / "provider_failure_stats_latest.json",
        )
        parts.append(provider_stats)

    grade_trend: dict[str, Any] = {"status": "skipped", "warnings": [], "grade_counts": {}}
    if truthy(args.include_grade_trend):
        symbols = freshness.get("selected_symbols") or selected_symbols
        grade_trend = build_data_grade_trend_summary(
            target_date=target_date,
            symbols=symbols,
            freshness_summary=freshness,
            intraday_summary=intraday,
            write=True,
            output_path=output_dir / "data_grade_trend_latest.json",
        )
        parts.append(grade_trend)

    parameter_sensitivity_summary = _load_parameter_sensitivity_summary()
    extended_data_readiness = write_extended_data_readiness_outputs(
        output_path=output_dir / "extended_data_readiness_latest.json",
        report_path=PROJECT_ROOT / "reports" / "extended_data_readiness_latest.md",
    )
    parts.append(extended_data_readiness)
    tushare_queue_status = write_tushare_queue_status_outputs(
        output_path=output_dir / "tushare_queue_status_latest.json",
        report_path=PROJECT_ROOT / "reports" / "tushare_queue_status_latest.md",
        include_processes=False,
    )
    parts.append(tushare_queue_status)
    tushare_minute_status = write_tushare_minute_status_outputs(
        output_path=output_dir / "tushare_minute_status_latest.json",
        report_path=PROJECT_ROOT / "reports" / "tushare_minute_status_latest.md",
        include_processes=False,
    )
    parts.append(tushare_minute_status)
    tushare_minute_coverage = write_tushare_minute_coverage_outputs(
        output_path=output_dir / "tushare_minute_coverage_latest.json",
        report_path=PROJECT_ROOT / "reports" / "tushare_minute_coverage_latest.md",
    )
    parts.append(tushare_minute_coverage)
    tushare_minute_quality = load_latest_tushare_minute_quality_snapshot(project_root=PROJECT_ROOT)
    completed_minute = (
        tushare_minute_coverage.get("latest_completed_job")
        if isinstance(tushare_minute_coverage.get("latest_completed_job"), dict)
        else {}
    )
    tushare_minute_completed_quality = summarize_completed_minute_quality_snapshot(
        tushare_minute_quality,
        expected_job_path=str(completed_minute.get("path") or ""),
    )
    tushare_retry_plan = write_tushare_retry_plan_outputs(
        output_path=output_dir / "tushare_retry_plan_latest.json",
        report_path=PROJECT_ROOT / "reports" / "tushare_retry_plan_latest.md",
    )
    parts.append(tushare_retry_plan)
    extended_import_audit = build_extended_staged_import_audit(
        output_path=output_dir / "extended_staged_import_audit_latest.json",
        report_path=PROJECT_ROOT / "reports" / "extended_staged_import_audit_latest.md",
        max_files_per_rule=20,
    )
    parts.append(extended_import_audit)
    extended_import_verification = write_extended_import_verification_outputs(
        output_path=output_dir / "extended_import_verification_latest.json",
        report_path=PROJECT_ROOT / "reports" / "extended_import_verification_latest.md",
    )
    parts.append(extended_import_verification)
    previous_data_gates = read_json(output_dir / "extended_data_gates_latest.json", {})
    extended_data_gates = build_extended_data_gates_from_payloads(
        queue_status=tushare_queue_status,
        minute_status=tushare_minute_status,
        minute_coverage=tushare_minute_coverage,
        minute_quality=tushare_minute_quality,
        retry_plan=tushare_retry_plan,
        import_audit=extended_import_audit,
        import_verification=extended_import_verification,
        previous_payload=previous_data_gates if isinstance(previous_data_gates, dict) else {},
    )
    extended_data_gates["output_path"] = _rel(output_dir / "extended_data_gates_latest.json")
    extended_data_gates["report_path"] = _rel(PROJECT_ROOT / "reports" / "extended_data_gates_latest.md")
    write_json(output_dir / "extended_data_gates_latest.json", extended_data_gates)
    write_text(PROJECT_ROOT / "reports" / "extended_data_gates_latest.md", render_extended_data_gates_report(extended_data_gates))
    parts.append(extended_data_gates)
    extended_gate_chain = extended_data_gates.get("gate_chain") if isinstance(extended_data_gates.get("gate_chain"), dict) else {}
    extended_gate_queue = extended_gate_chain.get("queue") if isinstance(extended_gate_chain.get("queue"), dict) else {}
    extended_gate_progress = (
        extended_data_gates.get("queue_progress_since_previous")
        if isinstance(extended_data_gates.get("queue_progress_since_previous"), dict)
        else {}
    )
    extended_gate_policy = (
        extended_data_gates.get("operator_action_policy")
        if isinstance(extended_data_gates.get("operator_action_policy"), dict)
        else {}
    )

    daily_chain = latest_daily_data_chain(PROJECT_ROOT)
    minute_universe_coverage = build_minute_universe_coverage(daily_chain)
    scheduler_probe = daily_data_scheduler_status(project_root=PROJECT_ROOT)
    evidence_freshness = build_evidence_freshness(
        PROJECT_ROOT,
        as_of=target_date,
    )
    daily_chain_overlay = build_daily_chain_snapshot_overlay(
        daily_chain,
        scheduler_probe,
    )
    warnings: list[str] = [str(item) for item in (target_info.get("target_date_warnings") or []) if item]
    if daily_chain.get("exists") and daily_chain.get("status") != "completed":
        warnings.append(
            f"每日数据链状态为 {daily_chain.get('status') or 'unknown'}；下游不得标记为完整 fresh。"
        )
    for part in parts:
        warnings.extend(short_error_text(item) for item in (part.get("warnings") or []) if item)
    if not evidence_freshness.get("metrics_trustworthy", False):
        warnings.append(
            "审计账本/候选绩效证据过期、缺失或纸面账户不一致；当前绩效指标不可信。"
        )
    suggested = _choose_suggested_next_action(
        freshness=freshness,
        intraday=intraday,
        provider_stats=provider_stats,
        extended_data_readiness=extended_data_readiness,
        tushare_retry_plan=tushare_retry_plan,
        extended_import_verification=extended_import_verification,
        extended_data_gates=extended_data_gates,
    )
    status, chain_degraded_reasons = _reconcile_status_with_chain(
        _status_from_parts(parts),
        daily_chain=daily_chain,
        scheduler=scheduler_probe,
    )
    evidence_status = str(evidence_freshness.get("status") or "warning").lower()
    evidence_reasons = [
        str(item)
        for item in evidence_freshness.get("reason_codes") or []
        if str(item)
    ]
    evidence_metrics_trustworthy = bool(evidence_freshness.get("metrics_trustworthy"))
    if evidence_status == "failed":
        status = "failed"
    elif (
        evidence_status in {"warning", "stale", "missing"}
        or not evidence_metrics_trustworthy
    ) and status == "passed":
        status = "warning"
    evidence_overlay_reasons = list(evidence_reasons)
    if not evidence_overlay_reasons and evidence_status in {"failed", "warning", "stale", "missing"}:
        evidence_overlay_reasons.append(f"audit_evidence_{evidence_status}")
    if not evidence_metrics_trustworthy:
        evidence_overlay_reasons.append("audit_metrics_untrustworthy")
    chain_degraded_reasons = _merge_evidence_reason_codes(
        chain_degraded_reasons,
        evidence_overlay_reasons,
        evidence_status,
    )
    dashboard = {
        "ok": status != "failed",
        "schema_version": API_SCHEMA_VERSION,
        "app_version": APP_VERSION,
        "package_version": PACKAGE_VERSION,
        "api_schema_version": API_SCHEMA_VERSION,
        "task_type": "data_health_dashboard",
        "status": status,
        # Non-empty when the component parts were healthier than the live
        # chain/scheduler state, so a cached snapshot cannot read green while the
        # latest attempt is blocked, running, or the scheduler is misconfigured.
        "health_degraded_reason_codes": chain_degraded_reasons,
        "target_date": target_date,
        "network": network,
        "full_scan": False,
        "symbols": freshness.get("selected_symbols") or intraday.get("selected_symbols") or [],
        "data_scope": scope_summary.get("data_scope") or freshness.get("data_scope") or "unknown",
        **scope_fields(scope_summary),
        "symbols_requested": freshness.get("symbols_requested") or [],
        "symbols_checked": freshness.get("symbols_checked") or freshness.get("selected_symbols") or [],
        "freshness": freshness,
        "evidence_freshness": evidence_freshness,
        "intraday": intraday,
        "provider_failure_stats": provider_stats,
        "data_grade_trend": grade_trend,
        "parameter_sensitivity_summary": parameter_sensitivity_summary,
        "extended_data_readiness": extended_data_readiness,
        "tushare_queue_status": tushare_queue_status,
        "tushare_minute_status": tushare_minute_status,
        "tushare_minute_coverage": tushare_minute_coverage,
        "tushare_minute_quality": tushare_minute_quality,
        "tushare_minute_completed_quality": tushare_minute_completed_quality,
        "tushare_retry_plan": tushare_retry_plan,
        "extended_import_audit": extended_import_audit,
        "extended_import_verification": extended_import_verification,
        "extended_data_gates": extended_data_gates,
        "daily_data_chain": daily_chain,
        "minute_universe_coverage": minute_universe_coverage,
        **daily_chain_overlay,
        "data_health_summary": {
            "status": status,
            "daily_data_chain_status": daily_chain.get("status") or "not_run",
            "daily_data_chain_run_id": daily_chain.get("run_id") or "",
            "minute_target_date": minute_universe_coverage.get("target_trade_date") or "",
            "minute_coverage_status": minute_universe_coverage.get("status") or "not_run",
            "minute_critical_coverage_ratio": (
                (minute_universe_coverage.get("tiers") or {}).get("critical", {}).get("coverage_ratio")
            ),
            "minute_analysis_pool_coverage_ratio": (
                (minute_universe_coverage.get("tiers") or {}).get("current_analysis_pool", {}).get("coverage_ratio")
            ),
            "minute_extended_pool_coverage_ratio": None,
            "minute_failed_count": int(
                ((minute_universe_coverage.get("tiers") or {}).get("critical", {}).get("failed_count") or 0)
                + ((minute_universe_coverage.get("tiers") or {}).get("current_analysis_pool", {}).get("failed_count") or 0)
            ),
            "freshness_status": freshness.get("status") or "missing",
            "audit_evidence_status": evidence_freshness.get("status") or "missing",
            "audit_metrics_trustworthy": bool(evidence_freshness.get("metrics_trustworthy")),
            "audit_evidence_reason_codes": evidence_reasons,
            "data_scope": freshness.get("data_scope") or "unknown",
            "coverage_date": freshness.get("coverage_date") or "",
            "latest_date_max": freshness.get("latest_date_max") or "",
            "stale_count": int(freshness.get("stale_count") or 0),
            "missing_daily_count": int(freshness.get("missing_count") or 0),
            "intraday_coverage_rate": float(intraday.get("coverage_rate") or 0.0),
            "provider_failure_summary": _provider_failure_summary(provider_stats),
            "data_grade_counts": grade_trend.get("grade_counts") if isinstance(grade_trend.get("grade_counts"), dict) else {},
            "parameter_sensitivity_status": parameter_sensitivity_summary.get("status"),
            "parameter_robustness_score": parameter_sensitivity_summary.get("robustness_score"),
            "extended_data_status": extended_data_readiness.get("status"),
            "extended_data_capabilities": extended_data_readiness.get("capabilities") or {},
            "tushare_queue_status": tushare_queue_status.get("status"),
            "tushare_queue_stage": (
                tushare_queue_status.get("queue", {}).get("current_stage")
                if isinstance(tushare_queue_status.get("queue"), dict)
                else ""
            ),
            "tushare_minute_status": tushare_minute_status.get("status"),
            "tushare_minute_symbol_done_count": int((tushare_minute_status.get("log_summary") or {}).get("symbol_done_count") or 0) if isinstance(tushare_minute_status.get("log_summary"), dict) else 0,
            "tushare_minute_symbol_count": int((tushare_minute_status.get("log_summary") or {}).get("symbol_count") or 0) if isinstance(tushare_minute_status.get("log_summary"), dict) else 0,
            "tushare_minute_last_symbol": (
                (tushare_minute_status.get("log_summary") or {}).get("last_symbol_done", {}).get("symbol")
                if isinstance((tushare_minute_status.get("log_summary") or {}).get("last_symbol_done"), dict)
                else ""
            ),
            "tushare_minute_traceback_count": int((tushare_minute_status.get("log_summary") or {}).get("traceback_count") or 0) if isinstance(tushare_minute_status.get("log_summary"), dict) else 0,
            "tushare_minute_suggested_next_action": tushare_minute_status.get("suggested_next_action") or "",
            "tushare_minute_coverage_status": tushare_minute_coverage.get("status"),
            "tushare_minute_target_symbol_count": int((tushare_minute_coverage.get("target_pool") or {}).get("symbol_count") or 0) if isinstance(tushare_minute_coverage.get("target_pool"), dict) else 0,
            "tushare_minute_current_available_count": int(tushare_minute_coverage.get("current_available_count") or 0),
            "tushare_minute_current_available_count_source": tushare_minute_coverage.get("current_available_count_source") or "",
            "tushare_minute_canonical_current_available_count": int(tushare_minute_coverage.get("canonical_current_available_count") or 0),
            "tushare_minute_staged_current_not_imported_count": int(tushare_minute_coverage.get("staged_current_not_imported_count") or 0),
            "tushare_minute_canonical_current_available_ratio": tushare_minute_coverage.get("canonical_current_available_ratio"),
            "tushare_minute_staged_current_not_imported_ratio": tushare_minute_coverage.get("staged_current_not_imported_ratio"),
            "tushare_minute_missing_or_stale_count": int(tushare_minute_coverage.get("missing_or_stale_count") or 0),
            "tushare_minute_current_available_ratio": tushare_minute_coverage.get("current_available_ratio"),
            "tushare_minute_canonical_file_count": int(tushare_minute_coverage.get("canonical_file_count") or 0),
            "tushare_minute_latest_completed_job_path": (
                (tushare_minute_coverage.get("latest_completed_job") or {}).get("path")
                if isinstance(tushare_minute_coverage.get("latest_completed_job"), dict)
                else ""
            ),
            "tushare_minute_latest_completed_job_symbol_count": int(
                ((tushare_minute_coverage.get("latest_completed_job") or {}).get("symbol_count") or 0)
                if isinstance(tushare_minute_coverage.get("latest_completed_job"), dict)
                else 0
            ),
            "tushare_minute_latest_completed_job_canonical_current_count": int(
                ((tushare_minute_coverage.get("latest_completed_job") or {}).get("canonical_current_count") or 0)
                if isinstance(tushare_minute_coverage.get("latest_completed_job"), dict)
                else 0
            ),
            "tushare_minute_latest_completed_job_missing_or_stale_count": int(
                ((tushare_minute_coverage.get("latest_completed_job") or {}).get("missing_or_stale_count") or 0)
                if isinstance(tushare_minute_coverage.get("latest_completed_job"), dict)
                else 0
            ),
            "tushare_minute_latest_completed_quality_status": tushare_minute_completed_quality.get("status"),
            "tushare_minute_latest_completed_quality_snapshot_path": tushare_minute_completed_quality.get("snapshot_path") or "",
            "tushare_minute_latest_completed_quality_job_matches": bool(
                tushare_minute_completed_quality.get("job_matches_latest_completed")
            ),
            "tushare_minute_latest_completed_quality_target_date": tushare_minute_completed_quality.get("target_date") or "",
            "tushare_minute_latest_completed_quality_total_symbols": int(
                tushare_minute_completed_quality.get("total_symbols") or 0
            ),
            "tushare_minute_latest_completed_quality_usable_count": int(
                tushare_minute_completed_quality.get("usable_count") or 0
            ),
            "tushare_minute_latest_completed_quality_partial_count": int(
                tushare_minute_completed_quality.get("partial_count") or 0
            ),
            "tushare_minute_latest_completed_quality_insufficient_count": int(
                tushare_minute_completed_quality.get("insufficient_count") or 0
            ),
            "tushare_minute_latest_completed_quality_invalid_count": int(
                tushare_minute_completed_quality.get("invalid_count") or 0
            ),
            "tushare_minute_latest_completed_quality_missing_count": int(
                tushare_minute_completed_quality.get("missing_count") or 0
            ),
            "tushare_minute_latest_completed_quality_usable_ratio": tushare_minute_completed_quality.get("usable_ratio"),
            "tushare_minute_latest_completed_quality_suggested_next_action": (
                tushare_minute_completed_quality.get("suggested_next_action") or ""
            ),
            "tushare_minute_coverage_suggested_next_action": tushare_minute_coverage.get("suggested_next_action"),
            "tushare_retry_gate": tushare_retry_plan.get("retry_gate"),
            "extended_data_current_gate": extended_data_gates.get("current_gate"),
            "extended_data_gate_suggested_next_action": extended_data_gates.get("suggested_next_action"),
            "extended_data_operator_wait_reason": extended_data_gates.get("operator_wait_reason"),
            "extended_data_queue_stage_progress": extended_gate_queue.get("stage_position_label") or "",
            "extended_data_queue_stage_requested_count": int(extended_gate_queue.get("stage_requested_count") or 0),
            "extended_data_queue_stage_completed_count": int(extended_gate_queue.get("stage_completed_count") or 0),
            "extended_data_queue_stage_remaining_count": int(extended_gate_queue.get("stage_remaining_count") or 0),
            "extended_data_queue_stage_remaining_keys": extended_gate_queue.get("stage_remaining_keys") or [],
            "extended_data_queue_stage_current_elapsed_seconds": extended_gate_queue.get("stage_current_elapsed_seconds"),
            "extended_data_queue_stage_last_event_age_seconds": extended_gate_queue.get("stage_last_event_age_seconds"),
            "extended_data_queue_stage_last_event_status": extended_gate_queue.get("stage_last_event_status") or "",
            "extended_data_manual_review_required": bool(extended_gate_policy.get("manual_review_required")),
            "extended_data_manual_review_reason": extended_gate_policy.get("manual_review_reason") or "",
            "extended_data_requires_user_confirmation": bool(extended_gate_policy.get("requires_user_confirmation")),
            "extended_data_confirmation_required_command": extended_gate_policy.get("confirmation_required_command") or "",
            "extended_data_safe_next_commands": extended_gate_policy.get("safe_next_commands") or [],
            "extended_data_blocked_without_confirmation": extended_gate_policy.get("blocked_without_confirmation") or [],
            "extended_data_queue_ok_count": int(extended_gate_queue.get("ok_count") or 0),
            "extended_data_queue_fail_count": int(extended_gate_queue.get("fail_count") or 0),
            "extended_data_queue_ssl_eof_count": int(extended_gate_queue.get("ssl_eof_count") or 0),
            "extended_data_queue_timeout_count": int(extended_gate_queue.get("timeout_count") or 0),
            "extended_data_queue_traceback_count": int(extended_gate_queue.get("traceback_count") or 0),
            "extended_data_queue_stdout_age_seconds": extended_gate_queue.get("stdout_age_seconds"),
            "extended_data_queue_stderr_age_seconds": extended_gate_queue.get("stderr_age_seconds"),
            "extended_data_queue_liveness": extended_gate_queue.get("queue_liveness") or "unknown",
            "extended_data_queue_progress_state": extended_gate_progress.get("progress_state") or "unknown",
            "extended_data_queue_ok_delta": extended_gate_progress.get("ok_delta"),
            "extended_data_queue_fail_delta": extended_gate_progress.get("fail_delta"),
            "extended_data_queue_traceback_delta": extended_gate_progress.get("traceback_delta"),
            "tushare_retry_fail_events": (
                tushare_retry_plan.get("summary", {}).get("fail_event_count", 0)
                if isinstance(tushare_retry_plan.get("summary"), dict)
                else 0
            ),
            "tushare_retry_candidate_groups": len(tushare_retry_plan.get("retry_candidates") or []),
            "extended_import_preapply_gate": extended_import_audit.get("preapply_gate"),
            "extended_import_planned_file_count": int(extended_import_audit.get("planned_file_count") or 0),
            "extended_import_audit_status": extended_import_audit.get("status"),
            "extended_import_domain_counts": extended_import_audit.get("domain_counts") if isinstance(extended_import_audit.get("domain_counts"), dict) else {},
            "extended_import_missing_planned_domains": extended_import_audit.get("missing_planned_domains") or [],
            "extended_import_duplicate_destination_count": int(extended_import_audit.get("duplicate_destination_count") or 0),
            "extended_import_existing_destination_count": int(extended_import_audit.get("existing_destination_count") or 0),
            "extended_import_would_improve_domains": extended_import_audit.get("would_improve_domains") or [],
            "extended_import_max_files_per_rule": int(extended_import_audit.get("max_files_per_rule") or 0),
            "extended_import_source_scan_limit_applied": bool(extended_import_audit.get("source_scan_limit_applied")),
            "extended_import_source_scan_limited_domains": extended_import_audit.get("source_scan_limited_domains") or [],
            "extended_import_source_scan_limited_rule_count": int(extended_import_audit.get("source_scan_limited_rule_count") or 0),
            "extended_import_full_audit_command": extended_import_audit.get("full_audit_command") or "",
            "extended_import_source_scan_full_audit_command": extended_import_audit.get("source_scan_full_audit_command") or "",
            "extended_import_gate": extended_import_verification.get("post_import_gate"),
            "extended_import_canonical_presence_ratio": extended_import_verification.get("canonical_presence_ratio"),
            "extended_import_missing_domains": extended_import_verification.get("missing_canonical_domains") or [],
        },
        "warnings": sorted(dict.fromkeys(warnings)),
        "suggested_next_action": suggested,
        "output_paths": {
            "dashboard_json": _rel(output_dir / "data_health_dashboard_latest.json"),
            "report": _rel(REPORT_PATH),
            "freshness": _rel(output_dir / "data_freshness_summary_latest.json"),
            "intraday": _rel(output_dir / "intraday_coverage_summary_latest.json"),
            "provider_failure_stats": _rel(output_dir / "provider_failure_stats_latest.json"),
            "data_grade_trend": _rel(output_dir / "data_grade_trend_latest.json"),
            "extended_data_readiness": _rel(output_dir / "extended_data_readiness_latest.json"),
            "extended_data_readiness_report": _rel(PROJECT_ROOT / "reports" / "extended_data_readiness_latest.md"),
            "tushare_queue_status": _rel(output_dir / "tushare_queue_status_latest.json"),
            "tushare_queue_status_report": _rel(PROJECT_ROOT / "reports" / "tushare_queue_status_latest.md"),
            "tushare_minute_status": _rel(output_dir / "tushare_minute_status_latest.json"),
            "tushare_minute_status_report": _rel(PROJECT_ROOT / "reports" / "tushare_minute_status_latest.md"),
            "tushare_minute_coverage": _rel(output_dir / "tushare_minute_coverage_latest.json"),
            "tushare_minute_coverage_report": _rel(PROJECT_ROOT / "reports" / "tushare_minute_coverage_latest.md"),
            "tushare_minute_quality": _rel(tushare_minute_quality.get("snapshot_path") or "results/data_health/tushare_minute_quality_latest.json"),
            "tushare_minute_quality_report": "reports/tushare_minute_quality_latest.md",
            "tushare_retry_plan": _rel(output_dir / "tushare_retry_plan_latest.json"),
            "tushare_retry_plan_report": _rel(PROJECT_ROOT / "reports" / "tushare_retry_plan_latest.md"),
            "extended_import_audit": _rel(output_dir / "extended_staged_import_audit_latest.json"),
            "extended_import_audit_report": _rel(PROJECT_ROOT / "reports" / "extended_staged_import_audit_latest.md"),
            "extended_import_verification": _rel(output_dir / "extended_import_verification_latest.json"),
            "extended_import_verification_report": _rel(PROJECT_ROOT / "reports" / "extended_import_verification_latest.md"),
            "extended_data_gates": _rel(output_dir / "extended_data_gates_latest.json"),
            "extended_data_gates_report": _rel(PROJECT_ROOT / "reports" / "extended_data_gates_latest.md"),
        },
        "generated_at": now_text(),
        "disclaimer": DISCLAIMER,
    }
    write_json(output_dir / "data_health_dashboard_latest.json", dashboard)
    write_dashboard_report(dashboard, REPORT_PATH)
    return dashboard


def _status_label(status: Any) -> str:
    labels = {
        "passed": "正常",
        "warning": "提示",
        "failed": "失败",
        "missing": "未生成",
        "skipped": "已跳过",
    }
    return labels.get(str(status or "").lower(), str(status or "-"))


def _print_remediation(actions: list[dict[str, Any]], *, max_actions: int = 3) -> None:
    print("")
    print("我现在该怎么修：")
    print("建议修复：")
    if not actions:
        print("1. 查看当前状态：")
        print("   python scripts/qd.py status")
        return
    for index, action in enumerate(actions[:max_actions], start=1):
        title = action.get("title") or action.get("why") or "修复建议"
        print(f"{index}. {title}")
        print("   需要显式联网授权" if action.get("network_required") else "   离线可运行，不会联网")
        print(f"   {action.get('command') or '-'}")
        if action.get("follow_up_command"):
            print("   然后：")
            print(f"   {action.get('follow_up_command')}")


def print_summary(payload: dict[str, Any], *, verbose: bool = False) -> None:
    if verbose:
        print_legacy_summary(payload)
        return
    freshness = payload.get("freshness") if isinstance(payload.get("freshness"), dict) else {}
    intraday = payload.get("intraday") if isinstance(payload.get("intraday"), dict) else {}
    health = payload.get("data_health_summary") if isinstance(payload.get("data_health_summary"), dict) else {}
    paths = payload.get("output_paths") if isinstance(payload.get("output_paths"), dict) else {}
    dashboard_path = paths.get("dashboard_json") or "results/data_health/data_health_dashboard_latest.json"
    symbols = payload.get("symbols_checked") or payload.get("symbols") or freshness.get("symbols_checked") or freshness.get("selected_symbols") or []
    checked = safe_symbol_count(symbols)
    total = int(payload.get("symbol_count") or freshness.get("symbol_count") or checked or 0)
    stale_symbols = freshness.get("stale_symbols") or payload.get("stale_symbols") or []
    missing_symbols = freshness.get("missing_symbols") or payload.get("missing_symbols") or []

    print("QuantDesk 数据健康诊断完成")
    print("")
    print(f"目标日期：{payload.get('target_date') or '-'}")
    print(f"统计范围：{payload.get('data_scope') or freshness.get('data_scope') or '-'}")
    print(f"检查数量：{checked or total} / 当前分析池 {total}")
    print(f"全池覆盖到：{freshness.get('coverage_date') or '-'}")
    print(f"最晚已有日线：{freshness.get('latest_date_max') or '-'}")
    print(f"状态：{_status_label(payload.get('status'))}")
    print(f"network: {str(payload.get('network')).lower()} (本命令不主动抓行情)")
    print(f"新鲜：{freshness.get('fresh_count', 0)}")
    print(f"过期：{freshness.get('stale_count', 0)}")
    print(f"缺失：{freshness.get('missing_count', 0)}")
    print(f"分钟线覆盖：{intraday.get('coverage_rate', 0)}%")
    if health.get("tushare_minute_target_symbol_count"):
        print(
            "Tushare 250d minute pool: "
            f"downloaded={health.get('tushare_minute_symbol_done_count', 0)}/{health.get('tushare_minute_symbol_count', 0)}, "
            f"available={health.get('tushare_minute_current_available_count', 0)}/{health.get('tushare_minute_target_symbol_count', 0)}, "
            f"canonical={health.get('tushare_minute_canonical_current_available_count', 0)}, "
            f"staged_pending_import={health.get('tushare_minute_staged_current_not_imported_count', 0)}, "
            f"missing_or_stale={health.get('tushare_minute_missing_or_stale_count', 0)}, "
            f"status={health.get('tushare_minute_status') or '-'}, "
            f"traceback={health.get('tushare_minute_traceback_count', 0)}, "
            f"action={health.get('tushare_minute_suggested_next_action') or '-'}"
        )
        if health.get("tushare_minute_status") == "running_with_warnings":
            print("Tushare minute warning policy: continue monitoring; no automatic restart without user confirmation")
    if health.get("tushare_minute_latest_completed_job_symbol_count"):
        print(
            "Tushare completed minute job: "
            f"available={health.get('tushare_minute_latest_completed_job_canonical_current_count', 0)}/"
            f"{health.get('tushare_minute_latest_completed_job_symbol_count', 0)}, "
            f"canonical_files={health.get('tushare_minute_canonical_file_count', 0)}, "
            f"job={health.get('tushare_minute_latest_completed_job_path') or '-'}"
        )
    if health.get("tushare_minute_latest_completed_quality_total_symbols"):
        print(
            "Tushare completed minute quality: "
            f"status={health.get('tushare_minute_latest_completed_quality_status') or '-'}, "
            f"usable={health.get('tushare_minute_latest_completed_quality_usable_count', 0)}/"
            f"{health.get('tushare_minute_latest_completed_quality_total_symbols', 0)}, "
            f"invalid={health.get('tushare_minute_latest_completed_quality_invalid_count', 0)}, "
            f"missing={health.get('tushare_minute_latest_completed_quality_missing_count', 0)}"
        )
    if stale_symbols:
        print(f"过期示例：{format_symbol_preview(stale_symbols, max_visible=5)}")
    if missing_symbols:
        print(f"缺失示例：{format_symbol_preview(missing_symbols, max_visible=5)}")
    omitted_note = build_symbols_omitted_note(symbols, max_visible=5, result_path=dashboard_path)
    if omitted_note:
        print(omitted_note)
    for warning in (payload.get("warnings") or [])[:3]:
        print(f"提示：{warning}")
    print("")
    print(f"结果文件：{dashboard_path}")
    if paths.get("report"):
        print(f"报告文件：{paths.get('report')}")
    remediation = payload.get("remediation_actions") if isinstance(payload.get("remediation_actions"), list) else []
    _print_remediation(remediation)
    print("")
    print(DISCLAIMER)


def print_legacy_summary(payload: dict[str, Any]) -> None:
    freshness = payload.get("freshness") if isinstance(payload.get("freshness"), dict) else {}
    intraday = payload.get("intraday") if isinstance(payload.get("intraday"), dict) else {}
    provider = payload.get("provider_failure_stats") if isinstance(payload.get("provider_failure_stats"), dict) else {}
    grade = payload.get("data_grade_trend") if isinstance(payload.get("data_grade_trend"), dict) else {}
    parameter = payload.get("parameter_sensitivity_summary") if isinstance(payload.get("parameter_sensitivity_summary"), dict) else {}
    health = payload.get("data_health_summary") if isinstance(payload.get("data_health_summary"), dict) else {}
    paths = payload.get("output_paths") if isinstance(payload.get("output_paths"), dict) else {}
    print("QuantDesk 数据健康仪表盘")
    print(f"target_date: {payload.get('target_date') or '-'}")
    print(f"symbols: {', '.join(payload.get('symbols') or []) or '-'}")
    print(f"data_scope: {payload.get('data_scope') or '-'}")
    print(f"coverage_date: {freshness.get('coverage_date') or '-'}")
    print(f"latest_date_max: {freshness.get('latest_date_max') or '-'}")
    print(f"network: {payload.get('network')} (本命令不主动抓行情)")
    print(f"数据新鲜度状态: {freshness.get('status') or 'missing'}")
    print(f"stale_count / missing_count: {freshness.get('stale_count', 0)} / {freshness.get('missing_count', 0)}")
    print(f"分钟线覆盖率: {intraday.get('coverage_rate', 0)} ({intraday.get('covered_count', 0)}/{intraday.get('symbol_count', 0)})")
    print(
        "tushare 250d minute coverage: "
        f"downloaded={health.get('tushare_minute_symbol_done_count', 0)}/{health.get('tushare_minute_symbol_count', 0)}, "
        f"available={health.get('tushare_minute_current_available_count', 0)}/{health.get('tushare_minute_target_symbol_count', 0)}, "
        f"canonical={health.get('tushare_minute_canonical_current_available_count', 0)}, "
        f"staged_pending_import={health.get('tushare_minute_staged_current_not_imported_count', 0)}, "
        f"missing_or_stale={health.get('tushare_minute_missing_or_stale_count', 0)}, "
        f"status={health.get('tushare_minute_status') or '-'}, "
        f"traceback={health.get('tushare_minute_traceback_count', 0)}, "
        f"action={health.get('tushare_minute_suggested_next_action') or '-'}"
    )
    if health.get("tushare_minute_status") == "running_with_warnings":
        print("tushare minute warning policy: continue monitoring; no automatic restart without user confirmation")
    if health.get("tushare_minute_latest_completed_job_symbol_count"):
        print(
            "tushare completed minute job: "
            f"available={health.get('tushare_minute_latest_completed_job_canonical_current_count', 0)}/"
            f"{health.get('tushare_minute_latest_completed_job_symbol_count', 0)}, "
            f"canonical_files={health.get('tushare_minute_canonical_file_count', 0)}"
        )
    if health.get("tushare_minute_latest_completed_quality_total_symbols"):
        print(
            "tushare completed minute quality: "
            f"status={health.get('tushare_minute_latest_completed_quality_status') or '-'}, "
            f"usable={health.get('tushare_minute_latest_completed_quality_usable_count', 0)}/"
            f"{health.get('tushare_minute_latest_completed_quality_total_symbols', 0)}, "
            f"invalid={health.get('tushare_minute_latest_completed_quality_invalid_count', 0)}, "
            f"missing={health.get('tushare_minute_latest_completed_quality_missing_count', 0)}"
        )
    print(f"provider 失败统计: failed={provider.get('failed_count', 0)}, skipped={provider.get('skipped_count', 0)}, offline_skipped={provider.get('offline_skipped_count', 0)}")
    print(f"data_grade 分布: {grade.get('grade_counts') or {}}")
    print(
        "parameter_sensitivity: "
        f"status={parameter.get('status') or 'missing'}, "
        f"robustness_score={parameter.get('robustness_score')}, "
        f"combination_count={parameter.get('combination_count', 0)}"
    )
    for warning in (payload.get("warnings") or [])[:20]:
        print(f"warning: {warning}")
    print(f"suggested_next_action: {payload.get('suggested_next_action') or '-'}")
    remediation = payload.get("remediation_actions") if isinstance(payload.get("remediation_actions"), list) else []
    if remediation:
        print("建议修复：")
        for action in remediation[:2]:
            print("需要显式联网授权" if action.get("network_required") else "离线可运行，不会联网")
            print(action.get("command") or "-")
            if action.get("follow_up_command"):
                print("离线可运行，不会联网")
                print(action.get("follow_up_command"))
    print(f"dashboard json: {paths.get('dashboard_json') or '-'}")
    print(f"report path: {paths.get('report') or '-'}")
    print(DISCLAIMER)


def main(argv: list[str] | None = None) -> int:
    started_at = now_text()
    start_time = time.perf_counter()
    args = _parse_args(argv)
    try:
        payload = run(args)
        remediation_plan = build_remediation_plan(data_health=payload)
        payload["remediation_plan"] = remediation_plan
        payload["remediation_actions"] = remediation_plan.get("actions", [])[:5]
        payload["next_actions"] = payload["remediation_actions"]
        paths = payload.get("output_paths") if isinstance(payload.get("output_paths"), dict) else {}
        if paths.get("dashboard_json"):
            write_json(PROJECT_ROOT / str(paths.get("dashboard_json")), payload)
        if truthy(args.summary):
            summary = build_cli_summary(payload, task_type="data_health_dashboard")
            if truthy(args.json):
                print(json.dumps(summary, ensure_ascii=True, indent=2, default=str))
            else:
                print(format_human_summary(summary))
        elif truthy(args.json):
            print(json.dumps(payload, ensure_ascii=False, indent=2))
        else:
            print_summary(payload, verbose=truthy(args.verbose))
        log_operation(
            command="data-health",
            args=sys.argv[1:] if argv is None else argv,
            target_date=args.target_date,
            symbols=args.symbols,
            network=truthy(args.network),
            llm_enabled=False,
            status=_operation_log_status(payload.get("status")),
            started_at=started_at,
            start_time=start_time,
            output_paths=list((payload.get("output_paths") or {}).values()),
            warnings=payload.get("warnings"),
            suggested_next_action=payload.get("suggested_next_action"),
        )
        # The command is a diagnostic producer: an unhealthy payload is a
        # successful diagnosis, not an execution failure.  Keep the payload
        # and operation-log status failed while reserving a non-zero exit code
        # for exceptions that prevented the snapshot from being produced.
        return 0
    except Exception as exc:  # noqa: BLE001
        log_operation(
            command="data-health",
            args=sys.argv[1:] if argv is None else argv,
            target_date=getattr(args, "target_date", ""),
            symbols=getattr(args, "symbols", ""),
            network=truthy(getattr(args, "network", "false")),
            llm_enabled=False,
            status="failed",
            started_at=started_at,
            start_time=start_time,
            failed_reason=str(exc),
            suggested_next_action="检查 data-health 参数和输出目录权限后重试。",
        )
        print(f"data-health failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
