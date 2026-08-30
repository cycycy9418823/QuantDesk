from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import Any

from quantdesk.llm_providers.base import build_llm_result
from quantdesk.shared import short_error_text


def _api_key(config: dict[str, Any]) -> str:
    env_name = str(config.get("api_key_env") or "QUANTDESK_LLM_API_KEY")
    return str(os.environ.get(env_name) or config.get("api_key") or "")


def _endpoint(base_url: str) -> str:
    base = str(base_url or "").strip().rstrip("/")
    if not base:
        return ""
    if base.endswith("/chat/completions"):
        return base
    return f"{base}/chat/completions"


def _prompt_text(prompt: Any) -> str:
    if isinstance(prompt, dict):
        return "\n\n".join(str(prompt.get(key) or "") for key in ("system_rules", "user_prompt") if prompt.get(key))
    return str(prompt or "")


def _http_post_json(url: str, payload: dict[str, Any], *, api_key: str, timeout: int = 30) -> dict[str, Any]:
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    headers = {
        "Content-Type": "application/json",
        "User-Agent": "QuantDesk/1.0",
    }
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    request = urllib.request.Request(url, data=data, headers=headers, method="POST")
    with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310 - user-triggered optional endpoint
        raw = response.read()
    return json.loads(raw.decode("utf-8"))


def validate_provider_config(config: dict[str, Any] | None = None) -> dict[str, Any]:
    cfg = config or {}
    if not cfg.get("enabled"):
        return {"ok": False, "provider": "openai_compatible", "status": "disabled", "errors": ["provider disabled"], "warnings": []}
    if not _endpoint(str(cfg.get("base_url") or "")):
        return {"ok": False, "provider": "openai_compatible", "status": "missing_base_url", "errors": ["missing base_url"], "warnings": []}
    if not str(cfg.get("model") or "").strip():
        return {"ok": False, "provider": "openai_compatible", "status": "missing_model", "errors": ["missing model"], "warnings": []}
    if not _api_key(cfg):
        return {"ok": False, "provider": "openai_compatible", "status": "missing_api_key", "errors": ["missing api key"], "warnings": []}
    return {"ok": True, "provider": "openai_compatible", "status": "configured", "errors": [], "warnings": []}


def provider_capability(config: dict[str, Any] | None = None) -> dict[str, Any]:
    validation = validate_provider_config(config)
    return {
        "provider": "openai_compatible",
        "status": validation["status"],
        "enabled": bool((config or {}).get("enabled")),
        "api_key_present": bool(_api_key(config or {})),
        "network_required": True,
        "errors": validation.get("errors") or [],
        "suggested_next_action": "配置 base_url、model 和 API Key 环境变量后再启用。" if not validation.get("ok") else "可在用户显式允许联网时调用。",
    }


def generate_observation_report(prompt: Any, context: dict[str, Any], config: dict[str, Any] | None = None, *, network: bool = False) -> dict[str, Any]:
    cfg = config or {}
    if not network:
        return build_llm_result(ok=False, status="skipped", provider="openai_compatible", skipped_reason="network=false，未调用 LLM。")
    validation = validate_provider_config(cfg)
    if not validation.get("ok"):
        return build_llm_result(ok=False, status=str(validation.get("status") or "failed"), provider="openai_compatible", failed_reason="; ".join(validation.get("errors") or []))
    url = _endpoint(str(cfg.get("base_url") or ""))
    api_key = _api_key(cfg)
    # Extract system_rules from the prompt bundle if available; fall back to a
    # generic prompt only when the bundle is a plain string (legacy callers).
    system_content = "You are a read-only learning review assistant. Do not provide investment advice."
    if isinstance(prompt, dict):
        system_content = str(prompt.get("system_rules") or system_content)
    user_content = _prompt_text(prompt)
    body = {
        "model": cfg.get("model"),
        "messages": [
            {"role": "system", "content": system_content},
            {"role": "user", "content": user_content},
        ],
        "temperature": float(cfg.get("temperature", 0.2)),
    }
    try:
        response = _http_post_json(url, body, api_key=api_key, timeout=int(cfg.get("timeout_seconds") or 30))
        choices = response.get("choices") if isinstance(response.get("choices"), list) else []
        message = choices[0].get("message") if choices and isinstance(choices[0], dict) else {}
        text = str(message.get("content") if isinstance(message, dict) else "")
        if not text.strip():
            return build_llm_result(ok=False, status="failed", provider="openai_compatible", failed_reason="empty LLM response")
        return build_llm_result(ok=True, status="ok", provider="openai_compatible", report=text, llm_used=True, fallback_used=False)
    except (TimeoutError, urllib.error.URLError, urllib.error.HTTPError) as exc:
        return build_llm_result(ok=False, status="failed", provider="openai_compatible", failed_reason=short_error_text(exc))
    except Exception as exc:  # noqa: BLE001
        return build_llm_result(ok=False, status="failed", provider="openai_compatible", failed_reason=short_error_text(exc))


def chat_completion(messages: list[dict[str, Any]], config: dict[str, Any] | None = None, *, network: bool = False) -> dict[str, Any]:
    """Multi-turn chat for the interactive observation assistant.

    Posts the full ``messages`` list (system / context / history / user) to the
    OpenAI-compatible ``/chat/completions`` endpoint. Gated exactly like
    ``generate_observation_report``: skipped when offline, validated before any
    network call, errors degrade to a structured failure (never raise).
    """
    cfg = config or {}
    if not network:
        return build_llm_result(ok=False, status="skipped", provider="openai_compatible", skipped_reason="network=false，未调用 LLM。")
    validation = validate_provider_config(cfg)
    if not validation.get("ok"):
        return build_llm_result(ok=False, status=str(validation.get("status") or "failed"), provider="openai_compatible", failed_reason="; ".join(validation.get("errors") or []))
    url = _endpoint(str(cfg.get("base_url") or ""))
    api_key = _api_key(cfg)
    body = {
        "model": cfg.get("model"),
        "messages": [dict(message) for message in (messages or [])],
        "temperature": float(cfg.get("temperature", 0.2)),
    }
    try:
        response = _http_post_json(url, body, api_key=api_key, timeout=int(cfg.get("timeout_seconds") or 30))
        choices = response.get("choices") if isinstance(response.get("choices"), list) else []
        message = choices[0].get("message") if choices and isinstance(choices[0], dict) else {}
        text = str(message.get("content") if isinstance(message, dict) else "")
        if not text.strip():
            return build_llm_result(ok=False, status="failed", provider="openai_compatible", failed_reason="empty LLM response")
        return build_llm_result(ok=True, status="ok", provider="openai_compatible", report=text, llm_used=True)
    except (TimeoutError, urllib.error.URLError, urllib.error.HTTPError) as exc:
        return build_llm_result(ok=False, status="failed", provider="openai_compatible", failed_reason=short_error_text(exc))
    except Exception as exc:  # noqa: BLE001
        return build_llm_result(ok=False, status="failed", provider="openai_compatible", failed_reason=short_error_text(exc))
