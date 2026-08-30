# QuantDesk v3.7.0.1 - Strategy Viability On Data Backbone

v3.7.0.1 is a small strategy viability sprint on top of the v3.7.0 data backbone.

## What Changed

- Added data precheck for local daily CSV schema, benchmark availability, historical listings, survivorship-bias risk, and partial-daily isolation.
- Added five research-only price baseline tests: `momentum_20d`, `low_volatility`, `momentum_low_vol`, `trend_continuation`, and `rsi_recovery`.
- Added monthly rebalance simulation with one-day execution lag, new-stock and zero-volume filters, and explicit transaction costs.
- Added Strategy Viability Report with annual return, max drawdown, Sharpe, turnover, yearly returns, cost drag, best strategy, and decision gate.

## Release Honesty

- This release does not expand the SQLite schema or replace CSV.
- SQLite remains a shadow layer and CSV remains the fallback.
- No AI Advisor, no OpenAI/Claude API, no broker integration, no real orders, and no automatic trading.
- The report must keep weak or failed strategies visible.
- If delisted/ST history is unavailable, survivorship-bias risk is `high` or `unknown`, never `low`.
- SourcePortable still requires Python.
