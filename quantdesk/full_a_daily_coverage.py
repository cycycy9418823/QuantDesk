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
import hashlib
import io
import json
import re
from collections import Counter
from collections.abc import Iterable
from copy import deepcopy
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any


def prepare_roster_version(rows: list[dict[str, Any]], *, source: str, as_of: str,
                           run_id: str, accepted_at: str) -> dict[str, Any]:
    """Prepare immutable roster bytes and a pointer; never publishes by itself."""
    day = date.fromisoformat(as_of).isoformat()
    stamp = datetime.fromisoformat(accepted_at)
    if not source or stamp.tzinfo is None or not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", run_id):
        raise ValueError("roster_identity_missing")
    codes = [str(r.get("symbol") or "") for r in rows]
    if not codes or len(set(codes)) != len(codes) or any(not re.fullmatch(r"[0-9]{6}", c) for c in codes):
        raise ValueError("roster_symbols_invalid")
    stream = io.StringIO(newline="")
    writer = csv.DictWriter(stream, fieldnames=["symbol", "name", "source", "as_of", "updated_at"])
    writer.writeheader()
    writer.writerows({"symbol": r["symbol"], "name": r.get("name") or "", "source": source,
        "as_of": day, "updated_at": accepted_at} for r in sorted(rows, key=lambda r: r["symbol"]))
    raw = stream.getvalue().encode("utf-8")
    manifest = {"schema": "a_share_roster.v1", "status": "accepted", "accepted": True,
        "source": source, "as_of": day, "accepted_at": accepted_at, "run_id": run_id,
        "path": f"data/universe/versions/{day}_{run_id}.csv", "row_count": len(codes),
        "sha256": hashlib.sha256(raw).hexdigest(), "symbols_sha256": hashlib.sha256(
            json.dumps(sorted(codes), separators=(",", ":")).encode()).hexdigest()}
    return {"csv_bytes": raw, "manifest": manifest, "publication_status": "proposal_only"}


def load_roster_evidence(project_root: Path, *, target_date: str) -> dict[str, Any]:
    """Use a verified version, then an explicit last-accepted or legacy fallback."""
    root = Path(project_root).resolve()
    reasons: list[str] = []
    for pointer_name in ("a_share_roster_latest.json", "a_share_roster_last_accepted.json"):
        pointer = root / "data/universe" / pointer_name
        if not pointer.is_file():
            continue
        try:
            if pointer.is_symlink() or pointer.stat().st_size > 256 * 1024:
                raise ValueError("pointer_size_or_link")
            pointer_raw = pointer.read_bytes()
            meta = json.loads(pointer_raw.decode("utf-8-sig"))
            path = root / meta["path"]
            resolved = path.resolve()
            if (path.is_symlink() or not resolved.is_relative_to(root / "data/universe/versions")
                    or meta.get("schema") != "a_share_roster.v1" or meta.get("accepted") is not True
                    or meta.get("status") != "accepted" or not meta.get("source")
                    or not isinstance(meta.get("run_id"), str)
                    or not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", meta["run_id"])
                    or date.fromisoformat(meta["as_of"]) > date.fromisoformat(target_date)
                    or datetime.fromisoformat(meta["accepted_at"]).tzinfo is None
                    or path.stat().st_size > 8 * 1024 * 1024):
                raise ValueError("roster_identity_or_path_invalid")
            raw = path.read_bytes()
            records = list(csv.DictReader(io.StringIO(raw.decode("utf-8-sig"))))
            codes = [row.get("symbol") for row in records]
            if (not codes or len(set(codes)) != len(codes)
                    or any(not isinstance(code, str) or not re.fullmatch(r"[0-9]{6}", code) for code in codes)
                    or any(row.get("source") != meta["source"] or row.get("as_of") != meta["as_of"]
                           or row.get("updated_at") != meta["accepted_at"] for row in records)):
                raise ValueError("roster_row_identity_mismatch")
            symbols_sha = hashlib.sha256(json.dumps(sorted(codes), separators=(",", ":")).encode()).hexdigest()
            if (hashlib.sha256(raw).hexdigest() != meta["sha256"] or len(codes) != meta["row_count"]
                    or symbols_sha != meta["symbols_sha256"]):
                raise ValueError("roster_hash_or_count_mismatch")
            return {**meta, "symbols": sorted(codes), "identity_status": "verified_version",
                "fallback": pointer_name != "a_share_roster_latest.json",
                "age_days": (date.fromisoformat(target_date) - date.fromisoformat(meta["as_of"])).days,
                "pointer_path": str(pointer), "pointer_sha256": hashlib.sha256(pointer_raw).hexdigest(),
                "reason_codes": reasons, "read_only": True}
        except (OSError, ValueError, TypeError, KeyError, UnicodeError, csv.Error) as exc:
            reasons.append("roster_pointer_invalid:" + pointer_name + ":" + type(exc).__name__)
    legacy = root / "data/universe/a_share_symbols.csv"
    try:
        raw = legacy.read_bytes()
        records = list(csv.DictReader(io.StringIO(raw.decode("utf-8-sig"))))
        codes = _symbols_from_rows(records)
        days = {_date_text(r.get("as_of") or r.get("updated_at")) for r in records} - {""}
        day = next(iter(days)) if len(days) == 1 else ""
        sources = {str(r.get("source") or "") for r in records} - {""}
        if day and date.fromisoformat(day) > date.fromisoformat(target_date):
            return {"symbols": [], "source": "legacy_csv", "as_of": day,
                "sha256": hashlib.sha256(raw).hexdigest(), "row_count": len(codes),
                "path": "data/universe/a_share_symbols.csv", "identity_status": "future_roster",
                "accepted": None, "fallback": True,
                "reason_codes": [*reasons, "legacy_roster_after_target_date"], "read_only": True}
        return {"symbols": sorted(codes), "source": next(iter(sources)) if len(sources) == 1 else "legacy_csv",
            "as_of": day, "sha256": hashlib.sha256(raw).hexdigest(), "row_count": len(codes),
            "path": "data/universe/a_share_symbols.csv", "identity_status": "legacy_unverified_acceptance",
            "age_days": (date.fromisoformat(target_date) - date.fromisoformat(day)).days if day else None,
            "accepted": None, "accepted_at": None, "fallback": True,
            "reason_codes": [*reasons, "versioned_roster_unavailable"], "read_only": True}
    except (OSError, ValueError, UnicodeError, csv.Error) as exc:
        return {"symbols": [], "identity_status": "missing", "as_of": "", "sha256": "",
            "reason_codes": [*reasons, "roster_missing_or_unreadable:" + type(exc).__name__], "read_only": True}


