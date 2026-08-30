from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

import quantdesk.paper.lifecycle as lifecycle
from quantdesk.paper.lifecycle import (
    CandidateDecisionEvent,
    LifecycleConflictError,
    LifecycleEventStore,
    LifecycleStoreCorruptError,
    LifecycleValidationError,
    evaluate_candidate_eligibility,
)
from quantdesk.research import paper_watchlist
from quantdesk.research.daily_watchpool_step import build_daily_watchpool_step


FORMAL_EVIDENCE = {
    "eligible": True,
    "evidence_kind": "formal",
    "coverage": 1.0,
    "minimum_coverage": 1.0,
    "pit_eligible": True,
    "price_contract_eligible": True,
}


def test_lifecycle_module_exposes_required_api() -> None:
    assert lifecycle is not None, "quantdesk.paper.lifecycle must exist"
    assert CandidateDecisionEvent is not None
    assert LifecycleEventStore is not None
    assert callable(evaluate_candidate_eligibility)


def _event(
    event_id: str,
    *,
    occurred_at: str = "2026-07-10T10:00:00+08:00",
    requested_state: str = "paper_watchlist",
    decision_origin: str = "system",
    evidence: dict | None = None,
    payload: dict | None = None,
    source_event_ids: tuple[str, ...] = (),
    gate_evidence: dict | None = None,
) -> CandidateDecisionEvent:
    return CandidateDecisionEvent(
        event_id=event_id,
        symbol="600000",
        occurred_at=occurred_at,
        requested_state=requested_state,
        decision_origin=decision_origin,
        evidence=FORMAL_EVIDENCE if evidence is None else evidence,
        payload={"name": "浦发银行"} if payload is None else payload,
        source_event_ids=source_event_ids,
        gate_evidence={} if gate_evidence is None else gate_evidence,
    )


def test_candidate_event_has_stable_content_digest_without_mutating_input() -> None:
    evidence = copy.deepcopy(FORMAL_EVIDENCE)
    payload = {"name": "浦发银行", "rationale": ["formal evidence"]}
    original_evidence = copy.deepcopy(evidence)
    original_payload = copy.deepcopy(payload)

    first = _event("evt-stable", evidence=evidence, payload=payload)
    second = _event("evt-stable", evidence=copy.deepcopy(evidence), payload=copy.deepcopy(payload))

    assert first.to_dict() == second.to_dict()
    assert first.content_digest == second.content_digest
    assert evidence == original_evidence
    assert payload == original_payload


def test_store_append_is_idempotent_and_conflicting_event_id_fails_closed(tmp_path: Path) -> None:
    store = LifecycleEventStore(tmp_path / "lifecycle.json")
    event = _event("evt-idempotent")

    first = store.append(event)
    duplicate = store.append(event)

    assert first["appended"] is True
    assert duplicate == {"appended": False, "status": "duplicate_noop", "event_id": "evt-idempotent"}
    assert len(store.read()["events"]) == 1

    with pytest.raises(LifecycleConflictError, match="evt-idempotent"):
        store.append(_event("evt-idempotent", payload={"name": "changed"}))


def test_as_of_replay_only_sees_events_available_at_cutoff(tmp_path: Path) -> None:
    store = LifecycleEventStore(tmp_path / "lifecycle.json")
    store.append(_event("evt-early", occurred_at="2026-07-10T10:00:00+08:00"))
    store.append(
        _event(
            "evt-late",
            occurred_at="2026-07-11T10:00:00+08:00",
            requested_state="excluded_pending_research",
        )
    )

    snapshot = store.replay(as_of="2026-07-10T23:59:59+08:00")

    assert [row["event_id"] for row in snapshot["events"]] == ["evt-early"]
    assert snapshot["by_symbol"]["600000"]["effective_state"] == "paper_watchlist"
    assert snapshot["paper_watchlist"] == ["600000"]


@pytest.mark.parametrize(
    ("evidence", "reason"),
    [
        ({**FORMAL_EVIDENCE, "eligible": False}, "evidence_not_eligible"),
        ({**FORMAL_EVIDENCE, "evidence_kind": "proxy"}, "proxy_or_non_formal_evidence"),
        ({**FORMAL_EVIDENCE, "coverage": 0.5}, "evidence_coverage_below_minimum"),
        ({**FORMAL_EVIDENCE, "pit_eligible": False}, "pit_evidence_not_eligible"),
        ({**FORMAL_EVIDENCE, "price_contract_eligible": False}, "price_contract_not_eligible"),
    ],
)
def test_ineligible_evidence_projects_to_excluded_pending_research(
    evidence: dict,
    reason: str,
    tmp_path: Path,
) -> None:
    decision = evaluate_candidate_eligibility(evidence)
    assert decision["eligible"] is False
    assert reason in decision["reason_codes"]

    store = LifecycleEventStore(tmp_path / f"{reason}.json")
    stored = store.append(_event(f"evt-{reason}", evidence=evidence))["event"]
    assert stored["effective_state"] == "excluded_pending_research"
    assert reason in stored["eligibility_reason_codes"]


