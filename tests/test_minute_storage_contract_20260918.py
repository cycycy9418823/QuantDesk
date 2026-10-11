"""Minute archives keep column identity, append idempotency and honest health."""
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo
import csv

import pytest

from quantdesk.intraday_refresh import collector


def _bar(stamp: str = "2026-09-17 10:00:00") -> dict:
    return {"symbol": "600000", "datetime": stamp, "date": stamp[:10], "time": stamp[11:],
            "open": 10.0, "high": 10.5, "low": 9.5, "close": 10.2, "volume": 100,
            "amount": 1020, "source": "fixture", "updated_at": stamp, "price_basis": "raw"}


def _write(path: Path, rows: list[dict], columns=None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(columns or collector.MINUTE_COLUMNS), extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def test_reordered_header_deduplicates_using_datetime_name(tmp_path: Path) -> None:
    path = tmp_path / "600000.csv"
    columns = ["datetime", "symbol", *collector.MINUTE_COLUMNS[2:]]
    _write(path, [_bar()], columns)
    before = path.read_bytes()
    accepted, reasons = collector._append_rows(path, [_bar()], now_iso="2026-09-17T10:01:00")
    assert accepted == 0
    assert reasons[0].startswith("duplicate_minute_bar:")
    assert path.read_bytes() == before
    assert collector._last_datetime_of(path) == "2026-09-17 10:00:00"


def test_duplicate_outside_old_tail_window_is_not_appended(tmp_path: Path) -> None:
    path = tmp_path / "600000.csv"
    start = datetime(2026, 9, 1, 9, 30)
    rows = [_bar((start + timedelta(minutes=i)).strftime("%Y-%m-%d %H:%M:%S")) for i in range(2200)]
    _write(path, rows)
    assert path.stat().st_size > 200_000
    before = path.read_bytes()
    accepted, reasons = collector._append_rows(path, [rows[0]], now_iso="2026-09-17T10:01:00")
    assert accepted == 0 and reasons[0].startswith("duplicate_minute_bar:")
    assert path.read_bytes() == before


@pytest.mark.parametrize("stamp", ["2026-09-17 10:00", "2026-09-17T10:00:00"])
def test_equivalent_timestamp_spelling_is_the_same_bar(tmp_path: Path, stamp: str) -> None:
    path = tmp_path / "600000.csv"
    _write(path, [_bar()])
    before = path.read_bytes()
    accepted, _ = collector._append_rows(path, [_bar(stamp)], now_iso="2026-09-17T10:01:00")
    assert accepted == 0
    assert path.read_bytes() == before


def test_late_historical_append_does_not_hide_newer_existing_bar(tmp_path: Path) -> None:
    path = tmp_path / "600000.csv"
    _write(path, [_bar("2026-09-17 10:00:00"), _bar("2026-09-16 15:00:00")])
    assert collector._last_datetime_of(path) == "2026-09-17 10:00:00"


@pytest.mark.parametrize("columns", [
    # Only the exact old schema missing price_basis has a separate-file route.
    # Other missing columns still block; keep all failure assertions below.
    [name for name in collector.MINUTE_COLUMNS if name != "source"],
    [*collector.MINUTE_COLUMNS, "datetime"],
])
def test_invalid_schema_is_visible_in_round_terminal(tmp_path: Path, monkeypatch, columns) -> None:
    path = tmp_path / "data/intraday/1m/600000.csv"
    _write(path, [_bar("2026-09-17 09:59:00")], columns)
    before = path.read_bytes()
    monkeypatch.setattr(collector, "refresh_full_market_snapshot", lambda *a, **k: {"movers": [], "reason_codes": []})
    result = collector.collect_intraday_refresh(
        tmp_path, network=True, trading_day=True,
        now=datetime(2026, 9, 17, 10, 1, tzinfo=ZoneInfo("Asia/Shanghai")),
        fetcher=lambda *_a: ([_bar()], ""),
        scope_builder=lambda *a, **k: {"symbols": [{"symbol": "600000", "priority": "critical"}], "warnings": []},
    )
    assert result["status"] != "ready"
    assert result["fresh_count"] == 0
    assert result["provider_status"][0]["status"] == "storage_blocked"
    assert result["provider_status"][0]["error_stage"] == "storage"
    assert "minute_header_mismatch:600000.csv" in result["reason_codes"]
    assert result["provider_failed_count"] == 0
    assert path.read_bytes() == before


def test_normal_append_keeps_existing_bytes_and_all_column_values(tmp_path: Path) -> None:
    path = tmp_path / "600000.csv"
    _write(path, [_bar("2026-09-17 09:59:00")])
    before = path.read_bytes()
    accepted, reasons = collector._append_rows(path, [_bar()], now_iso="2026-09-17T10:01:00")
    assert accepted == 1 and reasons == []
    assert path.read_bytes().startswith(before)
    with path.open(encoding="utf-8", newline="") as stream:
        rows = list(csv.DictReader(stream))
    assert len(rows) == 2 and rows[-1]["price_basis"] == "raw"
    assert rows[-1]["source"] == "fixture" and float(rows[-1]["close"]) == 10.2
