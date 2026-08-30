"""Read-only Agent panel routes for the QuantDesk 8000 server."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from flask import Blueprint, Flask, Response, jsonify, request

from scripts.server.helpers.util import clean_payload


@dataclass(frozen=True)
class AgentRouteDeps:
    """Callbacks that keep this blueprint decoupled from quant_api_server."""

    project_root: Callable[[], Path]
    build_agent_chat_context: Callable[[], dict[str, Any]]
    sanitize_agent_client_context: Callable[[dict[str, Any]], dict[str, Any]]
    attach_focused_candidate_context: Callable[[dict[str, Any], dict[str, Any]], None]
    request_truthy: Callable[[Any], bool]


def create_agent_blueprint(deps: AgentRouteDeps) -> Blueprint:
    """Create the read-only Agent panel shell and context routes."""

    bp = Blueprint("agent_api", __name__)

    @bp.route("/agent")
    def agent_chat_page() -> Response:
        """Compatibility route for the evidence-grounded observation chat."""
        html = (deps.project_root() / "app" / "agent_chat.html").read_text(encoding="utf-8")
        return Response(html, mimetype="text/html; charset=utf-8")

    @bp.route("/api/agent/skills", methods=["GET"])
    def agent_skills() -> Any:
        """Skills list + (secret-free) LLM config status for the chat UI."""
        from quantdesk.agent.observation_chat import list_skills
        from quantdesk.llm_config import load_llm_config, safe_config_for_output, validate_llm_config

        config = load_llm_config()
        return jsonify(
            {
                "ok": True,
                "skills": list_skills(),
                "llm": validate_llm_config(config),
                "config": safe_config_for_output(config),
            }
        )

    @bp.route("/api/agent/context", methods=["GET"])
    def agent_context() -> Any:
        """Secret-free read-only context preview used by the integrated agent panel."""
        return jsonify(
            clean_payload(
                {
                    "ok": True,
                    "context": deps.build_agent_chat_context(),
                    "research_only": True,
                    "paper_only": True,
                    "no_broker": True,
                    "no_live_trading": True,
                    "no_auto_execution": True,
                    "no_buy_sell_advice": True,
                    "no_position_advice": True,
                }
            )
        )

    @bp.route("/api/agent/chat", methods=["POST"])
    def agent_chat() -> Any:
        """One grounded, guarded observation-chat turn.

        The model only sees read-only local evidence; replies are policy-guarded
        and grounding-checked by quantdesk.agent.observation_chat. The endpoint
        degrades to a safe local message when LLM is off/unconfigured.
        """
        from quantdesk.agent.observation_chat import observation_chat
        from quantdesk.llm_config import load_llm_config

        body = request.get_json(silent=True) or {}
        question = str(body.get("question") or body.get("message") or "").strip()
        if not question:
            return jsonify({"ok": False, "status": "empty_question", "reply": "请输入要问的问题。", "llm_used": False})
        history = body.get("history") if isinstance(body.get("history"), list) else []
        skill = body.get("skill") or None
        network = deps.request_truthy(body.get("network") or body.get("explicit_network"))
        client_context = deps.sanitize_agent_client_context(
            body.get("context") if isinstance(body.get("context"), dict) else {}
        )
        context = deps.build_agent_chat_context()
        context.update(client_context)
        deps.attach_focused_candidate_context(context, client_context)
        result = observation_chat(question, context, history, llm_config=load_llm_config(), network=network, skill=skill)
        return jsonify(clean_payload(result))

    return bp


def register_agent_routes(app: Flask, deps: AgentRouteDeps) -> None:
    app.register_blueprint(create_agent_blueprint(deps))
