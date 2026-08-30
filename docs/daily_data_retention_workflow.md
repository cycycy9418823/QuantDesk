# Daily Data Retention Workflow

`v3.2.4-daily-data-retention-and-reproducibility-manifest` adds a local workflow for keeping a daily record of what QuantDesk used and produced. In v3.2.6 the manifest can also record feature quality and backtest statistical validation summaries when those local result files exist. In v3.2.8 it can additionally record `data_health_summary` from `data-health`. In v3.2.9 it can record a compact `parameter_sensitivity_summary` when `param-sensitivity` has been run. In v3.3.1 the 今日工作台 surfaces daily-retain as a required workflow step and the 产物中心 shows the latest retention manifest/summary when they exist.

It is not a strategy optimizer. It does not search for higher returns, does not change scores, does not change backtest returns, does not connect to brokers, and does not place orders.

## Command

```powershell
python scripts/qd.py daily-retain --symbols 600744,002115 --target-date 2026-05-19 --network false
```

Defaults:

- `network=false`
- `llm=false`
- `include-provider-smoke=true`
- `include-strategy-validation=true`
- `include-observation-report=true`
- `max-symbols=3`

If `--symbols` is omitted, QuantDesk samples at most `max-symbols` rows from the low-price learning universe where `included=true`. It never defaults to a full-A scan.

## Outputs

Daily retention writes runtime artifacts under:

- `results/daily_retention/{target_date}/daily_retention_manifest.json`
- `results/daily_retention/{target_date}/daily_retention_summary.md`

The `results/` directory is excluded from clean zip exports.

## Suggested Daily Flow

1. Pre-market provider check: `python scripts/qd.py providers --network false`
2. Intraday or post-market retention: `python scripts/qd.py daily-retain --symbols 600744,002115 --target-date YYYY-MM-DD --network false`
3. Data health: `python scripts/qd.py data-health --symbols 600744,002115 --target-date YYYY-MM-DD --network false`
4. Post-market validation: `python scripts/qd.py strategy-validate --symbol 600744 --target-date YYYY-MM-DD --network false`
5. Feature audit: `python scripts/qd.py feature-audit --symbols 600744,002115 --target-date YYYY-MM-DD --network false`
6. Backtest statistics: `python scripts/qd.py stats-validate --symbol 600744 --target-date YYYY-MM-DD --network false --iterations 500 --seed 42`
7. Parameter robustness: `python scripts/qd.py param-sensitivity --symbol 600744 --target-date YYYY-MM-DD --network false --max-combinations 50 --seed 42`
8. Learning review: `python scripts/qd.py observation-report --symbol 600744 --target-date YYYY-MM-DD --network false --llm false --audit true`
9. Development/export: `powershell -ExecutionPolicy Bypass -NoProfile -File .\export_project.ps1`

Do not temporarily change strategy parameters during retention just to improve results. The goal is reproducibility and honesty, not return optimization.

## Boundaries

- No broker API.
- No real orders.
- No real account information.
- No investment advice.
- No default network.
- No default LLM.
- No daily or realtime data used to fake minute-level execution.
## 留存前的数据更新预检

v3.3.2 起，daily-retain manifest 会记录 `stale_data_warning`。如果本地日线未更新到目标完整交易日，manifest 会提示“学习复盘可信度下降”，但不会自动联网。

建议先运行：

```bash
python scripts/qd.py data-update --symbols 600744,002115 --target-date YYYY-MM-DD --network false
python scripts/qd.py data-health --symbols 600744,002115 --target-date YYYY-MM-DD --network false
python scripts/qd.py daily-retain --symbols 600744,002115 --target-date YYYY-MM-DD --network false
```

只有显式 `--enable-network` 才允许补日线。
# v3.3.2.5 与 data-update / data-health 的关系

每日留存 `daily-retain` 不负责联网补数，也不判断哪只股票应该交易。推荐顺序是先用 data-update 检查或显式补日线，再用 data-health 刷新 `coverage_date`、`latest_date_max`、`stale_symbols` 和分钟线覆盖，最后运行 daily-retain 记录 manifest。

如果只更新了 600744 / 002115，低价学习池整体仍可能 stale；daily-retain manifest 里的 stale data warning 是流程提示，说明复盘可信度受数据新鲜度影响，不构成投资建议。
