# QuantDesk 一页现状系统图
本图服务于宪章 §2、§7、§8：让所有者看清每天发生什么。纸面研究，不连接券商、不下真实订单。
观测日期：2026-09-21；任务表为 21:09，本机端口为 21:40，账本文件时间为 20:44 读取。所有时间均为北京时间。
主线：行情留档 → 策略账／AI 交易员账 → 日报与页面 → 所有者复盘；运维负责拉回、备份和健康行。
“活着”指有启用任务或可追到的当前调用入口，不代表每天成功；unknown 表示本次证据不能确定。

## 0 每日时间线
依据 [P1-2 任务表](E:/codex/claude/receipts/20260919-P1-2-grok/output/02_tasks_table.md) 与本次 schtasks：本机共 17 个 QuantDesk 任务，11 个启用、6 个停用。下表保留完整任务名，列主产物，非全部附属文件。
除标明“每天／周六”外，任务按周一至周五触发；入口还可能自行判断交易日。启用不等于已跑完，返回 0 也不单独证明产物成功。

| 时间 | 启用任务／留存云端入口 | 跑什么 | 主要写入文件 |
|---|---|---|---|
| 08:35 | QuantDesk Morning Brief | 汇总研究早报并按配置尝试推送 | results/morning_brief/morning_brief_latest.json；reports/morning_brief/morning_brief_latest.md |
| 盘中每 5 分（C1 留存） | 云端 quantdesk-snapshots.timer | 留存全市场快照；非本机计划任务 | 云端 /home/azureuser/quantdesk/collector/data/snapshots/日期/时刻.csv 及 manifest.json |
| 09:35–11:30、13:05–15:05，每 5 分 | QuantDesk Intraday Data Refresh | 刷新关注股票分钟数据，给两档 AI 记观察与意图 | data/intraday/1m/；results/intraday/intraday_refresh_current.json；两档 daytime_notes.jsonl、daytime_intents.jsonl |
| 15:40、18:30 | QuantDesk Data Close Pipeline | 名册、日线、研究链、核对 | data/universe/a_share_symbols.csv；data/daily/逐股.csv；results/data_close_pipeline/data_close_pipeline_latest.json |
| 16:30，每天 | QuantDesk Daily Integrity Check | 检查日线文件完整性 | results/daily_integrity/daily_integrity_latest.json |
| 20:00 | QuantDesk Daily Review Routine | 每日研究、策略纸面组合与复盘 | state/auto_paper_portfolio.json；results/daily_workflow_latest.json；reports/daily_quant_report_latest.md |
| 20:20 | QuantDesk AI Trader Evening | 默认档晚间判断与纸面计划 | state/ai_trader/ledger.json、decision_log.jsonl、plan_latest.json；results/ai_trader/ |
| 20:40 | QuantDesk AI Trader Evening (GLM) | GLM 档独立晚间流程 | state/ai_trader_glm/ledger.json、decision_log.jsonl、plan_latest.json；results/ai_trader_glm/ |
| 21:30，每天 | QuantDesk Snapshot Pull | 拉回云端快照 | data/snapshots/日期/；logs/ops/snapshot_pull.log |
| 22:00，每天 | QuantDesk Backup Push | 打包账本等指定文件，上传并校验 | E:/codex/tmp/backup/日期包与清单；云端 backups/windows/；logs/ops/backup_push.log |
| 22:30 | QuantDesk Health Line | 把既有产物汇成一行状态 | STATUS.md；logs/ops/health_line.log |
| 周六 09:30 | QuantDesk Weekly Daily Wide Pool | 当前入口实际做日线覆盖审计 | results/data_chain/full_a_daily_reconciliation_latest.json；不是据任务名推断补采 |

**Disabled 单列：以下任务当前不自动触发，不能计入上述活跃写手。**

