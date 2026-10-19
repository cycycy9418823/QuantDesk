"""Offline end-to-end checks for the AI decision boundary and retained history."""
from __future__ import annotations

import json
from copy import deepcopy
from dataclasses import fields, replace
from pathlib import Path

import pytest

from ai_trader import overnight
from ai_trader.ledger import save_ledger
from ai_trader.paths import ledger_id_for_path
from tests._ai_recovery_fixtures import (
    DECISION_DATE, TRADE_DATE, buy_intent, cycle_paths, empty_ledger, plan, seed_root, stub_evening,
)


@pytest.mark.parametrize("profile", ["", "glm"])
@pytest.mark.parametrize("outcome", ["transport:RemoteDisconnected", "unparseable_json", "model_hold"])
@pytest.mark.parametrize("prompt_candidate", [None, "independent_v1"])
def test_evening_failure_holds_but_prior_plan_settles_and_marks(tmp_path, monkeypatch, profile, outcome, prompt_candidate):
    prior = plan(plan_id="before_failure", intents=[buy_intent("600500", quantity=100, ref_close=5.0)])
    root = seed_root(tmp_path, plan_obj=prior, ledger=empty_ledger(), symbols={
        "600500": {"prev_close": 5.0, "open": 5.08, "close": 5.10},
        "600501": {"prev_close": 5.0, "open": 5.0, "close": 5.0},
    })
    runtime = cycle_paths(root)
    original_default = runtime.ledger.read_bytes()
    if profile:
        changes = {}
        for field in fields(runtime):
            value = getattr(runtime, field.name)
            if field.name not in {"daily_dir", "minute_dir"}:
                changes[field.name] = value.parent.with_name("ai_trader_" + profile) / value.name
        runtime = replace(runtime, **changes)
        book = empty_ledger()
        book["ledger_id"] = ledger_id_for_path(runtime.ledger)
        save_ledger(book, runtime.ledger)
        overnight.save_plan(prior, runtime.plan_latest)
    monkeypatch.setenv("AI_TRADER_PROFILE", profile)
    stub_evening(monkeypatch, overnight, overnight.settle_plan, stub_model=False, symbols=["600500", "600501"])
    monkeypatch.setattr(overnight, "pick_operating_method", lambda _: ({"hypothesis_id": "reversal_5"}, "live"))
    monkeypatch.setattr(overnight, "rank_today", lambda *a: [("600501", 10.0)])
    monkeypatch.setattr(overnight, "_story_broken", lambda *a: "a rule would sell")

    def answer(context, diagnostics, **kwargs):
        if outcome == "model_hold":
            return {"buys": [], "sells": [], "model": "offline_fixture", "market_note": ""}
        diagnostics["reason"] = outcome
        return None

    monkeypatch.setattr(overnight, "propose_picks", answer)
    if prompt_candidate:
        def no_research_recommendations(**kwargs):
            raise AssertionError("independent menu must not read strategy recommendations")
        monkeypatch.setattr(overnight, "load_recommendations", no_research_recommendations)
    payload = overnight.run_evening(paths=runtime, network_news=False, prompt_candidate=prompt_candidate)
    book = json.loads(runtime.ledger.read_text(encoding="utf-8"))
    assert any(t.get("plan_id") == "before_failure" for t in book["trades"])
    assert book["positions"][0]["symbol"] == "600500"
    assert book["positions"][0]["market_price"] == 5.10
    assert book["ledger_id"] == ledger_id_for_path(runtime.ledger)
    from ai_trader.evaluation_contract import with_fill_evidence
    prior_record = {"plan_id": "before_failure", "ledger_id": book["ledger_id"], "symbol": "600500",
                    "side": "buy", "trade_date": prior["trade_date"]}
    assert with_fill_evidence([prior_record], book)[0]["fill_evidence"]["status"] == "confirmed"
    if outcome != "model_hold":
        assert payload["plan"]["intents"] == []
        assert payload["plan"]["decision_mode"] == "hold_only"
        assert payload["plan"]["llm"]["used"] is False
        assert payload["plan"]["llm"]["unavailable_reason"] == outcome
        assert "无新增交易" in payload["journal"]["headline"]
    else:
        assert payload["plan"]["llm"]["used"] is True
        if prompt_candidate:
            assert payload["plan"]["intents"] == [], "Model hold must not be replaced by a rule sell in candidate mode"
        else:
            assert all(row["decision_origin"] == "rule" for row in payload["plan"]["intents"])
    assert payload["plan"]["prompt_selection"] == (prompt_candidate or "active")
    if profile:
        assert (root / "state/ai_trader/ledger.json").read_bytes() == original_default


