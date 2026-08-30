# Reproducibility Manifest

The reproducibility manifest records enough local metadata to check whether two QuantDesk runs used the same inputs and produced comparable outputs.

It is an audit artifact only. It does not call providers, does not call LLMs, does not connect to brokers, and does not provide investment advice.

## What It Records

- `app_version`, `package_version`, `api_schema_version`
- `target_date`
- explicit `symbols`
- `network` and `llm_enabled`
- `calendar_quality`
- command-line arguments
- input and output file records
- non-sensitive file hashes
- provider chain and data source trace
- strategy validation summary
- warnings and disclaimer

## What It Does Not Record

- API Key values
- `config/llm_config.json` contents
- `config/provider_secrets.json` contents
- real account information
- broker credentials
- real orders

Secret config files may be recorded as present or missing, but their contents are never stored in the manifest.

## Validate A Manifest

```powershell
python scripts/qd.py manifest-check --manifest results/daily_retention/2026-05-19/daily_retention_manifest.json
```

Compare two manifests:

```powershell
python scripts/qd.py manifest-check --manifest results/daily_retention/2026-05-19/daily_retention_manifest.json --compare-other results/daily_retention/2026-05-20/daily_retention_manifest.json
```

The checker is local-only: no network, no LLM, no provider calls.

## Clean Zip

Retention outputs remain under `results/`, so clean zip exports exclude:

- `results/daily_retention/`
- `daily_retention_manifest.json`
- `daily_retention_summary.md`

Static project assets such as `data/calendar/a_share_trading_calendar.csv` remain included.

