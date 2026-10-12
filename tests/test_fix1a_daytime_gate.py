"""Batch 1A-F: daytime fills write the real AI ledger only when the ledger header
grants it (charter §10 trust ladder + a named authorisation); otherwise default deny.

Before the fix the environment switch AI_TRADER_DAYTIME=1 alone let a slot write
real fills into a book that already had trades ("first day only observes" never
applied to it), a zero-volume quote could fill, and a quote with no source filled
with price_source="".
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from ai_trader import daytime_fill
from ai_trader.ledger import empty_ledger
from ai_trader.paths import CyclePaths

DAY = "2026-09-22"
NOW = datetime(2026, 9, 22, 9, 45, 10)
QUOTE_TIME = "2026-09-22T09:45:05+08:00"
GRANT = {"authorized_by": "owner", "authorized_at": "2026-09-21T20:00:00+08:00"}


def _paths(tmp_path: Path) -> CyclePaths:
    state = tmp_path / "state" / "ai_trader"
    state.mkdir(parents=True, exist_ok=True)
    return CyclePaths(ledger=state / "ledger.json", daytime_intents=state / "daytime_intents.jsonl",
                      daytime_notes=state / "daytime_notes.jsonl")


def _intent(side: str = "buy", quantity: int = 200) -> dict:
    return {"as_of": DAY, "slot": "0940", "created_at": f"{DAY}T09:40:03+08:00", "symbol": "600000",
            "side": side, "quantity": quantity, "reason": "本槽事实", "status": "pending",
            "valid_until_slot": "1000", "model_used": True, "elapsed_ms": 5}


def _row(**overrides) -> dict:
    row = {"symbol": "600000", "name": "浦发银行", "price": 10.0, "pct_chg": 1.0, "pre_close": 9.9,
           "volume": 100000.0, "quote_time": QUOTE_TIME}
    row.update(overrides)
    return row


def _book(trust: dict | None = None, **overrides) -> dict:
    ledger = empty_ledger()
    ledger.update({"cash": 100000.0, "equity": 100000.0, "positions": [], "trades": []})
    if trust is not None:
        ledger["daytime_trust"] = trust
    ledger.update(overrides)
    return ledger


def _fill(tmp_path: Path, monkeypatch, *, intents: list[dict], book: dict, rows: list[dict] | None = None,
          source: str = "cloud_slot"):
    paths = _paths(tmp_path)
    with paths.daytime_intents.open("w", encoding="utf-8") as handle:
        for row in intents:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
    monkeypatch.setenv(daytime_fill.DRY_ENV, "0")
    saved: list[dict] = []
    result = daytime_fill.fill_slot(
        tmp_path, paths=paths, rows=rows or [_row()], quote_slot="0945", quote_time=QUOTE_TIME,
        rows_source=source, ledger=book, now=NOW, write=True, dry=False,
        save=lambda ledger, path: (saved.append(ledger), path)[1],
    )
    return result, paths, saved


def test_book_without_a_trust_header_never_writes_the_ledger(tmp_path, monkeypatch) -> None:
    result, paths, saved = _fill(tmp_path, monkeypatch, intents=[_intent()], book=_book())
    assert saved == [] and result["fills"] == 0 and result["ledger_written"] is False
    assert result["gated_fills"] == 1
    gated = result["gated_records"][0]
    assert gated["dry_run"] is True and gated["gate_blocked"].startswith(daytime_fill.BLOCK_TRUST_GATE)
    dry = (paths.daytime_notes.parent / daytime_fill.DRY_FILLS_NAME).read_text(encoding="utf-8")
    assert "gate_blocked" in dry
    assert not (paths.daytime_notes.parent / daytime_fill.FILLS_NAME).exists()
    journal = (paths.daytime_notes.parent / daytime_fill.JOURNAL_NAME).read_text(encoding="utf-8")
    assert '"status": "trust_blocked"' in journal


def test_level_one_allows_exits_but_not_opens(tmp_path, monkeypatch) -> None:
    held = [{"symbol": "600000", "quantity": 500, "avg_cost": 9.0, "acquired_on": "2026-09-18"}]
    sell, _, saved_sell = _fill(tmp_path / "sell", monkeypatch, intents=[_intent("sell", 100)],
                                book=_book({"level": 1, **GRANT}, positions=held))
    assert sell["fills"] == 1 and saved_sell
    buy, _, saved_buy = _fill(tmp_path / "buy", monkeypatch, intents=[_intent()],
                              book=_book({"level": 1, **GRANT}))
    assert buy["fills"] == 0 and not saved_buy and "required=2" in buy["gated_records"][0]["gate_blocked"]


def test_level_without_a_named_authorisation_is_denied(tmp_path, monkeypatch) -> None:
    result, _, saved = _fill(tmp_path, monkeypatch, intents=[_intent()], book=_book({"level": 2}))
    assert not saved and result["gated_records"][0]["gate_blocked"].endswith("authorization_missing")


def test_authorised_level_two_writes_the_fill_with_evidence(tmp_path, monkeypatch) -> None:
    result, _, saved = _fill(tmp_path, monkeypatch, intents=[_intent()], book=_book({"level": 2, **GRANT}))
    assert result["fills"] == 1 and saved
    record = result["records"][0]
    assert record["price_source"] == "cloud_slot" and record["volume"] == 100000.0 and record["pre_close"] == 9.9


def test_zero_volume_and_sourceless_quotes_do_not_fill(tmp_path, monkeypatch) -> None:
    zero, _, saved_zero = _fill(tmp_path / "zero", monkeypatch, intents=[_intent()],
                                book=_book({"level": 2, **GRANT}), rows=[_row(volume=0)])
    assert not saved_zero and zero["blocks"][0]["blocked_by"] == daytime_fill.BLOCK_ZERO_VOLUME
    bare, _, saved_bare = _fill(tmp_path / "bare", monkeypatch, intents=[_intent()],
                                book=_book({"level": 2, **GRANT}), source="")
    assert not saved_bare and bare["blocks"][0]["blocked_by"] == daytime_fill.BLOCK_NO_SOURCE
