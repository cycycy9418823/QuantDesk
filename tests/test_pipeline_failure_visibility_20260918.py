"""Explicit family failures survive the saved startup terminal and GET UI DTO."""
from copy import deepcopy
from pathlib import Path
import json

from flask import Flask
import pytest

from scripts import startup_data_update
from scripts import run_data_close_pipeline
from scripts import run_daily_data_refresh
from scripts.server.routes.startup import StartupRouteDeps, create_startup_blueprint


def _report(error_type: str) -> dict:
    return {"run_id": "fixture-close", "target_date": "2026-09-17", "status": "failed", "stages": {
        "market_refresh": {"status": "failed", "reason_codes": ["stage_exit_code:market_refresh:1"], "payload": {"stdout_json": {
            "families": {"bars_and_valuation": {"status": "failed", "ok": False, "error_type": error_type}}
        }}}
    }}


@pytest.mark.parametrize(("error_type", "category"), [("TimeoutError", "provider_transport"), ("TypeError", "processing_exception")])
def test_saved_terminal_keeps_distinguishable_failure(tmp_path: Path, monkeypatch, error_type: str, category: str) -> None:
    report = _report(error_type)
    monkeypatch.setattr(run_data_close_pipeline, "run_pipeline", lambda **kw: deepcopy(report))
    monkeypatch.setattr(run_daily_data_refresh, "_complete_live_session", lambda *_a: True)
    result = startup_data_update.run_startup_data_update(tmp_path, enable_network=True, target_date="2026-09-17")
    assert result["status"] == "failed" and result["ok"] is False
    assert result["failure_details"][0]["error_type"] == error_type
    assert result["failure_details"][0]["category"] == category
    stored = json.loads((tmp_path / "results/startup_update_latest.json").read_text(encoding="utf-8"))
    assert stored["failure_details"] == result["failure_details"]
    assert result["pipeline"] == report


def _client(payload: dict):
    def forbidden(*_a, **_kw):
        raise AssertionError("read-only failure display must not start work or change state")
    deps = StartupRouteDeps(
        read_startup_payload=lambda: deepcopy(payload), write_startup_status_payload=forbidden,
        generation_status_for_ui=forbidden, startup_refresh_state_payload=lambda: {"running": False},
        startup_payload_has_completed_after_state=lambda *_a: False, set_startup_refresh_state=forbidden,
        candidate_rows=forbidden, data_gap_summary=forbidden,
        startup_provider_update_diagnosis=lambda *_a: {}, startup_update_explanation=lambda *_a: {},
        request_truthy=bool, make_request_id=lambda: "fixture", start_startup_refresh_worker=forbidden,
    )
    app = Flask(__name__)
    app.register_blueprint(create_startup_blueprint(deps))
    return app.test_client()


@pytest.mark.parametrize(("error_type", "category"), [("TimeoutError", "provider_transport"), ("TypeError", "processing_exception")])
def test_get_exposes_original_nested_failure_without_rewriting_history(error_type: str, category: str) -> None:
    payload = {"status": "failed", "pipeline": _report(error_type)}
    original = deepcopy(payload)
    response = _client(payload).get("/api/startup/update-status")
    assert response.status_code == 200
    details = response.get_json()["failure_details"]
    assert len(details) == 1 and details[0]["category"] == category
    assert details[0]["error_type"] == error_type
    assert payload == original


def test_skip_and_unknown_failures_are_not_invented() -> None:
    from quantdesk.pipeline_terminal import pipeline_failure_details
    report = {"stages": {
        "health_alert": {"status": "skipped", "reason_codes": ["data_health_alert_push_not_explicitly_enabled"]},
        "research_chain": {"status": "failed", "payload": {"exit_code": 1}},
    }}
    details = pipeline_failure_details(report)
    assert len(details) == 1 and details[0]["stage"] == "research_chain"
    assert details[0]["error_type"] == "" and details[0]["category"] == "unclassified"


def test_success_with_failed_optional_family_keeps_warning_and_terminal() -> None:
    from quantdesk.pipeline_terminal import pipeline_failure_details
    report = _report("JSONDecodeError")
    report["status"] = "completed_with_skips"
    stage = report["stages"]["market_refresh"]
    stage["status"] = "completed_with_warnings"
    original = deepcopy(report)
    details = pipeline_failure_details(report)
    assert details[0]["error_type"] == "JSONDecodeError"
    assert report == original


def test_failure_display_does_not_copy_raw_traceback_or_secrets() -> None:
    from quantdesk.pipeline_terminal import pipeline_failure_details
    report = _report("RuntimeError")
    report["stages"]["market_refresh"]["payload"]["stdout_json"]["families"]["bars_and_valuation"]["message"] = "token=do-not-copy <script>"
    encoded = json.dumps(pipeline_failure_details(report))
    assert "do-not-copy" not in encoded and "<script>" not in encoded


@pytest.mark.parametrize("status", ["skipped_network_false", "skipped_offseason"])
def test_explicit_family_skips_are_not_reported_as_failures(status: str) -> None:
    from quantdesk.pipeline_terminal import pipeline_failure_details
    report = {"stages": {"market_refresh": {"status": "completed_with_warnings", "payload": {"stdout_json": {
        "families": {"quarterly": {"ok": False, "status": status}}
    }}}}}
    assert pipeline_failure_details(report) == []


@pytest.mark.parametrize(("error", "category"), [(TimeoutError("fixture timeout"), "provider_transport"), (TypeError("fixture bad call"), "processing_exception")])
def test_actual_benchmark_failure_reaches_saved_and_get_results(tmp_path: Path, monkeypatch, error: Exception, category: str) -> None:
    from types import SimpleNamespace
    from quantdesk.data_truth import benchmark_incremental_update
    def fail(*_a, **_kw):
        raise error
    monkeypatch.setattr(run_daily_data_refresh, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(benchmark_incremental_update, "update_benchmark_files", fail)
    lease = SimpleNamespace(target="2026-09-17", assert_active=lambda: None)
    family = run_daily_data_refresh.family_benchmarks(True, lease=lease, target_date="2026-09-17")
    assert family["error_type"] == type(error).__name__
    report = _report(type(error).__name__)
    report["stages"]["market_refresh"]["payload"]["stdout_json"]["families"] = {"benchmarks": family}
    monkeypatch.setattr(run_data_close_pipeline, "run_pipeline", lambda **kw: report)
    monkeypatch.setattr(run_daily_data_refresh, "_complete_live_session", lambda *_a: True)
    result = startup_data_update.run_startup_data_update(tmp_path, enable_network=True, target_date="2026-09-17")
    saved = json.loads((tmp_path / "results/startup_update_latest.json").read_text(encoding="utf-8"))
    visible = _client(saved).get("/api/startup/update-status").get_json()
    assert visible["failure_details"] == result["failure_details"]
    assert visible["failure_details"][0]["category"] == category
    assert visible["failure_details"][0]["component"] == "benchmarks"