def test_repeated_judgment_revision_is_deduplicated(tmp_path):
    from ai_trader.decision_log import upsert_decision_records
    path = tmp_path / "decisions.jsonl"
    original = {"decision_id": "one", "decision_date": "2026-09-10", "judgment": "first", "recorded_at": "2026-09-10T20:20:00+08:00"}
    upsert_decision_records(path, [original])
    revision = {**original, "judgment": "revised", "structured_at": "2026-10-12T20:20:00+08:00"}
    first = upsert_decision_records(path, [revision])
    repeated = upsert_decision_records(path, [{**revision, "structured_at": "2026-10-12T20:21:00+08:00"}])
    assert len(first) == len(repeated) == 2
    assert repeated[0]["judgment"] == "first"


def _evaluation_case(tmp_path):
    from ai_trader import decision_log as dl
    from ai_trader.evaluation_contract import with_fill_evidence

    daily = tmp_path / "daily"
    daily.mkdir()
    (daily / "600000.csv").write_text(
        "date,symbol,open,high,low,close,volume,amount,pct_chg,adjust\n"
        "2026-09-01,600000,10,10,10,10,10000,100000,0,raw\n"
        "2026-09-02,600000,10,10.5,10,10.5,10000,100000,5,raw\n"
        "2026-09-03,600000,10.5,10.5,9.5,9.5,10000,100000,-9.52,raw\n", encoding="utf-8")
    plan_obj = {"plan_id": "declared-plan", "ledger_id": "fixture-book",
                "decision_date": "2026-09-01", "trade_date": "2026-09-02", "committed": True,
                "recorded_at": "2026-09-01T20:20:00+08:00", "llm": {"used": True, "model": "offline-fixture"}}
    intent = {"symbol": "600000", "side": "buy", "ref_close": 10, "reason": "研究判断",
              "decision_origin": "model", "verification": {"kind": "stand_above", "n_sessions": 2,
                                                             "level": 10.4, "price_basis": "raw"}}
    record = dl.build_decision_record(intent, plan=plan_obj, daily_dir=daily)
    ledger = {"ledger_id": "fixture-book", "trades": [
        {"plan_id": "declared-plan", "date": "2026-09-02", "symbol": "600000", "side": "buy",
         "quantity": 100, "price": 10, "price_evidence": {"price_basis": "raw", "trade_date": "2026-09-02",
                    "side": "buy", "price": 10, "evidence_id": "fixture-original-raw"}}]}
    return daily, with_fill_evidence([record], ledger)[0], ledger


def test_evidenced_model_early_hit_waits_for_maturity(tmp_path):
    from ai_trader import decision_log as dl

    daily, record, _ = _evaluation_case(tmp_path)
    early = dl.score_decision(record, daily_dir=daily, as_of="2026-09-02")
    assert early["result"] == dl.RESULT_PENDING
    assert early["result_evidence"][0]["kind"] == "early_hit"
    assert dl.decision_review_summary([early], as_of="2026-09-02")["credible_model"]["denominator"] == 0
    mature = dl.score_decision(early, daily_dir=daily, as_of="2026-09-03")
    assert mature["result"] == dl.RESULT_RIGHT
    summary = dl.decision_review_summary([mature], as_of="2026-09-03")
    assert summary["credible_model"] == {"denominator": 1, "correct": 1, "accuracy": 1.0}


