# Project Alignment — 2026-06-13

**所有 AI Agent 必须对齐此文档。不要再各干各的，不要生成多个版本。**

---

## 一、哪个是真正的服务器/前端

| 组件 | 正确路径 | 端口 | 框架 |
|------|---------|------|------|
| **主服务** | `scripts/quant_api_server.py` | **8000** | Flask |
| **前端模板** | `app/templates/daily_research.html` | — | Jinja2 |
| **前端 JS** | `app/static/js/daily_research.js` | — | 原生 JS + ECharts |

### 不要碰这些

| 旧版 | 说明 |
|------|------|
| `scripts/quant_app_server.py` | 端口 8765，旧版，**不要再改**；Round 21 起 `/api/paper-buy`、`/api/paper-sell` 返回 HTTP 410 且不写任何账本 |
| `app/index.html` | 端口 8765 的前端，**不要再改** |
| `app/static/app.js` | 端口 8765 的 JS，**不要再改** |

正式纸面账本唯一为 `state/auto_paper_portfolio.json`（详见
`docs/CURRENT_STATE.md` 首屏"30 秒对齐"与 `docs/architecture.md` Active Stack 节；
三处如有分歧须同一次变更内一起修正）。Stage 4 三账户回放是合成研究账户，
**Stage 4 ≠ 用户账**。

如果 Codex 或其他 Agent 在改 8765 的这些文件，**应当停止**。所有精力集中到 8000 端口。

---

## 二、当前设计方向

> **2026-06-14 主题更新**：用户反馈深色主题（slate/OLED）文字看不清，
> 已切换为**浅色暖灰主题**。`design-system/MASTER.md` 与
> `app/templates/daily_research.html` 已同步。下面的浅色调色板为准。

```
背景:    #F5F0E8              暖卡其/米白（不是冷蓝灰、不是纯白）
卡片:    #FFFFFF              纯白卡片/面板
文字:    #0F172A              主文字（近黑）
次要文字: #475569              正文
边框:    #D6CFC4              卡片边框
主色:    #1D4ED8              蓝，链接/可点击
强调:    #7C3AED              紫，入选标记
上涨:    #DC2626              A股红涨（深红，浅底可读）
下跌:    #15803D              A股绿跌（深绿，浅底可读）
警告:    #B45309              琥珀
信息:    #1D4ED8              蓝
```

风格方向：专业、数据可读性优先、不刺眼、适合长时间盯盘。
完整设计系统见：`design-system/MASTER.md`

---

## 三、最近已完成（截至 2026-06-14）

### 主题与板块（2026-06-14 追加）
- [x] **浅色暖灰主题**：daily_research.html / daily_research.js / MASTER.md 全部从深色 slate 切到浅色 `#F5F6F8`；红涨绿跌改为深红 `#DC2626` / 深绿 `#15803D`（浅底可读）
- [x] **行业板块**：板块涨跌榜从"按交易所分（主板/创业板）"改为东方财富真实行业板块（银行/半导体/白酒等），通过 `POST /api/sector/refresh` 联网拉取并本地缓存
- [x] 前端新增"刷新板块"按钮 + JS handler

### 数据与API
- [x] 新闻提供者从占位符改为直连东方财富公告 API
- [x] 新闻 `parents[1]→parents[2]` 路径修复 + 启动时后台自动刷新
- [x] 行业板块启动时自动刷新（东方财富真实行业板块：银行/半导体/白酒等）
- [x] 上证指数数据下载（`data/benchmark/sh000001.csv` 8660 行）
- [x] 市场快照刷新（`data/cache/spot_snapshot_latest.csv` 5526 只）
- [x] `calculate_sector_stats()` 改为真实行业板块缓存读取
- [x] `load_benchmark_index()` 移除脆弱的 `abs(change)<=1` 启发式
- [x] `hs300.csv` 重新下载并统一 pct_chg 为百分比格式
- [x] 新增 `GET /api/news/status`、`POST /api/news/refresh`、`POST /api/sector/refresh` API

### 代码清理
- [x] 删除 22 个根级/子目录重复文件（paper/intraday/research 迁移遗留）
- [x] 创建 `v5_daily_report/_shared.py` 集中化 145 个 builder 工具函数
- [x] BOUNDARY 安全策略从 136 个文件复制改为 builder 函数调用
- [x] `cn()` → `z()` 全局替换

### 前端
- [x] 启动时自动检查/刷新新闻缓存
- [x] 打开候选详情时自动后台拉取新闻
- [x] NaN 值在图表中不再渲染为 0（改为 null，echarts 自动断线）
- [x] 详情弹窗新增 8 个跟踪字段（入选价、经过交易日、下次复核日等）
- [x] `findSelectIndex` 找不到日期时 console.warn

