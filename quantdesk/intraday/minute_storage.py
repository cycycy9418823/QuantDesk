"""Route new declared-basis rows without rewriting legacy minute evidence.

The directory name is only a storage convention. Every consumer must still
classify the original contents; this module never supplies a price basis.
"""
from __future__ import annotations

import csv
from datetime import datetime, timedelta
import math
from pathlib import Path
from zoneinfo import ZoneInfo

DECLARED_BASIS_DIR = "declared_basis"
QUARANTINE_DIR = ".quarantine"
LEGACY_COLUMNS = {
    "symbol", "datetime", "date", "time", "open", "high", "low", "close",
    "volume", "amount", "source", "updated_at",
}

# Float serialization noise, not a price move. The stored archives carry values
# such as open=39.690000000000005 against high=39.69 (600036, 2026-04-24
# 09:32); across the 2026-10-09 storage_blocked files every envelope
# "violation" was below 2e-14 relative. 1e-12 keeps a 50x margin and is still
# about a million times smaller than one price tick.
ENVELOPE_REL_TOLERANCE = 1e-12


def envelope_violation(open_price: float, high: float, low: float, close: float) -> str:
    """Return "" or the reason the OHLC envelope is inverted beyond float noise.

    Same rule for raw and unknown rows, stored and incoming. Real inversions
    (low above the body, high below the body, high below low) still fail.
    """
    body_top, body_bottom = max(open_price, close), min(open_price, close)

    def _below(value: float, floor: float) -> bool:
        return value < floor and not math.isclose(value, floor, rel_tol=ENVELOPE_REL_TOLERANCE, abs_tol=0.0)

    if _below(body_bottom, low):
        return "low_above_body"
    if _below(high, body_top) or _below(high, low):
        return "high_below_body"
    return ""


def is_legacy_minute_file(path: Path) -> bool:
    """Recognize only the collector's exact old schema missing price_basis."""
    try:
        with Path(path).open(encoding="utf-8-sig", newline="") as handle:
            columns = next(csv.reader(handle), [])
    except (OSError, UnicodeError, csv.Error):
        return False
    return len(columns) == len(LEGACY_COLUMNS) and set(columns) == LEGACY_COLUMNS


def _has_unknown_basis_history(path: Path, *, now: datetime | None = None) -> bool:
    """Recognize the collector schema with explicitly unknown historical rows.

    Only raw/unknown tokens are eligible for a separate new-evidence file.
    Adjusted, conflicting-schema and damaged histories keep their existing
    rejection path. Nothing here changes the basis of an old or incoming row.
    """
    checked_at = now or datetime.now(ZoneInfo("Asia/Shanghai"))
    if checked_at.tzinfo is not None:
        checked_at = checked_at.astimezone(ZoneInfo("Asia/Shanghai"))
    checked_at = checked_at.replace(tzinfo=None)
    try:
        # The common all-raw archive keeps the existing validator and needs no
        # second price/date validation here. Only a possible unknown segment
        # goes through the full content check below.
        with path.open(encoding="utf-8-sig", newline="") as handle:
            reader = csv.reader(handle)
            columns = next(reader, [])
            if len(columns) != len(LEGACY_COLUMNS) + 1 or set(columns) != LEGACY_COLUMNS | {"price_basis"}:
                return False
            basis_column = columns.index("price_basis")
            for values in reader:
                if len(values) != len(columns):
                    return False
                basis = values[basis_column].strip().lower()
                if basis not in {"raw", "unknown", ""}:
                    return False
                if basis != "raw":
                    break
            else:
                return False
        with path.open(encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle)
            columns = reader.fieldnames or []
            if len(columns) != len(LEGACY_COLUMNS) + 1 or set(columns) != LEGACY_COLUMNS | {"price_basis"}:
                return False
            unknown = False
            for row in reader:
                if None in row or any(value is None for value in row.values()):
                    return False
                basis = row["price_basis"].strip().lower()
                if basis not in {"raw", "unknown", ""}:
                    return False
                if row["symbol"] != path.stem:
                    return False
                try:
                    prices = [float(row[name]) for name in ("open", "high", "low", "close")]
                    stamp = datetime.fromisoformat(row["datetime"].strip().replace("T", " "))
                except (TypeError, ValueError):
                    return False
                open_price, high, low, close = prices
                # Unknown rows stay quarantined and never become price
                # evidence. Old float serialization may differ in the last
                # decimal digits for raw and unknown rows alike (2026-10-09:
                # every raw "violation" was below 2e-14 relative); a real
                # inversion still fails. Nothing here rewrites a row.
                if (not all(math.isfinite(value) and value > 0 for value in prices)
                        or envelope_violation(open_price, high, low, close)
                        or stamp.tzinfo is not None or stamp > checked_at + timedelta(minutes=2)
                        or row["date"] != stamp.date().isoformat()
                        or row["time"] not in {stamp.strftime("%H:%M"), stamp.strftime("%H:%M:%S")}):
                    return False
                unknown = unknown or basis != "raw"
            return unknown
    except (OSError, UnicodeError, csv.Error):
        return False


def minute_write_path(path: Path, *, history_now: datetime | None = None) -> Path:
    """Keep a legacy file at its original path; append new evidence separately."""
    path = Path(path)
    if path.parent.name == DECLARED_BASIS_DIR:
        return path
    alternate = path.parent / DECLARED_BASIS_DIR / path.name
    if is_legacy_minute_file(path) or _has_unknown_basis_history(path, now=history_now) or (not path.exists() and alternate.is_file()):
        if not alternate.resolve().is_relative_to(path.parent.resolve()):
            raise OSError("minute_storage_path_outside_archive")
        return alternate
    return path


def resolve_minute_read_path(path: Path) -> Path:
    """Select the physical file, leaving missing/unknown legacy evidence visible."""
    original = Path(path)
    alternate = minute_write_path(original)
    return alternate if alternate.is_file() else original
