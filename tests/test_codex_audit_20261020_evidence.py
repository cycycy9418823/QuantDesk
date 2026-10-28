"""Formal readers and real writers reject polluted, old or unknown evidence."""
from __future__ import annotations

from pathlib import Path
from datetime import date
import json

import pandas as pd
import pytest

from scripts import quant_api_server as server
from quantdesk.research.combined_score_backtest import write_combined_score_backtest_outputs


def test_synthetic_latest_is_not_available_formal_context(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(server, "project_root", tmp_path)
    monkeypatch.setattr(server, "expected_settlement_date", lambda *_a: date(2026, 10, 19))
    path = tmp_path / "results/backtest/backtest_latest.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"status": "passed", "synthetic_test_data": True,
        "end_date": "2026-10-19", "metrics": {"total_return": 0.1}}), encoding="utf-8")
    result = server.latest_research_evidence_context()["backtest_summary"]
    assert result["available"] is False
    assert result["formal_eligible"] is False
    assert any("nonformal" in r for r in result["evidence_admission"]["reason_codes"])


def test_combined_synthetic_writer_preserves_formal_latest_bytes(tmp_path: Path, monkeypatch) -> None:
    from quantdesk import experiment_tracker
    monkeypatch.setattr(experiment_tracker, "log_experiment_non_blocking", lambda *_a, **_k: {})
    out = tmp_path / "results"; reports = tmp_path / "reports"
    out.mkdir(); reports.mkdir()
    formal = out / "combined_score_backtest_latest.json"
    formal.write_bytes(b"formal sentinel\n")
    result = write_combined_score_backtest_outputs({"ok": True, "status": "warning",
        "data_source": "synthetic_test_data", "warnings": ["synthetic_test_data: demo only"]},
        output_dir=out, report_dir=reports)
    assert formal.read_bytes() == b"formal sentinel\n"
    assert "nonformal" in result["output_paths"]["latest_json"]


def test_identity_requires_current_date_mode_code_data_and_config(tmp_path: Path) -> None:
    from quantdesk.research.evidence_identity import assess_research_evidence, stamp_research_evidence
    payload = stamp_research_evidence({"ok": True, "status": "passed"}, target_date="2026-10-19",
        code_identity={"source_sha256": "a" * 64}, data_identity={"600000": "b" * 64},
        config_identity={"holding_days": 5}, run_id="fixture-run")
    assert assess_research_evidence(payload, target_date="2026-10-19")["formal_eligible"]
    assert not assess_research_evidence(payload, target_date="2026-10-20")["formal_eligible"]
    payload["evidence_identity"]["run_id"] = "other"
    assert not assess_research_evidence(payload, target_date="2026-10-19")["formal_eligible"]


@pytest.mark.parametrize("code,data", [({"git_head": "unknown"}, {"file": "b" * 64}),
    ({"source_sha256": "a" * 64}, {"status": "stable"})])
def test_nonempty_identity_without_source_bytes_is_not_formal(code: dict, data: dict) -> None:
    from quantdesk.research.evidence_identity import assess_research_evidence, stamp_research_evidence
    payload = stamp_research_evidence({"ok": True}, target_date="20261019", code_identity=code,
        data_identity=data, config_identity={"holding_days": 5})
    assert payload["evidence_identity"]["target_date"] == "2026-10-19"
    assert not assess_research_evidence(payload, target_date="2026-10-19")["formal_eligible"]


def test_all_actual_research_context_readers_reject_demo(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(server, "project_root", tmp_path)
    monkeypatch.setattr(server, "expected_settlement_date", lambda *_a: date(2026, 10, 19))
    for rel in ("combined_score_backtest_latest.json", "walk_forward_latest.json",
                "strategy_lab/v420_strategy_leaderboard_latest.json", "factor_health_latest.json"):
        path = tmp_path / "results" / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"status": "passed", "data_scope": "synthetic_test_data"}), encoding="utf-8")
    context = server.latest_research_evidence_context()
    for key in ("combined_score_backtest", "walk_forward", "strategy_lab", "factor_health"):
        assert context[key]["available"] is False
        assert context[key]["formal_eligible"] is False


