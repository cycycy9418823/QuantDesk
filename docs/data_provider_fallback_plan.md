# QuantDesk Data Provider Fallback Plan

## v2.8.8 Scope

This version builds a provider diagnostic and fallback design, not a new trading strategy. It keeps the existing daily workflow intact and adds a status layer that explains which data providers are local, optional, unavailable, or not implemented.

## Provider Groups

### Daily Price Providers

Order for future user-triggered updates:

1. `existing_local`: read `data/daily/{symbol}.csv`; no network.
2. `existing_update_script`: current QuantDesk daily update command; user-triggered only.
3. `akshare_daily`: optional AKShare daily provider.
4. `ashare_daily`: optional Ashare `get_price`-style adapter.
5. `tencent_daily` / `sina_daily`: placeholders inspired by Ashare dual-source fallback.

### Intraday 1m Providers

1. `local_intraday_1m`: read `data/intraday/1m/{symbol}.csv`; no network.
2. `ashare_intraday`: optional Ashare minute-line adapter.
3. `tencent_intraday`: placeholder.

Missing intraday data must remain `no_fill`. Provider diagnostics may suggest how to fill the cache, but execution never uses daily close as a fake fill.

### Theme Map Providers

Current and planned order:

1. `local`
2. `import`
3. `akshare_em`
4. `akshare_ths`
5. `per_symbol_info`
6. `baidu_stock_concept` (placeholder)
7. `ths_hotspot` (placeholder)
8. `eastmoney_concept` (placeholder)
9. `seed`

Placeholders show `planned` / `placeholder=true`, not `failed`. They are roadmap diagnostics and should appear as gray “规划中 / 未启用” in provider summaries.

### Market Hotspot Providers

Planned only:

- `ths_hotspot`
- `baidu_stock_concept`
- `eastmoney_hotspot`

Future use: theme heat, board dispersion, risk notes, and candidate filters. v2.8.8 does not change strategy scoring.

### Risk Event Providers

Planned only:

- `lhb_provider`
- `unlock_provider`
- `announcement_news_provider`

Future use: report warnings and risk-event context. No automatic trading action.

## API Rules

- `GET /api/data-providers/status`: read-only, no network, no file generation, no provider probing beyond local dependency/path checks.
- `POST /api/data-providers/check`: user-triggered provider diagnostics. It may write `results/data_provider_status_latest.json`.
- `scripts/check_data_providers.py`: default no network. `--check-network` is the only CLI flag that permits network smoke checks where implemented.

## Failure Policy

- Missing optional dependencies such as `Ashare` or `mootdx` are reported as `unavailable`, never as fatal errors.
- Roadmap placeholders are reported as `planned` / `placeholder=true`, not as real failures.
- Provider failure must not stop one-click review.
- QuantDesk stays a local paper-review tool: no broker API, no real orders, no account storage, no investment advice, no full-A scan by default.
