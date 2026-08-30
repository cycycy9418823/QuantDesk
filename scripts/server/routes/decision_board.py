"""Read-only Decision Board route for the active QuantDesk port-8000 UI."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Sequence

from flask import Blueprint, Flask, jsonify

from quantdesk.decision_board import build_decision_board_snapshot
from quantdesk.data_chain_consumer_gate import (
    evaluate_data_chain_consumer_gate,
)
from quantdesk.trader_profile import ResearchPaperProfile
from scripts.server.helpers.util import clean_payload


@dataclass(frozen=True)
class DecisionBoardRouteDeps:
    load_watchlist: Callable[[], dict[str, Any]]
    load_portfolio: Callable[[], dict[str, Any]]
    load_equity_history: Callable[[], dict[str, Any]]
    load_benchmark: Callable[[], dict[str, Any]]
    load_profile: Callable[[], ResearchPaperProfile]
    load_calendar_days: Callable[[], Sequence[str]]
    strategy_version: Callable[[], str]
    source_paths: Callable[[], dict[str, str]]


def create_decision_board_blueprint(deps: DecisionBoardRouteDeps) -> Blueprint:
    bp = Blueprint("decision_board_api", __name__)

    @bp.route("/api/decision-board", methods=["GET"])
    def get_decision_board() -> Any:
        try:
            payload = build_decision_board_snapshot(
                watchlist_payload=deps.load_watchlist(),
                portfolio_payload=deps.load_portfolio(),
                equity_history_payload=deps.load_equity_history(),
                benchmark_payload=deps.load_benchmark(),
                profile=deps.load_profile(),
                calendar_days=deps.load_calendar_days(),
                strategy_version=deps.strategy_version(),
                source_paths=deps.source_paths(),
            )
            consumer_gate = evaluate_data_chain_consumer_gate()
            payload["data_chain_gate"] = {
                key: consumer_gate.get(key)
                for key in (
                    "status",
                    "allowed",
                    "authoritative",
                    "run_id",
                    "target_date",
                    "reasons",
                    "chain_status",
                )
            }
            if not consumer_gate.get("allowed"):
                payload["status"] = "degraded"
                payload.setdefault("warnings", []).append(
                    "downstream_data_chain_gate_blocked"
                )
            return jsonify(clean_payload(payload)), 200
        except Exception as exc:  # noqa: BLE001 - endpoint must degrade explicitly
            return jsonify(
                clean_payload(
                    {
                        "ok": False,
                        "status": "degraded",
                        "reason_code": "decision_board_build_failed",
                        "message_for_ui": f"决策看板后端证据聚合失败：{exc}",
                        "research_only": True,
                        "paper_only": True,
                        "no_broker": True,
                        "live_execution_locked": True,
                    }
                )
            ), 500

    return bp


def register_decision_board_routes(app: Flask, deps: DecisionBoardRouteDeps) -> None:
    app.register_blueprint(create_decision_board_blueprint(deps))


__all__ = [
    "DecisionBoardRouteDeps",
    "create_decision_board_blueprint",
    "register_decision_board_routes",
]
