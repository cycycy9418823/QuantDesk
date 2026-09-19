"""B6: GET observation endpoints are read-only cache views; refresh is POST-only.

A GET can never trigger provider work, regardless of ``network``/``refresh``
query parameters or a cross-site ``Origin`` header.  Refresh intent is an
explicit POST that flows through the existing origin gate plus the network
master switch and the caller's intent.
"""

from __future__ import annotations

import pytest
from unittest.mock import patch

GOOD_ORIGIN = "http://localhost:8000"
EVIL_ORIGIN = "http://evil.example"


@pytest.fixture()
def client():
    from scripts import quant_api_server

    return quant_api_server.app.test_client()


def _market(monkeypatch):
    calls = {}

    def fake_summary(**kwargs):
        calls["summary"] = dict(kwargs)
        return {"ok": True, "status": "cache_view", "network_authorized": False}

    monkeypatch.setattr("scripts.quant_api_server.realtime_market_summary", fake_summary)
    return calls


def test_malicious_origin_get_with_network_refresh_never_triggers_provider(client, monkeypatch):
    calls = _market(monkeypatch)
    response = client.get(
        "/api/realtime/market?refresh=1&network=1",
        headers={"Origin": EVIL_ORIGIN},
    )
    assert response.status_code == 200
    assert response.get_json()["ok"] is True
    # The route downgraded the GET to a cache view: no refresh, no network.
    assert calls["summary"] == {"refresh": False, "explicit_network": False}


def test_plain_get_stays_read_only_offline_zero(client, monkeypatch):
    calls = _market(monkeypatch)
    response = client.get("/api/realtime/market?offline=0")
    assert response.status_code == 200
    assert calls["summary"] == {"refresh": False, "explicit_network": False}


def test_same_origin_intent_post_can_trigger_provider(client, monkeypatch):
    calls = _market(monkeypatch)
    response = client.post(
        "/api/realtime/market",
        json={"refresh": True, "network": True},
        headers={"Origin": GOOD_ORIGIN},
    )
    assert response.status_code == 200
    assert calls["summary"] == {"refresh": True, "explicit_network": True}


def test_malicious_origin_post_is_403(client, monkeypatch):
    calls = _market(monkeypatch)
    response = client.post(
        "/api/realtime/market",
        json={"refresh": True, "network": True},
        headers={"Origin": EVIL_ORIGIN},
    )
    assert response.status_code == 403
    assert response.get_json()["status"] == "origin_blocked"
    # The route handler never ran.
    assert "summary" not in calls


def test_stock_search_service_switch_separate_from_query(client, monkeypatch):
    seen = {"search": 0, "refresh": 0}

    def fake_search(query, limit):
        seen["search"] += 1
        return []

    def fake_refresh(*, network, force=False):
        seen["refresh"] += 1
        return {"ok": True, "status": "noop"}

    monkeypatch.setattr("scripts.quant_api_server.search_stock_rows", fake_search)
    monkeypatch.setattr("scripts.quant_api_server.refresh_spot_snapshot_cache", fake_refresh)
    response = client.get("/api/realtime/stock-search?q=600000&limit=8&refresh=1")
    assert response.status_code == 200
    assert response.get_json()["query"] == "600000"
    # The query is served; the GET never fired the refresh mutation.
    assert seen["search"] == 1
    assert seen["refresh"] == 0


def test_stock_detail_get_never_refreshes_quote_or_daily(client, monkeypatch):
    seen = {"quote": 0}
    monkeypatch.setattr("scripts.quant_api_server.realtime_network_enabled", lambda: False)

    def fake_quote(symbol):
        seen["quote"] += 1
        return {"ok": True}

    monkeypatch.setattr("scripts.quant_api_server.refresh_single_quote_cache", fake_quote)

    def fake_daily(symbol, **kwargs):
        seen["daily"] = dict(kwargs)
        return {"attempted": False, "network_authorized": False}

    monkeypatch.setattr("scripts.quant_api_server.ensure_single_stock_daily_with_auth", fake_daily)
    response = client.get(
        "/api/realtime/stock/600000?network=1&refresh_daily=1&refresh_quote=1",
        headers={"Origin": EVIL_ORIGIN},
    )
    assert response.status_code == 200
    # A GET must not refresh the quote, no matter what the query string asks for.
    assert seen["quote"] == 0
    assert seen["daily"] == {"refresh_daily": False, "network_allowed": False}


