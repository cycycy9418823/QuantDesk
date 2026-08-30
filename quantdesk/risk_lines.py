from __future__ import annotations

from typing import Any

from quantdesk.shared import safe_float_zero as safe_float

RISK_LINE_DISCLAIMER = "虚拟风险线仅用于规则失效复核，不代表必须执行的真实价格。"


def build_risk_line(row: dict[str, Any], method: str = "ma20") -> dict[str, Any]:
    close = safe_float(row.get("close") or row.get("price") or row.get("current_price"))
    method = str(method or "ma20")
    line_type = method
    label = "规则失效观察线"
    if method == "atr":
        atr = safe_float(row.get("atr") or row.get("atr14"))
        price = close - atr * 2 if close and atr else 0.0
        label = "ATR 虚拟风险线"
    elif method == "recent_low":
        price = safe_float(row.get("recent_low") or row.get("low_20d") or row.get("recent_low_20"))
        label = "近期低点复核线"
    elif method == "manual":
        price = safe_float(row.get("manual_risk_line") or row.get("risk_line"))
        label = "手动虚拟风险线"
    else:
        price = safe_float(row.get("ma20") or row.get("ma_20") or row.get("moving_average_20"))
        line_type = "ma20"
        label = "20日均线复核线"
    distance = ((close - price) / close * 100.0) if close and price else 0.0
    return {
        "risk_line_price": round(price, 4) if price else 0.0,
        "risk_line_type": line_type,
        "risk_distance_pct": round(distance, 2),
        "risk_line_label": label,
        "explanation": "价格触及该线时只表示观察假设需要重新复核，不代表真实动作。",
        "disclaimer": RISK_LINE_DISCLAIMER,
    }
