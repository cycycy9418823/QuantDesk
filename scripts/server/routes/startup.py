"""Startup refresh/status routes for the QuantDesk 8000 server."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from flask import Blueprint, Flask, jsonify, request

from quantdesk.pipeline_terminal import pipeline_failure_details
from scripts.server.helpers.util import clean_payload


@dataclass(frozen=True)
class StartupRouteDeps:
    """Callbacks that keep this blueprint decoupled from quant_api_server."""

    read_startup_payload: Callable[[], dict[str, Any]]
    write_startup_status_payload: Callable[[dict[str, Any]], None]
    generation_status_for_ui: Callable[..., dict[str, Any]]
    startup_refresh_state_payload: Callable[[], dict[str, Any]]
    startup_payload_has_completed_after_state: Callable[[dict[str, Any], dict[str, Any]], bool]
    set_startup_refresh_state: Callable[..., None]
    candidate_rows: Callable[[int], list[dict[str, Any]]]
    data_gap_summary: Callable[[list[dict[str, Any]]], dict[str, Any]]
    startup_provider_update_diagnosis: Callable[[dict[str, Any]], dict[str, Any]]
    startup_update_explanation: Callable[[dict[str, Any]], dict[str, Any]]
    request_truthy: Callable[[Any], bool]
    make_request_id: Callable[[], str]
    start_startup_refresh_worker: Callable[[str, str], None]


def _default_startup_payload() -> dict[str, Any]:
    return {
        "task_id": "startup_data_update",
        "status": "not_run",
        "startup_update_attempted": False,
        "provider_network_authorized": False,
        "network": False,
        "dry_run": True,
        "scope": "full_a",
        "only_low_price": False,
        "daily_writer": "data_close_pipeline",
        "full_scan": False,
        "updated_symbols": 0,
        "failed_symbols": 0,
        "stale_count_after": 0,
        "missing_count_after": 0,
        "latest_local_date_after": "",
        "message": "尚未发现启动更新记录。请通过 START_QUANTDESK.cmd 或带 --update-on-start 的启动器打开。",
        "research_only": True,
        "paper_only": True,
        "no_broker": True,
        "no_live_trading": True,
        "no_auto_execution": True,
        "no_buy_sell_advice": True,
        "no_position_advice": True,
        "no_real_money_allocation": True,
    }


def create_startup_blueprint(deps: StartupRouteDeps) -> Blueprint:
    """Create startup status/refresh routes used by the active 8000 UI."""

    bp = Blueprint("startup_api", __name__)

    @bp.route("/api/startup/update-status", methods=["GET"])
    def get_startup_update_status() -> Any:
        payload = deps.read_startup_payload()
        if not payload:
            payload = _default_startup_payload()
        payload.setdefault("path", "results/startup_update_latest.json")
        payload.setdefault("research_only", True)
        payload.setdefault("paper_only", True)
        payload.setdefault("no_broker", True)
        payload.setdefault("no_live_trading", True)
        payload.setdefault("no_auto_execution", True)
        payload.setdefault("no_buy_sell_advice", True)
        payload.setdefault("no_position_advice", True)
        payload.setdefault("no_real_money_allocation", True)
        state = deps.startup_refresh_state_payload()
        if deps.startup_payload_has_completed_after_state(payload, state):
            deps.set_startup_refresh_state(running=False, status=payload.get("status") or "failed")
            state = deps.startup_refresh_state_payload()
        payload["running"] = bool(state.get("running"))
        payload["current_view_override"] = False
        if state.get("running"):
            payload["status"] = "running"
            payload["message"] = state.get("message_for_ui") or "收盘更新正在进行。"
            payload["request_id"] = state.get("request_id") or ""
        elif payload.get("status") == "running":
            payload["status"] = "interrupted"
            payload["message"] = "上次更新未留下完成记录；当前未在运行。"
        # Current candidate freshness is not evidence that this run completed.
        payload.setdefault("daily_writer", "data_close_pipeline")
        if payload.get("status") in {"completed", "completed_all_executed"} and not payload["running"]:
            generation = deps.generation_status_for_ui()
            current_rows = deps.candidate_rows(120)
            summary = deps.data_gap_summary(current_rows)
            payload["current_candidate_health"] = {"generation": generation, "summary": summary}
            same_target = not payload.get("target_date") or payload.get("target_date") == generation.get("target_date")
            if same_target and generation.get("freshness_status") in {"fresh", "current"} and not summary.get("gap_count"):
                # Display an independently computed current view only after the
                # original attempt already has its own successful terminal.
                payload.update(current_view_override=True, candidate_generation=generation,
                               current_candidate_gap_summary=summary,
                               current_status_label="当前候选已补齐", current_status_tone="ok",
                               current_message=summary.get("message") or "当前候选数据已核对。")
        payload.setdefault("provider_diagnosis", deps.startup_provider_update_diagnosis(payload))
        payload["startup_update_explanation"] = deps.startup_update_explanation(payload)
        if isinstance(payload.get("pipeline"), dict):
            # Derive display fields for old records in memory; never rewrite them.
            payload["failure_details"] = pipeline_failure_details(payload["pipeline"])
        return jsonify(clean_payload(payload))

    @bp.route("/api/startup/refresh", methods=["POST"])
    def post_startup_refresh() -> Any:
        body = request.get_json(silent=True) or {}
        network_allowed = deps.request_truthy(body.get("network")) or deps.request_truthy(body.get("enable_network"))
        if not network_allowed:
            payload = {
                "ok": True,
                "status": "network_disabled",
                "network": False,
                "message_for_ui": "Startup refresh skipped: network access is disabled by default. Pass network:true for an explicit research-only refresh.",
                "research_only": True,
                "paper_only": True,
                "no_broker": True,
                "no_live_trading": True,
                "no_auto_execution": True,
            }
            return jsonify(clean_payload(payload)), 200
        state = deps.startup_refresh_state_payload()
        if state.get("running"):
            payload = {
                "ok": True,
                "status": "running",
                "request_id": state.get("request_id") or state.get("started_at") or "",
                "message_for_ui": state.get("message_for_ui") or "Startup refresh is already running.",
            }
            return jsonify(clean_payload(payload)), 202
        request_id = deps.make_request_id()
        deps.set_startup_refresh_state(
            running=True,
            status="running",
            reason="explicit_update",
            started_at=request_id,
            request_id=request_id,
            message_for_ui="正在运行全 A 收盘更新。",
        )
        deps.start_startup_refresh_worker("explicit_update", request_id)
        payload = {
            "ok": True,
            "status": "running",
            "request_id": request_id,
            "message_for_ui": "Startup refresh started in the background.",
        }
        return jsonify(clean_payload(payload)), 202

    return bp


def register_startup_routes(app: Flask, deps: StartupRouteDeps) -> None:
    app.register_blueprint(create_startup_blueprint(deps))
