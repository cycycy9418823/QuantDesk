from __future__ import annotations

import math
from pathlib import Path
from typing import Any

import pandas as pd

from quantdesk.factor_evaluation import PIT_UNIVERSE_WARNING
from quantdesk.point_in_time_universe import (
    build_listing_index,
    filter_rows_for_pit_membership,
)
from quantdesk.paper.market_rules import round_notional_down_to_lot_quantity
from quantdesk.price_basis import DIVERGENT_ALIAS_WARNING, FORWARD_FALLBACK_WARNING, price_basis_labels
from quantdesk.shared import now_text, read_json, write_json
from quantdesk.version import API_SCHEMA_VERSION, APP_VERSION, PACKAGE_VERSION

DISCLAIMER = (
    "Research diagnostics only. Not investment advice. Combined-score backtest "
    "does not change default screen, backtest, daily workflow, trading signals, "
    "scoring weights, broker boundaries, or real orders."
)
EXECUTION_ASSUMPTION = "Signal date t is executed after execution_lag_days; default entry is the next available trading date."
SHORT_ASSUMPTION = "Long-short portfolios are research diagnostics only; A-share short-sale constraints may make them non-executable."


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        number = float(value)
    except Exception:
        return default
    return number if math.isfinite(number) else default


def _json_safe(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_json_safe(item) for item in value]
    if isinstance(value, tuple):
        return [_json_safe(item) for item in value]
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if hasattr(value, "item"):
        try:
            return _json_safe(value.item())
        except Exception:
            pass
    try:
        if pd.isna(value):
            return None
    except Exception:
        pass
    return value


def load_combined_score_payload(path: str | Path) -> dict[str, Any]:
    payload = read_json(path, {})
    if not isinstance(payload, dict):
        return {"warnings": [f"missing_combined_score: invalid JSON payload at {path}"]}
    warnings = list(payload.get("warnings") or [])
    task_type = str(payload.get("task_type") or "")
    if task_type and task_type not in {"factor_combination", "combined_score"}:
        warnings.append(f"incompatible_combined_score_task_type: {task_type}")
    if not payload.get("data_source"):
        warnings.append("combined_score_data_source_missing")
    payload["warnings"] = list(dict.fromkeys(str(item) for item in warnings if item))
    return payload


def extract_score_panel(payload: dict[str, Any]) -> pd.DataFrame:
    warnings: list[str] = []
    rows = payload.get("score_panel") or payload.get("scores") or payload.get("score_rows")
    if isinstance(rows, list) and rows:
        frame = pd.DataFrame(rows)
    else:
        top = payload.get("top_candidates")
        if isinstance(top, list) and top:
            frame = pd.DataFrame(top)
            warnings.append("score_panel_unavailable: using top_candidates only; research backtest may be insufficient")
        else:
            frame = pd.DataFrame()
            warnings.append("missing_combined_score: no score_panel or top_candidates found")
    if not frame.empty:
        rename = {"trade_date": "date", "code": "symbol", "ts_code": "symbol", "score": "combined_score"}
        frame = frame.rename(columns={column: rename.get(str(column), str(column)) for column in frame.columns})
        for column in ("date", "symbol", "combined_score"):
            if column not in frame.columns:
                warnings.append(f"combined_score_required_column_missing:{column}")
        if "date" in frame.columns:
            frame["date"] = pd.to_datetime(frame["date"], errors="coerce")
        if "symbol" in frame.columns:
            frame["symbol"] = frame["symbol"].astype(str)
        if "combined_score" in frame.columns:
            frame["combined_score"] = pd.to_numeric(frame["combined_score"], errors="coerce")
        if {"date", "symbol", "combined_score"}.issubset(frame.columns):
            frame = frame.dropna(subset=["date", "symbol", "combined_score"])
            frame = frame[frame["combined_score"].map(math.isfinite)]
    frame.attrs["warnings"] = list(dict.fromkeys(warnings))
    return frame.reset_index(drop=True)


