"""Walk-forward validation framework for QuantDesk strategies.

This module splits a date range into rolling train/test windows
and evaluates strategy performance on the out-of-sample test periods.

All results are for local research and simulation review only.
They do not constitute investment advice.
"""

from __future__ import annotations

from datetime import datetime, timedelta
import math
from pathlib import Path

import pandas as pd

from quantdesk.backtest_metrics import (
    TradeRecord,
    WalkForwardSummary,
    WindowMetrics,
    compute_equity_metrics,
    compute_trade_metrics,
    summarize_windows,
)
from quantdesk.board_rules import classify_board, limit_fill_check
from quantdesk.corporate_action_repair import repair_corporate_actions
from quantdesk.csv_io import read_csv_frame
from quantdesk.data_quality import DataGrade, assess_daily_frame_grade
from quantdesk.data_truth.daily_bar_validity import daily_bar_validity_from_row
from quantdesk.indicators import add_indicators
from quantdesk.paper.market_rules import round_notional_down_to_lot_quantity
from quantdesk.paper.trade_costs import TradeCostConfig, compute_trade_costs
from quantdesk.price_basis import classify_frame_price_basis, validate_price_frame_set
from quantdesk.strategy_conditions import evaluate_conditions
from quantdesk.strategy_registry import StrategyDefinition, get_strategy


def generate_windows(
    start_date: str,
    end_date: str,
    train_years: int = 3,
    test_years: int = 1,
) -> list[dict[str, str]]:
    """Generate rolling train/test window date pairs."""
    fmt = "%Y%m%d"
    start = datetime.strptime(start_date, fmt)
    end = datetime.strptime(end_date, fmt)
    windows: list[dict[str, str]] = []
    cursor = start
    while True:
        train_end = cursor + timedelta(days=int(train_years * 365.25))
        test_end = train_end + timedelta(days=int(test_years * 365.25))
        if test_end > end:
            break
        windows.append({
            "train_start": cursor.strftime(fmt),
            "train_end": train_end.strftime(fmt),
            "test_start": (train_end + timedelta(days=1)).strftime(fmt),
            "test_end": test_end.strftime(fmt),
        })
        cursor = cursor + timedelta(days=int(test_years * 365.25))
    return windows


def _prepare_daily_frame(frame: pd.DataFrame, path: Path, *, as_of: str = "") -> pd.DataFrame:
    """Validate the original frame, then restate only information known by as_of."""

    basis, reason = classify_frame_price_basis(frame)
    gate = validate_price_frame_set({path.stem: frame}, consumer="backtest")
    if not gate["eligible"]:
        raise ValueError("walk_forward_price_contract:" + ",".join(gate["reason_codes"]))
    df = frame.copy()
    if "date" not in df or "close" not in df:
        empty = pd.DataFrame()
        empty.attrs["warnings"] = [f"daily_required_columns_missing:{path.stem}"]
        return empty
    df["date"] = pd.to_datetime(df["date"], errors="coerce")
    if as_of:
        df = df.loc[df["date"] <= pd.Timestamp(as_of)].copy()
    df = df.dropna(subset=["date"]).sort_values("date").drop_duplicates("date", keep="last").reset_index(drop=True)
    repaired, info = repair_corporate_actions(df) if basis == "raw" else (df, {"ca_bars": 0})
    if info.get("unverifiable"):
        # A large move that pct_chg cannot corroborate cannot be repaired, and
        # measuring it anyway would quietly poison the result.
        empty = pd.DataFrame()
        empty.attrs["warnings"] = [f"corporate_action_unverifiable:{path.stem}"]
        return empty
    if info.get("ca_bars"):
        repaired["adjust"] = "qfq"
        if "price_basis" in repaired:
            repaired["price_basis"] = "qfq"
        repaired["research_restatement"] = "reported_return_proxy"
        repaired.attrs["warnings"] = list(repaired.attrs.get("warnings") or []) + [
            f"corporate_action_repaired:{path.stem}:{info['ca_bars']}:research_proxy"
        ]
    effective_basis, _ = classify_frame_price_basis(repaired)
    repaired.attrs.update(original_price_basis=basis, price_basis=effective_basis,
        price_basis_reason=reason, information_cutoff=as_of or "latest_input",
        real_execution_price=False,
        execution_price_role="raw_market_bar_research_proxy" if effective_basis == "raw" else "same_basis_research_proxy")
    return repaired