def test_manual_addition_is_unfunded_manual_observation(tmp_path: Path) -> None:
    store = LifecycleEventStore(tmp_path / "lifecycle.json")
    stored = store.append(
        _event(
            "evt-manual",
            decision_origin="manual",
            evidence={},
            payload={"name": "浦发银行", "note": "manual local observation"},
        )
    )["event"]

    assert stored["effective_state"] == "manual_observation"
    for forbidden in ("cash", "quantity", "funded", "order_intent", "fill_price"):
        assert forbidden not in stored
        assert forbidden not in stored["payload"]


@pytest.mark.parametrize("forbidden", ["cash", "quantity", "funded", "order_intent", "fill_price"])
def test_watch_and_manual_events_reject_money_or_order_fields(forbidden: str, tmp_path: Path) -> None:
    store = LifecycleEventStore(tmp_path / f"{forbidden}.json")
    with pytest.raises(LifecycleValidationError, match=forbidden):
        store.append(_event(f"evt-{forbidden}", payload={forbidden: 100}))


def test_funded_position_requires_allocator_risk_and_simulation_kernel_evidence(tmp_path: Path) -> None:
    store = LifecycleEventStore(tmp_path / "lifecycle.json")

    with pytest.raises(LifecycleValidationError, match="allocator_gate_missing"):
        store.append(_event("evt-funded-blocked", requested_state="paper_position"))

    source_ids = ("allocator-1", "risk-1", "kernel-1")
    gate_evidence = {
        "allocator": {"passed": True, "event_id": "allocator-1"},
        "risk": {"passed": True, "event_id": "risk-1"},
        "simulation_kernel": {"passed": True, "event_id": "kernel-1"},
    }
    accepted = store.append(
        _event(
            "evt-funded-accepted",
            requested_state="paper_position",
            source_event_ids=source_ids,
            gate_evidence=gate_evidence,
        )
    )["event"]
    assert accepted["effective_state"] == "paper_position"
    assert accepted["funded"] is True


def test_funded_gate_source_ids_must_match_declared_gate_events(tmp_path: Path) -> None:
    store = LifecycleEventStore(tmp_path / "lifecycle.json")
    gate_evidence = {
        "allocator": {"passed": True, "event_id": "allocator-1"},
        "risk": {"passed": True, "event_id": "risk-1"},
        "simulation_kernel": {"passed": True, "event_id": "kernel-1"},
    }

    with pytest.raises(LifecycleValidationError, match="simulation_kernel_source_event_missing"):
        store.append(
            _event(
                "evt-funded-source-mismatch",
                requested_state="paper_position",
                source_event_ids=("allocator-1", "risk-1"),
                gate_evidence=gate_evidence,
            )
        )


def test_corrupt_primary_recovers_from_backup_and_exposes_warning(tmp_path: Path) -> None:
    path = tmp_path / "lifecycle.json"
    store = LifecycleEventStore(path)
    store.append(_event("evt-recover"))
    path.write_text("{bad json", encoding="utf-8")

    recovered = store.read()

    assert [row["event_id"] for row in recovered["events"]] == ["evt-recover"]
    assert recovered["state_recovered_from_backup"] is True
    assert "lifecycle_log_recovered_from_backup" in recovered["warnings"]


def test_missing_primary_recovers_existing_append_only_history_from_backup(tmp_path: Path) -> None:
    path = tmp_path / "lifecycle.json"
    store = LifecycleEventStore(path)
    store.append(_event("evt-missing-primary"))
    path.unlink()

    recovered = store.read()

    assert [row["event_id"] for row in recovered["events"]] == ["evt-missing-primary"]
    assert recovered["state_recovered_from_backup"] is True
    assert "lifecycle_log_recovered_from_backup" in recovered["warnings"]


def test_corrupt_primary_and_all_backups_fail_closed(tmp_path: Path) -> None:
    path = tmp_path / "lifecycle.json"
    store = LifecycleEventStore(path)
    store.append(_event("evt-corrupt"))
    path.write_text("{bad json", encoding="utf-8")
    for backup in path.parent.glob("lifecycle.backup_*.json"):
        backup.write_text("{also bad", encoding="utf-8")

    with pytest.raises(LifecycleStoreCorruptError, match="no valid backup"):
        store.read()


