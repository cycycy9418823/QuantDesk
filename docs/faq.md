# QuantDesk FAQ

## Does v3.7.0.4 make trading recommendations?

No. It is research-only. The git deletion audit only reports risk, and the capital / lot sensitivity report only evaluates whether small capital and 100-share lots distort low-price baseline tests.

Research diagnostics only. Not investment advice. QuantDesk outputs local research diagnostics and paper-only workflow artifacts; they are not investment advice.

PIT universe data is optional and must be supplied locally. The clean zip excludes runtime data and includes `RELEASE_ARTIFACT.json` as release proof.

## Is Similar Day a prediction?
No. It is a historical similarity search over local index features. Similar history does not imply future repetition.

## Why can Similar Day return warning?
It needs local index CSV history. If `data/index` is missing, real mode warns and returns no fabricated real matches.

## What is Stress Test based on?
It uses predefined historical windows and computes impact from local symbol history, then sector or index proxy if needed.

## Why use index proxy when symbol history is missing?
The proxy is a fallback estimate with source labels. It is less precise and remains research-only.

## Is Style Attribution commercial Barra?
No. It is a transparent 10-factor local-data lite model for exposure and contribution diagnostics.

## How should I read style exposure?
Large positive or negative factor exposure means the paper portfolio is tilted toward that local factor definition.

## How does Decision Journal compute hit rate?
Reviewed entries compare forward returns against the entry's simple thesis direction. Missing market data leaves outcomes pending or data_missing.

## What is synthetic-test-data?
Synthetic data is demo/test data. It is explicitly marked and never pretends to be real local market data.

## Why is data/index excluded from clean zip?
It is runtime/user data. Clean release zips include templates, not private or generated data.

## How do I prepare style_exposures.csv?
Use `templates/data/style_exposures_sample.csv` and provide `symbol,date,size,value,growth,momentum,volatility,liquidity,beta,leverage,profitability,industry,source`.

## 风险警报会自动卖出吗？
不会。v3.6.1 风险警报只做 paper-only / research-only 提醒，不连接券商，不修改真实仓位。

## Smart stop 是真实止损单吗？
不是。Smart stop 是纸面止损观察线，用于提示用户复核风险，不会提交任何订单。

## 通知系统会自动发外部消息吗？
默认不会。`notifications.enabled=false` 且 `dry_run=true`，外部渠道必须显式配置才会尝试。

## dry_run 是什么意思？
`dry_run=true` 表示只生成本地报告和本地 JSONL 记录，用于验证通知内容，不做外部发送。

## 为什么 risk_alerts 缺真实持仓时是 warning？
缺本地纸面持仓不是程序错误。QuantDesk 会保留 warning，让用户知道需要提供 `state/` 或 `results/` 中的纸面输入。

## 如何让 risk_alerts 读取我的纸面持仓？
提供 `state/auto_paper_portfolio.json`、`state/paper_account.json`、`state/paper_watchlist.json`，或生成 `results/multi_strategy_account_book_latest.json`、`results/intraday_live_workbench_latest.json`。

## notification dedupe 是什么？
去重状态默认写在 `state/notification_dedupe.json`。相同 `dedupe_key` 在 cooldown 窗口内不会重复真实发送。

## intraday partial daily 会进入回测吗？
不会。盘中 partial daily 只允许用于 intraday/risk alert 观察，不进入 backtest / factor / walk-forward。

## v3.6.0 为什么说是 skeleton/lite？
v3.6.0 建立了模块、CLI、payload、UI 和测试骨架，许多能力是 synthetic-first 或 local-CSV adapter-first，不代表深度生产完成。

## v3.6.1 和 v3.6.0 的区别是什么？
v3.6.1 把风险骨架接到真实纸面输入、Smart Stop、Risk Alerts、Notification dry-run、本地 UI 和 daily snapshot。

## QuantDesk 会自动买卖吗？

不会。QuantDesk 是 research-only / paper-only 工具，不接券商、不真实下单、不自动交易、不自动调仓。

## Strategy recommendation 是买卖建议吗？

不是。`qd strategy-recommend` 只给出研究分类，例如 `long_swing`、`board`、`observe`、`avoid`，并附带风险标签和解释。

## Smart stop 会自动平仓吗？

不会。`qd smart-stop` 只输出纸面风控预警和研究假设，不修改持仓，不连接券商，也不触发真实交易。

## Risk alerts 会自动下单吗？

不会。`qd risk-alerts` 只读取本地快照并生成提醒项，所有提醒都只是研究预警。

## SourcePortable 为什么需要 Python？

`QuantDesk_v3.6.0_SourcePortable.zip` 是源码便携包，不内置 Python runtime。只有真实生成 standalone artifact 时，才可能不依赖本机 Python。

## raw / forward / backward adjusted data 有什么区别？

`raw` 保留原始价格，`forward` 使用前复权口径，`backward` 使用后复权口径。v3.6.0 会显式记录 `price_adjustment`，避免不同口径混用。

## 没有资金流或龙虎榜数据怎么办？

相关模块只提供本地 CSV adapter。缺文件时返回 warning，不伪造外部数据，也不默认联网抓取。

## 通知系统默认会发外部消息吗？

不会。通知默认 `enabled=false` 且 `dry_run=true`。外部渠道需要用户显式配置，并且密钥不会写入报告。

## Execution realism 会改变旧回测吗？

不会。执行真实性模型默认关闭，只有显式传入 `--execution-realism true` 才会记录部分成交、涨停成交概率、动态滑点和费用假设。

## Decision journal 存在哪里？

默认存放在 `state/decision_journal.jsonl`。`state/` 是本地运行态目录，不进入 clean zip。

## Installer 没生成怎么办？

