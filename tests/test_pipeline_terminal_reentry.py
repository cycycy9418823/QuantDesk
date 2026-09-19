from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

import scripts.run_data_close_pipeline as pipeline
from quantdesk.pipeline_terminal import classify_pipeline_status


TARGET_DATE = "2026-09-04"


def _runner_set(
    statuses: dict[str, str] | None = None,
    reasons: dict[str, list[str]] | None = None,
) -> tuple[dict[str, object], list[str]]:
    selected_statuses = statuses or {}
    selected_reasons = reasons or {}
    calls: list[str] = []

    def make_runner(name: str):
        def run(*_args, **_kwargs):
            calls.append(name)
            return pipeline.StageResult(
                stage=name,
                status=selected_statuses.get(name, "completed"),
                started_at="2026-09-04T15:40:00+08:00",
                completed_at="2026-09-04T15:41:00+08:00",
                reason_codes=list(selected_reasons.get(name, [])),
            )

        return run

    return {name: make_runner(name) for name in pipeline.STAGE_ORDER}, calls


def _read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


@pytest.mark.parametrize(
    ("mutation", "expected_status", "expected_failure"),
    [
        ({"remove": "reconcile"}, "failed", "missing:reconcile"),
        ({"stage": "reconcile", "status": "mystery"}, "failed", "reconcile"),
        ({"stage": "reconcile", "status": "timeout"}, "failed", "reconcile"),
        ({"stage": "reconcile", "status": "failed"}, "failed", "reconcile"),
    ],
)
def test_terminal_classifier_never_promotes_absent_unknown_timeout_or_failure(
    mutation: dict[str, str], expected_status: str, expected_failure: str
) -> None:
    stages = {name: {"status": "completed"} for name in pipeline.STAGE_ORDER}
    removed = mutation.get("remove")
    if removed:
        stages.pop(removed)
    else:
        stages[mutation["stage"]]["status"] = mutation["status"]

    classified = classify_pipeline_status(stages)

    assert classified["status"] == expected_status
    assert expected_failure in classified["failed_stages"]


def test_fresh_run_publishes_all_executed_with_current_execution_proof(
    tmp_path: Path,
) -> None:
    runners, calls = _runner_set()

    report = pipeline.run_pipeline(
        tmp_path,
        network=True,
        run_id="fresh-all",
        target_date=TARGET_DATE,
        stage_runner=runners,
    )

    assert report["status"] == "completed_all_executed"
    assert report["executed_stages"] == list(pipeline.STAGE_ORDER)
    assert report["reused_stages"] == []
    assert calls == list(pipeline.STAGE_ORDER)
    latest = _read_json(tmp_path / pipeline.LATEST_JSON_RELATIVE)
    last_successful = _read_json(
        tmp_path
        / pipeline.LATEST_JSON_RELATIVE.parent
        / "data_close_pipeline_last_successful.json"
    )
    assert latest["status"] == "completed_all_executed"
    assert last_successful["status"] == "completed_all_executed"
    archived = _read_json(
        tmp_path / pipeline.CHECKPOINT_DIR_RELATIVE / "fresh-all" / "manifest.json"
    )
    assert archived == latest == last_successful == report


def test_fresh_run_publishes_deliberate_skip_with_stage_and_reason(
    tmp_path: Path,
) -> None:
    runners, calls = _runner_set()

    report = pipeline.run_pipeline(
        tmp_path,
        network=True,
        run_id="fresh-skip",
        target_date=TARGET_DATE,
        stage_runner=runners,
        only_stages=list(pipeline.STAGE_ORDER[:-1]),
    )

    assert report["status"] == "completed_with_skips"
    assert report["skipped_stages"] == [
        {"stage": "health_alert", "reason": "stage_not_selected"}
    ]
    assert calls == list(pipeline.STAGE_ORDER[:-1])
    latest = _read_json(tmp_path / pipeline.LATEST_JSON_RELATIVE)
    assert latest["skipped_stages"] == report["skipped_stages"]