def test_strategy_lab_summary_reads_valid_current_identity_bytes(tmp_path: Path, monkeypatch) -> None:
    from quantdesk.research.evidence_identity import stamp_research_evidence
    monkeypatch.setattr(server, "project_root", tmp_path)
    monkeypatch.setattr(server, "expected_settlement_date", lambda *_a: date(2026, 10, 19))
    directory = tmp_path / "results/strategy_lab"
    directory.mkdir(parents=True)
    leaderboard = stamp_research_evidence({"status":"completed_with_warnings", "items":[{
        "strategy_id":"limit_up_family", "research_rank":1, "mapping_coverage":0.9}],
        "research_only":True}, target_date="2026-10-19", run_id="fixture-run",
        code_identity={"source_sha256":"a" * 64}, data_identity={"fixture.csv":"b" * 64},
        config_identity={"scope":"summary-contract"})
    for name, value in {
        "v420_strategy_leaderboard_latest.json": leaderboard,
        "v420_real_use_strategy_lab_handoff_latest.json": {
            "remaining_gap_for_one_stop_research":"provider freshness needs explicit network update"},
        "v421_strategy_statistics_latest.json": {
            "items":[{"strategy_family":"limit_up_family", "candidate_count":8}]},
    }.items():
        (directory / name).write_text(json.dumps(value), encoding="utf-8")
    before = {path.name:path.read_bytes() for path in directory.iterdir()}
    value = server.app.test_client().get("/api/strategy-lab/summary").get_json()
    assert value["available"] is True
    assert value["evidence_only"] is True and value["paper_only"] is True
    assert value["no_broker"] is True and value["no_real_orders"] is True
    assert value["not_position_advice"] is True
    assert value["top_strategies"][0]["strategy_id"] == "limit_up_family"
    assert value["top_strategies"][0]["candidate_count"] == 8
    assert value["top_strategies"][0]["mapping_coverage"] == 0.9
    assert any("provider freshness" in text for text in value["warnings_top"])
    assert before == {path.name:path.read_bytes() for path in directory.iterdir()}


def test_actual_backtest_smoke_then_formal_restores_output_paths(tmp_path: Path, monkeypatch) -> None:
    from scripts import run_backtest as runner
    output = tmp_path / "results/backtest/backtest_latest.json"
    output.parent.mkdir(parents=True)
    output.write_bytes(b"formal sentinel\n")
    monkeypatch.setattr(runner, "RESULT_JSON", output)
    monkeypatch.setattr(runner, "REPORT_MD", tmp_path / "reports/backtest.md")
    monkeypatch.setattr(runner, "ARTIFACT_DIR", tmp_path / "results/backtest/artifacts")
    monkeypatch.setattr(runner, "log_experiment_non_blocking", lambda *_a, **_k: {})
    missing = str(tmp_path / "daily")
    assert runner.main(["--symbols", "600000", "--daily-dir", missing, "--end-date", "2026-10-19", "--smoke-run", "true", "--json", "true"]) == 0
    assert output.read_bytes() == b"formal sentinel\n"
    assert runner.RESULT_JSON == output
    report = tmp_path / "reports/backtest.md"
    smoke_report = tmp_path / "reports/nonformal/smoke/backtest.md"
    assert smoke_report.is_file() and not report.exists()
    smoke_bytes = smoke_report.read_bytes()
    assert runner.main(["--symbols", "600000", "--daily-dir", missing, "--end-date", "2026-10-19", "--json", "true"]) == 0
    assert json.loads(output.read_text(encoding="utf-8"))["run_type"] == "formal"
    assert report.is_file() and smoke_report.read_bytes() == smoke_bytes


def test_actual_postsplit_18_is_not_claimed_to_meet_20() -> None:
    from ai_trader import researcher
    panel = pd.DataFrame({str(600000 + i): [10 * (1.005 + i / 1e5) ** d for d in range(95)]
        for i in range(10)}, index=pd.bdate_range("2026-01-01", periods=95).strftime("%Y-%m-%d"))
    row = researcher._evaluate_template(researcher.TEMPLATES[1], panel)
    assert row["holdout_n"] in {18, 19, 20}
    assert row["meets_holdout_minimum"] is (row["holdout_n"] >= 20)
    assert row["proven_edge"] is False
    assert "允许上场" not in row["reason"]
    assert row["validation_label"] == "研究假设 · 未验证"


