from __future__ import annotations

from quantdesk.strategy_lab.research_signal_safety import RESEARCH_ONLY_NOTE, boundary_payload
from quantdesk.strategy_lab.rolling_strategy_backtest import backtest_summary


def generate_low_price(metrics: list[dict]) -> tuple[dict, list[dict]]:
    pool = [m for m in metrics if 0 < m["close"] <= 8 and m["avg_volume_20d"] > 0]
    ranked = sorted(pool, key=lambda x: (x["return_20d"], -x["volatility"]), reverse=True)[:30]
    rows = []
    for m in ranked:
        sid = "low_price_reversal" if m["return_20d"] < 0 else ("low_price_volume_breakout" if m["return_5d"] > 0.03 else "low_price_trend_repair")
        rows.append(
            {
                "symbol": m["symbol"],
                "name": "",
                "strategy_id": sid,
                "signal_date": m["signal_date"],
                "trigger_reason": "low_price_research_filter_with_recent_return_and_liquidity_proxy",
                "risk_flags": "metadata_proxy_warning",
                "liquidity_score": m["amount_proxy"],
                "recent_return": m["return_20d"],
                "volatility": m["volatility"],
                "research_signal_label": "research_candidate_watch",
                "prohibited_usage_note": RESEARCH_ONLY_NOTE,
            }
        )
    summary = {"task_id": "v420_low_price_strategy_backtest", "status": "completed_with_warnings", "metrics": backtest_summary("low_price_family", ranked), "affected_by_ST_proxy": True, "affected_by_delist": True, "liquidity_risk_count": sum(1 for m in ranked if m["amount_proxy"] <= 0), "limit_up_untradable_count": sum(1 for m in ranked if m["limit_up_proxy"]), **boundary_payload()}
    return summary, rows

