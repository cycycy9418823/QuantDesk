"""Read-only S04 diagnostics against unchanged accepted S03 source bytes."""
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo
import csv
import json

import pytest

from quantdesk import snapshot_history as history

TZ = ZoneInfo("Asia/Shanghai")


def at(value: str) -> datetime:
    return datetime.fromisoformat("2026-09-07T" + value).replace(tzinfo=TZ)


@pytest.mark.parametrize(("date", "time", "expected"), [
    ("2026-09-07", "10:00:10", "usable"),
    ("2026-09-07", "10:00:15", "usable"),
    ("2026-09-07", "10:00:16", "future"),
    ("2026-09-06", "15:00:00", "stale"),
    ("2026-09-08", "10:00:10", "future"),
    ("2026-09-07", "09:30:00", "usable"),
    ("2026-09-07", "", "missing"),
    ("2026-09-07", "invalid", "unparseable"),
])
def test_current_timestamp_boundary(date: str, time: str, expected: str) -> None:
    assert history.QUOTE_TIME_CLOCK_SKEW_SECONDS == 5
    assert history._quote_time_state(
        {"quote_date": date, "quote_time": time}, "2026-09-07", fetched_at=at("10:00:10")
    ) == expected


@pytest.mark.parametrize(("started", "completed", "quote", "expected_state", "outside"), [
    ("10:00:00", "10:00:30", "10:00:20", "usable", False),
    ("10:00:00", "10:00:30", "10:00:36", "future", False),
    ("10:04:59", "10:05:01", "10:05:00", "usable", True),
])
def test_real_collection_uses_completion_time(
    tmp_path: Path, started: str, completed: str, quote: str, expected_state: str, outside: bool
) -> None:
    calendar = tmp_path / "data/calendar/a_share_trading_calendar.csv"
    calendar.parent.mkdir(parents=True)
    calendar.write_text("date,is_open,source\n2026-09-07,1,fixture\n", encoding="utf-8")
    ticks = iter([at(started), at(completed)])
    raw = [{"symbol": "600000", "name": "fixture", "price": 10.0, "quote_date": "2026-09-07", "quote_time": quote}]
    result = history.collect_market_snapshot(
        tmp_path, network=True, now=at(started), clock=lambda: next(ticks),
        fetcher=lambda: (raw, "fixture.s04"), symbols=["600000"],
    )
    assert result["attempt_started_at"] == at(started).isoformat()
    assert result["fetched_at"] == result["attempt_completed_at"] == at(completed).isoformat()
    assert result["quote_time_states"] == {expected_state: 1}
    assert ("fetch_completed_outside_requested_slot" in result["warnings"]) is outside
    assert result["status"] == ("partial" if outside or expected_state != "usable" else "written")
    assert result["written"] is True
    metadata = json.loads((tmp_path / result["metadata_path"]).read_text(encoding="utf-8"))
    with (tmp_path / result["snapshot_path"]).open(encoding="utf-8", newline="") as stream:
        row = next(csv.DictReader(stream))
    assert row["quote_time"] == quote
    assert row["fetched_at"] == metadata["fetched_at"] == at(completed).isoformat()
    evidence = history._slot_evidence(tmp_path, "2026-09-07", "1000")
    assert evidence["quality"]["fetch_outside_slot"] is outside
    assert evidence["quality"]["quote_time_states"][expected_state] == 1
    if result["status"] == "partial":
        summary = history.snapshot_day_summary(tmp_path, "2026-09-07", symbols=["600000"], as_of=at(completed))
        assert summary["invalid_attempts"] == {}
        assert len(summary["slot_attempts"]["1000"]) == 1
