from __future__ import annotations

import math
from typing import Any

import pandas as pd

from quantdesk.price_basis import price_basis_labels

DEFAULT_RESEARCH_DISCLAIMER = "Research diagnostics only."
DEFAULT_NOT_INVESTMENT_ADVICE = "Not investment advice."
DEFAULT_SURVIVORSHIP_BIAS_WARNING = (
    "Point-in-time universe reconstruction is not enabled; survivorship bias may exist."
)
DEFAULT_PIT_UNIVERSE_ASSUMPTION = (
    "Point-in-time universe metadata is available only when historical listings are provided and explicitly enabled."
)
DEFAULT_EXECUTION_LAG_ASSUMPTION = (
    "Signals are simulated with an explicit execution lag; simulated fills do not equal real broker or exchange fills."
)
DEFAULT_TRANSACTION_COST_ASSUMPTION = "Transaction costs are modeled from the configured basis-point assumptions."
DEFAULT_SLIPPAGE_ASSUMPTION = "Slippage is modeled from the configured basis-point assumptions."
DEFAULT_DATA_SCOPE_ASSUMPTION = "Backtests use the local histories supplied to the run."
DEFAULT_SHORT_SELLING_LIMITATION = (
    "Short-selling results are research diagnostics only; A-share short-sale constraints may make them non-executable."
)
DEFAULT_PAPER_TRADING_ONLY = "Paper/simulated execution only; no broker connection and no real orders."


def _score_dimension_adjudication() -> dict[str, Any]:
    """Round 22 裁决披露；延迟导入避免核心 assumptions 依赖研究包顶层。"""

    from quantdesk.research.factor_adjudication import (
        score_dimension_adjudication_disclosure,
    )

    return score_dimension_adjudication_disclosure()


# Legacy-compatible dict helpers

def default_backtest_assumptions() -> dict[str, object]:
    """Assumptions matching the REAL default :class:`BacktestConfig`.

    Round 21 alignment: this helper previously claimed zero costs and
    ``costs_implemented: False`` while the actual simulator charged commission,
    sell-side stamp duty, and slippage. Reports and execution now share one
    truth derived from the live config defaults.
    """

    from quantdesk.backtest import BacktestConfig  # lazy: avoid circular import

    return backtest_assumptions_from_config(BacktestConfig())


