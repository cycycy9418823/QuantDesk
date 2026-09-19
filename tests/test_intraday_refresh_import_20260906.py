from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path


def test_daytime_import_exposes_snapshot_contract() -> None:
    from ai_trader.daytime import load_daytime_notes_for, observe_session
    from quantdesk.intraday_refresh.snapshot import (
        LIMIT_PCT,
        SPOT_META_RELATIVE,
        SPOT_RELATIVE,
    )

    assert callable(observe_session)
    assert callable(load_daytime_notes_for)
    assert LIMIT_PCT == 9.5
    assert SPOT_RELATIVE.as_posix() == "data/cache/spot_snapshot_latest.csv"
    assert SPOT_META_RELATIVE.as_posix() == "data/cache/spot_snapshot_meta.json"


def test_daytime_reads_local_snapshot_and_notes_without_writes(tmp_path: Path) -> None:
    from ai_trader.daytime import load_daytime_notes_for, observe_session
    from ai_trader.ledger import empty_ledger
    from ai_trader.paths import CyclePaths

    state = tmp_path / "state" / "ai_trader"
    cache = tmp_path / "data" / "cache"
    state.mkdir(parents=True)
    cache.mkdir(parents=True)
    (state / "ledger.json").write_text(json.dumps(empty_ledger()), encoding="utf-8")
    (state / "plan_latest.json").write_text(json.dumps({"intents": []}), encoding="utf-8")
    (state / "watchlist.json").write_text(json.dumps({"symbols": []}), encoding="utf-8")
    (cache / "spot_snapshot_latest.csv").write_text(
        "symbol,name,price,pct_chg\n600000,浦发,10.1,1.0\n", encoding="utf-8"
    )
    note = {"as_of": "2026-09-07", "kind": "position_move"}
    notes_path = state / "daytime_notes.jsonl"
    notes_path.write_text(json.dumps(note, ensure_ascii=False) + "\n", encoding="utf-8")
    paths = CyclePaths(
        ledger=state / "ledger.json",
        plan_latest=state / "plan_latest.json",
        watchlist=state / "watchlist.json",
        daytime_notes=notes_path,
        daytime_session=state / "daytime_session.json",
    )
    before = {path: path.read_bytes() for path in state.iterdir()}

    assert load_daytime_notes_for("2026-09-07", notes_path=notes_path) == [note]
    payload = observe_session(
        tmp_path,
        now=datetime(2026, 9, 7, 10, 0),
        paths=paths,
        write=False,
    )

    assert payload["ok"] is True
    assert payload["snapshot_rows"] == 1
    assert payload["ledger_written"] is False
    assert {path: path.read_bytes() for path in state.iterdir()} == before
