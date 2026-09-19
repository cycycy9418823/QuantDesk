"""Regression for the observed list-source outage, through the real A0 archive."""
from datetime import datetime
import csv
import json
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from quantdesk import data_providers
from quantdesk import snapshot_history as history


class Clock:
    def __init__(self):
        self.value = 0.0

    def __call__(self):
        return self.value


def quote(symbol, **extra):
    return {"symbol": symbol, "name": "fixture", "price": 11.0, "prev_close": 10.0,
            "open": 10.0, "high": 11.0, "low": 9.0, "volume": 1000, "amount": 11000,
            "date": "2026-09-14", "time": "10:00:01", **extra}


def batch(rows=(), errors=()):
    return SimpleNamespace(rows=list(rows), warnings=[], provider_errors=list(errors))


def fail_old_sources(monkeypatch, failure=None):
    def direct(**kwargs):
        raise failure or TimeoutError("recorded_list_timeout")

    monkeypatch.setattr(history, "_sina_direct_fetch", direct)
    monkeypatch.setattr(history, "_akshare_provider",
                        lambda name: (_ for _ in ()).throw(ConnectionError("recorded_adapter_failure")))


@pytest.mark.parametrize("cause", ["http456", "read_timeout"])
def test_known_list_failure_uses_hq_and_really_archives(tmp_path, monkeypatch, cause):
    from requests.exceptions import HTTPError, ReadTimeout
    failure = HTTPError("456 list source failure", response=SimpleNamespace(status_code=456)) if cause == "http456" else ReadTimeout("recorded_read_timeout")
    fail_old_sources(monkeypatch, failure)
    calls = []

    def fetch(keys, **kwargs):
        calls.append(list(keys))
        return batch(quote(key) for key in keys)

    monkeypatch.setattr(data_providers, "fetch_spot_sina_batch", fetch)
    calendar = tmp_path / "data/calendar/a_share_trading_calendar.csv"
    calendar.parent.mkdir(parents=True)
    calendar.write_text("date,is_open,source\n2026-09-14,1,fixture\n", encoding="utf-8")
    now = datetime(2026, 9, 14, 10, 0, 2, tzinfo=ZoneInfo("Asia/Shanghai"))
    result = history.collect_market_snapshot(
        tmp_path, symbols=["600000", "000001"], network=True, now=now, clock=lambda: now,
        fetcher=lambda symbols: history.default_network_fetch(symbols, budget_seconds=None),
    )
    assert result["written"] is True
    assert result["source"] == "sina.hq_batch"
    path = tmp_path / "data/snapshots/2026-09-14/1000.csv"
    with path.open(encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))
    assert {row["symbol"] for row in rows} == {"600000", "000001"}
    assert all(row["quote_date"] == "2026-09-14" and row["quote_time"] == "10:00:01" for row in rows)
    assert all(float(row["volume"]) == 1000 and float(row["amount"]) == 11000 for row in rows)
    assert all(float(row["pct_chg"]) == pytest.approx(10) for row in rows)
    meta = json.loads(path.with_suffix(".json").read_bytes())
    assert meta["status"] == "partial"  # Source failure remains visible.
    original = next(row for row in meta["failure_detail"]["provider_errors"] if row["source"] == "sina.spot_direct")
    assert original["outcome"] == "failed"
    if cause == "http456":
        assert original["http_status"] == 456
    assert len(calls) == 1


def test_normal_primary_does_not_call_any_fallback(monkeypatch):
    monkeypatch.setattr(history, "_sina_direct_fetch", lambda **kwargs: ([{"symbol": "600000", "price": 11}], "sina.spot_direct"))
    monkeypatch.setattr(data_providers, "fetch_spot_sina_batch", lambda *a, **k: pytest.fail("HQ must not run"))
    monkeypatch.setattr(history, "_akshare_provider", lambda *a: pytest.fail("AkShare must not run"))
    rows, source = history.default_network_fetch(["600000"])
    assert source == "sina.spot_direct" and len(rows) == 1
    assert rows.warnings == []


