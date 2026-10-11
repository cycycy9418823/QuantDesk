"""D5/T44：页面衔接——缺数、计划与成交、两账收益、AI 原判断与后续核验都要看得见。

全部合成夹具（临时 state/results），不联网、不调模型、不写正式账本。
"""

from __future__ import annotations

import json
from pathlib import Path

from ai_trader import dashboard


def _payload(tmp_path: Path, *, plan=None, journal=None, ledger=None, decision=None,
             dry_state=None) -> dict:
    state = tmp_path / "state" / "ai_trader"
    results = tmp_path / "results" / "ai_trader"
    state.mkdir(parents=True, exist_ok=True)
    results.mkdir(parents=True, exist_ok=True)
    files = {
        "plan_latest.json": plan, "journal_latest.json": journal, "ledger.json": ledger,
        "decision_latest.json": decision, "daytime_dry_state.json": dry_state,
    }
    for name, payload in files.items():
        if payload is not None:
            (state / name).write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return dashboard.build_dashboard_payload(state_dir=state, results_dir=results)


def test_missing_files_are_labelled_not_hidden(tmp_path: Path) -> None:
    payload = _payload(tmp_path)

    state = payload["research_state"]
    assert state["markers"]["missing_data"]["present"] is True, state
    assert "缺数" in state["markers"]["missing_data"]["label"]
    assert payload["degraded"] is True


def test_plan_and_fills_and_book_are_shown_side_by_side(tmp_path: Path) -> None:
    ledger = {"cash": 8000.0, "equity": 10123.5, "positions": [], "trades": [], "realized_pnl": 123.5}
    review = {
        "actions": [
            {"action": "buy", "symbol": "600000", "status": "filled", "quantity": 100},
            {"action": "buy", "symbol": "600001", "status": "rejected", "reason": "现金不够"},
        ]
    }
    plan = {"committed": True, "intents": [{"symbol": "600000", "side": "buy", "quantity": 100}]}

    payload = _payload(tmp_path, plan=plan, ledger=ledger)
    (tmp_path / "state" / "ai_trader" / "review_latest.json").write_text(
        json.dumps(review, ensure_ascii=False), encoding="utf-8")
    payload = _payload(tmp_path, plan=plan, ledger=ledger)

    state = payload["research_state"]
    assert state["plan_vs_fills"]["committed_intents"] == 1, state["plan_vs_fills"]
    assert state["plan_vs_fills"]["filled"] >= 1 and state["plan_vs_fills"]["rejected_or_abandoned"] >= 1
    assert state["book"]["cash"] == 8000.0 and state["book"]["equity"] == 10123.5
    assert "not_applied" not in state["markers"], "部署状态不能写死在产品事实里"


def test_unverified_and_recheck_are_visible_without_rewriting_the_verdict(tmp_path: Path) -> None:
    decision = {
        "records": [
            {"decision_id": "a", "result": "陈述与数据不符", "statement_unverified": [{"kind": "x"}]},
            {"decision_id": "b", "result": "已到期 · 证据不足",
             "recheck": {"as_of": "2026-10-02", "result": "对"}},
            {"decision_id": "c", "result": "对"},
        ]
    }

    state = _payload(tmp_path, decision=decision)["research_state"]

    assert state["records"] == {"available": True, "total": 3, "unverified": 1, "rechecked": 1, "rechecked_results": ["对"]}
    assert state["markers"]["unverified_statements"]["present"] is True
    assert state["markers"]["recheck_present"]["present"] is True


def test_model_fallback_and_unknown_are_marked(tmp_path: Path) -> None:
    fallback = _payload(tmp_path / "f", plan={"llm": {"used": False, "unavailable_reason": "model_timeout"}})
    assert fallback["research_state"]["markers"]["model_fallback"]["present"] is True
    assert fallback["research_state"]["markers"]["model_unknown"]["present"] is False
    assert fallback["research_state"]["llm"]["mode"] == "rule_fallback"

    unknown = _payload(tmp_path / "u")
    assert unknown["research_state"]["markers"]["model_unknown"]["present"] is True


def test_st_unknown_fill_is_marked(tmp_path: Path) -> None:
    review = {"actions": [{"action": "buy", "symbol": "600000", "status": "filled",
                           "quantity": 100, "st_unknown": True}]}
    state_dir = tmp_path / "state" / "ai_trader"
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / "review_latest.json").write_text(json.dumps(review, ensure_ascii=False), encoding="utf-8")

    payload = _payload(tmp_path)

    assert payload["research_state"]["markers"]["unknown_identity"]["present"] is True


def test_dry_state_is_exposed(tmp_path: Path) -> None:
    state = _payload(tmp_path, dry_state={"observed_days": ["2026-10-06"], "completed_dry_day": "2026-10-06"})

    assert state["research_state"]["dry_state"]["completed_dry_day"] == "2026-10-06"
