# Reference: simonlin1212/a-stock-data

v2.8.8 只把该项目作为数据源组织方式参考，不复制其 Skill 代码进 QuantDesk 主代码。

## Fetch Status

- Local clone target: `external_refs/a-stock-data`
- Current machine status: `git` is not available in PATH, so the clone command could not run in this Codex session.
- Reference used for this summary: GitHub README and SKILL entry available from the repository page.
- License observed from README: Apache License 2.0. If future code reuse is considered, verify the license file first and keep attribution.

## Data Source Structure Learned

`a-stock-data` is organized as a six-layer A-share data toolkit:

- Market data: mootdx quotes, multi-period K line, level-2-style order book fields, tick data, Tencent Finance valuation fields.
- Research: Eastmoney report list/PDF, akshare consensus, iwencai natural-language report search.
- Signals: THS hot stocks, theme attribution, northbound flow, Baidu concept blocks, fund flow, Dragon Tiger Board, lockup expiry, industry comparison.
- News: stock news, CLS flash, global finance news.
- Fundamentals: mootdx finance, F10 company profile and quarterly snapshot.
- Filings: cninfo and mootdx announcement channels.

The useful design lesson for QuantDesk is not “copy every endpoint”; it is to group providers by purpose, expose provider-level status, and make fallback order visible.

## Fit For QuantDesk

Good candidates for future adapters:

- `mootdx` as an optional daily/intraday quote provider when users explicitly install it.
- Tencent/Sina daily and minute HTTP ideas as light fallback providers.
- Baidu concept blocks as a theme-map fallback when akshare board APIs fail.
- THS hotspot/industry comparison as a future market-hotspot provider.
- Dragon Tiger Board and lockup expiry as risk-event providers for reports.

Reference-only for now:

- iwencai semantic search, because it may need keys and custom authentication.
- Report PDF downloading, because QuantDesk currently focuses on local paper review and data diagnostics.
- News/fund-flow endpoints, until their rate limits and field stability are tested.

## Dependencies And Risks

- Optional dependencies: `mootdx`, `akshare`, `requests`, `pandas`, `stockstats`.
- Risks: endpoint instability, connection interruption, field changes, anti-scraping rules, data delays after close, non-trading-session unavailability, and dependency installation friction.
- Boundary: no broker connection, no real order placement, no account credentials, no investment advice.
