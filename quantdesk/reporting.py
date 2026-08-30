from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from quantdesk.backtest import BacktestMetrics
from quantdesk.price_basis import describe_price_basis


def fmt_pct(value: object) -> str:
    if isinstance(value, str):
        return value
    if value is None or pd.isna(value):
        return "N/A"
    return f"{float(value) * 100:.2f}%"


def fmt_value(value: object, digits: int = 2) -> str:
    if value is None or pd.isna(value):
        return "-"
    if isinstance(value, (int, np.integer)):
        return str(value)
    if isinstance(value, (float, np.floating)):
        return f"{float(value):.{digits}f}"
    return str(value)


def audit_file_name(files: dict[str, Path] | None, key: str) -> str:
    path = (files or {}).get(key)
    return path.name if path else "not generated"


def _markdown_cell(value: object) -> str:
    if value is None:
        text = ""
    else:
        try:
            missing = bool(pd.isna(value))
        except (TypeError, ValueError):
            missing = False
        text = "" if missing else str(value)
    return text.replace("|", "\\|").replace("\n", "<br>")


def _simple_markdown_table(frame: pd.DataFrame, columns: list[str]) -> str:
    selected = frame[columns]
    header = "| " + " | ".join(columns) + " |"
    divider = "| " + " | ".join(["---"] * len(columns)) + " |"
    rows = [
        "| " + " | ".join(_markdown_cell(value) for value in row) + " |"
        for row in selected.itertuples(index=False, name=None)
    ]
    return "\n".join([header, divider] + rows)


def table_or_message(frame: pd.DataFrame, columns: list[str], empty_message: str) -> str:
    if frame.empty:
        return empty_message
    try:
        return frame[columns].to_markdown(index=False)
    except ImportError:
        return _simple_markdown_table(frame, columns)


def risk_summary_dict(source: Any) -> dict[str, Any]:
    if source is None:
        return {}
    if isinstance(source, dict):
        return dict(source)
    frame = pd.DataFrame(source)
    if frame.empty or not {"metric", "value"}.issubset(frame.columns):
        return {}
    return {str(row["metric"]): row["value"] for _, row in frame.iterrows()}


def risk_value(summary: dict[str, Any], key: str, default: Any = 0) -> Any:
    value = summary.get(key, default)
    if value is None or (isinstance(value, float) and np.isnan(value)):
        return default
    return value


def risk_value_or_unknown(summary: dict[str, Any] | None, key: str, default: Any = "not_measured") -> Any:
    """Return measured risk values while preserving unknown/missing state.

    A real measured ``0`` is meaningful and must remain ``0``. Missing keys,
    ``None`` and ``NaN`` mean the risk pass was not measured; rendering them as
    zero would falsely say the gate ran cleanly.
    """
    if not isinstance(summary, dict) or key not in summary:
        return default
    value = summary.get(key)
    if value is None:
        return default
    if isinstance(value, (float, np.floating)) and np.isnan(value):
        return default
    return value


def build_data_quality_markdown(summary: dict[str, int], stale_warning_days: int, health_file_name: str) -> str:
    return f"""## Data Quality

- Daily data checks: {summary["total"]}
- Fresh fetch success: {summary["fresh_fetch_success"]}
- Cache present: {summary["cache_present"]}
- Cache merged: {summary["cache_merged"]}
- Served from cache after fetch failure: {summary["served_from_cache"]}
- Fallback used: {summary["fallback_used"]}
- Stale over {stale_warning_days} trading-aware calendar days: {summary["stale"]}
- Empty / no-data histories: {summary["no_data"]}
- ERROR rows: {summary["error"]}
- WARNING rows: {summary["warning"]}
- Data health file: {health_file_name}
"""


def build_backtest_risk_summary_markdown(risk_summary: dict[str, Any] | None) -> str:
    summary = risk_summary if isinstance(risk_summary, dict) else {}
    blocked_limit_up = risk_value_or_unknown(summary, "blocked_limit_up")
    blocked_limit_down = risk_value_or_unknown(summary, "blocked_limit_down")
    blocked_suspended = risk_value_or_unknown(summary, "blocked_suspended")
    blocked_cash = risk_value_or_unknown(summary, "blocked_cash_insufficient")
    blocked_turnover = risk_value_or_unknown(summary, "blocked_turnover_limit")
    industry_warning = risk_value_or_unknown(summary, "industry_concentration_warning")
    if industry_warning in ("", None):
        industry_warning = "none"
    max_industry_weight = risk_value_or_unknown(summary, "max_industry_weight")
    max_rebalance_turnover = risk_value_or_unknown(summary, "max_single_rebalance_turnover")
    turnover_triggered = risk_value_or_unknown(summary, "turnover_limit_triggered")
    return f"""### Backtest Risk Summary

- Data error exclusions: {risk_value_or_unknown(summary, "excluded_by_data_error")}
- Data WARNING names included: {risk_value_or_unknown(summary, "included_with_data_warning")}
- Fallback data names included: {risk_value_or_unknown(summary, "included_with_fallback")}
- Blocked trades total: {risk_value_or_unknown(summary, "blocked_trades_total")}
- Blocked reasons: limit up {blocked_limit_up}, limit down {blocked_limit_down}, suspended {blocked_suspended}, cash insufficient {blocked_cash}, turnover limit {blocked_turnover}
- Max industry weight: {fmt_pct(max_industry_weight)}
- Industry concentration warning: {industry_warning}
- Max single rebalance turnover: {fmt_pct(max_rebalance_turnover)}
- Turnover limit triggered: {turnover_triggered}
- Still not modeled: 盘口排队、部分成交、真实撮合深度、精确停复牌公告、真实交易日历。
"""