def build_ranked_portfolios(
    score_panel: pd.DataFrame,
    *,
    date_col: str = "date",
    symbol_col: str = "symbol",
    score_col: str = "combined_score",
    mode: str = "top_n",
    top_n: int = 20,
    quantile: float = 0.2,
    long_short: bool = False,
    min_cross_section: int = 2,
) -> dict[str, Any]:
    warnings: list[str] = []
    if score_panel.empty:
        return {"portfolios": [], "warnings": ["missing_combined_score: score panel is empty"]}
    required = {date_col, symbol_col, score_col}
    if not required.issubset(score_panel.columns):
        missing = sorted(required - set(score_panel.columns))
        return {"portfolios": [], "warnings": [f"combined_score_required_columns_missing:{','.join(missing)}"]}
    frame = score_panel.copy()
    frame[date_col] = pd.to_datetime(frame[date_col], errors="coerce")
    frame[score_col] = pd.to_numeric(frame[score_col], errors="coerce")
    frame = frame.dropna(subset=[date_col, symbol_col, score_col])
    frame = frame[frame[score_col].map(math.isfinite)]
    portfolios: list[dict[str, Any]] = []
    mode = str(mode or "top_n")
    quantile = min(max(_safe_float(quantile, 0.2), 0.01), 0.5)
    for date, group in frame.groupby(date_col):
        ranked = group.sort_values(score_col, ascending=False).reset_index(drop=True)
        count = len(ranked)
        if count < max(1, int(min_cross_section)):
            warnings.append(f"insufficient_cross_section:{date.date() if hasattr(date, 'date') else date}")
            continue
        q_count = max(1, int(math.ceil(count * quantile)))
        if mode == "long_short_quantile" or long_short:
            if count < q_count * 2:
                warnings.append(f"insufficient_long_short_cross_section:{date.date() if hasattr(date, 'date') else date}")
                continue
            long_symbols = ranked.head(q_count)[symbol_col].astype(str).tolist()
            short_symbols = ranked.tail(q_count)[symbol_col].astype(str).tolist()
            portfolios.append({
                "signal_date": pd.Timestamp(date),
                "long_symbols": long_symbols,
                "short_symbols": short_symbols,
                "selected_symbols": long_symbols,
                "weights": {symbol: 1.0 / len(long_symbols) for symbol in long_symbols},
                "short_weights": {symbol: -1.0 / len(short_symbols) for symbol in short_symbols},
            })
        else:
            select_count = min(max(1, int(top_n)), count) if mode == "top_n" else q_count
            selected = ranked.head(select_count)[symbol_col].astype(str).tolist()
            if count < int(top_n) and mode == "top_n":
                warnings.append(f"top_n_larger_than_cross_section:{date.date() if hasattr(date, 'date') else date}")
            portfolios.append({
                "signal_date": pd.Timestamp(date),
                "selected_symbols": selected,
                "long_symbols": selected,
                "short_symbols": [],
                "weights": {symbol: 1.0 / len(selected) for symbol in selected},
                "short_weights": {},
            })
    return {"portfolios": portfolios, "warnings": list(dict.fromkeys(warnings))}


def _prepare_price_panel(price_panel: pd.DataFrame) -> pd.DataFrame:
    if price_panel.empty:
        return pd.DataFrame()
    frame = price_panel.copy()
    rename = {"trade_date": "date", "code": "symbol", "ts_code": "symbol"}
    frame = frame.rename(columns={column: rename.get(str(column), str(column)) for column in frame.columns})
    if not {"date", "symbol", "close"}.issubset(frame.columns):
        return pd.DataFrame()
    frame["date"] = pd.to_datetime(frame["date"], errors="coerce")
    frame["symbol"] = frame["symbol"].astype(str)
    frame["close"] = pd.to_numeric(frame["close"], errors="coerce")
    frame = frame.dropna(subset=["date", "symbol", "close"])
    frame = frame[frame["close"] > 0]
    return frame.sort_values(["symbol", "date"]).reset_index(drop=True)


def _price_return(price_map: dict[tuple[str, pd.Timestamp], float], symbol: str, entry: pd.Timestamp, exit_date: pd.Timestamp) -> float | None:
    entry_price = price_map.get((symbol, entry))
    exit_price = price_map.get((symbol, exit_date))
    if not entry_price or not exit_price:
        return None
    return _safe_float(exit_price / entry_price - 1.0)


def _merge_pit_stage_audits(
    stages: dict[str, dict[str, Any]],
    pit_status: dict[str, Any],
    *,
    enabled: bool,
) -> dict[str, Any]:
    reason_counts: dict[str, int] = {}
    by_date: dict[str, dict[str, Any]] = {}
    warnings: list[str] = []
    requested = 0
    included = 0
    excluded = 0
    for audit in stages.values():
        requested += int(audit.get("requested_row_count") or 0)
        included += int(audit.get("included_row_count") or 0)
        excluded += int(audit.get("excluded_row_count") or 0)
        warnings.extend(str(item) for item in audit.get("warnings") or [] if item)
        for reason, count in (audit.get("reason_counts") or {}).items():
            reason_counts[str(reason)] = reason_counts.get(str(reason), 0) + int(count or 0)
        for date, item in (audit.get("by_date") or {}).items():
            target = by_date.setdefault(
                str(date),
                {
                    "requested_row_count": 0,
                    "included_row_count": 0,
                    "excluded_row_count": 0,
                    "reason_counts": {},
                },
            )
            for key in ("requested_row_count", "included_row_count", "excluded_row_count"):
                target[key] += int(item.get(key) or 0)
            for reason, count in (item.get("reason_counts") or {}).items():
                target["reason_counts"][str(reason)] = (
                    target["reason_counts"].get(str(reason), 0) + int(count or 0)
                )
    for item in by_date.values():
        item["reason_counts"] = dict(sorted(item["reason_counts"].items()))
    checked_dates = sorted(by_date)
    coverage_info = {
        "list_date_coverage": _safe_float(pit_status.get("list_date_coverage")),
        "delist_date_coverage": _safe_float(pit_status.get("delist_date_coverage")),
        "st_coverage": _safe_float(pit_status.get("st_coverage")),
        "suspension_coverage": _safe_float(pit_status.get("suspend_coverage")),
        "metadata_coverage": dict(pit_status.get("metadata_coverage") or {}),
    }
    return {
        "pit_universe_used": bool(enabled),
        "target_date": checked_dates[-1] if checked_dates else "",
        "checked_dates": checked_dates,
        "checked_date_count": len(checked_dates),
        "requested_row_count": requested,
        "included_row_count": included,
        "excluded_row_count": excluded,
        "requested_count": requested,
        "included_count": included,
        "excluded_count": excluded,
        "coverage": round(included / requested, 6) if requested else 0.0,
        "reason_counts": dict(sorted(reason_counts.items())),
        "excluded_counts": dict(sorted(reason_counts.items())),
        "by_date": dict(sorted(by_date.items())),
        "coverage_info": coverage_info,
        **coverage_info,
        "survivorship_bias_risk": str(
            pit_status.get("survivorship_bias_risk") or "high"
        ),
        "pit_status": pit_status,
        "stages": stages,
        "warnings": list(dict.fromkeys(warnings)),
        "row_count_semantics": "membership_checks_across_signal_and_entry_stages",
    }


