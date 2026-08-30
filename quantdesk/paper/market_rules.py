"""Shared paper-account market-rule helpers.

The helpers in this module keep paper-only quantity handling in one place.
They are deliberately small and deterministic: no broker access, no live
orders, and no real-account state.  Defaults preserve the historical QuantDesk
paper-account convention of 100-share A-share lots.
"""

from __future__ import annotations

import math
from typing import Any

from quantdesk.board_rules import classify_board

# Historical QuantDesk paper-account convention: main-board A-share lots are
# 100 shares. Board-specific lots (e.g. STAR 200) come from ``classify_board``.
DEFAULT_LOT_SIZE = 100

# Single source of truth for the paper-only / no-broker boundary that every
# consumer-visible paper payload (buy/sell result, portfolio, position, trade,
# blocked order, delist action, closed position) must carry, so no payload can
# be mistaken for a real order or a broker connection.
PAPER_ONLY_BOUNDARY = {
    "is_real_order": False,
    "research_only": True,
    "paper_only": True,
    "paper_trading_only": True,
    "no_broker": True,
    "no_real_orders": True,
    "not_investment_advice": True,
    "not_position_advice": True,
}


def paper_only_boundary() -> dict[str, bool]:
    """Return a fresh copy of the shared paper-only / no-broker boundary."""

    return dict(PAPER_ONLY_BOUNDARY)

# Proximity tolerance for the paper limit-up/limit-down block gates, in percent.
# 0.2% == 20bps mirrors the board-rule tolerance and keeps a quote that is
# effectively at the daily limit from being treated as a reliable paper fill.
_LIMIT_TOLERANCE_PCT = 0.2

def _looks_like_st_prefix(text: str) -> bool:
    """True when *text* begins with an ST / *ST marker but not "STAR"-like text.

    ST must be followed by end-of-string or a non-ASCII-alphanumeric character
    (whitespace, brackets, CJK, dash). An ASCII-alphanumeric continuation such
    as the "A" in "STAR" is rejected so English board labels are not mistaken
    for ST status.
    """

    stripped = text.strip()
    if stripped.startswith("*"):
        stripped = stripped[1:]
    upper = stripped.upper()
    if not upper.startswith("ST"):
        return False
    tail = stripped[2:3]
    if not tail:
        return True
    return not (tail.isascii() and tail.isalnum())


def _normalized_lot_size(lot_size: Any) -> int:
    try:
        parsed = int(lot_size)
    except (TypeError, ValueError):
        return DEFAULT_LOT_SIZE
    return parsed if parsed > 0 else DEFAULT_LOT_SIZE


def round_down_to_lot(quantity: Any, *, lot_size: int = DEFAULT_LOT_SIZE) -> int:
    """Round *quantity* down to a whole lot.

    This preserves existing paper-account behaviour for A-shares:
    ``345 -> 300`` and values below one lot become ``0``.
    """

    lot = _normalized_lot_size(lot_size)
    try:
        parsed = int(quantity)
    except (TypeError, ValueError):
        return 0
    if parsed <= 0:
        return 0
    lots = parsed // lot
    return int(lots * lot)


def round_notional_down_to_lot_quantity(
    notional: Any,
    price: Any,
    *,
    lot_size: int = DEFAULT_LOT_SIZE,
) -> int:
    """Convert notional value to a whole-lot quantity rounded down.

    This centralizes the existing paper/backtest convention:
    ``floor(notional / price / lot_size) * lot_size``.  Invalid or non-positive
    values fail closed to ``0`` so callers do not accidentally create a
    tradable paper quantity from missing price/cash evidence.
    """

    lot = _normalized_lot_size(lot_size)
    try:
        notional_value = float(notional)
        price_value = float(price)
    except (TypeError, ValueError):
        return 0
    if not math.isfinite(notional_value) or not math.isfinite(price_value):
        return 0
    if notional_value <= 0 or price_value <= 0:
        return 0
    lots = math.floor(notional_value / price_value / lot)
    if lots <= 0:
        return 0
    return int(lots * lot)


def has_minimum_lot(quantity: Any, *, lot_size: int = DEFAULT_LOT_SIZE) -> bool:
    """Return True when *quantity* reaches at least one whole lot."""

    lot = _normalized_lot_size(lot_size)
    try:
        parsed = int(quantity)
    except (TypeError, ValueError):
        return False
    return parsed >= lot


def is_whole_lot(quantity: Any, *, lot_size: int = DEFAULT_LOT_SIZE) -> bool:
    """Return True when *quantity* is a positive exact whole-lot quantity."""

    lot = _normalized_lot_size(lot_size)
    try:
        parsed = int(quantity)
    except (TypeError, ValueError):
        return False
    return parsed > 0 and parsed % lot == 0


# Boards whose minimum order is the lot size but which allow single-share
# increments above that minimum (科创板 / 北交所), unlike the main board and
# ChiNext which trade in strict whole multiples of the lot.
_MIN_PLUS_ONE_INCREMENT_BOARDS = {"star", "bse"}


