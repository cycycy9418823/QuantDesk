"""Actual new decision records must carry the original response and data chain."""
from __future__ import annotations

from copy import deepcopy
from pathlib import Path
import hashlib
import json
from datetime import datetime

import pandas as pd
import pytest

from ai_trader import decision_log as dl


def new_record(tmp_path: Path, *, observation: bool = False) -> dict:
    daily = tmp_path / "daily"
    daily.mkdir(exist_ok=True)
    pd.DataFrame([dict(date=d, symbol="600000", open=p, high=p, low=p, close=p,
        volume=10000, amount=p * 10000, price_basis="raw", adjust="raw", source="offline_fixture")
        for d, p in [("2026-09-01", 10), ("2026-09-02", 10.5), ("2026-09-03", 10.6)]]).to_csv(daily / "600000.csv", index=False)
    verification = dict(kind="stand_above", n_sessions=2, level=10.4, price_basis="raw",
        evaluation_mode="price_observation" if observation else "filled_trade")
    original = json.dumps({"buys": [dict(symbol="600000", reason="原判断", style="短线",
        verification=verification)], "sells": []}, ensure_ascii=False)
    plan = dict(plan_id="plan-1", ledger_id="ai_trader_100k", decision_date="2026-09-01",
        trade_date="2026-09-02", recorded_at="2026-09-01T20:20:00+08:00", committed=True,
        input_evidence={"as_of": "2026-09-01", "observed_end": "2026-09-01",
            "sources": {"600000": {"status": "stable", "sha256": hashlib.sha256((daily / "600000.csv").read_bytes()).hexdigest()}},
            "price_policy_validation": {"eligible": True, "accepted_price_basis": "raw"}},
        llm={"used": True, "model": "requested-alias", "response_evidence": {
            "response_model": "actual-model", "raw_text": original, "text_status": "received",
            "observed_at": "2026-09-01T20:19:00+08:00", "call_state": "attempted"}})
    intent = dict(symbol="600000", side="buy", ref_close=10, decision_origin="model",
        llm_reason="原判断", style="短线", verification=verification)
    row = dl.build_decision_record(intent, plan=plan, daily_dir=daily)
    row["fill_evidence"] = {"status": "confirmed", "ledger_id": "ai_trader_100k", "trades": [
        dict(plan_id="plan-1", symbol="600000", side="buy", date="2026-09-02", quantity=100,
            price=10.5, price_basis="raw", evidence_id="fixture-fill")]}
    return dl.score_decision(row, daily_dir=daily, as_of="2026-09-03")


def test_new_record_automatically_matures_with_complete_evidence(tmp_path: Path) -> None:
    row = new_record(tmp_path)
    assert row["llm_model"] == "actual-model"
    assert row["evaluation_evidence"]["original_response_sha256"]
    assert row["result_evidence"][0]["source_sha256"]
    review = dl.decision_review_summary([row], as_of="2026-09-03")
    assert review["credible_model"]["denominator"] == 1
    assert review["model_style_groups"]["actual-model|短线"]["denominator"] == 1


def test_declared_observation_does_not_invent_a_fill(tmp_path: Path) -> None:
    row = new_record(tmp_path, observation=True)
    row.pop("fill_evidence")
    assert dl.decision_review_summary([row], as_of="2026-09-03")["credible_model"]["denominator"] == 1


@pytest.mark.parametrize("case", ["missing_response", "wrong_response_hash", "missing_data", "wrong_data_hash",
    "wrong_account", "missing_fill", "changed_condition", "changed_style", "missing_outcome_hash", "backfill", "failed_model"])
def test_incomplete_new_chain_never_enters_denominator(tmp_path: Path, case: str) -> None:
    row = new_record(tmp_path)
    evidence = row.get("evaluation_evidence", {})
    if case == "missing_response": evidence.pop("original_response", None)
    elif case == "wrong_response_hash": evidence["original_response_sha256"] = "0" * 64
    elif case == "missing_data": evidence.pop("input_evidence", None)
    elif case == "wrong_data_hash": evidence["input_evidence_sha256"] = "0" * 64
    elif case == "wrong_account": evidence["ledger_id"] = "ai_trader_100k_glm"
    elif case == "missing_fill": row.pop("fill_evidence")
    elif case == "changed_condition": row["verification"]["level"] = 9
    elif case == "changed_style": row["style"] = "长线"
    elif case == "missing_outcome_hash": row["result_evidence"][0].pop("source_sha256", None)
    elif case == "backfill": row["backfill"] = True
    else: row["llm_used"] = False
    assert dl.decision_review_summary([row], as_of="2026-09-03")["credible_model"]["denominator"] == 0


