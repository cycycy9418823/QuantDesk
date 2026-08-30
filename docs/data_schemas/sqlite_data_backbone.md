# SQLite Data Backbone

QuantDesk v3.7.0 adds `data/quantdesk.db` as a SQLite shadow cache. It does not replace CSV by default.

Core tables:

- `daily_bars_forward_adj`
- `daily_bars_back_adj`
- `daily_bars_raw`
- `intraday_partial_daily`
- `fundamentals`
- `stock_sectors`
- `index_daily`
- `intraday_bars_manifest`
- `dragon_tiger`
- `block_trades`
- `index_membership`
- `data_quality_issues`
- `data_update_log`
- `data_source_status`

`daily_bars` is a compatibility view over `daily_bars_forward_adj` only. `intraday_partial_daily` is never joined into that view.

SQLite uses WAL, `busy_timeout=30000`, a process-level single-writer lock, and batch transactions.

`qd data-health` also returns `strategy_readiness` as an audit field, not a strategy backtest. It reports daily-bar coverage, verifies generated rebalance samples use `signal_date < execution_date`, marks survivorship-bias risk as `high` when delisted/ST history is unavailable, and repeats that partial daily data remains isolated.
