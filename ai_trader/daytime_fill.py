"""S3 daytime fills: a slot's intent is filled off the *next* slot's snapshot.

Paper only, and deliberately narrow:

* the quote must be strictly later than the intent (timestamp and slot), so one
  snapshot can never fill its own intent;
* limit-up blocks a buy, limit-down blocks a sell, a suspended or missing quote
  fills nothing — the same policy ``quantdesk.paper.matching`` already declares;
* T+1 comes from the ledger (``acquired_on == today`` cannot be sold);
* fees come from ``quantdesk.paper.trade_costs`` — there is no second cost model;
* the household mandate (``ai_trader.mandate``) still hard-blocks, and every
  block is written into ``daytime_journal.jsonl`` as
  ``{slot, symbol, side, qty, blocked_by, ai_reason}`` so the evening review can
  count them;
* ``AI_TRADER_DAYTIME_DRY=1`` writes ``daytime_dry_fills.jsonl`` and leaves the
  ledger untouched.

Every fill and every journal row carries ``model_used`` and ``elapsed_ms``
(carried over from the intent) so the health line can report them later.
"""

from __future__ import annotations

import json
import os
from copy import deepcopy
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from ai_trader.ledger import load_ledger, mark_to_market, position_map, save_ledger, session_days_held
from ai_trader.mandate import DEFAULT_MANDATE, TraderMandate
from ai_trader.paths import PROJECT_ROOT, CyclePaths, assert_not_quantdesk_ledger, profile_paths
from quantdesk.paper.matching import next_open_limit_blocked
from quantdesk.paper.market_rules import lot_size_for_symbol
from quantdesk.paper.trade_costs import TradeCostConfig, buy_cash_outflow, sell_cash_inflow
from quantdesk.market_clock import to_shanghai_datetime

FILL_SCHEMA = "ai_trader_daytime_fill_v1"
FILLS_NAME = "daytime_fills.jsonl"
DRY_FILLS_NAME = "daytime_dry_fills.jsonl"
JOURNAL_NAME = "daytime_journal.jsonl"
DRY_ENV = "AI_TRADER_DAYTIME_DRY"
COST_CONFIG = TradeCostConfig.cn_current()
# Said out loud whenever a row carries neither an is_st flag nor a name, so the
# 10% limit assumption is visible in the record instead of silent.
ST_UNKNOWN_NOTE = "ST 未知，按 10% 判断"

# Why an intent did not become a fill.
BLOCK_SAME_SLOT = "same_slot_no_self_fill"
BLOCK_NOT_LATER = "quote_not_after_intent"
BLOCK_MISSING_QUOTE = "missing_quote_no_fill"
BLOCK_LIMIT_UP = "limit_up_no_buy"
BLOCK_LIMIT_DOWN = "limit_down_no_sell"
BLOCK_NOT_A_LOT = "not_a_full_lot"
BLOCK_T_PLUS_1 = "t_plus_1_not_sellable"
BLOCK_NO_POSITION = "no_position_to_sell"
BLOCK_TOO_MANY_SHARES = "more_than_held"
BLOCK_INVALID_INTENT = "invalid_intent"

# Household mandate tokens (kept separate: these are rails, not market rules).
MANDATE_OPENS_PER_DAY = "mandate_max_opens_per_day"
MANDATE_EXITS_PER_DAY = "mandate_max_exits_per_day"
MANDATE_MAX_HOLDINGS = "mandate_max_holdings"
MANDATE_SINGLE_WEIGHT = "mandate_max_single_weight"
MANDATE_CASH_FLOOR = "mandate_min_cash_weight"

SIDE_BUY = "buy"
SIDE_SELL = "sell"


def dry_mode() -> bool:
    return os.environ.get(DRY_ENV, "").strip() == "1"


def fills_path(runtime: CyclePaths, *, dry: bool) -> Path:
    name = DRY_FILLS_NAME if dry else FILLS_NAME
    return assert_not_quantdesk_ledger(runtime.daytime_notes.parent / name)


def journal_path(runtime: CyclePaths) -> Path:
    return assert_not_quantdesk_ledger(runtime.daytime_notes.parent / JOURNAL_NAME)


