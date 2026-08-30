"""Read-only coverage audit for local Tushare 1-minute download targets.

The status monitor tells operators whether a minute download job is moving.
This module answers the next practical question: for the target pool that the
project actually wants to use, how many symbols are already available in
canonical ``data/intraday/1m`` data, how many are staged by the current Tushare
job, and how many are still missing/stale.

It is intentionally read-only.  It never fetches network data, never reads
provider tokens, never restarts jobs, never imports staged files, and never
writes canonical market data.
"""

from __future__ import annotations

import csv
import json
import re
from pathlib import Path
from typing import Any

from quantdesk.shared import read_json, write_json, write_text


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT_PATH = PROJECT_ROOT / "results" / "data_health" / "tushare_minute_coverage_latest.json"
DEFAULT_REPORT_PATH = PROJECT_ROOT / "reports" / "tushare_minute_coverage_latest.md"

TARGET_POOL_GLOB = ".tmp/minute_target_pool_project_real_*.json"
MINUTE_JOB_GLOB = "data/vendor_staging/tushare_proxy_minutes_250d*"
TOKEN_LIKE_RE = re.compile(r"\b[a-fA-F0-9]{32,}\b")
DATE_RE = re.compile(r"(20\d{2})[-/]?(\d{2})[-/]?(\d{2})")


def _root(project_root: str | Path | None) -> Path:
    return Path(project_root).resolve() if project_root else PROJECT_ROOT


def _relative(path: Path, root: Path) -> str:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except Exception:
        return path.as_posix()


def _normalize_symbol(value: Any) -> str:
    text = str(value or "").strip().upper()
    if not text:
        return ""
    if "." in text:
        text = text.split(".", 1)[0]
    digits = "".join(ch for ch in text if ch.isdigit())
    if not digits:
        return ""
    return digits.zfill(6)[-6:]


def _normalize_date(value: Any) -> str:
    match = DATE_RE.search(str(value or ""))
    if not match:
        return ""
    return "".join(match.groups())


def _latest_path(root: Path, pattern: str) -> Path | None:
    paths = [path for path in root.glob(pattern) if path.exists()]
    if not paths:
        return None
    return sorted(paths, key=lambda item: (item.stat().st_mtime, item.name), reverse=True)[0]


def _minute_job_paths(root: Path) -> list[Path]:
    return sorted(
        [path for path in root.glob(MINUTE_JOB_GLOB) if path.is_dir()],
        key=lambda item: (item.stat().st_mtime, item.name),
        reverse=True,
    )


def _latest_target_pool_path(root: Path) -> Path | None:
    paths = [
        path
        for path in root.glob(TARGET_POOL_GLOB)
        if path.exists() and not path.name.endswith("_summary.json")
    ]
    if not paths:
        return None
    return sorted(paths, key=lambda item: (item.stat().st_mtime, item.name), reverse=True)[0]


