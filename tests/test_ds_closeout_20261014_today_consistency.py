"""Current-day displays must bind their quantities to the actual dated run."""
from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path

import pytest

from quantdesk import daily_workflow_orchestrator as workflow
from tests.test_project_status_ui import _run_status_ui

DAY = "2026-10-16"
PRIOR = "2026-10-15"


def dated_payload() -> dict:
    return {
        "target_date": DAY, "run_id": "fixture_today", "workflow_status": "completed_with_warnings",
        "generated_at": DAY + "T20:06:00+08:00",
        "formal_candidate_intake": {
            "status": "passed", "target_date": DAY, "candidate_count": 1,
            "candidates": [{"symbol": "600500", "strategy_type": "long"}],
            "source_run_id": "fixture_chain", "path": "fixture/formal_batch.json",
        },
        "auto_paper_portfolio": {
            "ok": True, "valuation_date": DAY,
            "summary": {"daily_pnl": -125.5, "daily_pnl_status": "verified",
                        "cumulative_pnl": -2000.0, "previous_equity_date": PRIOR},
        },
    }


@pytest.mark.parametrize("valuation_day,status", [(PRIOR, "valuation_date_mismatch"), (None, "valuation_date_unverified")])
def test_verified_label_without_this_day_valuation_is_not_today_profit(valuation_day, status):
    payload = dated_payload()
    payload["auto_paper_portfolio"]["valuation_date"] = valuation_day
    before = deepcopy(payload)
    summary = workflow._build_summary(payload)
    assert summary["today_pnl"] is None
    assert summary["today_pnl_status"] == status
    assert summary["cumulative_pnl"] == -2000
    assert "今日盈亏：-125.5" not in workflow.build_daily_workflow_report({**payload, "summary": summary})
    assert payload == before


@pytest.mark.parametrize("number", [float("nan"), float("inf"), True, "-125.5"])
def test_invalid_daily_amount_is_unknown_in_report(number):
    payload = dated_payload()
    payload["auto_paper_portfolio"]["summary"]["daily_pnl"] = number
    assert workflow._build_summary(payload)["today_pnl"] is None


def test_summary_and_report_use_same_dated_input_instead_of_old_cached_summary():
    payload = dated_payload()
    payload["summary"] = {"long_candidate_count": 99, "board_candidate_count": 9, "today_pnl": 0}
    report = workflow.build_daily_workflow_report(payload)
    assert "长线 1 只，打板 0 只" in report
    assert "今日盈亏：-125.5" in report
    assert "长线 99" not in report