def backtest_assumptions_from_config(
    config: Any,
    *,
    price_fallback_used: bool = False,
    pct_change_approximated: bool = False,
    portfolio_rule: str = "single asset",
) -> dict[str, object]:
    benchmark = config.benchmark if getattr(config, "benchmark", None) else "not provided"
    return {
        "strategy": "ma_breakout: close > MA20 and activity_value > activity_avg_20 * 1.5",
        "signal_date": "T close / end-of-day signal",
        "signal_timing": config.signal_timing,
        "execution_date": "T+1",
        "execution_timing": "next trading row after signal",
        "execution_price": config.execution_price,
        "execution_price_fallback": (
            "research-only same-day close fallback used when next open was missing (allow_close_fallback_research=true)"
            if price_fallback_used
            else "not used; missing next open refuses the fill (no_fill_price_unavailable)"
        ),
        "allow_close_fallback_research": bool(getattr(config, "allow_close_fallback_research", False)),
        "use_official_limit_metadata": bool(getattr(config, "use_official_limit_metadata", False)),
        # Round 21 Part D: 评分权重为启发式设定，未经 IC/统计验证；
        # 禁止在报告措辞中称其为「最优/推荐」权重。
        "score_weights": {"trend": 0.28, "momentum": 0.22, "activity": 0.18, "position": 0.14, "risk": 0.18},
        "score_weights_provenance": "heuristic_unvalidated",
        "score_missing_factor_handling": "explicit_na_excluded_fixed_denominator",
        # Round 22 预注册裁决：四个方向性维度被判无效，仅 risk 有效；
        # 用户裁定权重不改、只披露（延迟导入避免核心模块依赖研究包顶层）。
        "score_dimension_adjudication": _score_dimension_adjudication(),
        "commission_rate": config.commission_rate,
        "min_commission": config.min_commission,
        "stamp_duty_rate": config.stamp_duty_rate,
        "transfer_fee_rate": getattr(config, "transfer_fee_rate", 0.0),
        "cost_profile": getattr(config, "cost_profile", "current_a_share"),
        "cost_model": "cn_a_share_unified",
        "stamp_duty_rate_warning": (
            "legacy pre-2023-08-28 stamp-duty profile; current A-share sell-side rate is lower."
            if math.isclose(float(config.stamp_duty_rate), 0.001, rel_tol=0.0, abs_tol=1e-12)
            else ""
        ),
        "slippage_bps": config.slippage_bps,
        "initial_cash": config.initial_cash,
        "lot_size": config.lot_size,
        "max_position_pct": config.max_position_pct,
        "rebalance_rule": "daily signal evaluation; trade only when target holding changes",
        "universe_rule": "provided histories only; not a historical point-in-time universe",
        "portfolio_rule": portfolio_rule,
        "benchmark": benchmark,
        "data_adjustment": "input history adjustment as provided",
        # Option C: local backtest histories come from raw canonical data/daily.
        **price_basis_labels("raw"),
        "costs_implemented": True,
        "board_candidate_execution": "打板候选纸面模拟可能记录到真实市场涨停板难以成交的情形；该结果仅用于学习观察，不代表真实可成交。",
        "limit_up_rule": f"buy blocked when execution-basis change >= {config.limit_up_pct}%",
        "limit_down_rule": f"sell blocked when execution-basis change <= {config.limit_down_pct}%",
        "suspension_rule": "trade blocked when volume is zero or high/low/close is missing",
        "limit_change_source": (
            "open / previous close for next_open execution"
            if str(config.execution_price or "").lower() == "next_open"
            else ("pct_chg field" if not pct_change_approximated else "open / previous close approximation used when pct_chg is missing")
        ),
        "liquidity_participation_basis": (
            "previous row amount for next_open execution; execution-day full-day amount is not known at open"
            if str(config.execution_price or "").lower() == "next_open"
            else "execution row amount"
        ),
        "not_implemented": "partial fills, order queue priority, intraday liquidity depth, exact exchange calendars",
        "warning": "当前回测已扣除基础手续费、卖出印花税和滑点，并记录涨跌停/停牌阻塞；仍不能等同真实成交。",
        "disclaimer": build_backtest_disclaimer(),
        "assumption_metadata": build_backtest_assumption_metadata(
            transaction_cost_bps=config.commission_rate * 10000,
            slippage_bps=config.slippage_bps,
            execution_price=config.execution_price,
            execution_lag_days=1,
            pit_universe=False,
            data_scope="provided_histories_only",
            allow_short=False,
        ),
        "risk_flags": build_backtest_risk_flags(pit_universe=False),
    }


def portfolio_backtest_assumptions_from_config(
    config: Any,
    *,
    price_fallback_used: bool = False,
    pct_change_approximated: bool = False,
) -> dict[str, object]:
    assumptions = backtest_assumptions_from_config(
        config,
        price_fallback_used=price_fallback_used,
        pct_change_approximated=pct_change_approximated,
        portfolio_rule="capital-constrained portfolio backtest",
    )
    assumptions.update(
        {
            "portfolio_mode": "capital constrained",
            "initial_cash": config.initial_cash,
            "max_positions": config.max_positions,
            "max_position_pct": config.max_position_pct,
            "rebalance": config.rebalance,
            "rebalance_rule": f"{config.rebalance} rebalance using prior close signals; execute on next open",
            "ranking_rule": config.ranking_rule,
            "ranking_fields": "raw_signal, close/MA20, MA20/MA60, 20-day return",
            "ranking_is_proxy": True,
            "max_turnover_per_rebalance": config.max_turnover_per_rebalance,
            "turnover_limit_implemented": config.max_turnover_per_rebalance is not None,
            "max_industry_pct": config.max_industry_pct,
            "industry_map_csv": str(config.industry_map_csv) if config.industry_map_csv else None,
            "industry_check_enabled": bool(config.industry_map_csv),
            "industry_weights_latest": {},
            "industry_warning": "",
            "not_a_capital_constrained_portfolio_backtest": False,
            "warning": "历史回测不代表未来收益，组合回测仍未模拟盘口排队、部分成交和真实撮合深度。当前组合回测使用历史可计算代理排序，不等同于日报实时综合评分。",
        }
    )
    return assumptions


