"""Conservative position sizing and risk reference for QuantDesk.

All values are conservative references for manual review only.
They do not constitute investment advice and do not auto-execute.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pandas as pd

from quantdesk.paper.market_rules import paper_only_boundary, round_down_to_lot, round_notional_down_to_lot_quantity

from quantdesk.data_quality import DataGrade

DISCLAIMER = (
    "纸面暴露敏感性和风险阈值观察仅供研究复盘，不构成投资建议或真实配置指令。\n"
    "任何真实决策都需人工复核并结合个人风险承受能力。"
)


@dataclass
class PositionReference:
    symbol: str = ""
    reference_price: float = 0.0
    atr14: float = 0.0
    stop_distance: float = 0.0
    stop_price: float = 0.0
    position_pct: float = 0.0
    position_shares: int = 0
    risk_per_trade_pct: float = 0.02
    data_grade: str = ""
    usable: bool = False
    reason: str = ""
    disclaimer: str = DISCLAIMER

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "reference_price": round(self.reference_price, 2),
            "atr14": round(self.atr14, 4),
            "stop_distance": round(self.stop_distance, 4),
            "stop_price": round(self.stop_price, 2),
            "position_pct": round(self.position_pct, 4),
            "position_shares": self.position_shares,
            "risk_per_trade_pct": self.risk_per_trade_pct,
            "data_grade": self.data_grade,
            "usable": self.usable,
            "reason": self.reason,
            "disclaimer": self.disclaimer,
            **paper_only_boundary(),
        }


def compute_position_reference(
    df: pd.DataFrame,
    symbol: str = "",
    total_capital: float = 100000.0,
    risk_per_trade_pct: float = 0.02,
    atr_multiplier: float = 2.0,
    data_grade: str = "",
    max_position_pct: float = 0.10,
) -> PositionReference:
    """Compute conservative ATR-based position sizing reference.

    Returns a PositionReference. Marks ``usable=False`` if data
    grade is too low or ATR cannot be computed.
    """
    ref = PositionReference(symbol=symbol, data_grade=data_grade, risk_per_trade_pct=risk_per_trade_pct)

    grade_order = [g.value for g in DataGrade]
    min_grade = DataGrade.SCORABLE.value
    if data_grade in grade_order and grade_order.index(data_grade) < grade_order.index(min_grade):
        ref.reason = f"数据等级 {data_grade} 不足以计算纸面暴露情景"
        return ref

    if df is None or df.empty or len(df) < 14:
        ref.reason = "数据行数不足14行，无法计算 ATR"
        return ref

    close = pd.to_numeric(df.get("close"), errors="coerce") if "close" in df.columns else None
    high = pd.to_numeric(df.get("high"), errors="coerce") if "high" in df.columns else None
    low = pd.to_numeric(df.get("low"), errors="coerce") if "low" in df.columns else None
    if close is None or high is None or low is None:
        ref.reason = "缺少 close/high/low 列"
        return ref

    prev_close = close.shift(1)
    tr = pd.concat([
        (high - low).abs(),
        (high - prev_close).abs(),
        (low - prev_close).abs(),
    ], axis=1).max(axis=1)
    atr14 = float(tr.tail(14).mean())

    current_price = float(close.iloc[-1])
    if pd.isna(current_price) or current_price <= 0 or pd.isna(atr14) or atr14 <= 0:
        ref.reason = "当前价格或 ATR 无效"
        return ref

    stop_distance = atr14 * atr_multiplier
    stop_price = current_price - stop_distance

    risk_amount = total_capital * risk_per_trade_pct
    if stop_distance > 0:
        max_shares_by_risk = round_notional_down_to_lot_quantity(risk_amount, stop_distance)
    else:
        max_shares_by_risk = 0

    max_shares_by_cap = round_notional_down_to_lot_quantity(total_capital * max_position_pct, current_price)
    shares = round_down_to_lot(min(max_shares_by_risk, max_shares_by_cap))
    if shares <= 0:
        ref.reference_price = current_price
        ref.atr14 = atr14
        ref.stop_distance = stop_distance
        ref.stop_price = max(stop_price, 0.0)
        ref.position_pct = 0.0
        ref.position_shares = 0
        ref.usable = False
        ref.reason = "可用资金与风险预算不足一整手，纸面数量保持为 0。"
        return ref

    position_pct = (shares * current_price) / total_capital if total_capital > 0 else 0.0

    ref.reference_price = current_price
    ref.atr14 = atr14
    ref.stop_distance = stop_distance
    ref.stop_price = max(stop_price, 0.0)
    ref.position_pct = min(position_pct, max_position_pct)
    ref.position_shares = shares
    ref.usable = True
    ref.reason = "ATR 纸面暴露情景已计算"
    return ref