def test_duplicate_same_judgment_counts_once(tmp_path: Path) -> None:
    row = new_record(tmp_path)
    clone = deepcopy(row)
    clone["decision_id"] = "copied-id"
    review = dl.decision_review_summary([row, clone], as_of="2026-09-03")
    assert review["credible_model"]["denominator"] == 1


def test_conflicting_same_id_excludes_both_versions(tmp_path: Path) -> None:
    row = new_record(tmp_path)
    changed = deepcopy(row)
    changed["trade_date"] = "2026-09-04"
    assert dl.decision_review_summary([row, changed], as_of="2026-09-04")["credible_model"]["denominator"] == 0


@pytest.mark.parametrize("side", ["buy", "sell"])
def test_actual_model_parse_evening_plan_and_review_bind_original_style(tmp_path: Path, monkeypatch, side: str) -> None:
    import ai_trader.llm as llm
    import ai_trader.overnight as overnight
    from ai_trader.ledger import empty_ledger, save_ledger
    from tests.test_ai_trader_transport_diagnostics import _enable, _install, _seed

    _enable(monkeypatch, tmp_path)
    runtime = _seed(tmp_path)
    decision_day = "2026-09-21"
    class FrozenClock(datetime):
        @classmethod
        def now(cls, tz=None):
            value = datetime.fromisoformat(decision_day + "T20:20:00+08:00")
            return value.astimezone(tz) if tz is not None else value
    monkeypatch.setattr(llm, "datetime", FrozenClock)
    monkeypatch.setattr(overnight, "datetime", FrozenClock)
    monkeypatch.setattr(overnight, "_now_iso", lambda: decision_day + "T20:20:00+08:00")
    monkeypatch.setattr(overnight, "latest_as_of", lambda *_a: {"as_of": decision_day})
    monkeypatch.setattr(overnight, "focus_universe", lambda *_a, **_k: ["600500"])
    monkeypatch.setattr(overnight, "load_recommendations", lambda **_k: {"symbols": [], "rows": [], "warnings": []})
    monkeypatch.setattr(overnight, "pick_operating_method", lambda _research: (
        {"hypothesis_id": "offline-existing-method", "title": "离线原方法夹具"}, "probation"))
    if side == "sell":
        ledger = empty_ledger(created_at="2026-09-18T20:00:00+08:00")
        ledger["cash"] = 99000
        ledger["positions"] = [dict(symbol="600500", quantity=100, cost_price=5.0,
            market_price=5.0, market_value=500, acquired_on="2026-09-18", style="长期")]
        save_ledger(ledger, runtime.ledger)
    condition = dict(kind="stand_above", n_sessions=2, level=5.1, price_basis="raw",
        evaluation_mode="price_observation")
    original = json.dumps({"buys" if side == "buy" else "sells": [dict(symbol="600500",
        reason="等待后续价格观察", style="波段", hold_days=15, verification=condition)],
        "market_note": "仅作研究观察"}, ensure_ascii=False)
    calls = _install(monkeypatch, payload={"model": "actual-model", "choices": [{
        "message": {"content": original}, "finish_reason": "stop"}]})
    snapshot = overnight.run_evening(paths=runtime, network_news=False)
    plan = snapshot["plan"]
    assert calls == [llm.DEFAULT_TIMEOUT_S]
    intent = next(item for item in plan["intents"] if item["side"] == side)
    assert intent["style"] == "波段" and intent["decision_origin"] == "model"
    assert plan["input_evidence"]["as_of"] == decision_day
    row = dl.build_decision_record(intent, plan=plan, daily_dir=runtime.daily_dir)
    assert row["evaluation_evidence"]["original_response"] == original
    from ai_trader.evaluation_contract import verified_declaration
    assert verified_declaration(row)
    with (runtime.daily_dir / "600500.csv").open("a", encoding="utf-8") as handle:
        handle.write("2026-09-22,600500,5.12,5.2,5.1,5.16,1000,5160,1.5,raw,synthetic,2026-09-22T16:00:00+08:00\n")
        handle.write("2026-09-23,600500,5.16,5.3,5.1,5.18,1000,5180,0.4,raw,synthetic,2026-09-23T16:00:00+08:00\n")
    scored = dl.score_decision(row, daily_dir=runtime.daily_dir, as_of="2026-09-23")
    summary = dl.decision_review_summary([scored], as_of="2026-09-23")
    assert summary["credible_model"]["denominator"] == 1
    assert summary["model_style_groups"]["actual-model|波段"]["denominator"] == 1
