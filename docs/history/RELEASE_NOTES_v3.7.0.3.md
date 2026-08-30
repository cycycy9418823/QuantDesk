# v3.7.0.3 Low Price Pipeline Finalization

QuantDesk v3.7.0.3 finalizes the research-only low-price strategy pipeline.

- Builds runtime `data/universe/historical_listings.csv` from local price CSV coverage without network access.
- Treats generated listings as degraded `price_csv_coverage`, not official listing, delisting, or historical ST data.
- Uses `list_date = first available local price date`; this is not the official listing date.
- Adds the `low_price` strategy viability profile: `initial_cash=10000`, `max_positions=10`, monthly rebalance, `top_n=10`, `lot_size=100`, and signal-date `close <= 15` filtering.
- Keeps `factor-combine --weights` intact and reuses the existing combined-score path.
- Reports benchmark availability, degraded PIT warnings, survivorship-bias risk, ST limitations, lot-size unfilled positions, and cash drag.
- Remains research-only: no broker, no real orders, no automatic trading, no AI Advisor, and no default network.

Runtime data, including `data/universe/historical_listings.csv`, is excluded from clean release zips.
