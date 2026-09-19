"""Intraday refresh round budget and failure-evidence regressions (2026-09-10).

Live evidence: the round task fired every 5 minutes with a 10-minute
ExecutionTimeLimit; the 11:05 and 11:20 rounds were killed at the limit
(TaskScheduler event 329 / 0x41306) and wrote no manifest at all, while nine
completed rounds reported 737 per-symbol requests as ``provider_no_rows`` with
``appended_rows=0``.  These tests pin the self-imposed budget, the per-stage
evidence, and the distinct terminal states.

No test contacts the network: providers, clocks and senders are injected.
"""

from __future__ import annotations

import json
import sys
import types
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from quantdesk.intraday_refresh import collector
from quantdesk.intraday_refresh.alerts import dispatch_intraday_alerts, evaluate_alerts
from quantdesk.intraday_refresh.collector import (
    DEFAULT_MAX_ROUND_SECONDS,
    collect_intraday_refresh,
    _mootdx_minute_rows,
)

SHANGHAI = ZoneInfo("Asia/Shanghai")
TRADE_DAY = "2026-09-01"


def _now(hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 9, 1, hour, minute, tzinfo=SHANGHAI)


def _bar(symbol: str, dt: str = "2026-09-01 09:55:00") -> dict:
    date_part, time_part = dt.split(" ")
    return {
        "symbol": symbol, "datetime": dt, "date": date_part, "time": time_part,
        "open": 10.0, "high": 10.0, "low": 10.0, "close": 10.0,
        "volume": 100.0, "amount": 1000.0, "price_basis": "raw", "source": "fake",
    }


def _root(tmp_path: Path, symbols: list[str]) -> Path:
    (tmp_path / "state" / "ai_trader").mkdir(parents=True, exist_ok=True)
    (tmp_path / "data" / "universe").mkdir(parents=True, exist_ok=True)
    (tmp_path / "results").mkdir(parents=True, exist_ok=True)
    (tmp_path / "state" / "ai_trader" / "ledger.json").write_text(
        json.dumps({"positions": {s: {"qty": 100} for s in symbols}}), encoding="utf-8"
    )
    (tmp_path / "state" / "ai_trader" / "plan_latest.json").write_text("{}", encoding="utf-8")
    (tmp_path / "state" / "ai_trader" / "watchlist.json").write_text("{}", encoding="utf-8")
    (tmp_path / "data" / "universe" / "a_share_symbols.csv").write_text(
        "symbol\n" + "\n".join(symbols) + "\n", encoding="utf-8"
    )
    return tmp_path


class _Clock:
    """Monotonic clock that advances a fixed step on every read."""

    def __init__(self, step: float) -> None:
        self.value = 0.0
        self.step = step

    def __call__(self) -> float:
        self.value += self.step
        return self.value


def _offline_snapshot():
    return ([], "fixture.snapshot")


# ---------------------------------------------------------------------------
# Self-imposed round budget
# ---------------------------------------------------------------------------


def test_round_stops_on_its_own_budget_and_names_untried_symbols(tmp_path):
    symbols = [f"60{i:04d}" for i in range(30)]
    root = _root(tmp_path, symbols=symbols)

    manifest = collect_intraday_refresh(
        root,
        network=True,
        now=_now(10, 0),
        fetcher=lambda symbol, offset: ([_bar(symbol)], ""),
        snapshot_fetcher=_offline_snapshot,
        max_round_seconds=30.0,
        monotonic=_Clock(step=2.0),
    )

    assert manifest["status"] == "timed_out"
    assert manifest["run_state"] == "completed"
    assert 0 < len(manifest["provider_status"]) < 30
    assert 0 < len(manifest["not_attempted_symbols"]) < 30
    # every requested symbol is accounted for exactly once
    attempted = {entry["symbol"] for entry in manifest["provider_status"]}
    assert attempted | set(manifest["not_attempted_symbols"]) == set(symbols)
    assert not (attempted & set(manifest["not_attempted_symbols"]))
    assert any(rc.startswith("round_budget_exhausted") for rc in manifest["reason_codes"])

    provider_stage = next(item for item in manifest["stage_timings"] if item["stage"] == "provider")
    assert provider_stage["outcome"] == "timed_out"
    assert provider_stage["symbols_attempted"] == len(manifest["provider_status"])
    assert provider_stage["symbols_requested"] == 30


