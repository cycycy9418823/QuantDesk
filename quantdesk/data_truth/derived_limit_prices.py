"""Derived limit prices: compute A-share daily up/down limit prices locally.

WHY: the official ``data/metadata/limit_price_history.csv`` stops at 2026-07-02
and has no free update source. Research tradability checks (e.g. one-word
limit boards must be un-buyable) still need limit prices after that date.

This module derives them deterministically from local data:

    up_limit   = round_half_up(pre_close * (1 + ratio), 2)
    down_limit = round_half_up(pre_close * (1 - ratio), 2)

with ratio by board (main 10% / ChiNext & STAR 20% / BSE 30%; ST on main
boards 5%). ST status comes from ``st_history.csv`` intervals; symbols whose
latest interval is open (no end date) are treated as ST from that start date
onward - the snapshot-name daily ST harvest supersedes this when present.

Round-half-up is the A-share rule (四舍五入到分), NOT banker's rounding.

Outputs a dated file per run under ``data/metadata/limit_prices_derived/``
plus a combined ``limit_prices_derived_latest.csv``, every row labelled
``source=derived_local``. The formal alpha machinery must keep rejecting
derived rows (it requires official provenance); this file is for research.

Self-validation: for sealed one-word limit-up bars in our own daily data
(high == low == close), the close MUST equal the official up_limit - so we
verify the derived value against every such bar we can find and report the
match rate.
"""

from __future__ import annotations

import csv
import json
import math
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterable

PROJECT_ROOT = Path(__file__).resolve().parents[2]

ST_RATIO = 0.05


def board_ratio(symbol: str, *, is_st: bool) -> float:
    """Limit ratio for a symbol on a given day (ST only narrows main boards)."""

    sym = str(symbol).strip()
    if sym.startswith(("300", "301", "688", "689")):
        return 0.20  # ChiNext / STAR: 20% even for ST
    if sym.startswith(("92", "43", "83", "87")):
        return 0.30  # BSE
    if sym.startswith(("000", "001", "002", "003", "600", "601", "603", "605")):
        return ST_RATIO if is_st else 0.10
    return 0.10  # default: main board


def round_half_up(value: float, digits: int = 2) -> float:
    factor = 10 ** digits
    return math.floor(value * factor + 0.5) / factor


def derive_limits(pre_close: float, symbol: str, *, is_st: bool) -> tuple[float, float]:
    ratio = board_ratio(symbol, is_st=is_st)
    up = round_half_up(pre_close * (1 + ratio))
    down = round_half_up(pre_close * (1 - ratio))
    return up, down


@dataclass
class STInterval:
    start: str
    end: str  # "" = open-ended (still ST)
    was_open: bool = False  # True when the source row had no end date


def load_st_intervals(
    path: Path,
    *,
    current_st_symbols: set[str] | None = None,
) -> dict[str, list[STInterval]]:
    """Load ST intervals; close stale open-ended intervals via current names.

    ``st_history.csv`` contains 533+ open-ended intervals whose symbol no
    longer carries ST in its current name (the export never recorded the
    removal). Treating those as "still ST" breaks limit derivation (533
    stocks would get ±5% forever). When ``current_st_symbols`` is provided
    (from the latest daily snapshot names), an open-ended interval whose
    symbol is NOT currently ST is closed at the snapshot date.
    """

    out: dict[str, list[STInterval]] = {}
    if not path.is_file():
        return out
    today = datetime.now().strftime("%Y-%m-%d")
    with path.open(encoding="utf-8-sig", newline="") as fh:
        for row in csv.DictReader(fh):
            sym = (row.get("symbol") or "").strip()
            start = (row.get("st_start_date") or "").strip()
            if not sym or not start:
                continue
            end = (row.get("st_end_date") or "").strip()
            if row.get("st_type") == "delisting_risk_name":
                continue  # name change, not an ST band for limit math
            if not end and current_st_symbols is not None and sym not in current_st_symbols:
                # stale open interval: symbol is not ST today; close it (the
                # clamp pass below decides whether it ends today or earlier).
                end = today
                out.setdefault(sym, []).append(STInterval(start=start, end=end, was_open=True))
                continue
            out.setdefault(sym, []).append(STInterval(start=start, end=end))
    # clamp pass: stale open intervals provisionally closed to "today" are
    # pulled back to the latest genuine closed end when one exists (the
    # removal evidence), so a 2021 removal does not read as ST-until-now.
    for sym, intervals in out.items():
        closed_ends = [it.end for it in intervals if it.end and not it.was_open]
        if closed_ends:
            max_closed = max(closed_ends)
            for it in intervals:
                if it.was_open and it.end == today:
                    it.end = max_closed
    return out


