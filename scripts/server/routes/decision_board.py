"""Read-only Decision Board route for the active QuantDesk port-8000 UI."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from typing import Any, Callable, Sequence

from flask import Blueprint, Flask, jsonify

from quantdesk.decision_board import build_decision_board_snapshot, _date_text
from quantdesk.data_chain_consumer_gate import (
    evaluate_data_chain_consumer_gate,
)
from quantdesk.trader_profile import ResearchPaperProfile
from quantdesk.trader_profile import default_research_paper_profile
from scripts.server.helpers.util import clean_payload


@dataclass(frozen=True)
class DecisionBoardRouteDeps:
    load_watchlist: Callable[[], dict[str, Any]]
    load_portfolio: Callable[[], dict[str, Any]]
    load_equity_history: Callable[[], dict[str, Any]]
    load_benchmark: Callable[[], dict[str, Any]]
    load_profile: Callable[[], ResearchPaperProfile]
    load_calendar_days: Callable[[], Sequence[str]]
    strategy_version: Callable[[], str]
    source_paths: Callable[[], dict[str, str]]


def create_decision_board_blueprint(deps: DecisionBoardRouteDeps) -> Blueprint:
    bp = Blueprint("decision_board_api", __name__)

    @bp.route("/api/decision-board", methods=["GET"])
    def get_decision_board() -> Any:
        try:
            profile = deps.load_profile()
            watchlist, portfolio, history = deps.load_watchlist(), deps.load_portfolio(), deps.load_equity_history()
            as_of = max((str(value.get(key) or "") for value in (watchlist, portfolio, history)
                for key in ("as_of", "generated_at", "updated_at", "last_trade_date")), default="")
            items = [*portfolio.get("positions", []), *watchlist.get("items", [])]
            starts = [_date_text(item.get("entry_date") or item.get("buy_date") or item.get("added_at"))
                for item in items if isinstance(item, dict)]
            ends = [_date_text(item.get("latest_date") or item.get("latest_reference_date") or as_of)
                for item in items if isinstance(item, dict)]
            start, end = min((d for d in starts if d), default=""), max((d for d in ends if d), default="")
            all_days = deps.load_calendar_days()
            # Holding-period calculations only use this inclusive range. Parse
            # the unchanged original calendar once instead of once per item.
            days = [_date_text(day) for day in all_days]
            calendar_days = [day for day in days if day and (not start or day >= start) and (not end or day <= end)]
            payload = build_decision_board_snapshot(
                watchlist_payload=watchlist,
                portfolio_payload=portfolio,
                equity_history_payload=history,
                benchmark_payload=deps.load_benchmark(),
                profile=profile,
                calendar_days=calendar_days,
                strategy_version=deps.strategy_version(),
                source_paths=deps.source_paths(),
            )
            # The generic first-run template does not declare an existing book's
            # funded capital. Display the book's own amount without resetting it
            # or changing any of the profile's risk ceilings.
            if profile == default_research_paper_profile():
                envelope = payload.get("envelope") or {}
                public = envelope.get("profile") or {}
                account = payload.get("account") if isinstance(payload.get("account"), dict) else {}
                if account.get("initial_cash") is not None and account["initial_cash"] > 0:
                    public.update(configured_template_capital=public.get("paper_capital"),
                        paper_capital=account["initial_cash"], capital_source="existing_strategy_ledger",
                        capital_alignment="ledger_authoritative")
                    payload["warnings"] = [w for w in payload.get("warnings", []) if w != "profile_capital_mismatch"]
                    payload["status"] = "degraded" if payload["warnings"] else "passed"
                    # Keep the response manifest bound to the displayed profile.
                    manifest = envelope.get("manifest") or {}
                    hashes = manifest.get("input_hashes") or {}
                    def digest(value: Any) -> str:
                        raw = json.dumps(value, ensure_ascii=False, sort_keys=True,
                            separators=(",", ":"), default=str)
                        return hashlib.sha256(raw.encode("utf-8")).hexdigest()
                    hashes["profile"] = digest(public)
                    manifest["manifest_id"] = digest(hashes)
            consumer_gate = evaluate_data_chain_consumer_gate()
            payload["data_chain_gate"] = {
                key: consumer_gate.get(key)
                for key in (
                    "status",
                    "allowed",
                    "authoritative",
                    "run_id",
                    "target_date",
                    "reasons",
                    "chain_status",
                )
            }
            if not consumer_gate.get("allowed"):
                payload["status"] = "degraded"
                payload.setdefault("warnings", []).append(
                    "downstream_data_chain_gate_blocked"
                )
            return jsonify(clean_payload(payload)), 200
        except Exception as exc:  # noqa: BLE001 - endpoint must degrade explicitly
            return jsonify(
                clean_payload(
                    {
                        "ok": False,
                        "status": "degraded",
                        "reason_code": "decision_board_build_failed",
                        "message_for_ui": f"决策看板后端证据聚合失败：{exc}",
                        "research_only": True,
                        "paper_only": True,
                        "no_broker": True,
                        "live_execution_locked": True,
                    }
                )
            ), 500

    return bp


def register_decision_board_routes(app: Flask, deps: DecisionBoardRouteDeps) -> None:
    app.register_blueprint(create_decision_board_blueprint(deps))


__all__ = [
    "DecisionBoardRouteDeps",
    "create_decision_board_blueprint",
    "register_decision_board_routes",
]
