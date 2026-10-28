"""Read-only quality audit for completed Tushare 1-minute download jobs.

This module audits canonical ``data/intraday/1m`` files that correspond to a
completed local Tushare minute staging job.  It does not download data, does not
read provider tokens, does not import staged files, and does not write canonical
market data.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from quantdesk.intraday.intraday_quality_audit import audit_intraday_batch
from quantdesk.shared import now_text, read_json, write_json, write_text


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT_PATH = PROJECT_ROOT / "results" / "data_health" / "tushare_minute_quality_latest.json"
DEFAULT_REPORT_PATH = PROJECT_ROOT / "reports" / "tushare_minute_quality_latest.md"
LAST_PASSED_SNAPSHOT_NAME = "tushare_minute_quality_last_passed.json"
from quantdesk.intraday.minute_bar_lifecycle import FORMAL_EVIDENCE_STANDARD

ELIGIBILITY_STANDARD = FORMAL_EVIDENCE_STANDARD
DEFAULT_LAST_PASSED_OUTPUT_PATH = DEFAULT_OUTPUT_PATH.with_name(LAST_PASSED_SNAPSHOT_NAME)

MINUTE_JOB_GLOB = "data/vendor_staging/tushare_proxy_minutes_250d*"
DATE_RE = re.compile(r"(20\d{2})[-/]?(\d{2})[-/]?(\d{2})")
TOKEN_LIKE_RE = re.compile(r"\b[a-fA-F0-9]{32,}\b")


def _root(project_root: str | Path | None) -> Path:
    return Path(project_root).resolve() if project_root else PROJECT_ROOT


def _relative(path: Path, root: Path) -> str:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except Exception:
        return path.as_posix()


def _normalize_symbol(value: Any) -> str:
    text = str(value or "").strip().upper()
    if "." in text:
        text = text.split(".", 1)[0]
    digits = "".join(ch for ch in text if ch.isdigit())
    return digits.zfill(6)[-6:] if digits else ""


def _normalize_date(value: Any) -> str:
    match = DATE_RE.search(str(value or ""))
    if not match:
        return ""
    year, month, day = match.groups()
    return f"{year}-{month}-{day}"


def _minute_job_paths(root: Path) -> list[Path]:
    return sorted(
        [path for path in root.glob(MINUTE_JOB_GLOB) if path.is_dir()],
        key=lambda item: (item.stat().st_mtime, item.name),
        reverse=True,
    )


def _latest_completed_job(root: Path, explicit_job_dir: str | Path | None = None) -> tuple[Path | None, dict[str, Any]]:
    paths = [Path(explicit_job_dir).resolve()] if explicit_job_dir else _minute_job_paths(root)
    for job in paths:
        summary = read_json(job / "summary.json", default={})
        if not isinstance(summary, dict):
            continue
        status = str(summary.get("status") or "").strip().lower()
        if status in {"completed", "complete", "done", "finished", "success"}:
            return job, summary
    return None, {}


def _summary_symbols(summary: dict[str, Any]) -> list[str]:
    symbols: set[str] = set()
    rows = summary.get("download") if isinstance(summary.get("download"), list) else []
    for row in rows:
        if not isinstance(row, dict):
            continue
        symbol = _normalize_symbol(row.get("symbol") or row.get("ts_code"))
        if symbol:
            symbols.add(symbol)
    raw_symbols = summary.get("symbols") if isinstance(summary.get("symbols"), list) else []
    for item in raw_symbols:
        symbol = _normalize_symbol(item)
        if symbol:
            symbols.add(symbol)
    return sorted(symbols)


def _download_status_counts(summary: dict[str, Any]) -> dict[str, int]:
    counts: dict[str, int] = {}
    rows = summary.get("download") if isinstance(summary.get("download"), list) else []
    for row in rows:
        if not isinstance(row, dict):
            continue
        status = str(row.get("status") or "unknown").strip().lower() or "unknown"
        counts[status] = counts.get(status, 0) + 1
    return counts


def _overall_status(summary: dict[str, int]) -> str:
    if not summary:
        return "missing"
    if int(summary.get("invalid") or 0) or int(summary.get("missing") or 0):
        return "warning"
    if int(summary.get("insufficient") or 0) or int(summary.get("partial") or 0):
        return "partial"
    if int(summary.get("formal_execution_eligible") or 0) != int(summary.get("usable") or 0):
        return "partial"
    return "passed"


def _is_clean_pass_quality_payload(payload: Any) -> bool:
    """True when a quality-audit payload is a full clean pass (usable == total)."""

    if not isinstance(payload, dict) or payload.get("task_type") != "tushare_minute_quality_audit":
        return False
    if str(payload.get("status") or "") != "passed":
        return False
    if payload.get("eligibility_standard") != ELIGIBILITY_STANDARD:
        return False
    quality = payload.get("quality_summary") if isinstance(payload.get("quality_summary"), dict) else {}
    total = int(payload.get("total_symbols") or 0)
    usable = int(quality.get("usable") or 0)
    not_usable = sum(int(quality.get(key) or 0) for key in ("partial", "insufficient", "invalid", "missing"))
    formal = quality.get("formal_execution_eligible")
    return (total > 0 and usable == total and not_usable == 0
            and isinstance(formal, int) and not isinstance(formal, bool) and formal == total)


def load_latest_tushare_minute_quality_snapshot(
    *,
    project_root: str | Path | None = None,
    snapshot_path: str | Path | None = None,
) -> dict[str, Any]:
    """Load the latest minute-quality JSON without re-auditing large CSV files.

    Dashboards use this helper to surface an already-verified completed batch
    such as the Top200 minute pool.  It is intentionally read-only and cheap:
    it reads only the latest JSON snapshot and never scans ``data/intraday``.
    """

    root = _root(project_root)
    path = Path(snapshot_path) if snapshot_path else root / "results" / "data_health" / "tushare_minute_quality_latest.json"
    if not path.is_absolute():
        path = root / path
    payload = read_json(path, default={})
    if not isinstance(payload, dict) or payload.get("task_type") != "tushare_minute_quality_audit":
        return {
            "task_type": "tushare_minute_quality_snapshot",
            "status": "missing",
            "snapshot_available": False,
            "snapshot_path": _relative(path, root),
            "quality_summary": {},
            "job": {"path": ""},
            "warnings": [],
            "suggested_next_action": "run_tushare_minute_quality_after_completed_job",
            "read_only": True,
            "network_used": False,
            "canonical_write": False,
        }
    payload = dict(payload)
    payload["snapshot_available"] = True
    payload["snapshot_path"] = _relative(path, root)
    return payload


def summarize_completed_minute_quality_snapshot(
    snapshot: dict[str, Any] | None,
    *,
    expected_job_path: str = "",
) -> dict[str, Any]:
    """Return stable dashboard fields for a completed minute-quality snapshot."""

    payload = snapshot if isinstance(snapshot, dict) else {}
    quality = payload.get("quality_summary") if isinstance(payload.get("quality_summary"), dict) else {}
    job = payload.get("job") if isinstance(payload.get("job"), dict) else {}
    expected = str(expected_job_path or "").replace("\\", "/").strip()
    actual = str(job.get("path") or "").replace("\\", "/").strip()
    snapshot_available = bool(payload.get("snapshot_available")) or payload.get("task_type") == "tushare_minute_quality_audit"
    job_matches = bool(snapshot_available) and (not expected or actual == expected)
    status = str(payload.get("status") or "missing")
    if snapshot_available and expected and actual and actual != expected:
        status = "stale_or_mismatch"
    elif not snapshot_available:
        status = "missing"
    usable = int(quality.get("usable") or 0)
    partial = int(quality.get("partial") or 0)
    insufficient = int(quality.get("insufficient") or 0)
    invalid = int(quality.get("invalid") or 0)
    missing = int(quality.get("missing") or 0)
    total = int(payload.get("total_symbols") or job.get("summary_symbol_count") or 0)
    return {
        "status": status,
        "snapshot_available": snapshot_available,
        "snapshot_path": payload.get("snapshot_path") or "",
        "job_path": actual,
        "job_matches_latest_completed": job_matches,
        "target_date": payload.get("target_date") or job.get("target_end") or "",
        "total_symbols": total,
        "usable_count": usable,
        "formal_execution_eligible_count": int(quality.get("formal_execution_eligible") or 0),
        "partial_count": partial,
        "insufficient_count": insufficient,
        "invalid_count": invalid,
        "missing_count": missing,
        "usable_ratio": round(usable / total, 6) if total else 0.0,
        "suggested_next_action": payload.get("suggested_next_action") or "",
    }


def _pool_payload_from_snapshot(
    root: Path,
    snapshot: dict[str, Any],
    *,
    source_snapshot: str,
) -> dict[str, Any]:
    """Build one verified-pool payload from a single quality snapshot."""

    summary = summarize_completed_minute_quality_snapshot(snapshot)
    warnings = [str(item) for item in (snapshot.get("warnings") or []) if item]
    job_path = str(summary.get("job_path") or "").strip()
    job = root / job_path if job_path else None
    job_summary = read_json(job / "summary.json", default={}) if job else {}
    if not isinstance(job_summary, dict):
        job_summary = {}
    symbols = _summary_symbols(job_summary)
    is_clean_pass = (
        _is_clean_pass_quality_payload(snapshot)
        and
        summary.get("status") == "passed"
        and int(summary.get("total_symbols") or 0) > 0
        and int(summary.get("usable_count") or 0) == int(summary.get("total_symbols") or 0)
        and int(summary.get("formal_execution_eligible_count") or 0) == int(summary.get("total_symbols") or 0)
        and int(summary.get("partial_count") or 0) == 0
        and int(summary.get("insufficient_count") or 0) == 0
        and int(summary.get("invalid_count") or 0) == 0
        and int(summary.get("missing_count") or 0) == 0
    )
    if is_clean_pass and not symbols:
        warnings.append("verified_minute_pool_job_summary_symbols_missing")
    expected_total = int(summary.get("total_symbols") or 0)
    if is_clean_pass and expected_total and symbols and len(symbols) != expected_total:
        warnings.append(f"verified_minute_pool_symbol_count_mismatch:{len(symbols)}/{expected_total}")
        is_clean_pass = False
    status = "passed" if is_clean_pass and symbols else ("missing" if summary.get("status") == "missing" else "warning")
    return {
        "task_type": "verified_minute_pool_snapshot",
        "status": status,
        "quality_status": summary.get("status") or "missing",
        "snapshot_available": bool(summary.get("snapshot_available")),
        "snapshot_path": summary.get("snapshot_path") or "",
        "source_snapshot": source_snapshot,
        "job_path": job_path,
        "target_date": summary.get("target_date") or "",
        "total_symbols": expected_total,
        "usable_count": int(summary.get("usable_count") or 0),
        "partial_count": int(summary.get("partial_count") or 0),
        "insufficient_count": int(summary.get("insufficient_count") or 0),
        "invalid_count": int(summary.get("invalid_count") or 0),
        "missing_count": int(summary.get("missing_count") or 0),
        "symbol_count": len(symbols),
        "symbols": symbols if status == "passed" else [],
        "warnings": warnings,
        "suggested_next_action": summary.get("suggested_next_action") or "run_tushare_minute_quality_after_completed_job",
        "read_only": True,
        "network_used": False,
        "canonical_write": False,
    }


def _last_passed_snapshot_path(root: Path, snapshot_path: str | Path | None) -> Path:
    """Resolve the sibling last-passed snapshot path for a latest snapshot path."""

    if snapshot_path:
        base = Path(snapshot_path)
        if not base.is_absolute():
            base = root / base
        return base.with_name(LAST_PASSED_SNAPSHOT_NAME)
    return root / "results" / "data_health" / LAST_PASSED_SNAPSHOT_NAME


def build_verified_minute_pool_snapshot(
    *,
    project_root: str | Path | None = None,
    snapshot_path: str | Path | None = None,
) -> dict[str, Any]:
    """Return the usable symbol set from the latest passed minute-quality run.

    A symbol is considered part of the verified pool only when a quality
    snapshot passed and every audited symbol is usable.  When the latest
    quality snapshot is not a clean pass (for example a newer, larger
    completed job was audited with partial coverage), the pool falls back to
    the preserved ``tushare_minute_quality_last_passed.json`` snapshot so an
    already-verified pool such as Top200 is not silently invalidated.  The
    canonical CSV still has to be checked by consumers before a symbol can be
    treated as executable; this helper only supplies provenance and pool
    membership.
    """

    root = _root(project_root)
    latest_snapshot = load_latest_tushare_minute_quality_snapshot(project_root=root, snapshot_path=snapshot_path)
    pool = _pool_payload_from_snapshot(root, latest_snapshot, source_snapshot="latest")
    if pool["status"] == "passed":
        return pool
    last_passed_path = _last_passed_snapshot_path(root, snapshot_path)
    if last_passed_path.exists():
        preserved_snapshot = load_latest_tushare_minute_quality_snapshot(project_root=root, snapshot_path=last_passed_path)
        fallback = _pool_payload_from_snapshot(root, preserved_snapshot, source_snapshot="last_passed")
        if fallback["status"] == "passed":
            fallback["warnings"] = list(fallback.get("warnings") or []) + [
                "verified_pool_from_last_passed_snapshot",
                f"latest_quality_snapshot_status:{pool.get('quality_status') or 'missing'}",
            ]
            fallback["latest_quality_status"] = pool.get("quality_status") or "missing"
            fallback["latest_snapshot_path"] = pool.get("snapshot_path") or ""
            return fallback
    return pool


def build_tushare_minute_quality_audit(
    *,
    project_root: str | Path | None = None,
    job_dir: str | Path | None = None,
    canonical_dir: str | Path | None = None,
    target_date: str = "",
    max_items: int = 30,
) -> dict[str, Any]:
    root = _root(project_root)
    job, summary = _latest_completed_job(root, job_dir)
    if job is None:
        return {
            "task_type": "tushare_minute_quality_audit",
            "status": "missing",
            "job": {"path": "", "summary_status": ""},
            "target_date": _normalize_date(target_date),
            "total_symbols": 0,
            "quality_summary": {},
            "download_status_counts": {},
            "items": [],
            "warnings": ["missing:completed_minute_job"],
            "suggested_next_action": "wait_for_completed_minute_job",
            "read_only": True,
            "network_used": False,
            "token_output": False,
            "canonical_write": False,
        }
    symbols = _summary_symbols(summary)
    resolved_target_date = _normalize_date(target_date) or _normalize_date(summary.get("target_end"))
    canonical_base = Path(canonical_dir).resolve() if canonical_dir else root / "data" / "intraday" / "1m"
    audit = audit_intraday_batch(
        symbols,
        resolved_target_date,
        intraday_dir=canonical_base,
        write=False,
    )
    items = audit.get("items") if isinstance(audit.get("items"), list) else []
    quality_summary = audit.get("summary") if isinstance(audit.get("summary"), dict) else {}
    payload: dict[str, Any] = {
        "task_type": "tushare_minute_quality_audit",
        "eligibility_standard": ELIGIBILITY_STANDARD,
        "status": _overall_status({str(key): int(value or 0) for key, value in quality_summary.items()}),
        "job": {
            "path": _relative(job, root),
            "summary_status": summary.get("status") or "",
            "summary_symbol_count": summary.get("symbol_count") or 0,
            "target_end": _normalize_date(summary.get("target_end")),
            "days": summary.get("days") or 0,
            "download_records": len(summary.get("download")) if isinstance(summary.get("download"), list) else 0,
        },
        "canonical_dir": _relative(canonical_base, root),
        "target_date": resolved_target_date,
        "total_symbols": len(symbols),
        "quality_summary": quality_summary,
        "download_status_counts": _download_status_counts(summary),
        "items": items[: max(0, int(max_items))],
        "item_count_returned": min(len(items), max(0, int(max_items))),
        "item_count_total": len(items),
        "warnings": [],
        "suggested_next_action": "use_completed_minute_pool_for_research" if _overall_status(quality_summary) == "passed" else "review_minute_quality_warnings",
        "read_only": True,
        "network_used": False,
        "token_output": False,
        "canonical_write": False,
        "generated_at": now_text(),
    }
    if TOKEN_LIKE_RE.search(json.dumps(payload, ensure_ascii=False)):
        payload["warnings"].append("token_like_text_present_unexpected")
    return payload


def render_tushare_minute_quality_report(payload: dict[str, Any]) -> str:
    job = payload.get("job") if isinstance(payload.get("job"), dict) else {}
    quality = payload.get("quality_summary") if isinstance(payload.get("quality_summary"), dict) else {}
    download = payload.get("download_status_counts") if isinstance(payload.get("download_status_counts"), dict) else {}
    lines = [
        "# Tushare Minute Quality Audit",
        "",
        "Research Only / Paper Account Only / No Broker Route / No Network Fetch",
        "",
        f"- status: `{payload.get('status')}`",
        f"- job: `{job.get('path') or '-'}`",
        f"- summary_status: `{job.get('summary_status') or '-'}`",
        f"- target_date: `{payload.get('target_date') or '-'}`",
        f"- total_symbols: `{payload.get('total_symbols') or 0}`",
        f"- canonical_dir: `{payload.get('canonical_dir') or '-'}`",
        f"- suggested_next_action: `{payload.get('suggested_next_action') or '-'}`",
        "",
        "## Quality summary",
        "",
        "| status | count |",
        "|---|---:|",
    ]
    for key in ("usable", "partial", "insufficient", "invalid", "missing"):
        lines.append(f"| {key} | {int(quality.get(key) or 0)} |")
    lines.extend(["", "## Download status counts", "", "| status | count |", "|---|---:|"])
    for key in sorted(download):
        lines.append(f"| {key} | {int(download.get(key) or 0)} |")
    lines.extend(["", "Read-only / no token output / no network fetch / no canonical data write / no broker."])
    return "\n".join(lines)


def write_tushare_minute_quality_outputs(
    *,
    project_root: str | Path | None = None,
    job_dir: str | Path | None = None,
    canonical_dir: str | Path | None = None,
    target_date: str = "",
    max_items: int = 30,
    output_path: str | Path = DEFAULT_OUTPUT_PATH,
    report_path: str | Path = DEFAULT_REPORT_PATH,
) -> dict[str, Any]:
    output = Path(output_path)
    last_passed_path = output.with_name(LAST_PASSED_SNAPSHOT_NAME)
    previous = read_json(output, default={})
    if _is_clean_pass_quality_payload(previous):
        # Preserve the outgoing clean-pass snapshot before this run can
        # overwrite ``latest`` with a non-passed audit of a newer job.
        write_json(last_passed_path, previous)
    payload = build_tushare_minute_quality_audit(
        project_root=project_root,
        job_dir=job_dir,
        canonical_dir=canonical_dir,
        target_date=target_date,
        max_items=max_items,
    )
    if _is_clean_pass_quality_payload(payload):
        write_json(last_passed_path, payload)
    payload["output_path"] = str(output_path)
    payload["report_path"] = str(report_path)
    payload["last_passed_snapshot_path"] = str(last_passed_path) if last_passed_path.exists() else ""
    write_json(output_path, payload)
    write_text(report_path, render_tushare_minute_quality_report(payload))
    return payload
