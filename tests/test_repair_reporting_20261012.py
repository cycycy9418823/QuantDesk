"""Regression evidence for the three report defects found on 2026-10-12."""
from __future__ import annotations

import copy
import json
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest

from quantdesk import daily_workflow_orchestrator as workflow
from quantdesk import plan_review_receipts as receipts
from quantdesk.paper import auto_paper_portfolio as paper
from tests.test_b5_formal_candidate_intake import prepare_root
from tests.test_plan_review_receipts import archive, make_plan


def calendar_between(previous: str, current: str) -> dict:
    day, last = date.fromisoformat(previous), date.fromisoformat(current)
    days = {}
    while day <= last:
        days[day.isoformat()] = day.weekday() < 5
        day += timedelta(days=1)
    return {"is_open_by_date": days}


def equity_row(day: str, equity: float) -> dict:
    return {"generated_at": f"{day}T20:05:00", "cash": equity,
            "market_value": 0.0, "total_equity": equity}


@pytest.mark.parametrize("previous,current,prior_equity,equity,expected", [
    ("2026-09-01", "2026-09-02", 100181.34, 98723.29, -1458.05),
    ("2026-10-09", "2026-10-12", 91698.89, 90953.11, -745.78),
])
def test_observed_equities_have_distinct_daily_and_cumulative_pnl(
    previous: str, current: str, prior_equity: float, equity: float, expected: float,
) -> None:
    state = {"initial_cash": 100000.0, "cash": equity, "positions": []}
    history = {"items": [equity_row(previous, prior_equity), equity_row(current, equity)]}
    result = paper._with_daily_pnl(paper._summary(state, []), valuation_date=current,
                                  history=history, calendar=calendar_between(previous, current))
    assert result["daily_pnl"] == expected
    assert result["cumulative_pnl"] == round(equity - 100000.0, 2)
    assert result["previous_equity_date"] == previous
    # Same-day records and repeated generation cannot turn the daily loss into zero.
    history["items"].append(equity_row(current, equity))
    rerun = paper._with_daily_pnl(paper._summary(state, []), valuation_date=current,
                                 history=history, calendar=calendar_between(previous, current))
    assert rerun == result


@pytest.mark.parametrize("history,calendar,valid,reason", [
    ({"items": []}, calendar_between("2026-10-09", "2026-10-12"), True, "missing_previous_equity"),
    ({"items": [equity_row("2026-10-08", 100000)]}, calendar_between("2026-10-08", "2026-10-12"), True, "missing_previous_equity"),
    ({"items": [equity_row("2026-10-09", 100000)]}, {"is_open_by_date": {}}, True, "previous_session_unverified"),
    ({"items": [equity_row("2026-10-09", 100000)]}, calendar_between("2026-10-09", "2026-10-12"), False, "current_valuation_unverified"),
])
def test_missing_reference_or_current_mark_is_not_zero(history: dict, calendar: dict, valid: bool, reason: str) -> None:
    result = paper._with_daily_pnl({"total_equity": 99000}, valuation_date="2026-10-12",
                                  history=history, calendar=calendar, valuation_valid=valid)
    assert result["daily_pnl"] is None
    assert result["daily_return_pct"] is None
    assert result["daily_pnl_status"] == reason


def intake(rows: list[dict], **changes) -> dict:
    return {"status": "passed", "target_date": "2026-10-12", "candidate_count": len(rows),
            "candidates": rows, **changes}


def test_formal_four_plus_two_overrides_skipped_and_old_strategy_counts() -> None:
    rows = [{"strategy_type": "long"}] * 4 + [{"strategy_type": "board"}] * 2
    payload = {"target_date": "2026-10-12", "formal_candidate_intake": intake(rows),
               "auto_strategy": {"status": "skipped", "summary": {"long_candidate_count": 99}}}
    summary = workflow._build_summary(payload)
    assert (summary["long_candidate_count"], summary["board_candidate_count"]) == (4, 2)
    assert summary["candidate_count_source"] == "formal_candidate_batch"
    report = workflow.build_daily_workflow_report({**payload, "summary": summary})
    assert "长线 4 只，打板 2 只" in report
    assert "当日正式候选 2026-10-12" in report


@pytest.mark.parametrize("changes", [{"status": "missing"}, {"status": "blocked"},
                                     {"target_date": "2026-10-09"}, {"candidate_count": 3}])
