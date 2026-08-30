"""Read-only daily research routes for the QuantDesk 8000 server."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from flask import Blueprint, Flask, jsonify, request

from scripts.server.helpers.util import clean_payload, finite_float


@dataclass(frozen=True)
class DailyRouteDeps:
    candidate_rows: Callable[[int], list[dict[str, Any]]]
    annotate_recommendation_priorities: Callable[[list[dict[str, Any]]], list[dict[str, Any]]]
    daily_focus_rows: Callable[[list[dict[str, Any]]], list[dict[str, Any]]]
    candidate_groups: Callable[[list[dict[str, Any]]], list[dict[str, Any]]]
    quant_daily_summary: Callable[[list[dict[str, Any]]], dict[str, Any]]
    generation_status_for_ui: Callable[[], dict[str, Any]]
    focus_score: Callable[[dict[str, Any]], float]
    candidate_pool_review_policy: Callable[[list[dict[str, Any]]], dict[str, Any]]
    data_gap_summary: Callable[[list[dict[str, Any]]], dict[str, Any]]
    candidate_confidence_summary: Callable[[list[dict[str, Any]]], dict[str, Any]]
    load_benchmark_index: Callable[[str], dict[str, Any]]
    spot_market_breadth: Callable[[], dict[str, Any]]
    generate_market_sentiment: Callable[[float | None, float | None, int, int], Any]
    calculate_sector_stats: Callable[[], dict[str, Any]]
    market_overview_hint: str


def create_daily_blueprint(deps: DailyRouteDeps) -> Blueprint:
    """Create read-only daily candidate and summary routes."""

    bp = Blueprint("daily_api", __name__)

    @bp.route("/api/daily/overview", methods=["GET"])
    def get_daily_overview() -> Any:
        hs300 = deps.load_benchmark_index("hs300")
        sh_index = deps.load_benchmark_index("sh_index")
        breadth = deps.spot_market_breadth()
        hs_change = finite_float(hs300.get("change"))
        sh_change = finite_float(sh_index.get("change"))
        sentiment = deps.generate_market_sentiment(
            hs_change,
            sh_change,
            int(breadth.get("advancing_count", 0)),
            int(breadth.get("declining_count", 0)),
        )
        sector_stats = deps.calculate_sector_stats()
        payload = {
            "hs300": hs300,
            "sh_index": sh_index,
            "market_breadth": breadth,
            "sector_top_gainers": sector_stats.get("gainers") or [],
            "sector_top_losers": sector_stats.get("losers") or [],
            "market_sentiment": sentiment,
            "update_hint": deps.market_overview_hint,
            "research_only": "仅用于股票研究学习；不接券商、不下真实订单。",
        }
        return jsonify(clean_payload(payload))

    @bp.route("/api/daily/candidates", methods=["GET"])
    def get_daily_candidates() -> Any:
        rows = deps.annotate_recommendation_priorities(deps.candidate_rows(18))
        focus = deps.daily_focus_rows(rows)
        groups = deps.candidate_groups(rows)
        quant_summary = deps.quant_daily_summary(rows)
        generation = deps.generation_status_for_ui()
        today_rows = sorted(
            [row for row in rows if row.get("is_today_recommendation")],
            key=deps.focus_score,
            reverse=True,
        )
        observation_rows = [row for row in rows if not row.get("is_today_recommendation")]
        generation_notice = ""
        if generation.get("freshness_status") == "stale":
            generation_notice = f" {generation.get('freshness_message')}"
        message = (
            f"今日新候选 {len(today_rows)} 只；历史观察 {len(observation_rows)} 只。"
            "今日新候选来自本地完整日线研究生成，历史候选保留用于后续验证。"
            f"{generation_notice}"
        )
        if not rows:
            message = "本地候选股票数据缺失，请点击“生成今日研究候选”，或先导入 candidate_ledger.csv。"
        return jsonify(
            clean_payload(
                {
                    "candidate_generation": generation,
                    "today_recommendations": today_rows,
                    "observation_candidates": observation_rows,
                    "daily_focus": focus,
                    "candidates": rows[:10],
                    "all_candidates": rows,
                    "candidate_groups": groups,
                    "candidate_pool_policy": deps.candidate_pool_review_policy(rows),
                    "data_gap_summary": deps.data_gap_summary(rows),
                    "candidate_confidence_summary": deps.candidate_confidence_summary(rows),
                    "quant_daily_summary": quant_summary,
                    "message": message,
                    "research_only": "Research Only；仅用于学习复盘，不构成投资建议。",
                }
            )
        )

    @bp.route("/api/benchmark/series", methods=["GET"])
    def get_benchmark_series() -> Any:
        """Read-only local benchmark index series for dashboard comparison lines."""
        from scripts.server.helpers.market import load_benchmark_series

        kind = str(request.args.get("index") or "hs300").strip().lower()
        try:
            days = int(str(request.args.get("days") or "120"))
        except ValueError:
            days = 120
        days = max(2, min(500, days))
        payload = load_benchmark_series(kind, days=days)
        payload.setdefault("research_only", "仅用于研究对比展示；不构成投资建议。")
        return jsonify(clean_payload(payload)), 200 if payload.get("ok", True) else 404

    @bp.route("/api/daily/quant-summary", methods=["GET"])
    def get_daily_quant_summary() -> Any:
        rows = deps.candidate_rows(18)
        return jsonify(clean_payload(deps.quant_daily_summary(rows)))

    return bp


def register_daily_routes(app: Flask, deps: DailyRouteDeps) -> None:
    app.register_blueprint(create_daily_blueprint(deps))