def test_stock_detail_post_refresh_is_explicit_mutation(client, monkeypatch):
    seen = {"quote": 0}
    monkeypatch.setattr("scripts.quant_api_server.realtime_network_enabled", lambda: False)

    def fake_quote(symbol):
        seen["quote"] += 1
        return {"ok": True, "updated": True}

    monkeypatch.setattr("scripts.quant_api_server.refresh_single_quote_cache", fake_quote)

    def fake_daily(symbol, **kwargs):
        seen["daily"] = dict(kwargs)
        return {"attempted": True, "network_authorized": True}

    monkeypatch.setattr("scripts.quant_api_server.ensure_single_stock_daily_with_auth", fake_daily)
    response = client.post(
        "/api/realtime/stock/600000/refresh",
        json={"refresh_quote": True, "refresh_daily": True, "network": True},
        headers={"Origin": GOOD_ORIGIN},
    )
    assert response.status_code == 200
    assert seen["quote"] == 1
    assert seen["daily"] == {"refresh_daily": True, "network_allowed": True}


def test_news_get_never_uses_network(client, monkeypatch):
    seen = {}

    def fake_news(symbol, limit, network):
        seen["network"] = network
        return {"ok": True, "rows": [], "network_used": network}

    monkeypatch.setattr("scripts.quant_api_server.fetch_news", fake_news)
    response = client.get(
        "/api/stock/600000/news?network=1&enable_network=1",
        headers={"Origin": EVIL_ORIGIN},
    )
    assert response.status_code == 200
    # GET news is a cache read; the network/refresh query params are ignored.
    assert seen["network"] is False


# ---------------------------------------------------------------------------
# Startup: a failed/interrupted record must never be green-washed by current
# candidate freshness; diagnostics and dates are preserved (B6).
# ---------------------------------------------------------------------------


def _startup_fixtures(monkeypatch, *, payload):
    monkeypatch.setattr(
        "scripts.quant_api_server.read_json_file", lambda _path: dict(payload)
    )
    monkeypatch.setattr(
        "scripts.quant_api_server.generation_status_for_ui",
        lambda current=None: {
            "signal_date": "2026-09-11",
            "candidate_count": 18,
            "freshness_status": "fresh",
            "target_date": "2026-09-11",
        },
    )
    monkeypatch.setattr(
        "scripts.quant_api_server.candidate_rows",
        lambda limit=18: [
            {
                "symbol": "600000",
                "data_quality": {"latest_date": "2026-09-11", "gap_count": 0},
            }
        ],
    )
    monkeypatch.setattr(
        "scripts.quant_api_server.data_gap_summary",
        lambda rows: {
            "gap_count": 0,
            "field_gap_count": 0,
            "candidate_count": 18,
            "stale_count": 0,
            "missing_count": 0,
            "message": "all fresh",
        },
    )
    monkeypatch.setattr(
        "scripts.quant_api_server.startup_payload_has_completed_after_state",
        lambda payload, state: False,
    )
    monkeypatch.setattr(
        "scripts.quant_api_server.startup_refresh_state_payload",
        lambda: {"running": False, "status": "idle", "request_id": ""},
    )


def test_startup_failed_record_is_never_overridden_by_candidate_freshness(client, monkeypatch):
    _startup_fixtures(
        monkeypatch,
        payload={
            "status": "failed",
            "updated_at": "2026-09-11T10:00:00",
            "target_date": "2026-09-11",
            "provider_diagnosis": {"tone": "warn", "summary": "provider failed", "next_action": "retry"},
            "update_result": {"status": "failed", "target_date": "2026-09-11"},
        },
    )
    response = client.get("/api/startup/update-status")
    assert response.status_code == 200
    payload = response.get_json()
    # The recorded failure is preserved, not flipped to green by current data.
    assert payload["status"] == "failed"
    assert payload.get("current_view_override") is not True
    # Top-level diagnostics and target date are preserved.
    assert payload["provider_diagnosis"]["tone"] == "warn"
    assert payload["target_date"] == "2026-09-11"


def test_startup_completed_record_still_shows_current_view(client, monkeypatch):
    _startup_fixtures(
        monkeypatch,
        payload={
            "status": "completed",
            "updated_at": "2026-09-11T10:00:00",
            "target_date": "2026-09-11",
            "update_result": {"status": "completed", "target_date": "2026-09-11"},
        },
    )
    response = client.get("/api/startup/update-status")
    assert response.status_code == 200
    payload = response.get_json()
    assert payload.get("current_view_override") is True
    assert payload["status"] == "completed"


def test_startup_missing_fields_fall_back_explainably(client, monkeypatch):
    _startup_fixtures(
        monkeypatch,
        payload={"status": "failed", "message": "provider down"},
    )
    response = client.get("/api/startup/update-status")
    assert response.status_code == 200
    payload = response.get_json()
    # Sparse records still yield an explanation and a diagnosis, never a crash.
    explanation = payload.get("startup_update_explanation") or {}
    assert explanation.get("reason_label") or explanation.get("summary")
    assert payload.get("provider_diagnosis") is not None
    assert payload.get("status") == "failed"
