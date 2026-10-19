"""A slow minute source must not consume the whole round before its tail."""
import csv
import json
import math
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from quantdesk.intraday import intraday_providers
from quantdesk.intraday_refresh import collector
from quantdesk.intraday_refresh.scope import build_dynamic_scope


NOW = datetime(2026, 10, 12, 10, 20, tzinfo=ZoneInfo("Asia/Shanghai"))


class Clock:
    def __init__(self):
        self.value = 0.0

    def __call__(self):
        return self.value


def scope(count):
    return {"symbols": [{"symbol": f"{600000 + index:06d}", "priority": "normal"}
                        for index in range(count)], "warnings": []}


@pytest.fixture
def no_snapshot(monkeypatch):
    monkeypatch.setattr(collector, "refresh_full_market_snapshot", lambda *a, **k: {
        "ok": False, "status": "fixture", "movers": [], "reason_codes": [],
    })


def test_ninety_timeouts_all_receive_a_share_instead_of_starving_tail(tmp_path, no_snapshot):
    clock = Clock()
    calls = []

    def slow(symbol, offset, *, timeout_seconds):
        calls.append((symbol, timeout_seconds))
        clock.value += timeout_seconds
        return [], "provider_failed:fixture:TimeoutError", {"error_stage": "timeout"}

    result = collector.collect_intraday_refresh(
        tmp_path, network=True, now=NOW, trading_day=True, fetcher=slow,
        scope_builder=lambda *a, **k: scope(90), max_round_seconds=240,
        monotonic=clock,
    )
    assert len(calls) == 90 and len(result["provider_status"]) == 90
    assert not result["not_attempted_symbols"] and clock.value < 240
    assert calls[0][1] < 3
    assert all(0 < seconds <= collector.PROVIDER_TIMEOUT_SECONDS for _, seconds in calls)
    assert result["missing_count"] == 90 and result["status"] == "blocked"
    assert all(row["reason"] == "provider_failed:fixture:TimeoutError" for row in result["provider_status"])
    assert all(row["granted_budget_seconds"] > 0 for row in result["provider_status"])


def test_round_reuses_successful_sina_after_native_connection_failure(tmp_path, no_snapshot, monkeypatch):
    clock = Clock()
    native_calls, sina_calls = [], []

    def native(symbol, **kwargs):
        native_calls.append(symbol)
        clock.value += min(0.1, kwargs["timeout_seconds"])
        return [], "provider_failed:mootdx:ConnectionError", {"error_stage": "connect", "stages": []}

    def sina(url, *, timeout, monotonic):
        sina_calls.append(url)
        clock.value += 0.05
        return json.dumps([{"day": f"2026-10-12 {stamp}", "open": "10", "high": "10",
                            "low": "10", "close": "10", "volume": "100"}
                           for stamp in ("10:19:00", "10:20:00")]), {}

    monkeypatch.setattr(collector, "_mootdx_minute_rows", native)
    monkeypatch.setattr(collector, "_sina_http_get_text", sina)
    result = collector.collect_intraday_refresh(
        tmp_path, network=True, now=NOW, trading_day=True,
        scope_builder=lambda *a, **k: scope(3), monotonic=clock,
    )
    assert native_calls == ["600000"] and len(sina_calls) == 3
    assert result["fresh_count"] == 3
    second = result["provider_status"][1]["diagnostics"]
    assert second["routing"] == "round_sina_first"
    assert second["round_source_route"]["evidence_symbol"] == "600000"
    assert second["round_source_route"]["reason"] == "provider_failed:mootdx:ConnectionError"
    for path in (tmp_path / "data/intraday/1m").glob("*.csv"):
        with path.open(encoding="utf-8", newline="") as handle:
            records = list(csv.DictReader(handle))
        assert all(row["source"] == "sina_intraday" and row["price_basis"] == "raw" for row in records)
        assert all(math.isnan(float(row["amount"])) for row in records)


