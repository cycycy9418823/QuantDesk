# Agent Evidence Guard

v3.2.1 adds an evidence guard and report quality audit for QuantDesk learning review reports.

## What It Does

- Builds an `evidence_pack` from the local `strategy_observation_context`.
- Lets the optional LLM report use only facts present in that evidence pack.
- Checks generated reports for unsupported numbers, dates, symbols, provider claims, and contradictions.
- Writes audit metadata with safe hashes for context, evidence pack, prompt, and report.
- Keeps the policy guard active before the grounding guard.

## Defaults

- LLM remains disabled by default.
- `network=false` remains the default.
- No provider calls are made by the Agent layer.
- Missing data must be described as missing.
- Reports are learning review output only and do not constitute investment advice.

## Commands

```powershell
python scripts/qd.py observation-report --symbol 600744 --target-date YYYY-MM-DD --network false --llm false --audit true
python scripts/qd.py agent-report-audit --report results/strategy_observation_report_latest.md --context results/strategy_observation_context_latest.json
```

Use `--show-evidence true` only when you want to inspect the evidence pack JSON. It is written under `results/`, which is excluded from clean zip exports.

## Audit Fields

- `policy_guard_status`
- `grounding_guard_status`
- `grounding_score`
- `unsupported_issue_count`
- `context_hash`
- `evidence_pack_hash`
- `prompt_hash`
- `report_hash`

The audit file does not contain API keys or real account information.