### 诊断工具
- [x] `scripts/check_data_completeness.py` 数据完整性一键诊断
- [x] `scripts/repair_missing_data.py --network` 缺失数据一键修复

---

## 四、当前状态 — 验证通过

```
compileall -q quantdesk scripts tests  →  通过
self_check.py  →  pass=487 warn=5 fail=0
6 个 API 端点测试  →  全部通过
15 个关键数据文件  →  全部存在
daily_research.js 语法  →  通过
```

服务运行：`python scripts/quant_api_server.py --port 8000`

---

## 五、下一步待做（按优先级）

> **2026-06-14 更新**：浅色主题 + 行业板块已落地（见下方"最近追加"）。
> 旧的"按方案B深色重写 CSS"和"图表配色升级"两项已完成/作废。

| P | 任务 | 涉及文件 |
|----|------|---------|
| P1 | 行业板块缓存刷新后的过期策略（目前每次手动点"刷新板块"） | `quant_api_server.py` / `daily_research.js` |
| ~~P1~~ 已闭合 | ~~字体替换（head 已接 Google Fonts）~~ → Round 21：模板已移除外链字体，离线优先；Fira 系回退到系统字体，如需启用请把 .woff2 放入 `app/static/fonts/` 并加 @font-face，禁止默认外网拉字体 | HTML head |
| P2 | 卡片 hover 效果、加载骨架屏、动画过渡（浅色主题下重新校准） | HTML + JS |
| P2 | 响应式滚动条样式（浅色） | CSS |
| P2 | `prefers-reduced-motion` 适配 | CSS |

---

## 六、绝对禁止

- ❌ 不要在 `quant_app_server.py`（端口 8765）上工作
- ❌ 不要在 `app/index.html` / `app/static/app.js` 上工作
- ❌ 不要触碰 broker、订单、真实账户
- ❌ 不要用纯黑色 OLED 背景，也不要回到深色 slate 主题（用户反馈深色文字看不清，已切浅色暖卡其 #F5F0E8）
- ❌ 不要改回深色主题，也不要改回冷蓝灰（daily_research.html 已是浅色暖卡其 #F5F0E8）
- ❌ 不要创建新版本的前端文件（如 daily_research_v2.html）
- ❌ 不要自作主张把新闻/预测功能设为"红线"禁止——用户明确需要这些能力作为研究线索。原则：提供真实数据，由用户判断用途。不编造数据，但不禁止真实数据的获取和展示。
- ❌ **不要修改其他 Agent 已经写好的函数签名和返回字段**。如果你需要改 `build_research_reference` 的返回结构或 `renderResearchReference` 的参数，先读当前代码确认不破坏前端 JS 的引用链。改后端字段必须同步改前端引用。改前端函数必须确认调用方传参不变。

---

## 七、关键文件索引

| 用途 | 路径 |
|------|------|
| 主服务器 | `scripts/quant_api_server.py` |
| 前端模板 | `app/templates/daily_research.html` |
| 前端 JS | `app/static/js/daily_research.js` |
| 新闻提供者 | `quantdesk/providers/news_provider.py` |
| 列名别名 | `quantdesk/column_aliases.py` |
| 设计系统 | `design-system/MASTER.md` |
| 数据诊断 | `scripts/check_data_completeness.py` |
| 数据修复 | `scripts/repair_missing_data.py` |
| v5 共享工具 | `quantdesk/v5_daily_report/_shared.py` |
| 质量报告 | `docs/quality_assessment_report_2026-06-12.md` |
| 隐患报告 | `docs/followup_issues_2026-06-12.md` |

---

## 八、2026-07-16 起：AI Fund Manager 接线阶段

- 当前状态页：`docs/CURRENT_STATE.md`，用于快速核对数据资产、能力接线状态与唯一行动队列。
- 唯一任务队列与证据 checkpoint：`docs/internal/NEXT_TASKS_AND_WORKTREE_RULES.md`。
- 新阶段继续只使用 8000 主服务与现有浅色前端；8765 旧栈保持冻结。
- 当前执行顺序为 Stage 0 -> 6 -> 1 -> 2 -> 3 -> 4 -> 5，每个 Stage 完成后必须停下等待外部验收。
- 风险门真拦截 3b 尚未批准，保持 shadow；Research Only、Paper Account Only、No Broker Route 边界不变。

## 九、2026-08-13：因子裁决结论（Round 22/23）——所有后续 Agent 必读

生产五维评分与教科书式反转因子已经过**预注册裁决**（判据先于跑数定稿，
全 A 5573 只、PIT 官方名单、官方复权因子、周频 333 截面 2020–2026）：

| 对象 | 判决 | 一句话原因 |
|---|---|---|
| trend / momentum / activity / position | **kill** | 5 日 IC 显著为负（t -4.1～-5.9，逐年稳定），方向指反 |
| risk（低波动罚分） | **keep** | 唯一通过全部判据的维度 |
| rev5 / rev10 / rev20（反转因子） | **kill** | IC 为正且真实（rev20 t=4.6）但 ICIR 未达 0.30，且净价差扣双腿成本后为负 |

