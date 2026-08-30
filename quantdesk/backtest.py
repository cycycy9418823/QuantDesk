from __future__ import annotations

import math
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import pandas as pd

from quantdesk.backtest_assumptions import (
    backtest_cost_warning,  # noqa: F401 - compatibility re-export
    backtest_assumptions_from_config,
    build_backtest_assumption_metadata,  # noqa: F401 – re-export
    build_backtest_assumptions,  # noqa: F401 – re-export
    build_backtest_disclaimer,  # noqa: F401 - compatibility re-export
    build_backtest_risk_flags,  # noqa: F401 - compatibility re-export
    default_backtest_assumptions,
    portfolio_backtest_assumptions_from_config,  # noqa: F401 – re-export
    render_backtest_assumptions_markdown,  # noqa: F401 - compatibility re-export
    summary_rows_from_metrics,
)
from quantdesk.backtest_loaders import benchmark_total_return, benchmark_window_from_equity_curve, load_benchmark_series, load_industry_map  # noqa: F401 – compatibility re-export
from quantdesk.backtest_metrics import compute_equity_metrics
from quantdesk.board_rules import classify_board
from quantdesk.board_rules import is_limit_down as board_is_limit_down
from quantdesk.board_rules import is_limit_up as board_is_limit_up
from quantdesk.paper.market_rules import round_notional_down_to_lot_quantity
from quantdesk.column_aliases import get_value as _col_get
from quantdesk.indicators import add_indicators
from quantdesk.paper.trade_costs import (
    DEFAULT_COMMISSION_RATE,
    DEFAULT_MIN_COMMISSION,
    DEFAULT_STAMP_TAX_RATE,
    DEFAULT_TRANSFER_FEE_RATE,
    TRADE_COST_MODEL,
    TradeCostConfig,
    compute_trade_costs,
)
from quantdesk.shared import normalize_symbol
from quantdesk.slippage_model import (
    SlippageModelConfig,
)
from quantdesk.strategy_conditions import evaluate_conditions
from quantdesk.strategy_registry import resolve_strategy

# Constants

PROJECT_ROOT = Path(__file__).resolve().parents[1]
REPORT_DIR = PROJECT_ROOT / "reports"

TRADE_COLUMNS = [
    "date",
    "symbol",
    "side",
    "signal_date",
    "execution_date",
    "requested_price",
    "executed_price",
    "shares",
    "gross_amount",
    "commission",
    "stamp_duty",
    "transfer_fee",
    "slippage_cost",
    "total_cost",
    "reason",
    "blocked",
    "blocked_reason",
]

EQUITY_CURVE_COLUMNS = [
    "date",
    "cash",
    "shares",
    "close",
    "market_value",
    "equity",
    "position_intent",
]

POSITIONS_COLUMNS = [
    "date",
    "symbol",
    "shares",
    "close",
    "market_value",
    "weight",
    "cost_basis",
    "unrealized_pnl",
]

REBALANCE_LOG_COLUMNS = [
    "date",
    "action",
    "symbol",
    "reason",
    "target_weight",
    "current_weight",
    "cash_before",
    "cash_after",
    "equity_before",
    "equity_after",
]

RISK_SUMMARY_COLUMNS = ["metric", "value"]

# Export helpers

def save_csv_safely(df: pd.DataFrame, path: Path) -> Path:
    try:
        df.to_csv(path, index=False, encoding="utf-8-sig")
        return path
    except PermissionError:
        fallback = path.with_name(f"{path.stem}_locked{path.suffix}")
        df.to_csv(fallback, index=False, encoding="utf-8-sig")
        return fallback

# Dataclasses

@dataclass
class BacktestMetrics:
    """Computed backtest metrics for research review, not a promise of returns.

    Metrics are based on the simplified simulator in this module. They include
    modeled costs where implemented, but do not represent real exchange
    matching, queue priority, partial fills, or future performance.
    """

    annual_return: float
    total_return: float
    max_drawdown: float
    sharpe: float
    win_rate: float
    days: int
    assumptions: dict[str, object] = field(default_factory=default_backtest_assumptions)
    number_of_trades: int = 0
    turnover: float = 0.0
    final_equity: float = np.nan
    benchmark_return: float = np.nan
    excess_return: float = np.nan
    sortino: float = np.nan

    @property
    def daily_positive_rate(self) -> float:
        """Alias clarifying that ``win_rate`` is daily-positive ratio here."""
        return self.win_rate

@dataclass
class BacktestConfig:
    """Configuration for the minimal single-asset backtest.

    Signals are produced after the T-day close and executed on the next row by
    default. The simulator models basic costs and blocking rules, but it is not
    a real broker or exchange matching engine; historical results do not
    guarantee future returns.
    """

    initial_cash: float = 100_000
    commission_rate: float = DEFAULT_COMMISSION_RATE
    min_commission: float = DEFAULT_MIN_COMMISSION
    stamp_duty_rate: float = DEFAULT_STAMP_TAX_RATE
    transfer_fee_rate: float = DEFAULT_TRANSFER_FEE_RATE
    cost_profile: str = "current_a_share"
    slippage_bps: float = 5
    lot_size: int = 100
    max_position_pct: float = 1.0
    execution_price: str = "next_open"
    signal_timing: str = "close"
    limit_up_pct: float = 9.8
    limit_down_pct: float = -9.8
    benchmark: str | None = None
    max_participation_rate: float = 0.05
    use_board_rules: bool = True
    use_liquidity_slippage: bool = False
    enforce_t_plus_one: bool = True
    # Round 21: when next_open is missing, the default is to refuse the fill
    # (no_fill_price_unavailable). Falling back to the same-day close is a
    # research-only approximation and must be enabled explicitly.
    allow_close_fallback_research: bool = False
    # Round 21: consult the official limit-price / suspension metadata tables
    # (data/metadata/limit_price_history.csv, suspension_history.csv) instead
    # of the pct-threshold heuristic. Opt-in at library level; the backtest CLI
    # enables it by default when the metadata files exist.
    use_official_limit_metadata: bool = False

    def __post_init__(self) -> None:
        legacy_profiles = {"legacy", "legacy_pre_20230828", "pre_20230828", "legacy_stamp_0_1pct"}
        if str(self.cost_profile).strip().lower() in legacy_profiles and math.isclose(
            float(self.stamp_duty_rate),
            DEFAULT_STAMP_TAX_RATE,
            rel_tol=0.0,
            abs_tol=1e-12,
        ):
            self.stamp_duty_rate = 0.001

@dataclass
class PortfolioBacktestConfig(BacktestConfig):
    """Configuration for the capital-constrained portfolio backtest.

    The portfolio uses one shared cash pool with position-count and
    position-size limits. It still uses simplified next-open execution and does
    not model order book depth, partial fills, or guaranteed tradability.
    """

    max_positions: int = 5
    max_position_pct: float = 0.2
    rebalance: str = "weekly"
    ranking_rule: str = "score_proxy"
    max_turnover_per_rebalance: float | None = None
    max_industry_pct: float | None = None
    industry_map_csv: str | Path | None = None

@dataclass
class BacktestResult:
    """Auditable container returned by single-asset and portfolio simulations.

    The result keeps equity, trades, assumptions, optional positions, and
    rebalance logs together so a report can explain what was simulated. It is a
    research artifact only; historical backtests do not guarantee future
    returns.
    """

    equity_curve: pd.DataFrame
    trades: list[dict[str, object]]
    metrics: BacktestMetrics
    assumptions: dict[str, object]
    positions: pd.DataFrame = field(default_factory=pd.DataFrame)
    rebalance_log: pd.DataFrame = field(default_factory=pd.DataFrame)
    summary: pd.DataFrame = field(default_factory=pd.DataFrame)
    extra_metrics: dict[str, object] = field(default_factory=dict)

# Signal helpers

def _ensure_trade_aliases(df: pd.DataFrame) -> pd.DataFrame:
    aliases = {
        "date": "日期",
        "open": "开盘",
        "high": "最高",
        "low": "最低",
        "close": "收盘",
        "volume": "成交量",
        "amount": "成交额",
        "turnover": "换手率",
    }
    for english, chinese in aliases.items():
        if english in df.columns and chinese not in df.columns:
            df[chinese] = df[english]
    return df

