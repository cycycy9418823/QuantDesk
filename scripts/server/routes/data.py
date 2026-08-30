"""Read-only data health routes for the QuantDesk 8000 server."""

from __future__ import annotations

from typing import Any

from flask import Blueprint, Flask, jsonify

from scripts.server.helpers.util import clean_payload


def create_data_blueprint() -> Blueprint:
    """Create data status routes that never write or download."""

    bp = Blueprint("data_api", __name__)

    @bp.route("/api/data-health/dashboard", methods=["GET"])
    def get_data_health_dashboard() -> Any:
        """Read-only data freshness/health aggregate. Never downloads or updates."""
        try:
            from quantdesk.data_health_dashboard import build_cached_data_health_dashboard

            payload = build_cached_data_health_dashboard()
        except Exception as exc:  # noqa: BLE001
            return jsonify(
                clean_payload(
                    {
                        "ok": False,
                        "message_for_ui": f"数据健康看板生成失败：{exc}",
                        "error": str(exc),
                    }
                )
            ), 500
        return jsonify(clean_payload(payload))

    return bp


def register_data_routes(app: Flask) -> None:
    app.register_blueprint(create_data_blueprint())
