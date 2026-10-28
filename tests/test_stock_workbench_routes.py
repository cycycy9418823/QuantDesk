from __future__ import annotations

from dataclasses import replace
from datetime import datetime
from unittest.mock import Mock

import pandas as pd
import pytest
from flask import Flask

from scripts.server.routes.stock_workbench import StockQuoteMemory, StockWorkbenchDeps, create_stock_workbench_blueprint


def deps(*, refresh_allowed=True):
    provider = Mock(return_value={"ok": True, "row": {
        "symbol": "600000", "price": 10.3, "source": "fixture.provider",
        "quote_time": "2026-10-23 10:00:00", "updated_at": "2026-10-23 10:00:02", "price_basis": "raw"}})
    return StockWorkbenchDeps(
        load_original_daily=lambda symbol: {"frame": pd.DataFrame()},
        cached_quote=lambda symbol: {"symbol": symbol, "available": False},
        fetch_quote=provider, research_builder=lambda df: {}, load_calendar=lambda: {
            "is_open_by_date": {"2026-10-22": True, "2026-10-23": True, "2026-10-26": True}},
        network_enabled=lambda: False, refresh_allowed=lambda: refresh_allowed,
        now=lambda: datetime.fromisoformat("2026-10-23T10:00:10+08:00"))


def client(dependencies):
    app = Flask(__name__)
    app.register_blueprint(create_stock_workbench_blueprint(dependencies))
    return app.test_client()


def test_get_with_network_query_never_fetches_or_writes():
    d = deps()
    body = client(d).get("/api/stock/600000/workbench?network=true&refresh=true").get_json()
    d.fetch_quote.assert_not_called()
    assert body["ok"]
    assert not body["canonical_writes"]
    assert not body["llm_used"]


def test_missing_daily_history_still_returns_quote_view():
    body = client(deps()).get("/api/stock/600000/workbench").get_json()
    assert "quote" in body and "quote_evidence" in body
    assert body["reference"]["reason_codes"] == ["daily_history_missing"]


def test_refresh_requires_explicit_network_intent():
    d = deps()
    response = client(d).post("/api/stock/600000/quote-refresh", json={})
    assert response.status_code == 403
    d.fetch_quote.assert_not_called()


def test_explicit_quote_refresh_changes_memory_only_and_is_readable():
    d = deps()
    c = client(d)
    response = c.post("/api/stock/600000/quote-refresh", json={"network": True})
    assert response.status_code == 200
    assert response.get_json()["view"]["quote"]["price"] == 10.3
    assert not response.get_json()["canonical_writes"]
    assert c.get("/api/stock/600000/workbench").get_json()["quote"]["price"] == 10.3
    d.fetch_quote.assert_called_once_with("600000")


def test_preview_blocks_refresh_before_provider():
    d = deps(refresh_allowed=False)
    assert client(d).post("/api/stock/600000/quote-refresh", json={"network": True}).status_code == 423
    d.fetch_quote.assert_not_called()


def test_invalid_code_cannot_be_a_filesystem_or_provider_request():
    d = deps()
    c = client(d)
    for symbol in ("12345", "abcdef", "１２３４５６"):
        assert c.get("/api/stock/" + symbol + "/workbench").status_code == 400
        assert c.post("/api/stock/" + symbol + "/quote-refresh", json={"network": True}).status_code == 400
    d.fetch_quote.assert_not_called()


def test_provider_failure_keeps_last_success_and_clock():
    memory = StockQuoteMemory(cooldown_seconds=0)
    provider = deps().fetch_quote
    memory.refresh("600000", provider)
    old = memory.read("600000")
    failure = memory.refresh("600000", lambda symbol: {"ok": False, "message_for_ui": "fixture failure"})
    assert failure["status"] == "provider_failed"
    assert memory.read("600000") == old


def test_requests_have_a_global_interval_and_no_batch_fetch():
    clock = Mock(return_value=100.0)
    memory = StockQuoteMemory(clock=clock)
    provider = deps().fetch_quote
    memory.refresh("600000", provider)
    assert memory.refresh("600001", provider)["status"] == "refresh_deferred"
    provider.assert_called_once_with("600000")


