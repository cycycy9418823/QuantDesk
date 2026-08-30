"""Startup refresh/status routes for the QuantDesk 8000 server."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from flask import Blueprint, Flask, jsonify, request

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
        "scope": "current_analysis_pool",
        "only_low_price": True,
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
        if not isinstance(payload.get("candidate_generation"), dict):
            payload["candidate_generation"] = deps.generation_status_for_ui()
        else:
            payload["candidate_generation"] = deps.generation_status_for_ui(payload.get("candidate_generation"))
        state = deps.startup_refresh_state_payload()
        if deps.startup_payload_has_completed_after_state(payload, state):
            deps.set_startup_refresh_state(
                running=False,
                status=str(payload.get("status") or "completed"),
                request_id=state.get("request_id") or "",
                message_for_ui=str(payload.get("message_for_ui") or payload.get("message") or "Startup refresh completed."),
            )
            state = deps.startup_refresh_state_payload()
        state_running = bool(state.get("running"))
        payload["running"] = state_running
        payload.setdefault("request_id", state.get("request_id") or "")
        if not state_running and payload.get("status") == "running":
            update_result = payload.get("update_result") if isinstance(payload.get("update_result"), dict) else {}
            candidate_generation = payload.get("candidate_generation") if isinstance(payload.get("candidate_generation"), dict) else {}
            recovered_status = str(update_result.get("status") or candidate_generation.get("status") or "interrupted")
            if recovered_status == "failed" and candidate_generation.get("status") == "completed":
                recovered_status = "completed_with_warnings"
            payload["status"] = recovered_status
            payload["message"] = payload.get("message") or "启动更新任务未在当前服务进程中运行，已按最新本地结果恢复状态。"
        if state_running:
            payload["status"] = "running"
            payload["current_view_override"] = False
            payload["message"] = state.get("message_for_ui") or payload.get("message") or "Startup refresh is running in the background."
            payload["startup_update_attempted"] = True
            payload["provider_network_authorized"] = True
            payload["request_id"] = state.get("request_id") or payload.get("request_id") or ""
        try:
            rows = deps.candidate_rows(18)
            summary = deps.data_gap_summary(rows)
            payload["current_candidate_gap_summary"] = summary
            if rows and payload.get("status") != "running":
                update_result = payload.get("update_result") if isinstance(payload.get("update_result"), dict) else {}
                target_date = payload.get("target_date") or update_result.get("target_date") or summary.get("target_date") or ""
                latest_dates = [
                    str(row.get("data_quality", {}).get("latest_date") or "")
                    for row in rows
                    if str(row.get("data_quality", {}).get("latest_date") or "") not in {"", "数据缺失"}
                ]
                latest_min = min(latest_dates) if latest_dates else ""
                latest_max = max(latest_dates) if latest_dates else ""
                gap_count = int(summary.get("gap_count") or 0)
                field_gap_count = int(summary.get("field_gap_count") or 0)
                stale_count = int(summary.get("stale_count") or 0)
                missing_count = int(summary.get("missing_count") or 0)
                if gap_count == 0 and field_gap_count == 0:
                    status_label = "当前候选已补齐"
                    status_tone = "ok"
                    status = "completed"
                    current_message = (
                        f"当前页面 {summary.get('candidate_count', len(rows))} 只候选日线和关键字段可读取；"
                        "启动时历史更新记录仅作参考。"
                    )
                elif gap_count == 0 and field_gap_count > 0:
                    status_label = "价格日线已更新，成交额降级"
                    status_tone = "warning"
                    status = "completed_with_warnings"
                    current_message = (
                        f"当前页面 {summary.get('candidate_count', len(rows))} 只候选价格/K线可读取；"
                        f"{field_gap_count} 只缺成交额/量能字段，量能解释降级，但不代表价格日线没有更新。"
                    )
                else:
                    status_label = "部分候选仍需更新"
                    status_tone = "warning"
                    status = "completed_with_warnings"
                    current_message = summary.get("message") or "当前候选仍有日线滞后或缺失，需要继续联网更新或导入 CSV。"
                if gap_count > 0 and stale_count == 0 and missing_count == 0:
                    status_label = "价格日线已更新，存在质量提示"
                    status_tone = "warning"
                    status = "completed_with_warnings"
                    current_message = summary.get("message") or "当前候选价格日线已到目标交易日，但仍有字段或异常行过滤等质量提示。"
                candidate_generation = payload.get("candidate_generation") if isinstance(payload.get("candidate_generation"), dict) else {}
                if candidate_generation.get("freshness_status") == "stale":
                    status_label = "研究候选批次需要更新"
                    status_tone = "warning"
                    status = "completed_with_warnings"
                    freshness_message = str(candidate_generation.get("freshness_message") or "")
                    if freshness_message and freshness_message not in current_message:
                        current_message = f"{current_message} {freshness_message}".strip()
                    if not target_date:
                        target_date = str(candidate_generation.get("target_date") or "")
                provider_diagnosis = deps.startup_provider_update_diagnosis(payload)
                if stale_count > 0 and provider_diagnosis.get("tone") == "partial":
                    status_label = str(provider_diagnosis.get("label") or "自动刷新已部分完成")
                    status_tone = "warning"
                    status = "completed_with_warnings"
                    current_message = str(provider_diagnosis.get("summary") or current_message)
                payload.update(
                    {
                        "current_view_override": True,
                        "status": status,
                        "current_status_label": status_label,
                        "current_status_tone": status_tone,
                        "current_message": current_message,
                        "target_date": target_date,
                        "current_candidate_latest_date_min": latest_min,
                        "current_candidate_latest_date_max": latest_max,
                        "latest_local_date_after": latest_min or target_date,
                        "stale_count_after": summary.get("stale_count", 0),
                        "missing_count_after": summary.get("missing_count", 0),
                        "failed_symbols": summary.get("gap_count", 0),
                        "provider_diagnosis": provider_diagnosis,
                    }
                )
        except Exception as exc:  # noqa: BLE001
            payload["current_candidate_gap_summary"] = {
                "message": f"当前候选池健康状态复核失败：{exc}",
                "gap_count": "数据缺失",
                "field_gap_count": "数据缺失",
            }
        payload["startup_update_explanation"] = deps.startup_update_explanation(payload)
        payload.setdefault("provider_diagnosis", deps.startup_provider_update_diagnosis(payload))
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
            deps.write_startup_status_payload(payload)
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
            reason="page_open",
            started_at=request_id,
            request_id=request_id,
            message_for_ui="正在后台尝试刷新当前研究池；若外部数据源不可达，会回退到本地最新完整日线。",
        )
        deps.start_startup_refresh_worker("page_open", request_id)
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
