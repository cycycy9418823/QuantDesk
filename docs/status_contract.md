# QuantDesk Status Contract

> v2.5.2 — 状态文件和 API 字段分工定义。

## global_history_status

代表**整个本地历史库**，不是本次报告。

| 字段 | 类型 | 说明 |
|:---|:---|:---|
| `universe_total` | int | 股票池总数 |
| `csv_count` | int | 本地日线 CSV 总数 |
| `min_date` | str | 最早日线日期 |
| `max_date` | str | 最新日线日期 |
| `expected_latest_trading_day` | str | 理论应有最新交易日 |
| `success_count` | int | 上次更新成功数 |
| `failed_count` | int | 上次更新失败数 |
| `history_short_count` | int | 历史不足股票数 |
| `data_grade_distribution` | dict | 各等级股票分布 |
| `provider_summary` | dict | 数据源使用统计 |
| `failed_by_error_type` | dict | 按错误类型的失败分布 |
| `failed_by_market` | dict | 按市场的失败分布 |

## incremental_update_status

代表**最近一次增量更新任务**。

| 字段 | 类型 | 说明 |
|:---|:---|:---|
| `last_update_mode` | str | incremental / backfill / retry_failed |
| `last_update_started_at` | str | 开始时间 |
| `last_update_finished_at` | str | 结束时间 |
| `target_latest_trading_day` | str | 目标最新交易日 |
| `updated_count` | int | 本次更新成功数 |
| `already_fresh_count` | int | 本次跳过（已最新）数 |
| `failed_count` | int | 本次失败数 |
| `skipped_count` | int | 本次跳过数 |
| `lookback_days` | int | 回溯天数 |
| `provider_summary` | dict | 数据源使用统计 |

## report_run_status

代表**当前日报/筛选任务**，不是全市场数据建库。

| 字段 | 类型 | 说明 |
|:---|:---|:---|
| `report_file` | str | 报告文件路径 |
| `report_date` | str | 报告日期 |
| `sample_count` | int | 本次报告分析的样本数 |
| `analyzed_count` | int | 本次分析数（同 sample_count） |
| `long_candidates_count` | int | 长期候选数 |
| `board_candidates_count` | int | 打板候选数 |
| `warning_count` | int | 数据警告数 |
| `skipped_count` | int | 跳过数 |
| `report_mode` | str | report / sample / empty |

> **重要**：`report_run_status.sample_count` 是本次报告样本数，不是全市场股票总数。
> 例如本次报告只处理 28 只股票时，全局历史库仍可能有 5000+ CSV。

## cache_inventory

代表**轻量缓存摘要**，避免每次 UI 加载扫描 5000+ CSV。

| 字段 | 类型 | 说明 |
|:---|:---|:---|
| `csv_count` | int | CSV 文件总数 |
| `min_date` | str | 最早日线日期 |
| `max_date` | str | 最新日线日期 |
| `latest_date_distribution` | dict | 最新日期分布 |
| `data_grade_distribution` | dict | 各等级分布 |
| `generated_at` | str | 生成时间 |

## research_validation_status

代表 walk-forward / backtest_quality / audit 报告状态。包含在 `data_foundation_status.research_validation_ready` 中。

## practical_tools_status

代表 v2.5.0 practical tools 状态。包含在 `data_foundation_status.practical_tools_ready` 和 `data_foundation_status.practical_tools` 中。

## system_diagnostic_status

代表系统诊断状态。

| 字段 | 说明 |
|:---|:---|
| `app_version` | APP_VERSION |
| `api_schema_version` | API_SCHEMA_VERSION |
| `self_check_status` | self_check 结果（PASS/WARN/FAIL 计数） |

## 字段分工规则

1. CSV 数量 → `global_history_status.csv_count`
2. 股票池总数 → `global_history_status.universe_total`
3. 本次报告样本数 → `report_run_status.sample_count`
4. **三者必须分开显示，不能混用。**