def test_independent_plan_hash_and_future_window_are_reproducible() -> None:
    from quantdesk.research.validation_identity import build_validation_plan, evaluate_independent_returns
    args = dict(selection_end="2026-10-19", validation_start="2026-10-27", validation_end="2027-04-01",
        selected_config={"hypothesis_id": "low_vol_20", "lookback": 20}, selection_data_hash="a" * 64, run_id="fixed")
    plan = build_validation_plan(**args)
    assert plan == build_validation_plan(**args)
    values = pd.Series([.03] * 19, index=pd.bdate_range("2026-10-27", periods=19))
    result = evaluate_independent_returns(plan, values)
    assert "independent_observations_below_minimum" in result["warnings"]
    assert result["proven_edge"] is False
    plan["round_trip_cost"] = 0
    with pytest.raises(ValueError, match="hash"):
        evaluate_independent_returns(plan, values)


def test_validation_checks_session_spacing_purge_and_predeclaration() -> None:
    from quantdesk.research.validation_identity import build_validation_plan, evaluate_independent_returns, identity_hash
    plan = build_validation_plan(selection_end="2026-10-19", validation_start="2026-10-27", validation_end="2027-06-01",
        selected_config={"hypothesis_id": "low_vol_20"}, selection_data_hash="a" * 64, run_id="frozen")
    sessions = pd.bdate_range("2026-10-19", "2027-06-01").strftime("%Y-%m-%d").tolist()
    indices = [i for i in range(6, 106, 5)]
    values = pd.Series([.03] * 20, index=pd.to_datetime([sessions[i] for i in indices]))
    benchmark = pd.Series([.01] * 20, index=values.index)
    first = evaluate_independent_returns(plan, values, benchmark=benchmark)
    interval = {"plan_sha256": plan["plan_sha256"], "returns_sha256": first["returns_sha256"], "trading_sessions": sessions,
        "intervals": [{"entry_date": sessions[i], "exit_date": sessions[i+5]} for i in indices]}
    interval["evidence_sha256"] = identity_hash(interval)
    unseen = {"status": "verified", "plan_sha256": plan["plan_sha256"], "run_id": "frozen",
        "config_sha256": plan["config_sha256"], "selection_data_sha256": "a" * 64,
        "validation_data_sha256": first["returns_sha256"], "predeclared_at": "2026-10-20T20:00:00+08:00",
        "selection_max_label_date": "2026-10-19"}
    unseen["evidence_sha256"] = identity_hash(unseen)
    valid = evaluate_independent_returns(plan, values, benchmark=benchmark, unseen_evidence=unseen, interval_evidence=interval)
    assert valid["independent_n"] == 20 and valid["validation_status"] == "research_diagnostic_complete"
    assert valid["proven_edge"] is False
    unseen["predeclared_at"] = "2027-01-01T00:00:00+08:00"
    unseen["evidence_sha256"] = identity_hash({k: v for k, v in unseen.items() if k != "evidence_sha256"})
    assert not evaluate_independent_returns(plan, values, benchmark=benchmark, unseen_evidence=unseen, interval_evidence=interval)["unseen_evidence_verified"]
    interval["intervals"][1]["entry_date"] = interval["intervals"][0]["entry_date"]
    interval["evidence_sha256"] = identity_hash({k: v for k, v in interval.items() if k != "evidence_sha256"})
    assert not evaluate_independent_returns(plan, values, benchmark=benchmark, interval_evidence=interval)["interval_evidence_verified"]


@pytest.mark.parametrize("field", ["open", "high", "low", "close", "volume"])
@pytest.mark.parametrize("value", [float("inf"), float("-inf"), float("nan"), 0])
def test_daily_bar_nonfinite_or_zero_is_not_a_trading_bar(field: str, value: float) -> None:
    from quantdesk.data_truth.daily_bar_validity import daily_bar_validity_from_row
    row = dict(open=10, high=10, low=10, close=10, volume=1000)
    row[field] = value
    assert daily_bar_validity_from_row(row)[0] is False
