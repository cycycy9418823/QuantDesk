"""S3 daytime fills: the gate rules, the mandate rails and dry mode.

Everything runs on injected rows and a tmp_path book. No network, no model, no
real ledger.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from ai_trader import daytime_fill
from ai_trader.ledger import empty_ledger
from ai_trader.mandate import TraderMandate
from ai_trader.paths import CyclePaths

DAY = "2026-09-22"
NOW = datetime(2026, 9, 22, 9, 45, 10)
INTENT_SLOT = "0940"
QUOTE_SLOT = "0945"
QUOTE_TIME = "2026-09-22T09:45:05+08:00"


def _paths(tmp_path: Path) -> CyclePaths:
    state = tmp_path / "state" / "ai_trader"
    state.mkdir(parents=True, exist_ok=True)
    return CyclePaths(ledger=state / "ledger.json", plan_latest=state / "plan_latest.json",
                      watchlist=state / "watchlist.json",
                      daytime_intents=state / "daytime_intents.jsonl",
                      daytime_notes=state / "daytime_notes.jsonl")


def _intent(**overrides: object) -> dict:
    row = {
        "schema_version": "ai_trader_daytime_intent_v1", "as_of": DAY, "slot": INTENT_SLOT,
        "created_at": f"{DAY}T09:40:03+08:00", "symbol": "600000", "side": "buy",
        "quantity": 200, "reason": "本槽放量上穿均价", "reason_scope": "slot_facts_only",
        "thesis": "", "overnight_plan_thesis": {"decision_date": "2026-09-21", "items": []},
        "stop": 9.8, "valid_until_slot": "1000", "status": "pending",
        "fill_policy_intraday": "snapshot", "model_used": True, "model": "fake-model",
        "elapsed_ms": 611, "prompt_chars": 1234, "note": "",
    }
    row.update(overrides)
    return row


def _write_intents(paths: CyclePaths, rows: list[dict]) -> None:
    paths.daytime_intents.parent.mkdir(parents=True, exist_ok=True)
    with paths.daytime_intents.open("a", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")


def _row(**overrides: object) -> dict:
    row = {"symbol": "600000", "name": "浦发银行", "price": 10.0, "pct_chg": 1.5,
           "quote_time": QUOTE_TIME, "fetched_at": QUOTE_TIME}
    row.update(overrides)
    return row


def _book(**overrides: object) -> dict:
    ledger = empty_ledger()
    ledger.update({"cash": 100000.0, "equity": 100000.0, "positions": [], "trades": []})
    ledger.update(overrides)
    return ledger


def _run(tmp_path: Path, monkeypatch, *, intents: list[dict], rows: list[dict],
         book: dict | None = None, write: bool = True, dry: bool = False,
         mandate: TraderMandate | None = None, quote_slot: str = QUOTE_SLOT,
         quote_time: str = QUOTE_TIME):
    paths = _paths(tmp_path)
    _write_intents(paths, intents)
    monkeypatch.setenv(daytime_fill.DRY_ENV, "1" if dry else "0")
    saved: list[dict] = []

    def _save(ledger: dict, path: Path) -> Path:
        saved.append(json.loads(json.dumps(ledger)))
        return path

    kwargs = {"mandate": mandate} if mandate is not None else {}
    result = daytime_fill.fill_slot(tmp_path, paths=paths, rows=rows, quote_slot=quote_slot,
                                    quote_time=quote_time, rows_source="cloud_slot",
                                    ledger=book if book is not None else _book(),
                                    now=NOW, write=write, dry=dry, save=_save, **kwargs)
    return result, paths, saved


def test_buy_fills_on_the_next_slot_and_writes_the_ledger(tmp_path, monkeypatch) -> None:
    result, paths, saved = _run(tmp_path, monkeypatch, intents=[_intent()], rows=[_row()])
    assert result["fills"] == 1 and result["written"] is True
    record = result["records"][0]
    assert record["side"] == "buy" and record["quantity"] == 200
    assert record["price"] == 10.0 and record["price_source"] == "cloud_slot"
    assert record["price_timestamp"] == QUOTE_TIME
    assert record["slot"] == QUOTE_SLOT and record["intent_slot"] == INTENT_SLOT
    assert record["model_used"] is True and record["elapsed_ms"] == 611
    assert saved and saved[-1]["positions"][0]["symbol"] == "600000"
    assert saved[-1]["positions"][0]["acquired_on"] == DAY
    assert saved[-1]["trades"][0]["source"] == "daytime_snapshot"
    assert paths.daytime_notes.parent.joinpath(daytime_fill.FILLS_NAME).is_file()


def test_same_snapshot_cannot_fill_its_own_intent(tmp_path, monkeypatch) -> None:
    result, _, saved = _run(tmp_path, monkeypatch, intents=[_intent(slot=QUOTE_SLOT)],
                            rows=[_row()], quote_slot=QUOTE_SLOT)
    assert result["fills"] == 0 and not saved
    assert result["blocks"][0]["blocked_by"] == daytime_fill.BLOCK_SAME_SLOT


def test_quote_older_than_the_intent_is_rejected(tmp_path, monkeypatch) -> None:
    result, _, _ = _run(tmp_path, monkeypatch, intents=[_intent()],
                        rows=[_row(quote_time="2026-09-22T09:39:00+08:00",
                                   fetched_at="2026-09-22T09:39:00+08:00")])
    assert result["fills"] == 0
    assert result["blocks"][0]["blocked_by"] == daytime_fill.BLOCK_NOT_LATER


def test_limit_up_cannot_buy_and_limit_down_cannot_sell(tmp_path, monkeypatch) -> None:
    up, _, _ = _run(tmp_path / "up", monkeypatch, intents=[_intent()],
                    rows=[_row(pct_chg=10.02)])
    assert up["fills"] == 0
    assert up["blocks"][0]["blocked_by"] == daytime_fill.BLOCK_LIMIT_UP
    down, _, _ = _run(tmp_path / "down", monkeypatch,
                      intents=[_intent(side="sell", quantity=100)],
                      rows=[_row(pct_chg=-10.0)],
                      book=_book(positions=[{"symbol": "600000", "quantity": 500,
                                             "avg_cost": 9.0, "acquired_on": "2026-09-18"}]))
    assert down["fills"] == 0
    assert down["blocks"][0]["blocked_by"] == daytime_fill.BLOCK_LIMIT_DOWN


def test_suspended_and_missing_quotes_never_fill(tmp_path, monkeypatch) -> None:
    missing, _, _ = _run(tmp_path, monkeypatch, intents=[_intent()], rows=[_row(symbol="600111")])
    assert missing["fills"] == 0
    assert missing["blocks"][0]["blocked_by"] == daytime_fill.BLOCK_MISSING_QUOTE
    suspended, _, _ = _run(tmp_path, monkeypatch, intents=[_intent()],
                           rows=[_row(trading="false")])
    assert suspended["fills"] == 0
    assert suspended["blocks"][0]["blocked_by"] == daytime_fill.BLOCK_MISSING_QUOTE


def test_odd_lot_is_refused(tmp_path, monkeypatch) -> None:
    result, _, _ = _run(tmp_path, monkeypatch, intents=[_intent(quantity=150)], rows=[_row()])
    assert result["fills"] == 0
    assert result["blocks"][0]["blocked_by"] == daytime_fill.BLOCK_NOT_A_LOT


def test_t_plus_1_blocks_a_same_day_sell(tmp_path, monkeypatch) -> None:
    book = _book(positions=[{"symbol": "600000", "quantity": 500, "avg_cost": 9.5,
                             "acquired_on": DAY}])
    result, _, saved = _run(tmp_path, monkeypatch, intents=[_intent(side="sell", quantity=100)],
                            rows=[_row()], book=book)
    assert result["fills"] == 0 and not saved
    assert result["blocks"][0]["blocked_by"] == daytime_fill.BLOCK_T_PLUS_1


def test_sell_uses_the_shared_cost_model_and_updates_realized_pnl(tmp_path, monkeypatch) -> None:
    book = _book(positions=[{"symbol": "600000", "quantity": 300, "avg_cost": 9.0,
                             "acquired_on": "2026-09-18"}])
    result, _, saved = _run(tmp_path, monkeypatch, intents=[_intent(side="sell", quantity=300)],
                            rows=[_row(price=11.0, pct_chg=-0.5)], book=book)
    record = result["records"][0]
    assert record["fee"] > 0 and saved
    assert saved[-1]["positions"] == []
    assert saved[-1]["realized_pnl"] > 0
    assert saved[-1]["trades"][0]["realized_pnl"] > 0


def test_mandate_block_is_recorded_in_the_journal(tmp_path, monkeypatch) -> None:
    mandate = TraderMandate(max_opens_per_day=0)
    result, paths, saved = _run(tmp_path, monkeypatch, intents=[_intent()], rows=[_row()],
                                mandate=mandate)
    assert result["fills"] == 0 and not saved
    row = result["mandate_blocks"][0]
    assert row["blocked_by"] == daytime_fill.MANDATE_OPENS_PER_DAY
    assert row["symbol"] == "600000" and row["side"] == "buy" and row["qty"] == 200
    assert row["ai_reason"] == "本槽放量上穿均价"
    journal = paths.daytime_notes.parent / daytime_fill.JOURNAL_NAME
    written = [json.loads(line) for line in journal.read_text(encoding="utf-8").splitlines()]
    assert written[0]["status"] == "mandate_blocked"
    assert written[0]["blocked_by"] == daytime_fill.MANDATE_OPENS_PER_DAY


def test_cash_floor_rail_blocks_a_buy_that_would_break_it(tmp_path, monkeypatch) -> None:
    book = _book(cash=16000.0, equity=100000.0)
    result, _, _ = _run(tmp_path, monkeypatch, intents=[_intent(quantity=1500)], rows=[_row()],
                        book=book)
    assert result["fills"] == 0
    assert result["mandate_blocks"][0]["blocked_by"] == daytime_fill.MANDATE_CASH_FLOOR


def test_dry_day_writes_only_the_dry_fills_file(tmp_path, monkeypatch) -> None:
    result, paths, saved = _run(tmp_path, monkeypatch, intents=[_intent()], rows=[_row()],
                                dry=True)
    assert result["dry_run"] is True and result["ledger_written"] is False
    assert result["records"][0]["dry_run"] is True
    assert not saved
    dry_file = paths.daytime_notes.parent / daytime_fill.DRY_FILLS_NAME
    real_file = paths.daytime_notes.parent / daytime_fill.FILLS_NAME
    assert dry_file.is_file() and not real_file.exists()
    assert not paths.ledger.exists()


def test_filled_intents_are_not_filled_twice(tmp_path, monkeypatch) -> None:
    paths = _paths(tmp_path)
    _write_intents(paths, [_intent()])
    monkeypatch.setenv(daytime_fill.DRY_ENV, "0")

    def _save(ledger: dict, path: Path) -> Path:
        return path

    first = daytime_fill.fill_slot(tmp_path, paths=paths, rows=[_row()], quote_slot=QUOTE_SLOT,
                                   quote_time=QUOTE_TIME, rows_source="cloud_slot",
                                   ledger=_book(), now=NOW, write=True, save=_save)
    second = daytime_fill.fill_slot(tmp_path, paths=paths, rows=[_row()], quote_slot="0950",
                                    quote_time="2026-09-22T09:50:05+08:00",
                                    rows_source="cloud_slot", ledger=_book(), now=NOW,
                                    write=True, save=_save)
    assert first["fills"] == 1 and second["fills"] == 0
    assert second["pending_intents"] == 1


def test_intent_expires_after_its_valid_until_slot(tmp_path, monkeypatch) -> None:
    """The AI's own window has teeth: a stale intent is not retried forever."""

    stale, _, saved = _run(tmp_path / "stale", monkeypatch,
                           intents=[_intent(valid_until_slot="0945")], rows=[_row()],
                           quote_slot="0950", quote_time="2026-09-22T09:50:05+08:00")
    assert stale["fills"] == 0 and stale["blocks"] == [] and not saved
    assert stale["pending_intents"] == 0 and stale["expired_intents"] == 1
    alive, _, _ = _run(tmp_path / "alive", monkeypatch,
                       intents=[_intent(valid_until_slot="0950")], rows=[_row()],
                       quote_slot="0950", quote_time="2026-09-22T09:50:05+08:00")
    assert alive["fills"] == 1 and alive["expired_intents"] == 0