@pytest.mark.parametrize("mutation,reason", [
    ({"decision_origin": "rule"}, "origin_not_model"),
    ({"llm_model": ""}, "model_identity_missing"),
    ({"recorded_at": "2026-09-01T20:20:00"}, "original_timestamp_missing"),
    ({"recorded_at": "2026-09-02T10:00:00+08:00"}, "declaration_not_before_window"),
    ({"recorded_at_source": "capture"}, "original_timestamp_missing"),
    ({"fill_evidence": {"status": "unconfirmed"}}, "filled_trade_unconfirmed"),
    ({"backfill": True}, "historical_review"),
    ({"committed": False}, "unaccepted"),
])
def test_incomplete_or_nonmodel_record_never_enters_accuracy(tmp_path, mutation, reason):
    from ai_trader import decision_log as dl

    daily, record, _ = _evaluation_case(tmp_path)
    mature = dl.score_decision(record, daily_dir=daily, as_of="2026-09-03")
    mature.update(mutation)
    original = deepcopy(mature)
    summary = dl.decision_review_summary([mature], as_of="2026-09-03")
    assert summary["credible_model"]["denominator"] == 0
    assert reason in summary["record_context"][mature["decision_id"]]["exclusion_reasons"]
    assert mature == original


def test_program_derived_and_legacy_early_verdict_are_retained_but_excluded(tmp_path):
    from ai_trader import decision_log as dl

    daily, record, _ = _evaluation_case(tmp_path)
    record["result"] = dl.RESULT_RIGHT
    record["result_as_of"] = "2026-09-02"
    record["verification"]["source"] = "derived_from_declared_horizon"
    unchanged = dl.score_decision(record, daily_dir=daily, as_of="2026-09-03")
    assert unchanged == record
    context = dl.decision_review_summary([unchanged], as_of="2026-09-03")["record_context"][record["decision_id"]]
    assert "verification_not_model_declared" in context["exclusion_reasons"]
    assert "verdict_before_maturity" in context["exclusion_reasons"]


def test_early_hit_with_missing_final_bar_does_not_pass(tmp_path):
    from ai_trader import decision_log as dl

    daily, record, _ = _evaluation_case(tmp_path)
    path = daily / "600000.csv"
    path.write_text("\n".join(path.read_text(encoding="utf-8").splitlines()[:-1]) + "\n", encoding="utf-8")
    scored = dl.score_decision(record, daily_dir=daily, as_of="2026-09-03")
    assert scored["result"] == dl.RESULT_EXPIRED_NO_EVIDENCE
    assert dl.decision_review_summary([scored], as_of="2026-09-03")["credible_model"]["denominator"] == 0


@pytest.mark.parametrize("field,value", [("plan_id", "wrong"), ("date", "2026-09-03"),
                                           ("side", "sell"), ("symbol", "600001")])
def test_fill_join_requires_exact_trade_identity(tmp_path, field, value):
    from ai_trader.evaluation_contract import with_fill_evidence

    _, record, ledger = _evaluation_case(tmp_path)
    ledger["trades"][0][field] = value
    assert with_fill_evidence([record], ledger)[0]["fill_evidence"]["status"] == "unconfirmed"


def test_plan_receipt_alone_or_wrong_ledger_never_proves_a_fill(tmp_path):
    from ai_trader.evaluation_contract import with_fill_evidence

    _, record, ledger = _evaluation_case(tmp_path)
    record["price_evidence"] = ledger["trades"][0]["price_evidence"]
    assert with_fill_evidence([record], {"ledger_id": "fixture-book", "trades": []})[0]["fill_evidence"]["status"] == "unconfirmed"
    ledger["ledger_id"] = "other-profile"
    assert with_fill_evidence([record], ledger)[0]["fill_evidence"]["reason"] == "ledger_identity_mismatch"


