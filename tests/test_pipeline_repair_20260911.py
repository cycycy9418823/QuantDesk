"""Pipeline recovery receipts and benchmark target evidence, with no live I/O."""
from __future__ import annotations

import hashlib
import json
from datetime import datetime
from pathlib import Path

import pandas as pd
import pytest

from scripts import run_data_close_pipeline as pipeline
from scripts import run_daily_data_refresh as refresh
from quantdesk.data_truth import benchmark_incremental_update as benchmark

TARGET = "2026-09-10"


def _json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _project(root: Path) -> Path:
    config = root / "config"
    config.mkdir(parents=True, exist_ok=True)
    source = "stock_info_a_code_name"
    (config / "weekly_daily_wide_pool_contract.json").write_text(json.dumps({
        "rules": {"approved_market_universe_sources": [source]},
    }), encoding="utf-8")
    csv_path = root / "data/universe/a_share_symbols.csv"
    csv_path.parent.mkdir(parents=True)
    today = datetime.now(pipeline.SHANGHAI).strftime("%Y-%m-%d")
    csv_path.write_text("symbol,name,source,updated_at\n" + "".join(
        f"{index:06d},Fixture{index},{source},{today}T09:00:00\n" for index in range(20)
    ), encoding="utf-8")
    evidence = root / "results/universe_baseline_refresh/fixture/universe_baseline_refresh.json"
    evidence.parent.mkdir(parents=True)
    evidence.write_text(json.dumps({"accepted": True, "after": {
        "sources": [source], "sha256": hashlib.sha256(csv_path.read_bytes()).hexdigest(),
    }}), encoding="utf-8")
    return root


def _runners(calls: list[str], *, universe_status: str = "completed") -> dict:
    def stage(name):
        def run(*args, **kwargs):
            calls.append(name)
            return pipeline.StageResult(stage=name, status=universe_status if name == "universe" else "completed",
                                        started_at=pipeline._now_iso(), completed_at=pipeline._now_iso())
        return run
    return {name: stage(name) for name in pipeline.STAGE_ORDER}


def _run(root: Path, run_id: str, calls: list[str], **kwargs) -> dict:
    return pipeline.run_pipeline(root, network=True, run_id=run_id, target_date=TARGET,
                                 stage_runner=kwargs.pop("runners", _runners(calls)), **kwargs)


def test_degraded_proof_reuses_and_preserves_both_invocation_archives(tmp_path):
    root = _project(tmp_path)
    calls = []
    first = _run(root, "first", calls, runners=_runners(calls, universe_status="failed"))
    stage = first["stages"]["universe"]
    assert stage["stage_version"] == pipeline._stage_version("universe")
    assert stage["output_manifest"] == pipeline._output_manifest_for("universe", root)
    assert stage["degraded_proof"]["input_output_proof"]["output_manifest"] == stage["output_manifest"]
    accepted = stage["degraded_proof"]["accepted_roster"]["acceptance_evidence"]
    assert accepted["accepted"] is True
    assert accepted["sha256"] == hashlib.sha256((root / accepted["path"]).read_bytes()).hexdigest()
    first_file = root / pipeline.CHECKPOINT_DIR_RELATIVE / "first/manifest.json"
    original, checkpoint = first_file.read_bytes(), pipeline.checkpoint_path(root, "first").read_bytes()
    calls.clear()
    second = _run(root, "second", calls, trigger="scheduled")
    assert second["status"] == "completed_all_executed"
    assert not calls
    assert first_file.read_bytes() == original
    assert pipeline.checkpoint_path(root, "first").read_bytes() == checkpoint
    archived = _json(root / pipeline.CHECKPOINT_DIR_RELATIVE / "second/manifest.json")
    assert archived["invocation"]["run_id"] == "second"
    assert archived["invocation"]["finished_at"]
    assert second["reentry_invocation"]["finished_at"]
    assert second["attempt_id"] != first["attempt_id"]


