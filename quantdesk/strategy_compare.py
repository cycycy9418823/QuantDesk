"""Strategy comparison dashboard — compare multiple strategies side-by-side.

Compares walk-forward results across strategies on the same symbol set and
date range, producing a ranked table of metrics per strategy and per market
regime window.

All results are for local research and simulation review only.
They do not constitute investment advice.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from quantdesk.backtest_metrics import (
    WalkForwardSummary,
    WindowMetrics,
)
from quantdesk.backtest_quality import BacktestQuality, evaluate_backtest_quality

COMPARE_DISCLAIMER = (
    "策略对比仅用于本地研究参考，历史表现不代表未来收益，"
    "不构成投资建议，不自动下单。"
)


@dataclass
class StrategyCompareRow:
    """One row in the comparison table."""

    strategy_id: str = ""
    strategy_name: str = ""
    total_windows: int = 0
    positive_windows: int = 0
    negative_windows: int = 0
    total_trades: int = 0
    average_annual_return: float = 0.0
    median_annual_return: float = 0.0
    worst_max_drawdown: float = 0.0
    aggregate_sharpe: float = 0.0
    aggregate_win_rate: float = 0.0
    profit_factor: float = 0.0
    quality_grade: str = "INVALID"
    quality_score: int = 0
    regime_performance: dict[str, float] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "strategy_id": self.strategy_id,
            "strategy_name": self.strategy_name,
            "total_windows": self.total_windows,
            "positive_windows": self.positive_windows,
            "negative_windows": self.negative_windows,
            "total_trades": self.total_trades,
            "average_annual_return": round(self.average_annual_return, 6),
            "median_annual_return": round(self.median_annual_return, 6),
            "worst_max_drawdown": round(self.worst_max_drawdown, 6),
            "aggregate_sharpe": round(self.aggregate_sharpe, 4),
            "aggregate_win_rate": round(self.aggregate_win_rate, 4),
            "profit_factor": round(self.profit_factor, 4),
            "quality_grade": self.quality_grade,
            "quality_score": self.quality_score,
            "regime_performance": {k: round(v, 6) for k, v in self.regime_performance.items()},
            "warnings": self.warnings,
        }


@dataclass
class StrategyCompareResult:
    """Full comparison result."""

    rows: list[StrategyCompareRow] = field(default_factory=list)
    ranked_by: str = "aggregate_sharpe"
    disclaimer: str = COMPARE_DISCLAIMER
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "rows": [r.to_dict() for r in self.rows],
            "ranked_by": self.ranked_by,
            "best_strategy": self.rows[0].strategy_id if self.rows else "",
            "disclaimer": self.disclaimer,
            "warnings": self.warnings,
        }


def _classify_window_regime(wm: WindowMetrics) -> str:
    """Classify a window as bull / bear / sideways based on annual return."""
    if wm.annual_return > 0.05:
        return "bull"
    elif wm.annual_return < -0.05:
        return "bear"
    return "sideways"


def _aggregate_profit_factor(summary: WalkForwardSummary) -> float:
    """Derive profit factor from window-level data."""
    total_win = 0.0
    total_loss = 0.0
    for w in (summary.windows or []):
        if w.annual_return > 0:
            total_win += w.annual_return
        else:
            total_loss += abs(w.annual_return)
    if total_loss == 0:
        return 99.0 if total_win > 0 else 0.0
    return round(total_win / total_loss, 4)


def build_compare_row(
    strategy_id: str,
    strategy_name: str,
    summary: WalkForwardSummary,
    quality: BacktestQuality | None = None,
) -> StrategyCompareRow:
    """Build one comparison row from a walk-forward summary."""
    if quality is None:
        quality = evaluate_backtest_quality(summary)

    regime_perf: dict[str, list[float]] = {"bull": [], "bear": [], "sideways": []}
    for w in (summary.windows or []):
        regime = _classify_window_regime(w)
        regime_perf[regime].append(w.annual_return)

    regime_avg: dict[str, float] = {}
    for regime, returns in regime_perf.items():
        if returns:
            regime_avg[regime] = sum(returns) / len(returns)

    return StrategyCompareRow(
        strategy_id=strategy_id,
        strategy_name=strategy_name,
        total_windows=summary.total_windows,
        positive_windows=summary.positive_windows,
        negative_windows=summary.negative_windows,
        total_trades=summary.total_trades,
        average_annual_return=summary.average_annual_return,
        median_annual_return=summary.median_annual_return,
        worst_max_drawdown=summary.worst_max_drawdown,
        aggregate_sharpe=summary.aggregate_sharpe,
        aggregate_win_rate=summary.aggregate_win_rate,
        profit_factor=_aggregate_profit_factor(summary),
        quality_grade=quality.quality_grade,
        quality_score=quality.score,
        regime_performance=regime_avg,
    )


def compare_strategies(
    summaries: dict[str, tuple[WalkForwardSummary, BacktestQuality | None]],
    rank_by: str = "aggregate_sharpe",
    strategy_names: dict[str, str] | None = None,
) -> StrategyCompareResult:
    """Compare multiple strategies and rank them.

    Parameters
    ----------
    summaries : dict mapping strategy_id to (WalkForwardSummary, optional BacktestQuality)
    rank_by : metric name to rank by (default: aggregate_sharpe)
    strategy_names : optional dict mapping strategy_id to display name

    Returns
    -------
    StrategyCompareResult with rows sorted by rank_by descending.
    """
    names = strategy_names or {}
    rows: list[StrategyCompareRow] = []
    warnings: list[str] = []

    for sid, (summary, quality) in summaries.items():
        row = build_compare_row(sid, names.get(sid, sid), summary, quality)
        rows.append(row)

    if not rows:
        warnings.append("没有可对比的策略")
        return StrategyCompareResult(rows=[], ranked_by=rank_by, warnings=warnings)

    valid_keys = {"aggregate_sharpe", "average_annual_return", "aggregate_win_rate", "quality_score", "profit_factor"}
    if rank_by not in valid_keys:
        warnings.append(f"不支持的排序字段 {rank_by}，使用 aggregate_sharpe")
        rank_by = "aggregate_sharpe"

    rows.sort(key=lambda r: getattr(r, rank_by, 0), reverse=True)

    return StrategyCompareResult(rows=rows, ranked_by=rank_by, warnings=warnings)


def compare_to_markdown(result: StrategyCompareResult) -> str:
    """Render comparison result as Markdown table."""
    lines: list[str] = []
    lines.append("# 策略对比报告")
    lines.append("")
    lines.append(f"> {COMPARE_DISCLAIMER}")
    lines.append("")
    lines.append(f"排序依据: **{result.ranked_by}**")
    lines.append("")

    if not result.rows:
        lines.append("没有可对比的策略。")
        return "\n".join(lines)

    lines.append("| 排名 | 策略 | 窗口数 | 年化收益 | 最大回撤 | Sharpe | 胜率 | 盈亏比 | 质量 |")
    lines.append("|---:|:---|---:|---:|---:|---:|---:|---:|:---|")
    for i, row in enumerate(result.rows, 1):
        lines.append(
            f"| {i} | {row.strategy_name} | {row.total_windows} "
            f"| {row.average_annual_return:+.2%} | {row.worst_max_drawdown:.2%} "
            f"| {row.aggregate_sharpe:.2f} | {row.aggregate_win_rate:.1%} "
            f"| {row.profit_factor:.2f} | {row.quality_grade} |"
        )

    lines.append("")
    lines.append("## 市场环境表现")
    lines.append("")
    lines.append("| 策略 | 牛市 | 熊市 | 震荡 |")
    lines.append("|:---|---:|---:|---:|")
    for row in result.rows:
        bull = row.regime_performance.get("bull")
        bear = row.regime_performance.get("bear")
        side = row.regime_performance.get("sideways")
        lines.append(
            f"| {row.strategy_name} "
            f"| {bull:+.2%} " if bull is not None else f"| {row.strategy_name} | — "
        )
        # rebuild with proper formatting
    # Simpler approach
    lines_regime: list[str] = []
    lines_regime.append("| 策略 | 牛市 | 熊市 | 震荡 |")
    lines_regime.append("|:---|---:|---:|---:|")
    for row in result.rows:
        def _fmt(val: float | None) -> str:
            return f"{val:+.2%}" if val is not None else "—"
        bull = _fmt(row.regime_performance.get("bull"))
        bear = _fmt(row.regime_performance.get("bear"))
        side = _fmt(row.regime_performance.get("sideways"))
        lines_regime.append(f"| {row.strategy_name} | {bull} | {bear} | {side} |")

    # Replace the broken regime table
    # Find and replace
    idx = len(lines)
    while idx > 0 and lines[idx - 1] != "## 市场环境表现":
        idx -= 1
    if idx > 0:
        lines = lines[:idx + 1]
        lines.append("")
        lines.extend(lines_regime)

    lines.append("")
    lines.append(f"*{COMPARE_DISCLAIMER}*")
    return "\n".join(lines)
