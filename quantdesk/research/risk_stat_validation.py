from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from quantdesk.csv_io import read_csv_frame
from quantdesk.research.backtest_overfitting import deflated_sharpe_ratio, minimum_track_record_length
from quantdesk.research.market_impact import estimate_impact_bps
from quantdesk.risk.var import historical_var
from quantdesk.shared import now_text

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_PORTFOLIO_CSV = PROJECT_ROOT / "results" / "portfolio_daily_equity_rebuild_latest.csv"
DEFAULT_OUTPUT_JSON = PROJECT_ROOT / "results" / "risk_stat_validation_latest.json"
DEFAULT_REPORT_MD = PROJECT_ROOT / "reports" / "risk_stat_validation_latest.md"
PROHIBITED_USAGE = [
    "live_trading",
    "broker_connection",
    "auto_execution",
    "buy_sell_recommendation",
    "real_money_allocation_instruction",
]


def _project_path(path: str | Path) -> Path:
    target = Path(path)
    return target if target.is_absolute() else PROJECT_ROOT / target


def _finite_float(value: Any, default: float = 0.0) -> float:
    try:
        number = float(value)
    except Exception:
        return default
    return number if math.isfinite(number) else default


def _load_equity_csv(path: str | Path, portfolio_name: str = "combo_50_50") -> tuple[pd.DataFrame, list[str]]:
    warnings: list[str] = []
    target = _project_path(path)
    if not target.exists():
        return pd.DataFrame(), [f"missing_input:{target}"]
    df = read_csv_frame(target)
    warnings.extend(str(item) for item in df.attrs.get("warnings", []) if item)
    if "portfolio_name" in df.columns:
        filtered = df[df["portfolio_name"].astype(str).eq(portfolio_name)].copy()
        if filtered.empty:
            warnings.append(f"portfolio_name_missing:{portfolio_name}")
        else:
            df = filtered
    if "date" not in df.columns:
        warnings.append("missing_date_column")
    if "daily_return" not in df.columns and "equity" in df.columns:
        equity = pd.to_numeric(df["equity"], errors="coerce")
        df["daily_return"] = equity.ffill().pct_change(fill_method=None).fillna(0.0)
        warnings.append("daily_return_derived_from_equity")
    required = {"date", "equity", "daily_return"}
    missing = sorted(required - set(df.columns))
    if missing:
        warnings.append(f"missing_required_columns:{','.join(missing)}")
        return pd.DataFrame(), warnings
    df["date"] = pd.to_datetime(df["date"], errors="coerce")
    df["equity"] = pd.to_numeric(df["equity"], errors="coerce")
    df["daily_return"] = pd.to_numeric(df["daily_return"], errors="coerce")
    bad_numeric = int(df[["equity", "daily_return"]].replace([np.inf, -np.inf], np.nan).isna().sum().sum())
    if bad_numeric:
        warnings.append(f"bad_numeric_values:{bad_numeric}")
    df = df.replace([np.inf, -np.inf], np.nan).dropna(subset=["date", "equity", "daily_return"]).sort_values("date")
    return df.reset_index(drop=True), warnings


def _max_drawdown(equity: pd.Series) -> dict[str, Any]:
    clean = pd.to_numeric(equity, errors="coerce").dropna()
    if clean.empty:
        return {"max_drawdown": 0.0, "start": "", "trough": "", "recovery": ""}
    peak = clean.cummax()
    drawdown = clean / peak - 1.0
    trough_idx = drawdown.idxmin()
    peak_idx = clean.loc[:trough_idx].idxmax()
    recovery = ""
    post = clean.loc[trough_idx:]
    recovered = post[post >= clean.loc[peak_idx]]
    if not recovered.empty:
        recovery = str(recovered.index[0])
    return {
        "max_drawdown": round(float(drawdown.loc[trough_idx]), 6),
        "start_index": int(peak_idx),
        "trough_index": int(trough_idx),
        "recovery_index": recovery,
    }


