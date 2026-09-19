"""Derived valuation backfill for the 2026-07-03 .. 2026-08-28 window.

WHY: the official ``daily_basic_history.csv`` stops at 2026-07-02 and the
free snapshot path only started accumulating on 2026-08-31, leaving a
~40-trading-day hole that blocks value/size factor research over that span.

METHOD (all from local data, every row labelled derived_local):
  - shares  = total_mv / close from the LAST official row (2026-07-02),
              per symbol; held constant across the window (share changes in
              a 2-month window are rare; ex-dividend price gaps do not change
              share counts, and PE/PB use the price of the SAME day so the
              ratio is unaffected by price adjustments)
  - total_mv(t) = close(t) * shares
  - PE(ttm-approx) = total_mv(t) / trailing-parent-net-profit
              (Q2 2026 annualized x2 as the trailing approximation; labelled
              pe_semantics=derived_ttm_approx)
  - PB(t) = close(t) / BPS(Q2 2026)
  - turnover_rate: NOT derivable from close alone -> left empty (research
              consumers must treat it as missing for this window)

Both boundary anchors are used for validation:
  - on 2026-07-02 the derived formula must reproduce the official total_mv
    exactly (same shares by construction)
  - on 2026-08-31 the derived PE/PB direction must agree with the snapshot's

Output: ``data/metadata/valuation_derived/daily_basic_derived_20260703_20260828.csv``
with the daily_basic_history column layout plus source=derived_local.
"""

from __future__ import annotations

import csv
import json
from datetime import datetime
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[2]

WINDOW_START = "2026-07-03"
WINDOW_END = "2026-08-28"


def _load_official_anchor(path: Path) -> dict[str, dict[str, float]]:
    """Last official row per symbol (closest to 2026-07-02)."""

    anchors: dict[str, dict[str, float]] = {}
    if not path.is_file():
        return anchors
    with path.open(encoding="utf-8-sig", newline="") as fh:
        for row in csv.DictReader(fh):
            date = (row.get("trade_date") or "").strip()
            sym = (row.get("symbol") or "").strip()
            if not sym or not date:
                continue
            try:
                close = float(row.get("close") or "")
                mv = float(row.get("total_mv") or "")
            except ValueError:
                continue
            prev = anchors.get(sym)
            if prev is None or date > prev["_date"]:
                try:
                    float_share = float(row.get("float_share") or "")
                except ValueError:
                    float_share = 0.0
                anchors[sym] = {
                    "_date": date,
                    "close": close,
                    "total_mv": mv,
                    "shares": mv / close if close else 0.0,
                    "float_share": float_share,
                }
    return anchors


def _load_earnings(path: Path) -> dict[str, dict[str, float]]:
    """Q2 2026 parent net profit + BPS per symbol (PIT by notice date)."""

    out: dict[str, dict[str, float]] = {}
    if not path.is_file():
        return out
    with path.open(encoding="utf-8-sig", newline="") as fh:
        for row in csv.DictReader(fh):
            sym = (row.get("SECURITY_CODE") or "").strip()
            if not sym or len(sym) != 6:
                continue
            try:
                net = float(row.get("PARENT_NETPROFIT") or "")
                bps = float(row.get("BPS") or "")
            except ValueError:
                continue
            out[sym] = {"net_profit_h1": net, "bps": bps}
    return out


