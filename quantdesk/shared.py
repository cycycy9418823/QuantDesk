"""Shared low-level helpers for QuantDesk.

These helpers are intentionally small and dependency-light.  They centralize
symbol normalization, truthy parsing, timestamp formatting, JSON IO, and safe
numeric conversion so feature modules do not keep drifting apart.
"""

from __future__ import annotations

import csv
import json
import math
import os
import time
import uuid
from datetime import date, datetime
from pathlib import Path
from typing import Any

from quantdesk.time_utils import now_iso as local_now_iso  # noqa: F401 – re-export
from quantdesk.time_utils import now_text, utc_now_iso  # noqa: F401 – re-export


def normalize_symbol(symbol: Any) -> str:
    """Return a stable six-digit A-share symbol when possible.

    The function is defensive by design: malformed values are stripped and
    returned as text instead of raising in long-running workflows.
    """

    if symbol is None:
        return ""
    if isinstance(symbol, bool):
        return str(symbol)
    if isinstance(symbol, (int,)):
        return str(symbol).zfill(6)[-6:]
    if isinstance(symbol, float):
        if math.isnan(symbol):
            return ""
        if symbol.is_integer():
            return str(int(symbol)).zfill(6)[-6:]
        text = str(symbol).strip()
    else:
        text = str(symbol).strip()
    if not text:
        return ""
    lowered = text.lower().replace(" ", "")
    if lowered in {"nan", "none", "null", "<na>"}:
        return ""
    for prefix in ("sh.", "sz.", "bj.", "sh", "sz", "bj"):
        if lowered.startswith(prefix):
            text = text[len(prefix):]
            lowered = lowered[len(prefix):]
            break
    if "." in lowered:
        head, tail = lowered.split(".", 1)
        if tail in {"sh", "sz", "bj", "ss"}:
            text = head
            lowered = head
    digits = "".join(ch for ch in str(text) if ch.isdigit())
    if digits:
        return digits.zfill(6)[-6:]
    return str(symbol).strip()


def truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        try:
            return float(value) != 0.0
        except Exception:
            return False
    text = str(value or "").strip().lower()
    if "=" in text:
        text = text.split("=", 1)[1].strip()
    return text in {"1", "true", "yes", "y", "on", "included", "是", "真", "对"}


