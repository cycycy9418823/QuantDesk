"""流动性感知滑点模型 (Liquidity-Aware Slippage Model)。

替换固定 5bps 线性滑点，根据订单金额相对日成交额的比例动态计算滑点。

公式：
    slippage_bps = base_bps + impact_coefficient * (order_amount / daily_turnover) ^ impact_power

该模型仅用于本地研究回测模拟，不构成投资建议。
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

# ──────────────────────────────────────────────────────────────────────────────
# 常量
# ──────────────────────────────────────────────────────────────────────────────

DEFAULT_BASE_BPS = 3.0          # 基础滑点 3bps（市场微观结构噪声）
DEFAULT_IMPACT_COEFFICIENT = 30.0  # 冲击系数（经验值，A股约 30bps 起点）
DEFAULT_IMPACT_POWER = 0.5       # 冲击指数（平方根模型，Almgren-Chriss 经验）
DEFAULT_MAX_SLIPPAGE_BPS = 200.0  # 滑点上限 200bps（2%），防止极端失真
DEFAULT_MIN_DAILY_AMOUNT = 1e6   # 日成交额下限 100万元，低于此值视为极低流动性

SLIPPAGE_DISCLAIMER = "滑点模型仅基于经验参数近似，不代表真实订单簿行为。"


# ──────────────────────────────────────────────────────────────────────────────
# 配置
# ──────────────────────────────────────────────────────────────────────────────

@dataclass
class SlippageModelConfig:
    """滑点模型配置参数。"""

    base_bps: float = DEFAULT_BASE_BPS
    impact_coefficient: float = DEFAULT_IMPACT_COEFFICIENT
    impact_power: float = DEFAULT_IMPACT_POWER
    max_slippage_bps: float = DEFAULT_MAX_SLIPPAGE_BPS
    min_daily_amount: float = DEFAULT_MIN_DAILY_AMOUNT

    def to_dict(self) -> dict[str, Any]:
        return {
            "model": "liquidity_aware",
            "base_bps": self.base_bps,
            "impact_coefficient": self.impact_coefficient,
            "impact_power": self.impact_power,
            "max_slippage_bps": self.max_slippage_bps,
            "min_daily_amount": self.min_daily_amount,
        }


# ──────────────────────────────────────────────────────────────────────────────
# 核心计算
# ──────────────────────────────────────────────────────────────────────────────

def compute_slippage_bps(
    *,
    order_amount: float,
    daily_amount: float,
    config: SlippageModelConfig | None = None,
) -> float:
    """计算流动性感知滑点（单位：基点 bps）。

    Parameters
    ----------
    order_amount : float
        本次订单金额（股数 × 执行价格），单位元。
    daily_amount : float
        当日该股票的总成交金额，单位元。
    config : SlippageModelConfig | None
        模型参数，默认使用内置经验值。

    Returns
    -------
    float
        滑点 bps，范围 [base_bps, max_slippage_bps]。
        若输入不合法（order_amount <= 0 或 daily_amount <= 0），返回保守上限。
    """
    cfg = config or SlippageModelConfig()

    if not math.isfinite(order_amount) or order_amount <= 0:
        return cfg.max_slippage_bps

    effective_daily = daily_amount if (math.isfinite(daily_amount) and daily_amount > 0) else cfg.min_daily_amount
    effective_daily = max(effective_daily, cfg.min_daily_amount)

    # 市场参与率 = 订单金额 / 日成交额
    participation_rate = order_amount / effective_daily

    # 冲击项：sqrt 模型
    impact = cfg.impact_coefficient * (participation_rate ** cfg.impact_power)

    total_bps = cfg.base_bps + impact
    return float(min(total_bps, cfg.max_slippage_bps))


def compute_executed_price(
    *,
    requested_price: float,
    side: str,
    order_amount: float,
    daily_amount: float,
    config: SlippageModelConfig | None = None,
) -> tuple[float, float, str]:
    """计算执行价格和滑点成本。

    Parameters
    ----------
    requested_price : float
        请求价格（信号日 next_open 或 close）。
    side : str
        "BUY" 或 "SELL"。
    order_amount : float
        订单金额（shares × requested_price）。
    daily_amount : float
        当日总成交额。
    config : SlippageModelConfig | None
        模型参数。

    Returns
    -------
    tuple[float, float, str]
        (executed_price, slippage_bps, warning)
        warning 为空字符串表示无异常。
    """
    cfg = config or SlippageModelConfig()
    warning = ""

    if not math.isfinite(requested_price) or requested_price <= 0:
        return requested_price, cfg.max_slippage_bps, "requested_price 无效，无法计算滑点。"

    bps = compute_slippage_bps(
        order_amount=order_amount,
        daily_amount=daily_amount,
        config=cfg,
    )

    if side == "BUY":
        executed_price = requested_price * (1.0 + bps / 10_000)
    else:
        executed_price = requested_price * (1.0 - bps / 10_000)

    if not math.isfinite(daily_amount) or daily_amount <= 0:
        warning = "日成交额不可用，已使用保守下限，滑点可能偏高。"
    elif order_amount / max(daily_amount, 1) > 0.05:
        warning = f"订单参与率 {order_amount / daily_amount:.2%} 超过 5%，滑点可能低估真实冲击。"

    return float(executed_price), float(bps), warning


def fixed_slippage_bps(bps: float) -> SlippageModelConfig:
    """创建固定滑点配置，兼容旧版行为。

    当用户明确指定 slippage_bps=N 时，可用此配置退化为固定模式。
    """
    return SlippageModelConfig(
        base_bps=bps,
        impact_coefficient=0.0,
        impact_power=0.0,
        max_slippage_bps=bps,
        min_daily_amount=1.0,
    )
