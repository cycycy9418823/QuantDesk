"""Read-only AI paper-trader routes for the QuantDesk 8000 server.

Namespace: ``/api/ai-trader/*``. These routes surface the *independent* AI
trader's own paper book (``state/ai_trader/``) inside the main 8000 UI.

Boundary (non-negotiable):

- Research Only / Paper Account Only. No broker, no QMT/PTrade order route,
  no real orders, no live execution.
- The dashboard is a pure read of the trader's authoritative artifacts via
  ``ai_trader.dashboard`` (no equity/position recomputation here).
- Chat can ONLY explain. It appends to the trader's own chat log and can never
  touch any ledger, plan or journal; there is no buy/sell-in-chat endpoint.
- The evening cycle stays the single automated decision entry (20:20 scheduled
  task); these routes deliberately expose no run/replay control.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from flask import Blueprint, Flask, Response, jsonify, request

from scripts.server.helpers.util import clean_payload

MAX_CHAT_QUESTION_CHARS = 2000
DECISION_LATEST_PATH = Path(__file__).resolve().parents[3] / "state/ai_trader/decision_latest.json"

_DISCLAIMER = "纸面模拟与本地研究，不接券商、不下单、不构成投资建议。"
_CHAT_NOTE = "聊天不能下单：仓位只在每晚纸面复盘流程中变化。"


def _clean_dashboard_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Apply generic display cleanup without changing the token-null contract."""

    cleaned = clean_payload(payload)
    runtime = cleaned.get("llm_runtime") if isinstance(cleaned, dict) else None
    if isinstance(runtime, dict):
        # ``None`` normally becomes the Chinese missing-data placeholder in
        # generic dashboard payloads.  An LLM credential is different: JSON
        # null is the deliberate, secret-free public contract.
        runtime["token"] = None
    return json.loads(json.dumps(cleaned, ensure_ascii=False))


def _resolve_profile() -> tuple[str, dict[str, Any] | None]:
    """Which paper book this request asks for.

    ``?profile=glm`` selects the second book. An unknown name is refused rather
    than silently answered with the default book's data.
    """

    from ai_trader.paths import PROFILE_NAMES

    raw = str(request.args.get("profile") or "").strip().lower()
    if not raw or raw == "default":
        return "", None
    if raw not in PROFILE_NAMES:
        return raw, {
            "ok": False,
            "reason": "unknown_profile",
            "profile": raw,
            "known_profiles": ["default", *PROFILE_NAMES],
        }
    return raw, None


def _dashboard_payload(profile: str = "") -> dict[str, Any]:
    from ai_trader.dashboard import build_dashboard_payload, build_degraded_payload

    # The default book keeps calling with no args so the dashboard module's own
    # path constants stay the single source of truth (and stay patchable).
    kwargs: dict[str, Any] = {}
    if profile and profile != "default":
        from ai_trader.paths import profile_results_dir, profile_state_dir

        kwargs = {"state_dir": profile_state_dir(profile), "results_dir": profile_results_dir(profile)}
    try:
        payload = _clean_dashboard_payload(build_dashboard_payload(**kwargs))
    except Exception as exc:  # noqa: BLE001 - a viewer must never 500
        payload = _clean_dashboard_payload(
            build_degraded_payload(f"{type(exc).__name__}: {exc}")
        )
    payload["profile"] = profile or "default"
    return payload


def _dashboard_response(profile: str = "") -> Response:
    """Serialize the dashboard locally so an intentional token null survives."""

    body = json.dumps(_dashboard_payload(profile), ensure_ascii=False, allow_nan=False)
    return Response(body, mimetype="application/json")



def _decision_latest_path(profile: str = "") -> Path:
    """Default book keeps the module constant (tests swap it); a profile reads its own."""

    if not profile or profile == "default":
        return DECISION_LATEST_PATH
    from ai_trader.paths import profile_paths

    return profile_paths(profile).decision_latest


def _decisions_payload(profile: str = "") -> dict[str, Any]:
    """Read C-line results as published; viewing never scores or repairs them."""

    decision_latest = _decision_latest_path(profile)
    source_label = (
        "state/ai_trader/decision_latest.json"
        if not profile or profile == "default"
        else f"state/{decision_latest.parent.name}/decision_latest.json"
    )
    payload: dict[str, Any] = {
        "ok": False, "available": False, "status": "missing", "degraded": True,
        "source": source_label, "as_of": None,
        "count": None, "records": [], "warnings": [],
        "research_only": True, "paper_only": True, "disclaimer": "纸面 · 未验证",
        "profile": profile or "default",
    }
    try:
        def reject_constant(value: str) -> None:
            raise ValueError(f"nonfinite_json:{value}")

        raw = json.loads(decision_latest.read_text(encoding="utf-8-sig"), parse_constant=reject_constant)
    except FileNotFoundError:
        payload["warnings"] = ["尚无 AI 判断核对记录，数量未知。"]
        return payload
    except (OSError, ValueError) as exc:
        payload.update(status="unreadable", warnings=[f"AI 判断记录无法读取：{type(exc).__name__}"])
        return payload
    if not isinstance(raw, dict) or raw.get("schema") != "ai_trader_decision_v1" or not isinstance(raw.get("records"), list):
        payload.update(status="invalid", warnings=["AI 判断记录格式不符合已知版本。"])
        return payload
    records = [row for row in raw["records"] if isinstance(row, dict)
               and isinstance(row.get("decision_id"), str) and row["decision_id"].strip()]
    warnings = []
    if len(records) != len(raw["records"]):
        warnings.append(f"{len(raw['records']) - len(records)} 条记录格式损坏，未计入可读范围。")
    if raw.get("count") != len(raw["records"]):
        warnings.append("源文件记录总数与内容不符。")
    if not isinstance(raw.get("as_of"), str) or not raw["as_of"]:
        warnings.append("未提供程序复核截至日期。")
    available = bool(records) or not raw["records"]
    payload.update(ok=available, available=available, status="degraded" if warnings else "available",
                   degraded=bool(warnings), schema=raw["schema"], records=records,
                   count=len(records) if available else None, as_of=raw.get("as_of"), warnings=warnings)
    from ai_trader.decision_log import decision_review_summary

    payload["review_summary"] = decision_review_summary(records, as_of=raw.get("as_of"))
    return payload