def patch_latest(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(workflow, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(workflow, "REPORTS_DIR", tmp_path / "reports")
    monkeypatch.setattr(workflow, "DAILY_WORKFLOW_LATEST_JSON", tmp_path / "results/daily_workflow_latest.json")
    monkeypatch.setattr(workflow, "infer_target_date", lambda *a, **k: (DAY, "fixed_fixture"))
    workflow.DAILY_WORKFLOW_LATEST_JSON.parent.mkdir(parents=True)
    workflow.REPORTS_DIR.mkdir()
    return workflow.DAILY_WORKFLOW_LATEST_JSON


@pytest.mark.parametrize("content,expected", [("{broken", "unreadable"), ("[]", "invalid")])
def test_bad_latest_is_visible_and_cannot_resurrect_old_success(tmp_path, monkeypatch, content, expected):
    latest = patch_latest(tmp_path, monkeypatch)
    latest.write_text(content, encoding="utf8")
    old = workflow.REPORTS_DIR / "daily_workflow_report_old.md"
    old.write_text("OLD_SUCCESS_MARKER", encoding="utf8")
    before = {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    data = workflow.daily_workflow_latest()
    assert data["ok"] is False and data["artifact_status"] == expected
    rendered = workflow.daily_workflow_report_html()
    assert "OLD_SUCCESS_MARKER" not in rendered
    assert "无法" in rendered or "无效" in rendered
    assert before == {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}


def test_failed_current_run_preview_does_not_open_previous_run_report(tmp_path, monkeypatch):
    latest = patch_latest(tmp_path, monkeypatch)
    old = workflow.REPORTS_DIR / "daily_workflow_report_old.md"
    old.write_text("OLD_SUCCESS_MARKER", encoding="utf8")
    payload = {**dated_payload(), "ok": False, "workflow_status": "failed", "status": "failed",
               "report_path": "reports/daily_workflow_report_old.md", "errors": ["fixture_pipeline_interrupted"]}
    latest.write_text(json.dumps(payload), encoding="utf8")
    before = latest.read_bytes()
    data = workflow.daily_workflow_latest()
    assert data["artifact_status"] == "failed" and data["result_target_date"] == DAY
    page = workflow.daily_workflow_report_html()
    assert "OLD_SUCCESS_MARKER" not in page
    assert "failed" in page and DAY in page and "fixture_pipeline_interrupted" in page
    assert latest.read_bytes() == before


def test_previous_run_has_separate_result_and_current_target_dates(tmp_path, monkeypatch):
    latest = patch_latest(tmp_path, monkeypatch)
    payload = dated_payload()
    payload["target_date"] = PRIOR
    latest.write_text(json.dumps(payload), encoding="utf8")
    data = workflow.daily_workflow_latest()
    assert data["artifact_status"] == "stale"
    assert data["result_target_date"] == PRIOR and data["current_target_date"] == DAY
    assert data["generated_at"] == payload["generated_at"]
    assert DAY in workflow.daily_workflow_report_html() and PRIOR in workflow.daily_workflow_report_html()


def render_candidates(payload: dict) -> dict:
    script = (
        "const fixture=" + json.dumps(payload, ensure_ascii=False) + ";"
        "const before=JSON.stringify(fixture);"
        "renderHomeKpiCandidates(fixture);renderHomeCandidateChart(fixture);"
        "console.log(JSON.stringify({value:nodeText('kpiCandidatesValue'),sub:nodeText('kpiCandidatesSub'),"
        "note:nodeText('homeCandidateNote'),unchanged:before===JSON.stringify(fixture),requests}));"
    )
    return _run_status_ui("vm.runInContext(" + json.dumps(script, ensure_ascii=False) + ",context);")


def candidate_payload(**changes) -> dict:
    return {"candidate_generation": {
        "status": "completed", "signal_date": DAY, "target_date": DAY, "freshness_status": "current",
        "candidate_count": 6, "generated_batch": "fixture_batch", **changes,
    }, "today_recommendations": [{"symbol": "600500", "signal_date": DAY, "priority_score": 80}],
        "daily_focus": [{"symbol": "600501", "priority_score": 70}],
        "observation_candidates": [{"symbol": str(n), "priority_score": n} for n in range(18)]}


def test_home_count_does_not_replace_six_today_candidates_with_eighteen_old_observations():
    result = render_candidates(candidate_payload())
    assert result["value"] == "6 只"
    assert "历史观察 18" in result["sub"] and DAY in result["sub"]
    assert result["unchanged"] is True and result["requests"] == []


@pytest.mark.parametrize("changes", [
    {"status": "missing", "signal_date": "", "candidate_count": 0, "freshness_status": "missing"},
    {"status": "failed"}, {"signal_date": PRIOR, "freshness_status": "stale"},
    {"signal_date": "2026-10-19"}, {"candidate_count": None}, {"candidate_count": True},
    {"candidate_count": -1},
])
def test_missing_failed_stale_and_invalid_batches_do_not_claim_today_count(changes):
    result = render_candidates(candidate_payload(**changes))
    assert result["value"] == "—"
    assert "今日新生成" not in result["sub"]
    assert "今日无新候选" not in result["note"]
    assert result["unchanged"] is True and result["requests"] == []


def test_verified_empty_generation_and_old_observations_are_distinct():
    payload = candidate_payload(candidate_count=0)
    payload["today_recommendations"] = []
    result = render_candidates(payload)
    assert result["value"] == "0 只"
    assert "历史观察 18" in result["sub"]
    assert "历史观察" in result["note"]


def test_paper_home_exposes_missing_valuation_date_instead_of_using_watchlist_date():
    script = "document.getElementById('dutyStatusStrip').querySelectorAll=()=>[];renderHomeEquity({ok:true,as_of:'2026-10-16',account:{total_equity:98000,total_return_pct:-2,position_count:1},return_series:{items:[]}});console.log(JSON.stringify({sub:nodeText('kpiPaperSub')}));"
    result = _run_status_ui("vm.runInContext(" + json.dumps(script) + ",context);")
    assert "估值日期未提供" in result["sub"]
    assert "截至 2026-10-16" not in result["sub"]
