from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from quantdesk.intraday import bse_minute_provider as provider
from quantdesk.intraday.minute_bar_lifecycle import bar_evidence_status, normalize_bar_observation
from quantdesk.intraday.intraday_schema import read_intraday_csv
from quantdesk.intraday_refresh import collector

NOW = datetime(2026, 10, 26, 10, 3, 20, tzinfo=ZoneInfo("Asia/Shanghai"))


def payload(lines: list[str] | None = None, *, code: str = "920786", market: int = 0) -> dict:
    return {"rc": 0, "data": {"code": code, "market": market, "trends": lines or [
        "2026-10-26 09:30,6.50,6.50,6.50,6.50,10,6500,6.5",
        "2026-10-26 09:31,6.50,6.60,6.70,6.40,12,7900,6.5",
        "2026-10-26 10:03,6.60,6.70,6.70,6.60,3,2000,6.6",
    ]}}


def test_real_fields_remain_observations_and_current_minute_is_excluded():
    rows = provider.parse_bse_minute_payload(payload(), symbol="920786", now=NOW)
    assert len(rows) == 1 and rows[0]["datetime"] == "2026-10-26 09:31:00"
    assert rows[0]["volume"] == "1200" and rows[0]["source_volume_hands"] == "12"
    assert rows[0]["amount"] == "7900" and rows[0]["price_basis"] == "raw"
    event = normalize_bar_observation(rows[0], fetched_at=NOW.isoformat())
    status = bar_evidence_status(event)
    assert status["signal_eligible"] is False
    assert "bar_finality_unverified" in status["reason_codes"]
    assert "timestamp_convention_unknown" in status["reason_codes"]


@pytest.mark.parametrize("code,market", [("920045", 0), ("920786", 1), ("920786", False)])
def test_other_symbol_or_market_cannot_be_relabelled(code, market):
    with pytest.raises(ValueError, match="identity_mismatch"):
        provider.parse_bse_minute_payload(payload(code=code, market=market), symbol="920786", now=NOW)


@pytest.mark.parametrize("lines", [
    ["2026-10-26 09:31,6.5,6.6,6.7,6.4,12,7900,6.5"] * 2,
    ["2026-10-26 09:32,6.5,6.6,6.7,6.4,12,7900,6.5", "2026-10-26 09:31,6.5,6.6,6.7,6.4,12,7900,6.5"],
    ["2026-10-26 09:31,6.5,6.6,6.0,6.4,12,7900,6.5"],
    ["2026-10-26 09:31,6.5,6.6,6.7,6.4,NaN,7900,6.5"],
])
def test_source_conflicts_and_invalid_numbers_fail_closed(lines):
    with pytest.raises(ValueError):
        provider.parse_bse_minute_payload(payload(lines), symbol="920786", now=NOW)


def test_network_false_never_calls_injected_transport():
    called = []
    result = provider.fetch_bse_minute_rows("920786", offset=240, now=NOW, timeout_seconds=2,
                                          get_json=lambda *args: called.append(args))
    assert called == [] and result[0] == [] and result[2]["network_attempted"] is False


def test_timeout_and_response_scope_are_bounded():
    calls = []
    rows, error, detail = provider.fetch_bse_minute_rows(
        "920786", offset=240, now=NOW, timeout_seconds=2, network=True,
        get_json=lambda url, timeout: calls.append((url, timeout)) or payload())
    assert not error and rows and calls[0][1] == 2
    assert "ndays=1" in calls[0][0] and "secid=0.920786" in calls[0][0]
    assert detail["formal_execution_eligible"] is False


def test_builtin_bse_failure_uses_independent_source_with_correct_provenance(monkeypatch):
    monkeypatch.setattr(collector, "_sina_http_pending_failure", lambda **kw: None)
    monkeypatch.setattr(collector, "_mootdx_minute_rows_once", lambda *a, **kw: ([], "native_failed", {}))
    fetched = []
    expected = provider.parse_bse_minute_payload(payload(), symbol="920786", now=NOW)
    def backup(code, **kwargs):
        fetched.append((code, kwargs))
        return expected, "", {"provider": provider.PROVIDER, "live_availability": "live_response"}
    monkeypatch.setattr(provider, "fetch_bse_minute_rows", backup)
    rows, error, detail = collector._live_minute_rows("920786", offset=240, now=NOW, timeout_seconds=4)
    assert rows == expected and not error and len(fetched) == 1
    assert fetched[0][1]["network"] is True and fetched[0][1]["timeout_seconds"] <= 4
    assert detail["provider"] == provider.PROVIDER and detail["live_availability"] == "live_response"
    assert detail["attempts"][0]["error"] == "native_failed"