def clamp_open_only_stale(
    st_map: dict[str, list[STInterval]],
    *,
    current_st_symbols: set[str],
    first_snapshot_date: str,
) -> int:
    """Symbols with ONLY open intervals and no longer ST today.

    There is no closed end to clamp to, so we close the interval at the first
    snapshot date (the earliest date we can PROVE they were already non-ST).
    Between the true removal day and the snapshot date the ST flag stays
    wrong; that residual error is bounded and reported by the caller. Only 7
    symbols are affected in the current data.
    """

    fixed = 0
    for sym, intervals in st_map.items():
        if sym in current_st_symbols:
            continue
        has_closed = any(it.end and not it.was_open for it in intervals)
        if has_closed:
            continue
        for it in intervals:
            if it.was_open and it.end and it.end >= first_snapshot_date:
                it.end = first_snapshot_date
                fixed += 1
    return fixed


def load_current_st_symbols(snapshot_dir: Path) -> set[str]:
    """ST symbols from the newest valuation snapshot's name column."""

    best: Path | None = None
    if snapshot_dir.is_dir():
        for path in sorted(snapshot_dir.glob("*.csv"), reverse=True):
            if path.is_file():
                best = path
                break
    if best is None:
        return set()
    out: set[str] = set()
    try:
        with best.open(encoding="utf-8-sig", newline="") as fh:
            for row in csv.DictReader(fh):
                name = str(row.get("name") or "")
                sym = str(row.get("symbol") or "").strip()
                if sym and ("ST" in name.upper()):
                    out.add(sym)
    except OSError:
        return set()
    return out


def is_st_on(intervals: list[STInterval], date: str) -> bool:
    for it in intervals:
        if it.start <= date and (not it.end or date <= it.end):
            return True
    return False


def derive_for_dates(
    symbol: str,
    bars: Iterable[dict],
    st_intervals: list[STInterval],
    *,
    start_date: str,
) -> list[dict]:
    """bars: chronological dicts with keys date, close (str)."""

    out: list[dict] = []
    prev_close: float | None = None
    for bar in bars:
        date = str(bar.get("date") or "").strip()
        close = str(bar.get("close") or "").strip()
        if not date or not close:
            continue
        try:
            close_f = float(close)
        except ValueError:
            prev_close = None
            continue
        if prev_close is not None and date >= start_date:
            st = is_st_on(st_intervals, date)
            up, down = derive_limits(prev_close, symbol, is_st=st)
            out.append({
                "trade_date": date,
                "symbol": symbol,
                "pre_close": prev_close,
                "up_limit": up,
                "down_limit": down,
                "st": st,
            })
        prev_close = close_f
    return out


def _iter_daily_bars(path: Path) -> list[dict]:
    rows: list[dict] = []
    try:
        with path.open(encoding="utf-8-sig", newline="") as fh:
            reader = csv.DictReader(fh)
            for row in reader:
                rows.append(row)
    except OSError:
        return []
    return rows


def _first_snapshot_date(snapshot_dir: Path) -> str:
    if not snapshot_dir.is_dir():
        return datetime.now().strftime("%Y-%m-%d")
    for path in sorted(snapshot_dir.glob("*.csv")):
        digits = "".join(ch for ch in path.stem if ch.isdigit())
        if len(digits) == 8:
            return f"{digits[:4]}-{digits[4:6]}-{digits[6:]}"
    return datetime.now().strftime("%Y-%m-%d")


