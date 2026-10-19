"""Strategy-screening background routes for the QuantDesk 8000 server.

Research-only / paper-only: these endpoints launch local strategy diagnostics
and write local artifacts. They do not connect brokers, place orders, or turn
screening output into trading instructions.
"""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, MutableMapping

from flask import Blueprint, Flask, jsonify, request

from quantdesk.paper.trade_costs import DEFAULT_COMMISSION_RATE, DEFAULT_SLIPPAGE_BPS, DEFAULT_STAMP_TAX_RATE
from quantdesk.research.sample_provenance import research_sample_context
from scripts.server.helpers.util import clean_payload, finite_float, safe_int


@dataclass(frozen=True)
class StrategyScreeningRouteDeps:
    """Callbacks/state accessors that keep this blueprint decoupled from the monolith."""

    project_root: Callable[[], Path]
    task_json: Callable[[], Path]
    state: Callable[[], MutableMapping[str, Any]]
    state_lock: Callable[[], Any]
    data_write_lock: Callable[[], Any]
    read_json_file: Callable[[Path], dict[str, Any]]
    request_truthy: Callable[[Any], bool]
    make_request_id: Callable[[], str]
    start_worker: Callable[[str, dict[str, Any]], None]


def compact_strategy_screening_result(payload: dict[str, Any]) -> dict[str, Any]:
    """Return the compact UI payload for the latest strategy-screening result."""

    performance = payload.get("performance") if isinstance(payload.get("performance"), dict) else {}
    dq = payload.get("data_quality_summary") if isinstance(payload.get("data_quality_summary"), dict) else {}
    cache = performance.get("feature_cache") if isinstance(performance.get("feature_cache"), dict) else {}
    leaderboard = payload.get("leaderboard") if isinstance(payload.get("leaderboard"), list) else []
    handoff = payload.get("backtest_handoff") if isinstance(payload.get("backtest_handoff"), dict) else {}
    return {
        **research_sample_context(payload),
        "exists": bool(payload),
        "ok": payload.get("ok"),
        "status": payload.get("status") or "",
        "task_type": payload.get("task_type") or "strategy_screening_funnel",
        "decision": payload.get("decision") or "",
        "elapsed_seconds": finite_float(performance.get("elapsed_seconds")),
        "feature_cache_status": cache.get("status") or "",
        "max_symbols": safe_int(performance.get("max_symbols") or dq.get("max_symbols"), 0),
        "daily_csv_available": safe_int(dq.get("daily_csv_available"), 0),
        "scanned_csv": safe_int(dq.get("scanned_csv") or dq.get("total_csv"), 0),
        "usable_csv": safe_int(dq.get("usable_csv"), 0),
        "strategies_tested": safe_int(
            performance.get("strategies_tested") or len(payload.get("strategy_results") or []),
            0,
        ),
        "candidate_count": len(payload.get("candidate_pool") or []),
        "watchlist_count": len(payload.get("watchlist") or []),
        "backtest_handoff": {
            "status": handoff.get("status") or "unavailable",
            "source": handoff.get("source") or "",
            "strategy": handoff.get("strategy") or "",
            "symbol_count": safe_int(handoff.get("symbol_count"), 0),
            "symbols": [str(item) for item in (handoff.get("symbols") or [])[:12]],
            "command_text": handoff.get("command_text") or "",
        },
        "top_strategies": [
            {
                **research_sample_context(item),
                "strategy": item.get("strategy") or "",
                "status": item.get("status") or "",
                "annualized_return_net": finite_float(item.get("annualized_return_net")),
                "max_drawdown": finite_float(item.get("max_drawdown")),
                "sharpe": finite_float(item.get("sharpe")),
            }
            for item in leaderboard[:5]
            if isinstance(item, dict)
        ],
        "output_paths": payload.get("output_paths") or {},
        "warnings": [str(item) for item in (payload.get("warnings") or [])[:8]],
        "research_only": True,
        "paper_only": True,
        "no_broker": True,
        "no_real_orders": True,
        "not_investment_advice": True,
    }


def write_strategy_screening_status(deps: StrategyScreeningRouteDeps, payload: dict[str, Any]) -> None:
    """Persist the current background status for the UI."""

    task_json = deps.task_json()
    task_json.parent.mkdir(parents=True, exist_ok=True)
    task_json.write_text(
        json.dumps(clean_payload(payload), ensure_ascii=False, indent=2, allow_nan=False),
        encoding="utf-8",
    )


