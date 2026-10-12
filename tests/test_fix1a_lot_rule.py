"""Batch 1A-H: one order-size rule for every paper path.

Before the fix three rules disagreed: daytime_decide demanded a multiple of 100,
daytime_fill a multiple of the board lot (STAR 200), and market_rules allowed
STAR "200 起 1 股递增". A STAR order of 300 shares was accepted by the decider and
refused by the fill; 250 was refused by both although the exchange allows it.
"""

from __future__ import annotations

from ai_trader.daytime_decide import parse_orders
from ai_trader.daytime_fill import BLOCK_NOT_A_LOT, plan_fills, quote_index
from quantdesk.paper.market_rules import order_quantity_rule


def test_shared_rule_by_board() -> None:
    assert order_quantity_rule("600000", 200) == ""
    assert order_quantity_rule("600000", 150) != ""
    assert order_quantity_rule("300001", 100) == ""
    assert order_quantity_rule("688001", 250) == ""
    assert order_quantity_rule("688001", 100) != ""
    assert order_quantity_rule("689009", 201) == ""
    assert order_quantity_rule("920001", 101) == ""
    assert order_quantity_rule("600000", 37, held_shares=37) == ""  # sub-lot remainder sell


def _order(symbol: str, qty: int) -> dict:
    return {"orders": [{"symbol": symbol, "side": "buy", "qty": qty, "reason": "本槽事实"}]}


def test_decider_and_fill_agree_on_star_and_main_board() -> None:
    accepted, rejected = parse_orders(_order("688001", 250))
    assert accepted and not rejected
    accepted, rejected = parse_orders(_order("600000", 150))
    assert not accepted and "qty_not_a_lot" in rejected[0]["problems"]

    intent = {"as_of": "2026-09-22", "slot": "0940", "created_at": "2026-09-22T09:40:03+08:00",
              "symbol": "688001", "side": "buy", "quantity": 250, "reason": "x", "status": "pending"}
    quotes = quote_index([{"symbol": "688001", "name": "科创样本", "price": 20.0, "pre_close": 19.8,
                           "volume": 1000.0, "quote_time": "2026-09-22T09:45:05+08:00"}],
                         quote_slot="0945", quote_time="2026-09-22T09:45:05+08:00", source="cloud_slot")
    book = {"cash": 100000.0, "equity": 100000.0, "positions": [], "trades": []}
    plan = plan_fills([intent], quotes, book, as_of="2026-09-22")
    assert len(plan["fills"]) == 1
    odd = dict(intent, quantity=150)
    plan_odd = plan_fills([odd], quotes, book, as_of="2026-09-22")
    assert not plan_odd["fills"] and plan_odd["blocks"][0]["blocked_by"] == BLOCK_NOT_A_LOT
