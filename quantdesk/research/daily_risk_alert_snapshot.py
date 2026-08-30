from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

from quantdesk.shared import read_json

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DISCLAIMER = "Daily risk snapshot is research-only and paper-only. No real orders."


def _display(path: Path) -> str:
    try:
        return str(path.relative_to(PROJECT_ROOT)).replace("\\", "/")
    except ValueError:
        return str(path)


def _age_hours(path: Path) -> float | None:
    if not path.exists():
        return None
    return max(0.0, (datetime.now().timestamp() - path.stat().st_mtime) / 3600.0)


def load_daily_risk_alert_snapshot(config: dict[str, Any] | None = None) -> dict[str, Any]:
    cfg = dict(config or {})
    risk_path = PROJECT_ROOT / str(cfg.get("risk_alerts_json") or "results/risk_alerts_latest.json")
    smart_path = PROJECT_ROOT / str(cfg.get("smart_stop_json") or "results/smart_stop_latest.json")
    stale_hours = float(cfg.get("stale_hours") or 24)
    warnings: list[str] = []
    risk_payload = read_json(risk_path, {}) if risk_path.exists() else {}
    smart_payload = read_json(smart_path, {}) if smart_path.exists() else {}
    if not risk_path.exists():
        warnings.append("risk_alerts_latest_missing")
    if not smart_path.exists():
        warnings.append("smart_stop_latest_missing")
    for path, label in ((risk_path, "risk_alerts_snapshot_stale"), (smart_path, "smart_stop_snapshot_stale")):
        age = _age_hours(path)
        if age is not None and age > stale_hours:
            warnings.append(label)
    if isinstance(risk_payload, dict):
        warnings.extend(str(item) for item in risk_payload.get("warnings") or [] if item)
    status = str((risk_payload or {}).get("status") or ("missing" if warnings else "passed"))
    if warnings and status == "passed":
        status = "warning"
    alerts = list((risk_payload or {}).get("alerts") or [])[: int(cfg.get("max_alerts") or 5)]
    return {
        "ok": True,
        "status": status,
        "generated_at": (risk_payload or {}).get("generated_at") or "",
        "alert_count": int((risk_payload or {}).get("alert_count") or len((risk_payload or {}).get("alerts") or [])),
        "critical_count": int((risk_payload or {}).get("critical_count") or 0),
        "top_alerts": alerts,
        "smart_stop_summary": (risk_payload or {}).get("smart_stop_summary") or {
            "status": (smart_payload or {}).get("status") or "missing",
            "safe_count": (smart_payload or {}).get("safe_count") or 0,
            "watch_count": (smart_payload or {}).get("watch_count") or 0,
            "triggered_count": (smart_payload or {}).get("triggered_count") or 0,
        },
        "latest_json": _display(risk_path),
        "latest_report": ((risk_payload or {}).get("output_paths") or {}).get("latest_report") or "reports/risk_alerts_latest.md",
        "warnings": list(dict.fromkeys(warnings)),
        "disclaimer": DISCLAIMER,
    }


def render_daily_risk_alert_snapshot_markdown(snapshot: dict[str, Any]) -> str:
    lines = [
        "## Risk Alert Snapshot",
        "",
        f"- status: `{snapshot.get('status') or 'missing'}`",
        f"- generated_at: `{snapshot.get('generated_at') or '-'}`",
        f"- alert_count: `{snapshot.get('alert_count', 0)}`",
        f"- critical_count: `{snapshot.get('critical_count', 0)}`",
        f"- latest_report: `{snapshot.get('latest_report') or '-'}`",
        "",
    ]
    for alert in snapshot.get("top_alerts") or []:
        lines.append(f"- `{alert.get('severity')}` {alert.get('alert_type')}: {alert.get('symbol') or alert.get('sector') or 'portfolio'} - {alert.get('message')}")
    if not snapshot.get("top_alerts"):
        lines.append("- No latest risk alert rows. Run `python scripts/qd.py risk-alerts --summary true`.")
    if snapshot.get("warnings"):
        lines += ["", "Warnings:", *[f"- {item}" for item in snapshot.get("warnings") or []]]
    lines += ["", snapshot.get("disclaimer") or DISCLAIMER]
    return "\n".join(lines)