对后续 Agent 的硬约束：

1. **不得把评分/综合排序当作已验证信号**引用或宣传；披露块
   `score_dimension_adjudication` 已接入回测 assumptions、综合评分报告
   payload、回测审计 markdown，不得移除。
2. **用户已裁定生产权重不改**（只披露）。任何权重修改、维度删减、
   反转信号接入都需要用户明确批准 + 新的预注册轮。
3. 新因子想法必须走裁决管线：先写预注册判据文档（阈值定稿后不得因
   结果修改），再用 `quantdesk/research/factor_adjudication.py`（带与生产
   评分公式逐行等值的守护测试）跑判决。禁止先跑数后定判据。
4. 证据位置：`results/round22_factor_adjudication/`、
   `results/round23_reversal_factor/`（运行时产物，不入库）；预注册文档在
   `docs/internal/round22_factor_adjudication_preregistration.md` 与
   `docs/internal/round23_reversal_factor_preregistration.md`。
5. PIT 权威名单是 `data/metadata/security_master.csv`（+ st_history /
   suspension_history / adjustment_factors）；降级版
   `data/universe/historical_listings.csv` 不得再用作名单来源。

## 十、2026-08-15：云端迁移被驳回 —— 唯一写入方 = 本机（新增硬边界）

云端接管**没有**发生。项目运行方向不变：主服务仍是本机
`scripts/quant_api_server.py`（端口 8000），唯一正式纸面账本仍是本机
`state/auto_paper_portfolio.json`。

新增不可协商边界：

1. **`<repo root>` 是 canonical 行情、纸面账本与全部 append-only 证据树的
   唯一权威写入方。** 云端已被降为只读监控（marker 撤销 + 5 条写任务
   disabled）。任何 Agent 不得在两侧同时启用写任务，也不得"先停本机再查云端"。
2. **禁止用 `quant_clean_repo` 播种任何远端运行环境。** 08-15 审计已证明：
   云端源码与 `quant_clean_repo` 逐字节相同（4/4 哈希命中）、与权威工作树
   0/4 命中，导致 Round 22/23 裁决资产整体缺失，`ALIGNMENT.md` 停在 07-18 版，
   §九 强制的 `score_dimension_adjudication` 披露块被静默丢弃。
   `quant_clean_repo` 只是交付快照，不是可运行基线。
3. **任何未来切换必须同时携带证据树**：`data/features`、`data/sector_history`
   （锚定 T0=2026-07-27 与 Stage 4 全部维护门的 append-only 前向 PIT 归档）、
   `data/benchmark`，以及 `results/`（含 `results/risk_gate_shadow_daily`
   dated 归档）。缺任一项即视为证据链分叉，切换不得宣布完成。
4. 复核证据：`docs/CURRENT_STATE.md`「2026-08-15 权威覆盖」节与
   `docs/internal/NEXT_TASKS_AND_WORKTREE_RULES.md`
   「2026-08-15 cloud cutover audit」checkpoint。

## 十一、2026-08-16：云端资格重建，仍未切换写入方

用户已明确要求继续使用现有云端容器，但这只重新打开了“资格重建”范围，
没有改变唯一写入方裁决。通过 SSH 对现有实例的只读核对发现，历史记录中的
`/workspace`、`/workspace/quant-ops`、`/workspace/venvs/quantdesk313` 和
`/workspace/quant-runtime` 当前均不存在；实际根目录是 `/home/developer`，
且没有可恢复的 QuantDesk 数据/状态/证据树或专用 venv。故：

1. 本机仍是唯一 authoritative writer；云端 QuantDesk 写任务不得启用。
2. `/home/developer/quant-runtime` 只能在证明重启后持久化后创建，不能把临时
   容器层当作持久卷。
3. 先运行本地工程红门和 cloud qualification，再做源码 release、依赖安装、
   数据迁移和自然影子观察；未通过资格门不得复制大行情树或停用本机任务。
4. 当前产品边界仍是 Research Only、Paper Account Only、No Broker Route、
   Live Execution Locked；容器不会改变 Stage 4 缺失历史证据或 alpha 未验证的
   结论。

## 十二、2026-08-17：独立纸面 AI 交易员（`ai_trader/`，端口 8001）

这是**并行产品**，不是 8000 研究台的替代，也不是对正式纸面账
`state/auto_paper_portfolio.json` 的改写。

| 组件 | 路径 / 端口 | 说明 |
|------|-------------|------|
| 代码 | `ai_trader/` | 自己想办法、本地证伪、通过后才下纸面单 |
| 教室页 | `python -m ai_trader app` **8001** | 禁止接到 8000 / `daily_research.html` |
| 账本 | `state/ai_trader/ledger.json` | 新开 10 万现金账 |
| 禁止写入 | `state/auto_paper_portfolio.json` | QuantDesk 旧真账，封存 |