def _date_text(value: Any) -> str:
    text = str(value or "").strip()
    if len(text) >= 10 and text[4] in {"-", "/"}:
        return text[:10].replace("/", "-")
    return ""


def _symbols_from_rows(rows: Iterable[dict[str, Any]]) -> list[str]:
    """Preserve the permissive legacy CSV policy without rereading its file."""
    symbols: list[str] = []
    seen: set[str] = set()
    for row in rows:
        symbol = str(row.get("symbol") or row.get("code") or "").strip()
        if len(symbol) == 6 and symbol.isdigit() and symbol not in seen:
            seen.add(symbol)
            symbols.append(symbol)
    return symbols


def _read_universe(path: Path) -> list[str]:
    """Return the current, de-duplicated six-digit universe in file order."""
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return _symbols_from_rows(csv.DictReader(handle))


def _last_date_and_rows(path: Path) -> tuple[str, int, bool]:
    """Return max daily date, row count and duplicate-date evidence for one CSV."""

    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return _date_summary(csv.reader(handle))


def _date_summary(rows: Iterable[list[str]]) -> tuple[str, int, bool]:
    dates: list[str] = []
    reader = iter(rows)
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


def _inspect_target_rows(path: Path, rows: list[dict[str, Any]], *, target_date: str,
                         source_sha256: str) -> dict[str, Any]:
    from quantdesk.data_truth.daily_bar_validity import classify_daily_row
    from quantdesk.price_basis import validate_price_frame_set
    import pandas as pd

    gate = validate_price_frame_set({path.stem: pd.DataFrame(rows)}, consumer="paper_execution")
    selected = [r for r in rows if _date_text(r.get("date") or r.get("trade_date")) == target_date]
    classification = classify_daily_row(selected[0]) if len(selected) == 1 else {
        "classification": "unknown_gap", "valid_trading_bar": False, "execution_eligible": False,
        "reason_code": "target_row_missing" if not selected else "duplicate_target_rows"}
    if not gate.get("eligible"):
        classification.update(classification="unknown_gap", valid_trading_bar=False, execution_eligible=False,
            reason_code="price_basis_untrusted")
    return {**classification, "target_date": target_date, "target_row_count": len(selected),
        "target_row_present": bool(selected), "price_policy_validation": gate,
        "source_sha256": source_sha256, "source_path": str(path)}


def _daily_read_failure(exc: Exception) -> dict[str, Any]:
    return {"classification": "unknown_gap", "valid_trading_bar": False, "execution_eligible": False,
        "target_row_present": False, "target_row_count": 0, "reason_code": "daily_read_failed:" + type(exc).__name__}