def test_replay_is_deterministic_and_does_not_mutate_stored_events(tmp_path: Path) -> None:
    store = LifecycleEventStore(tmp_path / "lifecycle.json")
    store.append(_event("evt-deterministic"))
    before = json.loads((tmp_path / "lifecycle.json").read_text(encoding="utf-8"))

    first = store.replay(as_of="2026-07-10T23:59:59+08:00")
    second = store.replay(as_of="2026-07-10T23:59:59+08:00")
    after = json.loads((tmp_path / "lifecycle.json").read_text(encoding="utf-8"))

    assert first == second
    assert before == after


def test_watchlist_shadow_lifecycle_preserves_legacy_projection_golden(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(paper_watchlist, "decision_id", lambda: "dj_fixed")
    row = {"symbol": "600000", "name": "浦发银行", "strategy": "observe", "close": 10.0}

    legacy_path = tmp_path / "legacy_watchlist.json"
    shadow_path = tmp_path / "shadow_watchlist.json"
    legacy = paper_watchlist.add_watchlist_items([row], legacy_path)
    lifecycle_store = LifecycleEventStore(tmp_path / "lifecycle.json")
    shadow = paper_watchlist.add_watchlist_items(
        [row],
        shadow_path,
        lifecycle_store=lifecycle_store,
        manual=True,
    )

    assert shadow == legacy
    assert json.loads(shadow_path.read_text(encoding="utf-8")) == json.loads(
        legacy_path.read_text(encoding="utf-8")
    )
    event = lifecycle_store.read()["events"][0]
    assert event["effective_state"] == "manual_observation"
    assert event["requested_state"] == "paper_watchlist"


def test_daily_watchpool_can_shadow_write_automatic_lifecycle_event(tmp_path: Path) -> None:
    lifecycle_store = LifecycleEventStore(tmp_path / "lifecycle.json")
    result = build_daily_watchpool_step(
        [
            {
                "symbol": "600000",
                "name": "浦发银行",
                "long_score": 90,
                "close": 10.0,
                "eligible": True,
                "evidence_kind": "formal",
                "coverage": 1.0,
                "minimum_coverage": 1.0,
                "pit_eligible": True,
                "price_contract_eligible": True,
            }
        ],
        watchlist_path=tmp_path / "paper_watchlist.json",
        lifecycle_store=lifecycle_store,
        top=1,
    )

    assert result["added_count"] == 1
    event = lifecycle_store.read()["events"][0]
    assert event["decision_origin"] == "system"
    assert event["effective_state"] == "paper_watchlist"
    assert "funded" not in event


def test_production_watchlist_path_shadow_writes_lifecycle_by_default(tmp_path: Path) -> None:
    watchlist_path = tmp_path / "paper_watchlist.json"

    paper_watchlist.add_watchlist_items(
        [{"symbol": "600000", "name": "sample"}],
        watchlist_path,
        manual=True,
    )

    lifecycle_path = tmp_path / "paper_watchlist_lifecycle.json"
    assert lifecycle_path.exists()
    event = LifecycleEventStore(lifecycle_path).read()["events"][0]
    assert event["effective_state"] == "manual_observation"
    assert "funded" not in event


def test_automatic_ineligible_candidate_is_excluded_in_compatibility_projection(
    tmp_path: Path,
) -> None:
    watchlist_path = tmp_path / "paper_watchlist.json"

    result = paper_watchlist.add_watchlist_items(
        [{"symbol": "600000", "name": "sample", "evidence_kind": "proxy"}],
        watchlist_path,
        manual=False,
    )

    assert result["items"][0]["review_status"] == "excluded_pending_research"
    assert result["items"][0]["lifecycle_effective_state"] == "excluded_pending_research"
    assert "proxy_or_non_formal_evidence" in result["items"][0]["eligibility_reason_codes"]
    lifecycle_path = tmp_path / "paper_watchlist_lifecycle.json"
    event = LifecycleEventStore(lifecycle_path).read()["events"][0]
    assert event["effective_state"] == "excluded_pending_research"


def test_legacy_watchlist_projection_does_not_gain_null_schema_field(tmp_path: Path) -> None:
    path = tmp_path / "paper_watchlist.json"

    paper_watchlist.init_paper_watchlist(path)

    on_disk = json.loads(path.read_text(encoding="utf-8"))
    assert "schema_version" not in on_disk
