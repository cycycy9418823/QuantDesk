"""D4/T27–T30：盘中候选四项正确性（批内推进、保存失败与重试、三时刻、观察日生命周期）。

全部合成夹具（复用作者既有盘中测试的构造器），不联网、不调模型、不写正式账本。
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from ai_trader import daytime_fill, daytime_runner
from ai_trader.ledger import empty_ledger

from test_ai_trader_daytime_fill import (  # noqa: E402
    DAY,
    QUOTE_SLOT,
    QUOTE_TIME,
    _book,
    _intent,
    _paths,
    _row,
    _write_intents,
)

NEXT_DAY = "2026-09-23"
NOW = datetime(2026, 9, 22, 9, 45, 10)


def _quotes(rows: list[dict]) -> dict:
    return daytime_fill.quote_index(rows, quote_slot=QUOTE_SLOT, quote_time=QUOTE_TIME, source="synthetic")


def _position(quantity: int = 200) -> dict:
    return {"symbol": "600000", "quantity": quantity, "avg_cost": 10.0, "acquired_on": "2026-09-19"}


def test_batch_sell_cannot_oversell_the_same_position() -> None:
    """同一批里第二笔卖必须看到第一笔已经卖掉的持仓（不能累计超卖）。"""

    book = _book(positions=[_position(200)])
    intents = [
        _intent(side="sell", quantity=200),
        _intent(side="sell", quantity=200, slot="0941", created_at=f"{DAY}T09:41:00+08:00"),
    ]

    plan = daytime_fill.plan_fills(intents, _quotes([_row()]), book, as_of=DAY)

    assert len(plan["fills"]) == 1, plan["fills"]
    assert [row["blocked_by"] for row in plan["blocks"]] == [daytime_fill.BLOCK_NO_POSITION], plan["blocks"]


def test_batch_buy_sees_the_earlier_buy_cash_outflow() -> None:
    """同一批里第二笔买必须看到第一笔已经花掉的钱（不能超用现金）。"""

    book = _book(cash=20000.0, equity=100000.0, positions=[], trades=[])
    intents = [
        _intent(quantity=400),
        _intent(quantity=400, slot="0941", created_at=f"{DAY}T09:41:00+08:00"),
    ]

    plan = daytime_fill.plan_fills(intents, _quotes([_row(price=10.0)]), book, as_of=DAY)

    assert len(plan["fills"]) == 1, plan["fills"]
    assert [row["blocked_by"] for row in plan["mandate_blocks"]] == [daytime_fill.MANDATE_CASH_FLOOR], plan["mandate_blocks"]


def test_batch_result_does_not_depend_on_intent_order() -> None:
    """同一批的顺序不同，结论必须相同（去重与额度一致）。"""

    book = _book(positions=[_position(200)])
    first = _intent(side="sell", quantity=200)
    second = _intent(side="sell", quantity=200, slot="0941", created_at=f"{DAY}T09:41:00+08:00")
    quotes = _quotes([_row()])

    forward = daytime_fill.plan_fills([first, second], quotes, book, as_of=DAY)
    backward = daytime_fill.plan_fills([second, first], quotes, book, as_of=DAY)

    assert len(forward["fills"]) == len(backward["fills"]) == 1
    assert len(forward["blocks"]) == len(backward["blocks"]) == 1


def test_already_filled_intent_is_not_planned_twice() -> None:
    """已成交的意图不再进入计划（批内去重）。"""

    book = _book(positions=[_position(200)])
    intent = _intent(side="sell", quantity=200)
    key = daytime_fill.intent_key(intent)

    plan = daytime_fill.plan_fills([intent], _quotes([_row()]), book, as_of=DAY, already_filled={key})

    assert plan["fills"] == [] and plan["blocks"] == []



def _slot(tmp_path: Path, monkeypatch, *, save=None, dry: bool = False, book: dict | None = None):
    paths = _paths(tmp_path)
    _write_intents(paths, [_intent()])
    monkeypatch.setenv(daytime_fill.DRY_ENV, "1" if dry else "0")
    result = daytime_fill.fill_slot(paths=paths, now=NOW, rows=[_row()], quote_slot=QUOTE_SLOT,
                                    quote_time=QUOTE_TIME, rows_source="synthetic",
                                    ledger=book or _book(), write=True, dry=dry, save=save)
    return result, paths


def test_save_failure_is_never_reported_as_success(tmp_path: Path, monkeypatch) -> None:
    def boom(ledger, path):
        raise RuntimeError("disk_full")

    result, paths = _slot(tmp_path, monkeypatch, save=boom)

    assert result["written"] is False and result["ledger_written"] is False, result
    assert "disk_full" in str(result.get("save_error") or ""), result
    assert not (paths.daytime_notes.parent / daytime_fill.FILLS_NAME).is_file(), (
        "保存失败时不得留下会被当成已成交的记录"
    )


def test_retry_after_save_failure_applies_exactly_once(tmp_path: Path, monkeypatch) -> None:
    """保存失败后重试：账本只记一次，且成交记录与账本一致。"""

    def boom(ledger, path):
        raise RuntimeError("disk_full")

    _first, paths = _slot(tmp_path, monkeypatch, save=boom)
    saved: list[dict] = []

    def _save(ledger: dict, path: Path) -> Path:
        saved.append(json.loads(json.dumps(ledger)))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(ledger, ensure_ascii=False), encoding="utf-8")
        return path

    second = daytime_fill.fill_slot(paths=paths, now=NOW, rows=[_row()], quote_slot=QUOTE_SLOT,
                                    quote_time=QUOTE_TIME, rows_source="synthetic",
                                    ledger=_book(), write=True, dry=False, save=_save)
    third = daytime_fill.fill_slot(paths=paths, now=NOW, rows=[_row()], quote_slot=QUOTE_SLOT,
                                   quote_time=QUOTE_TIME, rows_source="synthetic",
                                   ledger=saved[-1], write=True, dry=False, save=_save)

    assert second["ledger_written"] is True and len(saved[0]["trades"]) == 1, saved
    assert third["fills"] == 0, "重试不得再记一笔成交"
    assert len(saved[-1]["trades"]) == 1, saved[-1]["trades"]


def test_ledger_already_holding_the_fill_is_not_applied_twice(tmp_path: Path) -> None:
    """账本已带 intent_key 时，即使成交记录丢失，重试也不会重复扣钱（T28 幂等）。"""

    paths = _paths(tmp_path)
    intent = _intent()
    key = daytime_fill.intent_key(intent)
    book = _book()
    book["trades"] = [{"date": DAY, "symbol": "600000", "side": "buy", "quantity": 200,
                       "daytime_intent_key": key}]
    book["cash"] = 97990.0
    paths.ledger.parent.mkdir(parents=True, exist_ok=True)
    paths.ledger.write_text(json.dumps(book, ensure_ascii=False), encoding="utf-8")

    assert key in daytime_fill.fill_keys(paths, DAY)
    plan = daytime_fill.plan_fills([intent], _quotes([_row()]), book, as_of=DAY,
                                   already_filled=daytime_fill.fill_keys(paths, DAY))
    assert plan["fills"] == []



def test_quote_and_received_times_are_kept_apart() -> None:
    """报价时刻与收到时刻必须分开保存，不能合成一个时间。"""

    index = daytime_fill.quote_index(
        [_row(quote_time="2026-09-22T09:45:05+08:00", fetched_at="2026-09-22T09:45:31+08:00")],
        quote_slot=QUOTE_SLOT,
    )
    quote = index["600000"]

    assert quote["quote_time"] == "2026-09-22T09:45:05+08:00"
    assert quote["received_at"] == "2026-09-22T09:45:31+08:00"
    assert quote["time"] == quote["quote_time"] and quote["time_source"] == "quote"


def test_stale_quote_cannot_back_a_fill() -> None:
    """报价本身早于意图时不得成交——哪怕我们是刚刚才收到这张快照。"""

    rows = [_row(quote_time="2026-09-22T09:39:00+08:00", fetched_at=QUOTE_TIME)]

    plan = daytime_fill.plan_fills([_intent()], _quotes(rows), _book(), as_of=DAY)

    assert plan["fills"] == [], plan["fills"]
    assert [row["blocked_by"] for row in plan["blocks"]] == [daytime_fill.BLOCK_NOT_LATER], plan["blocks"]


def test_fresh_quote_still_fills() -> None:
    """正常对照：报价晚于意图时照旧成交。"""

    plan = daytime_fill.plan_fills([_intent()], _quotes([_row()]), _book(), as_of=DAY)

    assert len(plan["fills"]) == 1, plan


def test_first_dry_day_is_not_ended_by_same_day_records(tmp_path: Path) -> None:
    """首个观察日按交易日推进：同一天再写多少记录都还是首日。"""

    paths = _paths(tmp_path)
    assert daytime_runner.is_first_trading_day(paths, as_of=DAY) is True

    calendar = tmp_path / "data/calendar/a_share_trading_calendar.csv"
    calendar.parent.mkdir(parents=True)
    calendar.write_text(f"date,is_open\n{DAY},1\n{NEXT_DAY},1\n", encoding="utf-8")
    daytime_runner.mark_dry_day_observed(paths, as_of=DAY, root=tmp_path,
        observation={"status": "decided", "written": True, "valid_quotes": 1})
    assert daytime_runner.is_first_trading_day(paths, as_of=DAY, root=tmp_path) is True, "同日一条记录不得提前结束首日"

    (paths.daytime_notes.parent / daytime_fill.DRY_FILLS_NAME).write_text(
        json.dumps({"as_of": DAY, "intent_key": "x"}, ensure_ascii=False) + "\n", encoding="utf-8")
    assert daytime_runner.is_first_trading_day(paths, as_of=DAY) is True

    assert daytime_runner.is_first_trading_day(paths, as_of=NEXT_DAY, root=tmp_path) is False, "次日应推进到正常模式"


def test_real_trade_ends_the_first_day(tmp_path: Path) -> None:
    """账上有真实成交后就不再是首日。"""

    paths = _paths(tmp_path)
    ledger = empty_ledger()
    ledger["trades"] = [{"date": DAY, "symbol": "600000", "side": "buy", "quantity": 100}]
    paths.ledger.parent.mkdir(parents=True, exist_ok=True)
    paths.ledger.write_text(json.dumps(ledger, ensure_ascii=False), encoding="utf-8")

    assert daytime_runner.is_first_trading_day(paths, as_of=DAY) is False
