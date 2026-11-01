from __future__ import annotations

import functools
import json
import multiprocessing
import os
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from quantdesk import daily_data_chain as chain
from quantdesk.process_deadline import ProcessDeadlineResult
from scripts import run_daily_review_orchestrator as review
from scripts import run_data_close_pipeline as pipeline
from tests.test_daily_data_chain_universe_refresh import _install_minimal_chain_fakes
from tests.test_daily_data_chain_upstream_reuse_preflight import _valid_upstream_reuse


def _forbid_unbounded_market(monkeypatch):
    monkeypatch.setitem(sys.modules, "scripts.server.helpers.market", SimpleNamespace(
        refresh_industry_board_cache=lambda: pytest.fail("unbounded provider ran in the parent process"),
    ))


@pytest.mark.parametrize("batch,reason", [
    (ProcessDeadlineResult({}, {}, [0], True, [123]), "sector_refresh_timeout"),
    (ProcessDeadlineResult({}, {0: "fixture provider failed"}, [], False, [123]), "sector_refresh_worker_failed"),
    (ProcessDeadlineResult({}, {}, [0], False, [123]), "sector_refresh_worker_failed"),
])
def test_sector_refresh_has_one_finite_owned_process_budget(monkeypatch, batch, reason):
    _forbid_unbounded_market(monkeypatch)
    calls = []

    def bounded(worker, tasks, **kwargs):
        calls.append((worker, tasks, kwargs))
        return batch

    monkeypatch.setattr(chain, "run_process_tasks_until_deadline", bounded, raising=False)
    result = chain._refresh_sector_snapshot(chain._CANONICAL_PROJECT_ROOT, network=True)
    assert result["ok"] is False and result["status"] == "blocked"
    assert reason in result["reason_codes"]
    assert result["worker_exit_confirmed"] is True
    assert len(calls) == 1 and len(calls[0][1]) == 1
    assert calls[0][2]["max_workers"] == 1
    assert calls[0][2]["timeout_seconds"] == 1200.0
    assert not result.get("evidence_eligible")


def test_completed_worker_preserves_membership_evidence(monkeypatch):
    _forbid_unbounded_market(monkeypatch)
    observation = {"ok": False, "status": "degraded", "row_count": 0, "errors": ["fixture_missing"]}
    payload = {"ok": True, "status": "completed", "source": "eastmoney", "membership_observation": observation}
    monkeypatch.setattr(chain, "run_process_tasks_until_deadline", lambda *a, **k:
        ProcessDeadlineResult({0: payload}, {}, [], False, [123]), raising=False)
    result = chain._refresh_sector_snapshot(chain._CANONICAL_PROJECT_ROOT, network=True)
    assert result["membership_observation"] == observation
    assert result["worker_exit_confirmed"] is True


def test_unconfirmed_worker_exit_cannot_degrade_into_continued_writes(monkeypatch):
    _forbid_unbounded_market(monkeypatch)

    def stuck(*args, **kwargs):
        raise RuntimeError("process_deadline_workers_still_alive:123")

    monkeypatch.setattr(chain, "run_process_tasks_until_deadline", stuck, raising=False)
    with pytest.raises(RuntimeError, match="workers_still_alive"):
        chain._refresh_sector_snapshot(chain._CANONICAL_PROJECT_ROOT, network=True)


@pytest.mark.parametrize("budget", [0.0, -1.0, float("inf"), float("nan")])
def test_sector_budget_cannot_be_disabled(monkeypatch, budget):
    _forbid_unbounded_market(monkeypatch)
    with pytest.raises(ValueError, match="sector_refresh_timeout_invalid"):
        chain._refresh_sector_snapshot(chain._CANONICAL_PROJECT_ROOT, network=True, timeout_seconds=budget)


def test_custom_root_and_offline_never_spawn_default_provider(tmp_path, monkeypatch):
    _forbid_unbounded_market(monkeypatch)
    monkeypatch.setattr(chain, "run_process_tasks_until_deadline",
                        lambda *a, **k: pytest.fail("provider must not start"), raising=False)
    assert chain._refresh_sector_snapshot(tmp_path, network=False)["status"] == "skipped"
    assert chain._refresh_sector_snapshot(tmp_path, network=True)["reason_codes"] == [
        "sector_refresh_custom_root_not_supported"
    ]


def _slow_sector_fixture(task, context, *, started: str, late: str):
    Path(started).write_text("started", encoding="utf-8")
    time.sleep(30)
    Path(late).write_text("late provider write must never happen", encoding="utf-8")
    return {"ok": True, "status": "completed"}


def test_real_sector_worker_is_stopped_before_parent_continues(tmp_path, monkeypatch):
    _forbid_unbounded_market(monkeypatch)
    started, late = tmp_path / "started", tmp_path / "late"
    monkeypatch.setattr(chain, "_sector_snapshot_worker", functools.partial(
        _slow_sector_fixture, started=str(started), late=str(late),
    ), raising=False)
    result = chain._refresh_sector_snapshot(chain._CANONICAL_PROJECT_ROOT, network=True, timeout_seconds=8.0)
    assert started.is_file(), "exercise an active provider, not only interpreter startup"
    assert result["status"] == "blocked" and result["worker_exit_confirmed"] is True
    assert not late.exists()
    assert not (set(result["worker_pids"]) & {p.pid for p in multiprocessing.active_children()})


