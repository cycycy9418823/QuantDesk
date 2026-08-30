"""Stock detail compatibility routes for the QuantDesk 8000 server."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from flask import Blueprint, Flask, request


@dataclass(frozen=True)
class StockRouteDeps:
    """Callbacks that keep this blueprint decoupled from quant_api_server."""

    get_stock_detail: Callable[[str], Any]


def create_stock_blueprint(deps: StockRouteDeps) -> Blueprint:
    """Create stock compatibility routes used by legacy local links."""

    bp = Blueprint("stock_api", __name__)

    @bp.route("/api/stock/<symbol>/detail", methods=["GET"])
    @bp.route("/api/stock-detail", methods=["GET"])
    @bp.route("/api/stock-detail/<symbol>", methods=["GET"])
    def get_stock_detail_legacy(symbol: str = "") -> Any:
        requested_symbol = symbol or request.args.get("symbol") or request.args.get("code") or ""
        return deps.get_stock_detail(requested_symbol)

    return bp


def register_stock_routes(app: Flask, deps: StockRouteDeps) -> None:
    app.register_blueprint(create_stock_blueprint(deps))
