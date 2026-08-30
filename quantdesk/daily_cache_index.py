from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any, Mapping

import pandas as pd

from quantdesk.csv_io import read_csv_frame
from quantdesk.data_quality import DATA_GRADE_LABELS, assess_daily_frame_grade, normalize_date_text
from quantdesk.utils import atomic_write_json, read_json_safe, safe_int

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = PROJECT_ROOT / "data"
DAILY_DIR = DATA_DIR / "daily"
CACHE_DIR = DATA_DIR / "cache"
INDEX_PATH = CACHE_DIR / "daily_cache_index.json"

COLUMN_ALIASES = {
    "日期": "date",
    "开盘": "open",
    "收盘": "close",
    "最高": "high",
    "最低": "low",
    "成交量": "volume",
    "成交额": "amount",
    "涨跌幅": "pct_chg",
    "涨跌额": "change",
    "换手率": "turnover",
}


def now_text() -> str:
    return datetime.now().isoformat(timespec="seconds")


def normalize_symbol(value: object) -> str:
    text = str(value or "").strip()
    digits = "".join(ch for ch in text if ch.isdigit())
    if len(digits) >= 6:
        return digits[-6:]
    if digits:
        return digits.zfill(6)
    return ""


def daily_cache_index_path() -> Path:
    return INDEX_PATH


def _relative_path_text(path: Path) -> str:
    try:
        return str(path.resolve().relative_to(PROJECT_ROOT)).replace("\\", "/")
    except Exception:
        return str(path).replace("\\", "/")


def _empty_index(path: Path | None = None) -> dict[str, Any]:
    target = path or INDEX_PATH
    return {
        "schema_version": "1",
        "updated_at": "",
        "symbol_count": 0,
        "symbols": {},
        "warnings": [],
        "path": str(target),
        "exists": False,
    }


def load_daily_cache_index(path: str | Path | None = None) -> dict[str, Any]:
    target = Path(path) if path else INDEX_PATH
    raw = read_json_safe(target, default=None)
    if not isinstance(raw, dict):
        return _empty_index(target)
    symbols = raw.get("symbols")
    if not isinstance(symbols, dict):
        symbols = {}
    raw["symbols"] = symbols
    warnings = raw.get("warnings")
    raw["warnings"] = list(warnings) if isinstance(warnings, list) else []
    raw["schema_version"] = str(raw.get("schema_version") or "1")
    raw["updated_at"] = str(raw.get("updated_at") or "")
    raw["symbol_count"] = safe_int(raw.get("symbol_count"), len(symbols)) or len(symbols)
    raw["path"] = str(target)
    raw["exists"] = True
    return raw


def save_daily_cache_index(index: Mapping[str, Any], path: str | Path | None = None) -> Path:
    target = Path(path) if path else INDEX_PATH
    payload = dict(index or {})
    symbols = payload.get("symbols")
    if not isinstance(symbols, dict):
        symbols = {}
    payload["symbols"] = symbols
    warnings = payload.get("warnings")
    payload["warnings"] = list(warnings) if isinstance(warnings, list) else []
    payload["schema_version"] = str(payload.get("schema_version") or "1")
    payload["updated_at"] = payload.get("updated_at") or now_text()
    payload["symbol_count"] = len(symbols)
    payload["path"] = str(target)
    payload["exists"] = True
    return atomic_write_json(target, payload)


def _canonicalize_frame(df: pd.DataFrame) -> pd.DataFrame:
    frame = df.copy()
    rename_map: dict[str, str] = {}
    for column in frame.columns:
        key = str(column).strip()
        if key in COLUMN_ALIASES:
            rename_map[column] = COLUMN_ALIASES[key]
        elif key.lower() == "date":
            rename_map[column] = "date"
        elif key.lower() == "close":
            rename_map[column] = "close"
        elif key.lower() == "symbol":
            rename_map[column] = "symbol"
    if rename_map:
        frame = frame.rename(columns=rename_map)
    return frame


