"""Overnight paper trader: plan tonight, fill at next open, score tomorrow evening.

Fills never use the next day's high/low. High/low are only used after the
session ends, to check whether a preset level was traded through.
"""

from __future__ import annotations

import json
from copy import deepcopy
from typing import Any

import pandas as pd

from ai_trader.hypotheses import load_hypotheses, save_hypotheses
from ai_trader.journal import append_journal, build_journal
from ai_trader.ledger import (
    init_ledger,
    load_ledger,
    mark_to_market,
    position_map,
    save_ledger,
)
from ai_trader.ledger import session_days_held as _session_days_held
from ai_trader.llm import propose_picks
from ai_trader.mandate import BOUNDARY, DEFAULT_MANDATE, max_open_gap_for_style
from ai_trader.market import (
    focus_universe,
    latest_as_of,
    load_close_panel,
    load_minute_session,
    load_ohlc_row,
)
from ai_trader.paths import (
    PLAN_LATEST_PATH,
    CyclePaths,
    assert_not_quantdesk_ledger,
)
from ai_trader.philosophy import (
    add_note,
    load_philosophy,
    record_closed_trade,
    record_stance,
    save_philosophy,
    style_scoreboard,
)
from ai_trader.recommendations import load_recommendations, recommendation_note
from ai_trader.researcher import rank_today, research_ideas, template_by_id
from ai_trader.trader import execute_intents
from ai_trader.watchlist import load_watchlist, save_watchlist, touch_watchlist, watch_names
from quantdesk.paper.market_rules import lot_size_for_symbol, round_notional_down_to_lot_quantity
from quantdesk.trading_calendar import next_trade_day
from quantdesk.utils import atomic_write_json

PLAN_SCHEMA = "ai_trader_plan_v1"
PREDICTION_NOTE = (
    "预设买卖点只用于次日晚上验收这只交易员自己的判断，"
    "不是给你的目标价或投资建议。成交只按次日开盘价记账。"
)


def _read_json(path: Any) -> dict[str, Any]:
    target = assert_not_quantdesk_ledger(path)
    if not target.is_file():
        return {}
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def load_plan(path: Any = None) -> dict[str, Any]:
    return _read_json(path or PLAN_LATEST_PATH)


def save_plan(plan: dict[str, Any], path: Any = None) -> None:
    target = assert_not_quantdesk_ledger(path or PLAN_LATEST_PATH)
    target.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_json(target, plan)


def pick_operating_method(research: dict[str, Any]) -> tuple[dict[str, Any] | None, str]:
    live = research.get("live") if isinstance(research.get("live"), dict) else None
    if live:
        return live, "live"
    scored = [
        dict(item)
        for item in (research.get("evaluated") or [])
        if isinstance(item, dict) and item.get("holdout_mean_net") is not None
    ]
    if not scored:
        return None, "idle"
    best = dict(max(scored, key=lambda row: float(row.get("holdout_mean_net") or -999)))
    net = float(best.get("holdout_mean_net") or 0.0)
    best["status"] = "probation"
    best["reason"] = (
        f"没有方法通过成本门。暂用相对最好的「{best.get('title')}」开观察仓"
        f"（样本外净收益{net:.2%}），明确未验证，仓位减半、每天最多1笔。"
    )
    return best, "probation"


def session_days_held(acquired_on: str, as_of: str) -> int:
    """Re-exported from ``ledger`` so existing callers and tests keep working."""

    return _session_days_held(acquired_on, as_of)


def committed_buy_symbols_from_plan(plan: dict[str, Any]) -> list[str]:
    """Buy symbols actually written into this plan, in intent order."""

    out: list[str] = []
    for row in plan.get("intents") or []:
        if (
            isinstance(row, dict)
            and str(row.get("side") or "") == "buy"
            and row.get("symbol")
        ):
            symbol = str(row["symbol"])
            if symbol not in out:
                out.append(symbol)
    return out


def llm_journal_headline(
    trade_date: str,
    *,
    proposed: list[str],
    committed_buy_symbols: list[str],
    plan_committed: bool,
) -> str:
    """Headline that never conflates LLM proposals with committed intents.

    The brain may float several names; account constraints (probation open cap,
    holdings/cash limits) decide what actually lands in tomorrow's paper plan.
    Writing "看上 A、B" when only A survived made a proposal read like an
    order, so the wording now separates 提交买入 from 仅提议·未提交.
    """

    if not plan_committed:
        note = "、".join(dict.fromkeys(proposed)) if proposed else "无"
        return (
            f"今晚思考已记录（提议：{note}）；上一份计划还没结算，"
            f"明天（{trade_date}）不叠加新纸面委托。"
        )
    if committed_buy_symbols:
        headline = f"明天（{trade_date}）AI自己拿主意：提交买入 {'、'.join(committed_buy_symbols)}。"
    elif proposed:
        headline = f"明天（{trade_date}）AI看完没有提交新开仓。"
    else:
        headline = f"明天（{trade_date}）AI看完决定空仓，今晚不追高。"
    withheld = [s for s in dict.fromkeys(proposed) if s not in set(committed_buy_symbols)]
    if withheld:
        headline += f"（曾提议 {'、'.join(withheld)}，未通过账户约束，仅作记录、未提交。）"
    return headline


