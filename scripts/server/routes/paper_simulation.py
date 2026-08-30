"""Paper-simulation routes for the QuantDesk 8000 server."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from flask import Blueprint, Flask, jsonify, request

from scripts.server.helpers.util import clean_payload, safe_int


@dataclass(frozen=True)
class PaperSimulationRouteDeps:
    """Callbacks that keep this blueprint decoupled from quant_api_server."""

    data_write_lock: Callable[[], Any]
    paper_watchlist_json: Callable[[], Path]
    project_relative_path: Callable[[Path], str]
    load_paper_simulation_watchlist: Callable[[], dict[str, Any]]
    paper_research_decision_snapshot: Callable[[dict[str, Any]], dict[str, Any]]
    latest_research_evidence_context: Callable[[], dict[str, Any]]
    add_candidate_to_paper_simulation_pool: Callable[..., dict[str, Any]]
    sync_candidates_to_paper_simulation_pool: Callable[..., dict[str, Any]]
    refresh_paper_simulation_watchlist_tracking: Callable[[], dict[str, Any]]
    paper_simulation_lifecycle_action: Callable[..., dict[str, Any]]
    request_truthy: Callable[[Any], bool]
    log_warning: Callable[..., None]


def _busy_payload() -> dict[str, Any]:
    return {
        "ok": False,
        "status": "busy",
        "message": "另一项本地数据写入正在进行，请稍后再试。",
        "message_for_ui": "另一项本地数据写入正在进行，请稍后再试。",
        "research_only": True,
        "paper_only": True,
        "no_broker": True,
    }


def create_paper_simulation_blueprint(deps: PaperSimulationRouteDeps) -> Blueprint:
    """Create paper-account simulation routes used by the active 8000 UI."""

    bp = Blueprint("paper_simulation_api", __name__)

    @bp.route("/api/paper-simulation/watchlist", methods=["GET"])
    def get_paper_simulation_watchlist() -> Any:
        payload = deps.load_paper_simulation_watchlist()
        payload["path"] = deps.project_relative_path(deps.paper_watchlist_json())
        items = [item for item in payload.get("items") or [] if isinstance(item, dict)]
        # Attach the read-only research-decision snapshot (LLM opinion when present,
        # otherwise a clearly-labelled local evidence-based fallback) so the paper
        # page shows a real decision + source for every sample instead of a bare
        # pending_discussion. This does not write to disk.
        for item in items:
            try:
                item["research_decision"] = deps.paper_research_decision_snapshot(item)
            except Exception as exc:  # noqa: BLE001 - decision snapshot is enrichment
                deps.log_warning(
                    "paper_watchlist_decision_snapshot_failed: symbol=%s error=%s",
                    item.get("symbol"),
                    exc,
                )
        active_items = [item for item in items if str(item.get("paper_lifecycle_state") or "") != "archived"]
        payload["watchlist_count"] = len(items)
        payload["active_watchlist_count"] = len(active_items)
        payload["archived_watchlist_count"] = len(items) - len(active_items)
        payload["research_evidence"] = deps.latest_research_evidence_context()
        payload.setdefault("message_for_ui", "纸面模拟池只用于本地学习跟踪；不接券商、不下真实订单。")
        return jsonify(clean_payload(payload)), 200 if payload.get("ok", True) else 500

    @bp.route("/api/paper-simulation/equity-history", methods=["GET"])
    def get_paper_simulation_equity_history() -> Any:
        """Read-only per-day paper equity series for the home dashboard."""
        try:
            from quantdesk.paper.auto_paper_portfolio import auto_paper_portfolio_equity_history

            payload = auto_paper_portfolio_equity_history(limit=safe_int(request.args.get("limit"), 250))
            return jsonify(clean_payload(payload)), 200 if payload.get("ok", True) else 500
        except Exception as exc:  # noqa: BLE001
            return jsonify(
                clean_payload(
                    {
                        "ok": False,
                        "message_for_ui": f"净值历史读取失败：{exc}",
                        "error": str(exc),
                        "research_only": True,
                        "paper_only": True,
                        "no_broker": True,
                    }
                )
            ), 500

    @bp.route("/api/paper-simulation/add-candidate", methods=["POST"])
    def post_paper_simulation_add_candidate() -> Any:
        lock = deps.data_write_lock()
        if not lock.acquire(blocking=False):
            return jsonify(clean_payload(_busy_payload())), 409
        try:
            body = request.get_json(silent=True) or {}
            payload = deps.add_candidate_to_paper_simulation_pool(
                str(body.get("symbol") or ""),
                entry_date=str(body.get("entry_date") or ""),
                lane=str(body.get("lane") or ""),
                note=str(body.get("note") or ""),
                llm_research_opinion=str(body.get("llm_research_opinion") or ""),
                llm_decision_status=str(body.get("llm_decision_status") or ""),
                llm_primary_action=str(body.get("llm_primary_action") or ""),
                llm_confidence=str(body.get("llm_confidence") or ""),
                llm_validation_days=body.get("llm_validation_days"),
                llm_success_condition=str(body.get("llm_success_condition") or ""),
                llm_failure_condition=str(body.get("llm_failure_condition") or ""),
                llm_missing_data=str(body.get("llm_missing_data") or ""),
                llm_review_sentence=str(body.get("llm_review_sentence") or ""),
            )
            return jsonify(clean_payload(payload)), 200 if payload.get("ok") else 400
        finally:
            lock.release()

    @bp.route("/api/paper-simulation/sync-candidates", methods=["POST"])
    def post_paper_simulation_sync_candidates() -> Any:
        lock = deps.data_write_lock()
        if not lock.acquire(blocking=False):
            return jsonify(clean_payload(_busy_payload())), 409
        try:
            body = request.get_json(silent=True) or {}
            payload = deps.sync_candidates_to_paper_simulation_pool(
                include_observation=deps.request_truthy(body.get("include_observation")),
                max_candidates=safe_int(body.get("max_candidates"), 10),
            )
            return jsonify(clean_payload(payload)), 200 if payload.get("ok") else 207
        finally:
            lock.release()

    @bp.route("/api/paper-simulation/refresh", methods=["POST"])
    def post_paper_simulation_refresh() -> Any:
        lock = deps.data_write_lock()
        if not lock.acquire(blocking=False):
            return jsonify(clean_payload(_busy_payload())), 409
        try:
            payload = deps.refresh_paper_simulation_watchlist_tracking()
            return jsonify(clean_payload(payload)), 200 if payload.get("ok") else 500
        finally:
            lock.release()

    @bp.route("/api/paper-simulation/action", methods=["POST"])
    def post_paper_simulation_action() -> Any:
        lock = deps.data_write_lock()
        if not lock.acquire(blocking=False):
            return jsonify(clean_payload(_busy_payload())), 409
        try:
            body = request.get_json(silent=True) or {}
            payload = deps.paper_simulation_lifecycle_action(
                str(body.get("symbol") or ""),
                str(body.get("action") or ""),
                note=str(body.get("note") or ""),
                watchlist_id=str(body.get("watchlist_id") or ""),
            )
            return jsonify(clean_payload(payload)), 200 if payload.get("ok") else 400
        finally:
            lock.release()

    return bp


def register_paper_simulation_routes(app: Flask, deps: PaperSimulationRouteDeps) -> None:
    app.register_blueprint(create_paper_simulation_blueprint(deps))