def test_budget_default_sits_under_the_task_time_limit():
    assert 0 < DEFAULT_MAX_ROUND_SECONDS < 600


def test_round_records_duration_and_stage_timings(tmp_path):
    root = _root(tmp_path, symbols=["600000"])
    manifest = collect_intraday_refresh(
        root,
        network=True,
        now=_now(10, 0),
        fetcher=lambda symbol, offset: ([_bar(symbol)], ""),
        snapshot_fetcher=_offline_snapshot,
        monotonic=_Clock(step=0.5),
    )
    stages = {item["stage"]: item for item in manifest["stage_timings"]}
    assert set(stages) >= {"snapshot", "scope", "provider"}
    assert manifest["round_duration_ms"] > 0
    for item in manifest["stage_timings"]:
        assert isinstance(item["duration_ms"], int) and item["duration_ms"] >= 0
    entry = manifest["provider_status"][0]
    assert entry["status"] == "ok"
    assert entry["duration_ms"] >= 0


def test_round_records_attempt_identity_and_budgets(tmp_path):
    root = _root(tmp_path, symbols=["600000"])
    manifest = collect_intraday_refresh(
        root,
        network=True,
        now=_now(10, 0),
        fetcher=lambda symbol, offset: ([_bar(symbol)], ""),
        snapshot_fetcher=_offline_snapshot,
        run_id="intraday-fixture",
        attempt_id="intraday-fixture#2",
        scheduler_instance="task-329",
        monotonic=_Clock(step=0.5),
    )
    assert manifest["run_id"] == "intraday-fixture"
    assert manifest["attempt_id"] == "intraday-fixture#2"
    assert manifest["scheduler_instance"] == "task-329"
    assert isinstance(manifest["runtime_pid"], int) and manifest["runtime_pid"] > 0
    assert len(manifest["source_fingerprint"]) == 64
    assert manifest["round_budget_seconds"] == float(DEFAULT_MAX_ROUND_SECONDS)
    assert manifest["symbol_budget_seconds"] > 0
    assert manifest["finished_at"]


def test_attempt_identity_defaults_are_explicit(tmp_path):
    root = _root(tmp_path, symbols=["600000"])
    manifest = collect_intraday_refresh(
        root,
        network=True,
        now=_now(10, 0),
        fetcher=lambda symbol, offset: ([_bar(symbol)], ""),
        snapshot_fetcher=_offline_snapshot,
        run_id="intraday-fixture",
        monotonic=_Clock(step=0.5),
    )
    assert manifest["attempt_id"] == "intraday-fixture#1"
    assert manifest["scheduler_instance"] == "unknown"


# ---------------------------------------------------------------------------
# Provider failure evidence: stage, source class, duration - never "no rows"
# ---------------------------------------------------------------------------


def test_provider_failure_exposes_the_failing_stage(tmp_path):
    root = _root(tmp_path, symbols=["600000"])
    manifest = collect_intraday_refresh(
        root,
        network=True,
        now=_now(10, 0),
        fetcher=lambda symbol, offset: ([], "provider_failed:mootdx:ReadTimeout"),
        snapshot_fetcher=_offline_snapshot,
    )
    entry = manifest["provider_status"][0]
    assert entry["status"] == "failed"
    assert entry["error_stage"] == "timeout"
    assert entry["error_type"] == "ReadTimeout"
    assert entry["duration_ms"] >= 0
    assert manifest["status"] == "blocked"


@pytest.mark.parametrize(
    ("reason", "stage"),
    [
        ("provider_failed:mootdx:ConnectionError", "connect"),
        ("provider_failed:mootdx:ReadTimeout", "timeout"),
        ("provider_parse_failed:mootdx:UnicodeDecodeError", "parse"),
        ("provider_no_rows:mootdx", "empty"),
        ("provider_close_failed:mootdx:OSError", "close"),
    ],
)
def test_reason_strings_are_classified_by_stage(tmp_path, reason, stage):
    root = _root(tmp_path, symbols=["600000"])
    manifest = collect_intraday_refresh(
        root,
        network=True,
        now=_now(10, 0),
        fetcher=lambda symbol, offset: ([], reason),
        snapshot_fetcher=_offline_snapshot,
    )
    assert manifest["provider_status"][0]["error_stage"] == stage


