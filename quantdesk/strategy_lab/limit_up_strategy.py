from __future__ import annotations

from quantdesk.strategy_lab.research_signal_safety import RESEARCH_ONLY_NOTE, boundary_payload
from quantdesk.strategy_lab.rolling_strategy_backtest import backtest_summary


def generate_limit_up(metrics: list[dict]) -> tuple[dict, list[dict], list[dict]]:
    pool = [m for m in metrics if m["return_5d"] > 0.08 or m["limit_up_proxy"]]
    ranked = sorted(pool, key=lambda x: x["return_5d"], reverse=True)[:30]
    rows = []
    dist = []
    for m in ranked:
        pattern = "first_limit_up_research" if m["limit_up_proxy"] else "next_day_limit_up_risk"
        rows.append(
            {
                "symbol": m["symbol"],
                "name": "",
                "signal_date": m["signal_date"],
                "research_signal_label": "limit_up_pattern_research_candidate",
                "pattern_detected": pattern,
                "risk_flags": "T_plus_1;limit_untradable_proxy;slippage;liquidity_filter",
                "historical_distribution_note": "daily_bar_proxy_only_intraday_unavailable",
                "prohibited_usage_note": RESEARCH_ONLY_NOTE,
            }
        )
        dist.append({"symbol": m["symbol"], "signal_date": m["signal_date"], "next_day_distribution_proxy": "research_distribution_pending_intraday", "recent_return_5d": m["return_5d"]})
    summary = {"task_id": "v420_limit_up_research_backtest", "status": "completed_with_warnings", "metrics": backtest_summary("limit_up_family", ranked), "a_share_trading_constraints": ["T+1", "limit_untradable_proxy", "slippage", "cost", "liquidity_filter"], **boundary_payload()}
    return summary, rows, dist