def _chat_profile_guard() -> tuple[Any, int] | None:
    """Chat reads the default book only; refuse rather than answer from it."""

    profile, error = _resolve_profile()
    if error is not None:
        return jsonify(error), 400
    if profile:
        return (
            jsonify(
                {
                    "ok": False,
                    "reason": "chat_not_available_for_profile",
                    "detail": f"聊天面板目前只读默认账；{profile} 档请看对比报告或它自己的决策页。",
                    "profile": profile,
                    "boundary_note": _CHAT_NOTE,
                }
            ),
            501,
        )
    return None


def create_ai_trader_blueprint() -> Blueprint:
    bp = Blueprint("ai_trader_api", __name__)

    @bp.route("/api/ai-trader/dashboard", methods=["GET"])
    def ai_trader_dashboard() -> Any:
        """Read-only view of the independent AI trader's paper state."""

        profile, error = _resolve_profile()
        if error is not None:
            return jsonify(error), 400
        return _dashboard_response(profile)

    @bp.route("/api/ai-trader/decisions", methods=["GET"])
    def ai_trader_decisions() -> Response:
        profile, error = _resolve_profile()
        if error is not None:
            return jsonify(error), 400
        return Response(json.dumps(_decisions_payload(profile), ensure_ascii=False, allow_nan=False),
                        mimetype="application/json", headers={"Cache-Control": "no-store"})

    @bp.route("/api/ai-trader/chat", methods=["GET"])
    def ai_trader_chat_history() -> Any:
        blocked = _chat_profile_guard()
        if blocked is not None:
            return blocked
        from ai_trader.chat import load_history

        return jsonify(
            {
                "ok": True,
                "messages": load_history(),
                "disclaimer": _DISCLAIMER,
                "boundary_note": _CHAT_NOTE,
            }
        )

    @bp.route("/api/ai-trader/chat", methods=["POST"])
    def ai_trader_chat_send() -> Any:
        """One explanation turn. Cannot place, change or preview orders."""

        blocked = _chat_profile_guard()
        if blocked is not None:
            return blocked
        from ai_trader.chat import MAX_QUESTION_CHARS, answer_question

        body = request.get_json(silent=True) or {}
        question = str(body.get("question") or "").strip()
        if not question:
            return (
                jsonify({"ok": False, "reason": "empty_question", "boundary_note": _CHAT_NOTE}),
                400,
            )
        limit = min(int(MAX_QUESTION_CHARS), MAX_CHAT_QUESTION_CHARS)
        if len(question) > limit:
            return (
                jsonify(
                    {
                        "ok": False,
                        "reason": "question_too_long",
                        "max_chars": limit,
                        "boundary_note": _CHAT_NOTE,
                    }
                ),
                400,
            )

        diagnostics: dict[str, Any] = {}
        result = answer_question(question, diagnostics)
        if result is None:
            # Structured 503 with the REAL reason: "brain unreachable" must
            # never be dressed up as an answer.
            return (
                jsonify(
                    {
                        "ok": False,
                        "reason": diagnostics.get("reason") or "llm_unavailable",
                        "detail": clean_payload(diagnostics),
                        "disclaimer": _DISCLAIMER,
                        "boundary_note": _CHAT_NOTE,
                    }
                ),
                503,
            )
        return jsonify(
            {
                "ok": True,
                **result,
                "disclaimer": _DISCLAIMER,
                "boundary_note": _CHAT_NOTE,
            }
        )

    @bp.route("/api/ai-trader/boundary", methods=["GET"])
    def ai_trader_boundary() -> Any:
        """Machine-readable boundary block shared by all ai-trader payloads."""

        profile, error = _resolve_profile()
        if error is not None:
            return jsonify(error), 400
        payload = _dashboard_payload(profile)
        return jsonify({"ok": True, "profile": profile or "default", "boundary": payload.get("boundary")})

    return bp


def register_ai_trader_routes(app: Flask) -> None:
    app.register_blueprint(create_ai_trader_blueprint())


__all__ = [
    "MAX_CHAT_QUESTION_CHARS",
    "create_ai_trader_blueprint",
    "register_ai_trader_routes",
]
