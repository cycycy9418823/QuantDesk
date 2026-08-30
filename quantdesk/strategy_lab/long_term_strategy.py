from __future__ import annotations

from quantdesk.strategy_lab.research_signal_safety import RESEARCH_ONLY_NOTE, boundary_payload
from quantdesk.strategy_lab.rolling_strategy_backtest import backtest_summary


def generate_long_term(metrics: list[dict]) -> tuple[dict, list[dict]]:
    pool = [m for m in metrics if m["return_120d"] > 0 and m["volatility"] < 0.45 and m["avg_volume_20d"] > 0]
    ranked = sorted(pool, key=lambda x: (x["return_120d"] - x["volatility"]), reverse=True)[:30]
    rows = []
    for m in ranked:
        rows.append(
            {
                "symbol": m["symbol"],
                "name": "",
                "strategy_id": "long_term_low_vol" if m["volatility"] < 0.3 else "long_term_trend",
                "signal_date": m["signal_date"],
                "trigger_reason": "long_horizon_trend_or_low_vol_research_filter",
                "risk_flags": "formal_metadata_gap_warning",
                "research_score": m["return_120d"] - m["volatility"],
                "recent_return": m["return_120d"],
                "volatility": m["volatility"],
                "research_signal_label": "long_term_research_candidate",
                "prohibited_usage_note": RESEARCH_ONLY_NOTE,
            }
        )
    summary = {"task_id": "v420_long_term_strategy_backtest", "status": "completed_with_warnings", "metrics": backtest_summary("long_term_family", ranked), "fundamental_data_limitation": "quality_value_proxy unavailable unless fundamental fields are supplied", **boundary_payload()}
    return summary, rows