def risk_summary_note_lines(risk_summary: dict[str, Any] | None) -> list[str]:
    summary = risk_summary if isinstance(risk_summary, dict) else {}
    industry_warning = risk_value_or_unknown(summary, "industry_concentration_warning")
    if industry_warning in ("", None):
        industry_warning = "none"
    return [
        f"Data error exclusions: {risk_value_or_unknown(summary, 'excluded_by_data_error')}",
        f"Data WARNING names included: {risk_value_or_unknown(summary, 'included_with_data_warning')}",
        f"Fallback data names included: {risk_value_or_unknown(summary, 'included_with_fallback')}",
        f"Blocked trades total: {risk_value_or_unknown(summary, 'blocked_trades_total')}",
        (
            "Blocked reasons: "
            f"limit up {risk_value_or_unknown(summary, 'blocked_limit_up')}, "
            f"limit down {risk_value_or_unknown(summary, 'blocked_limit_down')}, "
            f"suspended {risk_value_or_unknown(summary, 'blocked_suspended')}, "
            f"cash insufficient {risk_value_or_unknown(summary, 'blocked_cash_insufficient')}, "
            f"turnover limit {risk_value_or_unknown(summary, 'blocked_turnover_limit')}"
        ),
        f"Max industry weight: {fmt_pct(risk_value_or_unknown(summary, 'max_industry_weight'))}",
        f"Industry concentration warning: {industry_warning}",
        f"Max single rebalance turnover: {fmt_pct(risk_value_or_unknown(summary, 'max_single_rebalance_turnover'))}",
        f"Turnover limit triggered: {risk_value_or_unknown(summary, 'turnover_limit_triggered')}",
        "Still not modeled: 盘口排队、部分成交、真实撮合深度、精确停复牌公告、真实交易日历。",
    ]


