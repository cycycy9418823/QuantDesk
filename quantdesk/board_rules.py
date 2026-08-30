"""A 股板块规则引擎 (Board-Specific Rule Engine)。

根据股票代码前缀自动识别板块（主板/创业板/科创板/ST/北交所），
返回对应的涨跌停阈值、交易规则，替代固定 ±9.8% 阈值。

仅用于本地研究回测模拟，不构成投资建议，不连接任何真实交易系统。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from quantdesk.data_quality import classify_market

# ──────────────────────────────────────────────────────────────────────────────
# 板块类型与规则
# ──────────────────────────────────────────────────────────────────────────────

class BoardType:
    MAIN = "main"           # 主板（沪市 60xxxx / 深市 00xxxx）
    CHINEXT = "chinext"     # 创业板（300/301）
    STAR = "star"           # 科创板（688）
    BSE = "bse"             # 北交所（83/87/43/920）
    ST = "st"               # ST/*ST 股票
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class BoardRules:
    """某一板块的涨跌停阈值和交易规则。"""

    board_type: str
    limit_up_pct: float     # 涨停百分比（正数，如 10.0 代表 +10%）
    limit_down_pct: float   # 跌停百分比（负数，如 -10.0 代表 -10%）
    lot_size: int           # 交易单位（手），A股通常为 100 股
    first_day_no_limit: bool  # 上市首日是否无涨跌停（简化规则）
    label: str              # 板块中文标签

    def to_dict(self) -> dict[str, Any]:
        return {
            "board_type": self.board_type,
            "limit_up_pct": self.limit_up_pct,
            "limit_down_pct": self.limit_down_pct,
            "lot_size": self.lot_size,
            "first_day_no_limit": self.first_day_no_limit,
            "label": self.label,
        }


# ──────────────────────────────────────────────────────────────────────────────
# 板块规则注册表
# ──────────────────────────────────────────────────────────────────────────────

_BOARD_RULES: dict[str, BoardRules] = {
    BoardType.MAIN: BoardRules(
        board_type=BoardType.MAIN,
        limit_up_pct=10.0,
        limit_down_pct=-10.0,
        lot_size=100,
        first_day_no_limit=False,
        label="主板",
    ),
    BoardType.CHINEXT: BoardRules(
        board_type=BoardType.CHINEXT,
        limit_up_pct=20.0,
        limit_down_pct=-20.0,
        lot_size=100,
        first_day_no_limit=False,
        label="创业板",
    ),
    BoardType.STAR: BoardRules(
        board_type=BoardType.STAR,
        limit_up_pct=20.0,
        limit_down_pct=-20.0,
        lot_size=200,
        first_day_no_limit=False,
        label="科创板",
    ),
    BoardType.BSE: BoardRules(
        board_type=BoardType.BSE,
        limit_up_pct=30.0,
        limit_down_pct=-30.0,
        lot_size=100,
        first_day_no_limit=False,
        label="北交所",
    ),
    BoardType.ST: BoardRules(
        board_type=BoardType.ST,
        limit_up_pct=5.0,
        limit_down_pct=-5.0,
        lot_size=100,
        first_day_no_limit=False,
        label="ST 股票",
    ),
    BoardType.UNKNOWN: BoardRules(
        board_type=BoardType.UNKNOWN,
        limit_up_pct=10.0,
        limit_down_pct=-10.0,
        lot_size=100,
        first_day_no_limit=False,
        label="未知板块（保守默认主板）",
    ),
}


# ──────────────────────────────────────────────────────────────────────────────
# 核心函数
# ──────────────────────────────────────────────────────────────────────────────

def classify_board(symbol: str, *, is_st: bool = False) -> BoardRules:
    """根据股票代码识别板块并返回对应规则。

    Parameters
    ----------
    symbol : str
        6 位 A 股股票代码，如 "600000"、"300001"。
    is_st : bool
        调用者是否已知该股票为 ST/*ST。

    Returns
    -------
    BoardRules
        涨跌停阈值、交易单位等规则。
    """
    market = classify_market(str(symbol or "").strip())
    if market == "STAR":
        board = _BOARD_RULES[BoardType.STAR]
    elif market == "CHINEXT":
        board = _BOARD_RULES[BoardType.CHINEXT]
    elif market == "BSE":
        board = _BOARD_RULES[BoardType.BSE]
    elif market in ("SH", "SZ"):
        board = _BOARD_RULES[BoardType.MAIN]
    else:
        board = _BOARD_RULES[BoardType.UNKNOWN]

    if is_st:
        # ST/*ST narrows the daily limit to ±5% only on boards whose base limit
        # is the main-board 10%. ChiNext / STAR / BSE risk-warning stocks keep
        # their wider board limit (±20% / ±20% / ±30%); only the ST label, not
        # the board's price-limit band, changes for them.
        if board.board_type in (BoardType.MAIN, BoardType.UNKNOWN):
            return _BOARD_RULES[BoardType.ST]
        return board
    return board


def is_limit_up(
    *,
    change_pct: float,
    symbol: str,
    is_st: bool = False,
    tolerance_bps: float = 20.0,
) -> bool:
    """判断是否触及涨停。

    Parameters
    ----------
    change_pct : float
        当日涨跌幅（如 0.10 表示 +10%）。
    symbol : str
        股票代码。
    is_st : bool
        是否 ST 股票。
    tolerance_bps : float
        涨停判定容差（基点），默认 20bps = 0.2%。

    Returns
    -------
    bool
    """
    import math
    rules = classify_board(symbol, is_st=is_st)
    threshold = rules.limit_up_pct / 100.0 - tolerance_bps / 10_000
    return bool(math.isfinite(change_pct) and change_pct >= threshold)


def is_limit_down(
    *,
    change_pct: float,
    symbol: str,
    is_st: bool = False,
    tolerance_bps: float = 20.0,
) -> bool:
    """判断是否触及跌停。"""
    import math
    rules = classify_board(symbol, is_st=is_st)
    threshold = rules.limit_down_pct / 100.0 + tolerance_bps / 10_000
    return bool(math.isfinite(change_pct) and change_pct <= threshold)


def get_limit_pcts(symbol: str, is_st: bool = False) -> tuple[float, float]:
    """返回 (limit_up_pct, limit_down_pct) 百分比值。

    Example
    -------
    >>> get_limit_pcts("300001")
    (20.0, -20.0)
    >>> get_limit_pcts("600000")
    (10.0, -10.0)
    """
    rules = classify_board(symbol, is_st=is_st)
    return rules.limit_up_pct, rules.limit_down_pct


def board_rules_to_assumptions(symbol: str, is_st: bool = False) -> dict[str, Any]:
    """返回可用于 backtest assumptions 的板块规则摘要。"""
    rules = classify_board(symbol, is_st=is_st)
    return {
        "board_type": rules.board_type,
        "board_label": rules.label,
        "limit_up_pct": rules.limit_up_pct,
        "limit_down_pct": rules.limit_down_pct,
        "lot_size": rules.lot_size,
        "disclaimer": "涨跌停阈值根据股票代码前缀自动推断，实际涨跌停以交易所当日公告为准。",
    }


__all__ = [
    "BoardRules",
    "BoardType",
    "board_rules_to_assumptions",
    "classify_board",
    "get_limit_pcts",
    "is_limit_down",
    "is_limit_up",
]
