# Daily Workflow Quick Guardrails

## v3.3.4 console summary

`python scripts/qd.py daily --network false --with-guardrails true` 默认输出短摘要：日期、状态、数据覆盖、候选数量、Guardrails warning/failed 计数、报告路径和结果路径。完整 JSON 不再默认刷屏，但仍会写入 `results/daily_workflow_latest.json`。

需要排查完整 payload 时显式运行：

```powershell
python scripts/qd.py daily --network false --with-guardrails true --json true
python scripts/qd.py daily --network false --with-guardrails true --verbose true
```

报告路径固定为 `reports/daily_quant_report_latest.md`。`status` 会读取最近 daily 状态并指向该报告；产物中心的“每日流程”分组也会显示 daily/status/command-center 产物。daily warning 只是流程和数据可信度提示，不等于策略失败，不是收益保证，也不构成投资建议。

v3.3.3 起，`python scripts/qd.py daily --network false` 默认附带 quick guardrails。它们只做本地治理提示，不改变策略参数、不改变评分公式、不改变回测收益逻辑，也不会联网、下单或调用 LLM。

## 默认行为

```powershell
python scripts/qd.py daily --network false --with-guardrails true
```

quick guardrails 会汇总：

- strategy lookahead 快速检查。
- execution assumption 快速检查。
- feature-audit latest 状态。
- stats-validate latest 状态。
- param-sensitivity latest 状态。

如果某个 latest 文件不存在，会记录 warning，但不会直接让 daily failed。完整检查仍可单独运行：

```powershell
python scripts/qd.py strategy-validate --target-date YYYY-MM-DD --network false
python scripts/qd.py stats-validate --symbol 600744 --target-date YYYY-MM-DD --network false
python scripts/qd.py param-sensitivity --symbol 600744 --target-date YYYY-MM-DD --network false
```

## 关闭 quick guardrails

```powershell
python scripts/qd.py daily --network false --with-guardrails false
```

关闭后 daily 会写入 `quick_guardrails` skipped step。这个开关只影响治理提示，不影响数据更新、策略参数、评分公式或回测收益逻辑。

## 边界

quick guardrails 不是交易建议，不构成投资建议，不输出买卖、仓位、目标价或止损价。缺少分钟线时仍不能验证盘中触发，QuantDesk 不会用日线 high/low 冒充分钟线成交。
## v3.3.5 remediation follow-through

`daily` 的 quick guardrails 仍然只做流程和数据可信度提示；v3.3.5 会把这些提示写入 `remediation_plan` 和 `daily_console_summary.remediation_actions`。默认控制台只显示短摘要和最多 3 条修复路径，完整 JSON 仍在 `results/daily_workflow_latest.json`。

warning 不等于策略失败，也不是投资建议。缺分钟线时只能说明盘中触发不可可靠验证；统计样本不足时只能说明结论有限；参数敏感性 warning 不是自动调参建议。

## v3.3.6 guardrails remediation copy

Daily guardrails 的 warning 会继续写入 `remediation_plan`，但 UI 默认只显示最多 3 条“我现在该怎么修”。缺分钟线会提示导入真实分钟线 CSV 或忽略盘中触发验证，不会建议用日线 high/low 冒充分钟线；统计样本不足和参数敏感性提示只用于学习复盘，不代表策略失败或收益保证。

命令显示时会区分离线和显式联网：重新运行 `daily`、`data-health`、`schema-check` 默认离线；更新本地行情数据必须显式运行 `data-update --enable-network`。所有 remediation 都是流程建议，不构成投资建议。
## v3.3.7 related output style

`daily` keeps the v3.3.4 short summary behavior. The companion `data-update` and `data-health` commands now use the same quiet style: no full JSON by default, no full long symbol list by default, and full details available through result files or `--json true` / `--verbose true`. Guardrail warnings and remediation commands remain workflow guidance only, not trading advice or a return guarantee.
