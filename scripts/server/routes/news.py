"""News/status routes for the QuantDesk 8000 server."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable

from flask import Blueprint, Flask, jsonify, request

from quantdesk.market_clock import CN_TIMEZONE
from scripts.server.helpers.util import clean_payload

LOG = logging.getLogger("quant_api")


@dataclass(frozen=True)
class NewsRouteDeps:
    """Callbacks that keep this blueprint decoupled from quant_api_server."""

    fetch_news: Callable[[str, int, bool], dict[str, Any]]
    news_cache_stats: Callable[[], dict[str, Any]]
    refresh_single_news: Callable[[str], dict[str, Any]]
    normalize_symbol: Callable[[str], str]
    request_truthy: Callable[[Any], bool]


def create_news_blueprint(deps: NewsRouteDeps) -> Blueprint:
    """Create news routes used by the active 8000 UI."""

    bp = Blueprint("news_api", __name__)

    @bp.route("/api/news/refresh", methods=["POST"])
    def refresh_news() -> Any:
        body = request.get_json(silent=True) or {}
        symbol = str(body.get("symbol") or "").strip()
        if not symbol or len(symbol) < 6:
            return jsonify({"ok": False, "message": "需要有效的6位股票代码"}), 400
        result = deps.refresh_single_news(symbol)
        result["cache_stats"] = deps.news_cache_stats()
        return jsonify(clean_payload(result))

    @bp.route("/api/news/status", methods=["GET"])
    def news_status() -> Any:
        stats = deps.news_cache_stats()
        age_days = None
        if stats.get("latest_date") and len(stats["latest_date"]) == 8:
            try:
                age_days = (datetime.now(CN_TIMEZONE) - datetime.strptime(stats["latest_date"], "%Y%m%d").replace(tzinfo=CN_TIMEZONE)).days
            except Exception as exc:  # noqa: BLE001 - invalid cache metadata should be visible
                LOG.warning("news_status_latest_date_parse_failed: latest_date=%r error=%s", stats.get("latest_date"), exc)
        stats["age_days"] = age_days
        stats["ok"] = True
        stats["update_hint"] = "新闻来自东方财富个股新闻，仅用于学习观察，不构成投资建议。"
        return jsonify(clean_payload(stats))

    @bp.route("/api/stock/<symbol>/news", methods=["GET"])
    def get_stock_news(symbol: str) -> Any:
        clean_symbol = deps.normalize_symbol(symbol)
        # GET is a read-only cache view (B6): a query string must never trigger
        # a provider fetch.  News refresh is the explicit POST /api/news/refresh.
        network_allowed = False
        result = deps.fetch_news(clean_symbol, 8, network_allowed)
        return jsonify(clean_payload(result))

    return bp


def register_news_routes(app: Flask, deps: NewsRouteDeps) -> None:
    app.register_blueprint(create_news_blueprint(deps))
