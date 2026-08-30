from __future__ import annotations

from quantdesk.strategy_lab.research_signal_safety import boundary_payload


def build_leaderboard(summaries: list[dict]) -> tuple[dict, list[dict]]:
    rows = []
    for item in summaries:
        metrics = item.get("metrics", {})
        score = metrics.get("robustness_score", 0.0)
        rows.append(
            {
                "strategy_id": metrics.get("strategy_id", item.get("task_id", "")),
                "annualized_return": metrics.get("annualized_return", 0.0),
                "Sharpe": metrics.get("Sharpe", 0.0),
                "max_drawdown": metrics.get("max_drawdown", 0.0),
                "win_rate": metrics.get("win_rate", 0.0),
                "turnover": metrics.get("turnover", 0.0),
                "cost_stress_return": metrics.get("cost_stress_return", 0.0),
                "liquidity_risk": metrics.get("liquidity_risk", "monitored"),
                "data_risk_level": metrics.get("data_risk_level", "MODERATE_HIGH"),
                "robustness_score": score,
                "mapping_coverage": metrics.get("mapping_coverage", ""),
                "research_readiness": metrics.get("research_readiness", "research_ready_with_warnings"),
                "research_rank": 0,
                "strategy_health": "research_watch" if score < 0.65 else "research_healthy",
                "robustness_label": "definition_sensitive",
                "limitation_note": "research-only leaderboard; not a trade instruction",
            }
        )
    rows = sorted(rows, key=lambda r: r["robustness_score"], reverse=True)
    for i, row in enumerate(rows, start=1):
        row["research_rank"] = i
    return {"task_id": "v420_strategy_leaderboard", "status": "completed_with_warnings", "items": rows, **boundary_payload()}, rows
