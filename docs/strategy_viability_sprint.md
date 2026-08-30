# Strategy Viability Sprint

## v3.7.0.4 Evidence Hardening

The sprint now includes an optional capital / lot sensitivity pass. The key diagnostics are `invested_ratio_avg`, `unfilled_due_to_lot_size_count`, cash drag severity, and whether the low-price direction should continue. It remains research-only and does not produce trading instructions.

QuantDesk v3.7.0.1 runs a research-only viability check on top of the v3.7.0 data backbone.

## Commands

```powershell
E:\miniconda\python.exe scripts\qd.py strategy-viability precheck --json true --summary true
E:\miniconda\python.exe scripts\qd.py strategy-viability run --json true --summary true
E:\miniconda\python.exe scripts\qd.py strategy-viability report --json true --summary true
```

## Scope

- Uses local daily CSV data by default.
- Uses forward-adjusted compatible data by default.
- Keeps SQLite as a shadow layer and CSV as fallback.
- Does not use current PE/ROE snapshots for historical tests.
- Does not fetch network data by default.
- Does not place orders or connect to brokers.

## Baselines

- `momentum_20d`
- `low_volatility`
- `momentum_low_vol`
- `trend_continuation`
- `rsi_recovery`

Each baseline uses monthly rebalance, one trading day execution lag, listed-days and zero-volume filters, and explicit commission, stamp tax, and slippage.

## Decision Gate

Continue research when the best net baseline has annual return above 12%, max drawdown below 30%, and Sharpe above 1.0 without relying on a single dominant year.

Rethink when annual return is below 8%, max drawdown is above 40%, or Sharpe is below 0.6.
# v3.7.0.2 Repair Note

Strategy viability now defaults to `pit_mode=price_coverage` for screening when `historical_listings.csv` is missing. This is degraded PIT mode, keeps survivorship-bias risk high, and is not a formal point-in-time backtest.
## v3.7.0.3 Low Price Profile

Strategy viability defaults to the research-only `low_price` profile: `initial_cash=10000`, `max_positions=10`, `max_position_pct=0.10`, monthly rebalance, `top_n=10`, signal-date `close <= 15`, and `lot_size=100`. It must not pre-filter history using today's latest price.