def test_scheduled_reentry_preserves_all_executed_without_claiming_new_execution(
    tmp_path: Path,
) -> None:
    runners, _calls = _runner_set()
    first = pipeline.run_pipeline(
        tmp_path,
        network=True,        run_id="complete-all",
        target_date=TARGET_DATE,
        stage_runner=runners,
    )
    assert first["status"] == "completed_all_executed"
    forbidden_runners, reentry_calls = _runner_set(
        {name: "failed" for name in pipeline.STAGE_ORDER}
    )

    reentry = pipeline.run_pipeline(
        tmp_path,
        network=True,
        run_id="scheduled-probe",
        target_date=TARGET_DATE,
        stage_runner=forbidden_runners,
        trigger="scheduled",
    )

    assert reentry["status"] == "completed_all_executed"
    assert reentry["run_id"] == "complete-all"
    assert reentry["executed_stages"] == []
    assert reentry["reused_stages"] == list(pipeline.STAGE_ORDER)
    assert all(
        stage.get("reused_from_checkpoint") is True
        for stage in reentry["stages"].values()
    )
    assert reentry_calls == []


@pytest.mark.parametrize("skipped_stage", pipeline.STAGE_ORDER)
def test_scheduled_reentry_preserves_truthful_skip_evidence(
    tmp_path: Path, skipped_stage: str,
) -> None:
    runners, _calls = _runner_set()
    first = pipeline.run_pipeline(
        tmp_path,
        network=True,
        run_id="complete-skip",
        target_date=TARGET_DATE,
        stage_runner=runners,
        only_stages=[name for name in pipeline.STAGE_ORDER if name != skipped_stage],
    )
    assert first["status"] == "completed_with_skips"
    forbidden_runners, reentry_calls = _runner_set(
        {name: "failed" for name in pipeline.STAGE_ORDER}
    )

    reentry = pipeline.run_pipeline(
        tmp_path,
        network=True,
        run_id="scheduled-probe",
        target_date=TARGET_DATE,
        stage_runner=forbidden_runners,
        trigger="scheduled",
    )

    assert reentry["status"] == "completed_with_skips"
    assert reentry["skipped_stages"] == [
        {"stage": skipped_stage, "reason": "stage_not_selected"}
    ]
    assert reentry["executed_stages"] == []
    assert reentry["reused_stages"] == [
        name for name in pipeline.STAGE_ORDER if name != skipped_stage
    ]
    assert reentry_calls == []


def test_cli_rejects_old_completed_checkpoint_and_keeps_prior_success_pointer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    runners, _calls = _runner_set()
    prior = pipeline.run_pipeline(
        tmp_path,
        network=True,
        run_id="prior-success",
        target_date=TARGET_DATE,
        stage_runner=runners,
    )
    assert prior["status"] == "completed_all_executed"

    prior_checkpoint_path = pipeline.checkpoint_path(tmp_path, "prior-success")
    old_checkpoint = _read_json(prior_checkpoint_path)
    prior_checkpoint_path.unlink()
    old_checkpoint["run_id"] = "old-completed"
    old_checkpoint["status"] = "completed"
    for stage in old_checkpoint["stages"].values():
        stage.pop("stage_version", None)
        stage.pop("input_manifest", None)
        stage.pop("output_manifest", None)
    pipeline.save_checkpoint(tmp_path, "old-completed", old_checkpoint)

    def forbid_external_stage(*_args, **_kwargs):
        raise AssertionError("old checkpoint reentry must not execute a stage")

    monkeypatch.setattr(pipeline, "PROJECT_ROOT", tmp_path)
    for name in pipeline.STAGE_ORDER:
        monkeypatch.setattr(pipeline, f"stage_{name}", forbid_external_stage)

    exit_code = pipeline.main(
        [
            "--enable-network",
            "--target-date",
            TARGET_DATE,
            "--trigger",
            "scheduled",
            "--json",
            "true",
        ]
    )
    cli_report = json.loads(capsys.readouterr().out)

    assert exit_code == pipeline.EXIT_INTERNAL_ERROR
    assert cli_report["status"] == "failed"
    assert cli_report["run_id"] == "old-completed"
    assert cli_report["latest_attempt_is_successful"] is False
    assert cli_report["last_successful_run_id"] == "prior-success"
    assert any(
        name in cli_report["failed_stages"] for name in pipeline.STAGE_ORDER
    )
    success_pointer = _read_json(
        tmp_path
        / pipeline.LATEST_JSON_RELATIVE.parent
        / "data_close_pipeline_last_successful.json"
    )
    assert success_pointer["run_id"] == "prior-success"
    assert success_pointer["status"] == "completed_all_executed"