def test_richer_fetch_contract_carries_stage_evidence(tmp_path):
    root = _root(tmp_path, symbols=["600000"])
    stages = [{"stage": "connect", "outcome": "failed", "duration_ms": 7}]
    manifest = collect_intraday_refresh(
        root,
        network=True,
        now=_now(10, 0),
        fetcher=lambda symbol, offset: {
            "rows": [],
            "error": "provider_failed:mootdx:ConnectionError",
            "diagnostics": {
                "stages": stages, "error_stage": "connect",
                "error_type": "ConnectionError", "message": "connection aborted",
            },
        },
        snapshot_fetcher=_offline_snapshot,
    )
    entry = manifest["provider_status"][0]
    assert entry["error_stage"] == "connect"
    assert entry["stages"] == stages
    assert entry["message"] == "connection aborted"


def test_mootdx_client_is_constructed_fail_closed(monkeypatch):
    """Library defaults swallow the error; the collector must not use them."""

    captured: dict[str, object] = {}

    class FakeQuotes:
        @staticmethod
        def factory(**kwargs):
            captured.update(kwargs)
            raise RuntimeError("connection refused")

    quotes_module = types.ModuleType("mootdx.quotes")
    quotes_module.Quotes = FakeQuotes
    mootdx_module = types.ModuleType("mootdx")
    mootdx_module.quotes = quotes_module
    monkeypatch.setitem(sys.modules, "mootdx", mootdx_module)
    monkeypatch.setitem(sys.modules, "mootdx.quotes", quotes_module)

    rows, error, diagnostics = _mootdx_minute_rows("600000", offset=60, now=_now(10, 0))

    assert captured["auto_retry"] is False
    assert captured["raise_exception"] is True
    assert rows == []
    assert error == "provider_failed:mootdx:RuntimeError"
    assert error != "provider_no_rows:mootdx"
    assert diagnostics["error_stage"] == "connect"
    assert diagnostics["error_type"] == "RuntimeError"
    assert [item["stage"] for item in diagnostics["stages"]] == ["connect"]
    assert diagnostics["stages"][0]["outcome"] == "failed"


def test_empty_provider_response_is_labelled_empty_not_failed(monkeypatch):
    import pandas as pd

    class FakeClient:
        def bars(self, **_kwargs):
            return pd.DataFrame([])

        def close(self):
            return None

    class FakeQuotes:
        @staticmethod
        def factory(**_kwargs):
            return FakeClient()

    quotes_module = types.ModuleType("mootdx.quotes")
    quotes_module.Quotes = FakeQuotes
    mootdx_module = types.ModuleType("mootdx")
    mootdx_module.quotes = quotes_module
    monkeypatch.setitem(sys.modules, "mootdx", mootdx_module)
    monkeypatch.setitem(sys.modules, "mootdx.quotes", quotes_module)

    rows, error, diagnostics = _mootdx_minute_rows("600000", offset=60, now=_now(10, 0))
    assert rows == []
    assert error == "provider_no_rows:mootdx"
    assert diagnostics["error_stage"] == "empty"


# ---------------------------------------------------------------------------
# Terminal states stay distinct and a killed round cannot look successful
# ---------------------------------------------------------------------------


def test_outside_window_round_is_closed_and_never_fetches(tmp_path):
    root = _root(tmp_path, symbols=["600000"])
    calls: list[str] = []
    manifest = collect_intraday_refresh(
        root,
        network=True,
        now=_now(15, 10),  # after the 15:05 last invocation
        fetcher=lambda symbol, offset: calls.append(symbol) or ([], ""),
        snapshot_fetcher=_offline_snapshot,
    )
    assert manifest["status"] == "closed"
    assert "outside_collection_window" in manifest["reason_codes"]
    assert manifest["provider_status"] == []
    assert calls == []
    # A no-op close must not be readable as a successful collection.
    written = json.loads((root / "results/intraday/intraday_refresh_latest.json").read_text(encoding="utf-8"))
    assert written["status"] == "closed"
    assert written["run_id"] == manifest["run_id"]
    assert written["run_state"] == "skipped"
    assert written["appended_rows"] == 0


