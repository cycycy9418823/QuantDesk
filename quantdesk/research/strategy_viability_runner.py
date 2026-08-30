from __future__ import annotations

import hashlib
import json
import math
import tempfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd

from quantdesk.data.schema_normalizer import normalize_daily_csv
from quantdesk.paper.market_rules import round_notional_down_to_lot_quantity
from quantdesk.paper.trade_costs import DEFAULT_COMMISSION_RATE, DEFAULT_SLIPPAGE_BPS, DEFAULT_STAMP_TAX_RATE
from quantdesk.research.strategy_pipeline_repair import (
    build_pit_mode_status,
    compute_benchmark_comparison,
    default_strategy_repair_config,
    get_low_price_strategy_viability_config,
)
from quantdesk.research.strategy_viability_data_precheck import (
    PROJECT_ROOT,
    build_strategy_viability_precheck,
    write_precheck_outputs,
)
from quantdesk.price_basis import price_basis_labels
from quantdesk.shared import truthy
from quantdesk.version import API_SCHEMA_VERSION, APP_VERSION, PACKAGE_VERSION

RESULT_JSON = PROJECT_ROOT / "results" / "strategy_viability_baselines_latest.json"
REPORT_MD = PROJECT_ROOT / "reports" / "strategy_viability_baselines_latest.md"
LOW_PRICE_JSON = PROJECT_ROOT / "results" / "strategy_viability_low_price_latest.json"
LOW_PRICE_MD = PROJECT_ROOT / "reports" / "strategy_viability_low_price_latest.md"
DISCLAIMER = "Research-only baseline strategy viability test. No broker, no real orders, no automatic trading."

BASELINES = {
    "momentum_20d": {"weights": {"momentum_20d": 1.0}, "label": "pure_momentum"},
    "low_volatility": {"weights": {"volatility_20d": -1.0}, "label": "low_volatility"},
    "momentum_low_vol": {"weights": {"momentum_20d": 0.6, "volatility_20d": -0.4}, "label": "momentum_low_vol"},
    "trend_continuation": {"weights": {"trend_60d": 0.7, "momentum_20d": 0.3}, "label": "trend_continuation"},
    "rsi_recovery": {"weights": {"rsi_recovery": 1.0, "momentum_20d": 0.2}, "label": "rsi_recovery"},
    "rsi_recovery_weekly_control": {"weights": {"rsi_recovery": 1.0, "momentum_20d": 0.2}, "label": "rsi_recovery_weekly_control", "rebalance": "weekly"},
}

STRATEGY_FACTOR_REQUIREMENTS = {
    "momentum_20d": ["momentum_20d"],
    "low_volatility": ["volatility_20d"],
    "momentum_low_vol": ["momentum_20d", "volatility_20d"],
    "trend_continuation": ["trend_60d"],
    "rsi_recovery": ["rsi_recovery", "rsi_14"],
    "rsi_recovery_weekly_control": ["rsi_recovery", "rsi_14"],
}


@dataclass
class CostConfig:
    commission_bps: float = DEFAULT_COMMISSION_RATE * 10_000
    stamp_tax_bps: float = DEFAULT_STAMP_TAX_RATE * 10_000
    slippage_bps: float = DEFAULT_SLIPPAGE_BPS


def _rel(path: str | Path) -> str:
    target = Path(path)
    try:
        return str(target.relative_to(PROJECT_ROOT)).replace("\\", "/")
    except Exception:
        return str(target).replace("\\", "/")


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        number = float(value)
    except Exception:
        return default
    return number if math.isfinite(number) else default


def _json_safe(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_json_safe(v) for v in value]
    if isinstance(value, tuple):
        return [_json_safe(v) for v in value]
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if hasattr(value, "item"):
        try:
            return _json_safe(value.item())
        except Exception:
            pass
    return value


def factor_requirements_for_strategy(strategy_id: str) -> list[str]:
    return list(STRATEGY_FACTOR_REQUIREMENTS.get(str(strategy_id), list((BASELINES.get(str(strategy_id)) or {}).get("weights", {}).keys())))


