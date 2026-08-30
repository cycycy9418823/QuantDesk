# QuantDesk Agent Observation Report

## v3.2.3 Strategy Validation Summary

Observation context now includes `validation_summary` when `results/strategy_validation_latest.json` exists. If validation status is `failed`, reports must surface this warning:

> 当前策略验证存在风险，学习复盘报告不应被解读为可靠策略结论。

Agent audit and evidence pack include the validation summary so a learning review cannot hide future-function or execution-assumption risks. This still does not call an LLM by default and does not provide investment advice.

## v3.2.1 Evidence Guard And Audit

v3.2.1 adds an evidence pack, grounding guard, and report audit for learning review reports. The report can only explain facts already present in `strategy_observation_context`; unsupported numbers, dates, symbols, provider claims, or minute-data contradictions are flagged by `agent_hallucination_guard`.

Default report generation writes:

- `results/strategy_observation_context_latest.json`
- `results/strategy_observation_report_latest.md`
- `results/strategy_observation_llm_result_latest.json`
- `results/strategy_observation_report_audit_latest.json`

The audit JSON includes guard statuses, grounding score, unsupported issue count, and safe hashes for the context, evidence pack, prompt, and report. It is local only, does not call an LLM, and does not contain API keys.

```powershell
python scripts/qd.py observation-report --symbol 600744 --target-date YYYY-MM-DD --network false --llm false --audit true
python scripts/qd.py agent-report-audit --report results/strategy_observation_report_latest.md --context results/strategy_observation_context_latest.json
```

v3.1.3 adds a safe bridge for future agent-assisted explanation, but it does not call a real LLM and it does not automate trading.

The bridge builds a read-only observation context from existing local files:

- data health dashboard
- provider smoke and data-source trace
- strategy sandbox and daily workflow outputs
- conditional plans and conditional execution summaries
- intraday gap status
- paper portfolio snapshots
- Chanlun fractal, bi, and segment structures

Allowed future agent actions:

- explain
- summarize
- risk notes
- data-gap notes
- next observation steps
- experiment hypotheses

Forbidden future agent actions:

- buy or sell recommendations
- position sizing
- target levels
- risk-threshold levels presented as trading instructions
- real orders
- modifying strategy output, provider data, or paper portfolio state

Run the local template report:

```powershell
python scripts/qd.py observation-report --symbol 600744 --target-date YYYY-MM-DD --network false --llm false
```

v3.2.0 adds an optional LLM learning review agent. It is disabled by default and falls back to the offline template whenever `llm_enabled=false`, `network=false`, provider config is missing, an API key is missing, or the provider call fails.

```powershell
python scripts/qd.py llm-smoke --network false
python scripts/qd.py observation-report --symbol 600744 --target-date YYYY-MM-DD --network true --llm true --llm-provider ollama
```

All LLM output passes through `agent_policy_guard`. QuantDesk does not save API keys in reports or result JSON, does not let the frontend store secrets, and does not allow the agent to fetch market data, modify strategies, modify provider data, or change paper portfolio state.

The command writes:

- `results/strategy_observation_context_latest.json`
- `results/strategy_observation_report_latest.md`
- `results/strategy_observation_llm_result_latest.json`

These runtime outputs are excluded from clean zip exports.

Boundary statement: QuantDesk is only for local learning research and paper simulation. It does not connect to brokers, does not place real orders, does not save real account information, and does not constitute investment advice.
