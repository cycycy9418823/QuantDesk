"""Small system routes for the QuantDesk 8000 research server."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from flask import Blueprint, Flask, Response, jsonify, make_response, render_template


def create_system_blueprint(*, build_id: str, asset_versions_func: Callable[[], dict[str, str]]) -> Blueprint:
    """Create routes that expose server process metadata.

    The build id is captured when ``quant_api_server.py`` is imported so the
    launcher can detect a stale running process after source edits.
    """

    bp = Blueprint("system_api", __name__)

    @bp.route("/")
    def index() -> Any:
        # The page's CSS is inline in the template, so a cached HTML response
        # would keep serving stale styles even though versioned JS/CSS assets
        # update. Mark the HTML itself no-cache so edits show on a normal reload.
        html = render_template("daily_research.html", asset_versions=asset_versions_func())
        response = make_response(html)
        response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
        return response

    @bp.route("/api/launcher/build-id", methods=["GET"])
    def get_launcher_build_id() -> Any:
        return jsonify({"build_id": build_id})

    @bp.route("/favicon.ico")
    def favicon() -> Response:
        svg = (
            '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64">'
            '<rect width="64" height="64" rx="12" fill="#2563a9"/>'
            '<text x="32" y="41" text-anchor="middle" font-size="30" font-family="Arial" fill="white">Q</text>'
            "</svg>"
        )
        return Response(svg, mimetype="image/svg+xml")

    return bp


def register_system_routes(app: Flask, *, build_id: str, asset_versions_func: Callable[[], dict[str, str]]) -> None:
    app.register_blueprint(create_system_blueprint(build_id=build_id, asset_versions_func=asset_versions_func))