def test_bad_symbol_response_does_not_poison_selected_stock():
    memory = StockQuoteMemory()
    response = memory.refresh("600000", lambda symbol: {"ok": True, "row": {"symbol": "600001", "price": 10}})
    assert not response["ok"]
    assert memory.read("600000") is None


def test_actual_app_origin_guard_blocks_untrusted_refresh(monkeypatch):
    from scripts import quant_api_server as server
    provider = Mock()
    monkeypatch.setattr(server, "fetch_eastmoney_single_quote", provider)
    response = server.app.test_client().post("/api/stock/600000/quote-refresh", json={"network": True},
                                           headers={"Origin": "http://evil.example"})
    assert response.status_code == 403
    assert response.get_json()["status"] == "origin_blocked"
    provider.assert_not_called()


def test_time_only_cache_does_not_become_today(monkeypatch):
    from scripts.server.helpers import market
    monkeypatch.setattr(market, "load_spot_snapshot_frame", lambda: pd.DataFrame([
        {"symbol": "600000", "name": "fixture", "price": 10, "quote_time": "15:00:02",
         "updated_at": "2026-10-23 15:05:02", "source": "fixture.provider"}]))
    row = market.spot_quote_for_symbol("600000")
    assert row["quote_date"] == ""
    assert row["quote_time"] == "15:00:02"
    assert market.snapshot_quote_date(market.load_spot_snapshot_frame(), "2026-10-24") == ""


def test_cache_normalization_preserves_explicit_date_and_basis():
    from scripts.server.helpers import market
    frame = market._canonical_spot_frame(pd.DataFrame([
        {"symbol": "600000", "price": 10, "quote_time": "15:00:02",
         "quote_date": "2026-10-23", "price_basis": "raw"}]))
    assert frame.iloc[0]["quote_date"] == "2026-10-23"
    assert frame.iloc[0]["price_basis"] == "raw"


def test_provider_missing_clock_does_not_invent_quote_timestamp(monkeypatch):
    from scripts.server.helpers import market
    import requests
    response = Mock()
    response.json.return_value = {"data": {"f57": "600000", "f58": "fixture", "f43": 1000}}
    monkeypatch.setattr(requests, "get", Mock(return_value=response))
    result = market.fetch_eastmoney_single_quote("600000")
    row = result["row"]
    assert row["quote_time"] == ""
    assert row["quote_date"] == ""
    assert row["updated_at"]
    assert row["price_basis"] == "raw"
    assert not result["ok"]
    assert "已刷新" not in result["message_for_ui"]


def test_saved_quote_date_conflict_survives_cache_adapter(monkeypatch):
    from scripts.server.helpers import market
    from quantdesk.quote_evidence import inspect_quote
    monkeypatch.setattr(market, "load_spot_snapshot_frame", lambda: pd.DataFrame([
        {"symbol": "600000", "price": 10, "quote_time": "2026-10-23 10:00:00",
         "quote_date": "2026-10-22", "updated_at": "2026-10-23 10:00:02",
         "source": "fixture.provider", "price_basis": "raw"}]))
    row = market.spot_quote_for_symbol("600000")
    checked = inspect_quote(row, symbol="600000", now=deps().now(), session="morning_session",
                            expected_trade_date=None)
    assert row["quote_date"] == "2026-10-22"
    assert row["quote_time"] == "10:00:00"
    assert row["quote_timestamp"].startswith("2026-10-23")
    assert checked["status"] == "invalid_time"
    assert "quote_date_timestamp_conflict" in checked["reason_codes"]


def test_cache_keeps_a_full_provider_timestamp_separate_from_clock():
    from scripts.server.helpers import market
    frame = market._canonical_spot_frame(pd.DataFrame([
        {"symbol": "600000", "price": 10, "quote_time": "10:00:00",
         "quote_timestamp": "2026-10-23T10:00:00+08:00", "price_basis": "raw"}]))
    assert frame.iloc[0]["quote_timestamp"] == "2026-10-23T10:00:00+08:00"


def test_boolean_provider_price_is_not_converted_to_a_valid_price():
    from scripts.server.helpers import market
    assert market._em_scaled(True) is None