def run_combined_score_backtest(
    score_panel: pd.DataFrame,
    price_panel: pd.DataFrame,
    *,
    rebalance_frequency: str = "weekly",
    holding_days: int = 5,
    execution_lag_days: int = 1,
    portfolio_mode: str = "top_n",
    top_n: int = 20,
    quantile: float = 0.2,
    long_short: bool = False,
    transaction_cost_bps: float = 10.0,
    slippage_bps: float = 5.0,
    pit_universe_status: dict[str, Any] | None = None,
    profile: str = "general",
    initial_cash: float = 1_000_000.0,
    max_position_pct: float = 0.05,
    lot_size: int = 1,
    price_filter: dict[str, Any] | None = None,
    adjustment: str = "raw",
    historical_listings: pd.DataFrame | None = None,
) -> dict[str, Any]:
    pit_status = dict(pit_universe_status or {})
    pit_available = bool(pit_status.get("pit_universe"))
    # Point-in-time membership is opt-in and is enforced at both the signal
    # cross-section and the later execution date when listings are available.
    pit_listings = historical_listings if isinstance(historical_listings, pd.DataFrame) and not historical_listings.empty else None
    pit_per_date_enabled = bool(pit_available and pit_listings is not None)
    pit_listing_index = build_listing_index(pit_listings) if pit_per_date_enabled else {}
    pit_stage_audits: dict[str, dict[str, Any]] = {}
    pit_filter_audit = _merge_pit_stage_audits(
        pit_stage_audits,
        pit_status,
        enabled=pit_per_date_enabled,
    )
    warnings: list[str] = []
    warnings.extend(str(item) for item in pit_status.get("warnings", []) if item)
    requested_long_short = portfolio_mode == "long_short_quantile" or long_short
    if requested_long_short:
        warnings.append("long_short_accounting_contract_unresolved")
    # Option C: the combined-score backtest consumes a provided price panel that
    # defaults to raw local_daily. Actionable basis warnings (forward->flat-raw
    # fallback, divergent adjustment_factor.csv alias) are surfaced in the main
    # warnings list; the always-on raw basis note stays in the dedicated
    # adjustment_warnings field so it does not flip every run to "warning".
    for _basis_warning in price_basis_labels(adjustment)["adjustment_warnings"]:
        if _basis_warning in (FORWARD_FALLBACK_WARNING, DIVERGENT_ALIAS_WARNING):
            warnings.append(_basis_warning)
    price = _prepare_price_panel(price_panel)
    if score_panel.empty:
        warnings.append("missing_combined_score: score panel is empty")
    if price.empty:
        warnings.append("no_daily_data: price panel is empty or missing date/symbol/close")
    if score_panel.empty or price.empty:
        return _empty_payload(warnings, portfolio_mode, top_n, quantile, long_short, rebalance_frequency, holding_days, execution_lag_days, transaction_cost_bps, slippage_bps, pit_universe_status=pit_status, adjustment=adjustment, pit_filter_audit=pit_filter_audit)
    price_filter_cfg = dict(price_filter or {})
    price_filter_enabled = bool(price_filter_cfg.get("enabled", False))
    price_filter_max = _safe_float(price_filter_cfg.get("max_price"), 0.0)
    price_filter_counts: list[int] = []
    if price_filter_enabled and price_filter_max > 0:
        signal_prices = price[["date", "symbol", "close"]].rename(columns={"close": "signal_close"})
        score_panel = score_panel.copy()
        score_panel["date"] = pd.to_datetime(score_panel["date"], errors="coerce")
        score_panel["symbol"] = score_panel["symbol"].astype(str)
        score_panel = score_panel.merge(signal_prices, on=["date", "symbol"], how="left")
        missing_signal_price = int(score_panel["signal_close"].isna().sum())
        if missing_signal_price:
            warnings.append(f"price_filter_missing_signal_price_count:{missing_signal_price}")
        score_panel = score_panel[pd.to_numeric(score_panel["signal_close"], errors="coerce") <= price_filter_max].copy()
        price_filter_counts = [int(len(group)) for _date, group in score_panel.groupby("date")]
    # Enforce point-in-time membership on every signal row before cross-sectional
    # ranking. The shared lookup is built once for this entire engine run.
    if pit_per_date_enabled:
        score_panel = score_panel.copy()
        score_panel["date"] = pd.to_datetime(score_panel["date"], errors="coerce")
        score_panel["symbol"] = score_panel["symbol"].astype(str)
        score_panel, signal_audit = filter_rows_for_pit_membership(
            score_panel,
            pit_listings,
            symbol_column="symbol",
            date_column="date",
            listing_index=pit_listing_index,
            pit_status=pit_status,
        )
        pit_stage_audits["signal"] = signal_audit
        pit_filter_audit = _merge_pit_stage_audits(
            pit_stage_audits,
            pit_status,
            enabled=True,
        )
        warnings.extend(signal_audit.get("warnings") or [])
        if signal_audit.get("excluded_row_count"):
            warnings.append(
                f"pit_excluded_signal_date_rows_total:{signal_audit['excluded_row_count']}"
            )
    ranked = build_ranked_portfolios(
        score_panel,
        mode=portfolio_mode,
        top_n=top_n,
        quantile=quantile,
        long_short=long_short,
        # When point-in-time membership is enforced per signal date, a legitimately
        # small tradable universe (e.g. only one listed name on an early date) is a
        # valid single-name selection, not an "insufficient cross-section" skip.
        # Default stays 2 so non-PIT runs keep their existing behavior unchanged.
        min_cross_section=1 if pit_per_date_enabled else 2,
    )
    portfolios = ranked.get("portfolios") or []
    warnings.extend(ranked.get("warnings") or [])
    if not portfolios:
        warnings.append("insufficient_data: no ranked portfolios were built")
        return _empty_payload(warnings, portfolio_mode, top_n, quantile, long_short, rebalance_frequency, holding_days, execution_lag_days, transaction_cost_bps, slippage_bps, pit_universe_status=pit_status, adjustment=adjustment, pit_filter_audit=pit_filter_audit)
    all_dates = sorted(pd.Timestamp(item) for item in price["date"].dropna().unique())
    if rebalance_frequency == "monthly":
        filtered: list[dict[str, Any]] = []
        seen_months: set[str] = set()
        for item in portfolios:
            key = pd.Timestamp(item["signal_date"]).strftime("%Y-%m")
            if key not in seen_months:
                seen_months.add(key)
                filtered.append(item)
        portfolios = filtered
    elif rebalance_frequency == "weekly":
        portfolios = portfolios[:: max(1, int(holding_days))]
    price_map = {(str(row.symbol), pd.Timestamp(row.date)): _safe_float(row.close) for row in price.itertuples(index=False)}
    period_returns: list[float] = []
    gross_returns: list[float] = []
    turnovers: list[float] = []
    rebalances: list[dict[str, Any]] = []
    prev_symbols: set[str] = set()
    prev_capital_weights: dict[str, float] = {}
    total_cost = 0.0
    unfilled_due_to_lot_size_count = 0
    invested_ratios: list[float] = []
    cash_drags: list[float] = []
    entry_audits: list[dict[str, Any]] = []
    cost_rate = (_safe_float(transaction_cost_bps) + _safe_float(slippage_bps)) / 10000.0
    for item in portfolios:
        signal_date = pd.Timestamp(item["signal_date"])
        future_dates = [date for date in all_dates if date > signal_date]
        if len(future_dates) < max(1, int(execution_lag_days)):
            warnings.append(f"missing_entry_date_after_signal:{signal_date.date()}")
            continue
        entry = future_dates[max(0, int(execution_lag_days) - 1)]
        entry_index = all_dates.index(entry)
        exit_index = min(len(all_dates) - 1, entry_index + max(1, int(holding_days)))
        exit_date = all_dates[exit_index]
        long_symbols = [str(symbol) for symbol in item.get("long_symbols") or item.get("selected_symbols") or []]
        short_symbols = [str(symbol) for symbol in item.get("short_symbols") or []]
        long_short_mode = portfolio_mode == "long_short_quantile" or long_short
        if pit_per_date_enabled:
            entry_candidates = pd.DataFrame(
                [
                    {"date": entry, "symbol": symbol, "side": side}
                    for side, symbols in (("long", long_symbols), ("short", short_symbols))
                    for symbol in symbols
                ],
                columns=["date", "symbol", "side"],
            )
            eligible_entries, entry_audit = filter_rows_for_pit_membership(
                entry_candidates,
                pit_listings,
                symbol_column="symbol",
                date_column="date",
                listing_index=pit_listing_index,
                pit_status=pit_status,
            )
            entry_audits.append(entry_audit)
            eligible_longs = set(
                eligible_entries.loc[eligible_entries["side"] == "long", "symbol"].astype(str)
            )
            eligible_shorts = set(
                eligible_entries.loc[eligible_entries["side"] == "short", "symbol"].astype(str)
            )
            long_symbols = [symbol for symbol in long_symbols if symbol in eligible_longs]
            short_symbols = [symbol for symbol in short_symbols if symbol in eligible_shorts]
            if entry_audit.get("excluded_row_count"):
                reason_text = ",".join(
                    f"{reason}={count}"
                    for reason, count in (entry_audit.get("reason_counts") or {}).items()
                )
                warnings.append(
                    f"pit_excluded_on_entry_date:{entry.date()}:{entry_audit['excluded_row_count']}:{reason_text}"
                )
        executable_longs: list[str] = []
        invested_by_symbol: dict[str, float] = {}
        invested_total = 0.0
        remaining_capital = max(0.0, float(initial_cash))
        for symbol in long_symbols:
            entry_price = price_map.get((symbol, entry))
            if not entry_price:
                continue
            target_notional = float(initial_cash) * float(max_position_pct)
            if not long_short_mode:
                target_notional = min(target_notional, remaining_capital)
            shares = round_notional_down_to_lot_quantity(target_notional, entry_price, lot_size=max(1, int(lot_size)))
            if shares < max(1, int(lot_size)):
                unfilled_due_to_lot_size_count += 1
                continue
            executable_longs.append(symbol)
            invested_notional = shares * entry_price
            invested_by_symbol[symbol] = invested_notional
            invested_total += invested_notional
            if not long_short_mode:
                remaining_capital = max(0.0, remaining_capital - invested_notional)
        long_symbols = executable_longs
        long_returns_by_symbol = {
            symbol: ret
            for symbol in long_symbols
            if (ret := _price_return(price_map, symbol, entry, exit_date)) is not None
        }
        long_rets = list(long_returns_by_symbol.values())
        short_rets = [_price_return(price_map, symbol, entry, exit_date) for symbol in short_symbols]
        short_rets = [ret for ret in short_rets if ret is not None]
        if not long_rets:
            warnings.append(f"missing_price_for_selected_symbols:{signal_date.date()}")
            continue
        invested_ratio = invested_total / float(initial_cash) if initial_cash else 0.0
        invested_ratios.append(invested_ratio)
        cash_drags.append(max(0.0, 1.0 - invested_ratio))
        cash_return = 0.0
        invested_pnl = sum(
            invested_by_symbol[symbol] * ret
            for symbol, ret in long_returns_by_symbol.items()
        )
        asset_return = invested_pnl / invested_total if invested_total > 0 else 0.0
        gross = invested_pnl / float(initial_cash) if initial_cash else 0.0
        is_long_short = long_short_mode
        if is_long_short:
            # Long/short collateral, leverage and net-exposure accounting are a
            # separate unresolved contract. Preserve the historical proxy until
            # that contract is explicitly approved instead of applying the
            # long-only cash-dilution formula silently.
            gross = sum(long_rets) / len(long_rets)
            if short_rets:
                gross -= sum(short_rets) / len(short_rets)
            else:
                warnings.append(f"missing_price_for_short_symbols:{signal_date.date()}")
        current_symbols = set(long_symbols + short_symbols)
        if is_long_short:
            if not prev_symbols:
                turnover = 1.0
            else:
                overlap = len(prev_symbols & current_symbols)
                base = max(len(prev_symbols), len(current_symbols), 1)
                turnover = 1.0 - overlap / base
            prev_symbols = current_symbols
        else:
            current_capital_weights = {
                symbol: invested / float(initial_cash)
                for symbol, invested in invested_by_symbol.items()
                if initial_cash
            }
            turnover = sum(
                abs(current_capital_weights.get(symbol, 0.0) - prev_capital_weights.get(symbol, 0.0))
                for symbol in set(current_capital_weights) | set(prev_capital_weights)
            )
            prev_capital_weights = current_capital_weights
        cost = turnover * cost_rate
        net = gross - cost
        total_cost += cost
        gross_returns.append(_safe_float(gross))
        period_returns.append(_safe_float(net))
        turnovers.append(_safe_float(turnover))
        rebalances.append({
            "signal_date": signal_date.strftime("%Y-%m-%d"),
            "entry_date": entry.strftime("%Y-%m-%d"),
            "exit_date": exit_date.strftime("%Y-%m-%d"),
            "selected_symbols": long_symbols,
            "short_symbols": short_symbols,
            "gross_return": _safe_float(gross),
            "net_return": _safe_float(net),
            "asset_return": _safe_float(asset_return),
            "cash_return": _safe_float(cash_return),
            "accounting_status": (
                "degraded_long_short_proxy" if is_long_short else "actual_capital_long_only"
            ),
            "turnover": _safe_float(turnover),
            "cost": _safe_float(cost),
            "invested_ratio": _safe_float(invested_ratio),
            "cash_drag": _safe_float(max(0.0, 1.0 - invested_ratio)),
        })
    holding_period_days = max(1, int(holding_days))
    metrics = compute_performance_metrics(
        pd.Series(period_returns, dtype=float),
        pd.Series(turnovers, dtype=float),
        periods_per_year=252.0 / holding_period_days,
        annualization_period_days=holding_period_days,
    )
    metrics["gross_return"] = _safe_float((pd.Series(gross_returns, dtype=float) + 1.0).prod() - 1.0)
    metrics["net_return"] = _safe_float(metrics.get("cumulative_return"))
    metrics["cost_total"] = _safe_float(total_cost)
    metrics["unfilled_due_to_lot_size_count"] = int(unfilled_due_to_lot_size_count)
    metrics["average_cash_drag"] = _safe_float(pd.Series(cash_drags).mean()) if cash_drags else 0.0
    metrics["invested_ratio_avg"] = _safe_float(pd.Series(invested_ratios).mean()) if invested_ratios else 0.0
    metrics["final_cash"] = _safe_float(float(initial_cash) * (1.0 - (invested_ratios[-1] if invested_ratios else 0.0)))
    metrics["has_fractional_shares"] = False
    if pit_per_date_enabled:
        entry_stage_audit = _merge_pit_stage_audits(
            {f"entry_check_{index}": audit for index, audit in enumerate(entry_audits)},
            pit_status,
            enabled=True,
        )
        entry_stage_audit.pop("stages", None)
        entry_stage_audit["row_count_semantics"] = "actual_entry_date_membership_checks"
        pit_stage_audits["entry"] = entry_stage_audit
        pit_filter_audit = _merge_pit_stage_audits(
            pit_stage_audits,
            pit_status,
            enabled=True,
        )
    latest = rebalances[-1] if rebalances else {}
    status = "passed" if rebalances else "warning"
    if warnings:
        status = "warning"
    payload = {
        "ok": True,
        "status": status,
        "task_type": "combined_score_backtest",
        "generated_at": now_text(),
        "app_version": APP_VERSION,
        "package_version": PACKAGE_VERSION,
        "schema_version": API_SCHEMA_VERSION,
        "api_schema_version": API_SCHEMA_VERSION,
        "data_source": "local_daily",
        **price_basis_labels(adjustment),
        "target_date": str(latest.get("signal_date") or ""),
        "network": False,
        "survivorship_bias_risk": str(pit_status.get("survivorship_bias_risk") or "high"),
        "accounting_policy": {
            "long_only": "actual_capital_with_zero_cash_return",
            "cash_return": 0.0,
            "long_short": "degraded_unresolved" if requested_long_short else "not_applicable",
        },
        "config": {
            "profile": profile,
            "portfolio_mode": portfolio_mode,
            "top_n": int(top_n),
            "quantile": _safe_float(quantile),
            "long_short": bool(long_short or portfolio_mode == "long_short_quantile"),
            "rebalance_frequency": rebalance_frequency,
            "holding_days": int(holding_days),
            "execution_lag_days": int(execution_lag_days),
            "transaction_cost_bps": _safe_float(transaction_cost_bps),
            "slippage_bps": _safe_float(slippage_bps),
            "initial_cash": _safe_float(initial_cash),
            "max_position_pct": _safe_float(max_position_pct),
            "lot_size": int(lot_size),
            "price_filter_enabled": price_filter_enabled,
            "price_filter_max_price": price_filter_max,
            "price_filter_evaluated_on": "signal_date" if price_filter_enabled else "",
        },
        "universe": {
            "symbol_count": int(score_panel["symbol"].nunique()) if "symbol" in score_panel.columns else 0,
            "date_count": int(price["date"].nunique()),
            "signal_date_count": int(score_panel["date"].nunique()) if "date" in score_panel.columns else 0,
            "sample_count": int(len(score_panel)),
            "pit_universe": pit_available,
            "pit_status": {
                "available": bool(pit_status.get("available", False)),
                "pit_universe": pit_available,
                "symbol_count": int(pit_status.get("symbol_count") or 0),
                "list_date_coverage": pit_status.get("list_date_coverage") or 0.0,
                "delist_date_coverage": pit_status.get("delist_date_coverage") or 0.0,
                "st_coverage": pit_status.get("st_coverage") or 0.0,
                "suspend_coverage": pit_status.get("suspend_coverage") or 0.0,
                "survivorship_bias_risk": str(pit_status.get("survivorship_bias_risk") or "high"),
            },
            "price_filter": {
                "enabled": price_filter_enabled,
                "max_price": price_filter_max,
                "evaluated_on": "signal_date" if price_filter_enabled else "",
                "average_price_filtered_universe_count": _safe_float(pd.Series(price_filter_counts).mean()) if price_filter_counts else 0.0,
                "min_price_filtered_universe_count": int(min(price_filter_counts)) if price_filter_counts else 0,
                "max_price_filtered_universe_count": int(max(price_filter_counts)) if price_filter_counts else 0,
            },
        },
        "performance": metrics,
        "pit_filter_audit": pit_filter_audit,
        "portfolio_summary": {
            "latest_signal_date": str(latest.get("signal_date") or ""),
            "latest_entry_date": str(latest.get("entry_date") or ""),
            "latest_selected_count": len(latest.get("selected_symbols") or []),
            "latest_selected_symbols": list(latest.get("selected_symbols") or [])[:50],
        },
        "rebalances_preview": rebalances[-10:],
        "warnings": list(dict.fromkeys(str(item) for item in warnings if item)),
        "assumptions": [
            EXECUTION_ASSUMPTION,
            f"transaction_cost_bps={_safe_float(transaction_cost_bps)}; slippage_bps={_safe_float(slippage_bps)}",
            "Point-in-time universe metadata is available for research diagnostics." if pit_available else PIT_UNIVERSE_WARNING,
        ],
        "output_paths": {},
        "suggested_next_action": "Review the combined-score backtest report as research diagnostics only; do not treat selected symbols as trading advice.",
        "disclaimer": DISCLAIMER + (" " + SHORT_ASSUMPTION if (long_short or portfolio_mode == "long_short_quantile") else ""),
    }
    return _json_safe(payload)