def test_optional_timeout_leaves_progress_and_does_not_block_core(tmp_path, monkeypatch):
    _install_minimal_chain_fakes(monkeypatch, ["600000"])
    reuse = _valid_upstream_reuse(tmp_path)
    run_id = "sector-timeout-core-continues"
    progress = tmp_path / "results/data_chain/runs" / run_id / "progress.json"
    seen = []

    def unavailable(root, *, network):
        assert network is True
        record = json.loads(progress.read_text(encoding="utf-8"))
        assert record["run_id"] == run_id and record["target_date"] == "2026-08-14"
        assert record["phase"] == "sector_refresh" and record["status"] == "running"
        assert record["authoritative"] is False and record["published_latest"] is False
        seen.append(record)
        return {"ok": False, "status": "blocked", "network": True,
                "reason_codes": ["sector_refresh_timeout"], "worker_exit_confirmed": True}

    monkeypatch.setattr(chain, "_refresh_sector_snapshot", unavailable)
    result = chain.run_daily_data_chain(
        tmp_path, run_id=run_id, target_date="2026-08-14", network=True,
        rebuild_features=False, retry_from_latest=True, upstream_reuse=reuse,
        pipeline_run_id="pipeline-current",
    )
    assert seen and result["status"] == "completed"
    assert "sector_refresh_blocked" in result["warnings"]
    final = json.loads(progress.read_text(encoding="utf-8"))
    assert final["status"] == "completed" and final["finished_at"]
    assert final["pipeline_run_id"] == "pipeline-current"


def test_interruption_keeps_phase_without_publishing_running_as_latest(tmp_path, monkeypatch):
    _install_minimal_chain_fakes(monkeypatch, ["600000"])
    reuse = _valid_upstream_reuse(tmp_path)
    run_id = "interrupted-sector-observation"
    latest = tmp_path / "results/data_chain/daily_data_chain_latest.json"
    latest.parent.mkdir(parents=True, exist_ok=True)
    original = b'{"run_id":"previous","status":"completed","target_date":"2026-08-13"}'
    latest.write_bytes(original)

    def interrupt(*args, **kwargs):
        raise KeyboardInterrupt("fixture interruption")

    monkeypatch.setattr(chain, "_refresh_sector_snapshot", interrupt)
    with pytest.raises(KeyboardInterrupt):
        chain.run_daily_data_chain(
            tmp_path, run_id=run_id, target_date="2026-08-14", network=True,
            rebuild_features=False, retry_from_latest=True, upstream_reuse=reuse,
        )
    record = json.loads((tmp_path / "results/data_chain/runs" / run_id / "progress.json").read_text(encoding="utf-8"))
    assert record["status"] == "running" and record["phase"] == "sector_refresh"
    assert not record.get("finished_at")
    assert latest.read_bytes() == original
    observed = chain.latest_daily_data_chain(tmp_path)
    assert observed["status"] == "abandoned" and observed["phase"] == "sector_refresh"


def _interrupted_progress(root: Path, *, pipeline_run: str = "current-pipeline") -> Path:
    chain._write_chain_progress(root, {
        "run_id": "current-chain", "pipeline_run_id": pipeline_run,
        "target_date": "2026-10-30", "status": "running",
    }, "features")
    metadata = root / "state/daily_data_chain.lock.json"
    metadata.parent.mkdir(parents=True, exist_ok=True)
    metadata.write_text(json.dumps({
        "run_id": "current-chain", "target_date": "2026-10-30",
        "status": "released", "pid": os.getpid(),
    }), encoding="utf-8")
    return root / "results/data_chain/runs/current-chain/progress.json"


def test_pipeline_timeout_retains_phase_but_never_promotes_progress(tmp_path, monkeypatch):
    _interrupted_progress(tmp_path)
    monkeypatch.setattr(pipeline, "_run_owned_subprocess", lambda *a, **k:
        ("failed", ["stage_timeout:research_chain"], {"timeout": True, "child_pid": os.getpid()}))
    result = pipeline.stage_research_chain(
        tmp_path, network=True, timeout=10, target="2026-10-30", pipeline_run_id="current-pipeline",
    )
    assert result.status == "failed"
    assert result.payload["progress_evidence"]["phase"] == "features"
    assert result.payload["progress_evidence"]["authoritative"] is False
    assert "stdout_json" not in result.payload


