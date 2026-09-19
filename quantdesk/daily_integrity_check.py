"""Read-only integrity inspection for canonical daily price CSVs.

Detects the physical corruption class that silently blocks daily appends
(NUL-only inserted records, column-width mismatches, duplicate or
non-canonical dates, symbol/header mismatches). This module never writes,
fetches, repairs, or deletes anything.
"""

from __future__ import annotations

import csv
import io
from datetime import date
from pathlib import Path
from typing import Any, Iterable, Sequence

DAILY_COLUMNS: tuple[str, ...] = (
    "date", "symbol", "open", "high", "low", "close", "volume", "amount",
    "change", "pct_chg", "adjust", "source", "updated_at",
)

SCHEMA_VERSION = "daily_integrity_check.v1"

_ISSUE_ORDER = (
    "empty_file",
    "nul_bytes",
    "csv_unreadable",
    "header_mismatch",
    "column_mismatch",
    "symbol_mismatch",
    "noncanonical_date",
    "unordered_dates",
    "duplicate_dates",
)


def _canonical_date(value: str) -> bool:
    if len(value) != 10:
        return False
    try:
        return date.fromisoformat(value).isoformat() == value
    except ValueError:
        return False


def inspect_daily_file(
    path: Path,
    *,
    columns: Sequence[str] = DAILY_COLUMNS,
    expected_symbol: str | None = None,
) -> dict[str, Any]:
    """Inspect one daily CSV without mutating it. Always returns a payload."""

    symbol = expected_symbol or path.stem
    payload: dict[str, Any] = {
        "symbol": symbol,
        "file_name": path.name,
        "readable": False,
        "size_bytes": 0,
        "nul_byte_count": 0,
        "nul_record_lines": [],
        "row_count": 0,
        "last_date": "",
        "issue_codes": [],
    }
    try:
        raw = path.read_bytes()
    except OSError:
        payload["issue_codes"] = ["unreadable_file"]
        return payload
    payload["readable"] = True
    payload["size_bytes"] = len(raw)
    payload["nul_byte_count"] = raw.count(b"\x00")

    issues: list[str] = []
    if not raw:
        payload["issue_codes"] = ["empty_file"]
        return payload
    if payload["nul_byte_count"]:
        issues.append("nul_bytes")

    nul_lines: list[int] = []
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        issues.append("csv_unreadable")
        payload["issue_codes"] = sorted(set(issues), key=_ISSUE_ORDER.index)
        return payload

    try:
        rows = list(csv.reader(io.StringIO(text)))
    except csv.Error:
        issues.append("csv_unreadable")
        payload["issue_codes"] = sorted(set(issues), key=_ISSUE_ORDER.index)
        return payload

    if not rows or not any(cell.strip() for cell in rows[0]):
        issues.append("header_mismatch")
    elif tuple(cell.strip() for cell in rows[0]) != tuple(columns):
        issues.append("header_mismatch")

    data_rows = rows[1:]
    payload["row_count"] = len(data_rows)
    for offset, values in enumerate(data_rows, start=2):
        if len(values) != len(columns):
            issues.append("column_mismatch")
        if values and not values[0].strip():
            continue
        if len(values) != len(columns):
            continue
        row = dict(zip(columns, values))
        if not _canonical_date(row["date"]):
            issues.append("noncanonical_date")
        if row["symbol"].strip() != symbol:
            issues.append("symbol_mismatch")

    for number, line in enumerate(raw.splitlines(), start=1):
        body = line.rstrip(b"\r\n")
        if body and not body.strip(b"\x00"):
            nul_lines.append(number)
    payload["nul_record_lines"] = nul_lines

    dates = [
        values[0].strip()
        for values in data_rows
        if len(values) == len(columns) and _canonical_date(values[0].strip())
    ]
    payload["last_date"] = max(dates) if dates else ""
    if len(set(dates)) != len(dates):
        issues.append("duplicate_dates")
    if dates != sorted(dates):
        issues.append("unordered_dates")

    ordered = {code for code in issues if code in _ISSUE_ORDER}
    payload["issue_codes"] = [code for code in _ISSUE_ORDER if code in ordered]
    if "column_mismatch" not in ordered and nul_lines:
        payload["issue_codes"] = [code for code in _ISSUE_ORDER if code in (ordered | {"column_mismatch"})]
    return payload


def run_daily_integrity_check(
    daily_dir: str | Path,
    *,
    universe_symbols: Iterable[str] | None = None,
    columns: Sequence[str] = DAILY_COLUMNS,
    limit: int = 0,
    sample_limit: int = 50,
    stale_before: str = "",
) -> dict[str, Any]:
    """Scan a daily directory read-only and summarize every physical issue."""

    directory = Path(daily_dir).expanduser()
    files = sorted(directory.glob("*.csv"))
    if limit and limit > 0:
        files = files[:limit]

    flagged: list[dict[str, Any]] = []
    clean = 0
    issue_counts: dict[str, int] = {}
    present_symbols: set[str] = set()
    for path in files:
        present_symbols.add(path.stem)
        payload = inspect_daily_file(path, columns=columns)
        if payload["issue_codes"]:
            flagged.append({
                "symbol": payload["symbol"],
                "issue_codes": payload["issue_codes"],
                "nul_byte_count": payload["nul_byte_count"],
                "row_count": payload["row_count"],
                "last_date": payload["last_date"],
                "size_bytes": payload["size_bytes"],
            })
            for code in payload["issue_codes"]:
                issue_counts[code] = issue_counts.get(code, 0) + 1
        else:
            clean += 1

    missing: list[str] = []
    if universe_symbols is not None:
        expected = {str(item).strip() for item in universe_symbols if str(item).strip()}
        missing = sorted(expected - present_symbols)

    stale: list[str] = []
    if stale_before:
        for path in files:
            if path.stem in {item["symbol"] for item in flagged}:
                continue
            payload = inspect_daily_file(path, columns=columns)
            last = str(payload.get("last_date") or "")
            if not last or last < stale_before:
                stale.append(path.stem)

    return {
        "schema_version": SCHEMA_VERSION,
        "daily_dir": str(directory),
        "scanned_files": len(files),
        "clean_files": clean,
        "flagged_files": len(flagged),
        "issue_counts": {code: issue_counts[code] for code in _ISSUE_ORDER if code in issue_counts},
        "flagged_sample": flagged[: max(0, int(sample_limit))],
        "missing_universe_files": missing[: max(0, int(sample_limit))],
        "missing_universe_count": len(missing),
        "stale_sample": sorted(stale)[: max(0, int(sample_limit))],
        "stale_count": len(stale),
        "stale_before": stale_before,
        "ok": not flagged and not missing,
        "read_only": True,
        "canonical_write": False,
        "network_effective": False,
    }


def load_universe_symbols(path: str | Path, *, column: str = "symbol") -> list[str]:
    """Read six-digit symbols from a universe CSV read-only."""

    target = Path(path).expanduser()
    if not target.is_file():
        return []
    symbols: list[str] = []
    with target.open("r", encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            value = str(row.get(column) or row.get("code") or "").strip()
            if len(value) == 6 and value.isdigit() and value not in symbols:
                symbols.append(value)
    return symbols


__all__ = [
    "DAILY_COLUMNS",
    "SCHEMA_VERSION",
    "inspect_daily_file",
    "run_daily_integrity_check",
    "load_universe_symbols",
]
