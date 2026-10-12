"""Tests for the delisting liquidation engine (Phase 3 退市清算).

Verifies that delisted holdings are liquidated out of a portfolio state, that a
missing PIT data source is an honest no-op (never silently drops holdings), and
that the scan reports coverage truthfully.
"""
from __future__ import annotations

import quantdesk.paper.delist_liquidation as dl
from quantdesk.data_truth.pit_st_delist_loader import DelistRecord, StDelistData


def _data_with_delist() -> StDelistData:
    return StDelistData(
        st_intervals={},
        delist_records={
            "000002": DelistRecord(
                symbol="000002",
                name="退市示例",
                delist_date="2024-05-01",
                last_trade_date="2024-04-20",
                delist_reason="面值退市",
            )
        },
        st_source="local_csv",
        delist_source="local_csv",
    )


def _empty_data() -> StDelistData:
    return StDelistData(st_intervals={}, delist_records={}, st_source="absent", delist_source="absent")


def test_scan_flags_delisted_position() -> None:
    positions = [
        {"symbol": "000002", "quantity": 1000, "latest_price": 5.0, "avg_cost": 8.0},
        {"symbol": "600000", "quantity": 500, "latest_price": 10.0, "avg_cost": 9.0},
    ]
    result = dl.scan_positions_for_delisting(positions, as_of="2024-06-01", data=_data_with_delist())
    flagged = {a.symbol for a in result.actions}
    assert "000002" in flagged
    assert "600000" not in flagged
    assert result.coverage == "local_csv"


def test_scan_respects_as_of_before_delist() -> None:
    # Before the delist date the position is still listed -> not liquidated.
    positions = [{"symbol": "000002", "quantity": 1000, "latest_price": 5.0, "avg_cost": 8.0}]
    result = dl.scan_positions_for_delisting(positions, as_of="2024-03-01", data=_data_with_delist())
    flagged = {a.symbol for a in result.actions}
    assert "000002" not in flagged


def test_apply_moves_delisted_to_closed_and_credits_cash() -> None:
    state = {
        "cash": 0.0,
        "positions": [
            {"symbol": "000002", "quantity": 1000, "latest_price": 5.0, "avg_cost": 8.0},
            {"symbol": "600000", "quantity": 500, "latest_price": 10.0, "avg_cost": 9.0},
        ],
        "closed_positions": [],
    }
    # 1A: liquidation needs an observed last-tradable close; without one the
    # holding is kept as "no price, pending manual" (tests/test_fix1a_delist_no_invented_price.py).
    out = dl.apply_delisting_liquidation(
        state, as_of="2024-06-01", data=_data_with_delist(), last_close_lookup={"000002": 4.2}
    )
    assert out["ok"] is True
    assert out["liquidated"] >= 1
    new_state = out["state"]
    held = {p["symbol"] for p in new_state["positions"]}
    closed = {p["symbol"] for p in new_state["closed_positions"]}
    assert "000002" not in held  # delisted name removed from holdings
    assert "600000" in held      # normal holding preserved
    assert "000002" in closed    # moved to closed for traceability
    # Liquidation credits some cash (haircut proceeds), never negative.
    assert new_state["cash"] >= 0.0


def test_absent_data_is_honest_noop() -> None:
    state = {
        "cash": 0.0,
        "positions": [{"symbol": "000002", "quantity": 1000, "latest_price": 5.0, "avg_cost": 8.0}],
        "closed_positions": [],
    }
    out = dl.apply_delisting_liquidation(state, as_of="2024-06-01", data=_empty_data())
    # No PIT data -> nothing liquidated, holdings untouched, but a warning fires.
    assert out["liquidated"] == 0
    assert out["coverage"] == "absent"
    assert out["warnings"]  # must warn that survivorship bias is NOT handled
    assert {p["symbol"] for p in out["state"]["positions"]} == {"000002"}


def test_apply_with_no_delisted_holdings_is_clean() -> None:
    state = {
        "cash": 100.0,
        "positions": [{"symbol": "600000", "quantity": 500, "latest_price": 10.0, "avg_cost": 9.0}],
        "closed_positions": [],
    }
    out = dl.apply_delisting_liquidation(state, as_of="2024-06-01", data=_data_with_delist())
    assert out["liquidated"] == 0
    assert {p["symbol"] for p in out["state"]["positions"]} == {"600000"}