def backtest_cost_warning(assumptions: dict[str, object]) -> str:
    if not assumptions.get("costs_implemented", False):
        return "当前回测为简化模型，未完全模拟真实交易成本和成交约束。"
    return str(
        assumptions.get(
            "warning",
            "当前回测已加入基础交易成本和成交约束；仍不能等同真实成交。",
        )
    )


def summary_rows_from_metrics(
    metrics: Any,
    assumptions: dict[str, object],
    extra_metrics: dict[str, object] | None = None,
) -> pd.DataFrame:
    extra_metrics = extra_metrics or {}
    rows = [
        ("backtest_mode", assumptions.get("backtest_mode")),
        ("initial_cash", assumptions.get("initial_cash")),
        ("commission_rate", assumptions.get("commission_rate")),
        ("min_commission", assumptions.get("min_commission")),
        ("stamp_duty_rate", assumptions.get("stamp_duty_rate")),
        ("slippage_bps", assumptions.get("slippage_bps")),
        ("lot_size", assumptions.get("lot_size")),
        ("final_equity", metrics.final_equity),
        ("total_return", metrics.total_return),
        ("annual_return", metrics.annual_return),
        ("max_drawdown", metrics.max_drawdown),
        ("sharpe", metrics.sharpe),
        ("daily_positive_rate", metrics.daily_positive_rate),
        ("number_of_trades", metrics.number_of_trades),
        ("turnover", metrics.turnover),
        ("benchmark_return", metrics.benchmark_return),
        ("excess_return", metrics.excess_return),
        ("max_positions", assumptions.get("max_positions")),
        ("max_position_pct", assumptions.get("max_position_pct")),
        ("max_turnover_per_rebalance", assumptions.get("max_turnover_per_rebalance")),
        ("turnover_limit_implemented", assumptions.get("turnover_limit_implemented")),
        ("max_industry_pct", assumptions.get("max_industry_pct")),
        ("industry_check_enabled", assumptions.get("industry_check_enabled")),
        ("industry_warning", assumptions.get("industry_warning")),
        ("board_candidate_execution", assumptions.get("board_candidate_execution")),
        ("rebalance", assumptions.get("rebalance")),
        ("excluded_by_data_error", assumptions.get("excluded_by_data_error_count")),
        ("included_with_data_warning", assumptions.get("included_with_data_warning_count")),
        ("included_with_fallback", assumptions.get("included_with_fallback_count")),
    ]
    for key in ["exposure", "average_positions", "max_positions_used"]:
        rows.append((key, extra_metrics.get(key)))
    return pd.DataFrame(rows, columns=["metric", "value"])


# New research assumption helpers

def _clean_text(value: Any) -> str:
    text = str(value or "").strip()
    if not text or text.lower() in {"nan", "none", "inf", "-inf"}:
        return ""
    return text.replace("\ufffd", "")


def _safe_float(value: Any, default: float | None = None) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if math.isfinite(number) else default


def _append_unique(items: list[str], value: Any) -> None:
    text = _clean_text(value)
    if text and text not in items:
        items.append(text)


def build_backtest_disclaimer(
    *,
    research_only: bool = True,
    not_investment_advice: bool = True,
    paper_trading_only: bool = True,
) -> str:
    parts: list[str] = []
    if research_only:
        parts.append(DEFAULT_RESEARCH_DISCLAIMER)
    if not_investment_advice:
        parts.append(DEFAULT_NOT_INVESTMENT_ADVICE)
    if paper_trading_only:
        parts.append(DEFAULT_PAPER_TRADING_ONLY)
    if not parts:
        parts.append("Historical simulation output is informational only.")
    return " ".join(parts)


