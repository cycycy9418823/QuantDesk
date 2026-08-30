"""Read-only post-import verification for canonical extended data.

The staged importer answers "what would/could be copied"; this verifier answers
"what actually exists in canonical project directories now".  It is intentionally
read-only and conservative: it does not import, normalize, overwrite, fetch
network data, or touch ``data/daily``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from quantdesk.extended_data_readiness import build_extended_data_readiness
from quantdesk.extended_staged_data_importer import EXPECTED_IMPORT_DOMAINS
from quantdesk.shared import now_text, write_json, write_text


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT_PATH = PROJECT_ROOT / "results" / "data_health" / "extended_import_verification_latest.json"
DEFAULT_REPORT_PATH = PROJECT_ROOT / "reports" / "extended_import_verification_latest.md"


def _root(project_root: str | Path | None) -> Path:
    return Path(project_root).resolve() if project_root else PROJECT_ROOT


def _rel(path: Path, root: Path) -> str:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except Exception:
        return path.as_posix()


def _queue_is_running(readiness: dict[str, Any]) -> bool:
    queue = readiness.get("download_queue") if isinstance(readiness.get("download_queue"), dict) else {}
    status = str(queue.get("status") or "").strip().lower()
    if status in {"running", "running_or_present"}:
        return True
    if str(queue.get("current_stage") or "").strip() and status not in {"completed", "complete", "done", "finished", "success"}:
        return True
    return False


def _domain_rows(readiness: dict[str, Any]) -> dict[str, dict[str, Any]]:
    domains = readiness.get("domains") if isinstance(readiness.get("domains"), dict) else {}
    rows: dict[str, dict[str, Any]] = {}
    for domain in sorted(EXPECTED_IMPORT_DOMAINS):
        item = domains.get(domain) if isinstance(domains.get(domain), dict) else {}
        rows[domain] = {
            "domain": domain,
            "status": item.get("status") or "missing",
            "source_state": item.get("source_state") or "absent",
            "canonical_file_count": int(item.get("canonical_file_count") or 0),
            "staged_file_count": int(item.get("staged_file_count") or 0),
            "latest_date": item.get("latest_date") or "",
            "sample_paths": item.get("sample_paths") if isinstance(item.get("sample_paths"), list) else [],
            "warnings": item.get("warnings") if isinstance(item.get("warnings"), list) else [],
        }
    return rows


def _choose_gate(*, queue_running: bool, canonical_present: list[str], staged_not_imported: list[str], missing: list[str]) -> tuple[str, str, str]:
    if queue_running:
        return "warning", "wait_for_download_completion", "wait_for_queue_then_audit_and_import"
    if not canonical_present and (staged_not_imported or missing):
        return "warning", "no_canonical_import_yet", "run_audit_extended_import_then_confirm_apply"
    if missing or staged_not_imported:
        return "warning", "partial_canonical_import", "review_missing_domains_then_retry_or_defer"
    return "passed", "canonical_presence_verified", "run_data_readiness_and_market_intel"


def build_extended_import_verification(*, project_root: str | Path | None = None) -> dict[str, Any]:
    """Build a read-only verification payload for canonical extended import."""

    root = _root(project_root)
    readiness = build_extended_data_readiness(project_root=root)
    rows = _domain_rows(readiness)
    queue = readiness.get("download_queue") if isinstance(readiness.get("download_queue"), dict) else {}
    queue_running = _queue_is_running(readiness)
    canonical_present = sorted(domain for domain, row in rows.items() if int(row.get("canonical_file_count") or 0) > 0)
    missing_canonical = sorted(domain for domain in EXPECTED_IMPORT_DOMAINS if domain not in canonical_present)
    staged_not_imported = sorted(
        domain
        for domain, row in rows.items()
        if int(row.get("canonical_file_count") or 0) <= 0 and int(row.get("staged_file_count") or 0) > 0
    )
    absent = sorted(
        domain
        for domain, row in rows.items()
        if int(row.get("canonical_file_count") or 0) <= 0 and int(row.get("staged_file_count") or 0) <= 0
    )
    status, gate, suggested = _choose_gate(
        queue_running=queue_running,
        canonical_present=canonical_present,
        staged_not_imported=staged_not_imported,
        missing=absent,
    )
    warnings: list[str] = [
        "read_only_verification_no_canonical_writes",
        "research_only_no_broker_no_orders",
    ]
    if queue_running:
        warnings.append("download_queue_still_running")
    if staged_not_imported:
        warnings.append("staged_not_imported_domains:" + ",".join(staged_not_imported))
    if absent:
        warnings.append("missing_extended_domains:" + ",".join(absent))
    ratio = len(canonical_present) / max(1, len(EXPECTED_IMPORT_DOMAINS))
    return {
        "task_type": "extended_import_verification",
        "schema_version": "1.0",
        "status": status,
        "post_import_gate": gate,
        "canonical_presence_ratio": round(ratio, 6),
        "expected_domains": sorted(EXPECTED_IMPORT_DOMAINS),
        "canonical_present_domains": canonical_present,
        "missing_canonical_domains": missing_canonical,
        "staged_not_imported_domains": staged_not_imported,
        "absent_domains": absent,
        "domain_statuses": rows,
        "download_queue": queue,
        "canonical_writes_allowed": False,
        "suggested_next_action": suggested,
        "warnings": warnings,
        "generated_at": now_text(),
        "research_only": True,
        "paper_only": True,
        "no_network_fetch": True,
        "no_broker_route": True,
        "note": "Post-import verification checks canonical extended data presence only; it does not normalize schemas or prove full historical completeness.",
    }


def render_extended_import_verification_report(payload: dict[str, Any]) -> str:
    queue = payload.get("download_queue") if isinstance(payload.get("download_queue"), dict) else {}
    rows = payload.get("domain_statuses") if isinstance(payload.get("domain_statuses"), dict) else {}
    lines = [
        "# Extended Import Verification",
        "",
        "Research Only / Paper Account Only / No Broker Route / No Network Fetch / No Canonical Writes",
        "",
        "This report verifies whether staged extended data has materialized into canonical local directories. It does not import files, normalize schemas, or touch `data/daily`.",
        "",
        "## Summary",
        "",
        f"- status: {payload.get('status') or '-'}",
        f"- post_import_gate: {payload.get('post_import_gate') or '-'}",
        f"- canonical_presence_ratio: {payload.get('canonical_presence_ratio')}",
        f"- queue_status: {queue.get('status') or '-'}",
        f"- queue_stage: {queue.get('current_stage') or '-'}",
        f"- suggested_next_action: {payload.get('suggested_next_action') or '-'}",
        "",
        "## Domain Verification",
        "",
        "| domain | canonical_files | staged_files | status | latest_date |",
        "| --- | ---: | ---: | --- | --- |",
    ]
    for domain in sorted(EXPECTED_IMPORT_DOMAINS):
        row = rows.get(domain) if isinstance(rows.get(domain), dict) else {}
        lines.append(
            f"| {domain} | {int(row.get('canonical_file_count') or 0)} | "
            f"{int(row.get('staged_file_count') or 0)} | {row.get('status') or 'missing'} | {row.get('latest_date') or '-'} |"
        )
    lines.extend(["", "## Warnings", ""])
    warnings = payload.get("warnings") if isinstance(payload.get("warnings"), list) else []
    if warnings:
        lines.extend(f"- {warning}" for warning in warnings)
    else:
        lines.append("- none")
    lines.append("")
    return "\n".join(lines)


def write_extended_import_verification_outputs(
    *,
    project_root: str | Path | None = None,
    output_path: str | Path = DEFAULT_OUTPUT_PATH,
    report_path: str | Path = DEFAULT_REPORT_PATH,
) -> dict[str, Any]:
    root = _root(project_root)
    output = Path(output_path)
    report = Path(report_path)
    if not output.is_absolute():
        output = root / output
    if not report.is_absolute():
        report = root / report
    payload = build_extended_import_verification(project_root=root)
    payload["output_path"] = _rel(output, root)
    payload["report_path"] = _rel(report, root)
    write_json(output, payload)
    write_text(report, render_extended_import_verification_report(payload))
    return payload
