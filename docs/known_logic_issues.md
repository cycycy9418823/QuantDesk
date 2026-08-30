# QuantDesk Known Logic Issues

Current operator surface is the 8000 daily-research app plus the supported 8001
AI trader classroom. Historical references below to `app/static/app.js` or the
8765 workbench are retained as legacy issue records only.

This document keeps the v3.2.2 core-correctness baseline visible while adding newer diagnostic watchlists. Current releases build on v3.2.2 without changing the scoring formula, backtest return logic, or default strategy parameters.

## v3.3.2.2 Current / Fixed / Watchlist

### v3.3.2.2 CI Stability and Repo Hygiene Patch

- **Fixed / improved**: `self_check` no longer depends on long historical comments in `version.py` for key data-freshness, candidate-contract, and intraday-closure checks.
- **Fixed / improved**: `trading_calendar` caches local CSV loads and exposes `clear_calendar_cache()` for tests.
- **Fixed / improved**: placeholder provider rows are now planned diagnostics, not real failures.
- **Fixed / improved**: unreachable legacy code after `_normalize_intraday_frame` return was removed without changing paper execution behavior.
- **Current**: planned providers are not implemented by this patch; they remain documentation/roadmap placeholders.
- **Boundary**: no strategy, scoring, backtest return, provider protocol, broker, order, network default, LLM, or full-A default scan behavior changed.

## v3.3.1 Current / Fixed / Watchlist

### v3.3.1 Workbench UI Update

- **Fixed / improved**: The home page is now a 今日工作台 with status cards, a linear workflow, and up to three next actions instead of a loose stack of feature panels.
- **Fixed / improved**: The full command center is an independent left-nav page; it is no longer repeated at the top of every page.
- **Fixed / improved**: The 产物中心 groups important `results/` and `reports/` files so users can find outputs without browsing folders manually.
- **Current**: Workbench status is only as current as the latest local result files. It does not rerun checks, fetch data, call LLMs, or repair missing data.
- **Boundary**: UI suggestions are data/process/validation hints only. They are not trading advice and do not change strategies, scoring formulas, backtest logic, or parameters.

## v3.2.9 Current / Fixed / Watchlist

### v3.2.9 Parameter Sensitivity Update

- **Fixed / improved**: `param-sensitivity` can flag narrow peaks, parameter cliffs, drawdown sensitivity, and insufficient sample size.
- **Current**: It is not a parameter optimizer and must not be used to select “best parameters” for trading. A stable robustness score only means the tested historical sample was less sensitive to the audited perturbations; it does not imply future effectiveness.
- **Boundary**: The audit does not modify strategy parameters, scoring formulas, backtest return logic, provider data, or paper portfolio state.

## v3.2.8 Current / Fixed / Watchlist

### v3.2.8 Data Health Update

- **Fixed / improved**: `data-health` makes local日线 freshness、分钟线覆盖率、provider failed/skipped 分布和 data_grade 结果一眼可见。
- **Fixed / improved**: `network_disabled` is counted as offline skipped, not as a true provider failure.
- **Current**: data-health does not download or repair data. Missing daily or intraday files remain missing until the user runs an explicit provider workflow or imports local CSV.
- **Current**: data_grade is a data readiness label, not a trading signal or investment recommendation.

## v3.2.7 Current / Fixed / Watchlist

### v3.2.7 Governance Update

- **Fixed / improved**: `operation_log` records command outcomes and output paths so recent work is easier to audit.
- **Fixed / improved**: `archive-results` can gather existing latest outputs into a dated folder without failing on missing files.
- **Fixed / improved**: `command-center` summarizes recent CLI status from local logs and does not rerun tasks.
- **Current**: command-center accuracy depends on commands having been run through `qd.py` or governance-aware scripts. An empty operation log is normal in a clean zip.
- **Current**: archive manifests only prove local files were copied; they do not validate market data correctness.

## v3.2.6 Current / Fixed / Watchlist

This document is refreshed for `v3.2.6-backtest-statistical-validation-and-feature-quality-audit`. It preserves the v3.2.2 core-correctness fixes as current baseline context. Older v2.x notes are kept below as archive context; current operational limits are listed here first.

