from __future__ import annotations

from typing import Any

from quantdesk.agent.agent_safe_bridge import generate_stub_observation_report
from quantdesk.llm_providers.base import build_llm_result


def provider_capability(config: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "provider": "offline_stub",
        "status": "available",
        "enabled": True,
        "network_required": False,
        "llm_used": False,
        "disclaimer": "离线模板仅用于学习复盘，不构成投资建议。",
    }


def validate_provider_config(config: dict[str, Any] | None = None) -> dict[str, Any]:
    return {"ok": True, "provider": "offline_stub", "status": "available", "warnings": [], "errors": []}


def generate_observation_report(prompt: Any, context: dict[str, Any], config: dict[str, Any] | None = None) -> dict[str, Any]:
    payload = generate_stub_observation_report(context)
    return build_llm_result(
        ok=bool(payload.get("ok")),
        status="ok" if payload.get("ok") else "failed",
        provider="offline_stub",
        report=str(payload.get("report") or ""),
        llm_used=False,
        fallback_used=True,
        policy_guard_status="passed" if payload.get("ok") else "failed",
        warnings=payload.get("warnings") or [],
    )