@pytest.mark.parametrize(("symbol_input", "key"), [
    ("600000", "sh600000"), ("SH600000", "sh600000"), ("000001.SH", "sh000001"),
    ("000001", "sz000001"), ("900901.SH", "sh900901"),
    ("920010", "bj920010"), ("830001.BJ", "bj830001"),
])
def test_hq_keeps_declared_market(symbol_input, key, monkeypatch):
    calls = []

    def fetch(keys, **kwargs):
        calls.append(list(keys))
        return batch([quote(key)])

    monkeypatch.setattr(data_providers, "fetch_spot_sina_batch", fetch)
    rows, source = history._sina_hq_fetch([symbol_input])
    assert calls == [[key]]
    assert rows[0]["symbol"] == key[2:]
    assert source == "sina.hq_batch"


def test_hq_refuses_unknown_request_without_fetch(monkeypatch):
    monkeypatch.setattr(data_providers, "fetch_spot_sina_batch", lambda *a, **k: pytest.fail("invalid key"))
    rows, _ = history._sina_hq_fetch(["hk920010", "bad600000", "bj1920010"])
    assert rows == [] and rows.warnings


def test_hq_requires_matching_market_and_original_date(monkeypatch):
    monkeypatch.setattr(data_providers, "fetch_spot_sina_batch", lambda *a, **k: batch([
        quote("hk600000"), quote("sz600000"), quote("sh600000", date=""),
        quote("sh600000", time=""), quote("sh600000"),
        quote("sh600000", price=99.0),
    ]))
    rows, _ = history._sina_hq_fetch(["600000"])
    assert len(rows) == 1 and rows[0]["price"] == 11
    assert rows.provider_errors[0]["rejected_count"] == 5


def test_hq_full_universe_is_batched_without_truncation(monkeypatch):
    requested = [f"{600000+i:06d}" for i in range(5561)]
    calls = []

    def fetch(keys, *, batch_size, timeout):
        calls.append((list(keys), batch_size, timeout))
        return batch(quote(key) for key in keys)

    monkeypatch.setattr(data_providers, "fetch_spot_sina_batch", fetch)
    rows, _ = history._sina_hq_fetch(requested)
    assert len(calls) == 56
    assert [key for chunk, _, _ in calls for key in chunk] == ["sh" + value for value in requested]
    assert [row["symbol"] for row in rows] == requested
    assert all(size <= 100 and 0 < timeout <= 5 for _, size, timeout in calls)


def test_hq_short_budget_stops_later_batches(monkeypatch):
    clock = Clock()
    calls = []

    def fetch(keys, *, batch_size, timeout):
        calls.append(timeout)
        clock.value += timeout if len(calls) == 2 else 3.0
        return batch(quote(key) for key in keys)

    monkeypatch.setattr(data_providers, "fetch_spot_sina_batch", fetch)
    rows, _ = history._sina_hq_fetch([f"{600000+i:06d}" for i in range(201)], clock=clock, budget_seconds=4)
    assert calls == [4.0, 1.0]
    assert len(rows) == 200
    assert "sina.hq_batch:budget_exhausted" in rows.warnings


def test_hq_failed_batch_preserves_other_batches_and_diagnostics(monkeypatch):
    fail_old_sources(monkeypatch)
    calls = []

    def fetch(keys, **kwargs):
        calls.append(list(keys))
        return batch(errors=["read timed out"]) if keys[0] == "sh600100" else batch(quote(key) for key in keys)

    monkeypatch.setattr(data_providers, "fetch_spot_sina_batch", fetch)
    rows, source = history.default_network_fetch([f"{600000+i:06d}" for i in range(201)], budget_seconds=None)
    assert source == "sina.hq_batch" and len(rows) == 101
    assert len(calls) == 4
    assert any(item.get("batch_start") == 100 and "read timed out" in item["message"] for item in rows.provider_errors)
    assert "snapshot_provider_coverage_incomplete" in rows.warnings
    assert any(item["source"] == "sina.spot_direct" for item in rows.provider_errors)


