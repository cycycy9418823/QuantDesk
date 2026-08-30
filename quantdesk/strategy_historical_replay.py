from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Mapping

import pandas as pd

from quantdesk.research_metrics import research_metrics_to_dict, summarize_research_metrics
from quantdesk.strategy_demo import check_strategy_for_stocks
from quantdesk.strategy_features import build_strategy_feature_row

STRATEGY_REPLAY_DISCLAIMER = "历史回放仅用于本地研究学习，不代表未来收益，也不构成投资建议。"
RULE_REPLAY_MODE = "rule_replay"
RULE_REPLAY_MODE_LABEL = "规则层历史回放"
RULE_REPLAY_NOTE = "当前按历史日期检查规则满足情况，不等同于完整交易路径回测；交易路径回放已由独立 trade_replay 模块提供。"
STRATEGY_TRADE_REPLAY_STATUS = "available"
STRATEGY_TRADE_REPLAY_STATUS_LABEL = "交易路径回放已独立提供"


@dataclass
class StrategyHistoricalReplay:
    replay_mode: str
    replay_mode_label: str
    replay_note: str
    strategy_trade_replay_status: str
    strategy_trade_replay_status_label: str
    replay_start_date: str
    replay_end_date: str
    strategy_name: str
    universe_name: str
    sample_days: int
    daily_candidates: list[dict[str, Any]]
    forward_return_5d: float | None
    forward_return_10d: float | None
    forward_return_20d: float | None
    benchmark_return_5d: float | None
    benchmark_return_20d: float | None
    excess_return_5d: float | None
    excess_return_20d: float | None
    max_drawdown: float | None
    annual_breakdown: list[dict[str, Any]]
    monthly_breakdown: list[dict[str, Any]]
    survivorship_bias_warning: bool
    universe_note: str
    disclaimer: str


def _rule_replay_fields() -> dict[str, Any]:
    return {
        "replay_mode": RULE_REPLAY_MODE,
        "replay_mode_label": RULE_REPLAY_MODE_LABEL,
        "replay_note": RULE_REPLAY_NOTE,
        "strategy_trade_replay_status": STRATEGY_TRADE_REPLAY_STATUS,
        "strategy_trade_replay_status_label": STRATEGY_TRADE_REPLAY_STATUS_LABEL,
    }


def _as_frame(history: Any) -> pd.DataFrame:
    if history is None:
        return pd.DataFrame()
    if isinstance(history, pd.DataFrame):
        return history.copy()
    if isinstance(history, pd.Series):
        return history.to_frame().T
    if isinstance(history, Mapping):
        return pd.DataFrame([dict(history)])
    if isinstance(history, list):
        rows = [row for row in history if isinstance(row, Mapping)]
        return pd.DataFrame(rows) if rows else pd.DataFrame()
    return pd.DataFrame()


def _date_column(frame: pd.DataFrame) -> str | None:
    for column in ("date", "trade_date", "daily_bar_date", "report_date"):
        if column in frame.columns:
            return column
    return None


def _normalize_rows(rows: list[dict[str, Any]], data_mode: str = "report") -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    for row in rows:
        feature_row = build_strategy_feature_row(dict(row), data_mode=data_mode)
        feature_row.setdefault("symbol", str(row.get("symbol") or row.get("code") or "").zfill(6))
        feature_row.setdefault("name", str(row.get("name") or ""))
        normalized.append(feature_row)
    return normalized


def _compact_detail(detail: dict[str, Any]) -> dict[str, Any]:
    return {
        "symbol": detail.get("symbol", ""),
        "name": detail.get("name", ""),
        "status": detail.get("status", ""),
        "status_label": detail.get("status_label", ""),
        "matched_count": len(detail.get("matched") or []),
        "failed_count": len(detail.get("failed") or []),
        "missing_count": len(detail.get("missing") or []),
        "blocked_by_trust": bool(detail.get("blocked_by_trust")),
        "blocked_by_timing": bool(detail.get("blocked_by_timing")),
        "short_explanation": detail.get("explanation", ""),
        "risk_notes": list(detail.get("key_risks") or []),
        "detail": detail,
    }


