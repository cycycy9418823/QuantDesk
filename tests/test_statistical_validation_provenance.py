"""Formal statistics reject explicitly nonformal evidence before computation."""
from __future__ import annotations

import copy
import json
from pathlib import Path
import subprocess
import sys

import pytest

from quantdesk.research import backtest_statistical_validation as stats


@pytest.mark.parametrize("markers", [
    {"synthetic_test_data": True},
    {"report_provenance": {"synthetic_test_data": True}},
    {"assumptions": {"synthetic_test_data": True}},
    {"data_source": "synthetic_test_data", "synthetic_test_data": False},
    {"data_scope": "synthetic_test_data"},
    {"run_type": "smoke"},
    {"report_provenance": {"run_type": "demo"}},
    {"warnings": ["synthetic_test_data: test/demo only; not formal market evidence"]},
])
@pytest.mark.parametrize("strict", [True, False])
def test_nonformal_input_is_rejected_before_extracting_returns(
    markers: dict, strict: bool, monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = {"status": "passed", "trade_returns": [.05, -.01] * 20, **markers}
    before = copy.deepcopy(payload)

    def forbidden(*args, **kwargs):
        pytest.fail("nonformal input reached a statistical computation")

    for name in ("_extract_returns_with_source", "permutation_test_signals",
                 "bootstrap_metric_confidence_intervals", "compute_metric_summary"):
        monkeypatch.setattr(stats, name, forbidden)
    result = stats.run_backtest_statistical_validation(payload, strict=strict, iterations=40, write=False)
    assert result["ok"] is False
    assert result["status"] == "missing"
    assert result["input_rejection_reason"] == "nonformal_backtest_input"
    assert result["validation_run_status"] == "not_run"
    assert "没有真实回测" in result["interpretation"]
    assert result["iterations"] == result["sample_size"] == 0
    assert result["trade_count"] is result["negative_return_count"] is None
    assert all(value is None for value in result["metric_summary"].values())
    assert result["permutation_test"]["p_value"] is None
    assert result["permutation_test"]["enabled"] is result["bootstrap_ci"]["enabled"] is False
    assert payload == before


def test_unmarked_and_explicitly_nonsynthetic_samples_keep_the_same_statistics() -> None:
    payload = {"trade_returns": [.05, -.01] * 20}
    original = stats.run_backtest_statistical_validation(payload, iterations=200, seed=42, write=False)
    marked = stats.run_backtest_statistical_validation(
        {**payload, "synthetic_test_data": False}, iterations=200, seed=42, write=False,
    )
    assert original["status"] == marked["status"] == "passed"
    for key in ("metric_summary", "permutation_test", "bootstrap_ci", "sample_size", "trade_count"):
        assert original[key] == marked[key]
    assert marked["validation_status"] == "research_unvalidated"
    assert marked["out_of_sample_validation"] == "not_established"


def test_file_rejection_keeps_source_report_and_summary_without_stale_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    folder = tmp_path / "results" / "backtest"
    folder.mkdir(parents=True)
    primary = folder / "backtest_latest.json"
    old = folder / "latest.json"
    primary.write_text(json.dumps({"synthetic_test_data": True, "generated_at": "2026-10-16",
                                  "trade_returns": [.05, -.01] * 20}), encoding="utf-8")
    old.write_text(json.dumps({"trade_returns": [.05, -.01] * 20}), encoding="utf-8")
    before = {path: path.read_bytes() for path in (primary, old)}
    monkeypatch.setattr(stats, "PROJECT_ROOT", tmp_path)
    output = tmp_path / "validation.json"
    report = tmp_path / "validation.md"
    result = stats.run_backtest_statistical_validation(
        strict=False, write=True, output_path=output, report_path=report,
    )
    assert json.loads(output.read_text(encoding="utf-8")) == result
    assert {path: path.read_bytes() for path in before} == before
    assert result["backtest_result_source"]["path"] == str(primary)
    assert result["backtest_result_source"]["generated_at"] == "2026-10-16"
    assert result["backtest_result_source"]["fallback_used"] is False
    assert "没有真实回测" in report.read_text(encoding="utf-8")
    summary = stats.summarize_statistical_validation(result)
    assert summary["status"] == "missing"
    assert summary["input_data_mode"] == "synthetic"
    assert summary["input_rejection_reason"] == "nonformal_backtest_input"
    assert summary["validation_run_status"] == "not_run"
    assert summary["p_value"] is None
    assert all(not value for value in summary["bootstrap_ci_summary"].values())


def test_cli_rejects_synthetic_input_and_writes_only_requested_outputs(tmp_path: Path) -> None:
    source = tmp_path / "test-backtest.json"
    source.write_text(json.dumps({"synthetic_test_data": True,
                                 "trade_returns": [.05, -.01] * 20}), encoding="utf-8")
    output = tmp_path / "output" / "validation.json"
    report = tmp_path / "output" / "validation.md"
    defaults = (stats.DEFAULT_JSON_PATH, stats.DEFAULT_REPORT_PATH)
    before = {path: path.read_bytes() if path.exists() else None for path in defaults}
    root = Path(__file__).resolve().parents[1]
    completed = subprocess.run([
        sys.executable, "-B", "-X", "utf8", str(root / "scripts/run_backtest_statistical_validation.py"),
        "--backtest-result", str(source), "--strict", "false", "--network", "false",
        "--output-path", str(output), "--report-path", str(report),
    ], cwd=root, text=True, encoding="utf-8", capture_output=True, check=False, timeout=30)
    assert completed.returncode == 1, completed.stderr + completed.stdout
    assert "没有真实回测" in completed.stdout
    assert f"json path: {output}" in completed.stdout
    assert json.loads(output.read_text(encoding="utf-8"))["status"] == "missing"
    assert "没有真实回测" in report.read_text(encoding="utf-8")
    assert {path: path.read_bytes() if path.exists() else None for path in defaults} == before
