"""Actual collector fallback: dated raw OHLCV, bounded attempts, honest source."""

import csv
import json
import math
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from quantdesk.intraday import intraday_providers as providers
from quantdesk.intraday_refresh import collector


NOW = datetime(2026, 9, 14, 10, 0, tzinfo=ZoneInfo("Asia/Shanghai"))


class Clock:
    def __init__(self):
        self.value = 0.0

    def __call__(self):
        return self.value


def bars():
    return [
        {"day": f"2026-09-14 {stamp}", "open": "9.40", "high": "9.42",
         "low": "9.39", "close": "9.41", "volume": "200"}
        for stamp in ("09:59:00", "10:00:00")
    ]


@pytest.fixture
def native_failure(monkeypatch):
    calls = []

    def fail(symbol, **kwargs):
        calls.append((symbol, kwargs))
        return [], "provider_failed:mootdx:TdxFunctionCallError", {
            "error_stage": "request", "message": "unpack requires a buffer of 4 bytes",
            "stages": [{"stage": "request", "outcome": "failed", "duration_ms": 1}],
        }

    monkeypatch.setattr(collector, "_mootdx_minute_rows", fail)
    return calls


def serve(monkeypatch, payload):
    requests = []

    def get(url, *, timeout):
        requests.append((url, timeout))
        return json.dumps(payload)

    monkeypatch.setattr(providers, "_http_get_text", get)
    return requests


@pytest.mark.parametrize("symbol,prefix", [("600000", "sh"), ("000001", "sz"), ("300001", "sz")])
def test_native_failure_recovers_real_dated_ohlcv(monkeypatch, native_failure, symbol, prefix):
    requests = serve(monkeypatch, bars())
    rows, error, evidence = collector._live_minute_rows(symbol, offset=60, now=NOW)
    assert error == "" and len(rows) == 2
    assert f"symbol={prefix}{symbol}" in requests[0][0]
    assert 0 < requests[0][1] <= 8
    assert len(native_failure) == 1
    assert evidence["fallback_from"] == "mootdx" and evidence["primary_error"]
    for row in rows:
        assert row["symbol"] == symbol and row["date"] == "2026-09-14"
        assert row["source"] == "sina_intraday" and row["price_basis"] == "raw"
        assert (row["open"], row["high"], row["low"], row["close"], row["volume"]) == (9.4, 9.42, 9.39, 9.41, 200)
        assert math.isnan(row["amount"])  # This source did not supply turnover.


def test_healthy_native_does_not_request_fallback(monkeypatch):
    expected = [{"source": "mootdx_minute", "close": 9.4}]
    monkeypatch.setattr(collector, "_mootdx_minute_rows", lambda *a, **k: (expected, "", {}))
    requests = serve(monkeypatch, bars())
    assert collector._live_minute_rows("600000", offset=60, now=NOW)[0] == expected
    assert requests == []


@pytest.mark.parametrize("value", [-1, float("nan"), float("inf"), None])
def test_two_good_rows_do_not_validate_bad_volume_tail(monkeypatch, native_failure, value):
    payload = bars() + [{**bars()[-1], "day": "2026-09-14 10:01:00", "volume": value}]
    serve(monkeypatch, payload)
    rows, error, evidence = collector._live_minute_rows("600000", offset=60, now=NOW)
    assert error == "" and len(rows) == 2
    assert max(row["datetime"] for row in rows) == "2026-09-14 10:00:00"
    assert evidence["rejected_rows"] == 1 and evidence["rejection_reasons"]


@pytest.mark.parametrize("kind", ["old_day", "time_only", "price_only", "missing_volume", "future"])
def test_unproven_rows_do_not_become_current_ohlcv(monkeypatch, native_failure, kind):
    payload = bars()
    for row in payload:
        if kind == "old_day":
            row["day"] = row["day"].replace("2026-09-14", "2026-09-11")
        elif kind == "time_only":
            row["day"] = row["day"].split(" ")[1]
        elif kind == "price_only":
            for key in ("open", "high", "low"):
                row.pop(key)
        elif kind == "missing_volume":
            row.pop("volume")
        else:
            row["day"] = row["day"].replace("2026-09-14", "2026-09-15")
    serve(monkeypatch, payload)
    rows, error, evidence = collector._live_minute_rows("600000", offset=60, now=NOW)
    assert rows == [] and error
    assert "sina_minute_ohlcv_unavailable" in evidence["fallback_error"]


