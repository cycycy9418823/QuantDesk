"""Keep normal full-market collection and bounded transient recovery usable."""

from datetime import datetime
import sys
import types
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from quantdesk import snapshot_history as history
from quantdesk.intraday_refresh import collector


class Clock:
    def __init__(self):
        self.value = 0.0

    def __call__(self):
        return self.value

    def sleep(self, seconds):
        self.value += seconds


def paginated_source(monkeypatch, clock, delay):
    rows = [{"code": f"{600000 + i:06d}", "symbol": f"sh{600000 + i:06d}",
             "trade": 10.0} for i in range(5561)]

    def get(_url, *, params, headers, timeout):
        clock.sleep(min(delay, timeout))
        if delay > timeout:
            raise TimeoutError("fixture_request_timeout")
        start = (params["page"] - 1) * params["num"]
        return types.SimpleNamespace(
            raise_for_status=lambda: None,
            json=lambda: rows[start:start + params["num"]],
        )

    monkeypatch.setitem(sys.modules, "requests", types.SimpleNamespace(get=get))
    monkeypatch.setattr(history.time_module, "sleep", clock.sleep)
    return rows


@pytest.mark.parametrize("delay", [0.1, 0.25, 0.5, 1.0])
def test_full_market_normal_pagination_completes(monkeypatch, delay):
    clock = Clock()
    expected = paginated_source(monkeypatch, clock, delay)
    rows, source = history._sina_direct_fetch(clock=clock)
    assert rows == expected
    assert source == "sina.spot_direct"
    assert clock() < 90


def test_public_chain_keeps_slow_full_market_result(monkeypatch):
    clock = Clock()
    expected = paginated_source(monkeypatch, clock, 0.5)
    original = history._sina_direct_fetch
    monkeypatch.setattr(history, "_sina_direct_fetch",
                        lambda *, budget_seconds: original(clock=clock, budget_seconds=budget_seconds))
    rows, source = history.default_network_fetch(
        [row["code"] for row in expected], clock=clock,
    )
    assert list(rows) == expected
    assert source == "sina.spot_direct"


def test_snapshot_respects_shorter_caller_deadline(monkeypatch):
    clock = Clock()
    paginated_source(monkeypatch, clock, 1.0)
    with pytest.raises(TimeoutError):
        history._sina_direct_fetch(clock=clock, budget_seconds=5.0)
    assert clock() <= 5.0


def frame():
    return pd.DataFrame([{"datetime": "2026-09-14 10:00:00", "open": 10.0,
                          "high": 10.1, "low": 9.9, "close": 10.0,
                          "volume": 100, "amount": 1000}])


@pytest.mark.parametrize("failed_stage", ["connect", "request"])
def test_minutes_recover_after_one_transient_failure(monkeypatch, failed_stage):
    clock = Clock()
    monkeypatch.setattr(collector.time, "sleep", clock.sleep)
    calls = []
    closed = []

    class Client:
        def __init__(self, attempt):
            self.attempt = attempt

        def bars(self, **kwargs):
            if self.attempt == 1 and failed_stage == "request":
                raise ConnectionError("temporary request failure")
            return frame()

        def close(self):
            closed.append(self.attempt)

    def factory():
        calls.append(1)
        if len(calls) == 1 and failed_stage == "connect":
            raise ConnectionError("temporary connection failure")
        return Client(len(calls))

    rows, error, diagnostics = collector._mootdx_minute_rows(
        "600000", offset=60, now=datetime(2026, 9, 14, 10, 0, tzinfo=ZoneInfo("Asia/Shanghai")),
        client_factory=factory, monotonic=clock, timeout_seconds=2.0,
    )
    assert error == ""
    assert len(rows) == 1 and rows[0]["close"] == 10.0
    assert len(calls) == diagnostics["attempt_count"] == 2
    assert any(stage["stage"] == failed_stage and stage["outcome"] == "failed"
               for stage in diagnostics["attempts"][0]["stages"])
    assert closed == ([2] if failed_stage == "connect" else [1, 2])
    assert clock() < 2.0


def test_minutes_persistent_failure_has_bounded_retries_and_cause(monkeypatch):
    clock = Clock()
    monkeypatch.setattr(collector.time, "sleep", clock.sleep)
    calls = []

    def factory():
        calls.append(1)
        raise ConnectionError("still offline")

    rows, error, diagnostics = collector._mootdx_minute_rows(
        "600000", offset=60, now=datetime(2026, 9, 14, 10, 0),
        client_factory=factory, monotonic=clock, timeout_seconds=2.0,
    )
    assert rows == [] and error == "provider_failed:mootdx:ConnectionError"
    assert diagnostics["message"] == "still offline"
    assert len(calls) == diagnostics["attempt_count"] == 3
    assert clock() < 2.0


def test_minutes_do_not_restart_exhausted_symbol_allowance(monkeypatch):
    clock = Clock()
    monkeypatch.setattr(collector.time, "sleep", clock.sleep)
    calls = []

    def factory():
        calls.append(1)
        clock.sleep(1.0)
        raise TimeoutError("connection deadline")

    rows, error, diagnostics = collector._mootdx_minute_rows(
        "600000", offset=60, now=datetime(2026, 9, 14, 10, 0),
        client_factory=factory, monotonic=clock, timeout_seconds=1.0,
    )
    assert rows == [] and error
    assert len(calls) == diagnostics["attempt_count"] == 1
    assert clock() == 1.0
