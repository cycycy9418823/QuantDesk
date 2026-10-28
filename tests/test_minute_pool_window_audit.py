"""Stage 2 tests: windowed minute-pool quality audit and window verified pool.

Contract (task book section 6): read each canonical 1m CSV once, grade every
trading day in the window with the EXISTING graders (zero standard drift),
produce a window verified-pool snapshot under results/, and let replay
consumers accept symbol+date coverage.  No canonical writes, no network.
"""

from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import pytest

from quantdesk.intraday.intraday_quality_audit import audit_intraday_file
from quantdesk.intraday.minute_pool_window_audit import (
    audit_minute_pool_window,
    classify_minute_replay_evidence_windowed,
    load_window_verified_minute_pool,
    window_pool_covers,
)
from tests.minute_evidence_fixture import final_minute_fixture

WINDOW_DAYS = ("2026-07-13", "2026-07-14", "2026-07-15")


def _write_calendar(root: Path) -> Path:
    path = root / "calendar.csv"
    rows = ["date,is_open"]
    rows += [f"{d},1" for d in WINDOW_DAYS]
    rows += ["2026-07-11,0", "2026-07-12,0"]
    path.write_text("\n".join(rows) + "\n", encoding="utf-8")
    return path


def _minute_rows(symbol: str, day: str, *, rows: int = 240, bad_ohlc_day: bool = False):
    out = []
    for i in range(rows):
        hour = 9 + (i + 30) // 60 if i < 120 else 13 + (i - 120) // 60
        minute = (i + 30) % 60 if i < 120 else (i - 120) % 60
        hhmm = f"{hour:02d}:{minute:02d}:00"
        price = 10.0 + (i % 7) * 0.01
        high = price - 5.0 if bad_ohlc_day else price + 0.02
        out.append(
            {
                "symbol": symbol,
                "datetime": f"{day} {hhmm}",
                "date": day,
                "time": hhmm,
                "open": price,
                "high": high,
                "low": price - 0.02 if not bad_ohlc_day else price,
                "close": price + 0.01,
                "volume": 10_000 + i,
                "amount": (10_000 + i) * price,
                "price_basis": "raw",
                "source": "test_fixture",
                "updated_at": "2026-07-20T00:00:00",
            }
        )
    return out


def _pool_dir(root: Path) -> Path:
    intraday = root / "data" / "intraday" / "1m"
    intraday.mkdir(parents=True, exist_ok=True)
    # good: all three days complete
    rows = []
    for day in WINDOW_DAYS:
        rows += _minute_rows("600001", day)
    final_minute_fixture(pd.DataFrame(rows)).to_csv(intraday / "600001.csv", index=False, encoding="utf-8")
    # holed: middle day entirely missing (e.g. suspension)
    rows = _minute_rows("600002", WINDOW_DAYS[0]) + _minute_rows("600002", WINDOW_DAYS[2])
    final_minute_fixture(pd.DataFrame(rows)).to_csv(intraday / "600002.csv", index=False, encoding="utf-8")
    # corrupt: middle day has broken OHLC (high < low)
    rows = (
        _minute_rows("600003", WINDOW_DAYS[0])
        + _minute_rows("600003", WINDOW_DAYS[1], bad_ohlc_day=True)
        + _minute_rows("600003", WINDOW_DAYS[2])
    )
    pd.DataFrame(rows).to_csv(intraday / "600003.csv", index=False, encoding="utf-8")
    # thin: last day has only 40 rows (partial)
    rows = _minute_rows("600004", WINDOW_DAYS[0]) + _minute_rows("600004", WINDOW_DAYS[2], rows=40)
    pd.DataFrame(rows).to_csv(intraday / "600004.csv", index=False, encoding="utf-8")
    return intraday


@pytest.fixture()
def project(tmp_path):
    intraday = _pool_dir(tmp_path)
    calendar = _write_calendar(tmp_path)
    output = tmp_path / "results" / "data_health" / "minute_pool_window_audit_latest.json"
    return {"root": tmp_path, "intraday": intraday, "calendar": calendar, "output": output}


def _run(project, **kw):
    return audit_minute_pool_window(
        window_start=WINDOW_DAYS[0],
        window_end=WINDOW_DAYS[-1],
        intraday_dir=project["intraday"],
        calendar_path=project["calendar"],
        output_path=project["output"],
        **kw,
    )


def test_grades_match_existing_single_date_auditor_exactly(project):
    """Zero standard drift: window grades == audit_intraday_file grades."""

    payload = _run(project)
    by_symbol = {row["symbol"]: row for row in payload["symbols"]}
    for symbol in ("600001", "600002", "600003", "600004"):
        for day in WINDOW_DAYS:
            single = audit_intraday_file(symbol, day, intraday_dir=project["intraday"])
            window_status = by_symbol[symbol]["day_status"][day]
            assert window_status == single["status"], (symbol, day, window_status, single["status"])