@pytest.mark.parametrize("old_version", [None, "data_close_pipeline_v1:universe:1"])
def test_old_or_missing_proof_fails_without_erasing_prior_run(tmp_path, old_version):
    root = _project(tmp_path)
    _run(root, "first", [])
    cp = pipeline.checkpoint_path(root, "first")
    doc = _json(cp)
    doc["stages"]["universe"]["stage_version"] = old_version
    cp.write_text(json.dumps(doc), encoding="utf-8")
    original_cp = cp.read_bytes()
    first_file = cp.with_name("manifest.json")
    original_manifest = first_file.read_bytes()
    calls = []
    result = _run(root, "second", calls, trigger="scheduled")
    assert result["status"] == "failed"
    assert result["stages"]["universe"]["status"] == "invalid_stage_version"
    assert not calls
    assert cp.read_bytes() == original_cp and first_file.read_bytes() == original_manifest
    attempt = _json(root / pipeline.ATTEMPTS_DIR_RELATIVE / result["attempt_id"] / "manifest.json")
    assert attempt["invocation"]["status"] == "failed"
    assert attempt["invocation"]["finished_at"]


@pytest.mark.parametrize("change_roster", [False, True])
def test_explicit_resume_preserves_source_and_rechecks_downstream(tmp_path, change_roster):
    root = _project(tmp_path)
    _run(root, "first", [])
    cp = pipeline.checkpoint_path(root, "first")
    doc = _json(cp)
    doc["stages"]["universe"].pop("stage_version")
    cp.write_text(json.dumps(doc), encoding="utf-8")
    before = cp.read_bytes()
    calls = []
    runners = _runners(calls)
    original = runners["universe"]
    def universe(*args, **kwargs):
        if change_roster:
            path = root / "data/universe/a_share_symbols.csv"
            path.write_bytes(path.read_bytes().replace(b"Fixture0,", b"Renamed0,"))
        return original(*args, **kwargs)
    runners["universe"] = universe
    result = _run(root, "repair", calls, resume_run_id="first", runners=runners)
    assert result["status"] == "completed_all_executed"
    assert calls == (["universe", "market_refresh", "research_chain", "reconcile"] if change_roster else ["universe"])
    assert "health_alert" not in calls
    assert cp.read_bytes() == before
    assert result["recovery_plan"]["source_run_id"] == "first"


def test_recovery_plan_cli_is_read_only_and_bad_config_does_not_restart(tmp_path, monkeypatch, capsys):
    root = _project(tmp_path)
    _run(root, "first", [])
    before = {p.relative_to(root).as_posix(): p.read_bytes() for p in root.rglob("*") if p.is_file()}
    monkeypatch.setattr(pipeline, "PROJECT_ROOT", root)
    code = pipeline.main(["--enable-network", "--target-date", TARGET, "--resume-run-id", "first", "--recovery-plan"])
    report = json.loads(capsys.readouterr().out)
    assert code == 0 and report["recovery_plan"]["read_only"]
    assert report["recovery_plan"]["stages_to_run"] == []
    assert before == {p.relative_to(root).as_posix(): p.read_bytes() for p in root.rglob("*") if p.is_file()}
    calls = []
    result = _run(root, "bad-config", calls, resume_run_id="first", stage_timeouts={"universe": 1})
    assert result["status"] == "blocked" and not calls


def test_exception_leaves_own_terminal_before_releasing_writer(tmp_path):
    root = _project(tmp_path)
    calls = []
    runners = _runners(calls)
    def broken(*args, **kwargs):
        raise RuntimeError("fixture stage failure")
    runners["market_refresh"] = broken
    with pytest.raises(RuntimeError, match="fixture stage failure"):
        _run(root, "failed-run", calls, runners=runners)
    records = list((root / pipeline.ATTEMPTS_DIR_RELATIVE).glob("*/manifest.json"))
    assert len(records) == 1
    attempt = _json(records[0])
    assert attempt["status"] == "failed"
    assert attempt["invocation"]["aborted_stage"] == "market_refresh"
    assert attempt["invocation"]["finished_at"]
    owner, lock = pipeline.acquire_pipeline_lock(root, run_id="later")
    pipeline.release_pipeline_lock(owner, lock, run_id="later")


