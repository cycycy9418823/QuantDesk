# Strategy Regression Guard

v3.7.0.6 prevents strategy reports from looking valid when the report chain has degraded.

The guard checks baseline distinctness, complete capital/lot grids, drawdown and Sharpe sanity, top-level metric consistency, report provenance, and whether import rewrites touched strategy core files.

Smoke and regression commands can write to `results/smoke/` and `reports/smoke/` by using `--smoke-run true` or `--write-latest false`, so canonical `latest` reports remain reserved for formal runs.

This is not a strategy optimization release. It is research-only and does not connect to brokers, place real orders, automate trading, or provide investment advice.

