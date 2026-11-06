"""Conditional intraday execution and paper-ledger application.

The engine replays frozen conditional plans against local 1-minute bars. It
does not use daily feature rows or strategy candidates during execution.
"""

from __future__ import annotations

import copy
import hashlib
import json
import math
import os
from collections import Counter
from pathlib import Path
from typing import Any

import pandas as pd

from quantdesk.candidate_contract import normalize_strategy_type, strategy_label
from quantdesk.daily_candidate_refresh import verify_candidate_envelope
from quantdesk.csv_io import read_csv_frame
from quantdesk.intraday.minute_pool_capability import (
    load_pool_for_intraday_dir,
    summarize_minute_replay_capability,
)
from quantdesk.intraday.minute_pool_window_audit import (
    load_window_verified_minute_pool,
    resolve_minute_replay_evidence,
)
from quantdesk.intraday.intraday_schema import normalize_intraday_frame as normalize_intraday_schema_frame
from quantdesk.paper.conditional_trade_plan import (
    CONDITIONAL_TRADE_PLANS_LATEST_JSON,
    DISCLAIMER,
    PROJECT_ROOT,
    conditional_trade_plan_latest,
)
from quantdesk.paper.adjustment_policy import (
    AdjustmentFactorTable,
    apply_paper_adjustment_policy,
)
from quantdesk.paper.market_rules import lot_size_for_symbol, paper_only_boundary, round_down_to_lot
from quantdesk.paper.trade_costs import buy_cash_outflow, compute_trade_costs, sell_cash_inflow
from quantdesk.price_basis import PRICE_BASIS_RAW, classify_frame_price_basis
from quantdesk.risk.risk_gate_shadow import (
    _ledger_core,
    evaluate_shadow_risk_order,
    record_shadow_error_best_effort,
    summarize_shadow_records,
    write_shadow_summary,
)
from quantdesk.shared import normalize_symbol, now_text
from quantdesk.utils import atomic_write_json, read_json_safe, safe_float, safe_int
from quantdesk.version import API_SCHEMA_VERSION, APP_VERSION

RESULTS_DIR = PROJECT_ROOT / "results"
DAILY_DIR = PROJECT_ROOT / "data" / "daily"
INTRADAY_DIR = PROJECT_ROOT / "data" / "intraday"
CONDITIONAL_EXECUTION_LATEST_JSON = RESULTS_DIR / "conditional_execution_latest.json"
RISK_GATE_SHADOW_LATEST_JSON = RESULTS_DIR / "risk_gate_shadow_latest.json"
RISK_GATE_SHADOW_ERRORS_JSONL = RESULTS_DIR / "risk_gate_shadow_errors.jsonl"
SECTOR_ROTATION_LATEST_JSON = RESULTS_DIR / "sector_rotation_latest.json"
AUTO_PAPER_PORTFOLIO_STATE_JSON = PROJECT_ROOT / "state" / "auto_paper_portfolio.json"

TRADE_ACTIONS = {"buy", "add", "breakout_add", "reduce_half", "take_profit", "sell_all", "stop_loss"}
# Shadow-risk failures that could not reach their own sink (the shadow record
# list or the error log). Published by the strategy book's latest writer as
# ``shadow_warnings``; never written into the ledger state.
SHADOW_UNRECORDED: list[str] = []
EXIT_ACTIONS = {"reduce_half", "take_profit", "sell_all", "stop_loss"}
BUY_ACTIONS = {"buy", "add", "breakout_add"}

ACTION_LABELS = {
    "buy": "模拟买入",
    "add": "模拟加仓",
    "breakout_add": "模拟突破加仓",
    "hold": "继续持有",
    "reduce_half": "模拟减仓一半",
    "sell_all": "模拟清仓",
    "stop_loss": "模拟止损",
    "take_profit": "模拟止盈",
    "watch_exit": "退出观察",
    "no_fill": "计划未触发",
    "block": "风控阻断",
}


