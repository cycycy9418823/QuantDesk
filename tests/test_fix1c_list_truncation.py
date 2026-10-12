"""1C-C: a failed list page is a failure; a truncated list's gap is filled; bars
have their own deadline.

2026-10-08 15:40: a list page failed three times and was taken as "end of the
list" -> 900 rows, 4670 roster codes missing, 50 re-checked one by one, 4620
without a bar. 2026-10-09 15:40: every bar was written by 15:47:31 and the stage
was still killed at its 900 s limit, so the whole run read "failed".
These tests fail on the old refresh script.
"""
from __future__ import annotations

import csv
import io
import json
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts import run_data_close_pipeline as pipeline
from scripts import run_daily_data_refresh as refresh

TARGET = "2026-10-09"


def _item(code: str) -> dict:
    return {"code": code, "name": "fixture", "open": 10.0, "high": 10.2, "low": 9.8,
            "trade": 10.1, "volume": 10000, "amount": 100500.0, "per": 12.0}


def _quote(code: str) -> dict:
    return {"symbol": "sh" + code, "name": "fixture", "date": TARGET, "time": "15:00:00", "open": 10.0,
            "high": 10.2, "low": 9.8, "price": 10.1, "volume": 10000, "amount": 100500.0, "prev_close": 10.0}


class _Session:
    """Fake Sina list endpoint: ``pages`` maps page -> rows, an exception, or a status code."""

    def __init__(self, pages):
        self.pages = pages
        self.calls = []

    def get(self, url, params, headers, timeout):
        page = params["page"]
        self.calls.append(page)
        value = self.pages.get(page, [])
        if isinstance(value, Exception):
            raise value
        if isinstance(value, int):
            return SimpleNamespace(status_code=value, json=lambda: None, encoding="")
        return SimpleNamespace(status_code=200, json=lambda: value, encoding="")


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    monkeypatch.setattr(refresh.time, "sleep", lambda seconds: None)


def _full_page(start: int) -> list[dict]:
    return [_item(f"60{start + i:04d}") for i in range(100)]


def test_failed_page_is_truncation_not_end_of_list():
    session = _Session({1: _full_page(0), 2: TimeoutError("read timed out")})
    rows = refresh.fetch_market_snapshot(session)
    assert len(rows) == 100
    assert session.calls == [1, 2, 2, 2]
    assert rows.pagination["complete"] is False
    assert rows.pagination["end"] == "page_failed" and rows.pagination["failed_page"] == 2
    assert rows.pagination["reason_codes"] == ["list_page_failed:page=2:timeout"]


def test_rate_limit_status_is_named():
    rows = refresh.fetch_market_snapshot(_Session({1: _full_page(0), 2: 456}))
    assert rows.pagination["complete"] is False
    assert rows.pagination["reason_codes"] == ["list_page_failed:page=2:http_456"]


@pytest.mark.parametrize(("pages", "end", "count"), [
    ({1: _full_page(0), 2: []}, "empty_page", 100),
    ({1: _full_page(0)[:30]}, "short_page", 30),
])
def test_clean_end_of_list_is_complete(pages, end, count):
    rows = refresh.fetch_market_snapshot(_Session(pages))
    assert len(rows) == count
    assert rows.pagination["complete"] is True and rows.pagination["end"] == end


