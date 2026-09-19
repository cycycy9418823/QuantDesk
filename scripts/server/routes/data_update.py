"""Explicit update routes for the single Data Close Pipeline writer."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from flask import Blueprint, Flask, jsonify, request

from scripts.server.helpers.util import clean_payload, request_truthy


@dataclass(frozen=True)
class DataUpdateRouteDeps:
    """Compatibility callback names; the server binds updates to Pipeline."""

    project_root: Callable[[], Path]
    data_write_lock: Callable[[], Any]
    realtime_network_enabled: Callable[[], bool]
    run_full_market_catchup: Callable[..., dict[str, Any]]
    run_repair_daily: Callable[[], tuple[int, str, dict[str, Any]]]


def run_repair_daily_command(project_root: Path) -> tuple[int, str, dict[str, Any]]:
    """The removed price-reconstruction script must never be launched."""
    return 5, "", {"ok": False, "status": "retired", "canonical_write": False,
                   "message_for_ui": "旧日线修复入口已停用。缺失行情保留缺失，可运行收盘更新。",
                   "update_endpoint": "/api/data/update-all"}


def create_data_update_blueprint(deps: DataUpdateRouteDeps) -> Blueprint:
    bp = Blueprint("data_update_api", __name__)

    @bp.route("/api/data/update-all", methods=["POST"])
    def post_data_update_all() -> Any:
        data_lock = deps.data_write_lock()
        if not data_lock.acquire(blocking=False):
            return jsonify({"ok": False, "status": "busy",
                            "message_for_ui": "另一个数据更新正在进行。"}), 409
        try:
            body = request.get_json(silent=True) or {}
            network_allowed = bool(deps.realtime_network_enabled()) or request_truthy(body.get("explicit_network"))
            if not network_allowed:
                return jsonify({"ok": False, "status": "network_blocked",
                                "message_for_ui": "收盘更新需要显式授权联网。"}), 403
            try:
                payload = deps.run_full_market_catchup(
                    deps.project_root(), network=True, max_symbols=0, priority_only=False,
                )
                payload.setdefault("message_for_ui", payload.get("message") or "请查看收盘更新状态。")
                status = 409 if payload.get("status") in {"busy", "blocked"} else 500 if payload.get("status") == "failed" else 200
            except Exception as exc:
                payload = {"ok": False, "status": "failed",
                           "message_for_ui": f"收盘更新失败：{type(exc).__name__}: {exc}"}
                status = 500
            return jsonify(clean_payload(payload)), status
        finally:
            data_lock.release()

    @bp.route("/api/data/repair-daily", methods=["POST"])
    def post_data_repair_daily() -> Any:
        return jsonify(run_repair_daily_command(deps.project_root())[2]), 410

    return bp


def register_data_update_routes(app: Flask, deps: DataUpdateRouteDeps) -> None:
    app.register_blueprint(create_data_update_blueprint(deps))
