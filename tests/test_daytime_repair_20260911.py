"""Regression coverage for observed calendar, collector and GET failures."""
from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace

import pandas as pd
import pytest


@pytest.fixture(autouse=True)
def _legacy_hq_unavailable(monkeypatch):
    """These existing cases exercise the original providers with HQ unavailable."""
    from quantdesk import snapshot_history

    def unavailable(*args, **kwargs):
        raise RuntimeError("fixture_hq_unavailable")

    monkeypatch.setattr(snapshot_history, "_sina_hq_fetch", unavailable)

from quantdesk import candidate_tracking
from quantdesk.intraday_refresh import collector, snapshot


def test_a0_worker_timeout_is_terminated_without_publishing(monkeypatch):
    import subprocess
    from quantdesk import snapshot_history

    calls = []

    class Process:
        pid = 42
        returncode = -1

        def communicate(self, value=None, timeout=None):
            calls.append(("communicate", timeout))
            if value is not None:
                raise subprocess.TimeoutExpired("fixture", timeout)
            return "", ""

        def kill(self):
            calls.append(("kill", None))

    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: Process())
    with pytest.raises(snapshot_history.SnapshotProviderError) as caught:
        snapshot_history.fetch_snapshot_in_process(["600000"], network=True, budget_seconds=0.2)
    assert ("kill", None) in calls
    assert calls[0][1] <= 0.2
    assert caught.value.provider_errors[-1]["error_type"] == "TimeoutError"


def test_a0_worker_preserves_fallback_source_and_earlier_failure(monkeypatch):
    import json
    import subprocess
    from quantdesk import snapshot_history

    class Process:
        pid = 43
        returncode = 0

        def communicate(self, value=None, timeout=None):
            assert json.loads(value)["network"] is True
            return json.dumps({"ok": True, "source": "sina.spot_ak", "rows": [{"symbol": "600000", "price": 12}],
                               "provider_errors": [{"source": "sina.spot_direct", "outcome": "failed"}], "warnings": ["earlier_source_failed"]}), ""

    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: Process())
    rows = snapshot_history.fetch_snapshot_in_process(["600000"], network=True, budget_seconds=1)
    assert rows.source == "sina.spot_ak"
    assert rows[0]["price"] == 12
    assert rows.provider_errors[0]["outcome"] == "failed"
    assert rows.provider_errors[-1]["outcome"] == "ok"
    payload, source, warnings, errors = snapshot_history._fetch_result(rows)
    assert source == "sina.spot_ak"
    assert warnings == ["earlier_source_failed"]
    assert payload[0]["price"] == 12
    assert errors[0]["outcome"] == "failed"


class Clock:
    def __init__(self):
        self.value = 0.0

    def __call__(self):
        return self.value

    def advance(self, value):
        self.value += value


@pytest.mark.parametrize("flag", ["", "unknown", "missing", "duplicate", "false"])
def test_next_session_requires_each_original_calendar_flag(tmp_path, flag):
    path = tmp_path / "data/calendar/a_share_trading_calendar.csv"
    path.parent.mkdir(parents=True)
    middle = "" if flag == "missing" else f"2026-09-08,{flag if flag != 'duplicate' else 'false'},fixture,confirmed\n"
    if flag == "duplicate":
        middle += "2026-09-08,true,fixture,confirmed\n"
    path.write_text("date,is_open,source,note\n2026-09-07,true,fixture,confirmed\n" + middle + "2026-09-09,true,fixture,confirmed\n", encoding="utf-8")
    day, reason = candidate_tracking._verified_next_session(tmp_path, "2026-09-07")
    if flag == "false":
        assert (day, reason) == ("2026-09-09", "")
    else:
        assert day == ""
        assert reason


def test_late_snapshot_does_not_overwrite_last_good_cache(tmp_path):
    path = tmp_path / snapshot.SPOT_RELATIVE
    path.parent.mkdir(parents=True)
    path.write_bytes(b"prior-cache\n")
    clock = Clock()

    def fetch():
        clock.advance(1.1)
        return [{"symbol": "600000", "price": 12.0}], "fixture"

    result = snapshot.refresh_full_market_snapshot(
        tmp_path, network=True, fetcher=fetch, budget_seconds=1.0, clock=clock,
    )
    assert result["status"] == "timed_out"
    assert not result["ok"]
    assert path.read_bytes() == b"prior-cache\n"


