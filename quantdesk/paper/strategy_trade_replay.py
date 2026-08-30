"""Local trade-path replay (v2.2.1 hardening).

This module simulates a rule-driven trade path over local daily bars.  It is a
local research aid only: it does not connect to any broker, does not place
orders, and does not model the full A-share trading rule-set (T+1, 100-share
lots, limit up/down, suspensions, stamp duty, minimum commission, transfer fee
etc.).  All open/close language is scoped to "virtual" observations so that the
frontend text stays on the safe side.

The v2.2.1 pass tightens the following boundaries:

* signal_date vs execution_date are always separated on each trade, and the
  execution_policy describes which policy was applied.
* close mode settles on the signal day close, next_open mode settles on the
  next available bar open.  Missing next_open bars do not create fake fills.
* Parameters are validated up-front and surfaced as structured errors.
* Cash, commission and slippage are applied consistently on both sides.
* Open positions contribute to equity_curve and final_equity.
* warnings / assumptions are stable and de-duplicated.
"""

from __future__ import annotations

# Compatibility markers for release self-checks: market_rules cn_a_share.
import math
from dataclasses import dataclass, field
from typing import Any, Mapping

import pandas as pd

from quantdesk.backtest import backtest_signal_frame
from quantdesk.cn_a_share_rules import (
    CN_A_SHARE_ASSUMPTION,
    check_t_plus_one_sell_allowed,
    detect_limit_up_down,
    detect_suspension,
    normalize_order_quantity,
)
from quantdesk.paper.market_rules import (
    board_type_for_symbol,
    is_valid_order_quantity,
    lot_size_for_symbol,
    paper_only_boundary,
    round_down_to_lot,
)
from quantdesk.paper.trade_costs import TradeCostConfig, compute_trade_costs
from quantdesk.strategy_demo import check_entry_conditions, check_exit_conditions
from quantdesk.strategy_registry import resolve_strategy
from quantdesk.utils import clamp, safe_date, safe_float, safe_int

TRADE_REPLAY_DISCLAIMER = (
    "交易路径回放仅基于本地历史数据做模拟，不接券商、不会向交易系统提交委托，"
    "不构成投资建议，历史结果不代表未来收益。"
)
TRADE_REPLAY_MODE = "trade_replay"
TRADE_REPLAY_MODE_LABEL = "交易路径回放"
TRADE_REPLAY_STATUS = "simulated"
TRADE_REPLAY_STATUS_LABEL = "历史交易路径模拟"

_VALID_PRICE_MODES = ("close", "next_open")
_VALID_MARKET_RULES = ("simple", "cn_a_share")
_VALID_REPLAY_MODE = TRADE_REPLAY_MODE

_BASE_ASSUMPTIONS = (
    "本模块为本地历史交易路径模拟，不构成投资建议。",
    "不连接券商，不会向交易系统提交委托，历史结果不代表未来收益。",
    "信号在 signal_date 收盘后确认，close 模式为 signal 日收盘成交，next_open 模式为下一条有效行情的 open 成交。",
    "手续费和滑点按用户参数简化模拟。",
    "当前未完整模拟涨跌停、停牌、T+1、板块整手单位、印花税、过户费、最低手续费等 A 股真实交易约束。",
    "strategy_historical_replay 属于 rule_replay，仅做规则满足检查。",
    "strategy_trade_replay 属于 trade_replay，基于信号日与下一交易日口径做虚拟建仓 / 虚拟离场模拟。",
)


class TradeReplayParameterError(ValueError):
    """Raised when caller supplies invalid parameters to strategy_trade_replay."""


def _append_unique(items: list[str], text: str | None) -> None:
    value = str(text or "").strip()
    if value and value not in items:
        items.append(value)


def _market_rule_config(
    market_rules: str,
    *,
    min_commission: float | None,
    stamp_tax_rate: float | None,
    transfer_fee_rate: float | None,
    enforce_lot_size: bool | None,
    lot_size: int | None,
    enforce_t_plus_one: bool | None,
    enforce_price_limit: bool | None,
    enforce_suspension_check: bool | None,
) -> dict[str, Any]:
    normalized = str(market_rules or "simple").strip().lower()
    if normalized not in _VALID_MARKET_RULES:
        normalized = "simple"
    is_cn = normalized == "cn_a_share"
    return {
        "market_rules": normalized,
        "min_commission": float(5.0 if min_commission is None and is_cn else (min_commission or 0.0)),
        "stamp_tax_rate": float(0.0005 if stamp_tax_rate is None and is_cn else (stamp_tax_rate or 0.0)),
        "transfer_fee_rate": float(transfer_fee_rate or 0.0),
        "enforce_lot_size": bool(is_cn if enforce_lot_size is None else enforce_lot_size),
        "lot_size": int(lot_size or (100 if is_cn else 1)),
        "enforce_t_plus_one": bool(is_cn if enforce_t_plus_one is None else enforce_t_plus_one),
        "enforce_price_limit": bool(is_cn if enforce_price_limit is None else enforce_price_limit),
        "enforce_suspension_check": bool(is_cn if enforce_suspension_check is None else enforce_suspension_check),
        "fee_model": "cn_a_share_simplified" if is_cn else "simple_commission",
        "quantity_rule": "100_share_lot" if is_cn else "simple_quantity",
    }


