from __future__ import annotations

from typing import Any

from quantdesk.strategy_lab.research_signal_safety import RESEARCH_ONLY_NOTE, boundary_payload
from quantdesk.strategy_lab.sector_strategy import load_sector_theme_map, sector_name


def _normalize_symbol(symbol: Any) -> str:
    digits = "".join(ch for ch in str(symbol or "") if ch.isdigit())
    return digits[-6:] if len(digits) >= 6 else digits


def _theme_context(symbol: Any, theme_map: dict[str, dict[str, str]]) -> dict[str, Any]:
    normalized = _normalize_symbol(symbol)
    mapped = theme_map.get(normalized) or {}
    if mapped.get("sector_name"):
        return {
            "sector_name": mapped.get("sector_name", ""),
            "sector_mapping_source": "stock_theme_map",
            "industry": mapped.get("industry", ""),
            "sector": mapped.get("sector", ""),
            "primary_theme": mapped.get("primary_theme", ""),
            "concept_tags": mapped.get("concept_tags", ""),
            "theme_tags": mapped.get("theme_tags", ""),
            "sector_mapping_note": "formal theme/industry map matched",
        }
    return {
        "sector_name": sector_name(normalized),
        "sector_mapping_source": "symbol_prefix_proxy",
        "industry": "",
        "sector": "",
        "primary_theme": "",
        "concept_tags": "",
        "theme_tags": "",
        "sector_mapping_note": "formal theme map missing; using visible exchange-board proxy",
    }


def _stock_row_with_theme(
    row: dict[str, Any],
    base: dict[str, Any],
    theme_map: dict[str, dict[str, str]],
) -> dict[str, Any]:
    return {**base, **_theme_context(row.get("symbol"), theme_map)}


def _mapping_summary(stock_rows: list[dict[str, Any]]) -> dict[str, Any]:
    total = len(stock_rows)
    mapped = sum(1 for row in stock_rows if row.get("sector_mapping_source") == "stock_theme_map")
    return {
        "total_stock_rows": total,
        "mapped_stock_rows": mapped,
        "mapping_coverage": mapped / total if total else 0.0,
        "unmapped_stock_rows": total - mapped,
        "status": "complete" if total and mapped == total else "partial" if mapped else "missing",
    }


def combine_daily_signals(
    freshness: dict,
    leaderboard_rows: list[dict],
    low_rows: list[dict],
    long_rows: list[dict],
    limit_rows: list[dict],
    sector_rows: list[dict],
) -> tuple[dict, list[dict], list[dict], list[dict]]:
    theme_map = load_sector_theme_map()
    stock_rows: list[dict[str, Any]] = []
    for row in low_rows[:10]:
        stock_rows.append(
            _stock_row_with_theme(
                row,
                {
                    "symbol": row["symbol"],
                    "name": row.get("name", ""),
                    "signal_date": row["signal_date"],
                    "strategy_family": "low_price_family",
                    "strategy_id": row["strategy_id"],
                    "research_score": row.get("recent_return", 0),
                    "data_quality_score": 0.6,
                    "risk_flags": row["risk_flags"],
                    "trigger_reason": row["trigger_reason"],
                    "historical_evidence": "low_price_proxy_backtest",
                    "limitation_note": "metadata blockers remain",
                    "research_signal_label": row["research_signal_label"],
                    "prohibited_usage_note": RESEARCH_ONLY_NOTE,
                },
                theme_map,
            )
        )
    for row in long_rows[:10]:
        stock_rows.append(
            _stock_row_with_theme(
                row,
                {
                    "symbol": row["symbol"],
                    "name": row.get("name", ""),
                    "signal_date": row["signal_date"],
                    "strategy_family": "long_term_family",
                    "strategy_id": row["strategy_id"],
                    "research_score": row.get("research_score", 0),
                    "data_quality_score": 0.6,
                    "risk_flags": row["risk_flags"],
                    "trigger_reason": row["trigger_reason"],
                    "historical_evidence": "long_term_proxy_backtest",
                    "limitation_note": "metadata blockers remain",
                    "research_signal_label": row["research_signal_label"],
                    "prohibited_usage_note": RESEARCH_ONLY_NOTE,
                },
                theme_map,
            )
        )
    for row in limit_rows[:10]:
        stock_rows.append(
            _stock_row_with_theme(
                row,
                {
                    "symbol": row["symbol"],
                    "name": row.get("name", ""),
                    "signal_date": row["signal_date"],
                    "strategy_family": "limit_up_family",
                    "strategy_id": row["pattern_detected"],
                    "research_score": 0.5,
                    "data_quality_score": 0.5,
                    "risk_flags": row["risk_flags"],
                    "trigger_reason": row["pattern_detected"],
                    "historical_evidence": row["historical_distribution_note"],
                    "limitation_note": "daily-bar proxy only",
                    "research_signal_label": row["research_signal_label"],
                    "prohibited_usage_note": RESEARCH_ONLY_NOTE,
                },
                theme_map,
            )
        )
    sector_candidate_rows = [
        {
            "sector_name": r["sector_name"],
            "sector_score": r["sector_score"],
            "sector_rank": r["sector_rank"],
            "stock_symbol": r["stock_symbol"],
            "research_reason": r["research_reason"],
            "risk_flags": r["risk_flags"],
            "prohibited_usage_note": RESEARCH_ONLY_NOTE,
        }
        for r in sector_rows[:30]
    ]
    strategy_rows = [
        {k: r[k] for k in ["research_rank", "strategy_id", "strategy_health", "robustness_label", "limitation_note"]}
        for r in leaderboard_rows
    ]
    payload = {
        "task_id": "v420_daily_research_signals",
        "status": "completed_with_warnings",
        "data_status": freshness,
        "strategy_summary_count": len(strategy_rows),
        "stock_candidate_count": len(stock_rows),
        "sector_candidate_count": len(sector_candidate_rows),
        "top_strategy_family": strategy_rows[0]["strategy_id"] if strategy_rows else "",
        "stock_sector_mapping": _mapping_summary(stock_rows),
        "items_sample": stock_rows[:20],
        **boundary_payload(),
    }
    return payload, stock_rows, sector_candidate_rows, strategy_rows
