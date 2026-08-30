
from __future__ import annotations

import shutil
from datetime import date, datetime
from pathlib import Path
from typing import Any

from quantdesk.shared import read_json as _read_json
from quantdesk.shared import safe_float as _safe_float
from quantdesk.shared import safe_int as _safe_int
from quantdesk.shared import write_json as _write_json


safe_float = _safe_float
safe_int = _safe_int


def safe_pct(value: Any, default: float | None = None) -> float | None:
    number = safe_float(value, default)
    if number is None:
        return default
    return number


def safe_date(value: Any, default: str = "") -> str:
    if value is None or value == "":
        return default
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%d")
    if isinstance(value, date):
        return value.strftime("%Y-%m-%d")
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return parsed.strftime("%Y-%m-%d")
    except Exception:
        text = str(value).strip()
        return text[:10] if len(text) >= 10 else default


def clamp(value: float, low: float = 0.0, high: float = 100.0) -> float:
    return max(low, min(high, value))


def format_number(value: Any, digits: int = 2) -> str:
    number = safe_float(value, None)
    if number is None:
        return str(value or "")
    if abs(number) >= 10000:
        return f"{number:.0f}"
    text = f"{number:.{digits}f}".rstrip("0").rstrip(".")
    return text if text else "0"


def format_pct(value: Any, digits: int = 2) -> str:
    number = safe_float(value, None)
    if number is None:
        return ""
    return f"{number:.{digits}f}%"


def read_json_safe(path: str | Path, default: Any = None) -> Any:
    return _read_json(path, default)


def backup_bad_json(path: str | Path) -> Path | None:
    target = Path(path)
    if not target.exists():
        return None
    timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    backup = target.with_name(f'{target.stem}.bad_{timestamp}{target.suffix}')
    try:
        shutil.copy2(target, backup)
        return backup
    except Exception:
        return None


def atomic_write_json(path: str | Path, payload: Any) -> Path:
    return _write_json(path, payload)
