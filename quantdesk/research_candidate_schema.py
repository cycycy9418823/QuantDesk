from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

PROHIBITED_USAGE = {
    "live_trading",
    "broker_connection",
    "auto_execution",
    "buy_sell_recommendation",
    "real_money_allocation_instruction",
}
ALLOWED_USAGE = {
    "research_dashboard",
    "paper_research",
    "simulation",
    "risk_monitoring",
    "human_review",
}
VALID_MONITORING_STATUS = {
    "candidate_healthy",
    "candidate_watch",
    "candidate_degraded",
    "human_review_required",
    "data_issue",
}
CANDIDATE_ID = "portfolio_low_vol_rev120_candidate"


def load_json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as fh:
        return json.load(fh)


def _has_bad_number(value: Any) -> bool:
    if isinstance(value, float):
        return math.isnan(value) or math.isinf(value)
    if isinstance(value, dict):
        return any(_has_bad_number(v) for v in value.values())
    if isinstance(value, list):
        return any(_has_bad_number(v) for v in value)
    return False


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and not _has_bad_number(float(value))


def validate_registry(payload: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    if payload.get("registry_id") != "research_candidate_registry":
        errors.append("registry_id mismatch")
    if payload.get("research_only") is not True:
        errors.append("registry research_only must be true")
    candidates = payload.get("candidate_pool")
    if not isinstance(candidates, list) or not candidates:
        errors.append("candidate_pool missing")
        return errors
    candidate = candidates[0]
    if candidate.get("candidate_id") != CANDIDATE_ID:
        errors.append("candidate_id mismatch")
    if not ALLOWED_USAGE.issubset(set(candidate.get("allowed_usage", []))):
        errors.append("allowed_usage incomplete")
    if not PROHIBITED_USAGE.issubset(set(candidate.get("prohibited_usage", []))):
        errors.append("prohibited_usage incomplete")
    if not candidate.get("source_reports"):
        errors.append("source_reports missing")
    if not candidate.get("next_review_trigger"):
        errors.append("next_review_trigger missing")
    return errors


def validate_monitoring(payload: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    if payload.get("candidate_id") != CANDIDATE_ID:
        errors.append("candidate_id mismatch")
    if payload.get("research_only") is not True:
        errors.append("monitoring research_only must be true")
    if payload.get("monitoring_status") not in VALID_MONITORING_STATUS:
        errors.append("monitoring_status invalid")
    if not PROHIBITED_USAGE.issubset(set(payload.get("prohibited_usage", []))):
        errors.append("prohibited_usage incomplete")
    if "warnings" not in payload:
        errors.append("warnings missing")
    if "human_review_required_reasons" not in payload:
        errors.append("human_review_required_reasons missing")
    metrics = payload.get("metrics", {})
    for key in ["annualized_return", "Sharpe", "Sortino", "daily_MDD", "monthly_MDD"]:
        if key not in metrics or not _is_number(metrics.get(key)):
            errors.append(f"metric invalid: {key}")
    if _has_bad_number(payload):
        errors.append("NaN/inf present")
    return errors


def validate_dashboard_cards(payload: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    if payload.get("research_only") is not True:
        errors.append("dashboard cards research_only must be true")
    if not PROHIBITED_USAGE.issubset(set(payload.get("prohibited_usage", []))):
        errors.append("prohibited_usage incomplete")
    cards = payload.get("cards")
    if not isinstance(cards, list) or not cards:
        errors.append("cards missing")
        return errors
    card = cards[0]
    if card.get("candidate_id") != CANDIDATE_ID:
        errors.append("candidate_id mismatch")
    if card.get("monitoring_status") not in VALID_MONITORING_STATUS:
        errors.append("monitoring_status invalid")
    if not ALLOWED_USAGE.issubset(set(card.get("allowed_usage", []))):
        errors.append("allowed_usage incomplete")
    if not PROHIBITED_USAGE.issubset(set(card.get("prohibited_usage", []))):
        errors.append("card prohibited_usage incomplete")
    if not card.get("source_files"):
        errors.append("source_files missing")
    for key in ["annualized_return", "sharpe", "sortino", "daily_mdd", "monthly_mdd"]:
        if key not in card or not _is_number(card.get(key)):
            errors.append(f"card metric invalid: {key}")
    return errors


def validate_watchlist_cards(payload: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    if payload.get("research_only") is not True:
        errors.append("watchlist cards research_only must be true")
    cards = payload.get("watchlist_cards")
    if not isinstance(cards, list) or len(cards) < 5:
        errors.append("watchlist_cards missing or too short")
    return errors


def validate_state(payload: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    state = payload.get("final_state", {})
    if CANDIDATE_ID not in state.get("candidate_pool", []):
        errors.append("candidate_pool missing portfolio candidate")
    if state.get("risk_overlay_watchlist") not in ([], None):
        errors.append("risk_overlay_watchlist expected empty")
    return errors


def validate_history(payload: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    if "iteration" not in payload:
        errors.append("history iteration missing")
    if payload.get("monitoring_status") not in VALID_MONITORING_STATUS and payload.get("monitoring_status") is not None:
        errors.append("history monitoring_status invalid")
    return errors


def validate_file(path: Path, kind: str) -> dict[str, Any]:
    if not path.exists():
        return {"path": str(path), "kind": kind, "valid": False, "errors": ["missing file"]}
    try:
        payload = load_json(path)
    except Exception as exc:
        return {"path": str(path), "kind": kind, "valid": False, "errors": [f"json parse failed: {exc}"]}
    validators = {
        "registry": validate_registry,
        "monitoring": validate_monitoring,
        "dashboard_cards": validate_dashboard_cards,
        "watchlist_cards": validate_watchlist_cards,
        "state": validate_state,
        "history": validate_history,
    }
    errors = validators[kind](payload)
    return {"path": str(path), "kind": kind, "valid": not errors, "errors": errors}

