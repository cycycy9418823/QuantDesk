"""S2 daytime decide: protocol, parsing, slot rules and the two review additions.

Offline by construction: every test injects a fake model and a tmp_path state dir;
AI_TRADER_DAYTIME is set per-test and removed again.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from ai_trader import daytime_decide
from ai_trader.paths import CyclePaths

DAY = "2026-09-22"
NOW = datetime(2026, 9, 22, 9, 42, 30)


def _paths(tmp_path: Path) -> CyclePaths:
    state = tmp_path / "state" / "ai_trader"
    state.mkdir(parents=True, exist_ok=True)
    (state / "plan_latest.json").write_text(json.dumps({
        "decision_date": "2026-09-21",
        "intents": [{"symbol": "600111", "side": "buy", "thesis": "低估修复"}],
    }), encoding="utf-8")
    return CyclePaths(ledger=state / "ledger.json", plan_latest=state / "plan_latest.json",
                      watchlist=state / "watchlist.json",
                      daytime_intents=state / "daytime_intents.jsonl",
                      daytime_notes=state / "daytime_notes.jsonl")


class FakeModel:
    """Deterministic stand-in for the model; records every prompt it was given."""

    def __init__(self, *texts: str, error: str = "", model: str = "fake-model",
                 elapsed_ms: int = 7) -> None:
        self.texts = list(texts)
        self.error = error
        self.model = model
        self.elapsed_ms = elapsed_ms
        self.prompts: list[str] = []

    def __call__(self, prompt: str, *, max_tokens: int, timeout: float) -> daytime_decide.ModelCall:
        self.prompts.append(prompt)
        text = self.texts[min(len(self.prompts) - 1, len(self.texts) - 1)] if self.texts else ""
        if self.error:
            return daytime_decide.ModelCall(text="", model=self.model, elapsed_ms=self.elapsed_ms,
                                            prompt_chars=len(prompt), ok=False, error=self.error)
        return daytime_decide.ModelCall(text=text, model=self.model, elapsed_ms=self.elapsed_ms,
                                        prompt_chars=len(prompt), ok=True)


GOOD = json.dumps({
    "want_minutes": [],
    "orders": [{"symbol": "600000", "side": "buy", "qty": 200, "reason": "本槽放量上穿均价",
                "stop": 9.8, "valid_until_slot": "1000"}],
    "note": "一只",
}, ensure_ascii=False)


def _screen(slot: str = "0940") -> dict:
    return {"slot": slot, "source": "injected", "coverage": {"rows": 0, "valid_quotes": 0},
            "as_of": DAY}


def _run(tmp_path: Path, monkeypatch, model: FakeModel, *, slot: str = "0940", **kwargs):
    monkeypatch.setenv(daytime_decide.DAYTIME_ENV, "1")
    paths = _paths(tmp_path)
    result = daytime_decide.decide_slot(tmp_path, paths=paths, llm=model, screen=_screen(slot),
                                        screen_text="[屏] 假屏", now=NOW, **kwargs)
    return result, paths


def test_normal_return_writes_one_pending_intent_with_its_own_reason(tmp_path: Path,
                                                                    monkeypatch) -> None:
    model = FakeModel(GOOD)
    result, paths = _run(tmp_path, monkeypatch, model)

    assert result["status"] == "decided"
    assert result["model_used"] is True
    assert result["elapsed_ms"] == 7
    assert len(model.prompts) == 1
    intents = [json.loads(line) for line in
               paths.daytime_intents.read_text(encoding="utf-8").splitlines()]
    assert len(intents) == 1
    intent = intents[0]
    assert intent["status"] == "pending" and intent["fill_policy_intraday"] == "snapshot"
    assert intent["reason"] == "本槽放量上穿均价" and intent["reason_scope"] == "slot_facts_only"
    assert intent["thesis"] == ""
    assert intent["overnight_plan_thesis"]["decision_date"] == "2026-09-21"
    assert intent["overnight_plan_thesis"]["items"][0]["thesis"] == "低估修复"
    assert "低估修复" not in intent["reason"]
    assert intent["model_used"] is True and intent["elapsed_ms"] == 7


def test_journal_record_always_carries_model_used_and_elapsed(tmp_path: Path,
                                                              monkeypatch) -> None:
    model = FakeModel(GOOD)
    result, paths = _run(tmp_path, monkeypatch, model)
    journal_path = paths.daytime_notes.parent / "daytime_journal.jsonl"
    journal = json.loads(journal_path.read_text(encoding="utf-8").splitlines()[0])
    assert journal["model_used"] is True and journal["elapsed_ms"] == 7
    assert journal["calls"][0]["elapsed_ms"] == 7
    assert result["model_calls"] == 1


def test_timeout_is_skipped_and_recorded(tmp_path: Path, monkeypatch) -> None:
    model = FakeModel(error="timeout")
    result, paths = _run(tmp_path, monkeypatch, model)
    assert result["status"] == "skipped" and result["reason"] == "timeout"
    assert result["model_used"] is False and result["elapsed_ms"] == 7
    assert result["intents"] == []
    assert not paths.daytime_intents.exists() or \
        paths.daytime_intents.read_text(encoding="utf-8") == ""


def test_bad_json_is_skipped(tmp_path: Path, monkeypatch) -> None:
    model = FakeModel("这不是 JSON，只是解释")
    result, _ = _run(tmp_path, monkeypatch, model)
    assert result["status"] == "skipped" and result["reason"] == "bad_json"


def test_want_minutes_triggers_a_second_call_with_bars(tmp_path: Path, monkeypatch) -> None:
    first = json.dumps({"want_minutes": ["600000"], "orders": [], "note": "要看分钟线"})
    second = json.dumps({"want_minutes": [], "orders": [
        {"symbol": "600000", "side": "buy", "qty": 100, "reason": "分钟线放量"}], "note": "终稿"})
    model = FakeModel(first, second)
    bars = {"600000": [{"time": "09:40:00", "close": 10.1}, {"time": "09:45:00", "close": 10.2}]}
    result, _ = _run(tmp_path, monkeypatch, model, minute_bars=bars)

    assert len(model.prompts) == 2
    assert "分钟线" in model.prompts[1] and "09:40=10.1" in model.prompts[1]
    assert result["status"] == "decided" and result["orders"][0]["reason"] == "分钟线放量"


def test_minute_fetcher_failure_is_visible(tmp_path: Path, monkeypatch) -> None:
    first = json.dumps({"want_minutes": ["600000"], "orders": [], "note": ""})
    model = FakeModel(first, GOOD)

    def boom(symbols):
        raise RuntimeError("provider down")

    result, _ = _run(tmp_path, monkeypatch, model, minute_fetcher=boom)
    problems = [item.get("problems") for item in result["rejected"] if item.get("problems")]
    assert ["minute_fetch_failed:RuntimeError"] in problems


def test_watch_only_slot_rejects_buys_but_keeps_sells(tmp_path: Path, monkeypatch) -> None:
    both = json.dumps({"want_minutes": [], "orders": [
        {"symbol": "600000", "side": "buy", "qty": 100, "reason": "想买"},
        {"symbol": "600001", "side": "sell", "qty": 100, "reason": "想卖"}], "note": ""})
    model = FakeModel(both)
    result, _ = _run(tmp_path, monkeypatch, model, slot="0930")
    assert [order["side"] for order in result["orders"]] == ["sell"]
    assert ["watch_only_slot_no_buy"] in [item.get("problems") for item in result["rejected"]]


def test_after_1455_no_new_opens_but_sells_allowed(tmp_path: Path, monkeypatch) -> None:
    both = json.dumps({"want_minutes": [], "orders": [
        {"symbol": "600000", "side": "buy", "qty": 100, "reason": "想买"},
        {"symbol": "600001", "side": "sell", "qty": 100, "reason": "想卖"}], "note": ""})
    model = FakeModel(both)
    result, _ = _run(tmp_path, monkeypatch, model, slot="1455")
    assert [order["side"] for order in result["orders"]] == ["sell"]
    assert ["no_new_open_after_1455"] in [item.get("problems") for item in result["rejected"]]


def test_invalid_orders_are_rejected_with_reasons(tmp_path: Path, monkeypatch) -> None:
    payload = json.dumps({"want_minutes": [], "orders": [
        {"symbol": "600000", "side": "hold", "qty": 100, "reason": "x"},
        {"symbol": "600001", "side": "buy", "qty": 150, "reason": "x"},
        {"symbol": "600002", "side": "buy", "qty": 100, "reason": ""}], "note": ""})
    model = FakeModel(payload)
    result, _ = _run(tmp_path, monkeypatch, model)
    problems = sorted(tuple(item["problems"]) for item in result["rejected"])
    assert problems == [("bad_side",), ("qty_not_a_lot",), ("reason_missing",)]
    assert result["orders"] == []


def test_default_off_makes_no_calls_and_writes_nothing(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv(daytime_decide.DAYTIME_ENV, raising=False)
    model = FakeModel(GOOD)
    paths = _paths(tmp_path)
    result = daytime_decide.decide_slot(tmp_path, paths=paths, llm=model, screen=_screen(),
                                        screen_text="[屏]", now=NOW)
    assert result["reason"] == "daytime_disabled"
    assert result["model_calls"] == 0 and model.prompts == []
    assert not paths.daytime_intents.exists()
    assert not (paths.daytime_notes.parent / "daytime_journal.jsonl").exists()


def test_json_in_a_code_fence_is_parsed(tmp_path: Path, monkeypatch) -> None:
    model = FakeModel("```json\n" + GOOD + "\n```")
    result, _ = _run(tmp_path, monkeypatch, model)
    assert result["status"] == "decided" and len(result["orders"]) == 1


def test_system_prompt_is_passed_once_and_not_pasted_into_the_user_text(
        tmp_path: Path, monkeypatch) -> None:
    """S2-r1 minor: the protocol lives in the ``system`` slot only."""

    model = FakeModel(GOOD)
    _run(tmp_path, monkeypatch, model)
    assert model.prompts == ["[屏] 假屏"]
    assert daytime_decide.SYSTEM_PROMPT not in model.prompts[0]

