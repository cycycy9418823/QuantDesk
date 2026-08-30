# QuantDesk Schema Contracts

`v3.3.0-consolidation-and-schema-contracts` adds a lightweight result payload contract for QuantDesk runtime JSON files. It is a governance layer only: it does not add strategies, does not change scoring formulas, does not change backtest return logic, and does not connect to brokers.

## Common Result Contract

New and refreshed result payloads should keep their existing domain fields, while also exposing these common fields:

```json
{
  "schema_version": "3.3.0",
  "app_version": "3.3.0-consolidation-and-schema-contracts",
  "package_version": "3.3.0",
  "api_schema_version": "3.3.0",
  "status": "passed",
  "ok": true,
  "generated_at": "2026-05-21 10:00:00",
  "target_date": "2026-05-19",
  "symbols": ["600744"],
  "warnings": [],
  "failed_reason": "",
  "skipped_reason": "",
  "suggested_next_action": "",
  "disclaimer": "仅用于本地学习研究，不构成投资建议。"
}
```

`quantdesk/schema_contracts.py` provides helpers such as `build_base_result`, `build_warning_result`, `build_error_result`, `normalize_status`, `normalize_warnings`, and `validate_base_result_contract`. Existing modules can migrate gradually; old result fields remain compatible.

## Status Values

- `passed`: local check completed and did not find notable warnings.
- `warning`: check completed but data, sample size, coverage, or contract quality needs attention.
- `failed`: invalid input, parse error, write failure, internal exception, or strict contract violation.
- `skipped`: intentionally not run, commonly because `network=false` or optional LLM is disabled.
- `missing`: expected local runtime file does not exist.
- `partial`: some steps completed and some could not.
- `unavailable`: capability or dependency is unavailable.

Compatibility aliases are normalized as follows: `ok`, `success`, and `true` map to `passed`; `error` and `exception` map to `failed`; `not_available`, `notavailable`, and `not_implemented` map to `unavailable`.

`network_disabled` should be represented as skipped/offline behavior, not a real provider failure. Missing local data in clean zip environments should be `warning` or `missing`, not a program failure.

## Warning And Reason Fields

- `warnings`: always a list of short, non-secret diagnostic messages.
- `failed_reason`: only for actual failures.
- `skipped_reason`: for intentional skips such as offline mode.
- `suggested_next_action`: a local command or plain next step.

Do not place API keys, tokens, provider secret content, real account information, raw tracebacks, or full config file contents in these fields.

## Schema Check

Run:

```powershell
python scripts/qd.py schema-check --results-dir results --network false
```

`schema-check` reads existing `results/` files, writes `results/schema_validation_latest.json` and `reports/schema_validation_report_latest.md`, and defaults to non-strict mode. Missing result files are warnings because clean zip packages intentionally do not include runtime results.

Use strict mode when preparing releases:

```powershell
python scripts/qd.py schema-check --results-dir results --network false --strict true
```

In strict mode, secret leaks and illegal status values fail the command. The validator masks possible secrets and does not read `config/llm_config.json` or `config/provider_secrets.json`.

## Runtime Contracts

- Provider result contract: provider rows must distinguish `failed` from `skipped`; `network_disabled` is skipped/offline.
- Daily-retain manifest contract: records versions, symbols, hashes, validation summaries, data-health summaries, and parameter-sensitivity summaries without secrets.
- Strategy validation contract: reports lookahead and execution assumption status plus warnings.
- Feature-audit contract: reports row count, feature count, missing rates, NaN/Inf counts, and high-missing warnings.
- Stats-validate contract: reports permutation test, bootstrap confidence intervals, sample-size warnings, and conservative interpretation.
- Param-sensitivity contract: reports bounded parameter combinations, robustness score, and overfit warnings without recommending parameters.
- Observation context contract: carries compact summaries for validation, consistency, data health, feature quality, statistics, and parameter sensitivity.
- Agent audit contract: records hashes and policy/grounding status without leaking prompts or secrets.
- Command-center contract: reads operation_log and reports latest command status without rerunning tasks.

All contracts are local diagnostics and do not constitute investment advice.
# v3.3.2.5 schema-check 密钥扫描分级

schema-check 现在把敏感信息检查拆成两类：

- `confirmed_secret_leak`: 高风险疑似真实泄露，例如 `api_key: sk-...`、`authorization: Bearer ...`、长随机 `access_token`、非空真实样式 password/token。strict=true 时只有这类计数大于 0 才 failed。
- `secret_like_reference`: 低风险引用，例如 `provider_secrets.json` 路径名、`llm_config.json` 路径名、docs 中的“不要写入 API Key”、schema example placeholder、masked value、空值、true/false、example/demo/placeholder。

报告字段包括 `secret_leak_count`、`confirmed_secret_leak_count`、`secret_like_reference_count` 和 `secret_findings`。每个 finding 会遮蔽值，只显示 `severity`、`kind`、`field_path`、`masked_preview`、`reason` 和 `suggested_next_action`。`provider_secrets.json` 这个路径名不是确认泄露。schema-check 只读本地结果，不读取密钥配置，不联网，不调用 LLM，不构成投资建议。