@dataclass
class _OpenTrade:
    trade_id: str
    symbol: str
    name: str
    strategy_id: str
    strategy_name: str
    entry_signal_date: str
    entry_execution_date: str
    entry_execution_policy: str
    entry_price: float
    entry_signal: str
    quantity: int
    entry_fee: float
    entry_commission: float = 0.0
    entry_stamp_duty: float = 0.0
    entry_transfer_fee: float = 0.0
    entry_slippage_fee: float = 0.0
    entry_fee_model: str = ""
    exit_signal_date: str = ""
    exit_execution_date: str = ""
    exit_execution_policy: str = ""
    exit_price: float | None = None
    exit_signal: str = ""
    exit_fee: float = 0.0
    exit_commission: float = 0.0
    exit_stamp_duty: float = 0.0
    exit_transfer_fee: float = 0.0
    exit_slippage_fee: float = 0.0
    exit_fee_model: str = ""
    pnl: float | None = None
    return_pct: float | None = None
    holding_days: int | None = None
    status: str = "open"
    matched_conditions: list[str] = field(default_factory=list)
    failed_conditions: list[str] = field(default_factory=list)
    missing_fields: list[str] = field(default_factory=list)
    risk_notes: list[str] = field(default_factory=list)
    market_regime: str = ""
    fundamental_status: str = ""
    data_health_status: str = ""
    risk_line_type: str = ""
    risk_line_price: float | None = None
    risk_triggered: bool = False
    market_rules: str = "simple"
    fee_model: str = "simple_commission"
    quantity_rule: str = "simple_quantity"
    entry_blocked_reason: str = ""
    exit_blocked_reason: str = ""
    market_rule_warnings: list[str] = field(default_factory=list)
    mark_to_market_date: str = ""
    mark_to_market_price: float | None = None
    disclaimer: str = TRADE_REPLAY_DISCLAIMER

    @property
    def entry_date(self) -> str:
        """Back-compat alias kept for existing API / frontend consumers."""

        return self.entry_execution_date

    @property
    def exit_date(self) -> str:
        return self.exit_execution_date

    def close(
        self,
        *,
        execution_date: str,
        execution_policy: str,
        exit_price: float,
        exit_signal: str,
        exit_fee: float,
        exit_costs: Mapping[str, Any] | None = None,
    ) -> None:
        self.exit_execution_date = execution_date
        self.exit_execution_policy = execution_policy
        self.exit_price = float(exit_price)
        self.exit_signal = exit_signal
        self.exit_fee = float(exit_fee)
        if exit_costs:
            self.exit_commission = float(exit_costs.get("commission") or 0.0)
            self.exit_stamp_duty = float(exit_costs.get("stamp_duty") or 0.0)
            self.exit_transfer_fee = float(exit_costs.get("transfer_fee") or 0.0)
            self.exit_slippage_fee = float(exit_costs.get("slippage") or 0.0)
            self.exit_fee_model = str(exit_costs.get("fee_model") or self.exit_fee_model)
        entry_total = self.entry_price * self.quantity + self.entry_fee
        exit_total = exit_price * self.quantity - exit_fee
        self.pnl = round(exit_total - entry_total, 4)
        self.return_pct = round((self.pnl / entry_total) if entry_total else 0.0, 6)
        self.holding_days = _days_between(self.entry_execution_date, execution_date)
        self.status = "closed"
        self.mark_to_market_date = ""
        self.mark_to_market_price = None

    def mark_to_market(self, *, mark_date: str, mark_price: float) -> None:
        self.mark_to_market_date = mark_date
        self.mark_to_market_price = float(mark_price)
        entry_total = self.entry_price * self.quantity + self.entry_fee
        mark_total = mark_price * self.quantity
        self.pnl = round(mark_total - entry_total, 4)
        self.return_pct = round((self.pnl / entry_total) if entry_total else 0.0, 6)
        self.holding_days = _days_between(self.entry_execution_date, mark_date)
        self.status = "open"

    def to_dict(self) -> dict[str, Any]:
        return {
            **paper_only_boundary(),
            "trade_id": self.trade_id,
            "symbol": self.symbol,
            "name": self.name,
            "strategy_id": self.strategy_id,
            "strategy_name": self.strategy_name,
            "entry_signal_date": self.entry_signal_date,
            "entry_execution_date": self.entry_execution_date,
            "entry_execution_policy": self.entry_execution_policy,
            # Legacy alias retained for backward compatibility with existing
            # frontend / API consumers.  Equal to entry_execution_date.
            "entry_date": self.entry_execution_date,
            "entry_price": self.entry_price,
            "entry_signal": self.entry_signal,
            "quantity": self.quantity,
            "entry_fee": self.entry_fee,
            "entry_commission": self.entry_commission,
            "entry_stamp_duty": self.entry_stamp_duty,
            "entry_transfer_fee": self.entry_transfer_fee,
            "entry_slippage_fee": self.entry_slippage_fee,
            "entry_fee_model": self.entry_fee_model,
            "exit_signal_date": self.exit_signal_date,
            "exit_execution_date": self.exit_execution_date,
            "exit_execution_policy": self.exit_execution_policy,
            "exit_date": self.exit_execution_date,
            "exit_price": self.exit_price,
            "exit_signal": self.exit_signal,
            "exit_fee": self.exit_fee,
            "exit_commission": self.exit_commission,
            "exit_stamp_duty": self.exit_stamp_duty,
            "exit_transfer_fee": self.exit_transfer_fee,
            "exit_slippage_fee": self.exit_slippage_fee,
            "exit_fee_model": self.exit_fee_model,
            "pnl": self.pnl,
            "return_pct": self.return_pct,
            "holding_days": self.holding_days,
            "status": self.status,
            "matched_conditions": list(self.matched_conditions),
            "failed_conditions": list(self.failed_conditions),
            "missing_fields": list(self.missing_fields),
            "risk_notes": list(self.risk_notes),
            "market_regime": self.market_regime,
            "fundamental_status": self.fundamental_status,
            "data_health_status": self.data_health_status,
            "risk_line_type": self.risk_line_type,
            "risk_line_price": self.risk_line_price,
            "risk_triggered": self.risk_triggered,
            "market_rules": self.market_rules,
            "fee_model": self.fee_model,
            "quantity_rule": self.quantity_rule,
            "entry_blocked_reason": self.entry_blocked_reason,
            "exit_blocked_reason": self.exit_blocked_reason,
            "market_rule_warnings": list(self.market_rule_warnings),
            "mark_to_market_date": self.mark_to_market_date,
            "mark_to_market_price": self.mark_to_market_price,
            "disclaimer": self.disclaimer,
        }


def _as_frame(history: Any) -> pd.DataFrame:
    if history is None:
        return pd.DataFrame()
    if isinstance(history, pd.DataFrame):
        return history.copy()
    if isinstance(history, pd.Series):
        return history.to_frame().T
    if isinstance(history, Mapping):
        return pd.DataFrame([dict(history)])
    if isinstance(history, list):
        rows = [row for row in history if isinstance(row, Mapping)]
        return pd.DataFrame(rows) if rows else pd.DataFrame()
    return pd.DataFrame()


def _date_text(value: object) -> str:
    return safe_date(value, "")


def _days_between(start: str, end: str) -> int | None:
    try:
        start_dt = pd.to_datetime(start, errors="coerce")
        end_dt = pd.to_datetime(end, errors="coerce")
    except Exception:
        return None
    if pd.isna(start_dt) or pd.isna(end_dt):
        return None
    return max(int((end_dt - start_dt).days), 0)


def _num(row: Mapping[str, Any], *keys: str, default: float | None = None) -> float | None:
    for key in keys:
        if key in row:
            value = safe_float(row.get(key), None)
            if value is not None:
                return value
    return default


def _close_price(row: Mapping[str, Any]) -> float | None:
    return _num(row, "close", "收盘")


def _open_price(row: Mapping[str, Any]) -> float | None:
    return _num(row, "open", "开盘")


def _apply_slippage(base: float, *, side: str, slippage_rate: float) -> float:
    if side == "buy":
        price = base * (1.0 + slippage_rate)
    else:
        price = base * (1.0 - slippage_rate)
    return round(max(price, 0.0), 6)


def _execute_on_signal_close(
    *,
    signal_row: Mapping[str, Any],
    side: str,
    slippage_rate: float,
) -> tuple[float | None, str | None]:
    close = _close_price(signal_row)
    if close is None or close <= 0:
        return None, "signal 日缺少 close 价格，无法按 close 口径模拟成交。"
    return _apply_slippage(close, side=side, slippage_rate=slippage_rate), None


def _execute_on_next_open(
    *,
    next_row: Mapping[str, Any] | None,
    side: str,
    slippage_rate: float,
) -> tuple[float | None, str | None]:
    if not next_row:
        return None, "next_open 不可用：缺少下一条有效 K 线，已跳过该笔模拟成交。"
    base = _open_price(next_row)
    if base is None or base <= 0:
        close = _close_price(next_row)
        if close is None or close <= 0:
            return None, "next_open 不可用：下一交易日缺少可用价格，已跳过该笔模拟成交。"
        return _apply_slippage(close, side=side, slippage_rate=slippage_rate), (
            "next_open 不可用：下一交易日缺少 open，已按该日 close 近似模拟成交。"
        )
    return _apply_slippage(base, side=side, slippage_rate=slippage_rate), None


def _lot_quantity(quantity: int) -> int:
    if quantity <= 0:
        return 0
    return round_down_to_lot(quantity)


