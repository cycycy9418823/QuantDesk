# QuantDesk Quickstart

This path keeps everything local by default and uses research-only diagnostics.

## v3.7.0 Data Layer Quickstart

```powershell
E:\miniconda\python.exe scripts\qd.py data-db init --json true --summary true
E:\miniconda\python.exe scripts\qd.py data-db stats --json true --summary true
E:\miniconda\python.exe scripts\qd.py data-db migrate --adjustment forward --sample 20 --json true --summary true
E:\miniconda\python.exe scripts\qd.py data-health --json true --summary true
E:\miniconda\python.exe scripts\qd.py data-query daily --symbol 600000 --adjustment forward --json true --summary true
```

SQLite is a shadow layer. CSV remains available for rollback.

## v3.6.2 Research Analysis Loop

```powershell
python scripts\qd.py similar-day --synthetic-test-data true --summary true
python scripts\qd.py stress-test --synthetic-test-data true --summary true
python scripts\qd.py style-attribution --synthetic-test-data true --summary true
python scripts\qd.py decision-journal --synthetic-test-data true --summary true
```

Use real local CSV files for production research. Synthetic mode is for demos
and tests only. Missing local data returns warnings and does not fabricate real
market history.

## v3.6.1 Paper Risk Loop

```powershell
python scripts\qd.py smart-stop --synthetic-test-data true --summary true
python scripts\qd.py risk-alerts --synthetic-test-data true --summary true
python scripts\qd.py risk-alerts --synthetic-test-data true --notify true --dry-run true --summary true
```

This loop is paper-only and does not place orders.

## v3.6.0 Daily Research Loop

```powershell
E:\miniconda\python.exe scripts\qd.py data-integrity --synthetic-test-data true --summary true
E:\miniconda\python.exe scripts\qd.py sector-rotation --synthetic-test-data true --summary true
E:\miniconda\python.exe scripts\qd.py strategy-recommend --synthetic-test-data true --summary true
E:\miniconda\python.exe scripts\qd.py stock-picking-flow --synthetic-test-data true --summary true
E:\miniconda\python.exe scripts\qd.py paper-watchlist --add-from-strategy --top 10 --summary true
E:\miniconda\python.exe scripts\qd.py smart-stop --synthetic-test-data true --summary true
E:\miniconda\python.exe scripts\qd.py risk-alerts --synthetic-test-data true --summary true
```

Every command above is research-only / paper-only. Synthetic mode is a demo path
and does not pretend to be real market data.

## 1. Run self-check

For a first-time setup, run:

```powershell
E:\miniconda\python.exe scripts\qd.py first-run --json true --summary true
```

Then run self-check directly when you want the full diagnostic output.

```powershell
E:\miniconda\python.exe scripts\self_check.py
```

## 2. Run the daily workflow

```powershell
E:\miniconda\python.exe scripts\qd.py daily --network false --with-guardrails true --summary true
```

## 3. Run factor research

```powershell
E:\miniconda\python.exe scripts\qd.py factor-eval --summary true
```

## 4. Run a local backtest

```powershell
E:\miniconda\python.exe scripts\qd.py backtest --network false --summary true
```

Point-in-time filtering is off by default to preserve historical behavior. Use
`--pit-universe true` only after providing historical listings data.

## 5. Build the multi-strategy account book

```powershell
E:\miniconda\python.exe scripts\qd.py multi-strategy-book --summary true
```

This is a virtual research account book. It does not write broker state or
submit orders.

## 6. Run strategy recommendation

```powershell
E:\miniconda\python.exe scripts\qd.py strategy-recommend --synthetic-test-data true --summary true
```

This is a research-only strategy-style classifier. It does not create buy/sell
orders and does not connect to a broker.

## 7. Initialize and run the intraday workbench

```powershell
E:\miniconda\python.exe scripts\qd.py live --init
E:\miniconda\python.exe scripts\qd.py live --once --summary true
```

Use `Ctrl+C` to stop a foreground `--loop` run. `partial_daily` data is
provisional and never enters backtest, factor, walk-forward, or historical
replay flows.

## 8. Run risk attribution

```powershell
E:\miniconda\python.exe scripts\qd.py risk-attribution --summary true
```

Attribution is explanatory research support, not causal proof and not a trading
signal.

## 9. Review experiments

```powershell
E:\miniconda\python.exe scripts\qd.py experiments --summary true
```

Experiment records are stored under `results/experiments/` and are excluded
from clean release zips.
# v3.5.1.1 SourcePortable Note

If you are using `QuantDesk_v3.5.1.1_SourcePortable.zip`, install Python 3.10+ and dependencies first. It is not a no-Python standalone executable.

Five-minute path:

```powershell
python scripts\qd.py self-check
python scripts\qd.py daily --network false --with-guardrails true
python scripts\qd.py live --init
python scripts\qd.py attribution --synthetic-test-data true
python scripts\qd.py experiments
```

Use `qd risk-observation` for five-dimensional diagnostics. Use `qd attribution` for true Brinson allocation / selection / interaction and factor contribution.
