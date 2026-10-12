"""Paper-trader living rules — risk rails only, never a trading style.

The operator supplies no stock pool, no named strategy, and **no style**. Whether
this trader ends up chasing limit-ups, swinging over days, or buying something and
sitting on it for a year is its own call to make and to revise. The point of the
exercise is to watch what it chooses and why.

So the constraints here are strictly about account integrity — lot sizes, cash
floor, concentration, how much can change in one day. They are deliberately
style-agnostic: nothing in this file should make one holding period cheaper or
more legal than another.

Style itself lives in ``state/ai_trader/philosophy.json``, which the trader writes
for itself (see ``philosophy.py``).
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, replace
from typing import Any

BOUNDARY = {
    "research_only": True,
    "paper_only": True,
    "paper_account_only": True,
    "no_broker": True,
    "no_real_orders": True,
    "is_real_order": False,
    "live_execution_locked": True,
    "not_investment_advice": True,
    "not_position_advice": True,
    "qmt_order_route_locked": True,
}

# Style vocabulary the trader may choose from, and the horizon each implies.
# These are labels for *its* declarations, not rules imposed on it.
STYLES = {
    "打板": "追涨停/涨停板战法，持有 1-3 天，最激进",
    "短线": "几天内的快进快出",
    "波段": "一到几周，等一段行情走完",
    "中线": "一到几个月，跟一个逻辑",
    "长期": "半年以上，看公司而不是看盘",
}

# How much of an opening gap-up each style tolerates before abandoning the buy.
# This has to follow the declared style rather than be a constant: chasing a gap
# is the whole point of 打板, while a 长期 buyer holding for a year does not care
# that the open was 6% higher. A single global threshold would quietly forbid both
# ends of the style range. ``None`` means the gap is not a reason to abandon.
STYLE_MAX_OPEN_GAP_PCT: dict[str, float | None] = {
    "打板": None,
    "短线": 5.0,
    "波段": 5.0,
    "中线": 9.0,
    "长期": None,
}


def max_open_gap_for_style(style: str | None, default: float) -> float | None:
    """Gap tolerance for a declared style, falling back to the account default."""

    label = str(style or "").strip()
    if label in STYLE_MAX_OPEN_GAP_PCT:
        return STYLE_MAX_OPEN_GAP_PCT[label]
    return default


@dataclass(frozen=True)
class TraderMandate:
    ledger_id: str = "ai_trader_100k"
    initial_cash: float = 100_000.0
    currency: str = "CNY"
    # Risk rails. min_holdings is 0 on purpose: concentrating, or sitting in cash
    # because nothing looks good, are both legitimate decisions a real investor
    # makes. Forcing a minimum position count would be forcing a style.
    min_holdings: int = 0
    target_holdings: int = 6
    max_holdings: int = 8
    min_single_weight: float = 0.10
    max_single_weight: float = 0.20
    min_cash_weight: float = 0.15
    max_gross_weight: float = 0.85
    max_opens_per_day: int = 2
    max_exits_per_day: int = 2
    paper_credit_enabled: bool = False
    llm_enabled: bool = False
    network_enabled: bool = True
    persona: str = "self_directed_retail_investor"
    persona_zh: str = (
        "一个自己拿主意的 A 股散户：自己决定做打板、短线、波段、中线还是长期持有，"
        "也可以对不同的票用不同的做法，并且可以随着复盘改主意。"
        "钱是真账本记的，想法是自己的。"
    )
    # Fallback horizon used ONLY when no horizon was declared for a position.
    # It is not a target and not a deadline.
    fallback_hold_days: int = 5
    # Fallback gap tolerance for positions with no declared style; per-style values
    # in STYLE_MAX_OPEN_GAP_PCT take precedence.
    max_chase_open_gap_pct: float = 5.0
    max_watchlist: int = 30
    # Charter §3: whether the book may touch ST / BSE names is a ledger-header
    # parameter, not code. Defaults keep today's behaviour (both allowed).
    allow_st: bool = True
    allow_bse: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {**asdict(self), "boundary": dict(BOUNDARY), "styles": dict(STYLES)}

    def target_notional(self) -> float:
        return round(self.initial_cash / self.target_holdings, 2)

    def max_single_notional(self, equity: float) -> float:
        return round(max(equity, 0.0) * self.max_single_weight, 2)

    def min_cash_amount(self, equity: float) -> float:
        return round(max(equity, 0.0) * self.min_cash_weight, 2)


DEFAULT_MANDATE = TraderMandate()

# Header fields the owner may override. Identity/money fields (ledger_id,
# initial_cash, currency) and persona text are fixed at creation and ignored.
# min_holdings is not a rail (0 on purpose, see the class comment); the real
# headers still carry an old 5, which must not come back to life.
_OVERRIDABLE_INT = ("target_holdings", "max_holdings", "max_opens_per_day",
                    "max_exits_per_day", "fallback_hold_days", "max_watchlist")
_OVERRIDABLE_WEIGHT = ("min_single_weight", "max_single_weight", "min_cash_weight", "max_gross_weight")
_OVERRIDABLE_FLOAT = ("max_chase_open_gap_pct",)
_OVERRIDABLE_BOOL = ("allow_st", "allow_bse")


def _as_bool(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "on"}:
        return True
    if text in {"0", "false", "no", "off"}:
        return False
    return None


def mandate_from_ledger(ledger: Any, *, base: TraderMandate = DEFAULT_MANDATE) -> TraderMandate:
    """The rails this book actually runs under: header ``mandate`` over the defaults.

    The header used to be written at creation and never read, so changing it did
    nothing. Unknown or invalid values are ignored (the default stays), so a
    header that only carries today's defaults changes no behaviour.
    """

    header = ledger.get("mandate") if isinstance(ledger, dict) else None
    if not isinstance(header, dict):
        return base
    overrides: dict[str, Any] = {}
    for key in _OVERRIDABLE_INT:
        if key in header:
            try:
                value = int(header[key])
            except (TypeError, ValueError):
                continue
            if value >= 0:
                overrides[key] = value
    for key in _OVERRIDABLE_WEIGHT + _OVERRIDABLE_FLOAT:
        if key in header:
            try:
                value = float(header[key])
            except (TypeError, ValueError):
                continue
            if value != value or value < 0 or (key in _OVERRIDABLE_WEIGHT and value > 1):
                continue
            overrides[key] = value
    for key in _OVERRIDABLE_BOOL:
        if key in header:
            flag = _as_bool(header[key])
            if flag is not None:
                overrides[key] = flag
    if overrides.get("target_holdings", base.target_holdings) <= 0:
        overrides.pop("target_holdings", None)
    return replace(base, **overrides) if overrides else base
