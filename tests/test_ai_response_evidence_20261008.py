"""Keep actual AI failures inspectable without changing decisions or retrying."""

from __future__ import annotations

import json
import urllib.error
from pathlib import Path

import pandas as pd
import pytest

import ai_trader.llm as llm
import ai_trader.overnight as overnight
import ai_trader.paths as paths_module
from tests.test_ai_trader_transport_diagnostics import (
    SECRET,
    _assert_secret_free,
    _enable,
    _install,
    _seed,
)


def _setup_evening(monkeypatch, tmp_path: Path):
    _enable(monkeypatch, tmp_path)
    runtime = _seed(tmp_path)
    panel = pd.DataFrame({"600500": [5.0, 5.08]}, index=["2026-09-18", "2026-09-21"])
    monkeypatch.setattr(overnight, "latest_as_of", lambda *_: {"as_of": "2026-09-21"})
    monkeypatch.setattr(overnight, "focus_universe", lambda *a, **k: ["600500"])
    monkeypatch.setattr(overnight, "load_close_panel", lambda *a, **k: (panel.copy(), pd.DataFrame(), []))
    monkeypatch.setattr(overnight, "load_recommendations", lambda **k: {"symbols": [], "rows": [], "warnings": []})
    monkeypatch.setattr(overnight, "research_ideas", lambda p, b, **k: {"book": b, "evaluated": [], "live": None})
    monkeypatch.setattr(overnight, "pick_operating_method", lambda *_: (None, "idle"))
    monkeypatch.setattr(overnight, "next_trade_day", lambda *_: "2026-09-22")
    monkeypatch.setattr(overnight, "_build_llm_context", lambda **k: {"as_of": "2026-09-21", "candidates": []})
    monkeypatch.setattr(paths_module, "STATE_DIR", runtime.ledger.parent)
    monkeypatch.setattr(paths_module, "RESULTS_DIR", runtime.cycle_latest.parent)
    return runtime


def _openai(text: str, finish: str | None = "stop") -> dict:
    return {
        "model": "provider-returned-model",
        "choices": [{"message": {"content": text}, "finish_reason": finish}],
        "usage": {"completion_tokens": 1234},
    }


