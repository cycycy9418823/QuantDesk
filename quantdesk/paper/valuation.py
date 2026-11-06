"""Paper marks, equity summaries and evidence for daily PnL.

Execution owns quote acquisition, state/journal persistence and publication.
Legacy adapters supply dependencies at call time; optional calendar/history
reads remain lazy. Marks retain their original input mutation and policy order.
An injected adjustment policy may load its factor table and populate its cache.
"""
from __future__ import annotations

import math
from collections import Counter
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Callable, TYPE_CHECKING

from quantdesk.utils import safe_float, safe_int

if TYPE_CHECKING:
    from quantdesk.paper.adjustment_policy import AdjustmentFactorTable

Payload = dict[str, Any]


def holding_days(
    position: Payload, decision_date: str, *,
    datetime_type: type[datetime],
) -> int:
    entry = str(position.get("entry_date") or "")[:10]
    if not entry:
        return safe_int(position.get("holding_days"), 0) or 0
    try:
        return max(0, (datetime_type.fromisoformat(decision_date) - datetime_type.fromisoformat(entry)).days)
    except ValueError:
        return safe_int(position.get("holding_days"), 0) or 0


def mark_position(
    position: Payload, latest_price: float, decision_date: str, hold_reason: str, *,
    latest_quote: Callable[..., Payload],
    holding_days_for: Callable[[Payload, str], int],
    suspension_policy: Callable[[Payload, Payload], Payload],
    adjustment_policy: Callable[..., Payload],
    adjustment_factors: AdjustmentFactorTable | None = None, quote: Payload | None = None,
) -> Payload:
    original_price = safe_float(position.get("latest_price"), None)
    original_date = str(position.get("latest_date") or "")[:10]
    quote = quote if isinstance(quote, dict) else latest_quote(position.get("symbol"), decision_date=decision_date)
    quote_date = str(quote.get("date") or "")[:10]
    raw_price = safe_float(quote.get("price"), None)
    # A refusal to sell is independent of whether a valid raw mark is available.
    # A candidate price, missing basis or stale quote cannot establish today's value.
    raw_valid = (quote.get("price_basis") == "raw" and not quote.get("price_basis_reason")
                 and not quote.get("missing") and raw_price is not None and raw_price > 0
                 and quote.get("bar_valid") is not False
                 and not quote.get("suspended") and not quote.get("is_suspended"))
    if raw_valid:
        latest_price = raw_price
    elif original_price is not None and original_price > 0:
        latest_price = original_price
    quantity = safe_int(position.get("quantity"), 0) or 0
    # PnL follows the weighted cost after adds; entry_price remains the exit-rule reference.
    unit_cost = safe_float(position.get("avg_cost") or position.get("entry_price"), latest_price) or latest_price
    position["latest_price"] = round(latest_price, 4)
    position["market_value"] = round(latest_price * quantity, 2)
    position["unrealized_pnl"] = round((latest_price - unit_cost) * quantity, 2)
    return_pct = round((latest_price / unit_cost - 1.0) * 100.0, 2) if unit_cost > 0 else 0.0
    position["unrealized_return_pct"] = return_pct
    position["return_pct"] = return_pct
    position["holding_days"] = holding_days_for(position, decision_date)
    position["hold_reason"] = hold_reason
    position["sector"] = position.get("sector") or "未知板块"
    position["primary_theme"] = position.get("primary_theme") or "未知题材"
    position["latest_date"] = quote_date if raw_valid else original_date
    position["valuation_date"] = decision_date
    position["valuation_price_date"] = position["latest_date"]
    position["valuation_price_basis"] = "raw" if raw_valid else position.get("valuation_price_basis", "unknown")
    position["valuation_price_source"] = quote.get("source") if raw_valid else "carried_legacy_mark"
    position["valuation_status"] = "normal" if raw_valid and quote_date == decision_date else "stale" if raw_valid else "missing_or_untrusted"
    quote_warnings = [str(item) for item in (quote.get("warnings") or [])]
    if quote_warnings:
        seen = list(position.get("valuation_warnings") or [])
        position["valuation_warnings"] = seen + [w for w in quote_warnings if w not in seen]
    suspension_quote = quote
    if quote.get("price_basis") != "raw" or quote.get("price_basis_reason"):
        suspension_quote = {**quote, "price": latest_price, "close": latest_price}
    position = suspension_policy(position, suspension_quote)
    if position.get("valuation_status") == "suspended_frozen":
        position["latest_date"] = str(quote.get("last_tradable_date") or original_date)
        position["valuation_price_date"] = position["latest_date"]
    adjusted_price = safe_float(position.get("latest_price"), latest_price) or latest_price
    position = adjustment_policy(
        position,
        latest_price=float(adjusted_price),
        latest_date=str(position.get("latest_date") or decision_date),
        factors=adjustment_factors,
    )
    if str(position.get("entry_date") or "")[:10] == str(position.get("latest_date") or "")[:10]:
        position["zero_pnl_explanation"] = "今日新建仓，最新价等于模拟买入价，因此当前浮动盈亏为 0。需等下一交易日更新行情后才能复盘收益。"
    elif abs(safe_float(position.get("unrealized_pnl"), 0.0) or 0.0) < 1e-9:
        position["zero_pnl_explanation"] = "持仓估值仍使用买入日价格，可能是日线未更新或最新价未刷新。"
    else:
        position["zero_pnl_explanation"] = ""
    return position