def _stable_hash(value: Any) -> str:
    text = json.dumps(_json_safe(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# --- latest-report truthfulness gate ------------------------------------
#
# A canonical latest report (results/*_latest.json, reports/*_latest.md) may
# only be produced from the project's own canonical ``data/`` root. Reports
# built from temporary, pytest, fixture, or synthetic data roots may still be
# written as run-specific diagnostic output, but they must never overwrite the
# canonical latest evidence and must carry ``latest_eligible=false`` with an
# explicit blocked reason. Path comparison is done on resolved Paths, never on
# fragile string containment.

DATA_ROOT_CANONICAL = "canonical"
DATA_ROOT_TEMPORARY = "temporary"
DATA_ROOT_FIXTURE = "fixture"
DATA_ROOT_SYNTHETIC = "synthetic"
DATA_ROOT_UNKNOWN = "unknown"

_FIXTURE_PATH_MARKERS = (".quantdesk_test_tmp", "fixtures", "fixture", "testdata", "test_data")


def _resolved_canonical_data_root() -> Path:
    return (PROJECT_ROOT / "data").resolve()


def _is_relative_to(child: Path, parent: Path) -> bool:
    try:
        child.relative_to(parent)
        return True
    except ValueError:
        return False


def classify_data_root_kind(data_root: str | Path | None, *, synthetic: bool = False) -> dict[str, Any]:
    """Classify a data root as canonical / temporary / fixture / synthetic / unknown.

    Uses resolved Paths, not string containment, so a Temp path that merely
    happens to contain the substring ``data`` is never mistaken for canonical.
    """

    raw = Path(data_root) if data_root is not None else (PROJECT_ROOT / "data")
    if not raw.is_absolute():
        raw = PROJECT_ROOT / raw
    try:
        resolved = raw.resolve()
    except OSError:
        resolved = raw

    canonical = _resolved_canonical_data_root()
    is_canonical = resolved == canonical

    kind = DATA_ROOT_UNKNOWN
    if is_canonical:
        kind = DATA_ROOT_CANONICAL
    elif synthetic:
        kind = DATA_ROOT_SYNTHETIC
    else:
        try:
            temp_root = Path(tempfile.gettempdir()).resolve()
        except OSError:
            temp_root = None
        parts_lower = {part.lower() for part in resolved.parts}
        has_fixture_marker = any(
            marker in part.lower() for part in resolved.parts for marker in _FIXTURE_PATH_MARKERS
        )
        has_pytest_marker = any(part.lower().startswith("pytest-") for part in resolved.parts)
        if has_fixture_marker:
            kind = DATA_ROOT_FIXTURE
        elif temp_root is not None and _is_relative_to(resolved, temp_root):
            kind = DATA_ROOT_TEMPORARY
        elif has_pytest_marker or "tmp" in parts_lower:
            kind = DATA_ROOT_TEMPORARY
        else:
            kind = DATA_ROOT_UNKNOWN

    return {
        "data_root_kind": kind,
        "is_canonical_data_root": is_canonical,
        "resolved_data_root": str(resolved).replace("\\", "/"),
    }


def build_report_truthfulness(
    data_root: str | Path | None,
    *,
    write_latest: bool,
    smoke_run: bool,
    synthetic: bool = False,
    dataset_symbol_count: int | None = None,
    dataset_date_start: str | None = None,
    dataset_date_end: str | None = None,
) -> dict[str, Any]:
    """Return machine-readable latest-eligibility fields for a report provenance.

    ``latest_eligible`` is true only for a canonical data root on a
    non-smoke, latest-requested run. Every other case yields an explicit
    ``latest_write_blocked_reason`` so a stale/temporary report can never be
    silently promoted to canonical evidence.
    """

    classification = classify_data_root_kind(data_root, synthetic=synthetic)
    kind = classification["data_root_kind"]
    is_canonical = bool(classification["is_canonical_data_root"])

    blocked_reason = ""
    if not is_canonical:
        blocked_reason = f"non_canonical_data_root:{kind}"
    elif smoke_run:
        blocked_reason = "smoke_run_not_eligible_for_canonical_latest"
    elif not write_latest:
        blocked_reason = "write_latest_not_requested"

    latest_eligible = is_canonical and not smoke_run and bool(write_latest)

    return {
        "data_root_kind": kind,
        "is_canonical_data_root": is_canonical,
        "resolved_data_root": classification["resolved_data_root"],
        "latest_eligible": latest_eligible,
        "latest_write_blocked_reason": blocked_reason,
        "dataset_symbol_count": int(dataset_symbol_count) if dataset_symbol_count is not None else None,
        "dataset_date_start": dataset_date_start,
        "dataset_date_end": dataset_date_end,
        "smoke_run": bool(smoke_run),
    }


# --- zero-trade / execution-evidence diagnosis --------------------------
#
# When every baseline produces an empty equity curve (zero trades, zero
# holdings), the identical-hash "distinctness" check would mislabel the run as
# ``strategy_scoring_degenerated``. That is wrong: the strategies were never
# actually executed. This classifier separates the real causes so the audit can
# report ``insufficient_execution_evidence`` instead of a false homogeneity
# verdict, and so no "best strategy" is chosen from empty curves.

EXECUTION_STATUS_SUFFICIENT = "sufficient_execution_evidence"
EXECUTION_STATUS_INSUFFICIENT = "insufficient_execution_evidence"
EXECUTION_STATUS_NO_PRICE_HISTORY = "no_price_history_or_signal"


def classify_execution_evidence(results: list[dict[str, Any]], *, panel_empty: bool) -> dict[str, Any]:
    """Diagnose whether baselines produced enough real trades to be comparable.

    Returns a status plus a specific reason distinguishing: no history/signal,
    no price-filtered candidate, lot-size infeasibility, insufficient factor
    coverage, and genuinely-executed runs. Never chooses a best strategy; only
    describes execution feasibility.
    """

    total_trade_count = 0
    total_unfilled_due_to_lot_size = 0
    baselines_with_trades = 0
    max_price_filtered_universe = 0
    saw_missing_factor = False
    saw_insufficient_cross_section = False
    for row in results or []:
        metrics = row.get("metrics") or {}
        trade_count = int(metrics.get("trade_count") or 0)
        total_trade_count += trade_count
        total_unfilled_due_to_lot_size += int(metrics.get("unfilled_due_to_lot_size_count") or 0)
        if trade_count > 0:
            baselines_with_trades += 1
        price_filter = row.get("price_filter") or {}
        max_price_filtered_universe = max(
            max_price_filtered_universe, int(price_filter.get("max_price_filtered_universe_count") or 0)
        )
        for warning in row.get("warnings") or []:
            text = str(warning)
            if text.startswith("missing_factor_requirements"):
                saw_missing_factor = True
            if text.startswith("insufficient_strategy_cross_section"):
                saw_insufficient_cross_section = True

    if panel_empty or not results:
        reason = "price_panel_empty" if panel_empty else "no_baseline_results"
        return {
            "status": EXECUTION_STATUS_NO_PRICE_HISTORY,
            "reason": reason,
            "distinctness_evaluable": False,
            "total_trade_count": 0,
            "total_unfilled_due_to_lot_size": total_unfilled_due_to_lot_size,
            "baselines_with_trades": 0,
            "warnings": [EXECUTION_STATUS_NO_PRICE_HISTORY, reason],
        }

    if total_trade_count <= 0:
        if total_unfilled_due_to_lot_size > 0:
            reason = "no_lot_feasible_candidate"
        elif max_price_filtered_universe <= 0:
            reason = "no_price_filtered_candidate"
        elif saw_missing_factor:
            reason = "insufficient_factor_coverage"
        elif saw_insufficient_cross_section:
            reason = "insufficient_cross_section"
        else:
            reason = "no_executable_trades"
        return {
            "status": EXECUTION_STATUS_INSUFFICIENT,
            "reason": reason,
            "distinctness_evaluable": False,
            "total_trade_count": 0,
            "total_unfilled_due_to_lot_size": total_unfilled_due_to_lot_size,
            "baselines_with_trades": 0,
            "warnings": [EXECUTION_STATUS_INSUFFICIENT, reason],
        }

    return {
        "status": EXECUTION_STATUS_SUFFICIENT,
        "reason": "baselines_have_executable_trades",
        "distinctness_evaluable": True,
        "total_trade_count": total_trade_count,
        "total_unfilled_due_to_lot_size": total_unfilled_due_to_lot_size,
        "baselines_with_trades": baselines_with_trades,
        "warnings": [],
    }


def validate_performance_metrics_sanity(metrics: dict[str, Any], equity_curve: list[dict[str, Any]] | list[float]) -> dict[str, Any]:
    warnings: list[str] = []
    status = "passed"
    values: list[float] = []
    for row in equity_curve or []:
        if isinstance(row, dict):
            values.append(_safe_float(row.get("net_equity"), math.nan))
        else:
            values.append(_safe_float(row, math.nan))
    values = [x for x in values if math.isfinite(x)]
    if values:
        series = pd.Series(values, dtype="float64")
        observed_dd = _max_drawdown(series)
        reported_dd = _safe_float(metrics.get("max_drawdown"), 0.0)
        if observed_dd < -1e-9 and abs(reported_dd) < 1e-12:
            status = "failed"
            warnings.append("drawdown_sanity_failed")
    sharpe = metrics.get("sharpe")
    if sharpe is not None:
        sharpe_value = _safe_float(sharpe, 0.0)
        if abs(sharpe_value) > 10:
            status = "failed"
            warnings.append("suspicious_sharpe_ratio")
    returns: list[float] = []
    for idx in range(1, len(values)):
        prev = values[idx - 1]
        if prev:
            returns.append(values[idx] / prev - 1.0)
    if len(returns) >= 2 and pd.Series(returns).std(ddof=0) < 1e-12 and abs(_safe_float(metrics.get("annualized_return_net"), 0.0)) > 1e-9:
        status = "failed"
        warnings.append("invalid_near_zero_volatility")
    return {"status": status, "warnings": warnings}


def validate_strategy_result_metrics_consistency(result: dict[str, Any]) -> dict[str, Any]:
    metrics = result.get("metrics") or {}
    mapping = {
        "annualized_return_net": ("annualized_return_net", "annual_return_net"),
        "annualized_return_gross": ("annualized_return_gross", "annual_return_gross"),
        "max_drawdown": ("max_drawdown",),
        "sharpe": ("sharpe",),
        "sortino": ("sortino",),
        "turnover_annualized": ("turnover_annualized",),
        "cost_impact": ("cost_impact",),
        "invested_ratio_avg": ("invested_ratio_avg",),
        "unfilled_due_to_lot_size_count": ("unfilled_due_to_lot_size_count",),
    }
    mismatches: list[str] = []
    for top_key, metric_keys in mapping.items():
        if top_key not in result:
            continue
        metric_value = None
        for key in metric_keys:
            if key in metrics:
                metric_value = metrics.get(key)
                break
        if metric_value is None:
            continue
        if abs(_safe_float(result.get(top_key), 0.0) - _safe_float(metric_value, 0.0)) > 1e-10:
            mismatches.append(top_key)
    return {
        "status": "failed" if mismatches else "passed",
        "mismatches": mismatches,
        "warnings": ["top_level_metrics_mismatch"] if mismatches else [],
    }


def _daily_dir(data_root: Path, adjustment: str) -> Path:
    if adjustment == "forward":
        preferred = data_root / "daily" / "forward_adjusted"
        if preferred.exists() and any(preferred.glob("*.csv")):
            return preferred
        return data_root / "daily"
    if adjustment == "back":
        return data_root / "daily" / "back_adjusted"
    if adjustment == "raw":
        return data_root / "daily" / "raw"
    return data_root / "daily"


def _normalize_daily_frame(path: Path) -> tuple[pd.DataFrame, list[str]]:
    frame, meta = normalize_daily_csv(path)
    warnings = [f"{path.name}:{item}" for item in (meta.get("warnings") or [])[:5]]
    if frame.empty:
        return pd.DataFrame(), [f"{path.name}:{item}" for item in (meta.get("errors") or ["empty_or_quarantined"])]
    frame["date"] = pd.to_datetime(frame["date"], errors="coerce")
    for column in ("open", "high", "low", "close", "volume", "amount"):
        frame[column] = pd.to_numeric(frame[column], errors="coerce")
    frame = frame.dropna(subset=["date", "symbol", "close"])
    frame = frame[frame["close"] > 0]
    return frame[["date", "symbol", "open", "high", "low", "close", "volume", "amount"]].reset_index(drop=True), warnings


def load_price_panel(data_root: str | Path = "data", *, adjustment: str = "forward", max_symbols: int = 0, sample_seed: int = 20260528) -> tuple[pd.DataFrame, list[str]]:
    root = Path(data_root)
    if not root.is_absolute():
        root = PROJECT_ROOT / root
    daily_dir = _daily_dir(root, adjustment)
    warnings: list[str] = []
    files = sorted(daily_dir.glob("*.csv")) if daily_dir.exists() else []
    if max_symbols and max_symbols > 0 and len(files) > int(max_symbols):
        files = list(pd.Series(files).sample(n=int(max_symbols), random_state=int(sample_seed)).sort_values())
        warnings.append(f"performance_fallback_sampling:sample_method=random:sample_size={max_symbols}:sample_seed={sample_seed}")
    frames: list[pd.DataFrame] = []
    for path in files:
        frame, file_warnings = _normalize_daily_frame(path)
        warnings.extend(file_warnings[:2])
        if frame.empty or len(frame) < 121:
            continue
        frames.append(frame)
    if not frames:
        return pd.DataFrame(), warnings + [f"no_usable_daily_csv:{_rel(daily_dir)}"]
    panel = pd.concat(frames, ignore_index=True).sort_values(["symbol", "date"]).reset_index(drop=True)
    return panel, list(dict.fromkeys(warnings))


def _compute_features(panel: pd.DataFrame) -> pd.DataFrame:
    frame = panel.copy().sort_values(["symbol", "date"])
    group = frame.groupby("symbol", group_keys=False)
    frame["ret_1d"] = group["close"].transform(lambda values: values.ffill().pct_change(fill_method=None))
    frame["momentum_20d"] = group["close"].transform(
        lambda values: values.ffill().pct_change(20, fill_method=None)
    )
    frame["trend_60d"] = group["close"].transform(lambda values: values.ffill().pct_change(60, fill_method=None))
    frame["volatility_20d"] = group["ret_1d"].rolling(20, min_periods=10).std().reset_index(level=0, drop=True)
    delta = group["close"].diff()
    gain = delta.clip(lower=0).groupby(frame["symbol"]).rolling(14, min_periods=7).mean().reset_index(level=0, drop=True)
    loss = (-delta.clip(upper=0)).groupby(frame["symbol"]).rolling(14, min_periods=7).mean().reset_index(level=0, drop=True)
    rs = gain / loss.replace(0, pd.NA)
    frame["rsi_14"] = 100 - (100 / (1 + rs))
    frame["rsi_recovery"] = (50 - frame["rsi_14"]).where(frame["rsi_14"] < 50, 0)
    frame["listed_days"] = group.cumcount() + 1
    frame["zero_volume_5d"] = group["volume"].rolling(5, min_periods=1).apply(lambda x: 1.0 if (x == 0).any() else 0.0).reset_index(level=0, drop=True)
    return frame


def _month_signal_dates(dates: list[pd.Timestamp]) -> list[pd.Timestamp]:
    if not dates:
        return []
    frame = pd.DataFrame({"date": sorted(pd.to_datetime(dates))})
    frame["month"] = frame["date"].dt.to_period("M")
    return frame.groupby("month")["date"].max().tolist()


def _max_drawdown(equity: pd.Series) -> float:
    if equity.empty:
        return 0.0
    return float((equity / equity.cummax() - 1.0).min())


def _annual_return(equity: pd.Series, periods: int) -> float:
    if equity.empty or periods <= 0:
        return 0.0
    total = float(equity.iloc[-1] / equity.iloc[0] - 1.0)
    return (1.0 + total) ** (12.0 / max(periods, 1)) - 1.0


def _sharpe(returns: list[float]) -> float:
    series = pd.Series(returns, dtype="float64")
    if len(series) < 2 or series.std(ddof=0) == 0:
        return 0.0
    return float(series.mean() / series.std(ddof=0) * math.sqrt(12))


def _sortino(returns: list[float]) -> float:
    series = pd.Series(returns, dtype="float64")
    downside = series[series < 0]
    if len(series) < 2 or downside.std(ddof=0) == 0:
        return 0.0
    return float(series.mean() / downside.std(ddof=0) * math.sqrt(12))


def _yearly_returns(equity_rows: list[dict[str, Any]]) -> dict[str, float]:
    if not equity_rows:
        return {}
    frame = pd.DataFrame(equity_rows)
    frame["date"] = pd.to_datetime(frame["date"])
    frame["year"] = frame["date"].dt.year.astype(str)
    out: dict[str, float] = {}
    for year, group in frame.groupby("year"):
        values = pd.to_numeric(group["net_equity"], errors="coerce").dropna()
        if len(values) >= 2:
            out[str(year)] = float(values.iloc[-1] / values.iloc[0] - 1.0)
    return out


def _score_frame(frame: pd.DataFrame, weights: dict[str, float]) -> pd.Series:
    score = pd.Series(0.0, index=frame.index)
    for column, weight in weights.items():
        values = pd.to_numeric(frame.get(column), errors="coerce")
        rank = values.rank(pct=True)
        score = score + rank.fillna(0.0) * float(weight)
    return score


def run_one_baseline(features: pd.DataFrame, name: str, *, top_n: int = 20, cost: CostConfig | None = None, strategy_config: dict[str, Any] | None = None) -> dict[str, Any]:
    cost = cost or CostConfig()
    cfg = strategy_config or default_strategy_repair_config()
    spec = BASELINES[name]
    weights = dict(spec["weights"])
    factor_requirements = factor_requirements_for_strategy(name)
    rebalance = str(spec.get("rebalance") or cfg.get("rebalance") or "monthly")
    all_dates = sorted(pd.to_datetime(features["date"].dropna().unique()))
    signal_dates = _month_signal_dates(all_dates) if rebalance == "monthly" else all_dates[::5]
    by_symbol = {symbol: group.set_index("date").sort_index() for symbol, group in features.groupby("symbol")}
    prior_weights: dict[str, float] = {}
    gross_equity = 1.0
    net_equity = 1.0
    period_returns: list[float] = []
    turnovers: list[float] = []
    rows: list[dict[str, Any]] = []
    selections: list[dict[str, Any]] = []
    selected_symbols_by_rebalance: list[dict[str, Any]] = []
    eligible_count_by_rebalance: list[dict[str, Any]] = []
    selected_count_by_rebalance: list[dict[str, Any]] = []
    score_frame_summary: list[dict[str, Any]] = []
    strategy_warnings: list[str] = []
    price_counts: list[int] = []
    unfilled_due_to_lot_size_count = 0
    invested_ratios: list[float] = []
    cash_drags: list[float] = []
    price_filter = cfg.get("price_filter") or {}
    price_filter_enabled = bool(price_filter.get("enabled", cfg.get("price_filter_enabled", False)))
    max_price = _safe_float(price_filter.get("max_price", cfg.get("price_filter_max_price", 0.0)), 0.0)
    initial_cash = _safe_float(cfg.get("initial_cash"), 1_000_000.0)
    max_position_pct = _safe_float(cfg.get("max_position_pct"), 1.0 / max(1, int(top_n)))
    lot_size = max(1, int(cfg.get("lot_size") or cfg.get("min_trade_lot") or 1))
    for idx, signal_date in enumerate(signal_dates[:-1]):
        next_signal = signal_dates[idx + 1]
        execution_candidates = [date for date in all_dates if date > signal_date]
        exit_candidates = [date for date in all_dates if date > next_signal]
        if not execution_candidates or not exit_candidates:
            continue
        execution_date = execution_candidates[0]
        exit_date = exit_candidates[0]
        cross = features[features["date"] == signal_date].copy()
        cross = cross[(cross["listed_days"] >= int(cfg.get("min_listing_age_days") or 120)) & (cross["zero_volume_5d"] == 0)]
        if price_filter_enabled and max_price > 0:
            cross = cross[pd.to_numeric(cross["close"], errors="coerce") <= max_price]
        price_counts.append(int(len(cross)))
        eligible_before_factors = int(len(cross))
        available_requirements = [col for col in factor_requirements if col in cross.columns]
        missing_requirements = [col for col in factor_requirements if col not in cross.columns]
        if missing_requirements:
            strategy_warnings.append(f"missing_factor_requirements:{name}:{','.join(missing_requirements)}")
        cross = cross.dropna(subset=available_requirements) if available_requirements else cross.iloc[0:0]
        eligible_after_factors = int(len(cross))
        eligible_count_by_rebalance.append({
            "signal_date": signal_date.strftime("%Y-%m-%d"),
            "eligible_count": eligible_after_factors,
            "eligible_before_factor_filter": eligible_before_factors,
        })
        if eligible_after_factors < max(3, min(int(top_n), 3)):
            strategy_warnings.append(f"insufficient_strategy_cross_section:{name}:{signal_date.strftime('%Y-%m-%d')}:{eligible_after_factors}")
        if cross.empty:
            continue
        cross["score"] = _score_frame(cross, weights)
        score_frame_summary.append({
            "signal_date": signal_date.strftime("%Y-%m-%d"),
            "eligible_count": eligible_after_factors,
            "factor_requirements": factor_requirements,
            "strategy_specific_factor_coverage": {col: int(pd.to_numeric(cross[col], errors="coerce").notna().sum()) for col in available_requirements},
        })
        candidates = cross.sort_values("score", ascending=False).head(top_n)["symbol"].astype(str).tolist()
        executable: list[tuple[str, float, float]] = []
        weighted_returns: list[float] = []
        remaining_capital = max(0.0, initial_cash)
        for symbol in candidates:
            hist = by_symbol.get(symbol)
            if hist is None or execution_date not in hist.index or exit_date not in hist.index:
                continue
            entry = _safe_float(hist.loc[execution_date, "close"])
            exit_price = _safe_float(hist.loc[exit_date, "close"])
            if entry <= 0 or exit_price <= 0:
                continue
            target_cash = min(initial_cash * max_position_pct, remaining_capital)
            shares = round_notional_down_to_lot_quantity(target_cash, entry, lot_size=lot_size)
            if shares < lot_size:
                unfilled_due_to_lot_size_count += 1
                continue
            invested = shares * entry
            remaining_capital = max(0.0, remaining_capital - invested)
            ret = exit_price / entry - 1.0
            executable.append((symbol, invested, shares))
            weighted_returns.append(ret * invested)
        if not executable:
            selected_count_by_rebalance.append({"signal_date": signal_date.strftime("%Y-%m-%d"), "selected_count": 0})
            continue
        total_invested = sum(item[1] for item in executable)
        invested_ratio = total_invested / initial_cash if initial_cash > 0 else 0.0
        invested_ratios.append(invested_ratio)
        cash_drags.append(max(0.0, 1.0 - invested_ratio))
        # P0-4: portfolio return is measured on TOTAL capital, not just the
        # invested slice. Idle cash earns 0, so dilute the invested-capital return
        # by invested_ratio. A fully-invested config (invested_ratio == 1.0) is
        # unchanged, preserving the zero-cost fully-invested baseline.
        asset_return = float(sum(weighted_returns) / total_invested) if total_invested > 0 else 0.0
        cash_return = 0.0
        gross_ret = asset_return * invested_ratio + cash_return * max(0.0, 1.0 - invested_ratio)
        selected = [item[0] for item in executable]
        selected_symbols_by_rebalance.append({"signal_date": signal_date.strftime("%Y-%m-%d"), "symbols": selected})
        selected_count_by_rebalance.append({"signal_date": signal_date.strftime("%Y-%m-%d"), "selected_count": len(selected)})
        new_weights = {
            symbol: invested / initial_cash
            for symbol, invested, _shares in executable
            if initial_cash > 0
        }
        turnover = sum(abs(new_weights.get(s, 0.0) - prior_weights.get(s, 0.0)) for s in set(new_weights) | set(prior_weights))
        buy_sell_cost_bps = cost.commission_bps * 2 + cost.stamp_tax_bps + cost.slippage_bps * 2
        cost_ret = turnover * buy_sell_cost_bps / 10000.0
        net_ret = gross_ret - cost_ret
        gross_equity *= 1.0 + gross_ret
        net_equity *= 1.0 + net_ret
        period_returns.append(net_ret)
        turnovers.append(turnover)
        prior_weights = new_weights
        rows.append({
            "date": exit_date.strftime("%Y-%m-%d"),
            "signal_date": signal_date.strftime("%Y-%m-%d"),
            "execution_date": execution_date.strftime("%Y-%m-%d"),
            "gross_return": gross_ret,
            "net_return": net_ret,
            "asset_return": asset_return,
            "cash_return": cash_return,
            "cost_return": cost_ret,
            "gross_equity": gross_equity,
            "net_equity": net_equity,
            "turnover": turnover,
            "invested_ratio": invested_ratio,
            "cash_drag": max(0.0, 1.0 - invested_ratio),
        })
        if len(selections) < 5:
            selections.append({"signal_date": signal_date.strftime("%Y-%m-%d"), "execution_date": execution_date.strftime("%Y-%m-%d"), "symbols": selected[:10]})
    net_series = pd.Series([1.0] + [row["net_equity"] for row in rows], dtype="float64")
    gross_series = pd.Series([1.0] + [row["gross_equity"] for row in rows], dtype="float64")
    metrics = {
        "annual_return_gross": _annual_return(gross_series, len(rows)),
        "annual_return_net": _annual_return(net_series, len(rows)),
        "annualized_return_gross": _annual_return(gross_series, len(rows)),
        "annualized_return_net": _annual_return(net_series, len(rows)),
        "max_drawdown": _max_drawdown(net_series),
        "sharpe": _sharpe(period_returns),
        "sortino": _sortino(period_returns),
        "average_turnover": float(pd.Series(turnovers).mean()) if turnovers else 0.0,
        "turnover_annualized": float(pd.Series(turnovers).mean() * 12.0) if turnovers else 0.0,
        "trade_count": len(rows),
        "total_return_gross": float(gross_series.iloc[-1] - 1.0) if not gross_series.empty else 0.0,
        "total_return_net": float(net_series.iloc[-1] - 1.0) if not net_series.empty else 0.0,
        "trade_cost_drag": float((gross_series.iloc[-1] - net_series.iloc[-1]) if len(net_series) else 0.0),
        "cost_impact": float((gross_series.iloc[-1] - net_series.iloc[-1]) if len(net_series) else 0.0),
        "period_count": len(rows),
        "unfilled_due_to_lot_size_count": int(unfilled_due_to_lot_size_count),
        "average_cash_drag": float(pd.Series(cash_drags).mean()) if cash_drags else 0.0,
        "invested_ratio_avg": float(pd.Series(invested_ratios).mean()) if invested_ratios else 0.0,
        "final_cash": float(initial_cash * (1.0 - (invested_ratios[-1] if invested_ratios else 0.0))),
        "has_fractional_shares": False,
    }
    sanity = validate_performance_metrics_sanity(metrics, rows)
    strategy_warnings.extend(sanity.get("warnings") or [])
    return {
        "name": name,
        "strategy": spec.get("label") or name,
        "weights": weights,
        "factor_requirements": factor_requirements,
        "top_n": top_n,
        "rebalance": rebalance,
        "profile": cfg.get("profile", "general"),
        "metrics": metrics,
        "annualized_return_net": metrics["annualized_return_net"],
        "annualized_return_gross": metrics["annualized_return_gross"],
        "max_drawdown": metrics["max_drawdown"],
        "sharpe": metrics["sharpe"],
        "sortino": metrics["sortino"],
        "turnover_annualized": metrics["turnover_annualized"],
        "cost_impact": metrics["cost_impact"],
        "invested_ratio_avg": metrics["invested_ratio_avg"],
        "unfilled_due_to_lot_size_count": metrics["unfilled_due_to_lot_size_count"],
        "yearly_returns": _yearly_returns(rows),
        "equity_curve": rows,
        "selection_preview": selections,
        "selected_symbols_by_rebalance": selected_symbols_by_rebalance,
        "score_frame_summary": score_frame_summary,
        "eligible_count_by_rebalance": eligible_count_by_rebalance,
        "selected_count_by_rebalance": selected_count_by_rebalance,
        "equity_curve_hash": _stable_hash(rows),
        "selected_symbols_hash": _stable_hash(selected_symbols_by_rebalance),
        "metrics_sanity": sanity,
        "price_filter": {
            "enabled": price_filter_enabled,
            "max_price": max_price,
            "evaluated_on": "signal_date",
            "average_price_filtered_universe_count": float(pd.Series(price_counts).mean()) if price_counts else 0.0,
            "min_price_filtered_universe_count": int(min(price_counts)) if price_counts else 0,
            "max_price_filtered_universe_count": int(max(price_counts)) if price_counts else 0,
            "skipped_missing_signal_price_count": 0,
        },
        "lot_size": lot_size,
        "warnings": list(dict.fromkeys(strategy_warnings + ([] if rows else ["no_rebalance_periods_generated"]))),
    }


def _decision_gate(best: dict[str, Any], results: list[dict[str, Any]]) -> dict[str, Any]:
    metrics = best.get("metrics") or {}
    ann = _safe_float(metrics.get("annual_return_net"))
    dd = abs(_safe_float(metrics.get("max_drawdown")))
    sharpe = _safe_float(metrics.get("sharpe"))
    if ann > 0.12 and dd < 0.30 and sharpe > 1.0:
        recommendation = "continue_research"
        rationale = "data_backbone_worth_using"
    elif ann < 0.08 or dd > 0.40 or sharpe < 0.6:
        recommendation = "stop_and_rethink"
        rationale = "change_strategy_direction"
    else:
        recommendation = "refine_strategy"
        rationale = "do_not_engineer_until_robustness_improves"
    return {"recommendation": recommendation, "rationale": rationale}


def check_baseline_distinctness(results: list[dict[str, Any]]) -> dict[str, Any]:
    groups: dict[str, list[str]] = {}
    for row in results:
        metrics = row.get("metrics") or {}
        signature = _stable_hash({
            "annualized_return_net": metrics.get("annualized_return_net"),
            "max_drawdown": metrics.get("max_drawdown"),
            "equity_curve_hash": row.get("equity_curve_hash"),
            "selected_symbols_hash": row.get("selected_symbols_hash"),
        })
        groups.setdefault(signature, []).append(str(row.get("name")))
    duplicated = [names for names in groups.values() if len(names) > 1]
    universe_too_small = False
    if duplicated:
        counts: list[int] = []
        for row in results:
            for item in row.get("eligible_count_by_rebalance") or []:
                counts.append(int(item.get("eligible_count") or 0))
        universe_too_small = bool(counts) and max(counts) < 3
    status = "passed"
    warnings: list[str] = []
    reason = ""
    if duplicated:
        status = "warning" if universe_too_small else "failed"
        reason = "universe_too_small_after_filters" if universe_too_small else "strategy_scoring_degenerated"
        warnings.append(reason)
    return {
        "status": status,
        "duplicated_strategy_groups": duplicated,
        "reason": reason,
        "equity_curve_hash_by_strategy": {str(r.get("name")): r.get("equity_curve_hash") for r in results},
        "selected_symbols_hash_by_strategy": {str(r.get("name")): r.get("selected_symbols_hash") for r in results},
        "warnings": warnings,
    }


def _strategy_config_for_profile(profile: str) -> dict[str, Any]:
    return get_low_price_strategy_viability_config() if profile == "low_price" else default_strategy_repair_config()


def _smoke_precheck_payload(data_root: str | Path, pit_mode: str) -> dict[str, Any]:
    return {
        "ok": True,
        "status": "warning",
        "task_type": "strategy_pipeline_precheck",
        "survivorship_bias_risk": "high",
        "pit": {
            "ok": True,
            "status": "warning",
            "pit_mode": pit_mode,
            "survivorship_bias_risk": "high",
            "warnings": ["smoke_run_pit_not_evaluated"],
            "errors": [],
        },
        "benchmark": {"available": False, "warnings": ["smoke_run_benchmark_not_evaluated"]},
        "partial_daily": {
            "path": _rel((Path(data_root) if Path(data_root).is_absolute() else PROJECT_ROOT / data_root) / "intraday" / "partial_daily"),
            "exists": False,
            "isolated": True,
        },
        "warnings": ["smoke_run_precheck_not_evaluated", "smoke_run_pit_not_evaluated", "smoke_run_benchmark_not_evaluated"],
        "errors": [],
        "output_paths": {},
    }


def _smoke_pit_mode_status(pit_mode: str) -> dict[str, Any]:
    return {
        "ok": True,
        "status": "warning",
        "pit_mode": pit_mode,
        "historical_listings": {"path": "", "exists": False},
        "price_coverage_universe": {"mode": "not_evaluated", "symbol_count": 0, "windows": []},
        "survivorship_bias_risk": "high",
        "debug_only": pit_mode == "off",
        "degraded": pit_mode == "price_coverage",
        "warnings": ["smoke_run_pit_not_evaluated"],
        "errors": [],
    }


def run_strategy_viability_baselines(config: dict | None = None) -> dict[str, Any]:
    cfg = config or {}
    profile = str(cfg.get("profile") or "low_price")
    strategy_config = _strategy_config_for_profile(profile)
    data_root = Path(cfg.get("data_root") or "data")
    adjustment = str(cfg.get("adjustment") or strategy_config.get("adjustment") or "forward")
    top_n = int(cfg.get("top_n") or strategy_config.get("top_n") or 10)
    max_symbols = int(cfg.get("max_symbols") or 0)
    sample_seed = int(cfg.get("sample_seed") or strategy_config.get("sample_seed") or 20260528)
    pit_mode = str(cfg.get("pit_mode") or "price_coverage")
    write_latest = bool(cfg.get("write_latest", True))
    smoke_run = bool(cfg.get("smoke_run", False))
    full_universe = truthy(cfg.get("full", False))
    if smoke_run and max_symbols <= 0:
        max_symbols = 20
        strategy_config["sample_method"] = "random"
    elif smoke_run and max_symbols > 20:
        max_symbols = 20
        strategy_config["sample_method"] = "random"
    if max_symbols <= 0 and not full_universe:
        candidate_files = sorted(_daily_dir(Path(data_root) if Path(data_root).is_absolute() else PROJECT_ROOT / data_root, adjustment).glob("*.csv"))
        if len(candidate_files) > 500:
            max_symbols = 500
            strategy_config["sample_method"] = "random"
    if full_universe:
        strategy_config["sample_method"] = "none"
    precheck = _smoke_precheck_payload(data_root, pit_mode) if smoke_run else write_precheck_outputs(build_strategy_viability_precheck({"data_root": data_root, "pit_mode": pit_mode, "adjustment": adjustment, "max_symbols": max_symbols}))
    warnings: list[str] = list(precheck.get("warnings") or [])
    pit_status = _smoke_pit_mode_status(pit_mode) if smoke_run else build_pit_mode_status(pit_mode, data_root=data_root, adjustment=adjustment, max_symbols=max_symbols)
    warnings.extend(pit_status.get("warnings") or [])
    warnings.extend(pit_status.get("errors") or [])
    if smoke_run and int(cfg.get("max_symbols") or 0) <= 0:
        warnings.append("smoke_run_sampling:sample_method=random:sample_size=20:sample_seed=20260528")
    elif smoke_run and int(cfg.get("max_symbols") or 0) > 20:
        warnings.append(f"smoke_run_sample_cap_applied:requested={int(cfg.get('max_symbols') or 0)}:used=20")
    if max_symbols == 500 and int(cfg.get("max_symbols") or 0) <= 0 and not full_universe and not smoke_run:
        warnings.append("performance_fallback_sampling:sample_method=random:sample_size=500:sample_seed=20260528")
    if full_universe:
        warnings.append("full_universe_mode:no_sampling_all_eligible_symbols_used")
    panel, load_warnings = load_price_panel(data_root, adjustment=adjustment, max_symbols=max_symbols, sample_seed=sample_seed)
    warnings.extend(load_warnings)
    # Option C: surface price-basis warnings (forward->flat-raw fallback,
    # divergent adjustment_factor.csv alias) in the visible warnings list too.
    warnings.extend(price_basis_labels(adjustment, data_root=data_root)["adjustment_warnings"])
    results: list[dict[str, Any]] = []
    if panel.empty:
        warnings.append("price_panel_empty")
    else:
        features = _compute_features(panel)
        for name in BASELINES:
            results.append(run_one_baseline(features, name, top_n=top_n, strategy_config=strategy_config))
    best = max(results, key=lambda item: _safe_float((item.get("metrics") or {}).get("annual_return_net"), -999.0)) if results else {"name": None, "metrics": {}}
    gate = _decision_gate(best, results) if results else {"recommendation": "stop_and_rethink", "rationale": "no_usable_price_panel"}
    benchmark = (
        {"available": False, "comparisons": {}, "warnings": ["smoke_run_benchmark_not_evaluated"]}
        if smoke_run
        else compute_benchmark_comparison(_safe_float((best.get("metrics") or {}).get("annual_return_net")) if best else None, data_root)
    )
    # Execution-evidence gate runs BEFORE trusting distinctness. When every
    # baseline has zero trades (empty equity curves), the identical-hash
    # distinctness check would falsely report ``strategy_scoring_degenerated``;
    # instead we mark the run ``insufficient_execution_evidence`` / not
    # evaluable so no false homogeneity verdict is emitted and no best strategy
    # is chosen from empty curves.
    execution_evidence = classify_execution_evidence(results, panel_empty=panel.empty)
    distinctness_raw = check_baseline_distinctness(results)
    if execution_evidence.get("distinctness_evaluable"):
        distinctness = distinctness_raw
    else:
        distinctness = {
            "status": "not_evaluable",
            "duplicated_strategy_groups": distinctness_raw.get("duplicated_strategy_groups") or [],
            "reason": execution_evidence.get("reason") or execution_evidence.get("status"),
            "not_evaluable_reason": execution_evidence.get("status"),
            "equity_curve_hash_by_strategy": distinctness_raw.get("equity_curve_hash_by_strategy") or {},
            "selected_symbols_hash_by_strategy": distinctness_raw.get("selected_symbols_hash_by_strategy") or {},
            "warnings": list(execution_evidence.get("warnings") or []),
        }
    # No best strategy may be chosen when there is not enough real execution
    # evidence to compare baselines: an empty equity curve is not a winner.
    if not execution_evidence.get("distinctness_evaluable"):
        best = {"name": None, "metrics": {}}
        gate = {
            "recommendation": "stop_and_rethink",
            "rationale": execution_evidence.get("reason") or execution_evidence.get("status"),
            "detail": execution_evidence.get("status"),
        }
    consistency_checks = [validate_strategy_result_metrics_consistency(row) for row in results]
    consistency_warnings = [warning for check in consistency_checks for warning in (check.get("warnings") or [])]
    sanity_warnings = [warning for row in results for warning in ((row.get("metrics_sanity") or {}).get("warnings") or [])]
    if distinctness.get("warnings"):
        warnings.extend(distinctness.get("warnings") or [])
    warnings.extend(consistency_warnings)
    warnings.extend(sanity_warnings)
    if smoke_run and write_latest:
        warnings.append("smoke_run_requested_canonical_latest_write")
    price_summaries = [row.get("price_filter") or {} for row in results]
    metrics_rows = [row.get("metrics") or {} for row in results]
    # Dataset descriptors for provenance truthfulness (symbol count / date span).
    dataset_symbol_count = int(panel["symbol"].nunique()) if not panel.empty else 0
    dataset_date_start: str | None = None
    dataset_date_end: str | None = None
    if not panel.empty:
        panel_dates = pd.to_datetime(panel["date"], errors="coerce").dropna()
        if not panel_dates.empty:
            dataset_date_start = panel_dates.min().strftime("%Y-%m-%d")
            dataset_date_end = panel_dates.max().strftime("%Y-%m-%d")
    truthfulness = build_report_truthfulness(
        data_root,
        write_latest=write_latest,
        smoke_run=smoke_run,
        dataset_symbol_count=dataset_symbol_count,
        dataset_date_start=dataset_date_start,
        dataset_date_end=dataset_date_end,
    )
    if not truthfulness.get("latest_eligible") and write_latest:
        warnings.append(f"latest_write_blocked:{truthfulness.get('latest_write_blocked_reason')}")
    strategy_config.update({
        "top_n": top_n,
        "adjustment": adjustment,
        "sample_method": "random" if max_symbols else strategy_config.get("sample_method", "none"),
        "sample_seed": sample_seed,
    })
    payload = {
        "ok": True,
        "status": "warning" if warnings or any(r.get("warnings") for r in results) else "passed",
        "task_type": "strategy_viability_baselines",
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "package_version": PACKAGE_VERSION,
        "app_version": APP_VERSION,
        "api_schema_version": API_SCHEMA_VERSION,
        "research_only": True,
        "network": False,
        "data_source": "local_daily_csv",
        "profile": profile,
        "adjustment": adjustment,
        **price_basis_labels(adjustment, data_root=data_root),
        "pit_mode": pit_mode,
        "report_provenance": {
            "command": "strategy-viability run",
            "args": {k: str(v) for k, v in cfg.items()},
            "profile": profile,
            "pit_mode": pit_mode,
            "strategies": list(BASELINES.keys()),
            "full": True,
            "write_latest": write_latest,
            "smoke_run": smoke_run,
            "data_root_kind": truthfulness["data_root_kind"],
            "is_canonical_data_root": truthfulness["is_canonical_data_root"],
            "resolved_data_root": truthfulness["resolved_data_root"],
            "latest_eligible": truthfulness["latest_eligible"],
            "latest_write_blocked_reason": truthfulness["latest_write_blocked_reason"],
            "dataset_symbol_count": truthfulness["dataset_symbol_count"],
            "dataset_date_start": truthfulness["dataset_date_start"],
            "dataset_date_end": truthfulness["dataset_date_end"],
            "generated_at": datetime.now().isoformat(timespec="seconds"),
        },
        "initial_cash": strategy_config.get("initial_cash"),
        "max_positions": strategy_config.get("max_positions"),
        "max_position_pct": strategy_config.get("max_position_pct"),
        "rebalance": strategy_config.get("rebalance"),
        "top_n": top_n,
        "lot_size": strategy_config.get("lot_size"),
        "price_filter_enabled": strategy_config.get("price_filter_enabled"),
        "price_filter_max_price": strategy_config.get("price_filter_max_price"),
        "price_filter_evaluated_on": strategy_config.get("price_filter_evaluated_on"),
        "universe": strategy_config.get("universe"),
        "universe_mode": strategy_config.get("universe_mode"),
        "sample_method": strategy_config.get("sample_method"),
        "sample_seed": sample_seed,
        "eligible_universe_count": int(panel["symbol"].nunique()) if not panel.empty else 0,
        "average_price_filtered_universe_count": float(pd.Series([x.get("average_price_filtered_universe_count", 0) for x in price_summaries]).mean()) if price_summaries else 0.0,
        "min_price_filtered_universe_count": int(min([x.get("min_price_filtered_universe_count", 0) for x in price_summaries], default=0)),
        "max_price_filtered_universe_count": int(max([x.get("max_price_filtered_universe_count", 0) for x in price_summaries], default=0)),
        "skipped_missing_signal_price_count": int(sum(x.get("skipped_missing_signal_price_count", 0) for x in price_summaries)),
        "unfilled_due_to_lot_size_count": int(sum(x.get("unfilled_due_to_lot_size_count", 0) for x in metrics_rows)),
        "average_cash_drag": float(pd.Series([x.get("average_cash_drag", 0) for x in metrics_rows]).mean()) if metrics_rows else 0.0,
        "invested_ratio_avg": float(pd.Series([x.get("invested_ratio_avg", 0) for x in metrics_rows]).mean()) if metrics_rows else 0.0,
        "final_cash": _safe_float((best.get("metrics") or {}).get("final_cash")),
        "st_filter_enabled": True,
        "historical_st_period_available": False,
        "st_filter_quality": "degraded",
        "st_excluded_count": 0,
        "low_price_st_risk_warning": "ST low-price risk is high; current-name ST exclusion is applied when names are available, historical ST periods are unavailable.",
        "strategy_config": strategy_config,
        "benchmark": benchmark,
        "baseline_count": len(results),
        "baselines": results,
        "baseline_distinctness": distinctness,
        "baseline_distinctness_status": distinctness.get("status"),
        "execution_evidence": execution_evidence,
        "execution_evidence_status": execution_evidence.get("status"),
        "data_root_kind": truthfulness["data_root_kind"],
        "is_canonical_data_root": truthfulness["is_canonical_data_root"],
        "latest_eligible": truthfulness["latest_eligible"],
        "latest_write_blocked_reason": truthfulness["latest_write_blocked_reason"],
        "top_level_metrics_consistency": "failed" if any(check.get("status") == "failed" for check in consistency_checks) else "passed",
        "metrics_sanity_status": "failed" if any((row.get("metrics_sanity") or {}).get("status") == "failed" for row in results) else "passed",
        "best_strategy": best.get("name"),
        "decision_gate": {
            "recommendation": "refine_strategy",
            "rationale": distinctness.get("reason") or "strategy_logic_needs_repair",
            "detail": "strategy_logic_needs_repair",
        } if distinctness.get("status") == "failed" else gate,
        "precheck_summary": {
            "survivorship_bias_risk": precheck.get("survivorship_bias_risk"),
            "partial_daily_isolated": (precheck.get("partial_daily") or {}).get("isolated"),
            "pit_mode": pit_mode,
        },
        "warnings": list(dict.fromkeys(str(item) for item in warnings if item)),
        "errors": [],
        "warning_count": 0,
        "warnings_top": [],
        "output_paths": {},
        "disclaimer": DISCLAIMER,
    }
    payload["warning_count"] = len(payload["warnings"])
    payload["warnings_top"] = payload["warnings"][:10]
    return _json_safe(payload)


def render_baselines_markdown(payload: dict[str, Any]) -> str:
    lines = [
        "# Strategy Viability Baselines",
        "",
        DISCLAIMER,
        "",
        f"- status: `{payload.get('status')}`",
        f"- profile: `{payload.get('profile')}`",
        f"- best_strategy: `{payload.get('best_strategy')}`",
        f"- decision: `{payload.get('decision_gate', {}).get('recommendation')}`",
        f"- price_filter: `close <= {payload.get('price_filter_max_price')}` evaluated on `{payload.get('price_filter_evaluated_on')}`",
        "",
        "## Baseline Metrics",
        "",
        "| strategy | annual net | max drawdown | sharpe | turnover | cost impact | unfilled lot | invested avg |",
        "|:--|--:|--:|--:|--:|--:|--:|--:|",
    ]
    for row in payload.get("baselines") or []:
        m = row.get("metrics") or {}
        lines.append(
            f"| {row.get('name')} | {_safe_float(m.get('annual_return_net')):.2%} | {_safe_float(m.get('max_drawdown')):.2%} | "
            f"{_safe_float(m.get('sharpe')):.2f} | {_safe_float(m.get('turnover_annualized')):.2f} | "
            f"{_safe_float(m.get('cost_impact')):.2%} | {int(m.get('unfilled_due_to_lot_size_count') or 0)} | {_safe_float(m.get('invested_ratio_avg')):.2%} |"
        )
    lines.extend(["", "## Warnings", ""])
    warnings = payload.get("warnings") or []
    lines.extend([f"- {item}" for item in warnings] if warnings else ["- none"])
    return "\n".join(lines) + "\n"


def _run_tag(payload: dict[str, Any]) -> str:
    """Build a filesystem-safe run tag from provenance for run-specific output."""

    provenance = payload.get("report_provenance") or {}
    kind = str(provenance.get("data_root_kind") or "unknown")
    stamp = str(provenance.get("generated_at") or payload.get("generated_at") or "")
    stamp = "".join(ch if ch.isalnum() else "_" for ch in stamp).strip("_") or "run"
    return f"{kind}_{stamp}"


def write_baseline_outputs(payload: dict[str, Any], *, result_path: str | Path = RESULT_JSON, report_path: str | Path = REPORT_MD) -> dict[str, Any]:
    provenance = payload.get("report_provenance") or {}
    smoke_only = bool(provenance.get("smoke_run")) and not provenance.get("write_latest")
    # A report is only allowed to overwrite canonical *_latest when its
    # provenance says the run was latest-eligible (canonical data root, not a
    # smoke run, write_latest requested). Every other case is redirected to a
    # run-specific location so temporary / fixture / synthetic / smoke data can
    # never masquerade as formal latest research evidence. Existing latest
    # reports are left untouched (never deleted).
    latest_eligible = bool(provenance.get("latest_eligible", True))
    if smoke_only:
        result_path = PROJECT_ROOT / "results" / "smoke" / "strategy_viability_baselines_smoke_latest.json"
        report_path = PROJECT_ROOT / "reports" / "smoke" / "strategy_viability_baselines_smoke_latest.md"
    elif not latest_eligible:
        tag = _run_tag(payload)
        result_path = PROJECT_ROOT / "results" / "runs" / f"strategy_viability_baselines_{tag}.json"
        report_path = PROJECT_ROOT / "reports" / "runs" / f"strategy_viability_baselines_{tag}.md"
    result = Path(result_path)
    report = Path(report_path)
    result.parent.mkdir(parents=True, exist_ok=True)
    report.parent.mkdir(parents=True, exist_ok=True)
    payload = dict(payload)
    payload["output_paths"] = {"json": _rel(result), "markdown": _rel(report)}
    payload["latest_write_target"] = "canonical_latest" if latest_eligible and not smoke_only else ("smoke" if smoke_only else "run_specific")
    result.write_text(json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
    report.write_text(render_baselines_markdown(payload), encoding="utf-8")
    if payload.get("profile") == "low_price" and latest_eligible and not smoke_only:
        LOW_PRICE_JSON.parent.mkdir(parents=True, exist_ok=True)
        LOW_PRICE_MD.parent.mkdir(parents=True, exist_ok=True)
        LOW_PRICE_JSON.write_text(json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
        LOW_PRICE_MD.write_text(render_baselines_markdown(payload), encoding="utf-8")
    return payload
