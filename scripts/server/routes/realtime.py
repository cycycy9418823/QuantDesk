"""Realtime observation routes for the QuantDesk 8000 server."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

import pandas as pd
from flask import Blueprint, Flask, jsonify, request

from scripts.server.helpers.util import clean_payload


@dataclass(frozen=True)
class RealtimeRouteDeps:
    """Callbacks that keep this blueprint decoupled from quant_api_server."""

    request_truthy: Callable[[Any], bool]
    realtime_network_enabled: Callable[[], bool]
    realtime_market_summary: Callable[..., dict[str, Any]]
    refresh_spot_snapshot_cache: Callable[..., dict[str, Any]]
    search_stock_rows: Callable[[str, int], list[dict[str, Any]]]
    normalize_symbol: Callable[[str], str]
    refresh_single_quote_cache: Callable[[str], dict[str, Any]]
    maybe_start_realtime_background_refresh: Callable[..., Any]
    ensure_single_stock_daily_with_auth: Callable[..., dict[str, Any]]
    load_stock_data: Callable[[str], Any]
    spot_quote_for_symbol: Callable[[str], dict[str, Any]]
    candidate_name: Callable[[str], str]
    latest_indicators: Callable[[Any], dict[str, Any]]
    chart_rows: Callable[[Any], list[dict[str, Any]]]
    append_realtime_chart_row: Callable[[list[dict[str, Any]], dict[str, Any]], tuple[list[dict[str, Any]], str]]
    chart_markers: Callable[..., list[dict[str, Any]]]
    chart_number: Callable[..., Any]
    latest_pct_change: Callable[[Any], Any]
    risk_level_from_indicators: Callable[[dict[str, Any]], str]
    generate_indicator_explanation: Callable[[dict[str, Any]], str]
    generate_risk_warning: Callable[[dict[str, Any], int], str]
    build_realtime_observation: Callable[[Any, dict[str, Any]], dict[str, Any]]
    build_research_reference: Callable[[Any], dict[str, Any]]
    build_news_summary: Callable[[str], dict[str, Any]]
    build_industry_comparison: Callable[[str, Any], dict[str, Any]]
    add_manual_observation: Callable[..., dict[str, Any]]


def _truthy_text(value: Any) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def create_realtime_blueprint(deps: RealtimeRouteDeps) -> Blueprint:
    """Create realtime observation routes used by the active 8000 UI."""

    bp = Blueprint("realtime_api", __name__)

    @bp.route("/api/realtime/market", methods=["GET", "POST"])
    def realtime_market() -> Any:
        body = request.get_json(silent=True) or {}
        is_get = request.method == "GET"
        # GET is a read-only cache view (B6): a query string must never turn a
        # page load or a cross-site embed into a provider refresh.  Refresh
        # intent is an explicit POST action that flows through the origin gate.
        refresh = False if is_get else _truthy_text(body.get("refresh"))
        explicit_network = False if is_get else deps.request_truthy(body.get("network"))
        payload = deps.realtime_market_summary(refresh=refresh, explicit_network=explicit_network)
        return jsonify(clean_payload(payload))

    @bp.route("/api/realtime/stock-search", methods=["GET"])
    def realtime_stock_search() -> Any:
        query = str(request.args.get("q") or "").strip()
        try:
            limit = max(1, min(20, int(request.args.get("limit") or 8)))
        except (TypeError, ValueError):
            limit = 8
        # Search is read-only and the service switch stays separate from the
        # query: no ``refresh`` mutation fires on a GET (B6).
        rows = deps.search_stock_rows(query, limit)
        return jsonify(
            clean_payload(
                {
                    "ok": True,
                    "query": query,
                    "count": len(rows),
                    "rows": rows,
                    "network_authorized": deps.realtime_network_enabled(),
                    "temporary_quote_only": True,
                    "message": "搜索结果可进入单股观察；不会自动加入正式候选池。",
                }
            )
        )

    @bp.route("/api/realtime/stock/<symbol>", methods=["GET"])
    def realtime_stock_detail(symbol: str) -> Any:
        clean_symbol = deps.normalize_symbol(symbol)
        # GET is a read-only cache view (B6): quote/daily refresh and any
        # network intent are explicit POST actions, never query-string side
        # effects.  The master switch is still reported for transparency.
        refresh_daily = False
        refresh_quote = False
        network_allowed = bool(deps.realtime_network_enabled())
        quote_refresh_result: dict[str, Any] = {"attempted": False}
        daily_refresh = deps.ensure_single_stock_daily_with_auth(
            clean_symbol,
            refresh_daily=False,
            network_allowed=False,
        )
        stock_df = deps.load_stock_data(clean_symbol)
        quote = deps.spot_quote_for_symbol(clean_symbol)
        name = quote.get("name") if quote.get("available") else deps.candidate_name(clean_symbol)
        latest = stock_df.iloc[-1] if not stock_df.empty else None
        latest_date = (
            latest.get("date").strftime("%Y-%m-%d")
            if latest is not None and isinstance(latest.get("date"), pd.Timestamp)
            else str(latest.get("date", "")) if latest is not None else "数据缺失"
        )
        indicators = deps.latest_indicators(stock_df) if not stock_df.empty else {}
        rows = deps.chart_rows(stock_df) if not stock_df.empty else []
        rows, temp_date = deps.append_realtime_chart_row(rows, quote)
        markers = deps.chart_markers(indicators, latest_date, "单股观察", latest_date, {}) if rows else []
        if temp_date:
            markers.extend(
                [
                    {
                        "panel": "price",
                        "date": temp_date,
                        "label": "实时临时",
                        "text": "盘中/实时快照点，仅用于观察，不覆盖正式完整日线。",
                        "kind": "realtime_temp",
                    },
                    {
                        "panel": "volume",
                        "date": temp_date,
                        "label": "临时量能",
                        "text": "当前成交量来自实时快照，收盘前仍会变化。",
                        "kind": "realtime_temp",
                    },
                ]
            )
        payload = {
            "ok": True,
            "symbol": clean_symbol,
            "name": name,
            "network_authorized": network_allowed,
            "temporary_quote_only": True,
            "quote_refresh": quote_refresh_result,
            "daily_refresh": daily_refresh,
            "basic_info": {
                "symbol": clean_symbol,
                "name": name,
                "price": quote.get("price")
                if quote.get("available")
                else deps.chart_number(latest.get("close")) if latest is not None else "数据缺失",
                "pct_chg": quote.get("pct_chg")
                if quote.get("available")
                else deps.latest_pct_change(stock_df) if not stock_df.empty else "数据缺失",
                "date": quote.get("quote_date") if quote.get("available") else latest_date,
                "risk_level": deps.risk_level_from_indicators(indicators) if indicators else "中",
            },
            "realtime_quote": quote,
            "daily_layer": {
                "latest_complete_date": latest_date,
                "row_count": int(len(stock_df)),
                "message": "正式研究层仍使用完整日线；盘中实时点只作为观察覆盖层。",
            },
            "observation_analysis": deps.build_realtime_observation(stock_df, quote),
            "indicators_exp": (
                deps.generate_indicator_explanation(indicators)
                if indicators
                else "本地完整日线不足，暂时无法生成指标详解。"
            ),
            "risk_warning": (
                deps.generate_risk_warning(indicators, len(stock_df))
                if indicators
                else "本地完整日线不足，风险提示会降级。实时快照不构成投资建议。"
            ),
            "research_reference": (
                deps.build_research_reference(stock_df)
                if not stock_df.empty
                else {"available": False, "message": "本地完整日线不足，无法生成统计参考。"}
            ),
            "news_summary": deps.build_news_summary(clean_symbol),
            "industry_comparison": deps.build_industry_comparison(
                clean_symbol,
                quote.get("pct_chg") if quote.get("available") else deps.latest_pct_change(stock_df) if not stock_df.empty else None,
            ),
            "tracking_vs_benchmark": {
                "available": False,
                "hint": "单股实时观察尚未加入正式候选池，没有入选日期；这里只展示观察层，不做入选以来指数对比。",
            },
            "chart_data": {
                "kline_data": rows,
                "select_date": temp_date or latest_date,
                "select_reason": "单股观察",
                "markers": markers,
            },
            "research_only": "Research Only；实时观察不覆盖正式日线，不接券商、不下单、不构成投资建议。",
        }
        return jsonify(clean_payload(payload))

    @bp.route("/api/realtime/stock/<symbol>/refresh", methods=["POST"])
    def realtime_stock_refresh(symbol: str) -> Any:
        """Explicit single-stock refresh action (B6).

        GET stock detail is cache-only; refreshing the quote and/or daily layer
        is a deliberate POST that flows through the origin gate and the network
        master switch plus the caller's explicit intent.
        """
        clean_symbol = deps.normalize_symbol(symbol)
        body = request.get_json(silent=True) or {}
        refresh_daily = _truthy_text(body.get("refresh_daily"))
        refresh_quote = _truthy_text(body.get("refresh_quote", True))
        network_allowed = bool(deps.realtime_network_enabled() or deps.request_truthy(body.get("network")))
        quote_refresh_result: dict[str, Any] = {"attempted": False}
        if refresh_quote:
            if network_allowed:
                quote_refresh_result = deps.refresh_single_quote_cache(clean_symbol)
                if not quote_refresh_result.get("ok") and deps.realtime_network_enabled():
                    deps.maybe_start_realtime_background_refresh(force=False, reason=f"stock_detail:{clean_symbol}")
            else:
                quote_refresh_result = deps.refresh_spot_snapshot_cache(network=False, force=False)
        daily_refresh = deps.ensure_single_stock_daily_with_auth(
            clean_symbol,
            refresh_daily=refresh_daily,
            network_allowed=network_allowed,
        )
        return jsonify(
            clean_payload(
                {
                    "ok": True,
                    "symbol": clean_symbol,
                    "network_authorized": network_allowed,
                    "quote_refresh": quote_refresh_result,
                    "daily_refresh": daily_refresh,
                    "temporary_quote_only": True,
                    "message": "单股实时/日线刷新已按意图执行；不覆盖正式研究结论。",
                }
            )
        )

    @bp.route("/api/realtime/watchlist", methods=["POST"])
    def realtime_add_watchlist() -> Any:
        body = request.get_json(silent=True) or {}
        payload = deps.add_manual_observation(
            str(body.get("symbol") or ""),
            name=str(body.get("name") or ""),
            note=str(body.get("note") or ""),
        )
        return jsonify(clean_payload(payload)), 200 if payload.get("ok") else 400

    return bp


def register_realtime_routes(app: Flask, deps: RealtimeRouteDeps) -> None:
    app.register_blueprint(create_realtime_blueprint(deps))
