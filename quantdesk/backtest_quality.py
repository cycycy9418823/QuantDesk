"""Backtest quality scoring for QuantDesk walk-forward results.

Quality grades reflect data completeness and out-of-sample stability,
NOT future profitability. HIGH does not mean profitable; it means the
backtest process itself is more trustworthy as a research reference.

This module does not constitute investment advice.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from quantdesk.backtest_metrics import WalkForwardSummary

QUALITY_GRADES = ["INVALID", "LOW", "MEDIUM", "HIGH"]

DISCLAIMER = (
    "回测质量评分仅反映数据完整性和样本外稳定性，不代表策略盈利能力，"
    "不构成投资建议，不承诺收益，不自动下单。"
)


@dataclass
class BacktestQuality:
    quality_grade: str = "INVALID"
    score: int = 0
    reasons: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    allowed_for_research: bool = False
    allowed_for_trade_plan: bool = False
    disclaimer: str = DISCLAIMER

    def to_dict(self) -> dict[str, Any]:
        return {
            "quality_grade": self.quality_grade,
            "score": self.score,
            "reasons": list(self.reasons),
            "warnings": list(self.warnings),
            "allowed_for_research": self.allowed_for_research,
            "allowed_for_trade_plan": self.allowed_for_trade_plan,
            "disclaimer": self.disclaimer,
        }


def evaluate_backtest_quality(summary: WalkForwardSummary) -> BacktestQuality:
    """Evaluate the quality/trustworthiness of a walk-forward result."""
    score = 100
    reasons: list[str] = []
    warnings: list[str] = []

    if summary.total_windows == 0:
        return BacktestQuality(
            quality_grade="INVALID",
            score=0,
            reasons=["没有有效的测试窗口。"],
        )

    if summary.total_trades < 10:
        score -= 50
        reasons.append(f"总交易次数仅 {summary.total_trades} 笔，不足以做统计判断。")
    elif summary.total_trades < 30:
        score -= 25
        reasons.append(f"总交易次数 {summary.total_trades} 笔，统计可信度有限。")
    elif summary.total_trades < 100:
        score -= 10
        warnings.append(f"总交易次数 {summary.total_trades} 笔，尚可但不充分。")

    if summary.total_windows < 3:
        score -= 20
        reasons.append(f"测试窗口仅 {summary.total_windows} 个，样本外验证不充分。")

    if summary.negative_windows > summary.positive_windows:
        score -= 25
        reasons.append("负收益窗口多于正收益窗口，策略样本外表现不稳定。")
    elif summary.negative_windows > 0 and summary.positive_windows > 0:
        ratio = summary.negative_windows / summary.total_windows
        if ratio > 0.4:
            score -= 15
            warnings.append(f"负收益窗口占比 {ratio:.0%}，稳定性一般。")

    consecutive_neg = 0
    max_consecutive_neg = 0
    for w in summary.windows:
        if w.annual_return <= 0:
            consecutive_neg += 1
            max_consecutive_neg = max(max_consecutive_neg, consecutive_neg)
        else:
            consecutive_neg = 0
    if max_consecutive_neg >= 3:
        score -= 20
        reasons.append(f"连续 {max_consecutive_neg} 个窗口为负收益。")
    elif max_consecutive_neg >= 2:
        score -= 10
        warnings.append(f"连续 {max_consecutive_neg} 个窗口为负收益。")

    if summary.worst_max_drawdown > 0.5:
        score -= 25
        reasons.append(f"最大回撤 {summary.worst_max_drawdown:.1%}，风险过高。")
    elif summary.worst_max_drawdown > 0.3:
        score -= 15
        warnings.append(f"最大回撤 {summary.worst_max_drawdown:.1%}，较高。")

    if summary.aggregate_sharpe < 0:
        score -= 20
        reasons.append(f"Sharpe 为负 ({summary.aggregate_sharpe:.2f})，策略表现差于无风险。")
    elif summary.aggregate_sharpe < 0.5:
        score -= 10
        warnings.append(f"Sharpe {summary.aggregate_sharpe:.2f}，偏低。")

    returns = [w.annual_return for w in summary.windows]
    positive_returns = [r for r in returns if r > 0]
    if len(positive_returns) >= 2:
        best = max(positive_returns)
        total_positive = sum(positive_returns)
        if total_positive > 0 and best / total_positive > 0.5:
            score -= 15
            warnings.append("单一窗口贡献超过 50% 的正收益，存在集中度风险。")

    if summary.aggregate_win_rate < 0.3 and summary.total_trades >= 10:
        score -= 15
        reasons.append(f"总体胜率仅 {summary.aggregate_win_rate:.0%}。")

    windows_with_few_trades = sum(1 for w in summary.windows if w.trades_count < 3)
    if windows_with_few_trades > summary.total_windows * 0.5:
        score -= 15
        warnings.append("超过半数窗口交易次数不足3笔。")

    score = max(0, min(100, score))

    if score >= 70:
        grade = "HIGH"
    elif score >= 45:
        grade = "MEDIUM"
    elif score >= 20:
        grade = "LOW"
    else:
        grade = "INVALID"

    return BacktestQuality(
        quality_grade=grade,
        score=score,
        reasons=reasons,
        warnings=warnings + list(summary.warnings),
        allowed_for_research=grade in ("HIGH", "MEDIUM", "LOW"),
        allowed_for_trade_plan=grade in ("HIGH", "MEDIUM"),
    )
