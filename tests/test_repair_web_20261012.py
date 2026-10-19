"""8001 degradation must preserve the book and report missing snapshot content."""
from __future__ import annotations

import pytest
import json

from ai_trader import app as viewer
from ai_trader.ledger import empty_ledger, save_ledger
from ai_trader.paths import CyclePaths


@pytest.mark.parametrize("snapshot_text", [None, "{broken", "[]"])
def test_missing_snapshot_shows_saved_positions_without_reset_or_revaluation(tmp_path, monkeypatch, snapshot_text):
    paths = CyclePaths(ledger=tmp_path / "state/ledger.json", snapshot=tmp_path / "results/snapshot.json")
    monkeypatch.setattr(viewer, "profile_paths", lambda: paths)
    book = empty_ledger(ledger_id="ai_trader_100k")
    book.update(cash=90000, equity=101000, market_value=11000, as_of="2026-09-30",
                positions=[{"symbol": "600000", "quantity": 1000, "market_price": 11, "market_value": 11000}])
    save_ledger(book, paths.ledger)
    if snapshot_text is not None:
        paths.snapshot.parent.mkdir(parents=True)
        paths.snapshot.write_text(snapshot_text, encoding="utf-8")
    before = {path: path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()}
    payload = viewer.create_app().test_client().get("/api/snapshot").get_json()
    assert payload["ledger"]["positions"] == book["positions"]
    assert payload["journal"]["account"] == {"cash": 90000, "equity": 101000, "market_value": 11000, "pnl": 1000, "holdings": 1}
    assert payload["as_of"] == "2026-09-30"
    assert payload["valuation_status"] == "saved_book_only"
    assert "不代表当前行情" in payload["reason"]
    assert "新开的" not in payload["journal"]["headline"]
    assert {path: path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()} == before


def test_cli_remote_binding_requires_explicit_flag(monkeypatch):
    from ai_trader import cli

    calls = []
    monkeypatch.setattr(cli, "run_app", lambda **kwargs: calls.append(kwargs))
    assert cli.main(["app"]) == 0
    assert calls[-1]["allow_remote"] is False
    assert cli.main(["app", "--host", "0.0.0.0", "--allow-remote"]) == 0
    assert calls[-1] == {"host": "0.0.0.0", "port": 8001, "allow_remote": True}


def test_focus_get_missing_is_readonly(tmp_path, monkeypatch):
    paths = CyclePaths(ledger=tmp_path / "state/ai_trader/ledger.json")
    monkeypatch.setattr(viewer, "profile_paths", lambda: paths)
    response = viewer.create_app().test_client().get("/api/minute-focus")
    assert response.status_code == 200
    assert response.get_json()["status"] == "missing"
    assert not paths.ledger.parent.exists()


def test_focus_display_uses_text_not_html(tmp_path):
    from tests.fix2web_harness import json_route, run_page

    hostile = '<img src=x onerror="bad()">'
    result = run_page(tmp_path, {"GET /api/snapshot": json_route({"ok": True, "journal": {}, "ledger": {}}),
                                "GET /api/chat": json_route({"messages": []}),
                                "GET /api/minute-focus": json_route({"status": "active", "model": hostile,
                                                                       "symbols": [hostile]})})
    assert hostile in result["elements"]["minuteFocus"]["textContent"]
    assert result["elements"]["minuteFocus"]["innerHTML"] == ""
    assert result["unhandled"] == []


@pytest.mark.parametrize("status,highlight", [("stale", True), ("missing", True), ("unknown", True),
                                             ("current", False), ("not_due", False)])
def test_health_freshness_warning_preserves_original_line(status, highlight):
    from tests.test_project_status_ui import _run_status_ui

    payload = {"health_line": "原健康行：采集 ✓", "health_freshness": {
        "status": status, "expected_trade_date": "2026-10-12", "observed_trade_date": "2026-10-09",
        "generated_at": "2026-10-09T22:30:00+08:00", "warning": "新鲜度警告 <img src=x>"}}
    script = "vm.runInContext(" + json.dumps(
        "renderProjectStatusPayload(" + json.dumps(payload, ensure_ascii=False) + ");"
        "console.log(JSON.stringify({health:nodeText('projectStatusHealth'),"
        "red:nodeHasClass('projectStatusHealth','has-red'),requests}));", ensure_ascii=False) + ", context);"
    result = _run_status_ui(script)
    assert result["health"].startswith("原健康行：采集 ✓")
    assert ("新鲜度警告 <img src=x>" in result["health"]) is highlight
    assert result["red"] is highlight
    assert result["requests"] == []


def test_current_health_still_respects_original_red_mark():
    from tests.test_project_status_ui import _run_status_ui

    result = _run_status_ui("""
vm.runInContext(`renderProjectStatusPayload({health_line:'采集 ✗',health_freshness:{status:'current'}});
console.log(JSON.stringify({red:nodeHasClass('projectStatusHealth','has-red')}));`, context);
""")
    assert result["red"] is True
