"""Batch 1A-A wiring: every paper fill gate compares the booked price with the
cent-rounded limit price (quantdesk.board_rules), not "pct >= limit - tolerance".

Each case below filled on the old code. Charter §2 invariant 2.
"""

from __future__ import annotations

from ai_trader import daytime_fill, trader
from ai_trader.ledger import empty_ledger
from quantdesk.paper.auto_paper_portfolio import _fill_tradability_rejection
from quantdesk.paper.market_rules import paper_trade_block_reason
from quantdesk.paper.matching import evaluate_next_open_fill, exchange_pre_close

DAY = "2026-09-22"


def _bar(open_px: float, *, close: float | None = None, change: float | None = None, **extra) -> dict:
    row = {
        "date": DAY, "open": open_px, "high": open_px, "low": open_px,
        "close": close if close is not None else open_px, "volume": 1000.0,
        "open_status": "ok", "volume_status": "ok", "price_basis": "raw",
        "source": "unit_daily", "fetched_at": f"{DAY}T09:31:00+08:00",
    }
    if change is not None:
        row["original_row"] = {"close": str(row["close"]), "change": str(change)}
    row.update(extra)
    return row


def test_next_open_low_priced_limit_up_is_refused() -> None:
    # pre-close 1.04 -> limit 1.14 (+9.615%): the old "limit - 0.3pp" gate let it fill.
    decision = evaluate_next_open_fill(_bar(1.14), symbol="600001", side="buy", prev_close=1.04, trade_date=DAY)
    assert decision["ok"] is False and decision["reason"].startswith("limit_up:")
    ok = evaluate_next_open_fill(_bar(1.13), symbol="600001", side="buy", prev_close=1.04, trade_date=DAY)
    assert ok["ok"] is True
    assert ok["price_evidence"]["limit_check"]["up_limit"] == 1.14


def test_next_open_uses_the_exchange_pre_close_on_an_ex_rights_day() -> None:
    # 10-for-10 bonus: yesterday closed 10.00, exchange pre-close is 5.00, limit 5.50.
    bar = _bar(5.50, close=5.50, change=0.50)
    assert exchange_pre_close(bar, 10.0) == (5.0, "bar_close_minus_change")
    decision = evaluate_next_open_fill(bar, symbol="600001", side="buy", prev_close=10.0, trade_date=DAY)
    assert decision["ok"] is False and decision["reason"].startswith("limit_up:")


def test_execute_intents_refuses_low_priced_limit_open() -> None:
    ledger = empty_ledger(created_at=f"{DAY}T08:00:00+08:00")
    ledger.update({"cash": 100000.0, "equity": 100000.0, "as_of": "2026-09-21"})
    intents = [{"symbol": "600001", "side": "buy", "quantity": 1000, "ref_close": 1.04,
                "abandon_if_open_gap_pct": None}]
    _book, actions = trader.execute_intents(
        ledger, as_of=DAY, intents=intents, open_prices={"600001": 1.14},
        prev_closes={"600001": 1.04}, live_id=None,
    )
    assert actions[0]["status"] == "rejected" and actions[0]["reason"].startswith("limit_up:")
    # limit_bases (exchange pre-close) overrides the intent's reference close.
    _book2, actions2 = trader.execute_intents(
        ledger, as_of=DAY, intents=intents, open_prices={"600001": 1.14},
        prev_closes={"600001": 1.04}, live_id=None, limit_bases={"600001": 1.10},
    )
    assert actions2[0]["status"] == "filled"


def _daytime_plan(rows: list[dict], *, as_of: str = DAY) -> dict:
    intent = {
        "as_of": as_of, "slot": "0940", "created_at": f"{as_of}T09:40:03+08:00",
        "symbol": "600001", "side": "buy", "quantity": 1000, "reason": "test", "status": "pending",
    }
    quotes = daytime_fill.quote_index(rows, quote_slot="0945", quote_time=f"{as_of}T09:45:05+08:00",
                                      source="cloud_slot")
    book = {"cash": 100000.0, "equity": 100000.0, "positions": [], "trades": []}
    return daytime_fill.plan_fills([intent], quotes, book, as_of=as_of)


def _quote(as_of: str = DAY, **overrides) -> dict:
    row = {"symbol": "600001", "name": "样本", "price": 1.14, "quote_time": f"{as_of}T09:45:05+08:00"}
    row.update(overrides)
    return row


def test_daytime_low_priced_limit_up_is_refused() -> None:
    plan = _daytime_plan([_quote(pre_close=1.04, pct_chg=9.615)])
    assert not plan["fills"] and plan["blocks"][0]["blocked_by"] == daytime_fill.BLOCK_LIMIT_UP


def test_daytime_quote_without_pre_close_or_pct_does_not_fill() -> None:
    # Old code: missing pct_chg was "not a limit block", so the row filled at any price.
    plan = _daytime_plan([_quote(price=1.10)])
    assert not plan["fills"]
    assert plan["blocks"][0]["blocked_by"] == daytime_fill.BLOCK_LIMIT_UNVERIFIABLE


def test_daytime_st_name_before_2026_07_06_still_uses_five_percent() -> None:
    day = "2026-07-03"
    plan = _daytime_plan([_quote(day, name="*ST样本", price=10.5, pre_close=10.0, pct_chg=5.0)], as_of=day)
    assert not plan["fills"] and plan["blocks"][0]["blocked_by"] == daytime_fill.BLOCK_LIMIT_UP


def _daily_quote(**overrides) -> dict:
    quote = {"date": DAY, "price": 11.0, "open": 10.2, "high": 11.0, "low": 10.1, "close": 11.0,
             "volume": 5000.0, "pct_chg": 10.0, "change": 1.0, "previous_row_close": 10.0,
             "price_basis": "raw", "price_basis_reason": ""}
    quote.update(overrides)
    return quote


def test_strategy_book_refuses_a_close_sealed_at_the_limit_after_trading() -> None:
    # Not a one-word board (open 10.2, low 10.1) -> the old check let it fill.
    reason = _fill_tradability_rejection(_daily_quote(), "buy", fill_price=11.0, symbol="600001")
    assert "涨停" in reason


def test_strategy_book_refuses_low_priced_one_word_limit() -> None:
    quote = _daily_quote(price=1.14, open=1.14, high=1.14, low=1.14, close=1.14,
                         pct_chg=9.615, change=0.10, previous_row_close=1.04)
    assert "涨停" in _fill_tradability_rejection(quote, "buy", fill_price=1.14, symbol="600001")


def test_strategy_book_refuses_sell_at_limit_down_and_unverifiable_quote() -> None:
    down = _daily_quote(price=9.0, open=9.5, high=9.6, low=9.0, close=9.0, pct_chg=-10.0, change=-1.0)
    assert "跌停" in _fill_tradability_rejection(down, "sell", require_quote=True, fill_price=9.0, symbol="600001")
    bare = _daily_quote(pct_chg=None, change=None, previous_row_close=None)
    assert "缺少前收" in _fill_tradability_rejection(bare, "buy", fill_price=10.5, symbol="600001")
    assert _fill_tradability_rejection(_daily_quote(close=10.8, price=10.8, change=0.8), "buy",
                                       fill_price=10.8, symbol="600001") == ""


def test_market_rules_exact_path_catches_low_priced_limit() -> None:
    assert paper_trade_block_reason("BUY", {"pct_chg": 9.62}, symbol="600001") == ""  # legacy pct path
    assert "涨停" in paper_trade_block_reason(
        "BUY", {"price": 1.14, "pre_close": 1.04, "trade_date": DAY}, symbol="600001"
    )