def _load_target_pool(root: Path, target_pool_path: str | Path | None, job_summary: dict[str, Any]) -> tuple[list[dict[str, Any]], Path | None, list[str]]:
    warnings: list[str] = []
    path = Path(target_pool_path).resolve() if target_pool_path else _latest_target_pool_path(root)
    raw: Any = None
    if path and path.exists():
        raw = read_json(path, default=None)
    elif target_pool_path:
        warnings.append("missing:target_pool_path")

    rows: list[dict[str, Any]] = []
    if isinstance(raw, list):
        for item in raw:
            if isinstance(item, dict):
                symbol = _normalize_symbol(item.get("symbol") or item.get("ts_code") or item.get("code"))
                if not symbol:
                    continue
                sources = item.get("sources") if isinstance(item.get("sources"), list) else []
                rows.append(
                    {
                        "symbol": symbol,
                        "sources": [str(source) for source in sources if str(source).strip()],
                        "reason": str(item.get("reason") or ""),
                    }
                )
            else:
                symbol = _normalize_symbol(item)
                if symbol:
                    rows.append({"symbol": symbol, "sources": [], "reason": ""})
    elif isinstance(raw, dict):
        items = raw.get("symbols") or raw.get("target_symbols") or raw.get("targets")
        if isinstance(items, list):
            for item in items:
                if isinstance(item, dict):
                    symbol = _normalize_symbol(item.get("symbol") or item.get("ts_code") or item.get("code"))
                    sources = item.get("sources") if isinstance(item.get("sources"), list) else []
                    reason = str(item.get("reason") or "")
                else:
                    symbol = _normalize_symbol(item)
                    sources = []
                    reason = ""
                if symbol:
                    rows.append({"symbol": symbol, "sources": [str(source) for source in sources], "reason": reason})

    if not rows:
        summary_symbols = job_summary.get("symbols") if isinstance(job_summary.get("symbols"), list) else []
        for item in summary_symbols:
            symbol = _normalize_symbol(item)
            if symbol:
                rows.append({"symbol": symbol, "sources": ["download_summary"], "reason": ""})
        if rows:
            warnings.append("target_pool_missing_used_download_summary_symbols")

    deduped: dict[str, dict[str, Any]] = {}
    for row in rows:
        symbol = str(row.get("symbol") or "")
        if not symbol:
            continue
        current = deduped.setdefault(symbol, {"symbol": symbol, "sources": [], "reason": ""})
        for source in row.get("sources") or []:
            source_text = str(source).strip()
            if source_text and source_text not in current["sources"]:
                current["sources"].append(source_text)
        if row.get("reason") and not current.get("reason"):
            current["reason"] = row.get("reason")
    return list(deduped.values()), path if path and path.exists() else None, warnings


def _read_first_lines(path: Path) -> tuple[list[str], str]:
    try:
        with path.open("r", encoding="utf-8-sig", errors="replace", newline="") as handle:
            header_line = handle.readline()
            first_data_line = handle.readline()
    except Exception:
        return [], ""
    try:
        headers = next(csv.reader([header_line]))
    except Exception:
        headers = []
    return headers, first_data_line.strip()


def _read_last_line(path: Path, *, chunk_size: int = 8192) -> str:
    try:
        size = path.stat().st_size
        if size <= 0:
            return ""
        with path.open("rb") as handle:
            position = size
            buffer = b""
            while position > 0:
                read_size = min(chunk_size, position)
                position -= read_size
                handle.seek(position)
                buffer = handle.read(read_size) + buffer
                lines = [line for line in buffer.splitlines() if line.strip()]
                if len(lines) >= 2 or position == 0:
                    return lines[-1].decode("utf-8-sig", errors="replace") if lines else ""
    except Exception:
        return ""
    return ""


def _date_from_csv_line(headers: list[str], line: str) -> str:
    if not line:
        return ""
    try:
        values = next(csv.reader([line]))
    except Exception:
        return _normalize_date(line)
    row = {str(header).strip().lower(): values[index] for index, header in enumerate(headers) if index < len(values)}
    for key in ("date", "trade_date", "cal_date", "datetime", "trade_time", "time"):
        date_value = _normalize_date(row.get(key))
        if date_value:
            return date_value
    return _normalize_date(line)


def _count_data_rows(path: Path) -> int | None:
    try:
        with path.open("rb") as handle:
            line_count = sum(1 for _line in handle)
        return max(0, line_count - 1)
    except Exception:
        return None


def _csv_quick_stats(path: Path, *, root: Path, count_rows: bool = False) -> dict[str, Any]:
    if not path.exists():
        return {
            "exists": False,
            "path": _relative(path, root),
            "size_bytes": 0,
            "first_date": "",
            "last_date": "",
            "row_count": None,
        }
    size_bytes = path.stat().st_size
    headers, first_data_line = _read_first_lines(path)
    last_line = _read_last_line(path)
    return {
        "exists": True,
        "path": _relative(path, root),
        "size_bytes": size_bytes,
        "first_date": _date_from_csv_line(headers, first_data_line),
        "last_date": _date_from_csv_line(headers, last_line),
        "row_count": _count_data_rows(path) if count_rows else None,
    }