### v3.2.6.1 Feature Audit Empty Data Update

- **Fixed / improved**: clean zip environments without `data/daily/` now make `feature-audit` return warning with `insufficient_data`, `skipped_reason`, and next action, rather than a process failure.
- **Current**: the warning does not create data. Users still need `daily-retain --network true` or local CSV import before meaningful feature-quality statistics can be computed.

### v3.2.6 Statistical Validation And Feature Quality Update

- **Fixed / improved**: `feature-audit` records local feature missing rates, NaN/Inf counts, coverage by symbol/date, and high-missing warnings.
- **Fixed / improved**: `stats-validate` adds permutation-test and bootstrap-CI diagnostics for existing backtest outputs.
- **Current**: statistical validation is only as good as the local backtest sample. Small samples produce `insufficient_sample` warnings and must not be treated as proof of strategy stability.
- **Current**: `p_value` is not a future-return guarantee. It only describes the current sample versus a local random baseline.
- **Current**: high feature missing rates lower confidence in strategy observation, but the audit does not repair data or change strategy output.

### v3.2.4 Daily Retention Update

- **Fixed / improved**: `daily-retain` records local version, explicit symbols, provider trace, strategy validation summary, observation/audit paths, data status, command args, and file hashes under `results/daily_retention/`.
- **Current**: retention depends on local files that exist at run time. Missing daily or minute data is recorded as `missing`; QuantDesk must not fill it with daily bars or realtime quotes.
- **Current**: manifest hashes are reproducibility checks only; they do not prove market data correctness.
- **Fixed / guarded**: manifest 不保存 API Key, provider secret contents, or real account information. Runtime retention outputs stay under `results/` and remain excluded from clean zip.

### v3.2.3 Validation Guard Update

- **Fixed / improved**: `strategy-validate` now checks for future-date leakage, daily-bar intraday proxies, missing-minute fills, no-fill state changes, T+1 violations, and board-candidate fill risk.
- **Current**: validation depends on local result files. If execution or intraday status files are missing, the report must warn that the risk cannot be fully verified.
- **Watchlist**: validation is diagnostic only. It must not be treated as a high-return strategy search or as proof that paper fills are real fills.

### Fixed In v3.2.2

- **Trading calendar default**: `data/calendar/a_share_trading_calendar.csv` is now included as static baseline data. `trading_calendar.py` prefers this CSV and reports `calendar_quality`, coverage range, and `coverage_warning`.
- **Package version drift**: `pyproject.toml` no longer hard-codes `version = "3.0.0"`; packaging reads `quantdesk.version.PACKAGE_VERSION`.
- **mootdx duplicate skeletons**: old duplicate `fetch_realtime_quote`, `fetch_daily_bars`, `fetch_intraday_bars`, and `fetch_fundamentals` skeleton definitions were removed.
- **Board candidate transparency**: backtest assumptions now include `board_candidate_execution`, explaining that paper fills can include situations that may be hard to execute in a real limit-up queue.

### Current Limits

- **A-share calendar coverage**: local CSV coverage is required for accurate holiday handling. Dates outside the CSV range fall back to simplified weekday logic and must be treated with caution.
- **2027 calendar rows**: 2027 rows are marked provisional and should be refreshed when official exchange holiday arrangements are published.
- **Historical minute data**: providers may not supply historical 1-minute rows. CSV import remains the reliable path for old intraday replay.
- **Board candidate paper fills**: paper simulation cannot represent real queue priority or actual fill probability at limit-up prices.
- **Optional LLM reports**: LLM mode remains disabled by default. When enabled, it is for learning review only and passes policy, evidence, and grounding guards.
- **Provider failures**: provider failure never creates substitute market rows. Failed or skipped providers remain explicit in results.

### Boundary

QuantDesk does not connect to brokers, does not place real orders, does not save real account information, does not promise returns, and does not constitute investment advice.

> v2.5.2 — 已知或潜在逻辑问题清单。v2.6.7 补充第 8 / 9 条。

## 9. --spot-source cache 不代表日线完全离线

