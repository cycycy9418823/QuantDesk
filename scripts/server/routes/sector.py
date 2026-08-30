"""Sector board refresh route for the QuantDesk 8000 server."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from flask import Blueprint, Flask, jsonify

from scripts.server.helpers.util import clean_payload


@dataclass(frozen=True)
class SectorRouteDeps:
    """Callbacks that keep this blueprint decoupled from quant_api_server."""

    refresh_industry_board_cache: Callable[[], dict[str, Any]]


def create_sector_blueprint(deps: SectorRouteDeps) -> Blueprint:
    """Create sector board routes used by the active 8000 UI."""

    bp = Blueprint("sector_api", __name__)

    @bp.route("/api/sector/refresh", methods=["POST"])
    def refresh_sector_board() -> Any:
        """Explicit operator endpoint: fetch the Eastmoney industry board via akshare."""
        result = deps.refresh_industry_board_cache()
        return jsonify(clean_payload(result))

    return bp


def register_sector_routes(app: Flask, deps: SectorRouteDeps) -> None:
    app.register_blueprint(create_sector_blueprint(deps))