def _frame_entry(
    symbol: str,
    csv_path: Path,
    frame: pd.DataFrame,
    *,
    indexed_at: str | None = None,
    csv_read_warnings: list[str] | None = None,
) -> dict[str, Any] | None:
    if frame is None or frame.empty:
        return None
    normalized = _canonicalize_frame(frame)
    if "date" not in normalized.columns:
        return None
    dates = pd.to_datetime(normalized["date"], errors="coerce").dropna()
    if dates.empty:
        return None
    latest_ts = pd.Timestamp(dates.max()).normalize()
    first_ts = pd.Timestamp(dates.min()).normalize()
    assessment = assess_daily_frame_grade(
        normalized,
        provider_status="local_daily_cache",
        expected_latest_date=latest_ts,
        min_scorable_rows=20,
        min_research_rows=120,
        min_backtest_rows=500,
    )
    stat = csv_path.stat()
    entry = {
        "symbol": symbol,
        "csv_path": _relative_path_text(csv_path),
        "latest_date": latest_ts.strftime("%Y-%m-%d"),
        "first_date": first_ts.strftime("%Y-%m-%d"),
        "row_count": int(len(normalized)),
        "file_mtime": round(float(stat.st_mtime), 6),
        "file_size": int(stat.st_size),
        "indexed_at": indexed_at or now_text(),
        "data_grade": assessment.data_grade,
        "data_grade_label": assessment.data_grade_label,
        "data_grade_reason": assessment.reason,
        "provider_status": assessment.provider_status,
        "csv_read_warnings": list(csv_read_warnings or []),
    }
    return entry


def upsert_daily_cache_index_entry(
    index: Mapping[str, Any] | None,
    symbol: str,
    csv_path: str | Path | None = None,
    *,
    frame: pd.DataFrame | None = None,
    metadata: Mapping[str, Any] | None = None,
    indexed_at: str | None = None,
) -> dict[str, Any] | None:
    target_index = dict(index or _empty_index())
    symbols = target_index.setdefault("symbols", {})
    if not isinstance(symbols, dict):
        symbols = {}
        target_index["symbols"] = symbols
    normalized_symbol = normalize_symbol(symbol)
    if not normalized_symbol:
        return None
    path = Path(csv_path) if csv_path else DAILY_DIR / f"{normalized_symbol}.csv"
    if not path.is_absolute():
        path = PROJECT_ROOT / path
    if not path.exists():
        return None
    local_frame = frame
    csv_read_warnings: list[str] = []
    if local_frame is None:
        local_frame = read_csv_frame(path)
        csv_read_warnings = [
            f"daily_csv:{normalized_symbol}:{warning}"
            for warning in local_frame.attrs.get("warnings", [])
        ]
    entry = _frame_entry(
        normalized_symbol,
        path,
        local_frame,
        indexed_at=indexed_at,
        csv_read_warnings=csv_read_warnings,
    )
    if entry is None and metadata:
        row_count = safe_int(metadata.get("row_count"), 0) or 0
        latest_date = normalize_date_text(metadata.get("latest_date") or metadata.get("max_date") or "")
        first_date = normalize_date_text(metadata.get("min_date") or metadata.get("first_date") or "")
        if row_count > 0 and latest_date:
            try:
                stat = path.stat()
            except OSError:
                return None
            data_grade = str(metadata.get("data_grade") or metadata.get("grade") or "UNAVAILABLE").upper()
            entry = {
                "symbol": normalized_symbol,
                "csv_path": _relative_path_text(path),
                "latest_date": latest_date,
                "first_date": first_date or latest_date,
                "row_count": row_count,
                "file_mtime": round(float(stat.st_mtime), 6),
                "file_size": int(stat.st_size),
                "indexed_at": indexed_at or now_text(),
                "data_grade": data_grade,
                "data_grade_label": DATA_GRADE_LABELS.get(data_grade, data_grade),
                "data_grade_reason": str(metadata.get("data_grade_reason") or metadata.get("reason") or ""),
                "provider_status": str(metadata.get("provider_status") or metadata.get("provider_used") or "local_daily_cache"),
            }
    if entry is None:
        return None
    symbols[normalized_symbol] = entry
    if csv_read_warnings:
        existing_warnings = target_index.setdefault("warnings", [])
        if not isinstance(existing_warnings, list):
            existing_warnings = []
            target_index["warnings"] = existing_warnings
        for warning in csv_read_warnings:
            if warning not in existing_warnings:
                existing_warnings.append(warning)
    target_index["schema_version"] = str(target_index.get("schema_version") or "1")
    target_index["updated_at"] = indexed_at or now_text()
    target_index["symbol_count"] = len(symbols)
    target_index["path"] = str(target_index.get("path") or INDEX_PATH)
    target_index["exists"] = True
    return entry