def test_explicit_candidate_removes_research_inputs_and_versions_actual_content(tmp_path):
    from ai_trader.prompt_comparison import compare_prompt_packs, write_prompt_comparison
    from ai_trader.prompt_pack import PROMPT_DIR, load_prompt_pack

    active_bytes = (PROMPT_DIR / "versions.json").read_bytes()
    context = {"as_of": "2026-09-01", "trade_date": "2026-09-02", "candidates": ["600000"],
               "recommendations": ["DESK_SENTINEL"], "method_results": ["RULE_SENTINEL"],
               "evidence_notes": ["BASELINE_SENTINEL"]}
    result = compare_prompt_packs(context)
    assert result["model_called"] is False and result["active_selection_changed"] is False
    for marker in ("DESK_SENTINEL", "RULE_SENTINEL", "BASELINE_SENTINEL"):
        assert marker in result["variants"]["active"]["user"]
        assert marker not in result["variants"]["candidate"]["user"]
    assert result["variants"]["candidate"]["prompt_version"] != result["variants"]["active"]["prompt_version"]
    assert result["variants"]["candidate"]["effective_prompt_sha256"] != result["variants"]["active"]["effective_prompt_sha256"]
    assert load_prompt_pack().selection == "active"
    output = write_prompt_comparison(context, tmp_path / "comparison")
    assert Path(output["comparison"]).is_file()
    assert (PROMPT_DIR / "versions.json").read_bytes() == active_bytes


def test_model_verification_survives_original_response_parse_and_invalid_is_not_inferred():
    from ai_trader.llm import propose_picks

    condition = {"kind": "stand_above", "n_sessions": 2, "level": 10.4, "price_basis": "raw"}
    result = propose_picks({}, reuse_text=json.dumps({"buys": [{"symbol": "600000", "verification": condition}]}),
                           reuse_model="fixture")
    assert result["buys"][0]["verification"] == condition
    invalid = propose_picks({}, reuse_text=json.dumps({"buys": [{"symbol": "600000", "verification": {"level": 10}}]}))
    assert invalid["buys"][0]["verification"] is None
    assert invalid["prompt_sha256"] == "", "An old response cannot inherit current prompt hashes"


def test_candidate_empty_book_model_buy_does_not_require_rule_hypothesis(tmp_path, monkeypatch):
    root = seed_root(tmp_path, plan_obj=plan(plan_id="empty-fixture", intents=[]), ledger=empty_ledger(), symbols={
        "600500": {"prev_close": 5.0, "open": 5.0, "close": 5.0}})
    monkeypatch.setattr(overnight, "_news_digest", lambda *a, **k: {"status": "missing", "headlines": []})
    monkeypatch.setattr(overnight, "_session_note", lambda *a, **k: {"minutes": "missing", "path_comment": ""})
    condition = {"kind": "stand_above", "level": 5.2, "n_sessions": 2, "price_basis": "raw"}
    kwargs = dict(decision_date=DECISION_DATE, trade_date=TRADE_DATE, ledger=empty_ledger(), panel=None,
                  ranked=[("600500", 1.0)], live=None, daily_dir=root / "data/daily", minute_dir=root / "data/minute",
                  network_news=False, llm_directed=True,
                  pick_reasons={"600500": {"reason": "研究判断", "verification": condition}})
    active = overnight.build_plan(**kwargs)
    candidate = overnight.build_plan(**kwargs, independent_candidate=True)
    assert active["intents"] == [], "Active successful-model behaviour stays unchanged pending §4"
    assert len(candidate["intents"]) == 1
    assert candidate["intents"][0]["decision_origin"] == "model"
    assert candidate["intents"][0]["verification"] == condition