- **状态**: 已知，v2.6.7 文档补齐
- **影响**: 中。`scripts/daily_quant_report.py --spot-source cache` 只控制快照（spot snapshot）来源，**不**保证候选股日线计算路径完全脱网。当本地 `data/daily/*.csv` 缺失或时间戳不满足新鲜度规则时，日报阶段仍可能尝试通过 AKShare / Sina / Eastmoney fallback 获取日线，进而导致用户误以为"我已经离线了，怎么还在联网"。
- **建议修复版本**: v2.6.8+
- **缓解方案 / 下一步**:
  - 在 README / docs/data_flow_map.md 中明确解释 `--spot-source` 的语义。
  - 后续考虑新增 `--daily-source cache-only` 或 `--no-daily-refresh` 开关，让 `daily_quant_report.py` 在缺失数据时直接 SKIP/降级，而不是触发网络抓取。
  - 本轮（v2.6.7）不强制实现完全离线模式，避免引入未测试的联网降级路径。

## 8. update_market_data.py 同时运行多份会互相覆盖状态文件

- **状态**: 已修复（v2.6.7）
- **影响**: 高。两个并发 `update_market_data.py` 进程会同时写 `data/cache/update_status.json` 和 `data/cache/update_status_running.json`，UI 进度互相覆盖；Windows 上还会更容易触发文件锁。
- **修复**: v2.6.7 新增 `quantdesk/update_task_lock.py`，使用 `data/cache/update_market_data.lock` 串行化更新任务；`scripts/update_market_data.py` 和 `POST /api/update-market-data` 都拒绝启动第二个并发更新；前端 `app/static/app.js` 在按钮点击前先调用 `/api/update-market-data/progress` 检测 `running=true` 并提示"已有市场数据更新任务正在运行"。Stale lock（pid 已不存在）会在下一次获取时被自动清理。

## 1. report_run_status.sample_count 可能被误读为全市场数量

- **状态**: 已知
- **影响**: 中。如果 UI 将 sample_count 显示为"样本数"而不做区分，用户可能误认为本地只有 28 只股票，实际全局库有 5000+ CSV。
- **建议修复版本**: v2.5.2+
- **缓解方案**: UI 顶部状态栏分开显示 global_history_status.csv_count 和 report_run_status.sample_count。

## 2. Backfill 可能在 UI 中被无感知触发

- **状态**: 已修复 (v2.5.1)
- **影响**: 高。首次建库 backfill 可能耗时数小时，不应在页面加载时触发。
- **修复**: v2.5.1 将 backfill 按钮移到"高级"区域并要求确认。

## 3. history_short 股票可能被增量更新反复重拉

- **状态**: 已知
- **影响**: 低。历史不足的股票每次增量更新时可能尝试拉取多年历史，增加网络和时间消耗。
- **建议修复版本**: v2.6+
- **缓解方案**: 增量模式下如果本地已有最新日线，即使历史不足也只标记 `history_short`，不重新下载。

## 4. provider_summary 统计可能跨多次累计

- **状态**: 已知
- **影响**: 低。如果多次运行更新，provider_summary 只反映最后一次。
- **建议修复版本**: 不急
- **缓解方案**: 每次更新重置 provider_summary。

## 5. walk-forward backtest 在历史不足时可能产生偏差结果

- **状态**: 已知
- **影响**: 中。UNAVAILABLE / OBSERVE_ONLY 级别的股票做 walk-forward 可能因数据不足导致结果不可靠。
- **建议修复版本**: v2.6+
- **缓解方案**: backtest_quality 评分已在 v2.4+ 标记 INVALID/LOW，UI 中加注"样本偏差"提示。

## 6. 虚拟持仓市值在 15:00 前仅为估算

- **状态**: 设计如此
- **影响**: 低。盘中行情来自缓存 spot_snapshot，可能延迟。15:00 后需重新生成日报以获得正式结算。
- **缓解方案**: UI 中明确标注"等待今日收盘，仅估算"。

## 7. 长报告文件名可能超出 UI 卡片宽度

- **状态**: 已知
- **影响**: 低。文件名过长时可能撑破卡片布局。
- **建议修复版本**: v2.5.2+
- **缓解方案**: CSS `word-break: break-all` 和 `overflow: hidden`。

## 声明

本工具不提供投资建议，不接券商，不自动下单，不预测未来价格。
