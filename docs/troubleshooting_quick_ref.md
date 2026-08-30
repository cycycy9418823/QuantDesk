# QuantDesk Troubleshooting Quick Reference

v3.4.2.2 note: clean zip 首次打开没有股票池和 `data/daily/` 是正常的。先运行 `python scripts/qd.py doctor`，再运行 `python scripts/qd.py data-bootstrap --preset low_price_demo --network false`。离线初始化只创建/检查本地结构，不会拉取真实行情；如果要补齐真实日线，再显式切换到 `--enable-network`。默认交易日历是 `data/calendar/a_share_trading_calendar.csv`，YAML 仅 legacy fallback。

v3.4.2.1 note: 核心安装使用 `pip install -r requirements.txt`；AKShare / mootdx 属于可选真实 provider，使用 `pip install -r requirements-data.txt` 或 `pip install .[data]`。规划中的 provider 显示为“规划中 / 未启用”，不算真实失败。

v3.4.2 note: 首页卡片只显示短股票预览；`本次检查 80 / 当前分析池 120` 表示 `max-symbols` 限制了本次检查，不代表全市场或全部分析池。日线过期时，联网补齐命令是 `python scripts/qd.py data-update --enable-network --scope current_analysis_pool`；`--network false` 只做离线检查，不会补数。

v3.3.4 note: `python scripts/qd.py daily --network false --with-guardrails true` 默认输出短摘要，不再刷完整 JSON。完整结果在 `results/daily_workflow_latest.json`，报告在 `reports/daily_quant_report_latest.md`；需要看完整 JSON 时加 `--json true` 或 `--verbose true`。daily warning 是流程/数据可信度提示，不等于策略失败，也不构成投资建议。

症状 | 可能原因 | 解决命令 | 相关文档
---|---|---|---
daily 控制台没有完整 JSON | v3.3.4 起默认改为短摘要，完整 payload 写入结果文件 | `python scripts/qd.py daily --network false --with-guardrails true --json true` | `docs/daily_workflow_guardrails.md`
找不到 daily 报告 | clean zip 初次打开没有 runtime 结果，或尚未运行 daily | `python scripts/qd.py daily --network false --with-guardrails true` 后看 `reports/daily_quant_report_latest.md` | `README.md`
status 提示 daily 报告较旧 | data-update/data-health 比 daily 更新，daily 报告还没有重新生成 | `python scripts/qd.py daily --network false --with-guardrails true` | `docs/user_guide.md`

v3.3.2.5 note: 首页数据状态现在区分统计范围、全池覆盖到、最晚已有日线和股票列表。schema-check 也区分 `confirmed_secret_leak` 和 `secret_like_reference`：高风险为 0 时，路径名、示例、placeholder、masked value 通常只是提示，不是确认泄露。

症状 | 可能原因 | 解决命令 | 相关文档
---|---|---|---
数据显示又变成旧日期 | 首页显示的是 `coverage_date` 全池覆盖日期，不是单只股票最晚日期 | 查看数据更新卡片里的“最晚已有日线”和“查看已更新股票” | `docs/data_update_workbench.md`
更新 2 只股票后低价池仍 stale | 600744 / 002115 已更新，但低价学习池其他股票仍停在较早日期 | `python scripts/qd.py data-update --target-date YYYY-MM-DD --enable-network --max-symbols 80` | `docs/data_update_workbench.md`
schema-check secret-like warning | `provider_secrets.json`、`llm_config.json` 路径名、示例字段、masked value 或文档说明被记录为低风险引用 | 查看 `confirmed_secret_leak_count`；若为 0 通常无需处理 | `docs/schema_contracts.md`
分钟线缺失 | clean zip 不含 `data/intraday/` 或尚未导入分钟线 CSV | 导入 `data/intraday/{period}/{target_date}/{symbol}.csv` 后重跑 data-health | `docs/data_health_dashboard.md`
provider planned 不是 failure | 规划中的 provider 标记为 placeholder/planned | 不需要处理；它不计入真实 provider failure | `docs/provider_setup.md`
clean zip 缺 results 是正常的 | clean zip 故意排除运行态结果和本地数据 | 在新环境运行 `python scripts/qd.py --help` 后按每日流程生成 | `README.md`
data-update network=true 没生效的历史问题 | v3.3.2.1 已修复；现在摘要会打印 `network/update_allowed/dry_run` | `python scripts/qd.py data-update --target-date YYYY-MM-DD --enable-network` | `docs/data_update_workbench.md`
target-date 被改写的历史问题 | v3.3.2.3 已修复；显式 `--target-date` 会保留为 effective target date | `python scripts/qd.py data-update --target-date 2026-05-21 --network false` | `docs/data_update_workbench.md`
PowerShell 查看 JSON 中文乱码 | PowerShell 5.1 默认编码可能不是 UTF-8 | `Get-Content results\data_update\data_update_latest.json -Encoding UTF8` | `docs/data_update_workbench.md`