def test_repeated_requested_id_never_overwrites_first_checkpoint(tmp_path):
    root = _project(tmp_path)
    _run(root, "same", [])
    cp = pipeline.checkpoint_path(root, "same")
    old_cp, old_manifest = cp.read_bytes(), cp.with_name("manifest.json").read_bytes()
    second = _run(root, "same", [])
    assert second["run_id"] != "same"
    assert cp.read_bytes() == old_cp and cp.with_name("manifest.json").read_bytes() == old_manifest


def test_failed_universe_cannot_degrade_across_changed_inputs(tmp_path):
    root = _project(tmp_path)
    calls = []
    runners = _runners(calls)
    def failed(*args, **kwargs):
        (root / "config/default.yaml").write_text("fixture: changed\n", encoding="utf-8")
        return pipeline.StageResult(stage="universe", status="failed", started_at=pipeline._now_iso())
    runners["universe"] = failed
    result = _run(root, "changed-during-stage", calls, runners=runners)
    assert result["status"] == "failed"
    assert "input_mutated_during_stage" in result["stages"]["universe"]["reason_codes"]
    assert "degraded_proof" not in result["stages"]["universe"]


def test_checkpoint_rechecks_actual_benchmark_bytes(tmp_path):
    root = _project(tmp_path)
    _run(root, "first", [])
    path = root / "data/benchmark/hs300.csv"
    path.parent.mkdir(parents=True)
    path.write_text("date,close\n2026-09-09,100\n", encoding="utf-8")
    calls = []
    result = _run(root, "second", calls, trigger="scheduled")
    assert result["status"] == "failed"
    assert result["stages"]["market_refresh"]["status"] == "output_tampered_or_missing"
    assert not calls


def test_explicit_reuse_only_keeps_original_success_pointer(tmp_path):
    root = _project(tmp_path)
    _run(root, "first", [])
    pointer = root / pipeline.LATEST_JSON_RELATIVE.parent / "data_close_pipeline_last_successful.json"
    original = pointer.read_bytes()
    calls = []
    result = _run(root, "reuse-only", calls, resume_run_id="first")
    assert not calls and result["executed_stages"] == []
    assert result["last_successful_run_id"] == "first"
    assert pointer.read_bytes() == original


@pytest.mark.parametrize("local_current", [False, True])
def test_benchmark_family_uses_lease_target_and_keeps_no_rows_distinct(tmp_path, monkeypatch, local_current):
    root = tmp_path
    directory = root / "data/benchmark"
    directory.mkdir(parents=True)
    for spec in benchmark.BENCHMARK_SPECS:
        rows = "date,close\n2026-09-09,100\n" + (f"{TARGET},101\n" if local_current else "")
        (directory / spec.filename).write_text(rows, encoding="utf-8")
    originals = {p.name: p.read_bytes() for p in directory.iterdir()}
    monkeypatch.setattr(refresh, "PROJECT_ROOT", root)
    monkeypatch.setattr(benchmark, "default_index_fetcher", lambda symbol: pd.DataFrame(columns=["date", "close"]))
    owner, lock = pipeline.acquire_pipeline_lock(root, run_id="bench")
    try:
        grants = pipeline._grant_market_writer(root, run_id="bench", target=TARGET, owner=owner)
        for key, value in grants.items():
            monkeypatch.setenv(key, value)
        with pipeline.MarketWriterLease(root, run_id="bench", target=TARGET) as lease:
            result = refresh.family_benchmarks(True, lease=lease, target_date=TARGET)
    finally:
        pipeline.release_pipeline_lock(owner, lock, run_id="bench")
    assert result["target_date"] == TARGET
    assert result["target_covered"] is local_current
    assert result["ok"] is local_current
    assert all(row["target_date"] == TARGET and row["appended"] == 0 for row in result["indices"])
    assert "remote_no_rows" in result["reason_codes"]
    assert originals == {p.name: p.read_bytes() for p in directory.iterdir()}


