# QuantDesk v3.7.0.2 Strategy Pipeline Repair

This is a narrow repair release for the research-only strategy validation pipeline.

- Adds `pit_mode=strict|price_coverage|off`; strategy viability defaults to degraded `price_coverage` when `historical_listings.csv` is missing.
- Adds load-time CSV schema normalization and schema anomaly reporting without overwriting source CSV files.
- Adds `factor-combine --weights` for simple manual factor experiments.
- Adds `combined-score-backtest --pit-mode price_coverage` with explicit survivorship-bias warnings.
- Adds local benchmark discovery for CSI 300 / CSI 500 / CSI 1000 when files are present.
- Adds board-aware limit-rate helpers for ST, main board, ChiNext, STAR, and guarded unknown-board cases.

No broker integration, no real orders, no automatic trading, no AI Advisor, and no default network access are introduced.
