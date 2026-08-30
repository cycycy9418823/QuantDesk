from __future__ import annotations

import csv
from pathlib import Path
from typing import Any


REQUIRED_DAILY_COLUMNS = ["date", "symbol", "open", "high", "low", "close", "volume"]


def validate_daily_rows(rows: list[dict[str, Any]], expected_date: str) -> dict[str, Any]:
    errors: list[str] = []
    for index, row in enumerate(rows):
        missing = [col for col in REQUIRED_DAILY_COLUMNS if col not in row or row.get(col) in {"", None}]
        if missing:
            errors.append(f"row_{index}_missing:{','.join(missing)}")
        if str(row.get("date") or "") != expected_date:
            errors.append(f"row_{index}_date_mismatch:{row.get('date')}")
    return {"status": "passed" if not errors else "failed_validation", "schema_errors": errors, "row_count": len(rows)}


def read_last_date(path: Path) -> str:
    if not path.exists():
        return ""
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            rows = list(csv.DictReader(handle))
        return str(rows[-1].get("date") or "") if rows else ""
    except Exception:
        return ""


def validate_file_no_mismatch(path: Path) -> dict[str, Any]:
    errors: list[str] = []
    row_count = 0
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            reader = csv.reader(handle)
            header = next(reader, [])
            width = len(header)
            for index, row in enumerate(reader, start=2):
                if not row:
                    continue
                row_count += 1
                if len(row) != width:
                    errors.append(f"line_{index}_width_{len(row)}_expected_{width}")
    except Exception as exc:
        errors.append(repr(exc))
    return {"status": "passed" if not errors else "failed_validation", "row_count": row_count, "schema_errors": errors}