def _read(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


@pytest.mark.parametrize("finish,reported", [("length", True), ("stop", False), (None, None)])
def test_failed_answer_survives_real_evening_chain(tmp_path, monkeypatch, finish, reported):
    runtime = _setup_evening(monkeypatch, tmp_path)
    raw = ' \n{"buys":[{"symbol":"600500"  '
    calls = _install(monkeypatch, payload=_openai(raw, finish))
    snapshot = overnight.run_evening(paths=runtime, network_news=False)
    plan = _read(runtime.plan_latest)
    cycle = _read(runtime.cycle_latest)
    journal = _read(runtime.journal_latest)
    history = json.loads(runtime.journal_log.read_text(encoding="utf-8").splitlines()[-1])
    assert calls == [llm.DEFAULT_TIMEOUT_S]
    evidence = plan["llm"]["response_evidence"]
    assert evidence["raw_text"] == raw
    assert evidence["text_char_count"] == len(raw)
    assert evidence["text_status"] == "received"
    assert evidence["finish_reason"] == finish
    assert evidence["provider_reported_truncation"] is reported
    assert evidence["output_tokens"] == 1234
    assert evidence["requested_model"] == "glm-5.2"
    assert evidence["response_model"] == "provider-returned-model"
    assert evidence["parse_stage"] == "json"
    assert evidence["error"] == "unparseable_json"
    assert evidence["transport"]["http_status"] == 200
    assert evidence["observed_at"]
    assert evidence["cycle"] == {
        "ledger_id": _read(runtime.ledger)["ledger_id"],
        "plan_id": plan["plan_id"],
        "decision_date": "2026-09-21",
        "trade_date": "2026-09-22",
    }
    for row in (plan["llm"], cycle["plan"]["llm"], snapshot["plan"]["llm"], journal["llm"], history["llm"]):
        assert row["used"] is False
        assert row["unavailable_reason"] == "unparseable_json"
        assert row["response_evidence"] == evidence
    assert plan["llm"]["raw_response"] is None
    for path in (runtime.plan_latest, runtime.cycle_latest, runtime.journal_latest, runtime.journal_log):
        _assert_secret_free(path.read_text(encoding="utf-8"))


@pytest.mark.parametrize(
    "raw,reason,stage",
    [
        ('[{"buys":[]}]', "unparseable_json", "json"),
        ('```json\n[{"buys":[]}]\n```', "unparseable_json", "json"),
        ('{"buys":{"symbol":"600500"}}', "malformed_buys_not_array", "shape"),
        ('{"sells":true}', "malformed_sells_not_array", "shape"),
        ('{"stance":[]}', "malformed_stance_not_object", "shape"),
    ],
)
def test_rejected_json_keeps_reply_and_original_rejection(monkeypatch, tmp_path, raw, reason, stage):
    _enable(monkeypatch, tmp_path)
    calls = _install(monkeypatch, payload=_openai(raw))
    diagnostics = {}
    assert llm.propose_picks({"as_of": "2026-09-21"}, diagnostics) is None
    evidence = llm.persistable_response_evidence(diagnostics)
    assert diagnostics["reason"] == reason
    assert evidence["raw_text"] == raw
    assert evidence["error"] == reason
    assert evidence["parse_stage"] == stage
    assert len(calls) == 1


@pytest.mark.parametrize("provider", ["openai", "anthropic"])
def test_empty_answer_preserves_provider_end_and_excludes_reasoning(monkeypatch, tmp_path, provider):
    _enable(monkeypatch, tmp_path)
    monkeypatch.setenv("AI_TRADER_LLM_PROVIDER", provider)
    if provider == "openai":
        payload = _openai("", "length")
        payload["choices"][0]["message"]["reasoning_content"] = "private-thinking-sentinel"
    else:
        payload = {
            "model": "provider-returned-model",
            "content": [{"type": "thinking", "thinking": "private-thinking-sentinel"}],
            "usage": {"output_tokens": 1234},
            "stop_reason": "max_tokens",
        }
    calls = _install(monkeypatch, payload=payload)
    diagnostics = {}
    assert llm.propose_picks({}, diagnostics) is None
    evidence = llm.persistable_response_evidence(diagnostics)
    assert evidence["raw_text"] == ""
    assert evidence["text_char_count"] == 0
    assert evidence["text_status"] == "empty"
    assert evidence["provider_reported_truncation"] is True
    assert evidence["output_tokens"] == 1234
    assert evidence["response_model"] == "provider-returned-model"
    assert evidence["parse_stage"] == "text"
    assert evidence["error"].startswith("no_text_block:")
    assert evidence["transport"]["http_status"] == 200
    assert "private-thinking-sentinel" not in json.dumps(evidence)
    _assert_secret_free(json.dumps(evidence))
    assert len(calls) == 1


@pytest.mark.parametrize("phase", ["request", "response"])
def test_timeout_never_borrows_old_reply(tmp_path, monkeypatch, phase):
    runtime = _setup_evening(monkeypatch, tmp_path)
    old = _read(runtime.plan_latest)
    old["llm"] = {
        "raw_response": {"text": "old-reply-sentinel", "source": "llm", "created_at": "2026-09-18T20:20:00+08:00"},
        "response_evidence": {"raw_text": "old-reply-sentinel", "output_tokens": 999},
    }
    runtime.plan_latest.write_text(json.dumps(old), encoding="utf-8")
    failure = urllib.error.URLError(TimeoutError(SECRET))
    calls = _install(monkeypatch, **({"error": failure} if phase == "request" else {"read_error": failure}))
    overnight.run_evening(paths=runtime, network_news=False)
    plan = _read(runtime.plan_latest)
    evidence = plan["llm"]["response_evidence"]
    assert evidence["raw_text"] is None
    assert evidence["text_char_count"] is None
    assert evidence["text_status"] == "unavailable"
    assert evidence["finish_reason"] is None
    assert evidence["output_tokens"] is None
    assert evidence["provider_reported_truncation"] is None
    assert evidence["transport"]["stage"] == phase
    assert evidence["transport"]["error_category"] == "timeout"
    assert evidence["error"] == "transport:URLError"
    assert plan["llm"]["raw_response"] is None
    assert len(calls) == 1
    assert "old-reply-sentinel" not in json.dumps(evidence)
    for path in (runtime.plan_latest, runtime.cycle_latest, runtime.journal_latest):
        _assert_secret_free(path.read_text(encoding="utf-8"))


def test_same_cycle_recovery_preserves_failure_without_calling_model(tmp_path, monkeypatch):
    runtime = _setup_evening(monkeypatch, tmp_path)
    calls = _install(monkeypatch, payload=_openai('{"buys":', "length"))
    overnight.run_evening(paths=runtime, network_news=False)
    plan_bytes = runtime.plan_latest.read_bytes()
    evidence = _read(runtime.plan_latest)["llm"]["response_evidence"]
    original_ledger = _read(runtime.ledger)
    monkeypatch.setattr(overnight, "_now_iso", lambda: "2099-01-01T21:00:00+08:00")

    def forbidden(*a, **k):
        raise AssertionError("recovery attempted a second model call")

    monkeypatch.setattr(llm.urllib.request, "urlopen", forbidden)
    snapshot = overnight.run_evening(paths=runtime, network_news=False)
    assert len(calls) == 1
    assert runtime.plan_latest.read_bytes() == plan_bytes
    assert snapshot["plan"]["llm"]["response_evidence"] == evidence
    assert snapshot["journal"]["llm"]["response_evidence"] == evidence
    # save_ledger refreshes its write timestamp even for an unchanged book.
    assert {k: v for k, v in _read(runtime.ledger).items() if k != "updated_at"} == {
        k: v for k, v in original_ledger.items() if k != "updated_at"
    }
    assert len(runtime.journal_log.read_text(encoding="utf-8").splitlines()) == 1
    assert len((runtime.ledger.parent / "llm_response_evidence.jsonl").read_text(encoding="utf-8").splitlines()) == 1


def test_legacy_recovery_does_not_invent_response_evidence(tmp_path, monkeypatch):
    runtime = _setup_evening(monkeypatch, tmp_path)
    old = {
        "plan_id": "plan_20260921_for_20260922", "decision_date": "2026-09-21",
        "trade_date": "2026-09-22", "status": "planned", "committed": True,
        "recorded_at": "2026-09-21T20:20:00+08:00", "intents": [],
        "llm": {"used": True, "model": "original-model", "unavailable_reason": "", "raw_response": {
            "text": '{"buys":[]}', "model": "original-model", "source": "llm",
            "created_at": "2026-09-21T20:20:00+08:00",
        }},
    }
    runtime.plan_latest.write_text(json.dumps(old), encoding="utf-8")
    before = runtime.plan_latest.read_bytes()

    def forbidden(*a, **k):
        raise AssertionError("offline replay attempted network")

    monkeypatch.setattr(llm.urllib.request, "urlopen", forbidden)
    snapshot = overnight.run_evening(paths=runtime, network_news=False)
    assert runtime.plan_latest.read_bytes() == before
    assert snapshot["plan"]["llm"]["raw_response"]["created_at"] == old["recorded_at"]
    assert "response_evidence" not in snapshot["plan"]["llm"]
    assert "response_evidence" not in snapshot["journal"]["llm"]
    assert not (runtime.ledger.parent / "llm_response_evidence.jsonl").exists()


def test_success_and_replay_keep_original_behavior(monkeypatch, tmp_path):
    _enable(monkeypatch, tmp_path)
    raw = ' \n```json\n{"buys":[],"market_note":"observe"}\n```\n '
    calls = _install(monkeypatch, payload=_openai(raw))
    diagnostics = {"response_evidence": {"raw_text": "stale-diagnostic"}, "reason": "stale-error"}
    proposal = llm.propose_picks({}, diagnostics)
    assert proposal is not None and proposal["buys"] == []
    assert proposal["raw_response"] == raw.strip()
    evidence = llm.persistable_response_evidence(diagnostics)
    assert evidence["raw_text"] == raw
    assert evidence["parse_stage"] == "accepted"
    assert evidence["error"] is None
    replay = llm.propose_picks({}, diagnostics, reuse_text=proposal["raw_response"], reuse_model=proposal["model"])
    assert replay["buys"] == proposal["buys"]
    assert replay["reused_persisted_response"] is True
    assert len(calls) == 1
    replay_evidence = llm.persistable_response_evidence(diagnostics)
    assert replay_evidence["source"] == "persisted_replay"
    assert replay_evidence["call_state"] == "not_called"
    assert replay_evidence["response_model"] is None


def test_unpaired_unicode_answer_is_kept_reversibly_without_breaking_fallback(tmp_path, monkeypatch):
    runtime = _setup_evening(monkeypatch, tmp_path)
    raw = chr(0xD800)
    calls = _install(monkeypatch, payload=_openai(raw))
    snapshot = overnight.run_evening(paths=runtime, network_news=False)
    evidence = snapshot["plan"]["llm"]["response_evidence"]
    assert snapshot["plan"]["llm"]["unavailable_reason"] == "unparseable_json"
    assert evidence["raw_text"] is None
    assert json.loads(evidence["raw_text_json"]) == raw
    assert evidence["text_status"] == "received"
    assert evidence["text_char_count"] == 1
    assert evidence["text_storage"] == "json_escaped"
    assert _read(runtime.plan_latest)["llm"]["response_evidence"] == evidence
    assert len(calls) == 1


def test_deferred_plan_keeps_every_actual_reply_beyond_latest(tmp_path, monkeypatch):
    runtime = _setup_evening(monkeypatch, tmp_path)
    old = {
        "plan_id": "old_deferred", "decision_date": "2026-09-17", "trade_date": "2026-09-18",
        "status": "planned", "committed": True,
        "intents": [{"symbol": "600999", "side": "buy", "shares": 100}],
    }
    runtime.plan_latest.write_text(json.dumps(old), encoding="utf-8")
    first_calls = _install(monkeypatch, payload=_openai("failed-A"))
    overnight.run_evening(paths=runtime, network_news=False)
    second_calls = _install(monkeypatch, payload=_openai("failed-B"))
    snapshot = overnight.run_evening(paths=runtime, network_news=False)
    assert len(first_calls) == len(second_calls) == 1
    assert _read(runtime.plan_latest) == old
    assert snapshot["plan"]["llm"]["response_evidence"]["raw_text"] == "failed-B"
    # Keep existing per-day journal identity; its reuse must not lose a reply.
    assert len(runtime.journal_log.read_text(encoding="utf-8").splitlines()) == 1
    runtime.cycle_latest.write_text("{}", encoding="utf-8")
    runtime.snapshot.write_text("{}", encoding="utf-8")
    saved = [json.loads(line) for line in (runtime.ledger.parent / "llm_response_evidence.jsonl").read_text(encoding="utf-8").splitlines()]
    assert [row["raw_text"] for row in saved] == ["failed-A", "failed-B"]
    assert all(row["cycle"]["decision_date"] == "2026-09-21" for row in saved)


def test_history_write_failure_keeps_original_model_failure(tmp_path, monkeypatch):
    runtime = _setup_evening(monkeypatch, tmp_path)
    (runtime.ledger.parent / "llm_response_evidence.jsonl").mkdir()
    calls = _install(monkeypatch, payload=_openai("broken-answer"))
    snapshot = overnight.run_evening(paths=runtime, network_news=False)
    model = snapshot["plan"]["llm"]
    assert model["used"] is False
    assert model["unavailable_reason"] == "unparseable_json"
    assert model["response_evidence"]["raw_text"] == "broken-answer"
    assert model["response_evidence"]["history_status"] == "write_failed"
    assert any(str(w).startswith("llm_response_history_write_failed:") for w in snapshot["warnings"])
    assert len(calls) == 1


def test_history_preserves_interrupted_tail_before_new_reply(tmp_path, monkeypatch):
    runtime = _setup_evening(monkeypatch, tmp_path)
    path = runtime.ledger.parent / "llm_response_evidence.jsonl"
    tail = b'{"interrupted":'
    path.write_bytes(tail)
    calls = _install(monkeypatch, payload=_openai("new-broken-answer"))
    overnight.run_evening(paths=runtime, network_news=False)
    assert path.read_bytes().startswith(tail + b"\n")
    assert json.loads(path.read_text(encoding="utf-8").splitlines()[-1])["raw_text"] == "new-broken-answer"
    assert len(calls) == 1


@pytest.mark.parametrize("failure_at", ["fsync", "close"])
def test_history_never_claims_success_before_durable_append(tmp_path, monkeypatch, failure_at):
    runtime = _setup_evening(monkeypatch, tmp_path)
    history = runtime.ledger.parent / "llm_response_evidence.jsonl"
    calls = _install(monkeypatch, payload=_openai("broken-answer"))
    if failure_at == "fsync":
        original_fsync = overnight.os.fsync

        def fail_history_fsync(fd):
            if history.exists() and overnight.os.path.samestat(overnight.os.fstat(fd), history.stat()):
                raise OSError("synthetic history fsync failure")
            return original_fsync(fd)

        monkeypatch.setattr(overnight.os, "fsync", fail_history_fsync)
    else:
        original_open = Path.open

        class FailHistoryClose:
            def __init__(self, stream):
                self.stream = stream

            def __enter__(self):
                return self.stream.__enter__()

            def __exit__(self, *args):
                self.stream.__exit__(*args)
                raise OSError("synthetic history close failure")

        def open_with_close_failure(path, *args, **kwargs):
            stream = original_open(path, *args, **kwargs)
            return FailHistoryClose(stream) if path == history and args == ("a+b",) else stream

        monkeypatch.setattr(Path, "open", open_with_close_failure)

    snapshot = overnight.run_evening(paths=runtime, network_news=False)
    model = snapshot["plan"]["llm"]
    saved = json.loads(history.read_text(encoding="utf-8").splitlines()[-1])
    assert saved["raw_text"] == model["response_evidence"]["raw_text"] == "broken-answer"
    # A row cannot certify its own fsync/close before they have happened.
    assert "history_status" not in saved
    assert model["response_evidence"]["history_status"] == "write_failed"
    assert model["unavailable_reason"] == "unparseable_json"
    assert any(str(w).startswith("llm_response_history_write_failed:") for w in snapshot["warnings"])
    assert len(calls) == 1