def _annualized_metrics(returns: pd.Series, equity: pd.Series) -> dict[str, float]:
    clean = pd.to_numeric(returns, errors="coerce").dropna()
    if clean.empty:
        return {"annualized_return": 0.0, "sharpe": 0.0, "sortino": 0.0, "volatility": 0.0}
    final_nav = _finite_float(equity.dropna().iloc[-1], 1.0) if not equity.dropna().empty else 1.0
    annualized = final_nav ** (252 / max(len(clean), 1)) - 1 if final_nav > 0 else -1.0
    vol = float(clean.std(ddof=0) * np.sqrt(252))
    sharpe = float(clean.mean() / clean.std(ddof=0) * np.sqrt(252)) if clean.std(ddof=0) > 0 else 0.0
    downside = clean[clean < 0].std(ddof=0)
    sortino = float(clean.mean() / downside * np.sqrt(252)) if downside > 0 else 0.0
    return {
        "annualized_return": round(float(annualized), 6),
        "sharpe": round(sharpe, 6),
        "sortino": round(sortino, 6),
        "volatility": round(vol, 6),
    }


def _component_stats(df: pd.DataFrame) -> dict[str, Any]:
    result: dict[str, Any] = {}
    if {"low_vol_daily_return", "rev120_daily_return"}.issubset(df.columns):
        low = pd.to_numeric(df["low_vol_daily_return"], errors="coerce").fillna(0.0)
        rev = pd.to_numeric(df["rev120_daily_return"], errors="coerce").fillna(0.0)
        result["component_correlation"] = round(float(low.corr(rev)), 6) if low.std(ddof=0) > 0 and rev.std(ddof=0) > 0 else 0.0
        low_contribution = float(low.sum())
        rev_contribution = float(rev.sum())
        total = low_contribution + rev_contribution
        result["leg_contribution"] = {
            "low_vol_sum_return": round(low_contribution, 6),
            "rev120_sum_return": round(rev_contribution, 6),
            "rev120_share_abs": round(abs(rev_contribution) / (abs(low_contribution) + abs(rev_contribution)), 6)
            if (abs(low_contribution) + abs(rev_contribution)) > 0
            else 0.0,
            "total_sum_return": round(total, 6),
        }
    return result


def _impact_proxy(df: pd.DataFrame, order_notional: float, daily_amount: float, volatility: float) -> dict[str, Any]:
    if daily_amount <= 0:
        return {
            "status": "warning",
            "impact_bps": None,
            "warning": "daily_amount_missing_for_impact_proxy",
        }
    return {
        "status": "passed",
        "impact_bps": estimate_impact_bps(order_notional, daily_amount, daily_volatility=volatility),
        "order_notional": float(order_notional),
        "daily_amount": float(daily_amount),
    }