def compute_performance_metrics(
    daily_returns: pd.Series,
    turnover: pd.Series | None = None,
    *,
    periods_per_year: float = 252.0,
    annualization_period_days: int = 1,
) -> dict[str, Any]:
    returns = pd.to_numeric(daily_returns, errors="coerce").replace([math.inf, -math.inf], pd.NA).dropna().astype(float)
    turns = pd.to_numeric(turnover if turnover is not None else pd.Series(dtype=float), errors="coerce").replace([math.inf, -math.inf], pd.NA).dropna().astype(float)
    annualization_periods = _safe_float(periods_per_year, 252.0)
    if annualization_periods <= 0:
        annualization_periods = 252.0
    period_days = max(1, int(annualization_period_days or 1))
    if returns.empty:
        return {
            "gross_return": 0.0,
            "net_return": 0.0,
            "cumulative_return": 0.0,
            "annualized_return": 0.0,
            "annualized_volatility": 0.0,
            "sharpe_ratio": 0.0,
            "max_drawdown": 0.0,
            "win_rate": 0.0,
            "average_daily_return": 0.0,
            "best_day": 0.0,
            "worst_day": 0.0,
            "turnover_mean": 0.0,
            "turnover_median": 0.0,
            "rebalance_count": 0,
            "cost_total": 0.0,
            "annualization_periods_per_year": annualization_periods,
            "annualization_period_days": period_days,
        }
    cumulative_curve = (1.0 + returns).cumprod()
    cumulative_return = _safe_float(cumulative_curve.iloc[-1] - 1.0)
    running_max = cumulative_curve.cummax()
    drawdown = cumulative_curve / running_max - 1.0
    mean = _safe_float(returns.mean())
    std = _safe_float(returns.std(ddof=0))
    periods = max(1, len(returns))
    annualized_return = _safe_float((1.0 + cumulative_return) ** (annualization_periods / periods) - 1.0)
    annualized_vol = _safe_float(std * math.sqrt(annualization_periods))
    sharpe = _safe_float((mean / std) * math.sqrt(annualization_periods)) if std else 0.0
    return {
        "gross_return": 0.0,
        "net_return": cumulative_return,
        "cumulative_return": cumulative_return,
        "annualized_return": annualized_return,
        "annualized_volatility": annualized_vol,
        "sharpe_ratio": sharpe,
        "max_drawdown": _safe_float(drawdown.min()),
        "win_rate": _safe_float((returns > 0).mean()),
        "average_daily_return": mean,
        "best_day": _safe_float(returns.max()),
        "worst_day": _safe_float(returns.min()),
        "turnover_mean": _safe_float(turns.mean()) if not turns.empty else 0.0,
        "turnover_median": _safe_float(turns.median()) if not turns.empty else 0.0,
        "rebalance_count": int(len(returns)),
        "cost_total": 0.0,
        "annualization_periods_per_year": annualization_periods,
        "annualization_period_days": period_days,
    }


