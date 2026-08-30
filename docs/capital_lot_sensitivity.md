# Capital / Lot Sensitivity

v3.7.0.4 checks whether weak low-price baseline results are caused by strategy logic or by small capital plus the A-share 100-share minimum trade unit.

Default grid:

- `initial_cash`: 10000, 50000, 100000
- `max_positions`: 5, 10, 20
- `max_price`: 10, 15, 20
- `rebalance`: monthly
- `lot_size`: 100

The price filter is evaluated on `signal_date`. It must not use today or latest close to pre-filter historical backtests.

Key diagnostics are `invested_ratio_avg`, `unfilled_due_to_lot_size_count`, and `average_cash_drag`.

```powershell
python scripts/qd.py strategy-viability capital-lot-sensitivity --json true --summary true
```

Research-only. No broker, no real orders, no automatic trading, no investment advice.