def test_killed_round_leaves_a_marker_and_the_next_round_reports_it(tmp_path):
    root = _root(tmp_path, symbols=["600000"])
    current = root / "results/intraday/intraday_refresh_current.json"
    current.parent.mkdir(parents=True, exist_ok=True)
    current.write_text(
        json.dumps({
            "run_id": "intraday-20260910T110500",
            "run_state": "running",
            "status": "ready",
            "as_of": "11:05:12",
            "appended_rows": 0,
            "provider_status": [{"symbol": "600000", "status": "ok"}],
        }),
        encoding="utf-8",
    )
    latest = root / "results/intraday/intraday_refresh_latest.json"
    latest.write_text(
        json.dumps({"run_id": "intraday-20260910T105501", "run_state": "completed", "status": "blocked"}),
        encoding="utf-8",
    )

    manifest = collect_intraday_refresh(
        root,
        network=True,
        now=_now(11, 20),
        fetcher=lambda symbol, offset: ([_bar(symbol)], ""),
        snapshot_fetcher=_offline_snapshot,
    )

    assert manifest["previous_run"]["run_id"] == "intraday-20260910T110500"
    assert manifest["previous_run"]["attempted_symbols"] == 1
    assert any(rc.startswith("previous_run_abandoned") for rc in manifest["reason_codes"])

    # The stale latest is replaced by this round's own identity, never reused.
    written = json.loads(latest.read_text(encoding="utf-8"))
    assert written["run_id"] == manifest["run_id"] != "intraday-20260910T105501"
    assert written["run_state"] == "completed"

    marker = json.loads(current.read_text(encoding="utf-8"))
    assert marker["run_id"] == manifest["run_id"]
    assert marker["run_state"] == "completed"


def test_previous_session_bars_do_not_crash_the_validator():
    """DSR-08: a normal previous-day bar must not raise TypeError."""

    from quantdesk.intraday_refresh.collector import validate_minute_bar

    row = _bar("600000", "2026-09-09 15:00:00")
    now = datetime(2026, 9, 10, 9, 35, tzinfo=SHANGHAI)

    ok, reason = validate_minute_bar(row, now=now, symbol="600000")

    assert ok is True
    assert reason == ""


def test_busy_instance_never_takes_over_the_active_round(tmp_path):
    """DSR-09: busy may only leave its own attempt record."""

    from quantdesk.interprocess_lock import interprocess_file_lock

    root = _root(tmp_path, symbols=["600000"])
    current = root / "results/intraday/intraday_refresh_current.json"
    current.parent.mkdir(parents=True, exist_ok=True)
    active = {"run_id": "old-active", "run_state": "running", "status": "ready"}
    current.write_text(json.dumps(active), encoding="utf-8")
    latest = root / "results/intraday/intraday_refresh_latest.json"
    latest.write_text(json.dumps({"run_id": "old-latest", "run_state": "completed"}), encoding="utf-8")

    lock_path = root / "data" / "intraday" / "1m" / ".locks" / "stage6_append_only_batch.lock"
    with interprocess_file_lock(lock_path, timeout_seconds=0):
        manifest = collect_intraday_refresh(
            root,
            network=True,
            now=_now(10, 0),
            fetcher=lambda symbol, offset: ([_bar(symbol)], ""),
            snapshot_fetcher=_offline_snapshot,
            run_id="new-run",
        )

    assert manifest["status"] == "already_running"
    # The active round's evidence is untouched, including its start marker.
    assert json.loads(current.read_text(encoding="utf-8")) == active
    assert json.loads(latest.read_text(encoding="utf-8"))["run_id"] == "old-latest"
    # The refused round is recorded separately.
    busy = root / "results/intraday/busy_attempts/new-run.json"
    assert busy.is_file()
    assert json.loads(busy.read_text(encoding="utf-8"))["record"] == "intraday_busy_attempt"
    assert manifest["snapshot"] == {}  # no snapshot was fetched under a lock it did not own