def _ledger_snapshot_bytes(state: dict[str, Any]) -> bytes:
    return (
        json.dumps(
            state,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")


def _shadow_source_binding(role: str, path: Path | None) -> dict[str, Any]:
    if path is None:
        return {"role": role, "path": "", "exists": False, "file_sha256": ""}
    target = Path(path)
    if not target.is_file():
        return {
            "role": role,
            "path": str(target),
            "exists": False,
            "file_sha256": "",
        }
    return {
        "role": role,
        "path": str(target),
        "exists": True,
        "file_sha256": f"sha256:{hashlib.sha256(target.read_bytes()).hexdigest()}",
    }


def _write_shadow_ledger_snapshot_once(
    snapshot_dir: Path,
    state: dict[str, Any],
    *,
    event_ordinal: int,
    symbol: str,
    base_ledger_source_path: Path | None,
    execution_source_path: Path | None,
) -> Path:
    """Persist the exact sequential pre-trade ledger without replacing evidence."""

    snapshot = copy.deepcopy(state)
    snapshot["risk_gate_shadow_ledger_snapshot"] = {
        "schema_version": "risk_gate_shadow_ledger_snapshot_v1",
        "event_ordinal": event_ordinal,
        "symbol": normalize_symbol(symbol),
        "base_ledger_source": _shadow_source_binding(
            "base_ledger", base_ledger_source_path
        ),
        "execution_source": _shadow_source_binding(
            "conditional_execution", execution_source_path
        ),
    }
    content = _ledger_snapshot_bytes(snapshot)
    digest = hashlib.sha256(content).hexdigest()
    safe_symbol = normalize_symbol(symbol) or "unknown"
    target = Path(snapshot_dir) / (
        f"event_{event_ordinal:04d}_{safe_symbol}_{digest[:24]}.json"
    )
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL)
    except FileExistsError:
        if target.read_bytes() != content:
            raise RuntimeError("risk_gate_shadow_ledger_snapshot_conflict") from None
        return target
    completed = False
    try:
        with os.fdopen(descriptor, "wb") as handle:
            descriptor = -1
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        completed = True
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if not completed:
            target.unlink(missing_ok=True)
    if target.read_bytes() != content:
        raise RuntimeError("risk_gate_shadow_ledger_snapshot_verify_failed")
    return target


def _verify_shadow_base_ledger_source(
    source_path: Path,
    expected_state: dict[str, Any],
) -> None:
    if not source_path.is_file():
        raise RuntimeError("risk_gate_shadow_base_ledger_source_missing")
    try:
        source = json.loads(source_path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise RuntimeError(
            f"risk_gate_shadow_base_ledger_source_unreadable:{type(exc).__name__}"
        ) from exc
    if not isinstance(source, dict) or _ledger_core(source) != _ledger_core(expected_state):
        raise RuntimeError("risk_gate_shadow_base_ledger_source_mismatch")


def _verify_shadow_execution_source(
    source_path: Path,
    expected_execution: dict[str, Any],
) -> None:
    if not source_path.is_file():
        raise RuntimeError("risk_gate_shadow_execution_source_missing")
    try:
        source = json.loads(source_path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise RuntimeError(
            f"risk_gate_shadow_execution_source_unreadable:{type(exc).__name__}"
        ) from exc
    if not isinstance(source, dict) or source != expected_execution:
        raise RuntimeError("risk_gate_shadow_execution_source_mismatch")


def _display_path(path: Path) -> str:
    try:
        return str(path.relative_to(PROJECT_ROOT))
    except ValueError:
        return str(path)


def _default_window_snapshot_path(intraday_dir: Path) -> Path | None:
    """Resolve a window snapshot beside an injected intraday project.

    The formal project keeps the loader's ``WINDOW_AUDIT_LATEST_JSON`` default
    (represented by ``None`` here).  A temporary or alternate project must
    instead look under its own ``results/data_health`` tree, so tests and
    callers cannot accidentally inherit the formal repository's window.
    """

    path = Path(intraday_dir).resolve()
    if path.name == "1m" and path.parent.name == "intraday" and path.parent.parent.name == "data":
        project_root = path.parent.parent.parent
    elif path.name == "intraday" and path.parent.name == "data":
        project_root = path.parent.parent
    else:
        project_root = path.parent
    if project_root == PROJECT_ROOT.resolve():
        return None
    return project_root / "results" / "data_health" / "minute_pool_window_audit_latest.json"


def _normalize_intraday_root(intraday_dir: Path) -> Path:
    """Accept either ``data/intraday`` or its ``data/intraday/1m`` child."""

    path = Path(intraday_dir)
    if path.name == "1m" and path.parent.name == "intraday":
        return path.parent
    return path


def _round_price(value: Any) -> float | None:
    number = safe_float(value, None)
    if number is None:
        return None
    return round(number, 4)


def _normalize_intraday_frame(intraday_df: pd.DataFrame | None, trade_date: str = "") -> pd.DataFrame:
    payload = normalize_intraday_schema_frame(intraday_df, target_date=trade_date)
    normalized = payload.get("frame")
    if isinstance(normalized, pd.DataFrame) and not normalized.empty:
        normalized = normalized.copy()
        normalized["_dt"] = pd.to_datetime(normalized["datetime"], errors="coerce")
        return normalized.dropna(subset=["_dt", "close"]).sort_values("_dt").reset_index(drop=True)
    return pd.DataFrame()


def _event_base(plan_item: dict[str, Any]) -> dict[str, Any]:
    event = {
        "plan_id": plan_item.get("plan_id", ""),
        "symbol": normalize_symbol(plan_item.get("symbol")),
        "name": plan_item.get("name") or plan_item.get("symbol"),
        "strategy_type": normalize_strategy_type(plan_item.get("strategy_type"), "long"),
        "strategy_label": strategy_label(plan_item.get("strategy_type")),
        "position_state": plan_item.get("position_state") or "",
        "plan_date": plan_item.get("plan_date") or "",
        "trade_date": plan_item.get("trade_date") or "",
        "settlement_model": "a_share_t1",
        "is_real_order": False,
        "disclaimer": DISCLAIMER,
    }
    envelope = plan_item.get("candidate_envelope")
    if isinstance(envelope, dict):
        event["candidate_envelope"] = copy.deepcopy(envelope)
    return event


def _with_label(event: dict[str, Any]) -> dict[str, Any]:
    event["action_label"] = ACTION_LABELS.get(str(event.get("action") or ""), str(event.get("action") or ""))
    return event


def _resolve_board_lot(*sources: dict[str, Any]) -> int:
    """Resolve the board-aware paper lot for a plan item / position.

    Prefer an explicit ``lot_size`` already computed by the plan builder (e.g.
    STAR/科创板 200), otherwise derive it from the symbol via board rules. This
    keeps take-profit-half and exit rounding board-aware so a 100-share STAR
    half-position rounds down to 0 (hold) instead of being treated as one lot.
    """

    symbol = ""
    for source in sources:
        if not isinstance(source, dict):
            continue
        explicit = source.get("lot_size")
        if explicit:
            try:
                parsed = int(explicit)
            except (TypeError, ValueError):
                parsed = 0
            if parsed > 0:
                return parsed
        symbol = symbol or normalize_symbol(source.get("symbol"))
    return lot_size_for_symbol(symbol) if symbol else 100


def _rule_price(rule: dict[str, Any], low_key: str = "price_low", high_key: str = "price_high") -> tuple[float | None, float | None]:
    return safe_float(rule.get(low_key), None), safe_float(rule.get(high_key), None)


def _in_zone(price: float, rule: dict[str, Any]) -> bool:
    low, high = _rule_price(rule)
    if low is not None and price < low:
        return False
    if high is not None and price > high:
        return False
    return True


def _first_guard(plan_item: dict[str, Any], guard_name: str) -> dict[str, Any] | None:
    rules = plan_item.get("rules") if isinstance(plan_item.get("rules"), dict) else {}
    for guard in rules.get("risk_guards") or []:
        if guard.get("guard") == guard_name:
            return guard
    return None


def _blocked_by_data(plan_item: dict[str, Any]) -> dict[str, Any] | None:
    rules = plan_item.get("rules") if isinstance(plan_item.get("rules"), dict) else {}
    for guard in rules.get("risk_guards") or []:
        if guard.get("effect") == "block" and guard.get("guard") != "no_chase":
            return guard
    return None


def _is_one_word_limit_up_like(plan_item: dict[str, Any], df: pd.DataFrame) -> bool:
    if bool(plan_item.get("is_one_word_limit_up_like")):
        return True
    if not bool(plan_item.get("is_limit_up_like")) or df.empty:
        return False
    columns = [col for col in ("open", "high", "low", "close") if col in df.columns]
    if len(columns) < 4:
        return False
    first = df.iloc[0]
    prices = [safe_float(first.get(col), None) for col in columns]
    return all(price is not None for price in prices) and max(prices) - min(prices) <= 0.01


def _is_one_word_limit_down_like(plan_item: dict[str, Any], df: pd.DataFrame) -> bool:
    if bool(plan_item.get("is_one_word_limit_down_like")):
        return True
    if not bool(plan_item.get("is_limit_down_like")) or df.empty:
        return False
    columns = [col for col in ("open", "high", "low", "close") if col in df.columns]
    if len(columns) < 4:
        return False
    first = df.iloc[0]
    prices = [safe_float(first.get(col), None) for col in columns]
    return all(price is not None for price in prices) and max(prices) - min(prices) <= 0.01


def _event(
    plan_item: dict[str, Any],
    *,
    action: str,
    trigger_type: str,
    trigger_time: str = "",
    trigger_price: Any = None,
    quantity: int = 0,
    fill_model: str = "intraday_1m_close",
    is_reliable: bool = True,
    reason: str = "",
) -> dict[str, Any]:
    return _with_label({
        **_event_base(plan_item),
        "action": action,
        "trigger_type": trigger_type,
        "trigger_time": trigger_time,
        "trigger_price": _round_price(trigger_price),
        "price": _round_price(trigger_price),
        "quantity": int(quantity or 0),
        "fill_model": fill_model,
        "is_reliable": bool(is_reliable),
        "reason": reason,
    })


def _missing_intraday_result(plan_item: dict[str, Any]) -> dict[str, Any]:
    holding = plan_item.get("position_state") in {"holding", "watch_exit"}
    action = "watch_exit" if plan_item.get("position_state") == "watch_exit" else ("hold" if holding else "no_fill")
    trigger_type = "missing_intraday_hold" if holding else "no_fill_missing_intraday"
    reason = "缺少分钟线，无法确认计划价格是否真实出现，系统不会用收盘价假装成交。"
    if holding:
        reason = "缺少分钟线，无法确认加仓、减仓、止盈或止损条件是否真实触发；本次仅保留纸面持仓，不模拟成交。"
    reason = "缺少分钟线，无法确认计划价格是否真实出现，系统不会用收盘价假装成交。"
    if holding:
        reason = "缺少分钟线，无法确认加仓、减仓、止盈或止损条件是否真实触发；本次仅保留纸面持仓，不模拟成交。"
    return _event(
        plan_item,
        action=action,
        trigger_type=trigger_type,
        quantity=0,
        fill_model="missing_intraday",
        is_reliable=False,
        reason=reason,
    )


def _basis_proxy_result(
    plan_item: dict[str, Any],
    *,
    basis: str,
    basis_reason: str,
) -> dict[str, Any]:
    """分钟线口径不是纯 raw 时的结果：只能当同口径研究代理，不算可靠成交价。

    前复权/后复权、口径未知、或同一天混用多种口径时，分钟收盘价与真实成交价不是
    一回事：这里既不产出 stop_loss 之类的可执行动作，也明确标注不是真实执行价
    （T08：复权价、未知口径不能冒充可靠成交价）。
    """

    label = basis or "unknown"
    result = _event(
        plan_item,
        action="block",
        trigger_type="price_basis_gate",
        quantity=0,
        fill_model="price_basis_gate",
        is_reliable=False,
        reason=(
            f"分钟线口径为 {label}，只作同口径研究代理，不作为可靠成交价，本次不模拟成交。"
            + (f"（{basis_reason}）" if basis_reason else "")
        ),
    )
    result["price_basis"] = label
    result["execution_price_label"] = "same_basis_research_proxy"
    result["real_execution_price_claim"] = False
    return result


def _simulate_conditional_plan_raw(
    plan_item: dict[str, Any],
    intraday_df: pd.DataFrame | None,
    *,
    allow_daily_approx: bool = False,
    trigger_model: str = "minute_close",
) -> dict[str, Any]:
    """Replay one frozen plan item against intraday bars.

    The default model only uses minute close prices. It never reads strategy
    candidates or daily features during execution.

    调用方必须是已判过口径的纯 raw 帧；口径判定在 ``simulate_conditional_plan``。
    """

    if trigger_model != "minute_close":
        trigger_model = "minute_close"
    trade_date = str(plan_item.get("trade_date") or "")
    df = _normalize_intraday_frame(intraday_df, trade_date=trade_date)
    if df.empty:
        result = _missing_intraday_result(plan_item)
        if allow_daily_approx:
            result["fill_model"] = "daily_approx"
            result["reason"] += " 日线近似仅供观察，不计入可靠收益统计。"
        return result
    data_guard = _blocked_by_data(plan_item)
    if data_guard:
        return _event(
            plan_item,
            action="block",
            trigger_type=str(data_guard.get("guard") or "data_blocked"),
            quantity=0,
            fill_model="risk_guard",
            is_reliable=False,
            reason=str(data_guard.get("reason") or "数据或流动性风控阻断，不执行条件成交模拟。"),
        )
    rules = plan_item.get("rules") if isinstance(plan_item.get("rules"), dict) else {}
    position_state = str(plan_item.get("position_state") or "")
    holding = position_state in {"holding", "watch_exit"}
    no_chase = safe_float(plan_item.get("no_chase_above"), None)
    no_chase_guard = _first_guard(plan_item, "no_chase")
    one_word_up = _is_one_word_limit_up_like(plan_item, df)
    one_word_down = _is_one_word_limit_down_like(plan_item, df)
    for _, row in df.iterrows():
        price = safe_float(row.get("close"), None)
        if price is None or price <= 0:
            continue
        trigger_time = str(row.get("datetime") or row.get("time") or row.get("_dt") or "")
        if holding:
            for rule in rules.get("exit_rules") or []:
                trigger_type = str(rule.get("trigger_type") or rule.get("action") or "")
                if trigger_type == "stop_loss":
                    _, high = _rule_price(rule)
                    if high is not None and price <= high:
                        if one_word_down:
                            return _event(
                                plan_item,
                                action="block",
                                trigger_type="stop_loss_triggered_but_unfilled",
                                trigger_time=trigger_time,
                                trigger_price=price,
                                quantity=0,
                                fill_model="intraday_1m_close",
                                is_reliable=False,
                                reason="价格处于近似一字跌停，止损计划触发但成交可靠性不足，未按可靠成交处理。",
                            )
                        quantity = safe_int(plan_item.get("sellable_quantity"), 0) or 0
                        return _event(
                            plan_item,
                            action="stop_loss",
                            trigger_type="stop_loss",
                            trigger_time=trigger_time,
                            trigger_price=price,
                            quantity=quantity,
                            reason=str(rule.get("reason") or "跌破关键支撑，模拟止损清仓。"),
                        )
            for rule in rules.get("exit_rules") or []:
                trigger_type = str(rule.get("trigger_type") or rule.get("action") or "")
                if trigger_type == "take_profit_half":
                    low, _ = _rule_price(rule)
                    if low is not None and price >= low:
                        sellable = safe_int(plan_item.get("sellable_quantity"), 0) or 0
                        quantity = round_down_to_lot(sellable * 0.5, lot_size=_resolve_board_lot(plan_item))
                        if quantity <= 0:
                            return _event(
                                plan_item,
                                action="hold",
                                trigger_type="take_profit_hold",
                                trigger_time=trigger_time,
                                trigger_price=price,
                                quantity=0,
                                is_reliable=True,
                                reason="触发止盈观察价，但可卖数量不足以按整手减半，继续纸面持仓。",
                            )
                        return _event(
                            plan_item,
                            action="reduce_half",
                            trigger_type="take_profit_half",
                            trigger_time=trigger_time,
                            trigger_price=price,
                            quantity=quantity,
                            reason=str(rule.get("reason") or "冲高至压力区，模拟减仓一半。"),
                        )
            if no_chase is not None and price > no_chase and rules.get("add_rules"):
                return _event(
                    plan_item,
                    action="block",
                    trigger_type=str((no_chase_guard or {}).get("trigger_type") or "too_high_to_add"),
                    trigger_time=trigger_time,
                    trigger_price=price,
                    quantity=0,
                    fill_model="risk_guard",
                    is_reliable=True,
                    reason=str((no_chase_guard or {}).get("reason") or "价格超过不追高线，不执行加仓。"),
                )
            for rule in rules.get("add_rules") or []:
                if not _in_zone(price, rule):
                    continue
                action = str(rule.get("action") or "add")
                return _event(
                    plan_item,
                    action=action,
                    trigger_type=str(rule.get("trigger_type") or action),
                    trigger_time=trigger_time,
                    trigger_price=price,
                    quantity=safe_int(rule.get("quantity"), 100) or 100,
                    reason=str(rule.get("reason") or "盘中触发持仓管理加仓计划。"),
                )
        else:
            if no_chase is not None and price > no_chase:
                return _event(
                    plan_item,
                    action="block",
                    trigger_type=str((no_chase_guard or {}).get("trigger_type") or "too_high_to_buy"),
                    trigger_time=trigger_time,
                    trigger_price=price,
                    quantity=0,
                    fill_model="risk_guard",
                    is_reliable=True,
                    reason=str((no_chase_guard or {}).get("reason") or "价格超过不追高线，不执行新增买入。"),
                )
            for rule in rules.get("entry_rules") or []:
                trigger_type = str(rule.get("trigger_type") or rule.get("action") or "buy")
                if not _in_zone(price, rule):
                    continue
                if trigger_type == "breakout_buy" and one_word_up:
                    return _event(
                        plan_item,
                        action="no_fill",
                        trigger_type="one_word_limit_up_unfilled",
                        trigger_time=trigger_time,
                        trigger_price=price,
                        quantity=0,
                        fill_model="intraday_1m_close",
                        is_reliable=False,
                        reason="近似一字涨停，无法确认可成交，不模拟可靠买入。",
                    )
                board_lot = _resolve_board_lot(plan_item)
                rule_quantity = safe_int(rule.get("quantity"), 0) or 0
                # A missing or below-board-lot entry quantity must not silently
                # fall back to 100 shares: for STAR/科创板 (lot 200) that would
                # fake a sub-lot fill. Fail closed with a block instead.
                if rule_quantity < board_lot or rule_quantity % board_lot != 0:
                    return _event(
                        plan_item,
                        action="block",
                        trigger_type="below_board_lot_entry_blocked",
                        trigger_time=trigger_time,
                        trigger_price=price,
                        quantity=0,
                        fill_model="risk_guard",
                        is_reliable=False,
                        reason=(
                            f"入场计划数量 {rule_quantity} 不是 {plan_item.get('symbol')} 的整手"
                            f"（最小 {board_lot} 股）；未按 100 股兜底，阻断本次模拟买入。"
                        ),
                    )
                return _event(
                    plan_item,
                    action="buy",
                    trigger_type=trigger_type,
                    trigger_time=trigger_time,
                    trigger_price=price,
                    quantity=rule_quantity,
                    reason=str(rule.get("reason") or "盘中触发入场计划，模拟买入。"),
                )
    if holding:
        action = "watch_exit" if position_state == "watch_exit" else "hold"
        reason = "不再进入今日候选，但尚未触发卖出条件，进入退出观察。" if action == "watch_exit" else "仍在趋势观察区间，未触发加仓、减仓、止盈或止损条件，继续纸面持仓。"
        return _event(
            plan_item,
            action=action,
            trigger_type=action,
            quantity=0,
            is_reliable=True,
            reason=reason,
        )
    return _event(
        plan_item,
        action="no_fill",
        trigger_type="no_fill_not_triggered",
        quantity=0,
        is_reliable=True,
        reason="盘中未触发低吸或突破入场条件，记录计划未触发。",
    )


def simulate_conditional_plan(
    plan_item: dict[str, Any],
    intraday_df: pd.DataFrame | None,
    *,
    allow_daily_approx: bool = False,
    trigger_model: str = "minute_close",
) -> dict[str, Any]:
    """先按**原始帧**判价格口径，再决定它能不能当可靠成交价。

    价格数据契约要求在任何日期过滤、排序、去重或重采样之前先判口径：只有纯 raw 的
    分钟收盘价才进入正常模拟；前复权/后复权、未知、混合口径一律降级为同口径研究代理
    （``is_reliable=False``、不产出可执行动作、明确不是真实执行价，T08）。
    """

    frame = intraday_df if isinstance(intraday_df, pd.DataFrame) else None
    if frame is not None and not frame.empty:
        # 两列口径声明（``adjust`` 与契约规范列 ``price_basis``）**都要检查**：把它们并成
        # 一组 token 交给同一个分类器——两列一致 raw 才算 raw；任一侧写了 qfq/hfq/unknown
        # 或两侧互相矛盾 → 分类即未知/混合 → 拦下。列顺序、只有哪一列都不改变判断
        # （T08-BASIS-ALIAS-CONFLICT-20260930）。
        tokens = [
            value
            for column in ("adjust", "price_basis")
            if column in frame.columns
            for value in frame[column].tolist()
        ]
        basis_frame = pd.DataFrame({"adjust": tokens}) if tokens else frame
        if basis_frame is not frame:
            # 重建分类帧只为把两列列证据合并进来；原帧**已有的负面预检状态**（
            # ``price_basis_reason`` / ``observed_price_basis``）必须一起带上，否则
            # unknown/mixed 的预检错误会被"只剩 raw 列"冲掉而放行
            # （T08-PREFLIGHT-ATTRS-REGRESSION-20260930）。
            basis_frame.attrs = dict(frame.attrs)
        basis, basis_reason = classify_frame_price_basis(basis_frame)
        # 只有"分出来是 raw **且**没有任何未知/混合的错误理由"才进入模拟；raw 标签绝不覆盖
        # 错误理由，未声明口径（无列、无理由）同样拦。
        if basis != PRICE_BASIS_RAW or basis_reason:
            return _basis_proxy_result(plan_item, basis=basis, basis_reason=basis_reason)
        if "adjust" in frame.columns and "price_basis" in frame.columns:
            # 两列一致 raw：共享归一化器会把两列都映到同一个规范名（重名列会崩），这里先
            # 去掉别名列，只留 ``adjust``（它会被归一化成规范列）。判定已经在上一步做完。
            frame = frame.drop(columns=["price_basis"])
    result = _simulate_conditional_plan_raw(
        plan_item,
        frame if frame is not None else intraday_df,
        allow_daily_approx=allow_daily_approx,
        trigger_model=trigger_model,
    )
    if isinstance(result, dict) and frame is not None and not frame.empty:
        result.setdefault("price_basis", PRICE_BASIS_RAW)
        result.setdefault("execution_price_label", "raw_minute_close_paper_sim")
        result.setdefault("real_execution_price_claim", False)
    return result


def _read_intraday(symbol: str, intraday_dir: Path, source: str = "1m") -> pd.DataFrame | None:
    from quantdesk.intraday.minute_storage import resolve_minute_read_path

    path = intraday_dir / source / f"{normalize_symbol(symbol)}.csv"
    path = resolve_minute_read_path(path)
    if not path.exists():
        return None
    frame = read_csv_frame(path, dtype={"symbol": str})
    return frame if not frame.empty else None


def evaluate_position_risk_overlays(
    positions: list[dict],
    config: dict | None = None,
) -> dict:
    """Build research-only risk overlays without mutating paper positions."""
    try:
        from quantdesk.risk.smart_stop import build_smart_stop_for_position
    except Exception as exc:  # noqa: BLE001
        return {"ok": True, "status": "warning", "overlays": {}, "warnings": [f"smart_stop_unavailable:{exc}"], "research_only": True}
    overlays: dict[str, dict] = {}
    warnings: list[str] = []
    for row in positions:
        if not isinstance(row, dict):
            continue
        symbol = normalize_symbol(row.get("symbol"))
        if not symbol:
            continue
        item = build_smart_stop_for_position(row, config=config)
        warnings.extend(str(w) for w in item.get("warnings") or [])
        overlays[symbol] = {
            "smart_stop_status": item.get("trigger_status") or "unknown",
            "risk_alert_count": 1 if item.get("trigger_status") in {"watch", "triggered"} else 0,
            "critical_alert_count": 1 if item.get("trigger_status") == "triggered" else 0,
            "latest_risk_alerts_json": "results/risk_alerts_latest.json",
            "research_only": True,
        }
    return {"ok": True, "status": "warning" if warnings else "passed", "overlays": overlays, "warnings": list(dict.fromkeys(warnings)), "research_only": True}


def attach_risk_overlays_to_conditional_plan(plan_payload: dict, risk_payload: dict) -> dict:
    plan = dict(plan_payload or {})
    overlays = risk_payload.get("overlays") if isinstance(risk_payload.get("overlays"), dict) else {}
    symbols = []
    for row in plan.get("symbols") or []:
        if not isinstance(row, dict):
            symbols.append(row)
            continue
        symbol = normalize_symbol(row.get("symbol"))
        overlay = overlays.get(symbol, {"smart_stop_status": "unknown", "risk_alert_count": 0, "critical_alert_count": 0, "latest_risk_alerts_json": "results/risk_alerts_latest.json", "research_only": True})
        symbols.append({**row, "risk_overlay": overlay})
    plan["symbols"] = symbols
    plan["risk_overlay"] = {"status": risk_payload.get("status") or "unknown", "research_only": True}
    return plan


def simulate_conditional_plan_file(
    plan_payload: dict[str, Any] | None = None,
    *,
    plan_path: Path | None = None,
    intraday_dir: Path | None = None,
    window_snapshot_path: Path | None = None,
    output_path: Path | None = None,
    intraday_source: str = "1m",
    allow_daily_approx: bool = False,
    trigger_model: str = "minute_close",
    include_risk_overlays: bool = True,
    write: bool = True,
) -> dict[str, Any]:
    plan_path = Path(plan_path or CONDITIONAL_TRADE_PLANS_LATEST_JSON)
    intraday_dir = _normalize_intraday_root(Path(intraday_dir or INTRADAY_DIR))
    output_path = Path(output_path or CONDITIONAL_EXECUTION_LATEST_JSON)
    plan = (
        plan_payload
        if isinstance(plan_payload, dict)
        else conditional_trade_plan_latest(plan_path)
    )
    if not isinstance(plan, dict) or not plan or plan.get("exists") is False:
        return {"ok": False, "error": "missing_plan", "message": "尚未生成条件交易计划。", **paper_only_boundary(), "disclaimer": DISCLAIMER}
    validation = plan.get("plan_validation")
    validation = validation if isinstance(validation, dict) else {}
    if str(validation.get("status") or "") == "blocked":
        reason_codes = [str(item) for item in (validation.get("reason_codes") or [])]
        blocked_payload = {
            "ok": False,
            "status": "blocked",
            "error": "invalid_frozen_plan",
            "message": "条件计划冻结证据无效；保留旧文件用于审计，但不执行分钟回放。",
            "plan_id": str(plan.get("plan_id") or ""),
            "plan_date": plan.get("plan_date"),
            "trade_date": plan.get("trade_date"),
            "events": [],
            "no_fill": [],
            "blocked": [],
            "missing_intraday": [],
            "reason_codes": reason_codes,
            "plan_validation": validation,
            "summary": {
                "plan_count": len(
                    [row for row in (plan.get("symbols") or []) if isinstance(row, dict)]
                ),
                "event_count": 0,
                "no_fill_count": 0,
                "blocked_count": 0,
                "missing_intraday_count": 0,
            },
            **paper_only_boundary(),
            "disclaimer": DISCLAIMER,
        }
        if write:
            atomic_write_json(output_path, blocked_payload)
            blocked_payload["path"] = _display_path(output_path)
        return blocked_payload
    if include_risk_overlays:
        plan = attach_risk_overlays_to_conditional_plan(plan, evaluate_position_risk_overlays(list(plan.get("symbols") or [])))
    events: list[dict[str, Any]] = []
    no_fill: list[dict[str, Any]] = []
    blocked: list[dict[str, Any]] = []
    warnings: list[str] = []
    missing_intraday: list[dict[str, Any]] = []
    plan_id = str(plan.get("plan_id") or "")
    minute_pool = (
        load_pool_for_intraday_dir(intraday_dir)
        if str(intraday_source or "1m") == "1m"
        else {"status": "not_applicable_for_non_1m_source", "passed": False, "symbols": set()}
    )
    window_pool = (
        load_window_verified_minute_pool(
            snapshot_path=(
                Path(window_snapshot_path)
                if window_snapshot_path is not None
                else _default_window_snapshot_path(intraday_dir)
            )
        )
        if str(intraday_source or "1m") == "1m"
        else {"status": "not_applicable_for_non_1m_source", "snapshot_valid": False}
    )
    minute_evidence_items: list[dict[str, Any]] = []
    for item in plan.get("symbols") or []:
        if not isinstance(item, dict):
            continue
        item = {**item, "plan_id": plan_id}
        intraday_df = _read_intraday(item.get("symbol"), intraday_dir=intraday_dir, source=intraday_source)
        if isinstance(intraday_df, pd.DataFrame):
            for warning in intraday_df.attrs.get("warnings", []):
                warnings.append(f"{normalize_symbol(item.get('symbol'))}: {warning}")
        result = simulate_conditional_plan(
            item,
            intraday_df,
            allow_daily_approx=allow_daily_approx,
            trigger_model=trigger_model,
        )
        trade_date = str(
            result.get("trade_date")
            or item.get("trade_date")
            or plan.get("trade_date")
            or ""
        )[:10]
        resolved_evidence = resolve_minute_replay_evidence(
            result.get("symbol") or item.get("symbol"),
            fill_model=str(result.get("fill_model") or ""),
            trade_date=trade_date,
            window_pool=window_pool,
            symbol_pool=minute_pool,
        )
        minute_evidence = resolved_evidence["evidence"]
        result["intraday_quality_gate"] = minute_evidence["status"]
        result["intraday_evidence"] = minute_evidence
        result["intraday_pool_provenance"] = {
            "pool_source_snapshot": resolved_evidence["pool_source_snapshot"],
            "window_start": resolved_evidence["window_start"],
            "window_end": resolved_evidence["window_end"],
            "decision_basis": resolved_evidence["decision_basis"],
        }
        minute_evidence_items.append(minute_evidence)
        action = str(result.get("action") or "")
        if action in TRADE_ACTIONS and result.get("is_reliable"):
            events.append(result)
        elif action == "block":
            blocked.append(result)
        else:
            no_fill.append(result)
        if result.get("fill_model") == "missing_intraday":
            missing_intraday.append(result)
        if result.get("is_reliable") is False:
            warnings.append(f"{result.get('symbol')}: {result.get('reason')}")
    all_results = events + no_fill + blocked
    counts = Counter(str(row.get("action") or "") for row in all_results)
    minute_capability_summary = summarize_minute_replay_capability(minute_evidence_items, minute_pool)
    available_intraday_count = len({
        str(row.get("symbol") or "")
        for row in all_results
        if row.get("fill_model") == "intraday_1m_close"
    })
    payload = {
        "ok": True,
        "schema_version": API_SCHEMA_VERSION,
        "api_schema_version": API_SCHEMA_VERSION,
        "app_version": APP_VERSION,
        "plan_id": plan_id,
        "plan_date": plan.get("plan_date"),
        "trade_date": plan.get("trade_date"),
        "execution_mode": "intraday_1m",
        "trigger_model": "minute_close",
        "allow_daily_approx": bool(allow_daily_approx),
        "is_real_order": False,
        "generated_at": now_text(),
        "events": events,
        "no_fill": no_fill,
        "blocked": blocked,
        "missing_intraday": missing_intraday,
        "warnings": warnings,
        "summary": {
            "plan_count": len([row for row in plan.get("symbols") or [] if isinstance(row, dict)]),
            "event_count": len(events),
            "reliable_event_count": sum(1 for row in events if row.get("is_reliable")),
            "no_fill_count": len(no_fill),
            "blocked_count": len(blocked),
            "missing_intraday_count": len(missing_intraday),
            "available_intraday_count": available_intraday_count,
            "verified_completed_intraday_count": minute_capability_summary["verified_completed_intraday_count"],
            "local_unverified_intraday_count": minute_capability_summary["local_unverified_intraday_count"],
            "intraday_quality_gate": minute_capability_summary,
            "action_counts": dict(counts),
        },
        "risk_overlay": plan.get("risk_overlay", {"research_only": True}),
        **paper_only_boundary(),
        "disclaimer": DISCLAIMER,
    }
    if write:
        atomic_write_json(output_path, payload)
        payload["path"] = _display_path(output_path)
    return payload


# Round 21 mark-to-market basis gate: a daily close may enter valuation only
# when the WHOLE original frame is classified as raw before any date filtering.
# Non-raw or mixed frames degrade the day's valuation (previous qualified value
# is kept by the caller) instead of silently mixing price bases.
VALUATION_GATE_VERSION = "round21_raw_close_gate_v1"


def _apply_valuation_gate_marks(position: dict[str, Any], valuation_warnings: list[str]) -> None:
    """Mark a position as valuation-degraded when the basis gate blocked its close."""

    if any("valuation_basis_gate_blocked" in str(item) for item in valuation_warnings):
        position["valuation_degraded"] = True
        position["valuation_degraded_reason"] = "daily_close_price_basis_not_raw"
        position["valuation_gate_version"] = VALUATION_GATE_VERSION


def _read_daily_close(symbol: str, trade_date: str, daily_dir: Path = DAILY_DIR) -> tuple[float | None, str, list[str]]:
    path = daily_dir / f"{normalize_symbol(symbol)}.csv"
    if not path.exists():
        return None, "", []
    df = read_csv_frame(path)
    warnings = [str(item) for item in df.attrs.get("warnings", [])]
    basis, basis_reason = classify_frame_price_basis(df)
    if basis != PRICE_BASIS_RAW or basis_reason:
        warnings.append(
            "valuation_basis_gate_blocked:"
            f"{normalize_symbol(symbol)}:basis={basis}:reason={basis_reason or 'non_raw'}"
        )
        return None, "", warnings
    rename = {"日期": "date", "收盘": "close", "收盘价": "close"}
    df = df.rename(columns={key: value for key, value in rename.items() if key in df.columns and value not in df.columns})
    if "date" not in df.columns or "close" not in df.columns:
        return None, "", warnings
    df["date"] = pd.to_datetime(df["date"], errors="coerce")
    df["close"] = pd.to_numeric(df["close"], errors="coerce")
    cutoff = pd.to_datetime(trade_date, errors="coerce")
    if pd.notna(cutoff):
        df = df[df["date"] <= cutoff]
    df = df.dropna(subset=["date", "close"]).sort_values("date")
    if df.empty:
        return None, "", warnings
    row = df.iloc[-1]
    return safe_float(row.get("close"), None), pd.Timestamp(row.get("date")).strftime("%Y-%m-%d"), warnings


def _position_key(row: dict[str, Any]) -> tuple[str, str]:
    return normalize_symbol(row.get("symbol")), str(row.get("strategy_type") or "long")


def normalize_position_for_t1(position: dict[str, Any], trade_date: str) -> dict[str, Any]:
    """Keep documented old lots sellable after an add; never guess missing lots."""
    row = dict(position)
    quantity = safe_int(row.get("quantity"), 0) or 0

    def day(value: Any) -> Any:
        parsed = pd.to_datetime(str(value or "")[:10], errors="coerce")
        return None if pd.isna(parsed) else pd.Timestamp(parsed).date()

    def shares(value: Any) -> int | None:
        number = safe_float(value, None)
        if isinstance(value, bool) or number is None or not math.isfinite(number) or number < 0 or number != int(number):
            return None
        return int(number)

    entry_day = day(row.get("entry_date") or row.get("buy_date") or row.get("first_buy_date"))
    buy_day = day(row.get("last_buy_date")) if row.get("last_buy_date") else entry_day
    trade_day = day(trade_date)
    sellable, frozen = 0, quantity
    status, reason = "unverified", "missing_or_invalid_buy_date"
    if buy_day is not None and trade_day is not None:
        if entry_day is not None and entry_day > buy_day:
            reason = "inconsistent_buy_dates"
        elif trade_day > buy_day:
            sellable, frozen = quantity, 0
            status, reason = "verified_sellable", "buy_date_before_execution"
        elif trade_day < buy_day:
            reason = "execution_before_last_buy"
        elif entry_day == trade_day:
            status, reason = "frozen_today", "new_position_today"
        elif entry_day is not None and entry_day < trade_day:
            old_lot, new_lot = shares(row.get("sellable_quantity")), shares(row.get("frozen_quantity"))
            if old_lot is not None and new_lot is not None and new_lot > 0 and old_lot + new_lot == quantity:
                sellable, frozen = old_lot, new_lot
                status, reason = "partial_frozen_today" if old_lot else "frozen_today", "recorded_same_day_lots"
            else:
                reason = "same_day_lot_balances_missing_or_inconsistent"
    row["quantity"] = quantity
    row["sellable_quantity"] = max(0, min(quantity, sellable))
    row["frozen_quantity"] = max(0, min(quantity, frozen))
    row["t1_status"] = status
    row["t1_reason"] = reason
    row["avg_cost"] = safe_float(row.get("avg_cost"), safe_float(row.get("entry_price"), safe_float(row.get("latest_price"), 0.0))) or 0.0
    row.setdefault("management_state", "normal")
    return row


def _make_action(result: dict[str, Any], cash_after: float | None = None) -> dict[str, Any]:
    action = {
        "action": result.get("action"),
        "action_label": result.get("action_label") or ACTION_LABELS.get(str(result.get("action") or ""), ""),
        "symbol": normalize_symbol(result.get("symbol")),
        "name": result.get("name") or result.get("symbol"),
        "strategy_type": result.get("strategy_type") or "",
        "quantity": safe_int(result.get("quantity"), 0) or 0,
        "price": result.get("trigger_price") or result.get("price"),
        "decision_date": result.get("trade_date") or "",
        "trigger_time": result.get("trigger_time") or "",
        "trigger_price": result.get("trigger_price"),
        "trigger_type": result.get("trigger_type") or "",
        "fill_model": result.get("fill_model") or "",
        "is_reliable": bool(result.get("is_reliable")),
        "reason": result.get("reason") or "",
        "plan_id": result.get("plan_id") or "",
        "settlement_model": result.get("settlement_model") or "a_share_t1",
        "order_source": "conditional_position_management",
        **paper_only_boundary(),
    }
    for key in (
        "gross_amount",
        "commission",
        "stamp_duty",
        "transfer_fee",
        "slippage",
        "total_fee",
        "cash_in",
        "cash_out",
        "net_amount",
        "fee_model",
        "t_plus_one_sell_blocked",
    ):
        if key in result:
            action[key] = result.get(key)
    if cash_after is not None:
        action["cash_after"] = round(cash_after, 2)
    price = safe_float(action.get("price"), None)
    if price is not None and action["quantity"]:
        action["amount"] = round(price * action["quantity"], 2)
    return action


def _new_position_from_event(result: dict[str, Any], cost_info: dict[str, Any] | None = None) -> dict[str, Any]:
    price = safe_float(result.get("trigger_price"), 0.0) or 0.0
    quantity = safe_int(result.get("quantity"), 0) or 0
    cost = dict(cost_info or buy_cash_outflow(price=price, quantity=quantity))
    gross = safe_float(cost.get("gross"), price * quantity) or price * quantity
    total_fee = safe_float(cost.get("total_fee"), 0.0) or 0.0
    cash_out = safe_float(cost.get("cash_out"), gross + total_fee) or (gross + total_fee)
    cost_basis = safe_float(cost.get("cost_basis"), cash_out) or cash_out
    avg_cost = cost_basis / quantity if quantity else price
    position = {
        "symbol": normalize_symbol(result.get("symbol")),
        "name": result.get("name") or result.get("symbol"),
        "strategy_type": normalize_strategy_type(result.get("strategy_type"), "long"),
        "strategy_label": strategy_label(result.get("strategy_type")),
        "quantity": quantity,
        "sellable_quantity": 0,
        "frozen_quantity": quantity,
        "avg_cost": round(avg_cost, 4),
        "entry_price": round(avg_cost, 4),
        "execution_price": round(price, 4),
        "entry_gross_amount": round(gross, 2),
        "entry_total_fee": round(total_fee, 2),
        "entry_cash_out": round(cash_out, 2),
        "latest_price": round(price, 4),
        "latest_date": result.get("trade_date") or "",
        "entry_date": result.get("trade_date") or "",
        "last_buy_date": result.get("trade_date") or "",
        "last_action_date": result.get("trade_date") or "",
        "management_state": "normal",
        "market_value": round(price * quantity, 2),
        "unrealized_pnl": round((price - avg_cost) * quantity, 2),
        "unrealized_return_pct": round((price / avg_cost - 1.0) * 100.0, 2) if avg_cost else 0.0,
        "return_pct": round((price / avg_cost - 1.0) * 100.0, 2) if avg_cost else 0.0,
        "entry_reason": result.get("reason") or "条件交易计划盘中触发，模拟买入。",
        "trigger_time": result.get("trigger_time"),
        "trigger_price": result.get("trigger_price"),
        "trigger_type": result.get("trigger_type"),
        "order_source": "conditional_position_management",
        **paper_only_boundary(),
    }
    envelope = result.get("candidate_envelope")
    if isinstance(envelope, dict):
        position["candidate_envelope"] = copy.deepcopy(envelope)
        if not verify_candidate_envelope(envelope):
            candidate = envelope.get("candidate")
            if isinstance(candidate, dict):
                for field in ("sector", "industry"):
                    value = str(candidate.get(field) or "").strip()
                    if value:
                        position[field] = value
    return position


def _close_record(
    position: dict[str, Any],
    result: dict[str, Any],
    quantity: int,
    price: float,
    cash_info: dict[str, Any] | None = None,
    costs: dict[str, Any] | None = None,
) -> dict[str, Any]:
    avg_cost = safe_float(position.get("avg_cost"), safe_float(position.get("entry_price"), price)) or price
    cash_payload = dict(cash_info or sell_cash_inflow(price=price, quantity=quantity, avg_cost=avg_cost))
    cost_payload = dict(costs or compute_trade_costs(side="sell", gross=price * quantity))
    pnl = safe_float(cash_payload.get("realized_pnl"), (price - avg_cost) * quantity) or 0.0
    return {
        **position,
        "exit_date": result.get("trade_date") or "",
        "exit_price": round(price, 4),
        "exit_quantity": quantity,
        "gross_amount": round(safe_float(cash_payload.get("gross"), price * quantity) or price * quantity, 2),
        "commission": round(safe_float(cost_payload.get("commission"), 0.0) or 0.0, 2),
        "stamp_duty": round(safe_float(cost_payload.get("stamp_duty"), 0.0) or 0.0, 2),
        "transfer_fee": round(safe_float(cost_payload.get("transfer_fee"), 0.0) or 0.0, 2),
        "slippage": round(safe_float(cost_payload.get("slippage"), 0.0) or 0.0, 2),
        "total_fee": round(safe_float(cost_payload.get("total_fee"), 0.0) or 0.0, 2),
        "net_amount": round(safe_float(cash_payload.get("cash_in"), price * quantity) or price * quantity, 2),
        "fee_model": str(cost_payload.get("fee_model") or ""),
        "cost_profile": str(cost_payload.get("cost_profile") or "legacy"),
        "realized_pnl": round(pnl, 2),
        "return_pct": round((price / avg_cost - 1.0) * 100.0, 2) if avg_cost else 0.0,
        "exit_reason": result.get("reason") or "",
        "status": "closed",
        "management_state": "closed",
    }


def apply_conditional_execution_to_state(
    state: dict[str, Any],
    execution_payload: dict[str, Any],
    *,
    daily_dir: Path = DAILY_DIR,
    allow_same_day_rerun: bool = False,
    adjustment_factors: AdjustmentFactorTable | None = None,
    risk_gate_shadow_enabled: bool = False,
    risk_gate_enforced: bool = False,
    risk_gate_shadow_records: list[dict[str, Any]] | None = None,
    risk_gate_shadow_output_path: Path | None = None,
    risk_gate_shadow_error_path: Path | None = None,
    risk_gate_shadow_write_errors: bool = True,
    sector_rotation_path: Path | None = None,
    ledger_source_path: Path | None = None,
    execution_source_path: Path | None = None,
    risk_gate_shadow_ledger_snapshot_dir: Path | None = None,
) -> dict[str, Any]:
    portfolio = dict(state or {})
    base_portfolio = copy.deepcopy(portfolio)
    trade_date = str(execution_payload.get("trade_date") or "")[:10]
    plan_id = str(execution_payload.get("plan_id") or "")
    execution_id = f"{plan_id}:{trade_date}"
    applied = list(portfolio.get("applied_execution_ids") or [])
    if execution_id in applied and not allow_same_day_rerun:
        positions = [dict(row) for row in portfolio.get("positions") or [] if isinstance(row, dict)]
        position_by_key = {_position_key(row): row for row in positions}
        for result in list(execution_payload.get("events") or []) + list(execution_payload.get("no_fill") or []) + list(execution_payload.get("blocked") or []):
            if not isinstance(result, dict):
                continue
            position = position_by_key.get(_position_key(result))
            if not position:
                continue
            action = str(result.get("action") or "")
            reason = str(result.get("reason") or "")
            if action == "watch_exit":
                position["management_state"] = "watch_exit"
                position["hold_reason"] = reason or "不再进入今日候选，但尚未触发卖出条件，进入退出观察。"
            elif action == "hold":
                position["hold_reason"] = reason or "已有持仓已生成条件交易计划。本日未触发加仓、减仓、止盈或止损条件，继续纸面持仓。"
            elif action == "block":
                position["management_state"] = "risk_alert"
                position["hold_reason"] = reason or "条件计划被风控阻断，本次不模拟成交。"
        portfolio["positions"] = positions
        return {
            "ok": True,
            "already_updated_today": True,
            "state": portfolio,
            "actions": [],
            "message": "同一交易日的条件执行结果已应用，默认不重复追加历史。",
            "is_paper_simulation": True,
            **paper_only_boundary(),
        }
    positions = [normalize_position_for_t1(row, trade_date) for row in portfolio.get("positions") or [] if isinstance(row, dict)]
    closed_positions = list(portfolio.get("closed_positions") or [])
    realized_reductions = [dict(row) for row in portfolio.get("realized_reductions") or [] if isinstance(row, dict)]
    cash = safe_float(portfolio.get("cash"), safe_float(portfolio.get("initial_cash"), 0.0)) or 0.0
    position_by_key = {_position_key(row): row for row in positions}
    actions: list[dict[str, Any]] = []
    realized_today = 0.0
    shadow_records = risk_gate_shadow_records if risk_gate_shadow_records is not None else []
    owns_shadow_records = risk_gate_shadow_records is None

    def record_shadow_failure(surface: str, error: BaseException) -> None:
        record = {
            "evaluated": False,
            "status": "shadow_error",
            "surface": surface,
            "as_of": trade_date,
            "reason_codes": ["shadow_error"],
            "shadow_error": f"{type(error).__name__}:{error}",
            "would_pass": False,
            "would_block": False,
            "insufficient_evidence": False,
            "missing_components": [],
        }
        try:
            shadow_records.append(record)
        except Exception as append_exc:  # noqa: BLE001 - surfaced via SHADOW_UNRECORDED
            SHADOW_UNRECORDED.append(
                f"shadow_record_append_failed:{surface}:{type(error).__name__}:{type(append_exc).__name__}"
            )
        if risk_gate_shadow_write_errors:
            try:
                record_shadow_error_best_effort(
                    surface=surface,
                    error=error,
                    as_of=trade_date,
                    error_path=Path(
                        risk_gate_shadow_error_path
                        or (RESULTS_DIR / "risk_gate_shadow_errors.jsonl")
                    ),
                )
            except Exception as log_exc:  # noqa: BLE001 - surfaced, not swallowed
                record["shadow_error_log_failed"] = f"{type(log_exc).__name__}:{log_exc}"
                SHADOW_UNRECORDED.append(f"shadow_error_log_write_failed:{surface}:{type(log_exc).__name__}")
    incoming_results = list(execution_payload.get("events") or []) + list(execution_payload.get("no_fill") or []) + list(execution_payload.get("blocked") or [])
    ordered_results = sorted(
        enumerate(incoming_results),
        key=lambda item: (str(item[1].get("trigger_time") or "") if isinstance(item[1], dict) else "", item[0]),
    )
    for event_ordinal, (_, result) in enumerate(ordered_results, start=1):
        if not isinstance(result, dict):
            continue
        action = str(result.get("action") or "")
        key = _position_key(result)
        position = position_by_key.get(key)
        price = safe_float(result.get("trigger_price"), None)
        quantity = safe_int(result.get("quantity"), 0) or 0
        if action in BUY_ACTIONS and result.get("is_reliable") and price and quantity > 0:
            cost_info = buy_cash_outflow(price=price, quantity=quantity)
            cash_out = safe_float(cost_info.get("cash_out"), price * quantity) or price * quantity
            if cash_out <= cash:
                if risk_gate_shadow_enabled:
                    try:
                        envelope = result.get("candidate_envelope")
                        envelope_reasons = verify_candidate_envelope(
                            envelope if isinstance(envelope, dict) else None
                        )
                        if envelope_reasons:
                            shadow_records.append(
                                {
                                    "schema_version": API_SCHEMA_VERSION,
                                    "task_type": "risk_gate_shadow_evaluation",
                                    "surface": "conditional_execution_buy",
                                    "evaluated": True,
                                    "as_of": trade_date,
                                    "symbol": normalize_symbol(result.get("symbol")),
                                    "side": "buy",
                                    "status": "insufficient_evidence",
                                    "would_pass": False,
                                    "would_block": False,
                                    "insufficient_evidence": True,
                                    "reason_codes": envelope_reasons,
                                    "missing_components": ["candidate_envelope"],
                                    "missing_component_count": 1,
                                    "missing_component_reason_codes": {
                                        "candidate_envelope": envelope_reasons
                                    },
                                    "risk_gate_enforced": bool(risk_gate_enforced),
                                    "enforcement_applied": False,
                                    "shadow_only": True,
                                    "research_only": True,
                                    "paper_only": True,
                                    "no_broker": True,
                                    "is_real_order": False,
                                }
                            )
                        else:
                            envelope_candidate = envelope.get("candidate")
                            envelope_source = envelope.get("batch_path")
                            shadow_state = {
                                **portfolio,
                                "cash": cash,
                                "positions": copy.deepcopy(positions),
                            }
                            shadow_ledger_source = Path(
                                ledger_source_path
                                or AUTO_PAPER_PORTFOLIO_STATE_JSON
                            )
                            if risk_gate_shadow_ledger_snapshot_dir is not None:
                                _verify_shadow_base_ledger_source(
                                    Path(
                                        ledger_source_path
                                        or AUTO_PAPER_PORTFOLIO_STATE_JSON
                                    ),
                                    base_portfolio,
                                )
                                if execution_source_path is not None:
                                    _verify_shadow_execution_source(
                                        Path(execution_source_path),
                                        execution_payload,
                                    )
                                shadow_ledger_source = _write_shadow_ledger_snapshot_once(
                                    Path(risk_gate_shadow_ledger_snapshot_dir),
                                    shadow_state,
                                    event_ordinal=event_ordinal,
                                    symbol=normalize_symbol(result.get("symbol")),
                                    base_ledger_source_path=Path(
                                        ledger_source_path
                                        or AUTO_PAPER_PORTFOLIO_STATE_JSON
                                    ),
                                    execution_source_path=(
                                        Path(execution_source_path)
                                        if execution_source_path is not None
                                        else None
                                    ),
                                )
                            shadow_records.append(
                                evaluate_shadow_risk_order(
                                    state=shadow_state,
                                    candidate=envelope_candidate,
                                    side="buy",
                                    reference_price=price,
                                    requested_quantity=quantity,
                                    as_of=trade_date,
                                    daily_dir=daily_dir,
                                    candidate_source_path=Path(envelope_source),
                                    ledger_source_path=shadow_ledger_source,
                                    sector_rotation_path=Path(
                                        sector_rotation_path
                                        or SECTOR_ROTATION_LATEST_JSON
                                    ),
                                    price_source_path=Path(
                                        execution_source_path
                                        or CONDITIONAL_EXECUTION_LATEST_JSON
                                    ),
                                    price_data_cutoff=trade_date,
                                    risk_gate_enforced=risk_gate_enforced,
                                    surface="conditional_execution_buy",
                                )
                            )
                    except Exception as exc:
                        record_shadow_failure("conditional_execution_buy", exc)
                cash -= cash_out
                if position:
                    old_qty = safe_int(position.get("quantity"), 0) or 0
                    old_cost = safe_float(position.get("avg_cost"), safe_float(position.get("entry_price"), price)) or price
                    new_qty = old_qty + quantity
                    position["avg_cost"] = round(((old_cost * old_qty) + (safe_float(cost_info.get("cost_basis"), cash_out) or cash_out)) / new_qty, 4) if new_qty else old_cost
                    position["quantity"] = new_qty
                    position["frozen_quantity"] = (safe_int(position.get("frozen_quantity"), 0) or 0) + quantity
                    position["sellable_quantity"] = safe_int(position.get("sellable_quantity"), 0) or 0
                    position["last_buy_date"] = trade_date
                    position["last_action_date"] = trade_date
                    position["latest_price"] = round(price, 4)
                    position["latest_date"] = trade_date
                    position["last_trade_fee"] = round(safe_float(cost_info.get("total_fee"), 0.0) or 0.0, 2)
                    position["management_state"] = "normal"
                else:
                    position = _new_position_from_event(result, cost_info)
                    position_by_key[key] = position
                    positions.append(position)
                buy_costs = compute_trade_costs(side="buy", gross=price * quantity)
                result_for_action = {
                    **result,
                    "gross_amount": round(safe_float(cost_info.get("gross"), price * quantity) or price * quantity, 2),
                    "commission": round(safe_float(buy_costs.get("commission"), 0.0) or 0.0, 2),
                    "stamp_duty": round(safe_float(buy_costs.get("stamp_duty"), 0.0) or 0.0, 2),
                    "transfer_fee": round(safe_float(buy_costs.get("transfer_fee"), 0.0) or 0.0, 2),
                    "slippage": round(safe_float(buy_costs.get("slippage"), 0.0) or 0.0, 2),
                    "total_fee": round(safe_float(cost_info.get("total_fee"), 0.0) or 0.0, 2),
                    "cash_out": round(cash_out, 2),
                    "fee_model": str(buy_costs.get("fee_model") or ""),
                    "cost_profile": str(buy_costs.get("cost_profile") or "legacy"),
                }
                actions.append(_make_action(result_for_action, cash_after=cash))
            else:
                blocked = {**result, "action": "block", "action_label": ACTION_LABELS["block"], "reason": "现金不足，条件计划触发但未模拟买入或加仓。", "is_reliable": False}
                actions.append(_make_action(blocked, cash_after=cash))
        elif action in EXIT_ACTIONS and result.get("is_reliable") and position and price:
            sellable = safe_int(position.get("sellable_quantity"), 0) or 0
            sell_qty = min(sellable, quantity if quantity > 0 else sellable)
            sell_qty = round_down_to_lot(sell_qty, lot_size=_resolve_board_lot(position, result))
            if sell_qty <= 0:
                blocked = {**result, "action": "block", "action_label": ACTION_LABELS["block"], "quantity": 0, "reason": "A 股 T+1 约束下当前无可卖数量，未模拟卖出。", "is_reliable": False, "t_plus_one_sell_blocked": True}
                actions.append(_make_action(blocked, cash_after=cash))
                continue
            avg_cost = safe_float(position.get("avg_cost"), safe_float(position.get("entry_price"), price)) or price
            cash_info = sell_cash_inflow(price=price, quantity=sell_qty, avg_cost=avg_cost)
            sell_costs = compute_trade_costs(side="sell", gross=price * sell_qty)
            cash_in = safe_float(cash_info.get("cash_in"), price * sell_qty) or price * sell_qty
            cash += cash_in
            pnl = safe_float(cash_info.get("realized_pnl"), (price - avg_cost) * sell_qty) or 0.0
            realized_today += pnl
            position["quantity"] = (safe_int(position.get("quantity"), 0) or 0) - sell_qty
            position["sellable_quantity"] = max(0, (safe_int(position.get("sellable_quantity"), 0) or 0) - sell_qty)
            position["last_action_date"] = trade_date
            position["latest_price"] = round(price, 4)
            position["latest_date"] = trade_date
            if action in {"sell_all", "stop_loss"} or position["quantity"] <= 0:
                position["management_state"] = "closed" if position["quantity"] <= 0 else "stopped"
            else:
                position["management_state"] = "reduced"
            event_for_action = {
                **result,
                "quantity": sell_qty,
                "gross_amount": round(safe_float(cash_info.get("gross"), price * sell_qty) or price * sell_qty, 2),
                "commission": round(safe_float(sell_costs.get("commission"), 0.0) or 0.0, 2),
                "stamp_duty": round(safe_float(sell_costs.get("stamp_duty"), 0.0) or 0.0, 2),
                "transfer_fee": round(safe_float(sell_costs.get("transfer_fee"), 0.0) or 0.0, 2),
                "slippage": round(safe_float(sell_costs.get("slippage"), 0.0) or 0.0, 2),
                "total_fee": round(safe_float(sell_costs.get("total_fee"), 0.0) or 0.0, 2),
                "cash_in": round(cash_in, 2),
                "net_amount": round(cash_in, 2),
                "fee_model": str(sell_costs.get("fee_model") or ""),
                "cost_profile": str(sell_costs.get("cost_profile") or "legacy"),
            }
            action_row = _make_action(event_for_action, cash_after=cash)
            action_row["pnl"] = round(pnl, 2)
            actions.append(action_row)
            sale_record = _close_record(position, result, sell_qty, price, cash_info, sell_costs)
            if position["quantity"] <= 0:
                closed_positions.append(sale_record)
                positions = [row for row in positions if _position_key(row) != key]
                position_by_key.pop(key, None)
            else:
                sale_record["quantity"] = sell_qty
                sale_record["status"] = "reduced"
                sale_record["management_state"] = "reduced"
                realized_reductions.append(sale_record)
        else:
            if action == "watch_exit" and position:
                position["management_state"] = "watch_exit"
                position["last_action_date"] = trade_date
                position["hold_reason"] = str(result.get("reason") or "不再进入今日候选，但尚未触发卖出条件，进入退出观察。")
            elif action == "hold" and position:
                position["last_action_date"] = trade_date
                position["hold_reason"] = str(result.get("reason") or "已有持仓已生成条件交易计划。本日未触发加仓、减仓、止盈或止损条件，继续纸面持仓。")
            elif action == "block" and position:
                position["management_state"] = "risk_alert"
                position["last_action_date"] = trade_date
                position["hold_reason"] = str(result.get("reason") or "条件计划被风控阻断，本次不模拟成交。")
            actions.append(_make_action(result, cash_after=cash))
    factors = adjustment_factors
    for position in positions:
        symbol = normalize_symbol(position.get("symbol"))
        latest_price = None
        latest_date = ""
        valuation_warnings: list[str] = []
        event_prices = [safe_float(row.get("trigger_price"), None) for _, row in ordered_results if isinstance(row, dict) and normalize_symbol(row.get("symbol")) == symbol and safe_float(row.get("trigger_price"), None)]
        if event_prices:
            latest_price = event_prices[-1]
            latest_date = trade_date
        if latest_price is None:
            latest_price, latest_date, valuation_warnings = _read_daily_close(symbol, trade_date, daily_dir=daily_dir)
        if latest_price is None:
            latest_price = safe_float(position.get("latest_price"), safe_float(position.get("entry_price"), 0.0)) or 0.0
        latest_date = latest_date or position.get("latest_date") or trade_date
        adjusted = apply_paper_adjustment_policy(
            position,
            latest_price=float(latest_price),
            latest_date=str(latest_date or ""),
            factors=factors,
        )
        # The policy deliberately removes obsolete factor-derived balances.
        # Updating alone would leave those removed keys in the original row.
        position.clear()
        position.update(adjusted)
        if valuation_warnings:
            position["valuation_warnings"] = valuation_warnings
        _apply_valuation_gate_marks(position, valuation_warnings)
        if position.get("price_adjustment_status") in {"missing_adj_factor", "stale_adj_factor", "factor_reference_only"}:
            warnings = list(position.get("valuation_warnings") or [])
            warnings.append(str(position.get("price_adjustment_warning") or "missing adjustment factor evidence"))
            position["valuation_warnings"] = list(dict.fromkeys(warnings))
    market_value = sum(safe_float(row.get("market_value"), 0.0) or 0.0 for row in positions)
    unrealized_pnl = sum(safe_float(row.get("unrealized_pnl"), 0.0) or 0.0 for row in positions)
    initial_cash = safe_float(portfolio.get("initial_cash"), cash) or cash
    total_equity = cash + market_value
    # 账本键是 (plan_id, trade_date) 的**集合**语义：同日重跑
    # （allow_same_day_rerun）绕过 914 行的去重闸门后仍会走到这里，
    # 无条件 append 会让"发生过多少次执行"这个计数虚高，也让漏放扫描
    # 的分母失真。保序去重。
    if execution_id not in applied:
        applied.append(execution_id)
    portfolio.update({
        "schema_version": API_SCHEMA_VERSION,
        "app_version": APP_VERSION,
        "updated_at": now_text(),
        "cash": round(cash, 2),
        "positions": positions,
        "closed_positions": closed_positions,
        "realized_reductions": realized_reductions,
        "actions": actions,
        "daily_actions": actions,
        "realized_pnl": round((safe_float(portfolio.get("realized_pnl"), 0.0) or 0.0) + realized_today, 2),
        "unrealized_pnl": round(unrealized_pnl, 2),
        "market_value": round(market_value, 2),
        "total_equity": round(total_equity, 2),
        "total_return_pct": round((total_equity / initial_cash - 1.0) * 100.0, 2) if initial_cash else 0.0,
        "applied_execution_ids": applied[-500:],
        "last_conditional_plan_id": plan_id,
        "last_trade_date": trade_date,
        "disclaimer": DISCLAIMER,
    })
    state_warnings = list(portfolio.get("warnings") or [])
    for position in positions:
        for warning in position.get("valuation_warnings") or []:
            state_warnings.append(f"{normalize_symbol(position.get('symbol'))}: {warning}")
    if state_warnings:
        portfolio["warnings"] = list(dict.fromkeys(str(item) for item in state_warnings))
    if owns_shadow_records and risk_gate_shadow_enabled:
        try:
            summary = summarize_shadow_records(
                shadow_records,
                as_of=trade_date,
                risk_gate_enforced=risk_gate_enforced,
            )
            write_shadow_summary(
                summary,
                output_path=Path(
                    risk_gate_shadow_output_path
                    or (RESULTS_DIR / "risk_gate_shadow_latest.json")
                ),
            )
        except Exception as exc:
            record_shadow_failure("conditional_execution_shadow_output", exc)
    return {
        "ok": True,
        "already_updated_today": False,
        "state": portfolio,
        "actions": actions,
        "realized_today": round(realized_today, 2),
        "is_paper_simulation": True,
        **paper_only_boundary(),
    }


def conditional_execution_latest(path: Path | None = None) -> dict[str, Any]:
    path = Path(path or CONDITIONAL_EXECUTION_LATEST_JSON)
    data = read_json_safe(path, {})
    if not isinstance(data, dict) or not data:
        return {
            "ok": True,
            "exists": False,
            "path": _display_path(path),
            "message": "尚未生成盘中执行结果。",
            "disclaimer": DISCLAIMER,
        }
    data["exists"] = True
    data.setdefault("path", _display_path(path))
    data.setdefault("disclaimer", DISCLAIMER)
    return data
