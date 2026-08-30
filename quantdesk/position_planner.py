from __future__ import annotations

from typing import Any

from quantdesk.paper.market_rules import round_notional_down_to_lot_quantity
from quantdesk.shared import safe_float_zero as safe_float

POSITION_PLANNER_DISCLAIMER = "虚拟仓位计划仅用于本地模拟验证，不代表真实仓位安排，也不构成投资建议。"


def _lot_quantity(value: float, price: float) -> int:
    if price <= 0 or value <= 0:
        return 0
    return round_notional_down_to_lot_quantity(value, price)


def plan_virtual_position(
    cash: float,
    portfolio_value: float,
    risk_budget_pct: float,
    price: float,
    risk_line: float | None = None,
    *,
    max_single_position_pct: float = 0.10,
    strategy_confidence_level: str = "",
    market_regime: str = "",
    data_complete: bool = True,
) -> dict[str, Any]:
    """Build a conservative paper-only position plan.

    The result is for simulation and manual review only. It never represents a
    real-world order size.
    """
    cash_value = max(safe_float(cash), 0.0)
    equity = max(safe_float(portfolio_value, cash_value), cash_value)
    current_price = safe_float(price)
    risk_price = safe_float(risk_line, 0.0)
    market_pressure = str(market_regime or "").lower() in {"market_pressure", "pressure", "市场环境承压"}
    cap_pct = min(max_single_position_pct, 0.05 if market_pressure else max_single_position_pct)

    if not data_complete:
        return {
            "virtual_position_pct": 0.0,
            "virtual_quantity": 0,
            "risk_amount": 0.0,
            "risk_distance_pct": 0.0,
            "position_label": "仅观察",
            "explanation": "数据不完整，暂不生成虚拟仓位计划；请先完成数据复核。",
            "disclaimer": POSITION_PLANNER_DISCLAIMER,
        }
    if current_price <= 0:
        return {
            "virtual_position_pct": 0.0,
            "virtual_quantity": 0,
            "risk_amount": 0.0,
            "risk_distance_pct": 0.0,
            "position_label": "价格待复核",
            "explanation": "缺少有效价格，无法生成虚拟仓位计划。",
            "disclaimer": POSITION_PLANNER_DISCLAIMER,
        }

    risk_distance_pct = ((current_price - risk_price) / current_price * 100.0) if risk_price > 0 else 0.0
    risk_budget = equity * max(safe_float(risk_budget_pct, 0.01), 0.0)
    by_cap = equity * cap_pct
    by_cash = cash_value
    by_risk = risk_budget / ((current_price - risk_price) / current_price) if risk_price > 0 and current_price > risk_price else by_cap
    planned_value = max(min(by_cap, by_cash, by_risk), 0.0)
    quantity = _lot_quantity(planned_value, current_price)
    planned_value = quantity * current_price
    risk_amount = max(current_price - risk_price, 0.0) * quantity if risk_price > 0 else 0.0
    pct = planned_value / equity if equity else 0.0

    if quantity <= 0:
        label = "资金或风险距离待复核"
    elif market_pressure:
        label = "市场承压下的虚拟轻量计划"
    elif str(strategy_confidence_level or "") in {"深度复核", "条件较好"}:
        label = "虚拟复核计划"
    else:
        label = "虚拟观察计划"

    return {
        "virtual_position_pct": round(pct, 4),
        "virtual_quantity": int(quantity),
        "risk_amount": round(risk_amount, 2),
        "risk_distance_pct": round(risk_distance_pct, 2),
        "position_label": label,
        "explanation": "该数量只用于虚拟组合模拟验证；真实处理必须由用户自行复核。",
        "disclaimer": POSITION_PLANNER_DISCLAIMER,
    }