def test_round_over_budget_after_the_last_symbol_is_still_timed_out(tmp_path):
    """DSR-07: blowing the budget on the final symbol is not a clean round."""

    class _FlipClock:
        """Stays at zero until the provider call, then jumps past the budget."""

        def __init__(self):
            self.value = 0.0

        def __call__(self):
            return self.value

    root = _root(tmp_path, symbols=["600000"])
    clock = _FlipClock()

    def fetch(symbol, offset):
        clock.value = 200.0  # this symbol alone blows the 100s round budget
        return [_bar(symbol)], ""

    manifest = collect_intraday_refresh(
        root,
        network=True,
        now=_now(10, 0),
        fetcher=fetch,
        snapshot_fetcher=_offline_snapshot,
        max_round_seconds=100.0,
        monotonic=clock,
    )

    assert manifest["status"] == "timed_out"
    assert manifest["not_attempted_symbols"] == []  # every symbol was attempted
    assert any(
        reason.startswith("round_budget_exhausted:") for reason in manifest["reason_codes"]
    )
    assert any(
        reason.startswith("symbol_budget_exceeded:") for reason in manifest["reason_codes"]
    )


def test_unhandled_error_persists_this_rounds_terminal_state(tmp_path):
    """DSR-10: a catchable failure must not leave only the previous latest."""

    root = _root(tmp_path, symbols=["600000"])
    latest = root / "results/intraday/intraday_refresh_latest.json"
    latest.parent.mkdir(parents=True, exist_ok=True)
    latest.write_text(json.dumps({"run_id": "old-latest", "run_state": "completed"}), encoding="utf-8")

    def boom(*_args, **_kwargs):
        raise RuntimeError("scope exploded")

    with pytest.raises(RuntimeError):
        collect_intraday_refresh(
            root,
            network=True,
            now=_now(10, 0),
            fetcher=lambda symbol, offset: ([], ""),
            snapshot_fetcher=_offline_snapshot,
            scope_builder=boom,
            run_id="aborted-run",
        )

    marker = json.loads((root / "results/intraday/intraday_refresh_current.json").read_text(encoding="utf-8"))
    assert marker["run_state"] == "aborted"
    assert marker["status"] == "aborted"
    assert any(
        reason.startswith("round_aborted:") for reason in marker["reason_codes"]
    )
    # This round's own failure is now the freshest thing a reader can find.
    written = json.loads(latest.read_text(encoding="utf-8"))
    assert written["run_id"] == "aborted-run"
    assert written["run_state"] == "aborted"


def test_already_running_is_reported_separately(tmp_path):
    from quantdesk.interprocess_lock import interprocess_file_lock

    root = _root(tmp_path, symbols=["600000"])
    lock_path = root / "data" / "intraday" / "1m" / ".locks" / "stage6_append_only_batch.lock"
    with interprocess_file_lock(lock_path, timeout_seconds=0):
        manifest = collect_intraday_refresh(
            root,
            network=True,
            now=_now(10, 0),
            fetcher=lambda symbol, offset: ([_bar(symbol)], ""),
            snapshot_fetcher=_offline_snapshot,
        )
    assert manifest["status"] == "already_running"
    assert manifest["run_state"] == "skipped"
    assert manifest["status"] != "blocked"


# ---------------------------------------------------------------------------
# Health alerts must not miss a self-timed-out round
# ---------------------------------------------------------------------------


def test_alerts_flag_a_timed_out_round_with_untried_symbols(tmp_path):
    root = _root(tmp_path, symbols=["600000"])
    current, resolved = evaluate_alerts(
        root,
        intraday_manifest={
            "status": "timed_out",
            "not_attempted_symbols": ["600000", "000001"],
        },
    )
    keys = {item["dedup_key"] for item in current}
    assert "intraday_round_timed_out" in keys
    timed_out = next(item for item in current if item["dedup_key"] == "intraday_round_timed_out")
    assert timed_out["reason_code"] == "intraday_round_budget_exhausted"
    assert timed_out["affected"]["symbols_sample"] == ["600000", "000001"]

    recovered, _ = evaluate_alerts(root, intraday_manifest={"status": "ready"})
    assert "intraday_round_timed_out" not in {item["dedup_key"] for item in recovered}


def test_alert_dispatch_stays_a_preview_without_push_authorization(tmp_path):
    root = _root(tmp_path, symbols=["600000"])
    result = dispatch_intraday_alerts(
        root,
        network=False,
        push=False,
        dry_run=True,
        intraday_manifest={"status": "timed_out", "not_attempted_symbols": ["600000"]},
    )
    assert result["status"] == "preview_generated"
    assert result["event_count"] >= 1


# ---------------------------------------------------------------------------
# DSR-07/09/10 second round: the deadline is executed, and the shared files
# have exactly one writer
# ---------------------------------------------------------------------------


