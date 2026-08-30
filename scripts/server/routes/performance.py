"""Performance maintenance routes for the QuantDesk 8000 server."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from flask import Blueprint, Flask, jsonify

from scripts.server.helpers.util import clean_payload


@dataclass(frozen=True)
class PerformanceRouteDeps:
    """Callbacks that keep this blueprint decoupled from quant_api_server."""

    project_root: Callable[[], Path]
    build_candidate_performance: Callable[[Path], dict[str, Any]]


def create_performance_blueprint(deps: PerformanceRouteDeps) -> Blueprint:
    """Create performance maintenance routes used by the active 8000 UI."""

    bp = Blueprint("performance_api", __name__)

    @bp.route("/api/performance/rebuild", methods=["POST"])
    def post_performance_rebuild() -> Any:
        """Regenerate candidate_performance.csv from the ledger + local daily data."""
        try:
            payload = deps.build_candidate_performance(deps.project_root())
            payload.setdefault("ok", True)
            payload["message_for_ui"] = (
                f"绩效样本已重建：账本 {payload.get('ledger_rows', '-')} 行，"
                f"绩效 {payload.get('performance_rows', '-')} 行。"
            )
            status = 200
        except Exception as exc:  # noqa: BLE001
            payload = {"ok": False, "message_for_ui": f"绩效样本重建失败：{exc}", "error": str(exc)}
            status = 500
        return jsonify(clean_payload(payload)), status

    return bp


def register_performance_routes(app: Flask, deps: PerformanceRouteDeps) -> None:
    app.register_blueprint(create_performance_blueprint(deps))