如果 PyInstaller 或 NSIS 不可用，构建状态会诚实标记为 `skipped`。SourcePortable 仍会生成；不会伪造 exe 或 setup。

## 策略推荐器是什么？

它把板块状态、个股本地特征和风险标签合成为研究分类，用于帮助复盘和筛选观察对象。

## 它会直接告诉我买入吗？

不会。输出不应被理解为实盘指令，也不会写回真实交易状态。

## long_swing / board / observe / avoid 分别是什么意思？

`long_swing` 是趋势研究候选，`board` 是高波动打板风格观察，`observe` 是等待更多确认，`avoid` 是当前风险占优。

## 板块状态如何影响个股策略？

领先或转强板块会提高趋势/短线研究分数；走弱板块会增加风险标签，并把结果推向观察或回避。

## confidence 怎么读？

`confidence` 是 0-100 的规则评分，不是胜率，不是收益承诺。

## 为什么某些股票会被标记 avoid？

ST、停牌、低流动性、高波动、板块走弱、关键输入缺失等风险会导致 `avoid`。

## strategy-recommend 和 sector-rotation 有什么关系？

`sector-rotation` 提供板块热度、排名和趋势上下文；`strategy-recommend` 把它与个股特征、风险标签合并。

## strategy-recommend 和 risk attribution 有什么区别？

前者是当前候选分类，后者是组合表现或风险暴露解释。

## UI 为什么删除顶部导航？

重复顶部导航会增加认知负担。v3.5.2 之后使用分组侧边栏，顶部区域留给页面状态和主操作。

## Multi-strategy account book 是什么？

它是 research-only / paper-only 的多策略虚拟账本，用于拆分观察不同策略的纸面表现，不接券商，不真实下单。

## Intraday live workbench 是什么？

它是盘中观察工作台，只读取本地或显式授权的数据源，展示临时状态和风险提示，不做真实交易。

## Partial daily 可以进回测吗？

不可以。`partial_daily` 是盘中临时数据，不能进入 backtest、factor、walk-forward 或历史回放。

## live --init 会做什么？

`qd live --init` 只初始化本地 research-only 状态模板，不接券商，不创建真实账户。

## Experiment Tracker Lite 是什么？

它记录研究实验参数、输出路径、配置摘要和指标，帮助比较实验，不改变策略公式或交易状态。

## Daily data retention 是什么？

它把每日关键本地文件做留存 manifest，便于复现和审计，不保存 API Key。

## Reproducibility manifest 有什么用？

它记录本地文件 hash 和运行上下文，帮助比较两次研究环境是否一致。

## Local daily cache 是什么？

它是本地日线缓存层，用于减少重复读取或抓取；缺数据时只提示 warning，不伪造数据。

## Daily Cache Index 是什么？

`daily_cache_index.json` 是缓存索引，用于快速判断本地日线数据的新鲜度。

## Provider network smoke 是什么？

它是显式联网烟测，只有用户运行 `qd network-smoke --enable-network` 时才尝试外部 provider。

## Optional LLM observation agent 默认启用吗？

不启用。LLM 观察报告是可选功能，默认关闭，并且不能做交易决策。

## Agent evidence guard 做什么？

它约束可引用事实和数字，降低报告幻觉风险，输出仍然只是研究文本。

## Factor evaluation 会改默认评分公式吗？

不会。因子评估是研究诊断，不会自动改默认评分公式。

## Dynamic factor model 会替代默认模型吗？

不会。动态因子模型只输出研究组合权重和假设，默认不接管正式评分。

## Stress test 是真实预测吗？

不是。压力测试是情景假设，用来理解纸面风险暴露。

## Similar day 是预测工具吗？

不是。类似日检索只做历史特征对照；缺历史数据时 warning，不伪造结果。
## v3.7.0 Data Backbone

### 为什么不直接删除 CSV？
CSV 是兼容和回滚路径。v3.7.0 只加入 SQLite shadow layer。

### 什么是 SQLite shadow layer？
它是本地数据库影子缓存，不是唯一数据源。

### DualWriter 是什么？
同一批数据可同时写 CSV 和 SQLite；任一侧失败都会在状态中说明。

### 为什么 forward/back/raw 要分三张表？
三套复权语义不同，混表会污染研究和回测。

### partial_daily 为什么不能进入回测？
partial daily 是未完成盘中近似数据，只能用于盘中观察。

### SQLite WAL 是否支持并发写？
WAL 改善并发读，但写入仍需单写者锁。

### 为什么需要单写者锁？
避免多个写事务互相阻塞或造成半写状态。

### AKShare 不可用怎么办？
命令会 warning，并保留 SQLite/CSV/local fallback。

### mootdx 为什么不作为完整财务报表源？
mootdx 在本项目中只标注为 limited basic indicators。

### 分钟线为什么先用 Parquet 而不是 DuckDB？
v3.7.0 不新增 DuckDB 硬依赖，分钟线正文先放 Parquet/cache。

### read_prefer_sqlite 什么时候打开？
等迁移 compare 通过并确认本地数据覆盖后再显式打开。

### 如何验证 SQLite 与 CSV 一致？
运行 `qd data-db compare --sample 20 --json true --summary true`。

### 如何回滚到 CSV？
运行 `qd data-db disable --json true --summary true`。

### qd data-db reset / disable / enable 分别做什么？
`reset` 删除 SQLite db 但保留 CSV；`disable` 关闭 shadow layer；`enable` 重新打开但不默认改成 SQLite 优先读。

### data/quantdesk.db 为什么不进入 clean zip？
它是 runtime cache，不是源码 artifact。

### --repair true 会覆盖旧数据吗？
不会。v3.7.0 默认只标记问题；network=true 时也只补缺失日期，不覆盖已有行。