def _normalize_strategy_name(strategy: str) -> str:
    aliases = {
        "ma_trend": "ma_breakout",
        "均线突破": "ma_breakout",
        "activity_breakout": "volume_breakout",
        "量价突破观察": "volume_breakout",
    }
    key = str(strategy or "ma_breakout").strip()
    return aliases.get(key, key or "ma_breakout")

def _resolve_strategy_definition(strategy: str) -> tuple[dict[str, object], str]:
    normalized = _normalize_strategy_name(strategy)
    try:
        resolved = resolve_strategy(normalized)
        return resolved, str(resolved.get("strategy_warning") or "")
    except Exception:
        fallback = resolve_strategy("ma_breakout")
        return fallback, f"未知策略 {strategy}，已回退到 ma_breakout。"

def _score_component_value(value: float | None, low: float, high: float) -> float:
    if value is None or not math.isfinite(value):
        return 50.0
    if high <= low:
        return 50.0
    normalized = (value - low) / (high - low)
    return float(max(0.0, min(100.0, normalized * 100.0)))

def _score_components_for_row(row: pd.Series) -> dict[str, object]:
    close = safe_number(row.get("close"))
    ma5 = safe_number(row.get("ma5"))
    ma10 = safe_number(row.get("ma10"))
    ma20 = safe_number(row.get("ma20"))
    ma60 = safe_number(row.get("ma60"))
    macd_hist = safe_number(row.get("macd_histogram"))
    prev_macd_hist = safe_number(row.get("previous_macd_histogram"))
    rsi14 = safe_number(row.get("rsi14"))
    activity_ratio = safe_number(row.get("activity_ratio_20"))
    amount_ratio = safe_number(row.get("amount_ratio_20"))
    volume_ratio = safe_number(row.get("volume_ratio_20"))
    boll_mid = safe_number(row.get("boll_mid"))
    boll_upper = safe_number(row.get("boll_upper"))
    boll_lower = safe_number(row.get("boll_lower"))
    vol20 = safe_number(row.get("vol20"))
    atr14 = safe_number(row.get("atr14"))
    missing = int(safe_number(row.get("entry_missing_count"), 0) or 0) + int(safe_number(row.get("exit_missing_count"), 0) or 0)
    data_sufficient = any(
        math.isfinite(value)
        for value in (
            close,
            ma5,
            ma10,
            ma20,
            ma60,
            macd_hist,
            prev_macd_hist,
            rsi14,
            activity_ratio,
            amount_ratio,
            volume_ratio,
            boll_mid,
            boll_upper,
            boll_lower,
            vol20,
            atr14,
        )
    )

    # Round 21 Part D: 每个维度记录「是否有任何可用指标」。整维度无可用指标时
    # 返回显式 NA（None），不再回填 50 占位分冒充中性评分。
    trend_available = (
        (math.isfinite(close) and math.isfinite(ma20) and ma20 != 0)
        or (math.isfinite(ma20) and math.isfinite(ma60) and ma60 != 0)
        or (math.isfinite(ma5) and math.isfinite(ma10) and ma10 != 0)
    )
    trend_score: float | None = None
    if trend_available:
        trend_score = 50.0
        if math.isfinite(close) and math.isfinite(ma20) and ma20 != 0:
            trend_score += max(min((close / ma20 - 1) * 120, 25), -25)
        if math.isfinite(ma20) and math.isfinite(ma60) and ma60 != 0:
            trend_score += max(min((ma20 / ma60 - 1) * 100, 20), -20)
        if math.isfinite(ma5) and math.isfinite(ma10) and ma10 != 0:
            trend_score += max(min((ma5 / ma10 - 1) * 90, 15), -15)
        trend_score = float(max(0.0, min(100.0, trend_score)))

    momentum_available = math.isfinite(macd_hist) or math.isfinite(rsi14)
    momentum_score: float | None = None
    if momentum_available:
        momentum_score = 50.0
        if math.isfinite(macd_hist):
            momentum_score += max(min(macd_hist * 18, 18), -18)
        if math.isfinite(macd_hist) and math.isfinite(prev_macd_hist):
            momentum_score += max(min((macd_hist - prev_macd_hist) * 10, 12), -12)
        if math.isfinite(rsi14):
            momentum_score += max(min((rsi14 - 50) * 0.6, 12), -12)
        momentum_score = float(max(0.0, min(100.0, momentum_score)))

    activity_available = (
        math.isfinite(activity_ratio) or math.isfinite(amount_ratio) or math.isfinite(volume_ratio)
    )
    activity_score: float | None = None
    if activity_available:
        activity_score = 50.0
        if math.isfinite(activity_ratio):
            activity_score += max(min((activity_ratio - 1) * 28, 18), -18)
        if math.isfinite(amount_ratio):
            activity_score += max(min((amount_ratio - 1) * 18, 12), -12)
        if math.isfinite(volume_ratio):
            activity_score += max(min((volume_ratio - 1) * 18, 12), -12)
        activity_score = float(max(0.0, min(100.0, activity_score)))

    position_available = (
        (math.isfinite(close) and math.isfinite(boll_upper) and math.isfinite(boll_lower) and boll_upper != boll_lower)
        or (math.isfinite(close) and math.isfinite(ma20) and ma20 != 0)
    )
    position_score: float | None = None
    if position_available:
        position_score = 50.0
        if math.isfinite(close) and math.isfinite(boll_upper) and math.isfinite(boll_lower) and boll_upper != boll_lower:
            band_pct = (close - boll_lower) / (boll_upper - boll_lower)
            position_score = 100.0 * max(0.0, min(1.0, band_pct))
        elif math.isfinite(close) and math.isfinite(ma20) and ma20 != 0:
            position_score = 50.0 + max(min((close / ma20 - 1) * 120, 20), -20)
        position_score = float(max(0.0, min(100.0, position_score)))

    risk_available = math.isfinite(vol20) or (math.isfinite(atr14) and math.isfinite(close) and close > 0)
    risk_penalty: float | None = None
    if risk_available:
        risk_penalty = 0.0
        if math.isfinite(vol20):
            risk_penalty += max(min((vol20 - 0.05) * 1000, 35), 0)
        if math.isfinite(atr14) and math.isfinite(close) and close > 0:
            risk_penalty += max(min((atr14 / close - 0.02) * 800, 30), 0)
        risk_penalty += min(missing * 8.0, 32.0)
        if not math.isfinite(close):
            risk_penalty += 15.0
        risk_penalty = float(max(0.0, min(100.0, risk_penalty)))

    return {
        "trend_score": trend_score,
        "momentum_score": momentum_score,
        "activity_score": activity_score,
        "position_score": position_score,
        "risk_penalty": risk_penalty,
        "data_sufficient": bool(data_sufficient),
        "data_warning": "" if data_sufficient else "关键指标缺失，评分维度为显式 NA，不参与加权",
    }