@pytest.mark.parametrize("profile", ["default", "glm"])
def test_daytime_focus_is_profile_isolated_and_failure_clears_active_request(tmp_path, monkeypatch, profile):
    from datetime import datetime
    from ai_trader import daytime_decide, daytime_runner
    from ai_trader.minute_focus import focus_path, read_minute_focus
    from ai_trader.ledger import empty_ledger as new_book
    from ai_trader.paths import CyclePaths

    now = datetime.fromisoformat("2026-09-22T09:45:10+08:00")
    state = tmp_path / "state" / ("ai_trader" if profile == "default" else "ai_trader_" + profile)
    paths = CyclePaths(ledger=state / "ledger.json", plan_latest=state / "plan_latest.json",
                       watchlist=state / "watchlist.json", daytime_intents=state / "daytime_intents.jsonl",
                       daytime_notes=state / "daytime_notes.jsonl")
    save_ledger(new_book(ledger_id=ledger_id_for_path(paths.ledger)), paths.ledger)
    before = paths.ledger.read_bytes()
    monkeypatch.setenv(daytime_decide.DAYTIME_ENV, "1")
    def model(*a, **k):
        return daytime_decide.ModelCall(text=json.dumps({"want_minutes": ["600000", "600111", "600000"], "orders": []}),
                                        ok=True, model="deepseek-fixture" if profile == "glm" else "kimi-fixture")
    rows = [{"symbol": "600000", "price": 10, "pct_chg": 1, "quote_time": "2026-09-22T09:45:00+08:00"}]
    kwargs = dict(profile=profile, now=now, clock=lambda: now, slot="0945", rows=rows,
                  quote_time=rows[0]["quote_time"], source="cloud_slot", paths=paths, dry=True, idle_first_day=False)
    preview = daytime_runner.run_slot(tmp_path, **kwargs, llm=model, write=False)
    assert preview["minute_focus"]["symbols"] == ["600000"]
    assert not focus_path(paths).exists()
    result = daytime_runner.run_slot(tmp_path, **kwargs, llm=model)
    focus = result["minute_focus"]
    assert focus["profile"] == profile and focus["source"] == "model_want_minutes"
    assert focus["status"] == "active" and focus["symbols"] == ["600000"]
    assert focus["consumer_eligible"] is False, "A suggestion does not upgrade the ledger trust"
    assert focus["expires_at"] == "2026-09-22T10:15:10+08:00"
    assert read_minute_focus(paths, now=now)["status"] == "active"
    assert read_minute_focus(paths, now=datetime.fromisoformat("2026-09-22T10:16:00+08:00"))["status"] == "expired"
    failed = daytime_runner.run_slot(tmp_path, **kwargs,
                                   llm=lambda *a, **k: daytime_decide.ModelCall(ok=False, error="offline-timeout"))
    assert failed["minute_focus"]["status"] == "unavailable"
    assert json.loads(focus_path(paths).read_text(encoding="utf-8"))["symbols"] == []
    assert paths.ledger.read_bytes() == before


@pytest.mark.parametrize("stamp", ["2026-09-21T09:45:00+08:00", "2026-09-22T09:30:00+08:00",
                                   "2026-09-22T09:50:00+08:00", "2026-09-22T09:45:00", ""])
def test_minute_focus_rejects_stale_future_or_unzoned_quotes(tmp_path, stamp):
    from datetime import datetime
    from ai_trader.minute_focus import build_minute_focus
    from ai_trader.ledger import empty_ledger as new_book
    from ai_trader.paths import CyclePaths

    paths = CyclePaths(ledger=tmp_path / "ai_trader/ledger.json")
    save_ledger(new_book(ledger_id=ledger_id_for_path(paths.ledger)), paths.ledger)
    now = datetime.fromisoformat("2026-09-22T09:45:10+08:00")
    decision = {"status": "decided", "model_used": True, "want_minutes_model": "fixture", "as_of": "2026-09-22",
                "slot": "0945", "model_completed_at": now.isoformat(), "want_minutes": ["600000"]}
    focus = build_minute_focus(runtime=paths, profile="default", decision=decision, now=now,
                               visible_symbols=["600000"], quote_rows=[{"symbol": "600000", "price": 10, "quote_time": stamp}])
    assert focus["symbols"] == [] and focus["status"] == "empty"

