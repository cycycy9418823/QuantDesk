# QuantDesk Strategy Validation Suite

v3.3.1 的今日工作台把 `strategy-validate`、`consistency-check`、`feature-audit`、`stats-validate` 和 `param-sensitivity` 放在同一条验证流程里展示。工作台只读取 latest 结果，不会自动运行验证器，也不会把任何 passed/warning/failed 状态解释成买卖建议。

`v3.2.3-strategy-lookahead-and-execution-assumption-guard` adds local strategy correctness diagnostics inspired by Freqtrade-style lookahead analysis, adapted for A-share paper simulation and intraday data constraints.

`v3.2.4` keeps this suite unchanged and lets `daily-retain` include its summary in the daily reproducibility manifest. `v3.2.6` adds separate `feature-audit` and `stats-validate` diagnostics; `v3.2.9` adds separate `param-sensitivity` diagnostics for parameter robustness. These results can be summarized in daily retention and observation context. The validation suite still does not optimize returns or change strategy parameters.

This module is not a return optimizer. `param-sensitivity` is also not a parameter optimizer. QuantDesk does not search for high-yield strategies, does not change scoring formulas, does not change backtest return logic, and does not change strategy parameters.

## What It Checks

- Data dated after `target_date`.
- Feature metadata that suggests global or full-sample statistics.
- Daily `high` / `low` / `close` used as a proxy for intraday triggers.
- Reliable intraday execution when minute data is missing.
- `missing_intraday` or `no_fill` events that change cash or holdings.
- Board-candidate fills near limit-up prices.
- T+1 sellability violations.
- Missing `board_candidate_execution` assumption text.

## Command

```powershell
python scripts/qd.py strategy-validate --symbol 600744 --target-date 2026-05-19 --network false
```

Defaults:

- `network=false`
- `strict=true`
- `format=both`

Even if `network=true` is passed, v3.2.3 does not actively fetch market data. The command reads existing local results and writes:

- `results/strategy_validation_latest.json`
- `reports/strategy_validation_report_latest.md`

These runtime outputs are excluded from clean zip exports.

## Boundaries

QuantDesk does not connect to brokers, does not place real orders, does not save real account information, does not promise returns, and does not constitute investment advice. Missing minute data means intraday trigger verification is unavailable; daily bars must not be used to fake minute-level fills. Board-candidate paper fills cannot represent real queue execution at limit-up prices.
