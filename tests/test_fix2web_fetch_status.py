"""2WEB-2: every fetch on the 8001 page checks res.ok before rendering.

A failed request must show the error and its HTTP status, never paint the
error body as if it were data (or vanish into an unhandled rejection).
"""

from __future__ import annotations

from tests import fix2web_harness as harness

CHAT_EMPTY = harness.json_route({"ok": True, "messages": []})
SNAPSHOT_500 = {"ok": False, "reason": "ledger_unreadable"}


def _routes(**overrides: dict) -> dict:
    routes = {
        "GET /api/snapshot": harness.json_route({"ok": True, "degraded": True, "reason": "空账"}),
        "GET /api/chat": CHAT_EMPTY,
    }
    routes.update(overrides)
    return routes


def test_snapshot_error_shows_status_and_renders_no_data(tmp_path) -> None:
    result = harness.run_page(
        tmp_path, _routes(**{"GET /api/snapshot": harness.json_route(SNAPSHOT_500, 500)})
    )
    els = result["elements"]
    assert "500" in els["statusLine"]["textContent"]
    assert "ledger_unreadable" in els["statusLine"]["textContent"]
    # Nothing from the error body is painted as account data.
    assert els["kpis"]["innerHTML"] == ""
    assert els["positions"]["innerHTML"] == ""
    assert result["unhandled"] == []


def test_snapshot_non_json_error_still_reports_status(tmp_path) -> None:
    result = harness.run_page(
        tmp_path,
        _routes(**{"GET /api/snapshot": harness.text_route("<html>Bad Gateway</html>", 502)}),
    )
    els = result["elements"]
    assert "502" in els["statusLine"]["textContent"]
    assert els["kpis"]["innerHTML"] == ""
    assert result["unhandled"] == []


def test_chat_history_error_shows_status_not_empty_history(tmp_path) -> None:
    blocked = {
        "ok": False,
        "reason": "chat_not_available_for_profile",
        "detail": "chat 面板目前只读默认账；本页是 glm 档，请用对比报告看它。",
    }
    result = harness.run_page(tmp_path, _routes(**{"GET /api/chat": harness.json_route(blocked, 501)}))
    els = result["elements"]
    assert "501" in els["chatStatus"]["textContent"]
    assert "chat_not_available_for_profile" in els["chatStatus"]["textContent"]
    # "还没聊过" would claim an empty history that was never read.
    texts = [c["textContent"] for c in els["chatLog"]["children"]]
    assert not any("还没聊过" in t for t in texts)


def test_chat_send_error_shows_status_code(tmp_path) -> None:
    failed = {"ok": False, "reason": "unconfigured", "detail": {"reason": "unconfigured"}}
    result = harness.run_page(
        tmp_path,
        _routes(**{"POST /api/chat": harness.json_route(failed, 503)}),
        values={"chatInput": "在吗"},
        click=["chatSend"],
    )
    els = result["elements"]
    assert "503" in els["chatStatus"]["textContent"]
    assert "unconfigured" in els["chatStatus"]["textContent"]
    assert els["chatSend"]["disabled"] is False
    assert result["unhandled"] == []


def test_chat_send_non_json_error_reports_status(tmp_path) -> None:
    result = harness.run_page(
        tmp_path,
        _routes(**{"POST /api/chat": harness.text_route("upstream exploded", 502)}),
        values={"chatInput": "在吗"},
        click=["chatSend"],
    )
    assert "502" in result["elements"]["chatStatus"]["textContent"]