v3.3.2.4 note: 今日工作台会把 raw 状态和字段名翻译成中文标签，并把“我现在该做什么”按数据 / 验证 / 复盘提示分组。这只是 UI 可读性增强，不改变 JSON status 合约。

v3.3.2.3 note: 今日工作台“刷新本地状态”只读读取本地结果，不联网、不执行任务、不调用 LLM。显式 `data-update --target-date YYYY-MM-DD` 会保留该日期为 `effective_target_date`；只有未传 target-date 时才自动推断理论完整交易日。

v3.3.2.2 note: planned provider placeholders are gray "规划中 / 未启用" diagnostics, not system failures. `provider_failure_stats` skips rows with `placeholder=true` / `status=planned`. `trading_calendar` now caches CSV reads by path; tests can call `clear_calendar_cache()`.

症状 | 可能原因 | 解决命令 | 相关文档
---|---|---|---
data-health 显示日线 stale/missing | 本地 `data/daily` 缺失或最新日期落后目标交易日 | `python scripts/qd.py data-health --symbols 600744,002115 --target-date YYYY-MM-DD --network false` 后按建议补数据 | `docs/data_health_dashboard.md`
data-update 显式 target-date 被改成上一交易日 | 旧版本可能把显式目标日期和自动推断混在一起；v3.3.2.3 起显式日期优先 | `python scripts/qd.py data-update --target-date 2026-05-21 --network false` 后查看 `effective_target_date` | `docs/data_update_workbench.md`
今日工作台显示 null | 旧结果缺少某些嵌套字段，或 data-health 使用 `freshness` / `intraday` 嵌套结构 | 点击“刷新本地状态”，或重跑 `python scripts/qd.py data-health --symbols 600744,002115 --target-date YYYY-MM-DD --network false` | `docs/user_guide.md`
命令中心长命令竖排 | 旧 UI 使用窄卡片网格展示命令状态 | 使用 v3.3.2.3 之后的命令中心表格布局，或运行 `python scripts/qd.py command-center` 更新状态 | `docs/architecture.md`
首页看不懂字段名 | 旧 UI 直接显示 schema key，例如 `target_complete_trade_date` | 使用 v3.3.2.4 后首页状态卡片会显示中文指标名；JSON 字段名仍保持稳定 | `docs/user_guide.md`
data-update 结果看起来像乱码 | Windows PowerShell 5.1 默认可能不用 UTF-8 解码 JSON | `Get-Content results\data_update\data_update_latest.json -Encoding UTF8` | `docs/data_update_workbench.md`
data-update --network true 仍像离线 | 旧版本参数透传或 `--no-network` 冲突；v3.3.2.1 起摘要会打印 `network/update_allowed/dry_run` | `python scripts/qd.py data-update --target-date YYYY-MM-DD --enable-network`；若同时传 `--no-network`，以离线为准 | `docs/data_update_workbench.md`
data-health 分钟线覆盖率 0 | clean zip 不包含 `data/intraday/`，或目标日没有 1m CSV | 导入 `data/intraday/1m/{symbol}.csv` 后重跑 `data-health` | `docs/data_health_dashboard.md`
provider_failure_stats 中 network_disabled 很多 | 当前多数检查在离线模式运行 | 如需联网烟测，显式 `python scripts/qd.py network-smoke --enable-network` | `docs/data_health_dashboard.md`
provider 全部 skipped | 默认 `network=false`，未允许联网 | `python scripts/qd.py providers --enable-network` | `docs/provider_setup.md`
network=false 为什么不联网 | 项目默认本地优先，必须显式启用网络 | `python scripts/qd.py --help` | `README.md`
missing_intraday | 本地缺分钟线，provider 可能不支持历史分钟线 | 导入 `data/intraday` CSV 后重试 | `docs/strategy_validation_suite.md`
daily_retention_manifest_missing | 尚未运行每日留存 | `python scripts/qd.py daily-retain --symbols 600744 --target-date YYYY-MM-DD --network false` | `docs/daily_data_retention_workflow.md`
consistency-check warning | 回测、纸面模拟或分钟线证据存在不一致 | `python scripts/qd.py consistency-check --symbol 600744 --target-date YYYY-MM-DD --network false` | `docs/strategy_validation_suite.md`
feature-audit row_count=0 warning | clean zip 不包含 `data/daily/`，本地无特征数据 | `python scripts/qd.py daily-retain --symbols 600744 --target-date YYYY-MM-DD --enable-network` 或导入 CSV | `docs/feature_quality_audit.md`
stats-validate insufficient_sample | 回测结果缺失或样本太少 | 先生成/导入本地 backtest 结果，再运行 `stats-validate` | `docs/backtest_statistical_validation.md`
param-sensitivity warning | 样本不足、narrow peak、parameter cliff 或参数扰动后指标波动较大 | `python scripts/qd.py param-sensitivity --symbol 600744 --target-date YYYY-MM-DD --network false --max-combinations 50 --seed 42` 后查看报告；不要把结果当成推荐参数 | `docs/parameter_sensitivity_audit.md`
LLM 未调用 | 默认 `--llm false` 或 `llm_enabled=false` | 显式 `--enable-llm`，仍需 policy guard | `docs/llm_observation_agent.md`
API Key 不生效 | Key 未放在环境变量或 provider disabled | 设置环境变量并检查 `config/llm_config.example.json` | `docs/llm_observation_agent.md`
pyproject 版本检查 | 包版本应来自 `quantdesk.version.PACKAGE_VERSION` | `python scripts/self_check.py` | `pyproject.toml`
clean zip 缺 results | 这是设计，运行态结果不进入 clean zip | 在目标环境重新运行 CLI | `README.md`
pytest 未安装 WARN | 可选开发依赖缺失 | `pip install -r requirements-dev.txt` | `scripts/self_check.py`
node --check 失败 | Node.js 缺失或前端 JS 语法错误 | 安装 Node.js 后运行 `node --check app/static/js/daily_research.js` | `app/static/js/daily_research.js`
A 股交易日历 weekend_only WARN | 本地 CSV 缺失或超出覆盖范围 | 恢复 `data/calendar/a_share_trading_calendar.csv` | `docs/known_logic_issues.md`
manifest hash mismatch | 输入文件和 manifest 记录不一致 | `python scripts/qd.py manifest-check --manifest results/daily_retention/YYYY-MM-DD/daily_retention_manifest.json` | `docs/reproducibility_manifest.md`
operation_log 为空 | 还没有通过 `qd.py` 或治理 CLI 运行任务 | `python scripts/qd.py command-center` | `docs/architecture.md`
archive-results 没有归档任何文件 | latest 结果尚不存在或仅 dry-run | 先运行诊断命令，再运行 `archive-results` | `CHANGELOG.md`