def replay_current_candidates(
    candidates: Any,
    strategy_name: str,
    *,
    trust_map: Mapping[str, Any] | None = None,
    timing_advice: Mapping[str, Any] | None = None,
    benchmark_series: Any | None = None,
    data_mode: str = "report",
) -> dict[str, Any]:
    frame = _as_frame(candidates)
    rows = _normalize_rows(frame.to_dict("records"), data_mode=data_mode)
    if not rows:
        return {
            **_rule_replay_fields(),
            "strategy_name": strategy_name,
            "universe_name": "当前候选池",
            "sample_days": 0,
            "daily_candidates": [],
            "forward_return_5d": None,
            "forward_return_10d": None,
            "forward_return_20d": None,
            "benchmark_return_5d": None,
            "benchmark_return_20d": None,
            "excess_return_5d": None,
            "excess_return_20d": None,
            "max_drawdown": None,
            "annual_breakdown": [],
            "monthly_breakdown": [],
            "survivorship_bias_warning": True,
            "universe_note": "当前仅基于本地候选样本做规则回放，不代表完整历史股票池。",
            "disclaimer": STRATEGY_REPLAY_DISCLAIMER,
        }
    strategy_result = check_strategy_for_stocks(rows, strategy_name, trust_map, timing_advice)
    daily_candidates = []
    for row in strategy_result.get("rows", []) or []:
        daily_candidates.append(
            {
                "symbol": row.get("symbol", ""),
                "name": row.get("name", ""),
                "status": row.get("status", ""),
                "status_label": row.get("status_label", ""),
                "matched_count": len(row.get("matched") or []),
                "failed_count": len(row.get("failed") or []),
                "missing_count": len(row.get("missing") or []),
                "missing_fields": list(row.get("missing_fields") or []),
                "missing_labels": list(row.get("missing_labels") or []),
                "missing_field_diagnostics": [
                    {
                        "field": field,
                        "label": label or field,
                        "impact": "部分规则检查无法完成",
                        "fix": "检查本地日线字段并重新生成报告",
                    }
                    for field, label in zip(list(row.get("missing_fields") or []), list(row.get("missing_labels") or []))
                ],
                "short_explanation": row.get("short_explanation", ""),
                "plan_action": row.get("plan_action", ""),
                "plan_action_label": row.get("plan_action_label", ""),
                "risk_notes": list(row.get("risk_notes") or []),
            }
        )
    summary_metrics = summarize_research_metrics(rows)
    metrics = research_metrics_to_dict(summary_metrics)
    benchmark_return = None
    if benchmark_series is not None:
        benchmark_frame = _as_frame(benchmark_series)
        if not benchmark_frame.empty and "close" in benchmark_frame.columns:
            benchmark_return = float(pd.to_numeric(benchmark_frame["close"], errors="coerce").dropna().iloc[-1] / pd.to_numeric(benchmark_frame["close"], errors="coerce").dropna().iloc[0] - 1)
    return {
        **_rule_replay_fields(),
        "strategy_name": strategy_name,
        "universe_name": "当前候选池",
        "sample_days": 1,
        "daily_candidates": daily_candidates,
        "forward_return_5d": metrics.get("forward_return_5d_mean"),
        "forward_return_10d": None,
        "forward_return_20d": metrics.get("forward_return_20d_mean"),
        "benchmark_return_5d": benchmark_return,
        "benchmark_return_20d": benchmark_return,
        "excess_return_5d": None if benchmark_return is None else (metrics.get("forward_return_5d_mean") or 0) - benchmark_return,
        "excess_return_20d": None if benchmark_return is None else (metrics.get("forward_return_20d_mean") or 0) - benchmark_return,
        "max_drawdown": metrics.get("max_drawdown"),
        "annual_breakdown": metrics.get("annual_return_table", []),
        "monthly_breakdown": metrics.get("monthly_return_table", []),
        "survivorship_bias_warning": True,
        "universe_note": "当前仅基于本地候选样本做规则回放，不代表完整历史股票池。",
        "disclaimer": STRATEGY_REPLAY_DISCLAIMER,
    }


