# Feature Quality Audit

`v3.2.6-backtest-statistical-validation-and-feature-quality-audit` adds a local feature quality audit.

It checks missing rates, zero rates, NaN/Inf counts, symbol coverage, and date coverage for fields such as `close`, `volume`, `amount`, moving averages, volatility, turnover, and strategy score fields when present.

Patch `v3.2.6.1-feature-audit-empty-data-graceful-warning` makes clean-zip empty-data runs graceful: if no local feature or daily rows are found, the command reports `status=warning`, `insufficient_data=true`, and a next action instead of failing the process. It still does not fetch or fabricate data.

## Command

```powershell
python scripts/qd.py feature-audit --symbols 600744,002115 --target-date 2026-05-19 --network false
```

`network=false` is the default. This command reads local files only and does not fetch market data.

## Outputs

- `results/feature_quality_audit_latest.json`
- `reports/feature_quality_audit_report_latest.md`

These runtime artifacts are excluded from clean zip exports.

## Why It Matters

High missing rates or Inf/NaN values can make strategy observations less trustworthy. The audit does not repair data and does not change strategy results. It only records whether feature inputs are complete enough for learning review.

This module does not constitute investment advice.
