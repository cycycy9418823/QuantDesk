# QuantDesk Provider Setup

QuantDesk uses provider adapters only for local learning, research, and paper simulation. It does not connect to brokers, does not place real orders, does not save real account credentials, and does not constitute investment advice.

## Default Behavior

- Page initial load is read-only and does not access external data sources.
- `GET` APIs are read-only.
- `POST` APIs or CLI commands with explicit options are required before provider checks or downloads.
- Default scope is the low-price learning pool or current symbols, not the full A-share market.
- Provider failure is reported as `failed`, `not_available`, `not_configured`, or `skipped`; QuantDesk never treats empty or failed data as success.
- Providers marked `planned` / `placeholder=true` are roadmap placeholders. They are shown as gray “规划中 / 未启用” diagnostics and are not counted as real provider failures.

## Optional Dependencies

Install core dependencies first:

```powershell
python -m pip install -r requirements.txt
```

Install optional live data providers only when you need them:

```powershell
python -m pip install -r requirements-data.txt
```

Equivalent package extra:

```powershell
python -m pip install .[data]
```

`akshare` remains a supplement for daily bars, announcements, research reports, and basic data. It is not the only market-data pillar.

`mootdx` is the preferred optional provider for quotes, daily bars, intraday bars, and some F10/basic data when available.

## v3.1.2 mootdx Optional Provider

`mootdx` is optional and best-effort. If it is not installed, QuantDesk continues to work and falls back to Tencent Finance. It is never a hard dependency.

v3.2.2 cleaned the old skeleton definitions from `quantdesk/providers/mootdx_provider.py`; the public functions now refer to the real optional provider path only. `network=false` returns `skipped` and never connects to a mootdx server. `network=true` plus an installed dependency is required before QuantDesk attempts real mootdx requests. Failure records `failed_reason` and continues to Tencent fallback where applicable.

Install:

```powershell
python -m pip install mootdx
```

Check configuration without network access:

```powershell
python scripts/qd.py providers --network false
```

Run an explicit provider smoke check:

```powershell
python scripts/qd.py network-smoke --providers tencent,mootdx --symbols 600744,002115 --network true
```

Common mootdx failure reasons:

- 未安装 mootdx.
- Network connection failed.
- mootdx server unavailable.
- Unsupported symbol.
- Empty provider response.
- Historical intraday rows unavailable for the requested target date.

If mootdx fails, QuantDesk records `failed_reason` and continues to Tencent fallback. Missing minute data is still `missing_intraday`; the system does not use daily bars or realtime quotes to fake intraday fills.

## Providers Without API Keys

- Local cache: `data/daily`, `data/intraday/1m`, local theme maps.
- Tencent Finance lightweight adapters.
- AKShare and mootdx after installing their Python packages.

These providers may still fail because of network issues, interface changes, rate limits, or unavailable historical minute data.

## Providers Requiring Configuration

`iwencai` is optional and disabled by default. If you choose to use it later, create a local file:

```json
{
  "iwencai_api_key": ""
}
```

Save it as `config/provider_secrets.json`. This file is ignored by git and excluded from clean zip exports. Do not write real API keys, cookies, or tokens into source code, tests, logs, or reports.

## Disabled Or Deprecated Defaults

- Tushare: disabled by default because of points-wall constraints and insufficient free coverage for this workflow.
- Ashare: disabled by default because the project/interface is unstable or stale for current QuantDesk needs.

They remain visible in diagnostics so users understand why they are not core providers.

## Common Failure Reasons

- Missing dependency such as `mootdx` or `akshare`.
- Network failure or HTTPS disconnect.
- External interface field changes.
- Provider only supports current-day minute data, not historical target dates.
- Provider returns incomplete rows or non-target-date data.
- Free data source rate limiting or delay.

## Safety Boundary

Provider data is for local learning and paper simulation only. QuantDesk does not connect to securities brokers, does not place real orders, does not save real account information, does not promise returns, and does not constitute investment advice.

中文边界说明：QuantDesk 仅用于本地学习研究，不接券商、不真实下单、不保存真实账户信息、不构成投资建议。