def test_an_empty_scope_cannot_hide_a_blown_round_budget(tmp_path):
    """DSR-07: the budget check cannot live inside the per-symbol loop.

    A slow snapshot plus an empty scope used to finish ``ready`` after 700 s of
    a 480 s budget, because the only check sat behind ``and symbols``.
    """

    root = _root(tmp_path, symbols=["600000"])
    clock = _Clock(step=0.0)
    scope_calls: list[str] = []

    def slow_snapshot():
        clock.value += 700.0
        return [], "fixture.snapshot"

    def scope(*_args, **_kwargs):
        scope_calls.append("scope")
        return {"symbols": [], "warnings": []}

    manifest = collect_intraday_refresh(
        root,
        network=True,
        now=_now(10, 0),
        fetcher=lambda symbol, offset: ([], ""),
        snapshot_fetcher=slow_snapshot,
        scope_builder=scope,
        max_round_seconds=480.0,
        monotonic=clock,
    )

    assert manifest["status"] == "timed_out"
    assert any(rc.startswith("round_budget_exhausted") for rc in manifest["reason_codes"])
    # The next stage must not start on a budget that is already spent.
    assert scope_calls == []


def test_a_timed_out_round_is_never_relabelled_blocked(tmp_path):
    """DSR-07: terminal states stay mutually exclusive."""

    class _FlipClock:
        def __init__(self) -> None:
            self.value = 0.0

        def __call__(self) -> float:
            return self.value

    root = _root(tmp_path, symbols=["600000"])
    clock = _FlipClock()

    def fetch(symbol, offset):
        clock.value = 700.0
        return [], "provider_failed:mootdx:ReadTimeout"

    manifest = collect_intraday_refresh(
        root,
        network=True,
        now=_now(10, 0),
        fetcher=fetch,
        snapshot_fetcher=_offline_snapshot,
        max_round_seconds=100.0,
        monotonic=clock,
    )

    assert manifest["status"] == "timed_out"
    # The failure is still reported, but it does not become the terminal state.
    assert "provider_all_failed" in manifest["reason_codes"]


def test_default_provider_timeout_is_the_smallest_remaining_budget(tmp_path, monkeypatch):
    """DSR-07: one constant timeout respects neither budget."""

    captured: dict[str, object] = {}
    source_allowances = []
    actual_fetch = collector._live_minute_rows

    def capture_allowance(*args, **kwargs):
        source_allowances.append(kwargs["timeout_seconds"])
        return actual_fetch(*args, **kwargs)

    monkeypatch.setattr(collector, "_live_minute_rows", capture_allowance)

    class FakeFrame:
        def __len__(self) -> int:
            return 1

        def to_dict(self, orientation):
            assert orientation == "records"
            return [_bar("600000")]

    class FakeClient:
        def bars(self, **_kwargs):
            return FakeFrame()

        def close(self) -> None:
            pass

    class FakeQuotes:
        @staticmethod
        def factory(**kwargs):
            captured.update(kwargs)
            return FakeClient()

    quotes_module = types.ModuleType("mootdx.quotes")
    quotes_module.Quotes = FakeQuotes
    mootdx_module = types.ModuleType("mootdx")
    mootdx_module.quotes = quotes_module
    monkeypatch.setitem(sys.modules, "mootdx", mootdx_module)
    monkeypatch.setitem(sys.modules, "mootdx.quotes", quotes_module)

    root = _root(tmp_path, symbols=["600000"])
    manifest = collect_intraday_refresh(
        root,
        network=True,
        now=_now(10, 0),
        snapshot_fetcher=_offline_snapshot,
        max_round_seconds=5.0,
        max_symbol_seconds=3.0,
        monotonic=lambda: 0.0,
    )

    assert source_allowances == [3]  # both sources share the symbol budget, not 8
    assert 0 < captured["timeout"] <= 3  # native may reserve time for its fallback
    assert manifest["provider_status"][0]["status"] == "ok"


