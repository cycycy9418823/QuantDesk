"""Batch 1A-G: the ledger header's mandate is read, not only written; ST / BSE
switches exist; the evening open/exit count includes today's daytime fills.

Before the fix every entry used DEFAULT_MANDATE (changing the header did
nothing), there was no ST/BSE switch, and execute_intents restarted the daily
open/exit count at zero even when daytime fills had already used it.
"""

from __future__ import annotations

from ai_trader import trader
from ai_trader.ledger import empty_ledger
from ai_trader.mandate import DEFAULT_MANDATE, mandate_from_ledger

DAY = "2026-09-28"


def _ledger(mandate: dict | None = None, trades: list[dict] | None = None) -> dict:
    book = empty_ledger(created_at="2026-09-25T20:20:00+08:00")
    book.update({"as_of": "2026-09-25", "cash": 100000.0, "equity": 100000.0, "trades": trades or []})
    if mandate is not None:
        book["mandate"] = {**book["mandate"], **mandate}
    return book


def _buys(*symbols: str) -> list[dict]:
    return [{"symbol": s, "side": "buy", "quantity": 100, "ref_close": 10.0} for s in symbols]


def _execute(book: dict, intents: list[dict], *, identity=None) -> list[dict]:
    symbols = [row["symbol"] for row in intents]
    _after, actions = trader.execute_intents(
        book, as_of=DAY, intents=intents, open_prices={s: 10.1 for s in symbols},
        prev_closes={s: 10.0 for s in symbols}, live_id=None,
        st_identity=identity or {s: (False, False) for s in symbols},
    )
    return actions


def test_header_overrides_the_rails_and_bad_values_are_ignored() -> None:
    resolved = mandate_from_ledger(_ledger({"max_opens_per_day": 1, "max_holdings": "bad",
                                            "min_cash_weight": 2.0, "initial_cash": 1.0, "min_holdings": 5}))
    assert resolved.max_opens_per_day == 1
    assert resolved.max_holdings == DEFAULT_MANDATE.max_holdings
    assert resolved.min_cash_weight == DEFAULT_MANDATE.min_cash_weight
    assert resolved.initial_cash == DEFAULT_MANDATE.initial_cash
    assert resolved.min_holdings == DEFAULT_MANDATE.min_holdings
    assert mandate_from_ledger(_ledger()) == DEFAULT_MANDATE


def test_execute_intents_obeys_the_header_without_a_caller_mandate() -> None:
    actions = _execute(_ledger({"max_opens_per_day": 1}), _buys("600001", "600002"))
    assert sum(1 for row in actions if row.get("status") == "filled") == 1


def test_st_and_bse_switches_default_on_and_refuse_when_turned_off() -> None:
    assert DEFAULT_MANDATE.allow_st is True and DEFAULT_MANDATE.allow_bse is True
    assert _execute(_ledger(), _buys("920001"))[0]["status"] == "filled"
    bse = _execute(_ledger({"allow_bse": False}), _buys("920001"))
    assert bse[0]["status"] == "rejected" and bse[0]["reason"].startswith("mandate_bse_not_allowed")
    st = _execute(_ledger({"allow_st": False}), _buys("600001"), identity={"600001": (False, True)})
    assert st[0]["status"] == "rejected" and "st_unknown" in st[0]["reason"]


def test_daytime_plan_obeys_the_header_switches() -> None:
    from ai_trader import daytime_fill

    intent = {"as_of": "2026-09-22", "slot": "0940", "created_at": "2026-09-22T09:40:03+08:00",
              "symbol": "920001", "side": "buy", "quantity": 100, "reason": "x", "status": "pending"}
    quotes = daytime_fill.quote_index(
        [{"symbol": "920001", "name": "北交样本", "price": 10.0, "pre_close": 9.9, "volume": 1000.0,
          "quote_time": "2026-09-22T09:45:05+08:00"}],
        quote_slot="0945", quote_time="2026-09-22T09:45:05+08:00", source="cloud_slot",
    )
    book = _ledger({"allow_bse": False})
    plan = daytime_fill.plan_fills([intent], quotes, book, as_of="2026-09-22",
                                   mandate=mandate_from_ledger(book))
    assert not plan["fills"]
    assert plan["mandate_blocks"][0]["blocked_by"] == daytime_fill.MANDATE_BSE_NOT_ALLOWED


def test_daytime_fills_already_today_use_the_same_open_quota() -> None:
    daytime = [{"date": DAY, "symbol": "600009", "side": "buy", "quantity": 100,
                "source": "daytime_snapshot"}]
    actions = _execute(_ledger(trades=daytime), _buys("600001", "600002"))
    assert sum(1 for row in actions if row.get("status") == "filled") == DEFAULT_MANDATE.max_opens_per_day - 1