def test_slow_request_validates_the_quote_at_response_time():
    d = deps()
    current = [d.now()]
    def slow_provider(symbol):
        current[0] = datetime.fromisoformat("2026-10-23T10:00:18+08:00")
        return {"ok": True, "row": {
            **d.fetch_quote.return_value["row"], "quote_time": "2026-10-23 10:00:18",
            "updated_at": "2026-10-23 10:00:18"}}
    body = client(replace(d, fetch_quote=slow_provider, now=lambda: current[0])).post(
        "/api/stock/600000/quote-refresh", json={"network": True}).get_json()
    assert body["ok"]
    assert body["view"]["quote_evidence"]["status"] == "live"
    assert body["view"]["quote_evidence"]["quote_timestamp"].endswith("10:00:18+08:00")


def test_provider_older_than_verified_saved_quote_is_rejected():
    d = deps()
    saved = {**d.fetch_quote.return_value["row"], "price": 10.4,
             "quote_time": "2026-10-23 10:00:08", "updated_at": "2026-10-23 10:00:09"}
    body = client(replace(d, cached_quote=lambda symbol: saved)).post(
        "/api/stock/600000/quote-refresh", json={"network": True}).get_json()
    assert not body["ok"]
    assert body["status"] == "older_quote_rejected"
    assert body["view"]["quote"]["price"] == 10.4


def test_new_saved_snapshot_is_not_hidden_by_older_memory_quote():
    d = deps()
    saved = [{"symbol": "600000", "available": False}]
    c = client(replace(d, cached_quote=lambda symbol: saved[0]))
    assert c.post("/api/stock/600000/quote-refresh", json={"network": True}).get_json()["ok"]
    saved[0] = {**d.fetch_quote.return_value["row"], "price": 10.4,
                "quote_time": "2026-10-23 10:00:08", "updated_at": "2026-10-23 10:00:09"}
    assert c.get("/api/stock/600000/workbench").get_json()["quote"]["price"] == 10.4


@pytest.mark.parametrize("failure,reason", [
    ("read", "daily_history_read_failed"), ("compute", "reference_calculation_failed"),
])
def test_daily_failures_degrade_reference_without_losing_quote(failure, reason):
    d = deps()
    daily = pd.DataFrame({"date": pd.bdate_range(end="2026-10-22", periods=90),
                          "open": 10, "close": 10, "high": 10.2, "low": 9.8,
                          "volume": 1000, "adjust": "raw"})
    d = replace(d, cached_quote=lambda symbol: d.fetch_quote.return_value["row"],
                load_original_daily=Mock(side_effect=OSError("fixture")) if failure == "read" else lambda symbol: {"frame": daily},
                research_builder=Mock(side_effect=RuntimeError("fixture")))
    response = client(d).get("/api/stock/600000/workbench")
    assert response.status_code == 200
    body = response.get_json()
    assert body["quote"]["price"] == 10.3
    assert body["quote_evidence"]["status"] == "live"
    assert body["reference"]["status"] == "degraded"
    assert reason in body["reference"]["reason_codes"]
    assert "报价仍可单独查看" in body["reference"]["message"]


@pytest.mark.parametrize("calendar", [Mock(side_effect=OSError("fixture")), lambda: {}])
def test_unavailable_calendar_does_not_turn_saved_quote_into_live_signal(calendar):
    d = deps()
    response = client(replace(d, load_calendar=calendar,
                              cached_quote=lambda symbol: d.fetch_quote.return_value["row"])).get(
        "/api/stock/600000/workbench")
    assert response.status_code == 200
    body = response.get_json()
    assert body["quote"]["price"] == 10.3
    assert not body["market_active"] and not body["calendar_verified"]
    assert body["calendar_message"]
    assert not body["quote_evidence"]["comparable_to_raw_daily"]


def test_unreadable_cache_does_not_block_the_stock_view():
    response = client(replace(deps(), cached_quote=Mock(side_effect=OSError("fixture")))).get(
        "/api/stock/600000/workbench")
    assert response.status_code == 200
    assert response.get_json()["quote_evidence"]["status"] == "missing"