def run(
    project_root: Path | None = None,
    *,
    start_date: str = "2026-07-03",
    symbols: list[str] | None = None,
) -> dict:
    root = Path(project_root or PROJECT_ROOT)
    daily_dir = root / "data" / "daily"
    snapshot_dir = root / "data" / "metadata" / "valuation_snapshots"
    current_st = load_current_st_symbols(snapshot_dir)
    st_map = load_st_intervals(
        root / "data" / "metadata" / "st_history.csv",
        current_st_symbols=current_st,
    )
    first_snapshot = _first_snapshot_date(snapshot_dir)
    clamped_open_only = clamp_open_only_stale(
        st_map, current_st_symbols=current_st, first_snapshot_date=first_snapshot
    )
    out_dir = root / "data" / "metadata" / "limit_prices_derived"
    out_dir.mkdir(parents=True, exist_ok=True)

    targets = symbols or sorted(
        p.stem for p in daily_dir.glob("*.csv") if p.stem.isdigit()
    )
    all_rows: list[list[str]] = []
    sealed_checks = {"total": 0, "match": 0, "mismatch_samples": []}

    def _fmt(v: float) -> str:
        s = f"{v:.2f}".rstrip("0").rstrip(".")
        return s if s else "0"

    for sym in targets:
        path = daily_dir / f"{sym}.csv"
        bars = _iter_daily_bars(path)
        st_intervals = st_map.get(sym, [])
        derived = derive_for_dates(sym, bars, st_intervals, start_date=start_date)
        for row in derived:
            all_rows.append([
                row["trade_date"], sym, _fmt(row["pre_close"]),
                _fmt(row["up_limit"]), _fmt(row["down_limit"]),
                "derived_local", "local_computation",
                "st" if row["st"] else "",
            ])
        # self-validation against sealed one-word bars in the SAME derived window
        prev_close = None
        for bar in bars:
            date = str(bar.get("date") or "").strip()
            try:
                close_f = float(str(bar.get("close") or ""))
                high_f = float(str(bar.get("high") or ""))
                low_f = float(str(bar.get("low") or ""))
                vol = float(str(bar.get("volume") or "0"))
            except ValueError:
                prev_close = None
                continue
            if prev_close is not None and date >= start_date and vol > 0:
                if high_f == low_f == close_f:
                    # sealed one-word bar: matches EITHER the up limit
                    # (sealed limit-up) or the down limit (sealed limit-down).
                    st = is_st_on(st_intervals, date)
                    up, down = derive_limits(prev_close, sym, is_st=st)
                    sealed_checks["total"] += 1
                    if abs(up - close_f) < 0.005 or abs(down - close_f) < 0.005:
                        sealed_checks["match"] += 1
                    elif len(sealed_checks["mismatch_samples"]) < 8:
                        sealed_checks["mismatch_samples"].append(
                            f"{sym}:{date}:close={close_f}:derived=({up},{down})"
                        )
            prev_close = close_f

    header = "trade_date,symbol,pre_close,up_limit,down_limit,source,provider,st_flag"
    latest_path = out_dir / "limit_prices_derived_latest.csv"
    with latest_path.open("w", encoding="utf-8", newline="") as fh:
        fh.write(header + "\n")
        for row in all_rows:
            fh.write(",".join(row) + "\n")
    dated_path = out_dir / f"limit_prices_derived_{datetime.now().strftime('%Y%m%d')}.csv"
    dated_path.write_text(latest_path.read_text(encoding="utf-8"), encoding="utf-8")

    report = {
        "task": "derived_limit_prices",
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "start_date": start_date,
        "symbols": len(targets),
        "rows": len(all_rows),
        "st_internals": {
            "current_st_symbols": len(current_st),
            "open_only_stale_clamped": clamped_open_only,
            "note": "7 symbols with open-only ST records now non-ST are clamped to the first snapshot date; residual ST-flag error is bounded between true removal and that date",
        },
        "sealed_validation": {
            "total": sealed_checks["total"],
            "match": sealed_checks["match"],
            "match_rate": round(sealed_checks["match"] / sealed_checks["total"], 4) if sealed_checks["total"] else None,
            "mismatch_samples": sealed_checks["mismatch_samples"],
            "known_limitations": [
                "ex-dividend days: the limit base is the ADJUSTED pre-close, not the raw previous close; the daily CSVs are raw, so sealed bars on ex-dividend dates can mismatch (case 000078 2026-08-31)",
                "residual ST staleness for open-only intervals without a closed end (7 symbols, clamped to first snapshot date)",
            ],
        },
        "files": [
            str(latest_path.relative_to(root)),
            str(dated_path.relative_to(root)),
        ],
        "note": "source=derived_local; formal alpha machinery must keep rejecting derived rows",
    }
    (out_dir / "derived_limit_prices_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return report