def backtest_signal_frame(hist: pd.DataFrame, strategy: str = "ma_breakout", strategy_definition: dict[str, object] | None = None) -> pd.DataFrame:
    """Build a no-lookahead signal frame from the shared strategy registry.

    When ``strategy_definition`` is provided it is used verbatim instead of
    resolving ``strategy`` from the registry (used by the parameter-sensitivity
    sweep to backtest perturbed copies of a strategy).
    """
    if strategy_definition is not None:
        strategy_def = dict(strategy_definition)
        warning = str(strategy_def.get("strategy_warning") or "")
    else:
        strategy_def, warning = _resolve_strategy_definition(strategy)
    df = hist.copy()
    if "date" not in df.columns:
        for source in ("日期", "date", "trade_date", "datetime", "time"):
            if source in df.columns:
                df = df.rename(columns={source: "date"})
                break
    df = add_indicators(df)
    if "data_reliability_score" not in df.columns:
        df["data_reliability_score"] = 100.0
    if "date" not in df.columns:
        df["date"] = pd.RangeIndex(start=0, stop=len(df))
    df["date"] = pd.to_datetime(df["date"], errors="coerce")
    df = df.dropna(subset=["date"]).sort_values("date").reset_index(drop=True)
    if df.empty:
        df.attrs["strategy_warning"] = warning
        return df

    entry_rows: list[dict[str, object]] = []
    exit_rows: list[dict[str, object]] = []
    for _, row in df.iterrows():
        row_data = row.to_dict()
        entry_result = evaluate_conditions(row_data, list(strategy_def.get("entry_conditions", [])))
        # 退出用 OR：任一退出条件满足即触发，避免"所有退出条件同时成立"才离场。
        exit_result = evaluate_conditions(
            row_data, list(strategy_def.get("exit_conditions", [])), mode="any"
        )
        entry_rows.append(entry_result)
        exit_rows.append(exit_result)

    df["strategy_id"] = strategy_def.get("strategy_id", _normalize_strategy_name(strategy))
    df["strategy_name"] = strategy_def.get("name", _normalize_strategy_name(strategy))
    df["strategy_warning"] = warning
    df["entry_signal"] = [bool(item.get("all_matched")) for item in entry_rows]
    df["exit_signal"] = [bool(item.get("triggered")) for item in exit_rows]
    df["raw_signal"] = df["entry_signal"]
    df["entry_matched_count"] = [len(item.get("matched") or []) for item in entry_rows]
    df["entry_failed_count"] = [len(item.get("failed") or []) for item in entry_rows]
    df["entry_missing_count"] = [len(item.get("missing") or []) for item in entry_rows]
    df["exit_matched_count"] = [len(item.get("matched") or []) for item in exit_rows]
    df["exit_failed_count"] = [len(item.get("failed") or []) for item in exit_rows]
    df["exit_missing_count"] = [len(item.get("missing") or []) for item in exit_rows]
    shifted = df["raw_signal"].shift(1)
    df["position"] = shifted.where(shifted.notna(), False).astype(bool).astype(float)
    df["signal_for_execution"] = df["raw_signal"].shift(1).where(df["raw_signal"].shift(1).notna(), False).astype(bool)
    df["entry_signal_for_execution"] = df["entry_signal"].shift(1).where(df["entry_signal"].shift(1).notna(), False).astype(bool)
    df["exit_signal_for_execution"] = df["exit_signal"].shift(1).where(df["exit_signal"].shift(1).notna(), False).astype(bool)
    df.attrs["strategy_warning"] = warning
    return df

# Cost helpers

def safe_number(value: object, default: float = np.nan) -> float:
    try:
        number = float(value)
        if math.isfinite(number):
            return number
    except (TypeError, ValueError):
        pass
    return default


def trade_costs_for(side: str, gross_amount: float, config: BacktestConfig) -> dict[str, float | str]:
    """Return unified A-share cost components for a simulated backtest fill.

    Backtest price slippage is still represented by the executed-price offset,
    so this wrapper calls the shared cost service with ``slippage_bps=0`` to
    avoid double counting slippage.
    """

    if gross_amount <= 0:
        return {
            "commission": 0.0,
            "stamp_duty": 0.0,
            "stamp_tax": 0.0,
            "transfer_fee": 0.0,
            "slippage": 0.0,
            "total_fee": 0.0,
            "fee_model": TRADE_COST_MODEL,
        }
    return compute_trade_costs(
        side=side,
        gross=float(gross_amount),
        config=TradeCostConfig(
            commission_rate=float(config.commission_rate),
            min_commission=float(config.min_commission),
            stamp_tax_rate=float(config.stamp_duty_rate),
            transfer_fee_rate=float(getattr(config, "transfer_fee_rate", 0.0)),
            slippage_bps=0.0,
            apply_min_commission=float(config.commission_rate) > 0,
        ),
    )


def commission_for(gross_amount: float, config: BacktestConfig) -> float:
    return float(trade_costs_for("buy", gross_amount, config)["commission"])


def sell_taxes_for(gross_amount: float, config: BacktestConfig) -> float:
    costs = trade_costs_for("sell", gross_amount, config)
    return float(costs["stamp_duty"]) + float(costs["transfer_fee"])

def blocked_reason_counts(trades: list[dict[str, object]] | pd.DataFrame) -> dict[str, int]:
    frame = pd.DataFrame(trades)
    if frame.empty or "blocked" not in frame.columns:
        return {
            "limit_up": 0,
            "limit_down": 0,
            "suspended": 0,
            "no_fill_price_unavailable": 0,
            "cash_insufficient": 0,
            "turnover_limit": 0,
        }
    blocked = frame[frame["blocked"].fillna(False).astype(bool)].copy()
    reason = blocked.get("blocked_reason", pd.Series("", index=blocked.index)).astype(str).str.lower()
    return {
        "limit_up": int(reason.str.contains("limit up", regex=False).sum()),
        "limit_down": int(reason.str.contains("limit down", regex=False).sum()),
        "suspended": int(reason.str.contains("suspended", regex=False).sum()),
        "no_fill_price_unavailable": int(reason.str.contains("no_fill_price_unavailable", regex=False).sum()),
        "cash_insufficient": int((reason.str.contains("insufficient cash", regex=False) | reason.str.contains("cash insufficient", regex=False)).sum()),
        "turnover_limit": int(reason.str.contains("turnover limit", regex=False).sum()),
        "t_plus_one": int(reason.str.contains("t_plus_one", regex=False).sum()),
    }

def risk_summary_rows_from_result(result: BacktestResult) -> pd.DataFrame:
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
        ("blocked_no_fill_price_unavailable", counts.get("no_fill_price_unavailable", 0)),
        ("blocked_cash_insufficient", counts["cash_insufficient"]),
        ("blocked_turnover_limit", counts["turnover_limit"]),
        ("max_industry_weight", max_industry_weight),
        ("industry_concentration_warning", assumptions.get("industry_warning", "") or ""),
        ("max_single_rebalance_turnover", max_single_rebalance_turnover),
        ("turnover_limit_triggered", counts["turnover_limit"] > 0),
        ("warning", assumptions.get("warning", "")),
    ]
    return pd.DataFrame(rows, columns=RISK_SUMMARY_COLUMNS)

# Trade execution helpers

def daily_change_pct(row: pd.Series, prev_close: float) -> tuple[float, bool]:
    pct = safe_number(_col_get(row, "pct_chg", np.nan))
    if math.isfinite(pct):
        return pct, False
    open_price = safe_number(_col_get(row, "open", np.nan))
    if math.isfinite(open_price) and math.isfinite(prev_close) and prev_close > 0:
        return float((open_price / prev_close - 1) * 100), True
    return 0.0, True


def execution_limit_change_pct(row: pd.Series, prev_close: float, config: BacktestConfig) -> tuple[float, bool]:
    """Return the price-change basis that matches the configured fill price.

    For next-open execution, limit-up/down blocking must be based on the open
    gap versus previous close.  Using full-day ``pct_chg`` would let the close
    leak into an opening fill decision.
    """

    if str(config.execution_price or "").lower() == "next_open":
        open_price = safe_number(_col_get(row, "open", np.nan))
        if math.isfinite(open_price) and math.isfinite(prev_close) and prev_close > 0:
            return float((open_price / prev_close - 1) * 100), True
    return daily_change_pct(row, prev_close)


def row_amount(row: pd.Series | None) -> float:
    if row is None:
        return np.nan
    amount_candidates = [
        safe_number(_col_get(row, "amount", np.nan)),
        safe_number(row.get("amount") if hasattr(row, "get") else np.nan),
        safe_number(row.get("成交额") if hasattr(row, "get") else np.nan),
    ]
    finite_amounts = [value for value in amount_candidates if math.isfinite(value) and value > 0]
    return min(finite_amounts) if finite_amounts else np.nan


def liquidity_amount_for_execution(
    current_row: pd.Series,
    previous_row: pd.Series | None,
    config: BacktestConfig,
) -> tuple[float, str]:
    """Return amount visible at the time of the simulated fill.

    A next-open order cannot know the execution day's full-day turnover, so the
    participation cap uses the previous available row's amount.  Other
    execution modes keep the current-row amount.
    """

    if str(config.execution_price or "").lower() == "next_open":
        previous_amount = row_amount(previous_row)
        if math.isfinite(previous_amount) and previous_amount > 0:
            return previous_amount, "previous_row_amount_for_next_open"
        return np.nan, "previous_row_amount_missing_for_next_open"
    current_amount = row_amount(current_row)
    return current_amount, "execution_row_amount"


