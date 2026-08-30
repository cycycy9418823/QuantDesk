from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from quantdesk.shared import safe_float_zero as safe_float

PAPER_REVIEW_DISCLAIMER = "虚拟复盘只统计本地模拟记录，不代表真实收益，也不构成投资建议。"


def _parse_time(value: Any) -> datetime | None:
    text = str(value or "")
    for fmt, size in (("%Y-%m-%d %H:%M:%S", 19), ("%Y-%m-%d", 10)):
        try:
            return datetime.strptime(text[:size], fmt)
        except ValueError:
            continue
    return None


def _summary(portfolio: dict[str, Any], trades: list[dict[str, Any]]) -> dict[str, Any]:
    positions = portfolio.get("positions") or []
    entry_count = sum(str(t.get("action_type") or "").endswith("entry") or t.get("side") == "BUY" for t in trades)
    exit_count = sum(str(t.get("action_type") or "").endswith("exit") or t.get("side") == "SELL" for t in trades)
    strategy_counts: dict[str, int] = {}
    missing_counts: dict[str, int] = {}
    risk_counts: dict[str, int] = {}
    for trade in trades:
        strategy = str(trade.get("strategy_name") or "未标记策略")
        strategy_counts[strategy] = strategy_counts.get(strategy, 0) + 1
        for field in trade.get("missing_fields") or []:
            missing_counts[str(field)] = missing_counts.get(str(field), 0) + 1
        for note in trade.get("risk_notes") or []:
            risk_counts[str(note)] = risk_counts.get(str(note), 0) + 1
    summary = portfolio.get("summary") or {}
    return {
        "virtual_entry_count": entry_count,
        "virtual_exit_count": exit_count,
        "virtual_position_count": len(positions),
        "virtual_estimated_pnl": safe_float(summary.get("total_pnl")),
        "virtual_settled_pnl": sum(safe_float(p.get("unrealized_pnl")) for p in positions if not p.get("estimate_only")),
        "top_strategy": max(strategy_counts, key=strategy_counts.get) if strategy_counts else "",
        "common_missing_fields": sorted(missing_counts, key=missing_counts.get, reverse=True)[:5],
        "common_risk_review_reasons": sorted(risk_counts, key=risk_counts.get, reverse=True)[:5],
        "disclaimer": PAPER_REVIEW_DISCLAIMER,
    }


def daily_paper_review(portfolio: dict[str, Any], now: datetime | None = None) -> dict[str, Any]:
    today = (now or datetime.now()).date()
    trades = [
        trade
        for trade in portfolio.get("trades", []) or []
        if (_parse_time(trade.get("trade_time") or trade.get("date")) or datetime.min).date() == today
    ]
    return {"period": "daily", "date": today.isoformat(), **_summary(portfolio, trades)}


def weekly_paper_review(portfolio: dict[str, Any], now: datetime | None = None) -> dict[str, Any]:
    current = now or datetime.now()
    start = current.date() - timedelta(days=current.weekday())
    trades = [
        trade
        for trade in portfolio.get("trades", []) or []
        if (_parse_time(trade.get("trade_time") or trade.get("date")) or datetime.min).date() >= start
    ]
    return {"period": "weekly", "week_start": start.isoformat(), **_summary(portfolio, trades)}