def _append_jsonl(path: Path, records: Sequence[dict[str, Any]]) -> None:
    if not records:
        return
    target = assert_not_quantdesk_ledger(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    needs_newline = False
    if target.is_file() and target.stat().st_size:
        with target.open("rb") as existing:
            existing.seek(-1, os.SEEK_END)
            needs_newline = existing.read(1) != b"\n"
    with target.open("a", encoding="utf-8", newline="\n") as handle:
        if needs_newline:
            handle.write("\n")  # Preserve a torn tail; never glue a retry to it.
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
        handle.flush()
        os.fsync(handle.fileno())


def _read_jsonl(path: Path, *, strict: bool = False) -> list[dict[str, Any]]:
    target = assert_not_quantdesk_ledger(path)
    if not target.is_file():
        return []
    rows: list[dict[str, Any]] = []
    try:
        with target.open(encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    payload = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(payload, dict):
                    rows.append(payload)
    except OSError:
        if strict:
            raise
        return []
    return rows


def _num(value: Any) -> float | None:
    try:
        number = float(str(value).strip())
    except (TypeError, ValueError):
        return None
    return number if number == number and abs(number) != float("inf") else None


def _stamp(value: Any) -> datetime | None:
    raw = str(value or "").strip()
    if len(raw) <= 10:
        return None
    try:
        return to_shanghai_datetime(datetime.fromisoformat(raw.replace("Z", "+00:00")))
    except ValueError:
        return None


def intent_key(intent: Mapping[str, Any]) -> str:
    """Stable identity of one intent, used to keep fills idempotent."""

    return "|".join(
        str(intent.get(field) or "")
        for field in ("as_of", "slot", "symbol", "side", "created_at", "quantity")
    )


def _intent_time(intent: Mapping[str, Any]) -> datetime | None:
    created = _stamp(intent.get("created_at"))
    completed = _stamp(intent.get("model_completed_at"))
    if created is None or (intent.get("model_completed_at") and completed is None):
        return None
    return max(created, completed) if completed else created


def _sellable_quantities(ledger: Mapping[str, Any], as_of: str) -> dict[str, int]:
    bought: dict[str, int] = {}
    for trade in ledger.get("trades") or []:
        if (isinstance(trade, Mapping) and str(trade.get("date") or "")[:10] == as_of
                and trade.get("side") == SIDE_BUY):
            symbol = str(trade.get("symbol") or "")
            bought[symbol] = bought.get(symbol, 0) + int(trade.get("quantity") or 0)
    result = {}
    for symbol, row in position_map(dict(ledger)).items():
        acquired = str(row.get("acquired_on") or "")[:10]
        result[symbol] = (max(0, int(row.get("quantity") or 0) - bought.get(symbol, 0))
                          if acquired and acquired < as_of else 0)
    return result


def fill_keys(runtime: CyclePaths, as_of: str) -> set[str]:
    """Intents that already produced a fill (real, dry, or already on the ledger) today.

    The ledger is included so a crash between "ledger written" and "fill record written"
    does not let a retry apply the same intent a second time (T28).
    """

    keys: set[str] = set()
    for path in (fills_path(runtime, dry=False), fills_path(runtime, dry=True)):
        for row in _read_jsonl(path):
            if str(row.get("as_of") or "")[:10] == as_of and row.get("intent_key"):
                keys.add(str(row["intent_key"]))
    try:
        book = load_ledger(runtime.ledger)
    except Exception:
        book = {}
    for row in (book.get("trades") if isinstance(book, dict) else []) or []:
        if not isinstance(row, Mapping):
            continue
        if str(row.get("date") or "")[:10] != as_of:
            continue
        key = str(row.get("daytime_intent_key") or "")
        if key:
            keys.add(key)
    return keys


def pending_intents(runtime: CyclePaths, as_of: str, *, slot: str = "") -> list[dict[str, Any]]:
    """Pending intents for this date, minus the ones that outlived their window.

    ``valid_until_slot`` is what the model declared in the order; once the live
    slot is past it the intent is dead and must not be re-tried every round.
    """

    rows = _read_jsonl(runtime.daytime_intents)
    out: list[dict[str, Any]] = []
    for row in rows:
        if str(row.get("as_of") or "") != as_of:
            continue
        if str(row.get("status") or "") != "pending":
            continue
        if not row.get("symbol") or not row.get("side"):
            continue
        valid_until = str(row.get("valid_until_slot") or "").strip()
        if slot and valid_until and slot > valid_until:
            continue
        out.append(row)
    return out


def expired_intents(runtime: CyclePaths, as_of: str, *, slot: str) -> list[dict[str, Any]]:
    """Pending intents whose declared window closed before this slot."""

    if not slot:
        return []
    return [row for row in _read_jsonl(runtime.daytime_intents)
            if str(row.get("as_of") or "") == as_of
            and str(row.get("status") or "") == "pending"
            and str(row.get("valid_until_slot") or "").strip()
            and slot > str(row["valid_until_slot"]).strip()]


def _st_flags(row: Mapping[str, Any]) -> tuple[bool, bool]:
    """(is_st, st_unknown) for one snapshot row.

    The 5-minute snapshot CSV has no ``is_st`` column (see
    ``quantdesk.snapshot_history.SNAPSHOT_COLUMNS``), only ``name``. Treating every
    ST name as an ordinary stock made the limit check use 10% instead of 5%, so the
    name is the fallback and "neither column" is reported instead of assumed.
    """

    raw = row.get("is_st")
    if raw is not None and str(raw).strip() != "":
        return str(raw).strip().lower() in {"1", "1.0", "true", "yes"}, False
    name = str(row.get("name") or "").strip().upper().replace(" ", "")
    if name:
        return ("ST" in name), False
    return False, True


def quote_index(rows: Sequence[Mapping[str, Any]], *, quote_slot: str = "",
                quote_time: str = "", source: str = "") -> dict[str, dict[str, Any]]:
    """Keep each row's market clock separate from receipt and slot clocks."""

    out: dict[str, dict[str, Any]] = {}
    for row in rows or []:
        if not isinstance(row, Mapping):
            continue
        symbol = str(row.get("symbol") or "").strip()
        if len(symbol) != 6 or not symbol.isdigit():
            continue
        is_st, st_unknown = _st_flags(row)
        market_time = str(row.get("quote_time") or row.get("timestamp") or row.get("time") or "")
        market_date = str(row.get("quote_date") or "").strip()
        parsed_time = market_time
        if market_time and len(market_time) <= 10 and market_date:
            parsed_time = f"{market_date}T{market_time}"
        stamp = _stamp(parsed_time)
        time_error = ""
        if stamp is None:
            time_error = "missing_or_invalid_quote_time"
        elif market_date and stamp.date().isoformat() != market_date:
            time_error = "quote_date_conflict"
        received_time = str(row.get("fetched_at") or row.get("received_at") or "")
        out[symbol] = {
            "symbol": symbol,
            "price": _num(row.get("price")),
            "pct_chg": _num(row.get("pct_chg")),
            "is_st": is_st,
            "st_unknown": st_unknown,
            "st_note": ST_UNKNOWN_NOTE if st_unknown else "",
            "slot": str(row.get("slot") or quote_slot or ""),
            # 三个时刻分开留：报价时刻（行情自己的时间）> 收到时刻（我们抓到的时刻）> 槽时间。
            "quote_time": market_time,
            "quote_date": market_date,
            "received_at": received_time,
            "time": parsed_time,
            "time_error": time_error,
            "slot_time": quote_time,
            "time_source": "quote" if market_time else "unknown",
            "source": str(row.get("source") or source or ""),
            "trading": str(row.get("trading") or "").strip().lower()
                       not in {"0", "false", "no", "suspended"},
        }
    return out


def _later_than_intent(intent: Mapping[str, Any], quote: Mapping[str, Any]) -> str:
    """Empty string when the quote is genuinely after the intent, else the token."""

    intent_slot = str(intent.get("slot") or "")
    quote_slot = str(quote.get("slot") or "")
    if quote_slot and intent_slot and quote_slot <= intent_slot:
        return BLOCK_SAME_SLOT
    quote_stamp = _stamp(quote.get("time"))
    intent_stamp = _intent_time(intent)
    if quote.get("time_error"):
        return BLOCK_NOT_LATER
    if quote_stamp is not None and intent_stamp is not None:
        return "" if quote_stamp > intent_stamp else BLOCK_NOT_LATER
    return BLOCK_NOT_LATER


def _mandate_block(
    *,
    side: str,
    quantity: int,
    price: float,
    position: Mapping[str, Any] | None,
    cash: float,
    equity: float,
    opens_today: int,
    exits_today: int,
    holdings: int,
    mandate: TraderMandate,
) -> tuple[str, str]:
    """The household rails, checked before any market rule writes a fill."""

    if side == SIDE_BUY:
        if opens_today >= mandate.max_opens_per_day:
            return (MANDATE_OPENS_PER_DAY,
                    f"今天已开 {opens_today} 只，上限 {mandate.max_opens_per_day}。")
        if position is None and holdings >= mandate.max_holdings:
            return MANDATE_MAX_HOLDINGS, f"已持 {holdings} 只，上限 {mandate.max_holdings}。"
        notional = price * quantity
        ceiling = mandate.max_single_notional(equity)
        if equity > 0 and notional > ceiling:
            return MANDATE_SINGLE_WEIGHT, f"单票金额 {notional:.0f} 超过上限 {ceiling:.0f}。"
        outflow = float(buy_cash_outflow(price=price, quantity=quantity,
                                         config=COST_CONFIG)["cash_out"])
        floor = mandate.min_cash_amount(equity)
        if cash - outflow < floor:
            return (MANDATE_CASH_FLOOR,
                    f"买完现金 {cash - outflow:.0f} 低于下限 {floor:.0f}。")
        return "", ""
    if exits_today >= mandate.max_exits_per_day:
        return (MANDATE_EXITS_PER_DAY,
                f"今天已卖 {exits_today} 只，上限 {mandate.max_exits_per_day}。")
    return "", ""


def plan_fills(
    intents: Sequence[Mapping[str, Any]],
    quotes: Mapping[str, Mapping[str, Any]],
    ledger: Mapping[str, Any],
    *,
    as_of: str,
    mandate: TraderMandate = DEFAULT_MANDATE,
    already_filled: set[str] | None = None,
) -> dict[str, Any]:
    """Decide which intents would fill, without writing anything.

    Returns ``fills``, ``blocks`` (market rules) and ``mandate_blocks`` (rails).
    """

    done = set(already_filled or ())
    book = dict(ledger) if isinstance(ledger, Mapping) else {}
    positions = position_map(book)
    sellable = _sellable_quantities(book, as_of)
    cash = float(book.get("cash") or 0.0)
    equity = float(book.get("equity") or 0.0)
    trades = book.get("trades") if isinstance(book.get("trades"), list) else []
    same_day = [row for row in trades if isinstance(row, Mapping)
                and str(row.get("date") or "")[:10] == as_of]
    opens_today = sum(1 for row in same_day if str(row.get("side")) == SIDE_BUY)
    exits_today = sum(1 for row in same_day if str(row.get("side")) == SIDE_SELL)

    fills: list[dict[str, Any]] = []
    blocks: list[dict[str, Any]] = []
    mandate_blocks: list[dict[str, Any]] = []

    def _record(intent: Mapping[str, Any], quote: Mapping[str, Any] | None, token: str,
                detail: str) -> dict[str, Any]:
        unknown = bool((quote or {}).get("st_unknown"))
        return {
            "schema_version": FILL_SCHEMA, "as_of": as_of,
            "slot": str((quote or {}).get("slot") or ""),
            "symbol": str(intent.get("symbol") or ""), "side": str(intent.get("side") or ""),
            "qty": int(_num(intent.get("quantity")) or 0), "blocked_by": token,
            "detail": f"{detail} {ST_UNKNOWN_NOTE}" if unknown else detail,
            "ai_reason": str(intent.get("reason") or "")[:200], "intent_key": intent_key(intent),
            "model_used": bool(intent.get("model_used")),
            "elapsed_ms": int(_num(intent.get("elapsed_ms")) or 0),
            "st_unknown": unknown, "st_note": ST_UNKNOWN_NOTE if unknown else "",
        }

    earliest = to_shanghai_datetime(datetime.min)
    ordered = sorted(intents, key=lambda item: (
        _stamp((quotes.get(str(item.get("symbol") or "")) or {}).get("time")) or earliest,
        _intent_time(item) or earliest))
    for intent in ordered:
        key = intent_key(intent)
        if key in done:
            continue
        done.add(key)
        symbol = str(intent.get("symbol") or "")
        side = str(intent.get("side") or "")
        quantity = int(_num(intent.get("quantity")) or 0)
        quote = quotes.get(symbol)
        if side not in (SIDE_BUY, SIDE_SELL) or quantity <= 0:
            blocks.append(_record(intent, quote, BLOCK_INVALID_INTENT, "side/quantity 不合法，不成交。"))
            continue
        if quote is None:
            blocks.append(_record(intent, None, BLOCK_MISSING_QUOTE, f"{symbol} 本槽没有报价，不成交。"))
            continue
        if not quote.get("trading", True):
            blocks.append(_record(intent, quote, BLOCK_MISSING_QUOTE,
                                  f"{symbol} 停牌/无有效交易状态，不成交。"))
            continue
        ordering = _later_than_intent(intent, quote)
        if ordering:
            blocks.append(_record(intent, quote, ordering,
                                  "报价时间/槽不晚于意图，禁止用同一张快照自成交。"))
            continue
        price = _num(quote.get("price"))
        if price is None or price <= 0:
            blocks.append(_record(intent, quote, BLOCK_MISSING_QUOTE,
                                  f"{symbol} 报价缺失或非正，不成交。"))
            continue
        lot = int(lot_size_for_symbol(symbol) or 100)
        if lot <= 0 or quantity % lot != 0:
            blocks.append(_record(intent, quote, BLOCK_NOT_A_LOT,
                                  f"{quantity} 不是 {lot} 股整数倍，不成交。"))
            continue
        limit = next_open_limit_blocked(symbol, quote.get("pct_chg"), side,
                                        is_st=bool(quote.get("is_st")))
        if limit:
            token = BLOCK_LIMIT_UP if side == SIDE_BUY else BLOCK_LIMIT_DOWN
            blocks.append(_record(intent, quote, token, f"{symbol} {limit}，不成交。"))
            continue

        position = positions.get(symbol)
        mandate_token, mandate_detail = _mandate_block(
            side=side, quantity=quantity, price=price, position=position, cash=cash,
            equity=equity, opens_today=opens_today, exits_today=exits_today,
            holdings=len(positions), mandate=mandate)
        if mandate_token:
            mandate_blocks.append(_record(intent, quote, mandate_token, mandate_detail))
            continue

        if side == SIDE_SELL:
            if position is None or int(position.get("quantity") or 0) <= 0:
                blocks.append(_record(intent, quote, BLOCK_NO_POSITION, f"没有 {symbol} 的持仓，不成交。"))
                continue
            held = int(position.get("quantity") or 0)
            if quantity > held:
                blocks.append(_record(intent, quote, BLOCK_TOO_MANY_SHARES,
                                      f"卖出 {quantity} 超过持仓 {held}，不成交。"))
                continue
            acquired = str(position.get("acquired_on") or "")[:10]
            if quantity > sellable.get(symbol, 0):
                blocks.append(_record(intent, quote, BLOCK_T_PLUS_1,
                                      f"{symbol} 隔夜可卖 {sellable.get(symbol, 0)} 股，T+1 不可超卖。"))
                continue

        fills.append({
            "schema_version": FILL_SCHEMA, "as_of": as_of, "slot": str(quote.get("slot") or ""),
            "intent_slot": str(intent.get("slot") or ""), "intent_key": key,
            "symbol": symbol, "side": side, "quantity": quantity,
            "price": round(price, 4), "price_source": str(quote.get("source") or ""),
            "price_timestamp": str(quote.get("time") or ""), "pct_chg": quote.get("pct_chg"),
            "quote_date": quote.get("quote_date"), "quote_time": quote.get("quote_time"),
            "received_at": quote.get("received_at"), "time_source": quote.get("time_source"),
            "intent_created_at": intent.get("created_at"),
            "model_completed_at": intent.get("model_completed_at"),
            "fill_policy_intraday": "snapshot",
            "is_st": bool(quote.get("is_st")), "st_unknown": bool(quote.get("st_unknown")),
            "st_note": str(quote.get("st_note") or ""),
            "ai_reason": str(intent.get("reason") or "")[:200], "stop": intent.get("stop"),
            "overnight_plan_thesis": intent.get("overnight_plan_thesis"),
            "model_used": bool(intent.get("model_used")), "model": str(intent.get("model") or ""),
            "elapsed_ms": int(_num(intent.get("elapsed_ms")) or 0),
            "prompt_chars": int(_num(intent.get("prompt_chars")) or 0),
            "created_at": datetime.now().astimezone().replace(microsecond=0).isoformat(),
        })
        # 这一笔成交既计入当日额度，也**立刻改变本地现金与持仓**：批内后续动作必须看到
        # 前一个动作的账变动，否则同批两笔卖会累计超卖、两笔买会超用现金（T27）。
        if side == SIDE_BUY:
            opens_today += 1
            outflow = float(buy_cash_outflow(price=price, quantity=quantity,
                                             config=COST_CONFIG)["cash_out"])
            cash -= outflow
            if symbol in positions:
                positions[symbol] = {
                    **positions[symbol],
                    "quantity": int(positions[symbol].get("quantity") or 0) + quantity,
                }
            else:
                positions[symbol] = {"symbol": symbol, "quantity": quantity}
        else:
            exits_today += 1
            sellable[symbol] -= quantity
            cash += float(sell_cash_inflow(
                price=price, quantity=quantity,
                avg_cost=float((position or {}).get("avg_cost") or 0.0),
                config=COST_CONFIG)["cash_in"])
            left = int((position or {}).get("quantity") or 0) - quantity
            if left <= 0:
                positions.pop(symbol, None)
            elif position is not None:
                position["quantity"] = left
    return {"fills": fills, "blocks": blocks, "mandate_blocks": mandate_blocks}


def apply_to_ledger(
    ledger: dict[str, Any], fills: Sequence[Mapping[str, Any]], *, as_of: str,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Apply planned fills to a copy of the ledger, using the shared cost model."""

    updated = dict(ledger)
    positions = position_map(ledger)
    sellable = _sellable_quantities(ledger, as_of)
    cash = float(ledger.get("cash") or 0.0)
    realized = float(ledger.get("realized_pnl") or 0.0)
    trades = [dict(row) for row in (ledger.get("trades") or []) if isinstance(row, Mapping)]
    applied: list[dict[str, Any]] = []
    marks: dict[str, float] = {}

    for fill in fills:
        symbol = str(fill["symbol"])
        quantity = int(fill["quantity"])
        price = float(fill["price"])
        intent_key_value = str(fill.get("intent_key") or "")
        if intent_key_value and any(
            str(row.get("daytime_intent_key") or "") == intent_key_value
            and str(row.get("date") or "")[:10] == as_of
            for row in trades
        ):
            # 这一笔已经在账上（例如上次账本写成、成交记录没写成）：不重复记账（T28）。
            continue
        if fill["side"] == SIDE_BUY:
            money = buy_cash_outflow(price=price, quantity=quantity, config=COST_CONFIG)
            cash_out = float(money["cash_out"])
            if cash - cash_out < 0:
                continue
            cash -= cash_out
            existing = positions.get(symbol)
            if existing:
                held = int(existing.get("quantity") or 0)
                merged_qty = held + quantity
                merged_cost = (float(existing.get("avg_cost") or 0.0) * held
                               + float(money["cost_basis"])) / merged_qty
                positions[symbol] = {**existing, "quantity": merged_qty,
                                     "avg_cost": round(merged_cost, 6)}
            else:
                positions[symbol] = {
                    "symbol": symbol, "quantity": quantity,
                    "avg_cost": round(float(money["cost_basis"]) / quantity, 6),
                    "acquired_on": as_of, "style": None, "opened_by": "daytime_snapshot",
                }
            trade = {"date": as_of, "symbol": symbol, "side": SIDE_BUY, "quantity": quantity,
                     "price": round(price, 4), "cash_delta": round(-cash_out, 4),
                     "fee": float(money["total_fee"]), "reason": fill.get("ai_reason") or "",
                     "source": "daytime_snapshot", "slot": fill.get("slot"),
                     "daytime_intent_key": intent_key_value,
                     "model_used": fill.get("model_used"), "elapsed_ms": fill.get("elapsed_ms")}
        else:
            position = positions.get(symbol)
            if position is None or quantity > sellable.get(symbol, 0):
                continue
            sellable[symbol] -= quantity
            money = sell_cash_inflow(price=price, quantity=quantity,
                                     avg_cost=float(position.get("avg_cost") or 0.0),
                                     config=COST_CONFIG)
            cash += float(money["cash_in"])
            realized += float(money["realized_pnl"])
            held = int(position.get("quantity") or 0) - quantity
            if held > 0:
                positions[symbol] = {**position, "quantity": held}
            else:
                positions.pop(symbol, None)
            trade = {"date": as_of, "symbol": symbol, "side": SIDE_SELL, "quantity": quantity,
                     "price": round(price, 4), "cash_delta": round(float(money["cash_in"]), 4),
                     "fee": float(money["total_fee"]),
                     "realized_pnl": float(money["realized_pnl"]),
                     "reason": fill.get("ai_reason") or "", "source": "daytime_snapshot",
                     "slot": fill.get("slot"), "daytime_intent_key": intent_key_value,
                     "model_used": fill.get("model_used"),
                     "elapsed_ms": fill.get("elapsed_ms"),
                     "held_days": session_days_held(
                         str(position.get("acquired_on") or "")[:10], as_of)}
        evidence = {**deepcopy(dict(fill)), "cash_delta": trade["cash_delta"], "fee": trade["fee"],
                    "dry_run": False, "ledger_written": True}
        trade["daytime_fill_record"] = evidence
        trades.append(trade)
        applied.append(deepcopy(evidence))
        marks[symbol] = price

    updated["cash"] = round(cash, 4)
    updated["positions"] = list(positions.values())
    updated["realized_pnl"] = round(realized, 4)
    updated["trades"] = trades
    return mark_to_market(updated, marks, as_of=as_of), applied


def _restore_fill_evidence(runtime: CyclePaths, book: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Publish missing original receipts without changing money or timestamps."""
    path = fills_path(runtime, dry=False)
    published = {str(row.get("intent_key") or "") for row in _read_jsonl(path, strict=True)}
    missing = []
    for trade in book.get("trades") or []:
        if not isinstance(trade, Mapping):
            continue
        key = str(trade.get("daytime_intent_key") or "")
        if not key or key in published:
            continue
        original = trade.get("daytime_fill_record")
        if not isinstance(original, Mapping) or original.get("intent_key") != key:
            raise ValueError(f"original_fill_evidence_missing:{key}")
        missing.append(deepcopy(dict(original)))
        published.add(key)
    if missing:
        _append_jsonl(path, missing)
    return missing


def fill_slot(
    project_root: Path | None = None,
    *,
    profile: str | None = None,
    now: datetime | None = None,
    paths: CyclePaths | None = None,
    rows: Sequence[Mapping[str, Any]] | None = None,
    quote_slot: str = "",
    quote_time: str = "",
    rows_source: str = "",
    ledger: dict[str, Any] | None = None,
    mandate: TraderMandate = DEFAULT_MANDATE,
    write: bool = False,
    dry: bool | None = None,
    save: Callable[[dict[str, Any], Path], Path] | None = None,
) -> dict[str, Any]:
    """Fill earlier slots' pending intents off this slot's snapshot.

    ``write`` defaults to False: a caller has to say out loud that it wants files
    or the ledger touched. In dry mode the ledger is never saved.
    """

    root = Path(project_root or PROJECT_ROOT).resolve()
    _ = root  # kept for signature parity with the screen/decide modules
    runtime = paths or profile_paths(profile)
    current = to_shanghai_datetime(now)
    as_of = current.strftime("%Y-%m-%d")
    is_dry = dry_mode() if dry is None else bool(dry)
    book = ledger if ledger is not None else load_ledger(runtime.ledger)
    restored: list[dict[str, Any]] = []
    if write and not is_dry:
        try:
            restored = _restore_fill_evidence(runtime, book)
        except (OSError, ValueError) as exc:
            return {"schema_version": FILL_SCHEMA, "as_of": as_of, "fills": 0,
                    "written": False, "ledger_written": False, "records": [],
                    "evidence_error": f"{type(exc).__name__}:{exc}", "recovery_pending": True}
    quotes = quote_index(rows or [], quote_slot=quote_slot, quote_time=quote_time,
                         source=rows_source)
    intents = pending_intents(runtime, as_of, slot=quote_slot)
    expired = expired_intents(runtime, as_of, slot=quote_slot)
    plan = plan_fills(intents, quotes, book, as_of=as_of, mandate=mandate,
                      already_filled=fill_keys(runtime, as_of))
    updated, applied = apply_to_ledger(book, plan["fills"], as_of=as_of)
    records = [{**dict(record), "dry_run": is_dry,
                "ledger_written": bool(write and not is_dry)} for record in applied]
    journal_rows: list[dict[str, Any]] = []
    for status, source in (("blocked", plan["blocks"]),
                           ("mandate_blocked", plan["mandate_blocks"])):
        journal_rows.extend({
            "schema_version": "ai_trader_daytime_journal_v1", "as_of": as_of,
            "slot": row.get("slot"), "status": status, "blocked_by": row.get("blocked_by"),
            "symbol": row.get("symbol"), "side": row.get("side"), "qty": row.get("qty"),
            "ai_reason": row.get("ai_reason"), "detail": row.get("detail"),
            "model_used": row.get("model_used"), "elapsed_ms": row.get("elapsed_ms"),
        } for row in source)

    result = {
        "schema_version": FILL_SCHEMA, "as_of": as_of, "profile": str(profile or ""),
        "slot": str(quote_slot or ""), "rows_source": str(rows_source or ""),
        "pending_intents": len(intents), "expired_intents": len(expired), "fills": len(records),
        "blocked": len(plan["blocks"]), "mandate_blocked": len(plan["mandate_blocks"]),
        "dry_run": is_dry, "written": False, "ledger_written": False,
        "records": records, "blocks": plan["blocks"], "mandate_blocks": plan["mandate_blocks"],
        "journal_rows": journal_rows, "restored_records": restored,
    }
    if not write:
        return result
    if records and not is_dry:
        # 先把**账本**写下去（真正的资金效果），再写成交/日记记录：这样中途失败时，
        # 账本要么没动、要么已动且带 intent_key，重试都能正确判断，既不漏账也不重复（T28）。
        writer = save or save_ledger
        try:
            writer(updated, runtime.ledger)
        except Exception as exc:  # 保存失败绝不报成功，也不留下会被当成"已成交"的记录
            result["save_error"] = f"{type(exc).__name__}:{exc}"
            result["written"] = False
            result["ledger_written"] = False
            result["planned_records"] = [{**row, "ledger_written": False} for row in records]
            result["records"] = []
            result["fills"] = 0
            return result
        result["ledger_written"] = True
    try:
        _append_jsonl(fills_path(runtime, dry=is_dry), records)
        _append_jsonl(journal_path(runtime), journal_rows)
    except OSError as exc:
        result["evidence_error"] = f"{type(exc).__name__}:{exc}"
        result["recovery_pending"] = True
        return result
    result["written"] = bool(records or journal_rows or restored)
    return result


def load_daytime_fills_for(as_of: str, *, state_dir: Path) -> list[dict[str, Any]]:
    """This date's intraday fills, real and dry, for the evening review."""

    out: list[dict[str, Any]] = []
    for name in (FILLS_NAME, DRY_FILLS_NAME):
        for row in _read_jsonl(assert_not_quantdesk_ledger(Path(state_dir) / name)):
            if str(row.get("as_of") or "") == str(as_of)[:10]:
                out.append(row)
    return out


def load_daytime_journal_for(as_of: str, *, state_dir: Path) -> list[dict[str, Any]]:
    """This date's intraday journal rows (blocks, mandate blocks, decisions)."""

    return [row for row in _read_jsonl(assert_not_quantdesk_ledger(Path(state_dir) / JOURNAL_NAME))
            if str(row.get("as_of") or "") == str(as_of)[:10]]


__all__ = [
    "BLOCK_INVALID_INTENT",
    "BLOCK_LIMIT_DOWN",
    "BLOCK_LIMIT_UP",
    "BLOCK_MISSING_QUOTE",
    "BLOCK_NOT_A_LOT",
    "BLOCK_NOT_LATER",
    "BLOCK_NO_POSITION",
    "BLOCK_SAME_SLOT",
    "BLOCK_TOO_MANY_SHARES",
    "BLOCK_T_PLUS_1",
    "DRY_ENV",
    "DRY_FILLS_NAME",
    "FILLS_NAME",
    "FILL_SCHEMA",
    "JOURNAL_NAME",
    "MANDATE_CASH_FLOOR",
    "MANDATE_EXITS_PER_DAY",
    "MANDATE_MAX_HOLDINGS",
    "MANDATE_OPENS_PER_DAY",
    "MANDATE_SINGLE_WEIGHT",
    "apply_to_ledger",
    "dry_mode",
    "expired_intents",
    "fill_keys",
    "fill_slot",
    "fills_path",
    "intent_key",
    "journal_path",
    "load_daytime_fills_for",
    "load_daytime_journal_for",
    "pending_intents",
    "plan_fills",
    "quote_index",
]