def load_daily_csv(path: Path, *, as_of: str = "") -> pd.DataFrame:
    """Read complete original prices before applying the research cutoff."""

    frame = read_csv_frame(path)
    if frame.empty:
        return frame
    return _prepare_daily_frame(frame, path, as_of=as_of)


def _check_data_grade(
    df: pd.DataFrame,
    min_grade: str,
    expected_latest: str = "",
    min_backtest_rows: int = 500,
    deep_validate: bool = False,
) -> tuple[bool, str]:
    """Return (allowed, reason) based on data grade check.

    ``deep_validate`` runs the project's own content validator (outlier / logic /
    fake-suspension checks in ``data_deep_validation``), which downgrades a symbol
    below quality 50. It was previously unreachable: a repo-wide search found
    ``deep_validate=True`` only in a docstring, so ``check_return_spikes`` had
    never run for any caller. It defaults to off here because switching it on
    silently shrinks the tradable universe, which would change every result
    without saying so — the caller opts in.
    """

    grade_order = [g.value for g in DataGrade]
    assessment = assess_daily_frame_grade(
        df,
        provider_status="local_daily_cache",
        expected_latest_date=expected_latest,
        min_backtest_rows=min_backtest_rows,
        deep_validate=deep_validate,
    )
    actual_grade = assessment.data_grade
    if actual_grade not in grade_order or min_grade not in grade_order:
        return False, f"unknown grade {actual_grade}"
    if grade_order.index(actual_grade) < grade_order.index(min_grade):
        return False, f"data_grade={actual_grade} < {min_grade}"
    return True, ""


def _simple_strategy_signals(
    df: pd.DataFrame,
    strategy: StrategyDefinition,
) -> pd.DataFrame:
    """Apply strategy entry/exit conditions mechanically on daily data.

    Returns a DataFrame with columns: date, signal ('entry'/'exit'/None).
    Conditions are evaluated through ``strategy_conditions.evaluate_conditions``
    so walk-forward honours the SAME operator set as the main engine
    (cross_above/cross_below/between/lookback_high/lookback_low/near_pct, ...)
    instead of only the four comparison operators. Entry uses AND; exit uses
    OR (any matching exit rule triggers). This is a simplified rule-based
    signal generator for walk-forward use; it does not predict prices.
    """
    enhanced = add_indicators(df)
    if enhanced.empty:
        return pd.DataFrame(columns=["date", "signal"])
    entry_conditions = list(strategy.entry_conditions or [])
    exit_conditions = list(strategy.exit_conditions or [])
    signals = []
    for i in range(len(enhanced)):
        row = enhanced.iloc[i].to_dict()
        # Walk-forward only runs on backtest-grade data, so treat data
        # reliability as satisfied (the live pipeline supplies this field).
        if row.get("data_reliability_score") in (None, ""):
            row["data_reliability_score"] = 100
        entry_met = bool(evaluate_conditions(row, entry_conditions).get("all_matched"))
        exit_met = False
        if not entry_met and exit_conditions:
            exit_met = bool(
                evaluate_conditions(row, exit_conditions, mode="any").get("triggered")
            )
        date_val = row.get("date")
        if pd.notna(date_val):
            if entry_met:
                signals.append({"date": date_val, "signal": "entry"})
            elif exit_met:
                signals.append({"date": date_val, "signal": "exit"})
            else:
                signals.append({"date": date_val, "signal": None})
    return pd.DataFrame(signals)


def _is_one_word_bar(open_p: float, high: float, low: float, close: float) -> bool:
    """A one-word (locked) bar has open==high==low==close (no intraday range)."""

    values = [open_p, high, low, close]
    if any(pd.isna(v) or v is None for v in values):
        return False
    return abs(high - low) < 1e-9 and abs(open_p - close) < 1e-9 and abs(open_p - high) < 1e-9


