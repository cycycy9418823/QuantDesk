"""Read-only Market Brain / Strategy Brain shadow routes (port-8000 UI).

Stage 1 wiring (task book section 5): these endpoints only READ the latest
assembly artifacts under ``results/``.  They never compute a regime, never
trigger network access, and never touch paper state.  A missing artifact is
an explicit ``missing`` payload, not a fabricated snapshot.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from flask import Blueprint, Flask, jsonify

from scripts.server.helpers.util import clean_payload

_BOUNDARY = {
    "research_only": True,
    "paper_account_only": True,
    "no_broker_route": True,
    "live_execution_locked": True,
    "not_investment_advice": True,
    "not_position_advice": True,
}


@dataclass(frozen=True)
class MarketBrainRouteDeps:
    results_dir: Path


def _read_artifact(path: Path, *, artifact_label: str) -> tuple[dict[str, Any], int]:
    if not path.exists():
        return (
            {
                "ok": False,
                "status": "missing",
                "reason_code": f"{artifact_label}_artifact_missing",
                "message_for_ui": "尚未生成快照：请先运行每日工作流（qd.py daily）。",
                "artifact_path": str(path),
                **_BOUNDARY,
            },
            200,
        )
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        return (
            {
                "ok": False,
                "status": "degraded",
                "reason_code": f"{artifact_label}_artifact_unreadable",
                "message_for_ui": f"快照文件不可读：{exc}",
                "artifact_path": str(path),
                **_BOUNDARY,
            },
            500,
        )
    return (
        {
            "ok": True,
            "status": "loaded",
            "artifact_path": str(path),
            **payload,
            **_BOUNDARY,
        },
        200,
    )


def create_market_brain_blueprint(deps: MarketBrainRouteDeps) -> Blueprint:
    bp = Blueprint("market_brain_api", __name__)

    @bp.route("/api/market-brain/latest", methods=["GET"])
    def get_market_brain_latest() -> Any:
        payload, code = _read_artifact(
            deps.results_dir / "market_brain_snapshot_latest.json",
            artifact_label="market_brain",
        )
        return jsonify(clean_payload(payload)), code

    @bp.route("/api/strategy-brain/shadow-latest", methods=["GET"])
    def get_strategy_brain_shadow_latest() -> Any:
        payload, code = _read_artifact(
            deps.results_dir / "strategy_brain_shadow_latest.json",
            artifact_label="strategy_brain_shadow",
        )
        return jsonify(clean_payload(payload)), code

    return bp


def register_market_brain_routes(app: Flask, deps: MarketBrainRouteDeps) -> None:
    app.register_blueprint(create_market_brain_blueprint(deps))


__all__ = [
    "MarketBrainRouteDeps",
    "create_market_brain_blueprint",
    "register_market_brain_routes",
]
