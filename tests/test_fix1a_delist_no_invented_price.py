"""Batch 1A-H: a delisted holding with no observed price is never booked at an
invented price. Before the fix it was closed at 75% of its stale mark and the
"proceeds" were credited to cash (charter §2: 不编造行情, 缺数据不成交).
"""

from __future__ import annotations

import quantdesk.paper.delist_liquidation as dl
from quantdesk.data_truth.pit_st_delist_loader import DelistRecord, StDelistData


def _data() -> StDelistData:
    return StDelistData(
        st_intervals={},
        delist_records={"000002": DelistRecord(symbol="000002", name="退市示例", delist_date="2024-05-01",
                                               last_trade_date="2024-04-20", delist_reason="面值退市")},
        st_source="local_csv",
        delist_source="local_csv",
    )


def _state() -> dict:
    return {
        "cash": 100.0,
        "positions": [{"symbol": "000002", "quantity": 1000, "latest_price": 5.0, "avg_cost": 8.0}],
        "closed_positions": [],
    }


def test_no_price_keeps_the_holding_frozen_and_moves_no_cash() -> None:
    out = dl.apply_delisting_liquidation(_state(), as_of="2024-06-01", data=_data())
    state = out["state"]
    assert state["cash"] == 100.0
    assert state["closed_positions"] == []
    held = state["positions"][0]
    assert held["management_state"] == "delist_pending_manual" and held["can_sell"] is False
    assert held["delist_status"] == dl.NO_PRICE_PENDING_MANUAL
    action = out["actions"][0]
    assert action["action"] == "delist_pending_manual"
    assert action["liquidation_price"] is None and action["proceeds"] is None
    assert out["pending_manual_symbols"] == ["000002"] and out["liquidated"] == 0
    assert "delist_pending_manual:000002" in out["warnings"]


def test_observed_last_close_still_liquidates_at_that_price() -> None:
    out = dl.apply_delisting_liquidation(_state(), as_of="2024-06-01", data=_data(),
                                         last_close_lookup={"000002": 4.2})
    closed = out["state"]["closed_positions"][0]
    assert closed["exit_price"] == 4.2 and closed["price_basis"] == "last_tradable_close"
    assert out["state"]["cash"] == 100.0 + 4200.0
