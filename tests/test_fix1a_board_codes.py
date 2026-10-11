"""Batch 1A-H: 689xxx (STAR depositary receipts) and 302xxx (ChiNext) are real boards.

Before the fix both prefixes classified as UNKNOWN, so 689009 / 302132 fell back to
the main-board 10% band and the 100-share lot instead of their own board rules.
"""

from __future__ import annotations

from quantdesk.board_rules import classify_board
from quantdesk.data_quality import classify_market
from quantdesk.paper.market_rules import lot_size_for_symbol


def test_689_is_star_and_302_is_chinext() -> None:
    assert classify_market("689009") == "STAR"
    assert classify_market("302132") == "CHINEXT"
    assert classify_board("689009").board_type == "star"
    assert classify_board("689009").limit_up_pct == 20.0
    assert classify_board("302132").board_type == "chinext"
    assert classify_board("302132").limit_up_pct == 20.0
    assert lot_size_for_symbol("689009") == 200


def test_other_prefixes_unchanged() -> None:
    assert classify_market("688001") == "STAR"
    assert classify_market("300001") == "CHINEXT"
    assert classify_market("301001") == "CHINEXT"
    assert classify_market("600000") == "SH"
    assert classify_market("000001") == "SZ"
    assert classify_market("920001") == "BSE"
    assert classify_market("303001") == "UNKNOWN"
    assert classify_market("690001") == "UNKNOWN"