def _next_bar_change_pct(prev_close: float, close: float) -> float | None:
    if prev_close is None or close is None or pd.isna(prev_close) or pd.isna(close) or prev_close <= 0:
        return None
    return (close / prev_close - 1.0) * 100.0


def _simulate_trades(
    df: pd.DataFrame,
    signals: pd.DataFrame,
    cost_bps: float = 5.0,
    slippage_bps: float = 5.0,
    initial_cash: float = 100000.0,
    max_position_pct: float = 0.95,
    symbol: str = "",
    strategy_name: str = "",
    force_close_at_end: bool = False,
) -> tuple[list[TradeRecord], list[float]]:
    """Run a simple long-only single-position simulation.

    Returns (trades, equity_curve). Signals execute on the next dated, valid
    OHLCV row's open. Missing execution evidence cannot supply a fill.

    ``force_close_at_end`` (default off, so existing callers are unchanged)
    liquidates a still-open position on the final bar and records it as a trade.
    With it off, positions that never trigger an exit are silently excluded from
    the trade sample; for a rule whose exit only fires on strength (e.g.
    ``rsi14 >= 70``) that quietly keeps the winners and discards the laggards.
    """
    trades: list[TradeRecord] = []
    equity: list[float] = [initial_cash]
    gate = validate_price_frame_set({symbol or "requested_symbol": df}, consumer="backtest")
    if not gate["eligible"]:
        raise ValueError("walk_forward_price_contract:" + ",".join(gate["reason_codes"]))
    cash = initial_cash
    position = 0
    entry_price = 0.0
    entry_date = ""
    entry_cost = 0.0
    cost_config = TradeCostConfig(
        commission_rate=max(float(cost_bps or 0.0), 0.0) / 10_000.0,
        min_commission=5.0,
        stamp_tax_rate=0.0005,
        transfer_fee_rate=0.0,
        slippage_bps=0.0,
        apply_min_commission=float(cost_bps or 0.0) > 0,
    )
    slip_rate = slippage_bps / 10000.0
    max_position_pct = min(max(float(max_position_pct or 0.0), 0.0), 1.0)

    merged = df.copy()
    if "date" in merged.columns:
        merged["date"] = pd.to_datetime(merged["date"], errors="coerce")
    if "date" in signals.columns:
        signals = signals.copy()
        signals["date"] = pd.to_datetime(signals["date"], errors="coerce")
    signal_map = {}
    for _, row in signals.iterrows():
        d = row.get("date")
        if pd.notna(d) and row.get("signal"):
            signal_map[pd.Timestamp(d).normalize()] = row["signal"]

    close_col = "close" if "close" in merged.columns else None
    if close_col is None:
        return trades, equity
    if "date" not in merged:
        return trades, equity
    execution_col = "open" if "open" in merged.columns else close_col

    dates = merged["date"].tolist()
    closes = merged[close_col].tolist()
    execution_prices = merged[execution_col].tolist()
    board_lot = max(1, int(classify_board(str(symbol or "")).lot_size or 100))
    last_mark = next((float(value) for value in closes if pd.notna(value) and math.isfinite(float(value)) and value > 0), 0.)

    def can_fill(index: int, side: str, price: float, previous: float | None) -> bool:
        row = merged.iloc[index].to_dict()
        valid, _reason = daily_bar_validity_from_row(row)
        if not valid or any(not math.isfinite(float(row[key])) for key in ("open", "high", "low", "close", "volume")):
            return False
        if str(row.get("suspended") or row.get("is_suspended") or "").lower() in {"true", "1"}:
            return False
        base = row.get("pre_close", row.get("prev_close"))
        if base is None or pd.isna(base):
            base = previous
        return bool(limit_fill_check(symbol, side, price=price, trade_date=dates[index],
            pre_close=base, pct_chg=row.get("pct_chg"), name=row.get("name"))["ok"])

    for i in range(len(dates) - 1):
        today = pd.Timestamp(dates[i]).normalize() if pd.notna(dates[i]) else None
        if today is None:
            equity.append(equity[-1])
            continue
        sig = signal_map.get(today)
        next_close = closes[i + 1]
        next_exec_price = execution_prices[i + 1]
        if pd.notna(next_close) and math.isfinite(float(next_close)) and next_close > 0:
            last_mark = float(next_close)
        if (pd.isna(next_exec_price) or not math.isfinite(float(next_exec_price)) or next_exec_price <= 0
                or pd.isna(dates[i + 1]) or pd.Timestamp(dates[i + 1]).normalize() <= today):
            equity.append(cash + position * last_mark)
            continue

        if sig == "entry" and position == 0:
            exec_price = next_exec_price * (1.0 + slip_rate)
            if not can_fill(i + 1, "buy", exec_price, closes[i]):
                equity.append(cash + position * last_mark)
                continue
            shares = round_notional_down_to_lot_quantity(cash * max_position_pct, exec_price, lot_size=board_lot)
            while shares > 0:
                gross = shares * exec_price
                cost = float(compute_trade_costs(side="buy", gross=gross, config=cost_config)["total_fee"])
                if gross + cost <= cash:
                    break
                shares -= board_lot
            if shares > 0:
                gross = shares * exec_price
                buy_costs = compute_trade_costs(side="buy", gross=gross, config=cost_config)
                cost = float(buy_costs["total_fee"])
                cash -= shares * exec_price + cost
                position = shares
                entry_price = exec_price
                entry_cost = cost
                entry_date = pd.Timestamp(dates[i + 1]).strftime("%Y-%m-%d") if pd.notna(dates[i + 1]) else ""
        elif sig == "exit" and position > 0:
            exec_price = next_exec_price * (1.0 - slip_rate)
            if (pd.Timestamp(dates[i + 1]).normalize() <= pd.Timestamp(entry_date)
                    or not can_fill(i + 1, "sell", exec_price, closes[i])):
                equity.append(cash + position * last_mark)
                continue
            revenue = position * exec_price
            sell_costs = compute_trade_costs(side="sell", gross=revenue, config=cost_config)
            exit_fee = float(sell_costs["total_fee"])
            total_cost = entry_cost + exit_fee
            cash += revenue - exit_fee
            pnl = (exec_price - entry_price) * position - total_cost
            exit_date = pd.Timestamp(dates[i + 1]).strftime("%Y-%m-%d") if pd.notna(dates[i + 1]) else ""
            hold = 0
            hold_warning = ""
            try:
                delta = pd.Timestamp(exit_date) - pd.Timestamp(entry_date)
                if pd.isna(delta):
                    raise ValueError("holding period is NaT")
                hold = int(delta.days)
            except Exception as exc:  # noqa: BLE001
                hold = -1
                hold_warning = f"invalid holding period dates: {entry_date or '-'} -> {exit_date or '-'} ({exc})"
            trades.append(TradeRecord(
                symbol=symbol,
                entry_date=entry_date,
                exit_date=exit_date,
                entry_price=entry_price,
                exit_price=exec_price,
                quantity=position,
                pnl=pnl,
                pnl_pct=(exec_price / entry_price - 1.0) if entry_price > 0 else 0.0,
                cost=total_cost,
                hold_days=hold,
                hold_warning=hold_warning,
                strategy=strategy_name,
            ))
            position = 0
            entry_price = 0.0
            entry_cost = 0.0

        equity.append(cash + position * last_mark)

    if force_close_at_end and position > 0:
        # Liquidate on the last available bar so an unexited position still lands
        # in the sample. Priced at the final close (the last mark we can observe)
        # with the same slippage and sell costs a real exit would pay.
        final_price = float(closes[-1]) if closes and pd.notna(closes[-1]) else None
        final_date = pd.Timestamp(dates[-1]).normalize() if dates and pd.notna(dates[-1]) else None
        if (final_price is not None and math.isfinite(final_price) and final_price > 0
                and final_date is not None and final_date > pd.Timestamp(entry_date)
                and can_fill(len(dates) - 1, "sell", final_price * (1.0 - slip_rate), closes[-2])):
            exec_price = final_price * (1.0 - slip_rate)
            revenue = position * exec_price
            sell_costs = compute_trade_costs(side="sell", gross=revenue, config=cost_config)
            exit_fee = float(sell_costs["total_fee"])
            total_cost = entry_cost + exit_fee
            cash += revenue - exit_fee
            pnl = (exec_price - entry_price) * position - total_cost
            exit_date = ""
            for candidate_date in reversed(dates):
                if pd.notna(candidate_date):
                    exit_date = pd.Timestamp(candidate_date).strftime("%Y-%m-%d")
                    break
            hold = 0
            hold_warning = ""
            try:
                delta = pd.Timestamp(exit_date) - pd.Timestamp(entry_date)
                if pd.isna(delta):
                    raise ValueError("holding period is NaT")
                hold = int(delta.days)
            except Exception as exc:  # noqa: BLE001
                hold = -1
                hold_warning = f"invalid holding period dates: {entry_date or '-'} -> {exit_date or '-'} ({exc})"
            trades.append(TradeRecord(
                symbol=symbol,
                entry_date=entry_date,
                exit_date=exit_date,
                entry_price=entry_price,
                exit_price=exec_price,
                quantity=position,
                pnl=pnl,
                pnl_pct=(exec_price / entry_price - 1.0) if entry_price > 0 else 0.0,
                cost=total_cost,
                hold_days=hold,
                hold_warning=hold_warning,
                strategy=strategy_name,
                forced_close=True,
            ))
            position = 0
            equity[-1] = cash

    return trades, equity


