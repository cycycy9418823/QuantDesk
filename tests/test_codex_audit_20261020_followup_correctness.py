"""Recorded-day follow-up: percentage periods, PnL gaps and delayed archives."""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
from pathlib import Path

import pandas as pd
import pytest

from ai_trader import decision_log as decisions
from quantdesk.paper import auto_paper_portfolio as paper
from quantdesk.paper import ledger_read_contract as ledger_contract
from scripts.ops import write_health_line as health

TARGET = "2026-09-04"


@pytest.fixture
def daily(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    directory = tmp_path / "daily"
    directory.mkdir()
    days = ("2026-08-28", "2026-08-31", "2026-09-01", "2026-09-02", "2026-09-03", TARGET)
    closes = (100, 102, 104, 108, 116, 120)
    rows = []
    previous = closes[0]
    for day, close in zip(days, closes):
        rows.append({"date": day, "symbol": "600000", "open": close, "high": close,
                     "low": close, "close": close, "volume": 1000, "adjust": "raw",
                     "pct_chg": (close / previous - 1) * 100})
        previous = close
    pd.DataFrame(rows).to_csv(directory / "600000.csv", index=False)
    monkeypatch.setattr(decisions, "load_a_share_trading_calendar", lambda: {
        "is_open_by_date": {day: True for day in days}})
    return directory


@pytest.mark.parametrize("note", [
    "今天候选表极度两极：涨得最凶的(600000 +20%)",
    "今日候选名单：600000+20%",
    "2026-09-04候选清单：600000+20%",
])
def test_list_date_is_not_a_percentage_period(daily: Path, note: str) -> None:
    hits = decisions.verify_market_note(note, decision_date=TARGET, daily_dir=daily)
    assert hits and all(hit.get("evidence_status") == "unverified" for hit in hits)
    assert hits[0]["kind"] == "claimed_pct_ambiguous_period"
    assert hits[0]["claimed"] == 20
    assert "actual" not in hits[0]


@pytest.mark.parametrize("note", ["今天600000+20%", "今日候选600000当日涨20%"])
def test_explicit_daily_claim_still_cannot_borrow_five_day_return(daily: Path, note: str) -> None:
    hits = decisions.verify_market_note(note, decision_date=TARGET, daily_dir=daily)
    assert len(hits) == 1
    assert hits[0]["actual"] == pytest.approx(3.448275862)
    assert hits[0].get("evidence_status") != "unverified"


@pytest.mark.parametrize("reason", [
    "5天涨20%", "5日涨20%", "过去5个交易日涨20%",
    "最近5个交易日涨20%", "今日候选600000近5个交易日涨20%",
])
def test_explicit_numbered_return_period_uses_its_own_evidence(daily: Path, reason: str) -> None:
    hits = decisions.verify_intent_against_daily(
        {"symbol": "600000", "side": "buy", "ref_close": 120, "reason": reason},
        decision_date=TARGET, daily_dir=daily,
    )
    assert hits == []


@pytest.mark.parametrize("field", ["收盘", "开盘"])
def test_dated_list_keeps_an_explicit_single_day_price(daily: Path, field: str) -> None:
    note = f"今日候选600000{field}120元"
    assert decisions.verify_market_note(note, decision_date=TARGET, daily_dir=daily) == []
    hits = decisions.verify_market_note(
        f"今日候选600000{field}999元", decision_date=TARGET, daily_dir=daily,
    )
    assert len(hits) == 1
    assert hits[0]["actual"] == 120
    assert hits[0].get("evidence_status") != "unverified"


def test_ambiguous_comment_does_not_change_the_buy_or_sell_permission(daily: Path) -> None:
    plan = {
        "decision_date": TARGET,
        "llm": {"used": True, "market_note": "今日候选600000+20%"},
        "intents": [
            {"symbol": "600000", "side": "buy", "ref_close": 120},
            {"symbol": "600000", "side": "sell", "ref_close": 120,
             "reason": "近期涨20%"},
        ],
    }
    result = decisions.apply_gate_to_plan(plan, daily_dir=daily)
    assert [row["side"] for row in plan["intents"]] == ["buy", "sell"]
    assert result.market_note_ok is False
    assert result.market_note_status == decisions.RESULT_UNVERIFIED
    assert plan["intents"][1]["statement_label"] == decisions.STATEMENT_FLAG_UNVERIFIED


def account(*, difference: float = 0) -> dict:
    return {
        "initial_cash": 100000.0, "cash": 99500.0 + difference,
        "market_value": 600.0, "total_equity": 100100.0 + difference,
        "realized_pnl": 0.0, "unrealized_pnl": 100.0,
        "legacy_realized_adjustment": 0.0,
        "valuation_date": TARGET, "portfolio_review_valid": True,
        "positions": [{"symbol": "600000", "quantity": 100, "entry_price": 5.0,
                       "latest_price": 6.0, "latest_date": TARGET, "market_value": 600.0,
                       "unrealized_pnl": 100.0, "valuation_status": "normal",
                       "valuation_price_basis": "raw", "management_state": "normal",
                       "watch_status": "normal"}],
        "closed_positions": [], "realized_reductions": [], "actions": [],
        "accounting_reconciliation": {"status": "consistent"},
    }


def dump(path: Path, data: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


def test_unexplained_pnl_gap_does_not_get_hidden_by_consistent_detail_totals() -> None:
    original = account(difference=3.04)
    before = deepcopy(original)
    result = paper.reconcile_paper_account(original)
    assert original == before
    for key in ("cash", "initial_cash", "market_value", "total_equity", "realized_pnl",
                "unrealized_pnl", "legacy_realized_adjustment", "positions"):
        assert result[key] == original[key]
    check = result["accounting_reconciliation"]
    assert check["status"] == "unverified"
    assert check["pnl_identity"]["difference"] == pytest.approx(3.04)
    assert check["pnl_identity"]["status"] == "mismatch"
    assert "paper_account_pnl_identity_mismatch" in result["warnings"]


def test_pnl_gap_summary_and_health_are_visible_without_changing_account(tmp_path: Path) -> None:
    original = account(difference=3.04)
    dump(tmp_path / "state/auto_paper_portfolio.json", original)
    dump(tmp_path / "results/auto_paper_portfolio_latest.json", {
        **original, "ok": True, "generated_at": TARGET + "T20:00:00+08:00",
        "candidate_intake": {"as_of": TARGET},
    })
    before = {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    summary = paper._summary(original, [])
    assert summary["total_equity"] == 100103.04
    assert summary["pnl_identity"]["difference"] == pytest.approx(3.04)
    assert summary["accounting_status"] == "unverified"
    cell, explanation = health.strategy_cell(tmp_path, TARGET)
    assert cell == "策略账 ✗"
    assert "3.04" in explanation and "尚未核对完成" in explanation
    assert before == {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}


def test_balanced_account_stays_healthy_and_unchanged() -> None:
    original = account()
    result = paper.reconcile_paper_account(original)
    assert result["accounting_reconciliation"]["status"] == "consistent"
    assert result["accounting_reconciliation"]["pnl_identity"]["status"] == "consistent"
    assert ledger_contract.strategy_ledger_health(original, target_date=TARGET)["ok"] is True
    assert paper._summary(original, [])["accounting_status"] == "consistent"


def test_missing_pnl_components_are_not_supplied_as_zero() -> None:
    original = account()
    original.pop("realized_pnl")
    check = ledger_contract.strategy_pnl_identity(original)
    assert check["status"] == "missing"
    assert check["difference"] is None
    assert check["missing_fields"] == ["realized_pnl"]
    assert "realized_pnl" not in original


@pytest.mark.parametrize("field", ["initial_cash", "realized_pnl", "unrealized_pnl"])
def test_summary_keeps_missing_accounting_evidence_unverified(field: str) -> None:
    original = account()
    original.pop(field)
    before = deepcopy(original)
    summary = paper._summary(original, [])
    assert original == before
    assert summary["accounting_status"] == "unverified"
    assert summary["pnl_identity"]["status"] == "missing"
    assert summary["pnl_identity"]["difference"] is None
    assert summary["pnl_identity"]["missing_fields"] == [field]


def test_summary_does_not_clear_a_declared_pnl_gap_by_recomputing_details() -> None:
    original = account()
    original["realized_pnl"] = 50
    before = deepcopy(original)
    summary = paper._summary(original, [])
    assert original == before
    assert summary["accounting_status"] == "unverified"
    assert summary["pnl_identity"]["status"] == "mismatch"
    assert summary["pnl_identity"]["difference"] == -50


def test_persisted_snapshot_identity_uses_current_summary_and_keeps_original_gap(
    tmp_path: Path, monkeypatch,
) -> None:
    prior = account(difference=3.04)
    current = deepcopy(prior)
    current["positions"][0].update(latest_price=5.5, market_value=550.0, unrealized_pnl=50.0)
    # The assembled snapshot has today's cash/positions and yesterday's declared
    # PnL fields. The published summary already carries today's derived totals.
    summary = paper._summary(current, [])
    assert summary["pnl_identity"]["difference"] == pytest.approx(-46.96)
    snapshot = {
        **current, "ok": True, "generated_at": TARGET + "T20:00:00+08:00",
        "summary": summary,
    }
    original = deepcopy(snapshot)
    monkeypatch.setattr(paper, "AUTO_PAPER_PORTFOLIO_LATEST_JSON", tmp_path / "latest.json")
    result = paper._write_auto_paper_latest(snapshot)
    assert snapshot == original
    assert result["summary"]["pnl_identity"]["difference"] == pytest.approx(3.04)
    assert result["summary"]["pnl_identity"]["equity_from_pnl"] == 100050.0
    assert result["summary"]["accounting_status"] == "unverified"
    for key in ("cash", "market_value", "total_equity", "realized_pnl", "unrealized_pnl", "initial_cash"):
        assert result["summary"][key] == summary[key]
    assert result["positions"] == original["positions"]
    assert result["actions"] == original["actions"]
    assert json.loads((tmp_path / "latest.json").read_text(encoding="utf-8"))["summary"] == result["summary"]


@pytest.mark.parametrize("field", ["initial_cash", "realized_pnl", "unrealized_pnl"])
def test_persisted_summary_identity_never_supplies_a_missing_declared_component(
    tmp_path: Path, monkeypatch, field: str,
) -> None:
    current = account()
    summary = paper._summary(current, [])
    summary.pop(field)
    snapshot = {**current, "summary": summary}
    original = deepcopy(snapshot)
    monkeypatch.setattr(paper, "AUTO_PAPER_PORTFOLIO_LATEST_JSON", tmp_path / "latest.json")
    result = paper._write_auto_paper_latest(snapshot)
    assert snapshot == original
    assert field not in result["summary"]
    assert result["summary"]["pnl_identity"]["status"] == "missing"
    assert result["summary"]["pnl_identity"]["missing_fields"] == [field]
    assert result["summary"]["accounting_status"] == "unverified"


def test_persisted_summary_keeps_its_declared_conflict_with_position_details(
    tmp_path: Path, monkeypatch,
) -> None:
    current = account()
    summary = paper._summary(current, [])
    summary["realized_pnl"] = 50.0
    monkeypatch.setattr(paper, "AUTO_PAPER_PORTFOLIO_LATEST_JSON", tmp_path / "latest.json")
    result = paper._write_auto_paper_latest({**current, "summary": summary})
    assert result["summary"]["realized_pnl"] == 50.0
    assert result["summary"]["pnl_identity"]["difference"] == -50.0
    assert result["summary"]["accounting_status"] == "unverified"


def test_delayed_snapshot_preserves_business_day_and_receipt_time(tmp_path: Path) -> None:
    day = tmp_path / "data/snapshots/2026-10-20"
    day.mkdir(parents=True)
    manifest = {"trade_date": "2026-10-20", "expected_slot_count": 48,
                "collected_slot_count": 48, "completed_slot_count": 48,
                "field_verified_complete_slot_count": 0, "partial_slot_count": 0}
    dump(day / "manifest.json", manifest)
    for index in range(48):
        (day / f"{index:04d}.csv").write_text("symbol,price\n600000,10\n", encoding="utf-8")
    signature = hashlib.sha256((day / "manifest.json").read_bytes()).hexdigest()
    path = tmp_path / "logs/ops/snapshot_pull.log"
    dump(path, {"date": "2026-10-20", "published": True,
                "pulled_at": "2026-10-21T09:32:47+08:00", "manifest_sha256": signature})
    before = {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    cell, explanation = health.snapshot_cell(tmp_path, "2026-10-20")
    assert "48张" in cell and "10-21 09:32" in cell
    assert "行情日期仍是 2026-10-20" in explanation
    assert "字段核实 0" in cell
    assert before == {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
