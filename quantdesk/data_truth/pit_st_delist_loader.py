"""Point-in-time ST / delisting data loader (Phase 3 退市链路).

The historical ST/*ST and delisting registries in this project are placeholders
that always return empty (``build_st_registry`` / ``build_delist_registry``):
they wait for an external, verified data source.  Until that source exists, the
honest behaviour is *not* to pretend there is no ST/delisting risk, but to load
whatever point-in-time data the user has dropped into the local metadata folder
and, when it is absent, mark coverage as **explicitly unknown** so downstream
code can refuse to claim survivorship-bias safety.

This module is the single, dependency-light entry point for that data:

- :func:`load_st_intervals` reads ``data/metadata/st_history.csv``
  (schema: ``symbol, name, st_start_date, st_end_date, st_type``).
- :func:`load_delist_records` reads ``data/metadata/delist_history.csv``
  (schema: ``symbol, name, delist_date, last_trade_date, delist_reason``).
- :func:`is_st_on` / :func:`is_delisted_on` answer point-in-time questions
  (an open ``st_end_date`` means "still ST as of any later date").
- :func:`coverage_status` reports whether real data is present so callers can
  decide whether ST/delist filtering is trustworthy.

Read-only.  No network, no broker, not investment advice.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from quantdesk.shared import normalize_symbol

PROJECT_ROOT = Path(__file__).resolve().parents[2]
METADATA_DIR = PROJECT_ROOT / "data" / "metadata"
ST_HISTORY_CSV = METADATA_DIR / "st_history.csv"
DELIST_HISTORY_CSV = METADATA_DIR / "delist_history.csv"

ST_SCHEMA = ("symbol", "name", "st_start_date", "st_end_date", "st_type")
DELIST_SCHEMA = ("symbol", "name", "delist_date", "last_trade_date", "delist_reason")


def _norm_date(value: Any) -> str:
    """Normalize a date-ish value to ``YYYY-MM-DD`` text (empty if unparseable)."""
    text = str(value or "").strip()
    if not text:
        return ""
    text = text.replace("/", "-").replace(".", "-")
    # Accept compact YYYYMMDD too.
    if len(text) == 8 and text.isdigit():
        return f"{text[:4]}-{text[4:6]}-{text[6:]}"
    return text[:10]


@dataclass(frozen=True)
class StInterval:
    symbol: str
    name: str
    st_start_date: str
    st_end_date: str  # "" means open / still ST
    st_type: str

    def covers(self, as_of: str) -> bool:
        day = _norm_date(as_of)
        if not day or not self.st_start_date:
            return False
        if day < self.st_start_date:
            return False
        if self.st_end_date and day > self.st_end_date:
            return False
        return True


@dataclass(frozen=True)
class DelistRecord:
    symbol: str
    name: str
    delist_date: str
    last_trade_date: str  # "" -> fall back to delist_date
    delist_reason: str

    @property
    def effective_last_trade_date(self) -> str:
        return self.last_trade_date or self.delist_date

    def is_delisted_on(self, as_of: str) -> bool:
        day = _norm_date(as_of)
        if not day or not self.delist_date:
            return False
        return day >= self.delist_date


@dataclass
class StDelistData:
    st_intervals: dict[str, list[StInterval]] = field(default_factory=dict)
    delist_records: dict[str, DelistRecord] = field(default_factory=dict)
    st_source: str = "absent"
    delist_source: str = "absent"

    # --- point-in-time queries -------------------------------------------
    def is_st_on(self, symbol: str, as_of: str) -> bool:
        for interval in self.st_intervals.get(normalize_symbol(symbol), []):
            if interval.covers(as_of):
                return True
        return False

    def is_delisted_on(self, symbol: str, as_of: str) -> bool:
        record = self.delist_records.get(normalize_symbol(symbol))
        return bool(record and record.is_delisted_on(as_of))

    def delist_record(self, symbol: str) -> DelistRecord | None:
        return self.delist_records.get(normalize_symbol(symbol))

    def has_st_coverage(self) -> bool:
        return self.st_source != "absent" and bool(self.st_intervals)

    def has_delist_coverage(self) -> bool:
        return self.delist_source != "absent" and bool(self.delist_records)


def _read_csv_rows(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            return [dict(row) for row in csv.DictReader(handle)]
    except OSError:
        return []


def load_st_intervals(path: Path | None = None) -> tuple[dict[str, list[StInterval]], str]:
    """Load ST intervals from local CSV. Returns (mapping, source)."""
    target = path or ST_HISTORY_CSV
    rows = _read_csv_rows(target)
    if not rows:
        return {}, "absent"
    out: dict[str, list[StInterval]] = {}
    for row in rows:
        symbol = normalize_symbol(row.get("symbol") or row.get("code") or row.get("ts_code"))
        if not symbol:
            continue
        interval = StInterval(
            symbol=symbol,
            name=str(row.get("name") or "").strip(),
            st_start_date=_norm_date(row.get("st_start_date") or row.get("start_date")),
            st_end_date=_norm_date(row.get("st_end_date") or row.get("end_date")),
            st_type=str(row.get("st_type") or row.get("type") or "ST").strip(),
        )
        if not interval.st_start_date:
            continue
        out.setdefault(symbol, []).append(interval)
    return out, ("local_csv" if out else "absent")


def load_delist_records(path: Path | None = None) -> tuple[dict[str, DelistRecord], str]:
    """Load delisting records from local CSV. Returns (mapping, source)."""
    target = path or DELIST_HISTORY_CSV
    rows = _read_csv_rows(target)
    if not rows:
        return {}, "absent"
    out: dict[str, DelistRecord] = {}
    for row in rows:
        symbol = normalize_symbol(row.get("symbol") or row.get("code") or row.get("ts_code"))
        if not symbol:
            continue
        delist_date = _norm_date(row.get("delist_date") or row.get("delisting_date"))
        if not delist_date:
            continue
        out[symbol] = DelistRecord(
            symbol=symbol,
            name=str(row.get("name") or "").strip(),
            delist_date=delist_date,
            last_trade_date=_norm_date(row.get("last_trade_date")),
            delist_reason=str(row.get("delist_reason") or row.get("reason") or "").strip(),
        )
    return out, ("local_csv" if out else "absent")


def load_st_delist_data(
    *,
    st_path: Path | None = None,
    delist_path: Path | None = None,
) -> StDelistData:
    """Load both ST and delist data into a single queryable object."""
    st_map, st_source = load_st_intervals(st_path)
    delist_map, delist_source = load_delist_records(delist_path)
    return StDelistData(
        st_intervals=st_map,
        delist_records=delist_map,
        st_source=st_source,
        delist_source=delist_source,
    )


def coverage_status(data: StDelistData | None = None) -> dict[str, Any]:
    """Report whether real ST/delist data is available.

    When coverage is absent the caller MUST treat ST/delist filtering as
    *unverified* (survivorship bias not handled) rather than assuming safety.
    """
    data = data or load_st_delist_data()
    st_ok = data.has_st_coverage()
    delist_ok = data.has_delist_coverage()
    return {
        "st_coverage": "local_csv" if st_ok else "absent",
        "delist_coverage": "local_csv" if delist_ok else "absent",
        "st_symbols": len(data.st_intervals),
        "delist_symbols": len(data.delist_records),
        "survivorship_bias_handled": bool(st_ok and delist_ok),
        "trustworthy_for_filtering": bool(st_ok or delist_ok),
        "note": (
            "Real point-in-time ST/delist data loaded from data/metadata/."
            if (st_ok or delist_ok)
            else "No local ST/delist data present; ST/delist risk is UNVERIFIED. "
            "Drop data/metadata/st_history.csv and delist_history.csv to enable."
        ),
        "expected_st_schema": list(ST_SCHEMA),
        "expected_delist_schema": list(DELIST_SCHEMA),
    }


__all__ = [
    "DELIST_HISTORY_CSV",
    "DELIST_SCHEMA",
    "DelistRecord",
    "ST_HISTORY_CSV",
    "ST_SCHEMA",
    "StDelistData",
    "StInterval",
    "coverage_status",
    "load_delist_records",
    "load_st_delist_data",
    "load_st_intervals",
]
