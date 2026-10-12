"""A 股板块规则引擎 (Board-Specific Rule Engine)。

根据股票代码前缀自动识别板块（主板/创业板/科创板/ST/北交所），
返回对应的涨跌停阈值、交易规则，替代固定 ±9.8% 阈值。

仅用于本地研究回测模拟，不构成投资建议，不连接任何真实交易系统。
"""

from __future__ import annotations

import unicodedata
from dataclasses import asdict, dataclass
from datetime import date as _date
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any

from quantdesk.data_quality import classify_market

# 日期敏感的涨跌幅规则（成交真实性，宪章 §2 不变量 2）：
# - 创业板注册制改革 2020-08-24 起 20%，之前与主板同为 10%（ST 5%）；
# - 沪深主板风险警示股（ST/*ST）2026-07-06 起由 5% 调为 10%（HKEX 通函 055/2026）；
# - 科创板 20%、北交所 30%，ST 不改变这两个板块的幅度。
CHINEXT_20PCT_START = "2020-08-24"
MAIN_ST_10PCT_START = "2026-07-06"
# 比较容差只吸收浮点误差，远小于一分钱；涨跌停价本身按前收四舍五入到分。
LIMIT_PRICE_EPSILON = 0.0005

ST_STATUS_ST = "st"
ST_STATUS_NOT_ST = "not_st"
ST_STATUS_UNKNOWN = "unknown"

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

# 2026-07-06 起沪深主板风险警示股涨跌幅为 10%：板块身份仍是 ST（手数、标签不变），
# 只是幅度与普通主板相同。只有调用方给出交易日期时才会用到它。
_ST_MAIN_10PCT_RULES = BoardRules(
    board_type=BoardType.ST,
    limit_up_pct=10.0,
    limit_down_pct=-10.0,
    lot_size=100,
    first_day_no_limit=False,
    label="ST 股票（2026-07-06 起主板 10%）",
)

# 2020-08-24 之前创业板与主板同为 10%（ST 5%）。
_CHINEXT_PRE_2020_RULES = BoardRules(
    board_type=BoardType.CHINEXT,
    limit_up_pct=10.0,
    limit_down_pct=-10.0,
    lot_size=100,
    first_day_no_limit=False,
    label="创业板（2020-08-24 前 10%）",
)


def _date_text(value: Any) -> str:
    text = str(value or "").strip()
    digits = "".join(ch for ch in text[:10] if ch.isdigit())
    if len(digits) == 8 and digits[:2] in ("19", "20"):
        return f"{digits[:4]}-{digits[4:6]}-{digits[6:]}"
    return ""


# ──────────────────────────────────────────────────────────────────────────────
# 核心函数
# ──────────────────────────────────────────────────────────────────────────────

def classify_board(symbol: str, *, is_st: bool = False, trade_date: Any = None) -> BoardRules:
    """根据股票代码识别板块并返回对应规则。

    Parameters
    ----------
    symbol : str
        6 位 A 股股票代码，如 "600000"、"300001"。
    is_st : bool
        调用者是否已知该股票为 ST/*ST。
    trade_date : str | date | None
        交易日期。给出时按当日规则返回幅度（创业板 2020-08-24 前 10%、主板 ST
        2026-07-06 起 10%）；不给时保持历史默认（创业板 20%、主板 ST 5%），
        以免改变未传日期的旧回测口径。

    Returns
    -------
    BoardRules
        涨跌停阈值、交易单位等规则。
    """
    day = _date_text(trade_date) if trade_date is not None else ""
    market = classify_market(str(symbol or "").strip())
    if market == "STAR":
        board = _BOARD_RULES[BoardType.STAR]
    elif market == "CHINEXT":
        board = _BOARD_RULES[BoardType.CHINEXT]
        if day and day < CHINEXT_20PCT_START:
            board = _CHINEXT_PRE_2020_RULES
    elif market == "BSE":
        board = _BOARD_RULES[BoardType.BSE]
    elif market in ("SH", "SZ"):
        board = _BOARD_RULES[BoardType.MAIN]
    else:
        board = _BOARD_RULES[BoardType.UNKNOWN]

    if is_st:
        # ST/*ST narrows the daily limit to ±5% only on boards whose base limit
        # is the main-board 10% (and ChiNext before its 2020-08-24 reform).
        # STAR / BSE / post-reform ChiNext risk-warning stocks keep their wider
        # board limit; only the ST label changes for them. From 2026-07-06 the
        # main-board risk-warning band is 10% as well (dated callers only).
        if board.board_type in (BoardType.MAIN, BoardType.UNKNOWN) or board is _CHINEXT_PRE_2020_RULES:
            if day and day >= MAIN_ST_10PCT_START:
                return _ST_MAIN_10PCT_RULES
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