def _empty_payload(
    warnings: list[str],
    portfolio_mode: str,
    top_n: int,
    quantile: float,
    long_short: bool,
    rebalance_frequency: str,
    holding_days: int,
    execution_lag_days: int,
    transaction_cost_bps: float,
    slippage_bps: float,
    pit_universe_status: dict[str, Any] | None = None,
    adjustment: str = "raw",
    pit_filter_audit: dict[str, Any] | None = None,
) -> dict[str, Any]:
    pit_status = dict(pit_universe_status or {})
    pit_available = bool(pit_status.get("pit_universe"))
    return {
        "ok": True,
        "status": "warning",
        "task_type": "combined_score_backtest",
        "generated_at": now_text(),
        "app_version": APP_VERSION,
        "package_version": PACKAGE_VERSION,
        "schema_version": API_SCHEMA_VERSION,
        "api_schema_version": API_SCHEMA_VERSION,
        "data_source": "none",
        # Option C: price-basis labels (data/daily is raw canonical).
        **price_basis_labels(adjustment),
        "target_date": "",
        "network": False,
        "survivorship_bias_risk": str(pit_status.get("survivorship_bias_risk") or "high"),
        "config": {
            "portfolio_mode": portfolio_mode,
            "top_n": int(top_n),
            "quantile": _safe_float(quantile),
            "long_short": bool(long_short),
            "rebalance_frequency": rebalance_frequency,
            "holding_days": int(holding_days),
            "execution_lag_days": int(execution_lag_days),
            "transaction_cost_bps": _safe_float(transaction_cost_bps),
            "slippage_bps": _safe_float(slippage_bps),
        },
        "universe": {
            "symbol_count": 0,
            "date_count": 0,
            "signal_date_count": 0,
            "sample_count": 0,
            "pit_universe": pit_available,
            "pit_status": {
                "available": bool(pit_status.get("available", False)),
                "pit_universe": pit_available,
                "symbol_count": int(pit_status.get("symbol_count") or 0),
                "list_date_coverage": pit_status.get("list_date_coverage") or 0.0,
                "delist_date_coverage": pit_status.get("delist_date_coverage") or 0.0,
                "st_coverage": pit_status.get("st_coverage") or 0.0,
                "suspend_coverage": pit_status.get("suspend_coverage") or 0.0,
                "survivorship_bias_risk": str(pit_status.get("survivorship_bias_risk") or "high"),
            },
        },
        "performance": compute_performance_metrics(pd.Series(dtype=float)),
        "pit_filter_audit": dict(
            pit_filter_audit
            or _merge_pit_stage_audits({}, pit_status, enabled=False)
        ),
        "portfolio_summary": {"latest_signal_date": "", "latest_entry_date": "", "latest_selected_count": 0, "latest_selected_symbols": []},
        "warnings": list(dict.fromkeys(str(item) for item in warnings if item)),
        "assumptions": [
            EXECUTION_ASSUMPTION,
            f"transaction_cost_bps={_safe_float(transaction_cost_bps)}; slippage_bps={_safe_float(slippage_bps)}",
            "Point-in-time universe metadata is available for research diagnostics." if pit_available else PIT_UNIVERSE_WARNING,
        ],
        "output_paths": {},
        "suggested_next_action": "Generate combined_score_latest.json and local daily prices, then rerun combined-score-backtest.",
        "disclaimer": DISCLAIMER,
    }


