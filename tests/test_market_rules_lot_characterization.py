"""Behaviour-lock (characterization) tests for the shared market-rule helpers.

These tests exist to make the P0 "unify inline lot math into market_rules"
refactor safe. They pin the *current* numeric behaviour of the shared lot
helpers so the refactor can be proven behaviour-preserving rather than a blind
string swap.

Two guarantees are asserted explicitly:

1. Main-board (100-share lot) results are byte-identical to the historical
   inline formulas ``int(math.floor(notional / price / lot) * lot)`` /
   ``int(value // price // 100 * 100)``. These MUST NOT change.
2. Board-specific lots (STAR 200, ST 100, ChiNext 100, BSE 100) are recorded
   here as the single source of truth, so any future change to a non-main
   board is a deliberate, reviewed edit to this file — never a silent drift.
"""

from __future__ import annotations

import math

import pytest

from quantdesk.paper.market_rules import (
    DEFAULT_LOT_SIZE,
    lot_size_for_symbol,
    metadata_is_st,
    paper_trade_block_reason,
    round_down_to_lot,
    round_notional_down_to_lot_quantity,
)


def _legacy_notional_to_lots(notional: float, price: float, lot: int) -> int:
    """The historical inline formula used across backtest/paper/research."""

    if price <= 0 or notional <= 0:
        return 0
    return int(math.floor(notional / price / lot) * lot)


def _legacy_value_floor_lot(value: float, price: float) -> int:
    """position_planner's historical ``value // price // 100 * 100``."""

    if price <= 0:
        return 0
    return int(value // price // 100 * 100)


class TestNotionalLotMatchesLegacyMainBoard:
    @pytest.mark.parametrize("notional,price,lot", [
        (10_000, 7, 100),
        (100_000, 8, 100),
        (999, 10, 100),
        (50_000, 12.5, 100),
        (10_000, 7, 200),   # STAR lot; still floor-consistent
        (345.0, 1, 100),
        (0, 8, 100),
        (10_000, 0, 100),   # invalid price fails closed to 0
    ])
    def test_matches_legacy_floor_formula(self, notional, price, lot):
        assert round_notional_down_to_lot_quantity(notional, price, lot_size=lot) == _legacy_notional_to_lots(notional, price, lot)

    def test_walk_forward_percent_cap_identity(self):
        # walk_forward: int(cash * max_position_pct / exec_price / 100) * 100
        cash, mpct, price = 100_000, 0.1, 8.0
        legacy = int(cash * mpct / price / 100) * 100
        assert round_notional_down_to_lot_quantity(cash * mpct, price) == legacy == 1200

    def test_risk_position_cap_identity(self):
        total_capital, mpct, price = 100_000, 0.1, 8.0
        legacy = int(total_capital * mpct / price / 100) * 100
        assert round_notional_down_to_lot_quantity(total_capital * mpct, price) == legacy == 1200

    def test_position_planner_nested_floor_identity(self):
        value, price = 10_000, 7.0
        assert round_notional_down_to_lot_quantity(value, price) == _legacy_value_floor_lot(value, price) == 1400


class TestRoundDownToLotMatchesLegacy:
    @pytest.mark.parametrize("quantity,expected", [
        (345, 300),
        (99, 0),
        (100, 100),
        (250, 200),
        (0, 0),
        (-5, 0),
    ])
    def test_round_down_to_lot(self, quantity, expected):
        assert round_down_to_lot(quantity) == expected
        # legacy inline was int(q // 100 * 100) for positive q
        if quantity > 0:
            assert round_down_to_lot(quantity) == int(quantity // 100 * 100)


class TestBoardLotSizesAreDocumented:
    """Single source of truth for per-board paper lot size. A diff here is a
    deliberate board-rule change, not an accident."""

    @pytest.mark.parametrize("symbol,expected_lot", [
        ("600001", 100),   # 主板
        ("000001", 100),   # 深主板
        ("300001", 100),   # 创业板
        ("688001", 200),   # 科创板
        ("430001", 100),   # 北交所
        ("830001", 100),   # 北交所
    ])
    def test_lot_size_by_board(self, symbol, expected_lot):
        assert lot_size_for_symbol(symbol) == expected_lot

    def test_st_symbol_lot_size(self):
        # ST keeps a 100-share lot on the main board.
        assert lot_size_for_symbol("600001", metadata={"is_st": True}) == 100

    def test_default_lot_size_constant(self):
        assert DEFAULT_LOT_SIZE == 100


class TestBoardAwareLimitBlockDocumented:
    """Lock the board-aware paper block thresholds so the refactor of
    paper_trading's inline limit check preserves behaviour."""

    def test_main_board_near_limit_up_blocks_buy(self):
        assert "涨停" in paper_trade_block_reason("BUY", {"pct_chg": 9.9}, symbol="000001", name="sample")

    def test_chinext_10pct_does_not_block(self):
        # ChiNext limit is 20%; 10% is nowhere near.
        assert paper_trade_block_reason("BUY", {"pct_chg": 10.0}, symbol="300001", name="sample") == ""

    def test_st_5pct_blocks_buy(self):
        assert "涨停" in paper_trade_block_reason("BUY", {"pct_chg": 5.0}, symbol="600001", name="*ST sample")

    def test_main_board_near_limit_down_blocks_sell(self):
        assert "跌停" in paper_trade_block_reason("SELL", {"change_pct": -9.9}, symbol="000001", name="sample")

    def test_suspended_blocks(self):
        assert "停牌" in paper_trade_block_reason("BUY", {"suspended": True}, symbol="000001", name="sample")

    def test_st_detection_rejects_star(self):
        assert metadata_is_st({}, "*ST样本")
        assert not metadata_is_st({}, "STAR sample")