@pytest.mark.parametrize("status", ["timeout", "mystery", "failed"])
def test_public_failure_cannot_be_masked_by_deliberate_skip_reason(
    tmp_path: Path, status: str,
) -> None:
    runners, _calls = _runner_set(
        {"health_alert": status},
        {"health_alert": ["dry_run", "health_alert_artifact_missing"]},
    )

    report = pipeline.run_pipeline(
        tmp_path,
        network=True,
        run_id="invalid-skip",
        target_date=TARGET_DATE,
        stage_runner=runners,
    )

    assert report["status"] == "failed"
    assert report["failed_stages"] == ["health_alert"]
    assert report["latest_attempt_is_successful"] is False
    assert report["last_successful_run_id"] == ""
    assert not (
        tmp_path / pipeline.LATEST_JSON_RELATIVE.parent
        / "data_close_pipeline_last_successful.json"
    ).exists()


@pytest.mark.parametrize("binding_valid", [True, False])
def test_cli_health_preview_requires_bound_evidence_and_is_not_execution(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    binding_valid: bool,
) -> None:
    runners, calls = _runner_set()
    for name in pipeline.STAGE_ORDER[:-1]:
        monkeypatch.setattr(pipeline, f"stage_{name}", runners[name])
    monkeypatch.setattr(pipeline, "PROJECT_ROOT", tmp_path)
    subprocess_commands: list[list[str]] = []

    class PreviewProcess:
        returncode = 0

        def __init__(self, command, **kwargs):
            subprocess_commands.append(command)
            assert Path(kwargs["cwd"]) == tmp_path
            doc = {
                "status": "preview_generated",
                "reason_code": "dry_run",
                "pipeline_run_id": command[command.index("--pipeline-run-id") + 1],
                "target_date": TARGET_DATE if binding_valid else "2026-09-03",
            }
            artifact = tmp_path / "results/data_health/data_health_alert_delivery_latest.json"
            artifact.parent.mkdir(parents=True, exist_ok=True)
            artifact.write_text(json.dumps(doc), encoding="utf-8")
            doc["artifact_path"] = artifact.relative_to(tmp_path).as_posix()
            doc["artifact_sha256"] = "sha256:" + hashlib.sha256(artifact.read_bytes()).hexdigest()
            self.stdout = json.dumps(doc)

        def communicate(self, *, timeout):
            assert timeout > 0
            return self.stdout, ""

    monkeypatch.setattr(pipeline.subprocess, "Popen", PreviewProcess)

    exit_code = pipeline.main([
        "--enable-network", "--target-date", TARGET_DATE,
        "--run-id", "preview-cli", "--json", "true",
    ])
    report = json.loads(capsys.readouterr().out)

    assert calls == list(pipeline.STAGE_ORDER[:-1])
    assert len(subprocess_commands) == 1
    assert "--push" not in subprocess_commands[0]
    assert "--enable-network" not in subprocess_commands[0]
    if binding_valid:
        assert exit_code == pipeline.EXIT_OK
        assert report["status"] == "completed_with_skips"
        assert report["skipped_stages"] == [{"stage": "health_alert", "reason": "dry_run"}]
        assert report["executed_stages"] == list(pipeline.STAGE_ORDER[:-1])
    else:
        assert exit_code == pipeline.EXIT_INTERNAL_ERROR
        assert report["status"] == "failed"
        assert report["failed_stages"] == ["health_alert"]
        assert report["latest_attempt_is_successful"] is False


