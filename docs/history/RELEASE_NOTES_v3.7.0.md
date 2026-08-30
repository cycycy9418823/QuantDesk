# QuantDesk v3.7.0 - Data Backbone

v3.7.0 is a data backbone release: SQLite shadow layer + AKShare/network-gated provider chain + CSV migration + DualWriter + data health.

## What Changed

- Added a SQLite shadow layer under `quantdesk/data_store`.
- Added `QuantDeskDB`, schema initialization, WAL, busy timeout, single-writer lock, and batch transactions.
- Added separate daily tables for forward-adjusted, back-adjusted, and raw bars.
- Added `intraday_partial_daily` as an isolated incomplete-data table.
- Added CSV scanner, migration, sample compare, and row hash checks.
- Added DualWriter so CSV and SQLite can be written together while CSV remains the fallback.
- Added safe whitelisted `data-query` support.
- Added `data-health.strategy_readiness` as a readiness audit only: it checks daily-bar coverage, `signal_date < execution_date`, survivorship-bias risk, and partial-daily isolation without running strategy backtests.
- Added AKShare lazy-import provider helpers with `network=false` guard.
- Added provider chains for fundamentals, sectors, and intraday cache discovery.
- Added Parquet intraday cache path/manifest support without making DuckDB a dependency.
- Added data layer health and bounded data quality repair checks.

## Release Honesty

- SQLite is a shadow layer, not the only data source.
- CSV files are retained as compatibility and rollback paths.
- `read_prefer_sqlite` defaults to `false`.
- DualWriter can be enabled, but CSV fallback remains available.
- AKShare provider calls require `network=true`.
- Financial and sector coverage depends on local data and available provider interfaces.
- mootdx is not treated as a complete financial statement source.
- Minute history remains limited; v3.7.0 stores Parquet/cache manifests rather than forcing minute bars into SQLite main tables.
- No broker integration, no real orders, and no automatic trading.
- SourcePortable still requires Python.
- PyInstaller / NSIS artifacts are honestly skipped when the tools are unavailable.
