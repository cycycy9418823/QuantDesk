from __future__ import annotations

import math
from datetime import datetime
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from quantdesk.csv_io import read_csv_frame
from quantdesk.shared import read_json as shared_read_json
from quantdesk.shared import write_json as shared_write_json
from quantdesk.shared import write_text as shared_write_text

CANDIDATE_ID = "portfolio_low_vol_rev120_candidate"
PRIMARY_COMBO = "combo_50_50"
ALLOWED_USAGE = [
    "research_dashboard",
    "paper_research",
    "simulation",
    "risk_monitoring",
    "human_review",
]
PROHIBITED_USAGE = [
    "live_trading",
    "broker_connection",
    "auto_execution",
    "buy_sell_recommendation",
    "real_money_allocation_instruction",
]
REQUIRED_PORTFOLIO_COLUMNS = {
    "portfolio_name",
    "date",
    "equity",
    "daily_return",
    "low_vol_daily_return",
    "rev120_daily_return",
    "cash_flag",
}
_JSON_READ_ERROR = object()


def project_root() -> Path:
    return Path(__file__).resolve().parents[1]


def read_json(path: Path) -> dict[str, Any]:
    loaded = shared_read_json(path, _JSON_READ_ERROR)
    if loaded is _JSON_READ_ERROR or not isinstance(loaded, dict):
        raise ValueError(f"invalid json object: {path}")
    return loaded


def write_json(path: Path, payload: dict[str, Any]) -> None:
    shared_write_json(path, to_jsonable(payload))


def write_text(path: Path, text: str) -> None:
    shared_write_text(path, text, trailing_newline=False)


