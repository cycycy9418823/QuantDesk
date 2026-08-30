"""Read-only readiness contract for extended local market data.

This module does not import, mutate, or normalize vendor data.  It only reports
which local/staging data families are present so downstream reports can degrade
honestly until canonical importers are wired.
"""

from __future__ import annotations

import csv
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from quantdesk.extended_data_commands import FULL_AUDIT_COMMAND
from quantdesk.shared import write_json


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT_PATH = PROJECT_ROOT / "results" / "data_health" / "extended_data_readiness_latest.json"
DEFAULT_REPORT_PATH = PROJECT_ROOT / "reports" / "extended_data_readiness_latest.md"


@dataclass(frozen=True)
class DomainSpec:
    key: str
    label: str
    canonical_patterns: tuple[str, ...]
    staging_patterns: tuple[str, ...]
    capability: str
    available_threshold: int = 1


DOMAIN_SPECS: tuple[DomainSpec, ...] = (
    DomainSpec(
        key="daily_core",
        label="Daily OHLCV core",
        canonical_patterns=("data/daily/*.csv", "data/daily/*.parquet"),
        staging_patterns=("data/vendor_staging/tushare_proxy_core_full_*/raw/by_trade_date/daily/*.csv",),
        capability="daily_research",
        available_threshold=100,
    ),
    DomainSpec(
        key="metadata_core",
        label="Adjustment, limit, suspend, ST/delist metadata",
        canonical_patterns=(
            "data/metadata/adjustment_factors.csv",
            "data/metadata/delist_history.csv",
            # Project schema names (data_metadata_schema.py) used by all active
            # consumers such as data_gap_diagnosis and metadata validators.
            "data/metadata/st_history.csv",
            "data/metadata/suspension_history.csv",
            "data/metadata/limit_price_history.csv",
            # Tushare-native table names kept as an accepted second generation.
            "data/metadata/st_status_history.csv",
            "data/metadata/suspend_d.csv",
            "data/metadata/stk_limit.csv",
        ),
        staging_patterns=(
            "data/vendor_staging/tushare_proxy_core_full_*/raw/by_trade_date/adj_factor/*.csv",
            "data/vendor_staging/tushare_proxy_core_full_*/raw/by_trade_date/stk_limit/*.csv",
            "data/vendor_staging/tushare_proxy_core_full_*/raw/by_trade_date/suspend_d/*.csv",
        ),
        capability="data_truth_checks",
        available_threshold=3,
    ),
    DomainSpec(
        key="intraday_1m",
        label="1-minute bars",
        canonical_patterns=("data/intraday/1m/*.csv", "data/intraday/1m/*.parquet"),
        staging_patterns=(
            "data/vendor_staging/tushare_proxy_minutes_*/1m/*.csv",
            "data/vendor_staging/tushare_proxy_minutes_*/raw/1min/*.csv",
            "data/vendor_staging/tushare_continuous_queue_*/**/1m/*.csv",
            "data/vendor_staging/tushare_continuous_queue_*/**/1min/*.csv",
        ),
        capability="paper_intraday_validation",
        available_threshold=1000,
    ),
    DomainSpec(
        key="sector_index",
        label="Sector/index time series",
        canonical_patterns=("data/sector/index/*.csv", "data/sector_indices/*.csv", "data/index/*.csv"),
        staging_patterns=(
            "data/vendor_staging/tushare_proxy_extended_full_*/**/ths_index.csv",
            "data/vendor_staging/tushare_proxy_extended_full_*/**/ths_daily/*.csv",
            "data/vendor_staging/tushare_proxy_extended_full_*/**/index_daily/*.csv",
            "data/vendor_staging/tushare_continuous_queue_*/**/ths_index.csv",
            "data/vendor_staging/tushare_continuous_queue_*/**/ths_daily/*.csv",
            "data/vendor_staging/tushare_continuous_queue_*/**/index_daily/*.csv",
        ),
        capability="mainline_analysis",
        available_threshold=20,
    ),
    DomainSpec(
        key="sector_membership",
        label="Sector/concept membership",
        canonical_patterns=(
            "data/sector/members/*.csv",
            "data/sector/members/**/*.csv",
            "data/universe/stock_theme_map.csv",
            "data/metadata/sector_membership.csv",
        ),
        staging_patterns=(
            "data/vendor_staging/tushare_proxy_extended_full_*/**/ths_member/*.csv",
            "data/vendor_staging/tushare_proxy_extended_full_*/**/index_weight/*.csv",
            "data/vendor_staging/tushare_continuous_queue_*/**/ths_member/*.csv",
            "data/vendor_staging/tushare_continuous_queue_*/**/index_weight/*.csv",
        ),
        capability="mainline_analysis",
        available_threshold=20,
    ),
    DomainSpec(
        key="concept_theme",
        label="Concept/theme map",
        canonical_patterns=("data/universe/stock_theme_map.csv", "data/theme/*.csv"),
        staging_patterns=(
            "data/vendor_staging/tushare_proxy_extended_full_*/**/ths_index.csv",
            "data/vendor_staging/tushare_proxy_extended_full_*/**/ths_member/*.csv",
            "data/vendor_staging/tushare_continuous_queue_*/**/ths_index.csv",
            "data/vendor_staging/tushare_continuous_queue_*/**/ths_member/*.csv",
        ),
        capability="mainline_analysis",
        available_threshold=20,
    ),
    DomainSpec(
        key="events",
        label="Trading/event evidence",
        canonical_patterns=("data/events/*.csv", "data/events/**/*.csv"),
        staging_patterns=(
            "data/vendor_staging/tushare_proxy_extended_full_*/**/top_list/*.csv",
            "data/vendor_staging/tushare_proxy_extended_full_*/**/top_inst/*.csv",
            "data/vendor_staging/tushare_proxy_extended_full_*/**/margin/*.csv",
            "data/vendor_staging/tushare_proxy_extended_full_*/**/margin_detail/*.csv",
            "data/vendor_staging/tushare_proxy_extended_full_*/**/block_trade/*.csv",
            "data/vendor_staging/tushare_proxy_extended_full_*/**/limit_list_d/*.csv",
            "data/vendor_staging/tushare_continuous_queue_*/**/top_list/*.csv",
            "data/vendor_staging/tushare_continuous_queue_*/**/top_inst/*.csv",
            "data/vendor_staging/tushare_continuous_queue_*/**/margin/*.csv",
            "data/vendor_staging/tushare_continuous_queue_*/**/margin_detail/*.csv",
            "data/vendor_staging/tushare_continuous_queue_*/**/block_trade/*.csv",
            "data/vendor_staging/tushare_continuous_queue_*/**/limit_list_d/*.csv",
        ),
        capability="event_context",
        available_threshold=100,
    ),
    DomainSpec(
        key="fundamentals",
        label="Financial statements and indicators",
        canonical_patterns=("data/fundamentals/**/*.csv", "data/metadata/fundamental_announcement_dates.csv"),
        staging_patterns=(
            "data/vendor_staging/tushare_proxy_extended_full_*/**/income/*.csv",
            "data/vendor_staging/tushare_proxy_extended_full_*/**/balancesheet/*.csv",
            "data/vendor_staging/tushare_proxy_extended_full_*/**/cashflow/*.csv",
            "data/vendor_staging/tushare_proxy_extended_full_*/**/fina_indicator/*.csv",
            "data/vendor_staging/tushare_continuous_queue_*/**/income/*.csv",
            "data/vendor_staging/tushare_continuous_queue_*/**/balancesheet/*.csv",
            "data/vendor_staging/tushare_continuous_queue_*/**/cashflow/*.csv",
            "data/vendor_staging/tushare_continuous_queue_*/**/fina_indicator/*.csv",
        ),
        capability="fundamental_context",
        available_threshold=100,
    ),
    DomainSpec(
        key="moneyflow",
        label="Moneyflow evidence",
        canonical_patterns=("data/moneyflow/**/*.csv", "data/fundflow/**/*.csv"),
        staging_patterns=(
            "data/vendor_staging/tushare_proxy_extended_full_*/**/moneyflow/*.csv",
            "data/vendor_staging/tushare_proxy_extended_full_*/**/moneyflow_hsgt/*.csv",
            "data/vendor_staging/tushare_continuous_queue_*/**/moneyflow/*.csv",
            "data/vendor_staging/tushare_continuous_queue_*/**/moneyflow_hsgt/*.csv",
        ),
        capability="moneyflow_context",
        available_threshold=100,
    ),
)