@pytest.mark.parametrize("field,value", [
    ("pipeline_run_id", "older-pipeline"), ("target_date", "2026-10-29"),
    ("pid", 123456), ("run_id", "older-chain"), ("schema_version", "old"),
])
def test_pipeline_rejects_progress_from_another_process_or_run(tmp_path, monkeypatch, field, value):
    path = _interrupted_progress(tmp_path)
    doc = json.loads(path.read_text(encoding="utf-8"))
    doc[field] = value
    path.write_text(json.dumps(doc), encoding="utf-8")
    monkeypatch.setattr(pipeline, "_run_owned_subprocess", lambda *a, **k:
        ("failed", ["stage_timeout:research_chain"], {"timeout": True, "child_pid": os.getpid()}))
    result = pipeline.stage_research_chain(
        tmp_path, network=True, timeout=10, target="2026-10-30", pipeline_run_id="current-pipeline",
    )
    assert result.status == "failed" and "progress_evidence" not in result.payload


def test_released_lock_with_unfinished_progress_remains_awaiting_data(tmp_path):
    _interrupted_progress(tmp_path)
    latest = tmp_path / "results/data_chain/daily_data_chain_latest.json"
    latest.write_text(json.dumps({
        "authoritative": True, "run_id": "earlier-success", "target_date": "2026-10-30",
        "status": "completed",
    }), encoding="utf-8")
    original = latest.read_bytes()
    with pytest.raises(review.DailyChainWaitTimeout) as caught:
        with review.locked_authoritative_chain_event(
            project_root=tmp_path, manifest_path=latest,
            expected_target_date="2026-10-30", timeout_seconds=0,
        ):
            pytest.fail("unfinished retry must not authorize another review of the old event")
    assert caught.value.reason_code == "daily_data_chain_producer_abandoned"
    assert caught.value.producer_progress["phase"] == "features"
    assert latest.read_bytes() == original


def test_review_wait_can_recover_after_controlled_interruption(tmp_path, monkeypatch):
    _interrupted_progress(tmp_path)
    latest = tmp_path / "results/data_chain/daily_data_chain_latest.json"
    latest.write_text(json.dumps({
        "authoritative": True, "run_id": "old", "target_date": "2026-10-29", "status": "completed",
    }), encoding="utf-8")
    slept = []

    def recover(_seconds):
        slept.append(True)
        latest.write_text(json.dumps({
            "authoritative": True, "run_id": "recovered", "target_date": "2026-10-30", "status": "completed",
        }), encoding="utf-8")
        (tmp_path / "state/daily_data_chain.lock.json").write_text(json.dumps({
            "run_id": "recovered", "status": "released", "target_date": "2026-10-30",
        }), encoding="utf-8")

    monkeypatch.setattr(review.time, "sleep", recover)
    with review.locked_authoritative_chain_event(
        project_root=tmp_path, manifest_path=latest, expected_target_date="2026-10-30",
        timeout_seconds=5, poll_seconds=0.01,
    ) as selected:
        assert slept == [True]
        assert selected["run_id"] == "recovered"


def test_matching_terminal_remains_authority_even_if_progress_write_was_interrupted(tmp_path):
    _interrupted_progress(tmp_path)
    latest = tmp_path / "results/data_chain/daily_data_chain_latest.json"
    latest.write_text(json.dumps({
        "authoritative": True, "run_id": "current-chain", "target_date": "2026-10-30", "status": "completed",
    }), encoding="utf-8")
    with review.locked_authoritative_chain_event(
        project_root=tmp_path, manifest_path=latest, expected_target_date="2026-10-30", timeout_seconds=0,
    ) as selected:
        assert selected["run_id"] == "current-chain"


def test_progress_reader_is_bounded_and_cannot_follow_an_aliased_run(tmp_path):
    path = _interrupted_progress(tmp_path)
    path.write_bytes(b" " * 65537)
    assert chain._read_chain_progress(tmp_path, "current-chain") == {}
    outside = tmp_path / "protected"
    outside.mkdir()
    alias = tmp_path / "results/data_chain/runs/alias"
    try:
        alias.symlink_to(outside, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"directory symlink unavailable: {exc}")
    with pytest.raises(ValueError, match="chain_progress_path_escape"):
        chain._write_chain_progress(tmp_path, {"run_id": "alias", "target_date": "2026-10-30"}, "features")
    assert not list(outside.iterdir())


def test_sector_worker_owns_descendants_before_calling_provider(monkeypatch):
    calls = []
    monkeypatch.setattr(pipeline, "_protect_child_process_tree", lambda: calls.append("protect"))
    monkeypatch.setattr(chain, "_refresh_sector_snapshot_inline", lambda root, **kwargs:
                        calls.append("provider") or {"ok": True, "status": "completed"})
    result = chain._sector_snapshot_worker(str(chain._CANONICAL_PROJECT_ROOT), {})
    assert result["status"] == "completed"
    assert calls == (["protect", "provider"] if os.name == "nt" else ["provider"])


@pytest.mark.parametrize("field,value", [
    ("authoritative", True), ("published_latest", True), ("completion_scope", "business_completed"),
    ("phase", {}), ("pid", True), ("target_date", "2026-02-30"),
])
def test_corrupt_progress_cannot_create_a_completion_claim(tmp_path, field, value):
    path = _interrupted_progress(tmp_path)
    doc = json.loads(path.read_text(encoding="utf-8"))
    doc[field] = value
    path.write_text(json.dumps(doc), encoding="utf-8")
    assert chain._read_chain_progress(tmp_path, "current-chain") == {}