def row_is_st(row: pd.Series | None) -> bool:
    """Infer ST status from row-level metadata when available.

    Tushare/PIT metadata may join a boolean flag into the daily row.  Some
    older local frames only carry a name field, so this helper accepts both.
    It is deliberately conservative: missing metadata means "not known ST",
    not an error.
    """

    if row is None:
        return False
    truthy_values = {"1", "true", "yes", "y", "st", "*st", "s"}
    falsey_values = {"0", "false", "no", "n", "", "none", "nan"}
    for key in ("is_st", "st", "is_st_current", "st_current", "special_treatment"):
        if not hasattr(row, "get"):
            continue
        value = row.get(key)
        if isinstance(value, bool):
            return value
        if pd.isna(value):
            continue
        text = str(value).strip().lower()
        if text in truthy_values:
            return True
        if text in falsey_values:
            continue
    for key in ("name", "security_name", "stock_name", "名称", "股票名称"):
        if not hasattr(row, "get"):
            continue
        value = row.get(key)
        if pd.isna(value):
            continue
        text = str(value).upper()
        if "ST" in text:
            return True
    return False


def effective_board_rules(symbol: str, config: BacktestConfig, row: pd.Series | None = None) -> object | None:
    return classify_board(symbol, is_st=row_is_st(row)) if config.use_board_rules else None


def effective_limit_pcts(
    symbol: str,
    config: BacktestConfig,
    row: pd.Series | None = None,
) -> tuple[float, float, object | None]:
    board_rules = effective_board_rules(symbol, config, row)
    if board_rules:
        return board_rules.limit_up_pct, board_rules.limit_down_pct, board_rules
    return config.limit_up_pct, config.limit_down_pct, None


def effective_lot_size(symbol: str, config: BacktestConfig, row: pd.Series | None = None) -> int:
    board_rules = effective_board_rules(symbol, config, row)
    if board_rules:
        return max(1, int(board_rules.lot_size or config.lot_size or 100))
    return max(1, int(config.lot_size or 100))


def limit_up_blocked(
    *,
    change_pct: float,
    symbol: str,
    config: BacktestConfig,
    row: pd.Series | None = None,
) -> bool:
    if not math.isfinite(change_pct):
        return False
    if config.use_board_rules:
        return board_is_limit_up(change_pct=change_pct / 100.0, symbol=symbol, is_st=row_is_st(row))
    return change_pct >= float(config.limit_up_pct)


def limit_down_blocked(
    *,
    change_pct: float,
    symbol: str,
    config: BacktestConfig,
    row: pd.Series | None = None,
) -> bool:
    if not math.isfinite(change_pct):
        return False
    if config.use_board_rules:
        return board_is_limit_down(change_pct=change_pct / 100.0, symbol=symbol, is_st=row_is_st(row))
    return change_pct <= float(config.limit_down_pct)


def one_word_limit_change_pct(row: pd.Series, prev_close: float) -> float:
    """Return close-vs-prev change when the row is an all-day one-price board.

    This catches legacy data that lacks an ``open`` or ``pct_chg`` column but
    still shows high == low == close at the limit price.  Without this guard a
    next-open simulation may incorrectly fill a sealed one-word board.
    """

    if not math.isfinite(prev_close) or prev_close <= 0:
        return np.nan
    high = safe_number(_col_get(row, "high", np.nan))
    low = safe_number(_col_get(row, "low", np.nan))
    close = safe_number(_col_get(row, "close", np.nan))
    if not all(math.isfinite(value) and value > 0 for value in (high, low, close)):
        return np.nan
    open_price = safe_number(_col_get(row, "open", np.nan))
    values = [high, low, close]
    if math.isfinite(open_price) and open_price > 0:
        values.append(open_price)
    if max(values) - min(values) > max(abs(close) * 1e-8, 1e-8):
        return np.nan
    return float((close / prev_close - 1) * 100)


def _execution_basis_price(row: pd.Series, config: BacktestConfig) -> float:
    if str(config.execution_price or "").lower() == "next_open":
        return safe_number(_col_get(row, "open", np.nan))
    return safe_number(_col_get(row, "close", np.nan))


def official_limit_up_blocked(row: pd.Series, config: BacktestConfig) -> bool | None:
    """Decide limit-up blocking from the attached official limit price.

    Returns None when the row carries no usable official limit so callers can
    fall back to the pct-threshold heuristic (annotated per trade via the
    ``limit_price_source`` column).
    """

    up_limit = safe_number(_col_get(row, "official_up_limit", np.nan))
    if not math.isfinite(up_limit) or up_limit <= 0:
        return None
    price = _execution_basis_price(row, config)
    if not math.isfinite(price) or price <= 0:
        return None
    return bool(price >= up_limit - 1e-9)


def official_limit_down_blocked(row: pd.Series, config: BacktestConfig) -> bool | None:
    down_limit = safe_number(_col_get(row, "official_down_limit", np.nan))
    if not math.isfinite(down_limit) or down_limit <= 0:
        return None
    price = _execution_basis_price(row, config)
    if not math.isfinite(price) or price <= 0:
        return None
    return bool(price <= down_limit + 1e-9)


def limit_up_blocked_for_execution(
    row: pd.Series,
    change_pct: float,
    prev_close: float,
    symbol: str,
    config: BacktestConfig,
) -> bool:
    official = official_limit_up_blocked(row, config)
    if official is not None:
        return official
    if limit_up_blocked(change_pct=change_pct, symbol=symbol, config=config, row=row):
        return True
    one_word_change = one_word_limit_change_pct(row, prev_close)
    return limit_up_blocked(change_pct=one_word_change, symbol=symbol, config=config, row=row)


def limit_down_blocked_for_execution(
    row: pd.Series,
    change_pct: float,
    prev_close: float,
    symbol: str,
    config: BacktestConfig,
) -> bool:
    official = official_limit_down_blocked(row, config)
    if official is not None:
        return official
    if limit_down_blocked(change_pct=change_pct, symbol=symbol, config=config, row=row):
        return True
    one_word_change = one_word_limit_change_pct(row, prev_close)
    return limit_down_blocked(change_pct=one_word_change, symbol=symbol, config=config, row=row)


def t_plus_one_block_reason(current_date: object, entry_date: object) -> tuple[str, str]:
    """Return conservative T+1 block reason and a visible warning.

    If either date cannot be parsed, the simulator must not silently assume the
    position is sellable. The conservative research-only outcome is a blocked
    paper-trade event plus an assumptions warning.
    """

    try:
        current_ts = pd.Timestamp(current_date).normalize()
        entry_ts = pd.Timestamp(entry_date).normalize()
    except Exception as exc:
        return "t_plus_one_date_unparseable", f"t_plus_one_date_unparseable:{exc}"
    if current_ts <= entry_ts:
        return "t_plus_one", ""
    return "", ""


def is_trade_suspended(row: pd.Series) -> bool:
    # Round 21: official suspension_history.csv flag (attached as a column)
    # takes effect in addition to the bar heuristic below.
    official = _col_get(row, "official_suspended", None)
    if official is not None and pd.notna(official) and bool(official):
        return True
    for canonical in ("high", "low", "close"):
        value = safe_number(_col_get(row, canonical, np.nan))
        if not math.isfinite(value) or value <= 0:
            return True
    volume = _col_get(row, "volume", np.nan)
    if pd.notna(volume):
        volume_value = safe_number(volume)
        if math.isfinite(volume_value) and volume_value <= 0:
            return True
    return False