def refresh_daily_cache_index_for_symbol(symbol: str, csv_path: str | Path | None = None) -> dict[str, Any] | None:
    index = load_daily_cache_index()
    path = Path(csv_path) if csv_path else DAILY_DIR / f"{normalize_symbol(symbol)}.csv"
    if not path.is_absolute():
        path = PROJECT_ROOT / path
    entry = upsert_daily_cache_index_entry(index, symbol, path)
    if entry is None:
        return None
    save_daily_cache_index(index)
    return entry


def build_daily_cache_index(daily_dir: str | Path | None = None) -> dict[str, Any]:
    root = Path(daily_dir) if daily_dir else DAILY_DIR
    index = _empty_index(INDEX_PATH)
    symbols = index["symbols"]
    warnings = index["warnings"]
    if not root.exists():
        index["updated_at"] = now_text()
        return index
    for csv_path in sorted(root.glob("*.csv")):
        symbol = normalize_symbol(csv_path.stem)
        if not symbol:
            continue
        frame = read_csv_frame(csv_path)
        csv_read_warnings = [
            f"daily_csv:{symbol}:{warning}"
            for warning in frame.attrs.get("warnings", [])
        ]
        for warning in csv_read_warnings:
            if warning not in warnings:
                warnings.append(warning)
        entry = _frame_entry(symbol, csv_path, frame, csv_read_warnings=csv_read_warnings)
        if entry:
            symbols[symbol] = entry
    index["symbol_count"] = len(symbols)
    index["updated_at"] = now_text()
    return index


def get_index_entry(index: Mapping[str, Any] | None, symbol: str) -> dict[str, Any] | None:
    if not isinstance(index, Mapping):
        return None
    symbols = index.get("symbols")
    if not isinstance(symbols, Mapping):
        return None
    return symbols.get(normalize_symbol(symbol)) if normalize_symbol(symbol) else None


def is_index_entry_current(entry: Mapping[str, Any] | None, csv_path: str | Path | None) -> bool:
    if not isinstance(entry, Mapping):
        return False
    path = Path(csv_path) if csv_path else Path(str(entry.get("csv_path") or ""))
    if not path.is_absolute():
        path = PROJECT_ROOT / path
    if not path.exists():
        return False
    try:
        stat = path.stat()
    except OSError:
        return False
    try:
        stored_mtime = round(float(entry.get("file_mtime")), 6)
        stored_size = int(entry.get("file_size") or 0)
    except (TypeError, ValueError):
        return False
    return round(float(stat.st_mtime), 6) == stored_mtime and int(stat.st_size) == stored_size