def inspect_target_daily_row(path: Path, *, target_date: str) -> dict[str, Any]:
    """Read original rows before selecting a day; date coverage is not price proof."""
    try:
        raw = path.read_bytes()
        rows = list(csv.DictReader(io.StringIO(raw.decode("utf-8-sig"))))
        return _inspect_target_rows(path, rows, target_date=target_date, source_sha256=hashlib.sha256(raw).hexdigest())
    except (OSError, ValueError, UnicodeError, csv.Error) as exc:
        return _daily_read_failure(exc)


@dataclass(frozen=True)
class _DailyFileAudit:
    source_sha256: str
    target_row: dict[str, Any]
    latest_date: str
    coverage_dates: tuple[str, int, bool]
    coverage_error: str = ""


class DailyAuditCache:
    """Reuse parsing in one read-only audit; rehash complete bytes on every read.

    Only small summaries are retained, never CSV bytes or frames. The caller
    owns the lifetime, and returned dictionaries cannot mutate cached evidence.
    """

    def __init__(self) -> None:
        self._entries: dict[tuple[str, str], _DailyFileAudit] = {}

    def inspect(self, path: Path, *, target_date: str) -> _DailyFileAudit:
        key = (str(path), target_date)
        try:
            raw = path.read_bytes()
            digest = hashlib.sha256(raw).hexdigest()
            prior = self._entries.get(key)
            if prior is not None and prior.source_sha256 == digest:
                return deepcopy(prior)
            text = raw.decode("utf-8-sig")
        except (OSError, ValueError, UnicodeError) as exc:
            self._entries.pop(key, None)
            return _DailyFileAudit("", _daily_read_failure(exc), "", ("", 0, False), type(exc).__name__)

        rows = None
        try:
            rows = list(csv.DictReader(io.StringIO(text)))
            target_row = _inspect_target_rows(path, rows, target_date=target_date, source_sha256=digest)
        except (OSError, ValueError, UnicodeError, csv.Error) as exc:
            target_row = _daily_read_failure(exc)

        # Preserve the two existing date-only readers' distinct header/date
        # policies, including their newline handling. Neither grants price proof.
        latest = ""
        try:
            date_rows = rows
            if date_rows is None or "\r" in text.replace("\r\n", ""):
                date_rows = list(csv.DictReader(io.StringIO(text, newline="")))
            for row in date_rows:
                value = str(row.get("date") or row.get("trade_date") or "").strip()
                day = value[:10].replace("/", "-") if len(value) >= 10 else ""
                latest = max(latest, day)
        except (OSError, UnicodeError, csv.Error):
            latest = ""
        try:
            dates = _date_summary(csv.reader(io.StringIO(text, newline="")))
            error = ""
        except (OSError, UnicodeError, csv.Error) as exc:
            dates, error = ("", 0, False), type(exc).__name__
        result = _DailyFileAudit(digest, target_row, latest, dates, error)
        self._entries[key] = result
        return deepcopy(result)


def build_full_a_daily_coverage(
    *,
    target_date: str,
    universe_path: Path,
    daily_dir: Path,
    inspection_cache: DailyAuditCache | None = None,
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
    target_rows: dict[str, dict[str, Any]] = {}

    for symbol in universe:
        path = Path(daily_dir) / f"{symbol}.csv"
        if not path.is_file():
            missing.append(symbol)
            continue
        audit = None
        try:
            if inspection_cache is not None and target:
                audit = inspection_cache.inspect(path, target_date=target)
                if audit.coverage_error:
                    invalid.append(f"{symbol}:{audit.coverage_error}")
                    continue
                latest, row_count, has_duplicate = audit.coverage_dates
            else:
                latest, row_count, has_duplicate = _last_date_and_rows(path)
        except (OSError, UnicodeError, csv.Error) as exc:
            invalid.append(f"{symbol}:{type(exc).__name__}")
            continue
        if not latest or not row_count:
            invalid.append(f"{symbol}:empty_or_no_date")
            continue
        present.append(symbol)
        latest_by_symbol[symbol] = latest
        target_rows[symbol] = (audit.target_row if audit is not None else
                              inspect_target_daily_row(path, target_date=target) if target else {})
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
        sorted(symbol for symbol, proof in target_rows.items() if proof.get("target_row_present"))
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
        "target_date_coverage_contract": "date_rows_only_not_execution_evidence",
        "valid_raw_target_trading_bar_count": sum(p.get("valid_trading_bar") is True for p in target_rows.values()),
        "future_latest_count": sum(day > target for day in latest_by_symbol.values()) if target else 0,
        "target_row_quality": target_rows,
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
