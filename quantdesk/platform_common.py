from __future__ import annotations

import json
import math
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from quantdesk.shared import read_json as shared_read_json
from quantdesk.shared import truthy as shared_truthy
from quantdesk.shared import write_csv_rows
from quantdesk.shared import write_json as shared_write_json
from quantdesk.shared import write_text as shared_write_text
from quantdesk.time_utils import now_iso
from quantdesk.version import APP_VERSION

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DISCLAIMER = "研究观察，不构成买卖建议。No broker. No real orders."


def truthy(value: Any) -> bool:
    return shared_truthy(value)


def rel(path: Path) -> str:
    try:
        return path.relative_to(PROJECT_ROOT).as_posix()
    except ValueError:
        return str(path)


def clean_number(value: Any, default: float = 0.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    if math.isnan(number) or math.isinf(number):
        return default
    return number


def clamp(value: Any, low: float = 0.0, high: float = 100.0) -> float:
    return max(low, min(high, clean_number(value)))


def read_json(path: str | Path, default: Any = None) -> Any:
    target = Path(path)
    if not target.is_absolute():
        target = PROJECT_ROOT / target
    return shared_read_json(target, default)


def read_json_with_error(path: str | Path) -> dict[str, Any]:
    target = Path(path)
    if not target.is_absolute():
        target = PROJECT_ROOT / target
    if not target.exists():
        return {}
    try:
        loaded = json.loads(target.read_text(encoding="utf-8-sig"))
        return loaded if isinstance(loaded, dict) else {"_read_error": "json_root_not_object", "_path": rel(target)}
    except Exception as exc:
        return {"_read_error": str(exc), "_path": rel(target)}


def write_json(path: str | Path, payload: dict[str, Any]) -> None:
    target = Path(path)
    if not target.is_absolute():
        target = PROJECT_ROOT / target
    shared_write_json(target, payload)


def write_csv(
    path: str | Path,
    rows: list[dict[str, Any]],
    fields: list[str] | None = None,
    *,
    fieldnames: list[str] | None = None,
    encoding: str = "utf-8-sig",
) -> Path:
    target = Path(path)
    if not target.is_absolute():
        target = PROJECT_ROOT / target
    return write_csv_rows(target, rows, fields=fields, fieldnames=fieldnames, encoding=encoding)


def write_text(path: str | Path, text: str) -> None:
    target = Path(path)
    if not target.is_absolute():
        target = PROJECT_ROOT / target
    shared_write_text(target, text)


def base_payload(task_type: str, *, status: str = "passed", warnings: list[str] | None = None) -> dict[str, Any]:
    warnings = list(dict.fromkeys(warnings or []))
    return {
        "ok": status != "failed",
        "status": status,
        "task_type": task_type,
        "generated_at": now_iso(),
        "app_version": APP_VERSION,
        "network": False,
        "research_only": True,
        "paper_only": True,
        "warnings": warnings,
        "errors": [],
        "output_paths": {},
        "disclaimer": DISCLAIMER,
    }


def finalize_outputs(payload: dict[str, Any], stem: str, markdown: str) -> dict[str, Any]:
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    latest_json = PROJECT_ROOT / "results" / f"{stem}_latest.json"
    timestamp_json = PROJECT_ROOT / "results" / f"{stem}_{stamp}.json"
    latest_report = PROJECT_ROOT / "reports" / f"{stem}_latest.md"
    timestamp_report = PROJECT_ROOT / "reports" / f"{stem}_{stamp}.md"
    paths = {
        "latest_json": rel(latest_json),
        "timestamp_json": rel(timestamp_json),
        "latest_report": rel(latest_report),
        "timestamp_report": rel(timestamp_report),
    }
    payload["output_paths"] = paths
    write_json(latest_json, payload)
    write_json(timestamp_json, payload)
    write_text(latest_report, markdown)
    write_text(timestamp_report, markdown)
    return payload


def simple_markdown(title: str, payload: dict[str, Any], rows_key: str = "items") -> str:
    lines = [
        f"# {title}",
        "",
        f"- status: `{payload.get('status')}`",
        f"- generated_at: `{payload.get('generated_at')}`",
        f"- app_version: `{payload.get('app_version')}`",
        "",
    ]
    rows = payload.get(rows_key) or payload.get("top_candidates") or payload.get("alerts") or []
    if isinstance(rows, list):
        for row in rows[:20]:
            if isinstance(row, dict):
                label = row.get("symbol") or row.get("name") or row.get("scenario") or row.get("decision_id") or row.get("channel") or "item"
                lines.append(f"- `{label}`: {row.get('status') or row.get('primary_strategy') or row.get('severity') or row.get('summary') or ''}")
    if payload.get("warnings"):
        lines += ["", "## Warnings", *[f"- {item}" for item in payload.get("warnings", [])]]
    lines += ["", payload.get("disclaimer") or DISCLAIMER]
    return "\n".join(lines)


def synthetic_candidates() -> list[dict[str, Any]]:
    return [
        {"symbol": "600744", "name": "华银电力", "sector": "电力", "strategy": "long_swing", "confidence": 86, "stars": 5, "close": 8.8},
        {"symbol": "002115", "name": "三维通信", "sector": "通信", "strategy": "board", "confidence": 82, "stars": 4, "close": 9.9, "risk_flags": ["limit_up_chase_risk"]},
        {"symbol": "300323", "name": "华灿光电", "sector": "电子", "strategy": "observe", "confidence": 61, "stars": 3, "close": 5.1},
    ]


def decision_id() -> str:
    return "dj_" + uuid.uuid4().hex[:12]


def stale_date(days: int = 1) -> str:
    return (datetime.now() + timedelta(days=days)).date().isoformat()
