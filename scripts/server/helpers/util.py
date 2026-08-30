"""Pure utility helpers (number coercion, JSON/path, formatting) for the 8000 server.

Bottom layer of the server helper stack: depends only on stdlib + constants +
app_factory. Re-exported from quant_api_server to preserve the attribute contract.
"""
from __future__ import annotations

import json
import math
import time
from pathlib import Path
from typing import Any

from scripts.server.constants import project_root
from scripts.server.app_factory import sanitize_json_value


def finite_float(value: Any, default: float | None = None) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return default
    return result if math.isfinite(result) else default


def safe_int(value: Any, default: int = 0) -> int:
    number = finite_float(value)
    return int(number) if number is not None else default


def display_number(value: Any, digits: int = 2, suffix: str = "") -> str:
    number = finite_float(value)
    if number is None:
        return "数据缺失"
    return f"{number:.{digits}f}{suffix}"


def chart_number(value: Any, default: float = 0.0) -> float:
    number = finite_float(value)
    return float(number) if number is not None else default


def clean_payload(value: Any) -> Any:
    if value is None:
        return "数据缺失"
    cleaned = sanitize_json_value(value)
    if cleaned is None:
        return "数据缺失"
    if isinstance(cleaned, dict):
        return {str(k): clean_payload(v) for k, v in cleaned.items()}
    if isinstance(cleaned, list):
        return [clean_payload(v) for v in cleaned]
    return cleaned


def request_truthy(value: Any) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def resolve_project_csv_path(value: Any, default: Path) -> Path:
    raw = str(value or "").strip()
    candidate = Path(raw) if raw else default
    if not candidate.is_absolute():
        candidate = project_root / candidate
    resolved = candidate.resolve()
    root = project_root.resolve()
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise ValueError("只允许读取项目目录内的本地 CSV 文件。") from exc
    if resolved.suffix.lower() != ".csv":
        raise ValueError("导入文件必须是 CSV。")
    return resolved


def read_json_file(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except Exception as exc:
        return {
            "ok": False,
            "status": "unreadable",
            "message": f"JSON read failed: {exc}",
            "path": str(path),
        }
    return payload if isinstance(payload, dict) else {}


def cache_age_seconds(path: Path) -> float | None:
    try:
        return max(0.0, time.time() - path.stat().st_mtime)
    except OSError:
        return None


def human_duration(seconds: Any) -> str:
    value = finite_float(seconds)
    if value is None:
        return "数据缺失"
    seconds_int = max(0, int(value))
    if seconds_int < 60:
        return f"{seconds_int}秒"
    minutes = seconds_int // 60
    if minutes < 60:
        return f"{minutes}分钟"
    hours = minutes // 60
    if hours < 48:
        return f"{hours}小时"
    days = hours // 24
    return f"{days}天"