def to_jsonable(value: Any) -> Any:
    """Return a JSON-safe structure without NaN/Inf leakage."""

    if isinstance(value, dict):
        return {str(key): to_jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [to_jsonable(item) for item in value]
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if hasattr(value, "item"):
        try:
            return to_jsonable(value.item())
        except Exception:
            pass
    if hasattr(value, "isoformat"):
        try:
            return value.isoformat()
        except Exception:
            pass
    if hasattr(value, "to_dict"):
        try:
            return to_jsonable(value.to_dict())
        except Exception:
            pass
    return value


def _json_default(obj: Any) -> Any:
    try:
        import numpy as np
        import pandas as pd

        if isinstance(obj, (np.integer,)):
            return int(obj)
        if isinstance(obj, (np.floating,)):
            return float(obj)
        if isinstance(obj, (np.bool_,)):
            return bool(obj)
        if isinstance(obj, np.ndarray):
            return obj.tolist()
        if isinstance(obj, (pd.Timestamp,)):
            return obj.isoformat()
        if hasattr(obj, "to_dict"):
            return obj.to_dict()
    except Exception:
        pass
    if hasattr(obj, "item"):
        return obj.item()
    raise TypeError(f"Object of type {type(obj).__name__} is not JSON serializable")


def read_json(path: str | Path, default: Any = None) -> Any:
    target = Path(path)
    if not target.exists():
        return default
    try:
        return json.loads(target.read_text(encoding="utf-8-sig"))
    except Exception:
        return default


def read_json_default_dict(path: str | Path, default: Any = None) -> Any:
    return read_json(path, {} if default is None else default)


def read_csv_rows(path: str | Path) -> list[dict[str, str]]:
    target = Path(path)
    if not target.exists():
        return []
    with target.open("r", encoding="utf-8-sig", newline="") as handle:
        return [dict(row) for row in csv.DictReader(handle)]


def read_csv_rows_with_headers(path: str | Path) -> tuple[list[str], list[dict[str, str]]]:
    target = Path(path)
    if not target.exists():
        return [], []
    with target.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        return list(reader.fieldnames or []), [dict(row) for row in reader]


def write_json(path: str | Path, payload: Any) -> Path:
    target = Path(path)
    tmp: Path | None = None
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_name(f"{target.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
        text = json.dumps(to_jsonable(payload), ensure_ascii=False, indent=2, allow_nan=False, default=_json_default)
        tmp.write_text(text + "\n", encoding="utf-8")
        last_error: Exception | None = None
        for attempt in range(5):
            try:
                os.replace(tmp, target)
                break
            except PermissionError as exc:
                last_error = exc
                if attempt == 4:
                    raise
                time.sleep(0.05 * (attempt + 1))
        else:
            if last_error is not None:
                raise last_error
        return target
    except Exception as exc:  # noqa: BLE001
        if tmp is not None:
            try:
                tmp.unlink(missing_ok=True)
            except Exception:
                pass
        raise RuntimeError(f"write_json failed for {target}: {exc}") from exc


def write_text(path: str | Path, text: str, *, trailing_newline: bool = True) -> Path:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    content = str(text)
    if trailing_newline:
        content = content.rstrip() + "\n"
    target.write_text(content, encoding="utf-8")
    return target


def write_md(path: str | Path, text: str) -> Path:
    return write_text(path, text, trailing_newline=True)


def md_table(rows: list[dict[str, Any]], cols: list[str]) -> str:
    """Render a list of dicts as a GitHub-flavored Markdown table."""
    lines = ["|" + "|".join(cols) + "|", "|" + "|".join(["---"] * len(cols)) + "|"]
    for row in rows:
        lines.append("|" + "|".join(str(row.get(col, "")) for col in cols) + "|")
    return "\n".join(lines)


def write_csv_rows(
    path: str | Path,
    rows: list[dict[str, Any]],
    fieldnames: list[str] | None = None,
    *,
    fields: list[str] | None = None,
    encoding: str = "utf-8",
    lineterminator: str | None = None,
) -> Path:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    if fieldnames is not None:
        columns = fieldnames
    elif fields is not None:
        columns = fields
    else:
        columns = list(rows[0]) if rows else ["status"]
    with target.open("w", encoding=encoding, newline="") as handle:
        writer_kwargs: dict[str, Any] = {"fieldnames": columns}
        if lineterminator is not None:
            writer_kwargs["lineterminator"] = lineterminator
        writer = csv.DictWriter(handle, **writer_kwargs)
        writer.writeheader()
        for row in rows:
            writer.writerow({key: row.get(key, "") for key in columns})
    return target


def now_iso_tz(current: datetime | None = None) -> str:
    """Return local ISO timestamp with timezone offset, matching legacy reports."""

    return (current or datetime.now().astimezone()).isoformat(timespec="seconds")


def ensure_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, tuple):
        return list(value)
    if isinstance(value, set):
        return list(value)
    return [value]


def safe_float(value: Any, default: float | None = None) -> float | None:
    try:
        if value is None or value == "":
            return default
        number = float(value)
        if not math.isfinite(number):
            return default
        return number
    except Exception:
        return default


def safe_float_zero(value: Any, default: float = 0.0) -> float:
    number = safe_float(value, default)
    return default if number is None else float(number)


def safe_int(value: Any, default: int | None = None) -> int | None:
    number = safe_float(value, None)
    if number is None:
        return default
    try:
        return int(number)
    except Exception:
        return default


def safe_int_zero(value: Any, default: int = 0) -> int:
    number = safe_int(value, default)
    return default if number is None else int(number)


def short_error_text(value: Any, max_len: int = 300) -> str:
    text = str(value or "").strip().replace("\r", " ").replace("\n", " ")
    if len(text) <= max_len:
        return text
    return text[: max(0, max_len - 3)].rstrip() + "..."


def safe_fillna(
    frame: Any,
    value: Any = 0.0,
    reason: str = "",
    warnings: list[str] | None = None,
) -> Any:
    """Fill NaN values in a DataFrame/Series and record a warning.

    Unlike a bare ``fillna(0)`` this function ensures the caller is reminded
    *why* missing data was substituted, so downstream payloads can surface the
    gap to the user instead of silently fabricating results.
    """
    if warnings is not None and reason:
        warnings.append(reason)
    return frame.fillna(value)


def safe_empty_frame(
    reason: str,
    columns: list[str] | None = None,
    warnings: list[str] | None = None,
) -> Any:
    """Return an empty DataFrame and record a warning.

    Use this instead of a bare ``return pd.DataFrame()`` so callers cannot
    mistake an error-induced empty frame for genuinely empty data.
    """
    import pandas as pd

    if warnings is not None and reason:
        warnings.append(reason)
    if columns:
        return pd.DataFrame(columns=columns)
    return pd.DataFrame()
