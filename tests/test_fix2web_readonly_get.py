"""2WEB-3: GET routes and startup of the 8001 classroom never create a ledger.

The old /api/snapshot fell back to init_ledger(), so opening the page wrote a
fresh 100k book (or 500'd when only backups were left), and main() called
init_ledger() on the default path before serving. A viewer must say "尚无账本"
and leave the disk alone.
"""

from __future__ import annotations

import json

import flask
import pytest

import ai_trader.app as app_mod
from ai_trader.ledger import empty_ledger, save_ledger
from ai_trader.paths import CyclePaths


@pytest.fixture()
def isolated(tmp_path, monkeypatch):
    state = tmp_path / "state" / "ai_trader"
    results = tmp_path / "results" / "ai_trader"
    paths = CyclePaths(
        ledger=state / "ledger.json",
        snapshot=results / "snapshot_latest.json",
    )
    monkeypatch.setattr(app_mod, "profile_paths", lambda *a, **k: paths)
    app = app_mod.create_app()
    app.config.update(TESTING=True)
    return paths, app.test_client()


def test_snapshot_get_without_ledger_reports_missing_and_writes_nothing(isolated) -> None:
    paths, client = isolated
    resp = client.get("/api/snapshot")
    assert resp.status_code == 200
    payload = resp.get_json()
    assert payload["ledger_status"] == "missing"
    assert "尚无账本" in payload["reason"]
    assert payload["ledger"]["equity"] is None
    assert not paths.ledger.exists()
    assert not paths.ledger.parent.exists()


def test_ledger_get_without_ledger_is_explicit_404_and_writes_nothing(isolated) -> None:
    paths, client = isolated
    resp = client.get("/api/ledger")
    assert resp.status_code == 404
    payload = resp.get_json()
    assert payload["ok"] is False
    assert payload["status"] == "no_ledger"
    assert "尚无账本" in payload["reason"]
    assert not paths.ledger.exists()


def test_snapshot_get_with_only_history_left_does_not_500(isolated) -> None:
    paths, client = isolated
    paths.ledger.parent.mkdir(parents=True)
    (paths.ledger.parent / "journal.jsonl").write_text('{"as_of": "2026-10-09"}\n', encoding="utf-8")
    resp = client.get("/api/snapshot")
    assert resp.status_code == 200
    assert resp.get_json()["ledger_status"] == "missing"
    assert not paths.ledger.exists()


def test_existing_ledger_is_served_byte_for_byte_unchanged(isolated) -> None:
    paths, client = isolated
    book = empty_ledger(ledger_id="ai_trader_100k")
    book["cash"] = 12345.0
    save_ledger(book, paths.ledger)
    before = paths.ledger.read_bytes()
    snap = client.get("/api/snapshot").get_json()
    led = client.get("/api/ledger")
    assert snap["ledger_status"] == "ok"
    assert snap["ledger"]["cash"] == 12345.0
    assert led.status_code == 200
    assert led.get_json()["cash"] == 12345.0
    assert paths.ledger.read_bytes() == before


def test_corrupt_ledger_is_reported_not_replaced(isolated) -> None:
    paths, client = isolated
    paths.ledger.parent.mkdir(parents=True)
    paths.ledger.write_text("{broken", encoding="utf-8")
    led = client.get("/api/ledger")
    assert led.status_code == 500
    assert led.get_json()["status"] == "ledger_unreadable"
    assert client.get("/api/snapshot").get_json()["ledger_status"] == "unreadable"
    assert paths.ledger.read_text(encoding="utf-8") == "{broken"


def test_snapshot_file_present_is_returned_as_is(isolated) -> None:
    paths, client = isolated
    paths.snapshot.parent.mkdir(parents=True)
    paths.snapshot.write_text(json.dumps({"ok": True, "as_of": "2026-10-09"}), encoding="utf-8")
    assert client.get("/api/snapshot").get_json() == {"ok": True, "as_of": "2026-10-09"}
    assert not paths.ledger.exists()


def test_main_does_not_create_dirs_or_ledger(monkeypatch) -> None:
    calls: list[str] = []
    monkeypatch.setattr(app_mod, "init_ledger", lambda *a, **k: calls.append("init_ledger"), raising=False)
    monkeypatch.setattr(app_mod, "ensure_profile_dirs", lambda *a, **k: calls.append("dirs"), raising=False)
    served: list[tuple] = []
    monkeypatch.setattr(flask.Flask, "run", lambda self, host=None, port=None, **k: served.append((host, port)))
    app_mod.main()
    assert calls == []
    assert served == [("127.0.0.1", 8001)]
