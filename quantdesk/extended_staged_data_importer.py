"""Dry-run-first importer from vendor staging into canonical extended data dirs.

The importer is intentionally conservative:
- dry-run is the default;
- it copies CSV files only when ``apply=True``;
- it never touches ``data/daily`` or broker/live-trading state;
- it does not claim staged vendor files are clean, normalized, or complete.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from quantdesk.extended_data_commands import FULL_APPLY_COMMAND, FULL_AUDIT_COMMAND
from quantdesk.extended_data_readiness import build_extended_data_readiness
from quantdesk.shared import now_text, write_json
from quantdesk.tushare_queue_status import build_tushare_queue_status


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT_PATH = PROJECT_ROOT / "results" / "data_health" / "extended_staged_import_plan_latest.json"
DEFAULT_AUDIT_OUTPUT_PATH = PROJECT_ROOT / "results" / "data_health" / "extended_staged_import_audit_latest.json"
DEFAULT_AUDIT_REPORT_PATH = PROJECT_ROOT / "reports" / "extended_staged_import_audit_latest.md"
EXPECTED_IMPORT_DOMAINS = frozenset(
    {
        "sector_index",
        "sector_membership",
        "concept_theme",
        "intraday_1m",
        "moneyflow",
        "events",
        "fundamentals",
    }
)


@dataclass(frozen=True)
class ImportRule:
    domain: str
    source_patterns: tuple[str, ...]
    canonical_dir: Path
    filename_prefix: str = ""
    preserve_parent_dir: bool = False


IMPORT_RULES: tuple[ImportRule, ...] = (
    ImportRule(
        domain="sector_index",
        source_patterns=(
            "data/vendor_staging/tushare_continuous_queue_*/index_sectors/raw/index_daily/*.csv",
            "data/vendor_staging/tushare_proxy_extended_full_*/sectors/raw/index_daily/*.csv",
        ),
        canonical_dir=Path("data/sector/index"),
    ),
    ImportRule(
        domain="sector_index",
        source_patterns=(
            "data/vendor_staging/tushare_continuous_queue_*/index_sectors/raw/ths/ths_daily/*.csv",
            "data/vendor_staging/tushare_proxy_extended_full_*/sectors/raw/ths/ths_daily/*.csv",
        ),
        canonical_dir=Path("data/sector/index"),
        filename_prefix="ths_daily_",
    ),
    ImportRule(
        domain="sector_membership",
        source_patterns=(
            "data/vendor_staging/tushare_continuous_queue_*/index_sectors/raw/ths/ths_member/*.csv",
            "data/vendor_staging/tushare_proxy_extended_full_*/sectors/raw/ths/ths_member/*.csv",
        ),
        canonical_dir=Path("data/sector/members"),
        filename_prefix="ths_member_",
    ),
    ImportRule(
        domain="sector_membership",
        source_patterns=(
            "data/vendor_staging/tushare_continuous_queue_*/index_sectors/raw/index_weight/*/*.csv",
            "data/vendor_staging/tushare_proxy_extended_full_*/index/raw/index_weight/*/*.csv",
        ),
        canonical_dir=Path("data/sector/members/index_weight"),
        preserve_parent_dir=True,
    ),
    ImportRule(
        domain="concept_theme",
        source_patterns=(
            "data/vendor_staging/tushare_continuous_queue_*/index_sectors/raw/ths/ths_index.csv",
            "data/vendor_staging/tushare_proxy_extended_full_*/sectors/raw/ths/ths_index.csv",
        ),
        canonical_dir=Path("data/theme"),
    ),
    ImportRule(
        domain="moneyflow",
        source_patterns=(
            "data/vendor_staging/tushare_continuous_queue_*/moneyflow/raw/by_trade_date/moneyflow/*.csv",
            "data/vendor_staging/tushare_proxy_extended_full_*/moneyflow/raw/by_trade_date/moneyflow/*.csv",
        ),
        canonical_dir=Path("data/moneyflow/moneyflow"),
    ),
    ImportRule(
        domain="moneyflow",
        source_patterns=(
            "data/vendor_staging/tushare_continuous_queue_*/moneyflow/raw/by_trade_date/moneyflow_hsgt/*.csv",
            "data/vendor_staging/tushare_proxy_extended_full_*/moneyflow/raw/by_trade_date/moneyflow_hsgt/*.csv",
        ),
        canonical_dir=Path("data/moneyflow/moneyflow_hsgt"),
    ),
    ImportRule(
        domain="events",
        source_patterns=(
            "data/vendor_staging/tushare_continuous_queue_*/events/raw/by_trade_date/top_list/*.csv",
            "data/vendor_staging/tushare_proxy_extended_full_*/events/raw/by_trade_date/top_list/*.csv",
        ),
        canonical_dir=Path("data/events/top_list"),
    ),
    ImportRule(
        domain="fundamentals",
        source_patterns=(
            "data/vendor_staging/tushare_continuous_queue_*/fundamentals/raw/by_symbol/income/*.csv",
            "data/vendor_staging/tushare_proxy_extended_full_*/fundamentals/raw/by_symbol/income/*.csv",
        ),
        canonical_dir=Path("data/fundamentals/income"),
    ),
    ImportRule(
        domain="fundamentals",
        source_patterns=(
            "data/vendor_staging/tushare_continuous_queue_*/fundamentals/raw/by_symbol/balancesheet/*.csv",
            "data/vendor_staging/tushare_proxy_extended_full_*/fundamentals/raw/by_symbol/balancesheet/*.csv",
        ),
        canonical_dir=Path("data/fundamentals/balancesheet"),
    ),
    ImportRule(
        domain="fundamentals",
        source_patterns=(
            "data/vendor_staging/tushare_continuous_queue_*/fundamentals/raw/by_symbol/cashflow/*.csv",
            "data/vendor_staging/tushare_proxy_extended_full_*/fundamentals/raw/by_symbol/cashflow/*.csv",
        ),
        canonical_dir=Path("data/fundamentals/cashflow"),
    ),
    ImportRule(
        domain="fundamentals",
        source_patterns=(
            "data/vendor_staging/tushare_continuous_queue_*/fundamentals/raw/by_symbol/fina_indicator/*.csv",
            "data/vendor_staging/tushare_proxy_extended_full_*/fundamentals/raw/by_symbol/fina_indicator/*.csv",
        ),
        canonical_dir=Path("data/fundamentals/fina_indicator"),
    ),
    ImportRule(
        domain="fundamentals",
        source_patterns=(
            "data/vendor_staging/tushare_continuous_queue_*/fundamentals/raw/by_symbol/dividend/*.csv",
            "data/vendor_staging/tushare_proxy_extended_full_*/fundamentals/raw/by_symbol/dividend/*.csv",
        ),
        canonical_dir=Path("data/fundamentals/dividend"),
    ),
    ImportRule(
        domain="fundamentals",
        source_patterns=(
            "data/vendor_staging/tushare_continuous_queue_*/fundamentals/raw/by_symbol/forecast/*.csv",
            "data/vendor_staging/tushare_proxy_extended_full_*/fundamentals/raw/by_symbol/forecast/*.csv",
        ),
        canonical_dir=Path("data/fundamentals/forecast"),
    ),
    ImportRule(
        domain="fundamentals",
        source_patterns=(
            "data/vendor_staging/tushare_continuous_queue_*/fundamentals/raw/by_symbol/express/*.csv",
            "data/vendor_staging/tushare_proxy_extended_full_*/fundamentals/raw/by_symbol/express/*.csv",
        ),
        canonical_dir=Path("data/fundamentals/express"),
    ),
    ImportRule(
        domain="fundamentals",
        source_patterns=(
            "data/vendor_staging/tushare_continuous_queue_*/fundamentals/raw/stock_basic/*.csv",
            "data/vendor_staging/tushare_proxy_extended_full_*/fundamentals/raw/stock_basic/*.csv",
        ),
        canonical_dir=Path("data/fundamentals/stock_basic"),
    ),
    ImportRule(
        domain="events",
        source_patterns=(
            "data/vendor_staging/tushare_continuous_queue_*/events/raw/by_trade_date/top_inst/*.csv",
            "data/vendor_staging/tushare_proxy_extended_full_*/events/raw/by_trade_date/top_inst/*.csv",
        ),
        canonical_dir=Path("data/events/top_inst"),
    ),
    ImportRule(
        domain="events",
        source_patterns=(
            "data/vendor_staging/tushare_continuous_queue_*/events/raw/by_trade_date/margin/*.csv",
            "data/vendor_staging/tushare_proxy_extended_full_*/events/raw/by_trade_date/margin/*.csv",
        ),
        canonical_dir=Path("data/events/margin"),
    ),
    ImportRule(
        domain="events",
        source_patterns=(
            "data/vendor_staging/tushare_continuous_queue_*/events/raw/by_trade_date/margin_detail/*.csv",
            "data/vendor_staging/tushare_proxy_extended_full_*/events/raw/by_trade_date/margin_detail/*.csv",
        ),
        canonical_dir=Path("data/events/margin_detail"),
    ),
    ImportRule(
        domain="events",
        source_patterns=(
            "data/vendor_staging/tushare_continuous_queue_*/events/raw/by_trade_date/block_trade/*.csv",
            "data/vendor_staging/tushare_proxy_extended_full_*/events/raw/by_trade_date/block_trade/*.csv",
        ),
        canonical_dir=Path("data/events/block_trade"),
    ),
    ImportRule(
        domain="events",
        source_patterns=(
            "data/vendor_staging/tushare_continuous_queue_*/events/raw/by_trade_date/limit_list_d/*.csv",
            "data/vendor_staging/tushare_proxy_extended_full_*/events/raw/by_trade_date/limit_list_d/*.csv",
        ),
        canonical_dir=Path("data/events/limit_list_d"),
    ),
    ImportRule(
        domain="intraday_1m",
        source_patterns=(
            "data/vendor_staging/tushare_continuous_queue_*/minute_partial_retry/raw/1min/*.csv",
            "data/vendor_staging/tushare_proxy_minutes_*/raw/1min/*.csv",
            "data/vendor_staging/tushare_proxy_minutes_*/1m/*.csv",
        ),
        canonical_dir=Path("data/intraday/1m"),
    ),
)


def _root(project_root: str | Path | None) -> Path:
    return Path(project_root).resolve() if project_root else PROJECT_ROOT


def _rel(path: Path, root: Path) -> str:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except Exception:
        return path.as_posix()


def _iter_sources(root: Path, rule: ImportRule, *, max_files: int) -> tuple[list[Path], bool]:
    sources: list[Path] = []
    seen: set[Path] = set()
    for pattern in rule.source_patterns:
        for path in root.glob(pattern):
            if not path.is_file() or path.suffix.lower() != ".csv" or path in seen:
                continue
            if max_files and len(sources) >= max_files:
                return sources, True
            seen.add(path)
            sources.append(path)
    return sources, False


def _destination_for(root: Path, rule: ImportRule, source: Path) -> Path:
    base = root / rule.canonical_dir
    if rule.preserve_parent_dir:
        base = base / source.parent.name
    return base / f"{rule.filename_prefix}{source.name}"


def _copy_file(source: Path, destination: Path, *, overwrite: bool) -> str:
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists() and not overwrite:
        return "skipped_exists"
    shutil.copy2(source, destination)
    return "copied"


def build_extended_staged_import_plan(
    *,
    project_root: str | Path | None = None,
    apply: bool = False,
    allow_running_queue: bool = False,
    overwrite: bool = False,
    max_files_per_rule: int = 0,
    output_path: str | Path | None = None,
) -> dict[str, Any]:
    root = _root(project_root)
    queue_status_payload = build_tushare_queue_status(project_root=root, include_processes=False)
    queue_state = str(queue_status_payload.get("status") or "").strip().lower()
    queue_running = queue_state in {"running", "running_or_present"} or (
        queue_state not in {"", "missing", "completed", "complete", "done", "finished", "success"}
        and str(queue_status_payload.get("suggested_next_action") or "") == "continue_monitoring"
    )
    apply_requested = bool(apply)
    full_scan_requested = int(max_files_per_rule or 0) <= 0
    short_circuit_running_queue = bool(queue_running and not allow_running_queue and full_scan_requested)
    blocked_by_running_queue = bool(apply_requested and queue_running and not allow_running_queue)
    effective_apply = bool(apply_requested and not blocked_by_running_queue and not short_circuit_running_queue)
    actions: list[dict[str, Any]] = []
    copied = 0
    skipped = 0
    domain_counts: dict[str, int] = {}
    destination_counts: dict[str, int] = {}
    limited_rules: list[dict[str, Any]] = []
    if not short_circuit_running_queue:
        for rule in IMPORT_RULES:
            sources, scan_limited = _iter_sources(root, rule, max_files=max_files_per_rule)
            if scan_limited:
                limited_rules.append(
                    {
                        "domain": rule.domain,
                        "canonical_dir": rule.canonical_dir.as_posix(),
                        "max_files_per_rule": int(max_files_per_rule or 0),
                    }
                )
            for source in sources:
                dest = _destination_for(root, rule, source)
                dest_rel = _rel(dest, root)
                destination_counts[dest_rel] = destination_counts.get(dest_rel, 0) + 1
                domain_counts[rule.domain] = domain_counts.get(rule.domain, 0) + 1
                status = "blocked_running_queue" if blocked_by_running_queue else "planned"
                if effective_apply:
                    if rule.domain == "intraday_1m":
                        status = "blocked_requires_dedicated_intraday_importer"
                    else:
                        status = _copy_file(source, dest, overwrite=overwrite)
                        if status == "copied":
                            copied += 1
                        elif status == "skipped_exists":
                            skipped += 1
                actions.append(
                    {
                        "domain": rule.domain,
                        "source": _rel(source, root),
                        "destination": dest_rel,
                        "status": status,
                    }
                )
    duplicate_destinations = sorted(path for path, count in destination_counts.items() if count > 1)
    warnings = [
        (
            "blocked_running_download_queue_no_canonical_writes"
            if blocked_by_running_queue
            else ("dry_run_only_no_canonical_writes" if not effective_apply else "canonical_extended_data_written_from_staging")
        ),
        "copied_files_are_not_schema_normalized",
        "research_only_no_broker_no_orders",
    ]
    if queue_running:
        warnings.append("download_queue_still_running")
    if short_circuit_running_queue:
        warnings.append("full_import_plan_deferred_until_download_queue_completes")
    if duplicate_destinations:
        warnings.append(f"duplicate_destination_paths:{len(duplicate_destinations)}")
    if limited_rules:
        warnings.append(f"source_scan_limited_by_max_files_per_rule:{len(limited_rules)}")
    if any(
        action.get("status") == "blocked_requires_dedicated_intraday_importer"
        for action in actions
    ):
        warnings.append("intraday_1m_requires_staging_quality_audit_append_only_import")
    limited_domains = sorted({str(item.get("domain") or "") for item in limited_rules if item.get("domain")})
    payload = {
        "task_type": "extended_staged_data_import",
        "schema_version": "1.0",
        "status": "blocked" if (blocked_by_running_queue or short_circuit_running_queue) else ("applied" if effective_apply else "dry_run"),
        "dry_run": not bool(effective_apply),
        "apply": bool(effective_apply),
        "apply_requested": apply_requested,
        "allow_running_queue": bool(allow_running_queue),
        "blocked_reason": "download_queue_still_running" if (blocked_by_running_queue or short_circuit_running_queue) else "",
        "overwrite": bool(overwrite),
        "max_files_per_rule": int(max_files_per_rule or 0),
        "source_scan_limit_applied": bool(limited_rules),
        "source_scan_limited_rule_count": len(limited_rules),
        "source_scan_limited_domains": limited_domains,
        "source_scan_limited_rules": limited_rules,
        "full_audit_command": FULL_AUDIT_COMMAND,
        "source_scan_full_audit_command": FULL_AUDIT_COMMAND if limited_rules else "",
        "planned_file_count": len(actions),
        "copied_file_count": copied,
        "skipped_existing_count": skipped,
        "domain_counts": domain_counts,
        "duplicate_destination_count": len(duplicate_destinations),
        "duplicate_destination_samples": duplicate_destinations[:10],
        "actions": actions,
        "readiness_after": build_extended_data_readiness(project_root=root) if effective_apply else {},
        "download_queue": queue_status_payload.get("queue") if isinstance(queue_status_payload.get("queue"), dict) else {},
        "queue_status": queue_status_payload.get("status"),
        "suggested_next_action": "wait_for_queue_then_rerun_audit" if (blocked_by_running_queue or short_circuit_running_queue) else "",
        "warnings": warnings,
        "generated_at": now_text(),
    }
    if output_path is not None:
        output = Path(output_path)
        if not output.is_absolute():
            output = root / output
        payload["output_path"] = _rel(output, root)
        write_json(output, payload)
    return payload


def _group_duplicate_actions(actions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for action in actions:
        destination = str(action.get("destination") or "")
        if not destination:
            continue
        grouped.setdefault(destination, []).append(action)
    duplicates: list[dict[str, Any]] = []
    for destination, rows in sorted(grouped.items()):
        if len(rows) <= 1:
            continue
        duplicates.append(
            {
                "destination": destination,
                "count": len(rows),
                "domains": sorted({str(row.get("domain") or "") for row in rows if row.get("domain")}),
                "sources": [str(row.get("source") or "") for row in rows[:5]],
            }
        )
    return duplicates


def _existing_destinations(root: Path, actions: list[dict[str, Any]]) -> list[str]:
    existing: list[str] = []
    for action in actions:
        destination = str(action.get("destination") or "")
        if destination and (root / destination).exists():
            existing.append(destination)
    return sorted(set(existing))


def _status_from_audit(
    *,
    planned_file_count: int,
    duplicate_destination_count: int,
    existing_destination_count: int,
    missing_planned_domains: list[str],
    queue_running: bool,
) -> str:
    if planned_file_count <= 0:
        return "missing"
    if duplicate_destination_count or existing_destination_count or missing_planned_domains or queue_running:
        return "warning"
    return "passed"


def _preapply_gate(
    *,
    planned_file_count: int,
    duplicate_destination_count: int,
    existing_destination_count: int,
    queue_running: bool,
    source_scan_limit_applied: bool,
) -> str:
    if planned_file_count <= 0:
        return "no_staged_files_found"
    if queue_running:
        return "wait_for_download_completion"
    if duplicate_destination_count or existing_destination_count:
        return "review_required"
    if source_scan_limit_applied:
        return "ready_for_bounded_apply"
    return "ready_for_full_apply"


def _queue_is_running_for_audit(queue: dict[str, Any]) -> bool:
    status = str(queue.get("status") or "").strip().lower()
    if status in {"completed", "complete", "done", "finished", "success"}:
        return False
    if status in {"running", "running_or_present"}:
        return True
    return bool(str(queue.get("current_stage") or "").strip())


def _queue_runtime_state(
    queue_status_payload: dict[str, Any],
    *,
    queue_running: bool,
) -> tuple[str, bool]:
    processes = queue_status_payload.get("processes")
    process_counts = processes.get("counts") if isinstance(processes, dict) else {}
    if not isinstance(process_counts, dict):
        process_counts = {}
    queue_process_found = any(
        int(process_counts.get(name) or 0) > 0
        for name in (
            "tushare_continuous_data_queue.py",
            "tushare_proxy_extended_harvest.py",
        )
    )
    if not queue_running:
        return "inactive", queue_process_found
    suggested = str(queue_status_payload.get("suggested_next_action") or "")
    if suggested == "restart_queue_after_user_approval" and not queue_process_found:
        return "stale", False
    if suggested == "continue_monitoring" or queue_process_found:
        return "active", queue_process_found
    return "unknown", queue_process_found


def _markdown_audit_lines(payload: dict[str, Any]) -> list[str]:
    queue = payload.get("download_queue") if isinstance(payload.get("download_queue"), dict) else {}
    lines = [
        "# Extended Staged Import Audit",
        "",
        "Research Only / Paper Account Only / No Broker Route / No Network Fetch",
        "",
        "This audit is a dry-run preflight for staged extended market data. It does not copy files, normalize schemas, or touch `data/daily`.",
        "",
        "## Summary",
        "",
        f"- status: {payload.get('status') or 'unknown'}",
        f"- preapply_gate: {payload.get('preapply_gate') or '-'}",
        f"- planned_file_count: {payload.get('planned_file_count') or 0}",
        f"- max_files_per_rule: {payload.get('max_files_per_rule') or 0}",
        f"- source_scan_limit_applied: {payload.get('source_scan_limit_applied')}",
        f"- source_scan_limited_domains: {', '.join(payload.get('source_scan_limited_domains') or []) or '-'}",
        f"- full_audit_command: {payload.get('full_audit_command') or '-'}",
        f"- source_scan_full_audit_command: {payload.get('source_scan_full_audit_command') or '-'}",
        f"- duplicate_destination_count: {payload.get('duplicate_destination_count') or 0}",
        f"- existing_destination_count: {payload.get('existing_destination_count') or 0}",
        f"- missing_planned_domains: {', '.join(payload.get('missing_planned_domains') or []) or '-'}",
        f"- suggested_next_action: {payload.get('suggested_next_action') or '-'}",
        f"- apply_command_after_review: {payload.get('apply_command_after_review') or '-'}",
        "",
        "## Download Queue",
        "",
        f"- queue_status: {queue.get('status') or '-'}",
        f"- queue_stage: {queue.get('current_stage') or '-'}",
        f"- queue_stage_status: {queue.get('current_stage_status') or '-'}",
        f"- queue_path: {queue.get('path') or '-'}",
        f"- queue_liveness: {payload.get('queue_liveness') or '-'}",
        f"- queue_process_found: {payload.get('queue_process_found')}",
        "",
        "## Domain Counts",
        "",
        "| domain | planned_files | current_status | would_improve |",
        "| --- | ---: | --- | --- |",
    ]
    domain_counts = payload.get("domain_counts") if isinstance(payload.get("domain_counts"), dict) else {}
    current_statuses = payload.get("current_domain_statuses") if isinstance(payload.get("current_domain_statuses"), dict) else {}
    would_improve = set(payload.get("would_improve_domains") or [])
    for domain in sorted(EXPECTED_IMPORT_DOMAINS):
        lines.append(
            f"| {domain} | {int(domain_counts.get(domain, 0) or 0)} | "
            f"{current_statuses.get(domain) or 'missing'} | {'yes' if domain in would_improve else 'no'} |"
        )
    lines.extend(["", "## Duplicate Destinations", ""])
    duplicates = payload.get("duplicate_destinations") if isinstance(payload.get("duplicate_destinations"), list) else []
    if duplicates:
        for item in duplicates[:20]:
            lines.append(f"- {item.get('destination')}: {item.get('count')} sources")
    else:
        lines.append("- none")
    lines.extend(["", "## Warnings", ""])
    warnings = [str(item) for item in (payload.get("warnings") or []) if item]
    lines.extend([f"- {warning}" for warning in warnings] or ["- none"])
    if payload.get("queue_liveness") == "stale":
        lines.extend(
            [
                "",
                "Queue appears stalled; manual review is required before any restart or staged import.",
            ]
        )
    return lines


def build_extended_staged_import_audit(
    *,
    project_root: str | Path | None = None,
    max_files_per_rule: int = 0,
    output_path: str | Path | None = None,
    report_path: str | Path | None = None,
) -> dict[str, Any]:
    """Build a read-only preflight audit for extended staged import.

    This is deliberately stronger than the plan summary: it highlights duplicate
    destinations, existing canonical files that would be skipped/overwritten, and
    download queues that are still running before anyone considers ``--apply``.
    """

    root = _root(project_root)
    readiness = build_extended_data_readiness(project_root=root)
    queue = readiness.get("download_queue") if isinstance(readiness.get("download_queue"), dict) else {}
    queue_status_payload = build_tushare_queue_status(
        project_root=root,
        include_processes=True,
    )
    detailed_queue = queue_status_payload.get("queue")
    if isinstance(detailed_queue, dict) and detailed_queue:
        queue = {**queue, **detailed_queue}
    queue_running = _queue_is_running_for_audit(queue)
    queue_liveness, queue_process_found = _queue_runtime_state(
        queue_status_payload,
        queue_running=queue_running,
    )
    queue_stale = queue_running and queue_liveness == "stale"
    max_files_per_rule_value = int(max_files_per_rule or 0)
    domains = readiness.get("domains") if isinstance(readiness.get("domains"), dict) else {}
    current_domain_statuses = {
        domain: str((domains.get(domain) or {}).get("status") or "missing") for domain in sorted(EXPECTED_IMPORT_DOMAINS)
    }
    if queue_running and max_files_per_rule_value <= 0:
        warnings = [
            "dry_run_audit_only_no_canonical_writes",
            "copied_files_would_not_be_schema_normalized",
            "research_only_no_broker_no_orders",
            "download_queue_still_running",
            "full_audit_deferred_until_download_queue_completes",
        ]
        if queue_stale:
            warnings.append("download_queue_stale_manual_review_required")
        payload = {
            "task_type": "extended_staged_import_audit",
            "schema_version": "1.0",
            "status": "warning",
            "preapply_gate": (
                "wait_for_download_completion_queue_stale_manual_review"
                if queue_stale
                else "wait_for_download_completion"
            ),
            "planned_file_count": 0,
            "max_files_per_rule": max_files_per_rule_value,
            "source_scan_limit_applied": False,
            "source_scan_limited_rule_count": 0,
            "source_scan_limited_domains": [],
            "source_scan_limited_rules": [],
            "full_audit_command": FULL_AUDIT_COMMAND,
            "source_scan_full_audit_command": "",
            "domain_counts": {},
            "missing_planned_domains": [],
            "duplicate_destination_count": 0,
            "duplicate_destinations": [],
            "existing_destination_count": 0,
            "existing_destination_samples": [],
            "current_domain_statuses": current_domain_statuses,
            "would_improve_domains": [],
            "download_queue": queue,
            "queue_liveness": queue_liveness,
            "queue_process_found": queue_process_found,
            "canonical_writes_allowed": False,
            "apply_command_after_review": "",
            "suggested_next_action": (
                "review_queue_state_then_restart_or_confirm"
                if queue_stale
                else "wait_for_queue_then_rerun_audit"
            ),
            "warnings": warnings,
            "generated_at": now_text(),
        }
        if output_path is not None:
            output = Path(output_path)
            if not output.is_absolute():
                output = root / output
            payload["output_path"] = _rel(output, root)
            write_json(output, payload)
        if report_path is not None:
            report = Path(report_path)
            if not report.is_absolute():
                report = root / report
            payload["report_path"] = _rel(report, root)
            report.parent.mkdir(parents=True, exist_ok=True)
            report.write_text("\n".join(_markdown_audit_lines(payload)).rstrip() + "\n", encoding="utf-8")
        return payload
    plan = build_extended_staged_import_plan(
        project_root=root,
        apply=False,
        overwrite=False,
        max_files_per_rule=max_files_per_rule,
        output_path=None,
    )
    actions = [action for action in plan.get("actions", []) if isinstance(action, dict)]
    domain_counts = dict(plan.get("domain_counts") or {})
    duplicate_groups = _group_duplicate_actions(actions)
    existing_destinations = _existing_destinations(root, actions)
    missing_planned_domains = sorted(domain for domain in EXPECTED_IMPORT_DOMAINS if int(domain_counts.get(domain, 0) or 0) <= 0)
    would_improve_domains = sorted(
        domain
        for domain, count in domain_counts.items()
        if int(count or 0) > 0 and current_domain_statuses.get(domain, "missing") in {"missing", "staged_not_imported", "partial"}
    )
    planned_file_count = int(plan.get("planned_file_count") or 0)
    max_files_per_rule_value = int(plan.get("max_files_per_rule") or max_files_per_rule_value or 0)
    source_scan_limit_applied = bool(plan.get("source_scan_limit_applied"))
    source_scan_limited_domains = sorted(str(item) for item in (plan.get("source_scan_limited_domains") or []) if str(item))
    source_scan_limited_rules = [
        item for item in (plan.get("source_scan_limited_rules") or []) if isinstance(item, dict)
    ]
    duplicate_destination_count = len(duplicate_groups)
    existing_destination_count = len(existing_destinations)
    warnings = [
        "dry_run_audit_only_no_canonical_writes",
        "copied_files_would_not_be_schema_normalized",
        "research_only_no_broker_no_orders",
    ]
    if duplicate_destination_count:
        warnings.append(f"duplicate_destination_paths:{duplicate_destination_count}")
    if existing_destination_count:
        warnings.append(f"existing_destination_paths:{existing_destination_count}")
    if missing_planned_domains:
        warnings.append("missing_planned_domains:" + ",".join(missing_planned_domains))
    if queue_running:
        warnings.append("download_queue_still_running")
    if queue_stale:
        warnings.append("download_queue_stale_manual_review_required")
    if source_scan_limit_applied:
        warnings.append(f"source_scan_limited_by_max_files_per_rule:{len(source_scan_limited_rules)}")
    preapply_gate = _preapply_gate(
        planned_file_count=planned_file_count,
        duplicate_destination_count=duplicate_destination_count,
        existing_destination_count=existing_destination_count,
        queue_running=queue_running,
        source_scan_limit_applied=source_scan_limit_applied,
    )
    if queue_stale:
        preapply_gate = "wait_for_download_completion_queue_stale_manual_review"
    apply_command_after_review = (
        FULL_APPLY_COMMAND
        if preapply_gate in {"ready_for_bounded_apply", "ready_for_full_apply"}
        else ""
    )
    suggested_next_action = (
        "review_queue_state_then_restart_or_confirm"
        if queue_stale
        else "wait_for_queue_then_rerun_audit"
        if preapply_gate == "wait_for_download_completion"
        else (
            "review_duplicates_existing_destinations_then_rerun_audit"
            if preapply_gate == "review_required"
            else (
                "check_staging_outputs"
                if preapply_gate == "no_staged_files_found"
                else ("confirm_bounded_apply_with_user" if preapply_gate == "ready_for_bounded_apply" else "confirm_full_apply_with_user")
            )
        )
    )
    payload = {
        "task_type": "extended_staged_import_audit",
        "schema_version": "1.0",
        "status": _status_from_audit(
            planned_file_count=planned_file_count,
            duplicate_destination_count=duplicate_destination_count,
            existing_destination_count=existing_destination_count,
            missing_planned_domains=missing_planned_domains,
            queue_running=queue_running,
        ),
        "preapply_gate": preapply_gate,
        "planned_file_count": planned_file_count,
        "max_files_per_rule": max_files_per_rule_value,
        "source_scan_limit_applied": source_scan_limit_applied,
        "source_scan_limited_rule_count": len(source_scan_limited_rules),
        "source_scan_limited_domains": source_scan_limited_domains,
        "source_scan_limited_rules": source_scan_limited_rules,
        "full_audit_command": FULL_AUDIT_COMMAND,
        "source_scan_full_audit_command": FULL_AUDIT_COMMAND if source_scan_limit_applied else "",
        "domain_counts": domain_counts,
        "missing_planned_domains": missing_planned_domains,
        "duplicate_destination_count": duplicate_destination_count,
        "duplicate_destinations": duplicate_groups[:50],
        "existing_destination_count": existing_destination_count,
        "existing_destination_samples": existing_destinations[:50],
        "current_domain_statuses": current_domain_statuses,
        "would_improve_domains": would_improve_domains,
        "download_queue": queue,
        "queue_liveness": queue_liveness,
        "queue_process_found": queue_process_found,
        "canonical_writes_allowed": False,
        "apply_command_after_review": apply_command_after_review,
        "suggested_next_action": suggested_next_action,
        "warnings": warnings,
        "generated_at": now_text(),
    }
    if output_path is not None:
        output = Path(output_path)
        if not output.is_absolute():
            output = root / output
        payload["output_path"] = _rel(output, root)
        write_json(output, payload)
    if report_path is not None:
        report = Path(report_path)
        if not report.is_absolute():
            report = root / report
        payload["report_path"] = _rel(report, root)
        report.parent.mkdir(parents=True, exist_ok=True)
        report.write_text("\n".join(_markdown_audit_lines(payload)).rstrip() + "\n", encoding="utf-8")
    return payload
