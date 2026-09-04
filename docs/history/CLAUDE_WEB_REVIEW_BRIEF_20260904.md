# QuantDesk 审阅说明（给网页版 Claude）

> **历史文档（2026-09-04）。** 只用来了解以前怎么想的，不作依据。现行文件是仓库根目录 `CHARTER.md`。

把本文件整份上传或粘贴为第一条消息即可。不要上传整个仓库。

你是审稿人，不是实现者。先读完「硬边界」再给意见。不要改代码、不要 commit、不要碰计划任务、不要连券商、不要把研究输出写成可下单指令。

---

## 怎么用这份材料（给人看的操作）

网页版 Claude（claude.ai）吃不下整个 QuantDesk。正确做法：

1. 新建一个 Project 或新对话。
2. 上传**仅此文件**作为项目知识 / 第一条附件。
3. 若还要看源码，再按下面「建议另传的小文件」逐个上传，不要 zip 整个目录。
4. **禁止上传**：`data/`、`state/`、`results/`、`logs/`、`.venv/`、`dist/`、任何 token / SendKey / `.env`、真实行情 CSV。
5. 第一条提示词用：

> 请按附件 briefing 审阅 QuantDesk 当前 Data Close Pipeline 接管是否诚实、能否替换旧数据计划任务。先列 P0/P1，再写你还要看哪些文件。不要给实现补丁，除非我明确要求。

仓库在本机：`E:\codex\quant_new`  
分支：`codex/glm-data-plane-takeover`（相对 `9ac777a` 大量未提交改动）  
规范树就是这个目录；Windows 计划任务也指向这里。  
实现工作树（不要和规范树混改）：`E:\codex\quant_new\.worktrees\glm-data-ops-consolidation`

---

## 这是什么项目

QuantDesk 是**本机 A 股研究 + 纸面模拟**工具，不是实盘交易系统。

- 主服务：`scripts/quant_api_server.py`，端口 **8000**
- 主前端：`app/templates/daily_research.html` + `app/static/js/daily_research.js`
- 统一 CLI：`scripts/qd.py`
- 已冻结、不要再改：`scripts/quant_app_server.py`（8765）、`app/static/app.js`

当前发布轨：`v3.7.0.6-strategy-regression-guard-and-report-hygiene`。

---

## 硬边界（违反即 P0）

- 不连券商、不下真单、不存实盘密钥。
- 默认不联网。联网必须显式 `--enable-network` / `--network true`。
- 默认不用 LLM。
- 不编造行情、新闻、板块热度、provider 结果。
- 不把缺失数据当成真实数据；停牌/涨跌停/缺失分钟线不得当成可交易。
- 研究输出可以有入场/止损/目标价，但必须带诚实校验标签（如「研究输出·未验证」），且永远不是可执行订单。
- 不改策略公式、打分权重、回测收益逻辑，除非用户当场批准。
- 不把 `data/`、`state/`、`results/`、`reports/`、`logs/` 当源码提交。
- Worker 默认不能 commit / merge / 安装或切换计划任务。只有用户口头授权的那一件事能做。

---

## 当前要审的主线：Data Close Pipeline 能否替换旧数据任务

目标：收盘后只留**一个**可信全 A 数据写入者，给研究 / 纸面 / AI 消费。

Pipeline 五段，顺序固定：

| 段 | 作用 | 包装的旧入口 |
|---|---|---|
| 1 universe | 批准源全 A 名册 | `scripts/refresh_universe_baseline.py` 一类 |
| 2 market_refresh | 全 A 日线 / 停牌核验 / 估值 / 基准 / 新闻 | `scripts/run_daily_data_refresh.py` |
| 3 research_chain | 研究池深维护（特征、候选等） | `scripts/run_daily_data_chain.py`，应消费 `upstream_reuse.json` |
| 4 reconcile | 三路完备性 + 缺口重试（核验停牌从 retry 扣除） | `scripts/run_full_a_daily_reconciliation.py` |
| 5 health_alert | 健康告警；无 `--enable-alert-network --alert-push` 时只预览 | `scripts/run_data_health_alert_delivery.py` |

