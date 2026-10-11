"""Apply at most a few human-like paper trades under the mandate."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ai_trader.ledger import mark_to_market, position_map, session_days_held
from ai_trader.mandate import DEFAULT_MANDATE, TraderMandate
from quantdesk.paper.matching import next_open_limit_blocked
from quantdesk.paper.market_rules import lot_size_for_symbol, round_notional_down_to_lot_quantity
from quantdesk.paper.trade_costs import TradeCostConfig, buy_cash_outflow, sell_cash_inflow

COST_CONFIG = TradeCostConfig.cn_current()


def _limit_blocked(
    symbol: str, pct_chg: float | None, side: str, *, is_st: bool = False
) -> str | None:
    return next_open_limit_blocked(symbol, pct_chg, side, is_st=is_st)


ST_UNKNOWN_NOTE = "ST 未知，按 10% 判断"


def _listing_snapshot(symbol: str, daily_dir: Any) -> tuple[str, str]:
    """运行时清单里**目标证券自己那一行**的 ``(名称, 快照日期)``。

    名字与日期必须来自同一条证据；拿别的证券的日期来证明本证券的身份是不允许的，
    所以这里按 symbol 精确匹配行，找不到就返回空（缺证 → 未知）。
    """

    if daily_dir is None or not symbol:
        return "", ""
    root = Path(daily_dir).parent
    for candidate in (
        root / "metadata" / "security_master.csv",
        root / "universe" / "a_share_symbols.csv",
        root.parent / "metadata" / "security_master.csv",
    ):
        if not candidate.is_file():
            continue
        try:
            lines = candidate.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        if len(lines) < 2:
            continue
        header = [part.strip().lower() for part in lines[0].split(",")]
        if "symbol" not in header:
            continue
        symbol_idx = header.index("symbol")
        name_idx = next((header.index(key) for key in ("name", "名称", "证券名称") if key in header), None)
        as_of_idx = header.index("as_of") if "as_of" in header else None
        for line in lines[1:]:
            values = [part.strip() for part in line.split(",")]
            if len(values) <= symbol_idx or values[symbol_idx] != symbol:
                continue
            name = values[name_idx] if name_idx is not None and len(values) > name_idx else ""
            as_of = values[as_of_idx][:10] if as_of_idx is not None and len(values) > as_of_idx else ""
            return name, as_of
    return "", ""


def trusted_st_identity(
    symbol: str,
    *,
    row: dict[str, Any] | None = None,
    daily_dir: Any = None,
    as_of: str = "",
) -> tuple[bool, bool]:
    """``(is_st, st_unknown)`` —— 只用**当时**可验证的证据，绝不按历史或价格推断。

    顺序：① 该交易日的行情行 ``is_st`` 列；② 运行时清单名字，且清单自己的快照日期
    **必须等于**该交易日（今天的名单证明不了 9/5 的身份）；③ 其余一律未知。
    """

    raw = (row or {}).get("is_st") if isinstance(row, dict) else None
    if isinstance(raw, bool):
        return raw, False
    if raw is not None:
        text = str(raw).strip().lower()
        if text in {"1", "true", "yes", "y", "st"}:
            return True, False
        if text in {"0", "false", "no", "n"}:
            return False, False
    bar_day = str(as_of or (row or {}).get("date") or (row or {}).get("as_of") or "")[:10]
    name, snapshot_day = _listing_snapshot(symbol, daily_dir)
    cleaned = name.replace(" ", "").upper()
    if cleaned and snapshot_day and bar_day and snapshot_day == bar_day:
        return cleaned.startswith("ST") or cleaned.startswith("*ST"), False
    return False, True


def pd_finite(value: float) -> bool:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return False
    return number == number and abs(number) != float("inf")


def _can_sell(position: dict[str, Any], as_of: str) -> str | None:
    acquired = str(position.get("acquired_on") or "")[:10]
    if acquired and acquired >= as_of:
        return f"t1_blocked:{position.get('symbol')}:{acquired}"
    return None


def decide_and_trade(
    ledger: dict[str, Any],
    *,
    as_of: str,
    prices: dict[str, float],
    pct_chg: dict[str, float],
    ranked: list[tuple[str, float]],
    live_id: str | None,
    mandate: TraderMandate = DEFAULT_MANDATE,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    marked = mark_to_market(ledger, prices, as_of=as_of)
    actions: list[dict[str, Any]] = []
    if not live_id:
        actions.append(
            {
                "action": "hold_cash",
                "status": "skipped",
                "reason": "没有通过检验的方法，今天不新开仓。",
            }
        )
        return marked, actions

    equity = float(marked.get("equity") or 0.0)
    cash = float(marked.get("cash") or 0.0)
    positions = position_map(marked)
    target_notional = min(mandate.target_notional(), mandate.max_single_notional(equity))
    min_cash = mandate.min_cash_amount(equity)

    desired: list[str] = []
    for symbol, _score in ranked:
        if symbol in desired:
            continue
        price = prices.get(symbol)
        if not price or price <= 0:
            continue
        lot = lot_size_for_symbol(symbol)
        qty = round_notional_down_to_lot_quantity(target_notional, price, lot_size=lot)
        if qty <= 0:
            continue
        desired.append(symbol)
        if len(desired) >= mandate.target_holdings:
            break

    opens = 0
    exits = 0

    sell_candidates = [sym for sym in positions if desired and sym not in desired]
    for symbol in sell_candidates:
        if exits >= mandate.max_exits_per_day:
            break
        pos = positions[symbol]
        blocked = _can_sell(pos, as_of) or _limit_blocked(symbol, pct_chg.get(symbol), "sell")
        if blocked:
            actions.append({"action": "sell", "symbol": symbol, "status": "rejected", "reason": blocked})
            continue
        qty = int(pos.get("quantity") or 0)
        price = prices.get(symbol) or float(pos.get("market_price") or 0.0)
        if qty <= 0 or price <= 0:
            continue
        fill = sell_cash_inflow(
            price=price,
            quantity=qty,
            avg_cost=float(pos.get("avg_cost") or 0.0),
            config=COST_CONFIG,
        )
        cash += float(fill["cash_in"])
        realized = float(marked.get("realized_pnl") or 0.0) + float(fill["realized_pnl"])
        marked["realized_pnl"] = round(realized, 4)
        positions.pop(symbol, None)
        exits += 1
        trade = {
            "date": as_of,
            "symbol": symbol,
            "side": "sell",
            "quantity": qty,
            "price": round(price, 4),
            "cash_delta": round(float(fill["cash_in"]), 4),
            "fee": float(fill["total_fee"]),
            "realized_pnl": float(fill["realized_pnl"]),
            "hypothesis_id": live_id,
            "reason": "方法名单里不再需要这只，纸面卖出。",
            # Carry the style and holding period this position was opened under, so
            # per-style attribution can be computed from the trade record itself.
            "style": pos.get("style") or None,
            "held_days": session_days_held(str(pos.get("acquired_on") or "")[:10], as_of),
            "net_return": _net_return_on_cost(fill, pos, qty),
        }
        actions.append({**trade, "action": "sell", "status": "filled"})
        marked.setdefault("trades", []).append(trade)

    for symbol in desired:
        if opens >= mandate.max_opens_per_day:
            break
        if symbol in positions:
            continue
        if len(positions) >= mandate.max_holdings:
            actions.append(
                {
                    "action": "buy",
                    "symbol": symbol,
                    "status": "rejected",
                    "reason": f"已有{len(positions)}只，达到上限{mandate.max_holdings}。",
                }
            )
            break
        blocked = _limit_blocked(symbol, pct_chg.get(symbol), "buy")
        if blocked:
            actions.append({"action": "buy", "symbol": symbol, "status": "rejected", "reason": blocked})
            continue
        price = prices.get(symbol)
        if not price or price <= 0:
            continue
        lot = lot_size_for_symbol(symbol)
        qty = round_notional_down_to_lot_quantity(target_notional, price, lot_size=lot)
        if qty <= 0:
            actions.append(
                {
                    "action": "buy",
                    "symbol": symbol,
                    "status": "rejected",
                    "reason": "一手金额超过单票上限，放弃。",
                }
            )
            continue
        fill = buy_cash_outflow(price=price, quantity=qty, config=COST_CONFIG)
        cash_out = float(fill["cash_out"])
        if cash - cash_out < min_cash:
            actions.append(
                {
                    "action": "buy",
                    "symbol": symbol,
                    "status": "rejected",
                    "reason": "买完会把现金压到15%以下，放弃。",
                }
            )
            continue
        cash -= cash_out
        positions[symbol] = {
            "symbol": symbol,
            "quantity": qty,
            "avg_cost": round(float(fill["cost_basis"]) / qty, 6),
            "acquired_on": as_of,
            "hypothesis_id": live_id,
        }
        opens += 1
        trade = {
            "date": as_of,
            "symbol": symbol,
            "side": "buy",
            "quantity": qty,
            "price": round(price, 4),
            "cash_delta": round(-cash_out, 4),
            "fee": float(fill["total_fee"]),
            "hypothesis_id": live_id,
            "reason": "当前活着的方法选中这只，纸面买入。",
        }
        actions.append({**trade, "action": "buy", "status": "filled"})
        marked.setdefault("trades", []).append(trade)

    marked["cash"] = round(cash, 4)
    marked["positions"] = list(positions.values())
    marked = mark_to_market(marked, prices, as_of=as_of)
    if not any(item.get("status") == "filled" for item in actions):
        if not actions:
            actions.append({"action": "hold", "status": "skipped", "reason": "没有需要调整的仓位。"})
    return marked, actions


def _net_return_on_cost(fill: dict[str, Any], pos: dict[str, Any], qty: int) -> float | None:
    """Realised return on the capital that was actually tied up in this exit.

    Expressed on cost basis so it is comparable across position sizes, which is
    what per-style attribution needs.
    """

    try:
        avg_cost = float(pos.get("avg_cost") or 0.0)
        deployed = avg_cost * int(qty)
        if deployed <= 0:
            return None
        return round(float(fill["realized_pnl"]) / deployed, 6)
    except (TypeError, ValueError, KeyError, ZeroDivisionError):
        return None


def execute_intents(
    ledger: dict[str, Any],
    *,
    as_of: str,
    intents: list[dict[str, Any]],
    open_prices: dict[str, float],
    prev_closes: dict[str, float],
    mark_prices: dict[str, float] | None = None,
    live_id: str | None,
    mandate: TraderMandate = DEFAULT_MANDATE,
    st_identity: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Fill pending overnight intents at next-session open only."""

    def _identity(symbol: str) -> tuple[bool, bool]:
        """当晚这条代码的可信身份；没传就按"未知"处理，不改既有限制。"""

        entry = (st_identity or {}).get(symbol)
        if isinstance(entry, dict):
            return bool(entry.get("is_st")), bool(entry.get("st_unknown"))
        if isinstance(entry, (tuple, list)) and len(entry) == 2:
            return bool(entry[0]), bool(entry[1])
        return False, True

    marks = mark_prices or open_prices
    marked = mark_to_market(ledger, marks, as_of=as_of)
    actions: list[dict[str, Any]] = []
    cash = float(marked.get("cash") or 0.0)
    equity = float(marked.get("equity") or 0.0)
    positions = position_map(marked)
    min_cash = mandate.min_cash_amount(equity)
    opens = 0
    exits = 0
    if not intents:
        actions.append(
            {
                "action": "hold_cash",
                "status": "skipped",
                "reason": "昨夜无开盘指令，今天不交易。",
                "fill_model": "next_open_only",
            }
        )
        return marked, actions

    for intent in intents:
        side = str(intent.get("side") or "")
        symbol = str(intent.get("symbol") or "")
        qty = int(intent.get("quantity") or 0)
        open_px = open_prices.get(symbol)
        prev = prev_closes.get(symbol)
        gap_pct = None
        if open_px and prev and prev > 0:
            gap_pct = (open_px / prev - 1.0) * 100.0
        if side == "sell":
            if exits >= mandate.max_exits_per_day:
                continue
            pos = positions.get(symbol)
            if not pos:
                actions.append({"action": "sell", "symbol": symbol, "status": "rejected", "reason": "已无持仓。", "fill_model": "next_open_only"})
                continue
            blocked = _can_sell(pos, as_of)
            if blocked:
                actions.append({"action": "sell", "symbol": symbol, "status": "rejected", "reason": blocked, "fill_model": "next_open_only"})
                continue
            is_st, st_unknown = _identity(symbol)
            limit = _limit_blocked(symbol, gap_pct, "sell", is_st=is_st)
            if limit:
                actions.append({
                    "action": "sell", "symbol": symbol, "status": "rejected", "reason": limit,
                    "fill_model": "next_open_only",
                    "is_st": is_st, "st_unknown": st_unknown,
                    "st_note": ST_UNKNOWN_NOTE if st_unknown else "",
                })
                continue
            if not open_px or qty <= 0:
                actions.append({"action": "sell", "symbol": symbol, "status": "rejected", "reason": "missing_open", "fill_model": "next_open_only"})
                continue
            qty = min(qty, int(pos.get("quantity") or 0))
            fill = sell_cash_inflow(
                price=open_px,
                quantity=qty,
                avg_cost=float(pos.get("avg_cost") or 0.0),
                config=COST_CONFIG,
            )
            cash += float(fill["cash_in"])
            marked["realized_pnl"] = round(float(marked.get("realized_pnl") or 0.0) + float(fill["realized_pnl"]), 4)
            left = int(pos.get("quantity") or 0) - qty
            if left <= 0:
                positions.pop(symbol, None)
            else:
                pos["quantity"] = left
                positions[symbol] = pos
            exits += 1
            trade = {
                "date": as_of,
                "symbol": symbol,
                "side": "sell",
                "quantity": qty,
                "price": round(open_px, 4),
                "cash_delta": round(float(fill["cash_in"]), 4),
                "fee": float(fill["total_fee"]),
                "realized_pnl": float(fill["realized_pnl"]),
                "hypothesis_id": live_id or intent.get("hypothesis_id"),
                "reason": str(intent.get("reason") or "昨夜计划，今开成交。"),
                "fill_model": "next_open_only",
                "is_st": is_st,
                "st_unknown": st_unknown,
                "st_note": ST_UNKNOWN_NOTE if st_unknown else "",
                "style": pos.get("style") or intent.get("style") or None,
                "held_days": session_days_held(str(pos.get("acquired_on") or "")[:10], as_of),
                "net_return": _net_return_on_cost(fill, pos, qty),
            }
            actions.append({**trade, "action": "sell", "status": "filled"})
            marked.setdefault("trades", []).append(trade)
            continue
        if side != "buy":
            continue
        if opens >= mandate.max_opens_per_day:
            continue
        if symbol in positions:
            actions.append({"action": "buy", "symbol": symbol, "status": "rejected", "reason": "已持有。", "fill_model": "next_open_only"})
            continue
        if len(positions) >= mandate.max_holdings:
            actions.append({"action": "buy", "symbol": symbol, "status": "rejected", "reason": "持仓只数已满。", "fill_model": "next_open_only"})
            continue
        is_st, st_unknown = _identity(symbol)
        limit = _limit_blocked(symbol, gap_pct, "buy", is_st=is_st)
        if limit:
            actions.append({
                "action": "buy", "symbol": symbol, "status": "rejected", "reason": limit,
                "fill_model": "next_open_only",
                "is_st": is_st, "st_unknown": st_unknown,
                "st_note": ST_UNKNOWN_NOTE if st_unknown else "",
            })
            continue
        # An explicit None means the declared style does not treat an opening
        # gap-up as a reason to walk away (打板 chases it; 长期 does not care).
        # Only a missing key falls back to the account default.
        if "abandon_if_open_gap_pct" in intent and intent.get("abandon_if_open_gap_pct") is None:
            chase = None
        else:
            chase = float(intent.get("abandon_if_open_gap_pct") or mandate.max_chase_open_gap_pct)
        if chase is not None and gap_pct is not None and gap_pct > chase:
            actions.append(
                {
                    "action": "buy",
                    "symbol": symbol,
                    "status": "abandoned",
                    "reason": f"开盘相对昨收高开{gap_pct:.1f}%，超过追高阈值{chase:.1f}%，这笔放弃。",
                    "fill_model": "next_open_only",
                    "gap_pct": round(gap_pct, 4),
                }
            )
            continue
        if not open_px or qty <= 0:
            actions.append({"action": "buy", "symbol": symbol, "status": "rejected", "reason": "missing_open", "fill_model": "next_open_only"})
            continue
        fill = buy_cash_outflow(price=open_px, quantity=qty, config=COST_CONFIG)
        cash_out = float(fill["cash_out"])
        if cash - cash_out < min_cash:
            actions.append({"action": "buy", "symbol": symbol, "status": "rejected", "reason": "买完现金会低于15%。", "fill_model": "next_open_only"})
            continue
        cash -= cash_out
        positions[symbol] = {
            "symbol": symbol,
            "quantity": qty,
            "avg_cost": round(float(fill["cost_basis"]) / qty, 6),
            "acquired_on": as_of,
            "hypothesis_id": live_id or intent.get("hypothesis_id"),
            "thesis": intent.get("thesis") or intent.get("reason"),
            "planned_hold_days": int(intent.get("planned_hold_days") or mandate.fallback_hold_days),
            # The declared style has to live on the position, or tomorrow night the
            # trader cannot tell what it opened this as, and style attribution and
            # the style-aware exit rules both silently degrade.
            "style": intent.get("style") or None,
            "invalidation": intent.get("invalidation") or "",
        }
        opens += 1
        trade = {
            "date": as_of,
            "symbol": symbol,
            "side": "buy",
            "quantity": qty,
            "price": round(open_px, 4),
            "cash_delta": round(-cash_out, 4),
            "fee": float(fill["total_fee"]),
            "hypothesis_id": live_id or intent.get("hypothesis_id"),
            "reason": str(intent.get("reason") or "昨夜计划，今开成交。"),
            "fill_model": "next_open_only",
            "is_st": is_st,
            "st_unknown": st_unknown,
            "st_note": ST_UNKNOWN_NOTE if st_unknown else "",
        }
        actions.append({**trade, "action": "buy", "status": "filled"})
        marked.setdefault("trades", []).append(trade)

    marked["cash"] = round(cash, 4)
    marked["positions"] = list(positions.values())
    marked = mark_to_market(marked, marks, as_of=as_of)
    return marked, actions
