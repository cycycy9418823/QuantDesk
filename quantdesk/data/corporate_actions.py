from __future__ import annotations

import csv
from pathlib import Path
from typing import Any


def _load_csv(path: str | Path) -> list[dict[str, Any]]:
    target = Path(path)
    if not target.exists():
        return []
    with target.open("r", encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def load_dividends(path: str | Path) -> list[dict]:
    return _load_csv(path)


def load_splits(path: str | Path) -> list[dict]:
    return _load_csv(path)


def validate_corporate_actions(dividends: list[dict], splits: list[dict]) -> dict:
    warnings = []
    for name, rows, required in (
        ("dividends", dividends, {"symbol", "date", "cash_dividend", "stock_dividend", "source"}),
        ("splits", splits, {"symbol", "date", "split_ratio", "source"}),
    ):
        for idx, row in enumerate(rows):
            missing = required.difference(row)
            if missing:
                warnings.append(f"{name}_{idx}_missing_{'_'.join(sorted(missing))}")
    return {"ok": not warnings, "status": "passed" if not warnings else "warning", "warnings": warnings, "dividend_count": len(dividends), "split_count": len(splits)}


def detect_price_jump_candidates(daily_rows: list[dict]) -> dict:
    jumps = []
    prev = None
    for row in daily_rows:
        try:
            close = float(row.get("close") or row.get("adj_close") or 0)
        except (TypeError, ValueError):
            continue
        if prev and prev > 0 and abs(close / prev - 1) > 0.2:
            jumps.append({"date": row.get("date"), "prev_close": prev, "close": close, "jump_pct": close / prev - 1})
        prev = close
    return {"jump_count": len(jumps), "price_jump_candidates": jumps, "status": "warning" if jumps else "passed"}
