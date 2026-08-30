"""Daily candidate action routes for the QuantDesk 8000 server."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from flask import Blueprint, Flask, jsonify, request

from scripts.server.helpers.util import clean_payload


@dataclass(frozen=True)
class DailyActionRouteDeps:
    """Callbacks that keep daily action routes decoupled from quant_api_server."""

    save_candidate_review_action: Callable[..., dict[str, Any]]


def create_daily_action_blueprint(deps: DailyActionRouteDeps) -> Blueprint:
    """Create local research-state mutation routes for daily candidates."""

    bp = Blueprint("daily_action_api", __name__)

    @bp.route("/api/daily/candidate-action", methods=["POST"])
    def post_daily_candidate_action() -> Any:
        body = request.get_json(silent=True) or {}
        payload = deps.save_candidate_review_action(
            str(body.get("symbol") or ""),
            str(body.get("entry_date") or ""),
            str(body.get("lane") or ""),
            str(body.get("action") or ""),
            note=str(body.get("note") or ""),
        )
        return jsonify(clean_payload(payload)), 200 if payload.get("ok") else 400

    return bp


def register_daily_action_routes(app: Flask, deps: DailyActionRouteDeps) -> None:
    app.register_blueprint(create_daily_action_blueprint(deps))