# ──────────────────────────────────────────────────────────────────────────────
# 共用涨跌停价（成交门用）：按板块 + 日期给幅度，按前收四舍五入到分
# ──────────────────────────────────────────────────────────────────────────────

def _normalized_name(name: Any) -> str:
    return unicodedata.normalize("NFKC", str(name or "")).replace(" ", "").strip().upper()


def st_status_from_name(name: Any) -> str:
    """名称前缀给出的 ST 身份：``st`` / ``not_st``；没有名称时 ``unknown``。

    只看名称本身（行情快照的 name 列带 ST/*ST 前缀），不按价格或历史推断。
    除权除息前缀（XD/XR/DR）先剥掉；"STAR" 这类英文词不算 ST。
    """

    text = _normalized_name(name)
    if not text:
        return ST_STATUS_UNKNOWN
    for marker in ("XD", "XR", "DR"):
        if text.startswith(marker) and len(text) > 2 and not text[2:3].isascii():
            text = text[2:]
            break
    for prefix in ("*ST", "S*ST", "SST", "ST"):
        if text.startswith(prefix):
            tail = text[len(prefix):len(prefix) + 1]
            if not tail or not (tail.isascii() and tail.isalnum()):
                return ST_STATUS_ST
    return ST_STATUS_NOT_ST


def listing_marker_from_name(name: Any) -> str:
    """新股上市期标记：``N``（上市首日）、``C``（注册制上市后前 5 日），否则空。"""

    text = _normalized_name(name)
    if len(text) >= 2 and text[0] in ("N", "C"):
        tail = text[1:2]
        if not (tail.isascii() and tail.isalnum()):
            return text[0]
    return ""


def limit_ratio(
    symbol: Any,
    trade_date: Any,
    *,
    st_status: str = ST_STATUS_UNKNOWN,
    listing_first_day: bool = False,
) -> tuple[float | None, str]:
    """``(幅度, 依据)``。幅度 ``None`` 表示当日不设涨跌幅限制（新股上市首日）。

    主板 10%；创业板 2020-08-24 起 20%（之前 10%）；科创板 20%；北交所 30%；
    主板 ST 2026-07-06 前 5%、之后 10%。2026-07-06 前身份未知时按更严的 5%。
    """

    if listing_first_day:
        return None, "listing_period_no_limit"
    code = str(symbol or "").strip()
    market = classify_market(code)
    day = _date_text(trade_date) or _date.today().isoformat()
    if market == "BSE":
        return 0.30, "bse_30pct"
    if market == "STAR":
        return 0.20, "star_20pct"
    reform_chinext = market == "CHINEXT" and day >= CHINEXT_20PCT_START
    if reform_chinext:
        return 0.20, "chinext_20pct_from_2020_08_24"
    base_label = "chinext_pre_2020_08_24" if market == "CHINEXT" else (
        "main_board" if market in ("SH", "SZ") else "unknown_board_assumed_main"
    )
    if day >= MAIN_ST_10PCT_START:
        if st_status == ST_STATUS_ST:
            return 0.10, f"{base_label}_st_10pct_from_2026_07_06"
        return 0.10, f"{base_label}_10pct"
    if st_status == ST_STATUS_ST:
        return 0.05, f"{base_label}_st_5pct"
    if st_status == ST_STATUS_NOT_ST:
        return 0.10, f"{base_label}_10pct"
    return 0.05, f"{base_label}_st_unknown_strict_5pct"


