# QuantDesk Optional LLM Observation Agent

## v3.2.1 Evidence Guard Update

v3.2.1 adds an evidence pack, grounding guard, and report audit on top of the optional LLM layer. Generated reports must stay grounded in the local observation context. If a number, date, symbol, provider status, or minute-data state is not present in the evidence pack, the report is flagged by the grounding guard.

The CLI writes `results/strategy_observation_report_audit_latest.json` by default. The audit records safe hashes and guard status fields, not API keys or raw secrets. LLM remains disabled by default and `network=false` still prevents any LLM call.

```powershell
python scripts/qd.py observation-report --symbol 600744 --target-date YYYY-MM-DD --network false --llm false --audit true
python scripts/qd.py agent-report-audit --report results/strategy_observation_report_latest.md --context results/strategy_observation_context_latest.json
```

v3.2.0 adds an optional LLM learning review agent. It is a reporting layer only.

## Defaults

- `llm_enabled=false`
- `network=false`
- default provider: `offline_stub`
- no API key required for the offline template
- no OpenAI SDK or Ollama SDK hard dependency

If configuration is missing, the API key is missing, the provider is disabled, or the call fails, QuantDesk falls back to the offline template report.

## Providers

- `offline_stub`: always available, no network.
- `openai_compatible`: OpenAI-compatible HTTP API, enabled only by explicit config and environment API key.
- `ollama`: local Ollama HTTP API, enabled only by explicit config.

Copy `config/llm_config.example.json` to `config/llm_config.json` for local experiments. Do not put real API keys in the repository. Prefer environment variables such as `QUANTDESK_LLM_API_KEY`.

## Commands

```powershell
python scripts/qd.py observation-report --symbol 600744 --target-date YYYY-MM-DD --network false --llm false
python scripts/qd.py llm-smoke --network false
python scripts/qd.py observation-report --symbol 600744 --target-date YYYY-MM-DD --network true --llm true --llm-provider ollama
python scripts/qd.py observation-report --symbol 600744 --target-date YYYY-MM-DD --network true --llm true --llm-provider openai_compatible
```

## Safety

The LLM Agent may only explain, summarize, identify data gaps, describe uncertainty, list next observation steps, and propose experiment hypotheses.

It must not output buy/sell instructions, sizing, target levels, risk-threshold levels as trading instructions, or real orders. All output passes through `agent_policy_guard`; unsafe expressions are replaced or blocked.

QuantDesk does not connect to brokers, does not place real orders, does not save real account information, and does not constitute investment advice.

中文边界：本模块仅用于学习研究，不接券商、不真实下单、不保存真实账户信息、不构成投资建议。