| 原定时间 | 停用任务 | 登记入口／原用途 |
|---|---|---|
| 09:30、13:00 起各重复 2 小时，每 5 分 | QuantDesk A0 Snapshot Collector | collect_market_snapshots.py；本机快照采集 |
| 15:30，每天 | QuantDesk Universe Baseline Refresh | refresh_universe_baseline.py；旧名册刷新 |
| 15:40 | QuantDesk Daily Data Refresh | run_daily_data_refresh.py；旧日线刷新 |
| 15:45、18:30 | QuantDesk Daily Data Chain | run_daily_data_chain_task.ps1；旧数据链 |
| 20:35、22:30 | QuantDesk Full A Daily Reconciliation | run_full_a_daily_reconciliation.cmd；旧全 A 核对任务 |
| 周六 09:30 | QuantDesk Weekly Minute Wide Pool | run_weekly_minute_wide_pool.cmd；旧分钟维护 |

**账本文件索引：** 以下位于正式目录 state/，时间是文件最后写入时间，不等于核验通过；“读者”是当前调用证据，不是某进程此刻打开文件的证明。

| 文件 | 最后写入 | 已证实读者 |
|---|---|---|
| auto_paper_portfolio.json | 2026-09-21 20:04:33 | 每日复盘、条件计划、共享分配、风险汇总、备份；页面主要读其 results 汇总 |
| ai_trader/ledger.json | 2026-09-21 20:25:19 | 默认晚间流程、盘中观察、8000 AI 面板、默认聊天、健康行、备份 |
| ai_trader_glm/ledger.json | 2026-09-21 20:40:42 | GLM 晚间流程、盘中观察、8000 GLM 面板、健康行、备份；GLM 聊天禁用 |
| paper_account.json | 2026-06-21 19:02:47 | 风险提醒的持仓汇总仍读取 |
| paper_portfolio.json | 2026-08-13 02:59:42 | 旧 paper_trading 模块读取；其当前外部调用入口 unknown |
| intraday_live_account.json | 2026-07-20 20:14:21 | 手动盘中研究入口 run_intraday_live_workbench.py；自动任务读者 unknown |
| historical_replay_account.json | 2026-05-17 18:52:09 | 当前读取调用 unknown；回放引擎能写此文件，不等于会读它 |
| auto_paper_portfolio.backup_20260911_200438_261743.json | 2026-09-11 20:04:38 | 策略账损坏恢复器，条件读取 |
| auto_paper_portfolio.backup_20260914_200523_315369.json | 2026-09-14 20:05:23 | 同上 |
| auto_paper_portfolio.backup_20260915_200222_887572.json | 2026-09-15 20:02:22 | 同上 |
| auto_paper_portfolio.backup_20260916_200516_691198.json | 2026-09-16 20:05:16 | 同上 |
| auto_paper_portfolio.backup_20260917_200207_858821.json | 2026-09-17 20:02:07 | 同上 |
| auto_paper_portfolio.backup_20260918_200344_547699.json | 2026-09-18 20:03:44 | 同上 |
| auto_paper_portfolio.backup_20260921_200433_807201.json | 2026-09-21 20:04:33 | 同上 |

## 1 数据层

- 干什么：把全 A 日线、盘中快照和按需分钟行情留成可追溯文件，缺数据就标出来。
- 活着的入口：表 0 的 Data Close Pipeline、Intraday Data Refresh、Weekly Daily Wide Pool、Snapshot Pull；云端入口依据 C1 留存。
- 写哪些文件谁读：data/daily/、data/intraday/1m/ 供两本账和研究工具读取；快照存 data/snapshots/，供拉回核验、健康行与人工检查，A1 尚未开工。
- 没人用的清单：Gd 第 1 批的旧估值补填、旧复权加载器，本次均已无对应文件；其余 unknown。第 2、3 批只是候选，不能照抄为无人使用。

## 2 策略账

- 干什么：确定性规则每天形成研究结果并更新纸面组合，便于回看规则及结果。
- 活着的入口：QuantDesk Daily Review Routine → run_daily_review_orchestrator.py → 每日流程与自动策略组合。
- 写哪些文件谁读：正式账为上表 auto_paper_portfolio.json；results/auto_paper_portfolio_latest.json 供页面及健康行，日报供所有者；旧账户另列，不冒充这本账。
- 没人用的清单：unknown；Gd 表中的 paper_review.py、portfolio_health.py、position_planner.py 仍列有自检或测试引用。

## 3 AI 交易员账：默认档与 GLM 档

