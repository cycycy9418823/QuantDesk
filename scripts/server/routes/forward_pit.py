"""Read-only Forward PIT / T0 status routes (port-8000 UI).

Round 9S Part A: only READ existing readiness + chain artifacts. Never compute
a regime, never network, never touch paper/canonical state.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from flask import Blueprint, Flask, Response

from quantdesk.data_truth.forward_pit_ui_readiness import build_forward_pit_ui_readiness


@dataclass(frozen=True)
class ForwardPitRouteDeps:
    project_root: Path
    results_dir: Path | None = None


def _truth_json_response(payload: dict[str, Any], *, status: int) -> Response:
    """Serialize this typed truth contract without presentation placeholders."""

    body = json.dumps(
        payload,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
    )
    return Response(body, status=status, content_type="application/json; charset=utf-8")


def create_forward_pit_blueprint(deps: ForwardPitRouteDeps) -> Blueprint:
    bp = Blueprint("forward_pit_api", __name__)
    root = Path(deps.project_root).resolve()
    results_dir = Path(deps.results_dir) if deps.results_dir is not None else root / "results"

    @bp.route("/api/forward-pit/readiness", methods=["GET"])
    def get_forward_pit_readiness() -> Any:
        readiness_path = results_dir / "data_health" / "round7_forward_pit_readiness_latest.json"
        chain_path = results_dir / "data_chain" / "daily_data_chain_latest.json"
        # When results_dir is an isolated test root, project_root may still be
        # the real tree — prefer results_dir parent if it looks self-contained.
        project = root
        if (results_dir / "data_health").exists() or (results_dir / "data_chain").exists():
            project = results_dir.parent if results_dir.name == "results" else root
        payload = build_forward_pit_ui_readiness(
            project,
            readiness_path=readiness_path,
            chain_latest_path=chain_path,
        )
        code = 200
        if payload.get("ok") is False and str(payload.get("status") or "") == "degraded":
            if "unreadable" in str(payload.get("reason_code") or ""):
                code = 500
        return _truth_json_response(payload, status=code)

    return bp


def register_forward_pit_routes(app: Flask, deps: ForwardPitRouteDeps) -> None:
    app.register_blueprint(create_forward_pit_blueprint(deps))


__all__ = [
    "ForwardPitRouteDeps",
    "create_forward_pit_blueprint",
    "register_forward_pit_routes",
]