# --- v3.7.0.1 Bootstrap 置信区间 ---

def _bootstrap_sharpe_ci(
    equity_series: list[float],
    *,
    n_iterations: int = 100,
    confidence_level: float = 0.95,
    risk_free_annual: float = 0.02,
) -> dict[str, float] | None:
    """用 Bootstrap 法计算 Sharpe 比率的 95% 置信区间。

    Parameters
    ----------
    equity_series : list[float]
        每日权益曲线。
    n_iterations : int
        重采样次数。
    confidence_level : float
        置信水平（默认 95%）。
    risk_free_annual : float
        无风险年化收益率（默认 2%）。

    Returns
    -------
    dict | None
        {"sharpe_point", "ci_lower", "ci_upper", "n_samples"}
        若样本不足则返回 None。
    """
    import numpy as _np
    if len(equity_series) < 20:
        return None
    arr = _np.array(equity_series, dtype=float)
    returns = _np.diff(arr) / arr[:-1]
    returns = returns[_np.isfinite(returns)]
    if len(returns) < 20:
        return None
    rf_daily = risk_free_annual / 252.0
    excess = returns - rf_daily
    point_sharpe = float(_np.mean(excess) / _np.std(excess) * _np.sqrt(252)) if _np.std(excess) > 0 else 0.0
    rng = _np.random.default_rng(seed=42)
    boot_sharpes = []
    for _ in range(n_iterations):
        sample = rng.choice(excess, size=len(excess), replace=True)
        std = _np.std(sample)
        if std > 0:
            boot_sharpes.append(float(_np.mean(sample) / std * _np.sqrt(252)))
    if not boot_sharpes:
        return None
    alpha = 1.0 - confidence_level
    ci_lower = float(_np.percentile(boot_sharpes, 100 * alpha / 2))
    ci_upper = float(_np.percentile(boot_sharpes, 100 * (1 - alpha / 2)))
    return {
        "sharpe_point": point_sharpe,
        "ci_lower": ci_lower,
        "ci_upper": ci_upper,
        "n_samples": len(returns),
        "n_iterations": n_iterations,
        "confidence_level": confidence_level,
    }


