"""S2-r1: the daytime budget must actually reach the wire.

``urllib.request.urlopen`` is replaced by a recorder in every test, so nothing
here opens a socket. The point is that both dialects read ``max_tokens`` and
``timeout`` from the caller's cfg instead of falling back to the module's
``DEFAULT_MAX_TOKENS`` / ``DEFAULT_TIMEOUT_S`` (which is what the earlier
``setdefault`` only pretended to do).
"""

from __future__ import annotations

import json
import urllib.request
from pathlib import Path

import pytest

from ai_trader import daytime_decide, llm
from ai_trader.paths import CyclePaths

DUMMY_TOKEN = "dummy-not-a-secret"


class _Response:
    def __init__(self, data: bytes, status: int = 200) -> None:
        self._data = data
        self.status = status

    def read(self) -> bytes:
        return self._data

    def __enter__(self) -> "_Response":
        return self

    def __exit__(self, *exc: object) -> bool:
        return False


class Recorder:
    """Stand-in for urlopen: records body/timeout, returns a canned answer."""

    def __init__(self, payload: dict) -> None:
        self.payload = payload
        self.calls: list[dict] = []

    def __call__(self, request, timeout=None):  # noqa: ANN001 - mirrors urlopen
        body = json.loads((request.data or b"{}").decode("utf-8"))
        self.calls.append(
            {
                "url": str(getattr(request, "full_url", "")),
                "body": body,
                "timeout": timeout,
            }
        )
        return _Response(json.dumps(self.payload, ensure_ascii=False).encode("utf-8"))


def _openai_payload() -> dict:
    return {
        "choices": [{"message": {"content": "{}"}, "finish_reason": "stop"}],
        "usage": {"completion_tokens": 3},
    }


def _anthropic_payload() -> dict:
    return {"content": [{"type": "text", "text": "{}"}], "usage": {"output_tokens": 3}}


def _cfg(**extra: object) -> dict:
    cfg: dict = {
        "provider": llm.PROVIDER_OPENAI,
        "base_url": "http://127.0.0.1:9",
        "model": "fake-model",
        "token": DUMMY_TOKEN,
    }
    cfg.update(extra)
    return cfg


def test_daytime_adapter_reaches_the_wire_with_2000_tokens_and_120s(monkeypatch) -> None:
    recorder = Recorder(_openai_payload())
    monkeypatch.setattr(urllib.request, "urlopen", recorder)
    monkeypatch.setattr(llm, "load_llm_config", lambda **kwargs: _cfg())

    call = daytime_decide._default_llm("[屏] 假屏", max_tokens=daytime_decide.MAX_TOKENS,
                                       timeout=daytime_decide.TIMEOUT_SECONDS)

    assert call.ok is True
    assert len(recorder.calls) == 1
    sent = recorder.calls[0]
    assert sent["body"]["max_tokens"] == 2000
    assert sent["timeout"] == 120
    messages = sent["body"]["messages"]
    assert [message["role"] for message in messages] == ["system", "user"]
    assert messages[1]["content"] == "[屏] 假屏"
    assert "只输出 JSON" in messages[0]["content"]


def test_nightly_path_keeps_the_module_defaults(monkeypatch) -> None:
    recorder = Recorder(_openai_payload())
    monkeypatch.setattr(urllib.request, "urlopen", recorder)

    assert llm._call("SYS", "USER", _cfg()) == "{}"

    sent = recorder.calls[0]
    assert llm.DEFAULT_MAX_TOKENS == 24000 and llm.DEFAULT_TIMEOUT_S == 300
    assert sent["body"]["max_tokens"] == 24000
    assert sent["timeout"] == 300


def test_anthropic_dialect_honours_the_same_knobs(monkeypatch) -> None:
    recorder = Recorder(_anthropic_payload())
    monkeypatch.setattr(urllib.request, "urlopen", recorder)

    capped = _cfg(provider=llm.PROVIDER_ANTHROPIC, max_tokens="2000", timeout="120")
    assert llm._call("SYS", "USER", capped) == "{}"
    assert recorder.calls[0]["body"]["max_tokens"] == 2000
    assert recorder.calls[0]["timeout"] == 120

    assert llm._call("SYS", "USER", _cfg(provider=llm.PROVIDER_ANTHROPIC)) == "{}"
    assert recorder.calls[1]["body"]["max_tokens"] == 24000
    assert recorder.calls[1]["timeout"] == 300


@pytest.mark.parametrize(
    "raw,expected",
    [(None, 2000), ("", 2000), ("abc", 2000), ("0", 2000), (-5, 2000), (2000, 2000),
     ("2000", 2000), (2000.9, 2000)],
)
def test_cfg_int_falls_back_to_the_default_instead_of_raising(raw, expected: int) -> None:
    assert llm._cfg_int({"knob": raw}, "knob", 2000) == expected
    assert llm._cfg_int({}, "knob", 2000) == 2000


def test_decide_slot_hands_the_same_budget_to_the_model_callable(tmp_path: Path,
                                                                 monkeypatch) -> None:
    seen: list[tuple[int, float]] = []

    def model(prompt: str, *, max_tokens: int, timeout: float) -> daytime_decide.ModelCall:
        seen.append((max_tokens, timeout))
        return daytime_decide.ModelCall(text="{}", model="fake", elapsed_ms=1,
                                        prompt_chars=len(prompt), ok=True)

    monkeypatch.setenv(daytime_decide.DAYTIME_ENV, "1")
    state = tmp_path / "state" / "ai_trader"
    state.mkdir(parents=True, exist_ok=True)
    paths = CyclePaths(ledger=state / "ledger.json", plan_latest=state / "plan_latest.json",
                       watchlist=state / "watchlist.json",
                       daytime_intents=state / "daytime_intents.jsonl",
                       daytime_notes=state / "daytime_notes.jsonl")

    daytime_decide.decide_slot(tmp_path, paths=paths, llm=model,
                               screen={"slot": "0940", "source": "injected"},
                               screen_text="[屏] 假屏", write=False)

    assert seen == [(daytime_decide.MAX_TOKENS, daytime_decide.TIMEOUT_SECONDS)]