def build_backtest_assumptions(
    *,
    strategy: str | None = None,
    mode: str | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
    initial_cash: float | None = None,
    transaction_cost_bps: float | None = None,
    slippage_bps: float | None = None,
    execution_price: str | None = None,
    execution_lag_days: int | None = None,
    pit_universe: bool = False,
    data_scope: str | None = None,
    allow_short: bool = False,
    extra_assumptions: list[str] | None = None,
) -> list[str]:
    assumptions: list[str] = [build_backtest_disclaimer()]
    if strategy:
        _append_unique(assumptions, f"Strategy: {strategy}.")
    if mode:
        _append_unique(assumptions, f"Backtest mode: {mode}.")
    if start_date or end_date:
        _append_unique(assumptions, f"Backtest period: {start_date or 'unknown'} to {end_date or 'unknown'}.")
    cash = _safe_float(initial_cash)
    if cash is not None:
        _append_unique(assumptions, f"Initial cash: {cash:g}.")
    cost = _safe_float(transaction_cost_bps)
    if cost is not None:
        _append_unique(assumptions, f"Transaction cost assumption: {cost:g} bps.")
    else:
        _append_unique(assumptions, DEFAULT_TRANSACTION_COST_ASSUMPTION)
    slip = _safe_float(slippage_bps)
    if slip is not None:
        _append_unique(assumptions, f"Slippage assumption: {slip:g} bps.")
    else:
        _append_unique(assumptions, DEFAULT_SLIPPAGE_ASSUMPTION)
    if execution_price:
        _append_unique(assumptions, f"Execution price proxy: {execution_price}.")
    if execution_lag_days is not None:
        _append_unique(assumptions, f"Execution lag: {int(execution_lag_days)} trading day(s).")
    _append_unique(assumptions, DEFAULT_EXECUTION_LAG_ASSUMPTION)
    if pit_universe:
        _append_unique(assumptions, DEFAULT_PIT_UNIVERSE_ASSUMPTION)
    else:
        _append_unique(assumptions, DEFAULT_SURVIVORSHIP_BIAS_WARNING)
    _append_unique(assumptions, f"Data scope: {data_scope}." if data_scope else DEFAULT_DATA_SCOPE_ASSUMPTION)
    if allow_short:
        _append_unique(assumptions, DEFAULT_SHORT_SELLING_LIMITATION)
    for item in extra_assumptions or []:
        _append_unique(assumptions, item)
    return assumptions


def build_backtest_assumption_metadata(
    *,
    transaction_cost_bps: float | None = None,
    slippage_bps: float | None = None,
    execution_price: str | None = None,
    execution_lag_days: int | None = None,
    pit_universe: bool = False,
    data_scope: str | None = None,
    allow_short: bool = False,
) -> dict[str, Any]:
    return {
        "transaction_cost_bps": _safe_float(transaction_cost_bps, 0.0),
        "slippage_bps": _safe_float(slippage_bps, 0.0),
        "execution_price": _clean_text(execution_price) or "unspecified",
        "execution_lag_days": int(execution_lag_days or 0),
        "pit_universe": bool(pit_universe),
        "data_scope": _clean_text(data_scope) or "provided_histories_only",
        "allow_short": bool(allow_short),
        "survivorship_bias_warning": not bool(pit_universe),
        "research_only": True,
    }


def build_backtest_risk_flags(
    *,
    pit_universe: bool = False,
    insufficient_data: bool = False,
    missing_benchmark: bool = False,
    high_turnover: bool = False,
    synthetic_data: bool = False,
    allow_short: bool = False,
) -> list[str]:
    flags: list[str] = []
    if not pit_universe:
        flags.append("survivorship_bias_not_corrected")
    if insufficient_data:
        flags.append("insufficient_data")
    if missing_benchmark:
        flags.append("missing_benchmark")
    if high_turnover:
        flags.append("high_turnover")
    if synthetic_data:
        flags.append("synthetic_test_data")
    if allow_short:
        flags.append("short_selling_research_only")
    return list(dict.fromkeys(flags))


def render_backtest_assumptions_markdown(
    assumptions: list[str],
    *,
    title: str = "Backtest Assumptions",
) -> str:
    cleaned = [_clean_text(item) for item in assumptions]
    cleaned = [item for item in cleaned if item]
    if not cleaned:
        cleaned = [build_backtest_disclaimer(), DEFAULT_DATA_SCOPE_ASSUMPTION]
    lines = [f"## {title}", ""]
    lines.extend(f"- {item}" for item in cleaned)
    return "\n".join(lines)
