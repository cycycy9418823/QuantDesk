from __future__ import annotations

import csv
import math
from datetime import datetime
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[2]
PARTIAL_DAILY_COLUMNS = [
    "symbol",
    "date",
    "open",
    "high_so_far",
    "low_so_far",
    "last_price",
    "volume_so_far",
    "amount_so_far",
    "prev_close",
    "change_pct",
    "snapshot_time",
    "source",
    "is_complete",
]


def _project_path(path: str | Path) -> Path:
    target = Path(path)
    return target if target.is_absolute() else PROJECT_ROOT / target


def _display_path(path: str | Path) -> str:
    target = _project_path(path)
    try:
        return str(target.relative_to(PROJECT_ROOT)).replace("\\", "/")
    except Exception:
        return str(target).replace("\\", "/")


def _safe_float(value: Any, default: float | None = None) -> float | None:
    if value in (None, ""):
        return default
    try:
        number = float(value)
    except Exception:
        return default
    return number if math.isfinite(number) else default


def _safe_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {"1", "true", "yes", "y", "on"}


def _trade_date(value: str | None = None) -> str:
    return value or datetime.now().strftime("%Y-%m-%d")


def _snapshot_time(value: str | None = None) -> str:
    return value or datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def build_partial_daily_row(
    quote: dict[str, Any],
    trade_date: str | None = None,
    snapshot_time: str | None = None,
) -> dict[str, Any]:
    date = _trade_date(trade_date or str(quote.get("date") or "") or None)
    ts = _snapshot_time(snapshot_time or str(quote.get("snapshot_time") or quote.get("time") or "") or None)
    last_price = _safe_float(quote.get("last_price", quote.get("price")), 0.0) or 0.0
    prev_close = _safe_float(quote.get("prev_close"), None)
    change_pct = _safe_float(quote.get("change_pct"), None)
    if change_pct is None and prev_close and prev_close != 0:
        change_pct = (last_price / prev_close - 1.0) * 100.0
    high = _safe_float(quote.get("high_so_far", quote.get("high")), last_price) or 0.0
    low = _safe_float(quote.get("low_so_far", quote.get("low")), last_price) or 0.0
    return {
        "symbol": str(quote.get("symbol") or "").strip(),
        "date": date,
        "open": _safe_float(quote.get("open"), None),
        "high_so_far": high,
        "low_so_far": low,
        "last_price": last_price,
        "volume_so_far": _safe_float(quote.get("volume_so_far", quote.get("volume")), 0.0) or 0.0,
        "amount_so_far": _safe_float(quote.get("amount_so_far", quote.get("amount")), 0.0) or 0.0,
        "prev_close": prev_close,
        "change_pct": change_pct,
        "snapshot_time": ts,
        "source": str(quote.get("source") or "local_or_synthetic"),
        "is_complete": False,
    }


def validate_partial_daily_rows(rows: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[str], list[str]]:
    normalized: list[dict[str, Any]] = []
    warnings: list[str] = []
    errors: list[str] = []
    for index, row in enumerate(rows):
        item = dict(row or {})
        if _safe_bool(item.get("is_complete")):
            errors.append(f"partial_daily_row_complete_rejected:{item.get('symbol') or index}")
            continue
        if not item.get("symbol"):
            warnings.append(f"missing_symbol:{index}")
        if not item.get("snapshot_time"):
            warnings.append(f"missing_snapshot_time:{item.get('symbol') or index}")
            item["snapshot_time"] = _snapshot_time()
        if not item.get("date"):
            item["date"] = _trade_date()
        item["is_complete"] = False
        for key in ("open", "high_so_far", "low_so_far", "last_price", "volume_so_far", "amount_so_far", "prev_close", "change_pct"):
            item[key] = _safe_float(item.get(key), None)
        if item.get("last_price") is None:
            warnings.append(f"missing_last_price:{item.get('symbol') or index}")
            item["last_price"] = 0.0
        if item.get("high_so_far") is None:
            warnings.append(f"missing_high_so_far:{item.get('symbol') or index}")
            item["high_so_far"] = item.get("last_price") or 0.0
        if item.get("low_so_far") is None:
            warnings.append(f"missing_low_so_far:{item.get('symbol') or index}")
            item["low_so_far"] = item.get("last_price") or 0.0
        normalized.append({key: item.get(key) for key in PARTIAL_DAILY_COLUMNS})
    return normalized, list(dict.fromkeys(warnings)), list(dict.fromkeys(errors))


def write_partial_daily_snapshot(
    rows: list[dict[str, Any]],
    trade_date: str,
    path: str | Path | None = None,
) -> dict[str, Any]:
    normalized, warnings, errors = validate_partial_daily_rows(rows)
    target = _project_path(path or Path("data/intraday/partial_daily") / f"{trade_date}.csv")
    if errors:
        return {"ok": False, "status": "failed", "path": _display_path(target), "row_count": 0, "warnings": warnings, "errors": errors, "is_complete": False}
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=PARTIAL_DAILY_COLUMNS)
        writer.writeheader()
        writer.writerows(normalized)
    return {"ok": True, "status": "passed" if not warnings else "warning", "path": _display_path(target), "row_count": len(normalized), "warnings": warnings, "errors": [], "is_complete": False}


def load_partial_daily_snapshot(trade_date: str, path: str | Path | None = None) -> tuple[list[dict[str, Any]], list[str]]:
    target = _project_path(path or Path("data/intraday/partial_daily") / f"{trade_date}.csv")
    if not target.exists():
        return [], [f"missing_partial_daily_snapshot:{_display_path(target)}"]
    with target.open("r", encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))
    normalized, warnings, errors = validate_partial_daily_rows(rows)
    return normalized, warnings + errors


def write_spot_snapshot(
    rows: list[dict[str, Any]],
    trade_date: str,
    snapshot_time: str | None = None,
    path: str | Path | None = None,
) -> dict[str, Any]:
    ts = _snapshot_time(snapshot_time)
    stamp = ts.replace("-", "").replace(":", "").replace(" ", "_")
    target = _project_path(path or Path("data/intraday/spot_snapshots") / trade_date / f"{stamp}.csv")
    target.parent.mkdir(parents=True, exist_ok=True)
    columns = sorted({str(key) for row in rows for key in row.keys()} | {"symbol", "snapshot_time"})
    with target.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns)
        writer.writeheader()
        for row in rows:
            item = dict(row)
            item.setdefault("snapshot_time", ts)
            writer.writerow({key: item.get(key) for key in columns})
    return {"ok": True, "status": "passed", "path": _display_path(target), "row_count": len(rows), "snapshot_time": ts}
