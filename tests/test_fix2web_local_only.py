"""2WEB-4b: the 8001 classroom only accepts local state-changing requests and
refuses to bind to a non-loopback address unless remote access is explicit.
"""

from __future__ import annotations

import flask
import pytest

import ai_trader.app as app_mod
from ai_trader import chat as chat_mod


@pytest.fixture()
def client_and_calls(monkeypatch):
    calls: list[str] = []

    def fake_answer(question, diagnostics=None):
        calls.append(question)
        return {"answer": "ok", "model": "m"}

    monkeypatch.setattr(chat_mod, "answer_question", fake_answer)
    app = app_mod.create_app()
    app.config.update(TESTING=True)
    return app.test_client(), calls


@pytest.mark.parametrize(
    "headers",
    [
        {"Origin": "https://evil.example"},
        {"Origin": "null"},
        {"Referer": "https://evil.example/page"},
        {"Origin": "http://127.0.0.1.evil.example:8001"},
    ],
)
def test_cross_site_post_is_refused_before_the_handler(client_and_calls, headers) -> None:
    client, calls = client_and_calls
    resp = client.post("/api/chat", json={"question": "在吗"}, headers=headers)
    assert resp.status_code == 403
    assert resp.get_json()["reason"] == "non_local_request"
    assert calls == []


def test_dns_rebinding_host_is_refused_even_for_get(client_and_calls) -> None:
    client, calls = client_and_calls
    assert client.get("/api/snapshot", headers={"Host": "attacker.example:8001"}).status_code == 403
    resp = client.post(
        "/api/chat",
        json={"question": "在吗"},
        headers={"Host": "attacker.example:8001", "Origin": "http://attacker.example:8001"},
    )
    assert resp.status_code == 403
    assert calls == []


@pytest.mark.parametrize(
    "origin",
    ["http://127.0.0.1:8001", "http://localhost:8001", "http://[::1]:8001", None],
)
def test_local_post_still_reaches_the_handler(client_and_calls, origin) -> None:
    client, calls = client_and_calls
    headers = {"Origin": origin} if origin else {}
    resp = client.post("/api/chat", json={"question": "在吗"}, headers=headers)
    assert resp.status_code == 200
    assert calls == ["在吗"]


@pytest.mark.parametrize("host", ["0.0.0.0", "::", "192.168.1.20", "", "example.com"])
def test_main_refuses_non_loopback_bind(monkeypatch, host) -> None:
    served: list[tuple] = []
    monkeypatch.setattr(flask.Flask, "run", lambda self, host=None, port=None, **k: served.append((host, port)))
    with pytest.raises(SystemExit) as exc:
        app_mod.main(host=host)
    assert "allow-remote" in str(exc.value)
    assert served == []


@pytest.mark.parametrize("host", ["127.0.0.1", "localhost", "::1"])
def test_main_allows_loopback_bind(monkeypatch, host) -> None:
    served: list[tuple] = []
    monkeypatch.setattr(flask.Flask, "run", lambda self, host=None, port=None, **k: served.append((host, port)))
    app_mod.main(host=host, port=8123)
    assert served == [(host, 8123)]


def test_main_binds_remote_only_with_explicit_opt_in(monkeypatch) -> None:
    served: list[tuple] = []
    monkeypatch.setattr(flask.Flask, "run", lambda self, host=None, port=None, **k: served.append((host, port)))
    app_mod.main(host="0.0.0.0", allow_remote=True)
    assert served == [("0.0.0.0", 8001)]
