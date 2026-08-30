"""Focused chat safety regression coverage for the retained AI trader surface."""

from __future__ import annotations

from pathlib import Path

import pytest

from ai_trader.app import create_app
from ai_trader import chat as chat_mod


def test_chat_module_does_not_import_ledger_write_helpers() -> None:
    source = Path(chat_mod.__file__).read_text(encoding="utf-8")
    for forbidden in ("save_ledger", "apply_fill", "record_trade"):
        assert forbidden not in source, f"chat uses write helper: {forbidden}"


def test_answer_question_rejects_blank_with_reason() -> None:
    diag: dict = {}
    assert chat_mod.answer_question("   ", diag) is None
    assert diag["reason"] == "empty_question"


def test_answer_question_truncates_overlong_input(monkeypatch) -> None:
    captured: dict = {}

    def fake_config(explicit_optin=False, diagnostics=None):
        return {"model": "m", "base_url": "http://x", "token": "t"}

    def fake_call(system, user, cfg, diagnostics=None):
        captured["user"] = user
        return "ok"

    original_build = chat_mod._build_chat_prompt

    def capture_question(question, history):
        captured["question"] = question
        return original_build(question, history)

    monkeypatch.setattr(chat_mod, "load_llm_config", fake_config)
    monkeypatch.setattr(chat_mod, "_call", fake_call)
    monkeypatch.setattr(chat_mod, "append_turn", lambda *a, **k: None)
    monkeypatch.setattr(chat_mod, "load_history", lambda: [])
    monkeypatch.setattr(chat_mod, "_build_chat_prompt", capture_question)

    huge = "问" * (chat_mod.MAX_QUESTION_CHARS + 500)
    chat_mod.answer_question(huge)
    assert len(captured["question"]) == chat_mod.MAX_QUESTION_CHARS
    assert captured["question"] == huge[: chat_mod.MAX_QUESTION_CHARS]
    assert captured["question"] in captured["user"]


def test_answer_question_reports_unconfigured(monkeypatch) -> None:
    monkeypatch.setattr(
        chat_mod, "load_llm_config", lambda explicit_optin=False, diagnostics=None: None
    )
    diag: dict = {}
    assert chat_mod.answer_question("在吗", diag) is None
    assert diag["reason"] == "unconfigured"


def test_history_roundtrip_is_bounded(tmp_path, monkeypatch) -> None:
    path = tmp_path / "chat.jsonl"
    monkeypatch.setattr(chat_mod, "CHAT_LOG_PATH", path)
    for i in range(chat_mod.HISTORY_TURNS + 10):
        chat_mod.append_turn(f"q{i}", f"a{i}", "model-x")
    history = chat_mod.load_history()
    assert len(history) <= chat_mod.HISTORY_TURNS
    assert history[-1]["question"].startswith("q")


def test_corrupt_history_does_not_raise(tmp_path, monkeypatch) -> None:
    path = tmp_path / "chat.jsonl"
    path.write_text("{not json", encoding="utf-8")
    monkeypatch.setattr(chat_mod, "CHAT_LOG_PATH", path)
    assert chat_mod.load_history() == []


def test_default_model_is_the_configured_supported_name() -> None:
    from ai_trader import llm as llm_mod

    assert llm_mod.DEFAULT_MODEL == "claude-opus-5"


def test_chat_uses_model_from_resolved_llm_config(monkeypatch) -> None:
    from ai_trader import llm as llm_mod

    monkeypatch.setattr(
        chat_mod,
        "load_llm_config",
        lambda explicit_optin=False, diagnostics=None: {
            "model": llm_mod.DEFAULT_MODEL,
            "base_url": "http://localhost:8080",
            "token": "test",
        },
    )
    monkeypatch.setattr(chat_mod, "_call", lambda *args, **kwargs: "回答")
    monkeypatch.setattr(chat_mod, "append_turn", lambda *args, **kwargs: None)
    monkeypatch.setattr(chat_mod, "load_history", lambda: [])

    result = chat_mod.answer_question("在吗")

    assert result is not None
    assert result["model"] == "claude-opus-5"


@pytest.fixture()
def classroom_client():
    app = create_app()
    app.config.update(TESTING=True)
    return app.test_client()


def test_classroom_chat_get_returns_history(classroom_client, monkeypatch) -> None:
    history = [{"question": "在吗", "answer": "在。", "model": "claude-opus-5"}]
    monkeypatch.setattr(chat_mod, "load_history", lambda limit=8: history)

    resp = classroom_client.get("/api/chat")

    assert resp.status_code == 200
    payload = resp.get_json()
    assert payload["ok"] is True
    assert payload["messages"] == history
    assert "不接券商" in payload["disclaimer"]


def test_classroom_chat_post_blank_question_is_400(classroom_client) -> None:
    resp = classroom_client.post("/api/chat", json={"question": "   "})

    assert resp.status_code == 400
    payload = resp.get_json()
    assert payload["ok"] is False
    assert payload["reason"] == "empty_question"


def test_classroom_chat_post_failed_brain_surfaces_reason(
    classroom_client, monkeypatch
) -> None:
    def fail(question, diagnostics=None):
        if diagnostics is not None:
            diagnostics["reason"] = "model_not_found"
        return None

    monkeypatch.setattr(chat_mod, "answer_question", fail)

    resp = classroom_client.post("/api/chat", json={"question": "你现在怎么看"})

    assert resp.status_code == 503
    payload = resp.get_json()
    assert payload["ok"] is False
    assert payload["reason"] == "model_not_found"
    assert payload["detail"]["reason"] == "model_not_found"


def test_classroom_cycle_post_is_gone_and_never_runs_a_cycle(
    classroom_client, monkeypatch
) -> None:
    """The 8001 compatibility classroom must not bypass the evening writer."""
    import ai_trader.app as app_mod

    calls: list[str] = []

    def fake_run_cycle() -> dict:
        calls.append("run_cycle")
        return {"ok": True}

    monkeypatch.setattr(app_mod, "run_cycle", fake_run_cycle, raising=False)

    resp = classroom_client.post("/api/cycle")

    assert calls == []
    assert resp.status_code == 410
    payload = resp.get_json()
    assert payload["ok"] is False
    assert "20:20" in payload["reason"]
    assert "唯一" in payload["reason"]
