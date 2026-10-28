"""
QuantDesk daily research API server.

Research-only: local files only, no broker connection, no real orders, no network fetch by default.
"""

from __future__ import annotations

import csv
import hashlib
import json
import logging
import math
import os
import re
import subprocess
import sys
import time
import webbrowser
from io import BytesIO
from threading import Thread

logging.basicConfig(level=logging.WARNING, format="[%(levelname)s] %(name)s: %(message)s")
LOG = logging.getLogger("quant_api")
from datetime import date, datetime
from pathlib import Path
from typing import Any
import argparse

import pandas as pd
from flask import jsonify, request
from flask import g, has_request_context

project_root = Path(__file__).resolve().parents[1]
if str(project_root) not in sys.path:
    sys.path.insert(0, str(project_root))
# Remapped to the viewer data root because the tail of this path is under data/.
INTRADAY_ONE_MINUTE_DIR = project_root / "data" / "intraday" / "1m"

from quantdesk import technical_indicators
from quantdesk.csv_io import read_csv_frame
from quantdesk.ui_read_cache import (UI_READ_CACHE, byte_identity, cached_file_value,
    InputChangedDuringRead, detail_page, summary_projection, public_read_identity)
from quantdesk.candidate_contract import (
    attach_theme_map_to_candidates,
    summarize_candidate_theme_coverage,
)
from quantdesk.column_aliases import find_column, normalize_columns
from quantdesk.data_confidence import build_reference_confidence
from quantdesk.data_chain_consumer_gate import evaluate_data_chain_consumer_gate
from quantdesk.explainers import stock_explainer
from quantdesk.daily_candidate_generator import generate_daily_recommendations
from quantdesk.daily_candidate_refresh import load_formal_candidate_intake
from quantdesk.daily_price_update_engine import update_daily_prices
from quantdesk.market_clock import expected_settlement_date, get_market_session, to_shanghai_datetime
from quantdesk.market_intelligence_utils import intraday_file_exists
from quantdesk.paper.auto_paper_portfolio import (
    AUTO_PAPER_PORTFOLIO_HISTORY_JSON,
    AUTO_PAPER_PORTFOLIO_LATEST_JSON,
    auto_paper_portfolio_equity_history,
    auto_paper_portfolio_latest,
)
from quantdesk.providers.news_provider import fetch_news, news_cache_stats, refresh_single_news
from quantdesk.theme_attribution import build_theme_attribution, summarize_attribution_sources
from quantdesk.theme_current_candidate_validator import validate_theme_map_for_current_candidates
from quantdesk.theme_map_import_draft import (
    generate_import_draft as generate_theme_map_import_draft,
)
from quantdesk.theme_map_import_draft import (
    preview_import_draft as preview_theme_map_import_draft,
)
from quantdesk.theme_map_updater import (
    import_theme_map,
    theme_map_preview,
    theme_map_status,
    update_theme_map,
)
from quantdesk.theme_sector_engine import load_theme_map
from quantdesk.trader_profile import default_research_paper_profile
from quantdesk.server_build import compute_server_build_id
from quantdesk.trading_calendar import load_a_share_trading_calendar
from quantdesk.version import APP_VERSION
from scripts.startup_data_update import run_startup_data_update


from scripts.server.app_factory import (  # Flask app, JSON provider, CORS (re-exported)
    app, LOCAL_CORS_ORIGINS,
)
from scripts.server.routes.agent import AgentRouteDeps, register_agent_routes
from scripts.server.routes.backtest import register_backtest_routes
from scripts.server.routes.data import register_data_routes
from scripts.server.routes.forward_pit import ForwardPitRouteDeps, register_forward_pit_routes
from scripts.server.routes.data_update import DataUpdateRouteDeps, register_data_update_routes, run_repair_daily_command
from scripts.server.routes.daily_actions import DailyActionRouteDeps, register_daily_action_routes
from scripts.server.routes.daily_generation import DailyGenerationRouteDeps, register_daily_generation_routes
from scripts.server.routes.daily import DailyRouteDeps, register_daily_routes
from scripts.server.routes.decision_board import DecisionBoardRouteDeps, register_decision_board_routes
from scripts.server.routes.limit_up import LimitUpRouteDeps, register_limit_up_routes
from scripts.server.routes.market_brain import MarketBrainRouteDeps, register_market_brain_routes
from scripts.server.routes.news import NewsRouteDeps, register_news_routes
from scripts.server.routes.paper_simulation import PaperSimulationRouteDeps, register_paper_simulation_routes
from scripts.server.routes.performance import PerformanceRouteDeps, register_performance_routes
from scripts.server.routes.realtime import RealtimeRouteDeps, register_realtime_routes
from scripts.server.routes.research import ResearchRouteDeps, register_research_routes
from scripts.server.routes.sector import SectorRouteDeps, register_sector_routes
from scripts.server.routes.stock import StockRouteDeps, register_stock_routes
from scripts.server.routes.stock_workbench import StockWorkbenchDeps, register_stock_workbench_routes
from quantdesk.market_clock import load_cn_trading_calendar
from scripts.server.routes.strategy_screening import (
    StrategyScreeningRouteDeps,
    register_strategy_screening_routes,
    run_strategy_screening_worker,
)
from scripts.server.routes.startup import StartupRouteDeps, register_startup_routes
from scripts.server.routes.system import register_system_routes
from scripts.server.routes.theme_map import ThemeMapRouteDeps, register_theme_map_routes
from scripts.server.routes.ai_trader import register_ai_trader_routes

# --- Constants & shared global state live in server.constants (re-exported) ---
from scripts.server.constants import (
    DATA_DIR, DAILY_DIR, TRACKING_DIR, BENCHMARK_DIR, CACHE_DIR, UNIVERSE_DIR,
    STARTUP_UPDATE_JSON, STRATEGY_SCREENING_TASK_JSON,
    DAILY_CANDIDATE_GENERATION_JSON, DAILY_CANDIDATE_LATEST_CSV,
    _LATEST_GENERATION_SYMBOL_CACHE,
    INDUSTRY_BOARD_CACHE, SPOT_SNAPSHOT_CACHE, SPOT_SNAPSHOT_META,
    LIMIT_UP_REVIEW_CACHE, MANUAL_OBSERVATION_WATCHLIST, PAPER_WATCHLIST_JSON,
    STOCK_THEME_MAP_CSV, STOCK_THEME_MAP_IMPORT_CSV,
    UPDATE_HINT, MARKET_OVERVIEW_HINT,
    STRATEGY_LABELS, DETAIL_LANE_TO_STRATEGY, PRIMARY_STRATEGY_PRIORITY,
    STARTUP_REFRESH_LOCK, STARTUP_REFRESH_STATE, STARTUP_TERMINAL_STATUSES,
    STRATEGY_SCREENING_LOCK, STRATEGY_SCREENING_STATE,
    DATA_WRITE_LOCK,
)


# ---------------------------------------------------------------------------
# POST origin guard — block cross-origin POST to destructive endpoints.
# ---------------------------------------------------------------------------
_ALLOWED_POST_ORIGINS: set[str] = set()


def _rebuild_allowed_origins() -> None:
    """Rebuild the allowed-origin set from CORS config + dynamic port detection."""
    _ALLOWED_POST_ORIGINS.clear()
    for origin in LOCAL_CORS_ORIGINS:
        _ALLOWED_POST_ORIGINS.add(origin.rstrip("/"))
    # Also allow whatever port the server is actually running on.
    for port in (8000, int(os.environ.get("QUANTDESK_PORT", "0") or 0)):
        if port:
            _ALLOWED_POST_ORIGINS.add(f"http://127.0.0.1:{port}")
            _ALLOWED_POST_ORIGINS.add(f"http://localhost:{port}")


_rebuild_allowed_origins()


@app.before_request
def guard_post_origin() -> Response | None:
    """Reject POST requests from non-local origins (CSRF protection).

    GET/OPTIONS/HEAD are always allowed.  POST requests must either:
    - have no Origin header (same-site navigation, curl, launcher), or
    - have an Origin matching the localhost allow list.
    """
    if request.method not in ("POST", "PUT", "DELETE", "PATCH"):
        return None
    origin = request.headers.get("Origin", "")
    if not origin:
        return None  # same-site or non-browser client — OK
    if origin.rstrip("/") in _ALLOWED_POST_ORIGINS:
        return None  # localhost — OK
    return jsonify({
        "ok": False,
        "status": "origin_blocked",
        "message_for_ui": f"POST 请求来源 {origin} 不在允许的本地列表中。",
    }), 403


def static_asset_version(relative_path: str) -> str:
    path = project_root / "app" / "static" / relative_path
    try:
        stat = path.stat()
    except OSError:
        return "missing"
    return f"{int(stat.st_mtime)}-{stat.st_size}"


def daily_research_asset_versions() -> dict[str, str]:
    return {
        "echarts": static_asset_version("echarts.min.js"),
        "daily_research": static_asset_version("js/daily_research.js"),
        "stock_workbench": static_asset_version("js/stock_workbench.js"),
        "stock_workbench_css": static_asset_version("css/stock_workbench.css"),
    }


@app.after_request
def set_research_cache_headers(response: Response) -> Response:
    # The page shell, the API, and the always-no-store research bundle must never
    # be cached, so template/JS edits show on a plain refresh (no restart). Paired
    # with TEMPLATES_AUTO_RELOAD, which re-reads the Jinja template from disk.
    if (
        request.path == "/"
        or request.path.startswith("/api/")
        or request.path.endswith(("/daily_research.js", "/stock_workbench.js", "/stock_workbench.css"))
    ):
        response.headers["Cache-Control"] = "no-store, max-age=0"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
    return response


def server_source_build_id() -> str:
    """Stable id of active Python sources loaded by this server process."""

    return compute_server_build_id(project_root)


SERVER_BUILD_ID = server_source_build_id()


def runtime_capabilities() -> dict[str, Any]:
    read_only = bool(app.config.get("RESEARCH_READ_ONLY", False))
    return {"ok": True, "read_only": read_only, "mutations_enabled": not read_only,
            "selected_quote_refresh": bool(app.config.get("STOCK_QUOTE_REFRESH_ALLOWED", True)),
            "mode": "read_only_research" if read_only else "local_research_edit",
            "source_identity": {"server_build_id": SERVER_BUILD_ID,
                "entry_sha256": app.config.get("RUNTIME_ENTRY_SHA256") or hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                "data_root": str(app.config.get("QUANTDESK_PROJECT_ROOT") or project_root)},
            "paper_only": True, "no_broker": True, "label": "纸面 · 未验证"}


@app.before_request
def guard_research_mode() -> Response | None:
    if not app.config.get("RESEARCH_READ_ONLY") or request.method in {"GET", "HEAD", "OPTIONS"}:
        return None
    if (app.config.get("STOCK_QUOTE_REFRESH_ALLOWED", False) and request.method == "POST"
            and re.fullmatch(r"/api/stock/[0-9]{6}/quote-refresh", request.path)):
        return None
    return jsonify(ok=False, status="research_read_only",
                   message_for_ui="当前为只读研究模式，此操作未执行；查看行情和证据仍可继续。"), 423


from scripts.server.routes.product_review_routes import register_product_review_routes
register_product_review_routes(app, root=lambda: Path(app.config.get("QUANTDESK_PROJECT_ROOT") or project_root),
                               capabilities=runtime_capabilities)


def stock_reference_fee_assumptions() -> dict[str, Any]:
    from quantdesk.config import load_config
    config = load_config(project_root / "config/default.yaml")
    return {**config.get("backtest", {}), "source": "config/default.yaml/backtest；研究费用假设"}


def stock_reference_account_context() -> dict[str, Any]:
    value = read_json_file(project_root / "state/auto_paper_portfolio.json")
    return value if isinstance(value, dict) else {}


def ui_calendar_days() -> list[str]:
    paths = [project_root / "data/calendar/a_share_trading_calendar.csv",
        project_root / "config/trading_calendar_cn.yaml"]
    return cached_file_value(paths, lambda: [day for day, is_open in
        (load_a_share_trading_calendar().get("is_open_by_date") or {}).items() if is_open],
        scope="ui_calendar_days", code_id=str(id(load_a_share_trading_calendar)))


_UI_CACHE_PATHS = frozenset({"/api/daily/overview", "/api/daily/candidates", "/api/strategy-scorecard",
    "/api/decision-board", "/api/project-status", "/api/data-health/dashboard"})


def _ui_input_object(path: Path, *, fields: tuple[str, ...] = ()) -> dict[str, Any]:
    """Read claims for dependency enumeration; admission stays in the consumer."""
    try:
        def read() -> dict[str, Any]:
            value = json.loads(path.read_text(encoding="utf-8-sig"))
            if not isinstance(value, dict):
                return {}
            return {key: value[key] for key in fields if key in value} if fields else value
        value = cached_file_value([path], read, scope="ui_dependency_object:" + ",".join(fields))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError, UnicodeError):
        return {}


def _ui_generation_dependency_paths(target_text: str) -> list[Path]:
    from quantdesk.daily_candidate_refresh import _safe_run_id
    chain_path = project_root / "results/data_chain/daily_data_chain_latest.json"
    paths = [DAILY_CANDIDATE_GENERATION_JSON, DAILY_CANDIDATE_LATEST_CSV,
        TRACKING_DIR / "candidate_ledger.csv", chain_path]
    formal_dir = project_root / "results/candidate_refresh/daily"
    paths.append(formal_dir)
    if target_text:
        target = formal_dir / f"{target_text}.json"
        paths.append(target)
        if not target.is_file():
            previous = sorted(p for p in formal_dir.glob("????-??-??.json") if p.stem < target_text)
            if previous:
                paths.append(previous[-1])
    run_id = _safe_run_id(_ui_input_object(chain_path, fields=("run_id",)).get("run_id"))
    if run_id:
        paths.extend([project_root / "results/data_chain/runs" / run_id / "manifest.json",
            project_root / "results/candidate_refresh/runs" / run_id / "formal_candidate_batch.json"])
    return paths


def _ui_symbols_in_files(paths: list[Path]) -> set[str]:
    """Cache only symbol projections while rehashing source bytes on each read."""
    found: set[str] = set()
    for path in paths:
        if path.suffix not in {".csv", ".json"}:
            continue
        def extract(path: Path = path) -> frozenset[str]:
            symbols: set[str] = set()
            def visit(value: Any) -> None:
                if isinstance(value, list):
                    for item in value:
                        visit(item)
                elif isinstance(value, dict):
                    if "symbol" in value:
                        symbols.add(normalize_symbol(value["symbol"]))
                    for item in value.values():
                        visit(item)
            text = path.read_text(encoding="utf-8-sig")
            if path.suffix == ".csv":
                symbols.update(normalize_symbol(row.get("ts_code") or row.get("symbol") or row.get("code") or "")
                    for row in csv.DictReader(text.splitlines()))
            else:
                value = json.loads(text)
                if isinstance(value, dict):
                    visit(value)
            return frozenset(symbol for symbol in symbols if re.fullmatch(r"\d{6}", symbol))
        try:
            found.update(cached_file_value([path], extract, scope="ui_dependency_symbols",
                code_id=str(id(normalize_symbol))))
        except (OSError, ValueError, UnicodeError):
            pass
    return found


def _ui_dependency_paths() -> list[Path]:
    """Hash the inputs of this read view; never read all-market bar contents."""
    from quantdesk.daily_candidate_refresh import _safe_run_id
    from quantdesk.plan_review_receipts import STORE_PATH
    scope = request.path
    market = [BENCHMARK_DIR / name for name in ("hs300.csv", "sh000001.csv", "sh_index.csv", "000001_index.csv")]
    market.extend([SPOT_SNAPSHOT_CACHE, SPOT_SNAPSHOT_META, INDUSTRY_BOARD_CACHE,
        project_root / "data/calendar/a_share_trading_calendar.csv", project_root / "config/trading_calendar_cn.yaml"])
    if scope == "/api/daily/overview":
        return market
    target = expected_settlement_date(to_shanghai_datetime())
    target_text = target.isoformat() if target else ""
    if scope == "/api/project-status":
        root = Path(app.config.get("QUANTDESK_PROJECT_ROOT") or project_root)
        paths = [root / "STATUS.md", root / "data/calendar/a_share_trading_calendar.csv"]
        observed_dates = []
        for account in ("ai_trader", "ai_trader_glm"):
            cycle_path = root / "results" / account / "cycle_latest.json"
            paths.append(cycle_path)
            cycle = _ui_input_object(cycle_path)
            plan = cycle.get("plan") if isinstance(cycle.get("plan"), dict) else {}
            day = cycle.get("as_of") or plan.get("decision_date")
            if isinstance(day, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", day):
                observed_dates.append(day)
        if observed_dates:
            paths.append(root / "data/snapshots" / max(observed_dates) / "manifest.json")
        else:
            paths.append(root / "data/snapshots")
            paths.extend(sorted((root / "data/snapshots").glob("????-??-??/manifest.json")))
        return paths
    chain = [project_root / "results/data_chain/daily_data_chain_latest.json",
        project_root / "state/daily_data_chain.lock", project_root / "state/daily_data_chain.lock.json"]
    active = _safe_run_id(_ui_input_object(chain[-1]).get("run_id"))
    if active:
        chain.append(project_root / "results/data_chain/runs" / active / "checkpoint.json")
    portfolio = [project_root / "state/auto_paper_portfolio.json",
        project_root / "results/auto_paper_portfolio_latest.json", project_root / "results/auto_paper_portfolio_history.json"]
    if scope == "/api/decision-board":
        return [PAPER_WATCHLIST_JSON, *portfolio, *chain, project_root / STORE_PATH / "latest.json",
            BENCHMARK_DIR / "hs300.csv", *market[-2:]]
    if scope == "/api/data-health/dashboard":
        ai_inputs = [project_root / "state/ai_trader/ledger.json", project_root / "state/ai_trader/plan_latest.json"]
        paths = [*chain, *portfolio, *ai_inputs, *market[-2:],
            project_root / "results/data_health/data_health_dashboard_latest.json",
            project_root / "results/data_close_pipeline/data_close_pipeline_latest.json",
            project_root / "state/scheduler/daily_data_chain_status.json",
            project_root / "results/candidate_tracking/latest.json", TRACKING_DIR / "candidate_ledger.csv",
            TRACKING_DIR / "candidate_performance.csv", TRACKING_DIR / "tracking_summary.json"]
        paths.extend(DAILY_DIR / f"{symbol}.csv" for symbol in sorted(_ui_symbols_in_files(ai_inputs)))
        return paths
    generation_paths = _ui_generation_dependency_paths(target_text)
    paths = [*generation_paths, *market, TRACKING_DIR / "candidate_review_overrides.json",
        project_root / "data/universe/historical_listings.csv", STOCK_THEME_MAP_CSV, LIMIT_UP_REVIEW_CACHE,
        project_root / "results/strategy_effectiveness_weights.json"]
    if scope == "/api/daily/candidates":
        paths.extend([PAPER_WATCHLIST_JSON, portfolio[0]])
    # Only these selected rows are consumed by the daily read view. Binding all
    # historical ledger symbols hashed >100 MB of unrelated bars per request.
    # The ledger/generation bytes still bind selection; selected bar bytes are
    # rehashed, including same-stat rewrites, on every read.
    symbols = set(_candidate_read_symbols(18))
    # Empty/invalid ledgers use the consumer's first 18 local files. Directory
    # names bind additions/removals; only these requested frames are hashed.
    paths.append(DAILY_DIR)
    fallback = sorted(DAILY_DIR.glob("*.csv"))[:18]
    if not symbols:
        symbols.update(normalize_symbol(p.stem) for p in fallback)
    paths.extend(DAILY_DIR / f"{symbol}.csv" for symbol in sorted(symbols))
    paths.append(project_root / "data/intraday")
    for folder in ("data/intraday/1m", "data/intraday"):
        paths.append(project_root / folder)
        for symbol in sorted(symbols):
            paths.append(project_root / folder / f"{symbol}.csv")
        # The confidence consumer only checks presence for dated files.
        for directory in sorted((project_root / folder).glob("????-??-??")):
            if directory.is_dir():
                paths.append(directory)
    paths.extend(sorted((project_root / "data/intraday").glob("*_????-??-??.csv")))
    if scope == "/api/strategy-scorecard":
        paths.extend([PAPER_WATCHLIST_JSON, project_root / "data/news"])
        paths.extend(project_root / "results" / name for name in (
            "backtest/backtest_latest.json", "backtest/latest.json", "factor_health_latest.json",
            "strategy_lab/v420_strategy_leaderboard_latest.json", "strategy_lab/v420_real_use_strategy_lab_handoff_latest.json",
            "strategy_lab/v421_strategy_statistics_latest.json", "combined_score_backtest_latest.json", "walk_forward_latest.json"))
    return sorted(set(paths), key=str)


def _ui_request_identity() -> dict[str, Any]:
    target = expected_settlement_date(to_shanghai_datetime())
    callbacks = (candidate_rows, latest_generation_status, load_stock_data, build_strategy_scorecard,
        load_paper_simulation_watchlist, auto_paper_portfolio_latest, load_a_share_trading_calendar)
    return byte_identity(_ui_dependency_paths(), target_date=target.isoformat() if target else "",
        account="strategy/ai_trader/ai_trader_glm", scope=request.path,
        code_id=SERVER_BUILD_ID + ":" + ":".join(str(id(f)) for f in callbacks)
            + ":" + str(id(app.view_functions.get(request.endpoint))),
        config_id=json.dumps({"network": realtime_network_enabled(), "root": str(project_root),
            "configured_root": str(app.config.get("QUANTDESK_PROJECT_ROOT") or project_root)}, sort_keys=True))


@app.before_request
def serve_identified_ui_read() -> Response | None:
    """Cache only GET builds; each hit rehashes its explicit source bytes."""
    if request.method != "GET" or request.path not in _UI_CACHE_PATHS:
        return None
    if app.testing and not app.config.get("UI_READ_CACHE_ENABLED"):
        return None
    if app.config.get("UI_READ_CACHE_ENABLED") is False:
        return None
    try:
        identity = _ui_request_identity()
        scope = request.path
        # Presentation-only args share the exact full response and run identity.
        ignored = {"summary", "details", "pointer", "offset", "limit", "identity"}
        key = (scope, tuple(sorted((k,v) for k,v in request.args.items() if k not in ignored)), identity["sha256"])
        wants_summary = request_truthy(request.args.get("summary"))
        wants_details = request_truthy(request.args.get("details"))
        if scope == "/api/data-health/dashboard" and (wants_summary or wants_details):
            from quantdesk.data_health_dashboard import build_cached_data_health_dashboard
            def build_health() -> dict[str, Any]:
                value = build_cached_data_health_dashboard()
                if _ui_request_identity() != identity:
                    raise InputChangedDuringRead("ui_health_inputs_changed_during_response")
                return value
            payload = UI_READ_CACHE.get(("ui_health_payload", identity["sha256"]), build_health,
                copy=False, admit=lambda value: value.get("ok") is not False)
            status = 200
        else:
            def build() -> tuple[bytes, int]:
                response = app.make_response(app.view_functions[request.endpoint](**(request.view_args or {})))
                if _ui_request_identity() != identity:
                    raise InputChangedDuringRead("ui_inputs_changed_during_response")
                return response.get_data(), response.status_code
            raw, status = UI_READ_CACHE.get(key, build, copy=False, admit=lambda value: value[1] < 400
                and json.loads(value[0]).get("ok") is not False)
            if not wants_summary and not wants_details:
                return app.response_class(raw, status=status, mimetype="application/json")
            payload = json.loads(raw)
        claimed = request.args.get("identity")
        if claimed and claimed != identity["sha256"]:
            return jsonify(ok=False, status="read_identity_changed", read_identity=public_read_identity(identity)), 409
        if wants_details:
            value = detail_page(payload, pointer=request.args.get("pointer", ""),
                offset=int(request.args.get("offset", "0")), limit=int(request.args.get("limit", "100")))
        else:
            value = summary_projection(payload)
            value["details_url"] = scope + "?details=true&identity=" + identity["sha256"]
        value["read_identity"] = public_read_identity(identity)
        # JSON null is part of the paging/evidence contract (end of pages and
        # unknown values). Legacy display sanitization turns it into text and
        # cannot be used for these new machine-readable projections.
        return app.response_class(json.dumps(value, ensure_ascii=False, allow_nan=False,
            default=clean_payload), status=status, mimetype="application/json")
    except TimeoutError as exc:
        return jsonify(ok=False, status="ui_read_cache_wait_timeout", reason=type(exc).__name__,
                       message_for_ui="同一资料的读取仍在进行，本次等待已结束；可稍后重试，原有结果保留。", read_only=True), 503
    except (InputChangedDuringRead, OSError) as exc:
        return jsonify(ok=False, status="read_input_unstable", reason=type(exc).__name__, read_only=True), 409
    except (ValueError, KeyError, IndexError, TypeError) as exc:
        return jsonify(ok=False, status="invalid_read_projection", reason=type(exc).__name__, read_only=True), 400


def _start_strategy_screening_worker(request_id: str, params: dict[str, Any]) -> None:
    Thread(
        target=run_strategy_screening_worker,
        kwargs={
            "deps": _STRATEGY_SCREENING_ROUTE_DEPS,
            "request_id": request_id,
            "params": params,
        },
        daemon=True,
    ).start()


def _run_full_market_catchup_for_route(
    root: Path,
    *,
    network: bool,
    max_symbols: int,
    priority_only: bool,
) -> dict[str, Any]:
    return run_startup_data_update(root, enable_network=network)


register_system_routes(app, build_id=SERVER_BUILD_ID, asset_versions_func=daily_research_asset_versions)
register_agent_routes(
    app,
    AgentRouteDeps(
        project_root=lambda: project_root,
        build_agent_chat_context=lambda: _build_agent_chat_context(),
        sanitize_agent_client_context=lambda context: _sanitize_agent_client_context(context),
        attach_focused_candidate_context=lambda context, client_context: _attach_focused_candidate_context(
            context,
            client_context,
        ),
        request_truthy=lambda value: request_truthy(value),
    ),
)
register_backtest_routes(app, candidate_rows_func=lambda limit: candidate_rows(limit=limit))
register_data_routes(app)
register_data_update_routes(
    app,
    DataUpdateRouteDeps(
        project_root=lambda: project_root,
        data_write_lock=lambda: DATA_WRITE_LOCK,
        realtime_network_enabled=lambda: realtime_network_enabled(),
        run_full_market_catchup=lambda root, **kwargs: _run_full_market_catchup_for_route(root, **kwargs),
        run_repair_daily=lambda: run_repair_daily_command(project_root),
    ),
)
register_news_routes(
    app,
    NewsRouteDeps(
        fetch_news=lambda symbol, limit, network: fetch_news(symbol, limit=limit, network=network),
        news_cache_stats=lambda: news_cache_stats(),
        refresh_single_news=lambda symbol: refresh_single_news(symbol),
        normalize_symbol=lambda symbol: normalize_symbol(symbol),
        request_truthy=lambda value: request_truthy(value),
    ),
)
register_limit_up_routes(
    app,
    LimitUpRouteDeps(
        read_limit_up_review_cache=lambda **kwargs: read_limit_up_review_cache(**kwargs),
        refresh_limit_up_review=lambda date_text: refresh_limit_up_review(date_text),
        normalize_review_date=lambda date_text: normalize_review_date(date_text),
        request_truthy=lambda value: request_truthy(value),
    ),
)
register_stock_routes(
    app,
    StockRouteDeps(
        get_stock_detail=lambda symbol: get_stock_detail(symbol),
    ),
)
register_stock_workbench_routes(
    app,
    StockWorkbenchDeps(
        load_original_daily=lambda symbol: load_stock_workbench_daily(symbol),
        cached_quote=lambda symbol: spot_quote_for_symbol(symbol),
        fetch_quote=lambda symbol: fetch_eastmoney_single_quote(symbol),
        research_builder=lambda frame: build_research_reference(frame),
        load_calendar=lambda: load_cn_trading_calendar(project_root / "data/calendar/a_share_trading_calendar.csv"),
        network_enabled=lambda: bool(app.config.get("STOCK_QUOTE_NETWORK_ENABLED", realtime_network_enabled())),
        refresh_allowed=lambda: bool(app.config.get("STOCK_QUOTE_REFRESH_ALLOWED", True)),
        fee_assumptions=stock_reference_fee_assumptions,
        account_context=stock_reference_account_context,
        load_minute_bars=lambda symbol: stock_minute_view(symbol),
    ),
)
register_sector_routes(
    app,
    SectorRouteDeps(
        refresh_industry_board_cache=lambda: refresh_industry_board_cache(),
    ),
)
register_daily_routes(
    app,
    DailyRouteDeps(
        candidate_rows=lambda limit: candidate_rows(limit=limit),
        annotate_recommendation_priorities=lambda rows: annotate_recommendation_priorities(rows),
        daily_focus_rows=lambda rows: daily_focus_rows(rows),
        candidate_groups=lambda rows: candidate_groups(rows),
        quant_daily_summary=lambda rows: quant_daily_summary(rows),
        generation_status_for_ui=lambda: generation_status_for_ui(),
        focus_score=lambda row: focus_score(row),
        candidate_pool_review_policy=lambda rows: candidate_pool_review_policy(rows),
        data_gap_summary=lambda rows: data_gap_summary(rows),
        candidate_confidence_summary=lambda rows: candidate_confidence_summary(rows),
        load_benchmark_index=lambda name: load_benchmark_index(name),
        spot_market_breadth=lambda: spot_market_breadth(),
        generate_market_sentiment=lambda hs_change, sh_change, advancing_count, declining_count: (
            stock_explainer.generate_market_sentiment(hs_change, sh_change, advancing_count, declining_count)
        ),
        calculate_sector_stats=lambda: calculate_sector_stats(),
        market_overview_hint=MARKET_OVERVIEW_HINT,
        pool_context=lambda rows, focus: candidate_pool_context(rows, focus),
    ),
)
register_daily_action_routes(
    app,
    DailyActionRouteDeps(
        save_candidate_review_action=lambda *args, **kwargs: save_candidate_review_action(*args, **kwargs),
    ),
)
register_daily_generation_routes(
    app,
    DailyGenerationRouteDeps(
        project_root=lambda: project_root,
        data_write_lock=lambda: DATA_WRITE_LOCK,
        realtime_network_enabled=lambda: realtime_network_enabled(),
        request_truthy=lambda value: request_truthy(value),
        candidate_rows=lambda limit: candidate_rows(limit=limit),
        update_daily_prices=lambda **kwargs: run_startup_data_update(
            project_root, enable_network=bool(kwargs.get("network")),
        ),
        generate_daily_recommendations=lambda *args, **kwargs: generate_daily_recommendations(*args, **kwargs),
        append_paper_simulation_sync_to_generation_payload=lambda *args, **kwargs: (
            append_paper_simulation_sync_to_generation_payload(*args, **kwargs)
        ),
        data_chain_consumer_gate=lambda: evaluate_data_chain_consumer_gate(
            project_root=project_root,
        ),
    ),
)
register_research_routes(
    app,
    ResearchRouteDeps(
        project_root=lambda: project_root,
        paper_watchlist_json=lambda: PAPER_WATCHLIST_JSON,
        project_relative_path=lambda path: project_relative_path(path),
        latest_research_evidence_context=lambda: latest_research_evidence_context(),
        load_paper_simulation_watchlist=lambda: load_paper_simulation_watchlist(),
        build_strategy_scorecard=lambda: build_strategy_scorecard(),
    ),
)
register_theme_map_routes(
    app,
    ThemeMapRouteDeps(
        stock_theme_map_csv=lambda: STOCK_THEME_MAP_CSV,
        stock_theme_map_import_csv=lambda: STOCK_THEME_MAP_IMPORT_CSV,
        theme_map_status_payload=lambda: theme_map_status_payload(),
        theme_map_preview=lambda limit: theme_map_preview(limit=limit),
        preview_theme_map_import_draft=lambda limit: preview_theme_map_import_draft(limit=limit),
        generate_theme_map_import_draft=lambda **kwargs: generate_theme_map_import_draft(**kwargs),
        import_theme_map=lambda *args, **kwargs: import_theme_map(*args, **kwargs),
        update_theme_map=lambda **kwargs: update_theme_map(**kwargs),
        rebuild_limit_up_review_attribution_cache=lambda: rebuild_limit_up_review_attribution_cache(),
        resolve_project_csv_path=lambda value, default: resolve_project_csv_path(value, default),
        request_truthy=lambda value: request_truthy(value),
    ),
)
register_realtime_routes(
    app,
    RealtimeRouteDeps(
        request_truthy=lambda value: request_truthy(value),
        realtime_network_enabled=lambda: realtime_network_enabled(),
        realtime_market_summary=lambda **kwargs: realtime_market_summary(**kwargs),
        refresh_spot_snapshot_cache=lambda **kwargs: refresh_spot_snapshot_cache(**kwargs),
        search_stock_rows=lambda query, limit: search_stock_rows(query, limit=limit),
        normalize_symbol=lambda symbol: normalize_symbol(symbol),
        refresh_single_quote_cache=lambda symbol: refresh_single_quote_cache(symbol),
        maybe_start_realtime_background_refresh=lambda **kwargs: maybe_start_realtime_background_refresh(**kwargs),
        ensure_single_stock_daily_with_auth=lambda symbol, **kwargs: ensure_single_stock_daily_with_auth(symbol, **kwargs),
        load_stock_data=lambda symbol: load_stock_data(symbol),
        spot_quote_for_symbol=lambda symbol: spot_quote_for_symbol(symbol),
        candidate_name=lambda symbol: candidate_name(symbol),
        latest_indicators=lambda stock_df: latest_indicators(stock_df),
        chart_rows=lambda stock_df: chart_rows(stock_df),
        append_realtime_chart_row=lambda rows, quote: append_realtime_chart_row(rows, quote),
        chart_markers=lambda *args, **kwargs: chart_markers(*args, **kwargs),
        chart_number=lambda *args, **kwargs: chart_number(*args, **kwargs),
        latest_pct_change=lambda stock_df: latest_pct_change(stock_df),
        risk_level_from_indicators=lambda indicators: stock_explainer.risk_level_from_indicators(indicators),
        generate_indicator_explanation=lambda indicators: stock_explainer.generate_indicator_explanation(indicators),
        generate_risk_warning=lambda indicators, row_count: stock_explainer.generate_risk_warning(indicators, row_count),
        build_realtime_observation=lambda stock_df, quote: build_realtime_observation(stock_df, quote),
        build_research_reference=lambda stock_df: build_research_reference(stock_df),
        build_news_summary=lambda symbol: build_news_summary(symbol),
        build_industry_comparison=lambda symbol, stock_pct_chg: build_industry_comparison(symbol, stock_pct_chg),
        add_manual_observation=lambda symbol, **kwargs: add_manual_observation(symbol, **kwargs),
    ),
)
register_paper_simulation_routes(
    app,
    PaperSimulationRouteDeps(
        data_write_lock=lambda: DATA_WRITE_LOCK,
        paper_watchlist_json=lambda: PAPER_WATCHLIST_JSON,
        project_relative_path=lambda path: project_relative_path(path),
        load_paper_simulation_watchlist=lambda: load_paper_simulation_watchlist(),
        paper_research_decision_snapshot=lambda item: paper_research_decision_snapshot(item),
        latest_research_evidence_context=lambda: latest_research_evidence_context(),
        add_candidate_to_paper_simulation_pool=lambda symbol, **kwargs: add_candidate_to_paper_simulation_pool(
            symbol,
            **kwargs,
        ),
        sync_candidates_to_paper_simulation_pool=lambda **kwargs: sync_candidates_to_paper_simulation_pool(**kwargs),
        refresh_paper_simulation_watchlist_tracking=lambda: refresh_paper_simulation_watchlist_tracking(),
        paper_simulation_lifecycle_action=lambda symbol, action, **kwargs: paper_simulation_lifecycle_action(
            symbol,
            action,
            **kwargs,
        ),
        request_truthy=lambda value: request_truthy(value),
        log_warning=lambda *args, **kwargs: LOG.warning(*args, **kwargs),
    ),
)
register_decision_board_routes(
    app,
    DecisionBoardRouteDeps(
        load_watchlist=lambda: load_paper_simulation_watchlist(),
        load_portfolio=lambda: auto_paper_portfolio_latest(),
        load_equity_history=lambda: auto_paper_portfolio_equity_history(limit=250),
        load_benchmark=lambda: load_benchmark_series("hs300", days=500),
        load_profile=lambda: default_research_paper_profile(),
        load_calendar_days=lambda: ui_calendar_days(),
        strategy_version=lambda: APP_VERSION,
        source_paths=lambda: {
            "portfolio": project_relative_path(AUTO_PAPER_PORTFOLIO_LATEST_JSON),
            "watchlist": project_relative_path(PAPER_WATCHLIST_JSON),
            "equity_history": project_relative_path(AUTO_PAPER_PORTFOLIO_HISTORY_JSON),
            "benchmark": project_relative_path(BENCHMARK_DIR / "hs300.csv"),
        },
    ),
)
register_market_brain_routes(
    app,
    MarketBrainRouteDeps(results_dir=project_root / "results"),
)
register_startup_routes(
    app,
    StartupRouteDeps(
        read_startup_payload=lambda: read_json_file(STARTUP_UPDATE_JSON),
        write_startup_status_payload=lambda payload: _write_startup_status_payload(payload),
        generation_status_for_ui=lambda payload=None: generation_status_for_ui(payload),
        startup_refresh_state_payload=lambda: startup_refresh_state_payload(),
        startup_payload_has_completed_after_state=lambda payload, state: startup_payload_has_completed_after_state(payload, state),
        set_startup_refresh_state=lambda **updates: _set_startup_refresh_state(**updates),
        candidate_rows=lambda limit: candidate_rows(limit=limit),
        data_gap_summary=lambda rows: data_gap_summary(rows),
        startup_provider_update_diagnosis=lambda payload: startup_provider_update_diagnosis(payload),
        startup_update_explanation=lambda payload: startup_update_explanation(payload),
        request_truthy=lambda value: request_truthy(value),
        make_request_id=lambda: datetime.now().isoformat(timespec="seconds"),
        start_startup_refresh_worker=lambda reason, request_id: Thread(
            target=_run_startup_refresh_worker,
            kwargs={"reason": reason, "request_id": request_id},
            daemon=True,
        ).start(),
    ),
)
register_performance_routes(
    app,
    PerformanceRouteDeps(
        project_root=lambda: project_root,
        build_candidate_performance=lambda root: __import__(
            "quantdesk.candidate_tracking",
            fromlist=["build_candidate_performance"],
        ).build_candidate_performance(root),
    ),
)
_STRATEGY_SCREENING_ROUTE_DEPS = StrategyScreeningRouteDeps(
    project_root=lambda: project_root,
    task_json=lambda: STRATEGY_SCREENING_TASK_JSON,
    state=lambda: STRATEGY_SCREENING_STATE,
    state_lock=lambda: STRATEGY_SCREENING_LOCK,
    data_write_lock=lambda: DATA_WRITE_LOCK,
    read_json_file=lambda path: read_json_file(path),
    request_truthy=lambda value: request_truthy(value),
    make_request_id=lambda: datetime.now().astimezone().isoformat(timespec="seconds"),
    start_worker=lambda request_id, params: _start_strategy_screening_worker(request_id, params),
)
register_strategy_screening_routes(app, _STRATEGY_SCREENING_ROUTE_DEPS)
register_forward_pit_routes(app, ForwardPitRouteDeps(project_root=project_root))
# Independent AI paper trader: read-only dashboard + explain-only chat.
# No broker route, no order endpoint, no cycle-run control from the web.
register_ai_trader_routes(app)


from scripts.server.helpers.util import (
    finite_float, safe_int, display_number, chart_number, clean_payload,
    request_truthy, resolve_project_csv_path, read_json_file,
)


LOCAL_BIND_HOSTS = {"127.0.0.1", "localhost", "::1"}
REMOTE_BIND_HOSTS = {"0.0.0.0", "::"}


def _resolve_bind_host(host: str | None, allow_remote: bool = False) -> str:
    """Resolve Flask bind host and refuse accidental LAN exposure by default."""
    resolved = str(host or "").strip() or "127.0.0.1"
    if resolved in LOCAL_BIND_HOSTS:
        return resolved
    if allow_remote:
        return resolved
    if resolved in REMOTE_BIND_HOSTS or resolved not in LOCAL_BIND_HOSTS:
        raise ValueError(
            f"Refusing to bind QuantDesk to non-local host {resolved!r}. "
            "Pass --unsafe-allow-remote-access only for a deliberate local-network research session."
        )
    return resolved


import scripts.server.helpers.market as market_helpers
from scripts.server.helpers.market import *  # noqa: F401,F403  spot/realtime/sector/benchmark


def _sync_market_helper_contract() -> None:
    """Keep re-exported market helpers aligned with monkeypatched server globals.

    ``quant_api_server`` historically exposed constants such as ``DAILY_DIR`` and
    provider functions directly.  Tests and a few local tools monkeypatch those
    attributes on this module.  The market helper split should not silently
    bypass that contract by continuing to read the original helper-module
    globals.
    """
    for name in (
        "DAILY_DIR",
        "BENCHMARK_DIR",
        "CACHE_DIR",
        "UNIVERSE_DIR",
        "SPOT_SNAPSHOT_CACHE",
        "SPOT_SNAPSHOT_META",
        "INDUSTRY_BOARD_CACHE",
        "REALTIME_CACHE_MAX_AGE_SECONDS",
        "REALTIME_PROVIDER_RETRY_SECONDS",
    ):
        if name in globals():
            setattr(market_helpers, name, globals()[name])
    for name in ("fetch_eastmoney_spot_direct", "fetch_sina_spot_direct"):
        current = globals().get(name)
        if current is not None:
            setattr(market_helpers, name, current)


_market_load_stock_data = market_helpers.load_stock_data
_market_load_benchmark_index = market_helpers.load_benchmark_index
_market_read_theme_map = market_helpers.read_theme_map
_market_refresh_spot_snapshot_cache = market_helpers.refresh_spot_snapshot_cache


def load_stock_data(symbol: str) -> pd.DataFrame:
    _sync_market_helper_contract()
    path = DAILY_DIR / f"{normalize_symbol(symbol)}.csv"
    def load() -> pd.DataFrame:
        market_helpers._load_stock_data_cached.cache_clear()
        return _market_load_stock_data(symbol)
    return cached_file_value([path], load, scope="ui_daily_frame",
        code_id=str(id(_market_load_stock_data)))


def load_benchmark_index(kind: str) -> dict[str, Any]:
    _sync_market_helper_contract()
    return _market_load_benchmark_index(kind)


def read_theme_map() -> pd.DataFrame:
    _sync_market_helper_contract()
    return _market_read_theme_map()


def refresh_spot_snapshot_cache(*, network: bool, force: bool = False) -> dict[str, Any]:
    _sync_market_helper_contract()
    return _market_refresh_spot_snapshot_cache(network=network, force=force)

def load_candidate_ledger() -> pd.DataFrame:
    ledger_path = TRACKING_DIR / "candidate_ledger.csv"
    if not ledger_path.exists():
        return pd.DataFrame()
    try:
        df = read_csv_frame(ledger_path)
    except Exception as exc:  # noqa: BLE001 - ledger cache failure should be visible and degrade
        LOG.warning("candidate_ledger_read_failed: path=%s error=%s", ledger_path, exc)
        return pd.DataFrame()
    if "entry_date" in df.columns:
        df["entry_date"] = pd.to_datetime(df["entry_date"], errors="coerce")
        df = df.sort_values("entry_date", ascending=False)
    return df


def candidate_name(symbol: str, row: pd.Series | None = None) -> str:
    if row is not None:
        name = str(row.get("stock_name") or row.get("name") or "").strip()
        if name and name.lower() != "nan":
            return name
    listings = UNIVERSE_DIR / "historical_listings.csv"
    if listings.exists():
        try:
            df = cached_file_value([listings], lambda: read_csv_frame(listings,
                usecols=lambda col: col in {"symbol", "code", "ts_code", "name", "stock_name"}), scope="ui_names")
            for col in ("symbol", "code", "ts_code"):
                if col in df.columns:
                    hit = df[df[col].astype(str).map(normalize_symbol) == normalize_symbol(symbol)]
                    if not hit.empty:
                        for name_col in ("name", "stock_name"):
                            if name_col in hit.columns:
                                name = str(hit.iloc[-1].get(name_col) or "").strip()
                                if name and name.lower() != "nan":
                                    return name
        except Exception as exc:  # noqa: BLE001 - symbol-name lookup is best-effort
            LOG.warning("candidate_name_lookup_failed: symbol=%s path=%s error=%s", symbol, listings, exc)
    return normalize_symbol(symbol)


def latest_indicators(stock_df: pd.DataFrame) -> dict[str, Any]:
    indicators = technical_indicators.get_latest_indicator_values(stock_df)
    if stock_df.empty:
        return indicators
    latest = stock_df.iloc[-1]
    for key in ("open", "high", "low", "close", "volume", "amount"):
        if key in latest.index:
            indicators[key] = chart_number(latest.get(key))
    return indicators


def row_entry_date(row: pd.Series | None) -> str:
    if row is None:
        return ""
    value = row.get("entry_date")
    if isinstance(value, pd.Timestamp):
        return value.strftime("%Y-%m-%d")
    text = str(value or "").strip()
    return text[:10] if text and text.lower() != "nan" else ""


def row_strategy_type(row: pd.Series | None) -> str:
    if row is None:
        return ""
    text = str(row.get("strategy_type") or "").strip()
    return {"long": "long_term", "long_swing": "long_term", "board": "da_ban"}.get(
        text, text if text and text.lower() != "nan" else "")


def strategy_label(strategy_type: str) -> str:
    return STRATEGY_LABELS.get(strategy_type, "候选池观察")


def detail_lane(strategy_type: str) -> str:
    if strategy_type == "long_term":
        return "long"
    if strategy_type == "da_ban":
        return "board"
    return "pool"


def data_source_label(row: pd.Series | None) -> str:
    if row is None:
        return "本地日线"
    source = str(row.get("data_source") or "").strip().lower()
    if source == "formal":
        return "正式样本"
    if source == "proxy":
        return "代理样本"
    return "本地样本"


def row_signal_date(row: pd.Series | None) -> str:
    if row is None:
        return ""
    for key in ("signal_date", "entry_date"):
        value = row.get(key)
        if isinstance(value, pd.Timestamp):
            return value.strftime("%Y-%m-%d")
        text = str(value or "").strip()
        if text and text.lower() != "nan":
            return text[:10]
    return ""


def row_candidate_role(row: pd.Series | None) -> str:
    if row is None:
        return ""
    text = str(row.get("candidate_role") or "").strip()
    return text if text and text.lower() != "nan" else ""


def latest_generation_status() -> dict[str, Any]:
    target = expected_settlement_date(to_shanghai_datetime())
    target_text = target.isoformat() if target is not None else ""
    # A single response uses one admitted generation snapshot. The enclosing
    # response cache still rehashes every source before and after construction,
    # so a write during the request rejects the response rather than mixing it.
    request_key = (target_text, str(project_root), id(load_formal_candidate_intake))
    memo = getattr(g, "candidate_generation_snapshot", None) if has_request_context() else None
    if memo is not None and memo[0] == request_key:
        return memo[1]
    paths = _ui_generation_dependency_paths(target_text)
    value = cached_file_value(paths, _latest_generation_status_uncached, scope="ui_candidate_generation",
        target_date=target_text, code_id=str(id(load_formal_candidate_intake)))
    if has_request_context():
        g.candidate_generation_snapshot = (request_key, value)
    return value


def _latest_generation_status_uncached() -> dict[str, Any]:
    target = expected_settlement_date(to_shanghai_datetime())
    target_text = target.isoformat() if target is not None else ""
    formal_dir = project_root / "results/candidate_refresh/daily"
    if target_text and formal_dir.is_dir():
        source_date = target_text
        path = formal_dir / f"{target_text}.json"
        if not path.exists():
            previous = sorted(p.stem for p in formal_dir.glob("????-??-??.json") if p.stem < target_text)
            if previous:
                source_date = previous[-1]
                path = formal_dir / f"{source_date}.json"
        before = hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else ""
        intake = load_formal_candidate_intake(project_root, target_date=source_date)
        batch = intake.get("batch") if intake.get("status") == "passed" else None
        reasons = list(intake.get("reason_codes") or [])
        if batch and any(batch.get(k) is True for k in ("synthetic_test_data", "synthetic", "demo", "smoke_run")):
            reasons.append("candidate_batch_nonformal_mode")
        actual_path = Path(intake.get("path") or path)
        after = hashlib.sha256(actual_path.read_bytes()).hexdigest() if actual_path.is_file() else ""
        if actual_path == path and before != after:
            reasons.append("candidate_batch_changed_during_read")
        if reasons:
            batch = None
        candidates = list((batch or {}).get("candidates") or [])
        counts: dict[str, int] = {}
        for row in candidates:
            kind = row_strategy_type(pd.Series(row))
            counts[kind] = counts.get(kind, 0) + 1
        identity = {"target_date": source_date, "source_run_id": intake.get("source_run_id"),
            "batch_provenance_hash": intake.get("provenance_hash"), "source_path": str(actual_path),
            "source_sha256": after, "source_mode": intake.get("source_mode", "dated_verified_formal_batch")}
        return {"status": "passed" if batch else "blocked" if reasons else intake.get("status", "missing"),
            "signal_date": source_date if batch else "", "target_date": target_text,
            "candidate_count": len(candidates), "strategy_counts": counts, "source": "formal_candidate_daily_batch",
            "formal_current": bool(batch and source_date == target_text), "formal_rows": candidates,
            "batch_identity": identity, "reason_codes": reasons,
            "generated_batch": (batch or {}).get("generated_batch", ""),
            "message_for_ui": (f"已读取 {source_date} 正式研究候选 {len(candidates)} 只 · 纸面 · 未验证。"
                               if batch else "目标日正式研究候选缺失或未核实，保留历史观察。")}
    payload = read_json_file(DAILY_CANDIDATE_GENERATION_JSON)
    if not payload:
        return {
            "status": "missing",
            "signal_date": "",
            "candidate_count": 0,
            "new_ledger_rows": 0,
            "message_for_ui": "尚未生成今日研究候选；启动时会自动生成，也可以手动点击生成。",
        }
    payload.setdefault("message_for_ui", "已读取最近一次每日研究候选生成状态。")
    return payload


def _path_mtime(path: Path) -> float | None:
    try:
        return path.stat().st_mtime
    except OSError:
        return None


def latest_generation_ledger_symbols(signal_date: str) -> set[str]:
    """Return the authoritative latest daily recommendation symbols from ledger."""
    signal_date = str(signal_date or "")[:10]
    if not signal_date:
        return set()
    ledger = load_candidate_ledger()
    if ledger.empty:
        return set()
    symbol_col = "ts_code" if "ts_code" in ledger.columns else "symbol" if "symbol" in ledger.columns else ""
    if not symbol_col:
        return set()
    mask = pd.Series([True] * len(ledger), index=ledger.index)
    if "candidate_role" in ledger.columns:
        mask &= ledger["candidate_role"].astype(str).str.strip().eq("daily_recommendation")
    date_mask = pd.Series([False] * len(ledger), index=ledger.index)
    for column in ("signal_date", "entry_date"):
        if column in ledger.columns:
            date_mask |= ledger[column].astype(str).str[:10].eq(signal_date)
    mask &= date_mask
    filtered = ledger[mask]
    if filtered.empty:
        return set()
    return {
        normalize_symbol(str(value or ""))
        for value in filtered[symbol_col].tolist()
        if normalize_symbol(str(value or ""))
    }


def latest_generation_symbols(payload: dict[str, Any] | None = None) -> set[str]:
    raw = payload if isinstance(payload, dict) else latest_generation_status()
    if raw.get("source") == "formal_candidate_daily_batch":
        return {normalize_symbol(row.get("symbol")) for row in raw.get("formal_rows") or []} if raw.get("formal_current") else set()
    signal_date = str(raw.get("signal_date") or "")[:10]
    # Legacy in-memory symbol lookup must obey the same byte contract too.
    # A same-size rewrite with a preserved mtime is still a new batch input.
    ledger_mtime = byte_identity([TRACKING_DIR / "candidate_ledger.csv"])["sha256"]
    csv_mtime = byte_identity([DAILY_CANDIDATE_LATEST_CSV])["sha256"]
    if (
        _LATEST_GENERATION_SYMBOL_CACHE.get("csv_mtime") == csv_mtime
        and _LATEST_GENERATION_SYMBOL_CACHE.get("ledger_mtime") == ledger_mtime
        and _LATEST_GENERATION_SYMBOL_CACHE.get("signal_date") == signal_date
    ):
        cached = _LATEST_GENERATION_SYMBOL_CACHE.get("symbols")
        return set(cached) if isinstance(cached, set) else set()

    ledger_symbols = latest_generation_ledger_symbols(signal_date)
    if ledger_symbols:
        _LATEST_GENERATION_SYMBOL_CACHE.update(
            {
                "csv_mtime": csv_mtime,
                "ledger_mtime": ledger_mtime,
                "signal_date": signal_date,
                "symbols": ledger_symbols,
            }
        )
        return set(ledger_symbols)

    if not DAILY_CANDIDATE_LATEST_CSV.exists():
        _LATEST_GENERATION_SYMBOL_CACHE.update(
            {"csv_mtime": csv_mtime, "ledger_mtime": ledger_mtime, "signal_date": signal_date, "symbols": set()}
        )
        return set()
    try:
        df = read_csv_frame(DAILY_CANDIDATE_LATEST_CSV)
    except Exception as exc:  # noqa: BLE001 - latest generation cache is optional
        LOG.warning("latest_generation_csv_read_failed: path=%s error=%s", DAILY_CANDIDATE_LATEST_CSV, exc)
        return set()
    if df.empty:
        return set()
    symbol_col = "symbol" if "symbol" in df.columns else "ts_code" if "ts_code" in df.columns else ""
    if not symbol_col:
        return set()
    if signal_date and "signal_date" in df.columns:
        df = df[df["signal_date"].astype(str).str[:10] == signal_date]
    if "candidate_role" in df.columns:
        role_mask = df["candidate_role"].astype(str).str.strip().eq("daily_recommendation")
        if role_mask.any():
            df = df[role_mask]
    symbols = {
        normalize_symbol(str(value or ""))
        for value in df[symbol_col].tolist()
        if normalize_symbol(str(value or ""))
    }
    _LATEST_GENERATION_SYMBOL_CACHE.update(
        {"csv_mtime": csv_mtime, "ledger_mtime": ledger_mtime, "signal_date": signal_date, "symbols": symbols}
    )
    return set(symbols)


def generation_status_for_ui(payload: dict[str, Any] | None = None) -> dict[str, Any]:
    raw = payload if isinstance(payload, dict) else latest_generation_status()
    strategy_counts = raw.get("strategy_counts") if isinstance(raw.get("strategy_counts"), dict) else {}
    signal_date = str(raw.get("signal_date") or "")[:10]
    target_date = expected_settlement_date(to_shanghai_datetime())
    target_text = target_date.isoformat() if target_date is not None else ""
    freshness_status = "missing"
    freshness_label = "未生成"
    freshness_message = "尚未生成每日研究候选；需要先更新本地日线或手动生成研究候选。"
    needs_refresh = True
    if signal_date and target_text and signal_date < target_text:
        freshness_status = "stale"
        freshness_label = "研究候选批次滞后"
        freshness_message = (
            f"最新研究候选批次停在 {signal_date}，目标完整交易日为 {target_text}；"
            "当前列表只能视为最近一批研究重点，需要联网更新或重新生成后才是目标交易日研究候选。"
        )
    elif signal_date and target_text and signal_date > target_text:
        freshness_status = "future"
        freshness_label = "研究候选日期晚于目标日"
        freshness_message = f"批次日期 {signal_date} 晚于目标完整交易日 {target_text}，不可当作今日候选。"
    elif signal_date and target_text and signal_date == target_text:
        freshness_status = "current"
        freshness_label = "研究候选批次已覆盖目标交易日"
        needs_refresh = False
        freshness_message = f"最新研究候选批次为 {signal_date}，已覆盖目标完整交易日 {target_text or signal_date}。"
    return {
        "status": raw.get("status", "missing"),
        "signal_date": signal_date,
        "target_date": target_text,
        "freshness_status": freshness_status,
        "freshness_label": freshness_label,
        "freshness_message": freshness_message,
        "needs_refresh": needs_refresh,
        "generated_batch": raw.get("generated_batch", ""),
        "candidate_count": raw.get("candidate_count", 0),
        "new_ledger_rows": raw.get("new_ledger_rows", 0),
        "metrics_scanned": raw.get("metrics_scanned", 0),
        "scope_label": "本地日线有界研究扫描",
        "long_count": strategy_counts.get("long_term", 0),
        "board_count": strategy_counts.get("da_ban", 0),
        "message_for_ui": raw.get("message_for_ui", "尚未生成今日研究候选。"),
        "warning": raw.get("warning", ""),
        "source": raw.get("source", "legacy_generation"),
        "batch_identity": raw.get("batch_identity", {}),
        "reason_codes": raw.get("reason_codes", []),
        "boundary": "Research Only；不扫描全 A；不接券商；不下真实订单；Not Investment Advice；Not Position Advice。",
    }


def _startup_time_value(value: Any) -> float | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        return parsed.timestamp()
    except ValueError:
        return None


def startup_payload_has_completed_after_state(payload: dict[str, Any], state: dict[str, Any]) -> bool:
    """Recover a stuck in-memory running flag after the worker has written a terminal payload."""
    if not bool(state.get("running")):
        return False
    status = str(payload.get("status") or "").strip()
    if status not in STARTUP_TERMINAL_STATUSES:
        return False
    payload_time = _startup_time_value(payload.get("updated_at") or payload.get("finished_at") or payload.get("generated_at"))
    state_time = _startup_time_value(state.get("started_at") or state.get("request_id"))
    if payload_time is None or state_time is None:
        return False
    return payload_time >= state_time


def startup_provider_update_diagnosis(payload: dict[str, Any]) -> dict[str, Any]:
    update_result = payload.get("update_result") if isinstance(payload.get("update_result"), dict) else {}
    provider_results = update_result.get("provider_results")
    if not isinstance(provider_results, list):
        provider_results = []
    target_date = str(payload.get("target_date") or update_result.get("target_date") or "")[:10]
    latest_local = str(
        payload.get("latest_local_date_after")
        or update_result.get("latest_local_date_after")
        or update_result.get("latest_local_date")
        or ""
    )[:10]
    updated_symbols = safe_int(payload.get("updated_symbols") or update_result.get("updated_symbols"), 0)
    failed_symbols = safe_int(payload.get("failed_symbols") or update_result.get("failed_symbols"), 0)
    stale_count = safe_int(payload.get("stale_count_after") or update_result.get("stale_count_after"), 0)
    missing_count = safe_int(payload.get("missing_count_after") or update_result.get("missing_count_after"), 0)
    provider_summaries: list[dict[str, Any]] = []
    failed_symbol_names: list[str] = []
    best_failed_symbol_names: list[str] = []
    best_provider = ""
    best_updated = 0
    failed_reason = ""
    for item in provider_results:
        if not isinstance(item, dict):
            continue
        provider = str(item.get("provider") or "")
        status = str(item.get("status") or "")
        updated = safe_int(item.get("updated") or item.get("updated_symbols"), 0)
        failed = safe_int(item.get("failed") or item.get("failed_symbols"), 0)
        latest_date = str(item.get("latest_date") or "")[:10]
        reason = str(item.get("failed_reason") or item.get("message") or "")
        symbols = item.get("failed_symbol_list") if isinstance(item.get("failed_symbol_list"), list) else []
        provider_failed_symbols: list[str] = []
        for symbol in symbols:
            normalized = normalize_symbol(str(symbol or ""))
            if normalized:
                provider_failed_symbols.append(normalized)
        if updated > best_updated:
            best_updated = updated
            best_provider = provider
            best_failed_symbol_names = list(dict.fromkeys(provider_failed_symbols))
        if failed and not failed_reason and reason:
            failed_reason = reason
        for normalized in provider_failed_symbols:
            if normalized not in failed_symbol_names:
                failed_symbol_names.append(normalized)
        provider_summaries.append(
            {
                "provider": provider,
                "status": status,
                "updated": updated,
                "failed": failed,
                "latest_date": latest_date,
                "reason": reason,
            }
        )
    if updated_symbols and (failed_symbols or stale_count or missing_count):
        tone = "partial"
        label = "自动刷新已部分完成"
        display_failed_symbols = best_failed_symbol_names or failed_symbol_names
        failed_list = "、".join(display_failed_symbols[:6]) if display_failed_symbols else f"{failed_symbols or stale_count} 只"
        summary = (
            f"页面打开后已自动刷新到目标日 {target_date or '数据缺失'}："
            f"{best_provider or 'provider'} 成功更新 {updated_symbols} 只；"
            f"仍有 {failed_symbols or stale_count or missing_count} 只未拿到目标日完整日线"
            f"（{failed_list}）。今日重点可用；未更新样本只能降级观察/复盘。"
        )
        next_action = "稍后再次刷新研究闭环；如果同一批股票连续失败，再查看 provider 失败原因或手动导入 CSV。"
    elif updated_symbols and not (failed_symbols or stale_count or missing_count):
        tone = "ok"
        label = "自动刷新完成"
        summary = f"当前研究池已更新到 {target_date or latest_local or '最新可用日线'}，今日重点和纸面模拟池可以继续复盘。"
        next_action = "查看今日重点、LLM 研究室和策略成绩单。"
    elif target_date and latest_local and latest_local < target_date:
        tone = "warn"
        label = "provider 尚未返回目标日完整日线"
        summary = (
            f"系统已尝试刷新目标日 {target_date}，但本地最新完整日线仍停在 {latest_local}。"
            f"{(' 主要失败原因：' + failed_reason) if failed_reason else ''}"
        )
        next_action = "稍后重试，或导入目标日 CSV；在此之前正式研究层继续使用最新可用完整日线。"
    else:
        tone = "unknown"
        label = "刷新结果待确认"
        summary = str(payload.get("message") or update_result.get("message") or "尚未读取到 provider 结果明细。")
        next_action = "查看数据状态或重新刷新研究闭环。"
    return {
        "tone": tone,
        "label": label,
        "summary": summary,
        "next_action": next_action,
        "target_date": target_date,
        "latest_local_date": latest_local,
        "updated_symbols": updated_symbols,
        "failed_symbols": failed_symbols,
        "stale_count": stale_count,
        "missing_count": missing_count,
        "failed_symbols_sample": (best_failed_symbol_names or failed_symbol_names)[:12],
        "provider_summaries": provider_summaries,
    }


def startup_update_explanation(payload: dict[str, Any]) -> dict[str, Any]:
    """Explain page-open data freshness in terms a non-engineer can act on."""
    status = str(payload.get("status") or "unknown")
    attempted = bool(payload.get("startup_update_attempted"))
    network = bool(payload.get("provider_network_authorized") or payload.get("network"))
    generation = payload.get("candidate_generation") if isinstance(payload.get("candidate_generation"), dict) else {}
    summary = payload.get("current_candidate_gap_summary") if isinstance(payload.get("current_candidate_gap_summary"), dict) else {}
    update_result = payload.get("update_result") if isinstance(payload.get("update_result"), dict) else {}
    target_date = str(payload.get("target_date") or generation.get("target_date") or summary.get("target_date") or "")[:10]
    latest_local = str(
        payload.get("current_candidate_latest_date_min")
        or payload.get("latest_local_date_after")
        or update_result.get("latest_local_date_after")
        or ""
    )[:10]
    signal_date = str(generation.get("signal_date") or "")[:10]
    gap_count = safe_int(summary.get("gap_count") if summary else payload.get("failed_symbols"), 0)
    stale_count = safe_int(summary.get("stale_count"), 0) if summary else safe_int(payload.get("stale_count_after"), 0)
    missing_count = safe_int(summary.get("missing_count"), 0) if summary else safe_int(payload.get("missing_count_after"), 0)
    field_gap_count = safe_int(summary.get("field_gap_count"), 0) if summary else 0
    quality_warning_count = safe_int(summary.get("quality_warning_count"), 0) if summary else 0
    price_layer_ready = bool(summary.get("price_layer_ready")) if summary else False
    provider_diagnosis = startup_provider_update_diagnosis(payload)
    reason_code = "ready"
    reason_label = "数据已可用于当前研究页"
    summary_text = "当前候选池价格/K线和研究候选批次状态已可读；继续按纸面研究流程复盘。"
    next_action = "直接查看今日重点、纸面模拟池和策略成绩单。"
    if status == "running":
        reason_code = "running"
        reason_label = "正在后台更新"
        summary_text = "页面打开后已触发后台数据更新，当前显示仍可能来自本地缓存。"
        next_action = "等待状态刷新完成；完成后页面会重新读取今日重点和纸面池。"
    elif status in {"failed", "timeout", "timed_out", "interrupted", "aborted", "blocked", "provider_failed", "cancelled"}:
        reason_code = "interrupted" if status in {"interrupted", "aborted", "cancelled"} else "provider_failed"
        reason_label = "上次更新中断" if reason_code == "interrupted" else "更新失败或超时"
        summary_text = str(payload.get("message") or "该次更新没有完成，已保留原诊断和本地数据。")
        next_action = "查看这次更新的失败明细；收盘后由全 A 数据更新流程继续处理。"
    elif status in {"unknown", "unreadable", "not_run", "missing", "skipped", "network_disabled"}:
        reason_code = "record_unverified" if status in {"unknown", "unreadable"} else "not_attempted"
        reason_label = "更新记录尚未核实" if reason_code == "record_unverified" else "本次未执行更新"
        summary_text = str(payload.get("message") or "没有可核实的本次更新完成记录；当前数据仅作本地观察。")
        next_action = "查看全 A 收盘更新状态和最近一次成功记录。"
    elif not attempted:
        reason_code = "not_attempted"
        reason_label = "本次打开未执行自动更新"
        summary_text = "没有启动更新记录；通常是没有通过带自动更新参数的启动入口打开，或服务刚初始化。"
        next_action = "点击数据状态里的更新按钮，或使用带 update-on-start 的启动器打开。"
    elif not network:
        reason_code = "network_not_authorized"
        reason_label = "未授权联网刷新"
        summary_text = "本次启动没有显式授权外部 provider，系统只能读取本地缓存和 CSV。"
        next_action = "盘中使用显式报价刷新；完整日线由全 A 收盘更新流程处理。"
    elif status in {"failed", "timeout", "blocked"}:
        reason_code = "provider_failed"
        reason_label = "provider 更新失败或超时"
        summary_text = str(payload.get("message") or "外部数据源返回失败、超时或被本地网络限制；已保留本地缓存。")
        next_action = "查看 provider 失败明细；完整日线由全 A 收盘更新流程处理。"
    elif generation.get("freshness_status") == "stale":
        reason_code = "recommendation_stale"
        reason_label = "研究候选批次滞后"
        summary_text = str(generation.get("freshness_message") or "本地日线可能可读，但最新研究候选批次还没有覆盖目标交易日。")
        next_action = "重新生成每日研究候选；生成后再同步纸面模拟池并刷新策略成绩单。"
    elif stale_count > 0 or missing_count > 0:
        reason_code = "provider_partial_daily_gap" if provider_diagnosis.get("tone") == "partial" else "daily_gap"
        reason_label = str(provider_diagnosis.get("label") or "候选日线仍有缺口")
        summary_text = str(provider_diagnosis.get("summary") or summary.get("message") or "部分候选缺少完整日线或本地 CSV，不能做完整策略验证。")
        next_action = str(provider_diagnosis.get("next_action") or "联网更新当前研究池，或导入缺失股票的日线 CSV。")
    elif gap_count > 0 and price_layer_ready:
        reason_code = "quality_warning"
        reason_label = "价格日线已更新，存在质量提示"
        summary_text = str(summary.get("message") or "最新价格/K线已覆盖目标交易日，但存在异常历史行过滤等质量提示。")
        next_action = "可以继续研究今日重点；点开数据状态查看质量提示，必要时修复对应 CSV。"
    elif gap_count > 0:
        reason_code = "quality_warning"
        reason_label = "价格日线已更新，存在质量提示"
        summary_text = str(summary.get("message") or "最新价格/K线已覆盖目标交易日，但存在质量提示。")
        next_action = "可以继续研究今日重点；点开数据状态查看质量提示，必要时修复对应 CSV。"
    elif field_gap_count > 0:
        reason_code = "field_gap"
        reason_label = "价格日线可用，量能字段降级"
        summary_text = f"价格/K线已可读，但 {field_gap_count} 只候选缺成交额/量能字段；量能解释会降级。"
        next_action = "可以先看价格/K线研究；需要量能判断时补齐 amount/volume 字段。"
    elif quality_warning_count > 0:
        reason_code = "quality_warning"
        reason_label = "价格日线已更新，存在质量提示"
        summary_text = str(summary.get("message") or "价格/K线可读，但存在数据质量提示。")
        next_action = "可以继续研究今日重点；质量提示仅说明部分历史行或辅助字段需要后续修复。"
    return {
        "reason_code": reason_code,
        "reason_label": reason_label,
        "summary": summary_text,
        "next_action": next_action,
        "target_date": target_date or "数据缺失",
        "latest_local_date": latest_local or "数据缺失",
        "recommendation_signal_date": signal_date or "数据缺失",
        "network_authorized": network,
        "startup_update_attempted": attempted,
        "status": status,
        "provider_diagnosis": provider_diagnosis,
        "research_only": True,
        "paper_only": True,
        "no_broker": True,
    }


def stock_data_quality(stock_df: pd.DataFrame, symbol: str) -> dict[str, Any]:
    target_date = expected_settlement_date(to_shanghai_datetime())
    target_text = target_date.isoformat() if target_date is not None else ""
    if stock_df.empty:
        return {
            "status": "缺少日线",
            "tone": "missing",
            "latest_date": "数据缺失",
            "target_date": target_text or "数据缺失",
            "row_count": 0,
            "missing_reasons": [f"缺少 data/daily/{normalize_symbol(symbol)}.csv 或文件没有有效收盘价。"],
            "hint": UPDATE_HINT,
        }

    latest = stock_df.iloc[-1]
    latest_date = latest.get("date")
    latest_text = latest_date.strftime("%Y-%m-%d") if isinstance(latest_date, pd.Timestamp) else str(latest_date or "")
    reasons: list[str] = []
    amount_missing = False
    stale_to_target = False
    bad_ohlc_rows = int(stock_df.attrs.get("bad_ohlc_rows_filtered") or 0)
    if bad_ohlc_rows:
        reasons.append(f"已过滤 {bad_ohlc_rows} 行异常 OHLC 日线，指标基于剩余有效价格计算。")
    if len(stock_df) < 60:
        reasons.append(f"有效日线只有 {len(stock_df)} 条，长期指标和回看验证会降级。")
    if "amount" not in stock_df.columns:
        amount_missing = True
        reasons.append("价格日线可用，但文件缺少成交额列，成交额相关解释降级。")
    elif finite_float(latest.get("amount")) is None:
        amount_missing = True
        reasons.append("价格日线已更新，但最新一行成交额缺失，成交额相关解释降级。")
    stale_days = 0
    if isinstance(latest_date, pd.Timestamp):
        stale_days = max(0, (datetime.now().date() - latest_date.date()).days)
        if target_text and latest_text and latest_text < target_text:
            stale_to_target = True
            reasons.append(f"最新日线停在 {latest_text}，目标完整交易日为 {target_text}，需要联网更新或导入 CSV。")
    if not reasons:
        status = "数据完整"
        tone = "ok"
        hint = "本地日线、价格和指标可正常读取。"
    elif stale_to_target:
        status = "日线滞后"
        tone = "warn"
        hint = "；".join(reasons)
    elif amount_missing and len(reasons) == 1:
        status = "成交额缺失"
        tone = "field_gap"
        hint = "价格、涨跌幅和技术指标可用；成交额相关解释会降级。"
    else:
        status = "字段缺失"
        tone = "warn"
        hint = "；".join(reasons)
    return {
        "status": status,
        "tone": tone,
        "latest_date": latest_text or "数据缺失",
        "target_date": target_text or "数据缺失",
        "row_count": int(len(stock_df)),
        "stale_days": stale_days,
        "missing_reasons": reasons or ["未发现关键缺口。"],
        "hint": hint,
    }


def strategy_expected_path(strategy_type: str) -> str:
    if strategy_type == "da_ban":
        return "打板观察：入选后1到5个交易日看量价是否延续；不能延续时降级为普通观察样本。"
    if strategy_type == "long_term":
        return "长期观察：入选后10到20个交易日看趋势修复是否延续；未站稳均线时保留为只观察样本。"
    return "候选池观察：先确认本地数据完整，再做后续复核。"


def strategy_review_offset(strategy_type: str) -> int:
    if strategy_type == "da_ban":
        return 3
    if strategy_type == "long_term":
        return 10
    return 5


def build_strategy_tracking(stock_df: pd.DataFrame, row: pd.Series | None, indicators: dict[str, Any]) -> dict[str, Any]:
    strategy_type = row_strategy_type(row)
    entry_date = row_entry_date(row)
    if not entry_date or stock_df.empty or "date" not in stock_df.columns:
        return {
            "entry_date": entry_date or "数据缺失",
            "entry_price": "数据缺失",
            "latest_date": "数据缺失",
            "latest_price": "数据缺失",
            "elapsed_trading_days": 0,
            "return_since_select": "数据缺失",
            "max_drawdown_since_select": "数据缺失",
            "status_label": "数据不足",
            "status_tone": "missing",
            "expected_path": strategy_expected_path(strategy_type),
            "review_note": "缺少入选日期或本地日线，暂时不能验证入选后的走势是否符合观察假设。",
            "review_offset_days": strategy_review_offset(strategy_type),
            "review_date": "数据缺失",
        }

    try:
        entry_ts = pd.to_datetime(entry_date)
    except Exception as exc:  # noqa: BLE001 - invalid ledger entry date should degrade per candidate
        LOG.warning("candidate_tracking_entry_date_parse_failed: entry_date=%r error=%s", entry_date, exc)
        entry_ts = pd.NaT
    if pd.isna(entry_ts):
        history = pd.DataFrame()
    else:
        history = stock_df[stock_df["date"] >= entry_ts].copy()

    latest = stock_df.iloc[-1]
    latest_date = latest.get("date").strftime("%Y-%m-%d") if isinstance(latest.get("date"), pd.Timestamp) else str(latest.get("date", ""))
    latest_price = finite_float(latest.get("close"))
    entry_price = finite_float(row.get("entry_price") if row is not None else None)
    if entry_price is None and not history.empty:
        entry_price = finite_float(history.iloc[0].get("close"))

    if history.empty or entry_price in (None, 0) or latest_price is None:
        return {
            "entry_date": entry_date,
            "entry_price": display_number(entry_price, 2) if entry_price is not None else "数据缺失",
            "latest_date": latest_date or "数据缺失",
            "latest_price": display_number(latest_price, 2) if latest_price is not None else "数据缺失",
            "elapsed_trading_days": 0,
            "return_since_select": "数据缺失",
            "max_drawdown_since_select": "数据缺失",
            "status_label": "数据不足",
            "status_tone": "missing",
            "expected_path": strategy_expected_path(strategy_type),
            "review_note": "入选日之后没有足够的本地日线，暂时不能验证观察假设。",
            "review_offset_days": strategy_review_offset(strategy_type),
            "review_date": latest_date or "数据缺失",
        }

    closes = [finite_float(value) for value in history["close"].tolist()]
    clean_closes = [float(value) for value in closes if value is not None]
    elapsed = max(0, len(history) - 1)
    ret = (latest_price - entry_price) / entry_price * 100
    peak = entry_price
    max_drawdown = 0.0
    for close in clean_closes:
        peak = max(peak, close)
        if peak:
            max_drawdown = min(max_drawdown, (close / peak - 1) * 100)

    ma20 = finite_float(indicators.get("ma20"))
    status_label = "观察中"
    status_tone = "watch"
    if strategy_type == "da_ban":
        if elapsed >= 5 and (ret < 0 or max_drawdown <= -8):
            status_label = "偏离观察假设"
            status_tone = "warn"
        elif elapsed >= 1 and ret >= 2 and max_drawdown > -8:
            status_label = "阶段符合观察假设"
            status_tone = "ok"
        elif elapsed >= 3:
            status_label = "尚未确认"
    elif strategy_type == "long_term":
        if elapsed >= 10 and ma20 is not None and latest_price < ma20 and ret < -5:
            status_label = "偏离观察假设"
            status_tone = "warn"
        elif elapsed >= 5 and ma20 is not None and latest_price >= ma20 and ret >= 0:
            status_label = "阶段符合观察假设"
            status_tone = "ok"
        elif elapsed >= 5:
            status_label = "尚未确认"
    else:
        if elapsed >= 5 and ret >= 0:
            status_label = "阶段符合观察假设"
            status_tone = "ok"
        elif elapsed >= 5 and ret < 0:
            status_label = "尚未确认"

    review_offset = strategy_review_offset(strategy_type)
    review_row_index = min(review_offset, max(0, len(history) - 1))
    review_date = history.iloc[review_row_index].get("date")
    review_date_text = review_date.strftime("%Y-%m-%d") if isinstance(review_date, pd.Timestamp) else latest_date
    if elapsed <= 0:
        # Just-selected today: no trading day has elapsed since selection, so the
        # interval return / drawdown are not 0% "results" — there is simply
        # nothing to track yet. Say that plainly instead of showing 0.00%.
        review_note = (
            f"今日（{latest_date}）刚入选，入选后还没有新的交易日，暂无区间表现与回撤数据。"
            "从下一个交易日起，这里会显示入选以来的涨跌、最大回撤和复核结论。"
        )
    else:
        review_note = (
            f"从 {entry_date} 入选到 {latest_date} 已经过 {elapsed} 个交易日，"
            f"区间变化 {ret:.2f}%，最大回撤 {max_drawdown:.2f}%。"
        )
        if status_label == "阶段符合观察假设":
            review_note += " 当前结果与入选时的观察假设方向一致，但仍只作为研究复盘。"
        elif status_label == "偏离观察假设":
            review_note += " 当前结果与入选时的观察假设不一致，需要降级复盘原因。"
        else:
            review_note += " 当前证据还不足以判断观察假设是否成立。"

    return {
        "entry_date": entry_date,
        "entry_price": round(float(entry_price), 4),
        "latest_date": latest_date or "数据缺失",
        "latest_price": round(float(latest_price), 4),
        "elapsed_trading_days": int(elapsed),
        "return_since_select": round(float(ret), 2),
        "max_drawdown_since_select": round(float(max_drawdown), 2),
        "status_label": status_label,
        "status_tone": status_tone,
        "expected_path": strategy_expected_path(strategy_type),
        "review_note": review_note,
        "review_offset_days": review_offset,
        "review_date": review_date_text or latest_date or "数据缺失",
    }


def classify_candidate_lane(strategy_type: str, indicators: dict[str, Any], tracking: dict[str, Any], quality: dict[str, Any]) -> tuple[str, str, str]:
    if strategy_type == "da_ban":
        return "打板观察", "打板复核", "board"
    if strategy_type != "long_term":
        return "候选池观察", "普通观察", "general"
    close = finite_float(indicators.get("close"))
    ma20 = finite_float(indicators.get("ma20"))
    dif = finite_float(indicators.get("dif"), 0.0) or 0.0
    dea = finite_float(indicators.get("dea"), 0.0) or 0.0
    return_since_select = finite_float(tracking.get("return_since_select"))
    data_ok = quality.get("tone") in {"ok", "field_gap"}
    trend_ok = close is not None and ma20 is not None and close >= ma20 and dif >= dea
    follow_through = return_since_select is not None and return_since_select >= 0 and tracking.get("status_tone") != "warn"
    if data_ok and (trend_ok or follow_through):
        return "长期观察", "长期-波段复核", "long_swing"
    return "长期观察", "长期-只观察", "long_observe"


def selection_reason_from_row(row: pd.Series | None, indicators: dict[str, Any]) -> str:
    if row is not None:
        signal = str(row.get("entry_signal") or "").strip()
        if signal and signal.lower() != "nan":
            if "突破" in signal:
                return "量价突破观察"
            if "修复" in signal:
                return "趋势修复观察"
            return signal.replace("研究信号", "观察")
    dif = finite_float(indicators.get("dif"), 0.0) or 0.0
    dea = finite_float(indicators.get("dea"), 0.0) or 0.0
    return "MACD金叉观察" if dif >= dea else "候选池复盘观察"


def _row_matches_formal_generation(row: pd.Series | None, generation: dict[str, Any]) -> bool:
    if generation.get("source") != "formal_candidate_daily_batch":
        return True
    identity = generation.get("batch_identity") or {}
    return bool(row is not None and generation.get("formal_current") is True
        and str(row.get("source_run_id") or "") == str(identity.get("source_run_id") or "")
        and str(row.get("source_hash") or "") == str(identity.get("batch_provenance_hash") or ""))


def candidate_exposure_evidence(row: pd.Series | None) -> dict[str, Any]:
    """Keep recorded industry/theme facts without inferring missing mappings."""
    unknown = {"", "nan", "<na>", "none", "null", "未知", "未知行业", "未知题材", "未知板块", "数据缺失"}
    def recorded(key: str) -> str:
        raw = row.get(key) if row is not None else None
        value = str(raw).strip() if raw is not None else ""
        return "" if value.lower() in unknown else value
    values = {key: recorded(key) for key in ("industry", "sector", "theme", "primary_theme", "theme_tags")}
    industry_known = bool(values["industry"] or values["sector"])
    theme_known = any(values[key] for key in ("theme", "primary_theme", "theme_tags"))
    return {**values, "exposure_evidence": {
        "industry_status": "recorded" if industry_known else "unknown",
        "industry_source": recorded("industry_source") or ("candidate_record" if industry_known else ""),
        "industry_as_of": recorded("industry_as_of"),
        "theme_status": "recorded" if theme_known else "unknown",
        "theme_source": recorded("theme_source") or ("candidate_record" if theme_known else ""),
        "unknown_does_not_prove_diversification": True,
        "selection_note": "未知行业/题材不能视为独立分散；沿用原筛选分组，未调整排序和权重。"}}


def build_candidate(symbol: str, row: pd.Series | None = None, *, generation: dict[str, Any] | None = None) -> dict[str, Any]:
    stock_df = load_stock_data(symbol)
    clean_symbol = normalize_symbol(symbol)
    name = candidate_name(symbol, row)
    entry_date = row_entry_date(row)
    signal_date = row_signal_date(row)
    candidate_role = row_candidate_role(row)
    generation = generation if generation is not None else latest_generation_status()
    generation_signal_date = str(generation.get("signal_date") or "")[:10]
    generation_symbols = latest_generation_symbols(generation)
    is_today_recommendation = bool(
        candidate_role == "daily_recommendation"
        and signal_date
        and generation_signal_date
        and signal_date == generation_signal_date
        and (not generation_symbols or clean_symbol in generation_symbols)
        and _row_matches_formal_generation(row, generation)
    )
    candidate_origin_label = "今日新候选" if is_today_recommendation else "历史观察"
    strategy_type = row_strategy_type(row)
    strategy_name = strategy_label(strategy_type)
    quality = stock_data_quality(stock_df, symbol)
    if stock_df.empty or len(stock_df) < 20:
        tracking = build_strategy_tracking(stock_df, row, {})
        strategy_bucket, strategy_lane, group_key = classify_candidate_lane(strategy_type, {}, tracking, quality)
        return {
            **candidate_exposure_evidence(row),
            "symbol": clean_symbol,
            "name": name,
            "current_price": "数据缺失",
            "pct_chg": "数据缺失",
            "reason_cn": "本地历史数据不足，暂时只能保留为观察样本。请先运行数据更新，补齐至少20个交易日后再查看完整理由。",
            "macd_val": "数据缺失",
            "kdj_val": "数据缺失",
            "rsi_val": "数据缺失",
            "risk_level": "中",
            "select_date": entry_date,
            "signal_date": signal_date or entry_date,
            "candidate_origin_label": candidate_origin_label,
            "is_today_recommendation": is_today_recommendation,
            "select_reason": selection_reason_from_row(row, {}),
            "strategy_name": strategy_name,
            "strategy_bucket": strategy_bucket,
            "strategy_lane": strategy_lane,
            "group_key": group_key,
            "detail_lane": detail_lane(strategy_type),
            "source_label": data_source_label(row),
            "data_status": quality["status"],
            "data_quality": quality,
            "tracking": tracking,
            "focus_reason": f"{strategy_lane}；{tracking['status_label']}；{quality['status']}。",
            "data_hint": UPDATE_HINT,
            "batch_identity": generation.get("batch_identity", {}) if is_today_recommendation else {},
        }

    latest = stock_df.iloc[-1]
    pct_chg = latest_pct_change(stock_df)
    indicators = latest_indicators(stock_df)
    risk_level = stock_explainer.risk_level_from_indicators(indicators)
    tracking = build_strategy_tracking(stock_df, row, indicators)
    strategy_bucket, strategy_lane, group_key = classify_candidate_lane(strategy_type, indicators, tracking, quality)
    return {
        **candidate_exposure_evidence(row),
        "symbol": clean_symbol,
        "name": name,
        "current_price": chart_number(latest.get("close")),
        "pct_chg": chart_number(pct_chg, 0.0) if pct_chg is not None else "数据缺失",
        "reason_cn": stock_explainer.generate_selection_reason(clean_symbol, indicators, len(stock_df)),
        "macd_val": f"DIF {display_number(indicators.get('dif'), 3)} / DEA {display_number(indicators.get('dea'), 3)} / 柱 {display_number(indicators.get('macd'), 3)}",
        "kdj_val": f"K {display_number(indicators.get('k'), 1)} / D {display_number(indicators.get('d'), 1)} / J {display_number(indicators.get('j'), 1)}",
        "rsi_val": f"RSI6 {display_number(indicators.get('rsi6'), 1)} / RSI12 {display_number(indicators.get('rsi12'), 1)}",
        "risk_level": risk_level,
        "select_date": entry_date or latest.get("date").strftime("%Y-%m-%d"),
        "signal_date": signal_date or entry_date or latest.get("date").strftime("%Y-%m-%d"),
        "candidate_origin_label": candidate_origin_label,
        "is_today_recommendation": is_today_recommendation,
        "select_reason": selection_reason_from_row(row, indicators),
        "strategy_name": strategy_name,
        "strategy_bucket": strategy_bucket,
        "strategy_lane": strategy_lane,
        "group_key": group_key,
        "detail_lane": detail_lane(strategy_type),
        "source_label": data_source_label(row),
        "data_status": quality["status"],
        "data_quality": quality,
        "tracking": tracking,
        "focus_reason": f"{strategy_lane}；{tracking['status_label']}；{quality['status']}。",
        "data_hint": quality["hint"],
        "batch_identity": generation.get("batch_identity", {}) if is_today_recommendation else {},
    }


def strategy_priority(row: pd.Series, *, generation: dict[str, Any] | None = None) -> tuple[int, str]:
    strategy_type = row_strategy_type(row)
    entry_date = row_entry_date(row)
    signal_date = row_signal_date(row)
    generation = generation if generation is not None else latest_generation_status()
    generation_symbols = latest_generation_symbols(generation)
    symbol = normalize_symbol(str(row.get("ts_code") or row.get("symbol") or ""))
    is_today = (
        row_candidate_role(row) == "daily_recommendation"
        and signal_date
        and signal_date == str(generation.get("signal_date") or "")[:10]
        and (not generation_symbols or symbol in generation_symbols)
        and _row_matches_formal_generation(row, generation)
    )
    return (100 if is_today else 0, PRIMARY_STRATEGY_PRIORITY.get(strategy_type, 0), entry_date)


def candidate_auxiliary_strategy_labels(rows: list[pd.Series], primary: pd.Series | None) -> list[str]:
    primary_type = row_strategy_type(primary)
    labels: list[str] = []
    for row in rows:
        strategy_type = row_strategy_type(row)
        label = strategy_label(strategy_type)
        if strategy_type and strategy_type != primary_type and label not in labels:
            labels.append(label)
    return labels


def attach_strategy_conflict(candidate: dict[str, Any], related_rows: list[pd.Series], primary: pd.Series | None) -> dict[str, Any]:
    aux = candidate_auxiliary_strategy_labels(related_rows, primary)
    candidate["auxiliary_strategy_labels"] = aux
    if aux:
        candidate["strategy_conflict_note"] = (
            f"同一股票还命中过：{'、'.join(aux)}。页面只按主分层“{candidate.get('strategy_lane', '候选观察')}”展示，"
            "其他策略仅作为辅助线索，避免一只股票重复出现在多个池子。"
        )
    else:
        candidate["strategy_conflict_note"] = "未发现同一股票的其他策略命中。"
    return candidate


def _candidate_intraday_required(candidate: dict[str, Any]) -> bool:
    lane_text = " ".join(
        str(candidate.get(key) or "")
        for key in ("detail_lane", "strategy_lane", "strategy_bucket", "strategy_name", "group_key")
    )
    return "board" in lane_text.lower() or "打板" in lane_text or "涨停" in lane_text


def _candidate_reference_target_date(candidate: dict[str, Any]) -> str:
    quality = candidate.get("data_quality") if isinstance(candidate.get("data_quality"), dict) else {}
    for key in ("signal_date", "select_date"):
        text = str(candidate.get(key) or "").strip()
        if text and text != "数据缺失":
            return text[:10]
    text = str(quality.get("target_date") or quality.get("latest_date") or "").strip()
    return text[:10] if text else ""


def attach_reference_confidence(candidate: dict[str, Any]) -> dict[str, Any]:
    enriched = dict(candidate)
    target_date = _candidate_reference_target_date(enriched)
    symbol = normalize_symbol(str(enriched.get("symbol") or ""))
    intraday_required = _candidate_intraday_required(enriched)
    minute_available = False
    if intraday_required and symbol and target_date:
        minute_available = intraday_file_exists(symbol, target_date, project_root=project_root)
    payload = build_reference_confidence(
        {
            **enriched,
            "symbol": symbol,
            "latest_close": enriched.get("current_price") or enriched.get("latest_close") or enriched.get("close"),
            "daily_amount": enriched.get("daily_amount") or enriched.get("amount_ma20") or enriched.get("amount"),
            "intraday_required": intraday_required,
            "minute_data_available": minute_available,
            "data_quality": enriched.get("data_quality") if isinstance(enriched.get("data_quality"), dict) else {},
        },
        target_date=target_date,
        project_root=project_root,
    )
    enriched.update(payload)
    return enriched


def _gap_suggested_action(kind: str, symbol: str = "") -> str:
    if kind == "missing_daily_file":
        return f"补齐 data/daily/{symbol or '<symbol>'}.csv 后再复核。"
    if kind == "missing_1m_data":
        return "如需盘中触发复核，补齐 data/intraday/1m 下对应日期或股票文件。"
    if kind == "missing_liquidity_evidence":
        return "补齐 amount 或 volume 字段以改善流动性复核。"
    if kind == "short_daily_history":
        return "补齐更长历史日线后再进行严肃复核。"
    return "检查本地数据证据后再复核。"


def candidate_confidence_summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    labels = {
        "research_ready": "高可信参考",
        "partial": "部分可信参考",
        "degraded": "降级参考",
        "missing": "证据缺失",
    }
    counts = {key: 0 for key in labels}
    gap_map: dict[str, dict[str, Any]] = {}
    for row in rows:
        confidence = row.get("data_confidence") if isinstance(row.get("data_confidence"), dict) else {}
        status = str(confidence.get("status") or "missing")
        if status not in counts:
            status = "missing"
        counts[status] += 1
        evidence = row.get("missing_evidence") if isinstance(row.get("missing_evidence"), list) else []
        symbol = normalize_symbol(str(row.get("symbol") or ""))
        for item in evidence:
            if not isinstance(item, dict):
                continue
            kind = str(item.get("kind") or "unknown_gap")
            bucket = gap_map.setdefault(
                kind,
                {
                    "kind": kind,
                    "severity": str(item.get("severity") or "warning"),
                    "message": str(item.get("message") or kind),
                    "suggested_action": str(item.get("suggested_action") or _gap_suggested_action(kind, symbol)),
                    "count": 0,
                    "symbols": [],
                },
            )
            bucket["count"] = int(bucket.get("count") or 0) + 1
            symbols = bucket.setdefault("symbols", [])
            if symbol and symbol not in symbols:
                symbols.append(symbol)
    severity_rank = {"error": 0, "warning": 1, "info": 2}
    top_gaps = sorted(
        gap_map.values(),
        key=lambda item: (severity_rank.get(str(item.get("severity")), 3), -int(item.get("count") or 0), str(item.get("kind"))),
    )
    for gap in top_gaps:
        gap["symbols"] = gap.get("symbols", [])[:8]
    return {
        "candidate_count": len(rows),
        "usable_count": counts["research_ready"] + counts["partial"],
        "degraded_count": counts["degraded"] + counts["missing"],
        "status_groups": [
            {"status": status, "label": label, "count": counts[status]}
            for status, label in labels.items()
        ],
        "top_data_gaps": top_gaps[:8],
        "research_only": True,
        "paper_only": True,
        "no_broker": True,
        "no_investment_advice": True,
    }


def candidate_rows(limit: int = 10) -> list[dict[str, Any]]:
    ledger = load_candidate_ledger()
    rows: list[dict[str, Any]] = []
    overrides = load_candidate_review_overrides()
    generation = latest_generation_status()
    if generation.get("formal_current"):
        formal_rows = [{**row, "ts_code": row.get("symbol"), "stock_name": row.get("name"),
            "signal_date": generation["signal_date"], "entry_date": generation["signal_date"],
            "candidate_role": "daily_recommendation", "data_source": "formal_candidate_batch",
            "source_hash": generation["batch_identity"]["batch_provenance_hash"],
            "source_run_id": generation["batch_identity"]["source_run_id"]}
            for row in generation.get("formal_rows") or []]
        ledger = pd.concat([pd.DataFrame(formal_rows), ledger], ignore_index=True).fillna("")
    if not ledger.empty and "ts_code" in ledger.columns:
        generation_date = str(generation.get("signal_date") or "")[:10]
        generation_symbols = latest_generation_symbols(generation)
        grouped: dict[str, list[pd.Series]] = {}
        for _, raw_row in ledger.iterrows():
            symbol = normalize_symbol(str(raw_row.get("ts_code") or ""))
            if not symbol.strip("0"):
                continue
            grouped.setdefault(symbol, []).append(raw_row)
        today_symbols = []
        observation_symbols = []
        for symbol, related_rows in grouped.items():
            has_today = any(
                row_candidate_role(row) == "daily_recommendation"
                and row_signal_date(row) == generation_date
                and (not generation_symbols or symbol in generation_symbols)
                and _row_matches_formal_generation(row, generation)
                for row in related_rows
            )
            (today_symbols if has_today else observation_symbols).append(symbol)

        def symbol_priority(symbol: str) -> tuple[int, str]:
            return max(strategy_priority(row, generation=generation) for row in grouped[symbol])

        ordered_symbols = sorted(today_symbols, key=symbol_priority, reverse=True)
        ordered_symbols.extend(sorted(observation_symbols, key=symbol_priority, reverse=True))
        for symbol in ordered_symbols:
            related = grouped[symbol]
            primary = sorted(related, key=lambda row: strategy_priority(row, generation=generation), reverse=True)[0]
            built = (build_candidate(symbol, primary, generation=generation)
                     if generation.get("source") == "formal_candidate_daily_batch" else build_candidate(symbol, primary))
            candidate = attach_strategy_conflict(built, related, primary)
            rows.append(attach_reference_confidence(apply_candidate_review_state(candidate, overrides)))
            if len(rows) >= limit:
                break
    if rows:
        return rows

    for path in sorted(DAILY_DIR.glob("*.csv"))[:limit]:
        symbol = normalize_symbol(path.stem)
        candidate = attach_strategy_conflict(build_candidate(symbol, None), [], None)
        rows.append(attach_reference_confidence(apply_candidate_review_state(candidate, overrides)))
    return rows


def _candidate_read_symbols(limit: int = 18) -> list[str]:
    """Project the exact bounded candidate selection without building bars."""
    ledger = load_candidate_ledger()
    generation = latest_generation_status()
    if generation.get("formal_current"):
        formal_rows = [{**row, "ts_code": row.get("symbol"), "stock_name": row.get("name"),
            "signal_date": generation["signal_date"], "entry_date": generation["signal_date"],
            "candidate_role": "daily_recommendation", "data_source": "formal_candidate_batch",
            "source_hash": generation["batch_identity"]["batch_provenance_hash"],
            "source_run_id": generation["batch_identity"]["source_run_id"]}
            for row in generation.get("formal_rows") or []]
        ledger = pd.concat([pd.DataFrame(formal_rows), ledger], ignore_index=True).fillna("")
    if ledger.empty or "ts_code" not in ledger.columns:
        return [normalize_symbol(path.stem) for path in sorted(DAILY_DIR.glob("*.csv"))[:limit]]
    generation_date = str(generation.get("signal_date") or "")[:10]
    generation_symbols = latest_generation_symbols(generation)
    grouped: dict[str, list[Any]] = {}
    for _, row in ledger.iterrows():
        symbol = normalize_symbol(str(row.get("ts_code") or ""))
        if symbol.strip("0"):
            grouped.setdefault(symbol, []).append(row)
    def is_today(symbol: str) -> bool:
        return any(row_candidate_role(row) == "daily_recommendation"
            and row_signal_date(row) == generation_date
            and (not generation_symbols or symbol in generation_symbols)
            and _row_matches_formal_generation(row, generation) for row in grouped[symbol])
    def priority(symbol: str) -> tuple[int, str]:
        return max(strategy_priority(row, generation=generation) for row in grouped[symbol])
    today = sorted((s for s in grouped if is_today(s)), key=priority, reverse=True)
    historical = sorted((s for s in grouped if not is_today(s)), key=priority, reverse=True)
    return (today + historical)[:limit]


def search_stock_rows(query: str, limit: int = 8) -> list[dict[str, Any]]:
    q = str(query or "").strip()
    if not q:
        return []
    q_lower = q.lower()
    q_symbol = normalize_symbol(q) if any(ch.isdigit() for ch in q) else ""
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    direct_quote_result: dict[str, Any] = {}
    # Search is an observation endpoint. Refresh intent belongs to the explicit
    # stock POST route, independently of whether network use is enabled.

    def add(row: dict[str, Any]) -> None:
        symbol = normalize_symbol(str(row.get("symbol") or ""))
        if not symbol or symbol in seen:
            return
        seen.add(symbol)
        rows.append(row)

    spot = load_spot_snapshot_frame()
    if not spot.empty:
        for _, raw in spot.iterrows():
            symbol = normalize_symbol(str(raw.get("symbol") or ""))
            name = str(raw.get("name") or "").strip()
            if not symbol:
                continue
            if q_symbol and q_symbol != symbol and q_symbol not in symbol:
                continue
            if not q_symbol and q_lower not in name.lower() and q_lower not in symbol.lower():
                continue
            add(
                {
                    "symbol": symbol,
                    "name": name or candidate_name(symbol),
                    "price": chart_number(raw.get("price")),
                    "pct_chg": chart_number(raw.get("pct_chg"), 0.0),
                    "source": "单股实时" if direct_quote_result.get("ok") and symbol == q_symbol else "实时快照",
                }
            )
            if len(rows) >= limit:
                return rows

    listings = UNIVERSE_DIR / "historical_listings.csv"
    if listings.exists() and len(rows) < limit:
        try:
            df = read_csv_frame(listings, usecols=lambda col: col in {"symbol", "code", "ts_code", "name", "stock_name"})
            for _, raw in df.iterrows():
                symbol = ""
                for col in ("symbol", "code", "ts_code"):
                    if col in df.columns:
                        symbol = normalize_symbol(str(raw.get(col) or ""))
                        if symbol:
                            break
                name = str(raw.get("name") or raw.get("stock_name") or "").strip()
                if not symbol:
                    continue
                if q_symbol and q_symbol != symbol and q_symbol not in symbol:
                    continue
                if not q_symbol and q_lower not in name.lower() and q_lower not in symbol.lower():
                    continue
                add({"symbol": symbol, "name": name or symbol, "price": "数据缺失", "pct_chg": "数据缺失", "source": "本地股票名录"})
                if len(rows) >= limit:
                    return rows
        except Exception as exc:  # noqa: BLE001 - local search should continue across fallback files
            LOG.warning("stock_search_local_file_failed: path=%s query=%r error=%s", listings, query, exc)

    if q_symbol and q_symbol not in seen:
        add({"symbol": q_symbol, "name": candidate_name(q_symbol), "price": "数据缺失", "pct_chg": "数据缺失", "source": "代码输入"})
    return rows[:limit]


def stock_minute_view(symbol: str, *, max_rows: int = 300) -> dict[str, Any]:
    """Read the local 1-minute cache for one stock; never fill or invent bars.

    The cache lives in data/intraday/1m/<symbol>.csv and is written by the
    minute pool, not by this reader. Only the most recent session is returned so
    the page can show an honest intraday view together with its coverage.
    """
    clean_symbol = normalize_symbol(symbol)
    path = INTRADAY_ONE_MINUTE_DIR / f"{clean_symbol}.csv"
    payload: dict[str, Any] = {
        "available": False, "rows": [], "date": "", "times": [],
        "coverage": {}, "price_basis": [], "source": [], "message": "",
    }
    if not path.is_file():
        payload["message"] = "本地没有该股的1分钟缓存；不补齐、不编造。"
        return payload
    try:
        frame = read_csv_frame(path)
    except Exception as exc:  # noqa: BLE001 - a read failure must degrade visibly
        payload["message"] = "本地1分钟缓存读取失败：" + type(exc).__name__
        return payload
    if frame.empty:
        payload["message"] = "本地1分钟缓存为空文件。"
        return payload
    if "datetime" in frame.columns:
        stamps = pd.to_datetime(frame["datetime"], errors="coerce")
    elif {"date", "time"} <= set(frame.columns):
        stamps = pd.to_datetime(frame["date"].astype(str) + " " + frame["time"].astype(str), errors="coerce")
    else:
        payload["message"] = "本地1分钟缓存缺少日期或时间列；不推测。"
        return payload
    frame = frame.loc[stamps.notna()].copy()
    if frame.empty:
        payload["message"] = "本地1分钟缓存没有可解析的时间戳。"
        return payload
    frame["_stamp"] = stamps.loc[stamps.notna()].values
    last_date = frame["_stamp"].max().date()
    session = frame.loc[frame["_stamp"].dt.date == last_date].sort_values("_stamp")
    first_date = frame["_stamp"].min().date()
    rows = []
    for _, item in session.tail(max_rows).iterrows():
        rows.append({
            "time": item["_stamp"].strftime("%H:%M"),
            "open": chart_number(item.get("open")),
            "high": chart_number(item.get("high")),
            "low": chart_number(item.get("low")),
            "close": chart_number(item.get("close")),
            "volume": chart_number(item.get("volume")),
            "amount": chart_number(item.get("amount")),
        })
    def distinct(column: str) -> list[str]:
        if column not in frame.columns:
            return []
        values = sorted({str(value).strip() for value in frame[column].dropna().tolist() if str(value).strip()})
        return [value for value in values if value.lower() not in {"nan", "none"}]
    coverage = {
        "first_date": first_date.isoformat(), "last_date": last_date.isoformat(),
        "total_rows": int(len(frame)), "session_rows": int(len(session)),
        "returned_rows": len(rows),
    }
    return {
        **payload, "available": bool(rows), "rows": rows,
        "date": last_date.isoformat(),
        "times": [row["time"] for row in rows],
        "coverage": coverage,
        "price_basis": distinct("price_basis"), "source": distinct("source"),
        "message": ("仅展示本地缓存最近一个交易日（" + last_date.isoformat() + "）的1分钟数据；"
                    "缓存起止 " + first_date.isoformat() + " 至 " + last_date.isoformat() + "。"),
    }


def ensure_single_stock_daily(symbol: str, *, refresh_daily: bool) -> dict[str, Any]:
    return ensure_single_stock_daily_with_auth(
        symbol,
        refresh_daily=refresh_daily,
        network_allowed=realtime_network_enabled(),
    )


def ensure_single_stock_daily_with_auth(
    symbol: str,
    *,
    refresh_daily: bool,
    network_allowed: bool,
) -> dict[str, Any]:
    clean_symbol = normalize_symbol(symbol)
    return {
        "attempted": False, "status": "read_only", "symbol": clean_symbol,
        "network_authorized": bool(network_allowed), "canonical_write": False,
        "daily_writer": "data_close_pipeline", "update_endpoint": "/api/data/update-all",
        "message": "仅读取已有日线。" if not refresh_daily else "单股查看不补写日线；日线缺口保留到收盘更新。",
    }


def append_realtime_chart_row(rows: list[dict[str, Any]], quote: dict[str, Any]) -> tuple[list[dict[str, Any]], str]:
    if not quote.get("available"):
        return rows, ""
    price = finite_float(quote.get("price"))
    if price is None:
        return rows, ""
    quote_date = str(quote.get("quote_date") or datetime.now().strftime("%Y-%m-%d"))
    latest_date = str(rows[-1].get("date") or "") if rows else ""
    session = get_market_session(to_shanghai_datetime())
    if latest_date.startswith(quote_date) and session == "after_close":
        return rows, latest_date
    temp_date = f"{quote_date} 临时"
    open_price = finite_float(quote.get("open"), price) or price
    high_price = finite_float(quote.get("high"), max(open_price, price)) or max(open_price, price)
    low_price = finite_float(quote.get("low"), min(open_price, price)) or min(open_price, price)
    temp_row = {
        "date": temp_date,
        "open": open_price,
        "high": high_price,
        "low": low_price,
        "close": price,
        "volume": chart_number(quote.get("volume")),
        "amount": chart_number(quote.get("amount")),
        "ma5": "数据缺失",
        "ma10": "数据缺失",
        "ma20": "数据缺失",
        "macd_dif": "数据缺失",
        "macd_dea": "数据缺失",
        "macd_bar": "数据缺失",
        "kdj_k": "数据缺失",
        "kdj_d": "数据缺失",
        "kdj_j": "数据缺失",
        "rsi": "数据缺失",
        "temporary": True,
    }
    return rows + [temp_row], temp_date


def build_realtime_observation(stock_df: pd.DataFrame, quote: dict[str, Any]) -> dict[str, Any]:
    indicators = latest_indicators(stock_df) if not stock_df.empty else {}
    price = finite_float(quote.get("price")) if quote.get("available") else None
    pct = finite_float(quote.get("pct_chg")) if quote.get("available") else None
    close = finite_float(indicators.get("close"))
    ma20 = finite_float(indicators.get("ma20"))
    dif = finite_float(indicators.get("dif"))
    dea = finite_float(indicators.get("dea"))
    rsi6 = finite_float(indicators.get("rsi6"))
    volume = finite_float(quote.get("volume")) if quote.get("available") else None
    avg_volume = finite_float(indicators.get("vol_ma20"))

    lines: list[str] = []
    if price is not None and pct is not None:
        direction = "偏强" if pct > 1 else "偏弱" if pct < -1 else "震荡"
        lines.append(f"实时快照显示当前价 {price:.2f}，当日变化 {pct:+.2f}%，盘中状态暂按“{direction}”观察。")
    else:
        lines.append("实时快照暂不可用，只能基于本地完整日线做观察。")
    if close is not None and ma20 is not None:
        relation = "高于" if close >= ma20 else "低于"
        lines.append(f"最新完整日线收盘价 {close:.2f} {relation} MA20 {ma20:.2f}，用于判断中期位置。")
    if dif is not None and dea is not None:
        lines.append(f"MACD 当前 DIF {dif:.3f}、DEA {dea:.3f}，{'动能偏强' if dif >= dea else '动能偏弱'}，需要和价格位置一起看。")
    if rsi6 is not None:
        if rsi6 >= 70:
            lines.append(f"RSI6 为 {rsi6:.1f}，短线偏热，观察是否出现回落或钝化。")
        elif rsi6 <= 30:
            lines.append(f"RSI6 为 {rsi6:.1f}，短线偏冷，观察是否出现修复。")
        else:
            lines.append(f"RSI6 为 {rsi6:.1f}，处在常规波动区间。")
    if volume is not None and avg_volume not in (None, 0):
        ratio = volume / avg_volume
        lines.append(f"当前快照成交量约为20日均量的 {ratio:.2f} 倍；这是临时量能参考，收盘前会变化。")

    scenario = "若价格继续保持在关键均线附近且动能没有明显走弱，可继续作为观察样本；若实时变化与完整日线方向冲突，应等收盘后再用正式研究层复核。"
    if pct is not None and pct < -3:
        scenario = "实时跌幅较大时优先看风险暴露和数据原因；正式复核仍应等完整日线确认。"
    elif pct is not None and pct > 3:
        scenario = "实时涨幅较大时优先观察量能是否同步放大以及是否接近短线过热；正式复核仍应等完整日线确认。"
    return {
        "summary": " ".join(lines),
        "scenario_reference": scenario,
        "disclaimer": "这是研究性情景参考，不是价格预测、投资建议、仓位建议或交易指令。",
    }


def add_manual_observation(symbol: str, name: str = "", note: str = "") -> dict[str, Any]:
    clean_symbol = normalize_symbol(symbol)
    if not clean_symbol:
        return {"ok": False, "message": "股票代码无效。"}
    TRACKING_DIR.mkdir(parents=True, exist_ok=True)
    exists = MANUAL_OBSERVATION_WATCHLIST.exists()
    rows: list[dict[str, str]] = []
    if exists:
        try:
            with MANUAL_OBSERVATION_WATCHLIST.open("r", encoding="utf-8-sig", newline="") as handle:
                rows = list(csv.DictReader(handle))
        except Exception as exc:  # noqa: BLE001 - watchlist read failure should not break add action
            LOG.warning("manual_observation_watchlist_read_failed: path=%s error=%s", MANUAL_OBSERVATION_WATCHLIST, exc)
            rows = []
    if any(normalize_symbol(row.get("symbol") or "") == clean_symbol for row in rows):
        return {
            "ok": True,
            "status": "already_exists",
            "message": f"{clean_symbol} 已在手动观察池中。",
            "path": str(MANUAL_OBSERVATION_WATCHLIST.relative_to(project_root)),
        }
    row = {
        "symbol": clean_symbol,
        "name": name or candidate_name(clean_symbol),
        "added_at": datetime.now().isoformat(timespec="seconds"),
        "note": note or "手动加入实时观察池；未进入正式研究候选。",
    }
    with MANUAL_OBSERVATION_WATCHLIST.open("a", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=["symbol", "name", "added_at", "note"])
        if not exists:
            writer.writeheader()
        writer.writerow(row)
    return {
        "ok": True,
        "status": "added",
        "message": f"{row['name']} {clean_symbol} 已加入手动观察池；不会自动进入正式研究候选。",
        "path": str(MANUAL_OBSERVATION_WATCHLIST.relative_to(project_root)),
    }


def candidate_review_overrides_path() -> Path:
    return TRACKING_DIR / "candidate_review_overrides.json"


def candidate_review_key(symbol: str, entry_date: str = "", lane: str = "") -> str:
    return "|".join(
        [
            normalize_symbol(symbol),
            str(entry_date or "").strip()[:10],
            str(lane or "").strip(),
        ]
    )


def load_candidate_review_overrides() -> dict[str, dict[str, Any]]:
    path = candidate_review_overrides_path()
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001 - corrupt override file should be visible and ignored
        LOG.warning("candidate_review_overrides_read_failed: path=%s error=%s", path, exc)
        return {}
    if not isinstance(data, dict):
        return {}
    return {str(key): value for key, value in data.items() if isinstance(value, dict)}


def write_candidate_review_overrides(overrides: dict[str, dict[str, Any]]) -> None:
    path = candidate_review_overrides_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(overrides, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )


def project_relative_path(path: Path) -> str:
    try:
        return str(path.relative_to(project_root))
    except ValueError:
        return str(path)


def default_pool_action() -> dict[str, Any]:
    return {
        "status": "active",
        "label": "正常观察",
        "tone": "ok",
        "source": "auto",
        "reason": "当前仍在原研究分层中展示。",
        "manual": False,
    }


def auto_pool_action(candidate: dict[str, Any]) -> dict[str, Any]:
    tracking = candidate.get("tracking", {}) if isinstance(candidate.get("tracking"), dict) else {}
    status_tone = str(tracking.get("status_tone") or "")
    status_label = str(tracking.get("status_label") or "")
    group_key = str(candidate.get("group_key") or "")
    strategy_lane = str(candidate.get("strategy_lane") or "")
    if group_key == "board" and (status_tone == "warn" or status_label == "偏离观察假设"):
        return {
            "status": "auto_exit_review",
            "label": "自动转待移出",
            "tone": "warn",
            "source": "auto",
            "reason": "打板观察已偏离入选假设，不再放在打板复核层；保留到降级复盘区学习原因。",
            "manual": False,
            "group_key": "exit_review",
            "strategy_lane": "待移出复盘",
        }
    if group_key == "long_swing" and (status_tone == "warn" or status_label == "偏离观察假设"):
        return {
            "status": "auto_downgrade_observe",
            "label": "自动降级只观察",
            "tone": "warn",
            "source": "auto",
            "reason": "长期波段样本已偏离入选假设，自动降级到只观察层复盘。",
            "manual": False,
            "group_key": "long_observe",
            "strategy_lane": "长期-只观察",
        }
    if "打板" in strategy_lane and status_label == "尚未确认":
        return {
            "status": "board_pending_review",
            "label": "打板待确认",
            "tone": "watch",
            "source": "auto",
            "reason": "打板观察尚未确认延续性，继续按完整日线复核。",
            "manual": False,
        }
    return default_pool_action()


def apply_candidate_review_state(
    candidate: dict[str, Any],
    overrides: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    overrides = overrides if overrides is not None else load_candidate_review_overrides()
    key = candidate_review_key(
        str(candidate.get("symbol") or ""),
        str(candidate.get("select_date") or ""),
        str(candidate.get("detail_lane") or ""),
    )
    action = auto_pool_action(candidate)
    override = overrides.get(key)
    if override:
        manual_action = str(override.get("action") or "").strip()
        if manual_action == "keep":
            action = {
                "status": "manual_keep",
                "label": "手动保留观察",
                "tone": "ok",
                "source": "manual",
                "reason": str(override.get("note") or "你已手动保留该候选，继续按当前分层观察。"),
                "manual": True,
                "updated_at": override.get("updated_at", ""),
            }
        elif manual_action == "observe":
            action = {
                "status": "manual_observe",
                "label": "手动转只观察",
                "tone": "watch",
                "source": "manual",
                "reason": str(override.get("note") or "你已手动转入只观察层，不参与每日重点排序。"),
                "manual": True,
                "group_key": "manual_observe",
                "strategy_lane": "手动只观察",
                "updated_at": override.get("updated_at", ""),
            }
        elif manual_action == "review":
            action = {
                "status": "manual_exit_review",
                "label": "手动降级复盘",
                "tone": "warn",
                "source": "manual",
                "reason": str(override.get("note") or "你已手动转入降级复盘层，用来验证原入选逻辑哪里偏离。"),
                "manual": True,
                "group_key": "exit_review",
                "strategy_lane": "手动降级复盘",
                "updated_at": override.get("updated_at", ""),
            }
        elif manual_action == "hide":
            action = {
                "status": "manual_archived",
                "label": "归档/忽略",
                "tone": "missing",
                "source": "manual",
                "reason": str(override.get("note") or "你已手动归档/忽略该候选；它不再参与今日重点和活跃观察排序。"),
                "manual": True,
                "group_key": "manual_archived",
                "strategy_lane": "归档/忽略",
                "updated_at": override.get("updated_at", ""),
            }
    if action.get("group_key"):
        candidate["group_key"] = str(action["group_key"])
    if action.get("strategy_lane"):
        candidate["strategy_lane"] = str(action["strategy_lane"])
    candidate["pool_action"] = action
    candidate["pool_review_key"] = key
    return candidate


def save_candidate_review_action(
    symbol: str,
    entry_date: str,
    lane: str,
    action: str,
    note: str = "",
) -> dict[str, Any]:
    clean_symbol = normalize_symbol(symbol)
    clean_action = str(action or "").strip()
    if not clean_symbol:
        return {"ok": False, "message": "股票代码无效。"}
    allowed = {"keep", "observe", "review", "hide", "clear"}
    if clean_action not in allowed:
        return {"ok": False, "message": "候选池操作无效。"}
    key = candidate_review_key(clean_symbol, entry_date, lane)
    overrides = load_candidate_review_overrides()
    if clean_action == "clear":
        overrides.pop(key, None)
        write_candidate_review_overrides(overrides)
        return {
            "ok": True,
            "status": "cleared",
            "message": "已清除手动状态，候选将回到自动复核分层。",
            "path": project_relative_path(candidate_review_overrides_path()),
        }
    overrides[key] = {
        "symbol": clean_symbol,
        "entry_date": str(entry_date or "").strip()[:10],
        "lane": str(lane or "").strip(),
        "action": clean_action,
        "note": str(note or "").strip(),
        "updated_at": datetime.now().isoformat(timespec="seconds"),
        "research_only": True,
    }
    write_candidate_review_overrides(overrides)
    label = {
        "keep": "手动保留观察",
        "observe": "手动转只观察",
        "review": "手动降级复盘",
        "hide": "归档/忽略",
    }[clean_action]
    return {
        "ok": True,
        "status": clean_action,
        "message": f"{clean_symbol} 已标记为“{label}”。这是本地研究池状态，不是交易指令。",
        "path": project_relative_path(candidate_review_overrides_path()),
    }


def load_paper_simulation_watchlist() -> dict[str, Any]:
    if not PAPER_WATCHLIST_JSON.exists():
        return {
            "ok": True,
            "status": "passed",
            "watchlist_id": "paper_watchlist",
            "items": [],
            "research_only": True,
            "paper_only": True,
            "broker_boundary": "No Broker Route / No real orders.",
            "disclaimer": "Research Only / Paper Account Only / Not Investment Advice / Not Position Advice.",
        }
    data = read_json_file(PAPER_WATCHLIST_JSON)
    if not isinstance(data, dict) or data.get("status") == "unreadable":
        return {
            "ok": False,
            "status": "unreadable",
            "items": [],
            "message_for_ui": "纸面模拟池文件暂时不可读，未写入新内容。",
            "path": project_relative_path(PAPER_WATCHLIST_JSON),
        }
    data.setdefault("items", [])
    data["items"] = [
        ensure_paper_item_research_contract(item)
        for item in data.get("items", [])
        if isinstance(item, dict)
    ]
    for item in data["items"]:
        if item.get("tracking_metric_version") != "peak-to-trough-v2":
            item["legacy_max_drawdown_since_added"] = item.get("max_drawdown_since_added")
            item["max_drawdown_since_added"] = None
            item["max_drawdown_status"] = "legacy_metric_unverified"
            item["tracking_message"] = (str(item.get("tracking_message") or "")
                + " 历史缓存的最大回撤采用旧口径，已停止作为回撤展示；原始记录保留，等待按峰谷口径重新统计。")
    data.setdefault("research_only", True)
    data.setdefault("paper_only", True)
    data.setdefault("broker_boundary", "No Broker Route / No real orders.")
    data.setdefault("disclaimer", "Research Only / Paper Account Only / Not Investment Advice / Not Position Advice.")
    return data


def write_paper_simulation_watchlist(payload: dict[str, Any]) -> None:
    PAPER_WATCHLIST_JSON.parent.mkdir(parents=True, exist_ok=True)
    PAPER_WATCHLIST_JSON.write_text(json.dumps(clean_payload(payload), ensure_ascii=False, indent=2), encoding="utf-8")


def paper_simulation_lifecycle_action(
    symbol: str,
    action: str,
    *,
    note: str = "",
    watchlist_id: str = "",
) -> dict[str, Any]:
    """Apply a local paper-review lifecycle action to one watchlist item.

    Actions only change local research state. They do not create paper trades,
    broker routes, orders, position instructions, or live execution.
    """
    clean_symbol = normalize_symbol(symbol)
    clean_action = str(action or "").strip().lower()
    note = str(note or "").strip()
    allowed = {
        "continue": {
            "review_status": "paper_tracking",
            "review_label": "继续纸面验证",
            "review_tone": "watch",
            "lifecycle_state": "active_tracking",
            "expectation_fit_status": "pending",
            "expectation_fit_label": "继续验证",
            "message": "已确认继续纸面验证；下次刷新会继续对照原始预期。",
        },
        "review": {
            "review_status": "manual_downgrade_review",
            "review_label": "已确认降级复盘",
            "review_tone": "warn",
            "lifecycle_state": "downgrade_review",
            "expectation_fit_status": "deviated",
            "expectation_fit_label": "偏离原始预期",
            "message": "已确认进入降级复盘；成绩单会把它计入需要复盘样本。",
        },
        "archive": {
            "review_status": "manual_archived",
            "review_label": "已归档保留",
            "review_tone": "archived",
            "lifecycle_state": "archived",
            "message": "已从活跃纸面池归档；样本仍保留用于回看，不会删除。",
        },
        "restore": {
            "review_status": "paper_tracking",
            "review_label": "已恢复跟踪",
            "review_tone": "watch",
            "lifecycle_state": "active_tracking",
            "expectation_fit_status": "pending",
            "expectation_fit_label": "等待更多样本",
            "message": "已恢复为活跃纸面跟踪样本。",
        },
    }
    if not clean_symbol:
        return {
            "ok": False,
            "status": "missing_symbol",
            "message_for_ui": "缺少股票代码，无法更新纸面池状态。",
            "research_only": True,
            "paper_only": True,
            "no_broker": True,
        }
    if clean_action not in allowed:
        return {
            "ok": False,
            "status": "invalid_action",
            "message_for_ui": "不支持的纸面池动作；可用动作：继续验证、确认复盘、归档保留、恢复跟踪。",
            "research_only": True,
            "paper_only": True,
            "no_broker": True,
        }

    watchlist = load_paper_simulation_watchlist()
    if watchlist.get("ok") is False:
        return watchlist
    items = [item for item in watchlist.get("items") or [] if isinstance(item, dict)]
    target_index = -1
    for index, item in enumerate(items):
        if normalize_symbol(item.get("symbol")) != clean_symbol:
            continue
        if watchlist_id and str(item.get("watchlist_id") or "") != watchlist_id:
            continue
        target_index = index
        break
    if target_index < 0 and watchlist_id:
        for index, item in enumerate(items):
            if normalize_symbol(item.get("symbol")) == clean_symbol:
                target_index = index
                break
    if target_index < 0:
        return {
            "ok": False,
            "status": "not_found",
            "message_for_ui": "纸面模拟池中找不到这只股票，请先同步今日重点或从详情加入。",
            "research_only": True,
            "paper_only": True,
            "no_broker": True,
        }

    now = datetime.now().isoformat(timespec="seconds")
    patch = allowed[clean_action]
    item = {**items[target_index]}
    history = item.get("paper_lifecycle_history") if isinstance(item.get("paper_lifecycle_history"), list) else []
    history.append(
        {
            "action": clean_action,
            "label": patch["review_label"],
            "note": note,
            "created_at": now,
            "research_only": True,
            "paper_only": True,
            "no_broker": True,
        }
    )
    item.update(
        {
            "review_status": patch["review_status"],
            "review_label": patch["review_label"],
            "review_tone": patch["review_tone"],
            "paper_lifecycle_state": patch["lifecycle_state"],
            "paper_lifecycle_action": clean_action,
            "paper_lifecycle_note": note,
            "paper_lifecycle_updated_at": now,
            "paper_lifecycle_history": history[-20:],
            "updated_at": now,
            "research_only": True,
            "paper_only": True,
            "no_broker": True,
            "no_real_orders": True,
            "not_investment_advice": True,
            "not_position_advice": True,
        }
    )
    if clean_action == "review":
        item["expectation_fit_status"] = patch["expectation_fit_status"]
        item["expectation_fit_label"] = patch["expectation_fit_label"]
        item["expectation_fit_reason"] = note or item.get("expectation_fit_reason") or "用户确认原始观察假设需要降级复盘。"
        item["review_reason"] = note or item.get("review_reason") or "用户确认进入降级复盘。"
    elif clean_action in {"continue", "restore"}:
        item["expectation_fit_status"] = patch["expectation_fit_status"]
        item["expectation_fit_label"] = patch["expectation_fit_label"]
        item["expectation_fit_reason"] = note or "继续按原始研究假设等待更多完整日线验证。"
        item["review_reason"] = note or patch["message"]
    elif clean_action == "archive":
        item["review_reason"] = note or "用户确认归档保留，用于回看策略判断。"
        item["paper_exit_plan"] = {
            "plan_status": "archived_research_sample",
            "paper_exit_candidate": False,
            "auto_execute": False,
            "execution_allowed": False,
            "no_real_orders": True,
            "message": "归档仅影响本地研究视图，不是交易动作。",
        }
    items[target_index] = item

    active_items = [entry for entry in items if str(entry.get("paper_lifecycle_state") or "") != "archived"]
    archived_count = len(items) - len(active_items)
    summary = watchlist.get("paper_tracking_summary") if isinstance(watchlist.get("paper_tracking_summary"), dict) else {}
    summary.update(
        {
            "active_count": len(active_items),
            "archived_count": archived_count,
            "needs_review_count": sum(
                1
                for entry in active_items
                if str(entry.get("review_tone") or "") == "warn"
                or str(entry.get("expectation_fit_status") or "") in {"deviated", "data_gap"}
            ),
            "updated_at": now,
            "message": f"纸面池状态已更新：活跃 {len(active_items)} 只，归档 {archived_count} 只。",
        }
    )
    watchlist.update(
        {
            "ok": True,
            "status": "passed",
            "items": items,
            "watchlist_count": len(items),
            "active_watchlist_count": len(active_items),
            "archived_watchlist_count": archived_count,
            "paper_tracking_summary": summary,
            "updated_at": now,
            "research_only": True,
            "paper_only": True,
            "broker_boundary": "No Broker Route / No real orders.",
            "disclaimer": "Research Only / Paper Account Only / Not Investment Advice / Not Position Advice.",
        }
    )
    write_paper_simulation_watchlist(watchlist)
    return {
        "ok": True,
        "status": "updated",
        "action": clean_action,
        "message": patch["message"],
        "message_for_ui": patch["message"],
        "item": item,
        "paper_tracking_summary": summary,
        "watchlist_count": len(items),
        "active_watchlist_count": len(active_items),
        "archived_watchlist_count": archived_count,
        "path": project_relative_path(PAPER_WATCHLIST_JSON),
        "research_only": True,
        "paper_only": True,
        "no_broker": True,
        "no_real_orders": True,
        "not_investment_advice": True,
        "not_position_advice": True,
    }


def find_visible_candidate(symbol: str, entry_date: str = "", lane: str = "") -> dict[str, Any] | None:
    clean_symbol = normalize_symbol(symbol)
    clean_entry = str(entry_date or "").strip()[:10]
    clean_lane = str(lane or "").strip()
    rows = annotate_recommendation_priorities(candidate_rows(limit=18))
    for row in rows:
        if normalize_symbol(row.get("symbol")) != clean_symbol:
            continue
        if clean_entry and str(row.get("select_date") or "")[:10] != clean_entry:
            continue
        if clean_lane and str(row.get("detail_lane") or "") != clean_lane:
            continue
        return row
    for row in rows:
        if normalize_symbol(row.get("symbol")) == clean_symbol:
            return row
    return None


def paper_simulation_candidate_key(candidate: dict[str, Any]) -> str:
    return candidate_review_key(
        str(candidate.get("symbol") or ""),
        str(candidate.get("select_date") or candidate.get("signal_date") or ""),
        str(candidate.get("detail_lane") or ""),
    )


def paper_simulation_status_for_candidate(symbol: str, entry_date: str = "", lane: str = "") -> dict[str, Any]:
    clean_symbol = normalize_symbol(symbol)
    key = candidate_review_key(clean_symbol, entry_date, lane)
    watchlist = load_paper_simulation_watchlist()
    items = [item for item in watchlist.get("items") or [] if isinstance(item, dict)]
    for item in items:
        item_symbol = normalize_symbol(item.get("symbol"))
        item_key = str(item.get("source_candidate_key") or "")
        if item_symbol == clean_symbol and (not key.strip("|") or item_key == key or not item_key):
            return {
                "in_paper_pool": True,
                "status": str(item.get("review_status") or "watching"),
                "added_at": str(item.get("added_at") or ""),
                "watchlist_id": str(item.get("watchlist_id") or ""),
                "message": "已在研究观察中（仅观察，不产生持仓与盈亏）；后续可跟踪表现并用于复盘。",
                "observation_only": True,
                "creates_positions": False,
                "creates_pnl": False,
                "canonical_paper_ledger": "state/auto_paper_portfolio.json",
                "path": project_relative_path(PAPER_WATCHLIST_JSON),
            }
    return {
        "in_paper_pool": False,
        "status": "not_added",
        "message": "尚未加入研究观察；可由你手动确认后加入（仅观察，不产生持仓与盈亏）。",
        "observation_only": True,
        "creates_positions": False,
        "creates_pnl": False,
        "canonical_paper_ledger": "state/auto_paper_portfolio.json",
        "path": project_relative_path(PAPER_WATCHLIST_JSON),
    }


def candidate_paper_evidence(candidate: dict[str, Any]) -> dict[str, Any]:
    tracking = candidate.get("tracking") if isinstance(candidate.get("tracking"), dict) else {}
    quality = candidate.get("data_quality") if isinstance(candidate.get("data_quality"), dict) else {}
    strategy_profile = paper_strategy_profile(candidate)
    contract = build_paper_research_contract(candidate, strategy_profile=strategy_profile)
    evidence_lines = [
        f"策略层：{candidate.get('strategy_lane') or candidate.get('strategy_name') or '候选观察'}",
        f"入选日：{candidate.get('select_date') or candidate.get('signal_date') or '数据缺失'}",
        f"入选理由：{candidate.get('select_reason') or candidate.get('focus_reason') or '数据缺失'}",
        f"主动作：{strategy_profile['primary_action']}",
        f"验证周期：{contract['validation_cycle']['label']}",
        f"当前状态：{tracking.get('status_label') or candidate.get('data_status') or '观察中'}",
        f"数据状态：{quality.get('status') or candidate.get('data_status') or '数据缺失'}",
    ]
    reason = str(candidate.get("reason_cn") or "").strip()
    if reason:
        evidence_lines.append(f"指标解释：{reason[:260]}")
    success = contract.get("success_conditions") or []
    failure = contract.get("failure_conditions") or []
    if success:
        evidence_lines.append(f"成功条件：{success[0]}")
    if failure:
        evidence_lines.append(f"失败条件：{failure[0]}")
    return {
        "summary": "；".join(evidence_lines),
        "lines": evidence_lines,
        "tracking": tracking,
        "data_quality": quality,
        "strategy_profile": strategy_profile,
        "paper_research_contract": contract,
        "priority": {
            "rank": candidate.get("priority_rank"),
            "label": candidate.get("priority_label"),
            "reason": candidate.get("priority_reason"),
        },
        "agent_question_suggestion": (
            f"请基于本地证据复核 {candidate.get('name')}({candidate.get('symbol')}) "
            "是否适合进入/继续留在纸面模拟池：请给出结论、核心证据、反方风险、验证周期、成功条件、失败条件和需要补的数据。"
            "只做研究学习，不给买卖/仓位建议。"
        ),
    }


def _candidate_strategy_text(candidate: dict[str, Any]) -> str:
    return " ".join(
        str(candidate.get(key) or "")
        for key in ("detail_lane", "strategy_lane", "strategy_bucket", "group_key", "strategy_name")
    )


def paper_strategy_profile(candidate: dict[str, Any]) -> dict[str, Any]:
    text = _candidate_strategy_text(candidate)
    tags: list[str] = []
    if "打板" in text or "board" in text:
        family = "board"
        primary_action = "打板复核"
        tags.extend(["打板", "短线弹性"])
    elif "长期" in text or "波段" in text or "long" in text:
        family = "long_swing"
        primary_action = "长期/波段复核" if "波段" in text else "长期观察"
        tags.extend(["长期", "波段"])
    elif "复盘" in text or "exit" in text or "archive" in text:
        family = "review"
        primary_action = "降级复盘"
        tags.extend(["降级复盘", "失败样本"])
    else:
        family = "observe"
        primary_action = "观察讨论"
        tags.append("观察")

    if candidate.get("is_today_recommendation"):
        tags.append("今日重点")
    origin = str(candidate.get("candidate_origin_label") or "").strip()
    if origin and origin not in tags:
        tags.append(origin)
    bucket = str(candidate.get("strategy_bucket") or "").strip()
    if bucket and bucket not in tags:
        tags.append(bucket)

    return {
        "family": family,
        "primary_action": primary_action,
        "strategy_tags": list(dict.fromkeys(tag for tag in tags if tag)),
    }


def paper_validation_cycle(profile: dict[str, Any]) -> dict[str, Any]:
    family = str(profile.get("family") or "observe")
    if family == "board":
        return {
            "code": "board_1_3d",
            "label": "1-3 个完整交易日先验，最多 5 日内必须复核",
            "min_trading_days": 1,
            "review_trading_days": 3,
            "max_trading_days": 5,
        }
    if family == "long_swing":
        return {
            "code": "long_swing_5_20d",
            "label": "5 个交易日初检，10-20 个交易日复核趋势是否兑现",
            "min_trading_days": 5,
            "review_trading_days": 10,
            "max_trading_days": 20,
        }
    if family == "review":
        return {
            "code": "downgrade_review_now",
            "label": "立即进入复盘，不再按今日重点推进",
            "min_trading_days": 0,
            "review_trading_days": 1,
            "max_trading_days": 3,
        }
    return {
        "code": "observe_3_5d",
        "label": "3-5 个交易日观察，先验证数据和入选理由",
        "min_trading_days": 3,
        "review_trading_days": 5,
        "max_trading_days": 10,
    }


def build_paper_research_contract(
    candidate: dict[str, Any],
    *,
    strategy_profile: dict[str, Any] | None = None,
) -> dict[str, Any]:
    profile = strategy_profile or paper_strategy_profile(candidate)
    family = str(profile.get("family") or "observe")
    tracking = candidate.get("tracking") if isinstance(candidate.get("tracking"), dict) else {}
    quality = candidate.get("data_quality") if isinstance(candidate.get("data_quality"), dict) else {}
    selection_reason = str(
        candidate.get("select_reason")
        or candidate.get("focus_reason")
        or candidate.get("reason_cn")
        or "入选理由缺失，需要先回到今日重点详情补证据。"
    )
    expected_path = str(tracking.get("expected_path") or strategy_expected_path("da_ban" if family == "board" else "long_term" if family == "long_swing" else ""))

    if family == "board":
        success_conditions = [
            "入池后 1-3 个完整交易日仍能保持量价弹性，回撤没有快速扩大。",
            "候选理由里的动能、题材或资金关注度没有被最新日线证伪。",
            "若继续走强，需要能在 K 线与量能上看到延续，而不是单日孤立冲高。",
        ]
        failure_conditions = [
            "入池后 2 个完整交易日纸面变化小于等于 -3%，或最大回撤明显扩大。",
            "打板/短线弹性消失，量能跟不上，或风险观察线触发复盘。",
            "新闻、板块或市场环境与原入选逻辑反向，需要降级复盘而不是继续放在重点层。",
        ]
        daily_plan = "每天收盘后先看入池以来涨跌、最大回撤、量能是否延续，再对照原始打板假设打分。"
    elif family == "long_swing":
        success_conditions = [
            "5 个交易日初检没有明显破坏趋势，10-20 个交易日内均线/动能证据继续改善。",
            "入选理由中的长期或波段逻辑仍能被 MA20、MACD、量能和行业线索共同支持。",
            "回撤处在可解释范围内，且不是由数据缺口或单一偶发消息硬撑结论。",
        ]
        failure_conditions = [
            "5 个交易日后纸面变化小于等于 -6%，或价格持续低于关键均线且动能走弱。",
            "最大回撤接近或超过 -10%，且原始趋势修复逻辑无法解释。",
            "行业/题材背景明显转弱，或 LLM/复盘证据认为原入选假设已经不成立。",
        ]
        daily_plan = "每天收盘后更新入池以来表现；每 5 个交易日做一次趋势、行业和消息复核。"
    elif family == "review":
        success_conditions = [
            "作为失败/降级样本完整保留原因，能解释当初策略哪里判断过早或证据不足。",
            "复盘结论能沉淀到策略成绩单，避免同类条件重复误判。",
        ]
        failure_conditions = [
            "无法定位偏离原因，或数据缺失导致样本不能用于策略学习。",
            "继续留在活跃重点层会误导今日研究排序。",
        ]
        daily_plan = "不再追逐表现，重点补齐偏离原因、数据缺口和策略修正规则。"
    else:
        success_conditions = [
            "数据补齐后，入选理由能被完整日线、技术指标、行业/题材线索共同解释。",
            "进入纸面池前能形成明确主动作、验证周期和失败条件。",
        ]
        failure_conditions = [
            "连续复核仍只有模糊理由，或关键行情/行业/新闻证据缺失。",
            "观察期内表现明显偏离但无法归因，应转入降级复盘。",
        ]
        daily_plan = "先补数据和理由，再决定是否升级为今日重点或纸面模拟样本。"

    evidence_sources = [
        "daily_candidate_pool",
        "local_daily_bars",
        "technical_indicators",
        "candidate_tracking",
    ]
    if quality:
        evidence_sources.append("data_quality")
    if candidate.get("candidate_origin_label"):
        evidence_sources.append("candidate_origin")

    return {
        "entry_date": candidate.get("select_date") or candidate.get("signal_date") or "数据缺失",
        "entry_strategy": candidate.get("strategy_lane") or candidate.get("strategy_name") or "候选观察",
        "primary_action": profile.get("primary_action") or "观察讨论",
        "strategy_tags": profile.get("strategy_tags") or [],
        "selection_reason": selection_reason,
        "scenario_projection": {
            "expected_path": expected_path,
            "current_tracking_status": tracking.get("status_label") or "观察中",
            "current_tracking_note": tracking.get("review_note") or "等待后续完整日线验证。",
            "boundary": "研究情景推演，不是确定预测，不是买卖或仓位建议。",
        },
        "validation_cycle": paper_validation_cycle(profile),
        "success_conditions": success_conditions,
        "failure_conditions": failure_conditions,
        "daily_tracking_plan": daily_plan,
        "expected_path": expected_path,
        "llm_decision_status": "pending_discussion",
        "llm_research_opinion": "待在 LLM 研究室基于本地证据讨论；用户确认后仅进入纸面模拟跟踪。",
        "evidence_sources": evidence_sources,
        "data_status_at_entry": quality.get("status") or candidate.get("data_status") or "数据缺失",
        "research_only": True,
        "paper_only": True,
        "no_broker": True,
        "no_real_orders": True,
        "not_investment_advice": True,
        "not_position_advice": True,
    }


def _clean_llm_contract_text(value: Any, *, max_len: int = 600) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    if not text or text == "数据缺失":
        return ""
    return text[:max_len]


def apply_llm_decision_to_paper_contract(
    contract: dict[str, Any],
    *,
    llm_decision_status: str = "",
    llm_research_opinion: str = "",
    llm_primary_action: str = "",
    llm_confidence: str = "",
    llm_validation_days: Any = None,
    llm_success_condition: str = "",
    llm_failure_condition: str = "",
    llm_missing_data: str = "",
    llm_review_sentence: str = "",
) -> dict[str, Any]:
    """Merge a user-confirmed LLM paper decision into the paper contract.

    The merge only affects local paper-research metadata. It does not create
    orders, broker routes, positions, target prices, or real trading actions.
    """
    out = {**contract}
    llm_fields: dict[str, Any] = {}
    decision_status = _clean_llm_contract_text(llm_decision_status)
    opinion = _clean_llm_contract_text(llm_research_opinion, max_len=4000)
    primary_action = _clean_llm_contract_text(llm_primary_action, max_len=120)
    confidence = _clean_llm_contract_text(llm_confidence, max_len=40)
    success = _clean_llm_contract_text(llm_success_condition)
    failure = _clean_llm_contract_text(llm_failure_condition)
    missing = _clean_llm_contract_text(llm_missing_data)
    review_sentence = _clean_llm_contract_text(llm_review_sentence, max_len=800)

    if opinion:
        out["llm_research_opinion"] = opinion
        llm_fields["research_opinion"] = opinion
    if decision_status:
        out["llm_decision_status"] = decision_status
        llm_fields["decision_status"] = decision_status
    elif opinion:
        out["llm_decision_status"] = "user_confirmed_after_llm"
        llm_fields["decision_status"] = out["llm_decision_status"]
    if primary_action:
        out["primary_action"] = primary_action
        llm_fields["primary_action"] = primary_action
    if confidence:
        out["llm_confidence"] = confidence
        llm_fields["confidence"] = confidence

    validation_days = safe_int(llm_validation_days, 0)
    if validation_days > 0:
        cycle = out.get("validation_cycle") if isinstance(out.get("validation_cycle"), dict) else {}
        cycle = {**cycle}
        cycle["llm_validation_days"] = validation_days
        cycle["review_trading_days"] = validation_days
        cycle["max_trading_days"] = max(validation_days, safe_int(cycle.get("max_trading_days"), validation_days))
        cycle["label"] = f"LLM 建议 {validation_days} 个完整交易日后复核；仍以纸面研究为边界。"
        out["validation_cycle"] = cycle
        llm_fields["validation_days"] = validation_days

    if success:
        existing = [str(item) for item in out.get("success_conditions") or [] if item]
        out["success_conditions"] = list(dict.fromkeys([success, *existing]))[:5]
        llm_fields["success_condition"] = success
    if failure:
        existing = [str(item) for item in out.get("failure_conditions") or [] if item]
        out["failure_conditions"] = list(dict.fromkeys([failure, *existing]))[:5]
        llm_fields["failure_condition"] = failure
    if missing:
        out["llm_missing_data"] = missing
        llm_fields["missing_data"] = missing
    if review_sentence:
        scenario = out.get("scenario_projection") if isinstance(out.get("scenario_projection"), dict) else {}
        scenario = {**scenario, "llm_review_sentence": review_sentence}
        out["scenario_projection"] = scenario
        llm_fields["review_sentence"] = review_sentence

    if llm_fields:
        out["llm_structured_decision"] = {
            **llm_fields,
            "source": "agent_chat_user_confirmed",
            "research_only": True,
            "paper_only": True,
            "no_broker": True,
            "no_real_orders": True,
            "not_investment_advice": True,
            "not_position_advice": True,
        }
    return out


def ensure_paper_item_research_contract(item: dict[str, Any]) -> dict[str, Any]:
    """Backfill the v1 paper research contract onto older watchlist items.

    Existing state files were created before the paper-loop contract existed.
    Reading should upgrade them in memory so UI/Agent/scorecard never show
    "undefined" or blank critical fields. The writer will persist the enriched
    version on the next normal refresh/sync.
    """
    out = {**item}
    candidate_like = {
        "symbol": out.get("symbol"),
        "name": out.get("name"),
        "select_date": out.get("entry_date") or out.get("added_at"),
        "signal_date": out.get("entry_date") or out.get("added_at"),
        "detail_lane": out.get("detail_lane") or out.get("source_candidate_key", "").split("|")[-1],
        "strategy_lane": out.get("strategy") or out.get("strategy_lane"),
        "strategy_bucket": out.get("strategy_bucket"),
        "candidate_origin_label": out.get("candidate_origin_label"),
        "select_reason": out.get("selection_reason") or out.get("review_notes") or out.get("evidence_summary"),
        "focus_reason": out.get("evidence_summary") or out.get("review_reason"),
        "reason_cn": out.get("evidence_summary") or out.get("tracking_message"),
        "tracking": {
            "status_label": out.get("review_label") or out.get("review_status") or "纸面跟踪中",
            "review_note": out.get("tracking_message") or out.get("review_reason") or "",
            "expected_path": out.get("expected_path") or "",
        },
        "data_quality": {"status": "历史纸面样本", "latest_date": out.get("latest_reference_date") or ""},
    }
    profile = paper_strategy_profile(candidate_like)
    contract = out.get("paper_research_contract") if isinstance(out.get("paper_research_contract"), dict) else {}
    if not contract:
        contract = build_paper_research_contract(candidate_like, strategy_profile=profile)
    out.setdefault("primary_action", contract.get("primary_action") or profile["primary_action"])
    out.setdefault("strategy_tags", contract.get("strategy_tags") or profile["strategy_tags"])
    out.setdefault("selection_reason", contract.get("selection_reason") or "历史纸面样本，原始入选理由需回看候选详情。")
    out.setdefault("expected_path", contract.get("expected_path") or strategy_expected_path(""))
    out.setdefault("scenario_projection", contract.get("scenario_projection") or {})
    out.setdefault("validation_cycle", contract.get("validation_cycle") or paper_validation_cycle(profile))
    out.setdefault("success_conditions", contract.get("success_conditions") or [])
    out.setdefault("failure_conditions", contract.get("failure_conditions") or [])
    out.setdefault("daily_tracking_plan", contract.get("daily_tracking_plan") or "每天收盘后刷新纸面表现并复盘是否符合原始预期。")
    out.setdefault("llm_decision_status", contract.get("llm_decision_status") or "pending_discussion")
    out.setdefault("llm_research_opinion", contract.get("llm_research_opinion") or "待在 LLM 研究室基于本地证据讨论。")
    out.setdefault("paper_research_contract", contract)
    out.setdefault("expectation_fit_status", "pending")
    out.setdefault("expectation_fit_label", "等待更多样本")
    out.setdefault("expectation_fit_reason", "历史纸面样本已补齐研究契约，等待下一次刷新验证原始预期。")
    out.setdefault("research_only", True)
    out.setdefault("paper_only", True)
    out.setdefault("no_broker", True)
    out.setdefault("no_real_orders", True)
    out.setdefault("not_investment_advice", True)
    out.setdefault("not_position_advice", True)
    return out


def add_candidate_to_paper_simulation_pool(
    symbol: str,
    *,
    entry_date: str = "",
    lane: str = "",
    note: str = "",
    llm_research_opinion: str = "",
    llm_decision_status: str = "",
    llm_primary_action: str = "",
    llm_confidence: str = "",
    llm_validation_days: Any = None,
    llm_success_condition: str = "",
    llm_failure_condition: str = "",
    llm_missing_data: str = "",
    llm_review_sentence: str = "",
) -> dict[str, Any]:
    candidate = find_visible_candidate(symbol, entry_date, lane)
    if not candidate:
        message = "当前页面候选池中找不到这只股票，请先刷新候选池或重新生成今日研究候选。"
        return {
            "ok": False,
            "status": "candidate_not_found",
            "message": message,
            "message_for_ui": message,
            "research_only": True,
            "paper_only": True,
            "no_broker": True,
        }
    watchlist = load_paper_simulation_watchlist()
    if watchlist.get("ok") is False:
        return watchlist
    items = [item for item in watchlist.get("items") or [] if isinstance(item, dict)]
    key = paper_simulation_candidate_key(candidate)
    clean_symbol = normalize_symbol(candidate.get("symbol"))
    evidence = candidate_paper_evidence(candidate)
    contract = evidence["paper_research_contract"]
    profile = evidence["strategy_profile"]
    contract = apply_llm_decision_to_paper_contract(
        contract,
        llm_decision_status=llm_decision_status,
        llm_research_opinion=llm_research_opinion,
        llm_primary_action=llm_primary_action,
        llm_confidence=llm_confidence,
        llm_validation_days=llm_validation_days,
        llm_success_condition=llm_success_condition,
        llm_failure_condition=llm_failure_condition,
        llm_missing_data=llm_missing_data,
        llm_review_sentence=llm_review_sentence,
    )
    now = datetime.now().isoformat(timespec="seconds")
    latest_price = finite_float(candidate.get("current_price"))
    item = {
        "watchlist_id": key or f"paper-{clean_symbol}",
        "symbol": clean_symbol,
        "name": candidate.get("name") or clean_symbol,
        "strategy": candidate.get("strategy_lane") or candidate.get("strategy_name") or "observe",
        "strategy_bucket": candidate.get("strategy_bucket") or "",
        "primary_action": contract.get("primary_action") or profile["primary_action"],
        "strategy_tags": profile["strategy_tags"],
        "source_task": "daily_candidate_to_paper_simulation",
        "source_candidate_key": key,
        "candidate_origin_label": candidate.get("candidate_origin_label") or "",
        "entry_date": candidate.get("select_date") or candidate.get("signal_date") or "",
        "added_at": now,
        "updated_at": now,
        "initial_reference_price": latest_price,
        "latest_reference_price": latest_price,
        "latest_reference_date": candidate.get("data_quality", {}).get("latest_date") if isinstance(candidate.get("data_quality"), dict) else "",
        "review_status": "watching",
        "review_notes": note or "从候选池手动加入研究观察；仅观察，不产生持仓与盈亏。",
        "rationale": evidence["lines"],
        "evidence_summary": evidence["summary"],
        "agent_question_suggestion": evidence["agent_question_suggestion"],
        "selection_reason": contract["selection_reason"],
        "expected_path": contract["expected_path"],
        "scenario_projection": contract["scenario_projection"],
        "validation_cycle": contract["validation_cycle"],
        "success_conditions": contract["success_conditions"],
        "failure_conditions": contract["failure_conditions"],
        "daily_tracking_plan": contract["daily_tracking_plan"],
        "llm_decision_status": contract["llm_decision_status"],
        "llm_research_opinion": contract["llm_research_opinion"],
        "llm_confidence": contract.get("llm_confidence") or "",
        "llm_missing_data": contract.get("llm_missing_data") or "",
        "llm_structured_decision": contract.get("llm_structured_decision") or {},
        "paper_research_contract": contract,
        "expectation_fit_status": "pending",
        "expectation_fit_label": "等待更多样本",
        "expectation_fit_reason": "刚进入纸面模拟池，等待后续完整日线验证原始预期。",
        "max_gain_since_added": 0,
        "max_drawdown_since_added": 0,
        "research_only": True,
        "paper_only": True,
        "no_broker": True,
        "no_real_orders": True,
        "not_investment_advice": True,
        "not_position_advice": True,
    }
    replaced = False
    for index, existing in enumerate(items):
        if normalize_symbol(existing.get("symbol")) == clean_symbol:
            item["added_at"] = existing.get("added_at") or item["added_at"]
            item["initial_reference_price"] = existing.get("initial_reference_price") or item["initial_reference_price"]
            items[index] = {**existing, **item}
            replaced = True
            break
    if not replaced:
        items.append(item)
    watchlist.update(
        {
            "ok": True,
            "status": "passed",
            "watchlist_id": "paper_watchlist",
            "items": items,
            "watchlist_count": len(items),
            "updated_at": now,
            "research_only": True,
            "paper_only": True,
            "broker_boundary": "No Broker Route / No real orders.",
            "disclaimer": "Research Only / Paper Account Only / Not Investment Advice / Not Position Advice.",
        }
    )
    write_paper_simulation_watchlist(watchlist)
    message = (
        f"{item['name']} {clean_symbol} 已{'更新到' if replaced else '加入'}纸面模拟池。"
        "后续可在绩效复盘里跟踪，不会连接券商或生成真实订单。"
    )
    return {
        "ok": True,
        "status": "updated" if replaced else "added",
        "message": message,
        "message_for_ui": message,
        "item": item,
        "watchlist_count": len(items),
        "path": project_relative_path(PAPER_WATCHLIST_JSON),
        "agent_question_suggestion": evidence["agent_question_suggestion"],
        "research_only": True,
        "paper_only": True,
        "no_broker": True,
        "no_real_orders": True,
    }


def _round_float(value: Any, digits: int = 2) -> float | None:
    number = finite_float(value)
    return round(number, digits) if number is not None else None


def _paper_simulation_review_state(item: dict[str, Any], latest_return: float | None, max_drawdown: float | None, elapsed_days: int) -> dict[str, str]:
    strategy = str(item.get("strategy") or item.get("strategy_lane") or "")
    if latest_return is None:
        return {
            "review_status": "data_gap",
            "review_label": "数据不足",
            "review_tone": "missing",
            "review_reason": "本地日线不足，暂时不能评估入池后的表现。",
        }
    if "打板" in strategy and elapsed_days >= 2 and latest_return <= -3:
        return {
            "review_status": "board_downgrade_review",
            "review_label": "打板假设偏离，待降级复盘",
            "review_tone": "warn",
            "review_reason": "入池后短期表现与打板复核假设偏离，适合放入降级复盘里找原因。",
        }
    if "打板" in strategy and latest_return >= 5:
        return {
            "review_status": "board_following_expected",
            "review_label": "打板节奏阶段符合",
            "review_tone": "ok",
            "review_reason": "入池后短期弹性仍然存在，先作为纸面样本继续记录表现。",
        }
    if "长期" in strategy and elapsed_days >= 5 and latest_return <= -6:
        return {
            "review_status": "long_thesis_review",
            "review_label": "长期假设需复盘",
            "review_tone": "warn",
            "review_reason": "入池后阶段回撤较明显，需要复核原始长期/波段逻辑是否仍有证据支持。",
        }
    if max_drawdown is not None and max_drawdown <= -10:
        return {
            "review_status": "drawdown_review",
            "review_label": "回撤偏大，待复盘",
            "review_tone": "warn",
            "review_reason": "纸面跟踪区间最大回撤偏大，需要解释原因并记录是否偏离入池假设。",
        }
    if elapsed_days <= 1:
        return {
            "review_status": "paper_pending",
            "review_label": "刚入池，等待样本",
            "review_tone": "watch",
            "review_reason": "入池后交易日还少，先记录，不提前下结论。",
        }
    return {
        "review_status": "paper_tracking",
        "review_label": "纸面跟踪中",
        "review_tone": "watch",
        "review_reason": "入池后表现仍在可复盘区间内，继续等待更多完整日线验证。",
    }


def _paper_expectation_fit_state(
    item: dict[str, Any],
    *,
    latest_return: float | None,
    max_drawdown: float | None,
    elapsed_days: int,
) -> dict[str, str]:
    contract = item.get("paper_research_contract") if isinstance(item.get("paper_research_contract"), dict) else {}
    cycle = item.get("validation_cycle") if isinstance(item.get("validation_cycle"), dict) else contract.get("validation_cycle") if isinstance(contract.get("validation_cycle"), dict) else {}
    primary_action = str(item.get("primary_action") or contract.get("primary_action") or item.get("strategy") or "")
    review_status = str(item.get("review_status") or "")
    min_days = safe_int(cycle.get("min_trading_days"), 1)
    review_days = safe_int(cycle.get("review_trading_days"), min_days)

    if latest_return is None:
        return {
            "expectation_fit_status": "data_gap",
            "expectation_fit_label": "数据不足",
            "expectation_fit_reason": "本地完整日线不足，暂时不能验证原始研究预期。",
        }
    if review_status in {"data_gap"}:
        return {
            "expectation_fit_status": "data_gap",
            "expectation_fit_label": "数据不足",
            "expectation_fit_reason": "纸面跟踪刷新结果显示数据不足，不能给出符合/偏离判断。",
        }
    if review_status in {"smart_stop_triggered_review", "board_downgrade_review", "long_thesis_review", "drawdown_review"}:
        return {
            "expectation_fit_status": "deviated",
            "expectation_fit_label": "偏离原始预期",
            "expectation_fit_reason": str(item.get("review_reason") or "纸面表现触发复盘规则，需要进入降级复盘。"),
        }
    if "打板" in primary_action:
        if elapsed_days < min_days:
            return {
                "expectation_fit_status": "pending",
                "expectation_fit_label": "等待更多样本",
                "expectation_fit_reason": f"打板复核至少需要 {min_days} 个完整交易日；当前 {elapsed_days} 个。",
            }
        if latest_return >= 3 and (max_drawdown is None or max_drawdown > -8):
            return {
                "expectation_fit_status": "fit",
                "expectation_fit_label": "符合原始预期",
                "expectation_fit_reason": "短线弹性仍存在，且回撤没有触发打板复核失效条件。",
            }
        if elapsed_days >= review_days and latest_return <= 0:
            return {
                "expectation_fit_status": "deviated",
                "expectation_fit_label": "偏离原始预期",
                "expectation_fit_reason": "打板样本到复核窗口仍未体现短线弹性，应降级复盘入选逻辑。",
            }
    if "长期" in primary_action or "波段" in primary_action:
        if elapsed_days < min_days:
            return {
                "expectation_fit_status": "pending",
                "expectation_fit_label": "等待更多样本",
                "expectation_fit_reason": f"长期/波段样本至少需要 {min_days} 个完整交易日初检；当前 {elapsed_days} 个。",
            }
        if latest_return >= 0 and (max_drawdown is None or max_drawdown > -10):
            return {
                "expectation_fit_status": "fit",
                "expectation_fit_label": "符合原始预期",
                "expectation_fit_reason": "趋势修复方向没有被纸面跟踪证伪，继续按周期复核。",
            }
        if elapsed_days >= review_days and latest_return <= -6:
            return {
                "expectation_fit_status": "deviated",
                "expectation_fit_label": "偏离原始预期",
                "expectation_fit_reason": "长期/波段复核窗口内表现偏弱，需要复盘原始趋势假设。",
            }
    if max_drawdown is not None and max_drawdown <= -10:
        return {
            "expectation_fit_status": "deviated",
            "expectation_fit_label": "偏离原始预期",
            "expectation_fit_reason": "入池后最大回撤较大，已经削弱原始观察假设。",
        }
    return {
        "expectation_fit_status": "pending",
        "expectation_fit_label": "等待更多样本",
        "expectation_fit_reason": "当前表现还没有足够证据证明原始预期兑现或失效。",
    }


def _paper_simulation_smart_stop_context(
    item: dict[str, Any],
    window: "pd.DataFrame",
    *,
    initial_price: float,
    latest_price: float,
) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    for _, row in window.iterrows():
        date_value = row.get("date")
        if hasattr(date_value, "date"):
            date_text = str(date_value.date())
        else:
            date_text = str(date_value or "")[:10]
        close = finite_float(row.get("close"))
        high = finite_float(row.get("high")) or close
        low = finite_float(row.get("low")) or close
        rows.append({"date": date_text, "high": high, "low": low, "close": close})
    try:
        from quantdesk.risk.smart_stop import build_smart_stop_for_position

        payload = build_smart_stop_for_position(
            {
                "symbol": normalize_symbol(item.get("symbol")),
                "name": item.get("name") or "",
                "strategy": item.get("strategy") or item.get("strategy_lane") or "",
                "entry_price": initial_price,
                "reference_price": initial_price,
                "latest_price": latest_price,
                "entry_date": item.get("entry_date") or item.get("added_at") or "",
                "source": "paper_simulation_watchlist",
            },
            daily_rows=rows,
        )
    except Exception as exc:  # noqa: BLE001 - risk evidence is optional enrichment
        LOG.warning("paper_simulation_smart_stop_failed: symbol=%s error=%s", item.get("symbol"), exc)
        return {
            "available": False,
            "trigger_status": "unavailable",
            "message": f"风险观察线暂不可用：{exc}",
            "research_only": True,
            "paper_only": True,
            "no_broker": True,
            "no_real_orders": True,
        }
    return {
        "available": True,
        "trigger_status": payload.get("trigger_status") or "unknown",
        "severity": payload.get("severity") or "",
        "distance_to_stop_pct": _round_float((finite_float(payload.get("distance_to_stop_pct")) or 0.0) * 100, 2)
        if payload.get("distance_to_stop_pct") is not None else None,
        "drawdown_from_entry_pct": _round_float((finite_float(payload.get("drawdown_from_entry")) or 0.0) * 100, 2)
        if payload.get("drawdown_from_entry") is not None else None,
        "risk_observation_line": _round_float(payload.get("composite_stop_price")),
        "trigger_reason": payload.get("trigger_reason") or "",
        "rationale": payload.get("rationale") or [],
        "warnings": payload.get("warnings") or [],
        "paper_exit_plan": payload.get("paper_exit_plan") if isinstance(payload.get("paper_exit_plan"), dict) else {},
        "research_only": True,
        "paper_only": True,
        "no_broker": True,
        "no_real_orders": True,
        "not_investment_advice": True,
        "not_position_advice": True,
        "message": "本地纸面风险观察线，只用于复盘，不生成真实订单。",
    }


def _apply_paper_smart_stop_review(item: dict[str, Any], smart_stop: dict[str, Any]) -> dict[str, Any]:
    status = str(smart_stop.get("trigger_status") or "")
    reason = str(smart_stop.get("trigger_reason") or "").strip()
    if isinstance(smart_stop.get("paper_exit_plan"), dict):
        item["paper_exit_plan"] = smart_stop["paper_exit_plan"]
    if status == "triggered":
        item.update(
            {
                "review_status": "smart_stop_triggered_review",
                "review_label": "风险观察线触发，待复盘",
                "review_tone": "warn",
                "review_reason": (
                    "纸面风险观察线已触发，需要复核原始候选假设是否失效；"
                    "这不是交易指令，也不会连接券商或生成真实订单。"
                    + (f" 证据：{reason}" if reason else "")
                ),
            }
        )
    return item


def refresh_paper_simulation_item_tracking(item: dict[str, Any]) -> dict[str, Any]:
    """Refresh one paper-watch item from local daily bars only."""
    tracked = {**item}
    symbol = normalize_symbol(tracked.get("symbol"))
    now = datetime.now().isoformat(timespec="seconds")
    tracked["last_tracking_refresh_at"] = now
    tracked.setdefault("research_only", True)
    tracked.setdefault("paper_only", True)
    tracked.setdefault("no_broker", True)
    tracked.setdefault("no_real_orders", True)
    if str(tracked.get("paper_lifecycle_state") or "") == "archived":
        tracked.setdefault("review_status", "manual_archived")
        tracked.setdefault("review_label", "已归档保留")
        tracked.setdefault("review_tone", "archived")
        tracked["tracking_message"] = "该样本已归档保留，只用于回看；刷新纸面池时不再改变它的复盘状态。"
        return tracked
    if not symbol:
        tracked.update(_paper_simulation_review_state(tracked, None, None, 0))
        tracked.update(_paper_expectation_fit_state(tracked, latest_return=None, max_drawdown=None, elapsed_days=0))
        tracked["tracking_message"] = "股票代码缺失，无法刷新纸面跟踪。"
        return tracked

    frame = load_stock_data(symbol)
    if frame.empty or "date" not in frame.columns or "close" not in frame.columns:
        tracked.update(_paper_simulation_review_state(tracked, None, None, 0))
        tracked.update(_paper_expectation_fit_state(tracked, latest_return=None, max_drawdown=None, elapsed_days=0))
        tracked["tracking_message"] = "本地日线缺失，纸面模拟池只能保留样本，暂不能刷新表现。"
        return tracked

    work = frame.copy()
    work["date"] = pd.to_datetime(work["date"], errors="coerce")
    work["close"] = pd.to_numeric(work["close"], errors="coerce")
    work = work.dropna(subset=["date", "close"])
    work = work[work["close"] > 0].sort_values("date")
    if work.empty:
        tracked.update(_paper_simulation_review_state(tracked, None, None, 0))
        tracked.update(_paper_expectation_fit_state(tracked, latest_return=None, max_drawdown=None, elapsed_days=0))
        tracked["tracking_message"] = "本地日线收盘价无效，暂不能刷新表现。"
        return tracked

    entry_text = str(tracked.get("entry_date") or tracked.get("added_at") or "")[:10]
    entry_date = pd.to_datetime(entry_text, errors="coerce") if entry_text else pd.NaT
    if pd.isna(entry_date):
        window = work
        entry_date_text = str(work.iloc[0]["date"].date())
    else:
        window = work[work["date"] >= entry_date]
        entry_date_text = str(entry_date.date())
    if window.empty:
        latest = work.iloc[-1]
        tracked.update(_paper_simulation_review_state(tracked, None, None, 0))
        tracked.update(_paper_expectation_fit_state(tracked, latest_return=None, max_drawdown=None, elapsed_days=0))
        tracked.update(
            {
                "latest_reference_price": _round_float(latest.get("close")),
                "latest_reference_date": str(latest.get("date").date()),
                "tracking_message": "入池日期晚于本地最新日线，等待下一次数据更新后再评估。",
            }
        )
        return tracked

    initial_price = finite_float(tracked.get("initial_reference_price"))
    if initial_price is None or initial_price <= 0:
        initial_price = finite_float(window.iloc[0].get("close"))
        tracked["initial_reference_price"] = _round_float(initial_price)
    if initial_price is None or initial_price <= 0:
        tracked.update(_paper_simulation_review_state(tracked, None, None, 0))
        tracked.update(_paper_expectation_fit_state(tracked, latest_return=None, max_drawdown=None, elapsed_days=0))
        tracked["tracking_message"] = "入池参考价无效，暂不能计算纸面表现。"
        return tracked

    latest = window.iloc[-1]
    latest_price = finite_float(latest.get("close"))
    if latest_price is None or latest_price <= 0:
        tracked.update(_paper_simulation_review_state(tracked, None, None, len(window)))
        tracked.update(_paper_expectation_fit_state(tracked, latest_return=None, max_drawdown=None, elapsed_days=len(window)))
        tracked["tracking_message"] = "最新收盘价无效，暂不能计算纸面表现。"
        return tracked

    closes = pd.to_numeric(window["close"], errors="coerce").dropna()
    returns = (closes / initial_price - 1.0) * 100.0
    latest_return = float((latest_price / initial_price - 1.0) * 100.0)
    max_gain = float(returns.max()) if not returns.empty else latest_return
    min_return = float(returns.min()) if not returns.empty else latest_return
    peaks = closes.cummax().clip(lower=initial_price)
    max_drawdown = min(0.0, float(((closes / peaks - 1.0) * 100.0).min()))
    elapsed_days = max(0, int(len(window) - 1))
    review = _paper_simulation_review_state(tracked, latest_return, max_drawdown, elapsed_days)
    tracked.update(review)
    smart_stop = _paper_simulation_smart_stop_context(
        tracked,
        window,
        initial_price=initial_price,
        latest_price=latest_price,
    )
    tracked["smart_stop"] = smart_stop
    tracked = _apply_paper_smart_stop_review(tracked, smart_stop)
    tracked.update(
        _paper_expectation_fit_state(
            tracked,
            latest_return=latest_return,
            max_drawdown=max_drawdown,
            elapsed_days=elapsed_days,
        )
    )
    tracked.update(
        {
            "entry_date": tracked.get("entry_date") or entry_date_text,
            "latest_reference_price": _round_float(latest_price),
            "latest_reference_date": str(latest.get("date").date()),
            "return_since_added_pct": _round_float(latest_return),
            "max_gain_since_added": _round_float(max_gain),
            "max_drawdown_since_added": _round_float(max_drawdown),
            "min_return_since_added_pct": _round_float(min_return),
            "tracking_metric_version": "peak-to-trough-v2",
            "elapsed_trading_days": elapsed_days,
            "tracking_message": (
                f"入池以来 {elapsed_days} 个完整交易日，纸面变化 {latest_return:+.2f}%，"
                f"区间最大涨幅 {max_gain:+.2f}%，区间最大回撤 {max_drawdown:+.2f}%。"
            ),
        }
    )
    return tracked


def refresh_paper_simulation_watchlist_tracking() -> dict[str, Any]:
    watchlist = load_paper_simulation_watchlist()
    if watchlist.get("ok") is False:
        return watchlist
    items = [item for item in watchlist.get("items") or [] if isinstance(item, dict)]
    now = datetime.now().isoformat(timespec="seconds")
    if not items:
        watchlist.update(
            {
                "watchlist_count": 0,
                "updated_at": now,
                "paper_tracking_summary": {
                    "updated_count": 0,
                    "data_gap_count": 0,
                    "needs_review_count": 0,
                    "message": "纸面模拟池暂无样本；先从候选详情手动加入。",
                    "updated_at": now,
                },
            }
        )
        write_paper_simulation_watchlist(watchlist)
        return {
            "ok": True,
            "status": "empty",
            "watchlist_count": 0,
            "items": [],
            "paper_tracking_summary": watchlist["paper_tracking_summary"],
            "message": "纸面模拟池暂无样本；先从候选详情手动加入。",
            "message_for_ui": "纸面模拟池暂无样本；先从候选详情手动加入。",
            "research_only": True,
            "paper_only": True,
            "no_broker": True,
        }

    refreshed = [refresh_paper_simulation_item_tracking(item) for item in items]
    active_refreshed = [item for item in refreshed if str(item.get("paper_lifecycle_state") or "") != "archived"]
    archived_count = len(refreshed) - len(active_refreshed)
    data_gap_count = sum(1 for item in active_refreshed if item.get("review_status") == "data_gap")
    needs_review_count = sum(1 for item in active_refreshed if str(item.get("review_tone") or "") == "warn")
    paper_exit_plan_count = sum(
        1
        for item in active_refreshed
        if isinstance(item.get("paper_exit_plan"), dict)
        and item["paper_exit_plan"].get("paper_exit_candidate")
    )
    status_counts: dict[str, int] = {}
    fit_counts: dict[str, int] = {}
    strategy_fit_counts: dict[str, dict[str, int]] = {}
    for item in active_refreshed:
        status = str(item.get("review_status") or "unknown")
        status_counts[status] = status_counts.get(status, 0) + 1
        fit = str(item.get("expectation_fit_label") or "等待更多样本")
        fit_counts[fit] = fit_counts.get(fit, 0) + 1
        strategy = str(item.get("primary_action") or item.get("strategy") or "观察讨论")
        bucket = strategy_fit_counts.setdefault(strategy, {})
        bucket[fit] = bucket.get(fit, 0) + 1
    summary = {
        "updated_count": len(active_refreshed) - data_gap_count,
        "data_gap_count": data_gap_count,
        "needs_review_count": needs_review_count,
        "paper_exit_plan_count": paper_exit_plan_count,
        "active_count": len(active_refreshed),
        "archived_count": archived_count,
        "status_counts": status_counts,
        "expectation_fit_counts": fit_counts,
        "strategy_expectation_fit_counts": strategy_fit_counts,
        "updated_at": now,
        "message": (
            f"已刷新纸面模拟池 {len(active_refreshed)} 个活跃样本；"
            f"{needs_review_count} 个需要复盘，{data_gap_count} 个数据不足，{archived_count} 个已归档。"
        ),
    }
    watchlist.update(
        {
            "ok": True,
            "status": "passed",
            "items": refreshed,
            "watchlist_count": len(refreshed),
            "active_watchlist_count": len(active_refreshed),
            "archived_watchlist_count": archived_count,
            "updated_at": now,
            "paper_tracking_summary": summary,
            "research_only": True,
            "paper_only": True,
            "broker_boundary": "No Broker Route / No real orders.",
            "disclaimer": "Research Only / Paper Account Only / Not Investment Advice / Not Position Advice.",
        }
    )
    write_paper_simulation_watchlist(watchlist)
    return {
        "ok": True,
        "status": "refreshed",
        "watchlist_count": len(refreshed),
        "items": refreshed[:12],
        "paper_tracking_summary": summary,
        "message": summary["message"],
        "message_for_ui": summary["message"],
        "path": project_relative_path(PAPER_WATCHLIST_JSON),
        "research_only": True,
        "paper_only": True,
        "no_broker": True,
        "no_real_orders": True,
    }


def paper_simulation_context_payload(limit: int = 8) -> dict[str, Any]:
    watchlist = load_paper_simulation_watchlist()
    items = [item for item in watchlist.get("items") or [] if isinstance(item, dict)]
    preview = []
    for item in items[:limit]:
        preview.append(
            {
                "symbol": normalize_symbol(item.get("symbol")),
                "name": item.get("name"),
                "strategy": item.get("strategy"),
                "primary_action": item.get("primary_action"),
                "strategy_tags": item.get("strategy_tags") if isinstance(item.get("strategy_tags"), list) else [],
                "entry_date": item.get("entry_date"),
                "review_status": item.get("review_status"),
                "review_label": item.get("review_label"),
                "expectation_fit_status": item.get("expectation_fit_status"),
                "expectation_fit_label": item.get("expectation_fit_label"),
                "expectation_fit_reason": item.get("expectation_fit_reason"),
                "return_since_added_pct": item.get("return_since_added_pct"),
                "max_drawdown_since_added": item.get("max_drawdown_since_added"),
                "elapsed_trading_days": item.get("elapsed_trading_days"),
                "expected_path": item.get("expected_path"),
                "validation_cycle": item.get("validation_cycle") if isinstance(item.get("validation_cycle"), dict) else {},
                "failure_conditions": (item.get("failure_conditions") or [])[:3] if isinstance(item.get("failure_conditions"), list) else [],
                "success_conditions": (item.get("success_conditions") or [])[:3] if isinstance(item.get("success_conditions"), list) else [],
                "daily_tracking_plan": item.get("daily_tracking_plan"),
                "llm_decision_status": item.get("llm_decision_status"),
                "llm_research_opinion": item.get("llm_research_opinion"),
                "smart_stop": item.get("smart_stop") if isinstance(item.get("smart_stop"), dict) else {},
                "paper_exit_plan": item.get("paper_exit_plan") if isinstance(item.get("paper_exit_plan"), dict) else {},
                "tracking_message": item.get("tracking_message"),
            }
        )
    return {
        "watchlist_count": len(items),
        "preview": preview,
        "paper_tracking_summary": watchlist.get("paper_tracking_summary") or {},
        "research_evidence": latest_research_evidence_context(),
        "path": project_relative_path(PAPER_WATCHLIST_JSON),
        "research_only": True,
        "paper_only": True,
        "no_broker": True,
    }


def previous_recommendation_performance(current_signal_date: str = "", limit: int = 10) -> dict[str, Any]:
    """Summarize the recommendation batch immediately before today's focus."""
    ledger = load_candidate_ledger()
    if ledger.empty:
        return {
            "available": False,
            "status": "missing",
            "message_for_ui": "本地 candidate_ledger.csv 缺失，暂时不能复盘上一批候选表现。",
            "items": [],
        }
    rows_by_date: dict[str, list[pd.Series]] = {}
    for _, row in ledger.iterrows():
        if row_candidate_role(row) != "daily_recommendation":
            continue
        signal_date = row_signal_date(row)
        symbol = normalize_symbol(str(row.get("ts_code") or row.get("symbol") or row.get("code") or ""))
        if not signal_date or not symbol.strip("0"):
            continue
        rows_by_date.setdefault(signal_date, []).append(row)
    dates = sorted(rows_by_date.keys(), reverse=True)
    if not dates:
        return {
            "available": False,
            "status": "missing",
            "message_for_ui": "candidate_ledger.csv 中没有 daily_recommendation 记录，暂时不能复盘上一批候选。",
            "items": [],
        }
    current_date = str(current_signal_date or "")[:10]
    previous_dates = [date for date in dates if date < current_date] if current_date else dates[1:]
    if not previous_dates:
        return {
            "available": False,
            "status": "single_batch",
            "current_signal_date": current_date or dates[0],
            "message_for_ui": "目前只有一批研究候选记录，还没有可对照的上一批候选。",
            "items": [],
        }
    batch_date = previous_dates[0]
    items: list[dict[str, Any]] = []
    status_counts: dict[str, int] = {}
    returns: list[float] = []
    drawdowns: list[float] = []
    for row in rows_by_date[batch_date][:limit]:
        symbol = normalize_symbol(str(row.get("ts_code") or row.get("symbol") or row.get("code") or ""))
        candidate = build_candidate(symbol, row)
        tracking = candidate.get("tracking") if isinstance(candidate.get("tracking"), dict) else {}
        status_label = str(tracking.get("status_label") or "数据不足")
        status_counts[status_label] = status_counts.get(status_label, 0) + 1
        ret = finite_float(tracking.get("return_since_select"))
        drawdown = finite_float(tracking.get("max_drawdown_since_select"))
        if ret is not None:
            returns.append(ret)
        if drawdown is not None:
            drawdowns.append(drawdown)
        items.append(
            {
                "symbol": candidate.get("symbol") or symbol,
                "name": candidate.get("name") or symbol,
                "batch_date": batch_date,
                "strategy_lane": candidate.get("strategy_lane"),
                "status_label": status_label,
                "status_tone": tracking.get("status_tone") or "watch",
                "return_since_select": tracking.get("return_since_select"),
                "max_drawdown_since_select": tracking.get("max_drawdown_since_select"),
                "elapsed_trading_days": tracking.get("elapsed_trading_days"),
                "review_note": tracking.get("review_note"),
                "expected_path": tracking.get("expected_path"),
                "data_status": candidate.get("data_status"),
            }
        )
    deviated = sum(count for label, count in status_counts.items() if "偏离" in label or "数据不足" in label)
    fit = sum(count for label, count in status_counts.items() if "符合" in label)
    return {
        "available": True,
        "status": "passed",
        "batch_date": batch_date,
        "current_signal_date": current_date or dates[0],
        "sample_count": len(items),
        "status_counts": status_counts,
        "fit_count": fit,
        "deviated_or_gap_count": deviated,
        "avg_return_since_select_pct": round(sum(returns) / len(returns), 2) if returns else None,
        "avg_max_drawdown_since_select_pct": round(sum(drawdowns) / len(drawdowns), 2) if drawdowns else None,
        "items": items,
        "message_for_ui": f"上一批候选为 {batch_date}，已按本地日线复盘 {len(items)} 只；仅用于纸面研究验证。",
        "research_only": True,
        "paper_only": True,
        "no_broker": True,
    }


def _first_text(values: list[Any], fallback: str = "数据缺失") -> str:
    for value in values:
        if isinstance(value, list):
            nested = _first_text(value, "")
            if nested:
                return nested
            continue
        text = re.sub(r"\s+", " ", str(value or "")).strip()
        if text and text.lower() != "nan" and text != "数据缺失":
            return text
    return fallback


def _local_fallback_paper_decision(
    expectation_status: str,
    return_pct: Any,
    elapsed_days: int,
    validation_days: int,
) -> dict[str, str]:
    """Deterministic, evidence-based fallback decision for paper-pool items that
    never received an LLM research opinion.

    This is explicitly NOT an LLM call: it is a read of the item's own
    expectation-fit + tracking so the scorecard never shows a bare
    ``pending_discussion``. It is always tagged ``local_fallback`` so it can
    never be mistaken for an external LLM opinion. Research only / paper only.
    """
    ret = finite_float(return_pct)
    within_cycle = bool(validation_days) and elapsed_days < validation_days
    if expectation_status == "deviated":
        return {
            "decision_status": "本地兜底：建议降级复盘",
            "confidence": "偏低",
            "review_sentence": "本地证据兜底：纸面表现已偏离入选假设，建议进入降级复盘并核对失败条件是否触发。",
        }
    if expectation_status == "fit":
        return {
            "decision_status": "本地兜底：符合预期，继续纸面验证",
            "confidence": "中",
            "review_sentence": "本地证据兜底：当前纸面表现符合入选假设，按原验证周期继续跟踪。",
        }
    if expectation_status == "data_gap":
        return {
            "decision_status": "本地兜底：证据缺口，暂缓判定",
            "confidence": "低",
            "review_sentence": "本地证据兜底：关键数据缺口，暂不足以判定，先补齐证据再复核。",
        }
    note = "尚在验证周期内" if within_cycle else "验证周期已满，建议尽快复核"
    return {
        "decision_status": "本地兜底：继续纸面观察验证",
        "confidence": "中" if (ret is None or ret >= -3.0) else "偏低",
        "review_sentence": f"本地证据兜底：{note}，继续观察是否符合原始预期；若跌破失败条件则降级复盘。",
    }


def paper_research_decision_snapshot(item: dict[str, Any]) -> dict[str, Any]:
    """Compact read-only view of the original paper-research decision.

    This is used by the strategy scorecard to compare the original research
    hypothesis with current paper tracking. It never creates orders or position
    instructions.
    """
    contract = item.get("paper_research_contract") if isinstance(item.get("paper_research_contract"), dict) else {}
    structured = item.get("llm_structured_decision") if isinstance(item.get("llm_structured_decision"), dict) else {}
    if not structured and isinstance(contract.get("llm_structured_decision"), dict):
        structured = contract["llm_structured_decision"]
    validation = item.get("validation_cycle") if isinstance(item.get("validation_cycle"), dict) else {}
    if not validation and isinstance(contract.get("validation_cycle"), dict):
        validation = contract["validation_cycle"]
    scenario = item.get("scenario_projection") if isinstance(item.get("scenario_projection"), dict) else {}
    if not scenario and isinstance(contract.get("scenario_projection"), dict):
        scenario = contract["scenario_projection"]
    success_conditions = item.get("success_conditions") if isinstance(item.get("success_conditions"), list) else []
    if not success_conditions and isinstance(contract.get("success_conditions"), list):
        success_conditions = contract["success_conditions"]
    failure_conditions = item.get("failure_conditions") if isinstance(item.get("failure_conditions"), list) else []
    if not failure_conditions and isinstance(contract.get("failure_conditions"), list):
        failure_conditions = contract["failure_conditions"]

    primary_action = _first_text(
        [structured.get("primary_action"), item.get("primary_action"), contract.get("primary_action"), item.get("strategy")],
        "观察讨论",
    )
    validation_days = safe_int(
        structured.get("validation_days")
        or validation.get("llm_validation_days")
        or validation.get("review_trading_days")
        or validation.get("max_trading_days"),
        0,
    )
    expectation_status = str(item.get("expectation_fit_status") or "pending")
    expectation_label = _first_text([item.get("expectation_fit_label"), "等待更多样本"], "等待更多样本")
    if expectation_status == "fit":
        alignment_label = "已兑现研究判断"
        alignment_tone = "ok"
    elif expectation_status == "deviated":
        alignment_label = "偏离研究判断"
        alignment_tone = "warn"
    elif expectation_status == "data_gap":
        alignment_label = "证据缺口，暂不能判定"
        alignment_tone = "missing"
    else:
        alignment_label = "等待验证"
        alignment_tone = "watch"

    # Prefer a real LLM decision; otherwise synthesize a clearly-labelled local
    # evidence-based fallback so the scorecard never shows a bare pending state.
    # The fallback is deterministic and explicitly NOT presented as an LLM call.
    raw_llm_status = _first_text(
        [structured.get("decision_status"), item.get("llm_decision_status"), contract.get("llm_decision_status")],
        "",
    )
    has_llm_decision = bool(raw_llm_status) and raw_llm_status != "pending_discussion"
    if has_llm_decision:
        decision_status = raw_llm_status
        decision_source = "llm"
        decision_source_label = "LLM 研究意见"
        llm_used = True
        confidence = _first_text(
            [structured.get("confidence"), item.get("llm_confidence"), contract.get("llm_confidence")],
            "数据缺失",
        )
        fallback: dict[str, str] = {}
    else:
        fallback = _local_fallback_paper_decision(
            expectation_status,
            item.get("return_since_added_pct"),
            safe_int(item.get("elapsed_trading_days"), 0),
            validation_days,
        )
        decision_status = fallback["decision_status"]
        decision_source = "local_fallback"
        decision_source_label = "本地证据兜底（非外部 LLM）"
        llm_used = False
        confidence = fallback["confidence"]

    return {
        "symbol": normalize_symbol(item.get("symbol")),
        "name": item.get("name") or normalize_symbol(item.get("symbol")),
        "entry_date": item.get("entry_date") or contract.get("entry_date") or "数据缺失",
        "decision_status": decision_status,
        "decision_source": decision_source,
        "decision_source_label": decision_source_label,
        "llm_used": llm_used,
        "primary_action": primary_action,
        "confidence": confidence,
        "validation_days": validation_days if validation_days > 0 else "数据缺失",
        "success_condition": _first_text([structured.get("success_condition"), success_conditions]),
        "failure_condition": _first_text([structured.get("failure_condition"), failure_conditions]),
        "missing_data": _first_text([structured.get("missing_data"), item.get("llm_missing_data"), contract.get("llm_missing_data")], "暂无关键缺口"),
        "review_sentence": _first_text(
            [
                structured.get("review_sentence"),
                fallback.get("review_sentence"),
                scenario.get("llm_review_sentence"),
                item.get("expectation_fit_reason"),
                item.get("tracking_message"),
            ],
            "等待后续纸面跟踪验证。",
        ),
        "llm_research_opinion_excerpt": _first_text([item.get("llm_research_opinion"), contract.get("llm_research_opinion")], "尚未写入 LLM 研究意见")[:220],
        "expectation_fit_status": expectation_status,
        "expectation_fit_label": expectation_label,
        "expectation_fit_reason": _first_text([item.get("expectation_fit_reason"), item.get("tracking_message")], "等待后续纸面跟踪。"),
        "alignment_label": alignment_label,
        "alignment_tone": alignment_tone,
        "return_since_added_pct": item.get("return_since_added_pct"),
        "max_drawdown_since_added": item.get("max_drawdown_since_added"),
        "elapsed_trading_days": item.get("elapsed_trading_days"),
        "review_label": item.get("review_label") or item.get("review_status") or "",
        "research_only": True,
        "paper_only": True,
        "no_broker": True,
        "no_real_orders": True,
    }


def build_market_background_summary() -> dict[str, Any]:
    """Honest market / sector / news background for the daily scorecard.

    Reuses the same local sources as ``/api/daily/overview`` (benchmark index,
    spot breadth, sector stats) plus the news cache. Every sub-source degrades
    visibly (status + hint) instead of fabricating data. No broker route, no
    orders, no position advice — this is research-only context.
    """
    background: dict[str, Any] = {
        "available": False,
        "sentiment": "",
        "index": {},
        "breadth": {},
        "sector_top_gainers": [],
        "sector_top_losers": [],
        "news": {"status": "unavailable"},
        "hint": "市场/板块/新闻背景来自本地快照或缓存；缺失时降级显示，不伪造、不预测。",
    }
    try:
        hs300 = load_benchmark_index("hs300")
        sh_index = load_benchmark_index("sh_index")
        breadth = spot_market_breadth()
        sentiment = stock_explainer.generate_market_sentiment(
            finite_float(hs300.get("change")),
            finite_float(sh_index.get("change")),
            int(breadth.get("advancing_count", 0) or 0),
            int(breadth.get("declining_count", 0) or 0),
        )
        sector_stats = calculate_sector_stats()
        background.update(
            {
                "available": True,
                "sentiment": sentiment,
                "index": {
                    "hs300": {"value": hs300.get("value"), "change": hs300.get("change"), "date": hs300.get("date")},
                    "sh_index": {"value": sh_index.get("value"), "change": sh_index.get("change"), "date": sh_index.get("date")},
                },
                "breadth": {
                    "advancing_count": breadth.get("advancing_count"),
                    "declining_count": breadth.get("declining_count"),
                    "flat_count": breadth.get("flat_count"),
                    "total_amount": breadth.get("total_amount"),
                    "sample_count": breadth.get("sample_count"),
                },
                "sector_top_gainers": list(sector_stats.get("gainers") or [])[:5],
                "sector_top_losers": list(sector_stats.get("losers") or [])[:5],
            }
        )
    except Exception as exc:  # noqa: BLE001 - market background must never break the scorecard
        LOG.warning("scorecard_market_background_failed: %s", exc)
        background["error"] = str(exc)[:160]
        background["hint"] = "市场快照暂不可用（本地样本缺失或读取失败）；已降级显示，不伪造行情。"
    try:
        news = news_cache_stats()
        latest = str(news.get("latest_date") or "")
        age_days = None
        if len(latest) == 8:
            try:
                age_days = (datetime.now() - datetime.strptime(latest, "%Y%m%d")).days
            except (ValueError, TypeError):
                age_days = None
        background["news"] = {
            "latest_date": news.get("latest_date"),
            "age_days": age_days,
            "status": "fresh" if (age_days is not None and age_days <= 7) else "stale_or_missing",
            "hint": "新闻来自本地缓存（东方财富个股新闻），仅用于学习观察，不构成投资建议。",
        }
    except Exception as exc:  # noqa: BLE001 - news background must never break the scorecard
        LOG.warning("scorecard_news_background_failed: %s", exc)
        background["news"] = {"status": "unavailable", "hint": "新闻缓存暂不可用；缺失时降级显示，不伪造。"}
    return background


def _strategy_verdict_text(bucket: dict[str, Any]) -> str:
    """Readable per-strategy review conclusion for the scorecard.

    Turns the raw counts into a plain-language verdict (effective / needs review
    / keep observing) with the reason and concrete evidence samples, so the
    scorecard reads like a research review instead of a stats table. Research
    only — no advice, no targets, no position sizing.
    """
    name = bucket.get("strategy") or "未分类"
    n = int(bucket.get("sample_count") or 0)
    fit = int(bucket.get("fit_count") or 0)
    dev = int(bucket.get("deviated_count") or 0)
    review = int(bucket.get("needs_review_count") or 0)
    avg_ret = bucket.get("avg_return_since_added_pct")
    label = bucket.get("scorecard_label")
    ret_text = f"平均纸面 {avg_ret:+.2f}%" if isinstance(avg_ret, (int, float)) else "暂无足够纸面收益样本"
    examples = bucket.get("examples") or []

    def _ex_text(items: list[dict[str, Any]]) -> str:
        parts = []
        for e in items[:3]:
            sym = e.get("symbol")
            if not sym:
                continue
            ret = e.get("return_since_added_pct")
            ret_str = f"{ret:+.1f}%" if isinstance(ret, (int, float)) else "—"
            parts.append(f"{sym} {ret_str}")
        return "、".join(parts) or "—"

    if label == "需要降权/复盘":
        worst = sorted(examples, key=lambda e: (e.get("return_since_added_pct") if isinstance(e.get("return_since_added_pct"), (int, float)) else 0))
        return (
            f"「{name}」建议降权 / 复盘：{n} 只样本中 {dev} 只偏离研究判断、{review} 只触发复盘，{ret_text}。"
            f"原因：纸面走势与入选假设背离，需核对失败条件是否触发。证据样本：{_ex_text(worst)}。"
        )
    if label == "近期相对有效":
        best = sorted(examples, key=lambda e: -(e.get("return_since_added_pct") if isinstance(e.get("return_since_added_pct"), (int, float)) else 0))
        return (
            f"「{name}」近期相对有效：{n} 只样本中 {fit} 只符合预期，{ret_text}。"
            f"原因：纸面走势与入选假设一致，可按原验证周期继续跟踪。证据样本：{_ex_text(best)}。"
        )
    return (
        f"「{name}」继续观察：{n} 只样本（符合 {fit} / 偏离 {dev}），{ret_text}。"
        f"样本量或验证周期不足，暂不下有效性结论。证据样本：{_ex_text(examples)}。"
    )


def build_strategy_scorecard() -> dict[str, Any]:
    """Daily scorecard for the active research loop.

    It only summarizes local evidence: daily focus, paper simulation tracking,
    strategy buckets, data gaps, and next review list. No broker route, orders,
    or position advice are produced.
    """
    rows = annotate_recommendation_priorities(candidate_rows(limit=18))
    today_rows = sorted(
        [row for row in rows if row.get("is_today_recommendation")],
        key=focus_score,
        reverse=True,
    )
    watchlist = load_paper_simulation_watchlist()
    items = [item for item in watchlist.get("items") or [] if isinstance(item, dict)]
    active_items = [item for item in items if str(item.get("paper_lifecycle_state") or "") != "archived"]
    archived_count = len(items) - len(active_items)
    summary = watchlist.get("paper_tracking_summary") if isinstance(watchlist.get("paper_tracking_summary"), dict) else {}
    data_gaps = data_gap_summary(rows)
    quant_summary = quant_daily_summary(rows)
    generation = generation_status_for_ui()
    previous_batch = previous_recommendation_performance(str(generation.get("signal_date") or ""))

    strategy_stats: dict[str, dict[str, Any]] = {}
    paper_fit_counts: dict[str, int] = {}
    decision_snapshots: list[dict[str, Any]] = []
    for item in active_items:
        decision_snapshot = paper_research_decision_snapshot(item)
        decision_snapshots.append(decision_snapshot)
        strategy = str(item.get("primary_action") or item.get("strategy") or "观察讨论")
        bucket = strategy_stats.setdefault(
            strategy,
            {
                "strategy": strategy,
                "sample_count": 0,
                "fit_count": 0,
                "pending_count": 0,
                "deviated_count": 0,
                "data_gap_count": 0,
                "needs_review_count": 0,
                "return_values": [],
                "drawdown_values": [],
                "decision_status_counts": {},
                "examples": [],
            },
        )
        bucket["sample_count"] += 1
        status_key = str(decision_snapshot.get("decision_status") or "pending_discussion")
        bucket["decision_status_counts"][status_key] = bucket["decision_status_counts"].get(status_key, 0) + 1
        status = str(item.get("expectation_fit_status") or "")
        fit_label = str(item.get("expectation_fit_label") or "等待更多样本")
        paper_fit_counts[fit_label] = paper_fit_counts.get(fit_label, 0) + 1
        if status == "fit":
            bucket["fit_count"] += 1
        elif status == "deviated":
            bucket["deviated_count"] += 1
        elif status == "data_gap":
            bucket["data_gap_count"] += 1
        else:
            bucket["pending_count"] += 1
        if str(item.get("review_tone") or "") == "warn":
            bucket["needs_review_count"] += 1
        ret = finite_float(item.get("return_since_added_pct"))
        dd = finite_float(item.get("max_drawdown_since_added"))
        if ret is not None:
            bucket["return_values"].append(ret)
        if dd is not None:
            bucket["drawdown_values"].append(dd)
        if len(bucket["examples"]) < 5:
            bucket["examples"].append(
                {
                    "symbol": normalize_symbol(item.get("symbol")),
                    "name": item.get("name"),
                    "expectation_fit_label": item.get("expectation_fit_label"),
                    "return_since_added_pct": item.get("return_since_added_pct"),
                    "review_label": item.get("review_label"),
                    "decision_status": decision_snapshot.get("decision_status"),
                    "validation_days": decision_snapshot.get("validation_days"),
                    "alignment_label": decision_snapshot.get("alignment_label"),
                }
            )

    by_strategy: list[dict[str, Any]] = []
    for bucket in strategy_stats.values():
        returns = bucket.pop("return_values")
        drawdowns = bucket.pop("drawdown_values")
        sample_count = int(bucket.get("sample_count") or 0)
        bucket["fit_rate"] = round(bucket["fit_count"] / sample_count * 100, 2) if sample_count else None
        bucket["avg_return_since_added_pct"] = round(sum(returns) / len(returns), 2) if returns else None
        bucket["avg_max_drawdown_since_added_pct"] = round(sum(drawdowns) / len(drawdowns), 2) if drawdowns else None
        if bucket["deviated_count"] or bucket["needs_review_count"]:
            bucket["scorecard_label"] = "需要降权/复盘"
        elif bucket["fit_count"] and bucket["fit_count"] >= bucket["pending_count"]:
            bucket["scorecard_label"] = "近期相对有效"
        else:
            bucket["scorecard_label"] = "继续观察"
        bucket["verdict_text"] = _strategy_verdict_text(bucket)
        by_strategy.append(bucket)
    by_strategy.sort(
        key=lambda item: (
            -int(item.get("needs_review_count") or 0),
            -int(item.get("sample_count") or 0),
            str(item.get("strategy") or ""),
        )
    )

    review_needed = [
        {
            "symbol": normalize_symbol(item.get("symbol")),
            "name": item.get("name"),
            "primary_action": item.get("primary_action") or item.get("strategy"),
            "expectation_fit_label": item.get("expectation_fit_label"),
            "expectation_fit_reason": item.get("expectation_fit_reason"),
            "review_label": item.get("review_label"),
            "return_since_added_pct": item.get("return_since_added_pct"),
            "max_drawdown_since_added": item.get("max_drawdown_since_added"),
            "research_decision": paper_research_decision_snapshot(item),
        }
        for item in active_items
        if str(item.get("expectation_fit_status") or "") in {"deviated", "data_gap"}
        or str(item.get("review_tone") or "") == "warn"
    ]
    decision_review = sorted(
        decision_snapshots,
        key=lambda item: (
            {"warn": 0, "missing": 1, "watch": 2, "ok": 3}.get(str(item.get("alignment_tone") or "watch"), 4),
            str(item.get("symbol") or ""),
        ),
    )
    tomorrow_watch = [
        {
            "symbol": row.get("symbol"),
            "name": row.get("name"),
            "priority_rank": row.get("priority_rank"),
            "strategy_lane": row.get("strategy_lane"),
            "reason": row.get("priority_reason") or row.get("reason_cn") or row.get("focus_reason"),
            "data_status": row.get("data_status"),
        }
        for row in today_rows[:10]
    ]
    effective_strategies = [b for b in by_strategy if b.get("scorecard_label") == "近期相对有效"]
    review_strategies = [b for b in by_strategy if b.get("scorecard_label") == "需要降权/复盘"]
    watching_strategies = [b for b in by_strategy if b.get("scorecard_label") == "继续观察"]
    # How well paper outcomes have matched the original LLM / local research call.
    alignment_counts: dict[str, int] = {}
    for snap in decision_snapshots:
        lbl = str(snap.get("alignment_label") or "等待验证")
        alignment_counts[lbl] = alignment_counts.get(lbl, 0) + 1
    aligned = alignment_counts.get("已兑现研究判断", 0)
    deviated_align = alignment_counts.get("偏离研究判断", 0)
    conclusion_lines: list[str] = []
    if effective_strategies:
        conclusion_lines.append("近期相对有效：" + "、".join(str(b.get("strategy")) for b in effective_strategies))
    if review_strategies:
        conclusion_lines.append("需降权/复盘：" + "、".join(str(b.get("strategy")) for b in review_strategies))
    if watching_strategies:
        conclusion_lines.append("继续观察：" + "、".join(str(b.get("strategy")) for b in watching_strategies))
    if not by_strategy:
        conclusion_lines.append("纸面模拟池暂无活跃样本，尚不能给出策略有效性结论")
    conclusion_lines.append(
        f"研究判断对齐：{aligned} 只已兑现 / {deviated_align} 只偏离 / 共 {len(decision_snapshots)} 只在跟踪"
    )

    def _verdict_brief(buckets: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [
            {
                "strategy": b.get("strategy"),
                "sample_count": b.get("sample_count"),
                "avg_return_since_added_pct": b.get("avg_return_since_added_pct"),
                "verdict_text": b.get("verdict_text"),
            }
            for b in buckets
        ]

    strategy_conclusions = {
        "effective": _verdict_brief(effective_strategies),
        "needs_review": _verdict_brief(review_strategies),
        "watching": _verdict_brief(watching_strategies),
        "alignment_counts": alignment_counts,
        "conclusion_text": "；".join(conclusion_lines) + "。",
        "verdict_lines": [b.get("verdict_text") for b in by_strategy],
        "research_only": True,
        "paper_only": True,
    }
    # A+B+C: combine paper effectiveness, strategy-lab backtest stats, and
    # sample-size confidence into a soft per-strategy weight for display.
    # Computed pure (persist=False): the scorecard is served by a read-only GET,
    # so viewing it must not rewrite the weights file that feeds candidate
    # ordering. Never raises; soft tilt only.
    try:
        strategy_effectiveness = compute_strategy_effectiveness({"by_strategy": by_strategy}, persist=False)
    except Exception as exc:  # noqa: BLE001 - effectiveness is enrichment
        LOG.warning("strategy_effectiveness_failed: %s", exc)
        strategy_effectiveness = {"ok": False, "strategies": [], "weight_by_bucket": {}}
    market_context = {
        "generation": generation,
        "data_gap_summary": data_gaps,
        "quant_fit_counts": quant_summary.get("fit_counts") if isinstance(quant_summary, dict) else {},
        "background": build_market_background_summary(),
        "news_and_sector_note": "新闻/板块/题材线索来自本地缓存或显式联网刷新；缺失时必须降级显示，不伪造。",
    }
    payload = {
        "ok": True,
        "status": "passed",
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "today_focus": tomorrow_watch[:10],
        "today_focus_count": len(today_rows),
        "paper_pool_count": len(active_items),
        "paper_pool_total_count": len(items),
        "paper_archived_count": archived_count,
        "paper_expectation_fit_counts": paper_fit_counts or summary.get("expectation_fit_counts") or {},
        "paper_tracking_summary": summary,
        "previous_recommendation_performance": previous_batch,
        "by_strategy": by_strategy,
        "strategy_conclusions": strategy_conclusions,
        "strategy_effectiveness": strategy_effectiveness,
        "paper_research_decisions": decision_review[:12],
        "paper_research_decision_count": len(decision_review),
        "review_needed": review_needed[:12],
        "review_needed_count": len(review_needed),
        "data_gap_summary": data_gaps,
        "market_context": market_context,
        "tomorrow_watchlist": tomorrow_watch[:8],
        "scorecard_message": (
            f"今日重点 {len(today_rows)} 只；纸面模拟池活跃 {len(active_items)} 只；"
            f"需降级/复盘 {len(review_needed)} 只；已归档 {archived_count} 只。"
        ),
        "research_only": True,
        "paper_only": True,
        "no_broker": True,
        "no_real_orders": True,
        "not_investment_advice": True,
        "not_position_advice": True,
    }
    return payload


STRATEGY_EFFECTIVENESS_JSON = project_root / "results" / "strategy_effectiveness_weights.json"

# Canonical strategy buckets + the aliases each carries across the three layers
# (generator strategy_type/family, paper primary_action, scorecard bucket). The
# feedback loop joins on these buckets.
_STRATEGY_BUCKET_ALIASES: dict[str, tuple[str, ...]] = {
    "打板复核": ("打板", "da_ban", "limit_up", "volume_breakout", "量价突破", "board"),
    "长期/波段复核": ("长期", "波段", "long_term", "low_price", "趋势", "low_vol", "swing", "long_swing"),
}


def canonical_strategy_bucket(text: Any) -> str:
    t = str(text or "").lower()
    for key, aliases in _STRATEGY_BUCKET_ALIASES.items():
        if any(alias.lower() in t for alias in aliases):
            return key
    return "其他观察"


def _effectiveness_explanation(
    bucket: str, weight: float, fit_rate: float | None, avg_ret: float | None, n: int, n_full: int, bt_win: float | None
) -> str:
    if n < 3:
        return f"「{bucket}」样本仅 {n} 只，证据不足，权重保持中性（≈1.0）；继续积累纸面样本再判定。"
    direction = "上调" if weight > 1.02 else ("下调" if weight < 0.98 else "保持中性")
    bits: list[str] = []
    if fit_rate is not None:
        bits.append(f"纸面符合率 {fit_rate:.0f}%")
    if avg_ret is not None:
        bits.append(f"平均纸面 {avg_ret:+.1f}%")
    if bt_win is not None:
        bits.append(f"回测盈利占比 {bt_win*100:.0f}%")
    basis = "、".join(bits) or "现有证据"
    conf = "（样本充足）" if n >= n_full else f"（样本 {n}/{n_full}，已按置信度缩放）"
    return f"「{bucket}」据 {basis} {direction}今日排序权重至 {weight:.2f}{conf}。软性微调，不删策略、不构成建议。"


def compute_strategy_effectiveness(
    scorecard: dict[str, Any] | None = None, *, persist: bool = False
) -> dict[str, Any]:
    """Combine paper-tracking effectiveness (A), strategy-lab backtest stats (B),
    and sample-size confidence (C) into a soft, bounded per-strategy weight used
    to re-rank today's focus.

    Research only: the weight is a tilt in [0.6, 1.4], never a hard filter, never
    position advice.

    Pure by default: this is a read-only computation and must stay side-effect
    free so that read-only consumers (notably `GET /api/strategy-scorecard`)
    cannot change later candidate ordering just by being viewed. Persisting the
    weights is an explicit, opt-in action -- pass `persist=True`, or call
    `persist_strategy_effectiveness()` directly.
    """
    scorecard = scorecard if isinstance(scorecard, dict) else build_strategy_scorecard()
    by_strategy = scorecard.get("by_strategy") if isinstance(scorecard.get("by_strategy"), list) else []
    try:
        lab = latest_research_evidence_context().get("strategy_lab") or {}
    except Exception:  # noqa: BLE001 - backtest stats are optional enrichment
        lab = {}
    # Aggregate backtest stats per canonical bucket.
    bt: dict[str, dict[str, list[float]]] = {}
    for item in (lab.get("top_strategies") or []):
        key = canonical_strategy_bucket(item.get("strategy_id") or item.get("strategy"))
        agg = bt.setdefault(key, {"win_rate": [], "sharpe": [], "annualized_return": [], "max_drawdown": []})
        for field in ("win_rate", "sharpe", "annualized_return", "max_drawdown"):
            value = finite_float(item.get(field))
            if value is not None:
                agg[field].append(value)

    def _avg(values: list[float]) -> float | None:
        return round(sum(values) / len(values), 4) if values else None

    n_full = 15  # paper samples needed for full confidence
    strategies: list[dict[str, Any]] = []
    weight_by_bucket: dict[str, float] = {}
    for b in by_strategy:
        key = canonical_strategy_bucket(b.get("strategy"))
        n = safe_int(b.get("sample_count"), 0)
        fit_rate = finite_float(b.get("fit_rate"))
        avg_ret = finite_float(b.get("avg_return_since_added_pct"))
        needs_review = safe_int(b.get("needs_review_count"), 0)
        # Paper effectiveness in [-1, 1]: half from fit-rate, half from avg return.
        paper_score = 0.0
        if fit_rate is not None:
            paper_score += (fit_rate / 100.0 - 0.5) * 2 * 0.5
        if avg_ret is not None:
            paper_score += max(-1.0, min(1.0, avg_ret / 10.0)) * 0.5
        confidence = max(0.0, min(1.0, n / n_full))  # (C) sample-size confidence
        # Backtest prior in [-1, 1] from win-rate + sharpe.
        bt_bucket = bt.get(key) or {}
        bt_win = _avg(bt_bucket.get("win_rate") or [])
        bt_sharpe = _avg(bt_bucket.get("sharpe") or [])
        bt_terms = 0
        bt_score = 0.0
        if bt_win is not None:
            bt_score += max(-1.0, min(1.0, (bt_win - 0.5) * 2))
            bt_terms += 1
        if bt_sharpe is not None:
            bt_score += max(-1.0, min(1.0, bt_sharpe / 2.0))
            bt_terms += 1
        bt_prior = (bt_score / bt_terms) if bt_terms else 0.0
        # Soft tilt: confidence-scaled paper signal blended with backtest prior.
        tilt = paper_score * confidence * 0.6 + bt_prior * 0.4
        weight = round(max(0.6, min(1.4, 1.0 + tilt * 0.4)), 3)
        weight_by_bucket[key] = weight
        if n < 3:
            confidence_label = f"样本不足（{n}），暂按中性，还需≥{max(0, 3 - n)}个样本起判"
        elif n < n_full:
            confidence_label = f"初步（{n}/{n_full} 样本），权重已按置信度缩放"
        else:
            confidence_label = f"较可信（{n} 样本）"
        strategies.append(
            {
                "strategy": b.get("strategy"),
                "bucket": key,
                "sample_count": n,
                "paper_fit_rate": fit_rate,
                "paper_avg_return_pct": avg_ret,
                "paper_needs_review": needs_review,
                "backtest_win_rate": bt_win,
                "backtest_sharpe": bt_sharpe,
                "backtest_annualized_return": _avg(bt_bucket.get("annualized_return") or []),
                "backtest_max_drawdown": _avg(bt_bucket.get("max_drawdown") or []),
                "confidence": round(confidence, 2),
                "confidence_label": confidence_label,
                "weight": weight,
                "weight_direction": "↑ 升权" if weight > 1.02 else ("↓ 降权" if weight < 0.98 else "→ 中性"),
                "explanation": _effectiveness_explanation(key, weight, fit_rate, avg_ret, n, n_full, bt_win),
            }
        )
    payload = {
        "ok": True,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "method": "soft_rerank",
        "neutral_weight": 1.0,
        "weight_range": [0.6, 1.4],
        "full_confidence_samples": n_full,
        "weight_by_bucket": weight_by_bucket,
        "strategies": strategies,
        "note": "权重为有界软性微调，仅用于研究排序；不删策略、不下单、不构成投资建议。",
        "research_only": True,
        "paper_only": True,
        "no_broker": True,
    }
    if persist:
        persist_strategy_effectiveness(payload)
    return payload


def persist_strategy_effectiveness(payload: dict[str, Any]) -> bool:
    """Explicit writer for the soft strategy-effectiveness weights.

    Kept separate from `compute_strategy_effectiveness()` so read-only callers
    stay read-only. Returns True when the file was written. Never raises:
    persistence is best-effort enrichment, not a correctness gate.
    """

    try:
        STRATEGY_EFFECTIVENESS_JSON.parent.mkdir(parents=True, exist_ok=True)
        STRATEGY_EFFECTIVENESS_JSON.write_text(
            json.dumps(clean_payload(payload), ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return True
    except Exception as exc:  # noqa: BLE001 - persistence is best-effort
        LOG.warning("strategy_effectiveness_persist_failed: %s", exc)
        return False


def latest_strategy_effectiveness() -> dict[str, Any]:
    """Read the last persisted effectiveness weights (read-only, never raises)."""
    payload = read_json_file(STRATEGY_EFFECTIVENESS_JSON)
    return payload if isinstance(payload, dict) and payload else {"weight_by_bucket": {}, "strategies": []}


def strategy_weight_for(text: Any, weights: dict[str, float] | None = None) -> float:
    """Soft re-rank multiplier for a strategy label, defaulting to neutral 1.0."""
    if weights is None:
        weights = (latest_strategy_effectiveness() or {}).get("weight_by_bucket") or {}
    value = finite_float(weights.get(canonical_strategy_bucket(text)))
    return value if value and 0.5 <= value <= 1.6 else 1.0


def candidate_is_eligible_for_paper_sync(candidate: dict[str, Any], *, include_observation: bool = False) -> bool:
    pool_action = candidate.get("pool_action") if isinstance(candidate.get("pool_action"), dict) else {}
    pool_status = str(pool_action.get("status") or "")
    group_key = str(candidate.get("group_key") or "")
    if pool_status in {"manual_archived"} or group_key in {"exit_review"}:
        return False
    if candidate.get("is_today_recommendation"):
        return True
    if not include_observation:
        return False
    lane = str(candidate.get("strategy_lane") or candidate.get("strategy_bucket") or "")
    return any(marker in lane for marker in ("打板", "长期", "波段", "观察"))


def sync_candidates_to_paper_simulation_pool(
    *,
    include_observation: bool = False,
    max_candidates: int = 10,
) -> dict[str, Any]:
    max_candidates = max(1, min(int(max_candidates or 10), 30))
    rows = annotate_recommendation_priorities(candidate_rows(limit=18))
    selected: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    seen: set[str] = set()
    ordered = sorted(
        rows,
        key=lambda item: (
            0 if item.get("is_today_recommendation") else 1,
            int(item.get("priority_rank") or 999),
            -float(item.get("priority_score") or 0),
            str(item.get("symbol") or ""),
        ),
    )
    for row in ordered:
        symbol = normalize_symbol(row.get("symbol"))
        if not symbol or symbol in seen:
            continue
        seen.add(symbol)
        if candidate_is_eligible_for_paper_sync(row, include_observation=include_observation) and len(selected) < max_candidates:
            selected.append(row)
        else:
            skipped.append(row)

    results: list[dict[str, Any]] = []
    strategy_counts: dict[str, int] = {}
    for row in selected:
        lane = str(row.get("strategy_lane") or row.get("strategy_bucket") or "未分层")
        strategy_counts[lane] = strategy_counts.get(lane, 0) + 1
        result = add_candidate_to_paper_simulation_pool(
            str(row.get("symbol") or ""),
            entry_date=str(row.get("select_date") or row.get("signal_date") or ""),
            lane=str(row.get("detail_lane") or ""),
            note="按候选池与策略分层同步到纸面模拟池，用于后续本地跟踪复盘。",
        )
        results.append(result)

    refresh = refresh_paper_simulation_watchlist_tracking() if selected else load_paper_simulation_watchlist()
    added_count = sum(1 for item in results if item.get("status") == "added")
    updated_count = sum(1 for item in results if item.get("status") == "updated")
    failed = [item for item in results if not item.get("ok")]
    message = (
        f"已按候选池/策略同步 {len(selected)} 只到纸面模拟池：新增 {added_count}，更新 {updated_count}；"
        f"本轮跳过 {len(skipped)} 只历史或待复盘样本。"
    )
    return {
        "ok": not failed,
        "status": "synced" if not failed else "partial",
        "selected_count": len(selected),
        "added_count": added_count,
        "updated_count": updated_count,
        "failed_count": len(failed),
        "skipped_count": len(skipped),
        "watchlist_count": int(refresh.get("watchlist_count") or len(refresh.get("items") or [])),
        "selected_symbols": [normalize_symbol(row.get("symbol")) for row in selected],
        "strategy_counts": strategy_counts,
        "paper_tracking_summary": refresh.get("paper_tracking_summary") or {},
        "message": message,
        "message_for_ui": message,
        "sync_policy": (
            "默认同步今日新候选和当前策略候选；历史观察样本保留在候选池，除非显式 include_observation=true。"
        ),
        "research_only": True,
        "paper_only": True,
        "no_broker": True,
        "no_real_orders": True,
    }


PAPER_SIMULATION_SYNC_POLICY = (
    "After daily candidates are generated, today's recommendations are copied into the "
    "paper simulation watchlist for local review only. No paper portfolio trade, broker "
    "route, real order, position instruction, or auto execution is created."
)


def append_paper_simulation_sync_to_generation_payload(
    payload: dict[str, Any],
    *,
    max_candidates: int = 10,
    sync_paper: bool = True,
) -> dict[str, Any]:
    """Attach paper-watchlist sync status to a candidate-generation payload.

    This writes only the paper simulation watchlist used for review. It does not
    create paper portfolio trades, broker routes, orders, or position advice.
    """
    payload = payload if isinstance(payload, dict) else {}
    if not sync_paper:
        payload["paper_simulation_sync"] = {
            "ok": True,
            "status": "skipped",
            "message_for_ui": "本次按请求跳过纸面模拟池同步。",
            "research_only": True,
            "paper_only": True,
            "no_broker": True,
            "no_real_orders": True,
        }
        return payload

    generation = payload.get("candidate_generation") if isinstance(payload.get("candidate_generation"), dict) else {}
    candidate_count = safe_int(generation.get("candidate_count") or payload.get("candidate_count"), 0)
    if candidate_count <= 0:
        payload["paper_simulation_sync"] = {
            "ok": True,
            "status": "skipped_no_candidates",
            "message_for_ui": "本次没有新的今日候选，纸面模拟池未同步。",
            "research_only": True,
            "paper_only": True,
            "no_broker": True,
            "no_real_orders": True,
        }
        return payload

    try:
        paper_sync = sync_candidates_to_paper_simulation_pool(
            include_observation=False,
            max_candidates=max_candidates,
        )
        payload["paper_simulation_sync"] = paper_sync
        payload["paper_simulation_sync_policy"] = PAPER_SIMULATION_SYNC_POLICY
        base_message = str(payload.get("message_for_ui") or payload.get("message") or "")
        payload["message_for_ui"] = (
            base_message + f" 纸面模拟池同步 {paper_sync.get('selected_count', 0)} 只。"
        ).strip()
    except Exception as sync_exc:  # noqa: BLE001 - paper sync must not hide candidate generation
        LOG.warning("daily_candidate_paper_sync_failed: error=%s", sync_exc)
        payload["paper_simulation_sync"] = {
            "ok": False,
            "status": "failed",
            "error": f"{type(sync_exc).__name__}: {sync_exc}",
            "message_for_ui": "今日候选已生成，但同步纸面模拟池失败；可稍后手动点击同步。",
            "research_only": True,
            "paper_only": True,
            "no_broker": True,
            "no_real_orders": True,
        }
        if payload.get("status") == "completed":
            payload["status"] = "completed_with_warnings"
    return payload


def focus_score(candidate: dict[str, Any]) -> float:
    score = 0.0
    if candidate.get("is_today_recommendation"):
        score += 8
    if candidate.get("data_quality", {}).get("tone") == "ok":
        score += 3
    elif candidate.get("data_quality", {}).get("tone") == "warn":
        score += 1
    status_tone = str(candidate.get("tracking", {}).get("status_tone") or "")
    score += {"ok": 4, "watch": 2, "warn": -3, "missing": -4}.get(status_tone, 0)
    risk = str(candidate.get("risk_level") or "")
    score += {"低": 2, "中": 1, "高": -2}.get(risk, 0)
    if candidate.get("group_key") == "long_swing":
        score += 1.5
    if candidate.get("group_key") == "board":
        score += 1
    pct = finite_float(candidate.get("pct_chg"))
    if pct is not None:
        score += max(-2.0, min(2.0, pct / 3))
    return score


def priority_reason(candidate: dict[str, Any]) -> str:
    reasons: list[str] = []
    if candidate.get("is_today_recommendation"):
        reasons.append("今日新候选")
    quality_tone = str(candidate.get("data_quality", {}).get("tone") or "")
    if quality_tone == "ok":
        reasons.append("价格/K线与关键字段完整")
    elif quality_tone == "field_gap":
        reasons.append("价格/K线已更新，量能字段降级")
    tracking_tone = str(candidate.get("tracking", {}).get("status_tone") or "")
    if tracking_tone == "ok":
        reasons.append("入选后仍贴近观察假设")
    elif tracking_tone == "watch":
        reasons.append("仍需跟踪确认")
    elif tracking_tone == "warn":
        reasons.append("偏离假设，优先复盘原因")
    lane = str(candidate.get("strategy_lane") or "")
    if lane and lane not in reasons:
        reasons.append(lane)
    return "；".join(reasons[:4]) or "本地研究排序样本"


def focus_score_explanation(candidate: dict[str, Any], weight: float) -> dict[str, Any]:
    """Explain the unchanged UI ranking; these points are not probabilities."""
    quality = candidate.get("data_quality", {}).get("tone")
    tone = candidate.get("tracking", {}).get("status_tone")
    pct = finite_float(candidate.get("pct_chg"))
    components = [
        {"name": "今日候选", "points": 8 if candidate.get("is_today_recommendation") else 0},
        {"name": "字段完整性", "points": {"ok": 3, "warn": 1}.get(quality, 0)},
        {"name": "观察假设状态", "points": {"ok": 4, "watch": 2, "warn": -3, "missing": -4}.get(tone, 0)},
        {"name": "风险标签", "points": {"低": 2, "中": 1, "高": -2}.get(str(candidate.get("risk_level") or ""), 0)},
        {"name": "观察分组", "points": {"long_swing": 1.5, "board": 1}.get(candidate.get("group_key"), 0)},
        {"name": "当日涨跌", "points": max(-2.0, min(2.0, pct / 3)) if pct is not None else 0},
    ]
    return {"score_id": "ui-focus-score-v1", "components": components,
        "base_total": focus_score(candidate), "strategy_tilt": (weight - 1) * 10,
        "total": round(focus_score(candidate) + (weight - 1) * 10, 2), "ranking_only": True,
        "tie_breaker": "API同分保留输入次序；前端依优先名次、基础分及代码排序。同分或微小分差不证明优劣。",
        "not_comparable_to": "纸面研究·未验证；选股器原始分、其他策略分和历史统计使用不同口径，不能直接比较。"}


def candidate_pool_context(rows: list[dict[str, Any]], focus: list[dict[str, Any]]) -> dict[str, Any]:
    """Relate independent pools with their recorded dates, without syncing."""
    watch = read_json_file(PAPER_WATCHLIST_JSON)
    state = read_json_file(project_root / "state/auto_paper_portfolio.json")
    watch_rows = watch.get("items") if isinstance(watch, dict) else None
    positions = state.get("positions") if isinstance(state, dict) else None
    by_symbol: dict[str, list[dict[str, Any]]] = {}
    def add(symbol: Any, pool: str, label: str, as_of: Any) -> None:
        symbol = normalize_symbol(str(symbol or ""))
        if symbol:
            by_symbol.setdefault(symbol, []).append({"pool": pool, "label": label, "as_of": str(as_of or "")[:10]})
    for row in rows:
        today = row.get("is_today_recommendation") is True
        add(row.get("symbol"), "today" if today else "historical", "今日候选" if today else "历史观察", row.get("signal_date"))
    for row in focus:
        add(row.get("symbol"), "focus", "本次重点观察", row.get("signal_date"))
    for row in watch_rows if isinstance(watch_rows, list) else []:
        if isinstance(row, dict):
            add(row.get("symbol"), "paper_watchlist", "纸面观察池", row.get("latest_reference_date"))
    for row in positions if isinstance(positions, list) else []:
        if isinstance(row, dict):
            add(row.get("symbol"), "paper_position", "策略纸面持仓", state.get("valuation_date"))
    return {"by_symbol": by_symbol, "visible_candidate_count": len(rows), "focus_count": len(focus),
        "paper_watchlist_count": len(watch_rows) if isinstance(watch_rows, list) else None,
        "paper_position_count": len(positions) if isinstance(positions, list) else None,
        "read_only": True, "synchronization_executed": False,
        "note": "各池日期独立；重点观察是候选子集，观察池不是持仓。本次只读展示，没有同步或交易。"}


def annotate_recommendation_priorities(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    # Soft strategy-effectiveness re-rank: a bounded tilt on the focus order so
    # strategies that keep failing on paper (or fail the backtest) drift down,
    # validating ones drift up — scaled by sample-size confidence so small
    # samples barely move anything. Transparent: each row keeps its weight + note.
    effectiveness = latest_strategy_effectiveness() or {}
    weights = effectiveness.get("weight_by_bucket") or {}
    info_by_bucket: dict[str, Any] = {
        str(s.get("bucket")): s for s in (effectiveness.get("strategies") or []) if isinstance(s, dict)
    }
    tilt_scale = 10.0  # how far a full [0.6,1.4] weight can move a candidate

    def _strategy_text(row: dict[str, Any]) -> str:
        return " ".join(
            str(row.get(key) or "")
            for key in ("strategy_lane", "group_key", "strategy_bucket", "primary_action", "strategy")
        )

    def _weighted_score(row: dict[str, Any]) -> float:
        weight = strategy_weight_for(_strategy_text(row), weights)
        return focus_score(row) + (weight - 1.0) * tilt_scale

    ranked = sorted(
        [
            row
            for row in rows
            if row.get("is_today_recommendation")
            and str(row.get("pool_action", {}).get("status") or "") not in {"manual_archived"}
        ],
        key=_weighted_score,
        reverse=True,
    )
    ranks: dict[str, int] = {}
    for index, row in enumerate(ranked, start=1):
        symbol = str(row.get("symbol") or "")
        if symbol:
            ranks[symbol] = index

    annotated: list[dict[str, Any]] = []
    for row in rows:
        copy = {**row}
        copy["priority_score"] = round(focus_score(copy), 2)
        bucket = canonical_strategy_bucket(_strategy_text(copy))
        weight = strategy_weight_for(_strategy_text(copy), weights)
        info = info_by_bucket.get(bucket) or {}
        copy["strategy_bucket_canonical"] = bucket
        copy["strategy_weight"] = round(weight, 3)
        copy["weighted_priority_score"] = round(_weighted_score(copy), 2)
        copy["score_explanation"] = focus_score_explanation(copy, weight)
        if weight < 0.98:
            copy["strategy_weight_note"] = f"策略近期降权（{bucket}：{info.get('confidence_label') or '近期纸面偏离'}）"
        elif weight > 1.02:
            copy["strategy_weight_note"] = f"策略近期升权（{bucket}：{info.get('confidence_label') or '近期纸面兑现'}）"
        else:
            copy["strategy_weight_note"] = f"策略权重中性（{bucket}：{info.get('confidence_label') or '证据不足/均衡'}）"
        rank = ranks.get(str(copy.get("symbol") or ""))
        if rank is not None:
            copy["priority_rank"] = rank
            copy["priority_label"] = f"优先关注 #{rank}"
            copy["priority_reason"] = priority_reason(copy)
        else:
            copy["priority_rank"] = ""
            copy["priority_label"] = ""
            copy["priority_reason"] = priority_reason(copy)
        annotated.append(copy)
    return annotated


def daily_focus_rows(rows: list[dict[str, Any]], limit: int = 4) -> list[dict[str, Any]]:
    ordered = sorted(rows, key=focus_score, reverse=True)
    selected: list[dict[str, Any]] = []
    seen_symbols: set[str] = set()
    for row in ordered:
        pool_status = str(row.get("pool_action", {}).get("status") or "")
        if pool_status in {"auto_exit_review", "manual_observe", "manual_archived"}:
            continue
        symbol = str(row.get("symbol") or "")
        if not symbol or symbol in seen_symbols:
            continue
        selected.append({**row, "focus_label": "每日重点复核"})
        seen_symbols.add(symbol)
        if len(selected) >= limit:
            break
    if selected:
        return selected
    for row in ordered:
        pool_status = str(row.get("pool_action", {}).get("status") or "")
        if pool_status in {"manual_archived"}:
            continue
        symbol = str(row.get("symbol") or "")
        if not symbol or symbol in seen_symbols:
            continue
        selected.append(
            {
                **row,
                "focus_label": "降级复盘重点",
                "focus_degraded": True,
                "focus_reason": "当前没有活跃今日候选，页面仅展示可复核的历史/退出样本，避免空面板被误读为系统失效。",
            }
        )
        seen_symbols.add(symbol)
        if len(selected) >= limit:
            break
    return selected


def candidate_groups(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    definitions = [
        ("today_recommendation", "今日新候选", "基于最新完整日线和本地策略筛出的今日研究候选；用于下一交易日学习观察。"),
        ("long_swing", "长期 - 波段复核", "长期候选中，数据较完整且趋势修复证据相对清楚的观察样本。"),
        ("board", "打板观察", "量价突破类候选，重点看入选后1到5个交易日的延续性。"),
        ("long_observe", "长期 - 只观察", "长期候选中，数据缺口、趋势未确认或入选后验证不足的样本。"),
        ("exit_review", "待移出 / 降级复盘", "已偏离原打板或波段假设的样本，不再放在原活跃层；保留原因用于学习复盘。"),
        ("manual_observe", "手动只观察", "你手动转入只观察的样本，不参与每日重点排序。"),
        ("general", "普通候选", "未能归入长期或打板分层的本地候选。"),
        ("manual_archived", "手动移出活跃池", "你手动移出的样本，仅用于回看和恢复，不参与活跃候选排序。"),
    ]
    groups: list[dict[str, Any]] = []
    for key, title, note in definitions:
        if key == "today_recommendation":
            group_rows = [
                row
                for row in rows
                if row.get("is_today_recommendation")
                and str(row.get("pool_action", {}).get("status") or "") not in {"manual_archived"}
            ]
        else:
            group_rows = [
                row
                for row in rows
                if row.get("group_key") == key and not row.get("is_today_recommendation")
            ]
        groups.append(
            {
                "key": key,
                "title": title,
                "note": note,
                "count": len(group_rows),
                "rows": group_rows,
            }
        )
    return groups


def candidate_pool_review_policy(rows: list[dict[str, Any]]) -> dict[str, Any]:
    today_count = sum(1 for row in rows if row.get("is_today_recommendation"))
    history_count = len(rows) - today_count
    exit_review_count = sum(
        1
        for row in rows
        if row.get("group_key") == "exit_review" and not row.get("is_today_recommendation")
    )
    auto_exit_count = sum(
        1
        for row in rows
        if str(row.get("pool_action", {}).get("status") or "") == "auto_exit_review"
    )
    auto_downgrade_count = sum(
        1
        for row in rows
        if str(row.get("pool_action", {}).get("status") or "") == "auto_downgrade_observe"
    )
    manual_count = sum(
        1
        for row in rows
        if bool(row.get("pool_action", {}).get("manual"))
    )
    return {
        "title": "降级复盘机制",
        "summary": (
            "今日新候选只展示今天新筛出的研究候选；历史候选会进入观察池继续验证。"
            "偏离原入选假设的样本会自动调整到复盘分层，但不会自动删除，"
            "也不会替你做最终结论。"
        ),
        "today_count": today_count,
        "history_count": history_count,
        "exit_review_count": exit_review_count,
        "auto_exit_count": auto_exit_count,
        "auto_downgrade_count": auto_downgrade_count,
        "manual_count": manual_count,
        "steps": [
            "自动分层：打板样本偏离后进入待移出复盘；长期波段样本偏离后降为只观察。",
            "复盘查看：点开详情看入选理由、指标变化、入选以来表现和偏离原因。",
            "手动确认：你可以保留观察、转只观察、移出活跃池，或清除手动状态。",
            "持续跟踪：旧样本保留在历史观察池，用于对照当时策略假设是否兑现。",
        ],
        "backtest_note": "系统会显示回测可用性和复核线索；专项回测需要在 Quant 日总结或回测模块里触发。",
        "research_only": True,
        "paper_only": True,
        "no_broker": True,
        "no_auto_execution": True,
        "not_position_advice": True,
    }


def data_gap_summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    missing_rows = [row for row in rows if row.get("data_quality", {}).get("tone") == "missing"]
    stale_rows = [row for row in rows if row.get("data_quality", {}).get("status") == "日线滞后"]
    def has_field_gap(row: dict[str, Any]) -> bool:
        evidence = row.get("missing_evidence") or []
        return row.get("data_quality", {}).get("tone") == "field_gap" or any(
            isinstance(item, dict) and item.get("kind") == "missing_liquidity_evidence" for item in evidence)
    field_gap_rows = [row for row in rows if has_field_gap(row)]
    today_rows = [row for row in rows if bool(row.get("is_today_recommendation"))]
    observation_rows = [row for row in rows if not bool(row.get("is_today_recommendation"))]
    today_stale_rows = [row for row in stale_rows if bool(row.get("is_today_recommendation"))]
    observation_stale_rows = [row for row in stale_rows if not bool(row.get("is_today_recommendation"))]
    today_missing_rows = [row for row in missing_rows if bool(row.get("is_today_recommendation"))]
    observation_missing_rows = [row for row in missing_rows if not bool(row.get("is_today_recommendation"))]
    today_field_gap_rows = [row for row in field_gap_rows if bool(row.get("is_today_recommendation"))]
    observation_field_gap_rows = [row for row in field_gap_rows if not bool(row.get("is_today_recommendation"))]
    warning_rows = [
        row
        for row in rows
        if row.get("data_quality", {}).get("tone") not in {"ok", "field_gap"}
    ]
    quality_warning_rows = [
        row
        for row in warning_rows
        if row not in missing_rows and row not in stale_rows
    ]
    price_ready_rows = [
        row
        for row in rows
        if row not in missing_rows and row not in stale_rows
    ]
    reasons: list[str] = []
    for row in rows:
        for reason in row.get("data_quality", {}).get("missing_reasons", []):
            if reason not in reasons and reason != "未发现关键缺口。":
                reasons.append(str(reason))
    target_dates = [
        str(row.get("data_quality", {}).get("target_date") or "")
        for row in rows
        if str(row.get("data_quality", {}).get("target_date") or "") not in {"", "数据缺失"}
    ]
    if today_stale_rows or today_missing_rows:
        message = "今日重点里仍有股票未更新到目标完整日线，相关股票只能观察，不能做策略验证。"
    elif stale_rows or missing_rows:
        message = (
            f"今日重点价格/K线已覆盖目标日；仍有 {len(observation_stale_rows) + len(observation_missing_rows)} "
            "只历史观察/纸面样本日线未更新或缺少 CSV，只影响这些旧样本的复盘验证。"
        )
    elif field_gap_rows:
        if today_field_gap_rows and not observation_field_gap_rows:
            message = (
                f"今日重点价格/K线已更新；{len(today_field_gap_rows)} 只缺成交额/量能字段，"
                "MACD/KDJ/RSI 可读，量能和成交额解释降级。"
            )
        elif today_field_gap_rows:
            message = (
                f"今日重点价格/K线已更新；今日重点 {len(today_field_gap_rows)} 只、历史观察 "
                f"{len(observation_field_gap_rows)} 只缺成交额/量能字段，量能解释降级。"
            )
        else:
            message = (
                f"今日重点价格/K线已更新；{len(observation_field_gap_rows)} 只历史观察样本缺成交额/量能字段，"
                "不影响今日重点的价格和技术指标阅读。"
            )
    elif quality_warning_rows:
        message = (
            f"价格/K线已更新；{len(quality_warning_rows)} 只存在异常历史行过滤等质量提示，"
            "指标已基于剩余有效价格计算。"
        )
    else:
        message = "候选价格日线和关键字段可读取。"
    return {
        "candidate_count": len(rows),
        "gap_count": len(warning_rows),
        "missing_count": len(missing_rows),
        "stale_count": len(stale_rows),
        "quality_warning_count": len(quality_warning_rows),
        "field_gap_count": len(field_gap_rows),
        "field_gap_label": "成交额/量能字段缺口",
        "field_gap_explanation": "表示最新日线里缺 amount 等辅助字段，不等于价格/K线没更新。",
        "today_count": len(today_rows),
        "observation_count": len(observation_rows),
        "today_stale_count": len(today_stale_rows),
        "observation_stale_count": len(observation_stale_rows),
        "today_missing_count": len(today_missing_rows),
        "observation_missing_count": len(observation_missing_rows),
        "today_field_gap_count": len(today_field_gap_rows),
        "observation_field_gap_count": len(observation_field_gap_rows),
        "target_date": max(target_dates) if target_dates else "",
        "today_price_layer_ready": not today_missing_rows and not today_stale_rows,
        "price_layer_ready": not missing_rows and not stale_rows,
        "fresh_count": len(price_ready_rows),
        "reasons": reasons[:8] or ["候选价格指标可读取，暂无关键缺口。"],
        "message": message,
    }


def quant_fit_label(tracking: dict[str, Any]) -> tuple[str, str]:
    status = str(tracking.get("status_label") or "")
    tone = str(tracking.get("status_tone") or "watch")
    if status == "阶段符合观察假设":
        return "符合入选假设", "ok"
    if status == "偏离观察假设":
        return "偏离入选假设", "warn"
    if status == "数据不足":
        return "数据不足", "missing"
    return "等待更多交易日", tone if tone in {"ok", "watch", "warn", "missing"} else "watch"


def quant_reason_analysis(candidate: dict[str, Any]) -> str:
    tracking = candidate.get("tracking", {}) if isinstance(candidate.get("tracking"), dict) else {}
    quality = candidate.get("data_quality", {}) if isinstance(candidate.get("data_quality"), dict) else {}
    fit_label, _ = quant_fit_label(tracking)
    elapsed = safe_int(tracking.get("elapsed_trading_days"), 0)
    ret = finite_float(tracking.get("return_since_select"))
    drawdown = finite_float(tracking.get("max_drawdown_since_select"))
    strategy_lane = str(candidate.get("strategy_lane") or "候选观察")
    pieces: list[str] = []
    if ret is not None and drawdown is not None:
        pieces.append(f"入选后 {elapsed} 个交易日区间变化 {ret:+.2f}%，最大回撤 {drawdown:+.2f}%。")
    else:
        pieces.append("入选后表现暂缺少可计算区间，先保留为观察样本。")
    if fit_label == "符合入选假设":
        pieces.append(f"当前结果与“{strategy_lane}”的入选观察方向暂时一致。")
    elif fit_label == "偏离入选假设":
        pieces.append(f"当前结果偏离“{strategy_lane}”的观察假设，需要复盘是趋势失效、量能未延续，还是入选时样本质量不足。")
    elif fit_label == "数据不足":
        pieces.append("本地日线或入选区间不足，不能判断是否符合策略预期。")
    else:
        pieces.append("当前证据还没有形成明确结论，需要等到策略复核窗口。")
    data_status = str(quality.get("status") or candidate.get("data_status") or "")
    if data_status and data_status != "数据完整":
        pieces.append(f"数据状态为“{data_status}”，相关解释需要降级。")
    return "".join(pieces)


def quant_next_observation(candidate: dict[str, Any]) -> str:
    tracking = candidate.get("tracking", {}) if isinstance(candidate.get("tracking"), dict) else {}
    group_key = str(candidate.get("group_key") or "")
    fit_label, _ = quant_fit_label(tracking)
    review_date = str(tracking.get("review_date") or "下一次完整日线更新后")
    if fit_label == "偏离入选假设":
        return f"{review_date} 优先复盘偏离原因：价格是否继续低于关键均线、量能是否萎缩、题材或市场环境是否改变；不直接升级候选。"
    if group_key == "board":
        return f"{review_date} 复核打板观察路径：看入选后1到5个交易日是否有量价延续和回撤可控，未延续则降级为普通观察样本。"
    if group_key in {"long_swing", "long_observe"}:
        return f"{review_date} 复核长期观察路径：看收盘价与 MA20、MACD 方向、RSI 过热/修复是否匹配入选假设。"
    return f"{review_date} 先确认数据完整，再对照入选理由做复盘。"


def quant_backtest_readiness(candidate: dict[str, Any]) -> dict[str, Any]:
    tracking = candidate.get("tracking", {}) if isinstance(candidate.get("tracking"), dict) else {}
    quality = candidate.get("data_quality", {}) if isinstance(candidate.get("data_quality"), dict) else {}
    row_count = safe_int(quality.get("row_count"), 0)
    elapsed = safe_int(tracking.get("elapsed_trading_days"), 0)
    data_tone = str(quality.get("tone") or "")
    if data_tone in {"ok", "field_gap"} and row_count >= 120 and elapsed >= 1:
        return {
            "status": "ready",
            "label": "可进入回测队列",
            "scope": "入选日至最新完整日线；同策略历史样本横向复核；不改变当前评分公式。",
            "reason": f"本地日线 {row_count} 条，入选后 {elapsed} 个交易日可计算。",
        }
    if data_tone in {"ok", "field_gap"} and row_count >= 60:
        return {
            "status": "partial",
            "label": "可做轻量复盘",
            "scope": "可做入选后表现回看；完整策略回测需要更多历史样本。",
            "reason": f"本地日线 {row_count} 条，入选后 {elapsed} 个交易日。",
        }
    return {
        "status": "blocked",
        "label": "暂不适合回测",
        "scope": "先补齐日线和入选后区间，再进入回测队列。",
        "reason": f"本地日线 {row_count} 条；数据状态 {quality.get('status', '数据缺失')}。",
    }


def quant_daily_summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    today = datetime.now().strftime("%Y-%m-%d")
    generation = generation_status_for_ui()
    items: list[dict[str, Any]] = []
    fit_counts = {"符合入选假设": 0, "等待更多交易日": 0, "偏离入选假设": 0, "数据不足": 0}
    backtest_ready = 0
    for candidate in rows:
        tracking = candidate.get("tracking", {}) if isinstance(candidate.get("tracking"), dict) else {}
        fit_label, fit_tone = quant_fit_label(tracking)
        fit_counts[fit_label] = fit_counts.get(fit_label, 0) + 1
        readiness = quant_backtest_readiness(candidate)
        if readiness["status"] == "ready":
            backtest_ready += 1
        items.append(
            {
                "symbol": candidate.get("symbol"),
                "name": candidate.get("name"),
                "strategy_lane": candidate.get("strategy_lane"),
                "strategy_name": candidate.get("strategy_name"),
                "select_date": candidate.get("select_date"),
                "select_reason": candidate.get("select_reason"),
                "current_price": candidate.get("current_price"),
                "pct_chg": candidate.get("pct_chg"),
                "risk_level": candidate.get("risk_level"),
                "fit_label": fit_label,
                "fit_tone": fit_tone,
                "current_status": tracking.get("status_label", "数据不足"),
                "strategy_expectation": tracking.get("expected_path", strategy_expected_path("")),
                "result_review": tracking.get("review_note", "暂缺复核备注。"),
                "reason_analysis": quant_reason_analysis(candidate),
                "next_observation": quant_next_observation(candidate),
                "backtest_readiness": readiness,
                "data_status": candidate.get("data_status"),
                "data_quality": candidate.get("data_quality", {}),
            }
        )
    limit_review = current_limit_up_review_digest()
    ordered = sorted(
        items,
        key=lambda item: (
            {"warn": 0, "ok": 1, "watch": 2, "missing": 3}.get(str(item.get("fit_tone")), 4),
            str(item.get("symbol") or ""),
        ),
    )
    return {
        "date": today,
        "title": "Quant 日总结",
        "summary": (
            f"今日新候选 {generation.get('candidate_count', 0)} 只，历史观察 {sum(1 for row in rows if not row.get('is_today_recommendation'))} 只；"
            f"当前 Quant 样本 {len(rows)} 只：{fit_counts.get('符合入选假设', 0)} 只阶段符合，"
            f"{fit_counts.get('偏离入选假设', 0)} 只偏离，"
            f"{fit_counts.get('等待更多交易日', 0)} 只等待确认，"
            f"{backtest_ready} 只已具备回测队列条件。"
        ),
        "candidate_generation": generation,
        "fit_counts": fit_counts,
        "backtest_ready_count": backtest_ready,
        "limit_up_review": limit_review,
        "items": ordered,
        "boundary": "Research Only；Paper Account Only；No Broker Route；Not Investment Advice；Not Position Advice。",
        "report_note": "后续观察路径是入选假设复核，不是价格预测，也不输出价格点位、交易动作或资金配置。",
    }


def normalize_review_date(value: str = "") -> str:
    text = str(value or "").strip()
    if text:
        digits = "".join(ch for ch in text if ch.isdigit())
        if len(digits) == 8:
            return f"{digits[:4]}-{digits[4:6]}-{digits[6:8]}"
    return to_shanghai_datetime().strftime("%Y-%m-%d")


def compact_review_date(value: str) -> str:
    return normalize_review_date(value).replace("-", "")


def parse_board_count(value: Any) -> int:
    if value is None:
        return 1
    if isinstance(value, (int, float)) and math.isfinite(float(value)):
        return max(1, int(float(value)))
    text = str(value or "").strip()
    if not text or text.lower() == "nan":
        return 1
    patterns = [
        r"(\d+)\s*连板",
        r"(\d+)\s*板",
        r"(\d+)\s*天\s*(\d+)\s*板",
        r"(\d+)\s*/\s*(\d+)",
    ]
    for pattern in patterns:
        hit = re.search(pattern, text)
        if hit:
            return max(1, safe_int(hit.groups()[-1], 1))
    return max(1, safe_int(text, 1))


def normalize_limit_up_time(value: Any) -> str:
    text = str(value or "").strip()
    if not text or text.lower() == "nan":
        return "数据缺失"
    digits = "".join(ch for ch in text if ch.isdigit())
    if len(digits) >= 6:
        return f"{digits[-6:-4]}:{digits[-4:-2]}:{digits[-2:]}"
    if len(digits) == 4:
        return f"{digits[:2]}:{digits[2:]}"
    return text


def load_limit_up_theme_map() -> dict[str, dict[str, Any]]:
    path = UNIVERSE_DIR / "stock_theme_map.csv"
    if not path.exists():
        return {}
    df = read_csv_frame(path, dtype=str)
    for warning in df.attrs.get("warnings", []):
        LOG.warning("limit_up_theme_map_csv_warning: path=%s warning=%s", path, warning)
    if df.empty:
        return {}
    result: dict[str, dict[str, Any]] = {}
    for _, row in df.iterrows():
        symbol = normalize_symbol(str(row.get("symbol") or row.get("code") or ""))
        if not symbol:
            continue
        result[symbol] = {
            "sector": str(row.get("sector") or "未知板块").strip() or "未知板块",
            "industry": str(row.get("industry") or "未知行业").strip() or "未知行业",
            "concept_tags": str(row.get("concept_tags") or "").strip(),
            "theme_tags": str(row.get("theme_tags") or "").strip(),
            "primary_theme": str(row.get("primary_theme") or row.get("theme_tags") or "未知题材").strip() or "未知题材",
            "theme_confidence": "mapped",
            "source": str(row.get("source") or "stock_theme_map.csv").strip() or "stock_theme_map.csv",
            "updated_at": str(row.get("updated_at") or "").strip(),
            "notes": str(row.get("notes") or "").strip(),
        }
    return result


def cached_news_items_for_theme(symbol: str, limit: int = 5) -> list[dict[str, Any]]:
    try:
        payload = fetch_news(symbol, limit=limit, network=False)
    except Exception as exc:  # noqa: BLE001 - cached news is optional enrichment
        LOG.warning("cached_news_items_fetch_failed: symbol=%s error=%s", symbol, exc)
        return []
    data = payload.get("data") if isinstance(payload, dict) else []
    return data if isinstance(data, list) else []


def limit_up_theme_for_symbol(
    symbol: str,
    name: str,
    raw_industry: str,
    theme_map: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    return build_theme_attribution(
        symbol=normalize_symbol(symbol),
        name=name,
        map_item=theme_map.get(normalize_symbol(symbol)) or {},
        raw_industry=raw_industry,
        news_items=cached_news_items_for_theme(symbol),
    )


def display_source_text(value: Any) -> str:
    text = str(value or "").strip()
    return (
        text.replace("provider行业", "行业线索")
        .replace("provider 行业", "行业线索")
        .replace("Provider 行业", "行业线索")
        .replace("provider", "外部数据源")
        .replace("Provider", "外部数据源")
        .replace("依赖 行业线索", "依赖行业线索")
        .replace("依赖 外部数据源", "依赖外部数据源")
    )


def sanitize_limit_up_attribution_row(row: dict[str, Any]) -> dict[str, Any]:
    cleaned = dict(row)
    sources = cleaned.get("attribution_sources")
    if isinstance(sources, list):
        cleaned_sources: list[dict[str, Any]] = []
        for source in sources:
            if not isinstance(source, dict):
                continue
            item = dict(source)
            if item.get("source_type") == "provider_industry":
                item["source_label"] = "行业线索"
                if item.get("source_name"):
                    item["source_name"] = display_source_text(item.get("source_name"))
                if item.get("evidence_text"):
                    item["evidence_text"] = display_source_text(item.get("evidence_text"))
            elif item.get("source_label"):
                item["source_label"] = display_source_text(item.get("source_label"))
            cleaned_sources.append(item)
        cleaned["attribution_sources"] = cleaned_sources
    if cleaned.get("attribution_summary"):
        cleaned["attribution_summary"] = display_source_text(cleaned.get("attribution_summary"))
    if cleaned.get("reason_reference"):
        cleaned["reason_reference"] = display_source_text(cleaned.get("reason_reference"))
    return cleaned


def sanitize_limit_up_review_payload(payload: dict[str, Any]) -> dict[str, Any]:
    cleaned = dict(payload)
    for key in ("rows", "top_rows"):
        rows = cleaned.get(key)
        if isinstance(rows, list):
            cleaned[key] = [
                sanitize_limit_up_attribution_row(dict(row))
                for row in rows
                if isinstance(row, dict)
            ]
    summary = cleaned.get("attribution_summary")
    if isinstance(summary, dict) and summary.get("message"):
        summary = dict(summary)
        summary["message"] = display_source_text(summary.get("message"))
        cleaned["attribution_summary"] = summary
    if cleaned.get("message_for_ui"):
        cleaned["message_for_ui"] = display_source_text(cleaned.get("message_for_ui"))
    return cleaned


def ensure_limit_up_rows_attribution(rows: list[dict[str, Any]], *, force: bool = False) -> list[dict[str, Any]]:
    if not rows:
        return []
    if not force and all(isinstance(row.get("attribution_sources"), list) for row in rows):
        return [sanitize_limit_up_attribution_row(dict(row)) for row in rows]
    theme_map = load_limit_up_theme_map()
    enriched: list[dict[str, Any]] = []
    for original in rows:
        row = dict(original)
        if isinstance(row.get("attribution_sources"), list):
            enriched.append(sanitize_limit_up_attribution_row(row))
            continue
        symbol = normalize_symbol(str(row.get("symbol") or ""))
        name = str(row.get("name") or symbol).strip()
        raw_industry = str(row.get("industry") or row.get("primary_theme") or "").strip()
        theme = limit_up_theme_for_symbol(symbol, name, raw_industry, theme_map)
        row["industry"] = theme.get("industry") or raw_industry or "未知行业"
        row["sector"] = theme.get("sector") or row.get("industry") or "未知板块"
        row["primary_theme"] = theme.get("primary_theme") or row.get("primary_theme") or "未知题材"
        row["theme_confidence"] = theme.get("theme_confidence") or row.get("theme_confidence") or "unknown"
        row["concept_tags"] = theme.get("concept_tags") or row.get("concept_tags") or "未知概念"
        row["theme_tags"] = theme.get("theme_tags") or row.get("theme_tags") or "未知题材"
        row["attribution_sources"] = theme.get("attribution_sources") or []
        row["attribution_summary"] = theme.get("attribution_summary") or "暂缺可追溯题材线索。"
        row["has_news_evidence"] = bool(theme.get("has_news_evidence"))
        row["has_mapped_evidence"] = bool(theme.get("has_mapped_evidence"))
        row["provider_only"] = bool(theme.get("provider_only"))
        row["reason_reference"] = (
            f"进入当日涨停池；题材线索为“{row['primary_theme']}”，行业线索为“{row['industry']}”；"
            f"连板数 {row.get('consecutive_boards', '数据缺失')}，首次封板 {row.get('first_seal_time', '数据缺失')}。"
            f"归因证据：{row['attribution_summary']}。"
        )
        enriched.append(sanitize_limit_up_attribution_row(row))
    return enriched


def normalize_limit_up_pool_frame(raw: pd.DataFrame, *, theme_map: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    if raw is None or raw.empty:
        return []
    frame = normalize_columns(raw.copy())
    code_col = find_column(frame, "symbol") or find_column(frame, "code") or find_column(frame, "代码")
    name_col = find_column(frame, "name") or find_column(frame, "名称")
    pct_col = find_column(frame, "pct_chg") or find_column(frame, "涨跌幅")
    price_col = find_column(frame, "price") or find_column(frame, "最新价")
    amount_col = find_column(frame, "amount") or find_column(frame, "成交额")
    turnover_col = find_column(frame, "turnover_rate") or find_column(frame, "换手率")
    seal_amount_col = find_column(frame, "seal_amount") or find_column(frame, "封板资金")
    first_col = find_column(frame, "first_seal_time") or find_column(frame, "首次封板时间")
    last_col = find_column(frame, "last_seal_time") or find_column(frame, "最后封板时间")
    break_col = find_column(frame, "break_count") or find_column(frame, "炸板次数")
    board_col = find_column(frame, "consecutive_boards") or find_column(frame, "连板数") or find_column(frame, "涨停统计")
    industry_col = find_column(frame, "industry") or find_column(frame, "所属行业")
    rows: list[dict[str, Any]] = []
    for _, raw_row in frame.iterrows():
        symbol = normalize_symbol(str(raw_row.get(code_col) if code_col else ""))
        if not symbol:
            continue
        name = str(raw_row.get(name_col) if name_col else symbol).strip() or symbol
        if "ST" in name.upper() or "退" in name:
            continue
        industry = str(raw_row.get(industry_col) if industry_col else "").strip()
        theme = limit_up_theme_for_symbol(symbol, name, industry, theme_map)
        consecutive = parse_board_count(raw_row.get(board_col) if board_col else None)
        row = {
            "symbol": symbol,
            "name": name,
            "price": chart_number(raw_row.get(price_col), 0.0) if price_col else "数据缺失",
            "pct_chg": chart_number(raw_row.get(pct_col), 0.0) if pct_col else "数据缺失",
            "amount": chart_number(raw_row.get(amount_col), 0.0) if amount_col else "数据缺失",
            "turnover_rate": chart_number(raw_row.get(turnover_col), 0.0) if turnover_col else "数据缺失",
            "seal_amount": chart_number(raw_row.get(seal_amount_col), 0.0) if seal_amount_col else "数据缺失",
            "first_seal_time": normalize_limit_up_time(raw_row.get(first_col) if first_col else ""),
            "last_seal_time": normalize_limit_up_time(raw_row.get(last_col) if last_col else ""),
            "break_count": safe_int(raw_row.get(break_col), 0) if break_col else 0,
            "consecutive_boards": consecutive,
            "industry": theme.get("industry") or industry or "未知行业",
            "sector": theme.get("sector") or "未知板块",
            "primary_theme": theme.get("primary_theme") or "未知题材",
            "theme_confidence": theme.get("theme_confidence") or "unknown",
            "concept_tags": theme.get("concept_tags") or "未知概念",
            "theme_tags": theme.get("theme_tags") or "未知题材",
            "attribution_sources": theme.get("attribution_sources") or [],
            "attribution_summary": theme.get("attribution_summary") or "暂缺可追溯题材线索。",
            "has_news_evidence": bool(theme.get("has_news_evidence")),
            "has_mapped_evidence": bool(theme.get("has_mapped_evidence")),
            "provider_only": bool(theme.get("provider_only")),
        }
        row["reason_reference"] = (
            f"进入当日涨停池；题材线索为“{row['primary_theme']}”，行业线索为“{row['industry']}”；"
            f"连板数 {row['consecutive_boards']}，首次封板 {row['first_seal_time']}。"
            f"归因证据：{row['attribution_summary']}。"
        )
        rows.append(row)
    rows.sort(key=lambda item: (safe_int(item.get("consecutive_boards"), 1), finite_float(item.get("seal_amount"), 0.0) or 0.0), reverse=True)
    return rows


def fetch_limit_up_review_from_provider(date_text: str) -> tuple[list[dict[str, Any]], int, list[str]]:
    try:
        import akshare as ak
    except ImportError:
        return [], 0, ["未安装 akshare，无法联网拉取涨停池。"]
    provider_date = compact_review_date(date_text)
    errors: list[str] = []
    theme_map = load_limit_up_theme_map()
    try:
        raw_pool = _call_with_timeout(lambda: ak.stock_zt_pool_em(date=provider_date), timeout_seconds=35.0)
    except Exception as exc:  # noqa: BLE001
        return [], 0, [f"涨停池 provider 失败：{exc}"]
    rows = normalize_limit_up_pool_frame(raw_pool, theme_map=theme_map)
    broken_count = 0
    try:
        raw_broken = _call_with_timeout(lambda: ak.stock_zt_pool_zbgc_em(date=provider_date), timeout_seconds=20.0)
        if raw_broken is not None and not raw_broken.empty:
            broken_frame = normalize_columns(raw_broken.copy())
            name_col = find_column(broken_frame, "name") or find_column(broken_frame, "名称")
            if name_col:
                broken_count = int(
                    sum(1 for value in broken_frame[name_col].tolist() if "ST" not in str(value).upper() and "退" not in str(value))
                )
            else:
                broken_count = int(len(broken_frame))
    except Exception as exc:  # noqa: BLE001
        errors.append(f"炸板池 provider 失败：{exc}")
    return rows, broken_count, errors


def limit_up_theme_summary(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[str, dict[str, Any]] = {}
    for row in rows:
        theme = str(row.get("primary_theme") or "未知题材")
        item = grouped.setdefault(
            theme,
            {
                "primary_theme": theme,
                "count": 0,
                "max_consecutive_boards": 0,
                "symbols": [],
                "representatives": [],
                "source_counts": {},
                "news_evidence_count": 0,
            },
        )
        item["count"] += 1
        item["max_consecutive_boards"] = max(item["max_consecutive_boards"], safe_int(row.get("consecutive_boards"), 1))
        if len(item["symbols"]) < 8:
            item["symbols"].append(row.get("symbol"))
        if len(item["representatives"]) < 3:
            item["representatives"].append(f"{row.get('name')}({row.get('symbol')})")
        if row.get("has_news_evidence"):
            item["news_evidence_count"] += 1
        sources = row.get("attribution_sources") if isinstance(row.get("attribution_sources"), list) else []
        for source in sources:
            if not isinstance(source, dict):
                continue
            source_type = str(source.get("source_type") or "unknown")
            item["source_counts"][source_type] = item["source_counts"].get(source_type, 0) + 1
    return sorted(grouped.values(), key=lambda item: (item["count"], item["max_consecutive_boards"]), reverse=True)[:10]


def limit_up_board_ladder(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    counts: dict[int, int] = {}
    for row in rows:
        board = safe_int(row.get("consecutive_boards"), 1)
        counts[board] = counts.get(board, 0) + 1
    return [{"consecutive_boards": board, "count": counts[board]} for board in sorted(counts, reverse=True)]


def limit_up_quant_intersections(rows: list[dict[str, Any]], quant_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    limit_by_symbol = {str(row.get("symbol")): row for row in rows}
    intersections: list[dict[str, Any]] = []
    for candidate in quant_rows:
        symbol = str(candidate.get("symbol") or "")
        hit = limit_by_symbol.get(symbol)
        if not hit:
            continue
        tracking = candidate.get("tracking", {}) if isinstance(candidate.get("tracking"), dict) else {}
        intersections.append(
            {
                "symbol": symbol,
                "name": candidate.get("name") or hit.get("name"),
                "strategy_lane": candidate.get("strategy_lane"),
                "quant_status": tracking.get("status_label", "数据不足"),
                "fit_label": quant_fit_label(tracking)[0],
                "limit_up_theme": hit.get("primary_theme"),
                "consecutive_boards": hit.get("consecutive_boards"),
                "reason_reference": (
                    f"该股同时出现在 Quant 候选和当日涨停池；需复核它是否仍符合“{candidate.get('strategy_lane', '候选观察')}”的入选假设。"
                ),
            }
        )
    return intersections


def limit_up_review_daily_digest(payload: dict[str, Any]) -> dict[str, Any]:
    themes = payload.get("theme_summary") if isinstance(payload.get("theme_summary"), list) else []
    intersections = payload.get("quant_intersections") if isinstance(payload.get("quant_intersections"), list) else []
    provider_errors = payload.get("provider_errors") if isinstance(payload.get("provider_errors"), list) else []
    top_themes = [
        {
            "primary_theme": item.get("primary_theme", "未知题材"),
            "count": safe_int(item.get("count"), 0),
            "max_consecutive_boards": safe_int(item.get("max_consecutive_boards"), 0),
            "representatives": list(item.get("representatives") or [])[:3],
        }
        for item in themes[:5]
        if isinstance(item, dict)
    ]
    quant_hits = [
        {
            "symbol": item.get("symbol"),
            "name": item.get("name"),
            "strategy_lane": item.get("strategy_lane"),
            "fit_label": item.get("fit_label"),
            "limit_up_theme": item.get("limit_up_theme"),
            "consecutive_boards": item.get("consecutive_boards"),
            "reason_reference": item.get("reason_reference"),
        }
        for item in intersections[:8]
        if isinstance(item, dict)
    ]
    ok = bool(payload.get("ok"))
    limit_up_count = safe_int(payload.get("limit_up_count"), 0)
    broken_count = safe_int(payload.get("broken_count"), 0)
    connected_count = safe_int(payload.get("connected_count"), 0)
    seal_rate = payload.get("seal_rate", "数据缺失")
    if ok:
        theme_text = "、".join(f"{item['primary_theme']} {item['count']}只" for item in top_themes[:3]) or "题材归因待补充"
        hit_text = f"Quant 候选交集 {len(quant_hits)} 只"
        attribution = payload.get("attribution_summary") if isinstance(payload.get("attribution_summary"), dict) else {}
        trace_text = (
            f"追溯线索 {attribution.get('traceable_count', '数据缺失')} 只，"
            f"公告/新闻 {attribution.get('news_evidence_count', '数据缺失')} 只"
        )
        report_line = (
            f"涨停池 {limit_up_count} 只，连板 {connected_count} 只，"
            f"封板未遂 {broken_count} 只，封板率 {seal_rate}%；"
            f"题材线索：{theme_text}；{hit_text}；{trace_text}。"
        )
        status = "available"
    else:
        report_line = str(payload.get("message_for_ui") or "本地涨停复盘缓存缺失；可点击刷新涨停复盘显式联网拉取。")
        status = "missing"
    return {
        "status": status,
        "ok": ok,
        "date": payload.get("date", normalize_review_date()),
        "source": payload.get("source", "cache"),
        "updated_at": payload.get("updated_at", "数据缺失"),
        "limit_up_count": limit_up_count if ok else "数据缺失",
        "connected_count": connected_count if ok else "数据缺失",
        "broken_count": broken_count if ok else "数据缺失",
        "seal_rate": seal_rate if ok else "数据缺失",
        "top_themes": top_themes,
        "quant_intersections": quant_hits,
        "attribution_summary": payload.get("attribution_summary", {}),
        "provider_errors": [str(item) for item in provider_errors[:3]],
        "report_line": report_line,
        "boundary": "Research Only；涨停复盘用于题材和封板质量学习，不代表真实可成交，不构成投资建议或交易动作。",
    }


def current_limit_up_review_digest() -> dict[str, Any]:
    try:
        payload = read_limit_up_review_cache()
    except Exception as exc:  # noqa: BLE001
        payload = {
            "ok": False,
            "date": normalize_review_date(),
            "source": "cache_read_failed",
            "message_for_ui": f"涨停复盘缓存读取失败：{exc}",
            "provider_errors": [str(exc)],
        }
    return limit_up_review_daily_digest(payload)


def build_limit_up_review_payload(
    *,
    date_text: str,
    rows: list[dict[str, Any]],
    broken_count: int = 0,
    provider_errors: list[str] | None = None,
    source: str = "cache",
) -> dict[str, Any]:
    rows = ensure_limit_up_rows_attribution(rows)
    quant_rows = candidate_rows(limit=18)
    sealed_count = len(rows)
    attempted = sealed_count + max(0, int(broken_count or 0))
    seal_rate = round(sealed_count / attempted * 100, 2) if attempted else "数据缺失"
    connected_count = sum(1 for row in rows if safe_int(row.get("consecutive_boards"), 1) >= 2)
    attribution_summary = summarize_attribution_sources(rows)
    payload = {
        "ok": bool(rows),
        "date": normalize_review_date(date_text),
        "source": source,
        "updated_at": datetime.now().isoformat(timespec="seconds"),
        "limit_up_count": sealed_count,
        "broken_count": int(broken_count or 0),
        "seal_rate": seal_rate,
        "connected_count": connected_count,
        "board_ladder": limit_up_board_ladder(rows),
        "theme_summary": limit_up_theme_summary(rows),
        "quant_intersections": limit_up_quant_intersections(rows, quant_rows),
        "attribution_summary": attribution_summary,
        "rows": rows[:80],
        "top_rows": rows[:20],
        "provider_errors": provider_errors or [],
        "message_for_ui": (
            f"涨停池 {sealed_count} 只，封板未遂 {int(broken_count or 0)} 只，封板率 {seal_rate}%；"
            f"{attribution_summary.get('message', '')}仅用于涨停复盘和题材学习。"
            if rows
            else "本地涨停复盘缓存缺失；点击刷新涨停复盘可显式联网拉取。"
        ),
        "boundary": "Research Only；涨停复盘只用于题材和封板质量学习，不代表真实可成交，不构成投资建议或交易动作。",
    }
    return payload


def read_limit_up_review_cache(*, force_attribution: bool = False) -> dict[str, Any]:
    payload = read_json_file(LIMIT_UP_REVIEW_CACHE)
    if payload:
        payload.setdefault("source", "cache")
        payload.setdefault("ok", bool(payload.get("rows")))
        needs_attribution = not isinstance(payload.get("attribution_summary"), dict)
        rows = payload.get("rows") if isinstance(payload.get("rows"), list) else []
        if rows and any(not isinstance(row, dict) or not isinstance(row.get("attribution_sources"), list) for row in rows[:5]):
            needs_attribution = True
        if needs_attribution or force_attribution:
            return enrich_cached_limit_up_review_payload(payload, force=force_attribution)
        return sanitize_limit_up_review_payload(payload)
    return build_limit_up_review_payload(date_text=normalize_review_date(), rows=[], source="missing_cache")


def write_limit_up_review_cache(payload: dict[str, Any]) -> None:
    LIMIT_UP_REVIEW_CACHE.parent.mkdir(parents=True, exist_ok=True)
    LIMIT_UP_REVIEW_CACHE.write_text(json.dumps(clean_payload(payload), ensure_ascii=False, indent=2), encoding="utf-8")


def enrich_cached_limit_up_review_payload(payload: dict[str, Any], *, force: bool = False) -> dict[str, Any]:
    rows = payload.get("rows") if isinstance(payload.get("rows"), list) else []
    top_rows = payload.get("top_rows") if isinstance(payload.get("top_rows"), list) else rows[:20]
    if not rows:
        return payload
    enriched_rows = ensure_limit_up_rows_attribution([dict(row) for row in rows if isinstance(row, dict)], force=force)
    enriched_top_rows = ensure_limit_up_rows_attribution([dict(row) for row in top_rows if isinstance(row, dict)], force=force)
    enriched = dict(payload)
    enriched["rows"] = enriched_rows
    enriched["top_rows"] = enriched_top_rows
    enriched["attribution_summary"] = summarize_attribution_sources(enriched_rows)
    enriched["theme_summary"] = limit_up_theme_summary(enriched_rows)
    enriched["quant_intersections"] = limit_up_quant_intersections(enriched_rows, candidate_rows(limit=18))
    enriched["message_for_ui"] = (
        f"涨停池 {enriched.get('limit_up_count', len(enriched_rows))} 只，"
        f"封板未遂 {enriched.get('broken_count', 0)} 只，封板率 {enriched.get('seal_rate', '数据缺失')}%；"
        f"{enriched['attribution_summary'].get('message', '')}仅用于涨停复盘和题材学习。"
    )
    enriched["cache_enriched_locally"] = True
    return enriched


def rebuild_limit_up_review_attribution_cache() -> dict[str, Any]:
    payload = read_limit_up_review_cache(force_attribution=True)
    if payload.get("rows"):
        payload["cache_enriched_after_theme_map_update"] = True
        write_limit_up_review_cache(payload)
    return payload


def refresh_limit_up_review(date_text: str) -> dict[str, Any]:
    rows, broken_count, errors = fetch_limit_up_review_from_provider(date_text)
    payload = build_limit_up_review_payload(
        date_text=date_text,
        rows=rows,
        broken_count=broken_count,
        provider_errors=errors,
        source="akshare.eastmoney_zt_pool",
    )
    if rows:
        write_limit_up_review_cache(payload)
    else:
        cached = read_limit_up_review_cache()
        cached["provider_errors"] = errors or cached.get("provider_errors", [])
        cached["message_for_ui"] = "涨停池外部数据源暂不可用，页面显示本地缓存。" if cached.get("rows") else payload["message_for_ui"]
        cached["source"] = "cache_after_provider_failed" if cached.get("rows") else payload["source"]
        return cached
    return payload


def chart_rows(stock_df: pd.DataFrame, window: int = 160) -> list[dict[str, Any]]:
    chart_df = stock_df.tail(window).copy()
    indicators = technical_indicators.get_all_indicators(chart_df)
    rows: list[dict[str, Any]] = []
    for idx, (_, row) in enumerate(chart_df.iterrows()):
        rows.append(
            {
                "date": row.get("date").strftime("%Y-%m-%d") if isinstance(row.get("date"), pd.Timestamp) else str(row.get("date")),
                "open": chart_number(row.get("open")),
                "high": chart_number(row.get("high")),
                "low": chart_number(row.get("low")),
                "close": chart_number(row.get("close")),
                "volume": chart_number(row.get("volume")),
                "amount": chart_number(row.get("amount")),
                "ma5": chart_number((indicators.get("ma5") or [0])[idx]),
                "ma10": chart_number((indicators.get("ma10") or [0])[idx]),
                "ma20": chart_number((indicators.get("ma20") or [0])[idx]),
                "macd_dif": chart_number((indicators.get("dif") or [0])[idx]),
                "macd_dea": chart_number((indicators.get("dea") or [0])[idx]),
                "macd_bar": chart_number((indicators.get("macd") or [0])[idx]),
                "kdj_k": chart_number((indicators.get("k") or [50])[idx], 50.0),
                "kdj_d": chart_number((indicators.get("d") or [50])[idx], 50.0),
                "kdj_j": chart_number((indicators.get("j") or [50])[idx], 50.0),
                "rsi": chart_number((indicators.get("rsi6") or [50])[idx], 50.0),
            }
        )
    return rows


def build_structured_chart_series(stock_df: pd.DataFrame, window: int = 160) -> dict[str, Any]:
    """Explicit per-indicator chart series for stable frontend rendering + tests.

    Mirrors ``chart_rows`` but exposes macd / kdj / rsi / volume / ma as named
    arrays instead of burying them in each k-line row. Every indicator block
    carries an availability flag + Chinese degrade hint so the UI can render a
    fallback message instead of a blank chart or NaN/undefined when an indicator
    cannot be computed (e.g. too few trading days). Research only.
    """
    chart_df = stock_df.tail(window).copy()
    n = len(chart_df)
    dates = [
        row.get("date").strftime("%Y-%m-%d") if isinstance(row.get("date"), pd.Timestamp) else str(row.get("date"))
        for _, row in chart_df.iterrows()
    ]
    if n == 0:
        return {
            "available": False,
            "dates": [],
            "volume_data": [],
            "ma": {},
            "macd": {},
            "kdj": {},
            "rsi": {},
            "indicator_availability": {},
            "degrade_hint": "本地暂无该股票日线数据，无法绘制图表。",
        }
    indicators = technical_indicators.get_all_indicators(chart_df)

    def series(key: str, default: float | None = None) -> list[Any] | None:
        arr = indicators.get(key)
        if not isinstance(arr, (list, tuple)) or len(arr) < n:
            return None
        return [chart_number(arr[i], default) for i in range(n)]

    volume = [chart_number(r.get("volume")) for _, r in chart_df.iterrows()]
    amount = [chart_number(r.get("amount")) for _, r in chart_df.iterrows()]
    macd_dif, macd_dea, macd_bar = series("dif"), series("dea"), series("macd")
    kdj_k, kdj_d, kdj_j = series("k", 50.0), series("d", 50.0), series("j", 50.0)
    rsi6, rsi12, rsi24 = series("rsi6", 50.0), series("rsi12"), series("rsi24")
    ma5, ma10, ma20 = series("ma5"), series("ma10"), series("ma20")

    def _avail(*arrs: Any) -> bool:
        return all(a is not None for a in arrs)

    macd_ok, kdj_ok = _avail(macd_dif, macd_dea, macd_bar), _avail(kdj_k, kdj_d, kdj_j)
    rsi_ok = rsi6 is not None
    indicator_availability = {
        "macd": {"available": macd_ok, "hint": "" if macd_ok else "MACD 数据不足（需更多交易日），暂不绘制。"},
        "kdj": {"available": kdj_ok, "hint": "" if kdj_ok else "KDJ 数据不足，暂不绘制。"},
        "rsi": {"available": rsi_ok, "hint": "" if rsi_ok else "RSI 数据不足，暂不绘制。"},
        "volume": {"available": any(v is not None for v in volume), "hint": ""},
    }
    return {
        "available": True,
        "window": n,
        "dates": dates,
        "close": [chart_number(r.get("close")) for _, r in chart_df.iterrows()],
        "volume_data": [{"date": dates[i], "volume": volume[i], "amount": amount[i]} for i in range(n)],
        "ma": {"ma5": ma5, "ma10": ma10, "ma20": ma20},
        "macd": {"dif": macd_dif, "dea": macd_dea, "bar": macd_bar},
        "kdj": {"k": kdj_k, "d": kdj_d, "j": kdj_j},
        "rsi": {"rsi6": rsi6, "rsi12": rsi12, "rsi24": rsi24},
        "indicator_availability": indicator_availability,
    }


def chart_markers(
    indicators: dict[str, Any],
    select_date: str,
    select_reason: str,
    latest_date: str,
    tracking: dict[str, Any] | None = None,
) -> list[dict[str, str]]:
    active_select_date = select_date or latest_date
    tracking = tracking or {}
    expected_date = str(tracking.get("review_date") or latest_date)
    expected_label = f"{tracking.get('review_offset_days', 5)}日复核"
    expected_text = (
        f"策略预期复核点：{tracking.get('expected_path', '对照入选时的观察假设复盘。')}"
        f"{tracking.get('review_note', '')}"
    )

    def marker(panel: str, date: str, label: str, text: str, kind: str) -> dict[str, str]:
        return {
            "panel": panel,
            "date": date or latest_date,
            "label": label,
            "text": text,
            "kind": kind,
        }

    markers = [
        marker(
            "price",
            active_select_date,
            "入选观察",
            f"入选理由：{select_reason}。结合收盘价 {display_number(indicators.get('close'), 2)} 与 MA20 {display_number(indicators.get('ma20'), 2)} 复盘，不构成操作建议。",
            "selection",
        ),
        marker(
            "volume",
            active_select_date,
            "量能理由",
            f"量能观察：最新成交量 {display_number(indicators.get('volume'), 0)}，用于解释入选当时的活跃度，不构成操作建议。",
            "reason",
        ),
        marker(
            "macd",
            active_select_date,
            "MACD理由",
            f"DIF {display_number(indicators.get('dif'), 3)} / DEA {display_number(indicators.get('dea'), 3)} / MACD柱 {display_number(indicators.get('macd'), 3)}，只用于指标学习复盘。",
            "reason",
        ),
        marker(
            "kdj",
            active_select_date,
            "KDJ理由",
            f"K {display_number(indicators.get('k'), 1)} / D {display_number(indicators.get('d'), 1)} / J {display_number(indicators.get('j'), 1)}，只用于观察短线摆动状态。",
            "reason",
        ),
        marker(
            "rsi",
            active_select_date,
            "RSI理由",
            f"RSI6 {display_number(indicators.get('rsi6'), 1)} / RSI12 {display_number(indicators.get('rsi12'), 1)}，只用于观察强弱与过热风险。",
            "reason",
        ),
        marker(
            "price",
            latest_date,
            "后续观察",
            "下一次复盘观察收盘价与 MA20 的关系；这是学习锚点，不构成价格判断，不构成操作建议。",
            "next_review",
        ),
        marker(
            "volume",
            latest_date,
            "后续观察",
            "下一次复盘观察成交量是否继续支持价格变化；这是学习锚点，不构成价格判断，不构成操作建议。",
            "next_review",
        ),
        marker(
            "macd",
            latest_date,
            "后续观察",
            "下一次复盘观察 DIF/DEA 与 MACD柱方向；这是学习锚点，不构成价格判断，不构成操作建议。",
            "next_review",
        ),
        marker(
            "kdj",
            latest_date,
            "后续观察",
            "下一次复盘观察 K/D/J 是否延续钝化或回落；这是学习锚点，不构成价格判断，不构成操作建议。",
            "next_review",
        ),
        marker(
            "rsi",
            latest_date,
            "后续观察",
            "下一次复盘观察 RSI 是否接近过热或回落区间；这是学习锚点，不构成价格判断，不构成操作建议。",
            "next_review",
        ),
    ]
    for panel, label in (
        ("price", expected_label),
        ("volume", "量能复核"),
        ("macd", "MACD复核"),
        ("kdj", "KDJ复核"),
        ("rsi", "RSI复核"),
    ):
        markers.append(marker(panel, expected_date, label, expected_text, "expected_review"))
    return markers


def _compact_agent_candidate(row: dict[str, Any]) -> dict[str, Any]:
    """Small, read-only candidate summary for Agent grounding prompts."""
    compact: dict[str, Any] = {}
    for key in (
        "symbol",
        "name",
        "strategy_lane",
        "strategy_bucket",
        "priority_rank",
        "priority_label",
        "priority_score",
        "risk_level",
        "data_status",
        "candidate_origin_label",
        "is_today_recommendation",
        "pct_chg",
    ):
        value = row.get(key)
        if value not in (None, ""):
            compact[key] = value
    reason = row.get("reason_cn") or row.get("priority_reason") or row.get("focus_reason")
    if reason:
        text = str(reason)
        compact["reason"] = text[:220] + ("..." if len(text) > 220 else "")
    tracking = row.get("tracking") if isinstance(row.get("tracking"), dict) else {}
    if tracking:
        compact["tracking"] = {
            key: tracking.get(key)
            for key in ("status_label", "return_since_select", "max_drawdown_since_select", "elapsed_trading_days", "review_note")
            if tracking.get(key) not in (None, "")
        }
    return compact


def _agent_short_text(value: Any, *, max_len: int = 260) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    if not text or text.lower() == "nan":
        return "数据缺失"
    return text[:max_len] + ("..." if len(text) > max_len else "")


def _compact_agent_news(news: dict[str, Any]) -> dict[str, Any]:
    items = news.get("items") if isinstance(news.get("items"), list) else []
    return {
        "available": bool(news.get("available")),
        "count": safe_int(news.get("count"), len(items)),
        "hint": _agent_short_text(news.get("hint"), max_len=180) if news.get("hint") else "",
        "headlines": [
            {
                "published": _agent_short_text(item.get("published"), max_len=20),
                "title": _agent_short_text(item.get("title"), max_len=120),
                "source": _agent_short_text(item.get("source"), max_len=40),
            }
            for item in items[:5]
            if isinstance(item, dict)
        ],
    }


def _compact_agent_research_reference(reference: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(reference, dict) or not reference.get("available"):
        return {
            "available": False,
            "message": _agent_short_text(reference.get("message") if isinstance(reference, dict) else "研究参考缺失"),
        }
    band = reference.get("price_band") if isinstance(reference.get("price_band"), dict) else {}
    support = reference.get("support_resistance") if isinstance(reference.get("support_resistance"), dict) else {}
    backtest = reference.get("simple_backtest") if isinstance(reference.get("simple_backtest"), dict) else {}
    trend = reference.get("recent_trend") if isinstance(reference.get("recent_trend"), dict) else {}
    projection = reference.get("scenario_projection") if isinstance(reference.get("scenario_projection"), dict) else {}
    scenarios = projection.get("scenarios") if isinstance(projection.get("scenarios"), list) else []
    return {
        "available": True,
        "disclaimer": _agent_short_text(reference.get("disclaimer"), max_len=180),
        "price_band": {
            "latest": finite_float(band.get("latest")),
            "volatility_label": band.get("volatility_label") or "数据缺失",
            "daily_volatility_pct": finite_float(band.get("daily_volatility_pct")),
            "reference_1d_pct": finite_float(band.get("reference_1d_pct")),
            "reference_5d_pct": finite_float(band.get("reference_5d_pct")),
            "reference_1d": band.get("reference_1d") if isinstance(band.get("reference_1d"), dict) else {},
            "reference_5d": band.get("reference_5d") if isinstance(band.get("reference_5d"), dict) else {},
            "note": _agent_short_text(band.get("note"), max_len=220),
        },
        "support_resistance": {
            "high_60d": finite_float(support.get("high_60d")),
            "low_60d": finite_float(support.get("low_60d")),
            "pct_from_high": finite_float(support.get("pct_from_high")),
            "pct_from_low": finite_float(support.get("pct_from_low")),
            "position": support.get("position") or "数据缺失",
        },
        "simple_backtest": {
            "total_trades": safe_int(backtest.get("total_trades"), 0),
            "win_rate_pct": finite_float(backtest.get("win_rate_pct")),
            "avg_return_pct": finite_float(backtest.get("avg_return_pct")),
            "label": _agent_short_text(backtest.get("label"), max_len=160),
            "note": _agent_short_text(backtest.get("note"), max_len=180),
        },
        "recent_trend": {
            "return_5d_pct": finite_float(trend.get("return_5d_pct")),
            "return_20d_pct": finite_float(trend.get("return_20d_pct")),
            "trend_5d": trend.get("trend_5d") or "数据缺失",
            "trend_20d": trend.get("trend_20d") or "数据缺失",
        },
        "scenario_projection": {
            "bias": projection.get("bias") or "数据缺失",
            "thesis": _agent_short_text(projection.get("thesis"), max_len=240),
            "structure_note": _agent_short_text(projection.get("structure_note"), max_len=220),
            "confidence_note": _agent_short_text(projection.get("confidence_note"), max_len=240),
            "scenarios": [
                {
                    "name": _agent_short_text(item.get("name"), max_len=40),
                    "tone": item.get("tone") or "",
                    "weight": item.get("weight") or "",
                    "range": item.get("range") or "数据缺失",
                    "trigger": _agent_short_text(item.get("trigger"), max_len=180),
                    "watch": _agent_short_text(item.get("watch"), max_len=220),
                }
                for item in scenarios[:3]
                if isinstance(item, dict)
            ],
        },
    }


def _compact_agent_focused_candidate(candidate: dict[str, Any]) -> dict[str, Any]:
    compact = _compact_agent_candidate(candidate)
    for key in (
        "current_price",
        "macd_val",
        "kdj_val",
        "rsi_val",
        "select_date",
        "signal_date",
        "select_reason",
        "focus_reason",
        "strategy_name",
        "detail_lane",
        "group_key",
        "source_label",
        "data_hint",
        "strategy_conflict_note",
    ):
        value = candidate.get(key)
        if value not in (None, ""):
            compact[key] = _agent_short_text(value) if isinstance(value, str) else value
    quality = candidate.get("data_quality") if isinstance(candidate.get("data_quality"), dict) else {}
    if quality:
        compact["data_quality"] = {
            key: quality.get(key)
            for key in ("status", "tone", "latest_date", "row_count", "stale_days", "hint")
            if quality.get(key) not in (None, "")
        }
        reasons = quality.get("missing_reasons") if isinstance(quality.get("missing_reasons"), list) else []
        if reasons:
            compact["data_quality"]["missing_reasons"] = [_agent_short_text(item, max_len=120) for item in reasons[:5]]
    return compact


def _sanitize_agent_focus_locator(value: Any) -> dict[str, str]:
    if not isinstance(value, dict):
        return {}
    symbol = normalize_symbol(str(value.get("symbol") or value.get("ts_code") or ""))
    if not symbol:
        return {}
    return {
        "symbol": symbol,
        "name": _agent_short_text(value.get("name"), max_len=60) if value.get("name") else "",
        "entry_date": str(value.get("entry_date") or value.get("select_date") or "")[:10],
        "lane": _agent_short_text(value.get("lane") or value.get("detail_lane") or "", max_len=40),
        "source": _agent_short_text(value.get("source") or "ui_candidate_discussion", max_len=60),
    }


def _sanitize_agent_client_context(client_context: dict[str, Any]) -> dict[str, Any]:
    """Keep UI-provided chat context small and non-authoritative.

    The browser may identify the stock the user is discussing, but backend
    evidence is rebuilt from local files before it reaches the LLM.
    """
    if not isinstance(client_context, dict):
        return {}
    sanitized: dict[str, Any] = {}
    for key in ("ui_candidate_count", "ui_paper_loop_intent", "ui_research_only", "ui_no_broker", "ui_not_investment_advice"):
        value = client_context.get(key)
        if value not in (None, ""):
            sanitized[key] = value if isinstance(value, bool | int | float) else _agent_short_text(value, max_len=180)
    for key in ("ui_today_recommendations", "ui_observation_candidates"):
        rows = client_context.get(key) if isinstance(client_context.get(key), list) else []
        compact_rows: list[dict[str, Any]] = []
        for row in rows[:8]:
            if not isinstance(row, dict):
                continue
            compact_rows.append(
                {
                    "symbol": normalize_symbol(str(row.get("symbol") or row.get("ts_code") or "")),
                    "name": _agent_short_text(row.get("name") or row.get("stock_name"), max_len=60),
                    "strategy_type": _agent_short_text(row.get("strategy_type") or row.get("strategy") or row.get("lane"), max_len=80),
                    "priority_rank": _agent_short_text(row.get("priority_rank") or row.get("rank"), max_len=20),
                    "action_state": _agent_short_text(row.get("action_state") or row.get("action"), max_len=80),
                    "reason": _agent_short_text(row.get("reason") or row.get("selection_reason") or row.get("human_reason"), max_len=180),
                }
            )
        if compact_rows:
            sanitized[key] = compact_rows
    focus = _sanitize_agent_focus_locator(client_context.get("focused_candidate"))
    if focus:
        sanitized["focused_candidate_locator"] = focus
    return sanitized


def _build_focused_candidate_agent_context(locator: dict[str, str]) -> dict[str, Any]:
    symbol = normalize_symbol(str(locator.get("symbol") or ""))
    if not symbol:
        return {}
    entry_date = str(locator.get("entry_date") or "")[:10]
    lane = str(locator.get("lane") or "").strip()
    candidate = find_visible_candidate(symbol, entry_date=entry_date, lane=lane) or {}
    stock_df = load_stock_data(symbol)
    latest_date = "数据缺失"
    pct_chg = finite_float(candidate.get("pct_chg"))
    indicators: dict[str, Any] = {}
    if not stock_df.empty:
        latest = stock_df.iloc[-1]
        latest_date = latest.get("date").strftime("%Y-%m-%d") if isinstance(latest.get("date"), pd.Timestamp) else str(latest.get("date") or "数据缺失")
        pct_chg = pct_chg if pct_chg is not None else latest_pct_change(stock_df)
        if len(stock_df) >= 20:
            indicators = latest_indicators(stock_df)
    chart_select_date = str(candidate.get("select_date") or candidate.get("signal_date") or entry_date or latest_date)
    select_reason = str(candidate.get("select_reason") or candidate.get("focus_reason") or candidate.get("reason_cn") or "数据缺失")
    tracking = candidate.get("tracking") if isinstance(candidate.get("tracking"), dict) else {}
    quality = candidate.get("data_quality") if isinstance(candidate.get("data_quality"), dict) else stock_data_quality(stock_df, symbol)
    news = build_news_summary(symbol)
    industry = build_industry_comparison(symbol, pct_chg)
    tracking_vs_benchmark = build_tracking_vs_benchmark(stock_df, chart_select_date)
    reference = build_research_reference(stock_df) if not stock_df.empty else {"available": False, "message": "本地日线缺失，无法生成研究情景。"}
    evidence = candidate_paper_evidence(candidate) if candidate else {}
    paper_status = paper_simulation_status_for_candidate(symbol, entry_date=chart_select_date, lane=lane)
    marker_preview: list[dict[str, Any]] = []
    if indicators and latest_date != "数据缺失":
        marker_preview = chart_markers(indicators, chart_select_date, select_reason, latest_date, tracking)[:8]

    focused = {
        "source": "server_rebuilt_from_local_files",
        "locator": locator,
        "basic": {
            "symbol": symbol,
            "name": candidate.get("name") or locator.get("name") or candidate_name(symbol),
            "latest_date": latest_date,
            "current_price": candidate.get("current_price") or finite_float(indicators.get("close")) or "数据缺失",
            "pct_chg": pct_chg if pct_chg is not None else "数据缺失",
            "risk_level": candidate.get("risk_level") or "数据缺失",
        },
        "candidate": _compact_agent_focused_candidate(candidate) if candidate else {"symbol": symbol, "status": "not_in_current_candidate_pool"},
        "technical_indicators": {
            "macd": candidate.get("macd_val") or (
                f"DIF {display_number(indicators.get('dif'), 3)} / DEA {display_number(indicators.get('dea'), 3)} / 柱 {display_number(indicators.get('macd'), 3)}"
                if indicators else "数据缺失"
            ),
            "kdj": candidate.get("kdj_val") or (
                f"K {display_number(indicators.get('k'), 1)} / D {display_number(indicators.get('d'), 1)} / J {display_number(indicators.get('j'), 1)}"
                if indicators else "数据缺失"
            ),
            "rsi": candidate.get("rsi_val") or (
                f"RSI6 {display_number(indicators.get('rsi6'), 1)} / RSI12 {display_number(indicators.get('rsi12'), 1)}"
                if indicators else "数据缺失"
            ),
            "ma": {
                "ma5": finite_float(indicators.get("ma5")),
                "ma20": finite_float(indicators.get("ma20")),
                "ma60": finite_float(indicators.get("ma60")),
            },
        },
        "strategy_review": {
            "strategy_name": candidate.get("strategy_name") or "数据缺失",
            "strategy_lane": candidate.get("strategy_lane") or "数据缺失",
            "strategy_bucket": candidate.get("strategy_bucket") or "数据缺失",
            "select_date": chart_select_date,
            "select_reason": _agent_short_text(select_reason, max_len=220),
            "tracking": tracking or {"status_label": "数据缺失"},
            "data_quality": quality or {"status": "数据缺失"},
        },
        "research_reference": _compact_agent_research_reference(reference),
        "news_summary": _compact_agent_news(news),
        "industry_comparison": industry,
        "tracking_vs_benchmark": tracking_vs_benchmark,
        "paper_simulation": paper_status,
        "paper_research_contract": evidence.get("paper_research_contract") if isinstance(evidence, dict) else {},
        "paper_evidence_lines": evidence.get("lines", [])[:8] if isinstance(evidence, dict) else [],
        "chart_markers_preview": marker_preview,
        "boundary": "Research Only / Paper Account Only / No Broker Route / Not Investment Advice / Not Position Advice.",
    }
    return clean_payload(focused)


def _attach_focused_candidate_context(context: dict[str, Any], client_context: dict[str, Any]) -> None:
    locator = client_context.get("focused_candidate_locator") if isinstance(client_context.get("focused_candidate_locator"), dict) else {}
    if not locator:
        return
    try:
        focused = _build_focused_candidate_agent_context(locator)
    except Exception as exc:  # noqa: BLE001 - focused evidence is enrichment; keep chat usable
        LOG.warning("agent_focused_candidate_context_failed: locator=%s error=%s", locator, exc)
        focused = {
            "source": "server_rebuild_failed",
            "locator": locator,
            "status": "failed",
            "message": f"单股证据包生成失败：{exc}",
            "boundary": "Research Only / Paper Account Only / No Broker Route.",
        }
    if not focused:
        return
    context["focused_candidate"] = focused
    symbol = focused.get("basic", {}).get("symbol") if isinstance(focused.get("basic"), dict) else locator.get("symbol")
    date = focused.get("basic", {}).get("latest_date") if isinstance(focused.get("basic"), dict) else ""
    if symbol:
        context["symbol"] = symbol
    if date and date != "数据缺失":
        context["date"] = date
    gaps = context.setdefault("data_gaps", [])
    if isinstance(gaps, list):
        quality = focused.get("strategy_review", {}).get("data_quality") if isinstance(focused.get("strategy_review"), dict) else {}
        reasons = quality.get("missing_reasons") if isinstance(quality, dict) and isinstance(quality.get("missing_reasons"), list) else []
        for reason in reasons[:4]:
            gaps.append(f"{symbol}: {reason}")
        news = focused.get("news_summary") if isinstance(focused.get("news_summary"), dict) else {}
        if news and not news.get("available"):
            gaps.append(f"{symbol}: 新闻线索缺失或未刷新")
        industry = focused.get("industry_comparison") if isinstance(focused.get("industry_comparison"), dict) else {}
        if industry and not industry.get("available"):
            gaps.append(f"{symbol}: {industry.get('hint') or '行业对比缺失'}")
        reference = focused.get("research_reference") if isinstance(focused.get("research_reference"), dict) else {}
        if reference and not reference.get("available"):
            gaps.append(f"{symbol}: {reference.get('message') or '研究情景缺失'}")


def _compact_backtest_context(payload: dict[str, Any], *, path: Path) -> dict[str, Any]:
    metrics = payload.get("metrics") if isinstance(payload.get("metrics"), dict) else {}
    symbols = payload.get("symbols") if isinstance(payload.get("symbols"), list) else []
    warnings = payload.get("warnings") if isinstance(payload.get("warnings"), list) else []
    return {
        "available": bool(payload),
        "status": payload.get("status") or "",
        "strategy": payload.get("strategy") or "",
        "scope_source": payload.get("scope_source") or "",
        "data_scope": payload.get("data_scope") or "",
        "symbols_count": len(symbols) or safe_int(payload.get("symbols_count"), 0),
        "symbols_preview": symbols[:8],
        "trade_count": safe_int(payload.get("trade_count") or metrics.get("number_of_trades"), 0),
        "metrics": {
            "total_return": finite_float(metrics.get("total_return")),
            "annual_return": finite_float(metrics.get("annual_return")),
            "max_drawdown": finite_float(metrics.get("max_drawdown")),
            "sharpe": finite_float(metrics.get("sharpe")),
            "win_rate": finite_float(metrics.get("win_rate")),
            "final_equity": finite_float(metrics.get("final_equity")),
        },
        "warning_count": len(warnings),
        "warnings_top": [str(item) for item in warnings[:5]],
        "generated_at": payload.get("generated_at") or "",
        "path": project_relative_path(path),
        "research_only": True,
        "paper_only": True,
        "no_broker": True,
        "no_real_orders": True,
        "not_investment_advice": True,
    }


def _compact_factor_health_context(payload: dict[str, Any], *, path: Path) -> dict[str, Any]:
    try:
        from quantdesk.research.factor_research_dashboard import summarize_factor_health
    except Exception:  # noqa: BLE001
        summarize_factor_health = None  # type: ignore[assignment]
    warnings = payload.get("warnings") if isinstance(payload.get("warnings"), list) else []
    if str(payload.get("data_source") or "") == "synthetic_test_data":
        warnings = list(warnings) + ["synthetic_factor_health_evidence_only"]
    summary = summarize_factor_health(payload) if summarize_factor_health else {}
    return {
        "available": bool(payload),
        "status": payload.get("status") or "",
        "data_source": payload.get("data_source") or "",
        "generated_at": payload.get("generated_at") or "",
        "summary": summary,
        "warning_count": len(warnings),
        "warnings_top": [str(item) for item in warnings[:5]],
        "path": project_relative_path(path),
        "used_for_scoring": False,
        "evidence_only": True,
        "research_only": True,
        "not_investment_advice": True,
    }


def _compact_strategy_lab_context(
    leaderboard_payload: dict[str, Any],
    *,
    leaderboard_path: Path,
    handoff_payload: dict[str, Any] | None = None,
    statistics_payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    handoff = handoff_payload if isinstance(handoff_payload, dict) else {}
    statistics = statistics_payload if isinstance(statistics_payload, dict) else {}
    items = leaderboard_payload.get("items") if isinstance(leaderboard_payload.get("items"), list) else []
    stat_items = statistics.get("items") if isinstance(statistics.get("items"), list) else []
    stats_by_family = {
        str(item.get("strategy_family") or ""): item
        for item in stat_items
        if isinstance(item, dict) and item.get("strategy_family")
    }
    top_items: list[dict[str, Any]] = []
    for item in items[:5]:
        if not isinstance(item, dict):
            continue
        strategy_id = str(item.get("strategy_id") or "")
        family_stats = stats_by_family.get(strategy_id) or {}
        top_items.append(
            {
                "strategy_id": strategy_id,
                "research_rank": safe_int(item.get("research_rank"), 0),
                "strategy_health": item.get("strategy_health") or "",
                "research_readiness": item.get("research_readiness") or "",
                "robustness_label": item.get("robustness_label") or "",
                "robustness_score": finite_float(item.get("robustness_score")),
                "mapping_coverage": finite_float(item.get("mapping_coverage")),
                "annualized_return": finite_float(item.get("annualized_return")),
                "sharpe": finite_float(item.get("Sharpe") or item.get("sharpe")),
                "max_drawdown": finite_float(item.get("max_drawdown")),
                "win_rate": finite_float(item.get("win_rate")),
                "data_risk_level": item.get("data_risk_level") or "",
                "candidate_count": safe_int(family_stats.get("candidate_count"), 0) if family_stats else 0,
                "signal_count": safe_int(family_stats.get("signal_count"), 0) if family_stats else 0,
                "limitation_note": item.get("limitation_note") or family_stats.get("limitation_note") or "",
            }
        )
    warnings: list[str] = []
    if leaderboard_payload.get("status") and str(leaderboard_payload.get("status")) != "completed":
        warnings.append(str(leaderboard_payload.get("status")))
    if handoff.get("remaining_gap_for_one_stop_research"):
        warnings.append(str(handoff.get("remaining_gap_for_one_stop_research")))
    if handoff and not handoff.get("research_only_boundary_kept", True):
        warnings.append("strategy_lab_boundary_not_confirmed")
    return {
        "available": bool(items),
        "status": leaderboard_payload.get("status") or handoff.get("status") or "",
        "handoff_status": handoff.get("status") or "",
        "generated_at": handoff.get("created_at") or leaderboard_payload.get("generated_at") or "",
        "top_strategies": top_items,
        "top_strategy_count": len(top_items),
        "leaderboard_path": project_relative_path(leaderboard_path),
        "handoff_path": project_relative_path(project_root / "results" / "strategy_lab" / "v420_real_use_strategy_lab_handoff_latest.json"),
        "statistics_path": project_relative_path(project_root / "results" / "strategy_lab" / "v421_strategy_statistics_latest.json"),
        "warning_count": len(warnings),
        "warnings_top": warnings[:5],
        "used_for_scoring": False,
        "evidence_only": True,
        "research_only": True,
        "paper_only": True,
        "no_broker": True,
        "no_real_orders": True,
        "not_investment_advice": True,
        "not_position_advice": True,
    }


def latest_research_evidence_context() -> dict[str, Any]:
    from quantdesk.research.evidence_identity import read_research_evidence
    evidence: dict[str, Any] = {}
    target = expected_settlement_date(to_shanghai_datetime())
    target_text = target.isoformat() if target else ""
    backtest_paths = [
        project_root / "results" / "backtest" / "backtest_latest.json",
        project_root / "results" / "backtest" / "latest.json",
    ]
    for path in backtest_paths:
        admission = read_research_evidence(path, target_date=target_text)
        payload = admission["payload"]
        if payload and payload.get("status") != "unreadable":
            evidence["backtest_summary"] = _compact_backtest_context(payload, path=path)
            evidence["backtest_summary"].update(formal_eligible=admission["formal_eligible"],
                evidence_admission={k: v for k, v in admission.items() if k != "payload"})
            if not admission["formal_eligible"]:
                evidence["backtest_summary"].update(available=False, status="unverified_research_evidence")
            break
    factor_path = project_root / "results" / "factor_health_latest.json"
    factor_admission = read_research_evidence(factor_path, target_date=target_text)
    factor_payload = factor_admission["payload"]
    if factor_payload and factor_payload.get("status") != "unreadable":
        evidence["factor_health"] = _compact_factor_health_context(factor_payload, path=factor_path)
        evidence["factor_health"].update(formal_eligible=factor_admission["formal_eligible"],
            evidence_admission={k: v for k, v in factor_admission.items() if k != "payload"})
        if not factor_admission["formal_eligible"]:
            evidence["factor_health"].update(available=False, status="unverified_research_evidence")
    strategy_lab_path = project_root / "results" / "strategy_lab" / "v420_strategy_leaderboard_latest.json"
    strategy_admission = read_research_evidence(strategy_lab_path, target_date=target_text)
    strategy_lab_payload = strategy_admission["payload"]
    if strategy_lab_payload and strategy_lab_payload.get("status") != "unreadable":
        handoff_path = project_root / "results" / "strategy_lab" / "v420_real_use_strategy_lab_handoff_latest.json"
        statistics_path = project_root / "results" / "strategy_lab" / "v421_strategy_statistics_latest.json"
        evidence["strategy_lab"] = _compact_strategy_lab_context(
            strategy_lab_payload,
            leaderboard_path=strategy_lab_path,
            handoff_payload=read_json_file(handoff_path),
            statistics_payload=read_json_file(statistics_path),
        )
        evidence["strategy_lab"].update(formal_eligible=strategy_admission["formal_eligible"],
            evidence_admission={k: v for k, v in strategy_admission.items() if k != "payload"})
        if not strategy_admission["formal_eligible"]:
            evidence["strategy_lab"].update(available=False, status="unverified_research_evidence")
    for kind, filename in (("combined_score_backtest", "combined_score_backtest_latest.json"),
                           ("walk_forward", "walk_forward_latest.json")):
        path = project_root / "results" / filename
        admission = read_research_evidence(path, target_date=target_text)
        payload = admission["payload"]
        evidence[kind] = {"available": bool(payload) and admission["formal_eligible"],
            "formal_eligible": admission["formal_eligible"], "status": admission["status"],
            "path": project_relative_path(path), "research_only": True,
            "diagnostic_metrics": payload.get("performance") or payload.get("walk_forward_summary") or {},
            "warnings_top": list(payload.get("warnings") or [])[:8],
            "evidence_admission": {k: v for k, v in admission.items() if k != "payload"}}
    evidence.setdefault(
        "backtest_summary",
        {
            "available": False,
            "status": "missing",
            "path": project_relative_path(backtest_paths[0]),
            "message": "latest backtest evidence is missing; run qd.py backtest after screen handoff.",
            "research_only": True,
            "paper_only": True,
            "no_broker": True,
            "no_real_orders": True,
        },
    )
    evidence.setdefault(
        "factor_health",
        {
            "available": False,
            "status": "missing",
            "path": project_relative_path(factor_path),
            "message": "factor health evidence is missing; run qd.py factor-eval when local data is ready.",
            "used_for_scoring": False,
            "evidence_only": True,
            "research_only": True,
        },
    )
    strategy_lab_missing_path = project_root / "results" / "strategy_lab" / "v420_strategy_leaderboard_latest.json"
    evidence.setdefault(
        "strategy_lab",
        {
            "available": False,
            "status": "missing",
            "leaderboard_path": project_relative_path(strategy_lab_missing_path),
            "message": "strategy lab leaderboard is missing; run the strategy lab workflow when local data is ready.",
            "used_for_scoring": False,
            "evidence_only": True,
            "research_only": True,
            "paper_only": True,
            "no_broker": True,
            "no_real_orders": True,
        },
    )
    return evidence


def _attach_latest_research_evidence_context(context: dict[str, Any]) -> None:
    context.update(latest_research_evidence_context())


def _attach_paper_simulation_context(context: dict[str, Any]) -> None:
    paper = paper_simulation_context_payload(limit=8)
    context["paper_simulation_watchlist_count"] = paper["watchlist_count"]
    context["paper_simulation_preview"] = paper["preview"]
    context["paper_simulation_summary"] = paper["paper_tracking_summary"]
    context["paper_simulation_boundary"] = "Research Only / Paper Account Only / No Broker Route / No real orders."
    try:
        scorecard = build_strategy_scorecard()
        context["strategy_scorecard_summary"] = {
            "today_focus_count": scorecard.get("today_focus_count"),
            "paper_pool_count": scorecard.get("paper_pool_count"),
            "review_needed_count": scorecard.get("review_needed_count"),
            "paper_expectation_fit_counts": scorecard.get("paper_expectation_fit_counts"),
            "previous_recommendation_performance": scorecard.get("previous_recommendation_performance"),
            "by_strategy": scorecard.get("by_strategy", [])[:5],
            "scorecard_message": scorecard.get("scorecard_message"),
        }
        # Reuse the market/sector/news background already computed inside the
        # scorecard so the LLM can ground daily-review answers in real market
        # context (req: read 板块/新闻/市场背景) without recomputing it.
        market_bg = (scorecard.get("market_context") or {}).get("background") or {}
        if market_bg:
            context["market_background"] = {
                "available": market_bg.get("available"),
                "sentiment": market_bg.get("sentiment"),
                "sector_top_gainers": market_bg.get("sector_top_gainers"),
                "sector_top_losers": market_bg.get("sector_top_losers"),
                "news": market_bg.get("news"),
                "hint": market_bg.get("hint"),
            }
    except Exception as exc:  # noqa: BLE001 - scorecard context is enrichment
        LOG.warning("agent_strategy_scorecard_context_failed: %s", exc)


def _attach_current_candidate_context(context: dict[str, Any]) -> None:
    """Attach the same candidate pool that the active 8000 UI renders."""
    rows = annotate_recommendation_priorities(candidate_rows(limit=18))
    if not rows:
        return

    today_rows = [row for row in rows if row.get("is_today_recommendation")]
    observation_rows = [row for row in rows if not row.get("is_today_recommendation")]
    strategy_counts: dict[str, int] = {}
    data_status_counts: dict[str, int] = {}
    for row in rows:
        strategy = str(row.get("strategy_lane") or row.get("strategy_bucket") or "未分类")
        data_status = str(row.get("data_status") or "数据状态缺失")
        strategy_counts[strategy] = strategy_counts.get(strategy, 0) + 1
        data_status_counts[data_status] = data_status_counts.get(data_status, 0) + 1

    context["candidate_count"] = len(rows)
    context["current_ui_candidate_count"] = len(rows)
    context["today_recommendation_count"] = len(today_rows)
    context["observation_candidate_count"] = len(observation_rows)
    context["candidate_strategy_counts"] = strategy_counts
    context["candidate_data_status_counts"] = data_status_counts
    context["candidates_preview"] = [_compact_agent_candidate(row) for row in rows[:12]]
    context["top_priority_candidates"] = [
        _compact_agent_candidate(row)
        for row in sorted(rows, key=lambda item: int(item.get("priority_rank") or 999))[:5]
    ]
    context.pop("no_candidates_reason", None)


def _build_agent_chat_context() -> dict:
    """Best-effort read-only snapshot of the latest local results so the
    hallucination guard has real evidence to check the model's replies against.
    Never raises; an empty context just means the model will say data is missing."""
    context: dict = {}
    try:
        from quantdesk.shared import read_json
    except Exception:
        return context

    # Read daily_workflow_latest.json for comprehensive status
    try:
        workflow = read_json(project_root / "results" / "daily_workflow_latest.json", {})
        if isinstance(workflow, dict):
            # Extract key date and status fields
            if "dates" in workflow and isinstance(workflow["dates"], dict):
                dates = workflow["dates"]
                context["target_date"] = dates.get("local_complete_daily_date")
                context["generated_at"] = workflow.get("generated_at")

            # Extract data freshness status
            if "data_freshness_gate" in workflow and isinstance(workflow["data_freshness_gate"], dict):
                dfg = workflow["data_freshness_gate"]
                context["data_freshness"] = dfg.get("data_freshness_status")
                context["is_stale"] = dfg.get("is_stale", False)
                context["latest_local_date"] = dfg.get("local_daily_latest_date") or dfg.get("latest_complete_daily_date_for_scope")

            # Extract workflow status
            context["workflow_ok"] = workflow.get("ok", False)
            context["workflow_status"] = workflow.get("status", "unknown")

            # Extract candidates from auto_strategy
            auto_strat = workflow.get("auto_strategy")
            if isinstance(auto_strat, dict):
                long_cands = auto_strat.get("long_candidates", [])
                board_cands = auto_strat.get("board_candidates", [])
                all_cands = long_cands + board_cands

                if all_cands:
                    context["candidate_count"] = len(all_cands)
                    preview = []
                    for card in all_cands[:10]:
                        if not isinstance(card, dict):
                            continue
                        row = {
                            key: card.get(key)
                            for key in ("symbol", "name", "score", "trust_score", "trust_level", "action_state", "strategy")
                            if card.get(key) not in (None, "")
                        }
                        if row:
                            preview.append(row)
                    if preview:
                        context["candidates_preview"] = preview
                else:
                    context["candidate_count"] = 0
                    context["no_candidates_reason"] = "当前无符合策略条件的候选股票"

            # Filter warnings: only keep critical data issues
            raw_warnings = workflow.get("warnings")
            if isinstance(raw_warnings, list):
                critical = [
                    w for w in raw_warnings
                    if any(kw in str(w).lower() for kw in ["missing", "缺失", "failed", "失败"])
                    and not any(skip in str(w) for skip in [
                        "分钟线", "minute_data", "paper_account", "sector_map", "similar_day",
                        "feature missing rate is high", "quick guardrails", "score_panel_unavailable",
                        "historical_st_periods", "index_csv_missing", "unavailable", "stale"
                    ])
                ]
                if critical:
                    context["warnings"] = critical
    except Exception:
        pass

    # The active daily page renders candidates from candidate_rows(), not only
    # from daily_workflow_latest.json. Keep Agent grounding aligned with the UI.
    try:
        _attach_current_candidate_context(context)
    except Exception as exc:  # noqa: BLE001
        warnings = context.setdefault("warnings", [])
        if isinstance(warnings, list):
            warnings.append(f"agent_current_candidate_context_failed: {exc}")
    try:
        _attach_paper_simulation_context(context)
    except Exception as exc:  # noqa: BLE001
        warnings = context.setdefault("warnings", [])
        if isinstance(warnings, list):
            warnings.append(f"agent_paper_simulation_context_failed: {exc}")
    try:
        _attach_latest_research_evidence_context(context)
    except Exception as exc:  # noqa: BLE001
        warnings = context.setdefault("warnings", [])
        if isinstance(warnings, list):
            warnings.append(f"agent_research_evidence_context_failed: {exc}")

    # Fallback: try combined_score_latest.json
    if not context.get("candidates_preview"):
        try:
            combined = read_json(project_root / "results" / "combined_score_latest.json", {})
            if isinstance(combined, dict):
                rows = combined.get("rows") or combined.get("cards") or []
                if rows:
                    context["candidate_count"] = len(rows)
                    preview = []
                    for card in rows[:10]:
                        if not isinstance(card, dict):
                            continue
                        row = {
                            key: card.get(key)
                            for key in ("symbol", "name", "score", "trust_score", "trust_level")
                            if card.get(key) not in (None, "")
                        }
                        if row:
                            preview.append(row)
                    if preview:
                        context["candidates_preview"] = preview
        except Exception:
            pass

    return context


def startup_refresh_state_payload() -> dict[str, Any]:
    with STARTUP_REFRESH_LOCK:
        return dict(STARTUP_REFRESH_STATE)


def _set_startup_refresh_state(**updates: Any) -> None:
    with STARTUP_REFRESH_LOCK:
        STARTUP_REFRESH_STATE.update(updates)


def _write_startup_status_payload(payload: dict[str, Any]) -> None:
    STARTUP_UPDATE_JSON.parent.mkdir(parents=True, exist_ok=True)
    STARTUP_UPDATE_JSON.write_text(json.dumps(clean_payload(payload), ensure_ascii=False, indent=2), encoding="utf-8")


def _run_startup_refresh_worker(reason: str, request_id: str | None = None) -> None:
    """Run an explicitly requested close update using the single daily writer."""
    request_id = request_id or datetime.now().isoformat(timespec="seconds")
    _set_startup_refresh_state(
        running=True, status="running", reason=reason, request_id=request_id,
        started_at=request_id, message_for_ui="正在运行全 A 收盘更新。",
    )
    if not DATA_WRITE_LOCK.acquire(blocking=False):
        _set_startup_refresh_state(
            running=False, status="busy", request_id=request_id,
            message_for_ui="另一个数据更新正在进行。",
        )
        return
    try:
        payload = run_startup_data_update(project_root, enable_network=True)
        _write_startup_status_payload(payload)
        _set_startup_refresh_state(
            running=False, status=str(payload.get("status") or "failed"),
            request_id=request_id, message_for_ui=str(payload.get("message") or ""),
        )
    except Exception as exc:
        payload = {
            "task_id": "startup_data_update", "status": "failed", "ok": False,
            "daily_writer": "data_close_pipeline", "scope": "full_a",
            "request_id": request_id, "updated_at": datetime.now().isoformat(timespec="seconds"),
            "message": f"收盘更新失败：{type(exc).__name__}: {exc}",
            "research_only": True, "paper_only": True, "no_broker": True,
        }
        _write_startup_status_payload(payload)
        _set_startup_refresh_state(running=False, status="failed", request_id=request_id,
                                  message_for_ui=payload["message"])
    finally:
        DATA_WRITE_LOCK.release()


def load_stock_workbench_daily(symbol: str) -> dict[str, Any]:
    """Read original bytes; the new consumer classifies before transforming."""
    path = DAILY_DIR / f"{symbol}.csv"
    if not path.is_file():
        return {"frame": pd.DataFrame(), "sha256": ""}
    data = path.read_bytes()
    return {"frame": pd.read_csv(BytesIO(data), dtype={"adjust": str}),
            "sha256": hashlib.sha256(data).hexdigest()}


def get_stock_detail(symbol: str):
    clean_symbol = normalize_symbol(symbol)
    refresh_daily = request_truthy(request.args.get("refresh_daily"))
    explicit_network = request_truthy(request.args.get("network"))
    daily_refresh = ensure_single_stock_daily_with_auth(
        clean_symbol,
        refresh_daily=refresh_daily,
        network_allowed=bool(realtime_network_enabled() or explicit_network),
    )
    stock_df = load_stock_data(clean_symbol)
    if stock_df.empty or len(stock_df) < 20:
        return (
            jsonify(
                clean_payload(
                    {
                        "ok": False,
                        "error": "本地数据不足",
                        "message": f"股票 {clean_symbol} 的历史日线不足20条，无法生成详情。{UPDATE_HINT}",
                    }
                )
            ),
            404,
        )

    ledger = load_candidate_ledger()
    ledger_row = None
    if not ledger.empty and "ts_code" in ledger.columns:
        hits = ledger[ledger["ts_code"].astype(str).map(normalize_symbol) == clean_symbol]
        lane = str(request.args.get("lane") or "").strip()
        requested_strategy = DETAIL_LANE_TO_STRATEGY.get(lane, "")
        if requested_strategy and "strategy_type" in hits.columns:
            filtered = hits[hits["strategy_type"].astype(str) == requested_strategy]
            if not filtered.empty:
                hits = filtered
        requested_entry_date = str(request.args.get("entry_date") or "").strip()[:10]
        if requested_entry_date and "entry_date" in hits.columns:
            filtered = hits[hits["entry_date"].dt.strftime("%Y-%m-%d") == requested_entry_date]
            if not filtered.empty:
                hits = filtered
        if not hits.empty:
            ledger_row = hits.iloc[0]

    latest = stock_df.iloc[-1]
    pct_chg = latest_pct_change(stock_df)
    indicators = latest_indicators(stock_df)
    name = candidate_name(clean_symbol, ledger_row)
    risk_level = stock_explainer.risk_level_from_indicators(indicators)
    select_date = ""
    if ledger_row is not None and isinstance(ledger_row.get("entry_date"), pd.Timestamp):
        select_date = ledger_row.get("entry_date").strftime("%Y-%m-%d")
    select_reason = selection_reason_from_row(ledger_row, indicators)
    latest_date = latest.get("date").strftime("%Y-%m-%d") if isinstance(latest.get("date"), pd.Timestamp) else str(latest.get("date"))
    chart_select_date = select_date or latest_date
    strategy_type = row_strategy_type(ledger_row)
    tracking = build_strategy_tracking(stock_df, ledger_row, indicators)
    quality = stock_data_quality(stock_df, clean_symbol)
    strategy_bucket, strategy_lane, group_key = classify_candidate_lane(strategy_type, indicators, tracking, quality)
    review_state = apply_candidate_review_state(
        {
            "symbol": clean_symbol,
            "select_date": chart_select_date,
            "detail_lane": detail_lane(strategy_type),
            "group_key": group_key,
            "strategy_lane": strategy_lane,
            "tracking": tracking,
            "data_quality": quality,
        }
    )
    payload = {
        "ok": True,
        "basic_info": {
            "symbol": clean_symbol,
            "name": name,
            "price": chart_number(latest.get("close")),
            "pct_chg": chart_number(pct_chg, 0.0) if pct_chg is not None else "数据缺失",
            "date": latest_date,
            "risk_level": risk_level,
        },
        "strategy_review": {
            "strategy_name": strategy_label(strategy_type),
            "strategy_bucket": strategy_bucket,
            "strategy_lane": review_state.get("strategy_lane", strategy_lane),
            "group_key": review_state.get("group_key", group_key),
            "detail_lane": detail_lane(strategy_type),
            "select_date": chart_select_date,
            "select_reason": select_reason,
            "tracking": tracking,
            "data_quality": quality,
            "pool_action": review_state.get("pool_action", default_pool_action()),
        },
        "explanation": stock_explainer.generate_detailed_explanation(clean_symbol, indicators, len(stock_df)),
        "indicators_exp": stock_explainer.generate_indicator_explanation(indicators),
        "risk_warning": stock_explainer.generate_risk_warning(indicators, len(stock_df)),
        "chart_data": {
            "price_basis": stock_df.attrs.get("price_basis", "mixed_or_unknown"),
            "price_basis_verified": bool(stock_df.attrs.get("price_basis_eligible")),
            "price_basis_source": "original_daily_frame_before_filtering",
            "kline_data": chart_rows(stock_df),
            "select_date": chart_select_date,
            "select_reason": select_reason,
            "markers": chart_markers(indicators, chart_select_date, select_reason, latest_date, tracking),
            **build_structured_chart_series(stock_df),
        },
        "research_reference": (_research_reference := build_research_reference(stock_df)),
        "research_brief": build_stock_research_brief(_research_reference, name=candidate_name(clean_symbol), strategy_lane=detail_lane(strategy_type)),
        "news_summary": build_news_summary(clean_symbol),
        "industry_comparison": build_industry_comparison(clean_symbol, pct_chg),
        "tracking_vs_benchmark": build_tracking_vs_benchmark(stock_df, chart_select_date),
        "paper_simulation": paper_simulation_status_for_candidate(
            clean_symbol,
            entry_date=chart_select_date,
            lane=detail_lane(strategy_type),
        ),
        "daily_refresh": daily_refresh,
        "research_only": "仅用于股票研究学习；不接券商、不下真实订单。",
    }
    payload["strategy_review"].update(
        build_reference_confidence(
            {
                "symbol": clean_symbol,
                "latest_close": latest.get("close"),
                "daily_amount": latest.get("amount"),
                "strategy_name": strategy_label(strategy_type),
                "strategy_lane": review_state.get("strategy_lane", strategy_lane),
                "strategy_bucket": strategy_bucket,
                "detail_lane": detail_lane(strategy_type),
                "intraday_required": detail_lane(strategy_type) == "board",
                "minute_data_available": (
                    intraday_file_exists(clean_symbol, chart_select_date, project_root=project_root)
                    if chart_select_date and detail_lane(strategy_type) == "board"
                    else False
                ),
                "data_quality": quality,
            },
            target_date=chart_select_date,
            project_root=project_root,
        )
    )
    return jsonify(clean_payload(payload))


def visible_candidate_theme_status(path: Path | None = None, *, limit: int = 18) -> dict[str, Any]:
    source = path or STOCK_THEME_MAP_CSV
    rows = candidate_rows(limit=limit)
    mapping = load_theme_map(source)
    enriched = attach_theme_map_to_candidates([dict(row) for row in rows], mapping)
    coverage = summarize_candidate_theme_coverage(enriched)
    total = len(rows)
    mapped = int(coverage.get("mapped_count") or 0)
    missing = [str(symbol) for symbol in coverage.get("missing_symbols", []) if str(symbol)]
    if not total:
        reason = "页面候选池当前没有可校验股票。"
    elif mapped == total:
        reason = "页面候选池当前展示股票均已匹配 stock_theme_map.csv。"
    elif not source.exists():
        reason = "stock_theme_map.csv 不存在，请先生成导入草稿并导入正式映射。"
    else:
        reason = "页面候选池仍有股票未进入正式题材映射；可生成导入草稿后补齐，未知项会保留中文待补提示。"
    return {
        "ok": True,
        "candidate_count": total,
        "mapped_visible_candidates": mapped,
        "unknown_visible_candidates": max(0, total - mapped),
        "missing_symbols": missing,
        "path": str(source),
        "reason": reason,
        "message": f"页面候选池板块映射：已映射 {mapped} / {total}。",
    }


def theme_map_status_payload() -> dict[str, Any]:
    status = theme_map_status(STOCK_THEME_MAP_CSV)
    status["current_candidate_theme_status"] = validate_theme_map_for_current_candidates(STOCK_THEME_MAP_CSV)
    status["visible_candidate_theme_status"] = visible_candidate_theme_status(STOCK_THEME_MAP_CSV)
    status["boundary"] = "Research Only；题材映射只用于研究归因和分组展示，不接券商、不下单、不构成投资建议。"
    status["source_note"] = (
        "manual_import 表示从本地 stock_theme_map_import.csv 导入；导入草稿可能来自本地种子、已有正式映射或人工维护，"
        "不等同于实时联网下载。eastmoney_stock_profile、akshare/per_symbol_info 表示显式联网数据源。"
    )
    status["message_for_ui"] = (
        f"stock_theme_map.csv 已映射 {status.get('mapped_count', 0)} / {status.get('total_rows', 0)}；"
        f"待补 {status.get('pending_count', 0)}。"
    )
    return status


# ============================================================
# Quant capability endpoints — data ops, performance/P&L, backtest.
# Thin wrappers over existing quantdesk engines (no new quant logic).
# Heavy runs are POST (compute + write artifact); GET reads the latest.
# ============================================================

def build_news_summary(symbol: str) -> dict[str, Any]:
    """Get top news headlines for a stock as research clues."""
    try:
        result = fetch_news(symbol, limit=5, network=False)
        items = result.get("data", [])
        if not items:
            return {"available": False, "items": [], "hint": "暂无新闻缓存，打开详情时自动拉取。"}
        headlines = [
            {"title": item.get("title", "")[:80], "published": item.get("published", ""), "source": item.get("source", "")}
            for item in items[:5]
        ]
        return {"available": True, "items": headlines, "count": len(headlines)}
    except Exception as exc:  # noqa: BLE001 - news panel should show a degraded hint
        LOG.warning("news_summary_build_failed: symbol=%s error=%s", symbol, exc)
        return {"available": False, "items": [], "hint": "新闻读取失败。"}


def build_industry_comparison(symbol: str, stock_pct_chg: float | None) -> dict[str, Any]:
    """Compare a stock's performance to its industry sector."""
    if not INDUSTRY_BOARD_CACHE.exists():
        return {"available": False, "hint": "行业板块缓存不存在，请刷新板块数据。"}
    try:
        board_df = read_csv_frame(INDUSTRY_BOARD_CACHE)
    except Exception as exc:  # noqa: BLE001 - industry comparison should show a degraded hint
        LOG.warning("industry_board_cache_read_failed: path=%s error=%s", INDUSTRY_BOARD_CACHE, exc)
        return {"available": False, "hint": "行业板块缓存读取失败。"}

    # Find which sector this stock belongs to via theme map
    theme_df = read_theme_map()
    sector_name = ""
    if not theme_df.empty:
        sym_col = "symbol" if "symbol" in theme_df.columns else "ts_code" if "ts_code" in theme_df.columns else ""
        if sym_col:
            match = theme_df[theme_df[sym_col].astype(str).map(normalize_symbol) == normalize_symbol(symbol)]
            if not match.empty:
                row = match.iloc[0]
                sector_name = str(row.get("primary_theme") or row.get("sector") or row.get("industry") or "").strip()

    # Board classification fallback
    if not sector_name or sector_name in {"待分类", "未知板块", "未知行业", "未知题材"}:
        sector_name = _board_name(symbol)

    # Find sector average change from board cache
    name_col = find_column(board_df, "name") or find_column(board_df, "板块名称")
    change_col = find_column(board_df, "pct_chg") or find_column(board_df, "涨跌幅")
    if not name_col or not change_col:
        return {"available": False, "hint": "板块数据缺少必要列。"}

    board_df[change_col] = pd.to_numeric(board_df[change_col], errors="coerce")
    all_changes = board_df[change_col].dropna().sort_values(ascending=False)
    market_avg = round(float(all_changes.mean()), 2) if len(all_changes) > 0 else 0

    # Find closest matching sector
    sector_change = None
    sector_rank = None
    for idx, (_, brow) in enumerate(board_df.iterrows()):
        bname = str(brow.get(name_col, "")).strip()
        bchange = finite_float(brow.get(change_col))
        if bname and bchange is not None:
            if sector_name and sector_name in bname:
                sector_change = round(bchange, 2)
                sector_rank = idx + 1
                break

    stock_vs_sector = None
    if stock_pct_chg is not None and sector_change is not None:
        stock_vs_sector = round(stock_pct_chg - sector_change, 2)

    if sector_change is None:
        return {
            "available": False,
            "sector_name": sector_name,
            "hint": f"所属行业线索为“{sector_name}”，但行业板块缓存中未匹配到对应涨跌幅；请补齐题材映射或刷新板块缓存。",
        }

    return {
        "available": True,
        "sector_name": sector_name,
        "sector_change": sector_change,
        "sector_rank": sector_rank,
        "total_sectors": len(board_df),
        "market_avg_change": market_avg,
        "stock_vs_sector": stock_vs_sector,
        "hint": f"个股涨跌幅 vs 所属行业（{sector_name}）平均涨跌幅的差值。正值表示跑赢行业。",
    }


def build_tracking_vs_benchmark(stock_df: pd.DataFrame, entry_date_str: str) -> dict[str, Any]:
    """Compare stock performance since entry date vs benchmark (上证指数)."""
    if stock_df.empty or not entry_date_str or entry_date_str == "数据缺失":
        return {"available": False, "hint": "缺少入选日期或股票数据。"}

    # Parse entry date
    try:
        entry_dt = pd.to_datetime(entry_date_str)
    except Exception as exc:  # noqa: BLE001 - invalid entry date should degrade comparison only
        LOG.warning("tracking_benchmark_entry_date_parse_failed: entry_date=%r error=%s", entry_date_str, exc)
        return {"available": False, "hint": f"入选日期 '{entry_date_str}' 格式无效。"}

    # Filter stock data from entry date
    if "date" not in stock_df.columns:
        return {"available": False, "hint": "股票数据缺少日期列。"}
    if "close" not in stock_df.columns:
        return {"available": False, "hint": "股票数据缺少收盘价列。"}

    stock_df = stock_df.copy()
    stock_df["date"] = pd.to_datetime(stock_df["date"], errors="coerce")
    stock_df["close"] = pd.to_numeric(stock_df["close"], errors="coerce")
    stock_since = stock_df[stock_df["date"] >= entry_dt].dropna(subset=["date", "close"])
    if len(stock_since) < 2:
        return {"available": False, "hint": "入选后日线数据不足。"}

    stock_entry_close = finite_float(stock_since.iloc[0].get("close"))
    stock_latest_close = finite_float(stock_since.iloc[-1].get("close"))
    if stock_entry_close in (None, 0) or stock_latest_close is None:
        return {"available": False, "hint": "入选以来股票收盘价无效，无法计算复盘收益。"}
    stock_return = round((stock_latest_close / stock_entry_close - 1) * 100, 2)

    # Load benchmark (sh000001) and compare
    bench_path = BENCHMARK_DIR / "sh000001.csv"
    bench_return = None
    benchmark_hint = ""
    if bench_path.exists():
        try:
            bench_df = read_csv_frame(bench_path)
            if "date" in bench_df.columns and "close" in bench_df.columns:
                bench_df["date"] = pd.to_datetime(bench_df["date"], errors="coerce")
                bench_df["close"] = pd.to_numeric(bench_df["close"], errors="coerce")
                bench_since = bench_df[bench_df["date"] >= entry_dt].dropna(subset=["date", "close"])
                if len(bench_since) >= 2:
                    bench_entry = finite_float(bench_since.iloc[0].get("close"))
                    bench_latest = finite_float(bench_since.iloc[-1].get("close"))
                    if bench_entry not in (None, 0) and bench_latest is not None:
                        bench_return = round((bench_latest / bench_entry - 1) * 100, 2)
                    else:
                        benchmark_hint = "上证指数收盘价无效。"
                else:
                    benchmark_hint = "上证指数入选后数据不足。"
            else:
                benchmark_hint = "上证指数文件缺少 date/close 列。"
        except Exception as exc:
            benchmark_hint = f"上证指数文件读取失败：{exc}"
    else:
        benchmark_hint = "缺少 data/benchmark/sh000001.csv，无法计算同期上证指数。"

    excess = round(stock_return - bench_return, 2) if bench_return is not None else None
    days = len(stock_since)

    return {
        "available": True,
        "entry_date": entry_date_str,
        "trading_days": days,
        "stock_return_pct": stock_return,
        "benchmark_return_pct": bench_return,
        "excess_return_pct": excess,
        "benchmark_hint": benchmark_hint,
        "verdict": (
            f"入选以来 {days} 个交易日，个股 {'+' if stock_return > 0 else ''}{stock_return}%"
            + (f"，上证 {'+' if bench_return > 0 else ''}{bench_return}%，超额 {'+' if excess > 0 else ''}{excess}%" if bench_return is not None else "")
            + (f"；{benchmark_hint}" if benchmark_hint else "")
        ),
    }


def build_research_scenarios(
    *,
    latest_close: float,
    band_1d_pct: float,
    band_5d_pct: float,
    high_60: float,
    low_60: float,
    ret_5d: float,
    ret_20d: float,
    daily_vol: float,
    ma5: float | None = None,
    ma20: float | None = None,
    ma60: float | None = None,
    recent_volume_ratio: float | None = None,
) -> dict[str, Any]:
    """Bounded statistical scenario ranges for study, not a trade instruction."""
    one_day_abs = latest_close * band_1d_pct / 100
    five_day_abs = latest_close * band_5d_pct / 100
    one_day_low = max(0.01, latest_close - one_day_abs)
    one_day_high = latest_close + one_day_abs
    five_day_low = max(0.01, latest_close - five_day_abs)
    five_day_high = latest_close + five_day_abs
    breakout_ref = round(min(high_60, five_day_high), 2)
    pullback_ref = round(max(low_60, five_day_low), 2)

    ma5_text = f"MA5 {round(ma5, 2)}" if ma5 is not None else "MA5 数据缺失"
    ma20_text = f"MA20 {round(ma20, 2)}" if ma20 is not None else "MA20 数据缺失"
    ma60_text = f"MA60 {round(ma60, 2)}" if ma60 is not None else "MA60 数据缺失"
    volume_text = (
        f"近5日量能约为20日均量 {round(recent_volume_ratio, 2)} 倍"
        if recent_volume_ratio is not None
        else "量能对比数据缺失"
    )

    if ret_20d > 3 and ret_5d >= -1:
        bias = "偏强进攻观察"
        thesis = "20日路径仍偏上，短线没有明显破坏；重点验证量能能否把强势延续到压力区。"
        main_case = "强势后的整理不必急着定性，核心看回踩时是否守住短期均线，量能是否从放量转为健康缩量。"
        attack_weight = "主观察"
        neutral_weight = "次观察"
    elif ret_20d < -3 and ret_5d <= 1:
        bias = "偏弱修复观察"
        thesis = "20日路径仍偏弱，短线反弹需要先证明不是技术性抽动；关键是能否重新站回中期均线。"
        main_case = "弱势结构下，先看是否完成止跌和横盘换手，再看均线能否从压制转为支撑。"
        attack_weight = "次观察"
        neutral_weight = "主观察"
    else:
        bias = "震荡突破观察"
        thesis = "近20日与近5日信号并不单边，当前更像区间内选择方向；要看量价是否给出清晰突破或失效。"
        main_case = "震荡结构里，最有价值的是观察区间上沿放量突破，或区间下沿跌破后的风险复盘。"
        attack_weight = "重点观察"
        neutral_weight = "主观察"

    return {
        "available": True,
        "bias": bias,
        "thesis": thesis,
        "structure_note": f"{ma5_text}；{ma20_text}；{ma60_text}；{volume_text}。",
        "one_day_range": {
            "low": round(one_day_low, 2),
            "high": round(one_day_high, 2),
            "pct": band_1d_pct,
            "label": "1日历史波动区间",
        },
        "five_day_range": {
            "low": round(five_day_low, 2),
            "high": round(five_day_high, 2),
            "pct": band_5d_pct,
            "label": "5日历史波动区间",
        },
        "scenarios": [
            {
                "name": "进攻情景",
                "tone": "strong",
                "weight": attack_weight,
                "range": f"{round(latest_close, 2)} - {breakout_ref}",
                "trigger": f"收盘站稳短期均线，且 {volume_text} 不弱于前一轮推进。",
                "watch": "若价格沿5日线或20日线向上推进，优先观察是否靠近60日区间上沿；若上冲无量，要把它当作强度不足来复盘。",
            },
            {
                "name": "主线情景",
                "tone": "neutral",
                "weight": neutral_weight,
                "range": f"{round(five_day_low, 2)} - {round(five_day_high, 2)}",
                "trigger": "价格仍在5日统计波动带内，未出现放量突破或连续破位。",
                "watch": main_case,
            },
            {
                "name": "失效情景",
                "tone": "risk",
                "weight": "风险观察",
                "range": f"{pullback_ref} - {round(latest_close, 2)}",
                "trigger": "跌破近期波动区间下沿，或下跌时量能明显放大。",
                "watch": "这种路径说明原观察假设被削弱，应重点复盘题材、行业、指数环境和资金承接是否同步恶化。",
            },
        ],
        "confidence_note": (
            f"基于近20日历史波动率 {round(daily_vol * 100, 2)}% 与60日区间生成的条件化情景推演；"
            "先看触发条件，再看路径是否被证伪，不是价格预测。"
        ),
        "disclaimer": "研究情景不是确定预测，不是交易动作或资金配置建议。",
    }


def build_research_reference(stock_df: pd.DataFrame) -> dict[str, Any]:
    """Compute research-reference metrics from daily data.

    These are statistical observations and scenario ranges, NOT investment advice.
    """
    if stock_df.empty or len(stock_df) < 60:
        return {"available": False, "message": "历史日线不足60条，无法生成研究参考。请先更新数据。"}

    close = pd.to_numeric(stock_df["close"], errors="coerce").dropna()
    if len(close) < 60:
        return {"available": False, "message": "有效收盘价不足60条。"}

    latest_close = float(close.iloc[-1])
    returns = close.pct_change().dropna()

    # 1. Volatility scale. Keep this as percentage-only context to avoid target-price output.
    daily_vol = float(returns.tail(20).std())
    vol_label = "低波动" if daily_vol < 0.015 else "中波动" if daily_vol < 0.03 else "高波动"
    band_1d_pct = round(daily_vol * 100, 2)
    band_5d_pct = round(daily_vol * (5 ** 0.5) * 100, 2)

    # 2. Support / resistance from recent 60-day range
    high_60 = float(close.tail(60).max())
    low_60 = float(close.tail(60).min())
    pct_from_high = round((latest_close - high_60) / high_60 * 100, 1)
    pct_from_low = round((latest_close - low_60) / low_60 * 100, 1)
    position_label = (
        "接近60日高点" if pct_from_high > -3 else
        "接近60日低点" if pct_from_low < 5 else
        "处于60日区间中部"
    )

    # 3. Simple MA-cross backtest in-sample
    ma5 = close.rolling(5).mean()
    ma20 = close.rolling(20).mean()
    signal = (ma5 > ma20).astype(int).diff().fillna(0)
    trades: list[dict[str, Any]] = []
    in_position = False
    entry_price = 0.0
    wins = 0
    losses = 0
    total_return = 0.0

    for i in range(20, len(close)):
        sig = int(signal.iloc[i])
        price = float(close.iloc[i])
        if sig == 1 and not in_position:
            entry_price = price
            in_position = True
        elif sig == -1 and in_position:
            ret = (price - entry_price) / entry_price
            total_return += ret
            if ret > 0:
                wins += 1
            else:
                losses += 1
            in_position = False

    total_trades = wins + losses
    win_rate = round(wins / total_trades * 100, 1) if total_trades > 0 else 0
    avg_return = round(total_return / total_trades * 100, 2) if total_trades > 0 else 0
    trade_label = (
        f"MA5/MA20 交叉策略在历史{total_trades}笔模拟交易中盈利占比{win_rate}%"
        if total_trades >= 5 else
        "MA5/MA20 交叉信号不足5次，盈利占比参考有限"
    )

    # 4. Recent trend direction (last 5 / 20 days)
    ret_5d = round((close.iloc[-1] / close.iloc[-6] - 1) * 100, 2) if len(close) >= 6 else 0
    ret_20d = round((close.iloc[-1] / close.iloc[-21] - 1) * 100, 2) if len(close) >= 21 else 0
    trend_5d = "上行" if ret_5d > 1 else "下行" if ret_5d < -1 else "震荡"
    trend_20d = "上行" if ret_20d > 3 else "下行" if ret_20d < -3 else "震荡"
    ma5_latest = finite_float(ma5.iloc[-1]) if len(ma5.dropna()) else None
    ma20_latest = finite_float(ma20.iloc[-1]) if len(ma20.dropna()) else None
    ma60 = close.rolling(60).mean()
    ma60_latest = finite_float(ma60.iloc[-1]) if len(ma60.dropna()) else None
    recent_volume_ratio: float | None = None
    if "volume" in stock_df.columns:
        volume = pd.to_numeric(stock_df["volume"], errors="coerce").dropna()
        if len(volume) >= 20:
            avg_5 = finite_float(volume.tail(5).mean())
            avg_20 = finite_float(volume.tail(20).mean())
            if avg_5 is not None and avg_20 not in (None, 0):
                recent_volume_ratio = round(avg_5 / avg_20, 2)
    scenarios = build_research_scenarios(
        latest_close=latest_close,
        band_1d_pct=band_1d_pct,
        band_5d_pct=band_5d_pct,
        high_60=high_60,
        low_60=low_60,
        ret_5d=ret_5d,
        ret_20d=ret_20d,
        daily_vol=daily_vol,
        ma5=ma5_latest,
        ma20=ma20_latest,
        ma60=ma60_latest,
        recent_volume_ratio=recent_volume_ratio,
    )

    return {
        "available": True,
        "disclaimer": "以下为基于历史波动、均线结构和近60日区间的研究推演；用于学习复盘，不构成投资建议。",
        "price_band": {
            "latest": latest_close,
            "daily_volatility_pct": round(daily_vol * 100, 2),
            "volatility_label": vol_label,
            "reference_1d_pct": band_1d_pct,
            "reference_5d_pct": band_5d_pct,
            "reference_1d": {
                "low": scenarios["one_day_range"]["low"],
                "high": scenarios["one_day_range"]["high"],
                "label": "1日历史波动参考区间",
            },
            "reference_5d": {
                "low": scenarios["five_day_range"]["low"],
                "high": scenarios["five_day_range"]["high"],
                "label": "5日历史波动参考区间",
            },
            "note": f"基于20日波动率（{round(daily_vol*100,2)}%）推算的历史波动区间，不是确定预测。",
        },
        "support_resistance": {
            "high_60d": round(high_60, 2),
            "low_60d": round(low_60, 2),
            "pct_from_high": pct_from_high,
            "pct_from_low": pct_from_low,
            "position": position_label,
        },
        "simple_backtest": {
            "total_trades": total_trades,
            "wins": wins,
            "losses": losses,
            "win_rate_pct": win_rate,
            "avg_return_pct": avg_return,
            "label": trade_label,
            "note": "样本内理想毛收益：收盘生成信号并按同一收盘成交，未扣费用与滑点，未模拟涨跌停、停牌、T+1或资金约束；不能证明当前参考价有效。",
            "rule_id": "legacy-ma5-ma20-same-close-v1",
            "validation_status": "in_sample_idealized_unvalidated",
            "signal_timing": "same_close",
            "fill_timing": "same_close",
            "costs_included": False,
            "capital_constrained": False,
        },
        "recent_trend": {
            "return_5d_pct": ret_5d,
            "return_20d_pct": ret_20d,
            "trend_5d": trend_5d,
            "trend_20d": trend_20d,
        },
        "scenario_projection": scenarios,
    }


def build_stock_research_brief(reference: dict[str, Any], *, name: str = "", strategy_lane: str = "") -> dict[str, Any]:
    """Beginner-friendly, DIRECT research brief synthesized from research_reference.

    Plain and opinionated (a "看盘搭子" view a beginner can act on), but framed as
    RESEARCH OBSERVATION conditions: current state, a clear research lean, what to
    keep watching, what invalidates the idea, and the next research step. It is
    NOT a buy/sell order, price target, or position advice — that boundary holds.
    """
    if not isinstance(reference, dict) or not reference.get("available"):
        return {"available": False, "message": "历史日线不足，暂不能生成研究简报；请先更新数据后再看。"}
    sr = reference.get("support_resistance") if isinstance(reference.get("support_resistance"), dict) else {}
    pb = reference.get("price_band") if isinstance(reference.get("price_band"), dict) else {}
    trend = reference.get("recent_trend") if isinstance(reference.get("recent_trend"), dict) else {}
    scen = reference.get("scenario_projection") if isinstance(reference.get("scenario_projection"), dict) else {}
    scenarios = scen.get("scenarios") if isinstance(scen.get("scenarios"), list) else []
    by_tone = {str(s.get("tone")): s for s in scenarios if isinstance(s, dict)}
    attack = by_tone.get("strong") or {}
    risk = by_tone.get("risk") or {}

    latest = finite_float(pb.get("latest"))
    high_60 = finite_float(sr.get("high_60d"))
    low_60 = finite_float(sr.get("low_60d"))
    position = str(sr.get("position") or "位置数据缺失")
    vol_label = str(pb.get("volatility_label") or "波动数据缺失")
    ret_5d = finite_float(trend.get("return_5d_pct"))
    ret_20d = finite_float(trend.get("return_20d_pct"))
    bias = str(scen.get("bias") or "方向待观察")
    thesis = str(scen.get("thesis") or "")
    structure = str(scen.get("structure_note") or "")

    moves = []
    if ret_5d is not None:
        moves.append(f"近5日 {ret_5d:+.1f}%")
    if ret_20d is not None:
        moves.append(f"近20日 {ret_20d:+.1f}%")
    move_text = "、".join(moves) or "近期涨跌数据缺失"
    price_text = f"现价 {latest:.2f}" if latest is not None else "现价数据缺失"
    current_state = f"{price_text}，{position}，{move_text}，{vol_label}。{structure}".strip()

    lean = (f"研究倾向：{bias}。{thesis}".strip()).rstrip("。") + "。"
    continue_if = str(
        attack.get("trigger")
        or "若放量站稳短期均线、且不跌破前低，则视为强势延续，值得继续观察 / 纸面跟踪。"
    )
    invalidate_if = str(
        risk.get("trigger")
        or "若跌破近期波动区间下沿，或下跌时放量，则原观察假设被削弱，转降级复盘、不恋战。"
    )
    next_step = (
        "下一步研究：明日重点盯 ①能否放量站稳短期均线 ②是否跌破下面的证伪位 ③所属板块/题材是否同步走强。"
        "满足①继续纸面跟踪验证；触发②就降级复盘。"
    )
    return {
        "available": True,
        "headline": f"{(name or '').strip()} · {bias}".strip(" ·"),
        "current_state": current_state,
        "research_lean": lean,
        "continue_if": continue_if,
        "invalidate_if": invalidate_if,
        "key_levels": {
            "support_60d": round(low_60, 2) if low_60 is not None else None,
            "resistance_60d": round(high_60, 2) if high_60 is not None else None,
            "reference_1d": pb.get("reference_1d") or {},
            "reference_5d": pb.get("reference_5d") or {},
        },
        "next_step": next_step,
        "boundary": "以上为研究观察条件与情景推演（非确定预测），不是买卖指令，不提供价位目标，也不提供仓位建议。Research Only / Paper Only.",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Start QuantDesk daily research server")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument(
        "--unsafe-allow-remote-access",
        action="store_true",
        help="Allow binding to non-local hosts such as 0.0.0.0. Research-only; exposes local write APIs on your LAN.",
    )
    parser.add_argument("--open-browser", action="store_true", help="Open the local daily research UI in the default browser.")
    parser.add_argument(
        "--update-on-start",
        action="store_true",
        help="Run a bounded current-pool daily data update before serving the UI. Requires explicit local launch choice; research-only.",
    )
    parser.add_argument(
        "--enable-network",
        action="store_true",
        help="Permit explicit startup data refresh network access. Research-only; no broker route.",
    )
    parser.add_argument("--update-max-symbols", type=int, default=80, help="Legacy option; updates now use the full-A Pipeline")
    parser.add_argument("--update-providers", default="", help="Legacy option; Pipeline owns its provider policy")
    parser.add_argument(
        "--enable-realtime-network",
        action="store_true",
        help="Allow the realtime observation layer to refresh quote/sector snapshots. Research-only; no broker route.",
    )
    args = parser.parse_args()
    try:
        bind_host = _resolve_bind_host(args.host, allow_remote=bool(args.unsafe_allow_remote_access))
    except ValueError as exc:
        print(f"[security] {exc}", file=sys.stderr, flush=True)
        return 2
    if bind_host not in LOCAL_BIND_HOSTS:
        print(
            "[security] WARNING: remote access explicitly enabled. QuantDesk remains research-only and paper-only; "
            "do not expose this service beyond your trusted LAN.",
            flush=True,
        )
    app.config["REALTIME_NETWORK_ENABLED"] = bool(args.enable_realtime_network)

    if args.update_on_start and not args.enable_network:
        payload = {
            "task_id": "startup_data_update",
            "status": "network_disabled",
            "startup_update_attempted": False,
            "provider_network_authorized": False,
            "network": False,
            "dry_run": True,
            "message": "Startup update skipped: --update-on-start requires explicit --enable-network.",
            "updated_at": datetime.now().isoformat(timespec="seconds"),
            "research_only": True,
            "paper_only": True,
            "no_broker": True,
            "no_live_trading": True,
            "no_auto_execution": True,
        }
        _write_startup_status_payload(payload)
        print("[startup] 候选池日线更新已跳过：需要显式 --enable-network。", flush=True)

    if args.update_on_start and args.enable_network:
        providers = [item.strip() for item in str(args.update_providers or "").split(",") if item.strip()] or None
        try:
            payload = run_startup_data_update(
                project_root,
                enable_network=True,
                max_symbols=max(0, int(args.update_max_symbols or 0)),
                providers=providers,
                rebuild_features=True,
                generate_candidates=False,
            )
            _write_startup_status_payload(payload)
            generation = payload.get("candidate_generation") if isinstance(payload.get("candidate_generation"), dict) else {}
            print(
                "[startup] 候选池日线更新: "
                f"status={payload.get('status', '-')}; "
                f"updated={payload.get('updated_symbols', 0)}; "
                f"stale_after={payload.get('stale_count_after', 0)}; "
                f"missing_after={payload.get('missing_count_after', 0)}; "
                f"generated_candidates={generation.get('candidate_count', 0)}",
                flush=True,
            )
        except Exception as exc:  # noqa: BLE001
            failure = {
                "task_id": "startup_data_update",
                "status": "failed",
                "startup_update_attempted": True,
                "provider_network_authorized": True,
                "network": True,
                "dry_run": False,
                "message": f"服务启动时自动更新失败：{type(exc).__name__}: {exc}",
                "updated_at": datetime.now().isoformat(timespec="seconds"),
                "research_only": True,
                "paper_only": True,
                "no_broker": True,
                "no_live_trading": True,
                "no_auto_execution": True,
                "no_buy_sell_advice": True,
                "no_position_advice": True,
                "no_real_money_allocation": True,
            }
            STARTUP_UPDATE_JSON.parent.mkdir(parents=True, exist_ok=True)
            STARTUP_UPDATE_JSON.write_text(json.dumps(clean_payload(failure), ensure_ascii=False, indent=2), encoding="utf-8")
            print(f"[startup] 候选池日线更新失败: {exc}", flush=True)

    # Auto-check news freshness and sector board on startup
    import threading as _th
    def _startup_checks():
        import time as _time
        _time.sleep(2.0)
        # --- News ---
        try:
            stats = news_cache_stats()
            file_count = stats.get("file_count", 0)
            latest = stats.get("latest_date", "")
            age = None
            if latest and len(latest) == 8:
                from datetime import datetime as _dt
                age = (_dt.now() - _dt.strptime(latest, "%Y%m%d")).days
            if file_count == 0:
                print("[startup] 新闻缓存为空，首次刷新将在打开候选详情时自动触发。", flush=True)
            elif age is not None and age > 7:
                print(f"[startup] 新闻缓存已过期 {age} 天，后台自动刷新前10只追踪股票…", flush=True)
                symbols = _top_tracked_symbols(10)
                for sym in symbols:
                    try:
                        refresh_single_news(sym)
                        print(f"  [startup] 已刷新 {sym}", flush=True)
                    except Exception as exc:  # noqa: BLE001 - startup news refresh is best-effort
                        LOG.warning("startup_news_refresh_symbol_failed: symbol=%s error=%s", sym, exc)
                print("[startup] 新闻后台刷新完成。", flush=True)
            else:
                print(f"[startup] 新闻缓存新鲜 ({age} 天前)，跳过刷新。", flush=True)
        except Exception as exc:
            print(f"[startup] 新闻检查失败 (非致命): {exc}", flush=True)
        # --- Sector Board ---
        _time.sleep(0.5)
        try:
            if not INDUSTRY_BOARD_CACHE.exists():
                print("[startup] 行业板块缓存不存在，后台拉取中…", flush=True)
                result = refresh_industry_board_cache()
                print(f"[startup] 行业板块: {result.get('status','?')} ({result.get('board_count',0)} 个板块)", flush=True)
            else:
                cache_age = (_time.time() - INDUSTRY_BOARD_CACHE.stat().st_mtime) / 3600
                if cache_age > 24:
                    print(f"[startup] 行业板块缓存已过 {cache_age:.0f} 小时，后台刷新…", flush=True)
                    result = refresh_industry_board_cache()
                    print(f"[startup] 行业板块: {result.get('status','?')} ({result.get('board_count',0)} 个板块)", flush=True)
                else:
                    print(f"[startup] 行业板块缓存新鲜 ({cache_age:.0f} 小时前)，跳过。", flush=True)
        except Exception as exc:
            print(f"[startup] 行业板块检查失败 (非致命): {exc}", flush=True)

    def _top_tracked_symbols(n: int) -> list[str]:
        symbols: list[str] = []
        try:
            ledger_path = DATA_DIR / "tracking" / "candidate_ledger.csv"
            if ledger_path.exists():
                import csv as _csv
                with ledger_path.open("r", encoding="utf-8-sig", newline="") as fh:
                    for row in _csv.DictReader(fh):
                        code = normalize_symbol(row.get("ts_code") or row.get("symbol") or "")
                        if code and code not in symbols:
                            symbols.append(code)
            if not symbols:
                symbols = ["600519", "000001", "300750", "000858", "601318"]
        except Exception as exc:  # noqa: BLE001 - startup candidate sample fallback
            LOG.warning("startup_top_tracked_symbols_read_failed: error=%s", exc)
            symbols = ["600519", "000001", "300750", "000858", "601318"]
        return symbols[:n]

    if args.enable_realtime_network:
        _th.Thread(target=_startup_checks, daemon=True).start()
    else:
        print("[startup] 新闻/板块后台联网检查已跳过：实时观察层联网为 cache-only。", flush=True)

    url = f"http://{bind_host}:{args.port}/"
    if args.open_browser:
        Thread(target=lambda: (time.sleep(1.0), webbrowser.open(url)), daemon=True).start()

    print(f"QuantDesk 研究服务: {url}")
    print(f"实时观察层联网: {'enabled' if args.enable_realtime_network else 'cache-only'}")
    app.run(host=bind_host, port=args.port, debug=False, use_reloader=False, threaded=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
