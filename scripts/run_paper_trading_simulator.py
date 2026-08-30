"""Local paper-trading simulator CLI (v2.7.2).

This script runs a strategy-driven virtual simulation on local daily bars.
It does NOT connect to any broker, does NOT place real orders, and does NOT
constitute investment advice.  All results are local-only learning aids.

Usage examples:

    # Historical backtest
    python scripts/run_paper_trading_simulator.py \\
        --mode backtest --strategy long \\
        --start-date 2025-01-01 --end-date 2026-05-12 \\
        --initial-cash 100000

    # Live paper trading (reads latest local bars, generates next-day plan)
    python scripts/run_paper_trading_simulator.py \\
        --mode live --strategy long --initial-cash 10000
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from quantdesk.cn_a_share_rules import (
    check_t_plus_one_sell_allowed,
    normalize_order_quantity,
)
from quantdesk.csv_io import read_csv_frame
from quantdesk.paper.trade_costs import (
    DEFAULT_COMMISSION_RATE,
    DEFAULT_MIN_COMMISSION,
    DEFAULT_SLIPPAGE_BPS,
    DEFAULT_STAMP_TAX_RATE,
    DEFAULT_TRANSFER_FEE_RATE,
    TradeCostConfig,
    compute_trade_costs,
)
from quantdesk.paper.market_rules import lot_size_for_symbol, paper_trade_block_reason
from quantdesk.paper.portfolio_contracts import (
    PortfolioContractError,
    resolve_paper_capital_profile,
)
from quantdesk.strategy_signals import (
    add_signal_columns,
    evaluate_exit_signals,
    generate_board_signals,
    generate_long_signals,
    is_st_or_delisting,
)
from quantdesk.utils import safe_float
from quantdesk.version import APP_VERSION

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

DAILY_DIR = PROJECT_ROOT / "data" / "daily"
UNIVERSE_PATH = PROJECT_ROOT / "data" / "universe" / "low_price_learning_symbols.csv"
STATE_DIR = PROJECT_ROOT / "state"
RESULTS_DIR = PROJECT_ROOT / "results"
REPORTS_DIR = PROJECT_ROOT / "reports"

DISCLAIMER = "仅用于策略学习与模拟验证，不接券商，不真实下单，不构成投资建议。"


def _paper_simulation_boundary_fields() -> dict[str, Any]:
    return {
        "is_real_order": False,
        "research_only": True,
        "paper_only": True,
        "no_broker": True,
    }


def _paper_simulation_error(message: str) -> dict[str, Any]:
    return {
        **_paper_simulation_boundary_fields(),
        "ok": False,
        "message": message,
        "disclaimer": DISCLAIMER,
    }


def load_universe() -> pd.DataFrame:
    if not UNIVERSE_PATH.exists():
        return pd.DataFrame(columns=["symbol", "name", "included"])
    df = read_csv_frame(UNIVERSE_PATH, dtype=str)
    required = {"symbol", "name", "included"}
    if not required.issubset(df.columns):
        fallback = pd.DataFrame(columns=["symbol", "name", "included"])
        fallback.attrs["warnings"] = list(df.attrs.get("warnings", [])) + [
            f"missing_columns:{','.join(sorted(required - set(df.columns)))}"
        ]
        return fallback
    df["included"] = df["included"].astype(str).str.lower().isin(["true", "1", "yes"])
    return df


def load_daily(symbol: str) -> pd.DataFrame:
    code = str(symbol).zfill(6)
    for suffix in (".csv", "_qfq.parquet"):
        path = DAILY_DIR / f"{code}{suffix}" if suffix == ".csv" else DAILY_DIR / f"{code}_qfq.parquet"
        if path.exists():
            if suffix == ".csv":
                df = read_csv_frame(path)
            else:
                df = pd.read_parquet(path)
            # Normalize column names
            rename = {}
            for cn, en in [("日期", "date"), ("开盘", "open"), ("收盘", "close"),
                           ("最高", "high"), ("最低", "low"), ("成交量", "volume"),
                           ("成交额", "amount")]:
                if cn in df.columns and en not in df.columns:
                    rename[cn] = en
            if rename:
                df = df.rename(columns=rename)
            if "date" in df.columns:
                df["date"] = pd.to_datetime(df["date"], errors="coerce")
                df = df.dropna(subset=["date"]).sort_values("date").reset_index(drop=True)
            return df
    return pd.DataFrame()


def simulate_backtest(
    *,
    strategy: str,
    start_date: str,
    end_date: str,
    initial_cash: float,
    hold_days: int,
    stop_loss_pct: float,
    take_profit_pct: float,
    max_position_pct: float,
    commission_rate: float,
    stamp_tax_rate_sell: float,
    slippage_rate: float,
    min_commission: float,
    transfer_fee_rate: float = 0.0,
    fill_price: str,
) -> dict[str, Any]:
    boundary_fields = _paper_simulation_boundary_fields()
    universe = load_universe()
    included = universe[universe["included"] == True]
    if included.empty:
        return _paper_simulation_error("低价学习池为空或不存在。")

    cash = float(initial_cash)
    positions: list[dict[str, Any]] = []
    trades: list[dict[str, Any]] = []
    blocked_orders: list[dict[str, Any]] = []
    equity_curve: list[dict[str, Any]] = []
    no_signal_count = 0
    entry_signal_count = 0
    skipped_next_bar_count = 0
    skipped_fill_price_count = 0
    pending_created_count = 0
    pending_filled_count = 0
    pending_blocked_count = 0
    pending_entry_events: list[dict[str, Any]] = []

    start_dt = pd.to_datetime(start_date, errors="coerce")
    end_dt = pd.to_datetime(end_date, errors="coerce")

    # Collect all daily bars for included symbols
    all_bars: dict[str, pd.DataFrame] = {}
    for _, row in included.iterrows():
        sym = str(row["symbol"]).zfill(6)
        name = str(row.get("name") or sym)
        if is_st_or_delisting(name):
            continue
        df = load_daily(sym)
        if df.empty or len(df) < 60:
            continue
        df = add_signal_columns(df)
        if start_dt is not pd.NaT:
            df = df[df["date"] >= start_dt]
        if end_dt is not pd.NaT:
            df = df[df["date"] <= end_dt]
        df = df.sort_values("date").reset_index(drop=True)
        if not df.empty:
            all_bars[sym] = df

    if not all_bars:
        return _paper_simulation_error("没有可用的本地日线数据。")

    # Get union of all dates
    all_dates = sorted(set(d for df in all_bars.values() for d in df["date"].tolist()))

    # P0-5: next-open orders are booked on the REAL execution date, not the signal
    # date. A signal on day T enqueues a pending order; the fill (cash outflow,
    # position, entry_date, and T+1 start) all happen at day T+1's open. On the
    # signal day itself the order must not touch cash or NAV. The close-fill path
    # is unchanged and still books on the same bar.
    pending_entries: list[dict[str, Any]] = []

    def _record_entry_block(
        sym: str,
        date_str: str,
        reason: str,
        *,
        signal_date: str = "",
        scheduled_execution_date: str = "",
        detail: str = "",
    ) -> None:
        blocked_orders.append({
            **boundary_fields,
            "symbol": sym,
            "date": date_str,
            "signal_date": signal_date,
            "scheduled_execution_date": scheduled_execution_date,
            "reason": reason,
            "reason_detail": detail,
        })

    def _execute_entry(
        sym: str,
        name: str,
        exec_date_str: str,
        raw_entry_price: Any,
        *,
        signal_date: str,
        scheduled_execution_date: str,
    ) -> bool:
        nonlocal cash, skipped_fill_price_count
        entry_price = safe_float(raw_entry_price)
        if entry_price is None or entry_price <= 0:
            skipped_fill_price_count += 1
            _record_entry_block(
                sym,
                exec_date_str,
                "invalid_fill_price",
                signal_date=signal_date,
                scheduled_execution_date=scheduled_execution_date,
            )
            return False

        entry_price_adj = entry_price * (1.0 + slippage_rate)
        max_alloc = cash * (max_position_pct / 100.0)
        raw_qty = int(max_alloc / entry_price_adj)
        lot_size = lot_size_for_symbol(sym, name=name)
        norm = normalize_order_quantity(
            raw_qty,
            price=entry_price_adj,
            cash=cash,
            side="buy",
            lot_size=lot_size,
        )
        qty = norm["quantity"]
        if norm["blocked"] or qty <= 0:
            _record_entry_block(
                sym,
                exec_date_str,
                "invalid_lot_or_quantity",
                signal_date=signal_date,
                scheduled_execution_date=scheduled_execution_date,
                detail=str(norm.get("reason") or ""),
            )
            return False

        amount = entry_price_adj * qty
        fees = compute_trade_costs(
            side="buy",
            gross=amount,
            config=TradeCostConfig(
                commission_rate=commission_rate,
                min_commission=min_commission,
                stamp_tax_rate=stamp_tax_rate_sell,
                transfer_fee_rate=transfer_fee_rate,
                slippage_bps=0.0,
            ),
        )
        total_cost = amount + fees["total_fee"]
        if total_cost > cash + 1e-9:
            _record_entry_block(
                sym,
                exec_date_str,
                "insufficient_cash",
                signal_date=signal_date,
                scheduled_execution_date=scheduled_execution_date,
            )
            return False

        cash -= total_cost
        positions.append({
            **boundary_fields,
            "symbol": sym,
            "name": name,
            "signal_date": signal_date,
            "scheduled_execution_date": scheduled_execution_date,
            "entry_date": exec_date_str,
            "entry_price": round(entry_price_adj, 4),
            "quantity": qty,
            "cost": round(total_cost, 4),
            "holding_days": 0,
            "entry_fee": fees["total_fee"],
            "entry_commission": fees["commission"],
            "entry_stamp_duty": fees["stamp_duty"],
            "entry_transfer_fee": fees["transfer_fee"],
            "entry_slippage_fee": fees["slippage"],
            "entry_fee_model": fees["fee_model"],
        })
        return True

    for date in all_dates:
        date_str = pd.Timestamp(date).strftime("%Y-%m-%d")

        # 1. Check exits for current positions
        closed_today: list[str] = []
        for pos in list(positions):
            sym = pos["symbol"]
            df = all_bars.get(sym)
            if df is None:
                continue
            day_rows = df[df["date"] == date]
            if day_rows.empty:
                continue
            bar = day_rows.iloc[0].to_dict()

            # T+1 check
            t1 = check_t_plus_one_sell_allowed(
                entry_execution_date=pos["entry_date"],
                current_execution_date=date_str,
            )
            if not t1["allowed"]:
                continue

            should_exit, reason = evaluate_exit_signals(
                bar,
                entry_price=pos["entry_price"],
                holding_days=pos["holding_days"],
                max_holding_days=hold_days,
                stop_loss_pct=stop_loss_pct,
                take_profit_pct=take_profit_pct,
            )
            if should_exit:
                exit_price = safe_float(bar.get("open") if fill_price != "close" else bar.get("close"))
                if exit_price is None or exit_price <= 0:
                    continue
                exit_price_adj = exit_price * (1.0 - slippage_rate)
                amount = exit_price_adj * pos["quantity"]
                fees = compute_trade_costs(
                    side="sell",
                    gross=amount,
                    config=TradeCostConfig(
                        commission_rate=commission_rate,
                        min_commission=min_commission,
                        stamp_tax_rate=stamp_tax_rate_sell,
                        transfer_fee_rate=transfer_fee_rate,
                        # This simulator applies slippage by shifting the fill
                        # price, so do not add a second slippage fee here.
                        slippage_bps=0.0,
                    ),
                )
                proceeds = amount - fees["total_fee"]
                pnl = proceeds - pos["cost"]
                cash += proceeds
                trades.append({
                    **boundary_fields,
                    "symbol": sym, "name": pos["name"],
                    "entry_date": pos["entry_date"], "exit_date": date_str,
                    "entry_price": pos["entry_price"], "exit_price": round(exit_price_adj, 4),
                    "quantity": pos["quantity"], "pnl": round(pnl, 2),
                    "return_pct": round(pnl / pos["cost"] * 100, 2) if pos["cost"] else 0,
                    "exit_reason": reason, "holding_days": pos["holding_days"],
                    "exit_fee": fees["total_fee"],
                    "exit_commission": fees["commission"],
                    "exit_stamp_duty": fees["stamp_duty"],
                    "exit_transfer_fee": fees["transfer_fee"],
                    "exit_slippage_fee": fees["slippage"],
                    "exit_fee_model": fees["fee_model"],
                    "entry_commission": pos.get("entry_commission", 0.0),
                    "entry_stamp_duty": pos.get("entry_stamp_duty", 0.0),
                    "entry_transfer_fee": pos.get("entry_transfer_fee", 0.0),
                    "entry_slippage_fee": pos.get("entry_slippage_fee", 0.0),
                    "entry_fee_model": pos.get("entry_fee_model", ""),
                })
                closed_today.append(sym)

        positions = [p for p in positions if p["symbol"] not in closed_today]

        # 2. Update holding days
        for pos in positions:
            pos["holding_days"] += 1

        # 3. Execute next-open events on their real scheduled date. Existing
        # positions were evaluated for exits above, so a newly filled position
        # cannot be sold in this same loop; its T+1 clock begins today.
        due_events = [
            event
            for event in pending_entries
            if str(event.get("scheduled_execution_date") or "") == date_str
        ]
        pending_entries = [
            event
            for event in pending_entries
            if str(event.get("scheduled_execution_date") or "") != date_str
        ]
        for event in due_events:
            sym = str(event.get("symbol") or "")
            name = str(event.get("name") or sym)
            signal_date_text = str(event.get("signal_date") or "")
            scheduled_date = str(event.get("scheduled_execution_date") or date_str)
            event_result = {**event, "execution_date": date_str}

            if any(pos.get("symbol") == sym for pos in positions):
                _record_entry_block(
                    sym,
                    date_str,
                    "position_already_present",
                    signal_date=signal_date_text,
                    scheduled_execution_date=scheduled_date,
                )
                pending_blocked_count += 1
                pending_entry_events.append({**event_result, "status": "blocked", "reason": "position_already_present"})
                continue

            df = all_bars.get(sym)
            if df is None:
                _record_entry_block(
                    sym,
                    date_str,
                    "missing_symbol_bars",
                    signal_date=signal_date_text,
                    scheduled_execution_date=scheduled_date,
                )
                pending_blocked_count += 1
                pending_entry_events.append({**event_result, "status": "blocked", "reason": "missing_symbol_bars"})
                continue
            execution_rows = df[df["date"] == date]
            if execution_rows.empty:
                _record_entry_block(
                    sym,
                    date_str,
                    "missing_execution_bar",
                    signal_date=signal_date_text,
                    scheduled_execution_date=scheduled_date,
                )
                pending_blocked_count += 1
                pending_entry_events.append({**event_result, "status": "blocked", "reason": "missing_execution_bar"})
                continue

            execution_bar = execution_rows.iloc[0].to_dict()
            tradeability_reason = paper_trade_block_reason(
                "BUY",
                execution_bar,
                symbol=sym,
                name=name,
            )
            if tradeability_reason:
                _record_entry_block(
                    sym,
                    date_str,
                    "market_tradeability_blocked",
                    detail=tradeability_reason,
                    signal_date=signal_date_text,
                    scheduled_execution_date=scheduled_date,
                )
                pending_blocked_count += 1
                pending_entry_events.append({**event_result, "status": "blocked", "reason": "market_tradeability_blocked"})
                continue

            filled = _execute_entry(
                sym,
                name,
                date_str,
                execution_bar.get("open"),
                signal_date=signal_date_text,
                scheduled_execution_date=scheduled_date,
            )
            if filled:
                pending_filled_count += 1
                pending_entry_events.append({**event_result, "status": "filled", "reason": ""})
            else:
                pending_blocked_count += 1
                reason = str(blocked_orders[-1].get("reason") or "entry_blocked") if blocked_orders else "entry_blocked"
                pending_entry_events.append({**event_result, "status": "blocked", "reason": reason})

        # 4. Generate entry signals
        for sym, df in all_bars.items():
            if any(p["symbol"] == sym for p in positions):
                continue
            day_rows = df[df["date"] == date]
            if day_rows.empty:
                continue
            bar = day_rows.iloc[0].to_dict()
            bar["date"] = date_str

            # Check signal
            if strategy == "long":
                sigs = generate_long_signals(df[df["date"] <= date].tail(30), symbol=sym, name=bar.get("name", sym))
                has_signal = any(s["signal_date"] == date_str for s in sigs)
            else:
                sigs = generate_board_signals(df[df["date"] <= date].tail(5), symbol=sym, name=bar.get("name", sym))
                has_signal = any(s["signal_date"] == date_str for s in sigs)

            if not has_signal:
                no_signal_count += 1
                continue
            entry_signal_count += 1

            matching_signals = [
                signal
                for signal in sigs
                if str(signal.get("signal_date") or "") == date_str
            ]
            idx = df.index[df["date"] == date].tolist()
            if not idx:
                continue

            if fill_price in {"conservative", "open"}:
                next_idx = idx[0] + 1
                if next_idx >= len(df):
                    skipped_next_bar_count += 1
                    _record_entry_block(
                        sym,
                        date_str,
                        "no_next_bar_for_conservative_fill",
                        signal_date=date_str,
                    )
                    continue
                scheduled_date = pd.Timestamp(df.iloc[next_idx]["date"]).strftime("%Y-%m-%d")
                if any(event.get("symbol") == sym for event in pending_entries):
                    _record_entry_block(
                        sym,
                        date_str,
                        "duplicate_pending_entry",
                        signal_date=date_str,
                        scheduled_execution_date=scheduled_date,
                    )
                    continue

                event = {
                    **boundary_fields,
                    "event_type": "pending_paper_entry",
                    "status": "pending",
                    "symbol": sym,
                    "name": str(bar.get("name") or sym),
                    "strategy": strategy,
                    "signal_date": date_str,
                    "scheduled_execution_date": scheduled_date,
                    "fill_price_policy": "next_open",
                    "sizing_inputs": {
                        "max_position_pct": float(max_position_pct),
                        "cash_evaluated_at": "execution_date",
                    },
                }
                pending_entries.append(event)
                pending_created_count += 1
                pending_entry_events.append(dict(event))
                for _duplicate in matching_signals[1:]:
                    _record_entry_block(
                        sym,
                        date_str,
                        "duplicate_pending_entry",
                        signal_date=date_str,
                        scheduled_execution_date=scheduled_date,
                    )
                    pending_blocked_count += 1
                continue

            _execute_entry(
                sym,
                str(bar.get("name") or sym),
                date_str,
                bar.get("close"),
                signal_date=date_str,
                scheduled_execution_date=date_str,
            )
            continue

        # 5. Record equity
        market_value = 0.0
        for pos in positions:
            sym = pos["symbol"]
            df = all_bars.get(sym)
            if df is None:
                continue
            day_rows = df[df["date"] == date]
            if day_rows.empty:
                continue
            close = safe_float(day_rows.iloc[0].get("close"))
            if close:
                market_value += close * pos["quantity"]

        equity_curve.append({
            "date": date_str,
            "cash": round(cash, 2),
            "market_value": round(market_value, 2),
            "equity": round(cash + market_value, 2),
        })

    # Final mark-to-market
    final_equity = cash
    for pos in positions:
        sym = pos["symbol"]
        df = all_bars.get(sym)
        if df is not None and not df.empty:
            last_close = safe_float(df.iloc[-1].get("close"))
            if last_close:
                final_equity += last_close * pos["quantity"]

    total_return_pct = round((final_equity / initial_cash - 1.0) * 100, 4) if initial_cash else 0.0
    realized_pnl = sum(t["pnl"] for t in trades)

    result = {
        **boundary_fields,
        "ok": True,
        "mode": "backtest",
        "strategy": strategy,
        "app_version": APP_VERSION,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "initial_cash": initial_cash,
        "cash": round(cash, 2),
        "market_value": round(final_equity - cash, 2),
        "total_equity": round(final_equity, 2),
        "realized_pnl": round(realized_pnl, 2),
        "unrealized_pnl": round(final_equity - cash - sum(p["cost"] for p in positions), 2),
        "total_return_pct": total_return_pct,
        "trade_count": len(trades),
        "blocked_order_count": len(blocked_orders),
        "entry_signal_count": entry_signal_count,
        "no_signal_count": no_signal_count,
        "skipped_next_bar_count": skipped_next_bar_count,
        "skipped_fill_price_count": skipped_fill_price_count,
        "pending_order_count": len(pending_entries),
        "pending_created_count": pending_created_count,
        "pending_filled_count": pending_filled_count,
        "pending_blocked_count": pending_blocked_count,
        "fill_status_distribution": {
            "filled": len(trades) + len(positions),
            "blocked": len(blocked_orders),
            "no_signal": no_signal_count,
            "no_next_bar": skipped_next_bar_count,
            "invalid_fill_price": skipped_fill_price_count,
            "pending_created": pending_created_count,
            "pending_filled": pending_filled_count,
            "pending_blocked": pending_blocked_count,
            "pending_remaining": len(pending_entries),
        },
        "positions": positions,
        "trades": trades[-50:],
        "blocked_orders": blocked_orders[-20:],
        "pending_entries": pending_entries[-20:],
        "pending_entry_events": pending_entry_events[-100:],
        "equity_curve": equity_curve[-250:],
        "risk_notes": [
            "模拟使用本地日线数据，不代表真实成交。",
            "涨跌停、停牌等约束仅做简化判断。",
        ],
        "disclaimer": DISCLAIMER,
    }
    return result


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Local paper-trading simulator. Does NOT connect to brokers or place real orders."
    )
    parser.add_argument("--mode", choices=["backtest", "live"], default="backtest")
    parser.add_argument("--strategy", choices=["long", "board"], default="long")
    parser.add_argument("--start-date", default="",
                        help="Backtest start date (YYYY-MM-DD). Empty = auto from end_date - lookback_days.")
    parser.add_argument("--end-date", default="",
                        help="End date (YYYY-MM-DD). Empty = latest available daily bar date.")
    parser.add_argument("--lookback-days", type=int, default=120,
                        help="Trading days to look back when start_date is not specified.")
    parser.add_argument(
        "--initial-cash",
        type=float,
        default=None,
        help="Backtest default=100000 research baseline; live default=10000 user_10k paper profile.",
    )
    parser.add_argument(
        "--scenario-id",
        choices=["user_10k", "acceptance_100k"],
        default="user_10k",
        help="Paper-capital scenario for --mode live. 100k requires acceptance_100k.",
    )
    parser.add_argument("--hold-days", type=int, default=20)
    parser.add_argument("--stop-loss-pct", type=float, default=-8.0)
    parser.add_argument("--take-profit-pct", type=float, default=15.0)
    parser.add_argument("--max-position-pct", type=float, default=20.0)
    parser.add_argument("--commission-rate", type=float, default=DEFAULT_COMMISSION_RATE)
    parser.add_argument("--stamp-tax-rate-sell", type=float, default=DEFAULT_STAMP_TAX_RATE)
    parser.add_argument("--slippage-rate", type=float, default=DEFAULT_SLIPPAGE_BPS / 10_000)
    parser.add_argument("--min-commission", type=float, default=DEFAULT_MIN_COMMISSION)
    parser.add_argument("--transfer-fee-rate", type=float, default=DEFAULT_TRANSFER_FEE_RATE)
    parser.add_argument("--fill-price", choices=["open", "close", "conservative"], default="conservative")
    return parser


def resolve_simulation_initial_cash(
    *,
    mode: str,
    requested_initial_cash: float | None,
    scenario_id: str | None,
) -> float:
    """Resolve mode-specific capital without changing the research baseline."""

    normalized_mode = str(mode or "").strip().lower()
    if normalized_mode not in {"backtest", "live"}:
        raise PortfolioContractError("simulation_mode_invalid")
    if normalized_mode == "backtest":
        value = 100_000.0 if requested_initial_cash is None else float(requested_initial_cash)
        if not math.isfinite(value) or value <= 0:
            raise PortfolioContractError("backtest_initial_cash_invalid")
        return value
    profile = resolve_paper_capital_profile(
        scenario_id=scenario_id,
        requested_initial_capital=requested_initial_cash,
    )
    return profile.initial_capital


def main() -> int:
    parser = build_arg_parser()
    args = parser.parse_args()

    logging.info("Paper trading simulator: mode=%s strategy=%s", args.mode, args.strategy)
    logging.info("Disclaimer: %s", DISCLAIMER)
    try:
        resolved_initial_cash = resolve_simulation_initial_cash(
            mode=args.mode,
            requested_initial_cash=args.initial_cash,
            scenario_id=args.scenario_id,
        )
    except (PortfolioContractError, TypeError, ValueError) as exc:
        logging.error("Paper capital profile rejected: %s", exc)
        return 2

    if args.mode == "live":
        # Live mode: use latest available date, look back for indicators
        args.end_date = args.end_date or datetime.now().strftime("%Y-%m-%d")
        # start_date auto-computed inside simulate_backtest if empty

    # Auto-compute dates if not provided
    if not args.end_date:
        args.end_date = datetime.now().strftime("%Y-%m-%d")
    if not args.start_date:
        # Compute from end_date - lookback_days (approximate with calendar days * 1.5)
        try:
            end_dt = pd.to_datetime(args.end_date)
            start_dt = end_dt - pd.offsets.BDay(args.lookback_days)
            args.start_date = start_dt.strftime("%Y-%m-%d")
        except Exception:
            args.start_date = "2025-01-01"

    result = simulate_backtest(
        strategy=args.strategy,
        start_date=args.start_date,
        end_date=args.end_date,
        initial_cash=resolved_initial_cash,
        hold_days=args.hold_days,
        stop_loss_pct=args.stop_loss_pct,
        take_profit_pct=args.take_profit_pct,
        max_position_pct=args.max_position_pct,
        commission_rate=args.commission_rate,
        stamp_tax_rate_sell=args.stamp_tax_rate_sell,
        slippage_rate=args.slippage_rate,
        min_commission=args.min_commission,
        transfer_fee_rate=args.transfer_fee_rate,
        fill_price=args.fill_price,
    )

    result["mode"] = args.mode
    result["scenario_id"] = args.scenario_id if args.mode == "live" else "research_backtest"

    # Save results
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    output_path = RESULTS_DIR / "paper_trading_latest.json"
    output_path.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    logging.info("Result saved: %s", output_path)
    logging.info("Total equity: %.2f, Return: %.2f%%", result.get("total_equity", 0), result.get("total_return_pct", 0))
    print(json.dumps({"ok": result.get("ok"), "total_equity": result.get("total_equity"), "total_return_pct": result.get("total_return_pct"), "trade_count": result.get("trade_count"), "disclaimer": DISCLAIMER}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
