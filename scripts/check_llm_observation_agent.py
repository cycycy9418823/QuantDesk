from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from quantdesk.agent.agent_safe_bridge import generate_llm_observation_report  # noqa: E402
from quantdesk.llm_config import (  # noqa: E402
    get_llm_provider_config,
    load_llm_config,
    safe_config_for_output,
)
from quantdesk.llm_providers import offline_stub_provider, ollama_provider, openai_compatible_provider  # noqa: E402
from quantdesk.shared import now_text, truthy, write_json  # noqa: E402

RESULT_PATH = PROJECT_ROOT / "results" / "llm_observation_agent_smoke_latest.json"
PROVIDER_MODULES = {
    "offline_stub": offline_stub_provider,
    "openai_compatible": openai_compatible_provider,
    "ollama": ollama_provider,
}


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Check optional QuantDesk LLM observation agent capability.")
    parser.add_argument("--llm-provider", default="", choices=["", "offline_stub", "openai_compatible", "ollama"])
    parser.add_argument("--llm-config", default="")
    parser.add_argument("--network", default="false")
    parser.add_argument("--output", default=str(RESULT_PATH))
    return parser.parse_args(argv)


def _sample_context() -> dict[str, Any]:
    return {
        "schema_version": "3.2.0",
        "symbol": "600744",
        "date": "2026-05-19",
        "purpose": "learning_observation",
        "not_investment_advice": True,
        "data_health": {"status": "yellow", "summary_text": "本地样例用于 LLM smoke，不代表真实行情。"},
        "strategy_outputs": {"normalized_trigger_status": "observation_only"},
        "intraday_status": {"status": "cannot_verify_intraday_trigger", "explanation": "缺少分钟线，不能验证盘中触发。"},
        "chanlun": {"fractals": {"exists": False}, "bi": {"exists": False}, "segments": {"exists": False}},
        "warnings": ["smoke context only"],
        "data_gaps": ["sample_data_only"],
        "allowed_agent_actions": ["explain", "summarize", "risk_notes"],
        "forbidden_agent_actions": ["buy_recommendation", "sell_recommendation", "real_order"],
        "disclaimer": "仅用于本地学习研究，不构成投资建议。",
    }


def _capability(provider: str, config: dict[str, Any]) -> dict[str, Any]:
    provider_config = get_llm_provider_config(provider, config)
    module = PROVIDER_MODULES[provider]
    capability = module.provider_capability(provider_config)
    capability["provider"] = provider
    capability["api_key_value_exposed"] = False
    env_name = provider_config.get("api_key_env")
    if env_name:
        capability["api_key_present"] = bool(os.environ.get(str(env_name)))
    return capability


def run(args: argparse.Namespace) -> dict[str, Any]:
    network = truthy(args.network)
    config = load_llm_config(args.llm_config or None)
    providers = [args.llm_provider] if args.llm_provider else ["offline_stub", "openai_compatible", "ollama"]
    capability_results = [_capability(provider, config) for provider in providers]
    sample_result: dict[str, Any] | None = None
    if network and args.llm_provider:
        sample_config = dict(config)
        sample_config["llm_enabled"] = True
        sample_config["default_provider"] = args.llm_provider
        sample_result = generate_llm_observation_report(_sample_context(), sample_config, network=True)
        sample_result = {key: value for key, value in sample_result.items() if key != "raw_response"}
    payload = {
        "ok": True,
        "schema_version": "3.2.0",
        "task_type": "llm_observation_agent_smoke",
        "generated_at": now_text(),
        "network": network,
        "selected_provider": args.llm_provider or "all",
        "config": safe_config_for_output(config),
        "provider_capabilities": capability_results,
        "sample_result": sample_result,
        "disclaimer": "LLM Agent 仅用于学习复盘，不构成投资建议；network=false 时不会调用任何 LLM。",
    }
    output = Path(args.output)
    if not output.is_absolute():
        output = PROJECT_ROOT / output
    write_json(output, payload)
    return payload


def main(argv: list[str] | None = None) -> int:
    payload = run(_parse_args(argv))
    print("QuantDesk LLM Observation Agent Smoke")
    print(f"network: {payload.get('network')}")
    print(f"selected_provider: {payload.get('selected_provider')}")
    for item in payload.get("provider_capabilities") or []:
        print(
            f"- {item.get('provider')}: status={item.get('status')}, "
            f"enabled={item.get('enabled')}, api_key_present={item.get('api_key_present', '-')}, "
            f"reason={item.get('failed_reason') or item.get('skipped_reason') or '-'}"
        )
    sample = payload.get("sample_result") or {}
    if sample:
        print(
            f"sample: provider={sample.get('provider')}, llm_used={sample.get('llm_used')}, "
            f"fallback_used={sample.get('fallback_used')}, policy_guard={sample.get('policy_guard_status')}"
        )
    print("仅用于学习研究，不构成投资建议；不会打印 API Key。")
    return 0 if payload.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