def requested_execution_price(row: pd.Series, config: BacktestConfig) -> tuple[float, bool]:
    if config.execution_price == "next_open":
        open_price = safe_number(_col_get(row, "open", np.nan))
        if math.isfinite(open_price) and open_price > 0:
            return open_price, False
        # Round 21: missing next_open refuses the fill by default. The
        # same-day-close proxy is research-only and requires the explicit
        # allow_close_fallback_research switch; results are labeled via
        # price_fallback_used / execution_price_fallback.
        if getattr(config, "allow_close_fallback_research", False):
            close_price = safe_number(_col_get(row, "close", np.nan))
            if math.isfinite(close_price) and close_price > 0:
                return close_price, True
        return np.nan, False
    close_price = safe_number(_col_get(row, "close", np.nan))
    return close_price, False

def make_trade_record(
    *,
    date: object,
    symbol: str,
    side: str,
    signal_date: object,
    execution_date: object,
    requested_price: float,
    executed_price: float,
    shares: int,
    gross_amount: float,
    commission: float,
    stamp_duty: float,
    slippage_cost: float,
    total_cost: float,
    transfer_fee: float = 0.0,
    reason: str,
    blocked: bool,
    blocked_reason: str = "",
    extra: dict[str, object] | None = None,
) -> dict[str, object]:
    def fmt_date(value: object) -> str:
        parsed = pd.to_datetime(value, errors="coerce")
        return parsed.strftime("%Y-%m-%d") if pd.notna(parsed) else ""

    record = {
        "date": fmt_date(date),
        "symbol": symbol,
        "side": side,
        "signal_date": fmt_date(signal_date),
        "execution_date": fmt_date(execution_date),
        "requested_price": requested_price,
        "executed_price": executed_price,
        "shares": shares,
        "gross_amount": gross_amount,
        "commission": commission,
        "stamp_duty": stamp_duty,
        "transfer_fee": transfer_fee,
        "slippage_cost": slippage_cost,
        "total_cost": total_cost,
        "reason": reason,
        "blocked": blocked,
        "blocked_reason": blocked_reason,
    }
    return record | dict(extra or {})

def metrics_from_equity(
    equity_curve: pd.DataFrame,
    trades: list[dict[str, object]],
    assumptions: dict[str, object],
    initial_cash: float,
    benchmark_series: pd.Series | None = None,
) -> BacktestMetrics:
    bench_start, bench_end = benchmark_window_from_equity_curve(equity_curve)
    if equity_curve.empty:
        benchmark_return = benchmark_total_return(benchmark_series, start_date=bench_start, end_date=bench_end)
        return BacktestMetrics(
            annual_return=np.nan,
            total_return=np.nan,
            max_drawdown=np.nan,
            sharpe=np.nan,
            win_rate=np.nan,
            days=0,
            assumptions=assumptions,
            number_of_trades=0,
            turnover=0.0,
            final_equity=initial_cash,
            benchmark_return=benchmark_return,
            excess_return=np.nan,
        )

    equity = pd.to_numeric(equity_curve["equity"], errors="coerce").dropna()
    if equity.empty:
        benchmark_return = benchmark_total_return(benchmark_series, start_date=bench_start, end_date=bench_end)
        return BacktestMetrics(
            annual_return=np.nan,
            total_return=np.nan,
            max_drawdown=np.nan,
            sharpe=np.nan,
            win_rate=np.nan,
            days=0,
            assumptions=assumptions,
            number_of_trades=0,
            turnover=0.0,
            final_equity=initial_cash,
            benchmark_return=benchmark_return,
            excess_return=np.nan,
        )
    total_return = float(equity.iloc[-1] / initial_cash - 1)
    daily_returns = equity.ffill().pct_change(fill_method=None).fillna(0)
    days = int(len(equity))
    equity_metrics = compute_equity_metrics(list(equity))
    annual_return = float(equity_metrics["annual_return"])
    max_drawdown = -float(equity_metrics["max_drawdown"])
    sharpe = float(equity_metrics["sharpe"])
    sortino = float(equity_metrics["sortino"])
    win_rate = float((daily_returns > 0).mean())
    executed_trades = [trade for trade in trades if not trade.get("blocked")]
    turnover = float(sum(safe_number(trade.get("gross_amount"), 0.0) for trade in executed_trades) / initial_cash) if initial_cash else 0.0
    benchmark_return = benchmark_total_return(benchmark_series, start_date=bench_start, end_date=bench_end)
    excess_return = float(total_return - benchmark_return) if math.isfinite(benchmark_return) else np.nan
    return BacktestMetrics(
        annual_return=annual_return,
        total_return=total_return,
        max_drawdown=max_drawdown,
        sharpe=sharpe,
        win_rate=win_rate,
        days=days,
        assumptions=assumptions,
        number_of_trades=len(executed_trades),
        turnover=turnover,
        final_equity=float(equity.iloc[-1]),
        benchmark_return=benchmark_return,
        excess_return=excess_return,
        sortino=sortino,
    )

# Single asset backtest

