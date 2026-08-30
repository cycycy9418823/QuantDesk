"""Tests for the AI trader's optional LLM brain. No real network is used."""

from __future__ import annotations

import json

import ai_trader.llm as llm


def test_disabled_by_default_returns_none(monkeypatch, tmp_path) -> None:
    # No AI_TRADER_LLM_ENABLED => rules only, even if credentials exist on disk.
    monkeypatch.delenv("AI_TRADER_LLM_ENABLED", raising=False)
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "http://localhost:8080")
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "sk-test")
    monkeypatch.setattr(llm, "ENV_FILE", tmp_path / "missing.env")
    assert llm.load_llm_config() is None
    assert llm.propose_picks({"as_of": "2026-08-21"}) is None


def test_enabled_without_credentials_returns_none(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("AI_TRADER_LLM_ENABLED", "1")
    monkeypatch.delenv("ANTHROPIC_BASE_URL", raising=False)
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
    monkeypatch.setattr(llm, "ENV_FILE", tmp_path / "missing.env")
    assert llm.load_llm_config() is None


def test_enabled_reads_credentials_from_env_file(monkeypatch, tmp_path) -> None:
    env = tmp_path / "llm.env"
    env.write_text(
        'export ANTHROPIC_BASE_URL="http://localhost:8080"\n'
        'export ANTHROPIC_AUTH_TOKEN="sk-file-token"\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("AI_TRADER_LLM_ENABLED", "1")
    monkeypatch.delenv("ANTHROPIC_BASE_URL", raising=False)
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
    monkeypatch.setattr(llm, "ENV_FILE", env)
    cfg = llm.load_llm_config()
    assert cfg is not None
    assert cfg["base_url"] == "http://localhost:8080"
    assert cfg["token"] == "sk-file-token"
    assert cfg["model"] == llm.DEFAULT_MODEL


def test_env_file_enable_flag_does_not_activate_unattended_llm(monkeypatch, tmp_path) -> None:
    env = tmp_path / "llm.env"
    env.write_text(
        "AI_TRADER_LLM_ENABLED=1\n"
        'ANTHROPIC_BASE_URL="http://localhost:8080"\n'
        'ANTHROPIC_AUTH_TOKEN="sk-file-token"\n',
        encoding="utf-8",
    )
    monkeypatch.delenv("AI_TRADER_LLM_ENABLED", raising=False)
    monkeypatch.delenv("ANTHROPIC_BASE_URL", raising=False)
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
    monkeypatch.setattr(llm, "ENV_FILE", env)

    assert llm.load_llm_config() is None


def test_export_prefixed_env_is_parsed_with_process_gate(monkeypatch, tmp_path) -> None:
    env = tmp_path / "llm.env"
    env.write_text(
        'export ANTHROPIC_BASE_URL="http://localhost:8080"\n'
        'export ANTHROPIC_AUTH_TOKEN="sk-file-token"\n'
        'export AI_TRADER_LLM_MODEL="claude-opus-5"\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("AI_TRADER_LLM_ENABLED", "1")
    monkeypatch.delenv("ANTHROPIC_BASE_URL", raising=False)
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
    monkeypatch.delenv("AI_TRADER_LLM_MODEL", raising=False)
    monkeypatch.setattr(llm, "ENV_FILE", env)

    cfg = llm.load_llm_config()

    assert cfg is not None
    assert cfg == {
        "base_url": "http://localhost:8080",
        "token": "sk-file-token",
        "model": "claude-opus-5",
        "provider": "anthropic",
    }


# --------------------------------------------------------------------------
# Dual-protocol support: anthropic (/v1/messages) vs openai (chat/completions)
# --------------------------------------------------------------------------


def _fake_urlopen(monkeypatch, captured: dict, payload: dict, status: int = 200) -> None:
    class FakeResponse:
        def __init__(self) -> None:
            self.status = status

        def read(self) -> bytes:
            return json.dumps(payload).encode("utf-8")

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    def fake_urlopen(request, timeout=None):
        captured["url"] = request.full_url
        captured["body"] = json.loads(request.data.decode("utf-8"))
        captured["headers"] = {k.lower(): v for k, v in request.header_items()}
        return FakeResponse()

    monkeypatch.setattr(llm.urllib.request, "urlopen", fake_urlopen)


def test_openai_provider_from_env_file(monkeypatch, tmp_path) -> None:
    env = tmp_path / "llm.env"
    env.write_text(
        'export ANTHROPIC_BASE_URL="https://relay.example/v1"\n'
        'export ANTHROPIC_AUTH_TOKEN="sk-file-token"\n'
        'export AI_TRADER_LLM_MODEL="glm-5.2"\n'
        'export AI_TRADER_LLM_PROVIDER="openai"\n'
        f'export {llm.EXTERNAL_ALLOW_ENV}="1"\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("AI_TRADER_LLM_ENABLED", "1")
    for name in (
        "ANTHROPIC_BASE_URL",
        "ANTHROPIC_AUTH_TOKEN",
        "AI_TRADER_LLM_MODEL",
        "AI_TRADER_LLM_PROVIDER",
        llm.EXTERNAL_ALLOW_ENV,
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(llm, "ENV_FILE", env)

    cfg = llm.load_llm_config()

    assert cfg is not None
    assert cfg["provider"] == "openai"
    assert cfg["model"] == "glm-5.2"
    assert cfg["base_url"] == "https://relay.example/v1"


def test_external_endpoint_without_allow_fails_closed(monkeypatch, tmp_path) -> None:
    """A non-loopback endpoint without the explicit allow must fail closed."""

    env = tmp_path / "llm.env"
    env.write_text(
        'export ANTHROPIC_BASE_URL="https://relay.example/v1"\n'
        'export ANTHROPIC_AUTH_TOKEN="sk-file-token"\n'
        'export AI_TRADER_LLM_MODEL="glm-5.2"\n'
        'export AI_TRADER_LLM_PROVIDER="openai"\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("AI_TRADER_LLM_ENABLED", "1")
    for name in (
        "ANTHROPIC_BASE_URL",
        "ANTHROPIC_AUTH_TOKEN",
        "AI_TRADER_LLM_MODEL",
        "AI_TRADER_LLM_PROVIDER",
        llm.EXTERNAL_ALLOW_ENV,
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(llm, "ENV_FILE", env)

    diagnostics: dict = {}
    cfg = llm.load_llm_config(diagnostics=diagnostics)

    assert cfg is None
    assert diagnostics["reason"] == "external_llm_not_explicitly_allowed"
    # The nightly flow and propose_picks surface the same reason chain:
    assert llm.propose_picks({"as_of": "2026-08-26"}) is None


def test_loopback_endpoint_needs_no_external_allow(monkeypatch, tmp_path) -> None:
    env = tmp_path / "llm.env"
    env.write_text(
        'export ANTHROPIC_BASE_URL="http://127.0.0.1:8080"\n'
        'export ANTHROPIC_AUTH_TOKEN="sk-file-token"\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("AI_TRADER_LLM_ENABLED", "1")
    for name in (
        "ANTHROPIC_BASE_URL",
        "ANTHROPIC_AUTH_TOKEN",
        "AI_TRADER_LLM_MODEL",
        "AI_TRADER_LLM_PROVIDER",
        llm.EXTERNAL_ALLOW_ENV,
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(llm, "ENV_FILE", env)

    cfg = llm.load_llm_config()

    assert cfg is not None
    assert cfg["base_url"] == "http://127.0.0.1:8080"


def test_external_endpoint_with_allow_activates(monkeypatch, tmp_path) -> None:
    env = tmp_path / "llm.env"
    env.write_text(
        'export ANTHROPIC_BASE_URL="https://relay.example/v1"\n'
        'export ANTHROPIC_AUTH_TOKEN="sk-file-token"\n'
        'export AI_TRADER_LLM_MODEL="glm-5.2"\n'
        'export AI_TRADER_LLM_PROVIDER="openai"\n'
        f'export {llm.EXTERNAL_ALLOW_ENV}="1"\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("AI_TRADER_LLM_ENABLED", "1")
    for name in (
        "ANTHROPIC_BASE_URL",
        "ANTHROPIC_AUTH_TOKEN",
        "AI_TRADER_LLM_MODEL",
        "AI_TRADER_LLM_PROVIDER",
        llm.EXTERNAL_ALLOW_ENV,
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(llm, "ENV_FILE", env)

    cfg = llm.load_llm_config()

    assert cfg is not None
    assert cfg["provider"] == "openai"


def test_runtime_descriptor_never_contains_the_token(monkeypatch, tmp_path) -> None:
    env = tmp_path / "llm.env"
    env.write_text(
        'export ANTHROPIC_BASE_URL="https://relay.example/v1"\n'
        'export ANTHROPIC_AUTH_TOKEN="sk-MUST-NEVER-LEAK"\n'
        'export AI_TRADER_LLM_MODEL="glm-5.2"\n'
        'export AI_TRADER_LLM_PROVIDER="openai"\n'
        f'export {llm.EXTERNAL_ALLOW_ENV}="1"\n',
        encoding="utf-8",
    )
    for name in (
        "ANTHROPIC_BASE_URL",
        "ANTHROPIC_AUTH_TOKEN",
        "AI_TRADER_LLM_MODEL",
        "AI_TRADER_LLM_PROVIDER",
        llm.EXTERNAL_ALLOW_ENV,
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(llm, "ENV_FILE", env)

    runtime = llm.llm_runtime_descriptor()

    assert runtime["token"] is None
    assert runtime["token_present"] is True
    serialized = json.dumps(runtime, ensure_ascii=False)
    assert "sk-MUST-NEVER-LEAK" not in serialized
    assert "Bearer" not in serialized
    assert "authorization" not in serialized
    assert "authorization" not in json.dumps(runtime)


def test_runtime_descriptor_endpoint_host_is_hostname_only(monkeypatch, tmp_path) -> None:
    env = tmp_path / "llm.env"
    env.write_text(
        'export ANTHROPIC_BASE_URL="https://user:pw@relay.example:8443/v1?key=SECRET"\n'
        'export ANTHROPIC_AUTH_TOKEN="x"\n'
        'export AI_TRADER_LLM_MODEL="glm-5.2"\n'
        'export AI_TRADER_LLM_PROVIDER="openai"\n',
        encoding="utf-8",
    )
    for name in (
        "ANTHROPIC_BASE_URL",
        "ANTHROPIC_AUTH_TOKEN",
        "AI_TRADER_LLM_MODEL",
        "AI_TRADER_LLM_PROVIDER",
        llm.EXTERNAL_ALLOW_ENV,
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(llm, "ENV_FILE", env)

    runtime = llm.llm_runtime_descriptor()

    assert runtime["endpoint_host"] == "relay.example"
    # No userinfo, port, path, query or secret may appear in the host field.
    serialized = json.dumps(runtime)
    assert "user:pw" not in serialized
    assert "8443" not in serialized
    assert "key=SECRET" not in serialized
    assert "never contain" not in serialized


def test_runtime_descriptor_loopback_marks_local(monkeypatch, tmp_path) -> None:
    env = tmp_path / "llm.env"
    env.write_text(
        'export ANTHROPIC_BASE_URL="http://localhost:8080"\n'
        'export ANTHROPIC_AUTH_TOKEN="x"\n',
        encoding="utf-8",
    )
    for name in (
        "ANTHROPIC_BASE_URL",
        "ANTHROPIC_AUTH_TOKEN",
        "AI_TRADER_LLM_MODEL",
        "AI_TRADER_LLM_PROVIDER",
        llm.EXTERNAL_ALLOW_ENV,
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(llm, "ENV_FILE", env)

    runtime = llm.llm_runtime_descriptor()

    assert runtime["is_external"] is False
    assert runtime["endpoint_host"] == "localhost"
    assert runtime["configured"] is True


def test_openai_provider_without_model_fails_open_to_rules(monkeypatch, tmp_path) -> None:
    """An OpenAI-compatible relay has no universal default model id."""

    env = tmp_path / "llm.env"
    env.write_text(
        'export ANTHROPIC_BASE_URL="https://relay.example/v1"\n'
        'export ANTHROPIC_AUTH_TOKEN="sk-file-token"\n'
        'export AI_TRADER_LLM_PROVIDER="openai"\n'
        f'export {llm.EXTERNAL_ALLOW_ENV}="1"\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("AI_TRADER_LLM_ENABLED", "1")
    for name in (
        "ANTHROPIC_BASE_URL",
        "ANTHROPIC_AUTH_TOKEN",
        "AI_TRADER_LLM_MODEL",
        "AI_TRADER_LLM_PROVIDER",
        llm.EXTERNAL_ALLOW_ENV,
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(llm, "ENV_FILE", env)

    assert llm.load_llm_config() is None


def test_unknown_provider_value_falls_back_to_anthropic(monkeypatch, tmp_path) -> None:
    env = tmp_path / "llm.env"
    env.write_text(
        'export ANTHROPIC_BASE_URL="http://localhost:8080"\n'
        'export ANTHROPIC_AUTH_TOKEN="sk-file-token"\n'
        'export AI_TRADER_LLM_PROVIDER="wat"\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("AI_TRADER_LLM_ENABLED", "1")
    for name in (
        "ANTHROPIC_BASE_URL",
        "ANTHROPIC_AUTH_TOKEN",
        "AI_TRADER_LLM_MODEL",
        "AI_TRADER_LLM_PROVIDER",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(llm, "ENV_FILE", env)

    cfg = llm.load_llm_config()

    assert cfg is not None
    assert cfg["provider"] == "anthropic"
    assert cfg["model"] == llm.DEFAULT_MODEL


def test_openai_call_posts_chat_completions_and_parses_choices(monkeypatch) -> None:
    captured: dict = {}
    _fake_urlopen(
        monkeypatch,
        captured,
        {
            "choices": [
                {
                    # Reasoning relays carry thinking here; the parser must
                    # only promote `content`.
                    "message": {"role": "assistant", "content": '{"ok": true}', "reasoning_content": "思考中…"},
                    "finish_reason": "stop",
                }
            ],
            "usage": {"completion_tokens": 225},
        },
    )
    cfg = {
        "base_url": "https://relay.example/v1",
        "token": "secret-do-not-print",
        "model": "glm-5.2",
        "provider": "openai",
    }
    diagnostics: dict = {}
    out = llm._call("系统提示", "用户问题", cfg, diagnostics)

    assert out == '{"ok": true}'
    # OpenAI dialect: chat/completions (no double /v1), system as a message.
    assert captured["url"].endswith("/v1/chat/completions")
    assert captured["url"].count("/v1") == 1
    roles = [m["role"] for m in captured["body"]["messages"]]
    assert roles == ["system", "user"]
    assert captured["body"]["model"] == "glm-5.2"
    assert "system" not in captured["body"]
    assert captured["headers"]["authorization"] == "Bearer secret-do-not-print"
    assert "anthropic-version" not in captured["headers"]
    assert diagnostics.get("output_tokens") == 225


def test_anthropic_call_path_unchanged_for_local_relay(monkeypatch) -> None:
    captured: dict = {}
    _fake_urlopen(
        monkeypatch,
        captured,
        {"content": [{"type": "text", "text": "中转回答"}], "usage": {"output_tokens": 5}},
    )
    cfg = {
        "base_url": "http://localhost:8080",
        "token": "relay-token",
        "model": "claude-opus-5",
        "provider": "anthropic",
    }
    out = llm._call("系统提示", "用户问题", cfg)

    assert out == "中转回答"
    assert captured["url"].endswith("/v1/messages")
    assert captured["body"]["system"] == "系统提示"
    assert [m["role"] for m in captured["body"]["messages"]] == ["user"]
    assert captured["headers"]["authorization"] == "Bearer relay-token"


def test_openai_empty_content_reports_finish_reason(monkeypatch) -> None:
    captured: dict = {}
    _fake_urlopen(
        monkeypatch,
        captured,
        {"choices": [{"message": {"content": ""}, "finish_reason": "length"}], "usage": {"completion_tokens": 24000}},
    )
    cfg = {
        "base_url": "https://relay.example/v1",
        "token": "x",
        "model": "glm-5.2",
        "provider": "openai",
    }
    diagnostics: dict = {}
    assert llm._call("s", "u", cfg, diagnostics) is None
    assert diagnostics["reason"].startswith("no_text_block")
    assert "finish=length" in diagnostics["reason"]


def test_propose_picks_parses_and_sanitizes(monkeypatch) -> None:
    monkeypatch.setenv("AI_TRADER_LLM_ENABLED", "1")
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "http://localhost:8080")
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "sk-test")
    fake = (
        "```json\n"
        '{"buys":[{"symbol":"600000","reason":"低位企稳","hold_days":5,"conviction":"probation"},'
        '{"symbol":"bad","reason":"x","hold_days":3}],'
        '"avoid":["600519"],"market_note":"观望"}\n'
        "```"
    )
    monkeypatch.setattr(llm, "_call", lambda *a, **k: fake)
    out = llm.propose_picks({"as_of": "2026-08-21", "candidates": ["600000(+1%)"]})
    assert out is not None
    assert [b["symbol"] for b in out["buys"]] == ["600000"]  # 'bad' dropped (not 6 digits)
    assert out["buys"][0]["hold_days"] == 5
    assert out["avoid"] == ["600519"]
    assert out["market_note"] == "观望"


def test_propose_picks_none_when_call_fails(monkeypatch) -> None:
    monkeypatch.setenv("AI_TRADER_LLM_ENABLED", "1")
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "http://localhost:8080")
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "sk-test")
    monkeypatch.setattr(llm, "_call", lambda *a, **k: None)  # network/timeout/etc.
    assert llm.propose_picks({"as_of": "2026-08-21"}) is None


def test_extract_json_handles_plain_and_fenced() -> None:
    assert llm._extract_json('{"a":1}') == {"a": 1}
    assert llm._extract_json("noise ```json\n{\"a\":2}\n``` tail")["a"] == 2
    assert llm._extract_json("no json here") is None