def test_slow_primary_cannot_give_hq_a_second_full_budget(monkeypatch):
    clock = Clock()

    def direct(**kwargs):
        clock.value = 90.0
        raise TimeoutError("pagination exhausted its original 90 seconds")

    timeouts = []

    def fetch(keys, *, batch_size, timeout):
        timeouts.append(timeout)
        clock.value += timeout
        return batch(quote(key) for key in keys)

    monkeypatch.setattr(history, "_sina_direct_fetch", direct)
    monkeypatch.setattr(data_providers, "fetch_spot_sina_batch", fetch)
    monkeypatch.setattr(history, "_akshare_provider", lambda *a: pytest.fail("do not lose partial rows to unbounded adapters"))
    rows, source = history.default_network_fetch([f"{600000+i:06d}" for i in range(701)], clock=clock, budget_seconds=None)
    assert source == "sina.hq_batch" and len(rows) == 600
    assert sum(timeouts) == 30 and clock() == 120
    assert "sina.hq_batch:budget_exhausted" in rows.warnings


def test_hq_does_not_invent_movement_without_previous_price(monkeypatch):
    monkeypatch.setattr(data_providers, "fetch_spot_sina_batch", lambda *a, **k: batch([quote("sh600000", prev_close=None)]))
    rows, _ = history._sina_hq_fetch(["600000"])
    assert rows[0]["prev_close"] is None
    assert "pct_chg" not in rows[0] and "change" not in rows[0]


def test_hq_timeout_preserves_partial_rows_from_primary(monkeypatch):
    clock = Clock()

    def direct(**kwargs):
        clock.value = 89.0
        return [{"symbol": "600000", "price": 11}], "sina.spot_direct"

    def fetch(keys, *, batch_size, timeout):
        clock.value += timeout
        return batch(errors=["HQ timed out"])

    monkeypatch.setattr(history, "_sina_direct_fetch", direct)
    monkeypatch.setattr(data_providers, "fetch_spot_sina_batch", fetch)
    monkeypatch.setattr(history, "_akshare_provider", lambda *a: pytest.fail("preserve primary partial rows"))
    rows, source = history.default_network_fetch([f"{600000+i:06d}" for i in range(701)], clock=clock, budget_seconds=None)
    assert source == "sina.spot_direct" and len(rows) == 1
    assert clock() == 120
    assert "sina.hq_batch:budget_exhausted" in rows.warnings


def test_hq_transient_handshake_retries_only_with_remaining_budget(monkeypatch):
    clock = Clock()
    timeouts = []

    def fetch(keys, *, batch_size, timeout):
        timeouts.append(timeout)
        if len(timeouts) == 1:
            clock.value += 2.0
            return batch(errors=["The handshake operation timed out"])
        return batch(quote(key) for key in keys)

    monkeypatch.setattr(data_providers, "fetch_spot_sina_batch", fetch)
    rows, _ = history._sina_hq_fetch(["600000"], clock=clock, budget_seconds=4)
    assert len(rows) == 1 and timeouts == [4.0, 2.0]
    assert rows.provider_errors[0]["attempt"] == 1
    assert "handshake" in rows.provider_errors[0]["message"]


def test_hq_persistent_transport_failure_is_only_retried_once(monkeypatch):
    calls = []

    def fetch(keys, **kwargs):
        calls.append(list(keys))
        return batch(errors=["connection unavailable"])

    monkeypatch.setattr(data_providers, "fetch_spot_sina_batch", fetch)
    rows, _ = history._sina_hq_fetch(["600000"], clock=lambda: 0.0)
    assert rows == [] and len(calls) == 2
    assert [item["attempt"] for item in rows.provider_errors if "attempt" in item] == [1, 2]
