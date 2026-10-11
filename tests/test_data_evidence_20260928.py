"""An unexecuted benchmark check is not proof of missing target-day data."""

from unittest.mock import Mock

import pytest

from scripts import run_data_close_pipeline as pipeline


TARGET = "2026-09-24"


def _run_stage(tmp_path, monkeypatch, status, reasons, document):
    runner = Mock(return_value=(status, reasons, {"stdout_json": document}))
    monkeypatch.setattr(pipeline, "_run_owned_subprocess", runner)
    result = pipeline.stage_market_refresh(
        tmp_path, network=False, timeout=1, target=TARGET,
    )
    runner.assert_called_once()
    return result


@pytest.mark.parametrize("status", ["failed", "blocked", "completed"])
def test_absent_benchmark_check_remains_unknown(tmp_path, monkeypatch, status):
    reason = "live_snapshot_requires_today_completed_session"
    result = _run_stage(tmp_path, monkeypatch, status, [reason], {
        "target_date": TARGET, "status": "blocked", "families": {},
        "reason_codes": [reason],
    })

    evidence = result.payload["benchmark_evidence"]
    assert evidence["target_covered"] is None
    assert evidence["freshness_status"] == "not_assessed"
    assert evidence["target_date"] is None
    assert reason in result.reason_codes
    assert "benchmark_target_unverified" not in evidence["reason_codes"]
    assert pipeline._benchmark_target_covered(result.to_dict()) is None
    assert result.status == ("completed_with_warnings" if status == "completed" else status)


def test_explicit_unassessed_benchmark_remains_unknown(tmp_path, monkeypatch):
    result = _run_stage(tmp_path, monkeypatch, "completed", [], {
        "families": {"benchmarks": {
            "status": "error", "target_date": TARGET,
            "target_covered": None, "freshness_status": "not_assessed",
            "reason_codes": ["benchmark_update_failed:TimeoutError"],
        }},
    })
    assert result.payload["benchmark_evidence"]["target_covered"] is None
    assert "benchmarks:benchmark_update_failed:TimeoutError" in result.reason_codes
    assert result.status == "completed_with_warnings"


@pytest.mark.parametrize("covered", [True, False])
def test_actual_benchmark_claim_is_preserved(tmp_path, monkeypatch, covered):
    result = _run_stage(tmp_path, monkeypatch, "completed", [], {
        "benchmark_evidence": {
            "target_date": TARGET, "target_covered": covered,
            "freshness_status": "current" if covered else "behind_target",
            "reason_codes": [] if covered else ["benchmark_target_not_covered"],
        },
    })
    assert pipeline._benchmark_target_covered(result.to_dict()) is covered
    assert result.status == ("completed" if covered else "completed_with_warnings")


def test_mismatched_target_still_cannot_claim_coverage(tmp_path, monkeypatch):
    result = _run_stage(tmp_path, monkeypatch, "completed", [], {
        "benchmark_evidence": {
            "target_date": "2026-09-23", "target_covered": True,
            "freshness_status": "current", "reason_codes": [],
        },
    })
    evidence = result.payload["benchmark_evidence"]
    assert evidence["target_covered"] is False
    assert "benchmark_target_unverified" in evidence["reason_codes"]


def _timed_runners(monkeypatch, *, market_status="completed", explicit=False):
    clock = [100.0]
    calls = []
    monkeypatch.setattr(pipeline.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(pipeline, "_now_iso", lambda: "2026-09-24T15:41:00+08:00")

    def make(name):
        def run(*_args, **_kwargs):
            calls.append(name)
            clock[0] += 3.25
            return pipeline.StageResult(
                stage=name,
                status=market_status if name == "market_refresh" else "completed",
                started_at="2026-09-24T15:40:00+08:00",
                completed_at="2026-09-24T15:40:20+08:00" if explicit else "",
                elapsed_seconds=20.0 if explicit else 0.0,
            )
        return run

    return {name: make(name) for name in pipeline.STAGE_ORDER}, calls


@pytest.mark.parametrize("market_status", ["completed", "failed"])
def test_executed_stage_has_finish_time_and_measured_duration(tmp_path, monkeypatch, market_status):
    runners, calls = _timed_runners(monkeypatch, market_status=market_status)
    report = pipeline.run_pipeline(
        tmp_path, network=True, run_id="timing-first", target_date=TARGET,
        stage_runner=runners,
    )
    for name in calls:
        stage = report["stages"][name]
        assert stage["completed_at"] == "2026-09-24T15:41:00+08:00"
        assert stage["elapsed_seconds"] == 3.25
    assert report["stages"]["market_refresh"]["status"] == market_status
    assert (report["status"] == "failed") == (market_status == "failed")


def test_skipped_and_reused_stages_never_get_new_execution_times(tmp_path, monkeypatch):
    runners, calls = _timed_runners(monkeypatch)
    first = pipeline.run_pipeline(
        tmp_path, network=True, run_id="timing-source", target_date=TARGET,
        stage_runner=runners, only_stages=["market_refresh"],
    )
    assert calls == ["market_refresh"]
    assert first["stages"]["universe"]["completed_at"] == ""
    assert first["stages"]["universe"]["elapsed_seconds"] == 0.0
    original = first["stages"]["market_refresh"]
    assert original["elapsed_seconds"] == 3.25
    calls.clear()
    monkeypatch.setattr(pipeline, "_now_iso", lambda: "2026-09-24T18:31:00+08:00")
    second = pipeline.run_pipeline(
        tmp_path, network=True, run_id="timing-reentry", target_date=TARGET,
        stage_runner=runners, trigger="scheduled", only_stages=["market_refresh"],
    )
    assert calls == []
    reused = second["stages"]["market_refresh"]
    assert reused["reused_from_checkpoint"] is True
    assert reused["completed_at"] == original["completed_at"]
    assert reused["elapsed_seconds"] == original["elapsed_seconds"]


def test_runner_supplied_execution_times_are_preserved(tmp_path, monkeypatch):
    runners, _ = _timed_runners(monkeypatch, explicit=True)
    report = pipeline.run_pipeline(
        tmp_path, network=True, run_id="timing-explicit", target_date=TARGET,
        stage_runner=runners,
    )
    for stage in report["stages"].values():
        assert stage["completed_at"] == "2026-09-24T15:40:20+08:00"
        assert stage["elapsed_seconds"] == 20.0