def test_exhausted_native_budget_cannot_start_another_source(monkeypatch):
    monkeypatch.setattr(collector, "_sina_http_pending_failure", lambda **kw: None)
    clock = [0.0]
    def native(*args, **kwargs):
        clock[0] = 5.0
        return [], "native_failed", {}
    monkeypatch.setattr(collector, "_mootdx_minute_rows", native)
    called = []
    rows, error, detail = collector._live_minute_rows(
        "920786", offset=240, now=NOW, timeout_seconds=4, monotonic=lambda: clock[0],
        fallback_fetchers=[{"provider": "fixture", "exchanges": ("BJ",), "fetcher": lambda *a, **k: called.append(1)}])
    assert rows == [] and error and called == []


@pytest.mark.parametrize("symbol", ["900001", "830001", "92078", "９２０７８６"])
def test_unsupported_symbol_never_uses_the_bse_endpoint(symbol):
    called = []
    rows, error, detail = provider.fetch_bse_minute_rows(
        symbol, offset=240, now=NOW, timeout_seconds=2, network=True,
        get_json=lambda *a: called.append(a))
    assert rows == [] and "unsupported" in error
    assert not detail["network_attempted"] and called == []


def test_builtin_collection_persists_fallback_provenance_without_promoting_finality(tmp_path, monkeypatch):
    monkeypatch.setattr(collector, "refresh_full_market_snapshot", lambda *a, **k: {"movers": [], "reason_codes": []})
    monkeypatch.setattr(collector, "_prepare_minute_sources", lambda **kw: [])
    monkeypatch.setattr(collector, "_sina_http_pending_failure", lambda **kw: None)
    monkeypatch.setattr(collector, "_mootdx_minute_rows_once", lambda *a, **kw: ([], "native_failed", {}))
    calls = []
    def get_json(url, timeout):
        calls.append((url, timeout))
        return payload()
    monkeypatch.setattr(provider, "_get_json", get_json)
    manifest = collector.collect_intraday_refresh(
        tmp_path, network=True, trading_day=True, now=NOW,
        scope_builder=lambda *a, **k: {"symbols": [{"symbol": "920786", "priority": "critical"}]},
        monotonic=lambda: 0.0)
    assert len(calls) == 1 and manifest["appended_rows"] == 1
    receipt = manifest["provider_status"][0]
    assert receipt["status"] == "ok" and receipt["diagnostics"]["provider"] == provider.PROVIDER
    assert receipt["diagnostics"]["attempts"][0]["error"] == "native_failed"
    stored = read_intraday_csv(tmp_path / receipt["storage_path"], default_symbol="920786")
    assert stored["ok"] and len(stored["frame"]) == 1
    row = stored["frame"].iloc[0].to_dict()
    assert row["source"] == provider.PROVIDER and row["provider"] == provider.PROVIDER
    assert row["price_basis"] == "raw" and float(row["volume"]) == 1200.0
    assert row["fetched_at"] == NOW.isoformat(timespec="microseconds")
    assert row["finality"] == "unknown" and row["timestamp_convention"] == "unknown"
    assert not bar_evidence_status(row)["signal_eligible"]


def test_transport_cleanup_failure_stops_other_symbols(monkeypatch):
    monkeypatch.setattr(collector, "_sina_http_pending_failure", lambda **kw: None)
    monkeypatch.setattr(collector, "_mootdx_minute_rows_once", lambda *a, **kw: ([], "native_failed", {}))
    def broken(url, timeout):
        error = RuntimeError("cleanup unconfirmed")
        error.http = {"stop_collection": True, "cleanup": "unconfirmed"}
        raise error
    monkeypatch.setattr(provider, "_get_json", broken)
    rows, error, detail = collector._live_minute_rows("920786", offset=240, now=NOW, timeout_seconds=4)
    assert not rows and error and detail["stop_collection"]
    assert detail["error_stage"] == "cleanup" and detail["provider"] == provider.PROVIDER
