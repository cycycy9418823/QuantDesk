from __future__ import annotations

import time
from typing import Any

from quantdesk.platform_common import PROJECT_ROOT, read_json

SNAPSHOT_DEFS = {
    "similar_day": ("results/similar_day_latest.json", 168),
    "stress_scenarios": ("results/stress_scenarios_latest.json", 168),
    "style_attribution": ("results/style_attribution_latest.json", 168),
    "decision_journal": ("results/decision_journal_latest.json", 168),
}


def _load_snapshot(name: str, path: str, stale_hours: int) -> dict[str, Any]:
    target = PROJECT_ROOT / path
    warnings: list[str] = []
    payload = read_json(target, None)
    if not isinstance(payload, dict):
        return {"name": name, "status": "missing", "latest_json": path, "warnings": [f"{name}_latest_missing"]}
    age_hours = (time.time() - target.stat().st_mtime) / 3600 if target.exists() else None
    if age_hours is not None and age_hours > stale_hours:
        warnings.append(f"{name}_latest_stale")
    return {
        "name": name,
        "status": "warning" if warnings else str(payload.get("status") or "passed"),
        "latest_json": path,
        "latest_report": (payload.get("output_paths") or {}).get("latest_report", ""),
        "warning_count": len(payload.get("warnings") or []) + len(warnings),
        "warnings": warnings + list(payload.get("warnings") or [])[:5],
        "payload": payload,
    }


def load_daily_research_snapshots(config: dict | None = None) -> dict[str, Any]:
    cfg = dict(config or {})
    section = cfg.get("daily_research_snapshots") if isinstance(cfg.get("daily_research_snapshots"), dict) else {}
    snapshots = {}
    warnings: list[str] = []
    for name, (default_path, default_stale_hours) in SNAPSHOT_DEFS.items():
        item_cfg = section.get(name) if isinstance(section.get(name), dict) else {}
        if item_cfg.get("enabled", True) is False:
            snapshots[name] = {"name": name, "status": "skipped", "warnings": []}
            continue
        snapshot = _load_snapshot(name, item_cfg.get("latest_json") or default_path, int(item_cfg.get("stale_hours") or default_stale_hours))
        snapshots[name] = snapshot
        warnings.extend(snapshot.get("warnings") or [])
    return {"status": "warning" if warnings else "passed", "snapshots": snapshots, "warnings": warnings[:20]}


def render_daily_research_snapshots_markdown(payload: dict[str, Any]) -> str:
    snapshots = payload.get("snapshots") if isinstance(payload.get("snapshots"), dict) else {}
    similar = (snapshots.get("similar_day") or {}).get("payload") or {}
    stress = (snapshots.get("stress_scenarios") or {}).get("payload") or {}
    style = (snapshots.get("style_attribution") or {}).get("payload") or {}
    journal = (snapshots.get("decision_journal") or {}).get("payload") or {}
    worst = stress.get("worst_scenario") or {}
    top_tilt = (style.get("dominant_style_tilts") or [{}])[0] if style.get("dominant_style_tilts") else {}
    lines = [
        "## Research Snapshots",
        "",
        f"- Similar Day: {similar.get('status') or (snapshots.get('similar_day') or {}).get('status') or '-'} target={similar.get('target_date') or '-'} count={similar.get('similar_day_count') or 0}",
        f"- Stress: {stress.get('status') or (snapshots.get('stress_scenarios') or {}).get('status') or '-'} worst={worst.get('scenario_id') or '-'} return={worst.get('estimated_portfolio_return')}",
        f"- Style: {style.get('status') or (snapshots.get('style_attribution') or {}).get('status') or '-'} top_tilt={top_tilt.get('factor') or '-'} exposure={top_tilt.get('exposure')}",
        f"- Decision Journal: {journal.get('status') or (snapshots.get('decision_journal') or {}).get('status') or '-'} reviewed={journal.get('reviewed_count') or 0} pending={journal.get('pending_count') or 0}",
        "",
        "Research-only snapshots. Missing or stale latest JSON files are warnings, not workflow failures.",
    ]
    return "\n".join(lines)