def _finite_positive(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(str(value).strip())
    except (TypeError, ValueError):
        return None
    if number != number or number in (float("inf"), float("-inf")) or number <= 0:
        return None
    return number


def _finite(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(str(value).strip())
    except (TypeError, ValueError):
        return None
    if number != number or number in (float("inf"), float("-inf")):
        return None
    return number


def round_price_half_up(value: Any, places: int = 2) -> float:
    """A 股价格四舍五入到分（ROUND_HALF_UP，按十进制计算，不受二进制浮点误差影响）。"""

    try:
        number = Decimal(repr(float(value)))
    except (InvalidOperation, TypeError, ValueError):
        raise ValueError(f"not a price: {value!r}") from None
    quantum = Decimal(1).scaleb(-int(places))
    return float(number.quantize(quantum, rounding=ROUND_HALF_UP))


def _limit_from_base(base: float, ratio: float, *, up: bool) -> float:
    base_dec = Decimal(repr(round_price_half_up(base)))
    ratio_dec = Decimal(repr(float(ratio)))
    factor = Decimal(1) + ratio_dec if up else Decimal(1) - ratio_dec
    value = (base_dec * factor).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return float(value)


@dataclass(frozen=True)
class LimitPrices:
    """某只股票某一交易日的涨跌停价及其依据。``status`` 为 ``ok`` / ``no_limit`` / ``unverifiable``。"""

    symbol: str
    trade_date: str
    board_type: str
    ratio: float | None
    ratio_basis: str
    st_status: str
    pre_close: float | None
    pre_close_basis: str
    up_limit: float | None
    down_limit: float | None
    status: str
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def compute_limit_prices(
    symbol: Any,
    *,
    trade_date: Any,
    pre_close: Any = None,
    price: Any = None,
    pct_chg: Any = None,
    st_status: str | None = None,
    is_st: bool | None = None,
    name: Any = None,
    listing_first_day: bool | None = None,
) -> LimitPrices:
    """涨跌停价 = 前收 × (1 ± 幅度)，四舍五入到分。

    前收优先用调用方给的（交易所口径的昨收，除权日就是除权后的前收）；
    缺失时才用 ``price / (1 + pct_chg/100)`` 重算；两者都没有则 ``unverifiable``，
    调用方必须拒绝成交（缺数据不成交）。

    ST 身份顺序：显式 ``st_status`` → 显式 ``is_st`` → 名称前缀 → 未知。
    名称以 ``N`` 开头（上市首日）时当日不设限。
    """

    code = str(symbol or "").strip()
    day = _date_text(trade_date) or _date.today().isoformat()
    status_value = str(st_status or "").strip().lower()
    if status_value not in (ST_STATUS_ST, ST_STATUS_NOT_ST, ST_STATUS_UNKNOWN):
        status_value = ""
    if not status_value and is_st is not None:
        status_value = ST_STATUS_ST if bool(is_st) else ST_STATUS_NOT_ST
    if not status_value and name is not None and str(name).strip():
        status_value = st_status_from_name(name)
    if not status_value:
        status_value = ST_STATUS_UNKNOWN
    # N = 上市首日；C = 注册制新股上市后前 5 日（同样不设涨跌幅限制）。
    first_day = bool(listing_first_day) if listing_first_day is not None else (
        listing_marker_from_name(name) in ("N", "C")
    )
    rules = classify_board(code, is_st=status_value == ST_STATUS_ST, trade_date=day)
    ratio, ratio_basis = limit_ratio(code, day, st_status=status_value, listing_first_day=first_day)

    base = _finite_positive(pre_close)
    base_basis = "given_pre_close" if base is not None else ""
    if base is None:
        px = _finite_positive(price)
        pct = _finite(pct_chg)
        if px is not None and pct is not None and pct > -100.0:
            base = px / (1.0 + pct / 100.0)
            base_basis = "reconstructed_from_price_and_pct_chg"
    if ratio is None:
        return LimitPrices(
            symbol=code, trade_date=day, board_type=rules.board_type, ratio=None,
            ratio_basis=ratio_basis, st_status=status_value,
            pre_close=round_price_half_up(base) if base is not None else None,
            pre_close_basis=base_basis or "missing", up_limit=None, down_limit=None,
            status="no_limit", reason=ratio_basis,
        )
    if base is None:
        return LimitPrices(
            symbol=code, trade_date=day, board_type=rules.board_type, ratio=ratio,
            ratio_basis=ratio_basis, st_status=status_value, pre_close=None,
            pre_close_basis="missing", up_limit=None, down_limit=None,
            status="unverifiable", reason="missing_pre_close_and_pct_chg",
        )
    return LimitPrices(
        symbol=code, trade_date=day, board_type=rules.board_type, ratio=ratio,
        ratio_basis=ratio_basis, st_status=status_value,
        pre_close=round_price_half_up(base), pre_close_basis=base_basis,
        up_limit=_limit_from_base(base, ratio, up=True),
        down_limit=_limit_from_base(base, ratio, up=False),
        status="ok", reason="",
    )


LIMIT_OK = ""
LIMIT_UP_BLOCK = "limit_up"
LIMIT_DOWN_BLOCK = "limit_down"
LIMIT_UNVERIFIABLE = "limit_unverifiable"
LIMIT_MISSING_PRICE = "missing_fill_price"
LIMIT_OUTSIDE_BAND = "price_outside_limit_band"


def limit_fill_check(
    symbol: Any,
    side: str,
    *,
    price: Any,
    trade_date: Any,
    pre_close: Any = None,
    pct_chg: Any = None,
    st_status: str | None = None,
    is_st: bool | None = None,
    name: Any = None,
    listing_first_day: bool | None = None,
) -> dict[str, Any]:
    """成交价能不能成交：涨停不买、跌停不卖、算不出涨跌停价就不成交。

    返回 ``{"ok", "code", "reason", "limits"}``。``code`` 为空表示可以成交；
    否则是 ``limit_up`` / ``limit_down`` / ``limit_unverifiable`` /
    ``missing_fill_price`` / ``price_outside_limit_band`` 之一。
    """

    side_text = str(side or "").strip().lower()
    limits = compute_limit_prices(
        symbol, trade_date=trade_date, pre_close=pre_close, price=price, pct_chg=pct_chg,
        st_status=st_status, is_st=is_st, name=name, listing_first_day=listing_first_day,
    )
    result: dict[str, Any] = {"ok": False, "code": "", "reason": "", "limits": limits.to_dict()}
    px = _finite_positive(price)
    if px is None:
        result.update(code=LIMIT_MISSING_PRICE, reason=f"{LIMIT_MISSING_PRICE}:{limits.symbol}")
        return result
    if limits.status == "no_limit":
        result.update(ok=True, reason=limits.reason)
        return result
    if limits.status != "ok" or limits.up_limit is None or limits.down_limit is None:
        result.update(code=LIMIT_UNVERIFIABLE, reason=f"{LIMIT_UNVERIFIABLE}:{limits.symbol}:{limits.reason}")
        return result
    up, down = float(limits.up_limit), float(limits.down_limit)
    detail = f"{limits.symbol}:{px:.2f}:up={up:.2f}:down={down:.2f}:{limits.ratio_basis}"
    if side_text == "buy" and px >= up - LIMIT_PRICE_EPSILON:
        result.update(code=LIMIT_UP_BLOCK, reason=f"{LIMIT_UP_BLOCK}:{detail}")
        return result
    if side_text == "sell" and px <= down + LIMIT_PRICE_EPSILON:
        result.update(code=LIMIT_DOWN_BLOCK, reason=f"{LIMIT_DOWN_BLOCK}:{detail}")
        return result
    # 幅度本身不确定（未知板块、或 07-06 前 ST 身份未知按严口径）时，价格落在严口径
    # 带外不能证明数据有错，只做涨停/跌停判断，不做带外判断。
    band_certain = not limits.ratio_basis.startswith("unknown_board") and "st_unknown" not in limits.ratio_basis
    if band_certain and (px > up + LIMIT_PRICE_EPSILON or px < down - LIMIT_PRICE_EPSILON):
        result.update(code=LIMIT_OUTSIDE_BAND, reason=f"{LIMIT_OUTSIDE_BAND}:{detail}")
        return result
    result.update(ok=True)
    return result


__all__ = [
    "BoardRules",
    "BoardType",
    "CHINEXT_20PCT_START",
    "LIMIT_DOWN_BLOCK",
    "LIMIT_MISSING_PRICE",
    "LIMIT_OUTSIDE_BAND",
    "LIMIT_PRICE_EPSILON",
    "LIMIT_UNVERIFIABLE",
    "LIMIT_UP_BLOCK",
    "LimitPrices",
    "MAIN_ST_10PCT_START",
    "ST_STATUS_NOT_ST",
    "ST_STATUS_ST",
    "ST_STATUS_UNKNOWN",
    "board_rules_to_assumptions",
    "classify_board",
    "compute_limit_prices",
    "get_limit_pcts",
    "is_limit_down",
    "is_limit_up",
    "limit_fill_check",
    "limit_ratio",
    "listing_marker_from_name",
    "round_price_half_up",
    "st_status_from_name",
]