def _summary_download_map(summary: dict[str, Any]) -> dict[str, dict[str, Any]]:
    rows = summary.get("download") if isinstance(summary.get("download"), list) else []
    out: dict[str, dict[str, Any]] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        symbol = _normalize_symbol(row.get("symbol") or row.get("ts_code"))
        if not symbol:
            continue
        safe = {
            "status": str(row.get("status") or ""),
            "rows": row.get("rows"),
            "chunks": row.get("chunks"),
            "errors_count": len(row.get("errors")) if isinstance(row.get("errors"), list) else 0,
        }
        out[symbol] = safe
    return out


def _summary_symbols(summary: dict[str, Any]) -> list[str]:
    symbols = sorted(_summary_download_map(summary))
    if symbols:
        return symbols
    raw_symbols = summary.get("symbols") if isinstance(summary.get("symbols"), list) else []
    return sorted({symbol for symbol in (_normalize_symbol(item) for item in raw_symbols) if symbol})


def _download_status_counts(summary: dict[str, Any]) -> dict[str, int]:
    rows = summary.get("download") if isinstance(summary.get("download"), list) else []
    counts: dict[str, int] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        status = str(row.get("status") or "unknown").strip().lower() or "unknown"
        counts[status] = counts.get(status, 0) + 1
    return counts


def _empty_completed_job() -> dict[str, Any]:
    return {
        "path": "",
        "summary_status": "",
        "symbol_count": 0,
        "target_end": "",
        "days": 0,
        "download_records": 0,
        "download_status_counts": {},
        "canonical_current_count": 0,
        "missing_or_stale_count": 0,
        "current_available_ratio": 0.0,
    }


def _latest_completed_job_summary(root: Path, canonical_base: Path) -> dict[str, Any]:
    for job in _minute_job_paths(root):
        summary_path = job / "summary.json"
        summary = read_json(summary_path, default={})
        if not isinstance(summary, dict):
            continue
        summary_status = str(summary.get("status") or "").strip().lower()
        if summary_status not in {"completed", "complete", "done", "finished", "success"}:
            continue
        symbols = _summary_symbols(summary)
        target_end = _normalize_date(summary.get("target_end"))
        canonical_current = 0
        for symbol in symbols:
            stats = _csv_quick_stats(canonical_base / f"{symbol}.csv", root=root, count_rows=False)
            if _is_current(stats, target_end):
                canonical_current += 1
        symbol_count = len(symbols) or int(summary.get("symbol_count") or 0)
        missing_or_stale = max(0, symbol_count - canonical_current)
        rows = summary.get("download") if isinstance(summary.get("download"), list) else []
        return {
            "path": _relative(job, root),
            "summary_status": summary.get("status") or "",
            "symbol_count": symbol_count,
            "target_end": target_end,
            "days": summary.get("days") or 0,
            "download_records": len(rows),
            "download_status_counts": _download_status_counts(summary),
            "canonical_current_count": canonical_current,
            "missing_or_stale_count": missing_or_stale,
            "current_available_ratio": round(canonical_current / symbol_count, 6) if symbol_count else 0.0,
        }
    return _empty_completed_job()


def _is_current(stats: dict[str, Any], target_end: str) -> bool:
    if not stats.get("exists") or int(stats.get("size_bytes") or 0) <= 0:
        return False
    last_date = str(stats.get("last_date") or "")
    if not target_end:
        return bool(last_date)
    return bool(last_date) and last_date >= target_end


