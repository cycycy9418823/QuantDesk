from __future__ import annotations

import json
from pathlib import Path

import pytest

from quantdesk import daily_workflow_orchestrator as workflow
from quantdesk import data_freshness_gate as freshness
from tests.test_round18_candidate_envelope_wiring import TARGET_DATE, _patch_daily_workflow


def _unexpected(*args, **kwargs):
    raise AssertionError("unrequested legacy research or direct daily writer ran")


def _setup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    return _patch_daily_workflow(
        monkeypatch, tmp_path,
        formal_load_result={"status": "missing", "batch": None, "path": "", "reason_codes": ["missing"]},
    )


def test_default_review_skips_legacy_research_without_losing_due_plan_review(tmp_path, monkeypatch):
    formal_calls, portfolio_calls = _setup(tmp_path, monkeypatch)
    for name in ("_low_price_status", "_run_low_price_report", "run_auto_strategy_cycle", "build_daily_watchpool_step", "build_daily_features", "update_daily_prices", "intraday_gap_status"):
        monkeypatch.setattr(workflow, name, _unexpected)
    result = workflow.run_daily_workflow(with_guardrails=False, target_date=TARGET_DATE)
    steps = {step["name"]: step for step in result["steps"]}
    assert result["ok"] is True
    assert result["scope"] == "paper_positions_and_due_plans"
    assert len(formal_calls) == len(portfolio_calls) == 1
    assert portfolio_calls[0]["review_target_date"] == TARGET_DATE
    assert "auto_strategy_payload" not in portfolio_calls[0]
    for name in ("low_price_pool_check", "low_price_report", "auto_strategy_sandbox", "composite_watchpool", "market_brain_shadow", "strategy_lab"):
        assert steps[name]["status"] == "skipped"
    assert result["input_sources"]["auto_strategy"] == "not_run"
    assert result["input_sources"]["auto_paper_portfolio"] == "dated_formal_candidate_batch_and_due_plan"
    assert result["daily_price_status"]["status"] == "not_applicable"
    assert result["daily_price_status"]["latest_local_date"] == ""
    assert steps["intraday_gap_check"]["outputs"]["reason"] == "no_execution_symbols"
    assert not {
        "auto_strategy_latest", "daily_price_update_latest", "conditional_trade_plans_latest",
        "data_health", "guardrails", "command_center", "intraday_gap_status_latest", "daily_features",
    }.intersection(result["artifacts"])
    assert "daily_features" in result["input_artifacts"]
    report_path = (tmp_path / result["report_path"]).resolve()
    assert report_path.is_relative_to(tmp_path)
    assert report_path.is_file()


@pytest.mark.parametrize("fails", [False, True])
def test_explicit_legacy_research_remains_available_and_non_blocking(tmp_path, monkeypatch, fails):
    _, portfolio_calls = _setup(tmp_path, monkeypatch)
    old_runner = workflow.run_auto_strategy_cycle
    calls = []

    def run(**kwargs):
        calls.append(kwargs)
        if fails:
            raise RuntimeError("legacy fixture unavailable")
        return old_runner(**kwargs)

    monkeypatch.setattr(workflow, "run_auto_strategy_cycle", run)
    result = workflow.run_daily_workflow(with_guardrails=False, run_legacy_research=True, target_date=TARGET_DATE)
    assert len(calls) == len(portfolio_calls) == 1
    step = next(s for s in result["steps"] if s["name"] == "auto_strategy_sandbox")
    assert step["status"] == ("warning" if fails else "ok")
    assert result["ok"] is True
    assert "auto_strategy_payload" not in portfolio_calls[0]


def test_missing_held_price_cannot_be_replaced_by_legacy_pool_date(tmp_path, monkeypatch):
    _, portfolio_calls = _setup(tmp_path, monkeypatch)
    workflow.AUTO_PAPER_PORTFOLIO_STATE_JSON.write_text(
        json.dumps({"positions": [{"symbol": "600000", "shares": 100}]}), encoding="utf-8",
    )
    monkeypatch.setattr(freshness, "_low_price_report_date", _unexpected)
    monkeypatch.setattr(workflow, "update_daily_prices", _unexpected)
    result = workflow.run_daily_workflow(
        with_guardrails=False, target_date=TARGET_DATE, allow_daily_price_online_update=True,
    )
    assert portfolio_calls == []
    assert result["data_freshness_gate"]["portfolio_review_valid"] is False
    assert result["daily_price_status"]["missing_count"] == 1
    assert result["daily_price_update"]["reason"] == "pipeline_writer_required"
    assert result["network"] is False
    assert next(s for s in result["steps"] if s["name"] == "portfolio_update")["status"] == "skipped"
    assert result["input_sources"]["auto_paper_portfolio"] == "not_run"


