"""Configuration helpers for optional observation LLM providers.

LLM support is off by default.  Missing config files, missing API keys, and
disabled providers must never break QuantDesk; callers can always fall back to
the offline template report.
"""

from __future__ import annotations

import json
import os
from copy import deepcopy
from pathlib import Path
from typing import Any

from quantdesk.shared import read_json, short_error_text, truthy

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_LLM_CONFIG_PATH = PROJECT_ROOT / "config" / "llm_config.json"


DEFAULT_LLM_CONFIG: dict[str, Any] = {
    "llm_enabled": False,
    "default_provider": "offline_stub",
    "providers": {
        "offline_stub": {"enabled": True, "type": "offline_stub"},
        "openai_compatible": {
            "enabled": False,
            "type": "openai_compatible",
            "base_url": "",
            "api_key_env": "QUANTDESK_LLM_API_KEY",
            "model": "",
            "timeout_seconds": 30,
        },
        "ollama": {
            "enabled": False,
            "type": "ollama",
            "base_url": "http://localhost:11434",
            "model": "qwen2.5:7b",
            "timeout_seconds": 60,
        },
    },
}


def _merge_config(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    merged = deepcopy(base)
    for key, value in (override or {}).items():
        if key == "providers" and isinstance(value, dict):
            providers = merged.setdefault("providers", {})
            for provider_name, provider_value in value.items():
                current = providers.get(provider_name, {})
                if isinstance(current, dict) and isinstance(provider_value, dict):
                    updated = dict(current)
                    updated.update(provider_value)
                    providers[provider_name] = updated
                else:
                    providers[provider_name] = provider_value
        else:
            merged[key] = value
    return merged


def load_llm_config(path: str | Path | None = None) -> dict[str, Any]:
    config_path = Path(path) if path else DEFAULT_LLM_CONFIG_PATH
    payload = read_json(config_path, {})
    config = _merge_config(DEFAULT_LLM_CONFIG, payload if isinstance(payload, dict) else {})

    env_enabled = os.environ.get("QUANTDESK_LLM_ENABLED")
    if env_enabled is not None:
        config["llm_enabled"] = truthy(env_enabled)
    env_provider = os.environ.get("QUANTDESK_LLM_PROVIDER")
    if env_provider:
        config["default_provider"] = env_provider.strip()
    providers = config.setdefault("providers", {})
    openai_cfg = providers.setdefault("openai_compatible", {})
    if os.environ.get("QUANTDESK_LLM_BASE_URL"):
        openai_cfg["base_url"] = os.environ["QUANTDESK_LLM_BASE_URL"].strip()
    if os.environ.get("QUANTDESK_LLM_MODEL"):
        openai_cfg["model"] = os.environ["QUANTDESK_LLM_MODEL"].strip()
    config["config_path"] = str(config_path)
    config["config_exists"] = config_path.exists()
    return config


def get_llm_provider_config(provider_name: str, config: dict[str, Any] | None = None) -> dict[str, Any]:
    cfg = config or load_llm_config()
    providers = cfg.get("providers") if isinstance(cfg.get("providers"), dict) else {}
    provider = dict(providers.get(provider_name) or {})
    provider.setdefault("name", provider_name)
    provider.setdefault("enabled", provider_name == "offline_stub")
    provider.setdefault("type", provider.get("type") or provider_name)
    return provider


def is_llm_enabled(config: dict[str, Any] | None = None) -> bool:
    cfg = config or load_llm_config()
    return bool(truthy(cfg.get("llm_enabled")))


def mask_secret(value: Any) -> str:
    text = str(value or "")
    if not text:
        return ""
    if len(text) <= 8:
        return "*" * len(text)
    return f"{text[:3]}***{text[-3:]}"


def validate_llm_config(config: dict[str, Any] | None = None) -> dict[str, Any]:
    cfg = config or load_llm_config()
    warnings: list[str] = []
    errors: list[str] = []
    providers = cfg.get("providers") if isinstance(cfg.get("providers"), dict) else {}
    default_provider = str(cfg.get("default_provider") or "offline_stub")
    if default_provider not in providers:
        errors.append(f"default provider not configured: {default_provider}")
    for name, provider in providers.items():
        if not isinstance(provider, dict):
            errors.append(f"invalid provider config: {name}")
            continue
        if name != "offline_stub" and provider.get("api_key"):
            warnings.append(f"{name} has inline api_key; prefer api_key_env")
    if is_llm_enabled(cfg):
        provider = providers.get(default_provider)
        if not isinstance(provider, dict):
            errors.append(f"default provider invalid: {default_provider}")
        elif default_provider == "offline_stub":
            warnings.append("llm_enabled=true but default_provider=offline_stub")
        elif not provider.get("enabled"):
            errors.append(f"default provider disabled: {default_provider}")
        elif default_provider == "openai_compatible":
            if not str(provider.get("base_url") or "").strip():
                errors.append("openai_compatible missing base_url")
            if not str(provider.get("model") or "").strip():
                errors.append("openai_compatible missing model")
            env_name = str(provider.get("api_key_env") or "QUANTDESK_LLM_API_KEY")
            if not (str(provider.get("api_key") or "").strip() or os.environ.get(env_name)):
                errors.append(f"openai_compatible missing api key env: {env_name}")
        elif default_provider == "ollama":
            if not str(provider.get("base_url") or "").strip():
                errors.append("ollama missing base_url")
            if not str(provider.get("model") or "").strip():
                errors.append("ollama missing model")
    return {
        "ok": not errors,
        "llm_enabled": is_llm_enabled(cfg),
        "default_provider": default_provider,
        "providers": sorted(providers.keys()),
        "warnings": [short_error_text(item) for item in warnings],
        "errors": [short_error_text(item) for item in errors],
        "config_exists": bool(cfg.get("config_exists")),
        "disclaimer": "LLM Agent 仅用于学习复盘，不构成投资建议。",
    }


def safe_config_for_output(config: dict[str, Any]) -> dict[str, Any]:
    """Return config metadata without secrets."""

    cfg = deepcopy(config)
    providers = cfg.get("providers") if isinstance(cfg.get("providers"), dict) else {}
    for provider in providers.values():
        if isinstance(provider, dict):
            if provider.get("api_key"):
                provider["api_key"] = "***"
            env_name = provider.get("api_key_env")
            if env_name:
                provider["api_key_present"] = bool(os.environ.get(str(env_name)))
    return json.loads(json.dumps(cfg, ensure_ascii=False, default=str))
