from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

from quantdesk.shared import read_json

PROJECT_ROOT = Path(__file__).resolve().parents[2]
LATEST_JSON = PROJECT_ROOT / "results" / "strategy_recommendation_latest.json"
DISCLAIMER = "Strategy recommendation snapshot is research-only. It is not a buy/sell/order instruction."


def _age_hours(path: Path) -> float | None:
    if not path.exists():
        return None
    return max(0.0, (datetime.now().timestamp() - path.stat().st_mtime) / 3600.0)


def _display_path(path: Path) -> str:
    try:
        return str(path.relative_to(PROJECT_ROOT)).replace("\\", "/")
    except ValueError:
        return str(path)


def load_strategy_recommendation_snapshot(config: dict[str, Any] | None = None) -> dict[str, Any]:
    cfg = dict(config or {})
    latest = PROJECT_ROOT / str(cfg.get("latest_json") or "results/strategy_recommendation_latest.json")
    stale_hours = float(cfg.get("stale_hours") or 48)
    if not latest.exists():
        return {
            "ok": True,
            "status": "missing",
            "latest_json": _display_path(latest),
            "latest_report": "reports/strategy_recommendation_latest.md",
            "recommendation_count": 0,
            "strategy_counts": {},
            "top_recommendations": [],
            "top_sectors": [],
            "warnings": ["strategy_recommendation_latest_missing"],
            "disclaimer": DISCLAIMER,
        }
    payload = read_json(latest, {})
    if not isinstance(payload, dict):
        payload = {}
    age = _age_hours(latest)
    warnings = [str(item) for item in (payload.get("warnings") or []) if item]
    status = str(payload.get("status") or "warning")
    if age is not None and age > stale_hours:
        status = "warning"
        warnings.append("strategy_recommendation_snapshot_stale")
    outputs = payload.get("output_paths") if isinstance(payload.get("output_paths"), dict) else {}
    return {
        "ok": True,
        "status": status,
        "generated_at": payload.get("generated_at") or "",
        "latest_json": outputs.get("latest_json") or _display_path(latest),
        "latest_report": outputs.get("latest_report") or "reports/strategy_recommendation_latest.md",
        "recommendation_count": int(payload.get("recommendation_count") or len(payload.get("recommendations") or [])),
        "strategy_counts": payload.get("strategy_counts") or {},
        "top_recommendations": list(payload.get("recommendations") or [])[: int(cfg.get("max_preview_items") or 5)],
        "top_sectors": payload.get("top_sectors") or [],
        "risk_flags": payload.get("risk_flags") or [],
        "warnings": list(dict.fromkeys(warnings)),
        "disclaimer": DISCLAIMER,
    }


def render_daily_strategy_recommendation_snapshot_markdown(snapshot: dict[str, Any]) -> str:
    lines = [
        "## Strategy Recommendation Snapshot",
        "",
        f"- status: `{snapshot.get('status') or 'missing'}`",
        f"- generated_at: `{snapshot.get('generated_at') or '-'}`",
        f"- recommendation_count: `{snapshot.get('recommendation_count', 0)}`",
        f"- latest_json: `{snapshot.get('latest_json') or '-'}`",
        f"- latest_report: `{snapshot.get('latest_report') or '-'}`",
        "",
    ]
    for row in snapshot.get("top_recommendations") or []:
        lines.append(
            f"- `{row.get('symbol')}` {row.get('name') or ''}: "
            f"{row.get('primary_strategy')} confidence={row.get('confidence')} sector={row.get('sector') or '-'}"
        )
    if not snapshot.get("top_recommendations"):
        lines.append("- No latest strategy recommendation. Run `python scripts/qd.py strategy-recommend --summary true`.")
    if snapshot.get("warnings"):
        lines += ["", "Warnings:", *[f"- {item}" for item in snapshot.get("warnings") or []]]
    lines += ["", snapshot.get("disclaimer") or DISCLAIMER]
    return "\n".join(lines)
