from __future__ import annotations

from collections import Counter, defaultdict
from statistics import mean, median
from typing import Any


def strategy_statistics(stock_rows: list[dict[str, Any]]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    grouped = defaultdict(list)
    for row in stock_rows:
        grouped[row.get("strategy_family", "unknown")].append(row)
    rows = []
    for family, items in grouped.items():
        scores = [float(x.get("research_score") or 0) for x in items]
        data_scores = [float(x.get("data_quality_score") or 0) for x in items]
        risk_text = " ".join(x.get("risk_flags", "") for x in items)
        sectors = Counter(x.get("sector_name", "") or "Unknown" for x in items)
        rows.append(
            {
                "strategy_family": family,
                "signal_count": len(items),
                "candidate_count": len({x.get("symbol") for x in items}),
                "average_research_score": mean(scores) if scores else 0.0,
                "median_research_score": median(scores) if scores else 0.0,
                "average_data_quality_score": mean(data_scores) if data_scores else 0.0,
                "high_risk_count": sum(1 for x in items if "risk" in x.get("risk_flags", "").lower()),
                "ST_proxy_exposure": "ST" in risk_text,
                "delist_exposure": "delist" in risk_text,
                "suspension_proxy_exposure": "suspension" in risk_text,
                "abnormal_adj_price_exposure": "adjust" in risk_text,
                "sector_concentration": sectors.most_common(1)[0][0] if sectors else "",
                "recent_backtest_health": "research_watch",
                "limitation_note": "research-only statistics; metadata blockers remain",
            }
        )
    return {"task_id": "v421_strategy_statistics", "status": "completed", "items": rows}, rows