def test_benchmark_gap_reaches_pipeline_without_blocking_other_stages(tmp_path, monkeypatch):
    root = _project(tmp_path)
    calls = []
    runners = _runners(calls)
    runners["market_refresh"] = pipeline.stage_market_refresh
    def child(root_arg, stage, command, **kwargs):
        evidence = {"status": "partial", "target_date": TARGET, "target_covered": False,
                    "freshness_status": "behind_target", "coverage": {"requested": 3, "target_covered": 0},
                    "reason_codes": ["benchmark_target_not_covered"]}
        doc = {"pipeline_run_id": kwargs["pipeline_run_id"], "target_date": TARGET,
               "status": "completed_with_warnings", "benchmark_evidence": evidence}
        path = root / "results/data_refresh/data_refresh_latest.json"
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps(doc), encoding="utf-8")
        doc.update(artifact_path=path.relative_to(root).as_posix(),
                   artifact_sha256="sha256:" + hashlib.sha256(path.read_bytes()).hexdigest())
        return "completed", [], {"exit_code": 0, "stdout_json": doc}
    monkeypatch.setattr(pipeline, "_run_owned_subprocess", child)
    report = _run(root, "gap", calls, runners=runners)
    assert report["status"] == "completed_all_executed"
    assert report["stages"]["market_refresh"]["status"] == "completed_with_warnings"
    assert report["benchmark_evidence"]["target_covered"] is False
    assert report["benchmark_evidence"]["freshness_status"] == "behind_target"
    assert "research_chain" in calls and "reconcile" in calls


@pytest.mark.parametrize("covered", [False, True])
def test_daily_refresh_cli_preserves_benchmark_target_quality(tmp_path, monkeypatch, capsys, covered):
    monkeypatch.setattr(refresh, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(refresh, "RESULTS_DIR", tmp_path / "results/data_refresh")
    monkeypatch.setattr(refresh, "_complete_live_session", lambda target: True)
    monkeypatch.setattr(refresh, "family_bars_and_valuation", lambda *args, **kwargs: {"ok": True, "warnings": []})
    monkeypatch.setattr(refresh, "family_news", lambda *args, **kwargs: {"ok": True})
    monkeypatch.setattr(refresh, "family_quarterly", lambda *args, **kwargs: {"ok": True})
    seen = []
    def family(network, *, lease, target_date):
        lease.assert_active()
        seen.append(target_date)
        return {"ok": covered, "status": "up_to_date" if covered else "partial", "target_date": target_date,
                "target_covered": covered, "freshness_status": "current" if covered else "behind_target",
                "coverage": {"requested": 3, "target_covered": 3 if covered else 0},
                "reason_codes": [] if covered else ["benchmark_target_not_covered"]}
    monkeypatch.setattr(refresh, "family_benchmarks", family)
    owner, lock = pipeline.acquire_pipeline_lock(tmp_path, run_id="cli-fixture")
    try:
        for key, value in pipeline._grant_market_writer(tmp_path, run_id="cli-fixture", target=TARGET, owner=owner).items():
            monkeypatch.setenv(key, value)
        code = refresh.main(["--enable-network", "--target-date", TARGET, "--pipeline-run-id", "cli-fixture"])
    finally:
        pipeline.release_pipeline_lock(owner, lock, run_id="cli-fixture")
    result = json.loads(capsys.readouterr().out)
    assert code == 0 and seen == [TARGET]
    assert result["overall_ok"] is covered
    assert result["benchmark_evidence"]["target_covered"] is covered
    assert result["status"] == ("completed" if covered else "completed_with_warnings")
    assert result["reason_codes"] == ([] if covered else ["benchmarks:benchmark_target_not_covered"])
