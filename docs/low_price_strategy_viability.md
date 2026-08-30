# Low Price Strategy Viability

## v3.7.0.4 Note

v3.7.0.4 does not optimize the low-price strategy. It tests whether v3.7.0.3 weakness is partly explained by `initial_cash=10000`, `lot_size=100`, and resulting cash drag. The price filter still runs at `signal_date`; latest close must not be used to pre-filter history.

QuantDesk v3.7.0.3 adds a research-only low-price profile for strategy viability checks.

The runtime `data/universe/historical_listings.csv` is built from local daily price CSV files. It is `price_csv_coverage` data: `list_date` means the first available local price date, not the official listing date. Delisting history and historical ST periods are still incomplete, so survivorship-bias risk remains high.

Low-price filtering is evaluated at each rebalance event on the `signal_date` close. The pipeline must not use today's latest price to pre-filter historical backtests.

Default low-price profile:

- `initial_cash=10000`
- `max_positions=10`
- `max_position_pct=0.10`
- `rebalance=monthly`
- `top_n=10`
- `price_filter close <= 15`
- `lot_size=100`
- `exclude_st=true`

The simulator records positions skipped because one 100-share lot cannot be bought, plus cash drag and average invested ratio. Reports are research diagnostics only and do not provide trading instructions.
