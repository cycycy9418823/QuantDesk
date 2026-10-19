"""D2_v2 / R4: the symbol x trading-date history ledger.

A bar today must not hide an older gap: 000582 missing 10/8 and 10/9, or 000668
missing 10/8, 10/9 and 10/12, stay listed per date even when the symbol carries
a bar for the target day. The three classes - recovered, verified no-session,
still pending - are never merged, and valid old history is never rewritten.

Synthetic temp trees only. No network, no real data/, results/ or state/.
"""

from __future__ import annotations

import csv
from pathlib import Path

from quantdesk.daily_gap_recovery import (
    build_symbol_date_gap_ledger,
    recover_target_day_gaps,
)
from quantdesk.daily_session_evidence import build_baostock_session_evidence

TARGET = "2026-10-14"
DAYS = ["2026-10-08", "2026-10-09", "2026-10-12", "2026-10-13", TARGET]


def _provider_code(symbol: str) -> str:
    if symbol.startswith("6"):
        return f"sh.{symbol}"
    return f"sz.{symbol}"


def _daily(root: Path, symbol: str, dates: list[str], *, adjust: str = "raw") -> Path:
    path = root / "data" / "daily" / f"{symbol}.csv"
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = ["date,symbol,open,high,low,close,volume,amount,adjust"]
    for day in dates:
        lines.append(f"{day},{symbol},1.0,1.1,0.9,1.0,1000,1000,{adjust}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def _no_session(symbol: str, day: str) -> dict:
    return build_baostock_session_evidence(
        symbol=symbol,
        provider_code=_provider_code(symbol),
        query_start_date=day,
        query_end_date=day,
        records=[{"trade_date": day, "tradestatus": "0"}],
        captured_at="2026-10-14T18:45:10",
    )


def _row(ledger: dict, symbol: str) -> dict:
    return next(row for row in ledger["rows"] if row["symbol"] == symbol)


def _tree(tmp_path: Path) -> Path:
    root = tmp_path / "project"
    (root / "data" / "universe").mkdir(parents=True, exist_ok=True)
    with (root / "data" / "universe" / "a_share_symbols.csv").open(
        "w", encoding="utf-8", newline=""
    ) as handle:
        writer = csv.writer(handle)
        writer.writerow(["symbol", "name"])
        for symbol in ("000582", "000668", "000001"):
            writer.writerow([symbol, symbol])
    return root


# ------------------------------------------------------------ per-date records


def test_history_gaps_are_recorded_per_symbol_and_date(tmp_path: Path) -> None:
    root = _tree(tmp_path)
    _daily(root, "000582", ["2026-10-12", "2026-10-13", TARGET])
    _daily(root, "000668", ["2026-10-13", TARGET])
    _daily(root, "000001", DAYS)

    ledger = build_symbol_date_gap_ledger(
        project_root=root, symbols=["000582", "000668", "000001"], trading_days=DAYS
    )

    assert _row(ledger, "000582")["pending_dates"] == ["2026-10-08", "2026-10-09"]
    assert _row(ledger, "000582")["recovered_dates"] == ["2026-10-12", "2026-10-13", TARGET]
    assert _row(ledger, "000668")["pending_dates"] == ["2026-10-08", "2026-10-09", "2026-10-12"]
    assert _row(ledger, "000001")["pending_dates"] == []


def test_a_bar_today_does_not_hide_an_older_gap(tmp_path: Path) -> None:
    root = _tree(tmp_path)
    _daily(root, "000582", [TARGET])  # today is present, history is not

    ledger = build_symbol_date_gap_ledger(
        project_root=root, symbols=["000582"], trading_days=DAYS
    )
    row = _row(ledger, "000582")

    assert row["recovered_dates"] == [TARGET]
    assert row["pending_dates"] == ["2026-10-08", "2026-10-09", "2026-10-12", "2026-10-13"]
    assert "000582" in ledger["symbols_with_history_gaps"]


def test_the_three_classes_never_overlap(tmp_path: Path) -> None:
    root = _tree(tmp_path)
    _daily(root, "000582", ["2026-10-12", TARGET])
    ledger = build_symbol_date_gap_ledger(
        project_root=root,
        symbols=["000582"],
        trading_days=DAYS,
        session_evidence={"000582": _no_session("000582", "2026-10-08")},
    )
    row = _row(ledger, "000582")

    assert row["verified_no_session_dates"] == ["2026-10-08"]
    assert row["recovered_dates"] == ["2026-10-12", TARGET]
    assert row["pending_dates"] == ["2026-10-09", "2026-10-13"]
    seen = row["recovered_dates"] + row["verified_no_session_dates"] + row["pending_dates"]
    assert sorted(seen) == DAYS and len(set(seen)) == len(seen)
    assert ledger["totals"] == {"recovered": 2, "verified_no_session": 1, "pending": 2}


# ----------------------------------------------------- validation / integrity


def test_valid_old_history_is_never_rewritten(tmp_path: Path) -> None:
    root = _tree(tmp_path)
    path = _daily(root, "000582", ["2026-10-12", TARGET])
    before = path.read_bytes()

    build_symbol_date_gap_ledger(project_root=root, symbols=["000582"], trading_days=DAYS)

    assert path.read_bytes() == before


def test_non_raw_frame_is_not_turned_into_a_gap_list(tmp_path: Path) -> None:
    root = _tree(tmp_path)
    _daily(root, "000582", ["2026-10-12", TARGET], adjust="qfq")

    row = _row(
        build_symbol_date_gap_ledger(
            project_root=root, symbols=["000582"], trading_days=DAYS
        ),
        "000582",
    )

    assert row["usable"] is False
    assert "original_frame_not_verified_raw" in row["invalid_evidence"]
    assert row["pending_dates"] == [] and row["recovered_dates"] == []


def test_duplicate_and_invalid_dates_are_reported(tmp_path: Path) -> None:
    root = _tree(tmp_path)
    path = root / "data" / "daily" / "000582.csv"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "date,symbol,open,high,low,close,volume,amount,adjust\n"
        "2026-10-12,000582,1,1,1,1,1,1,raw\n"
        "2026-10-12,000582,1,1,1,1,1,1,raw\n"
        "not-a-date,000582,1,1,1,1,1,1,raw\n",
        encoding="utf-8",
    )

    row = _row(
        build_symbol_date_gap_ledger(
            project_root=root, symbols=["000582"], trading_days=DAYS
        ),
        "000582",
    )

    assert "duplicate_date:2026-10-12" in row["invalid_evidence"]
    assert "original_frame_invalid_date" in row["invalid_evidence"]
    assert row["usable"] is False


