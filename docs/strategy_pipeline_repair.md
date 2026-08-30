# Strategy Pipeline Repair

v3.7.0.2 repairs the research pipeline before drawing strategy conclusions.

The default viability path uses `pit_mode=price_coverage` when `data/universe/historical_listings.csv` is missing. This lets screening continue, but it is degraded PIT mode and keeps `survivorship_bias_risk=high`.

Key commands:

```powershell
python scripts/qd.py strategy-pipeline precheck --json true --summary true
python scripts/qd.py factor-combine --weights "momentum_20d=1.0" --json true --summary true
python scripts/qd.py combined-score-backtest --pit-mode price_coverage --json true --summary true
python scripts/qd.py strategy-viability run --pit-mode price_coverage --json true --summary true
```

The repaired defaults for viability screening are monthly rebalance, top 20, max 20 positions, max 8% per position, 120-day minimum listing age, liquidity/suspension filters where available, and explicit transaction costs.

This remains research-only and is not production-ready or real-trade-ready.
## v3.7.0.3 Low Price Finalization Note

The pipeline can build runtime `data/universe/historical_listings.csv` from local price CSV coverage. This remains degraded PIT data: `list_date` is the first local price date, not the official listing date, and historical ST/delist periods are still incomplete.