def build_combined_score_backtest_report(payload: dict[str, Any]) -> str:
    config = payload.get("config") if isinstance(payload.get("config"), dict) else {}
    universe = payload.get("universe") if isinstance(payload.get("universe"), dict) else {}
    perf = payload.get("performance") if isinstance(payload.get("performance"), dict) else {}
    summary = payload.get("portfolio_summary") if isinstance(payload.get("portfolio_summary"), dict) else {}
    warnings = payload.get("warnings") if isinstance(payload.get("warnings"), list) else []
    assumptions = payload.get("assumptions") if isinstance(payload.get("assumptions"), list) else []
    selected = summary.get("latest_selected_symbols") if isinstance(summary.get("latest_selected_symbols"), list) else []
    metrics = [
        ("Net Return", perf.get("net_return", 0.0)),
        ("Annualized Return", perf.get("annualized_return", 0.0)),
        ("Sharpe", perf.get("sharpe_ratio", 0.0)),
        ("Max Drawdown", perf.get("max_drawdown", 0.0)),
        ("Turnover Mean", perf.get("turnover_mean", 0.0)),
        ("Cost Total", perf.get("cost_total", 0.0)),
    ]
    lines = [
        "# QuantDesk Combined Score Backtest Report",
        "",
        f"- generated_at: `{payload.get('generated_at') or '-'}`",
        f"- app_version: `{payload.get('app_version') or '-'}`",
        f"- data_source: `{payload.get('data_source') or '-'}`",
        f"- portfolio_mode: `{config.get('portfolio_mode')}`",
        f"- top_n: `{config.get('top_n')}`",
        f"- quantile: `{config.get('quantile')}`",
        f"- long_short: `{config.get('long_short')}`",
        f"- rebalance_frequency: `{config.get('rebalance_frequency')}`",
        f"- holding_days: `{config.get('holding_days')}`",
        f"- execution_lag_days: `{config.get('execution_lag_days')}`",
        f"- transaction_cost_bps: `{config.get('transaction_cost_bps')}`",
        f"- slippage_bps: `{config.get('slippage_bps')}`",
        f"- symbols: `{universe.get('symbol_count', 0)}`",
        f"- dates: `{universe.get('date_count', 0)}`",
        f"- signal_dates: `{universe.get('signal_date_count', 0)}`",
        "",
        "## Performance",
        "",
        "| Metric | Value |",
        "|---|---:|",
    ]
    lines.extend(f"| {name} | {_safe_float(value):.6f} |" for name, value in metrics)
    lines.extend([
        "",
        "## Turnover",
        "",
        f"- turnover_mean: `{_safe_float(perf.get('turnover_mean')):.6f}`",
        f"- turnover_median: `{_safe_float(perf.get('turnover_median')):.6f}`",
        f"- rebalance_count: `{int(perf.get('rebalance_count') or 0)}`",
        "",
        "## Latest Selected Symbols",
        "",
        "| Rank | Symbol |",
        "|---:|---|",
    ])
    for index, symbol in enumerate(selected[:50], start=1):
        lines.append(f"| {index} | {symbol} |")
    if not selected:
        lines.append("| 0 | none |")
    lines.extend([
        "",
        "## Assumptions",
        "",
    ])
    lines.extend([f"- {item}" for item in assumptions] or ["- none"])
    if config.get("long_short"):
        lines.extend(["", "## Long-Short Limitation", "", SHORT_ASSUMPTION])
    lines.extend([
        "",
        "## PIT Universe Limitation",
        "",
        PIT_UNIVERSE_WARNING,
        "",
        "## Warnings",
        "",
    ])
    lines.extend([f"- {item}" for item in warnings] or ["- none"])
    lines.extend([
        "",
        "## Suggested Next Action",
        "",
        str(payload.get("suggested_next_action") or "-"),
        "",
        "## Disclaimer",
        "",
        str(payload.get("disclaimer") or DISCLAIMER),
    ])
    return "\n".join(lines) + "\n"