def test_session_evidence_for_another_symbol_is_rejected(tmp_path: Path) -> None:
    root = _tree(tmp_path)
    _daily(root, "000582", [TARGET])

    row = _row(
        build_symbol_date_gap_ledger(
            project_root=root,
            symbols=["000582"],
            trading_days=["2026-10-08"],
            session_evidence={"000582": _no_session("000001", "2026-10-08")},
        ),
        "000582",
    )

    assert row["verified_no_session_dates"] == []
    assert row["pending_dates"] == ["2026-10-08"]
    assert row["session_evidence_rejected"]


def test_invalid_trading_days_are_reported_not_used(tmp_path: Path) -> None:
    root = _tree(tmp_path)
    _daily(root, "000582", [TARGET])

    ledger = build_symbol_date_gap_ledger(
        project_root=root, symbols=["000582"], trading_days=["2026-10-08", "nope", "20261009"]
    )

    assert ledger["trading_days"] == ["2026-10-08", "2026-10-09"]
    assert "nope" in ledger["rejected_trading_days"]


# ------------------------------------------------------------------- wiring


def test_recover_target_day_gaps_attaches_the_history_ledger(tmp_path: Path) -> None:
    root = _tree(tmp_path)
    _daily(root, "000582", [TARGET])

    report = recover_target_day_gaps(
        project_root=root,
        target_date=TARGET,
        daily_payload={"target_date": TARGET, "processed_symbols": ["000582"]},
        output_dir=tmp_path / "recovery",
        network=False,
        ledger_symbols=["000582"],
        ledger_trading_days=DAYS,
    )

    assert report["status"] == "skipped" and report["reason"] == "network_disabled"
    ledger = report["historical_ledger"]
    assert ledger["schema_version"] == "symbol_date_gap_ledger.v1"
    assert _row(ledger, "000582")["pending_dates"] == [
        "2026-10-08", "2026-10-09", "2026-10-12", "2026-10-13",
    ]


def test_ledger_is_absent_when_the_caller_gives_no_days(tmp_path: Path) -> None:
    root = _tree(tmp_path)
    report = recover_target_day_gaps(
        project_root=root,
        target_date=TARGET,
        daily_payload={"target_date": TARGET, "processed_symbols": []},
        output_dir=tmp_path / "recovery",
        network=False,
    )
    assert "historical_ledger" not in report
