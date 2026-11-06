"""Scheduled unfinished runs select compatible evidence; no live I/O."""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest

from scripts import run_data_close_pipeline as pipeline

TARGET = "2026-11-02"


def runners(calls: list[str], *, unfinished: bool = False) -> dict[str, Any]:
    def make(name: str):
        def run(*_args, **_kwargs):
            calls.append(name)
            return pipeline.StageResult(stage=name,
                status="failed" if unfinished and name == "research_chain" else "completed",
                started_at="2026-11-02T15:40:00+08:00",
                completed_at="2026-11-02T15:41:00+08:00",
                payload={"synthetic_test_fixture": True})
        return run
    return {name: make(name) for name in pipeline.STAGE_ORDER}


def run(root: Path, run_id: str, calls: list[str], *, unfinished: bool = False, **kwargs) -> dict:
    return pipeline.run_pipeline(root, network=True, run_id=run_id,
        target_date=kwargs.pop("target_date", TARGET),
        stage_runner=runners(calls, unfinished=unfinished), **kwargs)


def original_files(root: Path, run_id: str) -> dict[Path, bytes]:
    folder = pipeline.checkpoint_path(root, run_id).parent
    return {folder / name: (folder / name).read_bytes() for name in ("checkpoint.json", "manifest.json")}


def change_config(monkeypatch: pytest.MonkeyPatch, change: str) -> dict:
    if change == "timeout":
        return {"stage_timeouts": {"universe": pipeline.DEFAULT_STAGE_TIMEOUTS["universe"] + 1}}
    version = pipeline._stage_version
    def changed(name: str) -> str:
        if name != "research_chain":
            return version(name)
        prefix, revision = version(name).rsplit(":", 1)
        return prefix + ":" + str(int(revision) + 1)
    monkeypatch.setattr(pipeline, "_stage_version", changed)
    return {}


@pytest.mark.parametrize("change", ["timeout", "stage_revision"])
def test_changed_config_does_not_select_unfinished_checkpoint(tmp_path, monkeypatch, change):
    first = run(tmp_path, "afternoon-old", [], unfinished=True)
    originals = original_files(tmp_path, "afternoon-old")
    config = change_config(monkeypatch, change)
    calls: list[str] = []
    report = run(tmp_path, "evening-new", calls, trigger="scheduled", **config)
    assert report["config_hash"] != first["config_hash"]
    assert report["status"] == "completed_all_executed"
    assert report["run_id"] == "evening-new"
    assert report["executed_stages"] == calls == list(pipeline.STAGE_ORDER)
    assert "resumed_from_run_id" not in report
    assert all(path.read_bytes() == raw for path, raw in originals.items())


def test_older_compatible_unfinished_run_is_selected_before_newer_incompatible(tmp_path):
    run(tmp_path, "compatible-old", [], unfinished=True)
    run(tmp_path, "incompatible-new", [], unfinished=True,
        stage_timeouts={"universe": pipeline.DEFAULT_STAGE_TIMEOUTS["universe"] + 1})
    os.utime(pipeline.checkpoint_path(tmp_path, "compatible-old"), (1000, 1000))
    os.utime(pipeline.checkpoint_path(tmp_path, "incompatible-new"), (2000, 2000))
    originals = {**original_files(tmp_path, "compatible-old"), **original_files(tmp_path, "incompatible-new")}
    calls: list[str] = []
    report = run(tmp_path, "resume-compatible", calls, trigger="scheduled")
    assert report["status"] == "completed_all_executed"
    assert report["resumed_from_run_id"] == "compatible-old"
    assert report["reused_stages"] == ["universe", "market_refresh", "health_alert"]
    assert calls == ["research_chain", "reconcile"]
    assert all(path.read_bytes() == raw for path, raw in originals.items())


