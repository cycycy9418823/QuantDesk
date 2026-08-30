"""Data update/repair write routes for the QuantDesk 8000 server.

These endpoints are local research maintenance actions. Network access remains
explicitly gated and writes are serialized through the shared data-write lock.
"""

from __future__ import annotations

import json
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from flask import Blueprint, Flask, jsonify, request

from scripts.server.helpers.util import clean_payload, request_truthy


@dataclass(frozen=True)
class DataUpdateRouteDeps:
    """Callbacks that keep data maintenance routes decoupled from quant_api_server."""

    project_root: Callable[[], Path]
    data_write_lock: Callable[[], Any]
    realtime_network_enabled: Callable[[], bool]
    run_full_market_catchup: Callable[..., dict[str, Any]]
    run_repair_daily: Callable[[], tuple[int, str, dict[str, Any]]]


def run_repair_daily_command(project_root: Path) -> tuple[int, str, dict[str, Any]]:
    """Run the daily repair script and return ``(returncode, stderr, latest_report)``."""

    script = project_root / "scripts" / "repair_daily_data.py"
    proc = subprocess.run(
        [sys.executable, str(script), "--apply"],
        capture_output=True,
        text=True,
        timeout=600,
        cwd=str(project_root),
    )
    reports = sorted((project_root / "results" / "data_repair").glob("repair_*.json"))
    report = json.loads(reports[-1].read_text(encoding="utf-8")) if reports else {}
    return proc.returncode, proc.stderr or "", report if isinstance(report, dict) else {}


def create_data_update_blueprint(deps: DataUpdateRouteDeps) -> Blueprint:
    """Create write-gated data update/repair routes for the active 8000 UI."""

    bp = Blueprint("data_update_api", __name__)

    @bp.route("/api/data/update-all", methods=["POST"])
    def post_data_update_all() -> Any:
        """Bounded full-market daily catch-up. Network + write-lock gated; research only."""

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
            # Strict consent only: a JSON string such as "false"/"0" must never
            # pass this gate, so the shared request_truthy() helper is used
            # instead of a generic bool() on the raw request value.
            network_allowed = bool(deps.realtime_network_enabled()) or request_truthy(
                body.get("explicit_network")
            )
            if not network_allowed:
                return jsonify(
                    clean_payload(
                        {
                            "ok": False,
                            "status": "network_blocked",
                            "message_for_ui": "全量数据更新需要联网权限。请启动服务时添加 --enable-realtime-network，或在请求中传入 explicit_network=true。",
                        }
                    )
                ), 403
            try:
                max_symbols = int(body.get("max_symbols") or 120)
            except (TypeError, ValueError):
                max_symbols = 120
            max_symbols = max(1, min(max_symbols, 300))
            try:
                payload = deps.run_full_market_catchup(
                    deps.project_root(),
                    network=network_allowed,
                    max_symbols=max_symbols,
                    priority_only=True,
                )
                rows = payload.get("rows_written", payload.get("rows", 0))
                payload.setdefault(
                    "message_for_ui",
                    f"全量数据更新：状态 {payload.get('status', '-')}，写入 {rows} 行，本轮上限 {max_symbols} 只。",
                )
                status = 200
            except Exception as exc:  # noqa: BLE001
                payload = {
                    "ok": False,
                    "status": "failed",
                    "message_for_ui": f"全量数据更新失败：{exc}",
                    "error": str(exc),
                }
                status = 500
            return jsonify(clean_payload(payload)), status
        finally:
            data_lock.release()

    @bp.route("/api/data/repair-daily", methods=["POST"])
    def post_data_repair_daily() -> Any:
        """Run the daily-data repair tool (reconstruct corrupt prices). Backs up first."""

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
            try:
                returncode, stderr, report = deps.run_repair_daily()
                ok = returncode == 0
                files_modified = report.get("files_modified", 0)
                cells = report.get("price_cells_fixed", 0)
                dropped = report.get("rows_dropped", 0)
                payload = {
                    "ok": ok,
                    "files_scanned": report.get("files_scanned"),
                    "files_modified": files_modified,
                    "price_cells_fixed": cells,
                    "rows_dropped": dropped,
                    "backup_dir": report.get("backup_dir", ""),
                    "message_for_ui": (
                        (
                            f"日线修复完成：修正 {files_modified} 个文件、{cells} 个价格单元格，丢弃 {dropped} 行；原文件已备份。"
                            + ("修复后建议在'绩效复盘'里重建样本。" if files_modified else "未发现损坏，数据已是干净状态。")
                        )
                        if ok
                        else f"日线修复失败：{stderr[-300:]}"
                    ),
                }
                status = 200 if ok else 500
            except Exception as exc:  # noqa: BLE001
                payload = {"ok": False, "message_for_ui": f"日线修复失败：{exc}", "error": str(exc)}
                status = 500
            return jsonify(clean_payload(payload)), status
        finally:
            data_lock.release()

    return bp


def register_data_update_routes(app: Flask, deps: DataUpdateRouteDeps) -> None:
    app.register_blueprint(create_data_update_blueprint(deps))