所有命令默认不联网、不接券商、不真实下单、不构成投资建议。
schema-check warning | latest 结果缺少通用字段，或 clean zip 中结果文件不存在 | `python scripts/qd.py schema-check --results-dir results --network false` 后查看 `reports/schema_validation_report_latest.md` | `docs/schema_contracts.md`
secret leak detected | 结果或报告里出现 api_key / token / secret 等敏感标记 | 删除运行态泄露文件，检查输出逻辑；严格模式下运行 `python scripts/qd.py schema-check --results-dir results --network false --strict true` | `docs/schema_contracts.md`
missing schema_version | 某个 JSON 结果没有 `schema_version` | 给对应模块补充通用 result contract 字段，或确认该文件是否为旧格式 | `docs/api_payload_schema.md`
illegal status value | `status` 不在 passed/warning/failed/skipped/missing/partial/unavailable 中 | 使用 `schema_contracts.normalize_status` 或更新输出模块 | `docs/schema_contracts.md`
results 文件不存在但 schema-check warning | clean zip 默认不包含 `results/`，或尚未运行对应命令 | 先运行对应 qd.py 命令生成 latest 结果；缺文件本身不是程序失败 | `README.md`
# v3.3.1 Workbench Quick Notes

症状 | 可能原因 | 解决命令 | 相关文档
---|---|---|---
今日工作台很多 missing / warning | clean zip 默认不包含 `results/`、`reports/`、`data/daily/`、`data/intraday/` | 先运行 `python scripts/qd.py data-health --symbols 600744,002115 --target-date YYYY-MM-DD --network false`，再按首页今日流程继续 | `docs/user_guide.md`
首页不再显示完整命令中心 | v3.3.1 已将完整命令中心移到左侧栏独立页面 | 左侧栏点击“命令中心”，或运行 `python scripts/qd.py command-center` | `docs/user_guide.md`
产物中心文件显示尚未生成 | 对应 latest JSON / Markdown 还不存在，missing 不是程序错误 | 从首页今日流程运行对应 qd.py 命令，或 `python scripts/qd.py archive-results --target-date YYYY-MM-DD --dry-run true` 查看计划 | `README.md`
没有 operation_log | 尚未通过 `qd.py` 运行治理命令，或 clean zip 刚解压 | 运行任意 `python scripts/qd.py ...` 命令后再打开命令中心 | `docs/architecture.md`
新手 / 专家模式没有记住 | 浏览器 localStorage 被清理或禁用 | 重新点击顶部“新手模式 / 专家模式”切换；状态 key 为 `quantdesk.ui.mode` | `docs/user_guide.md`
schema-check warning | 这是结构检查提示，不代表策略结果，也不是买卖信号 | `python scripts/qd.py schema-check --results-dir results --network false` | `docs/schema_contracts.md`
## 数据旧了但 UI 没自动更新

