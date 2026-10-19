from __future__ import annotations

from quantdesk.platform_common import truthy
from quantdesk.backtest import *  # noqa: F401,F403
from quantdesk.paper.market_rules import round_notional_down_to_lot_quantity


def simulate_portfolio_backtest(
    histories: dict[str, pd.DataFrame],
    config: PortfolioBacktestConfig | None = None,
    benchmark_series: pd.Series | None = None,
    data_health: object | None = None,
    strategy: str = "ma_breakout",
) -> BacktestResult:
    """Simulate a capital-constrained multi-stock portfolio.

    Candidate signals are known after each T-day close, ranked with the current
    score proxy using only prior data, and executed on T+1 open when the
    rebalance rule allows. The model shares one cash pool and applies basic
    costs and blocking rules, but it still does not simulate order-book depth,
    queue priority, partial fills, or guaranteed future tradability.
    """

    config = config or PortfolioBacktestConfig()
    health_rows = normalize_data_health_rows(data_health)
    excluded_by_data_error: list[str] = []
    included_with_data_warning: list[str] = []
    included_with_fallback: list[dict[str, str]] = []
    frames: dict[str, pd.DataFrame] = {}
    for symbol, hist in histories.items():
        normalized_symbol = normalize_symbol(symbol)
        health = health_rows.get(normalized_symbol, {})
        warning_level = str(health.get("warning_level", "OK")).upper()
        if warning_level == "ERROR":
            excluded_by_data_error.append(normalized_symbol)
            continue
        frame = prepare_portfolio_frame(symbol, hist, strategy=strategy)
        if frame.empty:
            continue
        frames[symbol] = frame
        if warning_level == "WARNING":
            included_with_data_warning.append(normalized_symbol)
        if warning_level == "INFO" or truthy(health.get("fallback_used")):
            included_with_fallback.append(
                {
                    "symbol": normalized_symbol,
                    "source": str(health.get("effective_source") or health.get("fallback_to") or "fallback"),
                }
            )
    data_health_assumptions = {
        "excluded_by_data_error": excluded_by_data_error,
        "excluded_by_data_error_count": len(excluded_by_data_error),
        "included_with_data_warning": included_with_data_warning,
        "included_with_data_warning_count": len(included_with_data_warning),
        "included_with_fallback": included_with_fallback,
        "included_with_fallback_count": len(included_with_fallback),
    }
    assumptions = portfolio_backtest_assumptions_from_config(config)
    assumptions.update(data_health_assumptions)
    if not frames:
        assumptions["warning"] = (
            str(assumptions.get("warning", ""))
            + " No valid histories remained after data health filtering."
        ).strip()
        metrics = metrics_from_equity(pd.DataFrame(columns=EQUITY_CURVE_COLUMNS), [], assumptions, config.initial_cash, benchmark_series)
        summary = summary_rows_from_metrics(metrics, assumptions, {"exposure": 0.0, "average_positions": 0.0, "max_positions_used": 0})
        return BacktestResult(
            pd.DataFrame(columns=EQUITY_CURVE_COLUMNS),
            [],
            metrics,
            assumptions,
            pd.DataFrame(columns=POSITIONS_COLUMNS),
            pd.DataFrame(columns=REBALANCE_LOG_COLUMNS),
            summary,
            {"exposure": 0.0, "average_positions": 0.0, "max_positions_used": 0},
        )

    by_date: dict[pd.Timestamp, dict[str, pd.Series]] = {}
    all_dates: list[pd.Timestamp] = []
    for symbol, frame in frames.items():
        for _, row in frame.iterrows():
            date = pd.Timestamp(row["日期"])
            by_date.setdefault(date, {})[symbol] = row
            all_dates.append(date)
    dates = sorted(set(all_dates))

    cash = float(config.initial_cash)
    positions: dict[str, dict[str, float]] = {}
    trades: list[dict[str, object]] = []
    equity_rows: list[dict[str, object]] = []
    position_rows: list[dict[str, object]] = []
    rebalance_rows: list[dict[str, object]] = []
    price_fallback_used = False
    pct_change_approximated = False
    last_close_by_symbol: dict[str, float] = {}
    previous_date: pd.Timestamp | None = None

    def current_equity(rows_for_date: dict[str, pd.Series]) -> tuple[float, float]:
        market_value = 0.0
        for symbol, position in positions.items():
            row = rows_for_date.get(symbol)
            close_price = safe_number(row.get("收盘")) if row is not None else last_close_by_symbol.get(symbol, np.nan)
            market_value += position_market_value(position, close_price)
        return cash + market_value, market_value

    for date in dates:
        rows_for_date = by_date.get(date, {})
        for symbol, row in rows_for_date.items():
            close_price = safe_number(row.get("收盘"))
            if math.isfinite(close_price):
                last_close_by_symbol[symbol] = close_price

        rebalance_today = should_rebalance_date(date, previous_date, config.rebalance)
        equity_before, market_value_before = current_equity(rows_for_date)

        if rebalance_today:
            target_candidates = []
            for symbol, row in rows_for_date.items():
                if bool(row.get("signal_for_execution", False)):
                    score = safe_number(row.get("score_proxy_for_execution"), -np.inf)
                    if math.isfinite(score):
                        target_candidates.append((symbol, score))
            target_symbols = [
                symbol
                for symbol, _ in sorted(target_candidates, key=lambda item: (-item[1], item[0]))[: max(config.max_positions, 0)]
            ]
            target_set = set(target_symbols)
            turnover_limit_value = (
                max(0.0, equity_before * config.max_turnover_per_rebalance)
                if config.max_turnover_per_rebalance is not None
                else None
            )
            turnover_used = 0.0

            for symbol in list(positions):
                if symbol in target_set:
                    continue
                row = rows_for_date.get(symbol)
                if row is None:
                    continue
                cash_before = cash
                equity_before_trade, _ = current_equity(rows_for_date)
                requested_price, used_price_fallback = requested_execution_price(row, config)
                price_fallback_used = price_fallback_used or used_price_fallback
                prev_close = safe_number(frames[symbol].loc[frames[symbol]["日期"].lt(date), "收盘"].iloc[-1]) if frames[symbol]["日期"].lt(date).any() else np.nan
                change_pct, used_change_approx = execution_limit_change_pct(row, prev_close, config)
                pct_change_approximated = pct_change_approximated or used_change_approx
                shares = int(positions[symbol]["shares"])
                reason = "SELL because symbol left target set"
                blocked_reason = ""
                if is_trade_suspended(row):
                    blocked_reason = "suspended"
                elif not math.isfinite(requested_price):
                    blocked_reason = "no_fill_price_unavailable"
                elif limit_down_blocked_for_execution(row, change_pct, prev_close, symbol, config):
                    blocked_reason = "limit down"
                if blocked_reason:
                    trades.append(
                        make_trade_record(
                            date=date,
                            symbol=symbol,
                            side="SELL",
                            signal_date=row.get("signal_date"),
                            execution_date=date,
                            requested_price=requested_price,
                            executed_price=np.nan,
                            shares=shares,
                            gross_amount=0.0,
                            commission=0.0,
                            stamp_duty=0.0,
                            slippage_cost=0.0,
                            total_cost=0.0,
                            reason=reason,
                            blocked=True,
                            blocked_reason=blocked_reason,
                        )
                    )
                    rebalance_rows.append(
                        {
                            "date": date.strftime("%Y-%m-%d"),
                            "action": "SELL_BLOCKED",
                            "symbol": symbol,
                            "reason": blocked_reason,
                            "target_weight": 0.0,
                            "current_weight": position_market_value(positions[symbol], safe_number(row.get("收盘"))) / equity_before_trade if equity_before_trade else 0.0,
                            "cash_before": cash_before,
                            "cash_after": cash,
                            "equity_before": equity_before_trade,
                            "equity_after": current_equity(rows_for_date)[0],
                        }
                    )
                    continue
                executed_price = requested_price * (1 - config.slippage_bps / 10_000)
                gross = shares * executed_price
                sell_costs = trade_costs_for("sell", gross, config, trade_date=date)
                commission = float(sell_costs["commission"])
                stamp_duty = float(sell_costs["stamp_duty"])
                transfer_fee = float(sell_costs["transfer_fee"])
                slippage_cost = abs(requested_price - executed_price) * shares
                if turnover_limit_value is not None and turnover_used + gross > turnover_limit_value:
                    trades.append(
                        make_trade_record(
                            date=date,
                            symbol=symbol,
                            side="SELL",
                            signal_date=row.get("signal_date"),
                            execution_date=date,
                            requested_price=requested_price,
                            executed_price=np.nan,
                            shares=shares,
                            gross_amount=0.0,
                            commission=0.0,
                            stamp_duty=0.0,
                            slippage_cost=0.0,
                            total_cost=0.0,
                            reason=reason,
                            blocked=True,
                            blocked_reason="turnover limit",
                        )
                    )
                    rebalance_rows.append(
                        {
                            "date": date.strftime("%Y-%m-%d"),
                            "action": "SELL_BLOCKED",
                            "symbol": symbol,
                            "reason": "turnover limit",
                            "target_weight": 0.0,
                            "current_weight": position_market_value(positions[symbol], safe_number(row.get("收盘"))) / equity_before_trade if equity_before_trade else 0.0,
                            "cash_before": cash_before,
                            "cash_after": cash,
                            "equity_before": equity_before_trade,
                            "equity_after": current_equity(rows_for_date)[0],
                        }
                    )
                    continue
                turnover_used += gross
                cash += gross - commission - stamp_duty - transfer_fee
                trades.append(
                    make_trade_record(
                        date=date,
                        symbol=symbol,
                        side="SELL",
                        signal_date=row.get("signal_date"),
                        execution_date=date,
                        requested_price=requested_price,
                        executed_price=executed_price,
                        shares=shares,
                        gross_amount=gross,
                        commission=commission,
                        stamp_duty=stamp_duty,
                        transfer_fee=transfer_fee,
                        slippage_cost=slippage_cost,
                        total_cost=commission + stamp_duty + transfer_fee + slippage_cost,
                        reason=reason,
                        blocked=False,
                        extra=cost_assumption_fields(sell_costs),
                    )
                )
                del positions[symbol]
                rebalance_rows.append(
                    {
                        "date": date.strftime("%Y-%m-%d"),
                        "action": "SELL",
                        "symbol": symbol,
                        "reason": reason,
                        "target_weight": 0.0,
                        "current_weight": 0.0,
                        "cash_before": cash_before,
                        "cash_after": cash,
                        "equity_before": equity_before_trade,
                        "equity_after": current_equity(rows_for_date)[0],
                    }
                )

            for symbol in list(positions):
                if symbol not in target_set:
                    continue
                row = rows_for_date.get(symbol)
                if row is None:
                    continue
                equity_before_trade, _ = current_equity(rows_for_date)
                close_price = safe_number(row.get("收盘"))
                requested_price, used_price_fallback = requested_execution_price(row, config)
                price_fallback_used = price_fallback_used or used_price_fallback
                if not math.isfinite(requested_price) or not math.isfinite(close_price):
                    continue
                current_value = position_market_value(positions[symbol], close_price)
                max_value = equity_before_trade * config.max_position_pct
                excess_value = current_value - max_value
                lot_size = effective_lot_size(symbol, config, row)
                trim_shares = round_notional_down_to_lot_quantity(excess_value, requested_price, lot_size=lot_size)
                trim_shares = min(trim_shares, int(positions[symbol]["shares"]))
                if trim_shares < lot_size:
                    continue
                cash_before = cash
                prev_close = safe_number(frames[symbol].loc[frames[symbol]["日期"].lt(date), "收盘"].iloc[-1]) if frames[symbol]["日期"].lt(date).any() else np.nan
                change_pct, used_change_approx = execution_limit_change_pct(row, prev_close, config)
                pct_change_approximated = pct_change_approximated or used_change_approx
                reason = "SELL to trim position above max_position_pct"
                blocked_reason = ""
                if is_trade_suspended(row):
                    blocked_reason = "suspended"
                elif limit_down_blocked_for_execution(row, change_pct, prev_close, symbol, config):
                    blocked_reason = "limit down"
                if blocked_reason:
                    trades.append(
                        make_trade_record(
                            date=date,
                            symbol=symbol,
                            side="SELL",
                            signal_date=row.get("signal_date"),
                            execution_date=date,
                            requested_price=requested_price,
                            executed_price=np.nan,
                            shares=trim_shares,
                            gross_amount=0.0,
                            commission=0.0,
                            stamp_duty=0.0,
                            slippage_cost=0.0,
                            total_cost=0.0,
                            reason=reason,
                            blocked=True,
                            blocked_reason=blocked_reason,
                        )
                    )
                    action = "SELL_BLOCKED"
                else:
                    executed_price = requested_price * (1 - config.slippage_bps / 10_000)
                    gross = trim_shares * executed_price
                    sell_costs = trade_costs_for("sell", gross, config, trade_date=date)
                    commission = float(sell_costs["commission"])
                    stamp_duty = float(sell_costs["stamp_duty"])
                    transfer_fee = float(sell_costs["transfer_fee"])
                    slippage_cost = abs(requested_price - executed_price) * trim_shares
                    if turnover_limit_value is not None and turnover_used + gross > turnover_limit_value:
                        trades.append(
                            make_trade_record(
                                date=date,
                                symbol=symbol,
                                side="SELL",
                                signal_date=row.get("signal_date"),
                                execution_date=date,
                                requested_price=requested_price,
                                executed_price=np.nan,
                                shares=trim_shares,
                                gross_amount=0.0,
                                commission=0.0,
                                stamp_duty=0.0,
                                slippage_cost=0.0,
                                total_cost=0.0,
                                reason=reason,
                                blocked=True,
                                blocked_reason="turnover limit",
                            )
                        )
                        action = "SELL_BLOCKED"
                        blocked_reason = "turnover limit"
                    else:
                        turnover_used += gross
                        cash += gross - commission - stamp_duty - transfer_fee
                        old_shares = positions[symbol]["shares"]
                        old_basis = positions[symbol].get("cost_basis", 0.0)
                        positions[symbol]["shares"] = old_shares - trim_shares
                        positions[symbol]["cost_basis"] = old_basis * (positions[symbol]["shares"] / old_shares) if old_shares else 0.0
                        if positions[symbol]["shares"] <= 0:
                            del positions[symbol]
                        trades.append(
                            make_trade_record(
                                date=date,
                                symbol=symbol,
                                side="SELL",
                                signal_date=row.get("signal_date"),
                                execution_date=date,
                                requested_price=requested_price,
                                executed_price=executed_price,
                                shares=trim_shares,
                                gross_amount=gross,
                                commission=commission,
                                stamp_duty=stamp_duty,
                                transfer_fee=transfer_fee,
                                slippage_cost=slippage_cost,
                                total_cost=commission + stamp_duty + transfer_fee + slippage_cost,
                                reason=reason,
                                blocked=False,
                                extra=cost_assumption_fields(sell_costs),
                            )
                        )
                        action = "SELL"
                rebalance_rows.append(
                    {
                        "date": date.strftime("%Y-%m-%d"),
                        "action": action,
                        "symbol": symbol,
                        "reason": reason if action == "SELL" else blocked_reason,
                        "target_weight": config.max_position_pct,
                        "current_weight": current_value / equity_before_trade if equity_before_trade else 0.0,
                        "cash_before": cash_before,
                        "cash_after": cash,
                        "equity_before": equity_before_trade,
                        "equity_after": current_equity(rows_for_date)[0],
                    }
                )

            for symbol in target_symbols:
                if symbol in positions or len(positions) >= config.max_positions:
                    continue
                row = rows_for_date.get(symbol)
                if row is None:
                    continue
                cash_before = cash
                equity_before_trade, _ = current_equity(rows_for_date)
                requested_price, used_price_fallback = requested_execution_price(row, config)
                price_fallback_used = price_fallback_used or used_price_fallback
                prev_close = safe_number(frames[symbol].loc[frames[symbol]["日期"].lt(date), "收盘"].iloc[-1]) if frames[symbol]["日期"].lt(date).any() else np.nan
                change_pct, used_change_approx = execution_limit_change_pct(row, prev_close, config)
                pct_change_approximated = pct_change_approximated or used_change_approx
                reason = "BUY because symbol is in ranked target set"
                blocked_reason = ""
                if is_trade_suspended(row):
                    blocked_reason = "suspended"
                elif not math.isfinite(requested_price):
                    blocked_reason = "no_fill_price_unavailable"
                elif limit_up_blocked_for_execution(row, change_pct, prev_close, symbol, config):
                    blocked_reason = "limit up"
                if blocked_reason:
                    trades.append(
                        make_trade_record(
                            date=date,
                            symbol=symbol,
                            side="BUY",
                            signal_date=row.get("signal_date"),
                            execution_date=date,
                            requested_price=requested_price,
                            executed_price=np.nan,
                            shares=0,
                            gross_amount=0.0,
                            commission=0.0,
                            stamp_duty=0.0,
                            slippage_cost=0.0,
                            total_cost=0.0,
                            reason=reason,
                            blocked=True,
                            blocked_reason=blocked_reason,
                        )
                    )
                    rebalance_rows.append(
                        {
                            "date": date.strftime("%Y-%m-%d"),
                            "action": "BUY_BLOCKED",
                            "symbol": symbol,
                            "reason": blocked_reason,
                            "target_weight": config.max_position_pct,
                            "current_weight": 0.0,
                            "cash_before": cash_before,
                            "cash_after": cash,
                            "equity_before": equity_before_trade,
                            "equity_after": current_equity(rows_for_date)[0],
                        }
                    )
                    continue
                executed_price = requested_price * (1 + config.slippage_bps / 10_000)
                max_value = equity_before_trade * config.max_position_pct
                lot_size = effective_lot_size(symbol, config, row)
                buy_shares = round_notional_down_to_lot_quantity(max_value, executed_price, lot_size=lot_size)
                previous_liquidity_rows = frames[symbol].loc[frames[symbol]["日期"].lt(date)] if "日期" in frames[symbol].columns else frames[symbol].loc[frames[symbol]["date"].lt(date)]
                previous_liquidity_row = previous_liquidity_rows.iloc[-1] if not previous_liquidity_rows.empty else None
                daily_amount, liquidity_amount_source = liquidity_amount_for_execution(row, previous_liquidity_row, config)
                if math.isfinite(daily_amount) and daily_amount > 0 and config.max_participation_rate > 0:
                    max_notional_by_liquidity = daily_amount * config.max_participation_rate
                    max_shares_by_liquidity = round_notional_down_to_lot_quantity(
                        max_notional_by_liquidity, executed_price, lot_size=lot_size
                    )
                    buy_shares = min(buy_shares, max_shares_by_liquidity)
                while buy_shares >= lot_size:
                    gross = buy_shares * executed_price
                    buy_costs = trade_costs_for("buy", gross, config, trade_date=date)
                    commission = float(buy_costs["commission"])
                    transfer_fee = float(buy_costs["transfer_fee"])
                    if gross + commission + transfer_fee <= cash:
                        break
                    buy_shares -= lot_size
                if buy_shares < lot_size:
                    trades.append(
                        make_trade_record(
                            date=date,
                            symbol=symbol,
                            side="BUY",
                            signal_date=row.get("signal_date"),
                            execution_date=date,
                            requested_price=requested_price,
                            executed_price=np.nan,
                            shares=0,
                            gross_amount=0.0,
                            commission=0.0,
                            stamp_duty=0.0,
                            slippage_cost=0.0,
                            total_cost=0.0,
                            reason=reason,
                            blocked=True,
                            blocked_reason="insufficient cash",
                            extra={"liquidity_amount_source": liquidity_amount_source},
                        )
                    )
                    rebalance_rows.append(
                        {
                            "date": date.strftime("%Y-%m-%d"),
                            "action": "BUY_BLOCKED",
                            "symbol": symbol,
                            "reason": "insufficient cash",
                            "target_weight": config.max_position_pct,
                            "current_weight": 0.0,
                            "cash_before": cash_before,
                            "cash_after": cash,
                            "equity_before": equity_before_trade,
                            "equity_after": current_equity(rows_for_date)[0],
                        }
                    )
                    continue
                gross = buy_shares * executed_price
                buy_costs = trade_costs_for("buy", gross, config, trade_date=date)
                commission = float(buy_costs["commission"])
                transfer_fee = float(buy_costs["transfer_fee"])
                slippage_cost = abs(executed_price - requested_price) * buy_shares
                if turnover_limit_value is not None and turnover_used + gross > turnover_limit_value:
                    trades.append(
                        make_trade_record(
                            date=date,
                            symbol=symbol,
                            side="BUY",
                            signal_date=row.get("signal_date"),
                            execution_date=date,
                            requested_price=requested_price,
                            executed_price=np.nan,
                            shares=buy_shares,
                            gross_amount=0.0,
                            commission=0.0,
                            stamp_duty=0.0,
                            slippage_cost=0.0,
                            total_cost=0.0,
                            reason=reason,
                            blocked=True,
                            blocked_reason="turnover limit",
                            extra={"liquidity_amount_source": liquidity_amount_source},
                        )
                    )
                    rebalance_rows.append(
                        {
                            "date": date.strftime("%Y-%m-%d"),
                            "action": "BUY_BLOCKED",
                            "symbol": symbol,
                            "reason": "turnover limit",
                            "target_weight": config.max_position_pct,
                            "current_weight": 0.0,
                            "cash_before": cash_before,
                            "cash_after": cash,
                            "equity_before": equity_before_trade,
                            "equity_after": current_equity(rows_for_date)[0],
                        }
                    )
                    continue
                turnover_used += gross
                cash -= gross + commission + transfer_fee
                positions[symbol] = {"shares": float(buy_shares), "cost_basis": gross + commission + transfer_fee}
                trades.append(
                    make_trade_record(
                        date=date,
                        symbol=symbol,
                        side="BUY",
                        signal_date=row.get("signal_date"),
                        execution_date=date,
                        requested_price=requested_price,
                        executed_price=executed_price,
                        shares=buy_shares,
                        gross_amount=gross,
                        commission=commission,
                        stamp_duty=0.0,
                        transfer_fee=transfer_fee,
                        slippage_cost=slippage_cost,
                        total_cost=commission + transfer_fee + slippage_cost,
                        reason=reason,
                        blocked=False,
                        extra={"liquidity_amount_source": liquidity_amount_source, **cost_assumption_fields(buy_costs)},
                    )
                )
                rebalance_rows.append(
                    {
                        "date": date.strftime("%Y-%m-%d"),
                        "action": "BUY",
                        "symbol": symbol,
                        "reason": reason,
                        "target_weight": config.max_position_pct,
                        "current_weight": position_market_value(positions[symbol], safe_number(row.get("收盘"))) / current_equity(rows_for_date)[0] if current_equity(rows_for_date)[0] else 0.0,
                        "cash_before": cash_before,
                        "cash_after": cash,
                        "equity_before": equity_before_trade,
                        "equity_after": current_equity(rows_for_date)[0],
                    }
                )

        equity, market_value = current_equity(rows_for_date)
        active_positions = 0
        for symbol, position in positions.items():
            row = rows_for_date.get(symbol)
            close_price = safe_number(row.get("收盘")) if row is not None else last_close_by_symbol.get(symbol, np.nan)
            value = position_market_value(position, close_price)
            if value <= 0:
                continue
            active_positions += 1
            position_rows.append(
                {
                    "date": date.strftime("%Y-%m-%d"),
                    "symbol": symbol,
                    "shares": int(position["shares"]),
                    "close": close_price,
                    "market_value": value,
                    "weight": value / equity if equity else 0.0,
                    "cost_basis": position.get("cost_basis", 0.0),
                    "unrealized_pnl": value - position.get("cost_basis", 0.0),
                }
            )
        equity_rows.append(
            {
                "date": date.strftime("%Y-%m-%d"),
                "cash": cash,
                "shares": active_positions,
                "close": np.nan,
                "market_value": market_value,
                "equity": equity,
                "position_intent": len([symbol for symbol, row in rows_for_date.items() if bool(row.get("signal_for_execution", False))]),
            }
        )
        previous_date = date

    equity_curve = pd.DataFrame(equity_rows, columns=EQUITY_CURVE_COLUMNS)
    positions_frame = pd.DataFrame(position_rows, columns=POSITIONS_COLUMNS)
    rebalance_log = pd.DataFrame(rebalance_rows, columns=REBALANCE_LOG_COLUMNS)
    industry_weights: dict[str, float] = {}
    industry_warning = ""
    industry_map = load_industry_map(config.industry_map_csv) if config.industry_map_csv else {}
    if industry_map and not positions_frame.empty:
        latest_date = positions_frame["date"].max()
        latest_positions = positions_frame[positions_frame["date"].eq(latest_date)].copy()
        latest_positions["industry"] = latest_positions["symbol"].map(lambda value: industry_map.get(normalize_symbol(value), "未分类"))
        total_market_value = pd.to_numeric(latest_positions["market_value"], errors="coerce").fillna(0).sum()
        if total_market_value > 0:
            grouped = latest_positions.groupby("industry")["market_value"].sum()
            industry_weights = {str(industry): float(value / total_market_value) for industry, value in grouped.items()}
            if config.max_industry_pct is not None:
                breaches = {
                    industry: weight
                    for industry, weight in industry_weights.items()
                    if weight > config.max_industry_pct
                }
                if breaches:
                    industry_warning = "Industry concentration warning: " + ", ".join(
                        f"{industry} {weight:.2%}" for industry, weight in sorted(breaches.items())
                    )
    position_constraint_violations: list[dict[str, object]] = []
    if not positions_frame.empty:
        latest_date = positions_frame["date"].max()
        latest_positions = positions_frame[positions_frame["date"].eq(latest_date)].copy()
        latest_equity_rows = equity_curve[equity_curve["date"].eq(latest_date)]
        latest_equity = safe_number(latest_equity_rows.iloc[-1].get("equity") if not latest_equity_rows.empty else np.nan, np.nan)
        if math.isfinite(latest_equity) and latest_equity > 0:
            if industry_map:
                latest_positions["industry"] = latest_positions["symbol"].map(lambda value: industry_map.get(normalize_symbol(value), "未分类"))
                industry_totals = latest_positions.groupby("industry")["market_value"].sum().to_dict()
            else:
                latest_positions["industry"] = ""
                industry_totals = {}
            for _, pos_row in latest_positions.iterrows():
                position_dict = pos_row.to_dict()
                industry = str(position_dict.get("industry") or "")
                position_dict["industry_total_market_value"] = float(industry_totals.get(industry, position_dict.get("market_value") or 0.0))
                check = check_position_constraints(position_dict, latest_equity, 0.0, config)
                if not check.get("passed"):
                    position_constraint_violations.append(
                        {
                            "symbol": str(position_dict.get("symbol") or ""),
                            "violations": check.get("violations") or [],
                        }
                    )
    assumptions = portfolio_backtest_assumptions_from_config(
        config,
        price_fallback_used=price_fallback_used,
        pct_change_approximated=pct_change_approximated,
    )
    assumptions.update(data_health_assumptions)
    assumptions["industry_weights_latest"] = industry_weights
    assumptions["industry_warning"] = industry_warning
    assumptions["position_constraint_violations_latest"] = position_constraint_violations
    assumptions["position_constraint_violation_count_latest"] = len(position_constraint_violations)
    if industry_warning:
        assumptions["warning"] = f"{assumptions.get('warning', '')} {industry_warning}".strip()
    assumptions["backtest_period"] = f"{dates[0]:%Y-%m-%d} to {dates[-1]:%Y-%m-%d}" if dates else ""
    metrics = metrics_from_equity(equity_curve, trades, assumptions, config.initial_cash, benchmark_series)
    extra_metrics = {
        "exposure": float((pd.to_numeric(equity_curve["market_value"], errors="coerce") / pd.to_numeric(equity_curve["equity"], errors="coerce")).replace([np.inf, -np.inf], np.nan).fillna(0).mean()) if not equity_curve.empty else 0.0,
        "average_positions": float(pd.to_numeric(equity_curve["shares"], errors="coerce").fillna(0).mean()) if not equity_curve.empty else 0.0,
        "max_positions_used": int(pd.to_numeric(equity_curve["shares"], errors="coerce").fillna(0).max()) if not equity_curve.empty else 0,
    }
    assumptions.update(extra_metrics)
    summary = summary_rows_from_metrics(metrics, assumptions, extra_metrics)
    return BacktestResult(equity_curve, trades, metrics, assumptions, positions_frame, rebalance_log, summary, extra_metrics)