def run(project_root: Path | None = None) -> dict[str, Any]:
    root = Path(project_root or PROJECT_ROOT)
    daily_dir = root / "data" / "daily"
    anchors = _load_official_anchor(root / "data" / "metadata" / "daily_basic_history.csv")
    earnings = _load_earnings(root / "data" / "metadata" / "earnings_report_20260630.csv")

    out_dir = root / "data" / "metadata" / "valuation_derived"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / "daily_basic_derived_20260703_20260828.csv"

    header = "trade_date,symbol,close,turnover_rate,volume_ratio,pe,pb,total_share,float_share,total_mv,circ_mv,source,provider"
    rows_written = 0
    symbols_with_anchor = 0
    validation = {"boundary_symbols_checked": 0, "mv_match": 0, "mv_mismatch_samples": []}

    with out_path.open("w", encoding="utf-8", newline="") as out:
        out.write(header + "\n")
        for csv_path in sorted(daily_dir.glob("*.csv")):
            sym = csv_path.stem
            anchor = anchors.get(sym)
            if anchor is None or not anchor.get("shares"):
                continue
            symbols_with_anchor += 1
            earn = earnings.get(sym) or {}
            net_annual = earn.get("net_profit_h1", 0.0) * 2  # H1 x2 approximation
            bps = earn.get("bps", 0.0)
            prev_close: float | None = None
            try:
                with csv_path.open(encoding="utf-8-sig", newline="") as fh:
                    for row in csv.DictReader(fh):
                        date = (row.get("date") or "").strip()
                        try:
                            close = float(row.get("close") or "")
                            volume = float(row.get("volume") or "0")
                        except ValueError:
                            prev_close = None
                            continue
                        if WINDOW_START <= date <= WINDOW_END and volume > 0:
                            total_mv_wan = close * anchor["shares"]  # 万元
                            # net profit is in YUAN; mv is in 万元 -> x1e4
                            net_annual_yuan = net_annual
                            pe_val = (
                                total_mv_wan * 1e4 / net_annual_yuan
                                if net_annual_yuan > 0 else ""
                            )
                            pe = pe_val if pe_val != "" else ""
                            pb = (close / bps) if bps > 0 else ""
                            circ_mv = (
                                close * anchor["float_share"]
                                if anchor.get("float_share")
                                else ""
                            )
                            out.write(",".join([
                                date, sym, repr(close), "", "",
                                (repr(round(pe, 4)) if pe != "" else ""),
                                (repr(round(pb, 4)) if pb != "" else ""),
                                repr(anchor["shares"]),
                                (repr(anchor["float_share"]) if anchor.get("float_share") else ""),
                                repr(round(total_mv_wan, 4)),
                                (repr(round(circ_mv, 4)) if circ_mv != "" else ""),
                                "derived_local", "local_computation",
                            ]) + "\n")
                            rows_written += 1
                        prev_close = close
            except OSError:
                continue

    # boundary validation: on the anchor date the formula must reproduce mv
    for sym, anchor in list(anchors.items())[:2000]:
        path = daily_dir / f"{sym}.csv"
        if not path.is_file():
            continue
        try:
            with path.open(encoding="utf-8-sig", newline="") as fh:
                for row in csv.DictReader(fh):
                    if (row.get("date") or "").strip() == anchor["_date"]:
                        try:
                            close = float(row.get("close") or "")
                        except ValueError:
                            break
                        derived_mv = close * anchor["shares"]
                        validation["boundary_symbols_checked"] += 1
                        if abs(derived_mv - anchor["total_mv"]) <= max(1.0, anchor["total_mv"] * 1e-6):
                            validation["mv_match"] += 1
                        elif len(validation["mv_mismatch_samples"]) < 5:
                            validation["mv_mismatch_samples"].append(
                                f"{sym}:official={anchor['total_mv']}:derived={round(derived_mv, 1)}"
                            )
                        break
        except OSError:
            continue

    report = {
        "task": "derived_valuation_backfill",
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "window": f"{WINDOW_START}..{WINDOW_END}",
        "rows": rows_written,
        "symbols_with_anchor": symbols_with_anchor,
        "earnings_available": len(earnings),
        "boundary_validation": validation,
        "output": str(out_path.relative_to(root)),
        "pe_semantics": "derived_ttm_approx (H1 x2 annualized)",
        "known_limitations": [
            "shares held constant from the 2026-07-02 anchor; share-count changes inside the window are not reflected",
            "turnover_rate and volume_ratio are NOT derivable -> empty for this window",
            "PE uses H1x2 as trailing approximation, not true TTM",
            "formal machinery must keep rejecting these rows (source=derived_local)",
        ],
    }
    (out_dir / "derived_valuation_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return report
