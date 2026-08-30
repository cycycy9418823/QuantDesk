from __future__ import annotations

from typing import Any


def simulate_backtest_result(histories: dict[str, Any], config: Any = None, benchmark_series: Any = None, strategy: str = "ma_breakout") -> Any:
    """Run the shared portfolio simulation and return its audited result."""

    from quantdesk.backtest import (
        EQUITY_CURVE_COLUMNS,
        BacktestConfig,
        BacktestMetrics,
        BacktestResult,
        backtest_assumptions_from_config,
        benchmark_total_return,
        metrics_from_equity,
        np,
        pd,
        simulate_single_asset_backtest,
    )

    config = config or BacktestConfig()
    results: list[Any] = []
    for symbol, hist in histories.items():
        if hist.empty or len(hist) < 130:
            continue
        results.append(simulate_single_asset_backtest(hist, symbol, config, benchmark_series=None, strategy=strategy))
    if not results:
        assumptions = backtest_assumptions_from_config(config)
        metrics = BacktestMetrics(
            annual_return=np.nan,
            total_return=np.nan,
            max_drawdown=np.nan,
            sharpe=np.nan,
            win_rate=np.nan,
            days=0,
            assumptions=assumptions,
            number_of_trades=0,
            turnover=0.0,
            final_equity=config.initial_cash,
            benchmark_return=benchmark_total_return(benchmark_series),
            excess_return=np.nan,
        )
        return BacktestResult(pd.DataFrame(columns=EQUITY_CURVE_COLUMNS), [], metrics, assumptions)
    if len(results) == 1:
        result = results[0]
        if benchmark_series is None:
            return result
        metrics = metrics_from_equity(result.equity_curve, result.trades, result.assumptions, config.initial_cash, benchmark_series)
        return BacktestResult(result.equity_curve, result.trades, metrics, result.assumptions)
    returns = []
    total_trades: list[dict[str, object]] = []
    for result in results:
        equity = result.equity_curve.set_index("date")["equity"].astype(float)
        returns.append(equity.ffill().pct_change(fill_method=None).fillna(0))
        total_trades.extend(result.trades)
    portfolio_returns = pd.concat(returns, axis=1).mean(axis=1).dropna()
    equity_curve = pd.DataFrame(
        {
            "date": portfolio_returns.index,
            "cash": np.nan,
            "shares": np.nan,
            "close": np.nan,
            "market_value": np.nan,
            "equity": config.initial_cash * (1 + portfolio_returns).cumprod(),
            "position_intent": np.nan,
        }
    )
    assumptions = backtest_assumptions_from_config(config, portfolio_rule="equal-weight average of independent single-asset simulations")
    assumptions["not_a_capital_constrained_portfolio_backtest"] = True
    assumptions["warning"] = (
        "当前多标的结果为独立单股回测平均，不代表真实资金组合。"
        "历史回测不代表未来收益。"
    )
    assumptions["backtest_period"] = f"{equity_curve['date'].min()} to {equity_curve['date'].max()}"
    metrics = metrics_from_equity(equity_curve, total_trades, assumptions, config.initial_cash, benchmark_series)
    return BacktestResult(equity_curve, total_trades, metrics, assumptions)
