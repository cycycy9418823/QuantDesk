"""1C-A: the spot cache keeps the provider's quote time, never the round clock.

2026-10-09 15:00: all 3600 rows of data/cache/spot_snapshot_latest.csv read
quote_time=15:00:01 because the missing provider time was filled with now,
while the A0 archive of the same minute kept quote_time=14:55:03 plus a
separate fetched_at.  These tests fail on the old normalizer.
"""
from __future__ import annotations

import csv
import json
import os
from datetime import datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from quantdesk.intraday_refresh import snapshot

SH = ZoneInfo("Asia/Shanghai")
NOW = datetime(2026, 10, 9, 15, 0, 1, tzinfo=SH)


def _sina(code: str, **extra) -> dict:
    row = {"symbol": f"sh{code}", "code": code, "name": "fixture", "trade": "10.0",
           "changepercent": "1.0", "pricechange": "0.1", "volume": "100", "amount": "1000",
           "open": "9.9", "settlement": "9.9", "high": "10.1", "low": "9.8"}
    row.update(extra)
    return row


def test_sina_ticktime_becomes_quote_time_not_the_round_clock():
    rows = snapshot.normalize_spot_rows([_sina("600000", ticktime="14:55:03")], source="sina.spot_direct", now=NOW)
    assert rows[0]["quote_time"] == "14:55:03"
    # The list endpoint sends no date: it stays missing instead of "today".
    assert rows[0]["quote_date"] == ""


def test_missing_provider_time_stays_empty():
    rows = snapshot.normalize_spot_rows([_sina("600000")], source="sina.spot_direct", now=NOW)
    assert rows[0]["quote_time"] == "" and rows[0]["quote_date"] == ""
    assert rows[0]["fetched_at"] == NOW.isoformat(timespec="seconds")


def test_provider_date_and_time_are_kept_as_sent():
    raw = {"code": "000001", "trade": "11.5", "date": "2026-10-09", "time": "15:00:03"}
    rows = snapshot.normalize_spot_rows([raw], source="sina.hq", now=NOW)
    assert rows[0]["quote_date"] == "2026-10-09" and rows[0]["quote_time"] == "15:00:03"


def test_fetched_at_is_the_moment_rows_arrived(tmp_path, monkeypatch):
    # First tick: round start. Second tick: rows in hand 37 seconds later.
    ticks = iter([100.0, 137.0])
    clock = lambda: next(ticks, 137.0)  # noqa: E731

    def fetcher():
        return [_sina("600000", ticktime="14:59:58")], "fixture.sina"

    result = snapshot.refresh_full_market_snapshot(
        tmp_path, network=True, now=NOW, fetcher=fetcher, clock=clock,
    )
    assert result["published"] is True
    with (tmp_path / snapshot.SPOT_RELATIVE).open(encoding="utf-8-sig", newline="") as handle:
        saved = list(csv.DictReader(handle))
    assert saved[0]["quote_time"] == "14:59:58"
    assert saved[0]["fetched_at"] == "2026-10-09T15:00:38+08:00"
    meta = json.loads((tmp_path / snapshot.SPOT_META_RELATIVE).read_text(encoding="utf-8"))
    assert meta["fetched_at"] == "2026-10-09T15:00:38+08:00"
    assert meta["quote_time_basis"] == snapshot.QUOTE_TIME_BASIS
    assert meta["quote_time_missing_count"] == 0 and meta["quote_date_missing_count"] == 1


def test_paged_sina_rows_keep_their_ticktime(tmp_path, monkeypatch):
    import requests
    import time

    page = [_sina(f"600{i:03d}", ticktime="11:29:59") for i in range(30)]
    monkeypatch.setattr(requests, "get", lambda *a, **kw: SimpleNamespace(
        raise_for_status=lambda: None, json=lambda: page if kw["params"]["page"] == 1 else []))
    monkeypatch.setattr(time, "sleep", lambda seconds: None)
    result = snapshot.refresh_full_market_snapshot(
        tmp_path, network=True, now=NOW.replace(hour=11, minute=30, second=5), budget_seconds=20, clock=lambda: 0,
    )
    assert result["status"] == "refreshed"
    with (tmp_path / snapshot.SPOT_RELATIVE).open(encoding="utf-8-sig", newline="") as handle:
        saved = list(csv.DictReader(handle))
    assert {row["quote_time"] for row in saved} == {"11:29:59"}
    assert {row["fetched_at"] for row in saved} == {"2026-10-09T11:30:05+08:00"}


def test_cache_write_is_atomic_when_replace_fails(tmp_path, monkeypatch):
    path = tmp_path / snapshot.SPOT_RELATIVE
    first = snapshot.normalize_spot_rows([_sina("600000", ticktime="10:00:00")], source="t", now=NOW)
    snapshot.write_spot_snapshot(tmp_path, first, source="t", now=NOW)
    before = path.read_bytes()

    def broken_replace(src, dst):
        raise PermissionError("held by a reader")

    monkeypatch.setattr(os, "replace", broken_replace)
    monkeypatch.setattr("time.sleep", lambda seconds: None)
    second = snapshot.normalize_spot_rows([_sina("600001", ticktime="10:05:00")], source="t", now=NOW)
    with pytest.raises(PermissionError):
        snapshot.write_spot_snapshot(tmp_path, second, source="t", now=NOW)
    assert path.read_bytes() == before
    assert not list(path.parent.glob(".*.tmp"))