def test_st_name_without_is_st_column_uses_the_five_percent_limit(tmp_path, monkeypatch) -> None:
    """A1-r1：快照 CSV 没有 is_st 列，ST 要从 name 认出来。

    1A：2026-07-06 起主板 ST 涨跌幅为 10%（HKEX 通函 055/2026），DAY=2026-09-22，
    所以 +5% 不再是涨停、+10% 才是；07-06 前的 5% 由 test_fix1a_fill_gates 覆盖。
    """

    result, _, saved = _run(tmp_path / "five", monkeypatch, intents=[_intent()],
                            rows=[_row(name="*ST美丽", pct_chg=5.0)])
    assert result["fills"] == 1 and saved
    blocked, _, saved_up = _run(tmp_path / "ten", monkeypatch, intents=[_intent()],
                                rows=[_row(name="*ST美丽", pct_chg=10.0)])
    assert blocked["fills"] == 0 and not saved_up
    assert blocked["blocks"][0]["blocked_by"] == daytime_fill.BLOCK_LIMIT_UP


def test_normal_name_at_five_percent_still_fills(tmp_path, monkeypatch) -> None:
    result, _, saved = _run(tmp_path, monkeypatch, intents=[_intent()],
                            rows=[_row(name="浦发银行", pct_chg=5.0)])
    assert result["fills"] == 1 and saved