def to_jsonable(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(k): to_jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [to_jsonable(v) for v in value]
    if isinstance(value, (np.integer,)):
        return int(value)
    if isinstance(value, (np.floating, float)):
        v = float(value)
        if math.isnan(v) or math.isinf(v):
            return None
        return v
    if isinstance(value, pd.Timestamp):
        return value.date().isoformat()
    return value


def pct(value: Any) -> str:
    if value is None:
        return "n/a"
    try:
        v = float(value)
    except (TypeError, ValueError):
        return "n/a"
    if math.isnan(v) or math.isinf(v):
        return "n/a"
    return f"{v * 100:.2f}%"


def fmt(value: Any, digits: int = 3) -> str:
    if value is None:
        return "n/a"
    try:
        v = float(value)
    except (TypeError, ValueError):
        return "n/a"
    if math.isnan(v) or math.isinf(v):
        return "n/a"
    return f"{v:.{digits}f}"


def metrics_from_returns(returns: pd.Series) -> dict[str, Any]:
    ret = returns.dropna().astype(float).sort_index()
    if ret.empty:
        raise ValueError("returns series is empty")
    equity = (1.0 + ret).cumprod()
    years = (ret.index[-1] - ret.index[0]).days / 365.25
    annualized = float(equity.iloc[-1] ** (1.0 / years) - 1.0) if years > 0 else None
    std = float(ret.std(ddof=0))
    sharpe = float(ret.mean() / std * math.sqrt(252)) if std > 0 else None
    downside = ret[ret < 0]
    downside_std = float(downside.std(ddof=0)) if len(downside) else 0.0
    sortino = float(ret.mean() / downside_std * math.sqrt(252)) if downside_std > 0 else None
    drawdown = equity / equity.cummax() - 1.0
    month_ret = (1.0 + ret).resample("ME").prod() - 1.0
    month_equity = (1.0 + month_ret).cumprod()
    month_drawdown = month_equity / month_equity.cummax() - 1.0
    trough = drawdown.idxmin()
    start = equity.loc[:trough].idxmax()
    recovered = equity.loc[trough:][equity.loc[trough:] >= equity.loc[start]]
    recovery = None if recovered.empty else recovered.index[0]
    yearly = {str(int(y)): float((1.0 + group).prod() - 1.0) for y, group in ret.groupby(ret.index.year)}

    return {
        "start_date": ret.index[0].date().isoformat(),
        "end_date": ret.index[-1].date().isoformat(),
        "annualized_return": annualized,
        "Sharpe": sharpe,
        "Sortino": sortino,
        "daily_MDD": float(drawdown.min()),
        "monthly_MDD": float(month_drawdown.min()) if len(month_drawdown) else None,
        "final_nav": float(equity.iloc[-1]),
        "yearly_returns": yearly,
        "positive_year_count": int(sum(v > 0 for v in yearly.values())),
        "worst_year": min(yearly.items(), key=lambda item: item[1]) if yearly else None,
        "max_drawdown": {
            "start_date": start.date().isoformat(),
            "trough_date": trough.date().isoformat(),
            "recovery_date": None if recovery is None else recovery.date().isoformat(),
            "daily_MDD": float(drawdown.loc[trough]),
        },
    }


def rolling_return(returns: pd.Series, window: int) -> float | None:
    ret = returns.dropna().astype(float)
    if len(ret) < window:
        return None
    return float((1.0 + ret.tail(window)).prod() - 1.0)


def rolling_volatility(returns: pd.Series, window: int) -> float | None:
    ret = returns.dropna().astype(float)
    if len(ret) < window:
        return None
    return float(ret.tail(window).std(ddof=0) * math.sqrt(252))


def rolling_max_drawdown(returns: pd.Series, window: int = 252) -> float | None:
    ret = returns.dropna().astype(float)
    if len(ret) < window:
        return None
    equity = (1.0 + ret.tail(window)).cumprod()
    return float((equity / equity.cummax() - 1.0).min())


def annualized_without_years(returns: pd.Series, years_to_exclude: set[int]) -> float | None:
    ret = returns[~returns.index.year.isin(years_to_exclude)].dropna().astype(float)
    if len(ret) < 2:
        return None
    equity = (1.0 + ret).cumprod()
    years = (ret.index[-1] - ret.index[0]).days / 365.25
    return float(equity.iloc[-1] ** (1.0 / years) - 1.0) if years > 0 else None


def worst_month(returns: pd.Series) -> dict[str, Any]:
    monthly = (1.0 + returns.dropna().astype(float)).resample("ME").prod() - 1.0
    if monthly.empty:
        return {}
    month = monthly.idxmin()
    return {"month": month.strftime("%Y-%m"), "return": float(monthly.loc[month])}


def load_portfolio_frame(path: Path) -> pd.DataFrame:
    frame = read_csv_frame(path, parse_dates=["date"])
    missing = REQUIRED_PORTFOLIO_COLUMNS - set(frame.columns)
    if missing:
        raise ValueError(f"portfolio schema missing columns: {sorted(missing)}")
    return frame


def _csv_warning_list(label: str, frame: pd.DataFrame) -> list[str]:
    return [f"{label}:{warning}" for warning in (frame.attrs.get("warnings") or [])]


def _base_failure(candidate_id: str, reason: str, details: list[str] | None = None) -> dict[str, Any]:
    return {
        "task_id": "candidate_monitoring_refresh",
        "status": "data_issue",
        "candidate_id": candidate_id,
        "monitoring_status": "data_issue",
        "warnings": [],
        "human_review_required_reasons": [reason],
        "failure_reasons": [reason] + (details or []),
        "research_only": True,
        "prohibited_usage": PROHIBITED_USAGE,
    }


def refresh_candidate_monitoring(
    *,
    candidate_id: str = CANDIDATE_ID,
    root: Path | None = None,
    registry_path: Path | None = None,
    portfolio_csv: Path | None = None,
    low_vol_csv: Path | None = None,
    rev120_csv: Path | None = None,
) -> dict[str, Any]:
    base = (root or project_root()).resolve()
    registry_path = registry_path or base / "results" / "research_candidate_registry_latest.json"
    portfolio_csv = portfolio_csv or base / "results" / "portfolio_daily_equity_rebuild_latest.csv"
    low_vol_csv = low_vol_csv or base / "results" / "low_vol_canonical_daily_equity_latest.csv"
    rev120_csv = rev120_csv or base / "results" / "rev120_bcanonical_daily_equity_latest.csv"
    required = [registry_path, portfolio_csv, low_vol_csv, rev120_csv]
    missing = [str(path) for path in required if not path.exists()]
    if missing:
        return _base_failure(candidate_id, "missing required input files", missing)

    try:
        registry = read_json(registry_path)
        frame = load_portfolio_frame(portfolio_csv)
        low_frame = read_csv_frame(low_vol_csv)
        rev_frame = read_csv_frame(rev120_csv)
    except Exception as exc:  # graceful fail for CLI/tests
        return _base_failure(candidate_id, f"input load failed: {exc}")
    input_warnings = (
        _csv_warning_list("portfolio_csv", frame)
        + _csv_warning_list("low_vol_csv", low_frame)
        + _csv_warning_list("rev120_csv", rev_frame)
    )

    combo = frame[frame["portfolio_name"] == PRIMARY_COMBO].set_index("date").sort_index()
    if combo.empty:
        return _base_failure(candidate_id, f"portfolio {PRIMARY_COMBO} not found")

    numeric_cols = ["equity", "daily_return", "low_vol_daily_return", "rev120_daily_return"]
    nonfinite = int((~np.isfinite(combo[numeric_cols].to_numpy(dtype=float))).sum())
    if nonfinite:
        payload = _base_failure(candidate_id, "unexpected NaN / infinite values", [f"nonfinite_count={nonfinite}"])
        payload["status"] = "completed"
        payload["monitoring_status"] = "human_review_required"
        payload["warnings"] = []
        return payload
    ret = combo["daily_return"].astype(float)
    low = combo["low_vol_daily_return"].astype(float)
    rev = combo["rev120_daily_return"].astype(float)

    try:
        metrics = metrics_from_returns(ret)
    except Exception as exc:
        return _base_failure(candidate_id, f"portfolio calculation fails: {exc}")

    equity = (1.0 + ret).cumprod()
    final_nav_csv = float(combo["equity"].iloc[-1])
    final_nav_calc = float(equity.iloc[-1])
    final_nav_diff = abs(final_nav_csv - final_nav_calc)
    cash_mask = combo["cash_flag"].astype(bool)
    cash_anomaly_count = int((cash_mask & (ret.abs() > 1e-12)).sum())
    last_date = combo.index.max()
    freshness_days = (pd.Timestamp(datetime.now().date()) - last_date.normalize()).days

    low_half = 0.5 * low
    rev_half = 0.5 * rev
    low_contribution = float((1.0 + low_half).prod() - 1.0)
    rev_contribution = float((1.0 + rev_half).prod() - 1.0)
    total_abs = abs(low_contribution) + abs(rev_contribution)
    rev_share = abs(rev_contribution) / total_abs if total_abs else None
    low_share = abs(low_contribution) / total_abs if total_abs else None

    dd_start = pd.Timestamp(metrics["max_drawdown"]["start_date"])
    dd_trough = pd.Timestamp(metrics["max_drawdown"]["trough_date"])
    low_dd = float((1.0 + low.loc[dd_start:dd_trough]).prod() - 1.0)
    rev_dd = float((1.0 + rev.loc[dd_start:dd_trough]).prod() - 1.0)

    cost_stress = registry.get("candidate_pool", [{}])[0].get("current_metrics", {}).get("cost_stress_proxy", {})
    if not cost_stress:
        # Fallback from previous dashboard card if registry is minimal.
        cost_stress = {"cost_plus_50bps_per_rebalance": {"annualized_return": None}}
    cost50 = cost_stress.get("cost_plus_50bps_per_rebalance", {})

    component_corr_full = float(low.corr(rev))
    component_corr_252 = float(low.tail(252).corr(rev.tail(252))) if len(low) >= 252 else None
    worst_20 = ret.nsmallest(20)

    warnings: list[str] = list(input_warnings)
    human_review: list[str] = []
    if metrics["daily_MDD"] < -0.30:
        warnings.append("daily MDD worse than -30%")
    r252 = rolling_return(ret, 252)
    if r252 is not None and r252 < 0:
        warnings.append("rolling 252d return < 0")
    if (metrics["Sharpe"] or 0) < 0.5:
        warnings.append("Sharpe below 0.5")
    if component_corr_252 is not None and component_corr_252 > 0.9:
        warnings.append("component correlation above 0.9 over long window")
    if rev_share is not None and rev_share > 0.80:
        warnings.append("rev120 leg dominates more than 80% of total return")
    if low_dd < -0.15:
        warnings.append("low_vol leg loses defensive function during drawdown")
    if cost50.get("annualized_return") is not None and cost50["annualized_return"] < 0.08:
        warnings.append("cost stress annualized return below 8%")
    if freshness_days > 45:
        warnings.append("data freshness issue")
    if cash_anomaly_count:
        warnings.append("cash-day return not zero")

    if metrics["daily_MDD"] < -0.35:
        human_review.append("daily MDD worse than -35%")
    if final_nav_diff > 1e-8:
        human_review.append("final NAV inconsistent with baseline")
    if nonfinite:
        human_review.append("unexpected NaN / infinite values")
    if set(low_frame.columns) == set() or set(rev_frame.columns) == set():
        human_review.append("canonical daily equity missing")

    if human_review:
        monitoring_status = "human_review_required"
    elif warnings:
        monitoring_status = "candidate_watch"
    else:
        monitoring_status = "candidate_healthy"

    return {
        "task_id": "candidate_monitoring_refresh",
        "status": "completed",
        "candidate_id": candidate_id,
        "monitoring_status": monitoring_status,
        "research_only": True,
        "prohibited_usage": PROHIBITED_USAGE,
        "allowed_usage": ALLOWED_USAGE,
        "as_of_date": last_date.date().isoformat(),
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "metrics": {
            **metrics,
            "rolling_60d_return": rolling_return(ret, 60),
            "rolling_120d_return": rolling_return(ret, 120),
            "rolling_252d_return": r252,
            "rolling_60d_volatility": rolling_volatility(ret, 60),
            "rolling_120d_volatility": rolling_volatility(ret, 120),
            "rolling_252d_max_drawdown": rolling_max_drawdown(ret, 252),
            "annualized_without_2024_2025_reference": annualized_without_years(ret, {2024, 2025}),
            "low_vol_leg_contribution": low_contribution,
            "rev120_leg_contribution": rev_contribution,
            "low_vol_contribution_share_abs": low_share,
            "rev120_contribution_share_abs": rev_share,
            "component_correlation": component_corr_full,
            "component_correlation_252d": component_corr_252,
            "active_day_ratio": float((~cash_mask).mean()),
            "cash_day_ratio": float(cash_mask.mean()),
            "cash_day_return_anomaly_count": cash_anomaly_count,
            "worst_20_days": [{"date": d.date().isoformat(), "daily_return": float(v)} for d, v in worst_20.items()],
            "worst_month": worst_month(ret),
            "cost_stress_proxy": cost_stress,
            "data_freshness_days": freshness_days,
            "canonical_input_health_status": {
                "low_vol_csv_present": low_vol_csv.exists(),
                "rev120_csv_present": rev120_csv.exists(),
                "low_vol_rows": int(len(low_frame)),
                "rev120_rows": int(len(rev_frame)),
            },
            "final_nav_csv": final_nav_csv,
            "final_nav_recalculated": final_nav_calc,
            "final_nav_diff": final_nav_diff,
            "low_vol_return_during_max_drawdown": low_dd,
            "rev120_return_during_max_drawdown": rev_dd,
            "main_risk_source": "rev120" if abs(rev_dd) > abs(low_dd) else "low_vol",
        },
        "warnings": warnings,
        "human_review_required_reasons": human_review,
        "failure_reasons": [],
        "no_trading_guarantee": True,
        "no_buy_sell_or_rebalance_instruction": True,
    }


def render_monitoring_markdown(payload: dict[str, Any]) -> str:
    if payload.get("status") != "completed" or "metrics" not in payload:
        return "\n".join(
            [
                "# Portfolio Candidate Monitoring Refresh",
                "",
                f"Status: `{payload.get('status')}`",
                f"Monitoring status: `{payload.get('monitoring_status')}`",
                f"Failure reasons: `{payload.get('failure_reasons', [])}`",
                "",
                "Research-only. No trading, broker connection, auto execution, or buy/sell advice.",
            ]
        ) + "\n"
    metrics = payload["metrics"]
    return "\n".join(
        [
            "# Portfolio Candidate Monitoring Refresh",
            "",
            f"Status: `{payload['status']}`",
            f"Monitoring status: `{payload['monitoring_status']}`",
            f"As-of date: `{payload['as_of_date']}`",
            "",
            "| metric | value |",
            "|---|---:|",
            f"| annualized_return | {pct(metrics['annualized_return'])} |",
            f"| Sharpe | {fmt(metrics['Sharpe'])} |",
            f"| Sortino | {fmt(metrics['Sortino'])} |",
            f"| daily MDD | {pct(metrics['daily_MDD'])} |",
            f"| monthly MDD | {pct(metrics['monthly_MDD'])} |",
            f"| rolling 60d return | {pct(metrics['rolling_60d_return'])} |",
            f"| rolling 120d return | {pct(metrics['rolling_120d_return'])} |",
            f"| rolling 252d return | {pct(metrics['rolling_252d_return'])} |",
            f"| rolling 60d volatility | {pct(metrics['rolling_60d_volatility'])} |",
            f"| rolling 120d volatility | {pct(metrics['rolling_120d_volatility'])} |",
            f"| rolling 252d MDD | {pct(metrics['rolling_252d_max_drawdown'])} |",
            f"| component correlation | {fmt(metrics['component_correlation'])} |",
            f"| main risk source | {metrics['main_risk_source']} |",
            "",
            f"Warnings: `{payload['warnings']}`",
            f"Human review reasons: `{payload['human_review_required_reasons']}`",
            "",
            "Research-only. No trading, broker connection, auto execution, allocation instruction, or buy/sell advice.",
        ]
    ) + "\n"