def is_valid_order_quantity(
    quantity: Any,
    *,
    lot_size: int = DEFAULT_LOT_SIZE,
    board_type: str = "main",
    held_shares: Any = None,
) -> bool:
    """Return True when *quantity* is a valid A-share paper order size.

    Rules by board:

    * Main board / ChiNext: strict whole multiples of the lot (100), min one lot.
    * STAR / BSE: minimum one lot (200 / 100); above the minimum any positive
      integer is allowed (1-share increments).

    Odd-lot sell exception: when ``held_shares`` is provided and the holding is
    itself a sub-lot remainder (e.g. 199 shares from a corporate action), that
    whole remainder may be sold in a single order even though it is below the
    board minimum. Selling a sub-lot quantity while holding more than one lot is
    not a valid clean order.
    """

    lot = _normalized_lot_size(lot_size)
    try:
        parsed = int(quantity)
    except (TypeError, ValueError):
        return False
    if parsed <= 0:
        return False

    # Odd-lot full liquidation: only when the entire holding is a sub-lot
    # remainder and the order clears it exactly.
    if held_shares is not None:
        try:
            held = int(held_shares)
        except (TypeError, ValueError):
            held = None
        if held is not None and 0 < held < lot and parsed == held:
            return True

    board = str(board_type or "main").strip().lower()
    if board in _MIN_PLUS_ONE_INCREMENT_BOARDS:
        return parsed >= lot
    return parsed % lot == 0


def board_type_for_symbol(
    symbol: Any,
    *,
    metadata: dict[str, Any] | None = None,
    name: Any = None,
) -> str:
    """Return the board type slug (main/chinext/star/bse/st/unknown) for a symbol."""

    code = str(symbol or "").strip()
    if not code:
        return "main"
    is_st = metadata_is_st(metadata, name)
    try:
        return str(classify_board(code, is_st=is_st).board_type)
    except Exception:
        return "main"


def lot_size_for_symbol(
    symbol: Any,
    *,
    metadata: dict[str, Any] | None = None,
    name: Any = None,
    default: int = DEFAULT_LOT_SIZE,
) -> int:
    """Return the board-aware paper lot size for a symbol.

    Defaults stay conservative and local-only: callers that do not provide a
    recognizable A-share symbol still get the historical 100-share convention.
    """

    code = str(symbol or "").strip()
    if not code:
        return _normalized_lot_size(default)
    is_st = metadata_is_st(metadata, name)
    try:
        rules = classify_board(code, is_st=is_st)
    except Exception:
        return _normalized_lot_size(default)
    lot = getattr(rules, "lot_size", None)
    return _normalized_lot_size(lot if lot else default)


def _truthy_metadata(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    text = str(value or "").strip().lower()
    return text in {"1", "yes", "true", "y", "t"}


def _optional_float(value: Any) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def metadata_is_st(metadata: dict[str, Any] | None, name: Any = None) -> bool:
    """Return True when metadata/name indicates ST status."""

    data = metadata if isinstance(metadata, dict) else {}
    for key in ("is_st", "is_st_current", "st"):
        if key in data and _truthy_metadata(data.get(key)):
            return True
    text = str(name if name is not None else data.get("security_name") or data.get("name") or "").strip()
    if not text:
        return False
    return _looks_like_st_prefix(text)


def paper_trade_block_reason(
    side: Any,
    metadata: dict[str, Any] | None,
    *,
    symbol: Any = None,
    name: Any = None,
) -> str:
    """Return a paper-only block reason for suspended/limit tradeability gates.

    Returns an empty string when no paper block applies. This never places a
    real order; it only decides whether a paper fill may be assumed.
    """

    data = metadata if isinstance(metadata, dict) else {}
    if _truthy_metadata(data.get("suspended")) or _truthy_metadata(data.get("is_suspended")):
        return "停牌或无有效成交数据，纸面交易已阻断"

    pct = _optional_float(data.get("pct_chg"))
    if pct is None:
        pct = _optional_float(data.get("change_pct"))
    if pct is None:
        return ""

    code = str(symbol or "").strip()
    is_st = metadata_is_st(data, name)
    try:
        rules = classify_board(code, is_st=is_st)
    except Exception:
        return ""

    side_upper = str(side or "").strip().upper()
    limit_up_pct = getattr(rules, "limit_up_pct", None)
    limit_down_pct = getattr(rules, "limit_down_pct", None)

    board_is_limit_up = (
        limit_up_pct is not None and pct >= float(limit_up_pct) - _LIMIT_TOLERANCE_PCT
    )
    board_is_limit_down = (
        limit_down_pct is not None and pct <= float(limit_down_pct) + _LIMIT_TOLERANCE_PCT
    )

    if side_upper == "BUY" and board_is_limit_up:
        return "涨停附近不可假设纸面买入成交"
    if side_upper == "SELL" and board_is_limit_down:
        return "跌停附近不可假设纸面卖出成交"
    return ""
