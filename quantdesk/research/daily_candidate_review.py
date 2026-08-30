from __future__ import annotations

from typing import Any

from quantdesk.platform_common import (
    base_payload,
    clean_number,
    finalize_outputs,
    simple_markdown,
    synthetic_candidates,
)


def build_daily_candidate_review_report(config: dict | None = None) -> dict:
    cfg = dict(config or {})
    candidates = synthetic_candidates() if cfg.get("synthetic_test_data") else []
    warnings = [] if candidates else ["candidate_review_inputs_missing"]
    rows: list[dict[str, Any]] = []
    for idx, row in enumerate(candidates):
        one = round(0.01 * (idx + 1), 4)
        rows.append({
            "symbol": row["symbol"],
            "name": row.get("name", ""),
            "sector": row.get("sector", ""),
            "strategy": row.get("strategy", "observe"),
            "1d_return": one,
            "3d_return": round(one * 1.8, 4),
            "5d_return": round(one * 2.4, 4),
            "max_drawdown": round(-one / 2, 4),
            "research_only": True,
        })
    payload = base_payload("daily_candidate_review", status="warning" if warnings else "passed", warnings=warnings)
    avg = sum(clean_number(row["1d_return"]) for row in rows) / len(rows) if rows else 0
    payload.update({
        "review_count": len(rows),
        "items": rows,
        "hit_rate_positive": sum(1 for row in rows if row["1d_return"] > 0) / len(rows) if rows else 0,
        "average_return": avg,
        "by_strategy": {row["strategy"]: row["1d_return"] for row in rows},
        "by_sector": {row["sector"]: row["1d_return"] for row in rows},
        "confidence_bucket_performance": {"synthetic": avg},
    })
    return finalize_outputs(payload, "daily_candidate_review", render_daily_candidate_review_markdown(payload))


def render_daily_candidate_review_markdown(payload: dict) -> str:
    return simple_markdown("Daily Candidate Review", payload, "items")
