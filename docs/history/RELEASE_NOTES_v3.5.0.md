# QuantDesk v3.5.0 Release Notes

Release: `3.5.0-battle-ready-research-and-portable-release`

## Overview

QuantDesk v3.5.0 packages the research platform into source clean zip and
portable-release workflows. It includes risk attribution, experiment tracking,
PIT universe diagnostics, chart-renderer fallback, package indexes, first-run
wizard, sample-data manifest, and build scripts.

## Install

Use the portable zip when no installer is available. Run:

```powershell
python scripts\qd.py first-run --json true --summary true
python scripts\qd.py daily --network false --with-guardrails true --summary true
```

## Boundaries

QuantDesk does not connect to brokers. It has no broker connection, no broker
API, does not place real orders, has no real orders execution path, no
auto-trading, and no investment advice. Network mode is off unless explicitly
enabled.

## PIT Data

PIT filtering is off by default. Users must provide historical listings data to
use `--pit-universe true`; QuantDesk does not fabricate listing or delisting
history.

## Partial Daily

`partial_daily` is provisional intraday context only. It never enters backtest,
factor, walk-forward, or historical replay flows.

## Attribution, Experiments, Charts

Risk attribution is explanatory and not causal proof. Experiment tracking stores
local metadata under `results/experiments/`. Chart rendering uses matplotlib
only when available and may return warnings instead of PNG files.

## Known Limits

PyInstaller and NSIS are optional external tools. If unavailable, the build
scripts keep the portable zip and installer script as the supported fallback.

## SHA256

Final hashes are generated in `dist/` during release export/build.
