"""Minute-data capability labels for research-only intraday consumers.

The module is intentionally read-only.  It does not download, import, restart
jobs, or write canonical data.  Its job is to let execution/replay layers
distinguish between:

* local minute bars that belong to a completed and quality-audited Tushare pool;
* local minute bars that exist but are not covered by that completed-pool gate;
* missing/stale minute evidence.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from quantdesk.shared import normalize_symbol, read_json
from quantdesk.tushare_minute_quality import (
    build_verified_minute_pool_snapshot,
)
from quantdesk.version import API_SCHEMA_VERSION


PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _root(project_root: str | Path | None) -> Path:
    return Path(project_root).resolve() if project_root else PROJECT_ROOT


def _project_root_from_intraday_dir(intraday_dir: Path | None) -> Path:
    """Infer project root from ``<root>/data/intraday`` when possible."""

    if intraday_dir is None:
        return PROJECT_ROOT
    path = Path(intraday_dir).resolve()
    if path.name == "intraday" and path.parent.name == "data":
        return path.parent.parent
    return PROJECT_ROOT


def _relative_or_join(root: Path, value: Any) -> Path:
    path = Path(str(value or ""))
    return path if path.is_absolute() else root / path


def _summary_symbols(summary: dict[str, Any]) -> set[str]:
    symbols: set[str] = set()
    rows = summary.get("download") if isinstance(summary.get("download"), list) else []
    for row in rows:
        if isinstance(row, dict):
            symbol = normalize_symbol(row.get("symbol") or row.get("ts_code") or row.get("code"))
            if symbol:
                symbols.add(symbol)
    raw_symbols = summary.get("symbols") if isinstance(summary.get("symbols"), list) else []
    for item in raw_symbols:
        symbol = normalize_symbol(item)
        if symbol:
            symbols.add(symbol)
    return symbols


def minute_pool_capability_source_hash(payload: dict[str, Any]) -> str:
    """Bind all public capability fields while normalising the symbol set."""

    canonical = dict(payload)
    canonical.pop("source_hash", None)
    symbols = canonical.get("symbols")
    if isinstance(symbols, (set, list, tuple)):
        canonical["symbols"] = sorted(str(item) for item in symbols)
    encoded = json.dumps(
        canonical,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return f"sha256:{hashlib.sha256(encoded.encode('utf-8')).hexdigest()}"


def load_completed_minute_pool_capability(
    *,
    project_root: str | Path | None = None,
    snapshot_path: str | Path | None = None,
) -> dict[str, Any]:
    """Return the latest completed minute-pool quality gate and symbol set."""

    root = _root(project_root)
    verified_pool = build_verified_minute_pool_snapshot(project_root=root, snapshot_path=snapshot_path)
    job_path = str(verified_pool.get("job_path") or "")
    job_summary_path = _relative_or_join(root, job_path) / "summary.json" if job_path else root / "__missing_minute_job__"
    job_summary = read_json(job_summary_path, default={})
    if not isinstance(job_summary, dict):
        job_summary = {}
    symbols = set(verified_pool.get("symbols") or [])
    if verified_pool.get("status") == "passed" and not symbols:
        symbols = _summary_symbols(job_summary)
    status = str(verified_pool.get("status") or "missing")
    usable_count = int(verified_pool.get("usable_count") or 0)
    total_symbols = int(verified_pool.get("total_symbols") or len(symbols) or 0)
    is_passed = status == "passed" and usable_count > 0 and len(symbols) == usable_count
    payload = {
        "schema_version": API_SCHEMA_VERSION,
        "task_type": "verified_minute_pool_snapshot",
        "status": status,
        "passed": bool(is_passed),
        "quality_status": verified_pool.get("quality_status") or status,
        "snapshot_available": bool(verified_pool.get("snapshot_available")),
        "job_path": job_path,
        "snapshot_path": verified_pool.get("snapshot_path") or "",
        "source_snapshot": verified_pool.get("source_snapshot") or "latest",
        "latest_quality_status": verified_pool.get("latest_quality_status") or "",
        "target_date": verified_pool.get("target_date") or "",
        "total_symbols": total_symbols,
        "usable_count": usable_count,
        "partial_count": int(verified_pool.get("partial_count") or 0),
        "insufficient_count": int(verified_pool.get("insufficient_count") or 0),
        "invalid_count": int(verified_pool.get("invalid_count") or 0),
        "missing_count": int(verified_pool.get("missing_count") or 0),
        "warnings": [str(item) for item in (verified_pool.get("warnings") or []) if item],
        "symbols": symbols if is_passed else set(),
        "symbol_count": len(symbols),
        "read_only": True,
        "network_used": False,
        "canonical_write": False,
    }
    payload["source_hash"] = minute_pool_capability_source_hash(payload)
    return payload


def classify_minute_replay_evidence(
    symbol: str,
    *,
    fill_model: str,
    pool: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Classify the minute evidence used by one replay result."""

    normalized = normalize_symbol(symbol)
    model = str(fill_model or "")
    symbols = pool.get("symbols") if isinstance(pool, dict) else set()
    symbol_set = symbols if isinstance(symbols, set) else set(symbols or [])
    pool_passed = bool(pool.get("passed")) if isinstance(pool, dict) else False
    if model == "missing_intraday":
        status = "missing_intraday"
        can_use = False
    elif model == "intraday_1m_close" and pool_passed and normalized in symbol_set:
        status = "verified_completed_pool"
        can_use = True
    elif model == "intraday_1m_close":
        status = "local_available_unverified_pool"
        can_use = True
    else:
        status = "not_intraday_fill_model"
        can_use = False
    return {
        "status": status,
        "symbol": normalized,
        "fill_model": model,
        "can_use_for_research_replay": bool(can_use),
        "verified_completed_pool": status == "verified_completed_pool",
        "pool_status": pool.get("status") if isinstance(pool, dict) else "missing",
        "pool_job_path": pool.get("job_path") if isinstance(pool, dict) else "",
        "quality_target_date": pool.get("target_date") if isinstance(pool, dict) else "",
        # Preserve pool provenance so a verified hit that actually came from the
        # last-passed snapshot (while the latest larger-pool quality is only
        # partial) is not silently presented as a fresh full-pool verification.
        "pool_source_snapshot": pool.get("source_snapshot") if isinstance(pool, dict) else "",
        "pool_latest_quality_status": pool.get("latest_quality_status") if isinstance(pool, dict) else "",
        "pool_warnings": [str(item) for item in (pool.get("warnings") or [])] if isinstance(pool, dict) else [],
        "research_only": True,
        "paper_only": True,
    }