- 干什么：两档分别记录判断、理由、计划和纸面账；与策略账平行，共用行情，不共用账本。
- 活着的入口：QuantDesk AI Trader Evening、QuantDesk AI Trader Evening (GLM)；Intraday Data Refresh 还调用两档观察，不能解释为已开放盘中成交。
- 写哪些文件谁读：state/ai_trader/ 与 state/ai_trader_glm/ 分别保存账本、判断、计划、日志；各自晚间流程、观察、面板、健康行与备份读取，文件时间见上表。
- 没人用的清单：Gd 第 1 批旧 scripts/ai_trader_compare.py 本次已无文件；其余 unknown。旧 P2 把 AI 模块入口判“无用”的结论已被否定。

## 4 界面与服务

- 干什么：让所有者看今天、看盘、两本账和复盘学习，研究工具作为次级入口。
- 活着的入口：手动入口 scripts/quant_api_server.py，主服务端口 8000；对应 daily_research.html 与 daily_research.js；21:40 本机未见 8000、8002、8765 监听。
- 写哪些文件谁读：主页面主要读 results/ 与 AI 分档状态供所有者看；页面操作的完整写入清单 unknown，不能把整个服务描述为只读。
- 没人用的清单：unknown；8002 已退役外移、8765 是冻结旧入口，均非“无人使用”的证明；旧 quant_app_server.py 本次已无文件。

## 5 运维与治理

- 干什么：把快照拉回来、把账本备出去、给所有者一行健康状态，并留下授权与验收记录。
- 活着的入口：QuantDesk Snapshot Pull、QuantDesk Backup Push、QuantDesk Health Line；人工检查入口 scripts/self_check.py，治理队列由 NEXT_TASKS_AND_WORKTREE_RULES.md 承载。
- 写哪些文件谁读：三项任务分别写拉回目录、备份包、logs/ops/ 与 STATUS.md，供健康行和所有者复核；状态页记载备份程序已更新，运行成功要另看当晚证据。
- 没人用的清单：unknown；Gd 中的打包／校验候选仍有自检、测试或打包连接，不据旧盘点停用它们。

## 6 研究工具箱与遗留

- 干什么：按需做回测、因子分析、归因和历史回放，保留研究材料，不把研究工具存在当成策略有效。
- 活着的入口：手动统一入口 scripts/qd.py；盘中研究由 run_intraday_live_workbench.py 调用，其他工具以各自当前调用关系为准。
- 写哪些文件谁读：通常写 results/、reports/ 供研究页面或人工阅读；四个旧账户及其已知读者见上表，是否接进每日链逐项看入口。
- 没人用的清单：unknown；Gd 第 2 批中的多周期归因、策略比较、缠论知识库仍见自检或打包连接，不能当成已废弃。

## 7 没分清的地方

- 旧 paper_portfolio.json 的外部调用方、historical_replay_account.json 的当前读者，以及完整性检查产物的当前读者：unknown。
- 云端现时配置、本机快照拉回与每个时段的质量是否完整：本轮未联网；只复用 C1 的 9/21 留存，不作新的运行结论。
- 页面全部操作的写入范围：unknown；未见端口监听也不能说明功能已删除。
- Gd 第 2、3 批哪些最终停用或应用：分批表不能单独回答；P2-1 的旧“无用”数字已被改判，不作为现状。
- 任务成功、模型真实调用、判断到期可评价是不同问题；本图的时刻和入口不能证明这些结果，需对应运行回执。

## 8 文档优先级

按本轮所有者指示：**宪章 > STATUS > 本图 > 其他**。本图是带观测时间的现状快照，不替代后续 STATUS 更新。
[docs/architecture.md](E:/codex/quant_new/docs/architecture.md) 与 docs/history/ 下材料标为 **历史**，不作为当前入口或调度依据；本次未改这些文件。
证据索引：[Gd 分批表](E:/codex/claude/receipts/20260921-Gd-grok/output/p1/01_batches.csv) · [P2-1 改判](E:/codex/claude/receipts/20260919-P2-1-grok/verdict.md) · [C1 云端留存](E:/codex/claude/receipts/20260921-C1-codex/result.json) · [本图回执与调用定位](E:/codex/claude/receipts/20260921-MAP-codex/result.json)。
交付状态：新建、未跟踪、未提交；待 Fable 终验、所有者审稿。