def test_summary_counts_and_verified_pool(project):
    payload = _run(project)
    assert payload["window_start"] == WINDOW_DAYS[0]
    assert payload["window_end"] == WINDOW_DAYS[-1]
    assert payload["trading_day_count"] == 3
    assert payload["total_symbols"] == 4
    by_symbol = {row["symbol"]: row for row in payload["symbols"]}
    assert by_symbol["600001"]["all_days_usable"] is True
    assert by_symbol["600002"]["all_days_usable"] is False
    assert by_symbol["600002"]["day_status"][WINDOW_DAYS[1]] == "missing"
    assert by_symbol["600003"]["day_status"][WINDOW_DAYS[1]] == "invalid"
    assert by_symbol["600004"]["day_status"][WINDOW_DAYS[2]] == "partial"
    assert payload["fully_usable_symbol_count"] == 1
    # per-day usable counts must reflect the holes honestly:
    # day0 all four usable; day1 only 600001 (600002/600004 missing, 600003 invalid)
    assert payload["per_day_usable_counts"][WINDOW_DAYS[0]] == 4
    assert payload["per_day_usable_counts"][WINDOW_DAYS[1]] == 1


def test_artifact_roundtrip_and_provenance(project):
    _run(project)
    artifact = json.loads(project["output"].read_text(encoding="utf-8"))
    assert artifact["task_type"] == "minute_pool_window_audit"
    assert artifact["criteria"]["usable_standard"] == "existing_intraday_quality_audit_v1"
    assert artifact["read_only"] is True
    assert artifact["network_used"] is False
    assert artifact["canonical_write"] is False
    assert artifact["intraday_dir"]
    assert artifact["generated_at"]


def test_window_pool_covers_symbol_and_date(project):
    _run(project)
    pool = load_window_verified_minute_pool(snapshot_path=project["output"])
    assert pool["passed"] is True
    assert window_pool_covers(pool, "600001", WINDOW_DAYS[1]) is True
    assert window_pool_covers(pool, "600002", WINDOW_DAYS[1]) is False  # missing day
    assert window_pool_covers(pool, "600002", WINDOW_DAYS[0]) is True   # usable day
    assert window_pool_covers(pool, "600003", WINDOW_DAYS[1]) is False  # invalid day
    assert window_pool_covers(pool, "600001", "2026-07-16") is False    # outside window
    assert window_pool_covers(pool, "999999", WINDOW_DAYS[0]) is False  # unknown symbol


def test_windowed_classifier_three_states(project):
    _run(project)
    pool = load_window_verified_minute_pool(snapshot_path=project["output"])
    verified = classify_minute_replay_evidence_windowed(
        "600001", WINDOW_DAYS[1], fill_model="intraday_1m_close", pool=pool
    )
    assert verified["status"] == "verified_completed_pool"
    assert verified["can_use_for_research_replay"] is True
    assert verified["pool_source_snapshot"] == "window_audit"
    unverified = classify_minute_replay_evidence_windowed(
        "600002", WINDOW_DAYS[1], fill_model="intraday_1m_close", pool=pool
    )
    assert unverified["status"] == "local_available_unverified_pool"
    missing = classify_minute_replay_evidence_windowed(
        "600001", WINDOW_DAYS[1], fill_model="missing_intraday", pool=pool
    )
    assert missing["status"] == "missing_intraday"
    outside = classify_minute_replay_evidence_windowed(
        "600001", "2026-07-16", fill_model="intraday_1m_close", pool=pool
    )
    assert outside["status"] == "local_available_unverified_pool"


def test_missing_snapshot_pool_fails_closed(tmp_path):
    pool = load_window_verified_minute_pool(snapshot_path=tmp_path / "absent.json")
    assert pool["passed"] is False
    assert window_pool_covers(pool, "600001", WINDOW_DAYS[0]) is False
    result = classify_minute_replay_evidence_windowed(
        "600001", WINDOW_DAYS[0], fill_model="intraday_1m_close", pool=pool
    )
    assert result["status"] == "local_available_unverified_pool"


def test_no_canonical_writes(project):
    files_before = {p: p.stat().st_mtime_ns for p in project["intraday"].glob("*.csv")}
    _run(project)
    files_after = {p: p.stat().st_mtime_ns for p in project["intraday"].glob("*.csv")}
    assert files_before == files_after


def test_symbol_subset_and_detail_cap(project):
    payload = _run(project, symbols=["600001", "600003"], max_exception_dates=1)
    assert payload["total_symbols"] == 2
    by_symbol = {row["symbol"]: row for row in payload["symbols"]}
    assert set(by_symbol) == {"600001", "600003"}
    # exception dates capped but the true count preserved
    row = by_symbol["600003"]
    assert row["not_usable_date_count"] == 1
    assert len(row["not_usable_dates"]) <= 1
