"""Read recorded paper-account observations; never reconstruct missing trades.

An account snapshot is evidence of a recorded value, not proof that its
underlying accounting, corporate actions or cash flows are complete. Benchmark
files contain index points and are not treated as adjusted stock prices.
"""
from __future__ import annotations

import csv
import hashlib
import io
import math
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, Mapping, Sequence


def finite_number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return result if math.isfinite(result) else None


def iso_date(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    try:
        parsed = date.fromisoformat(value)
    except ValueError:
        return ""
    return parsed.isoformat() if value == parsed.isoformat() else ""


@dataclass(frozen=True)
class RecordedPoint:
    date: str
    cash: float
    market_value: float
    equity: float
    review_valid: bool | None


@dataclass(frozen=True)
class HistoryEvidence:
    points: list[RecordedPoint]
    undated_count: int
    invalid_count: int
    conflicting_dates: list[str]
    duplicate_count: int
    invalid_dates: list[str]


def observed_history(history: Mapping[str, Any]) -> HistoryEvidence:
    """Use explicit valuation dates and balanced observed totals only."""
    rows = history.get("items")
    if not isinstance(rows, list):
        raise ValueError("history.items must be a list")
    by_date: dict[str, RecordedPoint] = {}
    conflicts: set[str] = set()
    invalid_dates: set[str] = set()
    undated = invalid = duplicates = 0
    for row in rows:
        if not isinstance(row, dict):
            invalid += 1
            continue
        day = iso_date(row.get("valuation_date"))
        if not day:
            undated += 1
            continue
        cash = finite_number(row.get("cash"))
        market = finite_number(row.get("market_value"))
        equity = finite_number(row.get("total_equity"))
        if (cash is None or market is None or equity is None
                or equity <= 0 or market < 0 or abs(cash + market - equity) > 0.015):
            invalid += 1
            # A contradictory row cannot be silently discarded in favour of a
            # convenient same-day value.
            invalid_dates.add(day)
            continue
        valid_flag = row.get("portfolio_review_valid")
        review_valid = valid_flag if isinstance(valid_flag, bool) else None
        point = RecordedPoint(day, cash, market, equity, review_valid)
        if day in by_date:
            duplicates += 1
            if by_date[day] != point:
                conflicts.add(day)
        else:
            by_date[day] = point
    conflicts.update(invalid_dates.intersection(by_date))
    points = [by_date[day] for day in sorted(by_date) if day not in conflicts]
    return HistoryEvidence(points, undated, invalid, sorted(conflicts), duplicates, sorted(invalid_dates))


@dataclass(frozen=True)
class IndexEvidence:
    values: dict[str, float]
    conflicts: set[str]
    status: str
    sha256: str


def read_index_series(path: Path) -> IndexEvidence:
    """Read local index points, retaining ambiguous duplicate dates."""
    try:
        content = path.read_bytes()
        decoded = content.decode("utf-8-sig")
        reader = csv.DictReader(io.StringIO(decoded))
        if not reader.fieldnames or not {"date", "close"}.issubset(reader.fieldnames):
            return IndexEvidence({}, set(), "missing_columns", hashlib.sha256(content).hexdigest())
        values: dict[str, float] = {}
        conflicts: set[str] = set()
        for row in reader:
            day = iso_date(row.get("date"))
            if not day:
                continue
            close = finite_number(row.get("close"))
            if close is None or close <= 0:
                conflicts.add(day)
            elif day in values and values[day] != close:
                conflicts.add(day)
            else:
                values[day] = close
        return IndexEvidence(values, conflicts, "available", hashlib.sha256(content).hexdigest())
    except FileNotFoundError:
        return IndexEvidence({}, set(), "missing_file", "")
    except (OSError, UnicodeError, csv.Error):
        return IndexEvidence({}, set(), "unreadable_file", "")


def compare_observed_endpoints(points: Sequence[RecordedPoint], index: IndexEvidence) -> dict[str, Any]:
    """Compare exact shared endpoints, never silently shortening the period."""
    if len(points) < 2:
        return {"available": False, "reason": "insufficient_dated_observations"}
    start, end = points[0], points[-1]
    if index.status != "available":
        return {"available": False, "reason": index.status}
    if start.date in index.conflicts or end.date in index.conflicts:
        return {"available": False, "reason": "endpoint_conflict"}
    if start.date not in index.values or end.date not in index.values:
        return {"available": False, "reason": "endpoint_missing"}
    first, last = index.values[start.date], index.values[end.date]
    return {
        "available": True, "start": start.date, "end": end.date,
        "recorded_equity_change_pct": round((end.equity / start.equity - 1) * 100, 2),
        "index_start": first, "index_end": last,
        "index_change_pct": round((last / first - 1) * 100, 2),
    }
