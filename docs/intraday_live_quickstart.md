# Intraday Live Workbench Quickstart

The Intraday Live Workbench is research-only. It does not connect to a broker, place real orders, or provide investment advice.

## 1. Initialize the intraday virtual account

Run this once before your first live snapshot:

```powershell
E:\miniconda\python.exe scripts\qd.py live --init
```

This creates `state/intraday_live_account.json` with an empty paper-only account:

- `cash: 0.0`
- `positions: []`
- `research_only: true`
- `no_broker: true`
- `no_real_orders: true`

The file is runtime state. It is ignored by git and excluded from clean zips.

## 2. Run one snapshot

```powershell
E:\miniconda\python.exe scripts\qd.py live --once --summary true
```

For compact JSON:

```powershell
E:\miniconda\python.exe scripts\qd.py live --once --json true --summary true
```

## 3. View the latest report

Open:

```text
reports/intraday_live_workbench_latest.md
```

The latest JSON is:

```text
results/intraday_live_workbench_latest.json
```

## 4. Run a foreground refresh loop

`--loop` runs in the current terminal. It is not a Windows service, systemd unit, or background daemon.

```powershell
E:\miniconda\python.exe scripts\qd.py live --loop --spot-refresh-seconds 60 --minute-bar-refresh-seconds 300 --summary true
```

Stop the loop with:

```text
Ctrl+C
```

For a short test loop:

```powershell
E:\miniconda\python.exe scripts\qd.py live --loop --max-iterations 1 --json true --summary true
```

## 5. Data boundary

`partial_daily` files are provisional intraday audit snapshots:

```text
data/intraday/partial_daily/
data/intraday/spot_snapshots/
```

They are never used by backtest, factor evaluation, walk-forward, or historical replay. Complete daily bars still come from the normal daily data pipeline.

## 6. Safety notes

- No broker connection.
- No real orders.
- No automatic rebalancing.
- Intraday scores are provisional and always marked `score_basis=partial_daily_snapshot`.
- Intraday observations are not formal daily scores and are not trading signals.