def test_preferred_sina_failure_retains_native_fallback_and_both_errors(monkeypatch):
    clock = Clock()
    native_calls = []

    def failed(*args, **kwargs):
        clock.value = 2.0
        return [], "provider_failed:sina_intraday:ConnectionError", {
            "error_type": "ConnectionError", "message": "fixture outage",
        }

    def native(symbol, **kwargs):
        native_calls.append(kwargs["timeout_seconds"])
        return [{"symbol": symbol, "price_basis": "raw", "amount": float("nan")}], "", {}

    monkeypatch.setattr(collector, "_sina_minute_rows", failed)
    monkeypatch.setattr(collector, "_mootdx_minute_rows", native)
    rows, error, evidence = collector._live_minute_rows(
        "600000", offset=240, now=NOW, monotonic=clock,
        timeout_seconds=3, preferred_provider="sina_intraday",
    )
    assert not error and math.isnan(rows[0]["amount"])
    assert native_calls == [1.0]
    assert evidence["primary_error"] == "provider_failed:sina_intraday:ConnectionError"
    assert [attempt["provider"] for attempt in evidence["attempts"]] == ["sina_intraday", "mootdx"]


def test_late_response_cannot_write_and_untried_names_are_explained(tmp_path, no_snapshot):
    clock = Clock()

    def late(symbol, offset, *, timeout_seconds):
        clock.value = 241
        return [{"symbol": symbol, "datetime": "2026-10-12 10:20:00",
                 "open": 10, "high": 10, "low": 10, "close": 10,
                 "volume": 100, "amount": float("nan"), "price_basis": "raw"}], ""

    result = collector.collect_intraday_refresh(
        tmp_path, network=True, now=NOW, trading_day=True, fetcher=late,
        scope_builder=lambda *a, **k: scope(3), monotonic=clock,
    )
    assert result["status"] == "timed_out" and result["appended_rows"] == 0
    assert result["not_attempted_reasons"] == {
        "600001": "round_budget_exhausted", "600002": "round_budget_exhausted",
    }
    assert not list((tmp_path / "data/intraday/1m").glob("*.csv"))


def write_focus(root, *, profile="default", level=3, overrides=None):
    folder = root / "state" / ("ai_trader" if profile == "default" else f"ai_trader_{profile}")
    folder.mkdir(parents=True, exist_ok=True)
    ledger_id = "ai_trader_100k" + ("" if profile == "default" else "_" + profile)
    (folder / "ledger.json").write_text(json.dumps({
        "ledger_id": ledger_id, "positions": {"600000": {"qty": 100}},
        "daytime_trust": {"level": level, "authorized_by": "fixture_owner", "authorized_at": "2026-10-12T09:00:00+08:00"},
    }), encoding="utf-8")
    payload = {
        "schema": "ai_minute_focus.v1", "source": "model_want_minutes", "profile": profile,
        "ledger_id": ledger_id, "as_of": "2026-10-12", "slot": "1020", "model_used": True,
        "model": "fixture_model", "status": "active", "symbols": ["600009", "600000"],
        "generated_at": "2026-10-12T10:20:00+08:00", "expires_at": "2026-10-12T10:50:00+08:00",
        "consumer_eligible": True, **(overrides or {}),
    }
    (folder / "minute_focus.json").write_text(json.dumps(payload), encoding="utf-8")
    return folder


def test_model_focus_needs_explicit_read_and_existing_level_three(tmp_path):
    folder = write_focus(tmp_path)
    before = {path: path.read_bytes() for path in folder.glob("*.json")}
    default = build_dynamic_scope(tmp_path, now=NOW)
    assert [row["symbol"] for row in default["symbols"]] == ["600000"]
    allowed = build_dynamic_scope(tmp_path, now=NOW, read_ai_minute_focus=True)
    assert [row["symbol"] for row in allowed["symbols"]] == ["600000", "600009"]
    assert allowed["symbols"][0]["priority"] == "critical"
    assert allowed["symbols"][1]["reasons"] == ["ai_minute_focus"]
    assert allowed["symbols"][1]["focus_provenance"][0]["source"] == "model_want_minutes"
    assert {path: path.read_bytes() for path in before} == before
    write_focus(tmp_path, level=2)
    denied = build_dynamic_scope(tmp_path, now=NOW, read_ai_minute_focus=True)
    assert [row["symbol"] for row in denied["symbols"]] == ["600000"]
    assert any("level_3_authorization_required" in warning for warning in denied["warnings"])


