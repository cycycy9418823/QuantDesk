# QuantDesk

## 最简单启动方式

最简单的启动方式是运行（或双击）`.\start_ui.cmd`，或直接执行 `python scripts/qd.py app`。它会打开当前的 8000 主研究台；默认不附带 `--update-on-start`，因此不会在启动时请求 provider 网络。

主页面入口固定为 `http://127.0.0.1:8000/`。如需单独检查 AI 纸面交易员兼容教室页，可运行 `python -m ai_trader app`，浏览器入口为 `http://127.0.0.1:8001/`；8000 仍是唯一推荐入口。

sample helpers are retired from the active launch, clean-export, and desktop-shortcut flow. `run_ui_sample.cmd` 与 `run_report_sample.cmd` 仅保留为本地兼容 helper，不再作为当前操作入口，也不会进入 clean zip。

## Script Governance Snapshot

For project cleanup and handoff, start with the read-only script inventory:

```powershell
python scripts/qd.py maintenance script-inventory --json true --summary true
python scripts/qd.py maintenance test-inventory --json true --summary true
python scripts/qd.py maintenance test-discovery-diagnostics --json true --summary true
python scripts/qd.py maintenance test-tier-runner --tier tier0_fast_unit --dry-run true --json true --summary true
python scripts/qd.py maintenance frontend-api-contract --json true --summary true
python scripts/qd.py maintenance dependency-contract --json true --summary true
python scripts/qd.py maintenance data-backtest-contract --json true --summary true
python scripts/qd.py maintenance coverage-contract --json true --summary true
```

It writes maintenance JSON/Markdown under `results/maintenance` and `reports/maintenance`, plus bounded coverage XML under `reports/coverage`. It classifies `qd.py` entrypoints versus raw historical scripts, maps test tiers, probes unittest discovery hot spots, plans bounded tier runs, checks frontend/server API alignment, verifies dependency declaration drift, audits data/backtest evidence metadata, records maintenance coverage, and does not delete, move, rewrite, enable network, call LLMs, connect brokers, or place real orders. See [Development and test guide](docs/development_test_guide.md).

Current local app entrypoint: `python scripts/qd.py app` starts the daily research app through `scripts/launcher.py` on port 8000. `python -m ai_trader app` is the supported 8001 compatibility classroom. The older 8765 workbench is retired and kept only for compatibility review.

## Current Status

QuantDesk is a local, research-only A-share workflow for daily review, factor
diagnostics, backtests, experiment tracking, intraday observation, and
attribution. It does not connect to brokers, does not place real orders, and
defaults to `network=false`. CSV remains the compatibility fallback for the
SQLite shadow data backbone (`read_prefer_sqlite` defaults to false).

New code should import from subpackages such as `quantdesk.paper`,
`quantdesk.intraday`, `quantdesk.agent`, and `quantdesk.research`. The
`quantdesk.__init__` compatibility aliases keep old imports working.

Per-release history lives in [docs/history/](docs/history/) and `CHANGELOG.md`.

## 10 Minute Data Layer Quickstart

```powershell
python scripts/qd.py data-db init --json true --summary true
python scripts/qd.py data-db stats --json true --summary true
python scripts/qd.py data-db migrate --adjustment forward --sample 20 --json true --summary true
python scripts/qd.py data-health --json true --summary true
python scripts/qd.py data-query daily --symbol 600000 --adjustment forward --json true --summary true
```

`network=false` is the default. AKShare and other external providers are only called when explicitly enabled.

## Quick Links

- [5-minute quickstart](docs/quickstart.md)
- [Development and test guide](docs/development_test_guide.md)
- [User guide](docs/user_guide.md)
- [FAQ](docs/faq.md)
- [API payload schema](docs/api_payload_schema.md)
- [Release notes](docs/history/RELEASE_NOTES_v3.6.2.md)

## First Commands

```powershell
python scripts\qd.py self-check
python scripts\qd.py daily --network false --with-guardrails true --summary true
python scripts\qd.py live --init --summary true
python scripts\qd.py live --once --summary true
python scripts\qd.py attribution --synthetic-test-data true --summary true
python scripts\qd.py strategy-recommend --synthetic-test-data true --summary true
python scripts\qd.py data-integrity --synthetic-test-data true --summary true
python scripts\qd.py stock-picking-flow --synthetic-test-data true --summary true
python scripts\qd.py smart-stop --synthetic-test-data true --summary true
python scripts\qd.py risk-alerts --synthetic-test-data true --summary true
python scripts\qd.py risk-alerts --synthetic-test-data true --notify true --dry-run true --summary true
python scripts\qd.py similar-day --synthetic-test-data true --summary true
python scripts\qd.py stress-test --synthetic-test-data true --summary true
python scripts\qd.py style-attribution --synthetic-test-data true --summary true
python scripts\qd.py decision-journal --synthetic-test-data true --summary true
python scripts\qd.py risk-observation --summary true
python scripts\qd.py experiments --summary true
```