def test_consistency_check_uses_this_review_without_latest_fallback(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    calls = []
    monkeypatch.setattr(workflow, "_run_daily_paper_consistency_check", lambda **kw: calls.append(kw) or {"ok": True, "status": "passed"})
    result = workflow.run_daily_workflow(with_guardrails=False, target_date=TARGET_DATE)
    assert calls[0]["target_date"] == TARGET_DATE
    assert calls[0]["paper_payload"] is result["auto_paper_portfolio"]
    assert calls[0]["execution_payload"] == result["auto_paper_portfolio"].get("conditional_execution", {})
    assert calls[0]["intraday_payload"]["status"] == "not_applicable"


def test_consistency_wrapper_forwards_explicit_empty_inputs(tmp_path, monkeypatch):
    from quantdesk.paper import paper_vs_backtest_consistency as consistency

    calls = []
    monkeypatch.setattr(consistency, "run_paper_vs_backtest_consistency_check", lambda **kw: calls.append(kw) or {"status": "passed"})
    workflow._run_daily_paper_consistency_check(
        target_date=TARGET_DATE, input_dir=tmp_path, output_dir=tmp_path, report_dir=tmp_path,
        paper_payload={}, execution_payload={}, intraday_payload={},
    )
    assert all(calls[0][key] == {} for key in ("paper_payload", "execution_payload", "intraday_payload"))


def test_holdings_review_does_not_require_research_features(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    daily = workflow.DAILY_DIR
    (daily / "600000.csv").write_text(
        "date,open,high,low,close,volume,price_basis\n" + f"{TARGET_DATE},10,11,9,10.5,1000,raw\n",
        encoding="utf-8",
    )
    state = {"positions": [{"symbol": "600000", "shares": 100}]}
    monkeypatch.setattr(freshness, "_low_price_report_date", _unexpected)
    monkeypatch.setattr(freshness, "daily_feature_status", _unexpected)
    result = freshness.evaluate_paper_review_freshness(state, target_date=TARGET_DATE, daily_dir=daily)
    assert result["portfolio_review_valid"] is True
    assert result["local_daily_latest_date"] == TARGET_DATE
    assert result["strategy_features_required"] is False


def test_review_cli_legacy_research_is_explicit():
    from scripts.run_daily_workflow import build_parser

    parser = build_parser()
    assert parser.parse_args([]).run_legacy_research is False
    assert parser.parse_args(["--run-legacy-research"]).run_legacy_research is True


@pytest.mark.parametrize("reason, expected", [
    ("not_requested", "completed"), ("no_existing_positions", "completed"),
    ("no_execution_symbols", "completed"), ("missing_prices", "partial"), ("", "partial"),
])
def test_optional_skip_does_not_mask_missing_data_or_create_false_failure(reason, expected):
    steps = [{"status": "ok"}, {"status": "skipped", "outputs": {"reason": reason}}]
    assert workflow._workflow_status(steps, []) == expected
    assert workflow._workflow_status(steps + [{"status": "warning"}], []) == "partial"


def test_minute_review_uses_only_holdings_and_dated_due_plan(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    workflow.AUTO_PAPER_PORTFOLIO_STATE_JSON.write_text(
        json.dumps({"positions": [{"symbol": "600000", "shares": 100}]}), encoding="utf-8",
    )
    (workflow.DAILY_DIR / "600000.csv").write_text(
        f"date,open,high,low,close,volume,price_basis\n{TARGET_DATE},10,11,9,10,1000,raw\n",
        encoding="utf-8",
    )
    archive = workflow.RESULTS_DIR / "conditional_trade_plans_daily" / f"{TARGET_DATE}.json"
    monkeypatch.setattr(workflow, "load_archived_conditional_plan", lambda *a, **kw: {
        "status": "passed", "archive_path": str(archive),
        "plan": {"trade_date": TARGET_DATE, "symbols": [{"symbol": "600001"}]},
    })
    calls = []

    def gap(**kwargs):
        calls.append(kwargs)
        return {"ok": True, "total_symbols": 2, "available_count": 1, "stale_count": 1}

    monkeypatch.setattr(workflow, "intraday_gap_status", gap)
    result = workflow.run_daily_workflow(with_guardrails=False, target_date=TARGET_DATE)
    assert calls[0]["source"] == "symbols"
    assert calls[0]["symbols"] == ["600000", "600001"]
    assert calls[0]["target_trade_date"] == TARGET_DATE
    assert calls[0]["plan_path"] == archive
    assert calls[0]["write"] is False
    assert calls[0]["intraday_dir"].is_relative_to(tmp_path)
    assert next(s for s in result["steps"] if s["name"] == "intraday_gap_check")["status"] == "warning"