def _float_param_or_default(params: dict[str, Any], key: str, default: float) -> float:
    value = params.get(key)
    if value is None or value == "":
        return float(default)
    return float(value)


def strategy_screening_status(deps: StrategyScreeningRouteDeps) -> dict[str, Any]:
    """Read the latest background status and attach the compact latest result."""

    payload = deps.read_json_file(deps.task_json())
    state = deps.state()
    if not payload:
        payload = {
            "ok": True,
            "status": state.get("status") or "idle",
            "running": bool(state.get("running")),
            "message_for_ui": "策略筛选漏斗尚未在后台运行。",
            "research_only": True,
            "paper_only": True,
            "no_broker": True,
            "no_real_orders": True,
            "not_investment_advice": True,
        }
    payload["running"] = bool(state.get("running")) or bool(payload.get("running"))
    latest = deps.read_json_file(deps.project_root() / "results" / "strategy_screening_funnel_latest.json")
    if latest and latest.get("status") != "unreadable":
        payload["latest_result"] = compact_strategy_screening_result(latest)
    return payload


def run_strategy_screening_worker(
    deps: StrategyScreeningRouteDeps,
    request_id: str,
    params: dict[str, Any],
) -> None:
    """Run the local strategy-screening funnel in a background worker."""

    started_at = datetime.now().astimezone().isoformat(timespec="seconds")
    state = deps.state()
    with deps.state_lock():
        state.update(
            {
                "running": True,
                "status": "running",
                "request_id": request_id,
                "started_at": started_at,
                "message_for_ui": "策略筛选漏斗正在后台运行；页面可继续使用，完成后读取 latest 结果。",
            }
        )
    running_payload = {
        "ok": True,
        "status": "running",
        "running": True,
        "request_id": request_id,
        "started_at": started_at,
        "params": params,
        "message_for_ui": "策略筛选漏斗正在后台运行；默认使用样本上限和特征缓存，避免阻塞 8000 页面。",
        "research_only": True,
        "paper_only": True,
        "no_broker": True,
        "no_real_orders": True,
        "not_investment_advice": True,
    }
    write_strategy_screening_status(deps, running_payload)
    data_lock = deps.data_write_lock()
    if not data_lock.acquire(blocking=False):
        failure = {
            **running_payload,
            "ok": False,
            "status": "busy",
            "running": False,
            "finished_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "message_for_ui": "另一个数据写入任务正在运行，本轮策略筛选已跳过。",
        }
        write_strategy_screening_status(deps, failure)
        with deps.state_lock():
            state.update({"running": False, "status": "busy", "message_for_ui": failure["message_for_ui"]})
        return
    try:
        from scripts.run_strategy_screening_funnel import run_screening, write_outputs

        args = argparse.Namespace(
            data_root=str(params.get("data_root") or "data/daily"),
            initial_cash=float(params.get("initial_cash") or 100000),
            top_n_default=int(params.get("top_n_default") or 20),
            benchmark=str(params.get("benchmark") or "000905"),
            start_date="",
            end_date="",
            min_avg_amount_20d=float(params.get("min_avg_amount_20d") or 20000000),
            min_history_days=int(params.get("min_history_days") or 180),
            lot_size=int(params.get("lot_size") or 100),
            commission_rate=_float_param_or_default(params, "commission_rate", DEFAULT_COMMISSION_RATE),
            stamp_tax_rate=_float_param_or_default(params, "stamp_tax_rate", DEFAULT_STAMP_TAX_RATE),
            slippage_bps=_float_param_or_default(params, "slippage_bps", DEFAULT_SLIPPAGE_BPS),
            output_dir=str(deps.project_root() / "results"),
            report_dir=str(deps.project_root() / "reports"),
            max_strategies=int(params.get("max_strategies", 8)),
            max_symbols=int(params.get("max_symbols", 600)),
            feature_cache=str(params.get("feature_cache") if params.get("feature_cache") is not None else "true"),
            cache_dir=None,
            mimo_retest=str(params.get("mimo_retest") or "false"),
            json="true",
            summary="true",
        )
        payload = write_outputs(run_screening(args), deps.project_root() / args.output_dir, deps.project_root() / args.report_dir)
        summary = compact_strategy_screening_result(payload)
        status_payload = {
            **running_payload,
            "ok": bool(payload.get("ok", False)),
            "status": payload.get("status") or "completed",
            "running": False,
            "finished_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "latest_result": summary,
            "message_for_ui": (
                f"策略筛选漏斗完成：扫描 {summary.get('scanned_csv')} / {summary.get('daily_csv_available')} 个本地 CSV，"
                f"测试 {summary.get('strategies_tested')} 个策略，缓存状态 {summary.get('feature_cache_status')}。"
            ),
        }
        write_strategy_screening_status(deps, status_payload)
        with deps.state_lock():
            state.update(
                {
                    "running": False,
                    "status": str(status_payload["status"]),
                    "message_for_ui": str(status_payload["message_for_ui"]),
                }
            )
    except Exception as exc:  # noqa: BLE001
        failure = {
            **running_payload,
            "ok": False,
            "status": "failed",
            "running": False,
            "finished_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "error": f"{type(exc).__name__}: {exc}",
            "message_for_ui": f"策略筛选漏斗后台运行失败：{type(exc).__name__}: {exc}",
        }
        write_strategy_screening_status(deps, failure)
        with deps.state_lock():
            state.update({"running": False, "status": "failed", "message_for_ui": failure["message_for_ui"]})
    finally:
        data_lock.release()


