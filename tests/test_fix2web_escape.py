"""2WEB-4a: ledger / journal / model text is HTML-escaped before innerHTML.

Model-written fields (lessons, thesis, reasons, method titles) were spliced
into innerHTML raw, so a crafted string in the snapshot became live markup.
"""

from __future__ import annotations

from tests import fix2web_harness as harness

EVIL = '<img src=x onerror="alert(1)">'

SNAPSHOT = {
    "ok": True,
    "as_of": "2026-10-09",
    "degraded": False,
    "journal": {
        "headline": EVIL,
        "lessons": [EVIL],
        "account": {"equity": 101000, "cash": 1, "market_value": 2, "pnl": 3},
        "tomorrow_plan": {
            "intents": [{"side": EVIL, "symbol": EVIL, "preset_touch_price": EVIL,
                         "predicted_direction": EVIL, "conviction": EVIL}],
        },
        "last_review": {
            "plan_id": "p1",
            "scorecard": [{"symbol": EVIL, "open_fill": EVIL, "fill_price": EVIL}],
        },
    },
    "hypotheses": [{"title": EVIL, "status": EVIL, "holdout_mean_net": None}],
    "ledger": {"positions": [{"symbol": EVIL, "quantity": EVIL, "market_value": 1, "thesis": EVIL}]},
    "actions": [{"action": EVIL, "symbol": EVIL, "status": EVIL, "reason": EVIL}],
}

HTML_TARGETS = ("kpis", "planBody", "reviewBody", "methods", "lessons", "positions", "acts")


def test_model_and_ledger_text_is_escaped_in_every_table(tmp_path) -> None:
    routes = {
        "GET /api/snapshot": harness.json_route(SNAPSHOT),
        "GET /api/chat": harness.json_route({"ok": True, "messages": []}),
    }
    els = harness.run_page(tmp_path, routes)["elements"]
    for target in HTML_TARGETS:
        html = els[target]["innerHTML"]
        assert "<img" not in html, f"{target} injected raw markup: {html[:200]}"
    for target in ("planBody", "reviewBody", "methods", "lessons", "positions", "acts"):
        assert "&lt;img src=x onerror=&quot;alert(1)&quot;&gt;" in els[target]["innerHTML"], target
    # textContent sinks were already safe and stay verbatim.
    assert els["headline"]["textContent"] == EVIL