@pytest.mark.parametrize("metadata", [{"price_basis": "qfq"}, {"price_only_intraday": True}])
def test_fallback_requires_raw_ohlcv_classification(monkeypatch, native_failure, metadata):
    serve(monkeypatch, bars())
    original = providers._normalize_provider_frame

    def classified(*args, **kwargs):
        return {**original(*args, **kwargs), **metadata}

    monkeypatch.setattr(providers, "_normalize_provider_frame", classified)
    rows, error, _ = collector._live_minute_rows("600000", offset=60, now=NOW)
    assert rows == [] and error


def test_no_fallback_after_total_deadline(monkeypatch):
    clock = Clock()

    def exhausted(*args, **kwargs):
        clock.value = 8
        return [], "provider_failed:mootdx:TimeoutError", {}

    monkeypatch.setattr(collector, "_mootdx_minute_rows", exhausted)
    requests = serve(monkeypatch, bars())
    rows, error, _ = collector._live_minute_rows("600000", offset=60, now=NOW, monotonic=clock)
    assert rows == [] and error and requests == []


def test_late_fallback_cannot_report_success(monkeypatch, native_failure):
    clock = Clock()

    def late(url, *, timeout):
        clock.value = 8.1
        return json.dumps(bars())

    monkeypatch.setattr(providers, "_http_get_text", late)
    rows, error, evidence = collector._live_minute_rows("600000", offset=60, now=NOW, monotonic=clock)
    assert rows == [] and error
    assert evidence["fallback_error"].startswith("TimeoutError:")
    assert len(native_failure) == 1


def test_fallback_failure_leaves_only_remaining_time_for_native(monkeypatch, native_failure):
    clock = Clock()

    def fail(url, *, timeout):
        clock.value = 3.0
        raise ConnectionError("fixture Sina outage")

    monkeypatch.setattr(providers, "_http_get_text", fail)
    rows, error, evidence = collector._live_minute_rows("600000", offset=60, now=NOW, monotonic=clock)
    assert rows == [] and error and len(native_failure) == 2
    assert native_failure[0][1]["timeout_seconds"] == 2.0
    assert native_failure[1][1]["timeout_seconds"] == 5.0
    assert native_failure[1][1]["max_attempts"] == 2
    assert "fixture Sina outage" in evidence["fallback_error"]


def test_bj_keeps_native_mapping(monkeypatch, native_failure):
    requests = serve(monkeypatch, bars())
    rows, error, _ = collector._live_minute_rows("920010", offset=60, now=NOW)
    assert rows == [] and error and requests == []
    assert len(native_failure) == 1 and native_failure[0][1]["timeout_seconds"] == 8


def test_real_collector_appends_source_and_deduplicates(monkeypatch, native_failure, tmp_path):
    serve(monkeypatch, bars())
    account = tmp_path / "state/ai_trader"
    account.mkdir(parents=True)
    (account / "ledger.json").write_text(json.dumps({"positions": {"600000": {"qty": 100}}}), encoding="utf-8")
    universe = tmp_path / "data/universe"
    universe.mkdir(parents=True)
    (universe / "a_share_symbols.csv").write_text("symbol\n600000\n", encoding="utf-8")
    monkeypatch.setattr(collector, "refresh_full_market_snapshot", lambda *a, **k: {
        "ok": False, "status": "skipped_in_test", "row_count": 0,
        "movers": [], "mover_count": 0, "reason_codes": [],
    })
    first = collector.collect_intraday_refresh(tmp_path, network=True, now=NOW)
    second = collector.collect_intraday_refresh(tmp_path, network=True, now=NOW)
    assert first["provider_status"][0]["appended"] == 2
    assert second["provider_status"][0]["appended"] == 0
    assert first["provider_status"][0]["diagnostics"]["fallback_from"] == "mootdx"
    with (tmp_path / "data/intraday/1m/600000.csv").open(encoding="utf-8", newline="") as handle:
        written = list(csv.DictReader(handle))
    assert len(written) == 2
    assert {row["source"] for row in written} == {"sina_intraday"}
    assert {row["price_basis"] for row in written} == {"raw"}