def paper_account_scale(
    initial_cash: float | None,
) -> tuple[str, str]:
    """Label the ledger scale for UI/早报 (Round 21 本金配置化).

    The live 10k ledger stays untouched; it is labeled ``legacy_10k`` so its
    returns are never compared against 100k-backtest numbers.
    """

    value = safe_float(initial_cash, 0.0) or 0.0
    if abs(value - 10_000.0) <= 1e-9:
        return "legacy_10k", "教学规模（1 万本金），不与 10 万回测比收益。"
    return "configured", ""


def summary(
    state: Payload, actions: list[Payload], *,
    account_scale_for: Callable[[float | None], tuple[str, str]],
    inspect_adjustment: Callable[..., Payload],
    pnl_identity_for: Callable[[Payload], Payload],
    correction_fields_for: Callable[[Payload], Payload],
) -> Payload:
    positions = state.get("positions") if isinstance(state.get("positions"), list) else []
    closed = state.get("closed_positions") if isinstance(state.get("closed_positions"), list) else []
    cash = safe_float(state.get("cash"), 0.0) or 0.0
    initial_cash = safe_float(state.get("initial_cash"), cash) or cash
    account_scale, account_scale_note = account_scale_for(initial_cash)
    market_value = sum(safe_float(row.get("market_value"), 0.0) or 0.0 for row in positions)
    closed_realized_pnl = sum(safe_float(row.get("realized_pnl"), 0.0) or 0.0 for row in closed)
    reductions = state.get("realized_reductions") if isinstance(state.get("realized_reductions"), list) else []
    partial_realized_pnl = sum(safe_float(row.get("realized_pnl"), 0.0) or 0.0 for row in reductions if isinstance(row, dict))
    legacy_realized_adjustment = safe_float(state.get("legacy_realized_adjustment"), 0.0) or 0.0
    correction = inspect_adjustment(state, legacy_base=legacy_realized_adjustment)
    effective_adjustment = safe_float(correction["effective_realized_adjustment"], legacy_realized_adjustment)
    realized_pnl = closed_realized_pnl + partial_realized_pnl + effective_adjustment
    unrealized_pnl = sum(safe_float(row.get("unrealized_pnl"), 0.0) or 0.0 for row in positions)
    total_equity = cash + market_value
    # Compare the declared accounting fields, not the display fallbacks above.
    # Missing evidence and differences must survive summary generation.
    pnl_identity = pnl_identity_for({**state, "total_equity": total_equity})
    counts = Counter(str(action.get("action") or "") for action in actions)
    return {
        "initial_cash": round(initial_cash, 2),
        "account_scale": account_scale,
        "account_scale_note": account_scale_note,
        "cash": round(cash, 2),
        "market_value": round(market_value, 2),
        "total_equity": round(total_equity, 2),
        "total_return_pct": round((total_equity / initial_cash - 1.0) * 100.0, 2) if initial_cash else 0.0,
        "cumulative_pnl": round(total_equity - initial_cash, 2),
        "accounting_status": "consistent" if pnl_identity["status"] == "consistent" else "unverified",
        "pnl_identity": pnl_identity,
        "daily_pnl": None,
        "daily_return_pct": None,
        "daily_pnl_status": "missing_previous_equity",
        "realized_pnl": round(realized_pnl, 2),
        "closed_realized_pnl": round(closed_realized_pnl, 2),
        "partial_realized_pnl": round(partial_realized_pnl, 2),
        "legacy_realized_adjustment": round(legacy_realized_adjustment, 2),
        **correction_fields_for(state),
        "unrealized_pnl": round(unrealized_pnl, 2),
        "position_count": len(positions),
        "today_new_count": counts.get("buy", 0),
        "today_add_count": counts.get("add", 0) + counts.get("breakout_add", 0),
        "today_reduce_count": counts.get("reduce_half", 0) + counts.get("take_profit", 0),
        "today_sell_count": counts.get("sell", 0) + counts.get("sell_all", 0) + counts.get("stop_loss", 0),
        "today_no_fill_count": counts.get("no_fill", 0),
        "today_hold_count": counts.get("hold", 0),
        "today_block_count": counts.get("block", 0),
        "today_watch_exit_count": counts.get("watch_exit", 0),
    }


