"""Execution boundaries: ordering, failures, repeated calls and lazy injection.

All evidence here is synthetic and all disk paths are pytest temporary paths.
The same scenario helpers are also used by the external frozen-source comparison.
"""
from __future__ import annotations

import copy
import inspect
import json
import subprocess
import sys
from dataclasses import FrozenInstanceError
from pathlib import Path
from types import SimpleNamespace

import pytest

from quantdesk.paper import auto_paper_portfolio as paper
from quantdesk.paper.execution import conditional, daily, snapshot
from tests.test_snapshot_open_fill_20261012 import (
    calendar, daily as write_daily, run_day, setup_book, source_row, write_snapshot,
)

DAY = "2026-10-12"
NEXT = "2026-10-13"


def _forbid(*args, **kwargs):
    raise AssertionError("unexpected dependency call")


def test_execution_imports_do_not_load_the_legacy_engine_or_io_modules():
    program = '''
import builtins, sys
forbidden = {'quantdesk.paper.auto_paper_portfolio', 'quantdesk.auto_paper_portfolio',
             'quantdesk.paper.state_store', 'quantdesk.paper.publication',
             'quantdesk.daily_candidate_refresh'}
original = builtins.__import__
def guard(name, *args, **kwargs):
    assert name not in forbidden, name
    return original(name, *args, **kwargs)
builtins.__import__ = guard
from quantdesk.paper.execution import daily, conditional, snapshot, ports
assert not forbidden.intersection(sys.modules), forbidden.intersection(sys.modules)
'''
    result = subprocess.run([sys.executable, "-B", "-c", program], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("name,module,factory", [
    ("_run_auto_paper_portfolio_unlocked", daily, "_daily_execution_ports"),
    ("_run_conditional_auto_paper_update", conditional, "_conditional_execution_ports"),
    ("_run_next_open_snapshot_update", snapshot, "_snapshot_execution_ports"),
])
def test_compatibility_entrypoints_forward_every_argument_and_rebind(monkeypatch, tmp_path, name, module, factory):
    entry = getattr(paper, name)
    supplied = {key: object() for key in inspect.signature(entry).parameters}
    captured = []
    result = object()
    monkeypatch.setattr(module, "run", lambda **kwargs: captured.append(kwargs) or result)
    for index in (1, 2):
        path = tmp_path / str(index) / "state.json"
        summary = lambda *args, index=index, **kwargs: index
        monkeypatch.setattr(paper, "AUTO_PAPER_PORTFOLIO_STATE_JSON", path)
        monkeypatch.setattr(paper, "_summary", summary)
        monkeypatch.setattr(paper, "_auto_paper_state_store", _forbid)
        monkeypatch.setattr(paper, "_archive_project_root", _forbid)
        monkeypatch.setattr(paper, "_sector_rotation_path", _forbid)
        assert entry(**supplied) is result
        forwarded = captured[-1].copy()
        ports = forwarded.pop("ports")
        assert forwarded == supplied
        assert ports.paths.state_path == path
        assert ports.valuation.summary is summary
        with pytest.raises(FrozenInstanceError):
            ports.paths.state_path = tmp_path
    assert captured[0]["ports"] is not captured[1]["ports"]


def test_factories_preserve_shared_references_without_resolving_lazy_paths(monkeypatch):
    unrecorded = ["previous warning"]
    monkeypatch.setattr(paper, "_SHADOW_UNRECORDED", unrecorded)
    for name in ("_archive_project_root", "_auto_paper_state_store", "_sector_rotation_path"):
        monkeypatch.setattr(paper, name, _forbid)
    ports = paper._daily_execution_ports()
    assert ports.risk.unrecorded is unrecorded and unrecorded == ["previous warning"]
    assert ports.open_fill is paper.open_fill
    assert paper._snapshot_execution_ports().journal is paper.fill_journal
    assert paper._conditional_execution_ports().state.auto_paper_state_store is _forbid
    # Incompatible modes return before clearing the shared warnings.
    result = paper._run_auto_paper_portfolio_unlocked(
        strategy_fill_mode="next_open_snapshot", enable_intraday_swing=True)
    assert not result["ok"] and unrecorded == ["previous warning"]


def _redirect(monkeypatch, root):
    values = {"PROJECT_ROOT": root, "RESULTS_DIR": root / "results", "DAILY_DIR": root / "data/daily",
              "INTRADAY_DIR": root / "data/intraday", "DAILY_FEATURE_LATEST_CSV": root / "features.csv",
              "AUTO_PAPER_PORTFOLIO_STATE_JSON": root / "state.json",
              "AUTO_PAPER_PORTFOLIO_HISTORY_JSON": root / "history.json",
              "AUTO_STRATEGY_LATEST_JSON": root / "strategy.json",
              "SWING_TRADE_PLANS_LATEST_JSON": root / "swing.json",
              "INTRADAY_EXECUTION_LATEST_JSON": root / "intraday.json"}
    for name, value in values.items():
        monkeypatch.setattr(paper, name, value)
    for name, value in {"_archive_project_root": root, "_conditional_plan_path": root / "plan.json",
                        "_conditional_execution_path": root / "execution.json",
                        "_risk_gate_shadow_path": root / "shadow.json",
                        "_risk_gate_shadow_error_path": root / "shadow_errors.jsonl",
                        "_sector_rotation_path": root / "sector.json"}.items():
        monkeypatch.setattr(paper, name, lambda value=value: value)
    monkeypatch.setattr(paper, "now_text", lambda: DAY + "T18:00:00+08:00")
    monkeypatch.setattr(paper, "_load_auto_adjustment_factors", lambda *_: None)
    monkeypatch.setattr(paper, "_with_daily_pnl", lambda summary, **_: dict(summary))
    monkeypatch.setattr(paper, "_enrich_positions_with_theme_map", lambda rows: rows)


def _conditional_scenario(monkeypatch, root, *, dry_run=False, stale=False, repeat=False,
                          fail_at=None, state_input=None, trace=None):
    """Capture real coordinator decisions, with every environmental effect injected."""
    _redirect(monkeypatch, root)
    events, calls = trace if trace is not None else ([], {})
    state = copy.deepcopy(state_input if state_input is not None else paper._empty_state(10000))
    state["daily_actions"] = []
    def effect(name, value=None):
        def call(*args, **kwargs):
            events.append(name)
            calls.setdefault(name, []).append(copy.deepcopy({"args": args, "kwargs": kwargs}))
            if name == fail_at:
                raise OSError("injected:" + name)
            return copy.deepcopy(value) if value is not None else (args[0] if args else {})
        return call
    monkeypatch.setattr(paper, "_load_state", effect("load", state))
    monkeypatch.setattr(paper, "evaluate_data_freshness", lambda: {
        "can_review_yesterday_positions": not stale, "target_complete_trade_date": DAY})
    next_plan = {"ok": True, "plan_id": "next", "plan_date": DAY, "trade_date": NEXT,
                 "summary": {"new_candidate_plan_count": 0}}
    due = {"plan_id": "due", "plan_date": "2026-10-09", "trade_date": DAY, "symbols": []}
    execution = {"ok": True, "plan_id": "due", "trade_date": DAY, "events": [], "summary": {}}
    monkeypatch.setattr(paper, "build_conditional_trade_plan", effect("build", next_plan))
    monkeypatch.setattr(paper, "enrich_legacy_due_plan_candidate_envelopes", effect("enrich", {
        "status": "completed", "plan": {**due, "enriched_fixture": True}}))
    monkeypatch.setattr(paper, "inspect_plan_minute_inputs", effect("inspect", {}))
    monkeypatch.setattr(paper, "simulate_conditional_plan_file", effect("simulate", execution))
    monkeypatch.setattr(paper, "publish_archived_conditional_plan", effect("archive", {"status": "passed"}))
    monkeypatch.setattr(paper, "apply_conditional_execution_to_state", effect("apply", {
        "state": state, "actions": [], "already_updated_today": repeat}))
    monkeypatch.setattr(paper, "intraday_gap_status", effect("minute_evidence", {}))
    monkeypatch.setattr(paper, "build_conditional_replay_receipt", effect("receipt", {"status": "passed"}))
    monkeypatch.setattr(paper, "build_decision_review", lambda *_: {})
    monkeypatch.setattr(paper, "build_yesterday_holding_review", lambda *_: {})
    monkeypatch.setattr(paper, "summarize_shadow_records", effect("shadow_summary", {}))
    monkeypatch.setattr(paper, "write_shadow_summary", effect("shadow_write", {}))
    monkeypatch.setattr(paper, "_record_shadow_failure", effect("shadow_failure", {}))
    monkeypatch.setattr(paper, "_write_conditional_compat_artifacts", effect("compat", {"report_path": root / "compat.md"}))
    monkeypatch.setattr(paper, "_write_auto_paper_state", effect("state"))
    monkeypatch.setattr(paper, "_auto_paper_state_store", lambda: SimpleNamespace(read=effect("persisted_read", state)))
    monkeypatch.setattr(paper, "_attach_bound_replay_review", effect("attach", {}))
    monkeypatch.setattr(paper, "_write_auto_paper_latest", effect("latest"))
    monkeypatch.setattr(paper, "_append_history", effect("history", {"items": []}))
    monkeypatch.setattr(paper, "_write_report", effect("report", root / "report.md"))
    result = paper._run_conditional_auto_paper_update(initial_cash=10000, quantity_per_stock=100,
        strategy="all", intraday_source="1m", allow_daily_approx=False, dry_run=dry_run,
        auto_strategy_path=None, auto_strategy_payload={}, plan_date=DAY, trade_date=NEXT,
        lookback_days=250, allow_same_day_rerun=False, risk_gate_shadow_enabled=True,
        risk_gate_enforced=False, due_plan_payload=due)
    return result, events, calls


@pytest.mark.parametrize("dry_run,stale,repeat", [
    (False, False, False), (False, False, True), (True, False, False),
    (False, True, False), (True, True, False),
])
def test_conditional_publication_order_and_no_write_preview(monkeypatch, tmp_path, dry_run, stale, repeat):
    result, events, calls = _conditional_scenario(monkeypatch, tmp_path, dry_run=dry_run, stale=stale, repeat=repeat)
    writes = [event for event in events if event in {"archive", "shadow_write", "compat", "state", "latest", "history", "report"}]
    if dry_run:
        assert writes == []
    elif stale:
        assert writes == ["shadow_write", "latest", "report", "latest"]
    elif repeat:
        assert writes == ["archive", "shadow_write", "compat", "report", "latest"]
    else:
        assert writes == ["archive", "shadow_write", "compat", "state", "latest", "history", "report", "latest"]
        assert events.index("state") < events.index("persisted_read") < events.index("receipt") < events.index("attach")
    if stale:
        assert result["review_mode"] == "stale_observation" and result["summary"]["daily_pnl"] is None
        assert "simulate" not in events
    else:
        assert events.index("enrich") < events.index("inspect") < events.index("simulate") < events.index("apply")
        assert len(calls["simulate"]) == 1
        assert calls["simulate"][0]["args"][0]["enriched_fixture"]
        assert calls["simulate"][0]["kwargs"]["write"] is (not dry_run)
        apply_args = calls["apply"][0]["kwargs"]
        assert apply_args["risk_gate_shadow_write_errors"] is (not dry_run)
        assert (apply_args["risk_gate_shadow_ledger_snapshot_dir"] is None) is dry_run
        assert calls["attach"][0]["kwargs"]["dry_run"] is dry_run
        assert result["already_updated_today"] is repeat


@pytest.mark.parametrize("stale", [False, True])
@pytest.mark.parametrize("failure", ["shadow_summary", "shadow_write"])
def test_shadow_failure_retains_legacy_publication(monkeypatch, tmp_path, stale, failure):
    result, events, _ = _conditional_scenario(monkeypatch, tmp_path, stale=stale, fail_at=failure)
    assert result["ok"] and "shadow_failure" in events
    assert events[-2:] == ["report", "latest"]


@pytest.mark.parametrize("failure", ["archive", "compat", "state", "persisted_read", "receipt", "attach", "history", "report"])
def test_conditional_failure_never_continues_past_the_failed_effect(monkeypatch, tmp_path, failure):
    trace = ([], {})
    with pytest.raises(OSError, match="injected:" + failure):
        _conditional_scenario(monkeypatch, tmp_path, fail_at=failure, trace=trace)
    assert trace[0][-1] == failure
    if failure in ("archive", "compat", "state"):
        assert "latest" not in trace[0]


def _daily_scenario(monkeypatch, root, *, mode="init", dry_run=False, force_reset=False,
                    swing=False, fail_at=None, trace=None):
    _redirect(monkeypatch, root)
    events, calls = trace if trace is not None else ([], {})
    state = paper._empty_state(10000)
    def effect(name, value=None):
        def call(*args, **kwargs):
            events.append(name)
            calls.setdefault(name, []).append(copy.deepcopy({"args": args, "kwargs": kwargs}))
            if name == fail_at:
                raise OSError("injected:" + name)
            return copy.deepcopy(value) if value is not None else args[0]
        return call
    monkeypatch.setattr(paper, "_strategy_book_existed", lambda: False)
    monkeypatch.setattr(paper, "_load_state", effect("load", state))
    monkeypatch.setattr(paper, "load_formal_candidate_intake", lambda *_, **__: {
        "status": "passed", "input_source": "synthetic_fixture", "reason_codes": []})
    monkeypatch.setattr(paper, "formal_candidate_strategy_payload", lambda *_: {
        "generated_at": DAY, "standard_candidates": [], "long_candidates": [], "board_candidates": []})
    monkeypatch.setattr(paper, "apply_delisting_liquidation", lambda state, **_: {"ok": True, "state": state, "actions": []})
    monkeypatch.setattr(paper, "evaluate_paper_review_freshness", lambda *_, **__: {"can_review_yesterday_positions": True})
    monkeypatch.setattr(paper, "build_decision_review", lambda *_: {})
    monkeypatch.setattr(paper, "build_yesterday_holding_review", lambda *_: {})
    monkeypatch.setattr(paper, "summarize_shadow_records", lambda *_, **__: {})
    monkeypatch.setattr(paper, "write_shadow_summary", effect("shadow", {}))
    monkeypatch.setattr(paper, "_write_intraday_swing_artifacts", effect("swing", {"report_path": root / "swing.md"}))
    monkeypatch.setattr(paper, "_write_auto_paper_state", effect("state"))
    monkeypatch.setattr(paper, "_write_auto_paper_latest", effect("latest"))
    def history(value):
        effect("history")(value)
        calls["history_file_exists"] = paper.AUTO_PAPER_PORTFOLIO_HISTORY_JSON.exists()
        return {"items": []}
    monkeypatch.setattr(paper, "_append_history", history)
    monkeypatch.setattr(paper, "_write_report", effect("report", root / "report.md"))
    result = paper._run_auto_paper_portfolio_unlocked(mode=mode, initial_cash=10000,
        dry_run=dry_run, force_reset=force_reset, plan_date=DAY, enable_intraday_swing=swing,
        strategy_fill_mode="same_day_close")
    return result, events, calls


@pytest.mark.parametrize("mode,dry_run,reset,swing", [
    ("init", False, False, False), ("init", False, True, True),
    ("init", True, True, True), ("update", False, False, False),
])
def test_daily_save_order_and_only_reset_deletes_history(monkeypatch, tmp_path, mode, dry_run, reset, swing):
    history = tmp_path / "history.json"
    history.write_bytes(b"synthetic prior history")
    result, events, calls = _daily_scenario(monkeypatch, tmp_path, mode=mode, dry_run=dry_run, force_reset=reset, swing=swing)
    assert result["ok"]
    writes = [event for event in events if event != "load"]
    assert writes == ([] if dry_run else ["shadow", *(["swing"] if swing else []), "state", "latest", "history", "report", "latest"])
    if not dry_run:
        assert calls["history_file_exists"] is (not reset)
    assert history.exists() is (dry_run or not reset)


@pytest.mark.parametrize("failure", ["swing", "state", "latest", "history", "report"])
def test_daily_failure_preserves_commit_and_history_deletion_order(monkeypatch, tmp_path, failure):
    history = tmp_path / "history.json"
    history.write_bytes(b"synthetic prior history")
    trace = ([], {})
    with pytest.raises(OSError, match="injected:" + failure):
        _daily_scenario(monkeypatch, tmp_path, force_reset=True, swing=True, fail_at=failure, trace=trace)
    assert trace[0][-1] == failure
    assert history.exists() is (failure in {"swing", "state", "latest"})


@pytest.mark.parametrize("route", ["conditional", "snapshot"])
def test_daily_dispatch_uses_current_compatibility_entrypoints(monkeypatch, tmp_path, route):
    _redirect(monkeypatch, tmp_path)
    monkeypatch.setattr(paper, "load_formal_candidate_intake", lambda *_, **__: {"input_source": "fixture", "status": "passed"})
    monkeypatch.setattr(paper, "formal_candidate_strategy_payload", lambda *_: {"generated_at": DAY})
    monkeypatch.setattr(paper, "_load_state", lambda *_: paper._empty_state(10000))
    name = "_run_conditional_auto_paper_update" if route == "conditional" else "_run_next_open_snapshot_update"
    for value in (1, 2):
        sentinel = {"ok": True, "fixture": value}
        seen = []
        monkeypatch.setattr(paper, name, lambda **kwargs: seen.append(kwargs) or sentinel)
        result = paper._run_auto_paper_portfolio_unlocked(mode="update", plan_date=DAY,
            enable_intraday_swing=route == "conditional", dry_run=True,
            strategy_fill_mode="next_open_snapshot" if route == "snapshot" else "same_day_close")
        assert result is sentinel and len(seen) == 1
        assert seen[0]["dry_run"] is True and seen[0]["initial_cash"] == 10000


def test_public_entry_keeps_one_lock_and_releases_on_coordinator_exception(monkeypatch, tmp_path):
    _redirect(monkeypatch, tmp_path)
    events = []
    monkeypatch.setattr(paper, "_auto_paper_lock_path", lambda: tmp_path / "lock")
    monkeypatch.setattr(paper, "acquire_update_lock", lambda *_, **__: events.append("lock") or {"token": "fixture"})
    monkeypatch.setattr(paper, "release_update_lock", lambda *_, **__: events.append("release"))
    def fail(**kwargs):
        events.append("coordinator")
        assert kwargs["initial_cash"] == 10000
        raise OSError("synthetic coordinator exception")
    monkeypatch.setattr(paper, "_run_auto_paper_portfolio_unlocked", fail)
    with pytest.raises(OSError, match="synthetic coordinator"):
        paper.run_auto_paper_portfolio(initial_cash=10000)
    assert events == ["lock", "coordinator", "release"]


def _snapshot_scenario(monkeypatch, root, *, dry_run=False, fail_at=None, replay=False, recovered=False):
    _redirect(monkeypatch, root)
    state = paper.reconcile_paper_account(paper._empty_state(10000))
    state["fixture_recovered"] = recovered
    events = []
    captured = {}
    def effect(name):
        events.append(name)
        if name == fail_at:
            raise OSError("injected:" + name)
    records = [{"transaction_id": "update:" + DAY, "result": {"ok": True, "fixture_replay": True}}] if replay else []
    monkeypatch.setattr(paper.fill_journal, "recoverable_state", lambda *_: {
        "state": state, "records": records, "status": "recovered_uncommitted_transaction" if recovered else "aligned"})
    monkeypatch.setattr(paper, "utc_now_iso", _forbid if replay else lambda: DAY + "T10:00:00+00:00")
    monkeypatch.setattr(paper, "load_a_share_trading_calendar", lambda *_: calendar())
    monkeypatch.setattr(paper, "_snapshot_daily_quotes", lambda *_: {})
    monkeypatch.setattr(paper.fill_journal, "validate_transaction", lambda *_: effect("validate"))
    def commit(*args, **kwargs):
        effect("commit")
        return {"state": {**kwargs["after"], "fixture_committed": True}}
    monkeypatch.setattr(paper.fill_journal, "commit_transaction", commit)
    def write(value):
        effect("state")
        captured["state"] = copy.deepcopy(value)
    monkeypatch.setattr(paper, "_auto_paper_state_store", lambda: SimpleNamespace(write=write))
    def publish(result):
        effect("publish")
        return {**result, "fixture_published": True}
    monkeypatch.setattr(paper, "_publish_snapshot_result", publish)
    result = paper._run_next_open_snapshot_update(mode="init", initial_cash=10000, quantity=100, strategy="all",
        decision_date=DAY, candidates=[], intake={}, dry_run=dry_run, force_reset=False,
        snapshot_root=root / "snapshots", journal_path=root / "fills.jsonl")
    return result, events, captured


@pytest.mark.parametrize("fail_at,expected", [
    (None, ["validate", "commit", "state", "publish"]),
    ("validate", ["validate"]), ("commit", ["validate", "commit"]),
    ("state", ["validate", "commit", "state"]),
    ("publish", ["validate", "commit", "state", "publish"]),
])
def test_snapshot_transaction_failure_cutoffs(monkeypatch, tmp_path, fail_at, expected):
    result, events, captured = _snapshot_scenario(monkeypatch, tmp_path, fail_at=fail_at)
    assert events == expected
    if fail_at:
        assert not result["ok"] and result["journal_may_require_recovery"]
        assert result["error"].startswith("snapshot_transaction_incomplete:")
    else:
        assert result["ok"] and captured["state"]["fixture_committed"]


def test_snapshot_preview_still_validates_without_commit(monkeypatch, tmp_path):
    result, events, captured = _snapshot_scenario(monkeypatch, tmp_path, dry_run=True)
    assert result["ok"] and events == ["validate"] and captured == {}


@pytest.mark.parametrize("recovered,dry_run,expected", [
    (False, False, ["publish"]), (True, False, ["state", "publish"]), (True, True, []),
])
def test_snapshot_replay_precedes_clock_and_never_recommits(monkeypatch, tmp_path, recovered, dry_run, expected):
    result, events, _ = _snapshot_scenario(monkeypatch, tmp_path, replay=True, recovered=recovered, dry_run=dry_run)
    assert result["ok"] and result["idempotent_replay"] and events == expected


@pytest.mark.parametrize("failure", ["state", "publish"])
def test_replay_failures_remain_in_recovery_exception_boundary(monkeypatch, tmp_path, failure):
    result, _, _ = _snapshot_scenario(monkeypatch, tmp_path, replay=True, recovered=True, fail_at=failure)
    assert not result["ok"] and result["error"].startswith("snapshot_journal_or_clock_invalid:")
    assert "journal_may_require_recovery" not in result


def test_exception_after_real_journal_append_recovers_without_duplicate_fill(monkeypatch, tmp_path):
    setup_book(tmp_path, monkeypatch)
    write_daily(tmp_path, "600000", {DAY: 10.0, NEXT: 10.2})
    assert run_day(tmp_path, monkeypatch, DAY, mode="init")["ok"]
    write_snapshot(tmp_path / "data/snapshots", rows=[source_row(open="10.10")])
    original = paper.fill_journal.commit_transaction
    def append_then_fail(*args, **kwargs):
        original(*args, **kwargs)
        raise OSError("synthetic interruption after durable journal append")
    before = paper.AUTO_PAPER_PORTFOLIO_STATE_JSON.read_bytes()
    monkeypatch.setattr(paper.fill_journal, "commit_transaction", append_then_fail)
    failed = run_day(tmp_path, monkeypatch, NEXT, close=10.2)
    assert not failed["ok"] and failed["journal_may_require_recovery"]
    assert paper.AUTO_PAPER_PORTFOLIO_STATE_JSON.read_bytes() == before
    journal_path = paper.AUTO_PAPER_PORTFOLIO_STATE_JSON.with_suffix(".fills.jsonl")
    journal_bytes = journal_path.read_bytes()
    monkeypatch.setattr(paper.fill_journal, "commit_transaction", _forbid)
    recovered = run_day(tmp_path, monkeypatch, NEXT, close=10.2)
    assert recovered["ok"] and recovered["idempotent_replay"]
    assert len(recovered["positions"]) == 1 and recovered["positions"][0]["quantity"] == 100
    assert journal_path.read_bytes() == journal_bytes
    assert json.loads(paper.AUTO_PAPER_PORTFOLIO_STATE_JSON.read_text(encoding="utf-8")) == paper.fill_journal.replay_journal(journal_path)["state"]
