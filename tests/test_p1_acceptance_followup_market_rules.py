"""Acceptance follow-up tests for three market-rule modeling defects.

1. STAR lot semantics: buy minimum is the board lot (200), and above the
   minimum shares increment by 1 (not multiples of 200). A sub-lot remainder
   holding must be sellable in one order.
2. ST limit override must not clobber the board: only main-board ST is ±5%.
   ChiNext / STAR risk-warning stocks keep their board limit (±20%).
3. Blocked paper results must carry the paper-only / no-broker boundary, and a
   loaded portfolio must force-correct a stale is_real_order=true.
"""

from __future__ import annotations

from pathlib import Path

from quantdesk.board_rules import BoardType, classify_board
from quantdesk.paper import paper_trading
from quantdesk.paper.market_rules import is_valid_order_quantity


class TestStarLotSemantics:
    def test_star_buy_minimum_is_200_then_increments_by_one(self):
        # Below the minimum is invalid; at/above the minimum any integer is fine.
        assert not is_valid_order_quantity(100, lot_size=200, board_type="star")
        assert not is_valid_order_quantity(199, lot_size=200, board_type="star")
        assert is_valid_order_quantity(200, lot_size=200, board_type="star")
        assert is_valid_order_quantity(201, lot_size=200, board_type="star")
        assert is_valid_order_quantity(300, lot_size=200, board_type="star")

    def test_main_board_still_requires_whole_lots(self):
        assert is_valid_order_quantity(100, lot_size=100, board_type="main")
        assert not is_valid_order_quantity(150, lot_size=100, board_type="main")
        assert is_valid_order_quantity(300, lot_size=100, board_type="main")

    def test_star_odd_remainder_can_be_sold_in_full(self):
        # A 199-share remainder (e.g. from a corporate action) must be sellable
        # all at once even though 199 < 200 minimum.
        assert is_valid_order_quantity(199, lot_size=200, board_type="star", held_shares=199)
        # But selling 199 while holding 300 is not a valid clean order.
        assert not is_valid_order_quantity(199, lot_size=200, board_type="star", held_shares=300)


class TestStBoardLimitOverride:
    def test_main_board_st_is_5pct(self):
        rules = classify_board("600000", is_st=True)
        assert rules.board_type == BoardType.ST
        assert rules.limit_up_pct == 5.0

    def test_chinext_st_keeps_20pct(self):
        rules = classify_board("300001", is_st=True)
        assert rules.limit_up_pct == 20.0
        assert rules.limit_down_pct == -20.0

    def test_star_st_keeps_20pct(self):
        rules = classify_board("688001", is_st=True)
        assert rules.limit_up_pct == 20.0

    def test_bse_st_keeps_30pct(self):
        rules = classify_board("830001", is_st=True)
        assert rules.limit_up_pct == 30.0


class TestBlockedResultBoundary:
    def test_error_block_carries_paper_boundary(self, tmp_path: Path):
        path = tmp_path / "paper.json"
        # 100-share STAR buy is below the 200 minimum -> blocked.
        blocked = paper_trading.paper_buy(
            "688001", "sample", 10.0, 100, path=path,
            metadata={"trade_date": "2026-06-18", "pct_chg": 0.0},
        )
        assert blocked["ok"] is False
        assert blocked["is_real_order"] is False
        assert blocked["paper_only"] is True
        assert blocked["no_broker"] is True
        assert blocked["research_only"] is True

    def test_loaded_portfolio_force_corrects_stale_real_order_flag(self, tmp_path: Path):
        path = tmp_path / "paper.json"
        paper_trading.paper_buy("000001", "sample", 10.0, 100, path=path, metadata={"trade_date": "2026-06-18"})
        # Corrupt the on-disk state to look like a real order.
        import json
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        data["is_real_order"] = True
        data["positions"][0]["is_real_order"] = True
        Path(path).write_text(json.dumps(data), encoding="utf-8")

        reloaded = paper_trading.load_paper_portfolio(path)
        assert reloaded["is_real_order"] is False
        assert reloaded["positions"][0]["is_real_order"] is False
