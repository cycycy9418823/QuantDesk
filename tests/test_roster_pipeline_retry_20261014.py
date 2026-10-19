"""Existing evening invocation retries stale failed rosters once, with fake stages."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from scripts import run_data_close_pipeline as pipeline
from tests.test_pipeline_repair_20260911 import _project, _runners


DAY = "2026-10-13"


def _set_roster_date(root: Path, day: str) -> None:
    path = root / "data/universe/a_share_symbols.csv"
    path.write_text("symbol,name,source,updated_at\n" + "".join(
        f"{index:06d},Fixture{index},stock_info_a_code_name,{day}T09:00:00\n"
        for index in range(20)), encoding="utf-8")
    evidence = root / "results/universe_baseline_refresh/fixture/universe_baseline_refresh.json"
    evidence.write_text(json.dumps({"accepted": True, "after": {
        "sources": ["stock_info_a_code_name"], "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
    }}), encoding="utf-8")


@pytest.fixture
def roster_project(tmp_path, monkeypatch):
    root = _project(tmp_path)
    _set_roster_date(root, "2026-10-12")
    clock = [DAY + "T15:40:00+08:00"]
    monkeypatch.setattr(pipeline, "_now_iso", lambda: clock[0])
    return root, clock


def _run(root, run_id, calls, *, status="failed", runners=None, **kwargs):
    return pipeline.run_pipeline(root, network=True, target_date=DAY, run_id=run_id,
        trigger=kwargs.pop("trigger", "scheduled"),
        stage_runner=runners or _runners(calls, universe_status=status), **kwargs)


@pytest.mark.parametrize("evening_status", ["completed", "failed"])
def test_evening_retries_old_failed_roster_once_and_preserves_prior_receipts(roster_project, evening_status):
    root, clock = roster_project
    calls = []
    first = _run(root, "afternoon", calls)
    assert first["stages"]["universe"]["status"] == "completed_with_warnings"
    prior = pipeline.checkpoint_path(root, "afternoon")
    original_cp, original_manifest = prior.read_bytes(), prior.with_name("manifest.json").read_bytes()
    old_roster = (root / "data/universe/a_share_symbols.csv").read_bytes()
    clock[0] = DAY + "T18:30:00+08:00"
    calls.clear()
    result = _run(root, "evening", calls, status=evening_status)
    assert calls == ["universe"]
    assert result["executed_stages"] == ["universe"]
    assert result["universe_evening_retry"]["source_run_id"] == "afternoon"
    assert (root / "data/universe/a_share_symbols.csv").read_bytes() == old_roster
    assert prior.read_bytes() == original_cp
    assert prior.with_name("manifest.json").read_bytes() == original_manifest
    clock[0] = DAY + "T18:40:00+08:00"
    calls.clear()
    _run(root, "after-evening", calls)
    assert calls == []


@pytest.mark.parametrize("moment", [DAY + "T18:29:59+08:00", "2026-10-14T18:30:00+08:00"])
def test_retry_is_only_same_day_at_existing_evening_opportunity(roster_project, moment):
    root, clock = roster_project
    calls = []
    _run(root, "afternoon", calls)
    clock[0] = moment
    calls.clear()
    _run(root, "outside-window", calls)
    assert calls == []


def test_manual_explicit_reuse_does_not_spend_evening_retry(roster_project):
    root, clock = roster_project
    calls = []
    _run(root, "afternoon", calls)
    clock[0] = DAY + "T18:30:00+08:00"
    calls.clear()
    _run(root, "manual-reuse", calls, trigger="manual", resume_run_id="afternoon")
    assert calls == []
    _run(root, "evening", calls)
    assert calls == ["universe"]


@pytest.mark.parametrize("current_roster,unrelated_warning", [(True, False), (False, True)])
def test_fresh_roster_or_unrelated_warning_does_not_trigger_retry(roster_project, current_roster, unrelated_warning):
    root, clock = roster_project
    if current_roster:
        _set_roster_date(root, DAY)
    calls = []
    _run(root, "afternoon", calls, status="completed_with_warnings" if unrelated_warning else "failed")
    clock[0] = DAY + "T18:30:00+08:00"
    calls.clear()
    _run(root, "evening", calls)
    assert calls == []


def test_retry_attempt_is_durable_before_stage_and_not_replayed_after_crash(roster_project):
    root, clock = roster_project
    calls = []
    _run(root, "afternoon", calls)
    clock[0] = DAY + "T18:30:00+08:00"
    calls.clear()
    runners = _runners(calls)

    def crash(*_args, **_kwargs):
        receipts = [json.loads(path.read_text(encoding="utf-8"))
                    for path in (root / pipeline.ATTEMPTS_DIR_RELATIVE).glob("*/manifest.json")]
        assert any(item.get("universe_evening_retry", {}).get("consumed") is True for item in receipts)
        calls.append("universe")
        raise RuntimeError("fixture_stopped_after_retry_started")

    runners["universe"] = crash
    with pytest.raises(RuntimeError, match="fixture_stopped"):
        _run(root, "crashed-retry", calls, runners=runners)
    assert calls == ["universe"]
    calls.clear()
    clock[0] = DAY + "T18:40:00+08:00"
    _run(root, "later-invocation", calls)
    assert calls == []


def test_changed_roster_rechecks_dependents_without_resending_notification(roster_project):
    root, clock = roster_project
    calls = []
    outputs = {"market_refresh": "results/data_refresh/data_refresh_latest.json",
        "research_chain": "results/data_chain/daily_data_chain_latest.json",
        "reconcile": "results/data_chain/full_a_daily_reconciliation_latest.json"}

    def runners(refresh_roster):
        result = _runners(calls, universe_status="completed" if refresh_roster else "failed")
        for name in pipeline.STAGE_ORDER:
            original = result[name]

            def run(*args, _name=name, _original=original, **kwargs):
                if _name == "universe" and refresh_roster:
                    _set_roster_date(root, DAY)
                if _name in outputs:
                    path = root / outputs[_name]
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_text(json.dumps({"run": kwargs["pipeline_run_id"]}), encoding="utf-8")
                if _name == "health_alert" and refresh_roster:
                    assert kwargs["push"] is False
                    assert kwargs["alert_push"] is False
                    assert kwargs["alert_network"] is False
                return _original(*args, **kwargs)

            result[name] = run
        return result

    _run(root, "afternoon", calls, runners=runners(False), alert_push=True, alert_network=True)
    clock[0] = DAY + "T18:30:00+08:00"
    calls.clear()
    result = _run(root, "evening", calls, runners=runners(True), alert_push=True, alert_network=True)
    assert calls == list(pipeline.STAGE_ORDER)
    assert result["alert_policy"] == "preview_only_on_roster_retry"
    assert result["stages"]["universe"]["status"] == "completed"


def test_invalid_existing_proof_is_not_used_as_retry_authority(roster_project):
    root, clock = roster_project
    calls = []
    _run(root, "afternoon", calls)
    checkpoint = pipeline.checkpoint_path(root, "afternoon")
    doc = json.loads(checkpoint.read_text(encoding="utf-8"))
    doc["stages"]["universe"]["stage_version"] = "invalid"
    checkpoint.write_text(json.dumps(doc), encoding="utf-8")
    clock[0] = DAY + "T18:30:00+08:00"
    calls.clear()
    result = _run(root, "evening", calls)
    assert result["status"] == "failed"
    assert calls == []


def test_consumed_retry_covers_terminal_failed_resume_and_local_health(roster_project):
    root, clock = roster_project
    calls = []
    _run(root, "afternoon", calls)
    clock[0] = DAY + "T18:30:00+08:00"
    calls.clear()
    runners = _runners(calls, universe_status="failed")
    original = runners["universe"]

    def invalidates_accepted_proof(*args, **kwargs):
        (root / "results/universe_baseline_refresh/fixture/universe_baseline_refresh.json").unlink()
        return original(*args, **kwargs)

    runners["universe"] = invalidates_accepted_proof
    failed = _run(root, "failed-evening", calls, runners=runners)
    assert failed["stages"]["universe"]["status"] == "blocked"
    assert calls == ["universe"]
    # A subsequent read-side report change invalidates the old health input.
    # Ordinary incomplete recovery must still retain the no-push restriction.
    health_input = root / "results/data_chain/daily_data_chain_latest.json"
    health_input.parent.mkdir(parents=True, exist_ok=True)
    health_input.write_text('{"status":"fixture_changed"}', encoding="utf-8")
    clock[0] = DAY + "T18:40:00+08:00"
    calls.clear()
    later_runners = _runners(calls, universe_status="failed")
    health = later_runners["health_alert"]

    def local_health(*args, **kwargs):
        assert kwargs["push"] is False
        assert kwargs["alert_network"] is False
        assert kwargs["alert_push"] is False
        return health(*args, **kwargs)

    later_runners["health_alert"] = local_health
    result = _run(root, "later-incomplete", calls, runners=later_runners, alert_push=True, alert_network=True)
    assert "universe" not in calls
    assert "health_alert" in calls
    assert result["universe_evening_retry_previously_consumed"] is True
    assert "universe_evening_retry_already_consumed" in result["stages"]["universe"]["reason_codes"]