def test_missing_stale_or_invalid_formal_batch_never_looks_empty(changes: dict) -> None:
    payload = {"target_date": "2026-10-12", "formal_candidate_intake": intake([], **changes),
               "auto_strategy": {"long_candidates": [{"symbol": "600000"}]}}
    summary = workflow._build_summary(payload)
    assert summary["long_candidate_count"] is None
    assert summary["board_candidate_count"] is None
    assert "缺失或未核验" in workflow.build_daily_workflow_report({**payload, "summary": summary})


def test_verified_empty_batch_can_report_zero() -> None:
    summary = workflow._build_summary({"target_date": "2026-10-12", "formal_candidate_intake": intake([])})
    assert (summary["long_candidate_count"], summary["board_candidate_count"]) == (0, 0)


def test_unknown_pnl_renders_unknown_with_separate_cumulative_amount() -> None:
    portfolio = paper._summary({"initial_cash": 100000, "cash": 90953.11, "positions": []}, [])
    payload = {"auto_paper_portfolio": {"summary": portfolio}}
    summary = workflow._build_summary(payload)
    text = workflow.build_daily_workflow_report({**payload, "summary": summary})
    assert "今日盈亏：未核验" in text
    assert "累计盈亏：-9046.89" in text
    assert "今日盈亏：0" not in text


def test_real_workflow_receipt_adapter_supplies_aware_time(tmp_path: Path, monkeypatch) -> None:
    plan = make_plan("2026-10-12")
    archive(tmp_path, plan)
    monkeypatch.setattr(workflow, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(workflow, "AUTO_PAPER_PORTFOLIO_STATE_JSON", tmp_path / "state/book.json")
    monkeypatch.setattr(workflow, "now_text", lambda: "2026-10-12T20:06:00")
    monkeypatch.setattr(workflow, "utc_now_iso", lambda: "2026-10-12T12:06:00+00:00")
    result = workflow._review_due_plan_for_workflow("2026-10-12", simulation_enabled=False)
    assert result["plan_review_receipt_archive"]["status"] == "completed"
    loaded = receipts.load_plan_review_receipts(tmp_path)["review_receipts"]
    assert len(loaded) == 1
    assert datetime.fromisoformat(loaded[0]["checked_at"]).utcoffset() == timedelta(hours=8)
    assert loaded[0]["status"] == "evidence_insufficient"  # missing minutes remain visible
    with pytest.raises(ValueError, match="aware checked_at"):
        receipts.build_expiry_review_receipt(tmp_path, plan, checked_at="2026-10-12T20:06:00")


def test_public_formal_update_and_same_day_rerun_use_previous_session(tmp_path: Path, monkeypatch) -> None:
    prepare_root(tmp_path, monkeypatch)
    calendar = tmp_path / "data/calendar/a_share_trading_calendar.csv"
    calendar.parent.mkdir(parents=True, exist_ok=True)
    calendar.write_text("date,is_open\n2026-07-31,true\n2026-08-01,false\n2026-08-02,false\n2026-08-03,true\n", encoding="utf-8")
    state = paper._empty_state(100000.0)
    state.update(cash=98000.0, market_value=0.0, total_equity=98000.0, valuation_date="2026-07-31")
    paper.AUTO_PAPER_PORTFOLIO_STATE_JSON.parent.mkdir(parents=True, exist_ok=True)
    paper.AUTO_PAPER_PORTFOLIO_STATE_JSON.write_text(json.dumps(state), encoding="utf-8")
    monkeypatch.setattr(paper, "now_text", lambda: "2026-08-03T20:00:00")
    result = paper.run_auto_paper_portfolio(mode="update", review_target_date="2026-08-03", risk_gate_shadow_enabled=False)
    assert result["ok"]
    expected = round(result["summary"]["total_equity"] - 98000.0, 2)
    assert result["summary"]["daily_pnl"] == expected
    before = copy.deepcopy(result["summary"])
    result = paper.run_auto_paper_portfolio(mode="update", review_target_date="2026-08-03", risk_gate_shadow_enabled=False)
    assert result["summary"]["daily_pnl"] == before["daily_pnl"]
    assert result["summary"]["previous_equity_date"] == "2026-07-31"
