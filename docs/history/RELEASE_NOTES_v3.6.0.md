# QuantDesk v3.6.0 Release Notes

Release string: `3.6.0-real-use-quant-platform-completion`

This release turns QuantDesk into a broader personal quantitative research and paper-risk platform. It remains research-only and paper-only: no broker connection, no real orders, no automatic trading, no default network access, and no buy/sell/order instructions.

## New Real-Use Flow

- Stock picking flow combines local data health, sector rotation, strategy recommendation, candidate review tasks, and paper watchlist suggestions.
- Paper watchlist stores local observation candidates under `state/` and is excluded from clean zips.
- Candidate review summarizes synthetic or local follow-up performance without becoming advice.

## Data Integrity

- Added raw / forward-adjusted / back-adjusted price layout policy.
- Added corporate actions schema helpers for dividends and splits.
- `qd data-integrity` reports missing local data as warnings, not fake data.

## Risk And Alerts

- Added smart stop, concentration checks, liquidity warnings, and risk alerts.
- These modules only warn. They do not close positions, send orders, or mutate broker state.

## Notification Center

- Notifications are disabled by default and dry-run by default.
- Local file notifications are supported; external channels remain opt-in and skipped unless configured.
- Secrets are not written to reports.

## Research Extensions

- Added dynamic factor model lite and value/quality/microstructure/sentiment/event factor helpers.
- Added optional execution realism assumptions, partial fill, limit-up fill model, slippage curve, and China A fee helpers without changing default backtest behavior.
- Added multiperiod attribution, style attribution lite, return explain, stress scenarios, similar day, and decision journal.
- Added local CSV adapters for money flow, dragon tiger, fundamentals, industry classification, and index membership history.
- Added optional SQLite local store. DuckDB remains optional and is not required.

## Packaging

SourcePortable remains Python-required unless true standalone artifacts exist. PyInstaller and NSIS status manifests are honest skipped/passed/failed records and must not be faked.

## Known Limitations And Release Honesty

- v3.6.0 is a skeleton / lite implementation release: it established modules, CLIs, payloads, UI panels, tests, and release proof.
- `risk_alerts` in v3.6.0 is synthetic-first and is not fully connected to a real paper portfolio.
- `smart_stop` in v3.6.0 provides ATR calculations but is not fully integrated into position management.
- `notification_center` in v3.6.0 is dry-run/local-first and does not auto-consume risk alerts by default.
- `similar_day` and `stress_scenarios` are lite placeholders and will be deepened later.
- No broker, no real orders, no automatic trading, and SourcePortable still requires Python.