def classify_cache_freshness_from_index(
    entry: Mapping[str, Any] | None,
    expected_latest_date: object,
    min_history_rows: int,
) -> dict[str, Any]:
    expected_text = normalize_date_text(expected_latest_date)
    if not isinstance(entry, Mapping) or not entry:
        return {
            "status": "missing_entry",
            "cache_fresh": False,
            "history_short": False,
            "row_count": 0,
            "latest_date": "",
            "first_date": "",
            "expected_latest_date": expected_text,
            "reason": "missing index entry",
        }
    csv_text = str(entry.get("csv_path") or "")
    csv_path = Path(csv_text) if csv_text else None
    if csv_path is not None and not csv_path.is_absolute():
        csv_path = PROJECT_ROOT / csv_path
    if csv_path is None or not csv_path.exists():
        return {
            "status": "missing_file",
            "cache_fresh": False,
            "history_short": False,
            "row_count": safe_int(entry.get("row_count"), 0) or 0,
            "latest_date": normalize_date_text(entry.get("latest_date") or ""),
            "first_date": normalize_date_text(entry.get("first_date") or ""),
            "expected_latest_date": expected_text,
            "reason": "missing local csv file",
        }
    if not is_index_entry_current(entry, csv_path):
        return {
            "status": "stale_entry",
            "cache_fresh": False,
            "history_short": False,
            "row_count": safe_int(entry.get("row_count"), 0) or 0,
            "latest_date": normalize_date_text(entry.get("latest_date") or ""),
            "first_date": normalize_date_text(entry.get("first_date") or ""),
            "expected_latest_date": expected_text,
            "reason": "index entry does not match local csv metadata",
        }
    row_count = safe_int(entry.get("row_count"), 0) or 0
    latest_text = normalize_date_text(entry.get("latest_date") or "")
    first_text = normalize_date_text(entry.get("first_date") or "")
    history_short = bool(min_history_rows and row_count > 0 and row_count < min_history_rows)
    if expected_text and latest_text and latest_text < expected_text:
        return {
            "status": "stale_date",
            "cache_fresh": False,
            "history_short": history_short,
            "row_count": row_count,
            "latest_date": latest_text,
            "first_date": first_text,
            "expected_latest_date": expected_text,
            "reason": "latest local date is older than expected latest trading day",
        }
    if history_short:
        return {
            "status": "history_short",
            "cache_fresh": True,
            "history_short": True,
            "row_count": row_count,
            "latest_date": latest_text,
            "first_date": first_text,
            "expected_latest_date": expected_text,
            "reason": "cache is fresh but history is short",
        }
    return {
        "status": "fresh",
        "cache_fresh": True,
        "history_short": False,
        "row_count": row_count,
        "latest_date": latest_text,
        "first_date": first_text,
        "expected_latest_date": expected_text,
        "reason": "cache is fresh and history is sufficient",
    }


def summarize_daily_cache_index(index: Mapping[str, Any] | None) -> dict[str, Any]:
    if not isinstance(index, Mapping):
        return {
            "exists": False,
            "path": str(INDEX_PATH),
            "symbol_count": 0,
            "latest_indexed_at": "",
            "latest_date_distribution": {},
            "data_grade_distribution": {},
            "min_date": "",
            "max_date": "",
        }
    symbols = index.get("symbols") if isinstance(index.get("symbols"), Mapping) else {}
    latest_date_distribution: dict[str, int] = {}
    data_grade_distribution: dict[str, int] = {}
    min_date = ""
    max_date = ""
    latest_indexed_at = str(index.get("updated_at") or "")
    for entry in symbols.values():
        if not isinstance(entry, Mapping):
            continue
        latest_date = str(entry.get("latest_date") or "")
        if latest_date:
            latest_date_distribution[latest_date] = latest_date_distribution.get(latest_date, 0) + 1
            min_date = latest_date if not min_date or latest_date < min_date else min_date
            max_date = latest_date if not max_date or latest_date > max_date else max_date
        data_grade = str(entry.get("data_grade") or "").upper()
        if data_grade:
            data_grade_distribution[data_grade] = data_grade_distribution.get(data_grade, 0) + 1
    return {
        "exists": bool(index.get("exists", True)),
        "path": str(index.get("path") or INDEX_PATH),
        "symbol_count": len(symbols),
        "latest_indexed_at": latest_indexed_at,
        "latest_date_distribution": latest_date_distribution,
        "data_grade_distribution": data_grade_distribution,
        "min_date": min_date,
        "max_date": max_date,
    }