def _story_broken(
    symbol: str,
    hypothesis_id: str,
    as_of: str,
    panel: Any,
    close: float | None,
) -> str | None:
    if not close or panel is None or getattr(panel, "empty", True):
        return None
    if str(hypothesis_id or "") != "ma20_trend" or symbol not in getattr(panel, "columns", []):
        return None
    series = panel[symbol].dropna()
    if as_of not in getattr(series, "index", []) or len(series.loc[:as_of]) < 20:
        return None
    ma = float(series.loc[:as_of].tail(20).mean())
    if close < ma:
        return f"收盘{close:.2f}跌回近20日均价{ma:.2f}下方，短线故事坏了。"
    return None


def _news_digest(symbol: str, *, network: bool = True) -> dict[str, Any]:
    try:
        from quantdesk.providers.news_provider import fetch_news
    except Exception as exc:  # pragma: no cover - import guard
        return {"status": "missing", "headlines": [], "reason": f"news_import_failed:{exc}"}
    payload = fetch_news(symbol, limit=3, network=network)
    rows = [row for row in (payload.get("data") or []) if isinstance(row, dict)]
    headlines = [str(row.get("title") or "").strip() for row in rows if str(row.get("title") or "").strip()]
    if headlines:
        return {
            "status": "cache" if not network else str(payload.get("status") or "fetched"),
            "headlines": headlines[:3],
            "reason": "",
        }
    return {
        "status": "missing",
        "headlines": [],
        "reason": str(payload.get("failed_reason") or "no_cached_news"),
    }


def _session_note(symbol: str, as_of: str, minute_dir: Any, daily_dir: Any) -> dict[str, Any]:
    tape = load_minute_session(symbol, as_of, minute_dir)
    daily = load_ohlc_row(symbol, as_of, daily_dir)
    note = {
        "minutes": tape.get("status") or "missing",
        "minute_bars": tape.get("bars") or 0,
        "minute_reason": tape.get("reason") or "",
        "close_location": tape.get("close_location"),
        "daily_close": daily.get("close"),
        "daily_open": daily.get("open"),
    }
    if tape.get("status") in {"complete", "partial"} and tape.get("close_location") is not None:
        loc = float(tape["close_location"])
        if loc >= 0.7:
            note["path_comment"] = "今天收在当日分钟区间偏高处。"
        elif loc <= 0.3:
            note["path_comment"] = "今天收在当日分钟区间偏低处。"
        else:
            note["path_comment"] = "今天收在当日分钟区间中部。"
    elif daily.get("close"):
        note["path_comment"] = "没有当天完整分钟，只用收盘后的日线做作业，不当盘中盯盘。"
    else:
        note["path_comment"] = "当天行情不足。"
    return note


def _direction_from_score(score: float) -> str:
    if score > 0:
        return "up"
    if score < 0:
        return "down"
    return "flat"


def _recent_change(panel: Any, symbol: str, as_of: str, days: int = 5) -> float | None:
    """N-session close-to-close change for one symbol, or None if too short."""

    try:
        series = panel[symbol].dropna()
        series = series[series.index <= as_of]
        if len(series) <= days:
            return None
        prior = float(series.iloc[-1 - days])
        if prior <= 0:
            return None
        return float(series.iloc[-1] / prior - 1.0)
    except (KeyError, IndexError, ValueError, TypeError):
        return None