def build_risk_stat_validation(
    equity_csv: str | Path = DEFAULT_PORTFOLIO_CSV,
    *,
    portfolio_name: str = "combo_50_50",
    order_notional: float = 100_000.0,
    daily_amount: float = 0.0,
) -> dict[str, Any]:
    df, warnings = _load_equity_csv(equity_csv, portfolio_name=portfolio_name)
    if df.empty:
        status = "failed"
        returns = pd.Series(dtype=float)
        equity = pd.Series(dtype=float)
    else:
        status = "warning" if warnings else "passed"
        returns = pd.to_numeric(df["daily_return"], errors="coerce").fillna(0.0)
        equity = pd.to_numeric(df["equity"], errors="coerce").ffill()
    metrics = _annualized_metrics(returns, equity)
    dd = _max_drawdown(equity.reset_index(drop=True))
    var_95 = historical_var(returns, confidence=0.95, lookback=min(500, len(returns))) if len(returns) else {"error": "no_returns"}
    sharpe_candidates = [metrics["sharpe"]]
    component = _component_stats(df)
    if component:
        low = pd.to_numeric(df["low_vol_daily_return"], errors="coerce").fillna(0.0)
        rev = pd.to_numeric(df["rev120_daily_return"], errors="coerce").fillna(0.0)
        for series in (low, rev):
            std = series.std(ddof=0)
            sharpe_candidates.append(float(series.mean() / std * np.sqrt(252)) if std > 0 else 0.0)
    validation = {
        "deflated_sharpe_ratio": deflated_sharpe_ratio(sharpe_candidates, T=max(len(returns), 2)),
        "minimum_track_record_length_years": minimum_track_record_length(metrics["sharpe"]),
    }
    impact = _impact_proxy(df, order_notional, daily_amount, metrics["volatility"])
    if impact.get("status") == "warning":
        warnings.append(str(impact.get("warning")))
    return {
        "task_id": "risk_stat_validation",
        "status": status,
        "research_only": True,
        "candidate_id": "portfolio_low_vol_rev120_candidate" if portfolio_name == "combo_50_50" else "",
        "portfolio_name": portfolio_name,
        "source_files": [str(_project_path(equity_csv))],
        "row_count": int(len(df)),
        "date_start": str(df["date"].min().date()) if not df.empty else "",
        "date_end": str(df["date"].max().date()) if not df.empty else "",
        "metrics": {**metrics, **dd},
        "historical_var": var_95,
        "statistical_validation": validation,
        "component_stats": component,
        "market_impact_proxy": impact,
        "warnings": list(dict.fromkeys(warnings)),
        "allowed_usage": ["research_dashboard", "paper_research", "simulation", "risk_monitoring", "human_review"],
        "prohibited_usage": PROHIBITED_USAGE,
        "generated_at": now_text(),
        "disclaimer": "Research-only diagnostics. No broker connection, no live orders, no buy/sell recommendation.",
    }


def render_risk_stat_markdown(payload: dict[str, Any]) -> str:
    metrics = payload.get("metrics") or {}
    var = payload.get("historical_var") or {}
    stat = payload.get("statistical_validation") or {}
    impact = payload.get("market_impact_proxy") or {}
    lines = [
        "# Risk / Statistical Validation",
        "",
        "Research-only diagnostics. This is not a trading signal and not investment advice.",
        "",
        f"- status: `{payload.get('status')}`",
        f"- portfolio_name: `{payload.get('portfolio_name')}`",
        f"- date_range: `{payload.get('date_start')}` to `{payload.get('date_end')}`",
        f"- annualized_return: `{metrics.get('annualized_return')}`",
        f"- sharpe: `{metrics.get('sharpe')}`",
        f"- sortino: `{metrics.get('sortino')}`",
        f"- max_drawdown: `{metrics.get('max_drawdown')}`",
        f"- var_95: `{var.get('var_95')}`",
        f"- cvar_95: `{var.get('cvar_95')}`",
        f"- deflated_sharpe_ratio: `{stat.get('deflated_sharpe_ratio')}`",
        f"- minimum_track_record_length_years: `{stat.get('minimum_track_record_length_years')}`",
        f"- market_impact_proxy_bps: `{impact.get('impact_bps')}`",
        "",
        "## Prohibited Usage",
        *(f"- {item}" for item in payload.get("prohibited_usage", [])),
        "",
        "No live trading, no broker connection, no auto execution, no buy/sell recommendation.",
    ]
    if payload.get("warnings"):
        lines.extend(["", "## Warnings", *(f"- {item}" for item in payload.get("warnings", []))])
    return "\n".join(lines) + "\n"


def write_risk_stat_validation(
    payload: dict[str, Any],
    output_json: str | Path = DEFAULT_OUTPUT_JSON,
    report_md: str | Path = DEFAULT_REPORT_MD,
) -> dict[str, str]:
    json_path = _project_path(output_json)
    md_path = _project_path(report_md)
    json_path.parent.mkdir(parents=True, exist_ok=True)
    md_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    md_path.write_text(render_risk_stat_markdown(payload), encoding="utf-8")
    return {"json_path": str(json_path), "report_path": str(md_path)}