def _classify_symbol(
    *,
    canonical_stats: dict[str, Any],
    staged_stats: dict[str, Any],
    download_row: dict[str, Any],
    target_end: str,
) -> str:
    staged_exists = bool(staged_stats.get("exists"))
    canonical_exists = bool(canonical_stats.get("exists"))
    staged_size = int(staged_stats.get("size_bytes") or 0)
    canonical_size = int(canonical_stats.get("size_bytes") or 0)
    download_status = str(download_row.get("status") or "").lower()

    if _is_current(canonical_stats, target_end):
        return "canonical_current"
    if _is_current(staged_stats, target_end) and download_status in {"", "ok"}:
        return "staged_current_not_imported"
    if staged_exists and staged_size <= 0:
        return "staged_empty"
    if staged_exists or download_status in {"partial", "failed", "error"}:
        return "staged_partial_or_stale"
    if canonical_exists and canonical_size <= 0:
        return "canonical_empty"
    if canonical_exists:
        return "canonical_stale"
    return "missing"


def build_tushare_minute_coverage(
    *,
    project_root: str | Path | None = None,
    target_pool_path: str | Path | None = None,
    job_dir: str | Path | None = None,
    canonical_dir: str | Path | None = None,
    count_rows: bool = False,
    max_sample: int = 30,
) -> dict[str, Any]:
    root = _root(project_root)
    selected_job = Path(job_dir).resolve() if job_dir else _latest_path(root, MINUTE_JOB_GLOB)
    summary_path = selected_job / "summary.json" if selected_job else None
    summary = read_json(summary_path, default={}) if summary_path else {}
    if not isinstance(summary, dict):
        summary = {}
    target_end = _normalize_date(summary.get("target_end"))
    targets, pool_path, warnings = _load_target_pool(root, target_pool_path, summary)
    canonical_base = Path(canonical_dir).resolve() if canonical_dir else root / "data" / "intraday" / "1m"
    staged_base = selected_job / "raw" / "1min" if selected_job else root / "data" / "vendor_staging" / "missing_minute_job" / "raw" / "1min"
    download_by_symbol = _summary_download_map(summary)
    canonical_file_count = len(list(canonical_base.glob("*.csv"))) if canonical_base.exists() else 0
    latest_completed_job = _latest_completed_job_summary(root, canonical_base)

    counts = {
        "canonical_current": 0,
        "staged_current_not_imported": 0,
        "staged_partial_or_stale": 0,
        "staged_empty": 0,
        "canonical_stale": 0,
        "canonical_empty": 0,
        "missing": 0,
    }
    source_counts: dict[str, dict[str, int]] = {}
    missing_or_stale_samples: list[dict[str, Any]] = []
    staged_samples: list[dict[str, Any]] = []
    canonical_samples: list[dict[str, Any]] = []

    for target in sorted(targets, key=lambda item: str(item.get("symbol") or "")):
        symbol = str(target.get("symbol") or "")
        sources = [str(source) for source in target.get("sources") or []] or ["unspecified"]
        canonical_stats = _csv_quick_stats(canonical_base / f"{symbol}.csv", root=root, count_rows=count_rows)
        staged_stats = _csv_quick_stats(staged_base / f"{symbol}.csv", root=root, count_rows=count_rows)
        download_row = download_by_symbol.get(symbol, {})
        status = _classify_symbol(
            canonical_stats=canonical_stats,
            staged_stats=staged_stats,
            download_row=download_row,
            target_end=target_end,
        )
        counts[status] += 1
        is_available_current = status in {"canonical_current", "staged_current_not_imported"}
        for source in sources:
            bucket = source_counts.setdefault(source, {"target": 0, "available_current": 0, "missing_or_stale": 0})
            bucket["target"] += 1
            if is_available_current:
                bucket["available_current"] += 1
            else:
                bucket["missing_or_stale"] += 1

        sample_row = {
            "symbol": symbol,
            "sources": sources,
            "status": status,
            "canonical_last_date": canonical_stats.get("last_date") or "",
            "staged_last_date": staged_stats.get("last_date") or "",
            "download_status": download_row.get("status") or "",
            "download_rows": download_row.get("rows"),
        }
        if status == "canonical_current" and len(canonical_samples) < max_sample:
            canonical_samples.append(sample_row)
        elif status == "staged_current_not_imported" and len(staged_samples) < max_sample:
            staged_samples.append(sample_row)
        elif status not in {"canonical_current", "staged_current_not_imported"} and len(missing_or_stale_samples) < max_sample:
            missing_or_stale_samples.append(sample_row)

    target_count = len(targets)
    canonical_current_available = counts["canonical_current"]
    staged_current_not_imported = counts["staged_current_not_imported"]
    current_available = canonical_current_available + staged_current_not_imported
    missing_or_stale = target_count - current_available
    if target_count == 0:
        status = "missing_target_pool"
        suggested = "provide_target_pool_or_wait_for_minute_job_summary"
    elif missing_or_stale == 0:
        status = "target_pool_current_in_canonical_or_staging"
        suggested = "audit_before_user_confirmed_import"
    elif selected_job and str(summary.get("status") or "").lower() == "running":
        status = "download_in_progress"
        suggested = "continue_monitoring_minute_download"
    else:
        status = "target_pool_incomplete"
        suggested = "review_missing_or_stale_before_retry"

    payload: dict[str, Any] = {
        "task_type": "tushare_minute_coverage",
        "status": status,
        "target_pool": {
            "path": _relative(pool_path, root) if pool_path else "",
            "symbol_count": target_count,
        },
        "minute_job": {
            "path": _relative(selected_job, root) if selected_job else "",
            "summary_status": summary.get("status") or "",
            "summary_symbol_count": summary.get("symbol_count") or 0,
            "target_end": target_end,
            "days": summary.get("days") or 0,
            "download_records": len(summary.get("download")) if isinstance(summary.get("download"), list) else 0,
        },
        "canonical_dir": _relative(canonical_base, root),
        "staging_dir": _relative(staged_base, root),
        "canonical_file_count": canonical_file_count,
        "latest_completed_job": latest_completed_job,
        "coverage_counts": counts,
        "current_available_count": current_available,
        "current_available_count_source": "canonical_plus_staged_pending_import",
        "canonical_current_available_count": canonical_current_available,
        "staged_current_not_imported_count": staged_current_not_imported,
        "missing_or_stale_count": missing_or_stale,
        "current_available_ratio": round(current_available / target_count, 6) if target_count else 0.0,
        "canonical_current_available_ratio": round(canonical_current_available / target_count, 6) if target_count else 0.0,
        "staged_current_not_imported_ratio": round(staged_current_not_imported / target_count, 6) if target_count else 0.0,
        "source_counts": source_counts,
        "samples": {
            "missing_or_stale": missing_or_stale_samples,
            "staged_current_not_imported": staged_samples,
            "canonical_current": canonical_samples,
        },
        "warnings": warnings,
        "suggested_next_action": suggested,
        "read_only": True,
        "network_used": False,
        "token_output": False,
        "canonical_write": False,
        "count_rows": bool(count_rows),
    }
    if TOKEN_LIKE_RE.search(json.dumps(payload, ensure_ascii=False)):
        payload["warnings"].append("token_like_text_present_unexpected")
    return payload