计划替换的旧任务只有四条：

- QuantDesk Universe Baseline Refresh（15:30）
- QuantDesk Daily Data Refresh（15:40）
- QuantDesk Daily Data Chain（15:45 + 18:30 重试）
- QuantDesk Full A Daily Reconciliation（20:35）

**不要**吸收：Morning Brief、AI Trader Evening、Daily Review Routine。它们是消费者，不是数据面。

切换条件（尚未满足）：

1. 安装脚本进入规范树的 Git 跟踪，并由用户管理员安装（先 Disabled）。
2. Pipeline **自己**跑完五段，且 Stage 3 子进程不是 `skipped_already_completed`。
3. 调度器自己触发的两个自然日 shadow，不是手动 trial。
4. 用户明确 Switch；只停上面四条；保留 7 天回滚。

当前标签必须仍是：`source_ready_not_installed` / `pipeline_not_switchable`。任何「可以装了 / 全绿可切换」都过卖。

---

## 2026-09-03 已核实的盘面（不要被顶层 completed 骗）

看子进程 stdout，不要只看 Pipeline 顶层 `completed`。适配器把 `skipped_already_completed` 算进顶层成功。

### `trial-20260903-full`（23:08–23:22）

- Universe：真跑，120s，`accepted=true`，5555→5554，`stock_zh_a_spot`
- Market Refresh：真跑，289s，`already_had_bar=5548`（幂等跳过正确），新闻 38 只刷新
- **Research Chain：1.9s，`skipped_already_completed`，搭 18:30 旧链 `daily-data-chain-20260903T103006Z-8ca8d9a57b`。`upstream_reuse.json` 写了且 accepted，stdout 里没消费。**
- Reconcile：真跑，429s，effective 5554/5554，`retry=[]`，6 只核验停牌从 retry 扣除
- Health：`preview_generated`，没真推

### `trial-20260903-r8-full`（23:46 起，用户叫停）

- 接线**没改**就又跑了一轮全量联网
- Stage 3 再次 1.9s skip，同一条旧链
- 停时锁已 `released`（23:59:58）

### 根因（已修，待生产复跑）

不是「设计成永远搭便车」。

- **内层** `_run_daily_data_chain_impl`：有 `upstream_reuse` 时禁止被同日 completed skip 抢走（注释已写明）。
- **外层** `run_daily_data_chain()`：CLI 默认 `--retry-from-latest true`，先 `preflight`，发现同日 completed 直接 return，**进不了内层**。

2026-09-04 00:15 左右 Grok 已把外层改成：仅当 `upstream_reuse is None` 才走 skip。旧链 18:30 重试行为不变。

新增测试：`tests/test_daily_data_chain_upstream_reuse_preflight.py`  
补丁前两条 reuse 测试就是 `skipped_already_completed`；补丁后与既有 skip 测试一共 54 passed（acceptance + universe_refresh + t1 recovery + 新文件）。

**生产盘面还没有一次 Stage 3 独立跑通。** 本 briefing 写出时，Grok 正准备对 `target_date=2026-09-03` 发 `trial-20260904-stage3-wiring`（联网、manual）。请以 `results/data_close_pipeline/runs/<run_id>/checkpoint.json` 里 Stage 3 的 `stdout_json.status` 为准：若仍是 `skipped_already_completed`，接线在生产上没生效。

Stage 3 子进程超时上限 5400s。若特征重建超过 90 分钟会被 Pipeline 杀掉——这也是审查点。

---

## 请重点审的问题