## Core Commands

- `qd daily`: daily research snapshot and report.
- `qd factor-eval`: factor IC / Rank IC diagnostics.
- `qd backtest --network false`: local backtest from local data.
- `qd backtest --pit-universe true`: optional PIT filtering when user-provided historical listings exist.
- `qd multi-strategy-book`: research-only virtual account book.
- `qd live --init` / `qd live --once`: intraday observation workbench, no broker, no real orders.
- `qd attribution`: true Brinson-Fachler allocation / selection / interaction plus Lite factor contribution.
- `qd strategy-recommend`: research-only strategy-style recommendation from sector status, symbol features, and risk flags.
- `qd stock-picking-flow`: daily research loop from data integrity to paper watchlist suggestions.
- `qd data-integrity`: adjusted-price layout and corporate-action checks.
- `qd smart-stop` / `qd risk-alerts`: paper-only warnings, no real exits or orders.
- `qd similar-day` / `qd stress-test` / `qd style-attribution` / `qd decision-journal`: research-only local-data analysis. Missing local data warns; synthetic data is demo-only.
- `qd notify --dry-run true`: local notification summary; external channels are disabled by default.
- `qd risk-observation`: five-dimensional diagnostics formerly exposed as risk attribution lite.
- `qd risk-attribution`: compatibility alias for `risk-observation`, not true Brinson attribution.
- `qd experiments`: list and diff local research experiment logs.
- `qd first-run`: safe local onboarding wizard; network remains off by default.

## Attribution Naming

`risk observation` explains diagnostics such as sub-account contribution, sector exposure, data risk, and provisional intraday context. `risk attribution` means true Brinson/factor attribution and lives under `qd attribution`.

## Packaging

The current scope is research-only (v580); installer / portable-release packaging
is intentionally out of scope, and the `build\build_portable_release.ps1` /
`build\build_installer.ps1` helpers are **not present** in this tree. Do not
assume they exist. `self_check.py` reports their absence as an expected WARN
under this scope.

To produce a clean source snapshot for handoff or backup, use the export helper
that does exist:

```powershell
powershell -ExecutionPolicy Bypass -NoProfile -File .\export_project.ps1
```