def test_failed_scheduled_reentry_never_labels_previous_stages_as_new_execution(
    tmp_path: Path,
) -> None:
    runners, _calls = _runner_set()
    pipeline.run_pipeline(
        tmp_path, network=True, run_id="complete-before-missing",
        target_date=TARGET_DATE, stage_runner=runners,
    )
    checkpoint = _read_json(pipeline.checkpoint_path(tmp_path, "complete-before-missing"))
    checkpoint["stages"].pop("health_alert")
    pipeline.save_checkpoint(tmp_path, "complete-before-missing", checkpoint)
    forbidden_runners, calls = _runner_set({name: "failed" for name in pipeline.STAGE_ORDER})

    report = pipeline.run_pipeline(
        tmp_path, network=True, run_id="missing-probe", target_date=TARGET_DATE,
        trigger="scheduled", stage_runner=forbidden_runners,
    )

    assert report["status"] == "failed"
    assert report["failed_stages"] == ["missing:health_alert"]
    assert report["executed_stages"] == []
    assert report["reused_stages"] == list(pipeline.STAGE_ORDER[:-1])
    assert calls == []


@pytest.mark.parametrize("missing_field", ["input_manifest", "output_manifest"])
def test_scheduled_reentry_rejects_version_without_manifest_proof(
    tmp_path: Path, missing_field: str,
) -> None:
    runners, _calls = _runner_set()
    pipeline.run_pipeline(
        tmp_path, network=True, run_id="missing-proof",
        target_date=TARGET_DATE, stage_runner=runners,
    )
    checkpoint = _read_json(pipeline.checkpoint_path(tmp_path, "missing-proof"))
    checkpoint["stages"]["health_alert"].pop(missing_field)
    pipeline.save_checkpoint(tmp_path, "missing-proof", checkpoint)
    forbidden_runners, calls = _runner_set({name: "failed" for name in pipeline.STAGE_ORDER})

    report = pipeline.run_pipeline(
        tmp_path, network=True, run_id="proof-probe", target_date=TARGET_DATE,
        trigger="scheduled", stage_runner=forbidden_runners,
    )

    assert report["status"] == "failed"
    assert report["failed_stages"] == ["health_alert"]
    assert report["latest_attempt_is_successful"] is False
    assert calls == []


def test_scheduled_reentry_retains_original_scope_when_invocation_changes(
    tmp_path: Path,
) -> None:
    runners, _calls = _runner_set()
    first = pipeline.run_pipeline(
        tmp_path, network=True, run_id="original-scope", target_date=TARGET_DATE,
        stage_runner=runners, only_stages=list(pipeline.STAGE_ORDER[:-1]),
        stage_timeouts={"universe": 10},
    )
    forbidden_runners, calls = _runner_set({name: "failed" for name in pipeline.STAGE_ORDER})

    report = pipeline.run_pipeline(
        tmp_path, network=True, run_id="different-scope", target_date=TARGET_DATE,
        trigger="scheduled", stage_runner=forbidden_runners,
        alert_network=True, alert_push=True,
    )

    assert report["status"] == "completed_with_skips"
    assert report["alert_policy"] == "preview_only"
    assert report["trigger"] == "manual"
    assert report["started_at"] == first["started_at"]
    assert report["finished_at"] == first["finished_at"]
    assert report["config_hash"] == first["config_hash"]
    assert report["selected_stages"] == list(pipeline.STAGE_ORDER[:-1])
    assert report["reentry_invocation"]["run_id"] == "different-scope"
    assert report["reentry_invocation"]["alert_policy"] == "push"
    assert report["reentry_invocation"]["trigger"] == "scheduled"
    assert report["reentry_invocation"]["config_hash"] != report["config_hash"]
    assert report["executed_stages"] == []
    assert calls == []