METADATA_CORE_COMPONENT_PATTERNS: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = {
    "adjustment_factors": (
        ("data/metadata/adjustment_factors.csv",),
        ("data/vendor_staging/tushare_proxy_core_full_*/raw/by_trade_date/adj_factor/*.csv",),
    ),
    "delist_history": (("data/metadata/delist_history.csv",), ()),
    # Each component accepts both the project schema filename (first) and the
    # Tushare-native filename (second); either one counts as canonical.
    "st_status_history": (
        (
            "data/metadata/st_history.csv",
            "data/metadata/st_status_history.csv",
        ),
        (),
    ),
    "suspend_d": (
        (
            "data/metadata/suspension_history.csv",
            "data/metadata/suspend_d.csv",
        ),
        ("data/vendor_staging/tushare_proxy_core_full_*/raw/by_trade_date/suspend_d/*.csv",),
    ),
    "stk_limit": (
        (
            "data/metadata/limit_price_history.csv",
            "data/metadata/stk_limit.csv",
        ),
        ("data/vendor_staging/tushare_proxy_core_full_*/raw/by_trade_date/stk_limit/*.csv",),
    ),
}


def _root(project_root: str | Path | None) -> Path:
    return Path(project_root).resolve() if project_root else PROJECT_ROOT


def _relative(path: Path, root: Path) -> str:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except Exception:
        return path.as_posix()