If release packaging is reopened as an explicit scope later, add the build
helpers back under `build\` before restoring packaging instructions here. See
[release build notes](docs/release_build.md) for background.

## Safety

QuantDesk is research software. It does not implement broker APIs, real orders, automatic trading, or automatic rebalancing. Reports, charts, attribution, observations, and experiments are not investment advice.

## Legacy Operator Notes

- v2.3.2 local daily cache docs: `update_market_data.py` and `local_daily_cache` read and write local cached daily bars only when explicitly used.
- Daily Cache Index: `daily_cache_index.json` accelerates local cache freshness checks and is exposed by `GET /api/daily-cache-index/summary`.
- 真实 provider 联网烟测: `python scripts/qd.py network-smoke --enable-network` is explicit, diagnostic, and 不构成投资建议.
- Provider chain note: `tencent,mootdx`; mootdx 是可选 provider and requires `python -m pip install mootdx`.
- Optional LLM Observation Agent remains off by default; `agent_policy_guard` sanitizes report language and 不构成投资建议.
- Daily Data Retention And Reproducibility Manifest records local file hashes and 不保存 API Key.
- 新手只需要记住 5 个命令: `status`, `data-health`, `daily`, `strategy-recommend`, and `app`.
- The intraday workbench docs remain explicit: 不接券商, 不真实下单, 不构成投资建议.
- Feature/stat validation docs include 置换检验 and remain 不构成投资建议.

## Legacy Docs Index

This compact index preserves historical operator anchors while v3.6.2 keeps the README short.

- Current release title: v3.6.2 Real Similar Day Stress And Style Attribution.
- Windows Quick Start: `powershell -ExecutionPolicy Bypass -File .\setup_windows.ps1`, `run_tests.cmd`, `self_check.cmd`, `run_daily_smoke.cmd`, `export_project.ps1`.
- Windows Python 选择: 文档里的 `python` 是占位命令; set `QUANTDESK_PYTHON`; run `env-doctor --json true`.
- RC1 Daily Usage Flow: generate a daily report, review Data Quality, Backtest Risk Summary, Candidate Decision Cards, Learning Appendix, and Risk Statement.
- Retired sample helpers: `run_report_sample.cmd`, `run_ui_sample.cmd`, `scripts\generate_sample_report.py`, `results\sample_report`. They remain local compatibility helpers and are no longer exported or installed as desktop shortcuts.
- 15:00 Settlement Rule: settlement-aware review text is for observation only; historical backtests do not guarantee future returns.
- Backtest limitations: 历史回测不代表未来收益; 组合回测仍未模拟盘口排队、部分成交和真实撮合深度.
- Data quality warning: 真实日报会联网抓取行情数据 only when explicitly enabled; default remains network=false.
- Safety phrases: 不接券商, 不真实下单, 不构成投资建议, 不会自动交易, does not connect live trading.
- Beginner Mode and Trust Decision Center: trust score is not a probability; raw strategy score is not reliability.
- News Status, 日线数据状态, 数据源诊断, 默认未联网检测, 联网补全低价池日线.
- Stock analysis / intraday observation: 股票分析, 盘中观察, 生成学习复盘报告, 当前报告模式.
- Research APIs: 研究可信度, strategy observation, `run_strategy_observation_report.py`, observation-report, agent-report-audit.
- cn_a_share and A-share Trading Calendar remain documented local-market constraints.
- Optional Integrations: AKShare, vectorbt, Backtrader, Qlib, FinRL are optional; missing optional packages do not block core workflows.
- License caution: Backtrader GPL-3.0; vectorbt Commons Clause; do not copy third-party project source; 不复制第三方项目源码.
- Historical milestones: v2.0, v2.4.0 Data Foundation, v2.5.2 Architecture Docs Primary Tab Navigation, v2.5.4 Visible Text Health.
- Historical releases: v2.6.7 Stability Patch / 2.6.7-stability-patch, v2.6.8 Low Price Snapshot Universe, v2.6.9 Low Price Universe Daily Update.
- Historical APIs: `/api/strategy-demo/check`, `/api/strategy-demo/overview`, `/api/learning-universe/low-price/update-daily`, `update_low_price_universe_daily.py`, `--update-mode-tag`, `/api/data-providers/status`.
- v2.8.8 reference_data_skill_adapter remains a historical data-provider milestone.
- Unified Workflow And Theme Map Fix; Unified Workflow Result And Theme Provider Fallback; `provider_results`; `/api/theme-map/import`.
- Strategy Observation Contract And Agent Safe Bridge; Agent Evidence Guard And Report Quality Audit; Daily Data Retention And Reproducibility Manifest.
- CHANGELOG.md and ROADMAP.md retain historical release notes; docs/roadmaps contains archived plans.
- Strategy demo anchors: 策略演示, v1.6 Strategy Demo Engine, 个股即时分析, Stock Search Analysis, 生成即时诊断.
- Beginner and replay anchors: 重要风险声明, 5分钟快速体验, 作为学习工具, 简化模式, v1.4, 策略历史回放（规则层）, 交易路径回放, rule_replay, trade_replay.
- Market-rule anchors: cn_a_share, market_rules, 100 股, T+1, 低价学习池, low_price_learning_symbols.csv.
- Operator workflow anchors: 新手只需要记住 5 个命令, qd.py 是唯一推荐入口, 每日流程.
- RC1 command anchors: `.\start_ui.cmd`, `.\run_tests.cmd`, `.\self_check.cmd`, `powershell -ExecutionPolicy Bypass -File .\export_project.ps1`.
- Report/UI anchors: current daily research app uses `python scripts\qd.py app` or `.\start_ui.cmd` on port 8000; the supported AI paper trader compatibility classroom uses `python -m ai_trader app` on port 8001. The legacy 8765 workbench is frozen; all development targets the 8000 Flask server (`scripts/quant_api_server.py`) with `app/templates/daily_research.html` + `app/static/js/daily_research.js`.
- Self-check/export anchors: 自检, `scripts\self_check.py`, dist/quantdesk_clean.zip, clean zip 不包含.
- Structured display anchors: 前端结构化展示说明, daily_brief, candidate_cards.
- Backtest/data anchors: 回测假设和限制, 部分成交, 真实撮合深度, 数据质量说明, Data Quality, ERROR.
- Risk anchors: 历史回测不代表未来收益, 不接实盘交易, 不构成投资建议, 不接券商, 不真实下单.
- Trading calendar anchors: A-share Trading Calendar, adjusted workday, 2026-05-05.
## v3.7.0.3 Low Price Pipeline Finalization

Research-only low-price viability now uses a local runtime `historical_listings.csv` built from price CSV coverage, plus a low-price profile with `initial_cash=10000`, `max_positions=10`, monthly rebalance, signal-date `close <= 15`, and A-share `lot_size=100`. The generated listings file is degraded PIT data and is excluded from clean zips.
# v3.7.0.6 Strategy Regression Guard and Report Hygiene

QuantDesk v3.7.0.6 adds regression guards for strategy reports, report provenance, and smoke-output hygiene. It remains research-only: no broker connection, no real orders, and no automatic trading.

## Research Candidate Monitoring

Research-only. Not trading advice. No live trading. No broker connection. No auto execution. No buy/sell recommendation. No real-money allocation instruction.
The candidate monitoring and dashboard outputs are for research review only and must not be used as trading, broker, execution, or allocation instructions.