def test_terminal_state_is_written_before_the_lock_is_released(tmp_path, monkeypatch):
    """DSR-09: a successor holding the lock must never be overwritten."""

    root = _root(tmp_path, symbols=["600000"])
    order: list[str] = []
    real_lock = collector._intraday_refresh_lock
    real_write = collector._write_manifests

    @contextmanager
    def traced_lock(lock_root):
        with real_lock(lock_root) as path:
            try:
                yield path
            finally:
                order.append("lock_release")

    def traced_write(manifest_root, run_id, manifest, **kwargs):
        order.append("terminal_write")
        return real_write(manifest_root, run_id, manifest, **kwargs)

    monkeypatch.setattr(collector, "_intraday_refresh_lock", traced_lock)
    monkeypatch.setattr(collector, "_write_manifests", traced_write)

    collect_intraday_refresh(
        root,
        network=True,
        now=_now(10, 0),
        fetcher=lambda symbol, offset: ([_bar(symbol)], ""),
        snapshot_fetcher=_offline_snapshot,
        run_id="lock-order-run",
    )

    assert order.index("terminal_write") < order.index("lock_release")


def test_offline_report_never_takes_over_the_active_round(tmp_path):
    """DSR-09: a read-only report must not clobber the round that owns the lock."""

    from quantdesk.interprocess_lock import interprocess_file_lock

    root = _root(tmp_path, symbols=["600000"])
    current = root / "results/intraday/intraday_refresh_current.json"
    current.parent.mkdir(parents=True, exist_ok=True)
    active = {"run_id": "old-active", "run_state": "running", "status": "ready"}
    current.write_text(json.dumps(active), encoding="utf-8")
    latest = root / "results/intraday/intraday_refresh_latest.json"
    latest.write_text(
        json.dumps({"run_id": "old-latest", "run_state": "completed"}), encoding="utf-8"
    )

    lock_path = root / "data" / "intraday" / "1m" / ".locks" / "stage6_append_only_batch.lock"
    with interprocess_file_lock(lock_path, timeout_seconds=0):
        manifest = collect_intraday_refresh(
            root, network=False, now=_now(10, 0), run_id="offline-report"
        )

    assert manifest["status"] == "planned_offline"
    assert json.loads(current.read_text(encoding="utf-8")) == active
    assert json.loads(latest.read_text(encoding="utf-8"))["run_id"] == "old-latest"
    assert not any(
        rc.startswith("previous_run_abandoned") for rc in manifest["reason_codes"]
    )
    # Its own run directory is still recorded: only the shared files belong to
    # the round holding the lock.
    assert (root / "results/intraday/runs/offline-report/manifest.json").is_file()


def test_closed_report_never_takes_over_the_active_round(tmp_path):
    """DSR-09: a closed-window report is a reader, not a writer."""

    from quantdesk.interprocess_lock import interprocess_file_lock

    root = _root(tmp_path, symbols=["600000"])
    current = root / "results/intraday/intraday_refresh_current.json"
    current.parent.mkdir(parents=True, exist_ok=True)
    active = {"run_id": "old-active", "run_state": "running", "status": "ready"}
    current.write_text(json.dumps(active), encoding="utf-8")
    latest = root / "results/intraday/intraday_refresh_latest.json"
    latest.write_text(
        json.dumps({"run_id": "old-latest", "run_state": "completed"}), encoding="utf-8"
    )

    lock_path = root / "data" / "intraday" / "1m" / ".locks" / "stage6_append_only_batch.lock"
    with interprocess_file_lock(lock_path, timeout_seconds=0):
        manifest = collect_intraday_refresh(
            root,
            network=True,
            now=_now(15, 10),
            snapshot_fetcher=_offline_snapshot,
            run_id="closed-report",
        )

    assert manifest["status"] == "closed"
    assert "outside_collection_window" in manifest["reason_codes"]
    assert json.loads(current.read_text(encoding="utf-8")) == active
    assert json.loads(latest.read_text(encoding="utf-8"))["run_id"] == "old-latest"
    assert manifest["provider_status"] == []


