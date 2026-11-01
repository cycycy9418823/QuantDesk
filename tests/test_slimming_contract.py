"""Behavior and evidence retention contracts for the first slimming batch."""
import importlib
import json
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from quantdesk.research._serialization import json_safe
from scripts import run_data_close_pipeline as pipeline


@pytest.mark.parametrize("name", [
    "combined_score_backtest", "daily_research_snapshot", "factor_research_dashboard",
    "point_in_time_universe", "sector_rotation_engine",
])
def test_report_values_and_missing_data_are_preserved(name):
    clean = importlib.import_module(f"quantdesk.research.{name}")._json_safe
    original = {7: (np.int64(3), np.float64(2.5), float("nan")),
                "missing": [pd.NA, pd.NaT, float("inf"), -float("inf")],
                "label": "缺分钟 · 纸面未验证"}
    assert clean(original) == {"7": [3, 2.5, None], "missing": [None] * 4,
                               "label": "缺分钟 · 纸面未验证"}
    stamp = pd.Timestamp("2026-10-28")
    array = np.array([1, 2])
    assert clean(stamp) is stamp
    assert clean(array) is array
    assert clean is json_safe


@pytest.mark.parametrize("sink", ["checkpoint", "attempt", "published", "shadow", "stdout"])
def test_pipeline_receipts_keep_all_evidence_with_less_storage(tmp_path, monkeypatch, capsys, sink):
    report = {"run_id": "slimming", "attempt_id": "a" * 24,
              "status": "completed_all_executed", "target_date": "2026-10-28",
              "invocation": {}, "executed_stages": ["research_chain"], "reused_stages": [],
              "stages": {"research_chain": {"status": "completed_with_warnings",
                  "payload": {"reason_codes": ["missing_minute_data"] * 100,
                              "universe": ["000001", "600000", "920016"],
                              "no_broker": True, "missing": None}}}}
    if sink == "checkpoint":
        pipeline.save_checkpoint(tmp_path, "slimming", report)
        files = [pipeline.checkpoint_path(tmp_path, "slimming")]
    elif sink == "attempt":
        pipeline._write_attempt(tmp_path, report, terminal=True)
        files = list(tmp_path.rglob("manifest.json"))
    elif sink == "published":
        pipeline._publish(tmp_path, report)
        files = list(tmp_path.rglob("*.json"))
    elif sink == "shadow":
        pipeline._publish_shadow(tmp_path, report)
        files = list(tmp_path.rglob("*.json"))
    else:
        monkeypatch.setattr(pipeline, "PROJECT_ROOT", tmp_path)
        monkeypatch.setattr(pipeline, "run_pipeline", lambda *args, **kwargs: report)
        assert pipeline.main(["--json", "true"]) == 0
        output = capsys.readouterr().out
        assert json.loads(output) == report
        files = [tmp_path / "stdout.json"]
        files[0].write_text(output, encoding="utf-8")
    assert files
    for path in files:
        text = Path(path).read_text(encoding="utf-8")
        assert json.loads(text) == report
        assert len(text.encode("utf-8")) < len(json.dumps(report, ensure_ascii=False, indent=2).encode("utf-8")) * 0.8


@pytest.mark.parametrize("status,exit_code", [("completed_with_skips", 0), ("failed", 1)])
@pytest.mark.parametrize("trigger,option,summary", [
    ("scheduled", [], True), ("scheduled", ["--summary", "false"], False),
    ("manual", [], False), ("manual", ["--summary", "true"], True),
])
def test_console_summary_preserves_failures_and_links_complete_evidence(
    tmp_path, monkeypatch, capsys, status, exit_code, trigger, option, summary,
):
    report = {"run_id": "log-slimming", "attempt_id": "b" * 24,
              "target_date": "2026-10-28", "status": status,
              "reason_codes": ["missing_minute_data"], "invocation": {},
              "failed_stages": ["market_refresh"] if status == "failed" else [],
              "skipped_stages": [{"stage": "health_alert", "reason": "not_enabled"}],
              "executed_stages": ["research_chain"], "reused_stages": [],
              "stages": {"market_refresh": {"status": "failed" if status == "failed" else "completed",
                  "reason_codes": ["provider_partial"], "payload": {"exit_code": exit_code,
                      "stderr_tail": ["provider warning"], "stdout_json": {
                          "status": "partial", "warnings": ["missing minute data"],
                          "families": {"benchmarks": {"status": "failed", "error_type": "TimeoutError"}},
                          "historical_ledger": {"rows": ["long date evidence"] * 10000}}}}}}
    pipeline._publish(tmp_path, report)
    archive = tmp_path / pipeline.ATTEMPTS_DIR_RELATIVE / report["attempt_id"] / "manifest.json"
    original = archive.read_bytes()
    monkeypatch.setattr(pipeline, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(pipeline, "run_pipeline", lambda *args, **kwargs: report)
    assert pipeline.main(["--trigger", trigger, *option]) == exit_code
    text = capsys.readouterr().out
    output = json.loads(text)
    assert archive.read_bytes() == original
    assert output["status"] == status
    assert output["failed_stages"] == report["failed_stages"]
    assert output["skipped_stages"] == report["skipped_stages"]
    assert output["reason_codes"] == report["reason_codes"]
    if summary:
        assert output["output_detail"] == "summary"
        saved = json.loads((tmp_path / output["detail_path"]).read_bytes())
        assert saved == report
        assert saved["stages"]["market_refresh"]["payload"]["stdout_json"]["historical_ledger"]["rows"] == ["long date evidence"] * 10000
        assert output["stages"]["market_refresh"]["reason_codes"] == ["provider_partial"]
        assert output["failure_details"][0]["error_type"] == "TimeoutError"
        assert len(text) < len(original) * 0.02
    else:
        assert output == report


def test_unsaved_error_keeps_full_console_evidence(tmp_path, monkeypatch, capsys):
    report = {"status": "failed", "reason_codes": ["invalid_run_id"],
              "attempt_id": "c" * 24, "diagnostic": {"details": ["input not accepted"]}}
    monkeypatch.setattr(pipeline, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(pipeline, "run_pipeline", lambda *args, **kwargs: report)
    assert pipeline.main(["--trigger", "scheduled"]) == 1
    assert json.loads(capsys.readouterr().out) == report
