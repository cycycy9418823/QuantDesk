"""Tests for the unified update-gate facade (Phase 0.5 闸门合并).

The facade consolidates the previously-scattered gate decisions
(app_open_refresh_gate / work_start_update_gate) plus the bounded updater into
a single decision object + a single guarded runner, without ripping out the
underlying gates.
"""
from __future__ import annotations

from quantdesk.data_providers import update_gate_facade as g


def test_decision_is_dataclass_with_expected_fields() -> None:
    import dataclasses

    assert dataclasses.is_dataclass(g.GateDecision)
    fields = {f.name for f in dataclasses.fields(g.GateDecision)}
    assert {"should_update", "reasons", "gate_results", "blocked_by"} <= fields


def test_no_gate_votes_defaults_to_skip() -> None:
    # With both gates disabled there is no positive vote -> conservative skip.
    decision = g.evaluate_update_gate(check_app_open=False, check_work_start=False)
    assert decision.should_update is False
    assert "no_gate_vote_default_skip" in decision.reasons


def test_as_dict_is_serializable() -> None:
    import json

    decision = g.evaluate_update_gate(check_app_open=False, check_work_start=False)
    payload = decision.as_dict()
    # must round-trip through JSON (no datetime / Path leakage)
    json.loads(json.dumps(payload))
    assert set(payload.keys()) >= {"should_update", "reasons", "gate_results", "blocked_by"}


def test_maybe_run_respects_skip_decision() -> None:
    decision = g.evaluate_update_gate(check_app_open=False, check_work_start=False)
    result = g.maybe_run_bounded_update(decision=decision, network=False)
    assert result["ran"] is False
    assert result.get("reason") == "update_gate_skip"


def test_force_overrides_skip_but_stays_offline() -> None:
    decision = g.evaluate_update_gate(check_app_open=False, check_work_start=False)
    # force=True should attempt the bounded update even when the gate says skip;
    # network=False keeps it offline (no real fetch).
    result = g.maybe_run_bounded_update(
        decision=decision, network=False, force=True, symbols=[], max_symbols=0
    )
    assert result["ran"] is True
