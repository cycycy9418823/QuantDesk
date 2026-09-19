from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any

from quantdesk.llm_providers.base import build_llm_result
from quantdesk.shared import short_error_text


def _endpoint(base_url: str) -> str:
    base = str(base_url or "http://localhost:11434").strip().rstrip("/")
    return f"{base}/api/generate"


def _prompt_text(prompt: Any) -> str:
    if isinstance(prompt, dict):
        return "\n\n".join(str(prompt.get(key) or "") for key in ("system_rules", "user_prompt") if prompt.get(key))
    return str(prompt or "")


def _http_post_json(url: str, payload: dict[str, Any], *, timeout: int = 60) -> dict[str, Any]:
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310 - user-triggered optional local endpoint
        raw = response.read()
    return json.loads(raw.decode("utf-8"))


def validate_provider_config(config: dict[str, Any] | None = None) -> dict[str, Any]:
    cfg = config or {}
    if not cfg.get("enabled"):
        return {"ok": False, "provider": "ollama", "status": "disabled", "errors": ["provider disabled"], "warnings": []}
    if not str(cfg.get("model") or "").strip():
        return {"ok": False, "provider": "ollama", "status": "missing_model", "errors": ["missing model"], "warnings": []}
    return {"ok": True, "provider": "ollama", "status": "configured", "errors": [], "warnings": []}


def provider_capability(config: dict[str, Any] | None = None) -> dict[str, Any]:
    validation = validate_provider_config(config)
    return {
        "provider": "ollama",
        "status": validation["status"],
        "enabled": bool((config or {}).get("enabled")),
        "network_required": True,
        "base_url": (config or {}).get("base_url") or "http://localhost:11434",
        "errors": validation.get("errors") or [],
        "suggested_next_action": "启动本地 Ollama 并配置模型后再启用。" if not validation.get("ok") else "可在用户显式允许联网时调用本地 Ollama。",
    }


def generate_observation_report(prompt: Any, context: dict[str, Any], config: dict[str, Any] | None = None, *, network: bool = False) -> dict[str, Any]:
    cfg = config or {}
    if not network:
        return build_llm_result(ok=False, status="skipped", provider="ollama", skipped_reason="network=false，未调用 LLM。")
    validation = validate_provider_config(cfg)
    if not validation.get("ok"):
        return build_llm_result(ok=False, status=str(validation.get("status") or "failed"), provider="ollama", failed_reason="; ".join(validation.get("errors") or []))
    body = {"model": cfg.get("model"), "prompt": _prompt_text(prompt), "stream": False}
    try:
        response = _http_post_json(_endpoint(str(cfg.get("base_url") or "")), body, timeout=int(cfg.get("timeout_seconds") or 60))
        text = str(response.get("response") or "")
        if not text.strip():
            return build_llm_result(ok=False, status="failed", provider="ollama", failed_reason="empty LLM response")
        return build_llm_result(ok=True, status="ok", provider="ollama", report=text, llm_used=True, fallback_used=False)
    except (TimeoutError, urllib.error.URLError, urllib.error.HTTPError) as exc:
        return build_llm_result(ok=False, status="failed", provider="ollama", failed_reason=short_error_text(exc))
    except Exception as exc:  # noqa: BLE001
        return build_llm_result(ok=False, status="failed", provider="ollama", failed_reason=short_error_text(exc))