def build_backtest_audit_markdown(
    metrics: BacktestMetrics,
    assumptions: dict[str, Any],
    backtest_files: dict[str, Path] | None,
    *,
    backtest_warning: str,
    backtest_mode: str = "portfolio",
    config_path: str = "config/default.yaml",
    risk_summary: dict[str, Any] | None = None,
) -> str:
    risk_summary = risk_summary or assumptions.get("backtest_risk_summary", {})
    risk_summary = risk_summary_dict(risk_summary)
    assumption_metadata = assumptions.get("assumption_metadata") if isinstance(assumptions.get("assumption_metadata"), dict) else {}
    pit_enabled = bool(assumption_metadata.get("pit_universe") or assumptions.get("pit_universe_used"))
    survivorship_banner = (
        ""
        if pit_enabled
        else (
            "\n> ⚠️ **survivorship_bias=high** — 本回测未启用 point-in-time 宇宙重建"
            "（pit_universe=false），股票池按当前存续名单构建，可能高估历史可得性与收益。\n"
        )
    )
    adjudication = assumptions.get("score_dimension_adjudication")
    adjudication_banner = ""
    if isinstance(adjudication, dict) and adjudication.get("verdicts"):
        verdict_text = "、".join(
            f"{dim}={verdict}" for dim, verdict in adjudication["verdicts"].items()
        )
        adjudication_banner = (
            "\n> ⚠️ **score_dimension_adjudication (Round 22)** — 预注册裁决："
            f"{verdict_text}。生产权重未改（用户裁定只披露），"
            "评分排序不得当作已验证信号使用。\n"
        )
    return f"""## Backtest Audit
{survivorship_banner}{adjudication_banner}
### Simple Strategy Check

### Strategy

- Rule tested: ma_breakout, requiring close > MA20 and activity_value > activity_avg_20 * 1.5.
- Purpose: research the rule's behavior under basic A-share trading constraints, not predict profit.

{build_backtest_risk_summary_markdown(risk_summary)}
### Backtest Period

- Mode: {assumptions.get("portfolio_mode", "independent average")}
- Backtest mode: {assumptions.get("backtest_mode", backtest_mode)}
- Config path: {assumptions.get("config_path", config_path)}
- Period: {assumptions.get("backtest_period", f"{metrics.days} trading rows")}
- Data adjustment: {assumptions["data_adjustment"]}
- 价格口径 (price_basis): {describe_price_basis(assumptions.get("price_basis", "raw"))} ({assumptions.get("price_basis", "raw")})
- 复权口径提示 (adjustment_warnings): {"；".join(assumptions.get("adjustment_warnings") or []) or "无"}
- Universe rule: {assumptions["universe_rule"]}
- Portfolio rule: {assumptions.get("portfolio_rule", "single asset")}
- Independent average warning: {"当前多标的结果为独立单股回测平均，不代表真实资金组合。" if assumptions.get("not_a_capital_constrained_portfolio_backtest") else "not applicable"}
- Benchmark: {assumptions["benchmark"]}
- excluded_by_data_error: {assumptions.get("excluded_by_data_error_count", 0)} {assumptions.get("excluded_by_data_error", [])}
- included_with_data_warning: {assumptions.get("included_with_data_warning_count", 0)} {assumptions.get("included_with_data_warning", [])}
- included_with_fallback: {assumptions.get("included_with_fallback_count", 0)} {assumptions.get("included_with_fallback", [])}

### Portfolio Mode

- Portfolio mode: {assumptions.get("portfolio_mode", "not used")}
- Initial cash: {fmt_value(assumptions.get("initial_cash"))}
- Max positions: {assumptions.get("max_positions", "not applicable")}
- Max position pct: {fmt_pct(assumptions.get("max_position_pct"))}
- Max turnover per rebalance: {assumptions.get("max_turnover_per_rebalance", "not limited")}
- Turnover limit implemented: {assumptions.get("turnover_limit_implemented", False)}
- Max industry pct: {assumptions.get("max_industry_pct", "not limited")}
- Industry check enabled: {assumptions.get("industry_check_enabled", False)}
- Industry warning: {assumptions.get("industry_warning", "") or "none"}
- Rebalance rule: {assumptions.get("rebalance_rule", "not implemented")}
- Ranking rule: {assumptions.get("ranking_rule", "not applicable")}
- Ranking fields: {assumptions.get("ranking_fields", "not applicable")}
- Ranking proxy: {assumptions.get("ranking_is_proxy", "not applicable")}

### Signal Timing

- Signal date: {assumptions["signal_date"]}
- Signal timing: {assumptions.get("signal_timing", "close")}

### Execution Timing

- Execution date: {assumptions["execution_date"]}
- Execution timing: {assumptions.get("execution_timing", "T+1 next trading row")}

### Execution Price

- Execution price: {assumptions["execution_price"]}
- Price fallback: {assumptions.get("execution_price_fallback", "not implemented")}

### Costs

- costs_implemented: {assumptions.get("costs_implemented", False)}
- commission_rate: {assumptions["commission_rate"]}
- min_commission: {assumptions.get("min_commission", "not implemented")}
- stamp_duty_rate: {assumptions["stamp_duty_rate"]} (sell only)
- slippage_bps: {assumptions["slippage_bps"]}
- lot_size: {assumptions.get("lot_size", "not implemented")}

### Limit-up / Limit-down Rule

- Limit-up: {assumptions.get("limit_up_rule", "not implemented")}
- Limit-down: {assumptions.get("limit_down_rule", "not implemented")}
- Change source: {assumptions.get("limit_change_source", "not implemented")}

### Suspension Rule

- Suspension: {assumptions.get("suspension_rule", "not implemented")}

### Metrics

- Days: {metrics.days}
- Number of executed trades: {metrics.number_of_trades}
- Final equity: {fmt_value(metrics.final_equity)}
- Turnover: {fmt_pct(metrics.turnover)}
- Exposure: {fmt_pct(metrics.assumptions.get("exposure", np.nan) if isinstance(metrics.assumptions, dict) else np.nan)}
- Average positions: {fmt_value(metrics.assumptions.get("average_positions", np.nan) if isinstance(metrics.assumptions, dict) else np.nan)}
- Max positions used: {metrics.assumptions.get("max_positions_used", "N/A") if isinstance(metrics.assumptions, dict) else "N/A"}
- Total return: {fmt_pct(metrics.total_return)}
- Annual return: {fmt_pct(metrics.annual_return)}
- Max drawdown: {fmt_pct(metrics.max_drawdown)}
- Sharpe: {fmt_value(metrics.sharpe)}
- Win-day ratio: {fmt_pct(metrics.win_rate)}
- Benchmark return: {fmt_pct(metrics.benchmark_return)}
- Excess return: {fmt_pct(metrics.excess_return)}

### Audit Files

- Trades file: {audit_file_name(backtest_files, "trades")}
- Equity curve file: {audit_file_name(backtest_files, "equity_curve")}
- Blocked trades file: {audit_file_name(backtest_files, "blocked_trades")}
- Positions file: {audit_file_name(backtest_files, "positions")}
- Rebalance log file: {audit_file_name(backtest_files, "rebalance_log")}
- Summary file: {audit_file_name(backtest_files, "summary")}
- Risk summary file: {audit_file_name(backtest_files, "risk_summary")}
- Assumptions file: {audit_file_name(backtest_files, "assumptions")}

### Warning

{backtest_warning}

- 历史回测不代表未来收益；组合回测仍未模拟盘口排队、部分成交和真实撮合深度。
- Not implemented: {assumptions.get("not_implemented", "not implemented")}
"""
