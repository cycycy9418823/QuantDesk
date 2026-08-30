"""事前风控门控 (Pre-Trade Risk Gate)。

在组合回测的交易执行前进行检查，主动阻止不合规的交易请求，
而不是在事后才发出警告。

检查项：
1. 单只股票仓位上限
2. 行业集中度上限
3. 总持仓数量上限
4. 单次换手率上限
5. 最大杠杆/总仓位上限

仅用于本地研究回测模拟，不构成投资建议。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from quantdesk.paper.market_rules import paper_only_boundary

# ──────────────────────────────────────────────────────────────────────────────
# 常量
# ──────────────────────────────────────────────────────────────────────────────

PRE_TRADE_GATE_DISCLAIMER = "事前风控仅为简化规则检查，不代表完整风险管理体系。"


# ──────────────────────────────────────────────────────────────────────────────
# 配置
# ──────────────────────────────────────────────────────────────────────────────

@dataclass
class PreTradeGateConfig:
    """事前风控门控参数。"""

    max_position_pct: float = 0.20       # 单只股票最大仓位 20%
    max_industry_pct: float = 0.40       # 单一行业最大仓位 40%
    max_positions: int = 10              # 最大持仓股票数
    max_turnover_per_rebalance: float = 0.50  # 单次再平衡最大换手率 50%
    max_total_exposure: float = 1.0      # 最大总仓位比例 100%（无杠杆）
    enforce: bool = True                 # 是否强制阻止（False 则只记录警告）

    def to_dict(self) -> dict[str, Any]:
        return {
            "max_position_pct": self.max_position_pct,
            "max_industry_pct": self.max_industry_pct,
            "max_positions": self.max_positions,
            "max_turnover_per_rebalance": self.max_turnover_per_rebalance,
            "max_total_exposure": self.max_total_exposure,
            "enforce": self.enforce,
        }


# ──────────────────────────────────────────────────────────────────────────────
# 检查结果
# ──────────────────────────────────────────────────────────────────────────────

@dataclass
class PreTradeCheckResult:
    """单次事前检查的结果。"""

    passed: bool
    check_name: str                      # 检查项名称
    blocked_reason: str = ""             # 阻止原因
    current_value: float = 0.0           # 当前值
    threshold_value: float = 0.0         # 阈值
    warning: str = ""                    # 附加警告

    def to_dict(self) -> dict[str, Any]:
        return {
            "passed": self.passed,
            "check_name": self.check_name,
            "blocked_reason": self.blocked_reason,
            "current_value": round(self.current_value, 6),
            "threshold_value": round(self.threshold_value, 6),
            "warning": self.warning,
            **paper_only_boundary(),
        }


# ──────────────────────────────────────────────────────────────────────────────
# 核心检查函数
# ──────────────────────────────────────────────────────────────────────────────

def check_position_size(
    *,
    symbol: str,
    order_amount: float,
    current_equity: float,
    config: PreTradeGateConfig,
    current_positions: dict[str, dict[str, Any]] | None = None,
) -> PreTradeCheckResult:
    """检查买入后单只股票仓位是否超限。"""
    if current_equity <= 0:
        return PreTradeCheckResult(
            passed=True,
            check_name="position_size",
            warning="当前权益为 0 或负数，跳过仓位检查。",
        )
    normalized = str(symbol or "").strip()
    existing_weight = 0.0
    existing_amount = 0.0
    for sym, position in (current_positions or {}).items():
        if str(sym or "").strip() != normalized:
            continue
        existing_weight = float(position.get("weight") or 0.0)
        existing_amount = float(position.get("market_value") or position.get("amount") or 0.0)
        break
    existing_exposure = existing_amount / current_equity if existing_amount > 0 else existing_weight
    weight_after = existing_exposure + order_amount / current_equity
    if weight_after > config.max_position_pct:
        return PreTradeCheckResult(
            passed=not config.enforce,
            check_name="position_size",
            blocked_reason=f"买入后 {symbol} 累计仓位 {weight_after:.2%} 超过上限 {config.max_position_pct:.2%}",
            current_value=weight_after,
            threshold_value=config.max_position_pct,
        )
    return PreTradeCheckResult(
        passed=True,
        check_name="position_size",
        current_value=weight_after,
        threshold_value=config.max_position_pct,
    )


def check_industry_concentration(
    *,
    industry: str,
    order_amount: float,
    current_equity: float,
    current_positions: dict[str, dict[str, Any]],
    industry_map: dict[str, str] | None = None,
    config: PreTradeGateConfig | None = None,
) -> PreTradeCheckResult:
    """检查买入后行业集中度是否超限。

    Parameters
    ----------
    industry : str
        待买入股票的行业。
    order_amount : float
        本次买入金额。
    current_equity : float
        当前组合总权益。
    current_positions : dict
        当前持仓 {symbol: {"weight": w, "industry": "xxx"}}。
    industry_map : dict | None
        symbol -> industry 映射（可选）。
    config : PreTradeGateConfig | None
        配置。
    """
    cfg = config or PreTradeGateConfig()
    if current_equity <= 0:
        return PreTradeCheckResult(
            passed=True,
            check_name="industry_concentration",
            warning="当前权益为 0，跳过行业集中度检查。",
        )

    # 计算当前行业权重
    current_industry_weight = 0.0
    for sym, pos in current_positions.items():
        pos_industry = str(pos.get("industry") or (industry_map or {}).get(sym, "unknown"))
        if pos_industry == industry:
            current_industry_weight += float(pos.get("weight", 0))

    weight_after = current_industry_weight + order_amount / current_equity
    if weight_after > cfg.max_industry_pct:
        return PreTradeCheckResult(
            passed=not cfg.enforce,
            check_name="industry_concentration",
            blocked_reason=f"买入后行业 {industry} 仓位 {weight_after:.2%} 超过上限 {cfg.max_industry_pct:.2%}",
            current_value=weight_after,
            threshold_value=cfg.max_industry_pct,
        )
    return PreTradeCheckResult(
        passed=True,
        check_name="industry_concentration",
        current_value=weight_after,
        threshold_value=cfg.max_industry_pct,
    )


def check_max_positions(
    *,
    current_position_count: int,
    symbol: str,
    current_positions: dict[str, Any] | None,
    config: PreTradeGateConfig,
) -> PreTradeCheckResult:
    """检查持仓数量是否已达上限。"""
    # 如果是已有持仓的加仓，不增加持仓数
    if current_positions and symbol in current_positions:
        return PreTradeCheckResult(
            passed=True,
            check_name="max_positions",
            current_value=float(current_position_count),
            threshold_value=float(config.max_positions),
        )
    if current_position_count >= config.max_positions:
        return PreTradeCheckResult(
            passed=not config.enforce,
            check_name="max_positions",
            blocked_reason=f"当前持仓数 {current_position_count} 已达上限 {config.max_positions}",
            current_value=float(current_position_count),
            threshold_value=float(config.max_positions),
        )
    return PreTradeCheckResult(
        passed=True,
        check_name="max_positions",
        current_value=float(current_position_count),
        threshold_value=float(config.max_positions),
    )


def check_turnover_limit(
    *,
    turnover_used: float,
    order_amount: float,
    current_equity: float,
    config: PreTradeGateConfig,
) -> PreTradeCheckResult:
    """检查单次再平衡换手率是否超限。"""
    if current_equity <= 0:
        return PreTradeCheckResult(
            passed=True,
            check_name="turnover_limit",
            warning="当前权益为 0，跳过换手率检查。",
        )
    turnover_after = turnover_used + order_amount / current_equity
    if turnover_after > config.max_turnover_per_rebalance:
        return PreTradeCheckResult(
            passed=not config.enforce,
            check_name="turnover_limit",
            blocked_reason=f"本次再平衡换手率 {turnover_after:.2%} 超过上限 {config.max_turnover_per_rebalance:.2%}",
            current_value=turnover_after,
            threshold_value=config.max_turnover_per_rebalance,
        )
    return PreTradeCheckResult(
        passed=True,
        check_name="turnover_limit",
        current_value=turnover_after,
        threshold_value=config.max_turnover_per_rebalance,
    )


def check_total_exposure(
    *,
    order_amount: float,
    current_equity: float,
    current_market_value: float,
    config: PreTradeGateConfig,
) -> PreTradeCheckResult:
    """检查总仓位比例是否超过上限（无杠杆检查）。"""
    if current_equity <= 0:
        return PreTradeCheckResult(
            passed=True,
            check_name="total_exposure",
            warning="当前权益为 0，跳过总仓位检查。",
        )
    exposure_after = (current_market_value + order_amount) / current_equity
    if exposure_after > config.max_total_exposure:
        return PreTradeCheckResult(
            passed=not config.enforce,
            check_name="total_exposure",
            blocked_reason=f"买入后总仓位 {exposure_after:.2%} 超过上限 {config.max_total_exposure:.2%}",
            current_value=exposure_after,
            threshold_value=config.max_total_exposure,
        )
    return PreTradeCheckResult(
        passed=True,
        check_name="total_exposure",
        current_value=exposure_after,
        threshold_value=config.max_total_exposure,
    )


# ──────────────────────────────────────────────────────────────────────────────
# 聚合检查
# ──────────────────────────────────────────────────────────────────────────────

def run_pre_trade_checks(
    *,
    symbol: str,
    order_amount: float,
    industry: str = "unknown",
    current_equity: float,
    current_market_value: float = 0.0,
    current_positions: dict[str, dict[str, Any]] | None = None,
    current_position_count: int = 0,
    turnover_used: float = 0.0,
    industry_map: dict[str, str] | None = None,
    config: PreTradeGateConfig | None = None,
) -> tuple[bool, list[PreTradeCheckResult]]:
    """运行全部事前风控检查。

    Returns
    -------
    tuple[bool, list[PreTradeCheckResult]]
        (all_passed, check_results)
    """
    cfg = config or PreTradeGateConfig()
    positions = current_positions or {}
    results: list[PreTradeCheckResult] = []

    # 1. 持仓数量
    results.append(check_max_positions(
        current_position_count=current_position_count,
        symbol=symbol,
        current_positions=positions,
        config=cfg,
    ))

    # 2. 单只仓位
    results.append(check_position_size(
        symbol=symbol,
        order_amount=order_amount,
        current_equity=current_equity,
        config=cfg,
        current_positions=positions,
    ))

    # 3. 行业集中度
    results.append(check_industry_concentration(
        industry=industry,
        order_amount=order_amount,
        current_equity=current_equity,
        current_positions=positions,
        industry_map=industry_map,
        config=cfg,
    ))

    # 4. 换手率
    results.append(check_turnover_limit(
        turnover_used=turnover_used,
        order_amount=order_amount,
        current_equity=current_equity,
        config=cfg,
    ))

    # 5. 总仓位
    results.append(check_total_exposure(
        order_amount=order_amount,
        current_equity=current_equity,
        current_market_value=current_market_value,
        config=cfg,
    ))

    all_passed = all(r.passed for r in results)
    return all_passed, results


__all__ = [
    "PRE_TRADE_GATE_DISCLAIMER",
    "PreTradeCheckResult",
    "PreTradeGateConfig",
    "check_industry_concentration",
    "check_max_positions",
    "check_position_size",
    "check_total_exposure",
    "check_turnover_limit",
    "run_pre_trade_checks",
]