def simulate_single_asset_backtest(
    hist: pd.DataFrame,
    symbol: str,
    config: BacktestConfig | None = None,
    benchmark_series: pd.Series | None = None,
    strategy: str = "ma_breakout",
    strategy_definition: dict[str, object] | None = None,
    signal_frame: pd.DataFrame | None = None,
) -> BacktestResult:
    """Simulate one symbol with shared strategy rules and local-only assumptions.

    ``strategy_definition`` overrides registry resolution when provided (used by
    the parameter-sensitivity sweep to backtest perturbed strategy copies).
    ``signal_frame`` is an optional, already validated signal view keyed by
    date.  It is used by the formal alpha evaluator to keep qfq research
    signals separate from raw execution prices; the default path still builds
    signals from ``hist`` exactly as before.
    """
    config = config or BacktestConfig()
    if hist.empty:
        assumptions = backtest_assumptions_from_config(config)
        metrics = metrics_from_equity(pd.DataFrame(), [], assumptions, config.initial_cash, benchmark_series)
        return BacktestResult(pd.DataFrame(), [], metrics, assumptions)

    if strategy_definition is not None:
        strategy_def = dict(strategy_definition)
        warning = str(strategy_def.get("strategy_warning") or "")
    else:
        strategy_def, warning = _resolve_strategy_definition(strategy)
    risk_line_type = str(strategy_def.get("risk_line_type") or "ma20")
    risk_multiplier = safe_number(strategy_def.get("risk_line_multiplier"), 2.0) or 2.0

    df = hist.copy()
    if "date" not in df.columns:
        for source in ("日期", "date", "trade_date", "datetime", "time"):
            if source in df.columns:
                df = df.rename(columns={source: "date"})
                break
    df["date"] = pd.to_datetime(df.get("date"), errors="coerce")
    df = add_indicators(df)
    df = _ensure_trade_aliases(df)
    for column in ["open", "high", "low", "close", "volume", "amount", "turnover"]:
        if column in df.columns:
            df[column] = pd.to_numeric(df[column], errors="coerce")
    df = df.dropna(subset=["date"]).sort_values("date").reset_index(drop=True)
    official_metadata_warnings: list[str] = []
    if getattr(config, "use_official_limit_metadata", False) and "official_up_limit" not in df.columns:
        from quantdesk.research.official_limit_metadata import (
            attach_official_limit_metadata,
            load_official_limit_metadata,
        )

        date_bounds = df["date"].dropna()
        official = load_official_limit_metadata(
            [symbol],
            start_date=date_bounds.min().strftime("%Y-%m-%d") if not date_bounds.empty else None,
            end_date=date_bounds.max().strftime("%Y-%m-%d") if not date_bounds.empty else None,
        )
        official_metadata_warnings = list(official.warnings)
        df = attach_official_limit_metadata(df, symbol, official)
    if signal_frame is None:
        signals = backtest_signal_frame(df, strategy=strategy, strategy_definition=strategy_definition)
    else:
        signals = signal_frame.copy()
        required_signal_columns = {
            "date",
            "strategy_id",
            "strategy_name",
            "strategy_warning",
            "entry_signal",
            "exit_signal",
            "raw_signal",
            "position",
            "entry_signal_for_execution",
            "exit_signal_for_execution",
            "entry_matched_count",
            "entry_failed_count",
            "entry_missing_count",
            "exit_matched_count",
            "exit_failed_count",
            "exit_missing_count",
        }
        missing_signal_columns = sorted(required_signal_columns - set(signals.columns))
        if missing_signal_columns:
            raise ValueError(
                "signal_frame_missing_columns:" + ",".join(missing_signal_columns)
            )
        signals["date"] = pd.to_datetime(signals["date"], errors="coerce")
        signals = signals.dropna(subset=["date"]).sort_values("date")
        signals = signals.drop_duplicates("date", keep="last").reset_index(drop=True)
    signals["date"] = pd.to_datetime(signals["date"], errors="coerce")
    df = df.merge(
        signals[[
            "date",
            "strategy_id",
            "strategy_name",
            "strategy_warning",
            "entry_signal",
            "exit_signal",
            "raw_signal",
            "position",
            "entry_signal_for_execution",
            "exit_signal_for_execution",
            "entry_matched_count",
            "entry_failed_count",
            "entry_missing_count",
            "exit_matched_count",
            "exit_failed_count",
            "exit_missing_count",
        ]],
        on="date",
        how="left",
    )
    for column in [
        "entry_signal",
        "exit_signal",
        "raw_signal",
        "position",
        "entry_signal_for_execution",
        "exit_signal_for_execution",
    ]:
        if column in df.columns:
            df[column] = df[column].fillna(False).astype(bool)
    cash = float(config.initial_cash)
    shares = 0
    trades: list[dict[str, object]] = []
    equity_rows: list[dict[str, object]] = []
    price_fallback_used = False
    pct_change_approximated = False
    last_close = np.nan
    highest_since_entry = np.nan

    # --- v3.7.0.1 增强项：板块规则 + T+1 跟踪 ---
    effective_limit_up, effective_limit_down, board_rules = effective_limit_pcts(symbol, config)
    slippage_cfg = SlippageModelConfig() if config.use_liquidity_slippage else None
    entry_date: pd.Timestamp | None = None  # T+1 跟踪
    t_plus_one_blocked_count = 0
    t_plus_one_warnings: list[str] = []

    for idx, row in df.iterrows():
        date = row["date"]
        signal_date = df.iloc[idx - 1]["date"] if idx > 0 else date
        prev_close = safe_number(df.iloc[idx - 1].get("close")) if idx > 0 else np.nan
        requested_price, used_price_fallback = requested_execution_price(row, config)
        price_fallback_used = price_fallback_used or used_price_fallback
        change_pct, used_change_approx = execution_limit_change_pct(row, prev_close, config)
        pct_change_approximated = pct_change_approximated or used_change_approx
        close_price = safe_number(row.get("close"))
        if math.isfinite(close_price):
            last_close = close_price

        entry_exec = bool(row.get("entry_signal_for_execution", False))
        exit_exec = bool(row.get("exit_signal_for_execution", False))
        risk_triggered = False
        risk_line_price = np.nan
        if shares > 0 and idx > 0:
            risk_row = df.iloc[idx - 1]
            risk_close = safe_number(risk_row.get("close"))
            if math.isfinite(risk_close):
                highest_since_entry = risk_close if not math.isfinite(highest_since_entry) else max(highest_since_entry, risk_close)
            if risk_line_type in {"atr", "atr14", "atr_trailing"}:
                atr14 = safe_number(risk_row.get("atr14"))
                if math.isfinite(highest_since_entry) and math.isfinite(atr14) and atr14 > 0:
                    risk_line_price = highest_since_entry - atr14 * risk_multiplier
                    risk_triggered = math.isfinite(risk_close) and risk_close < risk_line_price
            elif risk_line_type in {"manual", "manual_risk_line"}:
                risk_line_price = safe_number(risk_row.get("risk_line") or risk_row.get("manual_risk_line"))
                risk_triggered = math.isfinite(risk_close) and math.isfinite(risk_line_price) and risk_close < risk_line_price
            elif risk_line_type in {"recent_low", "low_20d"}:
                risk_line_price = safe_number(risk_row.get("low_20d") or risk_row.get("recent_low"))
                risk_triggered = math.isfinite(risk_close) and math.isfinite(risk_line_price) and risk_close < risk_line_price
            else:
                risk_line_price = safe_number(risk_row.get("ma20"))
                risk_triggered = math.isfinite(risk_close) and math.isfinite(risk_line_price) and risk_close < risk_line_price

        if idx > 0 and shares == 0 and entry_exec:
            reason = "虚拟建仓：前一日观察条件满足。"
            blocked_reason = ""
            if is_trade_suspended(row):
                blocked_reason = "suspended"
            elif limit_up_blocked_for_execution(row, change_pct, prev_close, symbol, config):
                blocked_reason = "limit up"
            elif not math.isfinite(requested_price):
                blocked_reason = "no_fill_price_unavailable"
            if blocked_reason:
                trade = make_trade_record(
                    date=date,
                    symbol=symbol,
                    side="BUY",
                    signal_date=signal_date,
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
                trade.update(
                    {
                        "strategy_id": row.get("strategy_id", strategy_def.get("strategy_id", strategy)),
                        "strategy_name": row.get("strategy_name", strategy_def.get("name", strategy)),
                        "entry_matched_count": int(row.get("entry_matched_count") or 0),
                        "entry_failed_count": int(row.get("entry_failed_count") or 0),
                        "entry_missing_count": int(row.get("entry_missing_count") or 0),
                        "exit_matched_count": int(row.get("exit_matched_count") or 0),
                        "exit_failed_count": int(row.get("exit_failed_count") or 0),
                        "exit_missing_count": int(row.get("exit_missing_count") or 0),
                        "limit_price_source": str(row.get("limit_price_source") or ""),
                        "risk_line_type": risk_line_type,
                        "risk_line_price": risk_line_price if math.isfinite(risk_line_price) else None,
                        "risk_line_triggered": False,
                    }
                )
                trades.append(trade)
            else:
                executed_price = requested_price * (1 + config.slippage_bps / 10_000)
                max_cash = cash * config.max_position_pct
                lot_size = effective_lot_size(symbol, config, row)
                buy_shares = round_notional_down_to_lot_quantity(max_cash, executed_price, lot_size=lot_size)
                previous_liquidity_row = df.iloc[idx - 1] if idx > 0 else None
                daily_amount, liquidity_amount_source = liquidity_amount_for_execution(row, previous_liquidity_row, config)
                if math.isfinite(daily_amount) and daily_amount > 0 and config.max_participation_rate > 0:
                    max_notional_by_liquidity = daily_amount * config.max_participation_rate
                    max_shares_by_liquidity = round_notional_down_to_lot_quantity(max_notional_by_liquidity, executed_price, lot_size=lot_size)
                    buy_shares = min(buy_shares, max_shares_by_liquidity)
                while buy_shares >= lot_size:
                    gross = buy_shares * executed_price
                    buy_costs = trade_costs_for("buy", gross, config)
                    commission = float(buy_costs["commission"])
                    transfer_fee = float(buy_costs["transfer_fee"])
                    if gross + commission + transfer_fee <= cash:
                        break
                    buy_shares -= lot_size
                if buy_shares < lot_size:
                    trade = make_trade_record(
                        date=date,
                        symbol=symbol,
                        side="BUY",
                        signal_date=signal_date,
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
                    )
                    trade.update(
                        {
                            "strategy_id": row.get("strategy_id", strategy_def.get("strategy_id", strategy)),
                            "strategy_name": row.get("strategy_name", strategy_def.get("name", strategy)),
                            "entry_matched_count": int(row.get("entry_matched_count") or 0),
                            "entry_failed_count": int(row.get("entry_failed_count") or 0),
                            "entry_missing_count": int(row.get("entry_missing_count") or 0),
                            "exit_matched_count": int(row.get("exit_matched_count") or 0),
                            "exit_failed_count": int(row.get("exit_failed_count") or 0),
                            "exit_missing_count": int(row.get("exit_missing_count") or 0),
                            "limit_price_source": str(row.get("limit_price_source") or ""),
                        "risk_line_type": risk_line_type,
                            "risk_line_price": risk_line_price if math.isfinite(risk_line_price) else None,
                            "risk_line_triggered": False,
                            "liquidity_amount_source": liquidity_amount_source,
                            "lot_size": lot_size,
                        }
                    )
                    trades.append(trade)
                else:
                    gross = buy_shares * executed_price
                    buy_costs = trade_costs_for("buy", gross, config)
                    commission = float(buy_costs["commission"])
                    transfer_fee = float(buy_costs["transfer_fee"])
                    slippage_cost = abs(executed_price - requested_price) * buy_shares
                    cash -= gross + commission + transfer_fee
                    shares = buy_shares
                    entry_date = pd.Timestamp(date)  # v3.7.0.1 T+1 跟踪
                    highest_since_entry = close_price if math.isfinite(close_price) else requested_price
                    trade = make_trade_record(
                        date=date,
                        symbol=symbol,
                        side="BUY",
                        signal_date=signal_date,
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
                    )
                    trade.update(
                        {
                            "strategy_id": row.get("strategy_id", strategy_def.get("strategy_id", strategy)),
                            "strategy_name": row.get("strategy_name", strategy_def.get("name", strategy)),
                            "entry_matched_count": int(row.get("entry_matched_count") or 0),
                            "entry_failed_count": int(row.get("entry_failed_count") or 0),
                            "entry_missing_count": int(row.get("entry_missing_count") or 0),
                            "exit_matched_count": int(row.get("exit_matched_count") or 0),
                            "exit_failed_count": int(row.get("exit_failed_count") or 0),
                            "exit_missing_count": int(row.get("exit_missing_count") or 0),
                            "limit_price_source": str(row.get("limit_price_source") or ""),
                        "risk_line_type": risk_line_type,
                            "risk_line_price": risk_line_price if math.isfinite(risk_line_price) else None,
                            "risk_line_triggered": False,
                            "liquidity_amount_source": liquidity_amount_source,
                            "lot_size": lot_size,
                        }
                    )
                    trades.append(trade)

        elif idx > 0 and shares > 0 and (exit_exec or risk_triggered):
            reason = "风险线触发：观察假设需要复核。" if risk_triggered else "虚拟离场：前一日观察条件失效。"
            blocked_reason = ""
            # --- v3.7.0.1 T+1 检查 ---
            if config.enforce_t_plus_one and entry_date is not None:
                blocked_reason, t_plus_one_warning = t_plus_one_block_reason(date, entry_date)
                if blocked_reason:
                    t_plus_one_blocked_count += 1
                if t_plus_one_warning:
                    t_plus_one_warnings.append(t_plus_one_warning)
            if not blocked_reason:
                if is_trade_suspended(row):
                    blocked_reason = "suspended"
                elif limit_down_blocked_for_execution(row, change_pct, prev_close, symbol, config):
                    blocked_reason = "limit down"
                elif not math.isfinite(requested_price):
                    blocked_reason = "no_fill_price_unavailable"
            if blocked_reason:
                trade = make_trade_record(
                    date=date,
                    symbol=symbol,
                    side="SELL",
                    signal_date=signal_date,
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
                trade.update(
                    {
                        "strategy_id": row.get("strategy_id", strategy_def.get("strategy_id", strategy)),
                        "strategy_name": row.get("strategy_name", strategy_def.get("name", strategy)),
                        "entry_matched_count": int(row.get("entry_matched_count") or 0),
                        "entry_failed_count": int(row.get("entry_failed_count") or 0),
                        "entry_missing_count": int(row.get("entry_missing_count") or 0),
                        "exit_matched_count": int(row.get("exit_matched_count") or 0),
                        "exit_failed_count": int(row.get("exit_failed_count") or 0),
                        "exit_missing_count": int(row.get("exit_missing_count") or 0),
                        "limit_price_source": str(row.get("limit_price_source") or ""),
                        "risk_line_type": risk_line_type,
                        "risk_line_price": risk_line_price if math.isfinite(risk_line_price) else None,
                        "risk_line_triggered": bool(risk_triggered),
                    }
                )
                trades.append(trade)
            else:
                executed_price = requested_price * (1 - config.slippage_bps / 10_000)
                sell_shares = shares
                gross = sell_shares * executed_price
                sell_costs = trade_costs_for("sell", gross, config)
                commission = float(sell_costs["commission"])
                stamp_duty = float(sell_costs["stamp_duty"])
                transfer_fee = float(sell_costs["transfer_fee"])
                slippage_cost = abs(requested_price - executed_price) * sell_shares
                cash += gross - commission - stamp_duty - transfer_fee
                shares = 0
                trade = make_trade_record(
                    date=date,
                    symbol=symbol,
                    side="SELL",
                    signal_date=signal_date,
                    execution_date=date,
                    requested_price=requested_price,
                    executed_price=executed_price,
                    shares=sell_shares,
                    gross_amount=gross,
                    commission=commission,
                    stamp_duty=stamp_duty,
                    transfer_fee=transfer_fee,
                    slippage_cost=slippage_cost,
                    total_cost=commission + stamp_duty + transfer_fee + slippage_cost,
                    reason=reason,
                    blocked=False,
                )
                trade.update(
                    {
                        "strategy_id": row.get("strategy_id", strategy_def.get("strategy_id", strategy)),
                        "strategy_name": row.get("strategy_name", strategy_def.get("name", strategy)),
                        "entry_matched_count": int(row.get("entry_matched_count") or 0),
                        "entry_failed_count": int(row.get("entry_failed_count") or 0),
                        "entry_missing_count": int(row.get("entry_missing_count") or 0),
                        "exit_matched_count": int(row.get("exit_matched_count") or 0),
                        "exit_failed_count": int(row.get("exit_failed_count") or 0),
                        "exit_missing_count": int(row.get("exit_missing_count") or 0),
                        "limit_price_source": str(row.get("limit_price_source") or ""),
                        "risk_line_type": risk_line_type,
                        "risk_line_price": risk_line_price if math.isfinite(risk_line_price) else None,
                        "risk_line_triggered": bool(risk_triggered),
                    }
                )
                trades.append(trade)

        market_value = shares * close_price if shares > 0 and math.isfinite(close_price) else 0.0
        equity_rows.append(
            {
                "date": pd.to_datetime(date).strftime("%Y-%m-%d"),
                "cash": cash,
                "shares": shares,
                "close": close_price,
                "market_value": market_value,
                "equity": cash + market_value,
                "position_intent": 1 if shares > 0 else 0,
            }
        )

    equity_curve = pd.DataFrame(equity_rows)
    assumptions = backtest_assumptions_from_config(
        config,
        price_fallback_used=price_fallback_used,
        pct_change_approximated=pct_change_approximated,
    )
    assumptions["strategy_name"] = strategy_def.get("name", strategy)
    assumptions["strategy_id"] = strategy_def.get("strategy_id", strategy)
    assumptions["strategy_warning"] = warning
    if official_metadata_warnings:
        assumptions["official_limit_metadata_warnings"] = official_metadata_warnings
    if "limit_price_source" in df.columns:
        assumptions["limit_price_source_counts"] = {
            str(key): int(value)
            for key, value in df["limit_price_source"].value_counts().items()
        }
    # --- v3.7.0.1 增强假设记录 ---
    if board_rules:
        assumptions["board_type"] = board_rules.board_type
        assumptions["board_label"] = board_rules.label
        assumptions["effective_limit_up_pct"] = effective_limit_up
        assumptions["effective_limit_down_pct"] = effective_limit_down
    assumptions["enforce_t_plus_one"] = config.enforce_t_plus_one
    assumptions["t_plus_one_blocked_count"] = t_plus_one_blocked_count
    assumptions["t_plus_one_warnings"] = t_plus_one_warnings
    assumptions["use_liquidity_slippage"] = config.use_liquidity_slippage
    if not df.empty:
        assumptions["backtest_period"] = f"{df['date'].min():%Y-%m-%d} to {df['date'].max():%Y-%m-%d}"
    metrics = metrics_from_equity(equity_curve, trades, assumptions, config.initial_cash, benchmark_series)
    return BacktestResult(equity_curve, trades, metrics, assumptions)

# Portfolio backtest

def prepare_portfolio_frame(
    symbol: str,
    hist: pd.DataFrame,
    strategy: str = "ma_breakout",
    *,
    custom_score_weights: dict[str, float] | None = None,
) -> pd.DataFrame:
    if hist.empty:
        return pd.DataFrame()
    df = hist.copy()
    if "date" not in df.columns:
        for source in ("日期", "date", "trade_date", "datetime", "time"):
            if source in df.columns:
                df = df.rename(columns={source: "date"})
                break
    df["date"] = pd.to_datetime(df.get("date"), errors="coerce")
    df = add_indicators(df)
    df = _ensure_trade_aliases(df)
    for column in ["open", "high", "low", "close", "volume", "amount", "turnover"]:
        if column in df.columns:
            df[column] = pd.to_numeric(df[column], errors="coerce")
    df = df.dropna(subset=["date"]).sort_values("date").reset_index(drop=True)
    if df.empty:
        return df
    signals = backtest_signal_frame(df, strategy=strategy)
    signals["date"] = pd.to_datetime(signals["date"], errors="coerce")
    df = signals.copy()
    score_components = df.apply(_score_components_for_row, axis=1, result_type="expand")
    for column in ["trend_score", "momentum_score", "activity_score", "position_score", "risk_penalty"]:
        df[column] = pd.to_numeric(score_components[column], errors="coerce")
    df["score_components"] = score_components.to_dict("records")
    # v3.7.0.1: 支持 IC 验证权重或自定义权重替代硬编码默认值
    _w = custom_score_weights or {"trend": 0.28, "momentum": 0.22, "activity": 0.18, "position": 0.14, "risk": 0.18}
    # Round 21 Part D: 缺失因子显式 NA、不参与加权（贡献 0 分），固定分母保持
    # 既有数字基线不漂移；逐行输出 score_coverage（在场权重占比）供分层复核。
    _missing_factor_warnings: list[str] = []
    for _col in ("trend_score", "momentum_score", "activity_score", "position_score", "risk_penalty"):
        _missing = int(df[_col].isna().sum())
        if _missing > 0:
            _missing_factor_warnings.append(
                f"{_col}: {_missing} rows missing factor score, kept as explicit NA and "
                "excluded from weighting (fixed denominator; score deflated, conservative bias)"
            )
    _factor_specs = [
        ("trend_score", _w.get("trend", 0.28), df["trend_score"]),
        ("momentum_score", _w.get("momentum", 0.22), df["momentum_score"]),
        ("activity_score", _w.get("activity", 0.18), df["activity_score"]),
        ("position_score", _w.get("position", 0.14), df["position_score"]),
        ("risk_penalty", _w.get("risk", 0.18), 100 - df["risk_penalty"]),
    ]
    _score_sum = pd.Series(0.0, index=df.index)
    _weight_present = pd.Series(0.0, index=df.index)
    _weight_total = 0.0
    for _name, _weight, _series in _factor_specs:
        _weight = float(_weight)
        _weight_total += _weight
        _score_sum = _score_sum + _series.fillna(0) * _weight
        _weight_present = _weight_present + df[_name].notna().astype(float) * _weight
    df["score_coverage"] = (
        (_weight_present / _weight_total).round(6) if _weight_total > 0 else 0.0
    )
    df["score_proxy"] = np.where(df["raw_signal"].fillna(False), _score_sum, -np.inf)
    df["score_proxy"] = pd.to_numeric(df["score_proxy"], errors="coerce").fillna(-np.inf)
    df.attrs["score_weight_policy"] = {
        "weights": dict(_w),
        "weights_provenance": "custom_supplied" if custom_score_weights else "heuristic_unvalidated",
        "missing_factor_handling": "explicit_na_excluded_fixed_denominator",
        "coverage_column": "score_coverage",
        "note": "权重未经统计验证，禁止在输出中使用「最优/推荐」措辞描述该组合。",
    }
    df["signal_for_execution"] = df["raw_signal"].shift(1).where(df["raw_signal"].shift(1).notna(), False).astype(bool)
    df["entry_signal_for_execution"] = df["entry_signal"].shift(1).where(df["entry_signal"].shift(1).notna(), False).astype(bool)
    df["exit_signal_for_execution"] = df["exit_signal"].shift(1).where(df["exit_signal"].shift(1).notna(), False).astype(bool)
    df["score_proxy_for_execution"] = df["score_proxy"].shift(1)
    df["signal_date"] = df["date"].shift(1)
    df["symbol"] = symbol
    if _missing_factor_warnings:
        existing = df.attrs.get("warnings", [])
        df.attrs["warnings"] = existing + _missing_factor_warnings
    return df

def should_rebalance_date(date: pd.Timestamp, previous_date: pd.Timestamp | None, rule: str) -> bool:
    if previous_date is None:
        return True
    rule = str(rule or "weekly").lower()
    if rule == "daily":
        return True
    if rule == "monthly":
        return (date.year, date.month) != (previous_date.year, previous_date.month)
    return date.isocalendar()[:2] != previous_date.isocalendar()[:2]

def position_market_value(position: dict[str, float], close_price: float) -> float:
    shares = int(position.get("shares", 0))
    if shares <= 0 or not math.isfinite(close_price):
        return 0.0
    return shares * close_price


def check_position_constraints(
    position: dict,
    portfolio_equity: float,
    daily_amount: float,
    config: PortfolioBacktestConfig,
) -> dict:
    """Evaluate research-only portfolio concentration and liquidity constraints."""

    violations: list[str] = []
    equity = safe_number(portfolio_equity, 0.0)
    market_value = safe_number(position.get("market_value"), 0.0)
    industry = str(position.get("industry") or "")
    industry_total = safe_number(position.get("industry_total_market_value"), 0.0)
    if equity > 0 and config.max_industry_pct is not None:
        industry_exposure = industry_total / equity
        if industry_exposure > config.max_industry_pct:
            label = industry or "unknown"
            violations.append(f"industry_{label}_exceeds_{config.max_industry_pct:.0%}")
    if daily_amount > 0 and market_value / daily_amount > 10:
        violations.append("liquidity_days_to_liquidate_gt_10")
    if equity > 0 and config.max_position_pct:
        weight = market_value / equity
        if weight > config.max_position_pct * 1.1:
            violations.append(f"single_stock_weight_{weight:.1%}_exceeds_{config.max_position_pct:.0%}")
    return {
        "passed": len(violations) == 0,
        "violations": violations,
    }

def normalize_data_health_rows(data_health: object | None) -> dict[str, dict[str, object]]:
    if data_health is None:
        return {}
    if isinstance(data_health, pd.DataFrame):
        rows = data_health.to_dict("records")
    elif isinstance(data_health, dict):
        rows = []
        for symbol, value in data_health.items():
            if isinstance(value, dict):
                row = dict(value)
                row.setdefault("symbol", symbol)
            else:
                row = {"symbol": symbol, "warning_level": value}
            rows.append(row)
    else:
        rows = list(data_health)  # type: ignore[arg-type]
    normalized: dict[str, dict[str, object]] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        symbol = normalize_symbol(row.get("symbol") or row.get("code"))
        if symbol:
            normalized[symbol] = row
    return normalized

def simulate_portfolio_backtest(
    histories: dict[str, pd.DataFrame],
    config: PortfolioBacktestConfig | None = None,
    benchmark_series: pd.Series | None = None,
    data_health: object | None = None,
    strategy: str = "ma_breakout",
) -> BacktestResult:
    """Run the public portfolio backtest wrapper while delegating simulation internals."""
    from quantdesk.research.backtest_portfolio_simulation import (
        simulate_portfolio_backtest as _simulate_portfolio_backtest,
    )

    return _simulate_portfolio_backtest(histories, config, benchmark_series, data_health, strategy=strategy)

# Compatibility exports remain available from quantdesk.backtest while their
# implementations live in focused research modules.
from quantdesk.research.backtest_export import export_backtest_result, json_safe
from quantdesk.research.backtest_simulation import simulate_backtest_result

def backtest(
    histories: dict[str, pd.DataFrame],
    config: BacktestConfig | None = None,
    benchmark_series: pd.Series | None = None,
    strategy: str = "ma_breakout",
) -> BacktestMetrics:
    return simulate_backtest_result(histories, config, benchmark_series, strategy=strategy).metrics