def strategy_historical_replay(
    history: Any,
    strategy_name: str,
    *,
    universe_name: str = "local_universe",
    benchmark_series: Any | None = None,
    sample_days: int | None = None,
) -> dict[str, Any]:
    frame = _as_frame(history)
    if frame.empty:
        return {
            **_rule_replay_fields(),
            "replay_start_date": "",
            "replay_end_date": "",
            "strategy_name": strategy_name,
            "universe_name": universe_name,
            "sample_days": 0,
            "daily_candidates": [],
            "forward_return_5d": None,
            "forward_return_10d": None,
            "forward_return_20d": None,
            "benchmark_return_5d": None,
            "benchmark_return_20d": None,
            "excess_return_5d": None,
            "excess_return_20d": None,
            "max_drawdown": None,
            "annual_breakdown": [],
            "monthly_breakdown": [],
            "survivorship_bias_warning": True,
            "universe_note": "缺少历史样本，只能做空结果演示。",
            "disclaimer": STRATEGY_REPLAY_DISCLAIMER,
        }

    date_column = _date_column(frame)
    if date_column is None:
        replay = replay_current_candidates(frame.to_dict("records"), strategy_name, benchmark_series=benchmark_series, data_mode=str(frame.get("data_mode", "report").iloc[0] if "data_mode" in frame.columns else "report"))
        replay.update(
            {
                **_rule_replay_fields(),
                "replay_start_date": "",
                "replay_end_date": "",
                "strategy_name": strategy_name,
                "universe_name": universe_name,
                "sample_days": 1,
                "survivorship_bias_warning": True,
                "universe_note": "历史样本缺少日期字段，只能做单次规则回放。",
            }
        )
        return replay

    temp = frame.copy()
    temp[date_column] = pd.to_datetime(temp[date_column], errors="coerce")
    temp = temp.dropna(subset=[date_column])
    temp = temp.sort_values(date_column)
    grouped = list(temp.groupby(temp[date_column].dt.strftime("%Y-%m-%d")))
    if sample_days is not None:
        grouped = grouped[: sample_days]

    daily_candidates: list[dict[str, Any]] = []
    selected_rows: list[dict[str, Any]] = []
    for date_text, group in grouped:
        rows = _normalize_rows(group.to_dict("records"), data_mode=str(group.get("data_mode", pd.Series(["report"])).iloc[0] if "data_mode" in group.columns else "report"))
        strategy_result = check_strategy_for_stocks(rows, strategy_name)
        selected = [row for row in strategy_result.get("rows", []) or [] if row.get("status") in {"all_matched", "partial"}]
        for row in selected:
            copied = dict(row)
            copied["date"] = date_text
            selected_rows.append(copied)
        daily_candidates.append(
            {
                "date": date_text,
                "candidate_count": len(rows),
                "selected_count": len(selected),
                "blocked_count": sum(1 for row in strategy_result.get("rows", []) or [] if row.get("status") == "blocked"),
                "missing_count": sum(1 for row in strategy_result.get("rows", []) or [] if row.get("status") == "missing_required"),
                "selected_symbols": [row.get("symbol", "") for row in selected[:8]],
                "summary": strategy_result.get("summary", {}),
            }
        )

    research_metrics = summarize_research_metrics(selected_rows or temp.to_dict("records"))
    metrics = research_metrics_to_dict(research_metrics)
    benchmark_return = None
    if benchmark_series is not None:
        benchmark_frame = _as_frame(benchmark_series)
        if not benchmark_frame.empty and "close" in benchmark_frame.columns:
            series = pd.to_numeric(benchmark_frame["close"], errors="coerce").dropna()
            if not series.empty:
                benchmark_return = float(series.iloc[-1] / series.iloc[0] - 1)
    start_date = str(temp[date_column].min().date()) if not temp.empty else ""
    end_date = str(temp[date_column].max().date()) if not temp.empty else ""
    return {
        **_rule_replay_fields(),
        "replay_start_date": start_date,
        "replay_end_date": end_date,
        "strategy_name": strategy_name,
        "universe_name": universe_name,
        "sample_days": len(grouped),
        "daily_candidates": daily_candidates,
        "forward_return_5d": metrics.get("forward_return_5d_mean"),
        "forward_return_10d": None,
        "forward_return_20d": metrics.get("forward_return_20d_mean"),
        "benchmark_return_5d": benchmark_return,
        "benchmark_return_20d": benchmark_return,
        "excess_return_5d": None if benchmark_return is None else (metrics.get("forward_return_5d_mean") or 0) - benchmark_return,
        "excess_return_20d": None if benchmark_return is None else (metrics.get("forward_return_20d_mean") or 0) - benchmark_return,
        "max_drawdown": metrics.get("max_drawdown"),
        "annual_breakdown": metrics.get("annual_return_table", []),
        "monthly_breakdown": metrics.get("monthly_return_table", []),
        "survivorship_bias_warning": True,
        "universe_note": "当前仅使用本地可见历史样本回放，不代表完整历史股票池，也可能存在幸存者偏差。",
        "disclaimer": STRATEGY_REPLAY_DISCLAIMER,
    }


def strategy_historical_replay_to_dict(result: StrategyHistoricalReplay) -> dict[str, Any]:
    return asdict(result)


__all__ = [
    "STRATEGY_REPLAY_DISCLAIMER",
    "StrategyHistoricalReplay",
    "replay_current_candidates",
    "strategy_historical_replay",
    "strategy_historical_replay_to_dict",
]
