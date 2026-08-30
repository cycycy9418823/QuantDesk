from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any


def json_safe(value: object) -> object:
    """Convert pandas/numpy values into JSON-safe primitive values."""

    from quantdesk.backtest import np, pd

    if isinstance(value, dict):
        return {str(key): json_safe(item) for key, item in value.items()}
    if isinstance(value, list):
        return [json_safe(item) for item in value]
    if isinstance(value, tuple):
        return [json_safe(item) for item in value]
    if isinstance(value, (np.integer,)):
        return int(value)
    if isinstance(value, (np.floating, float)):
        number = float(value)
        return number if math.isfinite(number) else None
    if isinstance(value, pd.Timestamp):
        return value.strftime("%Y-%m-%d")
    if isinstance(value, Path):
        return str(value)
    return value


def export_backtest_result(result: Any, symbol: str, output_dir: Path | None = None) -> dict[str, Path]:
    """Write the standard backtest CSV, JSON, and risk-summary artifacts."""

    from quantdesk.backtest import (
        EQUITY_CURVE_COLUMNS,
        POSITIONS_COLUMNS,
        REBALANCE_LOG_COLUMNS,
        REPORT_DIR,
        TRADE_COLUMNS,
        np,
        pd,
        risk_summary_rows_from_result,
        save_csv_safely,
        summary_rows_from_metrics,
    )

    output_dir = output_dir or REPORT_DIR
    output_dir.mkdir(parents=True, exist_ok=True)
    safe_symbol = "".join(ch for ch in str(symbol) if ch.isalnum() or ch in ("_", "-")) or "portfolio"
    prefix = f"backtest_{safe_symbol}"
    trades = pd.DataFrame(result.trades, columns=TRADE_COLUMNS)
    equity_curve = result.equity_curve.copy()
    for column in EQUITY_CURVE_COLUMNS:
        if column not in equity_curve.columns:
            equity_curve[column] = np.nan
    equity_curve = equity_curve[EQUITY_CURVE_COLUMNS]
    blocked = trades[trades["blocked"].fillna(False).astype(bool)].copy() if "blocked" in trades.columns else pd.DataFrame(columns=TRADE_COLUMNS)
    positions = result.positions.copy() if not result.positions.empty else pd.DataFrame(columns=POSITIONS_COLUMNS)
    for column in POSITIONS_COLUMNS:
        if column not in positions.columns:
            positions[column] = np.nan
    positions = positions[POSITIONS_COLUMNS]
    rebalance_log = result.rebalance_log.copy() if not result.rebalance_log.empty else pd.DataFrame(columns=REBALANCE_LOG_COLUMNS)
    for column in REBALANCE_LOG_COLUMNS:
        if column not in rebalance_log.columns:
            rebalance_log[column] = np.nan
    rebalance_log = rebalance_log[REBALANCE_LOG_COLUMNS]
    summary = result.summary.copy() if not result.summary.empty else summary_rows_from_metrics(result.metrics, result.assumptions, result.extra_metrics)
    risk_summary = risk_summary_rows_from_result(result)
    result.assumptions["backtest_risk_summary"] = {
        str(row["metric"]): json_safe(row["value"])
        for _, row in risk_summary.iterrows()
    }
    trades_path = save_csv_safely(trades, output_dir / f"{prefix}_trades.csv")
    equity_path = save_csv_safely(equity_curve, output_dir / f"{prefix}_equity_curve.csv")
    blocked_path = save_csv_safely(blocked, output_dir / f"{prefix}_blocked_trades.csv")
    positions_path = save_csv_safely(positions, output_dir / f"{prefix}_positions.csv")
    rebalance_path = save_csv_safely(rebalance_log, output_dir / f"{prefix}_rebalance_log.csv")
    summary_path = save_csv_safely(summary, output_dir / f"{prefix}_summary.csv")
    risk_summary_path = save_csv_safely(risk_summary, output_dir / f"{prefix}_risk_summary.csv")
    assumptions_path = output_dir / f"{prefix}_assumptions.json"
    assumptions_path.write_text(json.dumps(json_safe(result.assumptions), ensure_ascii=False, indent=2), encoding="utf-8")
    return {
        "trades": trades_path,
        "equity_curve": equity_path,
        "blocked_trades": blocked_path,
        "positions": positions_path,
        "rebalance_log": rebalance_path,
        "summary": summary_path,
        "risk_summary": risk_summary_path,
        "assumptions": assumptions_path,
    }


def risk_summary_rows_from_result(result: Any) -> Any:
    from quantdesk.backtest import (
        RISK_SUMMARY_COLUMNS,
        TRADE_COLUMNS,
        blocked_reason_counts,
        math,
        np,
        pd,
        safe_number,
    )

    assumptions = result.assumptions or {}
    trades = pd.DataFrame(result.trades, columns=TRADE_COLUMNS)
    counts = blocked_reason_counts(trades)
    blocked_total = int(trades["blocked"].fillna(False).astype(bool).sum()) if "blocked" in trades.columns else 0
    industry_weights = assumptions.get("industry_weights_latest", {})
    max_industry_weight = np.nan
    if isinstance(industry_weights, dict) and industry_weights:
        values = pd.to_numeric(pd.Series(list(industry_weights.values())), errors="coerce").dropna()
        if not values.empty:
            max_industry_weight = float(values.max())
    max_single_rebalance_turnover = np.nan
    if not trades.empty and {"date", "gross_amount", "blocked"}.issubset(trades.columns):
        executed = trades[~trades["blocked"].fillna(False).astype(bool)].copy()
        grouped = pd.to_numeric(executed.get("gross_amount", pd.Series(dtype=float)), errors="coerce").fillna(0).groupby(executed["date"]).sum()
        initial_cash = safe_number(assumptions.get("initial_cash"), np.nan)
        if not grouped.empty and math.isfinite(initial_cash) and initial_cash > 0:
            max_single_rebalance_turnover = float(grouped.max() / initial_cash)
    rows = [
        ("excluded_by_data_error", assumptions.get("excluded_by_data_error_count", 0)),
        ("included_with_data_warning", assumptions.get("included_with_data_warning_count", 0)),
        ("included_with_fallback", assumptions.get("included_with_fallback_count", 0)),
        ("blocked_trades_total", blocked_total),
        ("blocked_limit_up", counts["limit_up"]),
        ("blocked_limit_down", counts["limit_down"]),
        ("blocked_suspended", counts["suspended"]),
        ("blocked_cash_insufficient", counts["cash_insufficient"]),
        ("blocked_turnover_limit", counts["turnover_limit"]),
        ("max_industry_weight", max_industry_weight),
        ("industry_concentration_warning", assumptions.get("industry_warning", "") or ""),
        ("max_single_rebalance_turnover", max_single_rebalance_turnover),
        ("turnover_limit_triggered", counts["turnover_limit"] > 0),
        ("warning", assumptions.get("warning", "")),
    ]
    return pd.DataFrame(rows, columns=RISK_SUMMARY_COLUMNS)
