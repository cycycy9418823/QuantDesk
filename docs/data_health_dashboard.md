# v3.3.2.5 data-health 与覆盖日期

## v3.4.2 样本范围和短摘要

`data-health` 默认离线，只检查本地日线、分钟线覆盖和 provider 状态，不会联网补数。首页和数据健康页只显示短股票预览，完整 `symbols_checked`、`stale_symbols` 和 `missing_symbols` 保留在 JSON。`selected_count < analysis_pool_count` 时代表本次只检查了部分当前分析池，通常是 `max-symbols` 上限生效。

data-health 负责检查本地数据是否新鲜和完整，不负责联网补数。v3.3.2.5 起，日线新鲜度摘要会保留 `data_scope`、`symbols_checked`、`coverage_date`、`latest_date_min` 和 `latest_date_max`：

- `coverage_date` 是当前检查范围内所有 checked symbols 共同覆盖到的日期。
- `latest_date_max` 是当前检查范围里最晚已有日线日期。
- 如果只更新 600744 / 002115，低价学习池整体仍可能 stale；这时应比较 data-update 与 data-health 的 `data_scope` 和 target date，必要时用同一范围重新运行 data-health。
- 日线健康和分钟线覆盖是两件事：日线更新成功不代表有分钟线，缺分钟线只影响盘中触发验证，不能用日线 high/low 冒充盘中成交。

这些 warning 都是数据/流程提示，不构成投资建议。

# Data Health Dashboard

`data-health` 是 QuantDesk v3.2.8 的本地数据健康入口，用于回答四个问题：日线是否新鲜、分钟线是否覆盖、provider 最近失败还是离线跳过、data_grade 分布是否改善。

它不是收益优化器，不搜索策略，不改评分公式，不改回测收益逻辑，不接券商，不真实下单，也不构成投资建议。默认 `network=false`，即使传入 `network=true`，v3.2.8 也不会主动抓行情，只保留参数用于未来兼容。

## Command

```powershell
python scripts/qd.py data-health --symbols 600744,002115 --target-date 2026-05-19 --network false
```

如果不传 `--symbols`，QuantDesk 只会从低价学习池 `included=true` 中最多取 `--max-symbols` 个样本；不会默认扫描全 A。

## What It Checks

- 数据新鲜度：读取本地 `data/daily/{symbol}.csv`，使用 `trading_calendar.py` 计算交易日滞后，而不是自然日差。
- 分钟线覆盖率：读取本地 `data/intraday/1m/{symbol}.csv` 或兼容路径；缺少分钟线只标记 `missing_intraday`，不会用日线冒充盘中成交。
- Provider 失败统计：读取已有 `results/provider_network_smoke_latest.json` 和 `results/operation_log/operation_timeline.jsonl`；不主动调用 provider。
- Data Grade 趋势：根据本地 freshness 和 intraday coverage 生成 `BACKTEST_READY`、`RESEARCH_READY`、`SCORABLE`、`UNAVAILABLE`、`UNKNOWN` 分布。

## Outputs

- `results/data_health/data_health_dashboard_latest.json`
- `results/data_health/data_freshness_summary_latest.json`
- `results/data_health/intraday_coverage_summary_latest.json`
- `results/data_health/provider_failure_stats_latest.json`
- `results/data_health/data_grade_trend_latest.json`
- `results/data_health/data_grade_history.jsonl`
- `reports/data_health_dashboard_report_latest.md`

这些都是运行态结果，不进入 clean zip。

## Provider Failed vs Skipped

`failed` 表示 provider 调用或既有烟测记录中出现真实失败，例如 timeout、empty_response、parse_error。`skipped` 表示没有执行。`network_disabled` 是默认离线模式导致的 skipped，不算真实 provider 故障。

## Suggested Workflow

1. `python scripts/qd.py data-health --symbols 600744,002115 --target-date YYYY-MM-DD --network false`
2. 如果日线 missing/stale，先运行 `daily-retain` 或导入本地 `data/daily`。
3. 如果分钟线覆盖率低，导入 `data/intraday/1m` 后再做盘中触发验证。
4. 如果 provider 真实失败较多，先运行 `network-smoke --enable-network` 排查 provider。
5. 数据健康没有阻断后，再运行 `strategy-validate`、`consistency-check`、`feature-audit` 和 `stats-validate`。

## Parameter Sensitivity Status

In v3.2.9, `data-health` also displays `parameter_sensitivity_summary` if `results/parameter_sensitivity_latest.json` exists. This is only a visibility hook: data health does not replace `stats-validate`, does not optimize parameters, and does not recommend parameter values.

If `param-sensitivity` is missing, the panel shows `missing` and suggests running:

```powershell
python scripts/qd.py param-sensitivity --symbol 600744 --target-date YYYY-MM-DD --network false --max-combinations 50 --seed 42
```
# v3.3.1 Workbench Integration

今日工作台会读取 `data-health` latest 结果，并把数据新鲜度、分钟线覆盖率、provider 状态和 data_grade 摘要放进首页状态卡。产物中心会显示 data-health JSON / Markdown 文件是否存在。UI 只读取已有结果，不会自动运行 data-health，不会联网，不会调用 LLM，也不会把数据健康 warning 解释成交易建议。
## 与 data-update 的区别

`data-health` 只诊断本地数据是否新鲜、完整，不负责联网补数。若首页提示本地日线落后，应先运行：

```bash
python scripts/qd.py data-update --symbols 600744,002115 --target-date YYYY-MM-DD --network false
```

确认需要联网时，再显式使用 `--enable-network`。data-update 只处理用户指定股票或低价学习池 `included=true`，不会默认扫描全 A。完成后再运行 data-health 刷新健康状态。
## v3.3.7 data-health 短摘要与完整 JSON

`python scripts/qd.py data-health --target-date YYYY-MM-DD --network false --scope current_analysis_pool` 默认只输出短摘要：目标日期、统计范围、检查数量、coverage_date、latest_date_max、fresh/stale/missing、分钟线覆盖率和修复命令。完整 `stale_symbols` / `missing_symbols` 列表写入 `results/data_health/data_health_dashboard_latest.json`。

`data-health` 不联网、不调用 LLM、不补数据；看到 stale 时按提示运行 `data-update --enable-network`，然后重新运行 `data-health`。缺分钟线只影响盘中触发验证，不能用日线冒充分钟线。所有提示不构成投资建议。
## v3.4.1 Scope Summary

`data-health` now resolves the same default scope as `data-update` and `market-intel`. It defaults to `current_analysis_pool`, preserves full symbol lists in JSON, and shows only short previews in console/UI.

If the home page reports a scope mismatch, rerun:

```bash
python scripts/qd.py data-health --network false --scope current_analysis_pool
```
