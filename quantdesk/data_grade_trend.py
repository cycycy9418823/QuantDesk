"""Data grade snapshots and trend history for QuantDesk data-health checks."""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from typing import Any

from quantdesk.shared import normalize_symbol, now_text, short_error_text, write_json
from quantdesk.version import API_SCHEMA_VERSION, APP_VERSION, PACKAGE_VERSION

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_HISTORY_PATH = PROJECT_ROOT / "results" / "data_health" / "data_grade_history.jsonl"
DEFAULT_OUTPUT_PATH = PROJECT_ROOT / "results" / "data_health" / "data_grade_trend_latest.json"
DISCLAIMER = "data_grade 趋势仅用于本地数据健康观察，不构成投资建议。"
GRADE_ORDER = ["BACKTEST_READY", "RESEARCH_READY", "SCORABLE", "UNAVAILABLE", "UNKNOWN"]


def _project_path(path: str | Path) -> Path:
    target = Path(path)
    if not target.is_absolute():
        target = PROJECT_ROOT / target
    return target


def _grade_for_symbol(symbol: str, freshness: dict[str, Any], intraday: dict[str, Any]) -> str:
    fresh_rows = freshness.get("symbols") if isinstance(freshness.get("symbols"), dict) else {}
    intra_rows = intraday.get("symbols") if isinstance(intraday.get("symbols"), dict) else {}
    fresh_row = fresh_rows.get(symbol) if isinstance(fresh_rows.get(symbol), dict) else {}
    intra_row = intra_rows.get(symbol) if isinstance(intra_rows.get(symbol), dict) else {}
    status = str(fresh_row.get("status") or "").lower()
    has_intraday = bool(intra_row.get("has_intraday"))
    if status == "fresh" and has_intraday:
        return "BACKTEST_READY"
    if status == "fresh":
        return "RESEARCH_READY"
    if status == "stale":
        return "SCORABLE"
    if status == "missing":
        return "UNAVAILABLE"
    return "UNKNOWN"


def build_data_grade_snapshot(
    *,
    target_date: str = "",
    symbols: Any = None,
    freshness_summary: dict[str, Any] | None = None,
    intraday_summary: dict[str, Any] | None = None,
    existing_dashboard: dict[str, Any] | None = None,
) -> dict[str, Any]:
    freshness = freshness_summary if isinstance(freshness_summary, dict) else {}
    intraday = intraday_summary if isinstance(intraday_summary, dict) else {}
    explicit_symbols: list[str] = []
    if isinstance(symbols, str):
        explicit_symbols = [normalize_symbol(item) for item in symbols.split(",") if normalize_symbol(item)]
    elif isinstance(symbols, (list, tuple, set)):
        explicit_symbols = [normalize_symbol(item) for item in symbols if normalize_symbol(item)]
    if not explicit_symbols:
        explicit_symbols = list((freshness.get("symbols") or {}).keys()) if isinstance(freshness.get("symbols"), dict) else []
    grades: dict[str, str] = {}
    for symbol in sorted(dict.fromkeys(explicit_symbols)):
        grades[symbol] = _grade_for_symbol(symbol, freshness, intraday)
    if not grades and isinstance(existing_dashboard, dict):
        counts = existing_dashboard.get("data_grade_counts") if isinstance(existing_dashboard.get("data_grade_counts"), dict) else {}
        if counts:
            return {
                "target_date": target_date or existing_dashboard.get("target_date") or "",
                "grades": {},
                "grade_counts": {grade: int(counts.get(grade) or 0) for grade in GRADE_ORDER},
                "total": sum(int(counts.get(grade) or 0) for grade in GRADE_ORDER),
                "warnings": ["从已有 data-health dashboard 读取 data_grade_counts，未重新扫描。"],
            }
    counts = Counter(grades.values())
    return {
        "target_date": str(target_date or freshness.get("target_date") or intraday.get("target_date") or ""),
        "grades": grades,
        "grade_counts": {grade: int(counts.get(grade) or 0) for grade in GRADE_ORDER},
        "total": len(grades),
        "warnings": [] if grades else ["无法确定 data_grade，已标记 UNKNOWN。"],
    }


def append_data_grade_history(snapshot: dict[str, Any], history_path: str | Path = DEFAULT_HISTORY_PATH) -> Path:
    target = _project_path(history_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(snapshot, ensure_ascii=False, default=str) + "\n")
    return target


def load_data_grade_history(history_path: str | Path = DEFAULT_HISTORY_PATH, limit: int | None = None) -> list[dict[str, Any]]:
    target = _project_path(history_path)
    if not target.exists():
        return []
    rows: list[dict[str, Any]] = []
    for line in target.read_text(encoding="utf-8", errors="ignore").splitlines():
        if not line.strip():
            continue
        try:
            payload = json.loads(line)
        except Exception:
            continue
        if isinstance(payload, dict):
            rows.append(payload)
    return rows[-limit:] if limit else rows


def summarize_data_grade_trend(history: list[dict[str, Any]], *, trend_days: int = 30) -> dict[str, Any]:
    rows = history[-max(1, int(trend_days or 30)) :]
    latest = rows[-1] if rows else {}
    return {
        "history_count": len(rows),
        "latest_target_date": latest.get("target_date") or "",
        "latest_grade_counts": latest.get("grade_counts") if isinstance(latest.get("grade_counts"), dict) else {},
    }


def write_data_grade_trend_summary(payload: dict[str, Any], output_path: str | Path = DEFAULT_OUTPUT_PATH) -> Path:
    return write_json(_project_path(output_path), payload)


def build_data_grade_trend_summary(
    *,
    target_date: str = "",
    symbols: Any = None,
    freshness_summary: dict[str, Any] | None = None,
    intraday_summary: dict[str, Any] | None = None,
    history_path: str | Path = DEFAULT_HISTORY_PATH,
    trend_days: int = 30,
    write: bool = False,
    output_path: str | Path = DEFAULT_OUTPUT_PATH,
    append_history: bool = True,
) -> dict[str, Any]:
    snapshot = build_data_grade_snapshot(
        target_date=target_date,
        symbols=symbols,
        freshness_summary=freshness_summary,
        intraday_summary=intraday_summary,
    )
    snapshot.update(
        {
            "schema_version": API_SCHEMA_VERSION,
            "app_version": APP_VERSION,
            "package_version": PACKAGE_VERSION,
            "api_schema_version": API_SCHEMA_VERSION,
            "generated_at": now_text(),
        }
    )
    if append_history:
        append_data_grade_history(snapshot, history_path)
    history = load_data_grade_history(history_path, limit=trend_days)
    trend = summarize_data_grade_trend(history, trend_days=trend_days)
    payload = {
        "ok": True,
        "schema_version": API_SCHEMA_VERSION,
        "app_version": APP_VERSION,
        "package_version": PACKAGE_VERSION,
        "api_schema_version": API_SCHEMA_VERSION,
        "task_type": "data_grade_trend",
        "status": "warning" if snapshot.get("warnings") else "passed",
        "target_date": snapshot.get("target_date") or str(target_date or ""),
        "grade_counts": snapshot.get("grade_counts") or {grade: 0 for grade in GRADE_ORDER},
        "grades": snapshot.get("grades") or {},
        "total": int(snapshot.get("total") or 0),
        "trend_days": int(trend_days),
        "trend": trend,
        "warnings": [short_error_text(item) for item in (snapshot.get("warnings") or [])],
        "generated_at": now_text(),
        "disclaimer": DISCLAIMER,
    }
    if write:
        write_data_grade_trend_summary(payload, output_path)
    return payload

