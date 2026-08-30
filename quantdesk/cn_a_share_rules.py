"""Simplified CN A-share market rules for local trade replay.

These helpers are intentionally small and deterministic.  They approximate a
few common A-share constraints for historical simulation only; they do not
connect to brokers, do not submit orders, and are not a complete exchange rule
engine.
"""

from __future__ import annotations

from datetime import date
from typing import Any, Mapping

import pandas as pd

from quantdesk.utils import safe_float, safe_int

CN_A_SHARE_ASSUMPTION = (
    "cn_a_share 模式启用简化 A 股规则：板块整手单位、T+1、简化费用、涨跌停和疑似停牌基础判断。"
    "该规则层仅用于本地历史模拟，不代表交易所、券商或数据源的完整真实规则。"
)


def normalize_order_quantity(
    quantity: float | int | None,
    *,
    price: float,
    cash: float | None = None,
    side: str = "buy",
    lot_size: int = 100,
) -> dict[str, Any]:
    """Normalize order quantity to A-share board lots for buys.

    Buys are rounded down to a 100-share lot by default.  Sells default to the
    supplied quantity so that a current virtual position can be fully closed.
    """

    parsed = safe_int(quantity, 0) or 0
    lot = max(int(lot_size or 100), 1)
    if parsed <= 0:
        return {
            "quantity": 0,
            "blocked": True,
            "reason": "数量为空或小于等于 0。",
            "warning": "A 股规则：数量不足，无法生成模拟成交。",
        }
    if side == "sell":
        return {"quantity": parsed, "blocked": False, "reason": "", "warning": ""}

    normalized = int(parsed // lot * lot)
    if normalized <= 0:
        return {
            "quantity": 0,
            "blocked": True,
            "reason": f"买入数量不足 {lot} 股整数手。",
            "warning": f"A 股规则：买入数量不足 {lot} 股整数手，已阻止该笔模拟成交。",
        }
    if cash is not None and price > 0 and normalized * price > float(cash) + 1e-9:
        affordable = int(float(cash) // price // lot * lot)
        if affordable <= 0:
            return {
                "quantity": 0,
                "blocked": True,
                "reason": f"可用资金不足以买入 {lot} 股。",
                "warning": f"A 股规则：资金不足以买入 {lot} 股整数手，已阻止该笔模拟成交。",
            }
        normalized = affordable
    return {
        "quantity": normalized,
        "blocked": False,
        "reason": "",
        "warning": "" if normalized == parsed else f"A 股规则：买入数量已向下取整到 {lot} 股整数手。",
    }


def calculate_cn_a_share_fees(
    *,
    side: str,
    amount: float,
    commission_rate: float = 0.00025,
    min_commission: float = 5.0,
    stamp_tax_rate: float = 0.0005,
    transfer_fee_rate: float = 0.0,
    apply_min_commission: bool = True,
) -> dict[str, Any]:
    """Calculate a simplified A-share fee model."""

    gross = max(float(amount or 0.0), 0.0)
    commission = gross * max(float(commission_rate or 0.0), 0.0)
    if apply_min_commission and gross > 0:
        commission = max(commission, max(float(min_commission or 0.0), 0.0))
    transfer_fee = gross * max(float(transfer_fee_rate or 0.0), 0.0)
    stamp_tax = gross * max(float(stamp_tax_rate or 0.0), 0.0) if side == "sell" else 0.0
    total = commission + transfer_fee + stamp_tax
    return {
        "total_fee": round(total, 4),
        "commission": round(commission, 4),
        "stamp_tax": round(stamp_tax, 4),
        "transfer_fee": round(transfer_fee, 4),
        "fee_model": "cn_a_share_simplified",
    }


def _boolish(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    text = str(value or "").strip().lower()
    return text in {"1", "true", "yes", "y", "是", "涨停", "跌停", "停牌"}


def detect_limit_up_down(row: Mapping[str, Any], *, side: str) -> dict[str, Any]:
    """Detect simple limit-up/down blocks from explicit fields when available."""

    if _boolish(row.get("is_limit_up")) or _boolish(row.get("limit_up_hit")):
        if side == "buy":
            return {
                "status": "limit_up",
                "blocked": True,
                "reason": "触及涨停，买入方向模拟成交被阻止。",
                "warning": "A 股规则：触及涨停，已阻止虚拟建仓模拟成交。",
            }
    if _boolish(row.get("is_limit_down")) or _boolish(row.get("limit_down_hit")):
        if side == "sell":
            return {
                "status": "limit_down",
                "blocked": True,
                "reason": "触及跌停，卖出方向模拟成交被阻止。",
                "warning": "A 股规则：触及跌停，已阻止虚拟离场模拟成交。",
            }

    close = safe_float(row.get("close"), None)
    limit_up = safe_float(row.get("limit_up"), None)
    limit_down = safe_float(row.get("limit_down"), None)
    if close is None:
        return {
            "status": "unknown",
            "blocked": False,
            "reason": "缺少 close，无法判断涨跌停。",
            "warning": "A 股规则：缺少 close，涨跌停判断为 unknown。",
        }
    if limit_up is not None and side == "buy" and close >= limit_up - 1e-9:
        return {
            "status": "limit_up",
            "blocked": True,
            "reason": "触及涨停，买入方向模拟成交被阻止。",
            "warning": "A 股规则：触及涨停，已阻止虚拟建仓模拟成交。",
        }
    if limit_down is not None and side == "sell" and close <= limit_down + 1e-9:
        return {
            "status": "limit_down",
            "blocked": True,
            "reason": "触及跌停，卖出方向模拟成交被阻止。",
            "warning": "A 股规则：触及跌停，已阻止虚拟离场模拟成交。",
        }
    if limit_up is None and limit_down is None:
        return {
            "status": "unknown",
            "blocked": False,
            "reason": "缺少 limit_up / limit_down 字段。",
            "warning": "A 股规则：缺少涨跌停价字段，涨跌停判断为 unknown。",
        }
    return {"status": "normal", "blocked": False, "reason": "", "warning": ""}


def detect_suspension(row: Mapping[str, Any]) -> dict[str, Any]:
    """Detect a conservative suspected suspension / non-tradable bar."""

    if _boolish(row.get("is_suspended")) or _boolish(row.get("suspended")):
        return {
            "tradable": False,
            "status": "suspended",
            "reason": "字段标记为停牌。",
            "warning": "A 股规则：该日疑似停牌，已阻止模拟成交。",
        }
    required_prices = ["open", "high", "low", "close"]
    missing_prices = [key for key in required_prices if safe_float(row.get(key), None) is None]
    volume = safe_float(row.get("volume"), None)
    if missing_prices:
        return {
            "tradable": False,
            "status": "unknown",
            "reason": "缺少 open/high/low/close 字段。",
            "warning": "A 股规则：行情字段不完整，疑似不可成交，已阻止模拟成交。",
        }
    if volume is not None and volume <= 0:
        return {
            "tradable": False,
            "status": "zero_volume",
            "reason": "成交量为 0。",
            "warning": "A 股规则：成交量为 0，疑似停牌或不可成交，已阻止模拟成交。",
        }
    return {"tradable": True, "status": "tradable", "reason": "", "warning": ""}


def check_t_plus_one_sell_allowed(
    *,
    entry_execution_date: str | date | None,
    current_execution_date: str | date | None,
) -> dict[str, Any]:
    """Check simplified A-share T+1 sell constraint."""

    try:
        entry = pd.to_datetime(entry_execution_date, errors="coerce")
        current = pd.to_datetime(current_execution_date, errors="coerce")
    except Exception:
        entry = pd.NaT
        current = pd.NaT
    if pd.isna(entry) or pd.isna(current):
        return {
            "allowed": True,
            "status": "unknown",
            "reason": "日期不可解析，T+1 判断为 unknown。",
            "warning": "A 股规则：日期不可解析，T+1 判断为 unknown。",
        }
    if entry.date() == current.date():
        return {
            "allowed": False,
            "status": "blocked",
            "reason": "买入当天不允许卖出。",
            "warning": "A 股规则：T+1 限制阻止同日虚拟离场模拟成交。",
        }
    return {"allowed": True, "status": "allowed", "reason": "", "warning": ""}