def _combine_sleeve_equity(
    sleeves: list[pd.Series],
    sleeve_cash: list[float],
) -> pd.Series:
    """Sum independent per-symbol equity sleeves into one pooled curve.

    Each sleeve is funded with its own capital on the first day of the window,
    whether or not the symbol has printed a bar yet. A symbol that lists (or
    resumes) part-way through the window therefore holds idle cash -- its own
    starting capital -- until its first bar, and the last observed value after
    its final bar. Summing the raw concat instead turns the leading NaN into 0,
    so the sleeve's whole capital shows up as "profit" on the day the first bar
    appears; that is how a strategy that never traded reported +5.72% a year
    with a Sharpe of 2.11.
    """

    if not sleeves:
        return pd.Series(dtype=float)
    frame = pd.concat(sleeves, axis=1).sort_index().ffill()
    for position, cash in enumerate(sleeve_cash):
        frame.iloc[:, position] = frame.iloc[:, position].fillna(float(cash))
    return frame.sum(axis=1)


def run_single_window(
    *,
    daily_dir: Path,
    symbols: list[str],
    window: dict[str, str],
    strategy: StrategyDefinition,
    min_data_grade: str = "BACKTEST_READY",
    cost_bps: float = 5.0,
    slippage_bps: float = 5.0,
    initial_cash: float = 100000.0,
    max_position_pct: float = 0.95,
    min_test_days: int = 30,         # v3.7.0.1: 从 10 天提升到 30 天
    min_trades_for_valid: int = 30,  # v3.7.0.1: 从 5 笔提升到 30 笔
    bootstrap_iterations: int = 100, # v3.7.0.1: bootstrap 置信区间
    min_sleeve_cash: float = 20000.0,
    deep_validate: bool = True,
) -> WindowMetrics:
    """Run one train/test window for walk-forward validation.

    ``min_sleeve_cash`` is a floor on the per-symbol capital. Splitting a fixed pot
    across the universe means a wide run gives each symbol too little to buy one
    board lot, so almost nothing trades -- a 210-symbol probe produced **2 trades**
    over 8 years alongside a +6.65% median annual return, which cannot both be
    true. Each symbol now gets a sleeve it can actually trade.

    The reported equity is the SUM of those sleeves (one per symbol that passed
    the data gates in this window), not a per-sleeve figure and not a single
    account. Every sleeve is funded from the first day of the window; a symbol
    whose first bar comes later sits in idle cash until then (see
    ``_combine_sleeve_equity``) instead of contributing zero and then appearing
    as a jump in equity. Each entry deploys only ``max_position_pct`` of its
    sleeve, so the pooled curve understates the risk of a fully invested book.
    """
    test_start = pd.Timestamp(window["test_start"])
    test_end = pd.Timestamp(window["test_end"])
    # Per-symbol sleeve: never so small that a board lot is unaffordable.
    sleeve_cash = max(float(initial_cash) / max(len(symbols), 1), float(min_sleeve_cash))
    all_trades: list[TradeRecord] = []
    equity_series_by_symbol: list[pd.Series] = []
    sleeve_cash_by_symbol: list[float] = []
    valid_count = 0
    skipped_count = 0
    invalid_count = 0
    input_warnings: list[str] = []
    original_frames = {symbol: read_csv_frame(daily_dir / f"{symbol}.csv") for symbol in dict.fromkeys(symbols)}
    present_frames = {symbol: frame for symbol, frame in original_frames.items() if not frame.empty}
    if present_frames:
        price_gate = validate_price_frame_set(present_frames, consumer="backtest")
        if not price_gate["eligible"]:
            raise ValueError("walk_forward_price_contract:" + ",".join(price_gate["reason_codes"]))
        input_warnings.append(f"original_price_basis:{price_gate['accepted_price_basis']}")

    for symbol in dict.fromkeys(symbols):
        csv_path = daily_dir / f"{symbol}.csv"
        original = original_frames[symbol]
        read_warnings = list(original.attrs.get("warnings") or [])
        input_warnings.extend(read_warnings)
        if original.empty or any(item.startswith(("csv_parse_failed:", "csv_read_failed:")) for item in read_warnings):
            skipped_count += 1
            input_warnings.append(f"excluded_daily_unreadable_or_missing:{symbol}")
            continue
        df = _prepare_daily_frame(original, csv_path, as_of=str(test_end.date()))
        input_warnings.extend(df.attrs.get("warnings") or [])
        if df.empty:
            skipped_count += 1
            input_warnings.append(f"excluded_no_asof_data_or_unverifiable:{symbol}")
            continue
        # Safe to run now: measured on a 120-file sample post-repair, deep validation
        # downgrades zero symbols, because the return spikes it was built to catch
        # were the unadjusted corporate-action cliffs the loader now fixes. So this
        # costs no universe coverage and guards against genuine bad ticks.
        allowed, reason = _check_data_grade(df, min_data_grade, deep_validate=deep_validate)
        if not allowed:
            invalid_count += 1
            input_warnings.append(f"excluded_data_grade:{symbol}:{reason}")
            continue
        if "date" not in df.columns:
            skipped_count += 1
            continue
        test_df = df[(df["date"] >= test_start) & (df["date"] <= test_end)].copy()
        if len(test_df) < min_test_days:  # v3.7.0.1 提升最低测试天数
            skipped_count += 1
            input_warnings.append(f"excluded_short_window:{symbol}:{len(test_df)}")
            continue
        valid_count += 1
        signals = _simple_strategy_signals(test_df, strategy)
        trades, equity = _simulate_trades(
            test_df, signals,
            cost_bps=cost_bps, slippage_bps=slippage_bps,
            initial_cash=sleeve_cash,
            max_position_pct=max_position_pct,
            symbol=symbol, strategy_name=strategy.strategy_id,
        )
        all_trades.extend(trades)
        if equity:
            equity_dates = pd.to_datetime(test_df["date"], errors="coerce").dt.normalize().tolist()
            if len(equity_dates) >= len(equity):
                equity_series_by_symbol.append(pd.Series(equity, index=equity_dates[: len(equity)], dtype=float))
            else:
                equity_series_by_symbol.append(pd.Series(equity, dtype=float))
            sleeve_cash_by_symbol.append(float(sleeve_cash))

    trade_metrics = compute_trade_metrics(all_trades)
    if equity_series_by_symbol:
        aligned_equity = _combine_sleeve_equity(equity_series_by_symbol, sleeve_cash_by_symbol)
        all_equity = aligned_equity.tolist()
    else:
        all_equity = []
    equity_metrics = compute_equity_metrics(all_equity) if all_equity else {}
    warnings: list[str] = list(dict.fromkeys(input_warnings))
    if skipped_count or invalid_count:
        warnings.append(f"partial_requested_universe:{len(set(symbols))}:loaded={valid_count}:skipped={skipped_count}:invalid={invalid_count}")
    if trade_metrics["trades_count"] < min_trades_for_valid:
        warnings.append(
            f"交易次数 {trade_metrics['trades_count']} 不足 {min_trades_for_valid} 笔，"
            f"窗口结果可信度低，建议延长测试期或增加样本股票。"
        )
    if valid_count == 0:
        warnings.append("没有符合数据门禁的股票进入回测。")

    # v3.7.0.1: Bootstrap 置信区间
    sharpe_ci = _bootstrap_sharpe_ci(all_equity, n_iterations=bootstrap_iterations) if all_equity else None
    extra_warnings = []
    if sharpe_ci and sharpe_ci.get("ci_lower", 0) < 0:
        extra_warnings.append(
            f"Sharpe 95% CI [{sharpe_ci['ci_lower']:.3f}, {sharpe_ci['ci_upper']:.3f}] "
            f"包含 0，策略统计显著性不足。"
        )

    return WindowMetrics(
        train_start=window["train_start"],
        train_end=window["train_end"],
        test_start=window["test_start"],
        test_end=window["test_end"],
        symbols_count=valid_count,
        trades_count=trade_metrics["trades_count"],
        annual_return=equity_metrics.get("annual_return", 0.0),
        max_drawdown=equity_metrics.get("max_drawdown", 0.0),
        sharpe=equity_metrics.get("sharpe", 0.0),
        win_rate=trade_metrics["win_rate"],
        avg_win=trade_metrics["avg_win"],
        avg_loss=trade_metrics["avg_loss"],
        profit_factor=trade_metrics["profit_factor"],
        turnover=0.0,
        cost_bps=cost_bps,
        slippage_bps=slippage_bps,
        data_grade_filter=min_data_grade,
        invalid_symbols_count=invalid_count,
        skipped_symbols_count=skipped_count,
        warnings=warnings + extra_warnings,
        trades=all_trades,
    )