def write_combined_score_backtest_outputs(
    payload: dict[str, Any],
    *,
    output_dir: str | Path = "results",
    report_dir: str | Path = "reports",
) -> dict[str, Any]:
    output_path = Path(output_dir)
    report_path = Path(report_dir)
    output_path.mkdir(parents=True, exist_ok=True)
    report_path.mkdir(parents=True, exist_ok=True)
    stamp = now_text().replace("-", "").replace(":", "").replace("T", "_").replace(" ", "_")
    latest_json = output_path / "combined_score_backtest_latest.json"
    timestamp_json = output_path / f"combined_score_backtest_{stamp}.json"
    report = report_path / f"combined_score_backtest_report_{stamp}.md"
    latest_report = report_path / "combined_score_backtest_report_latest.md"
    payload = _json_safe(dict(payload))
    payload["output_paths"] = {
        "latest_json": str(latest_json).replace("\\", "/"),
        "timestamp_json": str(timestamp_json).replace("\\", "/"),
        "report": str(report).replace("\\", "/"),
        "latest_report": str(latest_report).replace("\\", "/"),
    }
    write_json(latest_json, payload)
    write_json(timestamp_json, payload)
    report_text = build_combined_score_backtest_report(payload)
    report.write_text(report_text, encoding="utf-8")
    latest_report.write_text(report_text, encoding="utf-8")
    try:
        from quantdesk.experiment_tracker import log_experiment_non_blocking

        performance = payload.get("performance") if isinstance(payload.get("performance"), dict) else {}
        tracking = log_experiment_non_blocking(
            "combined_score_backtest",
            payload.get("config") if isinstance(payload.get("config"), dict) else {},
            performance,
            output_paths=payload.get("output_paths") if isinstance(payload.get("output_paths"), dict) else {},
            tags=["auto_logged"],
        )
        if tracking.get("experiment_id"):
            payload["experiment_id"] = tracking.get("experiment_id")
        if tracking.get("warning") and not tracking.get("ok"):
            payload.setdefault("warnings", []).append(tracking["warning"])
    except Exception as exc:
        payload.setdefault("warnings", []).append(f"experiment_tracker_failed:{exc}")
    return payload