def _build_llm_context(
    *,
    as_of: str,
    trade_date: str,
    ledger: dict[str, Any],
    panel: Any,
    recommended: list[str],
    research: dict[str, Any],
    daily_dir: Any,
    philosophy: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Assemble the facts the trader's LLM brain reasons over. Read-only."""

    mandate = DEFAULT_MANDATE
    held = position_map(ledger)
    # Read the keys that actually exist. This previously asked for `cost_price` /
    # `avg_price`, neither of which is ever written, so every holding was shown as
    # "@成本?" — the component deciding what to sell could not tell whether a
    # position was up or down, and any sell reason it gave was a rationalisation.
    holdings = [
        f"{sym}×{int(pos.get('quantity') or 0)}"
        f"@成本{pos.get('avg_cost') or '?'}"
        f" 现价{pos.get('market_price') or '?'}"
        f" 浮动{float(pos.get('unrealized_pnl') or 0.0):+.0f}"
        f" 已持{session_days_held(str(pos.get('acquired_on') or '')[:10], as_of)}天"
        + (f" [{pos.get('style')}]" if pos.get("style") else "")
        + (f" 计划{pos.get('planned_hold_days')}天" if pos.get("planned_hold_days") else "")
        + (" ⚠价格是上次的(停牌/无行情)" if pos.get("stale_mark") else "")
        for sym, pos in held.items()
    ]
    columns = [str(col) for col in getattr(panel, "columns", [])]
    candidates: list[str] = []
    for sym in columns:
        if not load_ohlc_row(sym, as_of, daily_dir).get("close"):
            continue
        change = _recent_change(panel, sym, as_of)
        candidates.append(f"{sym}({change:+.1%})" if change is not None else sym)
        if len(candidates) >= 40:
            break
    method_results = [
        f"{row.get('title') or row.get('hypothesis_id')}:净{float(row.get('holdout_mean_net')):.1%}"
        for row in (research.get("evaluated") or [])
        if isinstance(row, dict) and row.get("holdout_mean_net") is not None
    ]
    mandate_note = (
        f"持仓≤{mandate.max_holdings}只、单票≤{mandate.max_single_weight:.0%}、"
        f"现金≥{mandate.min_cash_weight:.0%}、每天最多开{mandate.max_opens_per_day}只、"
        f"最多卖{mandate.max_exits_per_day}只。"
        "这些只是账户安全上限，不限制你的风格：持有多久、做什么类型，你自己定。"
    )
    return {
        "as_of": as_of,
        "trade_date": trade_date,
        "persona_zh": mandate.persona_zh,
        "equity": round(float(ledger.get("equity") or mandate.initial_cash), 2),
        "cash": round(float(ledger.get("cash") or 0.0), 2),
        "holdings_count": len(held),
        "holdings": holdings,
        "recommendations": list(recommended),
        "method_results": method_results,
        "candidates": candidates,
        "mandate_note": mandate_note,
        # Its own previously-written stance and its own realised track record per
        # style, so a change of mind is informed by evidence rather than mood.
        "stance": (philosophy or {}).get("stance") or {},
        "style_scoreboard": style_scoreboard(philosophy or {}),
        # Facts the local historical check established. Offered as evidence the
        # trader may weigh or reject, not as an instruction.
        "evidence_notes": [
            "本地历史检验(2018-2026,复权修复后):超跌反弹类信号真实存在但组合级年化仅个位数、"
            "回撤可达58%、夏普0.3-0.4;追涨类(突破/新高/放量)择时为负",
            "反转因子 rev20 预测力强(IC 0.052,方向64.7%)但毛利约33bp,接近换手成本,长期持有可省掉这部分成本",
        ],
    }


def build_plan(
    *,
    decision_date: str,
    trade_date: str,
    ledger: dict[str, Any],
    panel: Any,
    ranked: list[tuple[str, float]],
    live: dict[str, Any] | None,
    daily_dir: Any,
    minute_dir: Any,
    network_news: bool = True,
    conviction: str = "live",
    pick_reasons: dict[str, Any] | None = None,
    llm_sells: list[dict[str, Any]] | None = None,
    llm_directed: bool = False,
) -> dict[str, Any]:
    mandate = DEFAULT_MANDATE
    equity = float(ledger.get("equity") or mandate.initial_cash)
    scale = 0.5 if conviction == "probation" else 1.0
    max_opens = 1 if conviction == "probation" else mandate.max_opens_per_day
    target_notional = min(mandate.target_notional() * scale, mandate.max_single_notional(equity) * scale)
    held = position_map(ledger)
    intents: list[dict[str, Any]] = []
    warnings: list[str] = []
    live_id = str(live.get("hypothesis_id")) if live else None
    hold_days = int(mandate.fallback_hold_days)
    llm_sell_map = {
        str(row.get("symbol")): str(row.get("reason") or "")
        for row in (llm_sells or [])
        if isinstance(row, dict) and row.get("symbol")
    }

    # Exits must be considered whenever there is something to exit. Gating this on
    # a live rule-engine hypothesis would mean a self-directed trader could never
    # close a position it opened on its own reasoning.
    if live_id or (llm_directed and held):
        sell_count = 0
        for symbol, pos in held.items():
            if sell_count >= mandate.max_exits_per_day:
                break
            close = float(pos.get("market_price") or load_ohlc_row(symbol, decision_date, daily_dir).get("close") or 0)
            if close <= 0:
                continue
            acquired = str(pos.get("acquired_on") or "")[:10]
            planned = int(pos.get("planned_hold_days") or hold_days)
            held_days = session_days_held(acquired, decision_date)
            style = str(pos.get("style") or "")
            # The MA20 "story broken" rule is a short-term trading rule. Applying it
            # to a position opened as 中线/长期 would liquidate a long thesis on a
            # three-week wobble, which is exactly the style coercion this trader is
            # not supposed to be under.
            short_term = style not in {"中线", "长期"}
            broken = (
                _story_broken(symbol, str(pos.get("hypothesis_id") or live_id), decision_date, panel, close)
                if short_term
                else None
            )
            asked_to_sell = symbol in llm_sell_map
            # Reaching the planned horizon is a prompt to reconsider, not a deadline.
            # When the brain is live it decides; only without a brain does maturity
            # force the exit, so the rule engine still terminates on its own.
            matured = held_days >= planned
            if llm_directed:
                if not asked_to_sell and not broken:
                    continue
            elif not matured and not broken:
                continue
            news = _news_digest(symbol, network=network_news)
            session = _session_note(symbol, decision_date, minute_dir, daily_dir)
            if news["status"] == "missing":
                warnings.append(f"news_missing:{symbol}")
            if session["minutes"] == "missing":
                warnings.append(f"minutes_missing:{symbol}")
            why = (
                llm_sell_map.get(symbol)
                or broken
                or f"已经按计划拿了{held_days}天（打算{planned}天），明天开盘了结。"
            )
            intents.append(
                {
                    "symbol": symbol,
                    "side": "sell",
                    "quantity": int(pos.get("quantity") or 0),
                    "ref_close": close,
                    "predicted_direction": "down",
                    "preset_touch_price": round(close * 1.01, 2),
                    "preset_meaning": "预测次日最高价会触及此卖点（只验收，不按此价成交）",
                    "hypothesis_id": live_id,
                    "conviction": conviction,
                    "thesis": pos.get("thesis") or why,
                    "planned_hold_days": planned,
                    "reason": f"{session.get('path_comment')} {why}",
                    "news": news,
                    "session": session,
                }
            )
            sell_count += 1
        buy_count = 0
        for symbol, score in ranked:
            if buy_count >= max_opens:
                break
            if symbol in held or any(item.get("symbol") == symbol for item in intents):
                continue
            row = load_ohlc_row(symbol, decision_date, daily_dir)
            close = row.get("close")
            if not close:
                continue
            lot = lot_size_for_symbol(symbol)
            qty = round_notional_down_to_lot_quantity(target_notional, close, lot_size=lot)
            if qty <= 0:
                continue
            news = _news_digest(symbol, network=network_news)
            session = _session_note(symbol, decision_date, minute_dir, daily_dir)
            if news["status"] == "missing":
                warnings.append(f"news_missing:{symbol}")
            if session["minutes"] == "missing":
                warnings.append(f"minutes_missing:{symbol}")
            # A live LLM pick supplies its own reason and hold horizon; without
            # one we keep the rule-engine template. Every quantity/limit check
            # above and the ledger below are unchanged either way.
            llm_pick = (pick_reasons or {}).get(symbol) or {}
            llm_reason = str(llm_pick.get("reason") or "").strip()
            plan_hold = int(llm_pick.get("hold_days") or 0) or hold_days
            pick_style = str(llm_pick.get("style") or "").strip()
            # Gap tolerance follows the declared style, so a 打板 pick is allowed to
            # chase and a 长期 pick is not abandoned over an opening gap.
            gap_limit = max_open_gap_for_style(pick_style, mandate.max_chase_open_gap_pct)
            style_tag = f"[{pick_style}]" if pick_style else ""
            gap_text = "不因高开放弃" if gap_limit is None else f"高开超{gap_limit:.0f}%放弃"
            probation_tag = "【观察仓·未验证】" if conviction == "probation" else ""
            if llm_reason:
                thesis = f"{probation_tag}{style_tag}{llm_reason}（打算拿{plan_hold}个交易日，{gap_text}）"
            else:
                thesis = (
                    f"{probation_tag}"
                    f"今晚看上这只，打算拿{plan_hold}个交易日。{gap_text}。"
                )
            intents.append(
                {
                    "symbol": symbol,
                    "side": "buy",
                    "quantity": qty,
                    "ref_close": close,
                    "predicted_direction": _direction_from_score(score),
                    "preset_touch_price": round(close * 0.99, 2),
                    "preset_meaning": "预测次日最低价会触及此买点（只验收，不按此价成交）",
                    "hypothesis_id": live_id,
                    "conviction": conviction,
                    "thesis": thesis,
                    "llm_reason": llm_reason or None,
                    "planned_hold_days": plan_hold,
                    "style": pick_style or None,
                    "invalidation": (
                        "计划天数到了只是重新考虑的时点，不是必须卖；理由还在就继续拿。"
                        "不因为今晚没排进前几名就卖。"
                    ),
                    "abandon_if_open_gap_pct": gap_limit,
                    "reason": (
                        f"{thesis} {session.get('path_comment')}"
                        f"消息：{('；'.join(news['headlines'][:1]) if news['headlines'] else '未取到公告')}。"
                        f"{'没有当天分钟也照样看，分钟不决定能不能用。' if session['minutes'] == 'missing' else ''}"
                    ),
                    "news": news,
                    "session": session,
                }
            )
            buy_count += 1

    return {
        "schema_version": PLAN_SCHEMA,
        "plan_id": f"plan_{decision_date.replace('-', '')}_for_{trade_date.replace('-', '')}",
        "decision_date": decision_date,
        "trade_date": trade_date,
        "status": "pending_open",
        "execution_policy": "next_open_only",
        "live_method": live,
        "conviction": conviction,
        "persona": mandate.persona,
        "persona_zh": mandate.persona_zh,
        "intents": intents,
        "warnings": warnings[-30:],
        "prediction_note": PREDICTION_NOTE,
        "boundary": dict(BOUNDARY),
        "role": "overnight_open_trader",
        "role_zh": mandate.persona_zh,
    }


def _score_intent(intent: dict[str, Any], session: dict[str, float | None], fill: dict[str, Any] | None) -> dict[str, Any]:
    close_px = session.get("close")
    high = session.get("high")
    low = session.get("low")
    ref = float(intent.get("ref_close") or 0.0)
    preset = float(intent.get("preset_touch_price") or 0.0)
    predicted = str(intent.get("predicted_direction") or "flat")
    actual_dir = "flat"
    if close_px and ref:
        if close_px > ref * 1.001:
            actual_dir = "up"
        elif close_px < ref * 0.999:
            actual_dir = "down"
    direction_hit = predicted == actual_dir
    touch_hit = False
    if preset and low and high:
        side = str(intent.get("side") or "")
        if side == "buy":
            touch_hit = float(low) <= preset
        elif side == "sell":
            touch_hit = float(high) >= preset
        else:
            touch_hit = float(low) <= preset <= float(high)
    return {
        "symbol": intent.get("symbol"),
        "side": intent.get("side"),
        "predicted_direction": predicted,
        "actual_direction": actual_dir,
        "direction_hit": direction_hit,
        "preset_touch_price": preset,
        "session_open": session.get("open"),
        "session_high": high,
        "session_low": low,
        "session_close": close_px,
        "touch_hit": touch_hit,
        "touch_check": "daily_range_after_close",
        "used_high_low_for_fill": False,
        "open_fill": (fill or {}).get("status") or "not_attempted",
        "fill_price": (fill or {}).get("price"),
        "fill_model": "next_open_only",
    }


def settle_plan(
    plan: dict[str, Any],
    ledger: dict[str, Any],
    *,
    as_of: str,
    daily_dir: Any,
) -> tuple[dict[str, Any], dict[str, Any], list[dict[str, Any]]]:
    if str(plan.get("trade_date") or "") != as_of:
        return plan, ledger, []
    if str(plan.get("status") or "") == "settled":
        return plan, ledger, []
    intents = [dict(row) for row in (plan.get("intents") or []) if isinstance(row, dict)]
    open_prices: dict[str, float] = {}
    prev_closes: dict[str, float] = {}
    mark_prices: dict[str, float] = {}
    sessions: dict[str, dict[str, float | None]] = {}
    for intent in intents:
        symbol = str(intent.get("symbol") or "")
        row = load_ohlc_row(symbol, as_of, daily_dir)
        sessions[symbol] = row
        if row.get("open"):
            open_prices[symbol] = float(row["open"])
        if intent.get("ref_close"):
            prev_closes[symbol] = float(intent["ref_close"])
        if row.get("close"):
            mark_prices[symbol] = float(row["close"])
        elif row.get("open"):
            mark_prices[symbol] = float(row["open"])
    # Price the WHOLE book, not just tonight's intents. execute_intents marks the
    # entire ledger with this dict, so any holding absent from it would be valued
    # at zero and equity (plus the cash floor derived from it) would be wrong.
    for symbol in position_map(ledger):
        if symbol in mark_prices:
            continue
        row = load_ohlc_row(symbol, as_of, daily_dir)
        if row.get("close"):
            mark_prices[symbol] = float(row["close"])
        elif row.get("open"):
            mark_prices[symbol] = float(row["open"])
    live = plan.get("live_method") if isinstance(plan.get("live_method"), dict) else {}
    live_id = str(live.get("hypothesis_id") or "") or None
    ledger, actions = execute_intents(
        ledger,
        as_of=as_of,
        intents=intents,
        open_prices=open_prices,
        prev_closes=prev_closes,
        mark_prices=mark_prices,
        live_id=live_id,
    )
    fill_by_symbol = {}
    for row in actions:
        if row.get("symbol"):
            fill_by_symbol[str(row["symbol"])] = row
    scorecard = [_score_intent(intent, sessions.get(str(intent.get("symbol")), {}), fill_by_symbol.get(str(intent.get("symbol")))) for intent in intents]
    dir_hits = sum(1 for row in scorecard if row.get("direction_hit"))
    touch_hits = sum(1 for row in scorecard if row.get("touch_hit"))
    fills = sum(1 for row in scorecard if row.get("open_fill") == "filled")
    abandoned = sum(1 for row in scorecard if row.get("open_fill") == "abandoned")
    n = max(len(scorecard), 1)
    settled = deepcopy(plan)
    settled["status"] = "settled"
    settled["settled_on"] = as_of
    settled["actions"] = actions
    settled["scorecard"] = scorecard
    settled["score_summary"] = {
        "intents": len(scorecard),
        "open_fills": fills,
        "abandoned": abandoned,
        "direction_hits": dir_hits,
        "touch_hits": touch_hits,
        "direction_hit_rate": round(dir_hits / n, 4) if scorecard else None,
        "touch_hit_rate": round(touch_hits / n, 4) if scorecard else None,
    }
    return settled, ledger, actions


def run_evening(
    *,
    paths: CyclePaths,
    lookback: int = 180,
    universe_limit: int = 240,
    network_news: bool | None = None,
) -> dict[str, Any]:
    runtime = paths
    runtime.ledger.parent.mkdir(parents=True, exist_ok=True)
    init_ledger(path=runtime.ledger)
    ledger = load_ledger(runtime.ledger)
    calendar = latest_as_of(runtime.daily_dir)
    as_of = str(calendar.get("as_of") or "")[:10]
    watch = load_watchlist(runtime.watchlist)
    held_names = list(position_map(ledger).keys())
    recommendations = load_recommendations(require_fresh_for=as_of)
    recommended_names = list(recommendations.get("symbols") or [])
    focus_limit = min(max(int(universe_limit), 8), 80)
    symbols = focus_universe(
        runtime.daily_dir,
        as_of,
        held=held_names,
        watch=watch_names(watch),
        recommended=recommended_names,
        limit=focus_limit,
    )
    warnings: list[str] = []
    panel, _pct, market_warnings = (
        load_close_panel(symbols, runtime.daily_dir, lookback=lookback)
        if symbols
        else (pd.DataFrame(), pd.DataFrame(), ["focus_universe_empty"])
    )
    warnings.extend(market_warnings)
    if as_of not in getattr(panel, "index", []) and len(getattr(panel, "index", [])):
        earlier = panel.index[panel.index <= as_of]
        as_of = str((earlier[-1] if len(earlier) else panel.index[-1]))
        warnings.append(f"as_of_rolled_to_last_bar:{as_of}")
        symbols = focus_universe(
            runtime.daily_dir,
            as_of,
            held=held_names,
            watch=watch_names(watch),
            recommended=recommended_names,
            limit=focus_limit,
        )
        panel, _pct, market_warnings = load_close_panel(symbols, runtime.daily_dir, lookback=lookback)
        warnings.extend(market_warnings)

    prices = {}
    if as_of in getattr(panel, "index", []):
        prices = {str(col): float(val) for col, val in panel.loc[as_of].dropna().items()}
    ledger = mark_to_market(ledger, prices, as_of=as_of) if prices else ledger

    existing = load_plan(runtime.plan_latest)
    review: dict[str, Any] | None = None
    settle_actions: list[dict[str, Any]] = []
    deferred_plan: dict[str, Any] | None = None
    plan_trade_date = str((existing or {}).get("trade_date") or "")[:10]
    if existing and plan_trade_date and str(existing.get("status") or "") != "settled" and plan_trade_date <= as_of:
        # A missed evening (crash, power loss, machine off) must not silently
        # void a plan that was already committed and timestamped.  Settle it
        # against its OWN trade date -- that session's open is objective local
        # history -- and mark the lateness instead of back-dating quietly.
        # Guard: if that date has no bar for any intent yet, defer rather than
        # record a false "abandoned" caused by a data gap.
        intent_symbols = [
            str(row.get("symbol") or "")
            for row in (existing.get("intents") or [])
            if isinstance(row, dict) and row.get("symbol")
        ]
        has_bar = any(
            (load_ohlc_row(symbol, plan_trade_date, runtime.daily_dir) or {}).get("open")
            for symbol in intent_symbols
        )
        if not intent_symbols or has_bar:
            review, ledger, settle_actions = settle_plan(
                existing, ledger, as_of=plan_trade_date, daily_dir=runtime.daily_dir
            )
            if plan_trade_date != as_of:
                review["settled_late"] = True
                review["settled_late_from"] = plan_trade_date
                review["settled_late_observed_on"] = as_of
                warnings.append(f"plan_settled_late:{plan_trade_date}->{as_of}")
            save_plan(review, runtime.plan_latest)
            runtime.review_latest.parent.mkdir(parents=True, exist_ok=True)
            atomic_write_json(assert_not_quantdesk_ledger(runtime.review_latest), review)
            if prices and plan_trade_date != as_of:
                # Positions opened on an older session must be re-marked to the
                # latest close before tonight's decisions.
                ledger = mark_to_market(ledger, prices, as_of=as_of)
            save_ledger(ledger, runtime.ledger)
            warnings.extend(list(review.get("warnings") or []))
        else:
            # A committed plan must not be silently voided. There is only one
            # plan_latest slot, so tonight's plan must NOT overwrite it: doing so
            # loses those intents forever (never filled, never scored). Keep the
            # unsettled plan and commit nothing new until its bar arrives, which
            # the settled_late path above then handles.
            deferred_plan = existing
            warnings.append(f"plan_settlement_deferred_missing_bars:{plan_trade_date}")

    book = load_hypotheses(runtime.hypotheses)
    research = research_ideas(panel, book, as_of=as_of) if not panel.empty else {"book": book, "evaluated": [], "live": None}
    save_hypotheses(research["book"], runtime.hypotheses)
    use_network = DEFAULT_MANDATE.network_enabled if network_news is None else bool(network_news)
    method, conviction = pick_operating_method(research)
    ranked: list[tuple[str, float]] = []
    template = template_by_id(str((method or {}).get("hypothesis_id") or ""))
    if template is not None:
        ranked = rank_today(template, panel, as_of)
    try:
        trade_date = next_trade_day(as_of)
    except Exception:
        trade_date = as_of
        warnings.append("next_trade_day_failed")

    # LLM brain (optional): if a local endpoint is configured, let the trader
    # think and choose. It only reorders/replaces the buy candidates and gives
    # reasons; every quantity/limit/next-open/ledger rule below is unchanged.
    # No endpoint or any failure => proposal is None => rule `ranked` stands.
    pick_reasons: dict[str, Any] = {}
    llm_used = False
    llm_model = ""
    llm_note = ""
    llm_sells: list[dict[str, Any]] = []
    llm_stance: dict[str, Any] = {}
    llm_lesson = ""
    philosophy = load_philosophy(runtime.philosophy)
    philosophy_dirty = False
    # Attribute every position that closed today to the style it was opened under.
    # Without this the style_ledger stays permanently empty and the trader can
    # never be shown "波段 10 笔 7 负" — which is the entire point of letting it
    # pick its own style and watch how that works out.
    for action in settle_actions:
        if not isinstance(action, dict):
            continue
        if action.get("action") != "sell" or action.get("status") != "filled":
            continue
        philosophy = record_closed_trade(
            philosophy,
            style=str(action.get("style") or ""),
            net_return=action.get("net_return"),
            hold_days=action.get("held_days"),
        )
        philosophy_dirty = True
    llm_diag: dict[str, Any] = {}
    if as_of in getattr(panel, "index", []) and len(getattr(panel, "columns", [])):
        proposal = propose_picks(
            _build_llm_context(
                as_of=as_of,
                trade_date=trade_date,
                ledger=ledger,
                panel=panel,
                recommended=recommended_names,
                research=research,
                daily_dir=runtime.daily_dir,
                philosophy=philosophy,
            ),
            llm_diag,
        )
        if proposal is not None:
            llm_used = True
            llm_model = str(proposal.get("model") or "")
            llm_note = str(proposal.get("market_note") or "")
            llm_stance = proposal.get("stance") or {}
            llm_lesson = str(proposal.get("lesson") or "")
            # Only sells of positions actually held are honoured; a hallucinated
            # symbol can never become an order.
            held_now = set(position_map(ledger).keys())
            llm_sells = [
                row for row in (proposal.get("sells") or [])
                if str(row.get("symbol")) in held_now
            ]
            available = {str(col) for col in panel.columns}
            llm_ranked: list[tuple[str, float]] = []
            dropped: list[str] = []
            buys = proposal.get("buys") or []
            for index, buy in enumerate(buys):
                symbol = str(buy.get("symbol") or "")
                has_bar = bool(load_ohlc_row(symbol, as_of, runtime.daily_dir).get("close"))
                if symbol in available and has_bar and symbol not in pick_reasons:
                    # Descending score preserves the LLM's own ranking order.
                    llm_ranked.append((symbol, float(len(buys) - index)))
                    pick_reasons[symbol] = {
                        "reason": buy.get("reason") or "",
                        "hold_days": buy.get("hold_days") or 0,
                        "style": buy.get("style") or "",
                    }
                elif symbol:
                    dropped.append(symbol)
            # LLM leads: its (possibly empty) list replaces the rule ranking.
            # Empty means the trader chose to open nothing tonight.
            ranked = llm_ranked
            if dropped:
                warnings.append("llm_pick_no_bar_or_dupe:" + ",".join(dropped[:10]))
            if llm_note:
                warnings.append(f"llm_market_note:{llm_note[:60]}")

    plan = build_plan(
        decision_date=as_of,
        trade_date=trade_date,
        ledger=ledger,
        panel=panel,
        ranked=ranked,
        live=method,
        daily_dir=runtime.daily_dir,
        minute_dir=runtime.minute_dir,
        network_news=use_network,
        conviction=conviction,
        pick_reasons=pick_reasons,
        llm_sells=llm_sells,
        llm_directed=llm_used,
    )
    # Persist whatever the trader said about itself tonight, so tomorrow it can
    # read its own mind back and the user can watch the style evolve.
    if llm_stance.get("summary"):
        philosophy = record_stance(
            philosophy,
            as_of=as_of,
            summary=llm_stance.get("summary") or "",
            preferred_styles=llm_stance.get("preferred_styles") or [],
            reasoning=llm_stance.get("reasoning") or "",
        )
        philosophy_dirty = True
    if llm_lesson:
        philosophy = add_note(philosophy, as_of=as_of, note=llm_lesson)
        philosophy_dirty = True
    if philosophy_dirty:
        save_philosophy(philosophy, runtime.philosophy)
    plan["llm"] = {
        "used": llm_used,
        "model": llm_model,
        "market_note": llm_note,
        "picked": sorted(pick_reasons.keys()),
        "styles": {sym: (row.get("style") or "") for sym, row in pick_reasons.items()},
        "sells_requested": [str(row.get("symbol")) for row in llm_sells],
        "stance": llm_stance,
        "lesson": llm_lesson,
        # Why the brain did not engage, when it did not. Recorded so a silent
        # fallback to the rule engine is never mistaken for the AI's own decision.
        "unavailable_reason": "" if llm_used else str(llm_diag.get("reason") or ""),
    }
    if not llm_used and llm_diag.get("reason"):
        warnings.append(f"llm_unavailable:{llm_diag['reason']}")
    warnings.extend(list(plan.get("warnings") or []))
    # Record whether each pick came from the research desk or from the trader's
    # own scan, so the journal can show the user which is which.  Membership is
    # tested against the actual pool read above; it is never asserted blindly.
    recommended_set = set(recommended_names)
    for intent in plan.get("intents") or []:
        if not isinstance(intent, dict):
            continue
        symbol = str(intent.get("symbol") or "")
        from_desk = symbol in recommended_set
        by_llm = bool(intent.get("llm_reason")) or (symbol in pick_reasons and intent.get("side") == "buy")
        if by_llm and from_desk:
            intent["pick_source"] = "llm_research_desk"
            intent["pick_source_zh"] = "AI选(来自研究台推荐)"
        elif by_llm:
            intent["pick_source"] = "llm_self_selected"
            intent["pick_source_zh"] = "AI自选"
        else:
            intent["pick_source"] = "research_desk_recommendation" if from_desk else "self_selected"
            intent["pick_source_zh"] = "研究台推荐" if from_desk else "自己选的"
        if from_desk:
            note = recommendation_note(recommendations.get("rows") or [], symbol)
            if note:
                intent["recommendation_note"] = note
                intent["reason"] = f"{intent.get('reason') or ''} {note}".strip()
    plan["recommendation_view"] = {
        "symbols": recommended_names,
        "count": len(recommended_names),
        "warnings": list(recommendations.get("warnings") or []),
        "advisory_only": True,
    }
    warnings.extend(list(recommendations.get("warnings") or []))
    touched = [str(row.get("symbol")) for row in (plan.get("intents") or []) if row.get("symbol")]
    watch = touch_watchlist(
        watch,
        touched + held_names,
        as_of=as_of,
        reason="今晚复盘看过",
        keep=held_names,
    )
    save_watchlist(watch, runtime.watchlist)
    if deferred_plan is not None:
        # The still-unsettled committed plan keeps the slot. Tonight's thinking is
        # preserved in the journal, but nothing new is committed on top of it.
        plan["committed"] = False
        plan["not_committed_reason"] = (
            f"上一份计划({deferred_plan.get('trade_date')})还没成交结算(缺当天K线)，"
            "今晚不再叠加新委托，等它结算后再说。"
        )
        warnings.append("tonight_plan_not_committed_pending_deferred_settlement")
        save_plan(deferred_plan, runtime.plan_latest)
    else:
        plan["committed"] = True
        save_plan(plan, runtime.plan_latest)
    save_ledger(ledger, runtime.ledger)

    if conviction == "live" and method:
        headline = f"明天（{trade_date}）按开盘预案做。"
        thought = (
            "今晚先看手里的票，理由还在就继续拿——计划天数到了不等于必须卖。"
            f"没有大脑在线时按规则引擎的保守default走（{DEFAULT_MANDATE.fallback_hold_days}天）。"
        )
    elif conviction == "probation" and method:
        headline = (
            f"明天（{trade_date}）轻仓试一只。方法「{method.get('title')}」还没通过检验，当观察仓。"
        )
        thought = (
            str(method.get("reason") or "")
            + " 这是试错，不是打板。没有当天分钟也可以看这只，分钟不决定资格。"
        )
    else:
        headline = f"明天（{trade_date}）先空仓。今晚没看出值得试的票。"
        thought = "现金也是决定。编故事硬做，不像新人，像赌。"
    if llm_used:
        # The journal must say what was actually submitted. `picked` is the
        # LLM's proposal; account constraints may have kept only part of it
        # (e.g. probation allows one open per night), and history showed the
        # headline listing both as if both were ordered.
        headline = llm_journal_headline(
            trade_date,
            proposed=[str(s) for s in (plan.get("llm", {}).get("picked") or [])],
            committed_buy_symbols=committed_buy_symbols_from_plan(plan),
            plan_committed=bool(plan.get("committed", True)),
        )
        thought = (f"[AI大脑·{llm_model}] {llm_note}".strip() + " " + thought).strip()
    if review and review.get("score_summary"):
        summary = review["score_summary"]
        thought = (
            f"今晚验收上一笔计划：开盘成交{summary.get('open_fills')}/{summary.get('intents')}，"
            f"方向对了{summary.get('direction_hits')}，买点/卖点触及{summary.get('touch_hits')}。"
            + thought
        )
    note = build_journal(
        as_of=as_of,
        research=research,
        actions=settle_actions,
        ledger=ledger,
        warnings=warnings,
        trade_date=trade_date,
        # Without these the note you read each night showed only the rule
        # engine's narration, even when the trader's own brain made the call.
        llm=plan.get("llm"),
        philosophy=philosophy,
        # Keep 提议 and 已提交 apart inside the journal body as well; the
        # headline override below uses the same truth.
        committed_buy_symbols=(
            committed_buy_symbols_from_plan(plan)
            if plan.get("committed", True)
            else []
        ),
    )
    if conviction == "probation" and method:
        note["lessons"] = [
            f"【观察仓·未验证】{method.get('title')}：{method.get('reason')}"
        ] + list(note.get("lessons") or [])
    note["headline"] = headline
    note["thought"] = thought
    note["role_zh"] = plan.get("role_zh")
    note["tomorrow_plan"] = {
        "trade_date": trade_date,
        "intents": plan.get("intents"),
        "conviction": conviction,
        "prediction_note": PREDICTION_NOTE,
    }
    note["last_review"] = None if not review else {
        "plan_id": review.get("plan_id"),
        "score_summary": review.get("score_summary"),
        "scorecard": review.get("scorecard"),
    }
    append_journal(note, latest_path=runtime.journal_latest, log_path=runtime.journal_log)
    snapshot = {
        "ok": True,
        "as_of": as_of,
        "mode": "overnight_open",
        "persona": DEFAULT_MANDATE.persona,
        "persona_zh": DEFAULT_MANDATE.persona_zh,
        "hunting_ground_size": len(symbols),
        "watchlist": watch_names(watch),
        "live_method": method if conviction == "live" else None,
        "operating_method": method,
        "conviction": conviction,
        "network_news": use_network,
        "plan": plan,
        "review": review,
        "journal": note,
        "ledger": {
            "ledger_id": ledger.get("ledger_id"),
            "cash": ledger.get("cash"),
            "equity": ledger.get("equity"),
            "market_value": ledger.get("market_value"),
            "realized_pnl": ledger.get("realized_pnl"),
            "unrealized_pnl": ledger.get("unrealized_pnl"),
            "positions": ledger.get("positions"),
            "as_of": ledger.get("as_of"),
        },
        "hypotheses": research.get("evaluated"),
        "actions": settle_actions,
        "warnings": warnings[-30:],
        "mandate": DEFAULT_MANDATE.to_dict(),
        "boundary": dict(BOUNDARY),
        "disclaimer": note.get("disclaimer"),
        "prediction_note": PREDICTION_NOTE,
    }
    runtime.cycle_latest.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_json(assert_not_quantdesk_ledger(runtime.cycle_latest), snapshot)
    atomic_write_json(assert_not_quantdesk_ledger(runtime.snapshot), snapshot)
    return snapshot
