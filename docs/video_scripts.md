# QuantDesk v3.5.0 Video Scripts

These are operator scripts, not recorded videos.

## 1. Install And First Run

Show the portable zip, run `QuantDesk.bat --json true --summary true --run-self-check false`, and point to `~/.quantdesk/config.yaml`.

## 2. First Daily Report

Run `python scripts/qd.py daily --network false --with-guardrails true --summary true` and open `reports/daily_quant_report_latest.md`.

## 3. Factor Research

Run `factor-eval`, `factor-combine`, `combined-score-backtest`, and `research-dashboard` with `--summary true`.

## 4. Multi-Strategy Account Book

Run `multi-strategy-book`, then `risk-attribution`, emphasizing research-only virtual accounting.

## 5. Intraday Live Workbench

Run `live --init`, `live --once`, and optionally foreground `live --loop`; stop with `Ctrl+C`.