这是预期的安全边界：QuantDesk 默认 `network=false`，不会偷偷联网补数据。

先离线检查：

```bash
python scripts/qd.py data-update --symbols 600744,002115 --target-date YYYY-MM-DD --network false
```

如果确认要联网补日线：

```bash
python scripts/qd.py data-update --symbols 600744,002115 --target-date YYYY-MM-DD --enable-network
```

常见提示：

- `network=false`：只检查，不更新。
- `stale_data_warning`：本地日线未更新到目标完整交易日，复盘可信度下降。
- 缺少 `data/intraday`：不能验证盘中触发，不能用日线冒充分钟线。
v3.3.3 note: 不知道下一步时先运行 `python scripts/qd.py status`。它只读本地状态，不联网、不调用 LLM、不跑策略。`daily` 默认带 quick guardrails；warning 只表示治理提示，不改变策略参数、评分公式或回测收益逻辑，也不是投资建议。

症状 | 可能原因 | 解决命令 | 相关文档
---|---|---|---
help 里命令太多 | 历史功能多，v3.3.3 已把帮助分组 | `python scripts/qd.py status` | `docs/script_entrypoint_inventory.md`
daily 出现 quick_guardrails warning | latest 治理文件缺失、样本不足或成交假设需要人工查看 | `python scripts/qd.py strategy-validate --target-date YYYY-MM-DD --network false` | `docs/daily_workflow_guardrails.md`
想关闭 daily quick guardrails | 本轮允许显式关闭治理提示 | `python scripts/qd.py daily --network false --with-guardrails false` | `docs/daily_workflow_guardrails.md`
## Warning 后不知道怎么修

v3.3.5 起可以直接运行：

```powershell
python scripts\qd.py status
python scripts\qd.py fix-plan
```

`fix-plan` 只读取本地 `results/`、`reports/` 和 operation_log，不联网、不调用 LLM、不执行任务。它会把 stale data、missing intraday、stats warning、schema secret-like warning、provider offline skipped 等状态翻译成可复制命令。

- 日线 stale：显式运行 `data-update --enable-network`，再离线运行 `data-health`，最后重跑 `daily`。
- 分钟线缺失：只影响盘中触发验证，不代表日线失败；导入真实分钟线 CSV 后重跑 `data-health`。
- schema secret-like reference：如果 confirmed secret leak 为 0，通常只是路径名、示例字段或 masked value。
- provider skipped offline：默认离线跳过联网 provider，不是系统故障。

所有修复建议都是流程建议，不构成投资建议。

## v3.3.6 复制修复命令

症状：warning 有了但不知道先跑哪条。

处理：先看 UI 的“我现在该怎么修”，或运行 `python scripts/qd.py fix-plan`。离线命令会标注“离线可运行，不会联网”；`data-update --enable-network` 这类命令会标注“需要显式联网授权”。UI 只复制命令，不会自动执行，也不会偷偷联网。

产物中心里 `未生成` 是正常缺产物状态，`可能过期` 表示建议重新生成，`warning` 是提示，`failed` 才是失败。JSON/Markdown 预览只读，不读取密钥配置，不展示 API Key。所有修复建议都是流程建议，不构成投资建议。
## v3.3.7 quick fix notes

`data-update` / `data-health` 默认只显示短摘要。完整 symbols 列表和完整 JSON 在 latest 结果文件里；排查时加 `--json true` 或 `--verbose true`。如果 UI 中长命令被折叠，复制按钮仍复制完整命令。`data-update --enable-network` 需要显式联网授权；`data-health`、`daily` 默认离线。所有修复建议都是流程建议，不构成投资建议。
## market-intel 常见提示

- `market_news.status=skipped`：默认 `network=false`，没有抓取在线消息，这是正常安全行为。
- `data_scope=local_sample`：当前只基于本地低价池或分析池样本，不代表全 A。
- `缺少板块映射`：补充 `data/universe/stock_theme_map.csv` 后重新运行 `market-intel`。
- `缺分钟线`：打板观察的盘中触发无法可靠验证；导入真实分钟线后重跑。
- `scope=full_a requires --force true`：QuantDesk 不默认扫描全 A，必须显式确认。

所有修复建议都是流程建议，不构成投资建议。
## Market Intel Scope Mismatch

If the home page says `data-update / data-health / market-intel 使用的股票范围不同`, rerun the two read-only local checks with the same scope:

```bash
python scripts/qd.py data-health --network false --scope current_analysis_pool
python scripts/qd.py market-intel --network false --scope current_analysis_pool
```

Then review `reports/market_intelligence_report_latest.md`. This is still a paper-observation workflow and does not constitute investment advice.