def run_walk_forward(
    *,
    daily_dir: Path,
    symbols: list[str],
    strategy_id: str,
    start_date: str = "20150101",
    end_date: str = "20260101",
    train_years: int = 3,
    test_years: int = 1,
    min_data_grade: str = "BACKTEST_READY",
    cost_bps: float = 5.0,
    slippage_bps: float = 5.0,
    initial_cash: float = 100000.0,
    max_position_pct: float = 0.95,
    max_symbols: int = 0,
) -> WalkForwardSummary:
    """Run a complete walk-forward validation for a given strategy."""
    strategy = get_strategy(strategy_id)
    if strategy is None:
        summary = WalkForwardSummary(strategy=strategy_id)
        summary.warnings.append(f"strategy_not_found:{strategy_id}")
        summary.warnings.append(f"策略 {strategy_id} 未找到。")
        return summary

    if max_symbols and max_symbols > 0:
        symbols = symbols[:max_symbols]

    windows = generate_windows(start_date, end_date, train_years, test_years)
    if not windows:
        summary = WalkForwardSummary(strategy=strategy_id)
        summary.warnings.append("日期范围不足以生成训练/测试窗口。")
        return summary

    window_results: list[WindowMetrics] = []
    for win in windows:
        result = run_single_window(
            daily_dir=daily_dir,
            symbols=symbols,
            window=win,
            strategy=strategy,
            min_data_grade=min_data_grade,
            cost_bps=cost_bps,
            slippage_bps=slippage_bps,
            initial_cash=initial_cash,
            max_position_pct=max_position_pct,
        )
        window_results.append(result)

    summary = summarize_windows(window_results)
    summary.warnings.extend(dict.fromkeys(warning for result in window_results for warning in result.warnings))
    summary.strategy = strategy.strategy_id
    summary.start_date = start_date
    summary.end_date = end_date
    summary.train_years = train_years
    summary.test_years = test_years
    summary.data_grade_filter = min_data_grade
    summary.cost_bps = cost_bps
    summary.slippage_bps = slippage_bps
    if len(symbols) > 1:
        # Equity here is the SUM of one independent sleeve per symbol that passed
        # the data gates in a window (so the count can differ per window), and
        # every sleeve sits in cash whenever its rule is silent -- including the
        # days before a late-listing symbol's first bar, when it holds its own
        # starting capital rather than counting as zero. Idle cash does not draw
        # down, so aggregate drawdown is smoothed by roughly the symbol count, and
        # each entry deploys only max_position_pct of its sleeve. These are
        # per-rule diagnostics, NOT what an account with a few positions would
        # experience -- for that, use `scripts/qd.py backtest --mode portfolio`
        # (simulate_portfolio_backtest), which models slot contention and a
        # single shared pot. The scripts/run_portfolio_simulation.py named here
        # before never existed.
        summary.warnings.append(
            f"聚合口径提示：equity 是最多 {len(symbols)} 个独立仓位(每个 sleeve 独立本金，"
            "只计入当期通过数据门禁的股票)之和；窗口中途才有首根K线的股票在此之前按自身本金计为闲置现金。"
            f"每次开仓只用 sleeve 的 {max_position_pct:.0%}，空闲现金不产生回撤，"
            "因此这里的年化/最大回撤被分散效应显著平滑，"
            "不等于你用一个账户几个仓位能拿到的收益。"
            "要贴近现实请用 python scripts/qd.py backtest --mode portfolio（共享资金池）。"
        )
    return summary