def _infer_position_size(
    *,
    position_size: float | int | None,
    max_position_ratio: float,
    cash: float,
    price: float,
) -> int:
    if position_size is not None:
        parsed = safe_float(position_size, None)
        if parsed is None or parsed <= 0:
            return 0
        return _lot_quantity(int(parsed))
    ratio = clamp(float(max_position_ratio or 0.1), 0.0, 1.0)
    if cash <= 0 or price <= 0:
        return 0
    target_value = cash * ratio
    return _lot_quantity(int(target_value // price))


def _risk_line_price(
    row: Mapping[str, Any],
    risk_line_type: str,
    *,
    highest_since_entry: float | None = None,
) -> tuple[float | None, bool, str]:
    close = _close_price(row)
    if close is None:
        return None, False, ""
    normalized = str(risk_line_type or "ma20").strip().lower()
    if normalized in {"ma20", "ma_20"}:
        line = _num(row, "ma20", "ma_20")
        if line is None:
            return None, False, ""
        return line, close < line, "ma20"
    if normalized in {"atr", "atr14", "atr_trailing"}:
        atr = _num(row, "atr14", "atr")
        if atr is None:
            return None, False, ""
        high = _num(row, "high", "最高") or close
        trailing_high = high
        if highest_since_entry is not None and math.isfinite(highest_since_entry):
            trailing_high = max(trailing_high, highest_since_entry)
        line = trailing_high - atr * 2.0
        return line, close < line, "atr_trailing"
    if normalized in {"recent_low", "low_20d"}:
        line = _num(row, "low_20d", "recent_low", "lowest_20d")
        if line is None:
            return None, False, ""
        return line, close < line, "recent_low"
    manual = _num(row, "risk_line", "manual_risk_line")
    if manual is None:
        return None, False, ""
    return manual, close < manual, "manual"


def _normalize_price_mode(mode: str, *, side: str, errors: list[str]) -> str:
    normalized = str(mode or "close").strip().lower()
    if normalized not in _VALID_PRICE_MODES:
        errors.append(
            f"{side}_price_mode 非法：{mode!r}，仅允许 {list(_VALID_PRICE_MODES)}。"
        )
        return "close"
    return normalized


def _validate_params(
    *,
    symbol: str,
    strategy_name: str,
    initial_cash: float,
    commission_rate: float,
    slippage_rate: float,
    position_size: float | int | None,
    max_position_ratio: float,
    buy_price_mode: str,
    sell_price_mode: str,
    market_rules: str,
    min_commission: float | None,
    stamp_tax_rate: float | None,
    transfer_fee_rate: float | None,
    lot_size: int | None,
    start_date: str | None,
    end_date: str | None,
) -> tuple[list[str], str, str]:
    errors: list[str] = []
    if not str(symbol or "").strip():
        errors.append("symbol 不能为空。")
    if initial_cash is None or float(initial_cash) <= 0:
        errors.append("initial_cash 必须 > 0。")
    if commission_rate is None or float(commission_rate) < 0:
        errors.append("commission_rate 必须 >= 0。")
    if slippage_rate is None or float(slippage_rate) < 0:
        errors.append("slippage_rate 必须 >= 0。")
    if position_size is not None:
        parsed = safe_float(position_size, None)
        if parsed is None or parsed <= 0:
            errors.append("position_size 必须 > 0。")
    if max_position_ratio is None or float(max_position_ratio) <= 0 or float(max_position_ratio) > 1:
        errors.append("max_position_ratio 必须位于 (0, 1]。")
    buy_mode = _normalize_price_mode(buy_price_mode, side="buy", errors=errors)
    sell_mode = _normalize_price_mode(sell_price_mode, side="sell", errors=errors)
    if str(market_rules or "simple").strip().lower() not in _VALID_MARKET_RULES:
        errors.append(f"market_rules 非法：{market_rules!r}，仅允许 {list(_VALID_MARKET_RULES)}。")
    for label, value in (
        ("min_commission", min_commission),
        ("stamp_tax_rate", stamp_tax_rate),
        ("transfer_fee_rate", transfer_fee_rate),
    ):
        if value is not None and float(value) < 0:
            errors.append(f"{label} 必须 >= 0。")
    if lot_size is not None and int(lot_size) <= 0:
        errors.append("lot_size 必须 > 0。")

    if start_date and end_date:
        start_dt = pd.to_datetime(start_date, errors="coerce")
        end_dt = pd.to_datetime(end_date, errors="coerce")
        if pd.notna(start_dt) and pd.notna(end_dt) and start_dt > end_dt:
            errors.append("start_date 不能晚于 end_date。")

    try:
        resolved = resolve_strategy(strategy_name)
    except Exception as exc:  # pragma: no cover - defensive
        errors.append(f"策略无法解析：{exc}")
        resolved = {}
    if not resolved:
        errors.append(f"未知策略：{strategy_name!r}。")
    return errors, buy_mode, sell_mode


def _empty_result(
    *,
    symbol: str,
    strategy_name: str,
    initial_cash: float,
    buy_price_mode: str,
    sell_price_mode: str,
    commission_rate: float,
    slippage_rate: float,
    rule_config: Mapping[str, Any] | None = None,
    position_size: float | int | None,
    max_position_ratio: float,
    data_mode: str,
    history_source_symbol: str,
    history_source_note: str,
    warnings: list[str],
    errors: list[str],
    strategy_warning: str = "",
) -> dict[str, Any]:
    resolved = resolve_strategy(strategy_name) if strategy_name else {}
    rules = dict(rule_config or _market_rule_config(
        "simple",
        min_commission=0.0,
        stamp_tax_rate=0.0,
        transfer_fee_rate=0.0,
        enforce_lot_size=False,
        lot_size=1,
        enforce_t_plus_one=False,
        enforce_price_limit=False,
        enforce_suspension_check=False,
    ))
    assumptions = list(_BASE_ASSUMPTIONS)
    if rules.get("market_rules") == "cn_a_share":
        assumptions.append(CN_A_SHARE_ASSUMPTION)
    return {
        **paper_only_boundary(),
        "replay_mode": TRADE_REPLAY_MODE,
        "replay_mode_label": TRADE_REPLAY_MODE_LABEL,
        "trade_replay_status": TRADE_REPLAY_STATUS,
        "trade_replay_status_label": TRADE_REPLAY_STATUS_LABEL,
        "strategy_id": str(resolved.get("strategy_id") or strategy_name or "ma_breakout"),
        "strategy_name": str(resolved.get("name") or strategy_name or "均线突破"),
        "symbol": str(symbol or "").zfill(6),
        "name": "",
        "data_mode": data_mode,
        "history_source_symbol": str(history_source_symbol or symbol or "").zfill(6),
        "history_source_note": history_source_note,
        "history_rows": 0,
        "history_start_date": "",
        "history_end_date": "",
        "buy_price_mode": buy_price_mode,
        "sell_price_mode": sell_price_mode,
        "market_rules": rules.get("market_rules", "simple"),
        "fee_model": rules.get("fee_model", "simple_commission"),
        "quantity_rule": rules.get("quantity_rule", "simple_quantity"),
        "min_commission": float(rules.get("min_commission") or 0.0),
        "stamp_tax_rate": float(rules.get("stamp_tax_rate") or 0.0),
        "transfer_fee_rate": float(rules.get("transfer_fee_rate") or 0.0),
        "enforce_lot_size": bool(rules.get("enforce_lot_size")),
        "lot_size": int(rules.get("lot_size") or 1),
        "enforce_t_plus_one": bool(rules.get("enforce_t_plus_one")),
        "enforce_price_limit": bool(rules.get("enforce_price_limit")),
        "enforce_suspension_check": bool(rules.get("enforce_suspension_check")),
        "blocked_order_count": 0,
        "commission_rate": float(commission_rate),
        "slippage_rate": float(slippage_rate),
        "position_size": position_size,
        "max_position_ratio": float(max_position_ratio),
        "initial_cash": float(initial_cash),
        "final_cash": float(initial_cash),
        "final_equity": float(initial_cash),
        "total_return": 0.0,
        "max_drawdown": 0.0,
        "trade_count": 0,
        "closed_trade_count": 0,
        "open_trade_count": 0,
        "win_rate": None,
        "average_trade_return": None,
        "holding_days_avg": None,
        "equity_curve": [],
        "trades": [],
        "warnings": _dedupe(warnings),
        "assumptions": _dedupe(assumptions),
        "errors": list(errors),
        "strategy_warning": strategy_warning,
        "disclaimer": TRADE_REPLAY_DISCLAIMER,
    }


def _dedupe(items: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for item in items:
        text = str(item or "").strip()
        if not text or text in seen:
            continue
        seen.add(text)
        out.append(text)
    return out


def _condition_strings(detail: Mapping[str, Any]) -> tuple[list[str], list[str], list[str], list[str]]:
    def _collect(items: Any, primary: tuple[str, ...]) -> list[str]:
        out: list[str] = []
        for item in items or []:
            if not isinstance(item, Mapping):
                continue
            for key in primary:
                value = item.get(key)
                if value:
                    out.append(str(value))
                    break
        return out

    matched = _collect(detail.get("matched"), ("display_condition", "explain", "condition"))
    failed = _collect(detail.get("failed"), ("display_condition", "explain", "condition"))
    missing = _collect(detail.get("missing"), ("field", "display_condition", "explain"))
    risk_notes = [str(item) for item in (detail.get("risk_notes") or []) if str(item).strip()]
    return matched, failed, missing, risk_notes


def _fee_amount(
    *,
    side: str,
    amount: float,
    commission_rate: float,
    rule_config: Mapping[str, Any],
) -> float:
    return float(
        _fee_breakdown(
            side=side,
            amount=amount,
            commission_rate=commission_rate,
            rule_config=rule_config,
        )["total_fee"]
    )


def _fee_breakdown(
    *,
    side: str,
    amount: float,
    commission_rate: float,
    rule_config: Mapping[str, Any],
) -> dict[str, float | str]:
    gross = max(float(amount or 0.0), 0.0)
    if gross <= 0:
        return {
            "commission": 0.0,
            "stamp_duty": 0.0,
            "stamp_tax": 0.0,
            "transfer_fee": 0.0,
            "slippage": 0.0,
            "total_fee": 0.0,
            "fee_model": str(rule_config.get("fee_model") or "simple_commission"),
        }
    if rule_config.get("market_rules") == "cn_a_share":
        return compute_trade_costs(
            side=side,
            gross=gross,
            config=TradeCostConfig(
                commission_rate=float(commission_rate),
                min_commission=float(rule_config.get("min_commission") or 0.0),
                stamp_tax_rate=float(rule_config.get("stamp_tax_rate") or 0.0),
                transfer_fee_rate=float(rule_config.get("transfer_fee_rate") or 0.0),
                # strategy_trade_replay applies slippage by moving the execution
                # price, so the unified fee service must not add a second
                # slippage charge here.
                slippage_bps=0.0,
                apply_min_commission=True,
            ),
        )
    commission = round(gross * float(commission_rate), 4)
    return {
        "commission": commission,
        "stamp_duty": 0.0,
        "stamp_tax": 0.0,
        "transfer_fee": 0.0,
        "slippage": 0.0,
        "total_fee": commission,
        "fee_model": "simple_commission",
    }


def _market_rule_block(
    *,
    side: str,
    row: Mapping[str, Any],
    execution_price: float | None,
    execution_date: str,
    current_trade: _OpenTrade | None,
    rule_config: Mapping[str, Any],
) -> tuple[bool, str, list[str]]:
    if rule_config.get("market_rules") != "cn_a_share":
        return False, "", []
    warnings: list[str] = []
    if rule_config.get("enforce_suspension_check"):
        suspension = detect_suspension(row)
        if suspension.get("warning"):
            warnings.append(str(suspension["warning"]))
        if suspension.get("tradable") is False:
            return True, str(suspension.get("reason") or "疑似停牌或不可成交。"), warnings
    if rule_config.get("enforce_price_limit"):
        # The existing CN rule helper evaluates ``close``.  For next-open
        # execution the executable observation is the open/fill price, so pass
        # that verified value through the same rule instead of accidentally
        # checking the execution day's later close.
        limit_row = dict(row)
        if execution_price is not None:
            limit_row["close"] = execution_price
        limit = detect_limit_up_down(limit_row, side=side)
        if limit.get("warning"):
            warnings.append(str(limit["warning"]))
        if limit.get("blocked"):
            return True, str(limit.get("reason") or "涨跌停基础判断阻止成交。"), warnings
    if side == "sell" and rule_config.get("enforce_t_plus_one") and current_trade is not None:
        t1 = check_t_plus_one_sell_allowed(
            entry_execution_date=current_trade.entry_execution_date,
            current_execution_date=execution_date,
        )
        if t1.get("warning"):
            warnings.append(str(t1["warning"]))
        if t1.get("allowed") is False:
            return True, str(t1.get("reason") or "T+1 限制阻止成交。"), warnings
    return False, "", warnings


def strategy_trade_replay(
    history: Any,
    strategy_name: str,
    *,
    symbol: str = "",
    name: str = "",
    initial_cash: float = 100000,
    position_size: float | int | None = None,
    max_position_ratio: float = 0.1,
    commission_rate: float = 0.00025,
    slippage_rate: float = 0.0005,
    buy_price_mode: str = "close",
    sell_price_mode: str = "close",
    market_rules: str = "simple",
    min_commission: float | None = None,
    stamp_tax_rate: float | None = None,
    transfer_fee_rate: float | None = None,
    enforce_lot_size: bool | None = None,
    lot_size: int | None = None,
    enforce_t_plus_one: bool | None = None,
    enforce_price_limit: bool | None = None,
    enforce_suspension_check: bool | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
    data_mode: str = "report",
    history_source_symbol: str = "",
    history_source_note: str = "",
) -> dict[str, Any]:
    """Simulate a local trade path for a single stock and strategy.

    See module docstring for the exact simulation boundaries.
    """

    warnings: list[str] = []
    market_rule_warnings: list[str] = []
    blocked_order_count = 0
    rule_config = _market_rule_config(
        market_rules,
        min_commission=min_commission,
        stamp_tax_rate=stamp_tax_rate,
        transfer_fee_rate=transfer_fee_rate,
        enforce_lot_size=enforce_lot_size,
        lot_size=lot_size,
        enforce_t_plus_one=enforce_t_plus_one,
        enforce_price_limit=enforce_price_limit,
        enforce_suspension_check=enforce_suspension_check,
    )
    if rule_config.get("market_rules") == "cn_a_share":
        rule_config["board_type"] = board_type_for_symbol(symbol, name=name)
        if lot_size is None:
            rule_config["lot_size"] = lot_size_for_symbol(symbol, name=name)

    validation_errors, buy_mode, sell_mode = _validate_params(
        symbol=symbol,
        strategy_name=strategy_name,
        initial_cash=initial_cash,
        commission_rate=commission_rate,
        slippage_rate=slippage_rate,
        position_size=position_size,
        max_position_ratio=max_position_ratio,
        buy_price_mode=buy_price_mode,
        sell_price_mode=sell_price_mode,
        market_rules=market_rules,
        min_commission=min_commission,
        stamp_tax_rate=stamp_tax_rate,
        transfer_fee_rate=transfer_fee_rate,
        lot_size=lot_size,
        start_date=start_date,
        end_date=end_date,
    )
    if validation_errors:
        warnings.extend(validation_errors)
        return _empty_result(
            symbol=symbol,
            strategy_name=strategy_name,
            initial_cash=float(initial_cash or 0),
            buy_price_mode=buy_mode,
            sell_price_mode=sell_mode,
            commission_rate=float(commission_rate or 0),
            slippage_rate=float(slippage_rate or 0),
            rule_config=rule_config,
            position_size=position_size,
            max_position_ratio=float(max_position_ratio or 0),
            data_mode=data_mode,
            history_source_symbol=history_source_symbol,
            history_source_note=history_source_note,
            warnings=warnings,
            errors=validation_errors,
        )

    frame = _as_frame(history)
    if frame.empty:
        warnings.append("缺少历史数据，无法生成交易路径回放。")
        return _empty_result(
            symbol=symbol,
            strategy_name=strategy_name,
            initial_cash=float(initial_cash),
            buy_price_mode=buy_mode,
            sell_price_mode=sell_mode,
            commission_rate=float(commission_rate),
            slippage_rate=float(slippage_rate),
            rule_config=rule_config,
            position_size=position_size,
            max_position_ratio=float(max_position_ratio),
            data_mode=data_mode,
            history_source_symbol=history_source_symbol,
            history_source_note=history_source_note,
            warnings=warnings,
            errors=[],
        )

    signals = backtest_signal_frame(frame, strategy=strategy_name)
    strategy_warning = str(signals.attrs.get("strategy_warning") or "")
    if strategy_warning:
        warnings.append(strategy_warning)

    if "date" not in signals.columns:
        for column in ("日期", "trade_date", "daily_bar_date"):
            if column in signals.columns:
                signals = signals.rename(columns={column: "date"})
                break
    signals["date"] = pd.to_datetime(signals["date"], errors="coerce")
    signals = signals.dropna(subset=["date"]).sort_values("date").reset_index(drop=True)

    start_dt = pd.to_datetime(start_date, errors="coerce") if start_date else None
    end_dt = pd.to_datetime(end_date, errors="coerce") if end_date else None
    if start_dt is not None:
        signals = signals.loc[signals["date"] >= start_dt].reset_index(drop=True)
    if end_dt is not None:
        signals = signals.loc[signals["date"] <= end_dt].reset_index(drop=True)

    if signals.empty:
        warnings.append("start_date/end_date 过滤后无可用行情。")
        return _empty_result(
            symbol=symbol,
            strategy_name=strategy_name,
            initial_cash=float(initial_cash),
            buy_price_mode=buy_mode,
            sell_price_mode=sell_mode,
            commission_rate=float(commission_rate),
            slippage_rate=float(slippage_rate),
            rule_config=rule_config,
            position_size=position_size,
            max_position_ratio=float(max_position_ratio),
            data_mode=data_mode,
            history_source_symbol=history_source_symbol,
            history_source_note=history_source_note,
            warnings=warnings,
            errors=[],
            strategy_warning=strategy_warning,
        )

    strategy_def = resolve_strategy(strategy_name)
    strategy_id = str(
        signals.iloc[0].get("strategy_id")
        or strategy_def.get("strategy_id")
        or "ma_breakout"
    )
    strategy_name_label = str(
        signals.iloc[0].get("strategy_name")
        or strategy_def.get("name")
        or strategy_name
        or "均线突破"
    )
    entry_label = str(strategy_def.get("entry_label") or "虚拟建仓观察条件")
    exit_label = str(strategy_def.get("exit_label") or "虚拟离场观察条件")
    risk_line_type = str(strategy_def.get("risk_line_type") or "ma20")
    source_symbol = str(symbol or "").zfill(6)
    if not name:
        name = str(signals.iloc[0].get("name") or "")


    cash = float(initial_cash)
    current_trade: _OpenTrade | None = None
    pending_entry: dict[str, Any] | None = None
    pending_exit: dict[str, Any] | None = None
    pending_risk_exit: dict[str, Any] | None = None
    equity_curve: list[dict[str, Any]] = []
    trades: list[dict[str, Any]] = []
    highest_since_entry: float = float("nan")
    peak_equity = float(initial_cash)
    ignored_signals = 0
    signal_count = 0

    rows = [dict(raw) for _, raw in signals.iterrows()]
    n = len(rows)

    for index, row in enumerate(rows):
        signal_date = _date_text(row.get("date"))
        signal_close = _close_price(row)
        signal_open = _open_price(row)
        next_row = rows[index + 1] if index + 1 < n else None

        # Execute exits first so the cash is available for the same-day entry
        # (close mode); next_open executions wait until the following row.
        if current_trade is not None and pending_risk_exit is not None:
            policy = pending_risk_exit["policy"]
            if policy == "signal_close":
                exit_row = pending_risk_exit["row"]
                price, warn = _execute_on_signal_close(
                    signal_row=exit_row,
                    side="sell",
                    slippage_rate=float(slippage_rate),
                )
            else:
                price, warn = _execute_on_next_open(
                    next_row=row,
                    side="sell",
                    slippage_rate=float(slippage_rate),
                )
            if warn:
                warnings.append(warn)
            if price is None:
                warnings.append(
                    f"{signal_date or '未知日期'} 风险线触发但缺少可用执行价，已保留虚拟持仓。"
                )
            else:
                exec_date = (
                    pending_risk_exit["signal_date"]
                    if policy == "signal_close"
                    else signal_date
                )
                exec_row = pending_risk_exit["row"] if policy == "signal_close" else row
                blocked, reason, rule_warnings = _market_rule_block(
                    side="sell",
                    row=exec_row,
                    execution_price=price,
                    execution_date=exec_date,
                    current_trade=current_trade,
                    rule_config=rule_config,
                )
                for item in rule_warnings:
                    _append_unique(warnings, item)
                    _append_unique(market_rule_warnings, item)
                if blocked:
                    blocked_order_count += 1
                    current_trade.exit_blocked_reason = reason
                    current_trade.market_rule_warnings = _dedupe(
                        current_trade.market_rule_warnings + rule_warnings
                    )
                    warnings.append(f"{exec_date or '未知日期'} {reason} 已保留虚拟持仓。")
                    pending_risk_exit = None
                else:
                    current_trade.risk_line_type = pending_risk_exit.get(
                        "risk_line_type", current_trade.risk_line_type
                    )
                    current_trade.risk_line_price = pending_risk_exit.get(
                        "risk_line_price", current_trade.risk_line_price
                    )
                    current_trade.risk_triggered = True
                    current_trade.exit_signal_date = pending_risk_exit["signal_date"]
                    exit_costs = _fee_breakdown(
                        side="sell",
                        amount=price * current_trade.quantity,
                        commission_rate=float(commission_rate),
                        rule_config=rule_config,
                    )
                    exit_fee = float(exit_costs["total_fee"])
                    current_trade.close(
                        execution_date=exec_date,
                        execution_policy=policy,
                        exit_price=price,
                        exit_signal="风险线触发",
                        exit_fee=exit_fee,
                        exit_costs=exit_costs,
                    )
                    cash = round(
                        cash + current_trade.quantity * price - current_trade.exit_fee,
                        4,
                    )
                    trades.append(current_trade.to_dict())
                    current_trade = None
                    pending_exit = None
                    pending_entry = None
                    pending_risk_exit = None
                    highest_since_entry = float("nan")

        if current_trade is not None and pending_exit is not None:
            policy = pending_exit["policy"]
            if policy == "signal_close":
                exit_row = pending_exit["row"]
                price, warn = _execute_on_signal_close(
                    signal_row=exit_row,
                    side="sell",
                    slippage_rate=float(slippage_rate),
                )
            else:
                price, warn = _execute_on_next_open(
                    next_row=row,
                    side="sell",
                    slippage_rate=float(slippage_rate),
                )
            if warn:
                warnings.append(warn)
            if price is None:
                warnings.append(
                    f"{signal_date or '未知日期'} 无法执行虚拟离场，已保留虚拟持仓。"
                )
                pending_exit = None
            else:
                exec_date = (
                    pending_exit["signal_date"]
                    if policy == "signal_close"
                    else signal_date
                )
                exec_row = pending_exit["row"] if policy == "signal_close" else row
                blocked, reason, rule_warnings = _market_rule_block(
                    side="sell",
                    row=exec_row,
                    execution_price=price,
                    execution_date=exec_date,
                    current_trade=current_trade,
                    rule_config=rule_config,
                )
                for item in rule_warnings:
                    _append_unique(warnings, item)
                    _append_unique(market_rule_warnings, item)
                if blocked:
                    blocked_order_count += 1
                    current_trade.exit_blocked_reason = reason
                    current_trade.market_rule_warnings = _dedupe(
                        current_trade.market_rule_warnings + rule_warnings
                    )
                    warnings.append(f"{exec_date or '未知日期'} {reason} 已保留虚拟持仓。")
                    pending_exit = None
                else:
                    current_trade.exit_signal_date = pending_exit["signal_date"]
                    exit_costs = _fee_breakdown(
                        side="sell",
                        amount=price * current_trade.quantity,
                        commission_rate=float(commission_rate),
                        rule_config=rule_config,
                    )
                    exit_fee = float(exit_costs["total_fee"])
                    current_trade.close(
                        execution_date=exec_date,
                        execution_policy=policy,
                        exit_price=price,
                        exit_signal=pending_exit.get("label") or exit_label,
                        exit_fee=exit_fee,
                        exit_costs=exit_costs,
                    )
                    cash = round(
                        cash + current_trade.quantity * price - current_trade.exit_fee,
                        4,
                    )
                    trades.append(current_trade.to_dict())
                    current_trade = None
                    pending_exit = None
                    pending_entry = None
                    highest_since_entry = float("nan")


        if current_trade is None and pending_entry is not None:
            policy = pending_entry["policy"]
            if policy == "signal_close":
                price, warn = _execute_on_signal_close(
                    signal_row=pending_entry["row"],
                    side="buy",
                    slippage_rate=float(slippage_rate),
                )
            else:
                price, warn = _execute_on_next_open(
                    next_row=row,
                    side="buy",
                    slippage_rate=float(slippage_rate),
                )
            if warn:
                warnings.append(warn)
            if price is None:
                warnings.append(
                    f"{signal_date or '未知日期'} 无法计算虚拟建仓价格，已跳过该信号。"
                )
                pending_entry = None
            else:
                exec_date = (
                    pending_entry["signal_date"]
                    if policy == "signal_close"
                    else signal_date
                )
                exec_row = pending_entry["row"] if policy == "signal_close" else row
                blocked, reason, rule_warnings = _market_rule_block(
                    side="buy",
                    row=exec_row,
                    execution_price=price,
                    execution_date=exec_date,
                    current_trade=None,
                    rule_config=rule_config,
                )
                for item in rule_warnings:
                    _append_unique(warnings, item)
                    _append_unique(market_rule_warnings, item)
                if blocked:
                    blocked_order_count += 1
                    warnings.append(f"{exec_date or '未知日期'} {reason} 已跳过虚拟建仓。")
                    pending_entry = None
                    continue
                quantity = _infer_position_size(
                    position_size=position_size,
                    max_position_ratio=float(max_position_ratio),
                    cash=cash,
                    price=price,
                )
                if rule_config.get("enforce_lot_size"):
                    normalized = normalize_order_quantity(
                        quantity,
                        price=price,
                        cash=cash,
                        side="buy",
                        lot_size=int(rule_config.get("lot_size") or 100),
                    )
                    if normalized.get("warning"):
                        _append_unique(warnings, str(normalized["warning"]))
                        _append_unique(market_rule_warnings, str(normalized["warning"]))
                    quantity = int(normalized.get("quantity") or 0)
                    valid_quantity = is_valid_order_quantity(
                        quantity,
                        lot_size=int(rule_config.get("lot_size") or 100),
                        board_type=str(rule_config.get("board_type") or "main"),
                    )
                    if normalized.get("blocked") or not valid_quantity:
                        quantity = 0
                        blocked_order_count += 1
                if quantity <= 0:
                    warnings.append(
                        f"{signal_date or '未知日期'} 资金不足以建立最小仓位，已跳过虚拟建仓。"
                    )
                    pending_entry = None
                else:
                    entry_costs = _fee_breakdown(
                        side="buy",
                        amount=price * quantity,
                        commission_rate=float(commission_rate),
                        rule_config=rule_config,
                    )
                    entry_fee = float(entry_costs["total_fee"])
                    total_cost = round(price * quantity + entry_fee, 4)
                    if total_cost > cash + 1e-9:
                        warnings.append(
                            f"{signal_date or '未知日期'} 资金不足，未执行虚拟建仓。"
                        )
                        pending_entry = None
                    else:
                        detail = pending_entry.get("detail") or {}
                        matched, failed, missing, risk_notes = _condition_strings(detail)
                        current_trade = _OpenTrade(
                            trade_id=f"TRD-{len(trades) + 1:04d}",
                            symbol=source_symbol,
                            name=name,
                            strategy_id=strategy_id,
                            strategy_name=strategy_name_label,
                            entry_signal_date=pending_entry["signal_date"],
                            entry_execution_date=exec_date,
                            entry_execution_policy=policy,
                            entry_price=price,
                            entry_signal=entry_label,
                            quantity=int(quantity),
                            entry_fee=entry_fee,
                            entry_commission=float(entry_costs.get("commission") or 0.0),
                            entry_stamp_duty=float(entry_costs.get("stamp_duty") or 0.0),
                            entry_transfer_fee=float(entry_costs.get("transfer_fee") or 0.0),
                            entry_slippage_fee=float(entry_costs.get("slippage") or 0.0),
                            entry_fee_model=str(entry_costs.get("fee_model") or ""),
                            matched_conditions=matched,
                            failed_conditions=failed,
                            missing_fields=missing,
                            risk_notes=_dedupe(risk_notes),
                            market_regime=str(detail.get("market_regime") or ""),
                            fundamental_status=str(detail.get("fundamental_status") or ""),
                            data_health_status=str(
                                detail.get("data_status")
                                or detail.get("feature_freshness_label")
                                or ""
                            ),
                            risk_line_type=risk_line_type,
                            risk_line_price=None,
                            market_rules=str(rule_config.get("market_rules") or "simple"),
                            fee_model=str(rule_config.get("fee_model") or "simple_commission"),
                            quantity_rule=str(rule_config.get("quantity_rule") or "simple_quantity"),
                            market_rule_warnings=_dedupe(list(market_rule_warnings)),
                        )
                        cash = round(cash - total_cost, 4)
                        highest_since_entry = float(price)
                        pending_entry = None

        # Risk-line check happens at the close of the current bar for any bar
        # where the position has been held through that day.  The exit fill is
        # deferred by one bar when sell mode is next_open.
        if current_trade is not None and current_trade.entry_execution_date != signal_date:
            line, triggered, detected_type = _risk_line_price(
                row, risk_line_type, highest_since_entry=highest_since_entry
            )
            if triggered:
                if sell_mode == "close":
                    pending_risk_exit = {
                        "signal_date": signal_date,
                        "policy": "signal_close",
                        "row": row,
                        "risk_line_type": detected_type or risk_line_type,
                        "risk_line_price": line,
                    }
                else:
                    pending_risk_exit = {
                        "signal_date": signal_date,
                        "policy": "next_open",
                        "row": row,
                        "risk_line_type": detected_type or risk_line_type,
                        "risk_line_price": line,
                    }

        # Record rule-level entry / exit signals; execution happens on the
        # following iteration according to buy_mode / sell_mode.
        if bool(row.get("entry_signal")):
            signal_count += 1
            if current_trade is None and pending_entry is None:
                entry_detail = check_entry_conditions(row, strategy_id)
                pending_entry = {
                    "signal_date": signal_date,
                    "detail": entry_detail,
                    "row": row,
                    "policy": "signal_close" if buy_mode == "close" else "next_open",
                }
            else:
                ignored_signals += 1

        if bool(row.get("exit_signal")):
            if current_trade is not None and pending_exit is None and pending_risk_exit is None:
                exit_detail = check_exit_conditions(row, row, strategy_id)
                pending_exit = {
                    "signal_date": signal_date,
                    "detail": exit_detail,
                    "row": row,
                    "label": str(exit_detail.get("check_type_label") or exit_label),
                    "policy": "signal_close" if sell_mode == "close" else "next_open",
                }
            elif current_trade is None:
                ignored_signals += 1


        # Update running high for ATR trailing, and compute equity for the day.
        bar_close = signal_close if signal_close is not None else signal_open
        if current_trade is not None and bar_close is not None:
            highest_since_entry = (
                bar_close
                if not math.isfinite(highest_since_entry)
                else max(highest_since_entry, bar_close)
            )
            position_value = round(current_trade.quantity * bar_close, 4)
        else:
            position_value = 0.0
        equity = round(cash + position_value, 4)
        peak_equity = max(peak_equity, equity)
        drawdown = round((equity / peak_equity - 1.0) if peak_equity > 0 else 0.0, 6)
        equity_curve.append(
            {
                "date": signal_date,
                "close": bar_close,
                "cash": round(cash, 4),
                "position_value": position_value,
                "market_value": position_value,
                "equity": equity,
                "position_quantity": current_trade.quantity if current_trade is not None else 0,
                "position_active": current_trade is not None,
                "drawdown": drawdown,
                "entry_signal": bool(row.get("entry_signal")),
                "exit_signal": bool(row.get("exit_signal")),
                "risk_line_type": current_trade.risk_line_type
                if current_trade is not None
                else risk_line_type,
                "risk_line_price": current_trade.risk_line_price
                if current_trade is not None
                else None,
                "risk_triggered": bool(current_trade and current_trade.risk_triggered),
                "data_freshness": row.get("feature_freshness_label")
                or row.get("data_category")
                or "",
            }
        )

    # Finalize any remaining signal-close instructions on the last bar.
    last_row = rows[-1]
    last_date = _date_text(last_row.get("date"))
    last_close = _close_price(last_row) or _open_price(last_row) or 0.0
    if pending_risk_exit is not None:
        if pending_risk_exit.get("policy") == "signal_close" and current_trade is not None:
            price, warn = _execute_on_signal_close(
                signal_row=pending_risk_exit["row"],
                side="sell",
                slippage_rate=float(slippage_rate),
            )
            if warn:
                warnings.append(warn)
            if price is not None:
                current_trade.risk_line_type = pending_risk_exit.get("risk_line_type", current_trade.risk_line_type)
                current_trade.risk_line_price = pending_risk_exit.get("risk_line_price", current_trade.risk_line_price)
                current_trade.risk_triggered = True
                current_trade.exit_signal_date = pending_risk_exit["signal_date"]
                exit_costs = _fee_breakdown(
                    side="sell",
                    amount=price * current_trade.quantity,
                    commission_rate=float(commission_rate),
                    rule_config=rule_config,
                )
                current_trade.close(
                    execution_date=last_date,
                    execution_policy="signal_close",
                    exit_price=price,
                    exit_signal="风险线触发",
                    exit_fee=float(exit_costs["total_fee"]),
                    exit_costs=exit_costs,
                )
                cash = round(cash + current_trade.quantity * price - current_trade.exit_fee, 4)
                trades.append(current_trade.to_dict())
                current_trade = None
            else:
                warnings.append("最后一个风险线触发缺少 signal close 价格，已保留虚拟持仓。")
        else:
            warnings.append("最后一个风险线触发无下一条行情，next_open 止损已跳过。")
    if pending_exit is not None:
        if pending_exit.get("policy") == "signal_close" and current_trade is not None:
            price, warn = _execute_on_signal_close(
                signal_row=pending_exit["row"],
                side="sell",
                slippage_rate=float(slippage_rate),
            )
            if warn:
                warnings.append(warn)
            rule_blocked = False
            if price is not None:
                rule_blocked, reason, rule_warnings = _market_rule_block(
                    side="sell",
                    row=pending_exit["row"],
                    execution_price=price,
                    execution_date=last_date,
                    current_trade=current_trade,
                    rule_config=rule_config,
                )
                for item in rule_warnings:
                    _append_unique(warnings, item)
                    _append_unique(market_rule_warnings, item)
                if rule_blocked:
                    blocked_order_count += 1
                    current_trade.exit_blocked_reason = reason
                    current_trade.market_rule_warnings = _dedupe(
                        current_trade.market_rule_warnings + rule_warnings
                    )
                    warnings.append(f"{last_date or '未知日期'} {reason} 已保留虚拟持仓。")
            if price is not None and not rule_blocked:
                current_trade.exit_signal_date = pending_exit["signal_date"]
                exit_costs = _fee_breakdown(
                    side="sell",
                    amount=price * current_trade.quantity,
                    commission_rate=float(commission_rate),
                    rule_config=rule_config,
                )
                current_trade.close(
                    execution_date=last_date,
                    execution_policy="signal_close",
                    exit_price=price,
                    exit_signal=pending_exit.get("label") or exit_label,
                    exit_fee=float(exit_costs["total_fee"]),
                    exit_costs=exit_costs,
                )
                cash = round(cash + current_trade.quantity * price - current_trade.exit_fee, 4)
                trades.append(current_trade.to_dict())
                current_trade = None
            elif price is None:
                warnings.append("最后一个卖出信号缺少 signal close 价格，已保留虚拟持仓。")
        else:
            warnings.append("最后一个卖出信号无下一条行情，next_open 虚拟离场已跳过。")
    if pending_entry is not None:
        if pending_entry.get("policy") == "signal_close" and current_trade is None:
            price, warn = _execute_on_signal_close(
                signal_row=pending_entry["row"],
                side="buy",
                slippage_rate=float(slippage_rate),
            )
            if warn:
                warnings.append(warn)
            rule_blocked = False
            if price is not None:
                rule_blocked, reason, rule_warnings = _market_rule_block(
                    side="buy",
                    row=pending_entry["row"],
                    execution_price=price,
                    execution_date=last_date,
                    current_trade=None,
                    rule_config=rule_config,
                )
                for item in rule_warnings:
                    _append_unique(warnings, item)
                    _append_unique(market_rule_warnings, item)
                if rule_blocked:
                    blocked_order_count += 1
                    warnings.append(f"{last_date or '未知日期'} {reason} 已跳过虚拟建仓。")
            if price is not None and not rule_blocked:
                quantity = _infer_position_size(
                    position_size=position_size,
                    max_position_ratio=float(max_position_ratio),
                    cash=cash,
                    price=price,
                )
                if quantity > 0:
                    if rule_config.get("enforce_lot_size"):
                        normalized = normalize_order_quantity(
                            quantity,
                            price=price,
                            cash=cash,
                            side="buy",
                            lot_size=int(rule_config.get("lot_size") or 100),
                        )
                        if normalized.get("warning"):
                            _append_unique(warnings, str(normalized["warning"]))
                            _append_unique(market_rule_warnings, str(normalized["warning"]))
                        quantity = int(normalized.get("quantity") or 0)
                        valid_quantity = is_valid_order_quantity(
                            quantity,
                            lot_size=int(rule_config.get("lot_size") or 100),
                            board_type=str(rule_config.get("board_type") or "main"),
                        )
                        if quantity <= 0 or not valid_quantity:
                            blocked_order_count += 1
                            warnings.append("最后一个买入信号不足一手板块整手单位，未生成模拟成交。")
                            quantity = 0
                    entry_costs = _fee_breakdown(
                        side="buy",
                        amount=price * quantity,
                        commission_rate=float(commission_rate),
                        rule_config=rule_config,
                    )
                    entry_fee = float(entry_costs["total_fee"])
                    total_cost = round(price * quantity + entry_fee, 4)
                    if quantity > 0 and total_cost <= cash + 1e-9:
                        detail = pending_entry.get("detail") or {}
                        matched, failed, missing, risk_notes = _condition_strings(detail)
                        current_trade = _OpenTrade(
                            trade_id=f"TRD-{len(trades) + 1:04d}",
                            symbol=source_symbol,
                            name=name,
                            strategy_id=strategy_id,
                            strategy_name=strategy_name_label,
                            entry_signal_date=pending_entry["signal_date"],
                            entry_execution_date=last_date,
                            entry_execution_policy="signal_close",
                            entry_price=price,
                            entry_signal=entry_label,
                            quantity=int(quantity),
                            entry_fee=entry_fee,
                            entry_commission=float(entry_costs.get("commission") or 0.0),
                            entry_stamp_duty=float(entry_costs.get("stamp_duty") or 0.0),
                            entry_transfer_fee=float(entry_costs.get("transfer_fee") or 0.0),
                            entry_slippage_fee=float(entry_costs.get("slippage") or 0.0),
                            entry_fee_model=str(entry_costs.get("fee_model") or ""),
                            matched_conditions=matched,
                            failed_conditions=failed,
                            missing_fields=missing,
                            risk_notes=_dedupe(risk_notes),
                            market_regime=str(detail.get("market_regime") or ""),
                            fundamental_status=str(detail.get("fundamental_status") or ""),
                            data_health_status=str(
                                detail.get("data_status")
                                or detail.get("feature_freshness_label")
                                or ""
                            ),
                            risk_line_type=risk_line_type,
                            risk_line_price=None,
                            market_rules=str(rule_config.get("market_rules") or "simple"),
                            fee_model=str(rule_config.get("fee_model") or "simple_commission"),
                            quantity_rule=str(rule_config.get("quantity_rule") or "simple_quantity"),
                            market_rule_warnings=_dedupe(list(market_rule_warnings)),
                        )
                        cash = round(cash - total_cost, 4)
                        highest_since_entry = float(price)
                    else:
                        warnings.append("最后一个买入信号资金不足，未生成模拟成交。")
                else:
                    warnings.append("最后一个买入信号资金不足以建立最小仓位。")
            elif price is None:
                warnings.append("最后一个买入信号缺少 signal close 价格，未生成模拟成交。")
        else:
            warnings.append("最后一个信号日无下一条行情，next_open 买入已跳过。")

    if current_trade is not None:
        current_trade.mark_to_market(mark_date=last_date, mark_price=float(last_close))
        trades.append(current_trade.to_dict())
        current_trade = None

    closed_trades = [trade for trade in trades if trade.get("status") == "closed"]
    open_trades = [trade for trade in trades if trade.get("status") == "open"]
    final_cash = round(cash, 4)
    final_equity = round(
        final_cash
        + sum(float(trade.get("mark_to_market_price") or 0.0) * int(trade.get("quantity") or 0) for trade in open_trades),
        4,
    )
    if not open_trades and equity_curve:
        final_equity = round(float(equity_curve[-1].get("equity") or final_cash), 4)
    total_return = round((final_equity / float(initial_cash) - 1.0) if initial_cash else 0.0, 6)
    closed_returns = [safe_float(trade.get("return_pct"), None) for trade in closed_trades]
    closed_returns = [value for value in closed_returns if value is not None]
    holding_days = [safe_int(trade.get("holding_days"), None) for trade in closed_trades]
    holding_days = [value for value in holding_days if value is not None]
    equity_series = pd.Series([safe_float(row.get("equity"), 0.0) for row in equity_curve], dtype=float)
    max_drawdown = 0.0
    if not equity_series.empty:
        cummax = equity_series.cummax().replace(0, pd.NA)
        drawdowns = equity_series.divide(cummax).fillna(1.0) - 1
        max_drawdown = round(float(drawdowns.min()), 6)
    if signal_count == 0:
        warnings.append("没有买入信号，未生成虚拟交易。")
    if not closed_trades and not open_trades:
        warnings.append("未触发任何策略交易。")
    if ignored_signals:
        warnings.append(
            f"连续或重复信号已忽略 {ignored_signals} 次，单策略单股票仅保留单笔仓位。"
        )
    if open_trades:
        warnings.append("最后一天仍有持仓，已按最后可用 close 估算最终权益。")
    if buy_mode == "next_open" and any("next_open" in item and "不可用" in item for item in warnings):
        warnings.append("买入价格优先使用下一交易日开盘价，缺失时回退到收盘价。")
    if sell_mode == "next_open" and any("next_open" in item and "不可用" in item for item in warnings):
        warnings.append("卖出价格优先使用下一交易日开盘价，缺失时回退到收盘价。")
    assumptions = list(_BASE_ASSUMPTIONS)
    if rule_config.get("market_rules") == "cn_a_share":
        assumptions.append(CN_A_SHARE_ASSUMPTION)

    result = {
        **paper_only_boundary(),
        "replay_mode": TRADE_REPLAY_MODE,
        "replay_mode_label": TRADE_REPLAY_MODE_LABEL,
        "trade_replay_status": TRADE_REPLAY_STATUS,
        "trade_replay_status_label": TRADE_REPLAY_STATUS_LABEL,
        "strategy_id": strategy_id,
        "strategy_name": strategy_name_label,
        "symbol": source_symbol,
        "name": name,
        "data_mode": data_mode,
        "history_source_symbol": str(history_source_symbol or source_symbol).zfill(6),
        "history_source_note": history_source_note,
        "history_rows": int(len(signals)),
        "history_start_date": _date_text(signals.iloc[0].get("date")),
        "history_end_date": _date_text(signals.iloc[-1].get("date")),
        "buy_price_mode": buy_mode,
        "sell_price_mode": sell_mode,
        "market_rules": rule_config.get("market_rules", "simple"),
        "fee_model": rule_config.get("fee_model", "simple_commission"),
        "quantity_rule": rule_config.get("quantity_rule", "simple_quantity"),
        "min_commission": float(rule_config.get("min_commission") or 0.0),
        "stamp_tax_rate": float(rule_config.get("stamp_tax_rate") or 0.0),
        "transfer_fee_rate": float(rule_config.get("transfer_fee_rate") or 0.0),
        "enforce_lot_size": bool(rule_config.get("enforce_lot_size")),
        "lot_size": int(rule_config.get("lot_size") or 1),
        "enforce_t_plus_one": bool(rule_config.get("enforce_t_plus_one")),
        "enforce_price_limit": bool(rule_config.get("enforce_price_limit")),
        "enforce_suspension_check": bool(rule_config.get("enforce_suspension_check")),
        "blocked_order_count": int(blocked_order_count),
        "market_rule_warnings": _dedupe(market_rule_warnings),
        "commission_rate": float(commission_rate),
        "slippage_rate": float(slippage_rate),
        "position_size": position_size,
        "max_position_ratio": float(max_position_ratio),
        "initial_cash": round(float(initial_cash), 4),
        "final_cash": final_cash,
        "final_equity": final_equity,
        "total_return": total_return,
        "max_drawdown": max_drawdown,
        "trade_count": len(trades),
        "closed_trade_count": len(closed_trades),
        "open_trade_count": len(open_trades),
        "win_rate": round(
            len([trade for trade in closed_trades if (safe_float(trade.get("pnl"), 0.0) or 0.0) > 0])
            / len(closed_trades),
            6,
        )
        if closed_trades
        else None,
        "average_trade_return": round(sum(closed_returns) / len(closed_returns), 6) if closed_returns else None,
        "holding_days_avg": round(sum(holding_days) / len(holding_days), 2) if holding_days else None,
        "equity_curve": equity_curve,
        "trades": trades,
        "warnings": _dedupe(warnings),
        "assumptions": _dedupe(assumptions + ([f"买入价格优先使用 {buy_mode}。"] if buy_mode else []) + ([f"卖出价格优先使用 {sell_mode}。"] if sell_mode else [])),
        "strategy_warning": strategy_warning,
        "errors": [],
        "disclaimer": TRADE_REPLAY_DISCLAIMER,
    }
    return result


def strategy_trade_replay_to_dict(result: Mapping[str, Any]) -> dict[str, Any]:
    return dict(result)


__all__ = [
    "TRADE_REPLAY_DISCLAIMER",
    "TRADE_REPLAY_MODE",
    "TRADE_REPLAY_MODE_LABEL",
    "TRADE_REPLAY_STATUS",
    "TRADE_REPLAY_STATUS_LABEL",
    "TradeReplayParameterError",
    "strategy_trade_replay",
    "strategy_trade_replay_to_dict",
]