def test_missing_name_and_is_st_marks_the_ten_percent_assumption(tmp_path, monkeypatch) -> None:
    """两样都没有时不许静默当普通股：标记 st_unknown 并写明按 10% 判断。"""

    rows = [_row(name="")]
    quotes = daytime_fill.quote_index(rows, quote_slot=QUOTE_SLOT, quote_time=QUOTE_TIME,
                                      source="cloud_slot")
    assert quotes["600000"]["st_unknown"] is True
    assert quotes["600000"]["is_st"] is False

    result, _, _ = _run(tmp_path, monkeypatch, intents=[_intent()], rows=rows)
    record = result["records"][0]
    assert record["st_unknown"] is True
    assert "ST 未知" in record["st_note"] and "10%" in record["st_note"]


def test_known_st_flag_from_the_column_is_not_marked_unknown() -> None:
    rows = [{"symbol": "600000", "price": 10.0, "pct_chg": 1.0, "is_st": "1"}]
    quotes = daytime_fill.quote_index(rows)
    assert quotes["600000"]["is_st"] is True and quotes["600000"]["st_unknown"] is False


def test_named_non_st_row_is_not_marked_unknown() -> None:
    rows = [{"symbol": "600000", "name": "浦发银行", "price": 10.0, "pct_chg": 1.0}]
    quotes = daytime_fill.quote_index(rows)
    assert quotes["600000"]["is_st"] is False and quotes["600000"]["st_unknown"] is False