def summarize_minute_replay_capability(items: list[dict[str, Any]], pool: dict[str, Any] | None = None) -> dict[str, Any]:
    """Summarize per-result minute evidence labels for dashboards/reports."""

    counts: dict[str, int] = {}
    for item in items:
        status = str(item.get("status") or "unknown")
        counts[status] = counts.get(status, 0) + 1
    return {
        "pool_status": pool.get("status") if isinstance(pool, dict) else "missing",
        "pool_passed": bool(pool.get("passed")) if isinstance(pool, dict) else False,
        "pool_job_path": pool.get("job_path") if isinstance(pool, dict) else "",
        "quality_target_date": pool.get("target_date") if isinstance(pool, dict) else "",
        # Provenance: keep whether the verified pool came from the latest snapshot
        # or a last-passed fallback, and the latest quality status, so downstream
        # dashboards/reports can flag a Top200 pool reused while the larger pool is
        # still only partial. Loaded by the pool loader; must not be dropped here.
        "pool_source_snapshot": pool.get("source_snapshot") if isinstance(pool, dict) else "",
        "pool_latest_quality_status": pool.get("latest_quality_status") if isinstance(pool, dict) else "",
        "warnings": [str(item) for item in (pool.get("warnings") or [])] if isinstance(pool, dict) else [],
        "verified_completed_intraday_count": int(counts.get("verified_completed_pool") or 0),
        "local_unverified_intraday_count": int(counts.get("local_available_unverified_pool") or 0),
        "missing_intraday_count": int(counts.get("missing_intraday") or 0),
        "counts": counts,
        "suggested_next_action": (
            "use_verified_completed_pool_now"
            if counts.get("verified_completed_pool")
            else "continue_collecting_minute_data"
        ),
        "research_only": True,
        "paper_only": True,
    }


def load_pool_for_intraday_dir(intraday_dir: Path | None) -> dict[str, Any]:
    """Convenience wrapper for consumers that only know ``data/intraday``."""

    return load_completed_minute_pool_capability(project_root=_project_root_from_intraday_dir(intraday_dir))