1. 外层 `upstream_reuse is None` 才 skip，会不会误伤旧 18:30 重试？非法 reuse 进内层变 `blocked` 是否比 hitchhike 更诚实？
2. Pipeline 适配器把 `skipped_already_completed` 映射为顶层 `completed`，验收时是否必须强制看子进程？
3. 双写：旧四条任务仍启用。Pipeline 现在跑 Stage 3 会双写链产物。Switch 前这是否可接受？
4. 安装/Switch 脚本在磁盘上有，但 `git ls-files` 对 runner/installer 曾为空——规范树未跟踪能否给计划任务用？
5. Universe 门：最新 JSON `accepted=false` 但 CSV 来自批准源且 ≤14 天可降级可用；会不会把本地缓存假扮成市场名册？（2026-09-01 15:30 课）
6. `raw_coverage` vs `effective_coverage`：核验停牌是否只从 retry/effective 扣除，raw 是否仍可见？
7. Health 默认 preview_only；Switch 脚本是否 fail-closed 要求 `--enable-alert-network --alert-push` 且不把 SendKey 写进任务？
8. 脏工作区：约数十个已改/未跟踪文件。全量 pytest 曾有 `test_021` 触发保护目录写入 tripwire。不要为了绿而关 tripwire。

---

## 建议另传的小文件（按需，不要一次全塞）

优先：

- `tests/test_daily_data_chain_upstream_reuse_preflight.py`
- `quantdesk/data_close_upstream_contract.py`
- `scripts/run_daily_data_chain.py`
- `scripts/run_data_close_pipeline.py`（约 56KB）

其次（大，分段问）：

- `quantdesk/daily_data_chain.py` 里 `run_daily_data_chain()` 外层 preflight，以及 `_run_daily_data_chain_impl` 的 `upstream_reuse` 分支
- `tools/admin/switch_data_tasks_to_pipeline.ps1`（若磁盘上有）
- `AGENTS.md` 里 Non-Negotiable + Multi-Agent Git 两节

不要传：`data/`、`results/` 整目录、ledger、paper 持仓。

若需要证据摘录，让本机助手只导出某个 `manifest.json` 的 **stage 摘要**（去掉 `universe_symbols` 长列表）。

---

## 代码锚点（审外层接线）

文件：`quantdesk/daily_data_chain.py` 的 `run_daily_data_chain()`

意图：

```text
if retry_from_latest and symbols is None and not dry_run and upstream_reuse is None:
    preflight → 同日 completed 则 skipped_already_completed
# 只要 Pipeline 传入 upstream_reuse（哪怕稍后校验失败），就必须进 impl
```

内层已有：`upstream_reuse is not None` 时 recovery 记 `upstream_reuse_provided`，校验失败则 `blocked`，成功则跳过 universe/daily provider，继续特征/研究链。

Pipeline Stage 3：`scripts/run_data_close_pipeline.py` 的 `stage_research_chain` 传 `--reuse-upstream-manifest`；CLI 默认 `--retry-from-latest true`。这就是外层必须开口的原因。

---

## 计划任务（2026-09-03 夜核实，不要改）

| 任务 | 状态 | 备注 |
|---|---|---|
| Universe Baseline Refresh | Enabled，当天 LastResult=1 | 15:30 失败过；Pipeline 当晚 universe 曾成功 |
| Daily Data Refresh | Enabled，LastResult=0 | 15:40 |
| Daily Data Chain | Enabled，LastResult=0 | 下次 09-04 15:45；当天成功的是 18:30 那次 |
| Full A Daily Reconciliation | Enabled，LastResult=0 | |
| Daily Review Routine | Enabled，LastResult=78 | 消费者，不是数据面 |
| Morning Brief / AI Trader Evening | Enabled，LastResult=0 | 消费者 |

没有已安装的 Data Close Pipeline 计划任务。

---

## 你交回时请用的格式

1. 总判：能否 Switch（是/否 + 一句话原因）
2. P0 / P1 / P2 列表（每条：现象、文件、为何危险、建议）
3. 被顶层 `completed` 掩盖的不诚实处
4. 你还需要哪些文件才能下结论
5. 明确：**不要**给「现在就安装」的建议，除非上面四条切换条件都成立

审阅范围默认只覆盖数据面 Pipeline 接管。8000 UI、AI Trader、策略公式、回测收益不在本次，除非发现越界。
