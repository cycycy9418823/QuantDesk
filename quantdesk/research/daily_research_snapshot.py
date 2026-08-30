from __future__ import annotations

import json
import math
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DASHBOARD_JSON = "results/research_dashboard_latest.json"
DEFAULT_DASHBOARD_REPORT = "reports/research_dashboard_report_latest.md"
DISCLAIMER = "Research diagnostics only. Not investment advice."


def _project_path(path: str | Path) -> Path:
    target = Path(path)
    return target if target.is_absolute() else PROJECT_ROOT / target


def _display_path(path: str | Path) -> str:
    target = _project_path(path)
    try:
        return str(target.relative_to(PROJECT_ROOT)).replace("\\", "/")
    except Exception:
        return str(target).replace("\\", "/")


def _as_dict(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def _as_list(value: Any) -> list[Any]:
    return list(value) if isinstance(value, list) else []


def _safe_float(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        number = float(value)
    except Exception:
        return None
    return number if math.isfinite(number) else None


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        number = int(float(value))
    except Exception:
        return default
    return number


def _json_safe(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_json_safe(item) for item in value]
    if isinstance(value, tuple):
        return [_json_safe(item) for item in value]
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if hasattr(value, "item"):
        try:
            return _json_safe(value.item())
        except Exception:
            pass
    try:
        if pd.isna(value):
            return None
    except Exception:
        pass
    return value


def _parse_dt(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = pd.to_datetime(value, errors="coerce")
        if pd.isna(parsed):
            return None
        return parsed.to_pydatetime()
    except Exception:
        return None


def _is_stale(generated_at: Any, stale_hours: int) -> bool:
    dt = _parse_dt(generated_at)
    if dt is None:
        return False
    return datetime.now() - dt > timedelta(hours=max(1, int(stale_hours)))


def load_research_dashboard_snapshot(
    path: str | Path = DEFAULT_DASHBOARD_JSON,
    *,
    stale_hours: int = 48,
) -> dict[str, Any]:
    """Load the latest research dashboard for daily read-only display."""

    target = _project_path(path)
    latest_json = _display_path(target)
    if not target.exists():
        return build_daily_research_snapshot(
            None,
            stale_hours=stale_hours,
            latest_json=latest_json,
            warnings=[f"missing_research_dashboard: {latest_json}"],
            forced_status="missing",
        )
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        return build_daily_research_snapshot(
            None,
            stale_hours=stale_hours,
            latest_json=latest_json,
            warnings=[f"invalid_research_dashboard_json: {latest_json}: {exc}"],
            forced_status="invalid",
        )
    if not isinstance(payload, dict):
        return build_daily_research_snapshot(
            None,
            stale_hours=stale_hours,
            latest_json=latest_json,
            warnings=[f"invalid_research_dashboard_payload: {latest_json}"],
            forced_status="invalid",
        )
    warnings: list[str] = []
    if payload.get("task_type") != "factor_research_dashboard":
        warnings.append(f"task_type_mismatch: {payload.get('task_type') or 'missing'}")
    return build_daily_research_snapshot(
        payload,
        stale_hours=stale_hours,
        latest_json=latest_json,
        warnings=warnings,
    )


def build_daily_research_snapshot(
    payload: dict[str, Any] | None,
    *,
    stale_hours: int = 48,
    latest_json: str = DEFAULT_DASHBOARD_JSON,
    warnings: list[str] | None = None,
    forced_status: str | None = None,
) -> dict[str, Any]:
    warnings = [str(item) for item in (warnings or []) if item]
    payload = _as_dict(payload)
    output_paths = _as_dict(payload.get("output_paths"))
    dashboard = _as_dict(payload.get("dashboard_summary"))
    module_health = _as_dict(payload.get("module_health"))
    factor = _as_dict(payload.get("factor_health_summary"))
    backtest = _as_dict(payload.get("backtest_summary"))
    sector = _as_dict(payload.get("sector_rotation_summary"))
    pit_status_payload = _as_dict(payload.get("pit_status"))

    generated_at = str(payload.get("generated_at") or "")
    stale = _is_stale(generated_at, stale_hours)
    if forced_status:
        status = forced_status
    else:
        raw_status = str(payload.get("status") or "warning")
        status = "stale" if stale else (raw_status if raw_status in {"passed", "warning", "failed"} else "warning")
    if stale:
        warnings.append(f"stale_research_dashboard: {generated_at}")

    missing_modules = _safe_int(dashboard.get("missing_module_count"), len(_as_list(dashboard.get("missing_modules"))))
    stale_modules = _safe_int(dashboard.get("stale_module_count"), len(_as_list(dashboard.get("stale_modules"))))
    available_modules = sum(1 for item in module_health.values() if isinstance(item, dict) and item.get("available"))
    if not module_health:
        available_modules = 0

    top_sectors = _as_list(sector.get("top_sectors_preview"))[:5]
    if not top_sectors:
        top_sectors = _as_list(payload.get("top_sectors_preview"))[:5]

    pit_universe = bool(
        payload.get("pit_universe")
        or pit_status_payload.get("pit_universe")
        or _as_dict(payload.get("universe")).get("pit_universe")
    )
    pit_available = bool(pit_status_payload.get("available") or pit_universe)
    pit_status = "available" if pit_universe else ("warning" if pit_available else "missing" if payload else "unknown")

    risk_flags = _as_list(dashboard.get("risk_flags")) or _as_list(payload.get("risk_flags_preview"))
    actions = _as_list(dashboard.get("suggested_next_actions")) or _as_list(payload.get("suggested_next_actions_preview"))
    findings = _as_list(dashboard.get("key_findings")) or _as_list(payload.get("key_findings_preview"))
    warnings.extend(str(item) for item in _as_list(payload.get("warnings"))[:5] if item)

    snapshot = {
        "enabled": True,
        "status": status,
        "readiness_level": dashboard.get("readiness_level") or payload.get("readiness_level") or "unknown",
        "generated_at": generated_at,
        "stale": bool(stale),
        "available_module_count": int(available_modules),
        "missing_module_count": int(missing_modules),
        "stale_module_count": int(stale_modules),
        "key_findings_preview": [str(item) for item in findings[:5]],
        "risk_flags_preview": [str(item) for item in risk_flags[:5]],
        "suggested_next_actions_preview": [str(item) for item in actions[:5]],
        "top_sectors_preview": _json_safe(top_sectors),
        "usable_factor_count": _safe_int(factor.get("usable_count") or factor.get("usable_factor_count")),
        "weak_factor_count": _safe_int(factor.get("weak_count") or factor.get("weak_factor_count")),
        "pit_universe": bool(pit_universe),
        "pit_status": pit_status,
        "backtest_sharpe": _safe_float(backtest.get("sharpe_ratio")),
        "backtest_max_drawdown": _safe_float(backtest.get("max_drawdown")),
        "latest_report": str(output_paths.get("latest_report") or output_paths.get("report") or DEFAULT_DASHBOARD_REPORT).replace("\\", "/"),
        "latest_json": str(output_paths.get("latest_json") or latest_json or DEFAULT_DASHBOARD_JSON).replace("\\", "/"),
        "warnings": list(dict.fromkeys(warnings)),
        "warning_count": len(list(dict.fromkeys(warnings))),
        "disclaimer": DISCLAIMER,
    }
    return _json_safe(snapshot)


def render_daily_research_snapshot_markdown(snapshot: dict[str, Any]) -> str:
    snapshot = _as_dict(snapshot)
    status = str(snapshot.get("status") or "missing")
    readiness = str(snapshot.get("readiness_level") or "unknown")
    lines = [
        "## Research Dashboard Snapshot",
        "",
        "### Disclaimer",
        "",
        DISCLAIMER,
        "",
        f"- Status: `{status}`",
        f"- Readiness: `{readiness}`",
        f"- Generated at: {snapshot.get('generated_at') or '-'}",
        f"- Module availability: {snapshot.get('available_module_count', 0)} available, {snapshot.get('missing_module_count', 0)} missing, {snapshot.get('stale_module_count', 0)} stale",
        f"- Factor health: {snapshot.get('usable_factor_count', 0)} usable, {snapshot.get('weak_factor_count', 0)} weak",
        f"- Backtest: Sharpe={snapshot.get('backtest_sharpe')}, MaxDD={snapshot.get('backtest_max_drawdown')}",
        f"- PIT universe: {str(bool(snapshot.get('pit_universe'))).lower()} ({snapshot.get('pit_status') or 'unknown'})",
        f"- Dashboard report: {snapshot.get('latest_report') or DEFAULT_DASHBOARD_REPORT}",
        f"- Dashboard JSON: {snapshot.get('latest_json') or DEFAULT_DASHBOARD_JSON}",
        "",
    ]
    sectors = _as_list(snapshot.get("top_sectors_preview"))
    if sectors:
        lines += ["### Sector Rotation Highlights", ""]
        for item in sectors[:5]:
            row = _as_dict(item)
            lines.append(
                f"- {row.get('sector_name') or '-'}: heat={row.get('heat_score')}, status={row.get('heat_status') or '-'}, rank={row.get('current_rank') or '-'}"
            )
        lines.append("")
    findings = _as_list(snapshot.get("key_findings_preview"))
    if findings:
        lines += ["### Key Findings", ""]
        lines.extend(f"- {item}" for item in findings[:5])
        lines.append("")
    risks = _as_list(snapshot.get("risk_flags_preview"))
    if risks:
        lines += ["### Risk Flags", ""]
        lines.extend(f"- {item}" for item in risks[:5])
        lines.append("")
    actions = _as_list(snapshot.get("suggested_next_actions_preview"))
    if actions:
        lines += ["### Suggested Research Actions", ""]
        lines.extend(f"- {item}" for item in actions[:5])
        lines.append("")
    warnings = _as_list(snapshot.get("warnings"))
    if warnings:
        lines += ["### Warnings", ""]
        lines.extend(f"- {item}" for item in warnings[:5])
        lines.append("")
    if status in {"missing", "invalid"}:
        lines += [
            "Generate or refresh the dashboard with:",
            "",
            "```powershell",
            "python scripts/qd.py research-dashboard --summary true",
            "```",
            "",
        ]
    lines.append("This snapshot is read-only and does not change candidates, guardrails, backtests, scores, or trading conclusions.")
    return "\n".join(lines)
