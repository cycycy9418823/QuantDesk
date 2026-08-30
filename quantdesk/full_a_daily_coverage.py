"""Read-only coverage facts for the current A-share daily-data universe.

This module deliberately measures the intersection of the current universe
manifest and canonical ``data/daily`` files.  It does *not* use the raw number
of CSV files in the directory: that directory also retains delisted, renamed
and archive-only files, which must never be presented as current-market
coverage.

The helper performs no network access and no writes.  Callers that need a
fresh full-universe snapshot must opt in to this bounded local scan; the 8000
GET route reads the persisted result rather than rescanning thousands of CSVs.
"""

from __future__ import annotations

import csv
from collections import Counter
from pathlib import Path
from typing import Any


def _date_text(value: Any) -> str:
    text = str(value or "").strip()
    if len(text) >= 10 and text[4] in {"-", "/"}:
        return text[:10].replace("/", "-")
    return ""


def _read_universe(path: Path) -> list[str]:
    """Return the current, de-duplicated six-digit universe in file order."""

    symbols: list[str] = []
    seen: set[str] = set()
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            symbol = str(row.get("symbol") or row.get("code") or "").strip()
            if len(symbol) == 6 and symbol.isdigit() and symbol not in seen:
                seen.add(symbol)
                symbols.append(symbol)
    return symbols


def _last_date_and_rows(path: Path) -> tuple[str, int, bool]:
    """Return max daily date, row count and duplicate-date evidence for one CSV."""

    dates: list[str] = []
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.reader(handle)
        try:
            header = next(reader)
        except StopIteration:
            return "", 0, False
        names = [str(column).strip().lower() for column in header]
        try:
            date_index = names.index("date")
        except ValueError:
            try:
                date_index = names.index("trade_date")
            except ValueError:
                date_index = 0
        for row in reader:
            if len(row) <= date_index:
                continue
            day = _date_text(row[date_index])
            if day:
                dates.append(day)
    if not dates:
        return "", 0, False
    return max(dates), len(dates), len(set(dates)) != len(dates)


def build_full_a_daily_coverage(
    *,
    target_date: str,
    universe_path: Path,
    daily_dir: Path,
) -> dict[str, Any]:
    """Inspect current-universe daily coverage without fetching or writing.

    ``coverage_floor_date`` is the oldest latest date among current symbols with
    a readable daily file.  It is deliberately separate from
    ``latest_date_max`` and ``at_target_date_count`` so one up-to-date symbol
    cannot hide an out-of-date universe.
    """

    target = _date_text(target_date)
    universe = _read_universe(Path(universe_path))
    universe_set = set(universe)
    present: list[str] = []
    missing: list[str] = []
    stale: list[str] = []
    invalid: list[str] = []
    duplicate: list[str] = []
    latest_by_symbol: dict[str, str] = {}

    for symbol in universe:
        path = Path(daily_dir) / f"{symbol}.csv"
        if not path.is_file():
            missing.append(symbol)
            continue
        try:
            latest, row_count, has_duplicate = _last_date_and_rows(path)
        except (OSError, UnicodeError, csv.Error) as exc:
            invalid.append(f"{symbol}:{type(exc).__name__}")
            continue
        if not latest or not row_count:
            invalid.append(f"{symbol}:empty_or_no_date")
            continue
        present.append(symbol)
        latest_by_symbol[symbol] = latest
        if has_duplicate:
            duplicate.append(symbol)
        if target and latest < target:
            stale.append(symbol)

    dates = Counter(latest_by_symbol.values())
    present_dates = sorted(dates)
    on_disk = {
        path.stem
        for path in Path(daily_dir).glob("*.csv")
        if len(path.stem) == 6 and path.stem.isdigit()
    }
    at_target = (
        sorted(symbol for symbol, latest in latest_by_symbol.items() if latest >= target)
        if target
        else []
    )
    dominant_date = ""
    if dates:
        # Stable tie-break: newest date wins only when counts are equal.
        dominant_date = max(dates, key=lambda day: (dates[day], day))

    return {
        "task_type": "full_a_daily_coverage",
        "target_date": target,
        "universe_source": str(Path(universe_path)).replace("\\", "/"),
        "daily_dir": str(Path(daily_dir)).replace("\\", "/"),
        "universe_total": len(universe),
        "canonical_intersection": len(present),
        "coverage_text": f"{len(present)}/{len(universe)}",
        "at_target_date_count": len(at_target),
        "at_target_date_text": f"{len(at_target)}/{len(universe)}",
        "coverage_floor_date": present_dates[0] if present_dates else "",
        "latest_date_max": present_dates[-1] if present_dates else "",
        "dominant_latest_date": dominant_date,
        "dominant_latest_date_count": int(dates.get(dominant_date, 0)),
        "latest_date_counts": dict(sorted(dates.items())),
        "missing_count": len(missing),
        "missing_symbols_sample": missing[:50],
        "stale_count": len(stale),
        "stale_symbols_sample": stale[:50],
        "invalid_count": len(invalid),
        "invalid_symbols_sample": invalid[:50],
        "duplicate_count": len(duplicate),
        "duplicate_date_symbols_sample": duplicate[:50],
        "extra_files_outside_universe": len(on_disk - universe_set),
        "extra_files_sample": sorted(on_disk - universe_set)[:50],
        "network": False,
        "read_only": True,
        "note": (
            "覆盖率为当前 a_share_symbols 名册与 canonical data/daily 的交集；"
            "目录内归档或退市 CSV 不计为当前全量覆盖。"
        ),
    }


__all__ = ["build_full_a_daily_coverage"]