def _iter_matches(root: Path, patterns: Iterable[str], *, limit: int = 250) -> list[Path]:
    matches: list[Path] = []
    seen: set[Path] = set()
    for pattern in patterns:
        for path in root.glob(pattern):
            if path in seen:
                continue
            seen.add(path)
            matches.append(path)
            if len(matches) >= limit:
                return matches
    return matches


def _last_csv_row(path: Path) -> dict[str, str]:
    try:
        with path.open("rb") as handle:
            header = handle.readline().decode("utf-8-sig", errors="replace").strip()
            if not header:
                return {}
            handle.seek(0, 2)
            size = handle.tell()
            handle.seek(max(0, size - 65536))
            tail = handle.read().decode("utf-8-sig", errors="replace").splitlines()
        data_lines = [line.strip() for line in tail if line.strip()]
        if not data_lines:
            return {}
        last_line = data_lines[-1]
        if last_line == header and len(data_lines) > 1:
            last_line = data_lines[-2]
        reader = csv.DictReader([header, last_line])
        row = next(reader, {})
        return {str(k): str(v) for k, v in row.items() if k is not None}
    except Exception:
        return {}


def _date_from_value(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    text = text.replace("/", "-")
    if len(text) >= 10 and text[4:5] == "-" and text[7:8] == "-":
        return text[:10]
    digits = "".join(ch for ch in text if ch.isdigit())
    if len(digits) >= 8:
        return f"{digits[:4]}-{digits[4:6]}-{digits[6:8]}"
    return ""


def _latest_date(paths: list[Path]) -> str:
    candidates: list[str] = []
    date_fields = ("trade_date", "trade_time", "date", "cal_date", "end_date", "ann_date", "datetime", "time")
    for path in paths[:50]:
        if path.suffix.lower() != ".csv":
            continue
        row = _last_csv_row(path)
        for field in date_fields:
            value = row.get(field)
            parsed = _date_from_value(value)
            if parsed:
                candidates.append(parsed)
                break
        if not row:
            parsed = _date_from_value(path.stem)
            if parsed:
                candidates.append(parsed)
    return max(candidates) if candidates else ""


def _domain_payload(spec: DomainSpec, root: Path) -> dict[str, Any]:
    canonical = _iter_matches(root, spec.canonical_patterns)
    staged = [] if canonical else _iter_matches(root, spec.staging_patterns)
    file_count = len(canonical) if canonical else len(staged)
    if canonical:
        status = "available" if len(canonical) >= spec.available_threshold else "partial"
        source_state = "canonical"
    elif staged:
        status = "staged_not_imported"
        source_state = "staging"
    else:
        status = "missing"
        source_state = "absent"
    sample = canonical[:5] if canonical else staged[:5]
    warnings = _domain_warnings(spec.key, status)
    component_status: dict[str, dict[str, Any]] = {}
    if spec.key == "metadata_core":
        component_status = _metadata_core_component_status(root)
        for component, row in component_status.items():
            if row.get("status") == "missing":
                warnings.append(f"missing_metadata_component:{component}")
            elif row.get("status") == "staged_not_imported":
                warnings.append(f"staged_metadata_component_not_imported:{component}")
    return {
        "domain": spec.key,
        "label": spec.label,
        "status": status,
        "source_state": source_state,
        "capability": spec.capability,
        "file_count": file_count,
        "file_count_note": "sample_cap_reached" if file_count >= 250 else "",
        "canonical_file_count": len(canonical),
        "staged_file_count": len(staged),
        "latest_date": _latest_date(canonical or staged),
        "canonical_patterns": list(spec.canonical_patterns),
        "staging_patterns": list(spec.staging_patterns),
        "sample_paths": [_relative(path, root) for path in sample],
        "component_status": component_status,
        "warnings": warnings,
    }


def _metadata_core_component_status(root: Path) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for component, (canonical_patterns, staging_patterns) in METADATA_CORE_COMPONENT_PATTERNS.items():
        canonical = _iter_matches(root, canonical_patterns)
        staged = _iter_matches(root, staging_patterns) if not canonical and staging_patterns else []
        if canonical:
            status = "available"
            source_state = "canonical"
            sample = canonical[:5]
        elif staged:
            status = "staged_not_imported"
            source_state = "staging"
            sample = staged[:5]
        else:
            status = "missing"
            source_state = "absent"
            sample = []
        out[component] = {
            "status": status,
            "source_state": source_state,
            "canonical_file_count": len(canonical),
            "staged_file_count": len(staged),
            "sample_paths": [_relative(path, root) for path in sample],
        }
    return out


def _domain_warnings(domain: str, status: str) -> list[str]:
    if status == "missing":
        return [f"missing:{domain}"]
    if status == "staged_not_imported":
        return [f"staged_not_imported:{domain}"]
    if status == "partial":
        return [f"partial:{domain}"]
    return []


def _latest_queue(root: Path) -> dict[str, Any]:
    queue_dirs = sorted((root / "data" / "vendor_staging").glob("tushare_continuous_queue_*"))
    if not queue_dirs:
        return {"status": "missing", "path": "", "current_stage": "", "warnings": ["missing:tushare_continuous_queue"]}
    queue_dir = queue_dirs[-1]
    status_path = queue_dir / "queue_status.json"
    raw: dict[str, Any] = {}
    if status_path.exists():
        try:
            loaded = json.loads(status_path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                raw = loaded
        except Exception as exc:
            return {
                "status": "warning",
                "path": _relative(queue_dir, root),
                "current_stage": "",
                "warnings": [f"queue_status_unreadable:{exc.__class__.__name__}"],
            }
    current_stage = str(raw.get("current_stage") or raw.get("stage") or "").strip()
    current_stage_status = str(raw.get("current_stage_status") or "").strip()
    updated_at = str(raw.get("updated_at") or raw.get("last_updated") or "").strip()
    stages = raw.get("stages")
    if not current_stage and isinstance(stages, list):
        for item in reversed(stages):
            if not isinstance(item, dict):
                continue
            stage_key = str(item.get("key") or item.get("stage") or item.get("name") or "").strip()
            if not stage_key:
                continue
            current_stage = stage_key
            current_stage_status = str(item.get("status") or "").strip()
            updated_at = str(item.get("updated_at") or item.get("last_updated") or updated_at).strip()
            break
    return {
        "status": str(raw.get("status") or ("running_or_present" if status_path.exists() else "present")),
        "path": _relative(queue_dir, root),
        "current_stage": current_stage,
        "current_stage_status": current_stage_status,
        "updated_at": updated_at,
        "warnings": [],
    }


def _capabilities(domains: dict[str, dict[str, Any]]) -> dict[str, str]:
    def status(name: str) -> str:
        return str((domains.get(name) or {}).get("status") or "missing")

    mainline_inputs = [status("sector_index"), status("sector_membership"), status("concept_theme"), status("moneyflow")]
    if all(item in {"available", "partial"} for item in mainline_inputs[:3]):
        mainline = "partial" if "missing" in mainline_inputs or "staged_not_imported" in mainline_inputs else "available"
    elif any(item in {"available", "partial", "staged_not_imported"} for item in mainline_inputs):
        mainline = "partial"
    else:
        mainline = "degraded"

    intraday_status = status("intraday_1m")
    return {
        "daily_research": "available" if status("daily_core") in {"available", "partial"} else "degraded",
        "data_truth_checks": "available" if status("metadata_core") in {"available", "partial"} else "degraded",
        "paper_intraday_validation": "partial" if intraday_status in {"available", "partial", "staged_not_imported"} else "degraded",
        "mainline_analysis": mainline,
        "fundamental_context": "partial" if status("fundamentals") in {"available", "partial", "staged_not_imported"} else "degraded",
        "moneyflow_context": "partial" if status("moneyflow") in {"available", "partial", "staged_not_imported"} else "degraded",
        "event_context": "partial" if status("events") in {"available", "partial", "staged_not_imported"} else "degraded",
    }


def _overall_status(domains: dict[str, dict[str, Any]]) -> str:
    statuses = [str(item.get("status") or "missing") for item in domains.values()]
    if not statuses or all(status == "missing" for status in statuses):
        return "missing"
    if any(status in {"missing", "staged_not_imported", "partial"} for status in statuses):
        return "partial"
    return "passed"


def _queue_is_running(queue: dict[str, Any]) -> bool:
    status = str(queue.get("status") or "").strip().lower()
    if status in {"running", "running_or_present"}:
        return True
    if str(queue.get("current_stage") or "").strip() and status not in {"completed", "complete", "done", "finished", "success"}:
        return True
    return False


def _suggested_next_action(domains: dict[str, dict[str, Any]], queue: dict[str, Any]) -> str:
    if _queue_is_running(queue):
        return "wait_for_queue_then_rerun_audit"
    if any(str(item.get("status") or "") == "staged_not_imported" for item in domains.values()):
        return FULL_AUDIT_COMMAND
    if any(str(item.get("status") or "") in {"missing", "partial"} for item in domains.values()):
        return "Review reports/extended_data_readiness_latest.md and fill the missing local data domains."
    return "Extended data readiness is available; continue with data-health, market-intel, and research cards."


def build_extended_data_readiness(project_root: str | Path | None = None) -> dict[str, Any]:
    """Build a read-only extended-data readiness payload.

    The result is intentionally conservative: staged vendor files are not treated
    as canonical evidence until a dedicated importer materializes them into the
    maintained data layout.
    """

    root = _root(project_root)
    domains = {spec.key: _domain_payload(spec, root) for spec in DOMAIN_SPECS}
    warnings: list[str] = []
    for domain in domains.values():
        warnings.extend(str(item) for item in domain.get("warnings", []) if item)
    queue = _latest_queue(root)
    warnings.extend(str(item) for item in queue.get("warnings", []) if item)
    if _queue_is_running(queue):
        warnings.append("download_queue_still_running")
    return {
        "task_type": "extended_data_readiness",
        "schema_version": "1.0",
        "status": _overall_status(domains),
        "domains": domains,
        "capabilities": _capabilities(domains),
        "download_queue": queue,
        "warnings": warnings,
        "suggested_next_action": _suggested_next_action(domains, queue),
        "research_only": True,
        "paper_only": True,
        "no_broker_route": True,
        "note": "Read-only local/staging data readiness; staged data is not canonical until imported.",
    }


def _markdown_lines(payload: dict[str, Any]) -> list[str]:
    domains = payload.get("domains") if isinstance(payload.get("domains"), dict) else {}
    capabilities = payload.get("capabilities") if isinstance(payload.get("capabilities"), dict) else {}
    queue = payload.get("download_queue") if isinstance(payload.get("download_queue"), dict) else {}
    lines = [
        "# Extended Data Readiness",
        "",
        "Research Only / Paper Account Only / No Broker Route / Not Investment Advice / Not Position Advice",
        "",
        "This report is a read-only inventory of canonical and staged local data. It never imports vendor files, fetches network data, or changes strategy behavior.",
        "",
        "## Summary",
        "",
        f"- status: {payload.get('status') or 'unknown'}",
        f"- download_queue: {queue.get('status') or '-'}",
        f"- queue_stage: {queue.get('current_stage') or '-'}",
        f"- mainline_analysis: {capabilities.get('mainline_analysis') or 'degraded'}",
        f"- paper_intraday_validation: {capabilities.get('paper_intraday_validation') or 'degraded'}",
        f"- fundamental_context: {capabilities.get('fundamental_context') or 'degraded'}",
        f"- moneyflow_context: {capabilities.get('moneyflow_context') or 'degraded'}",
        "",
        "## Domain Status",
        "",
        "| domain | status | source_state | files | latest_date | note |",
        "| --- | --- | --- | ---: | --- | --- |",
    ]
    for key, item in domains.items():
        row = item if isinstance(item, dict) else {}
        note_parts = [str(value) for value in (row.get("warnings") or []) if value]
        if row.get("file_count_note"):
            note_parts.append(str(row.get("file_count_note")))
        lines.append(
            f"| {key} | {row.get('status') or 'missing'} | {row.get('source_state') or '-'} | "
            f"{row.get('file_count') or 0} | {row.get('latest_date') or '-'} | {'; '.join(note_parts) or '-'} |"
        )
    lines.extend(
        [
            "",
            "## Status Semantics",
            "",
            "- available: canonical project data exists and is enough for the domain gate.",
            "- partial: canonical project data exists but coverage is incomplete or sampled.",
            "- staged_not_imported: vendor/staging files exist, but they are not canonical project data yet.",
            "- missing: no canonical or staged evidence was found.",
            "",
            "## Warnings",
            "",
        ]
    )
    warnings = [str(item) for item in (payload.get("warnings") or []) if item]
    lines.extend([f"- {warning}" for warning in warnings] or ["- none"])
    return lines


def write_extended_data_readiness_outputs(
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
    payload = build_extended_data_readiness(project_root=root)
    try:
        payload["output_path"] = output.resolve().relative_to(root.resolve()).as_posix()
    except Exception:
        payload["output_path"] = output.as_posix()
    try:
        payload["report_path"] = report.resolve().relative_to(root.resolve()).as_posix()
    except Exception:
        payload["report_path"] = report.as_posix()
    write_json(output, payload)
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text("\n".join(_markdown_lines(payload)).rstrip() + "\n", encoding="utf-8")
    return payload
