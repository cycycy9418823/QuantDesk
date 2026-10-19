"""2WEB-1: the 8001 page must not offer a cycle trigger that the server refuses.

/api/cycle answers 410 (the 20:20 scheduled task is the only writer). The old
page still had a "今晚复盘" button that POSTed there and rendered the 410 body as
if it were a fresh snapshot (equity 0, P&L -100,000).
"""

from __future__ import annotations

import re

from ai_trader import app as app_mod
from tests import fix2web_harness as harness

SNAPSHOT_OK = {
    "ok": True,
    "as_of": "2026-10-09",
    "degraded": False,
    "journal": {"headline": "h", "thought": "t", "account": {"equity": 101000, "cash": 50000,
                                                           "market_value": 51000, "pnl": 1000}},
    "ledger": {"positions": []},
}

GONE_410 = {
    "ok": False,
    "reason": "8001 兼容教室不再运行 cycle；晚间 20:20 计划任务是唯一自动写入入口。",
}


def test_page_has_no_cycle_button_or_cycle_call() -> None:
    html = harness.page_html()
    assert 'id="runBtn"' not in html
    assert "/api/cycle" not in harness.page_script(html)
    # The page says where the evening review comes from instead.
    assert re.search(r"计划任务", html)
    assert "20:20" in html


def test_clicking_where_the_button_was_never_posts_cycle(tmp_path) -> None:
    routes = {
        "GET /api/snapshot": harness.json_route(SNAPSHOT_OK),
        "GET /api/chat": harness.json_route({"ok": True, "messages": []}),
        "POST /api/cycle": harness.json_route(GONE_410, 410),
    }
    result = harness.run_page(tmp_path, routes, click=["runBtn"])
    assert not [c for c in result["calls"] if c["url"] == "/api/cycle"]


def test_rendering_a_refusal_body_does_not_invent_minus_100k(tmp_path) -> None:
    routes = {
        "GET /api/snapshot": harness.json_route(SNAPSHOT_OK),
        "GET /api/chat": harness.json_route({"ok": True, "messages": []}),
    }
    result = harness.run_page(tmp_path, routes, render_payload=GONE_410)
    kpis = result["elements"]["kpis"]["innerHTML"]
    assert "-100,000" not in kpis
    assert "—" in kpis


def test_empty_snapshot_copy_no_longer_points_at_a_button() -> None:
    source = app_mod.__file__
    text = open(source, encoding="utf-8").read()
    assert "点「今天复盘一次」" not in text