def with_daily_pnl(
    summary: Payload, *,
    project_root: Callable[[], Path],
    load_calendar: Callable[[Path], Payload],
    read_history: Callable[[Path, Any], Any],
    history_path: Path,
    valuation_date: str, previous_state: Payload | None = None, history: Payload | None = None, calendar: Payload | None = None, valuation_valid: bool = True,
) -> Payload:
    """Separate cumulative PnL from an evidenced previous-session equity change.

    Same-day reruns always use the same previous session, never the equity
    already marked earlier today. Missing calendar/equity evidence stays null.
    This is reporting only; it never updates cash, fills, or valuation prices.
    """
    result = dict(summary)
    result.update(daily_pnl=None, daily_return_pct=None,
                  previous_total_equity=None, previous_equity_date="",
                  daily_pnl_status="missing_previous_equity", daily_pnl_source="")
    if not valuation_valid:
        result["daily_pnl_status"] = "current_valuation_unverified"
        return result
    try:
        target = date.fromisoformat(valuation_date)
    except (TypeError, ValueError):
        result["daily_pnl_status"] = "valuation_date_invalid"
        return result
    cal = calendar if calendar is not None else load_calendar(
        project_root() / "data/calendar/a_share_trading_calendar.csv"
    )
    sessions = cal.get("is_open_by_date") or {}
    if sessions.get(target.isoformat()) is not True:
        result["daily_pnl_status"] = "previous_session_unverified"
        return result
    previous = target - timedelta(days=1)
    for _ in range(370):
        if previous.isoformat() not in sessions:
            result["daily_pnl_status"] = "previous_session_unverified"
            return result
        if sessions[previous.isoformat()] is True:
            break
        previous -= timedelta(days=1)
    else:
        result["daily_pnl_status"] = "previous_session_unverified"
        return result
    previous_date = previous.isoformat()
    result["previous_equity_date"] = previous_date

    def number(value: Any) -> float | None:
        if isinstance(value, bool):
            return None
        value = safe_float(value, None)
        return value if value is not None and math.isfinite(value) else None

    prior = previous_state or {}
    reference = prior.get("daily_pnl_reference") or {}
    equity = None
    if (isinstance(reference, dict) and reference.get("valuation_date") == valuation_date
            and reference.get("previous_equity_date") == previous_date):
        equity = number(reference.get("previous_total_equity"))
        result["daily_pnl_source"] = "persisted_previous_session_reference"
    if equity is None:
        loaded = history if history is not None else read_history(history_path, {})
        items = loaded.get("items", []) if isinstance(loaded, dict) else []
        matches = [row for row in items if isinstance(row, dict)
                   and str(row.get("valuation_date") or row.get("generated_at") or "")[:10] == previous_date]
        matches.sort(key=lambda row: str(row.get("generated_at") or ""))
        if matches:
            # The latest record for that session is authoritative; do not skip
            # a damaged latest record to silently revive an older amount.
            row = matches[-1]
            recorded, cash, market = (number(row.get(key)) for key in ("total_equity", "cash", "market_value"))
            if (row.get("portfolio_review_valid") is not False and recorded is not None
                    and cash is not None and market is not None and abs(recorded - cash - market) <= 0.011):
                equity = recorded
                result["daily_pnl_source"] = "previous_session_history"
    if (equity is None and prior.get("valuation_date") == previous_date
            and prior.get("portfolio_review_valid") is not False):
        cash, market = number(prior.get("cash")), number(prior.get("market_value"))
        if cash is not None and market is not None:
            equity = cash + market
            result["daily_pnl_source"] = "previous_session_state"
    current_equity = number(result.get("total_equity"))
    if equity is None or current_equity is None:
        result["daily_pnl_source"] = ""
        return result
    result.update(previous_total_equity=round(equity, 2),
                  daily_pnl=round(current_equity - equity, 2), daily_pnl_status="verified",
                  daily_return_pct=round((current_equity / equity - 1.0) * 100.0, 4) if equity > 0 else None)
    return result


def daily_pnl_reference(
    summary: Payload, valuation_date: str,
) -> Payload:
    if summary.get("daily_pnl_status") != "verified":
        return {}
    return {"valuation_date": valuation_date, "previous_equity_date": summary["previous_equity_date"],
            "previous_total_equity": summary["previous_total_equity"]}


def total_equity_from_state(
    state: Payload,
) -> float:
    cash = safe_float(state.get("cash"), safe_float(state.get("initial_cash"), 0.0)) or 0.0
    market_value = sum(safe_float(row.get("market_value"), 0.0) or 0.0 for row in state.get("positions") or [] if isinstance(row, dict))
    if market_value <= 0:
        market_value = safe_float(state.get("market_value"), 0.0) or 0.0
    total = safe_float(state.get("total_equity"), None)
    return float(total) if total is not None and total > 0 else cash + market_value
