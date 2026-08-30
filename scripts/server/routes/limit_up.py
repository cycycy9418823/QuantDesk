"""Limit-up review routes for the QuantDesk 8000 server."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from flask import Blueprint, Flask, jsonify, request

from scripts.server.helpers.util import clean_payload


@dataclass(frozen=True)
class LimitUpRouteDeps:
    """Callbacks that keep this blueprint decoupled from quant_api_server."""

    read_limit_up_review_cache: Callable[..., dict[str, Any]]
    refresh_limit_up_review: Callable[[str], dict[str, Any]]
    normalize_review_date: Callable[[str], str]
    request_truthy: Callable[[Any], bool]


def create_limit_up_blueprint(deps: LimitUpRouteDeps) -> Blueprint:
    """Create limit-up review routes used by the active 8000 UI."""

    bp = Blueprint("limit_up_api", __name__)

    @bp.route("/api/limit-up/review", methods=["GET"])
    def get_limit_up_review() -> Any:
        """Cache-only limit-up review endpoint; does not perform network fetch."""
        force_attribution = deps.request_truthy(request.args.get("refresh_attribution"))
        return jsonify(clean_payload(deps.read_limit_up_review_cache(force_attribution=force_attribution)))

    @bp.route("/api/limit-up/refresh", methods=["POST"])
    def refresh_limit_up_review_endpoint() -> Any:
        body = request.get_json(silent=True) or {}
        network_confirmed = str(body.get("network") or "").strip().lower() in {"1", "true", "yes", "on"}
        if not network_confirmed:
            return (
                jsonify(
                    clean_payload(
                        {
                            "ok": False,
                            "status": "network_not_confirmed",
                            "message_for_ui": "刷新涨停复盘需要显式 network=true；GET 只读取本地缓存。",
                            "boundary": "Research Only；不接券商、不下单、不构成投资建议。",
                        }
                    )
                ),
                400,
            )
        date_text = deps.normalize_review_date(str(body.get("date") or ""))
        payload = deps.refresh_limit_up_review(date_text)
        return jsonify(clean_payload(payload)), 200 if payload.get("rows") else 502

    return bp


def register_limit_up_routes(app: Flask, deps: LimitUpRouteDeps) -> None:
    app.register_blueprint(create_limit_up_blueprint(deps))