@pytest.mark.parametrize("missing_hash", [None, ""])
def test_missing_config_hash_cannot_be_implicitly_resumed(tmp_path, missing_hash):
    run(tmp_path, "legacy-unfinished", [], unfinished=True)
    path = pipeline.checkpoint_path(tmp_path, "legacy-unfinished")
    doc = json.loads(path.read_bytes())
    if missing_hash is None:
        doc.pop("config_hash")
    else:
        doc["config_hash"] = missing_hash
    path.write_text(json.dumps(doc), encoding="utf-8")
    original = path.read_bytes()
    calls: list[str] = []
    report = run(tmp_path, "fresh-configured", calls, trigger="scheduled")
    assert report["status"] == "completed_all_executed"
    assert calls == list(pipeline.STAGE_ORDER)
    assert "resumed_from_run_id" not in report
    assert path.read_bytes() == original


def test_matching_unfinished_run_reuses_verified_stages(tmp_path):
    run(tmp_path, "same-config", [], unfinished=True)
    originals = original_files(tmp_path, "same-config")
    calls: list[str] = []
    report = run(tmp_path, "retry-same", calls, trigger="scheduled")
    assert report["status"] == "completed_all_executed"
    assert report["resumed_from_run_id"] == "same-config"
    assert report["reused_stages"] == ["universe", "market_refresh", "health_alert"]
    assert calls == ["research_chain", "reconcile"]
    assert all(path.read_bytes() == raw for path, raw in originals.items())


@pytest.mark.parametrize("change", ["timeout", "stage_revision"])
def test_explicit_cross_config_resume_still_blocks(tmp_path, monkeypatch, change):
    run(tmp_path, "explicit-source", [], unfinished=True)
    originals = original_files(tmp_path, "explicit-source")
    config = change_config(monkeypatch, change)
    calls: list[str] = []
    report = run(tmp_path, "explicit-rejected", calls,
        resume_run_id="explicit-source", trigger="scheduled", **config)
    assert report["status"] == "blocked"
    assert calls == []
    assert "resume_checkpoint_missing_or_target_config_invalid" in report["reason_codes"]
    assert all(path.read_bytes() == raw for path, raw in originals.items())


def test_completed_history_keeps_its_original_config_and_is_not_reexecuted(tmp_path):
    first = run(tmp_path, "completed-history", [],
        stage_timeouts={"universe": pipeline.DEFAULT_STAGE_TIMEOUTS["universe"] + 1})
    originals = original_files(tmp_path, "completed-history")
    calls: list[str] = []
    report = run(tmp_path, "later-invocation", calls, trigger="scheduled")
    assert report["status"] == "completed_all_executed"
    assert report["run_id"] == "completed-history"
    assert report["config_hash"] == first["config_hash"]
    assert report["executed_stages"] == calls == []
    assert report["reentry_invocation"]["config_hash"] != report["config_hash"]
    assert all(path.read_bytes() == raw for path, raw in originals.items())


def test_old_completed_stage_version_is_not_promoted_or_restarted(tmp_path, monkeypatch):
    run(tmp_path, "completed-old-code", [])
    originals = original_files(tmp_path, "completed-old-code")
    change_config(monkeypatch, "stage_revision")
    calls: list[str] = []
    report = run(tmp_path, "stale-completion-check", calls, trigger="scheduled")
    assert report["status"] == "failed"
    assert calls == []
    assert "scheduled_checkpoint_lacks_execution_proof" in report["reason_codes"]
    assert all(path.read_bytes() == raw for path, raw in originals.items())


def test_another_business_date_is_not_resumed(tmp_path):
    run(tmp_path, "other-day", [], unfinished=True, target_date="2026-10-30")
    originals = original_files(tmp_path, "other-day")
    calls: list[str] = []
    report = run(tmp_path, "current-day", calls, trigger="scheduled")
    assert report["status"] == "completed_all_executed"
    assert calls == list(pipeline.STAGE_ORDER)
    assert "resumed_from_run_id" not in report
    assert all(path.read_bytes() == raw for path, raw in originals.items())