def test_terminal_write_failure_still_records_this_round_state(tmp_path, monkeypatch):
    """DSR-10: one failed latest write must not skip the current marker."""

    root = _root(tmp_path, symbols=["600000"])
    latest = root / "results/intraday/intraday_refresh_latest.json"
    latest.parent.mkdir(parents=True, exist_ok=True)
    latest.write_text(
        json.dumps({"run_id": "old-latest", "run_state": "completed"}), encoding="utf-8"
    )
    current = root / "results/intraday/intraday_refresh_current.json"

    original_write_text = Path.write_text
    failures = [0]

    def flaky_write_text(path, data, *args, **kwargs):
        if path == latest and failures[0] == 0:
            failures[0] += 1
            raise OSError("transient latest write failure")
        return original_write_text(path, data, *args, **kwargs)

    monkeypatch.setattr(Path, "write_text", flaky_write_text)

    with pytest.raises(OSError):
        collect_intraday_refresh(
            root,
            network=True,
            now=_now(10, 0),
            fetcher=lambda symbol, offset: ([_bar(symbol)], ""),
            snapshot_fetcher=_offline_snapshot,
            run_id="flaky-write-run",
        )

    marker = json.loads(current.read_text(encoding="utf-8"))
    assert marker["run_id"] == "flaky-write-run"
    assert marker["run_state"] == "completed"
    assert marker["write_failures"] == ["intraday_refresh_latest.json:OSError"]


def test_an_aborted_stage_is_named_in_this_rounds_manifest(tmp_path):
    """DSR-10: the stage, its symbol and a masked error are recorded."""

    root = _root(tmp_path, symbols=["600000"])

    def boom(symbol, offset):
        raise RuntimeError("provider exploded key=SECRET123")

    with pytest.raises(RuntimeError):
        collect_intraday_refresh(
            root,
            network=True,
            now=_now(10, 0),
            fetcher=boom,
            snapshot_fetcher=_offline_snapshot,
            run_id="stage-abort-run",
        )

    written = json.loads(
        (root / "results/intraday/runs/stage-abort-run/manifest.json").read_text(encoding="utf-8")
    )
    assert written["run_state"] == "aborted"
    assert any(rc.startswith("round_aborted:") for rc in written["reason_codes"])
    failing = written["stage_timings"][-1]
    assert failing["stage"] == "provider"
    assert failing["outcome"] == "aborted"
    assert failing["symbol"] == "600000"
    assert failing["error_type"] == "RuntimeError"
    assert failing["error_stage"] == "request"
    assert "SECRET123" not in json.dumps(written)


def test_the_snapshot_stage_spends_what_is_left_of_the_round(tmp_path, monkeypatch):
    """DSR-07: the snapshot is bounded by the round, not only checked after it."""

    root = _root(tmp_path, symbols=["600000"])
    seen: dict[str, object] = {}

    def snapshot(snapshot_root, *, budget_seconds=None, **_kwargs):
        seen["budget_seconds"] = budget_seconds
        return {"ok": True, "status": "ok", "row_count": 1, "movers": []}

    monkeypatch.setattr(collector, "refresh_full_market_snapshot", snapshot)

    collect_intraday_refresh(
        root,
        network=True,
        now=_now(10, 0),
        fetcher=lambda symbol, offset: ([_bar(symbol)], ""),
        snapshot_fetcher=lambda: ([], ""),
        max_round_seconds=480.0,
        monotonic=lambda: 0.0,
    )

    assert seen["budget_seconds"] == 480.0


def test_a_sina_page_that_cannot_fit_the_window_is_never_started(monkeypatch):
    """DSR-07: half-started requests are what let a bounded round overrun."""

    from quantdesk.intraday_refresh import snapshot as snapshot_module

    calls: list[object] = []

    def fake_get(_url, params=None, headers=None, timeout=None):
        calls.append((params, timeout))
        raise AssertionError("a page without budget must not be requested")

    monkeypatch.setattr("requests.get", fake_get)

    rows = snapshot_module._fetch_sina_spot_pages(budget_seconds=0.2, clock=lambda: 0.0)

    assert rows == []
    assert calls == []


def test_a_sina_page_timeout_is_clamped_to_the_leftover_window(monkeypatch):
    """DSR-07: no request may spend a timeout the round no longer has."""

    from quantdesk.intraday_refresh import snapshot as snapshot_module

    timeouts: list[object] = []

    class FakeResponse:
        def raise_for_status(self) -> None:
            pass

        def json(self) -> list:
            return []  # an empty page ends the paging loop after one request

    def fake_get(_url, params=None, headers=None, timeout=None):
        timeouts.append(timeout)
        return FakeResponse()

    monkeypatch.setattr("requests.get", fake_get)

    snapshot_module._fetch_sina_spot_pages(
        budget_seconds=2.0, timeout_seconds=8.0, clock=lambda: 0.0
    )

    assert timeouts == [2.0]

