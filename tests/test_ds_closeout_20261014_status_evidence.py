"""Process receipts and current business facts must retain distinct statuses."""
from __future__ import annotations

import json
from pathlib import Path
import subprocess
from unittest.mock import Mock

import pytest

from quantdesk.project_status import read_runtime_observations
from scripts.ops import write_health_line as health
from scripts.run_scheduled_task import run_task, task_log_path
from tests.test_project_status_ui import _run_status_ui

DAY = "2026-10-16"
PRIOR = "2026-10-15"


def _json(root: Path, rel: str, value: object) -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf8")
    return path


@pytest.mark.parametrize("changes", [
    {"valuation_date": PRIOR}, {"valuation_date": "2026-10-19"}, {"valuation_date": None},
    {"ok": False}, {"portfolio_review_valid": False},
])
def test_candidate_intake_today_does_not_prove_today_valuation(tmp_path: Path, changes: dict) -> None:
    _json(tmp_path, "results/auto_paper_portfolio_latest.json", {
        "ok": True, "valuation_date": DAY, "portfolio_review_valid": True,
        "candidate_intake": {"as_of": DAY}, "generated_at": DAY + "T20:00:00+08:00", **changes})
    cell, reason = health.strategy_cell(tmp_path, DAY)
    assert "✓" not in cell
    assert reason


def test_today_valuation_is_independent_of_old_candidate_intake(tmp_path: Path) -> None:
    values = {"cash":100000.0, "initial_cash":100000.0, "positions":[],
        "market_value":0.0, "total_equity":100000.0, "valuation_date":DAY,
        "portfolio_review_valid":True}
    _json(tmp_path, "state/auto_paper_portfolio.json", values)
    _json(tmp_path, "results/auto_paper_portfolio_latest.json", {
        "ok": True, **values, "candidate_intake": {"as_of": PRIOR}})
    assert health.strategy_cell(tmp_path, DAY)[0] == "策略账 ✓"


def _ai_books(root: Path, *, as_of: str = DAY, trades: object = None) -> None:
    for _label, rel in health.AI_LEDGERS:
        _json(root, rel, {"as_of": as_of, "trades": [] if trades is None else trades})
        _json(root, str(Path(rel).with_name("journal_latest.json")), {"as_of": DAY, "llm": {"used": True}})


@pytest.mark.parametrize("as_of", ["2026-10-19", "not-a-date", "2026-99-99"])
def test_ai_ledger_future_or_bad_date_is_not_current(tmp_path: Path, as_of: str) -> None:
    _ai_books(tmp_path, as_of=as_of)
    cell, reason = health.ai_cell(tmp_path, DAY)
    assert "✓" not in cell
    assert reason


@pytest.mark.parametrize("trades", ["bad", [None], [{"date": "invalid"}]])
def test_malformed_ai_trade_records_do_not_claim_zero_fills(tmp_path: Path, trades: object) -> None:
    _ai_books(tmp_path, trades=trades)
    cell, reason = health.ai_cell(tmp_path, DAY)
    assert "✓" not in cell
    assert "未知" in cell + reason or "未核验" in cell + reason


@pytest.mark.parametrize("records", [None, "bad", [None], [{"decision_date": DAY, "result": "unknown"}]])
def test_unreadable_review_content_is_not_zero_mismatches(tmp_path: Path, records: object) -> None:
    for _label, rel in health.AI_DECISIONS:
        payload = {"as_of": DAY}
        if records is not None:
            payload["records"] = records
        _json(tmp_path, rel, payload)
    cell, reason = health.review_cell(tmp_path, DAY)
    assert "✓" not in cell
    assert "未知" in cell + reason or "未核验" in cell + reason


def test_process_exit_zero_carries_explicit_unverified_business_outcome(tmp_path: Path) -> None:
    runner = Mock(return_value=subprocess.CompletedProcess([], 0))
    action = {"executable": "fixture-never-run", "arguments": "", "working_directory": str(tmp_path)}
    assert run_task(tmp_path, "fixture-health", "daily", action, runner=runner) == 0
    row = json.loads(task_log_path(tmp_path, "fixture-health").with_suffix(".latest.json").read_text(encoding="utf8"))
    assert row["status"] == "completed"  # launcher compatibility, not a business verdict
    assert row["process_status"] == "completed"
    assert row["business_status"] == "unverified"
    assert row["business_completed"] is None


def test_latest_runtime_date_keeps_older_document_date_separate(tmp_path: Path) -> None:
    _json(tmp_path, "results/ai_trader/cycle_latest.json", {"as_of": DAY, "plan": {"llm": {"used": True}}})
    for day, n in ((PRIOR, 24), (DAY, 48)):
        _json(tmp_path, f"data/snapshots/{day}/manifest.json", {
            "trade_date": day, "collected_slot_count": n, "expected_slot_count": 48})
    result = read_runtime_observations(tmp_path, report_date=PRIOR)
    assert result["snapshot"]["as_of"] == DAY
    assert result["snapshot"]["collected_slots"] == 48
    assert result["snapshot"]["document_report_date"] == PRIOR
    assert result["profiles"]["default"]["as_of"] == DAY


def test_missing_declared_snapshot_date_does_not_borrow_directory_date(tmp_path: Path) -> None:
    _json(tmp_path, f"data/snapshots/{DAY}/manifest.json", {"collected_slot_count": 48, "expected_slot_count": 48})
    result = read_runtime_observations(tmp_path, report_date=DAY)
    assert result["snapshot"]["collected_slots"] is None
    assert result["snapshot"]["expected_slots"] is None


def test_conflicting_cycle_and_plan_dates_do_not_claim_model_success(tmp_path: Path) -> None:
    _json(tmp_path, "results/ai_trader/cycle_latest.json", {
        "as_of": DAY, "plan": {"decision_date": PRIOR, "committed": True, "llm": {"used": True}}})
    result = read_runtime_observations(tmp_path)
    profile = result["profiles"]["default"]
    assert profile["model_replied"] is None
    assert "日期" in profile["message"]


@pytest.mark.parametrize("mode,expected,forbidden", [
    ("hold_only", "无新增交易意图", "固定规则接管"),
    ("unknown", "回应情况未知", "固定规则接管"),
])
def test_ui_distinguishes_hold_only_and_unknown_non_model_paths(mode: str, expected: str, forbidden: str) -> None:
    observations = {"snapshot": {}, "profiles": {"default": {
        "as_of": DAY, "model_replied": False, "decision_mode": mode,
        "message": "未取得模型回答"}}}
    script = "renderOwnerRuntimeSummary(" + json.dumps(observations, ensure_ascii=False) + ");console.log(JSON.stringify({html:document.getElementById('projectRuntimeSummary').innerHTML}));"
    result = _run_status_ui("vm.runInContext(" + json.dumps(script, ensure_ascii=False) + ",context);")
    assert expected in result["html"]
    assert forbidden not in result["html"]


@pytest.mark.parametrize("changes", [
    {"target_date": "2026-10-19"}, {"fresh_count": 89}, {"requested_count": True},
    {"missing_count": -1}, {"fresh_count": 90.5},
])
def test_minute_round_requires_current_date_and_consistent_counts(tmp_path: Path, changes: dict) -> None:
    _json(tmp_path, "results/intraday/intraday_refresh_latest.json", {
        "target_date": DAY, "requested_count": 90, "fresh_count": 90,
        "missing_count": 0, "provider_status": [], **changes})
    cell, reason = health.minute_cell(tmp_path, DAY)
    assert "✓" not in cell
    assert reason