def create_strategy_screening_blueprint(deps: StrategyScreeningRouteDeps) -> Blueprint:
    """Create strategy-screening routes used by the active 8000 UI."""

    bp = Blueprint("strategy_screening_api", __name__)

    @bp.route("/api/strategy-screening/status", methods=["GET"])
    def get_strategy_screening_status() -> Any:
        return jsonify(clean_payload(strategy_screening_status(deps)))

    @bp.route("/api/strategy-screening/run", methods=["POST"])
    def post_strategy_screening_run() -> Any:
        state = deps.state()
        if state.get("running"):
            return jsonify(clean_payload(strategy_screening_status(deps))), 202
        body = request.get_json(silent=True) or {}
        if any(body.get(key) not in (None, "") for key in ("cache_dir", "output_dir", "report_dir")):
            return jsonify({"ok": False, "status": "invalid_request", "error": "screening_output_paths_are_server_managed"}), 400
        full_scan = deps.request_truthy(body.get("full_scan"))
        full_strategies = deps.request_truthy(body.get("full_strategies"))
        try:
            max_symbols = int(body.get("max_symbols") or 600)
        except (TypeError, ValueError):
            max_symbols = 600
        if full_scan:
            max_symbols = 0
        else:
            max_symbols = max(20, min(max_symbols, 1500))
        try:
            max_strategies = int(body.get("max_strategies") or 8)
        except (TypeError, ValueError):
            max_strategies = 8
        if full_strategies:
            max_strategies = 0
        else:
            max_strategies = max(1, min(max_strategies, 30))
        request_id = deps.make_request_id()
        params = {
            "data_root": "data/daily",
            "benchmark": str(body.get("benchmark") or "000905"),
            "max_symbols": max_symbols,
            "max_strategies": max_strategies,
            "feature_cache": "true" if body.get("feature_cache") is None else str(body.get("feature_cache")),
            "cache_dir": None,
            "mimo_retest": str(body.get("mimo_retest") or "false"),
            "full_scan_requested": full_scan,
            "full_strategies_requested": full_strategies,
        }
        with deps.state_lock():
            state.update(
                {
                    "running": True,
                    "status": "running",
                    "request_id": request_id,
                    "started_at": request_id,
                    "message_for_ui": "策略筛选漏斗已进入后台队列。",
                }
            )
        deps.start_worker(request_id, params)
        return jsonify(
            clean_payload(
                {
                    "ok": True,
                    "status": "running",
                    "running": True,
                    "request_id": request_id,
                    "params": params,
                    "message_for_ui": "策略筛选漏斗已在后台启动；默认限样本并启用特征缓存，不阻塞页面。",
                    "research_only": True,
                    "paper_only": True,
                    "no_broker": True,
                    "no_real_orders": True,
                    "not_investment_advice": True,
                }
            )
        ), 202

    return bp


def register_strategy_screening_routes(app: Flask, deps: StrategyScreeningRouteDeps) -> None:
    app.register_blueprint(create_strategy_screening_blueprint(deps))