允许只读借用 `data/daily`、交易日历、手数、`cn_current` 成本。
`ai_trader` 画像是收盘后复盘的新人短线波段股民：按故事持仓、开盘追高则放弃，不是打板。
默认联网拉东财公告（``python -m ai_trader run --offline`` 可关）。
这不改变 QuantDesk 主栈默认不联网。方法全杀时开未验证观察仓，不得写成已验证 alpha。
禁止把 `ai_trader` 折进 Stage 4/5、云端切换、生产评分权重或 8000 UI。

## 十三、2026-08-26：AI 纸面交易员只读接入 8000 主入口（取代 §十二 部分约束）

用户裁决：**8000 是唯一推荐入口**。独立 AI 纸面交易员以只读方式接入主服务；
§十二 中"教室页禁止接到 8000 / daily_research.html"的约束自本日起被本节取代。

| 组件 | 路径 / 端口 | 说明 |
|------|-------------|------|
| 只读 API | `scripts/server/routes/ai_trader.py` → `/api/ai-trader/*` | dashboard / chat(GET,POST) / boundary，注册于 `scripts/quant_api_server.py` |
| 共享数据层 | `ai_trader/dashboard.py` | 无 Flask 依赖的只读 adapter；不重算权益/持仓/收益 |
| 前端 | `app/templates/daily_research.html` + `app/static/js/daily_research.js` | 新增 `#ai-trader-section` 模块页（无 v2 文件） |
| 兼容入口 | 8001 教室 | 保留为兼容/诊断入口，不再是主要入口 |
| 已退休入口 | 8002 桌面 | 已外移至 `<repo-root>_slim_quarantine_20260827\R2_8002\`；不再属于产品契约 |

不可协商边界（全部有测试钉住）：

1. Research Only / Paper Account Only；不接券商、不下单、无 QMT/PTrade 路由。
2. 不写 `state/auto_paper_portfolio.json`；AI 账本仍唯一为 `state/ai_trader/ledger.json`。
3. LLM 只提观点/选股/卖出建议；成交、现金、手数、T+1、涨跌停由现有铁面执行。
4. 聊天只能解释：不能改账本/计划/仓位，没有"聊天中买入/卖出"接口。
5. 没有"网页重跑晚间 cycle"按钮；20:20 计划任务仍是唯一自动决策入口。
6. proposed / committed / filled / rejected 四态在 API 与页面严格区分，
   提议绝不显示成已提交或已成交（journal 未来文案已同步修复，历史 journal 不改写）。
7. 缺失/陈旧/损坏产物必须显示 degraded/warning，不得伪装成功。

## 十四、2026-08-30：R4 迁移——代码仓换到 `<repo-root>_new`，老仓待归档

用户裁决：**不再往外搬退役代码，改为往里拉产品闭包建新仓**。理由是往外搬的上限是
历史堆积大小，且搬错不报错——那批"字节校验老 UI"的测试里混着现役护栏，整组降级会
静默失去保护；往里拉，漏掉的直接 ImportError。

| 项目 | 状态 |
|------|------|
| 新仓 | `<repo-root>_new`，`git init` + 基线提交已完成 |
| `data/` | 48 GB 独立副本（425 目录 / 174415 文件 / 0 失败），**不再是目录联接** |
| `state/` | 67 文件独立副本；66 项与老仓逐项 SHA256 一致，`ai_trader/register_evening_task.ps1` 为 1 项**有意差异**（已去掉写死的老仓绝对路径，改为从自身位置推导仓库根）。正式纸面账本与 AI 账本哈希不变 |
| `.venv` | 新仓自有 Python 3.13.2，不再借老仓 |
| self_check | `450 pass / 8 warn / 0 fail`，与老仓一致 |
| 8000 / 8001 | 均已在新仓实测启动，只读路由全 200 |
| 7 个计划任务 | **仍全部指向老仓**，切换需管理员提权 |
| 老仓 | HEAD `7a63103` 未动，待全部验收后改名 `_archive` 只读归档 |

退役的 8765 闭包（老服务端、老 UI、`server_payloads`、`live_monitor`、sample 链）
按设计不进新仓；`tools/git-hooks/check_staged.py` 硬拦它们重新进入。

**`app/agent_chat.html` 不属于退役闭包**：现役 8000 服务器注册了 agent 路由
（`scripts/quant_api_server.py`）且 `/agent` 读该文件。R1 清点原始清单从未列入它。

不可协商边界不变：Research Only / Paper Account Only / 不接券商 / 不下单 /
不构成投资建议。两仓在 `data/`、`state/` 独立后，**同一时刻只允许一边跑计划任务**。
