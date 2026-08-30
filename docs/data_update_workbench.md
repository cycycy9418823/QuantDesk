# QuantDesk 数据更新工作台

## v3.4.2 离线检查与联网补数

`data-update --network false` 是离线检查 / 刷新本地状态：它只读取本地数据并写出缺口报告，不会补数。日线过期时，真正的补数命令必须显式授权联网：

```bash
python scripts/qd.py data-update --target-date YYYY-MM-DD --enable-network --scope current_analysis_pool
```

工作台会同时显示 `analysis_pool_count`、`selected_count`、`max_symbols` 和 `truncation_reason`。例如“本次检查 80 / 当前分析池 120”表示本次受 `max-symbols=80` 限制，只覆盖部分样本，不代表全市场或全部分析池都已检查。

数据更新工作台用于回答一个很朴素的问题：本地日线是否已经更新到目标完整交易日，如果没有，下一步该如何补数据。

它和 data-health 的区别：

- data-update：检查缺口，生成更新计划；用户显式开启 `--enable-network` / `--network true` 后，才会调用已有日线 provider 更新指定股票或低价学习池 `included=true`。
- data-health：检查本地数据是否新鲜、完整，显示 stale / missing、分钟线覆盖率、provider 失败统计和 data_grade。
- daily-retain：在数据状态明确后，记录每日留存 manifest、输入输出 hash 和摘要。

默认不联网。默认命令只检查，不联网：

```bash
python scripts/qd.py data-update --symbols 600744,002115 --target-date 2026-05-20 --network false
```

显式传入 `--target-date` 时，该日期会原样进入 `target_complete_trade_date` 和 `effective_target_date`。只有不传 `--target-date` 时，QuantDesk 才会根据交易日历和当前时间自动推断理论完整交易日。

如需联网补日线，必须显式授权：

```bash
python scripts/qd.py data-update --symbols 600744,002115 --target-date 2026-05-20 --enable-network
```

不传 `--symbols` 时，QuantDesk 只会从低价学习池 `included=true` 中选取最多 `--max-symbols` 个，默认 80 个；不会默认扫描全 A。

分钟线不由日线冒充。盘中触发验证需要导入真实分钟线 CSV：

```text
data/intraday/{period}/{target_date}/{symbol}.csv
```

本模块仅用于本地数据更新和诊断，不构成投资建议，不接券商，不真实下单，不保存真实账户信息。
# v3.3.2.5 数据更新口径说明

数据更新工作台现在会同时显示“统计范围”“全池覆盖到”“最晚已有日线”“最早本地日线”“已更新股票”“仍过期股票”和“缺失股票”。

- `data_scope`: `specified_symbols` 表示本次只检查用户指定股票，`low_price_included` 表示低价学习池 included=true，`analysis_universe` 表示当前分析池，`local_cache` 表示本地缓存。
- `coverage_date` / 全池覆盖到：只有当前 checked symbols 都覆盖到某日期，才显示该日期。如果 2 只股票到 2026-05-20，48 只仍在 2026-05-15，则全池覆盖到仍是 2026-05-15。
- `latest_date_max` / 最晚已有日线：当前范围里最晚的一条本地日线日期，能说明 600744 / 002115 已经更新到 2026-05-20。
- `latest_date_min` / 最早本地日线：当前范围里有数据股票的最早最新日期。
- `latest_local_daily_date`: 兼容旧字段，现在等同于 `coverage_date` 或 `latest_date_min`，不要把它理解成所有股票都“最新”。

更新 2 只股票后低价池仍 stale，不代表数据回退；这只是统计范围不同。要查看过期列表，请看 `stale_symbols`；要更新整个低价学习池，请显式运行：

```powershell
python scripts/qd.py data-update --target-date YYYY-MM-DD --enable-network --max-symbols 80
```

默认仍然 `network=false`，只检查不联网。数据更新不构成投资建议，不接券商，不真实下单，不默认扫描全 A。
## v3.3.7 控制台输出降噪

`python scripts/qd.py data-update --target-date YYYY-MM-DD --network false --scope current_analysis_pool` 默认只输出短摘要：目标日期、统计范围、检查数量、全池覆盖到、最晚已有日线、成功/跳过/失败计数、结果文件和“我现在该怎么修”。完整 symbols 列表只写入 `results/data_update/data_update_latest.json`。

需要完整 JSON 时运行 `--json true`；需要旧式详细控制台输出时运行 `--verbose true`。如果需要真正补齐日线，必须显式运行 `--enable-network` 或 `--network true`，且仅用于更新本地行情数据，不构成投资建议。
## v3.4.1 Scope Summary

`data-update` now records the shared scope fields used by `data-health` and `market-intel`: `data_scope`, `scope_label`, `scope_source`, `requested_count`, `selected_count`, `max_symbols`, `truncated`, `symbols_preview`, and `symbols_omitted_count`.

Use `--scope current_analysis_pool` for normal daily checks. Use `--symbols` only when intentionally checking a small explicit list.
