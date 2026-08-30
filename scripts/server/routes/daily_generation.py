"""Daily candidate update/generation write routes for the QuantDesk 8000 server."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from flask import Blueprint, Flask, jsonify, request

from scripts.server.helpers.util import clean_payload


@dataclass(frozen=True)
class DailyGenerationRouteDeps:
    """Callbacks that keep daily generation routes decoupled from quant_api_server."""

    project_root: Callable[[], Path]
    data_write_lock: Callable[[], Any]
    realtime_network_enabled: Callable[[], bool]
    request_truthy: Callable[[Any], bool]
    candidate_rows: Callable[[int], list[dict[str, Any]]]
    update_daily_prices: Callable[..., dict[str, Any]]
    generate_daily_recommendations: Callable[..., dict[str, Any]]
    append_paper_simulation_sync_to_generation_payload: Callable[..., dict[str, Any]]
    data_chain_consumer_gate: Callable[[], dict[str, Any]]


def create_daily_generation_blueprint(deps: DailyGenerationRouteDeps) -> Blueprint:
    """Create write-gated daily candidate update/generation routes."""

    bp = Blueprint("daily_generation_api", __name__)

    @bp.route("/api/daily/update-candidates", methods=["POST"])
    def post_daily_update_candidates() -> Any:
        data_lock = deps.data_write_lock()
        if not data_lock.acquire(blocking=False):
            return jsonify(
                clean_payload(
                    {
                        "ok": False,
                        "status": "busy",
                        "message_for_ui": "另一个数据写入操作正在进行，请稍后再试。",
                    }
                )
            ), 409
        try:
            body = request.get_json(silent=True) or {}
            network_allowed = bool(
                deps.realtime_network_enabled() or deps.request_truthy(body.get("explicit_network") or body.get("network"))
            )
            if not network_allowed:
                return jsonify(
                    clean_payload(
                        {
                            "ok": False,
                            "status": "network_blocked",
                            "message_for_ui": "候选池日线刷新需要联网权限。请启动服务时添加 --enable-realtime-network，或在请求中传入 explicit_network=true。",
                        }
                    )
                ), 403
            try:
                max_symbols = int(body.get("max_symbols") or 40)
            except (TypeError, ValueError):
                max_symbols = 40
            max_symbols = max(1, min(max_symbols, 80))
            try:
                visible_rows = deps.candidate_rows(min(max_symbols, 18))
                visible_symbols = [row["symbol"] for row in visible_rows if row.get("symbol")]
                payload = deps.update_daily_prices(
                    only_low_price=True,
                    providers=None,
                    workers=4,
                    network=network_allowed,
                    dry_run=False,
                    limit=0,
                    rebuild_features=True,
                    symbols=visible_symbols,
                )
                payload["scope"] = "visible_daily_research_candidates"
                payload["requested_symbols"] = visible_symbols
                payload["message_for_ui"] = (
                    f"已联网更新当前候选池：更新 {payload.get('updated_symbols', 0)} 只，"
                    f"失败 {payload.get('failed_symbols', 0)} 只，目标日期 {payload.get('target_date', '数据缺失')}。"
                )
                status = 200
            except Exception as exc:  # noqa: BLE001
                payload = {
                    "ok": False,
                    "status": "failed",
                    "message_for_ui": f"候选日线更新失败：{exc}",
                    "error": str(exc),
                    "research_only": True,
                    "paper_only": True,
                    "no_broker": True,
                    "no_live_trading": True,
                    "no_auto_execution": True,
                    "no_buy_sell_advice": True,
                    "no_position_advice": True,
                }
                status = 500
            return jsonify(clean_payload(payload)), status
        finally:
            data_lock.release()

    @bp.route("/api/daily/generate-recommendations", methods=["POST"])
    def post_daily_generate_recommendations() -> Any:
        data_lock = deps.data_write_lock()
        if not data_lock.acquire(blocking=False):
            return jsonify(
                clean_payload(
                    {
                        "ok": False,
                        "status": "busy",
                        "message_for_ui": "另一个数据写入操作正在进行，请稍后再试。",
                    }
                )
            ), 409
        try:
            body = request.get_json(silent=True) or {}
            gate = deps.data_chain_consumer_gate()
            if gate.get("allowed") is not True:
                gate_summary = {
                    key: gate.get(key)
                    for key in (
                        "status",
                        "allowed",
                        "run_id",
                        "target_date",
                        "reasons",
                        "chain_status",
                    )
                }
                return jsonify(
                    clean_payload(
                        {
                            "ok": False,
                            "status": "data_chain_blocked",
                            "message_for_ui": (
                                "The authoritative daily data chain is not ready. "
                                "Candidate generation and paper-pool sync remain blocked."
                            ),
                            "data_chain_gate": gate_summary,
                            "research_only": True,
                            "paper_only": True,
                            "no_broker": True,
                            "no_live_trading": True,
                            "no_auto_execution": True,
                            "no_buy_sell_advice": True,
                            "no_position_advice": True,
                        }
                    )
                ), 409
            try:
                max_scan_symbols = int(body.get("max_scan_symbols") or 300)
            except (TypeError, ValueError):
                max_scan_symbols = 300
            try:
                max_candidates = int(body.get("max_candidates") or 18)
            except (TypeError, ValueError):
                max_candidates = 18
            max_scan_symbols = max(1, min(max_scan_symbols, 800))
            max_candidates = max(1, min(max_candidates, 30))
            sync_paper = str(body.get("sync_paper", "true")).strip().lower() not in {"0", "false", "no", "off"}
            try:
                payload = deps.generate_daily_recommendations(
                    deps.project_root(),
                    max_scan_symbols=max_scan_symbols,
                    max_candidates=max_candidates,
                    append_ledger=True,
                )
                payload = deps.append_paper_simulation_sync_to_generation_payload(
                    payload,
                    max_candidates=max_candidates,
                    sync_paper=sync_paper,
                )
                payload["message_for_ui"] = payload.get("message_for_ui") or (
                    f"已生成 {payload.get('signal_date', '')} 每日研究候选 {payload.get('candidate_count', 0)} 只。"
                )
                status = 200
            except Exception as exc:  # noqa: BLE001
                payload = {
                    "ok": False,
                    "status": "failed",
                    "message_for_ui": f"每日研究候选生成失败：{exc}",
                    "error": str(exc),
                    "research_only": True,
                    "paper_only": True,
                    "no_broker": True,
                    "no_live_trading": True,
                    "no_auto_execution": True,
                    "no_buy_sell_advice": True,
                    "no_position_advice": True,
                }
                status = 500
            return jsonify(clean_payload(payload)), status
        finally:
            data_lock.release()

    return bp


def register_daily_generation_routes(app: Flask, deps: DailyGenerationRouteDeps) -> None:
    app.register_blueprint(create_daily_generation_blueprint(deps))
