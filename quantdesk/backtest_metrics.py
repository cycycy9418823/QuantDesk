"""Backtest performance metrics for walk-forward and quality evaluation.

All metrics are for local research and simulation review only.
They do not constitute investment advice, do not predict future prices,
and do not guarantee any outcome.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Sequence

DISCLAIMER = (
    "回测指标仅用于本地研究和模拟复盘，不构成投资建议，"
    "不承诺收益，不预测未来价格，不自动下单。"
)


@dataclass
class TradeRecord:
    symbol: str = ""
    entry_date: str = ""
    exit_date: str = ""
    entry_price: float = 0.0
    exit_price: float = 0.0
    quantity: int = 0
    pnl: float = 0.0
    pnl_pct: float = 0.0
    cost: float = 0.0
    hold_days: int = 0
    hold_warning: str = ""
    strategy: str = ""
    # True when the position was still open at the end of the evaluated range and
    # was liquidated to close the book. Without this, positions that never hit an
    # exit condition are dropped from the sample, which biases results toward
    # winners whenever the exit rule only fires on strength (e.g. "RSI >= 70").
    forced_close: bool = False


@dataclass
class WindowMetrics:
    train_start: str = ""
    train_end: str = ""
    test_start: str = ""
    test_end: str = ""
    symbols_count: int = 0
    trades_count: int = 0
    annual_return: float = 0.0
    max_drawdown: float = 0.0
    sharpe: float = 0.0
    win_rate: float = 0.0
    avg_win: float = 0.0
    avg_loss: float = 0.0
    profit_factor: float = 0.0
    turnover: float = 0.0
    cost_bps: float = 0.0
    slippage_bps: float = 0.0
    data_grade_filter: str = ""
    invalid_symbols_count: int = 0
    skipped_symbols_count: int = 0
    warnings: list[str] = field(default_factory=list)
    trades: list[TradeRecord] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        result = {
            "train_start": self.train_start,
            "train_end": self.train_end,
            "test_start": self.test_start,
            "test_end": self.test_end,
            "symbols_count": self.symbols_count,
            "trades_count": self.trades_count,
            "annual_return": round(self.annual_return, 6),
            "max_drawdown": round(self.max_drawdown, 6),
            "sharpe": round(self.sharpe, 4),
            "win_rate": round(self.win_rate, 4),
            "avg_win": round(self.avg_win, 6),
            "avg_loss": round(self.avg_loss, 6),
            "profit_factor": round(self.profit_factor, 4),
            "turnover": round(self.turnover, 4),
            "cost_bps": self.cost_bps,
            "slippage_bps": self.slippage_bps,
            "data_grade_filter": self.data_grade_filter,
            "invalid_symbols_count": self.invalid_symbols_count,
            "skipped_symbols_count": self.skipped_symbols_count,
            "warnings": list(self.warnings),
        }
        return result


@dataclass
class WalkForwardSummary:
    strategy: str = ""
    start_date: str = ""
    end_date: str = ""
    train_years: int = 0
    test_years: int = 0
    total_windows: int = 0
    positive_windows: int = 0
    negative_windows: int = 0
    median_annual_return: float = 0.0
    average_annual_return: float = 0.0
    worst_window_return: float = 0.0
    best_window_return: float = 0.0
    worst_max_drawdown: float = 0.0
    aggregate_sharpe: float = 0.0
    aggregate_win_rate: float = 0.0
    total_trades: int = 0
    quality_grade: str = "INVALID"
    data_grade_filter: str = ""
    cost_bps: float = 0.0
    slippage_bps: float = 0.0
    windows: list[WindowMetrics] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    disclaimer: str = DISCLAIMER

    def to_dict(self) -> dict[str, Any]:
        return {
            "strategy": self.strategy,
            "start_date": self.start_date,
            "end_date": self.end_date,
            "train_years": self.train_years,
            "test_years": self.test_years,
            "total_windows": self.total_windows,
            "positive_windows": self.positive_windows,
            "negative_windows": self.negative_windows,
            "median_annual_return": round(self.median_annual_return, 6),
            "average_annual_return": round(self.average_annual_return, 6),
            "worst_window_return": round(self.worst_window_return, 6),
            "best_window_return": round(self.best_window_return, 6),
            "worst_max_drawdown": round(self.worst_max_drawdown, 6),
            "aggregate_sharpe": round(self.aggregate_sharpe, 4),
            "aggregate_win_rate": round(self.aggregate_win_rate, 4),
            "total_trades": self.total_trades,
            "quality_grade": self.quality_grade,
            "data_grade_filter": self.data_grade_filter,
            "cost_bps": self.cost_bps,
            "slippage_bps": self.slippage_bps,
            "windows": [w.to_dict() for w in self.windows],
            "warnings": list(self.warnings),
            "disclaimer": self.disclaimer,
        }


def compute_trade_metrics(trades: Sequence[TradeRecord]) -> dict[str, float]:
    """Compute basic trade-level metrics from a list of trade records."""
    if not trades:
        return {
            "trades_count": 0,
            "win_rate": 0.0,
            "avg_win": 0.0,
            "avg_loss": 0.0,
            "profit_factor": 0.0,
            "total_pnl": 0.0,
        }
    wins = [t for t in trades if t.pnl > 0]
    losses = [t for t in trades if t.pnl <= 0]
    total_win = sum(t.pnl for t in wins)
    total_loss = abs(sum(t.pnl for t in losses))
    win_rate = len(wins) / len(trades) if trades else 0.0
    avg_win = total_win / len(wins) if wins else 0.0
    avg_loss = total_loss / len(losses) if losses else 0.0
    profit_factor = total_win / total_loss if total_loss > 0 else float("inf") if total_win > 0 else 0.0
    return {
        "trades_count": len(trades),
        "win_rate": win_rate,
        "avg_win": avg_win,
        "avg_loss": avg_loss,
        "profit_factor": profit_factor,
        "total_pnl": sum(t.pnl for t in trades),
    }


def compute_equity_metrics(
    equity_curve: Sequence[float],
    trading_days_per_year: float = 252.0,
    risk_free_annual: float = 0.02,
) -> dict[str, float]:
    """Compute annual return, max drawdown, Sharpe, and Sortino from an equity curve."""
    if len(equity_curve) < 2 or equity_curve[0] <= 0:
        return {"annual_return": 0.0, "max_drawdown": 0.0, "sharpe": 0.0, "sortino": 0.0}
    start_val = equity_curve[0]
    end_val = equity_curve[-1]
    n_days = len(equity_curve) - 1
    years = n_days / trading_days_per_year if trading_days_per_year > 0 else 1.0
    if years <= 0:
        years = 1.0
    total_return = (end_val / start_val) - 1.0 if start_val > 0 else 0.0
    annual_return = (1.0 + total_return) ** (1.0 / years) - 1.0 if years > 0 else 0.0

    peak = equity_curve[0]
    max_dd = 0.0
    for val in equity_curve:
        if val > peak:
            peak = val
        dd = (peak - val) / peak if peak > 0 else 0.0
        if dd > max_dd:
            max_dd = dd

    daily_returns = []
    for i in range(1, len(equity_curve)):
        prev = equity_curve[i - 1]
        if prev > 0:
            daily_returns.append(equity_curve[i] / prev - 1.0)
    if len(daily_returns) < 2:
        sharpe = 0.0
        sortino = 0.0
    else:
        rf_daily = float(risk_free_annual) / trading_days_per_year if trading_days_per_year > 0 else 0.0
        excess_returns = [r - rf_daily for r in daily_returns]
        mean_r = sum(excess_returns) / len(excess_returns)
        var_r = sum((r - mean_r) ** 2 for r in excess_returns) / (len(excess_returns) - 1)
        std_r = math.sqrt(var_r) if var_r > 0 else 0.0
        sharpe = (mean_r / std_r * math.sqrt(trading_days_per_year)) if std_r > 0 else 0.0
        downside_square_mean = sum(min(r, 0.0) ** 2 for r in excess_returns) / len(excess_returns)
        downside_deviation = math.sqrt(downside_square_mean) if downside_square_mean > 0 else 0.0
        sortino = (mean_r / downside_deviation * math.sqrt(trading_days_per_year)) if downside_deviation > 0 else 0.0

    return {
        "annual_return": annual_return,
        "max_drawdown": max_dd,
        "sharpe": sharpe,
        "sortino": sortino,
    }


def summarize_windows(windows: Sequence[WindowMetrics]) -> WalkForwardSummary:
    """Build an aggregate summary from a list of window metric results."""
    summary = WalkForwardSummary()
    summary.total_windows = len(windows)
    if not windows:
        return summary
    summary.windows = list(windows)
    returns = [w.annual_return for w in windows]
    summary.positive_windows = sum(1 for r in returns if r > 0)
    summary.negative_windows = sum(1 for r in returns if r <= 0)
    sorted_returns = sorted(returns)
    mid = len(sorted_returns) // 2
    summary.median_annual_return = (
        sorted_returns[mid]
        if len(sorted_returns) % 2 == 1
        else (sorted_returns[mid - 1] + sorted_returns[mid]) / 2.0
    )
    summary.average_annual_return = sum(returns) / len(returns) if returns else 0.0
    summary.worst_window_return = min(returns) if returns else 0.0
    summary.best_window_return = max(returns) if returns else 0.0
    drawdowns = [w.max_drawdown for w in windows]
    summary.worst_max_drawdown = max(drawdowns) if drawdowns else 0.0
    total_trades = sum(w.trades_count for w in windows)
    summary.total_trades = total_trades
    total_wins = sum(round(w.win_rate * w.trades_count) for w in windows)
    summary.aggregate_win_rate = total_wins / total_trades if total_trades > 0 else 0.0
    sharpes = [w.sharpe for w in windows if w.trades_count > 0]
    summary.aggregate_sharpe = sum(sharpes) / len(sharpes) if sharpes else 0.0

    if summary.total_trades < 30:
        summary.warnings.append("总交易次数不足30笔，结果可信度有限。")
    if summary.negative_windows > summary.positive_windows:
        summary.warnings.append("负收益窗口多于正收益窗口，策略样本外表现不稳定。")
    if summary.worst_max_drawdown > 0.3:
        summary.warnings.append(f"最大回撤 {summary.worst_max_drawdown:.1%}，超过 30% 阈值。")
    if len(returns) >= 3:
        best = max(returns)
        total_positive = sum(r for r in returns if r > 0)
        if total_positive > 0 and best / total_positive > 0.5:
            summary.warnings.append("单一窗口贡献超过 50% 的正收益，存在集中度风险。")
    return summary
