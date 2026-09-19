from __future__ import annotations

from typing import Any

from quantdesk.shared import now_text, short_error_text

LLM_DISCLAIMER = "LLM Agent 仅用于学习复盘，不接券商、不真实下单、不构成投资建议。"


def build_llm_result(
    *,
    ok: bool,
    status: str,
    provider: str,
    report: str = "",
    llm_used: bool = False,
    fallback_used: bool = False,
    failed_reason: str = "",
    skipped_reason: str = "",
    warnings: list[Any] | None = None,
    policy_guard_status: str = "not_checked",
    generated_at: str | None = None,
    **extra: Any,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "ok": bool(ok),
        "status": status,
        "provider": provider,
        "llm_used": bool(llm_used),
        "fallback_used": bool(fallback_used),
        "failed_reason": short_error_text(failed_reason) if failed_reason else "",
        "skipped_reason": short_error_text(skipped_reason) if skipped_reason else "",
        "warnings": [short_error_text(item) for item in (warnings or [])],
        "policy_guard_status": policy_guard_status,
        "report": report,
        "generated_at": generated_at or now_text(),
        "disclaimer": LLM_DISCLAIMER,
    }
    payload.update(extra)
    return payload


def provider_capability(config: dict[str, Any] | None = None) -> dict[str, Any]:
    cfg = config or {}
    return {
        "provider": cfg.get("name") or cfg.get("type") or "unknown",
        "enabled": bool(cfg.get("enabled")),
        "status": "configured" if cfg.get("enabled") else "disabled",
        "network_required": cfg.get("type") != "offline_stub",
        "disclaimer": LLM_DISCLAIMER,
    }


def validate_provider_config(config: dict[str, Any] | None = None) -> dict[str, Any]:
    cfg = config or {}
    return {
        "ok": bool(cfg.get("enabled")),
        "status": "configured" if cfg.get("enabled") else "disabled",
        "provider": cfg.get("name") or cfg.get("type") or "unknown",
        "warnings": [],
        "errors": [] if cfg.get("enabled") else ["provider disabled"],
    }