def test_focus_from_timely_model_response_can_cross_the_observation_slot_boundary(tmp_path):
    # A 10:04:50 observation belongs to 1000; a 10:05:01 model completion
    # produces this focus at 10:05:02 while its observation is only 12s old.
    folder = write_focus(tmp_path, overrides={
        "slot": "1000", "generated_at": "2026-10-12T10:05:02+08:00",
        "expires_at": "2026-10-12T10:35:02+08:00",
    })
    before = {path: path.read_bytes() for path in folder.glob("*.json")}
    current = NOW.replace(hour=10, minute=5, second=3)
    result = build_dynamic_scope(tmp_path, now=current, read_ai_minute_focus=True)
    assert [row["symbol"] for row in result["symbols"]] == ["600000", "600009"]
    assert result["symbols"][1]["reasons"] == ["ai_minute_focus"]
    assert not any("date_slot_or_expiry_unverified" in warning for warning in result["warnings"])
    assert {path: path.read_bytes() for path in before} == before


@pytest.mark.parametrize("overrides", [
    {"model_used": False}, {"model": ""}, {"source": "fixed_watchlist"},
    {"profile": "glm"}, {"ledger_id": "ai_trader_100k_glm"},
    {"generated_at": "2026-10-12T10:20:00"}, {"expires_at": "2026-10-12T10:20:00+08:00"},
    {"expires_at": "2026-10-13T10:30:00+08:00"}, {"expires_at": "2026-10-12T11:00:00+08:00"},
    {"as_of": "2026-10-09"}, {"symbols": ["600009", "600009"]}, {"slot": "1010"},
    {"symbols": [f"{600001+i:06d}" for i in range(11)]},
])
def test_unverified_model_focus_never_changes_the_minute_scope(tmp_path, overrides):
    write_focus(tmp_path, overrides=overrides)
    result = build_dynamic_scope(tmp_path, now=NOW, read_ai_minute_focus=True)
    assert [row["symbol"] for row in result["symbols"]] == ["600000"]
    assert any(warning.startswith("ai_minute_focus:") for warning in result["warnings"])


def test_focus_profiles_keep_identity_and_remain_inside_total_cap(tmp_path):
    write_focus(tmp_path)
    write_focus(tmp_path, profile="glm", overrides={"symbols": ["600008"]})
    result = build_dynamic_scope(tmp_path, now=NOW, read_ai_minute_focus=True, max_symbols=2)
    assert [row["symbol"] for row in result["symbols"]] == ["600000", "600008"]
    assert result["symbols"][1]["focus_provenance"][0]["profile"] == "glm"
    assert "minute_scope_capped:2" in result["warnings"]


def test_minute_focus_cli_flag_is_explicit_and_default_call_stays_compatible(monkeypatch):
    from scripts import run_intraday_data_refresh as runner

    calls = []
    monkeypatch.setattr(runner, "collect_intraday_refresh", lambda root, **kw: calls.append(kw) or {"status": "ready"})
    monkeypatch.setattr(runner, "observe_all_profiles", lambda root: ([], {}, {}))
    assert runner.main(["--no-alerts", "--json", "false"]) == 0
    assert "read_ai_minute_focus" not in calls[-1]
    assert runner.main(["--no-alerts", "--json", "false", "--read-ai-minute-focus"]) == 0
    assert calls[-1]["read_ai_minute_focus"] is True and calls[-1]["network"] is False