def render_tushare_minute_coverage_report(payload: dict[str, Any]) -> str:
    target = payload.get("target_pool") if isinstance(payload.get("target_pool"), dict) else {}
    job = payload.get("minute_job") if isinstance(payload.get("minute_job"), dict) else {}
    completed = payload.get("latest_completed_job") if isinstance(payload.get("latest_completed_job"), dict) else {}
    counts = payload.get("coverage_counts") if isinstance(payload.get("coverage_counts"), dict) else {}
    source_counts = payload.get("source_counts") if isinstance(payload.get("source_counts"), dict) else {}
    sample_missing = payload.get("samples", {}).get("missing_or_stale", []) if isinstance(payload.get("samples"), dict) else []

    lines = [
        "# Tushare Minute Coverage",
        "",
        f"- status: `{payload.get('status')}`",
        f"- target_pool: `{target.get('path') or '-'}`",
        f"- target_symbols: `{target.get('symbol_count') or 0}`",
        f"- minute_job: `{job.get('path') or '-'}`",
        f"- job_summary_status: `{job.get('summary_status') or '-'}`",
        f"- target_end: `{job.get('target_end') or '-'}`",
        f"- current_available: `{payload.get('current_available_count', 0)}`",
        f"- current_available_source: `{payload.get('current_available_count_source') or '-'}`",
        f"- canonical_current_available: `{payload.get('canonical_current_available_count', 0)}`",
        f"- staged_current_not_imported: `{payload.get('staged_current_not_imported_count', 0)}`",
        f"- missing_or_stale: `{payload.get('missing_or_stale_count', 0)}`",
        f"- available_ratio: `{payload.get('current_available_ratio', 0.0)}`",
        f"- canonical_available_ratio: `{payload.get('canonical_current_available_ratio', 0.0)}`",
        f"- staged_available_ratio: `{payload.get('staged_current_not_imported_ratio', 0.0)}`",
        f"- canonical_file_count: `{payload.get('canonical_file_count', 0)}`",
        f"- suggested_next_action: `{payload.get('suggested_next_action')}`",
        "",
        "## Latest completed minute job",
        "",
        f"- job: `{completed.get('path') or '-'}`",
        f"- status: `{completed.get('summary_status') or '-'}`",
        f"- target_end: `{completed.get('target_end') or '-'}`",
        f"- canonical_current: `{completed.get('canonical_current_count', 0)}/{completed.get('symbol_count', 0)}`",
        f"- missing_or_stale: `{completed.get('missing_or_stale_count', 0)}`",
        f"- available_ratio: `{completed.get('current_available_ratio', 0.0)}`",
        "",
        "## Coverage counts",
        "",
        "| bucket | count |",
        "|---|---:|",
    ]
    for key in sorted(counts):
        lines.append(f"| {key} | {counts.get(key, 0)} |")
    lines.extend(["", "## Source counts", "", "| source | target | available_current | missing_or_stale |", "|---|---:|---:|---:|"])
    for source, row in sorted(source_counts.items()):
        lines.append(
            f"| {source} | {row.get('target', 0)} | {row.get('available_current', 0)} | {row.get('missing_or_stale', 0)} |"
        )
    lines.extend(["", "## Missing/stale sample", "", "| symbol | status | sources | canonical_last | staged_last | download_status |", "|---|---|---|---|---|---|"])
    for row in sample_missing[:20]:
        lines.append(
            f"| {row.get('symbol')} | {row.get('status')} | {','.join(row.get('sources') or [])} | "
            f"{row.get('canonical_last_date') or '-'} | {row.get('staged_last_date') or '-'} | {row.get('download_status') or '-'} |"
        )
    lines.extend(["", "Read-only / no token output / no network fetch / no canonical data write / no broker."])
    return "\n".join(lines)


def write_tushare_minute_coverage_outputs(
    *,
    project_root: str | Path | None = None,
    target_pool_path: str | Path | None = None,
    job_dir: str | Path | None = None,
    canonical_dir: str | Path | None = None,
    output_path: str | Path = DEFAULT_OUTPUT_PATH,
    report_path: str | Path = DEFAULT_REPORT_PATH,
    count_rows: bool = False,
    max_sample: int = 30,
) -> dict[str, Any]:
    payload = build_tushare_minute_coverage(
        project_root=project_root,
        target_pool_path=target_pool_path,
        job_dir=job_dir,
        canonical_dir=canonical_dir,
        count_rows=count_rows,
        max_sample=max_sample,
    )
    payload["output_path"] = str(output_path)
    payload["report_path"] = str(report_path)
    write_json(output_path, payload)
    write_text(report_path, render_tushare_minute_coverage_report(payload))
    return payload
