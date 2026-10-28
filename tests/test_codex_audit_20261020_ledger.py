"""Consumer regressions for AU-02, AU-04 and AU-07; all inputs are detached."""
from __future__ import annotations

import copy
import json
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest

from quantdesk.paper import auto_paper_portfolio as paper
from scripts.ops.write_health_line import strategy_cell

TARGET = "2026-10-19"


def dump(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")


def test_valid_today_ledger_health_is_independent_of_old_candidate_intake(tmp_path: Path) -> None:
    values = {"cash":100000.0, "initial_cash":100000.0, "positions":[],
        "market_value":0.0, "total_equity":100000.0, "valuation_date":TARGET,
        "portfolio_review_valid":True}
    dump(tmp_path / "state/auto_paper_portfolio.json", values)
    dump(tmp_path / "results/auto_paper_portfolio_latest.json", {
        "ok":True, **values, "candidate_intake":{"as_of":"2026-10-16"}})
    assert strategy_cell(tmp_path, TARGET) == ("策略账 ✓", "")
    (tmp_path / "state/auto_paper_portfolio.json").unlink()
    cell, reason = strategy_cell(tmp_path, TARGET)
    assert cell == "策略账 ✗" and reason


@pytest.fixture
def isolated(tmp_path: Path, monkeypatch):
    source = Path(__file__).resolve().parents[1]
    for name, value in list(vars(paper).items()):
        if isinstance(value, Path) and value.is_relative_to(source):
            monkeypatch.setattr(paper, name, tmp_path / value.relative_to(source))
    monkeypatch.setattr(paper, "load_formal_candidate_intake", lambda *_a, **_k: {
        "ok": True, "status": "passed", "as_of": TARGET, "input_source": "synthetic_fixture",
        "reason_codes": [], "path": None,
    })
    monkeypatch.setattr(paper, "formal_candidate_strategy_payload", lambda _i: {
        "ok": True, "generated_at": TARGET + "T20:00:00+08:00", "strategies": [],
    })
    monkeypatch.setattr(paper, "candidate_rows_from_strategy_payload", lambda *_a, **_k: [])
    fresh = {"ok": True, "status": "passed", "target_complete_trade_date": TARGET,
             "can_update_portfolio_valuation": True, "can_review_yesterday_positions": True,
             "portfolio_review_valid": True, "warnings": []}
    monkeypatch.setattr(paper, "evaluate_data_freshness", lambda *_a, **_k: copy.deepcopy(fresh))
    monkeypatch.setattr(paper, "evaluate_paper_review_freshness", lambda *_a, **_k: copy.deepcopy(fresh))
    monkeypatch.setattr(paper, "apply_delisting_liquidation", lambda state, **_k: {
        "state": state, "actions": [], "warnings": [], "liquidated": 0,
    })
    return tmp_path


def state_with_position(root: Path, *, price: float = 5.86) -> dict:
    state = paper._empty_state(100000)
    state.update(cash=99445.73, valuation_date="2026-10-14", portfolio_review_valid=True)
    state["positions"] = [{
        "symbol": "600121", "name": "Synthetic A", "strategy_type": "long",
        "quantity": 100, "entry_price": 5.5427, "entry_cash_out": 554.27,
        "entry_date": "2026-10-13", "latest_price": price, "latest_date": "2026-10-14",
        "market_value": round(price * 100, 2), "unrealized_pnl": round((price - 5.5427) * 100, 2),
        "valuation_status": "normal", "valuation_price_basis": "raw",
    }]
    state = paper.reconcile_paper_account(state)
    dump(root / "state/auto_paper_portfolio.json", state)
    return state


def daily(root: Path, *, price: float = 4.88, volume: float = 1000,
          basis: str = "raw", day: str = TARGET) -> None:
    path = root / "data/daily/600121.csv"
    path.parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame([
        {"date": "2026-10-14", "symbol": "600121", "open": 5.86, "high": 5.9,
         "low": 5.8, "close": 5.86, "volume": 1000, "amount": 5860, "adjust": basis},
        {"date": day, "symbol": "600121", "open": price, "high": price, "low": price,
         "close": price, "volume": volume, "amount": price * volume, "adjust": basis,
         "pre_close": 5.42, "pct_chg": (price / 5.42 - 1) * 100},
    ]).to_csv(path, index=False)


def update() -> dict:
    return paper._run_auto_paper_portfolio_unlocked(
        mode="update", initial_cash=100000, dry_run=True,
        plan_date=TARGET, review_target_date=TARGET, risk_gate_shadow_enabled=False,
    )


def test_missing_funded_ledger_with_history_never_becomes_new_capital(isolated: Path) -> None:
    dump(paper.AUTO_PAPER_PORTFOLIO_HISTORY_JSON, {"items": [{"valuation_date": TARGET, "total_equity": 90000}]})
    loaded = paper._load_state(100000)
    assert loaded["accounting_reconciliation"]["status"] == "invalid"
    assert loaded.get("cash") != 100000
    assert update()["ok"] is False
    assert not paper.AUTO_PAPER_PORTFOLIO_STATE_JSON.exists()


def test_corrupt_ledger_repeated_read_preserves_bytes_and_failure(isolated: Path) -> None:
    path = paper.AUTO_PAPER_PORTFOLIO_STATE_JSON
    path.parent.mkdir(parents=True)
    path.write_bytes(b"{corrupt funded ledger")
    before = path.read_bytes()
    for _ in range(3):
        loaded = paper._load_state(100000)
        assert loaded["accounting_reconciliation"]["status"] == "invalid"
        assert loaded.get("cash") != 100000
        assert path.read_bytes() == before
    assert update()["ok"] is False
    assert list(path.parent.iterdir()) == [path]


def test_old_quarantine_is_permanent_initialization_evidence(isolated: Path) -> None:
    dump(paper.AUTO_PAPER_PORTFOLIO_STATE_JSON.with_name("auto_paper_portfolio.bad_old.json"), {})
    assert paper._load_state(100000)["accounting_reconciliation"]["status"] == "invalid"
    assert update()["ok"] is False


def test_update_of_never_initialized_book_requires_explicit_init(isolated: Path) -> None:
    assert update()["ok"] is False
    preview = paper._run_auto_paper_portfolio_unlocked(mode="init", dry_run=True,
        initial_cash=100000, plan_date=TARGET, review_target_date=TARGET, risk_gate_shadow_enabled=False)
    assert preview["ok"] is True
    assert preview["summary"]["cash"] == 100000
    assert not paper.AUTO_PAPER_PORTFOLIO_STATE_JSON.exists()


@pytest.mark.parametrize("payload", [{}, [], {"initial_cash": 100000, "positions": []}])
def test_empty_or_incomplete_existing_ledger_is_not_initialized(isolated: Path, payload: object) -> None:
    dump(paper.AUTO_PAPER_PORTFOLIO_STATE_JSON, payload)
    before = paper.AUTO_PAPER_PORTFOLIO_STATE_JSON.read_bytes()
    assert update()["ok"] is False
    assert paper.AUTO_PAPER_PORTFOLIO_STATE_JSON.read_bytes() == before


def test_current_valid_ledger_keeps_original_money_and_bytes(isolated: Path) -> None:
    state = state_with_position(isolated)
    before = paper.AUTO_PAPER_PORTFOLIO_STATE_JSON.read_bytes()
    loaded = paper._load_state(10000)
    assert loaded["cash"] == state["cash"]
    assert loaded["initial_cash"] == 100000
    assert loaded["positions"][0]["quantity"] == 100
    assert paper.AUTO_PAPER_PORTFOLIO_STATE_JSON.read_bytes() == before


@pytest.mark.parametrize("kind", ["valid", "wrong_identity", "bad_money", "missing_identity"])
def test_backup_is_only_recoverable_with_matching_account_evidence(isolated: Path, kind: str) -> None:
    state = state_with_position(isolated)
    backup = paper.AUTO_PAPER_PORTFOLIO_STATE_JSON.with_name("auto_paper_portfolio.backup_old.json")
    if kind != "missing_identity":
        paper._auto_paper_state_store().write(state)
    for old in paper._auto_paper_state_store()._backups():
        old.unlink()
    candidate = copy.deepcopy(state)
    if kind == "wrong_identity":
        candidate["created_at"] = "different-account"
    elif kind == "bad_money":
        candidate["cash"] = float("nan")
    dump(backup, candidate)
    path = paper.AUTO_PAPER_PORTFOLIO_STATE_JSON
    path.write_bytes(b"{broken current")
    before = {p: p.read_bytes() for p in path.parent.iterdir()}
    result = paper._auto_paper_state_store().read_snapshot()
    assert result.status == ("recovery_available" if kind == "valid" else "invalid")
    assert update()["ok"] is False
    assert before == {p: p.read_bytes() for p in path.parent.iterdir()}


def test_direct_writer_refuses_missing_previously_initialized_book(isolated: Path) -> None:
    state = state_with_position(isolated)
    paper._auto_paper_state_store().write(state)
    paper.AUTO_PAPER_PORTFOLIO_STATE_JSON.unlink()
    with pytest.raises(ValueError, match="explicit_recovery"):
        paper._auto_paper_state_store().write(paper._empty_state(100000))


def test_real_normal_sell_keeps_costs_and_cash_identity(isolated: Path) -> None:
    state_with_position(isolated)
    daily(isolated, price=5.05)
    result = update()
    assert result["positions"] == []
    closed = result["closed_positions"][0]
    assert closed["quantity"] == 100
    assert closed["exit_price"] == 5.05
    assert result["summary"]["cash"] == round(99445.73 + closed["net_amount"], 2)
    assert closed["total_fee"] > 0


def test_limit_down_refusal_marks_holding_at_current_raw_close(isolated: Path) -> None:
    state_with_position(isolated)
    daily(isolated)
    before = paper.AUTO_PAPER_PORTFOLIO_STATE_JSON.read_bytes()
    result = update()
    assert result["ok"] is True
    row = result["positions"][0]
    assert row["quantity"] == 100
    assert row["latest_price"] == 4.88
    assert row["latest_date"] == TARGET
    assert row["market_value"] == 488
    assert result["summary"]["total_equity"] == 99933.73
    assert not any(item["action"] == "sell" for item in result["actions"])
    assert any("跌停" in item.get("blocked_reason", "") for item in result["actions"])
    assert paper.AUTO_PAPER_PORTFOLIO_STATE_JSON.read_bytes() == before


@pytest.mark.parametrize("kind", ["missing", "stale", "qfq", "suspended"])
def test_absent_or_unusable_mark_never_claims_valid_today(isolated: Path, kind: str) -> None:
    state_with_position(isolated)
    if kind != "missing":
        daily(isolated, day="2026-10-16" if kind == "stale" else TARGET,
              basis="qfq" if kind == "qfq" else "raw", volume=0 if kind == "suspended" else 1000)
    result = update()
    assert result["portfolio_review_valid"] is False
    assert result["positions"][0]["valuation_status"] != "normal"
    assert not any(item["action"] == "sell" for item in result["actions"])


def healthy_snapshot(root: Path, *, stale_position: bool = False) -> None:
    state = state_with_position(root)
    state["valuation_date"] = TARGET
    state["positions"][0]["latest_date"] = "2026-10-14" if stale_position else TARGET
    dump(root / "state/auto_paper_portfolio.json", state)
    dump(root / "results/auto_paper_portfolio_latest.json", {
        **state, "ok": True, "generated_at": TARGET + "T20:00:00+08:00",
        "candidate_intake": {"as_of": TARGET}, "summary": {k: state[k] for k in ("initial_cash", "cash", "market_value", "total_equity")},
    })
    daily(root, price=5.86)


@pytest.mark.parametrize("kind", ["missing", "corrupt", "stale_position", "wrong_equity"])
def test_today_candidate_does_not_make_invalid_strategy_book_green(isolated: Path, kind: str) -> None:
    healthy_snapshot(isolated, stale_position=kind == "stale_position")
    path = paper.AUTO_PAPER_PORTFOLIO_STATE_JSON
    if kind == "missing":
        path.unlink()
    elif kind == "corrupt":
        path.write_bytes(b"{bad")
    elif kind == "wrong_equity":
        state = json.loads(path.read_text(encoding="utf-8"))
        state["total_equity"] += 10
        dump(path, state)
    before = {p: p.read_bytes() for p in isolated.rglob("*") if p.is_file()}
    text, explanation = strategy_cell(isolated, TARGET)
    assert text == "策略账 ✗"
    assert explanation
    assert before == {p: p.read_bytes() for p in isolated.rglob("*") if p.is_file()}


def test_health_accepts_same_date_reconciled_book(isolated: Path) -> None:
    healthy_snapshot(isolated)
    assert strategy_cell(isolated, TARGET)[0] == "策略账 ✓"


@pytest.mark.parametrize("receipt_status,expected", [
    ("completed_all_executed", "healthy"), ("completed_with_skips", "healthy"),
    ("failed", "last_run_failed"), ("running", "running"), ("historical", "last_run_unknown"),
])
def test_scheduler_uses_current_pipeline_and_keeps_disabled_legacy_history(
    isolated: Path, receipt_status: str, expected: str,
) -> None:
    from quantdesk.daily_scheduler_status import daily_data_scheduler_status
    calls = []
    dump(isolated / "results/data_close_pipeline/data_close_pipeline_latest.json", {
        "schema_version": "data_close_pipeline_v1", "run_id": "synthetic-test-run",
        "target_date": "2026-10-16" if receipt_status == "historical" else TARGET,
        "status": "completed_with_skips" if receipt_status == "historical" else receipt_status,
        "skipped_stages": ["health_alert"] if receipt_status == "completed_with_skips" else [],
    })

    def runner(command, **_kwargs):
        script = " ".join(command)
        current = "TaskName 'QuantDesk Data Close Pipeline'" in script
        calls.append("pipeline" if current else "legacy")
        value = {"task_name": "QuantDesk Data Close Pipeline" if current else "QuantDesk Daily Data Chain",
                 "exists": True, "managed": True, "enabled": current,
                 "state": "Ready" if current else "Disabled", "last_task_result": 0,
                 "last_run_time": TARGET + "T19:00:00+08:00", "next_run_time": "2026-10-20T15:40:00+08:00",
                 "execute": str(isolated / ".venv/Scripts/python.exe") if current else "cmd.exe",
                 "arguments": f'-B "{isolated / "scripts/run_data_close_pipeline.py"}" --enable-network --trigger scheduled --enable-alert-network --alert-push' if current else "",
                 "working_directory": str(isolated), "enabled_trigger_count": 2, "trigger_count": 2}
        return SimpleNamespace(returncode=0, stdout=json.dumps(value), stderr="")

    result = daily_data_scheduler_status(project_root=isolated, platform_name="win32", runner=runner, target_date=TARGET)
    assert "pipeline" in calls
    assert result["task_name"] == "QuantDesk Data Close Pipeline"
    assert result["status"] == expected
    assert result["completion_status"] == ("completed_with_skips" if receipt_status == "historical" else receipt_status)
    assert result["read_only"] is True


def test_dashboard_card_obeys_actual_ledger_status(isolated: Path) -> None:
    from quantdesk.data_health_dashboard import _portfolio_card
    result = _portfolio_card({"exists": True, "ok": True, "portfolio_review_valid": True,
                              "ledger_health": {"ok": False}},
                             {"portfolio_review_valid": True}, daily_status="green")
    assert result["status"] == "red"


def test_latest_read_does_not_hide_corrupt_ledger(isolated: Path) -> None:
    healthy_snapshot(isolated)
    paper.AUTO_PAPER_PORTFOLIO_STATE_JSON.write_bytes(b"{bad")
    before = paper.AUTO_PAPER_PORTFOLIO_STATE_JSON.read_bytes()
    result = paper.auto_paper_portfolio_latest()
    assert result["portfolio_review_valid"] is False
    assert result["ledger_health"]["ok"] is False
    assert paper.AUTO_PAPER_PORTFOLIO_STATE_JSON.read_bytes() == before


@pytest.mark.parametrize("kind", ["healthy", "missing", "corrupt", "stale_position", "old_account"])
def test_cached_health_refreshes_actual_ledger_without_scan_or_write(
    isolated: Path, monkeypatch, kind: str,
) -> None:
    from quantdesk import data_health_dashboard as dashboard
    healthy_snapshot(isolated, stale_position=kind == "stale_position")
    monkeypatch.setattr(dashboard, "PROJECT_ROOT", isolated)
    monkeypatch.setattr(dashboard, "RESULTS_DIR", isolated / "results")
    monkeypatch.setattr(dashboard, "daily_data_scheduler_status", lambda **_k: {"status": "healthy"})
    monkeypatch.setattr(dashboard, "_snapshot_age_seconds", lambda _date: 0)
    monkeypatch.setattr(dashboard, "_build_scope_health_block", lambda *_a, **_k: {})
    monkeypatch.setattr(dashboard, "daily_price_status", lambda *_a, **_k: pytest.fail("full CSV scan"))
    state_path = paper.AUTO_PAPER_PORTFOLIO_STATE_JSON
    if kind == "missing":
        state_path.unlink()
    elif kind == "corrupt":
        state_path.write_bytes(b"{bad ledger")
    elif kind == "old_account":
        for path in (state_path, paper.AUTO_PAPER_PORTFOLIO_LATEST_JSON):
            state = json.loads(path.read_text(encoding="utf-8"))
            state["valuation_date"] = "2026-10-14"
            state["positions"][0]["latest_date"] = "2026-10-14"
            dump(path, state)
    snapshot = isolated / "cached_health.json"
    dump(snapshot, {"ok": True, "health_level": "green", "generated_at": TARGET,
                    "cards": {"daily_data": {"status": "green"}, "portfolio_review": {"status": "green"}},
                    "source_status": {"freshness": {"portfolio_review_valid": True}}})
    before = {p: p.read_bytes() for p in isolated.rglob("*") if p.is_file()}
    result = dashboard.build_cached_data_health_dashboard(
        snapshot_path=snapshot, target_date=TARGET,
        daily_data_chain_payload={"status": "completed_all_executed", "target_date": TARGET},
        evidence_freshness_payload={"status": "passed", "metrics_trustworthy": True},
    )
    assert result["health_level"] == ("green" if kind == "healthy" else "red")
    assert result["cards"]["portfolio_review"]["status"] == ("green" if kind == "healthy" else "red")
    if kind in {"healthy", "stale_position", "old_account"}:
        assert result["source_status"]["auto_paper_portfolio"]["ledger_health"]["target_date"] == TARGET
    assert result["network"] is False and result["full_scan"] is False
    assert before == {p: p.read_bytes() for p in isolated.rglob("*") if p.is_file()}