@pytest.mark.parametrize("hour,minute", [(9, 31), (11, 45), (16, 0)])
def test_outside_window_does_not_fetch_even_without_manifest(tmp_path, hour, minute):
    calls = []

    def fetch():
        calls.append("provider")
        return [], "fixture"

    result = collector.collect_intraday_refresh(
        tmp_path, network=True, trading_day=True,
        now=datetime(2026, 9, 11, hour, minute), snapshot_fetcher=fetch,
        scope_builder=lambda *a, **k: {"symbols": [], "warnings": []}, write_manifest=False,
    )
    assert result["status"] == "closed"
    assert result["run_state"] == "skipped"
    assert result["finished_at"]
    assert calls == []


@pytest.mark.parametrize("connect_elapsed,request_elapsed,expected_rows", [(0.2, 0.2, 1), (1.1, 0, 0), (0.8, 0.3, 0)])
def test_mootdx_shares_one_deadline_across_connection_and_request(connect_elapsed, request_elapsed, expected_rows):
    clock = Clock()
    timeouts = []
    requests = []
    transport = SimpleNamespace(settimeout=timeouts.append)

    class Client:
        client = SimpleNamespace(client=transport)

        def bars(self, **kwargs):
            requests.append(clock())
            clock.advance(request_elapsed)
            return pd.DataFrame([{"datetime": "2026-09-11 10:00:00", "open": 10, "high": 10, "low": 10, "close": 10, "volume": 100, "amount": 1000}])

        def close(self):
            pass

    def factory():
        clock.advance(connect_elapsed)
        return Client()

    rows, error, diagnosis = collector._mootdx_minute_rows(
        "600000", offset=1, now=datetime(2026, 9, 11, 10, 1),
        client_factory=factory, monotonic=clock, timeout_seconds=1.0,
    )
    assert len(rows) == expected_rows
    if connect_elapsed >= 1:
        assert requests == []
    else:
        assert timeouts[0] == pytest.approx(1.0 - connect_elapsed)
    if not expected_rows:
        assert error
        assert diagnosis["error_type"] == "TimeoutError"


def test_fractional_or_exhausted_round_never_gets_a_full_second():
    assert collector._provider_timeout_seconds(symbol_budget_ms=200, round_deadline=0.2, monotonic=lambda: 0) == pytest.approx(0.2)
    assert collector._provider_timeout_seconds(symbol_budget_ms=200, round_deadline=0.2, monotonic=lambda: 0.3) == 0


def test_numeric_get_search_uses_actual_cache_consumer_without_refresh(tmp_path, monkeypatch):
    from scripts import quant_api_server as api

    calls = []
    monkeypatch.setattr(api, "realtime_network_enabled", lambda: True)
    monkeypatch.setattr(api, "refresh_single_quote_cache", lambda symbol: calls.append(symbol))
    monkeypatch.setattr(api, "load_spot_snapshot_frame", lambda: pd.DataFrame([{"symbol": "600000", "name": "fixture", "price": 12.0, "pct_chg": 1.0}]))
    monkeypatch.setattr(api, "UNIVERSE_DIR", tmp_path)
    response = api.app.test_client().get("/api/realtime/stock-search?q=600000&refresh=1&network=1", headers={"Origin": "http://evil.example"})
    assert response.status_code == 200
    assert response.get_json()["query"] == "600000"
    assert calls == []


@pytest.mark.parametrize("status", ["failed", "interrupted", "unknown", "unreadable", "not_run"])
def test_startup_explains_original_attempt_without_candidate_upgrade(status, monkeypatch):
    from scripts import quant_api_server as api

    payload = {"status": status, "target_date": "2026-09-10", "startup_update_attempted": True,
               "provider_network_authorized": True, "candidate_generation": {"target_date": "2026-09-11", "status": "completed", "freshness_status": "fresh"}}
    original_read = api.read_json_file
    monkeypatch.setattr(api, "read_json_file", lambda path: dict(payload) if path == api.STARTUP_UPDATE_JSON else original_read(path))
    monkeypatch.setattr(api, "startup_refresh_state_payload", lambda: {"running": False})
    monkeypatch.setattr(api, "startup_payload_has_completed_after_state", lambda *a: False)
    response = api.app.test_client().get("/api/startup/update-status")
    data = response.get_json()
    assert data["status"] == status
    assert data["current_view_override"] is False
    assert data["startup_update_explanation"]["target_date"] == "2026-09-10"
    assert data["startup_update_explanation"]["reason_code"] != "ready"
