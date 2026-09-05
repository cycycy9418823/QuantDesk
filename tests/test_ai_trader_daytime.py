from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from ai_trader.daytime import load_daytime_notes_for, observe_session
from ai_trader.paths import CyclePaths

SHANGHAI = ZoneInfo("Asia/Shanghai")


def _runtime(root: Path) -> CyclePaths:
    state = root / "state" / "ai_trader"
    state.mkdir(parents=True, exist_ok=True)
    (root / "data" / "cache").mkdir(parents=True, exist_ok=True)
    (root / "data" / "daily").mkdir(parents=True, exist_ok=True)
    (root / "results" / "ai_trader").mkdir(parents=True, exist_ok=True)
    ledger = {
        "schema_version": "ai_trader_ledger_v1",
        "ledger_id": "ai_trader_100k",
        "cash": 50000,
        "positions": [
            {
                "symbol": "600000",
                "quantity": 1000,
                "avg_cost": 10.0,
                "thesis": "test",
            }
        ],
    }
    (state / "ledger.json").write_text(json.dumps(ledger), encoding="utf-8")
    (state / "plan_latest.json").write_text(
        json.dumps(
            {
                "committed": True,
                "intents": [{"action": "buy", "symbol": "000001"}],
            }
        ),
        encoding="utf-8",
    )
    (state / "watchlist.json").write_text(json.dumps({"symbols": ["601398"]}), encoding="utf-8")
    return CyclePaths(
        daily_dir=root / "data" / "daily",
        ledger=state / "ledger.json",
        plan_latest=state / "plan_latest.json",
        watchlist=state / "watchlist.json",
        daytime_notes=state / "daytime_notes.jsonl",
        daytime_session=state / "daytime_session.json",
        journal_latest=state / "journal_latest.json",
        journal_log=state / "journal.jsonl",
    )


def test_observe_writes_notes_not_ledger(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)
    ledger_before = (runtime.ledger).read_bytes()
    plan_before = (runtime.plan_latest).read_bytes()
    rows = [
        {"symbol": "600000", "name": "浦发", "pct_chg": "4.2", "price": "10.4"},
        {"symbol": "000001", "name": "平安", "pct_chg": "10.1", "price": "12"},
        {"symbol": "300750", "name": "宁德", "pct_chg": "10.0", "price": "200"},
    ]
    first = observe_session(
        tmp_path,
        now=datetime(2026, 9, 4, 10, 5, tzinfo=SHANGHAI),
        snapshot_rows=rows,
        paths=runtime,
    )
    assert first["new_notes"] >= 2
    assert first["ledger_written"] is False
    assert runtime.ledger.read_bytes() == ledger_before
    assert runtime.plan_latest.read_bytes() == plan_before
    kinds = {event["kind"] for event in first["events"]}
    assert "position_move" in kinds
    assert "watch_limit" in kinds
    assert "board_limit" in kinds

    second = observe_session(
        tmp_path,
        now=datetime(2026, 9, 4, 10, 10, tzinfo=SHANGHAI),
        snapshot_rows=rows,
        paths=runtime,
    )
    assert second["new_notes"] == 0
    notes = load_daytime_notes_for("2026-09-04", notes_path=runtime.daytime_notes)
    assert len(notes) == first["new_notes"]


def test_position_bucket_change_emits_again(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)
    observe_session(
        tmp_path,
        now=datetime(2026, 9, 4, 10, 5, tzinfo=SHANGHAI),
        snapshot_rows=[{"symbol": "600000", "name": "浦发", "pct_chg": "2.5", "price": "10.2"}],
        paths=runtime,
    )
    again = observe_session(
        tmp_path,
        now=datetime(2026, 9, 4, 10, 10, tzinfo=SHANGHAI),
        snapshot_rows=[{"symbol": "600000", "name": "浦发", "pct_chg": "8.1", "price": "10.8"}],
        paths=runtime,
    )
    assert again["new_notes"] == 1
    assert again["events"][0]["kind"] == "position_move"