def test_cli_failure_on_new_date_keeps_older_success_and_separates_latest(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    runners, _calls = _runner_set()
    pipeline.run_pipeline(
        tmp_path, network=True, run_id="older-success", target_date="2026-09-03",
        stage_runner=runners,
    )
    success_path = (
        tmp_path / pipeline.LATEST_JSON_RELATIVE.parent
        / "data_close_pipeline_last_successful.json"
    )
    original_success = success_path.read_bytes()
    failure_runners, calls = _runner_set({"health_alert": "timeout"})
    monkeypatch.setattr(pipeline, "PROJECT_ROOT", tmp_path)
    for name in pipeline.STAGE_ORDER:
        monkeypatch.setattr(pipeline, f"stage_{name}", failure_runners[name])

    exit_code = pipeline.main([
        "--enable-network", "--target-date", TARGET_DATE,
        "--trigger", "scheduled", "--run-id", "new-failure", "--json", "true",
    ])
    report = json.loads(capsys.readouterr().out)
    latest = _read_json(tmp_path / pipeline.LATEST_JSON_RELATIVE)

    assert exit_code == pipeline.EXIT_INTERNAL_ERROR
    assert report["status"] == "failed"
    assert latest["status"] == "failed"
    assert latest["target_date"] == TARGET_DATE
    assert latest["last_successful_run_id"] == "older-success"
    assert latest["latest_attempt_is_successful"] is False
    assert success_path.read_bytes() == original_success
    assert calls == list(pipeline.STAGE_ORDER)


def test_universe_degrades_with_structured_roster_proof(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed universe refresh may degrade to the last accepted roster, but the
    report must carry the roster's path/hash/quality/age, the degradation reason,
    the stage version and the execution identity (B2)."""

    runners, _calls = _runner_set(
        statuses={"universe": "failed"},
        reasons={"universe": ["universe_refresh_failed"]},
    )
    monkeypatch.setattr(pipeline, "_universe_staleness_days", lambda root: 2)
    proof = {
        "path": "data/universe/a_share_symbols.csv",
        "sha256": "a" * 64,
        "quality": "approved_source",
        "approved_sources": ["approved_source_a"],
        "latest_updated_at": "2026-09-02",
        "age_days": 2,
    }
    monkeypatch.setattr(pipeline, "_accepted_universe_proof", lambda root: proof)

    report = pipeline.run_pipeline(
        tmp_path,
        network=True,
        run_id="universe-degraded",
        target_date=TARGET_DATE,
        stage_runner=runners,
    )

    stage = report["stages"]["universe"]
    assert stage["status"] == "completed_with_warnings"
    assert any(
        "universe_degraded_to_last_accepted" in reason
        for reason in stage["reason_codes"]
    )
    degraded = stage.get("degraded_proof")
    assert degraded is not None
    assert degraded["degraded"] is True
    assert degraded["degradation_reason"] == "universe_refresh_failed_used_last_accepted"
    assert degraded["accepted_roster"] == proof
    assert degraded["run_id"] == "universe-degraded"
    assert degraded["target_date"] == TARGET_DATE
    assert degraded["stage_version"]


def test_universe_without_usable_roster_is_blocked_not_degraded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runners, _calls = _runner_set(
        statuses={"universe": "failed"},
        reasons={"universe": ["universe_refresh_failed"]},
    )
    monkeypatch.setattr(pipeline, "_universe_staleness_days", lambda root: None)

    report = pipeline.run_pipeline(
        tmp_path,
        network=True,
        run_id="universe-blocked",
        target_date=TARGET_DATE,
        stage_runner=runners,
    )

    stage = report["stages"]["universe"]
    assert stage["status"] == "blocked"
    assert any("universe_unverified_or_missing" in reason for reason in stage["reason_codes"])
    assert "degraded_proof" not in stage