@pytest.fixture
def setup_refresh(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    for name, relative in [
        ("PROJECT_ROOT", "."), ("DAILY_DIR", "data/daily"),
        ("META_DIR", "data/metadata"), ("VAL_DIR", "data/metadata/valuation_snapshots"),
    ]:
        monkeypatch.setattr(refresh, name, tmp_path / relative)
    monkeypatch.setattr(refresh, "_complete_live_session", lambda target: True)
    monkeypatch.setattr(refresh.requests, "Session", lambda: SimpleNamespace(close=lambda: None))
    return tmp_path, SimpleNamespace(assert_active=lambda: None, run_id="fixture-1c")


def _truncated_list(codes: list[str]):
    # A full first page, then a page that fails: the list is cut, not ended.
    rows = refresh.fetch_market_snapshot(_Session({1: [_item(c) for c in codes], 2: TimeoutError("x")})) \
        if hasattr(refresh, "_ListRows") else [_item(c) for c in codes]
    return rows


def test_truncated_list_gap_is_filled_in_batches(setup_refresh, monkeypatch):
    root, lease = setup_refresh
    first_page = [f"60{i:04d}" for i in range(100)]
    gap = [f"60{i:04d}" for i in range(100, 350)]
    monkeypatch.setattr(refresh, "_load_universe_symbols", lambda: [*first_page, *gap])
    listed = _truncated_list(first_page)
    monkeypatch.setattr(refresh, "fetch_market_snapshot", lambda session: listed)
    seen: list[list[str]] = []

    def fetch(symbols):
        seen.append(list(symbols))
        return {code: _quote(code) for code in symbols}

    monkeypatch.setattr(refresh, "_dated_close_quotes", fetch)
    result = refresh.family_bars_and_valuation(TARGET, True, lease=lease)

    # One batched pass for the whole gap (chunked by 100 inside), not 50 single requests.
    assert seen == [first_page, gap]
    assert result["bars"]["appended"] == 350
    assert result["list_truncated"] is True and result["list_gap_fill"]["filled"] == 250
    assert any(w.startswith("snapshot_list_truncated:100_rows:list_page_failed:page=2") for w in result["warnings"])
    assert "valuation_partial:list_truncated" in result["warnings"]
    gap_rows = list(csv.DictReader(io.StringIO((root / "data/daily/600349.csv").read_text())))
    assert gap_rows[0]["source"] == "sina_dated_hq_list_gap_fill" and gap_rows[0]["date"] == TARGET
    valuation = list(csv.DictReader(io.StringIO(
        (root / "data/metadata/valuation_snapshots" / f"{TARGET.replace('-', '')}.csv").read_text())))
    assert [row["symbol"] for row in valuation] == first_page


def test_bars_stop_at_their_own_deadline_and_report_partial(setup_refresh, monkeypatch):
    root, lease = setup_refresh
    codes = ["600000", "600001", "600002"]
    monkeypatch.setattr(refresh, "_load_universe_symbols", lambda: codes)
    complete = refresh.fetch_market_snapshot(_Session({1: [_item(c) for c in codes]}))
    monkeypatch.setattr(refresh, "fetch_market_snapshot", lambda session: complete)
    monkeypatch.setattr(refresh, "_dated_close_quotes", lambda symbols: {c: _quote(c) for c in symbols})
    checks = iter([False])
    monkeypatch.setattr(refresh, "_bars_expired", lambda: next(checks, True), raising=False)

    result = refresh.family_bars_and_valuation(TARGET, True, lease=lease)

    assert result["status"] == "partial" and result["ok"] is True
    assert result["bars"]["appended"] == 1
    assert result["bars_deadline"]["hit"] is True and result["bars_deadline"]["unprocessed_count"] == 2
    assert not (root / "data/daily/600001.csv").exists()


def test_partial_bars_make_the_refresh_partial_and_deadlines_leave_report_time(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(refresh, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(refresh, "RESULTS_DIR", tmp_path / "results/data_refresh")
    monkeypatch.setattr(refresh, "_complete_live_session", lambda target: True)
    seen = {}

    def bars(*args, **kwargs):
        seen["bars_left"] = refresh._BARS_DEADLINE.get() - time.monotonic()
        seen["optional_left"] = refresh._remaining_refresh_seconds()
        return {"ok": True, "status": "partial", "warnings": ["bars_deadline_exhausted:unprocessed=2:not_requested=0"]}

    monkeypatch.setattr(refresh, "family_bars_and_valuation", bars)
    monkeypatch.setattr(refresh, "family_news", lambda *a, **k: {"ok": True})
    monkeypatch.setattr(refresh, "family_quarterly", lambda *a, **k: {"ok": True})
    monkeypatch.setattr(refresh, "family_benchmarks", lambda network, *, lease, target_date: {
        "ok": True, "status": "up_to_date", "target_date": target_date, "target_covered": True,
        "freshness_status": "current", "coverage": {"requested": 3, "target_covered": 3}, "reason_codes": []})
    owner, lock = pipeline.acquire_pipeline_lock(tmp_path, run_id="fix1c-cli")
    try:
        for key, value in pipeline._grant_market_writer(tmp_path, run_id="fix1c-cli", target=TARGET, owner=owner).items():
            monkeypatch.setenv(key, value)
        code = refresh.main(["--enable-network", "--target-date", TARGET, "--pipeline-run-id", "fix1c-cli",
                             "--max-refresh-seconds", "870"])
    finally:
        pipeline.release_pipeline_lock(owner, lock, run_id="fix1c-cli")
    result = json.loads(capsys.readouterr().out)
    assert result["status"] == "partial" and code == 4
    # Bars end first; the optional families end before the whole budget.
    assert seen["bars_left"] < seen["optional_left"] < 870
    assert 870 - seen["bars_left"] >= 80
