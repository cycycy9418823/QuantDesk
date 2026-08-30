## v3.7.0.6 Strategy Regression Guard And Report Hygiene

- Current release: `v3.7.0.6-strategy-regression-guard-and-report-hygiene`.
- Scope: strategy regression audit diagnostics, baseline distinctness checks,
  metric sanity checks, report provenance, and smoke-run/latest-output hygiene.
- Boundary: no strategy formula changes, no scoring-weight changes, no backtest
  return-logic changes, no broker route, no real orders, no default network
  access, and no default LLM usage.
- Immediate next candidate: close the current strategy-regression red gate
  honestly by distinguishing true strategy degeneration from too-small or
  stale local universes; do not hide warnings as passes.

## v3.7.0.5 Git Deletion Reconciliation

- Current release: `v3.7.0.5-git-deletion-reconciliation`.
- Scope: reconcile tracked D root-module deletions through compatibility alias coverage and limited import cleanup.
- Next candidate if high-risk deleted files remain zero: `v3.7.1-low-price-capital-aware-refinement`.

## v3.7.0.4 Capital Lot Sensitivity And Git Hygiene

- If `low_price_direction_rejected`, move to `v3.7.1-new-strategy-direction-screening`.
- If `low_price_needs_more_capital`, consider capital-aware low-price refinement.
- If `low_price_refine_needed`, refine low-price factors without production claims.
- If `pipeline_still_blocked`, repair pipeline before strategy conclusions.

## v3.7.0.1 Strategy Viability On Data Backbone

- Current roadmap release: `v3.7.0.1-strategy-viability-on-data-backbone`.
- Scope: data precheck plus five price-only baseline strategy viability tests on local daily CSV / data-backbone inputs.
- Next candidates: `v3.7.1-point-in-time-fundamentals` or `v3.7.1-strategy-refinement`.

## v3.7.0 Data Backbone

- Current roadmap release: `v3.7.0-data-backbone`.
- SQLite is a shadow layer, CSV remains the fallback, and `read_prefer_sqlite` stays false by default.
- Next candidates: `v3.7.1-fundamental-and-sector-data-integration`, `v3.7.2-data-quality-auto-repair-and-scheduler`, `v3.7.3-intraday-parquet-and-minute-backtest-lite`, and `v3.8.0-vectorbt-fast-backtest-and-parameter-search`.

## v3.6.2 Real Similar Day Stress And Style Attribution

- Current roadmap release: `v3.6.2-real-similar-day-stress-and-style-attribution`.
- Focus: Similar Day real local-data computation, historical scenario stress estimates, 10-factor style attribution lite, decision journal outcome statistics, and UI visibility.
- Missing local index, daily, exposure, or factor-return data produces warnings instead of fabricated real results.
- This release remains research-only / paper-only: no broker, no real orders, no automated trading.
- Next candidates: `v3.6.3-execution-realism-in-backtest`, `v3.6.4-daily-workflow-automation-and-review`, `v3.6.5-clean-windows-installer-validation`, and `v3.7.0-real-data-import-and-factor-validation`.

## v3.6.1 Real Risk Alerts And Smart Stop Integration

- Current roadmap release: `v3.6.1-real-risk-alerts-and-smart-stop-integration`.
- Focus: real paper position ingestion, smart stop integration, risk alert queue, local-file/dry-run notification pipeline, UI risk panel, and daily risk snapshot.
- v3.6.0 completed a broad real-use platform skeleton: modules, CLIs, tests, payload schemas, UI panels, and release proof.
- Many v3.6.0 modules are lite / synthetic-first / local-CSV-adapter implementations. True data integration, trigger logic, and module-to-module automation are planned from v3.6.1 to v3.6.4.
- Current next candidates: `v3.6.2-real-similar-day-and-stress-scenarios`, `v3.6.3-execution-realism-in-backtest`, `v3.6.4-daily-workflow-automation-and-review`, and `v3.6.5-clean-windows-installer-validation`.

## v3.6.0 Real Use Quant Platform Completion

- Current roadmap release: `v3.6.0-real-use-quant-platform-completion`.
- Completed scope: real-use stock-picking flow, paper watchlist, candidate review, adjusted-price data integrity, dynamic factor model, smart-stop, risk alerts, notification center, execution realism, style attribution, stress/similar-day tools, decision journal, local CSV data adapters, local store, and read-only UI panels.
- Release honesty: v3.6.0 is a skeleton / lite / adapter-first release, not a claim that every module is production-deep.
- Current next candidates: `v3.6.1-clean-windows-installer-validation`, `v3.6.1-real-data-import-pack`, `v3.6.2-factor-and-risk-model-validation`, and `v3.7.0-mobile-readonly-and-natural-language-query`.

## v3.5.2 UI Home And Strategy Recommender

- Current roadmap release: `v3.5.2-ui-home-and-strategy-recommender`.
- Completed scope: Home / Overview UI, grouped sidebar navigation, visual token polish, research-only strategy recommender, `qd strategy-recommend`, and daily strategy recommendation snapshot.
- Current next candidates: `v3.5.3-installer-hardening-on-clean-windows`, `v3.5.3-real-data-onboarding-lite`, `v3.5.4-deeper-package-migration`, and `v3.6.0-new-research-features`.

## v3.5.1.1 Release Staging Cleanup And Attribution Stress Tests

- Current roadmap release: `v3.5.1.1-release-staging-cleanup-and-attribution-stress-tests`.
- Completed maintenance scope: release staging cleanup, `-KeepStage`, `scripts/clean_release_staging.ps1`, and Brinson stress tests.
- Current next candidates: `v3.5.2-installer-hardening-on-clean-windows`, `v3.5.2-ui-home-and-user-flow-polish`, `v3.5.2-real-data-onboarding-lite`, and `v3.5.3-deeper-package-migration`.

## v3.5.0 Battle Ready Research And Portable Release

- Current roadmap release: `3.5.0-battle-ready-research-and-portable-release`.
- Completed release: `v3.5.0-battle-ready-research-and-portable-release`.
- Adds first-run onboarding, synthetic sample-data manifest, release notes, portable build script, PyInstaller spec, and NSIS installer script.
- Installer executable generation depends on optional external tools and is not faked when unavailable.
- Safety boundaries remain fixed: no broker, no real orders, no default network, no scoring changes, and no backtest math changes.
- Current next candidates: `v3.5.0-battle-ready-portable-release`, `v3.5.1-installer-hardening`, and `v3.5.1-ui-polish`.
- Older sections may contain historical next-candidate snapshots captured at the time of each release. The current next candidates are listed at the top.

## v3.4.5.1 Engineering Docs And Package Grouping Lite

- Completed release: `v3.4.5.1-engineering-docs-and-package-grouping-lite`.
- Package grouping remains lite: stable top-level modules stay compatible while subpackage indexes are introduced.
- `backtest.py` keeps behavior stable and re-exports loader helpers from `quantdesk/backtest_loaders.py`.
- `self_check.py` keeps check logic stable and delegates output rendering to `quantdesk/self_check_render.py`.
## v3.4.4.3 Experiment Tracker Lite

- Current roadmap release: `3.4.4.3-experiment-tracker-lite`.
- Completed release: `v3.4.4.3-experiment-tracker-lite`.
- This release adds research-only experiment logging, `qd.py experiments`, daily read-only experiment snapshots, and non-blocking auto logging from key research outputs.
- It does not place real orders, connect brokers, auto-trade, change strategies, change score weights, or change backtest return/matching logic.
- Current next candidates: `v3.5.0-battle-ready-portable-release`, `v3.5.1-installer-hardening`, and `v3.5.1-ui-polish`.
- Older sections may contain historical next-candidate snapshots captured at the time of each release. The current next candidates are listed at the top.
## v3.4.4.2 Risk Attribution Lite And Live Polish

- Current roadmap release: `3.4.4.2-risk-attribution-lite-and-live-polish`.
- Completed release: `v3.4.4.2-risk-attribution-lite-and-live-polish`.
- This release adds research-only Risk Attribution Lite, `qd.py risk-attribution`, daily risk attribution snapshot, `qd live --init`, and the intraday live quickstart.
- It does not place real orders, connect brokers, auto-trade, write attribution back to account books, change strategies, change score weights, or change backtest return/matching logic.
- Current Next candidates: `v3.4.4.3-experiment-tracker-lite`, `v3.4.4.3-backtest-loaders-split`, `v3.4.4.3-paper-package-grouping`, or `v3.4.4.3-intraday-live-engine-split`.
- Older sections may contain historical next-candidate snapshots captured at the time of each release. The current next candidates are listed at the top.
- No version collision: current next candidates do not point to an already released version.
- Future item: Windows service / systemd background live mode remains deferred; `qd live --loop` is foreground only.
- Future item: `intraday_live_engine.py` can later split renderer/account helpers into smaller files; this release keeps the existing file shape.

## v3.4.4.1 Intraday Live Workbench And Config Hygiene

- Historical roadmap release: `3.4.4.1-intraday-live-workbench-and-config-hygiene`.
- Completed release: `v3.4.4.1-intraday-live-workbench-and-config-hygiene`.
- Historical next candidates at that time: `v3.4.4.2-risk-attribution-lite`, `v3.4.4.2-experiment-tracker-lite`, `v3.4.4.2-backtest-loaders-split`, or `v3.4.4.2-paper-package-grouping`.

## v3.4.4.0 Multi Strategy Account Book

- Current roadmap release: `3.4.4.0-multi-strategy-account-book`.
- Completed release: `v3.4.4.0-multi-strategy-account-book`.
- This release adds a research-only / paper-only multi-strategy virtual account book with YAML-defined sub-accounts, allocation, JSON/Markdown outputs, CLI summary, and daily read-only snapshot.
- It does not add strategies, connect brokers, place orders, auto-rebalance, change scoring weights, or change backtest return/matching logic.
- Current Next candidates: `v3.4.4.2-experiment-tracker-lite`, `v3.4.4.2-backtest-loaders-split`, or `v3.4.4.2-risk-attribution-lite`.
- Older sections may contain historical next-candidate snapshots. The current next candidates are listed at the top.
- No version collision: current next candidates do not point to an already released version.

## v3.4.3.8 True Backtest Assumptions Split And Doc Hygiene

- Historical roadmap release: `3.4.3.8-true-backtest-assumptions-split-and-doc-hygiene`.
- Completed release: `v3.4.3.8-true-backtest-assumptions-split-and-doc-hygiene`.
- This release is engineering hygiene only: true backtest assumptions migration, architecture inventory freshness, ROADMAP next-candidate cleanup, FAQ expansion, and temporary file cleanup.
- Historical next candidates at that time: `v3.4.3.9-experiment-tracker-lite`, `v3.4.3.9-backtest-loaders-split`, or `v3.4.4.0-multi-strategy-account-book`.

## v3.4.3.6 Research Dashboard Daily Snapshot

- Current roadmap release: `3.4.3.7-backtest-assumptions-split-and-release-proof-polish`.
- Daily workflow now embeds a read-only research dashboard snapshot in payloads
  and Markdown reports.
- Snapshot covers research readiness, module availability, factor health,
  combined-score backtest metrics, sector rotation highlights, PIT universe
  status, stale/missing warnings, and suggested research actions.
- It does not alter default strategy, scoring weights, backtest matching, screen
  results, or daily trading conclusions.
- Next candidates: `v3.4.3.9-backtest-loaders-split` or
  `v3.4.3.9-experiment-tracker-lite`.

## v3.4.3.5 Point-in-Time Universe Lite And Project Hygiene

- Current roadmap release: `3.4.3.5-point-in-time-universe-lite-and-project-hygiene`.
- Added Point-in-Time Universe Lite for optional historical listing metadata,
  `universe_at(date)`, status summaries, and `qd.py pit-universe`.
- Research modules can display PIT availability while preserving legacy default
  behavior.
- Project hygiene updated: root `test_runtime/` ignore, architecture inventory,
  and README/user-guide/FAQ split.
- Next candidates: `v3.4.3.9-backtest-loaders-split-and-release-proof-polish` or
  `v3.4.3.6-backtest-assumptions-split`.

## v3.4.3.4 Factor Research Dashboard

- Current roadmap release: `3.4.3.4-factor-research-dashboard`.
- Added a research-only dashboard that consolidates factor evaluation, factor
  combination, combined-score research backtest, and sector rotation.
- Dashboard output covers module health, stale/missing payloads, key findings,
  risk flags, and research next actions.
- It does not alter default strategy, scoring weights, backtest matching, or
  daily workflow trading conclusions.
- Next candidates: `v3.4.3.5-point-in-time-universe-lite` to reduce
  survivorship-bias risk, or a read-only daily snapshot integration.

## v3.4.3.3 Sector Rotation Engine

- Current roadmap release: `3.4.3.3-sector-rotation-engine`.
- Added C3 sector/theme rotation diagnostics as a research observation layer.
- Sector rotation now covers sector returns, equal-weight benchmark relative
  strength, rank improvement, breadth, heat score, JSON output, Markdown report,
  `scripts/run_sector_rotation.py`, and `qd.py sector-rotation`.
- It does not alter default strategy, scoring weights, backtest matching, or
  daily workflow trading conclusions.
- Next candidates: `v3.4.3.4-factor-research-dashboard` to consolidate factor
  evaluation, factor combine, combined-score backtest, and sector rotation, or
  `v3.4.3.4-point-in-time-universe-lite` to reduce survivorship-bias risk.

## v3.4.3.2 Combined Score Research Backtest

- Current roadmap release: `3.4.3.2-combined-score-research-backtest`.
- Add a research-only backtest loop for `combined_score` ranking portfolios.
- Support Top-N, Top-Quantile, and Long-Short Quantile research modes with execution lag, transaction cost, slippage, turnover, drawdown, and return diagnostics.
- Keep combined-score backtest out of default screen, default backtest, daily workflow, trading signals, and scoring weights.
- Preserve PIT universe warnings and make missing combined-score / daily-price data visible warnings rather than traceback failures.
- Next candidates: `v3.4.3.3-sector_rotation_engine` for C3 board rotation, or `v3.4.3.3-factor-research-dashboard` for a combined research overview.

## v3.4.3.1 Factor Combiner And Neutralized Score

- Current roadmap release: `3.4.3.1-factor-combiner-and-neutralized-score`.
- Add a research-only combined factor score platform with winsorize, z-score / percentile rank, neutralization, optional orthogonalization, and IC-based weights.
- Keep `combined_score` out of default screen, backtest, daily workflow, trading signals, and production scoring weights.
- Preserve PIT universe warnings and keep missing factor health / exposure data as visible warnings rather than hidden assumptions.
- Next candidates: `v3.4.3.2-sector_rotation_engine` for C3 board rotation, or a research-mode backtest that evaluates combined_score without changing defaults.

## v3.4.3.0 Factor Evaluation Platform

- Current roadmap release: `3.4.3.0-factor-evaluation-platform`.
- Add an offline factor effectiveness research layer that answers whether current factors have predictive value for future returns.
- Track IC, Rank IC, ICIR, IC decay, quantile returns, long-short returns, coverage, and health status.
- Keep factor evaluation research-only; it must not alter default scoring weights, strategy signals, daily workflow recommendations, or backtest logic.
- Keep point-in-time universe reconstruction as a documented limitation and warning, not a hidden assumption.
- Next candidates: `v3.4.3.1-factor-combiner` for neutralization and IC-weighted research scoring, or `v3.4.3.1-sector-rotation-engine` for C3 sector rotation.

## v3.4.2.7 P3 Engineering Cleanup Replay Performance And Market Calendar

- Current roadmap release: `3.4.2.7-p3-engineering-cleanup-replay-performance-and-market-calendar`.
- Keep the release limited to P3 engineering cleanup: replay performance, calendar fallback transparency, and clean-zip Git hygiene.
- Historical replay uses in-memory DataFrame slices capped at each `signal_date` instead of per-signal-date tempfile CSV directories.
- Market calendar loading prefers CSV, then legacy YAML, then an explicit degraded warning. It no longer silently relies on hard-coded holiday sets.
- Clean zip remains a generated artifact and must not be tracked in Git.
- Next candidates after this cleanup: `v3.4.3.0-factor-evaluation-platform` for A1 factor effectiveness tracking, or C3 sector rotation if a smaller roadmap item is preferred.

## v3.4.2.5 CLI Summary Output And Operator Readability

- Historical roadmap release: `3.4.2.5-cli-summary-output-and-operator-readability`.
- Add explicit `--summary true` output for long CLI payloads while keeping full latest JSON files unchanged.
- Make `--json true --summary true` valid compact JSON for data-health, screen, backtest, market-intel, and related operator checks.
- Preserve warning visibility, suggested next actions, output paths, and safety disclaimers in every compact summary.
- Keep scope presentation-only: no strategy changes, no scoring changes, no backtest-return changes, no broker API, no real order, no default network, no LLM trading judgment.

## v3.4.2.4 Windows Python Env And Command Consistency

- Add `env-doctor` so clean-zip users can see the active Python executable, PATH `python`, Windows `py` launcher, encodings, and suggested next actions.
- Keep all Windows helper scripts aligned around `QUANTDESK_PYTHON` without hardcoding one local machine path as mandatory.
- Keep the v3.4.2.3 first-run quant loop intact while making command examples easier to copy on Windows.

## v3.4.2.3 CLI Contract First Run Quant Loop Closure

- Make every command recommended by `python scripts/qd.py --help` executable under the qd.py wrapper contract.
- Promote `backtest` from placeholder to a real local `data/daily` entrypoint while preserving existing strategy, scoring, and return logic.
- Add `build-features` and make `screen` return structured warnings instead of failing when clean zips have no features yet.
- Resolve default target dates to the latest complete A-share trading day and keep explicit non-trading/future dates transparent via warnings.
- Keep offline bootstrap honest: `network=false` creates/checks a tiny universe only and reports `daily_ready=false`.
- Keep operation logging single-entry per qd.py command.

## v3.4.2.1 Dependency Config Provider Hygiene And Paper Lock

- Keep the patch limited to dependency/config/provider hygiene and paper-state write protection.
- Treat `requirements.txt` as core runtime dependencies and `requirements-data.txt` / `pip install .[data]` as optional live provider dependencies.
- Keep roadmap-only providers as gray planned placeholders, not real provider failures.
- Add data-quality metadata for all-NaN score components without changing scores, strategies, parameters, or backtest returns.

## v3.4.2 Dashboard State Clarity Command Safety And Market Intel Report Polish

- Make the dashboard quieter: short symbol previews, explicit “本次检查 / 当前分析池” wording, and no long lists in home cards.
- Split sidebar state into data health, market-intel, and daily review so old reports are not mixed with current market observation.
- Separate offline checks from online data repair commands; stale daily data should show an explicit `--enable-network` command.
- Make market-intel cards and reports feel like compact research briefs while staying paper-observation only.

## v3.4.1 Market Intel Scope Sync Candidate Explainability And Dashboard Polish

- Align daily-use scope semantics across data-update, data-health, daily, status, fix-plan, and market-intel.
- Make the market observation dashboard explain what it found: hot sectors, low-price paper candidates, board-watch candidates, report paths, data gaps, and next observation steps.
- Prevent long symbols lists from returning to home, command-center, data-health, data-update, or market observation cards.
- Keep market-intel strictly as paper observation and learning research: no broker, no real order, no default network/LLM/full-A, no fabricated data, no investment advice.

## v3.3.7 Data Update Output Slim And Mobile Visual Check

- Make `python scripts/qd.py data-update` and `python scripts/qd.py data-health` quiet by default: short console summaries, full JSON in latest result files.
- Add shared symbol display helpers so long `symbols` lists are summarized in CLI/UI without losing full JSON detail or copy payloads.
- Add mobile/narrow-screen safeguards for home, command center, artifact center, and remediation command blocks.
- Keep daily operations conservative: no new strategy, no score change, no backtest return change, no broker API, no real order, no default network, no default LLM, and no investment advice.

## v3.3.4 Daily Output Summary and Artifact Center Followthrough

- Make `python scripts/qd.py daily --network false --with-guardrails true` usable as the main daily entrypoint by printing a short summary instead of full JSON.
- Keep complete machine-readable output in `results/daily_workflow_latest.json`; use `--json true` or `--verbose true` only when debugging.
- Standardize daily artifacts around `reports/daily_quant_report_latest.md` and `results/daily_workflow_latest.json`.
- Let `status`, command-center, and artifact center point users back to the latest daily report/result and warning counts.
- Scope remains UX/governance only: no new strategy, no score change, no return-logic change, no broker API, no real order, no default network, no full-A scan, no investment advice.

## v3.3.3 Entrypoint Consolidation Status and Daily Guardrails

- Make `python scripts/qd.py status` the first stop when users do not know what to run next.
- Keep `qd.py` as the only recommended daily entrypoint; bare scripts are inventoried as retained/internal/deprecated instead of being promoted to users.
- Embed quick governance guardrails into `qd.py daily --network false` by default, while preserving separate full commands for strategy validation, stats validation, and parameter sensitivity.
- Centralize recent timestamp helper copies in `quantdesk/time_utils.py`.
- Scope remains governance-only: no strategy changes, no score change, no return-logic change, no broker API, no real order, no default network, no full-A scan, no investment advice.

## v3.3.2.5 Workbench State Consistency Schema Noise and UI Polish

- Stabilize workbench state language: `coverage_date` means all checked symbols cover that date, while `latest_date_max` means at least one checked symbol has newer local daily data.
- Keep data-update and data-health separate in the UI; show warnings when target dates or `data_scope` differ.
- Reduce schema-check noise by splitting `confirmed_secret_leak` from `secret_like_reference`.
- Polish artifact center previews, copy-path actions, beginner/expert visibility, and long command wrapping.
- Scope remains patch-only: no new strategy, no score change, no return-logic change, no provider protocol change, no broker API, no real order, no default network, no full-A default scan, no investment advice.

## v3.3.2.4 Workbench Empty State Warning Severity and Actionability Fix

- Make 今日工作台 easier to scan by rendering Chinese metric/status labels instead of raw schema keys.
- Separate next-action warning types into data / validation / review / system categories.
- Rename the next-action block to “我现在该做什么” so the home page gives a direct workflow cue.
- Scope remains UI/read-only payload polish only: no strategy changes, no score change, no return-logic change, no provider protocol change, no broker API, no real order, no default network, no full-A default scan, no investment advice.

## v3.3.2.3 Workbench Refresh Command Center and Target Date Semantics Fix

- Preserve explicit `--target-date` in data-update payloads and expose `effective_target_date` for UI/contract consumers.
- Add a read-only 今日工作台 “刷新本地状态” path that re-reads local status without network, tasks, LLM, or trading actions.
- Make 命令中心 readable through timeline + table layout instead of narrow command cards.
- Clean up homepage empty-state semantics: no raw `null`, nested data-health summaries are read correctly, and next actions distinguish stale daily data, missing intraday data, and stale validation outputs.
- Scope remains patch-only: no strategy changes, no score change, no return-logic change, no provider protocol change, no broker API, no real order, no default network, no full-A default scan, no investment advice.

## v3.3.2.2 CI Stability Repo Hygiene and Calendar Cache

- Stabilize CI by recognizing the current patch version and avoiding brittle checks against historical version comments.
- Cache local trading-calendar CSV reads by path and keep `clear_calendar_cache()` available for deterministic tests.
- Keep provider diagnostics clearer: placeholder/planned providers are grey “规划中 / 未启用”, not red failures.
- Keep repo hygiene strict: temporary roots, one-off verification JSON, and `_maintenance/` stay out of source and clean zip.
- Scope remains patch-only: no new strategy, no score change, no return-logic change, no provider protocol change, no broker API, no real order, no default network, no full-A default scan, no investment advice.

## v3.3.2.1 Data Update Network Flag Console and Encoding Fix

- Fix `qd.py data-update --network true` and `--enable-network` so explicit network intent reaches the data-update orchestrator.
- Keep `--no-network` as the strongest safety override and record a warning when conflicting flags are supplied.
- Keep `dry_run=false` by default; only `--dry-run true` turns on dry-run behavior.
- Always print a console summary with target date, latest local daily date, `network`, `update_allowed`, `dry_run`, provider chain, result path, report path, and next action.
- Keep JSON and Markdown output UTF-8, and document the PowerShell `Get-Content ... -Encoding UTF8` viewing path.
- Scope remains a patch only: no new strategy, no score change, no return-logic change, no provider protocol change, no broker API, no real order, no default network, no full-A default scan, no investment advice.

## v3.3.1 QuantDesk Workbench UI and Workflow Unification

- Add a read-only workbench aggregation layer for `/api/workbench/overview`, `/api/workbench/command-center`, `/api/workbench/artifacts`, and `/api/workbench/next-actions`.
- Reorganize the UI around a collapsible left sidebar, 今日工作台, data-health page, independent command center, artifact center, and beginner/expert mode.
- Move the full command center out of the top of every page; the home page now shows concise status cards, a linear daily workflow, and at most three data/process/validation next actions.
- Improve clean-zip empty states for missing `data/daily`, `data/intraday`, `results`, `operation_log`, and observation reports.
- Keep scope UI/UX-only: no new strategy, no score change, no return-logic change, no parameter optimization, no broker API, no real order, no default network, no default LLM, no investment advice.

## v3.3.0 Consolidation and Schema Contracts

- Add `schema_contracts` as a lightweight common JSON result contract helper without a pydantic/config rewrite.
- Add `result_payload_validator` and `python scripts/qd.py schema-check --results-dir results --network false`.
- Write runtime outputs under `results/schema_validation_latest.json` and `reports/schema_validation_report_latest.md`; clean zip excludes these outputs.
- Feed `schema-check` into operation_log, command-center, archive-results, qd.py help, docs, and schema examples.
- Keep scope governance-only: no new strategy, no score change, no return-logic change, no parameter optimization, no broker API, no real order, no default network, no default LLM, no investment advice.

## v3.2.9 Parameter Sensitivity and Robustness Audit

- Add `param-sensitivity` to perturb a bounded set of parameters and check whether existing results are fragile.
- Add `quantdesk/parameter_sensitivity_audit.py`, `scripts/run_parameter_sensitivity_audit.py`, `config/parameter_sensitivity.example.json`, and `docs/parameter_sensitivity_audit.md`.
- Write runtime outputs under `results/parameter_sensitivity_latest.json` and `reports/parameter_sensitivity_report_latest.md`; clean zip excludes these outputs and real `config/parameter_sensitivity.json`.
- Feed `parameter_sensitivity_summary` into daily-retain manifest, observation context, evidence pack, audit, data-health, command-center, archive-results, and lightweight UI.
- Keep scope diagnostic only: no new strategy, no score change, no return-logic change, no parameter optimization, no best-parameter recommendation, no broker API, no real order, no default network, no investment advice.

## v3.2.8 Data Health Dashboard and Provider Failure Stats

- Add `data-health` to summarize local daily freshness, minute-line coverage, provider failed/skipped reasons, and data_grade distribution.
- Add `quantdesk/data_freshness_summary.py`, `intraday_coverage_dashboard.py`, `provider_failure_stats.py`, and `data_grade_trend.py`.
- Write runtime outputs under `results/data_health/` and `reports/data_health_dashboard_report_latest.md`; clean zip continues to exclude runtime outputs.
- Feed `data_health_summary` into daily-retain manifest, observation context, evidence pack, audit, command-center, archive-results, and the lightweight UI.
- Keep scope diagnostic only: no new strategy, no score change, no return-logic change, no parameter optimization, no broker API, no real order, no default network, no investment advice.

## v3.2.7 Governance Command Center and Doc Consolidation

- Add `operation_log` to record local CLI command status, arguments, output paths, warnings, and failed reasons without writing API keys or secrets.
- Add `archive-results` to copy existing latest results/reports into `results/archive/{target_date}/` with an archive manifest.
- Add `command-center` to summarize recent command state from operation_log without rerunning tasks,联网, or calling LLM.
- Slim the README top section toward quick start, qd.py-only entry, current workflow, docs links, and recent highlights; detailed v3 history is summarized in `CHANGELOG.md`.
- Add `docs/troubleshooting_quick_ref.md` and `docs/architecture.md`.
- Keep scope governance-only: no new strategy, no score change, no return-logic change, no parameter optimization, no broker API, no real order, no default network, no investment advice.

## v3.2.6.1 Feature Audit Empty Data Graceful Warning

- Treat clean-zip empty `data/daily/` environments as `feature-audit` warning instead of process failure.
- Add explicit `skipped_reason` and `suggested_next_action` for missing local feature rows.
- Keep scope diagnostic only: no network request, no fake data, no full-A scan, no strategy or return-logic change, no investment advice.

## v3.2.6 Backtest Statistical Validation And Feature Quality Audit

- Add `feature_quality_audit` to inspect local feature missing rates, zero rates, NaN/Inf counts, and symbol/date coverage.
- Add `backtest_statistical_validation` with permutation tests and bootstrap confidence intervals for existing backtest outputs.
- Add `python scripts/qd.py feature-audit --symbols 600744,002115 --target-date YYYY-MM-DD --network false`.
- Add `python scripts/qd.py stats-validate --symbol 600744 --target-date YYYY-MM-DD --network false --iterations 500 --seed 42`.
- Feed feature/statistical summaries into daily retention manifests, observation context, evidence pack, and learning reports.
- Keep scope diagnostic only: no new strategy, no score change, no return logic change, no parameter optimization, no broker API, no real order, no default network, no investment advice.

## v3.2.5 Paper Vs Backtest Consistency Check

- Add `paper_vs_backtest_consistency` to compare local backtest signals, paper actions, intraday execution, strategy validation, and daily retention manifest metadata.
- Add `python scripts/qd.py consistency-check --symbol 600744 --target-date YYYY-MM-DD --network false`.
- Write `results/paper_vs_backtest_consistency_latest.json` and `reports/paper_vs_backtest_consistency_report_latest.md`.
- Feed consistency warning/failed status into observation context so learning reports do not hide paper/backtest contradictions.
- Keep scope diagnostic only: no new strategy, no score change, no return logic change, no parameter optimization, no broker API, no real order, no default network, no investment advice.

## v3.2.4 Daily Data Retention And Reproducibility Manifest

- Add `daily_data_retention` to keep a scoped daily snapshot under `results/daily_retention/{target_date}/`.
- Add `reproducibility_manifest` with version metadata, explicit symbols, command args, provider trace, validation summary, output paths, and file hashes.
- Add `python scripts/qd.py daily-retain --symbols 600744,002115 --target-date YYYY-MM-DD --network false`.
- Add `python scripts/qd.py manifest-check --manifest results/daily_retention/YYYY-MM-DD/daily_retention_manifest.json`.
- Keep defaults safe: no broker API, no real orders, no full-A default scan, no default network, no default LLM, no API Key or real account content in manifests, and no investment advice.

## v3.2.3 Strategy Lookahead And Execution Assumption Guard

- Add `strategy_lookahead_guard` to detect post-target data, global statistic leakage, daily-bar intraday proxies, and reliable executions without minute data.
- Add `execution_assumption_guard` to flag daily high/low fills, missing-intraday fills, no-fill state changes, T+1 violations, and board-candidate limit-up fill risk.
- Add `strategy_validation_suite` plus `python scripts/qd.py strategy-validate --symbol 600744 --target-date YYYY-MM-DD --network false`.
- Feed `validation_summary` into observation reports and audit evidence so strategy validation risk is not hidden.
- Keep scope diagnostic only: no new strategy, no score change, no return logic change, no broker API, no real order, no default network, no investment advice.

## v3.2.2 Core Correctness And Doc Sync

- Add `data/calendar/a_share_trading_calendar.csv` as a clean-zip static asset, with 2020-2027 coverage and provisional labeling for 2027.
- Add `scripts/build_trading_calendar.py`; default `network=false` uses offline/manual rules and never requests external data.
- Expand `trading_calendar.calendar_info()` with quality, coverage, path, loaded date count, and warning metadata.
- Move Python package versioning to `quantdesk.version.PACKAGE_VERSION` via dynamic `pyproject.toml` metadata.
- Remove old duplicate mootdx skeleton function definitions while keeping the optional real provider and Tencent fallback behavior.
- Add a board-candidate paper execution assumption without changing scoring, strategy parameters, or return logic.

## v3.2.1 Agent Evidence Guard And Report Quality Audit

- Add `agent_evidence_pack` so optional Agent reports are bounded by facts extracted from the local observation context.
- Add `agent_hallucination_guard` to flag unsupported numbers, dates, symbols, provider claims, and contradictions such as missing minute data being described as available.
- Add `agent_report_audit` with safe hashes for context, evidence pack, prompt, and report, plus policy and grounding guard metadata.
- Add `python scripts/qd.py agent-report-audit --report results/strategy_observation_report_latest.md --context results/strategy_observation_context_latest.json`.
- Keep defaults unchanged: LLM disabled, network disabled, no provider calls from Agent, no broker API, no real orders, and no investment advice.

## v3.2.0 Optional LLM Observation Agent

- Add optional LLM observation reporting on top of the read-only strategy observation context.
- Keep `llm_enabled=false` and `network=false` as defaults; missing config or missing API keys falls back to `offline_stub`.
- Add OpenAI-compatible and Ollama provider adapters without SDK hard dependencies.
- Add `python scripts/qd.py llm-smoke --network false` and `--llm/--llm-provider` flags for observation reports.
- Run every generated report through the agent policy guard. The agent cannot trade, cannot mutate strategy/provider/portfolio state, and cannot provide investment advice.

## v3.1.3 Strategy Observation Contract And Agent Safe Bridge

- Add a read-only `strategy_observation_contract` that aggregates existing local outputs for learning observation.
- Add an offline `agent_safe_bridge` template report; it does not call any LLM or network service.
- Add `agent_policy_guard` to block advice-shaped terms such as buy/sell recommendations, target levels, risk-threshold levels, and sizing.
- Add `python scripts/qd.py observation-report --symbol 600744 --target-date YYYY-MM-DD --network false`.
- Keep boundaries unchanged: no broker API, no real orders, no full-A default scan, no provider data mutation, and no investment advice.

## v3.1.2 Real Mootdx Provider And Fallback Hardening

- `mootdx` is now a real optional provider for quote, daily, intraday, and fundamentals diagnostics.
- Missing `mootdx` dependency does not block QuantDesk; Tencent remains the primary fallback.
- Provider smoke accepts `--providers tencent,mootdx` and keeps `network=false` as the safe default.
- Minute-data gaps still remain explicit; no daily or realtime data is used to fake intraday execution.

## v3.1.1 Real Provider Smoke And Observability

- Add scoped provider network smoke checks through `quantdesk/provider_network_smoke.py` and `scripts/run_provider_network_smoke.py`.
- Add `python scripts/qd.py network-smoke --symbols 600744,002115 --target-date YYYY-MM-DD --network true`; the default remains `network=false`, which never opens external requests.
- Add a small provider result contract so smoke reports consistently expose `provider_chain`, `failed_reason`, `skipped_reason`, `data_rows`, `duration_ms`, and `retrieved_at`.
- Harden Tencent diagnostics for network-disabled, timeout, HTTP, parse, empty-response, unsupported-symbol, no-row, and date-out-of-scope cases.
- Keep boundaries unchanged: no broker API, no real orders, no full-A default scan, and no investment advice.

## v3.1.0 Real Tencent Provider And Entrypoint Simplification

- Tencent Finance provider moves from placeholder to real user-triggered HTTP adapter for realtime quote, daily K-line, cautious current-day intraday diagnostics, and valuation fields.
- `network=false` remains the default and never requests external data. `network=true` can return standard rows or clear `failed_reason` / `skipped_reason`.
- `scripts/qd.py` becomes the recommended beginner entrypoint while all original scripts remain available for advanced users.
- QuantDesk still does not connect to brokers, does not place real orders, and does not provide investment advice.

## v3.0.4 Chanlun Segment Engine

- 已实现缠论程序化第三层：基于有效笔序列生成上升线段观察、下降线段观察，并记录线段破坏观察。
- 新增 CLI：`python scripts/run_chanlun_segment_analysis.py --symbol 600744 --timeframe daily`。
- 新增 API：`POST /api/chanlun/segments/run`，GET 接口只读。
- 线段结果只作结构观察，不接入自动虚拟组合、条件交易计划或收益统计；后续 v3.0.5 才进入中枢识别。
- 继续保持：不接券商、不真实下单、不保存真实账户信息、不构成投资建议。

## v3.0.3 Chanlun Bi Engine

- 已实现缠论程序化第二层：基于顶/底分型筛选有效分型，按顶底交替和最小间隔规则生成上升笔观察、下降笔观察。
- 新增 CLI：`python scripts/run_chanlun_bi_analysis.py --symbol 600744 --timeframe daily`。
- 新增 API：`POST /api/chanlun/bi/run`，GET 接口只读。
- 笔结果只作结构观察，不接入自动虚拟组合、条件交易计划或收益统计；后续 v3.0.4 才进入线段识别。
- 继续保持：不接券商、不真实下单、不保存真实账户信息、不构成投资建议。

## v3.0.2 Chanlun Fractal Engine

- 已实现缠论程序化第一层：OHLC 标准化、包含关系处理、顶分型观察、底分型观察、分型质量诊断和学习报告。
- 新增 CLI：`python scripts/run_chanlun_fractal_analysis.py --symbol 600744 --timeframe daily`。
- 新增 API：`POST /api/chanlun/fractals/run`，GET 接口只读。
- 分型只作为结构观察，不接入自动虚拟组合；后续 v3.0.3 才进入笔识别。
- 继续保持：不接券商、不真实下单、不保存真实账户信息、不构成投资建议。

## v3.0.1 Chanlun Knowledge Base And Rule Roadmap

- 新增本地缠论学习知识库：`quantdesk/chanlun_knowledge_base.py`、`scripts/build_chanlun_knowledge_base.py`、`docs/chanlun_implementation_plan.md`。
- 输出 `chanlun_lessons_index.json`、`chanlun_concepts.json`、`chanlun_rule_roadmap.json` 和学习报告，但不保存 PDF 全文，不展示大段原文。
- 买卖点在 QuantDesk 中仅作为观察信号，不代表买卖建议；本阶段不生成交易信号，不接券商，不真实下单，不构成投资建议。
- 后续路线保留为：v3.0.2 分型识别、v3.0.3 笔识别、v3.0.4 线段识别、v3.0.5 中枢识别、v3.0.6 背驰观察、v3.0.7 历史回放验证。

## v3.0.0 A-Stock Data Provider Matrix And Adapter Layer

- 新增 `quantdesk/a_stock_data_matrix.py`，按行情、日线、分钟线、估值、财报/F10、公告、研报、新闻、热点题材、板块概念和风险事件维护 A 股 provider 矩阵。
- 新增 `quantdesk/a_stock_data_adapter.py` 和 `quantdesk/providers/`，统一返回 `provider_chain`、失败原因、可靠性和免责声明；provider 失败不会伪造数据。
- 行情路线优先 `mootdx + 腾讯财经`；AKShare 保留为补充 provider；Tushare 和 Ashare 默认禁用并保留诊断说明。
- 新增 `scripts/check_a_stock_data_providers.py`，默认 `network=false`，只做依赖和本地能力检查，不联网、不扫描全 A。
- 新增 `docs/provider_setup.md` 和 `config/provider_secrets.example.json`；真实 `provider_secrets.json` 不进入 git 和 clean zip。
- UI 增强“数据源矩阵 / Provider 能力诊断”，帮助用户看清哪些源可用、哪些缺依赖、哪些需要 API Key、下一步怎么补。

## v2.9.9 Real Intraday Data Workbench And Provider Hardening

- 新增 `quantdesk/intraday_data_workbench.py`，把分钟线缺口、provider 尝试、本地 CSV 导入、质量审计和条件计划执行串成一个真实分钟线工作台。
- 新增 `quantdesk/intraday_quality_audit.py`，识别 usable / partial / insufficient / invalid / missing，并阻断疑似日线误导入和异常 OHLC。
- 增强导入兼容：中文/英文字段、通达信、同花顺、聚宽/掘金/米筐、price-only、utf-8/gbk/gb18030。
- provider 失败或跳过时必须给出 skipped_reason / failed_reason / suggested_next_action；不再只显示“跳过”。
- 默认不联网、不扫描全 A、不更新自动虚拟组合；不接券商、不真实下单、不构成投资建议，不用日线价格假装盘中成交。

## v2.9.8 Beginner Data Health And Guided Home

- 新增 `quantdesk/data_health_dashboard.py`，聚合日线、分钟线、板块题材、虚拟组合、provider 诊断和自检状态，不联网、不跑重任务。
- 首页新增“数据健康状态”“今日操作指南”“今日 Top3 观察候选”，用红 / 黄 / 绿和白话说明现在能不能复盘、为什么盘中没有触发、下一步该做什么。
- 新增小白模式 / 专家模式，默认小白模式；专家模式保留原有全部模块。内部值如 `missing_intraday`、`no_fill`、`daily_review_only` 继续中文化。

## v2.9.7 Real Intraday Provider Capability And Sample Validation

- 新增分钟线 provider 能力探测：依赖状态、当日/历史分钟线支持、目标日期覆盖、返回行数、latest datetime、失败原因和 skipped_reason。
- 新增真实样本闭环：默认仅测 `600744` / `002115`，provider 成功才写入真实 `data/intraday/1m/{symbol}.csv`，随后执行条件计划回放。
- provider 失败时不生成假分钟线、不用日线价格冒充盘中成交、不更新自动虚拟组合。

## v2.9.6 Intraday Replay Self Validation Harness

- 新增 `quantdesk/intraday_replay_self_validation.py` 和 `scripts/run_intraday_replay_self_validation.py`，用合成 1 分钟线自动验证条件计划、分钟线回放、可靠触发、组合更新和 no_fill/missing_intraday 不成交。
- 覆盖 buy/add、breakout buy、reduce_half/take_profit、stop_loss/sell_all、no_fill、missing_intraday、止损优先级和 A 股 T+1 frozen quantity。
- 默认不联网、不写真实 `data/intraday/1m`、不覆盖 `state/auto_paper_portfolio.json`，仅输出自检 JSON 和报告。

## v2.9.5 Historical Intraday Replay And Trading Calendar

- 新增 `quantdesk/trading_calendar.py`，支持本地 `data/calendar/a_share_trading_calendar.csv`，缺失时使用 weekend-only 简化交易日历并在报告中提示质量。
- 新增历史分钟线回放：按 signal_date / trade_date 分离，walk-forward 生成条件交易计划并用 T+1 分钟线验证触发。
- 新增 `scripts/run_historical_intraday_replay.py`、`/api/historical-replay/*` 和“历史分钟线回放”小入口。
- 默认不联网、不更新当前自动虚拟组合、不扫描全 A；缺少分钟线时不假装成交。

## v2.9.4 Intraday 1m Provider Fallback

- 为当前条件计划、当前候选和当前自动虚拟持仓增加用户触发的 1 分钟线 provider fallback。
- 新增 `quantdesk/intraday_update_engine.py` 与 `scripts/update_intraday_1m_fallback.py`。
- `GET /api/intraday/update/status` 只读；`POST /api/intraday/update` 才允许联网补分钟线。
- provider 失败不崩溃，失败股票继续 `missing_intraday`，不使用日线价格假装成交。

## v2.9.3 Intraday 1m Import And Execution Closure

- 新增标准 1 分钟线 schema、缺口检查和本地 CSV 导入闭环。
- 支持为当前条件计划、持仓和候选生成分钟线导入模板。
- 支持用已有 `conditional_trade_plans_latest.json` 重新执行分钟 close 回放，并可选择只写执行结果或更新自动虚拟组合。
- 坚持缺分钟线不成交：完整日线控制收益复盘，分钟线控制盘中触发验证。

## v2.9.2 Shared Cleanup And Contract Enforcement

- 清理主线模块中重复的 `normalize_symbol`、`truthy`、时间和候选解析 wrapper，统一走 `quantdesk/shared.py` 与 `quantdesk/candidate_contract.py`。
- 强化 workflow `input_sources`，区分 `generated_in_memory`、`strategy_payload_in_memory`、`candidates_in_memory`、`plan_payload_in_memory` 与 `latest_file_fallback`。
- `self_check.py` 新增 shared cleanup guard，防止后续再复制本地 symbol/candidate 工具函数。

## v2.9.1 Shared Core And Candidate Contract

- Add `quantdesk/shared.py` for shared symbol normalization, truthy parsing, timestamp helpers, JSON IO, numeric coercion, and short error text.
- Add `quantdesk/candidate_contract.py` to standardize strategy candidates across sandbox, auto paper portfolio, conditional plans, and daily workflow.
- Preserve `strategy_type` for board / board_momentum candidates; only missing values fall back to defaults.
- Prefer in-memory objects inside the daily workflow; latest JSON remains an archive/fallback path and fallback emits a warning.
- Re-label legacy `paper_trading` as the manual observation ledger while keeping auto paper portfolio as the main workflow ledger.
- Tighten UI wording and CSS for sample counts, CSV counts, and long provider/path/error text.

## v2.9.0 Data Freshness Gate And Current Candidate Theme Sync

- Add `quantdesk/data_freshness_gate.py` so stale daily bars explicitly block current-day review, portfolio valuation refresh, and yesterday-position correctness conclusions.
- Keep stale strategy candidates visible only as `stale_observation`; one-click workflow returns `partial` instead of `completed` when the low-price pool is not fully updated to the target complete trading day.
- Add current-candidate theme-map validation and preserve 6-digit symbols with leading zeroes.
- Extend daily fallback output with `processed_symbols`, `updated_symbol_list`, `unchanged_symbol_list`, and `failed_symbol_list` at both top level and provider-result level.
- Add `--symbols` to `scripts/update_daily_price_fallback.py` and make `--limit` print the actual symbol list processed.
- Keep the boundaries unchanged: no broker API, no real orders, no account credentials, no investment advice, no full-A scan by default, and no fake intraday fills.

## v2.8.9 Daily Price Provider Fallback Engine

- Add executable low-price daily-bar fallback through `quantdesk/daily_price_update_engine.py`.
- Add `scripts/update_daily_price_fallback.py` with default low-price-only scope, dry-run mode, provider results, and no full-A scan by default.
- Add `GET /api/daily-price/status` and `POST /api/daily-price/update`.
- Wire one-click daily review to diagnose stale daily bars by default and only update them when `allow_daily_price_online_update=true`.
- Keep minute-line behavior unchanged: missing intraday data remains no-fill and never becomes a fake daily close fill.

## v2.8.8 Reference Data Skill Adapter

- Study external A-share data projects as references: `simonlin1212/a-stock-data`, `mpquant/Ashare`, and `Ogannesson/ashare-llm-analyst`.
- Add reference docs and `docs/data_provider_fallback_plan.md`; no external project code is copied into QuantDesk.
- Add `quantdesk/data_provider_registry.py`, plus daily and intraday provider skeleton modules.
- Add data-source diagnostics UI, `GET /api/data-providers/status`, `POST /api/data-providers/check`, and `scripts/check_data_providers.py`.
- Keep GET read-only and POST user-triggered; default diagnostics do not connect to external providers.
- Exclude `external_refs/` from clean zip.

## v2.8.7 Theme Map Import Draft Generator

- Add `quantdesk/theme_map_import_draft.py` and `scripts/generate_stock_theme_map_import.py`.
- Generate `data/universe/stock_theme_map_import.csv` automatically from current candidates, auto paper portfolio positions, pending rows, and low-price included symbols.
- Preserve existing user-edited import rows unless `--overwrite` is explicit.
- Add a narrow local seed mapping for current observation samples; unknown symbols stay `pending_manual`.
- Add `POST /api/theme-map/generate-import-draft` and read-only `GET /api/theme-map/import-draft/preview`.
- Add UI buttons for “生成待补导入草稿” and “导入草稿到正式映射”.
- Keep the boundary unchanged: no broker connection, no real orders, no account credentials, and no investment advice.

## v2.8.6 Unified Workflow Result And Theme Provider Fallback

- One-click daily review now persists structured results even when part of the workflow fails: `results/daily_workflow_latest.json` includes `workflow_status`, ordered `steps`, a non-empty `summary`, warnings, errors, and artifact paths.
- Theme-map failures no longer empty the workflow. Missing or failed board/theme data is reported as a warning, while local daily features, strategy sandbox output, conditional plans, and the auto paper portfolio can continue.
- Added multi-provider theme-map fallback in `quantdesk/theme_map_providers.py`: local files, Eastmoney-style akshare endpoints, THS-style akshare endpoints, per-symbol industry lookup, and user-imported CSV.
- Added manual fallback import: `stock_theme_map_import.csv`, CLI `--source import --input ...`, and `POST /api/theme-map/import`.
- UI now refreshes workflow latest/status after a run, shows provider-by-provider results, and gives a clear next step when `mapped_count=0`.
- The default boundary remains local paper simulation only: no broker API, no real orders, no account credentials, no investment advice, no full-A scan by default, and no close-price fake fills when minute data is missing.

## v2.8.5 Unified Workflow And Theme Map Fix

- 新增“今日一键复盘”统一入口：低价学习池检查 -> 低价报告 -> 本地日线特征 -> 板块题材检查 -> 自动策略沙盒 -> 条件交易计划 -> 盘中执行 -> 自动虚拟组合复盘 -> 统一报告。
- 新增 `quantdesk/daily_workflow_orchestrator.py`、`scripts/run_daily_workflow.py`、`results/daily_workflow_latest.json`、`reports/daily_workflow_report_*.md`。
- 修复板块/题材映射统计：`source=local` 但内容仍未知时不计入已映射；`mapped_count` 只统计真实 sector / industry / primary_theme / concept_tags / theme_tags。
- 联网补全板块/题材失败时保留 `failed_reason`，akshare 未安装、接口失败、返回为空、股票未匹配都不崩溃，并生成 pending 待补文件。
- UI 清理 `nan` / `NaN` / `None` / 空值，并将 false/true、long_swing、no_fill、block 等值中文化。
- 默认 GET 只读、POST 才触发任务；页面初始加载不联网、不跑重任务；一键工作流默认不联网补板块、不扫描全 A、不自动建仓。
- 继续保持不接券商、不真实下单、不保存真实账户、不构成投资建议、不承诺收益。

## v2.8.4 Conditional Position Management Engine

- 将自动虚拟组合 update 拆成：冻结条件交易计划 -> 分钟线盘中执行 -> 连续账本更新。
- 新增 `results/conditional_trade_plans_latest.json` 和 `results/conditional_execution_latest.json`。
- 新候选必须先生成低吸/突破/不追高计划，盘中触发才模拟买入；缺少分钟线时 no_fill，不用收盘价假装成交。
- 已有持仓支持模拟加仓、突破加仓、减仓一半、止盈、止损、清仓、退出观察、继续持有和风控阻断。
- 默认 `trigger_model=minute_close`，不使用当天最高价/最低价倒推成交。
- 保持 GET 只读、POST 才触发任务；页面初始加载不联网、不跑重任务；默认只处理低价学习池 included=true。
- 继续保持不接券商、不真实下单、不保存真实账户信息、不构成投资建议、不承诺收益。

## v2.7.8 Intraday Swing Execution Engine

- 前一交易日生成次日波段计划（低吸/突破/止盈/止损价格区间）
- 下一交易日按分钟线逐条回放触发
- 避免未来函数
- 接入自动虚拟组合 update 流程，输出 `results/swing_trade_plans_latest.json`、`results/intraday_execution_latest.json`、`reports/swing_execution_report_*.md`
- UI 在自动虚拟组合中展示“长线波段计划”和“盘中触发结果”
- 尚未完成：打板盘中可成交性、题材 / 板块热度分组、新闻热度。
- 不接券商、不真实下单、不构成投资建议

## v2.7.6 Workflow Unification Auto Portfolio

- 低价候选 → 自动策略沙盒 → 一键初始化自动虚拟组合 → 每只默认 100 股 → 每天更新组合盈亏 → 每个动作说明原因 → 策略决策审计基于真实组合动作。
- 旧版手动虚拟组合保留但明确标注。
- 不接券商、不真实下单、不构成投资建议。

## v2.7.5 Auto Paper Fixed 100 Sizing

- 自动策略沙盒默认采用固定 100 股纸面买入数量。
- `max_position_pct` 在默认模式下仅作为单票风险上限。
- 保留 `percent_cap` 作为进阶仓位比例模式。

## v1.4 Focus

QuantDesk v1.4 focuses on stability and user experience:

- Keep the original scoring formula unchanged.
- Keep backtest return logic unchanged.
- Do not add new strategy dimensions.
- Do not connect live trading or broker APIs.
- Improve conservative wording, beginner explanations, UI readability, paper-portfolio snapshots, and clean export privacy.

## Project Boundary

This project is a local learning and review tool for A-share observation. It organizes data quality, market-calendar status, news health, candidate explanations, virtual portfolio records, and backtest audit files. It does not provide trading instructions and does not promise outcomes.

## Maintenance Note

This is a personal learning project. Discussion and suggestions are welcome, but there is no commercial maintenance commitment.

## v2.7.4 Auto Strategy Sandbox and Workbench Polish

Status: local-only automatic paper-strategy sandbox.

- Add `quantdesk/auto_strategy_sandbox.py` and `scripts/run_auto_strategy_sandbox.py`.
- Auto-select simulation-ready symbols from the low-price learning universe.
- Split candidates into long and board paper-simulation groups.
- Generate local pending orders with `order_source=auto_strategy_sandbox`.
- Write local JSON / Markdown artifacts for automatic review and learning notes.
- Add `/api/auto-strategy/run`, `/api/auto-strategy/latest`, `/api/auto-strategy/report/latest-html`, and `/api/auto-strategy/artifacts`.
- Add a top-level “自动策略沙盒” UI tab. Initial page load is read-only and never starts the sandbox.
- No broker API, no real orders, no price prediction, and no investment advice.

## v2.7.3 Low Price Workbench Schema and Defaults

## v2.7.3 Low Price Workbench

Status: UI information-architecture cleanup for the low-price learning workflow.

- Promote “低价工作台” to a top-level navigation tab.
- Move low-price pool build, included-symbol daily update, low-price local report, paper simulator entry, and artifact center out of the report-generation panel.
- Keep the data center focused on ordinary daily report actions, with a redirect button to the low-price workbench.
- Keep initial page load read-only: no network update, no full-A scan, no report generation, and no paper-simulation run.
- No strategy, scoring, broker, real-order, or investment-advice capability is added.

## v2.7.2 Report UI Stability and Paper Simulation

Status: frontend null-DOM fix, low-price report structured rendering, local paper-trading simulator CLI.

- Safe DOM helpers eliminate `Cannot set properties of null` crashes.
- Low-price report: summary cards + HTML table + collapsible Markdown.
- `scripts/run_paper_trading_simulator.py`: backtest / live paper trading with A-share rules.
- `quantdesk/strategy_signals.py`: long and board signal generators.
- No broker, no real orders, no investment advice.

## v2.7.1 Low Price Report UI Stability

Status: low-risk UI and report-preview hardening for the low-price learning workflow.

- Add null-safe frontend DOM helpers for report loading and legacy panel updates.
- Add read-only latest-report APIs for JSON summary, Markdown content, and HTML preview.
- Add browser print / save-as-PDF flow through `/api/learning-universe/low-price/report/latest-html`.
- Clarify that low-price cache reports are separate from ordinary daily candidate results.
- No strategy change, no scoring change, no `/api/results` contract change, no full-A scan, no network-triggering page load.

## v2.7.0 Low Price Cache Report

Status: local-only low-price learning report. The flow reads only `data/universe/low_price_learning_symbols.csv` and the matching local `data/daily/{symbol}.csv` files for `included=true` symbols.

- Add `scripts/generate_low_price_report.py`.
- Add `POST /api/learning-universe/low-price/generate-report`.
- Add a "生成低价学习池本地报告" button to the existing low-price learning panel.
- Does not connect to any network provider, does not scan full A, does not change strategies, scoring, daily report defaults, or the existing report JSON contract.

## v2.6.9 Low Price Universe Daily Update

Status: minimal closed-loop daily-update for the included symbols of the low-price learning universe. No change to the full-A update flow, the daily report, the candidate JSON, or trading boundaries.

- Add `scripts/update_low_price_universe_daily.py` (thin wrapper that filters the low-price universe and delegates to `scripts/update_market_data.py`).
- Add `--update-mode-tag` to `scripts/update_market_data.py` and expose a new `mode` field in `data/cache/update_status_running.json` (falls back to `universe_mode` for legacy compatibility).
- Add read-only `GET /api/learning-universe/low-price/update-daily/summary` and start-task `POST /api/learning-universe/low-price/update-daily`. Both reuse the existing `update_market_data` task lock.
- Add a "更新低价学习池日线" button to the existing low-price universe panel with a running-guard precheck against `/api/update-market-data/progress`.
- Strictly offline build path is unchanged; the daily-update path only fetches data for `included == True` symbols.
- Does **not** change `daily_quant_report` defaults, `update_market_data` defaults, the candidate JSON, or the report JSON schema.

## v2.6.8 Low Price Snapshot Universe

Status: minimal closed-loop offline pool builder, no strategy / scoring / daily-report change.

- Add `quantdesk/learning_universe.py` with `low_price_universe_path` / `build_low_price_universe_from_spot` / `save_low_price_universe` / `load_low_price_universe` / `summarize_low_price_universe`.
- Add `scripts/build_low_price_universe.py` CLI (`--max-price`, `--buffer-price`, `--min-amount`).
- Add read-only `GET /api/learning-universe/low-price/summary` and offline-only `POST /api/learning-universe/low-price/build`.
- Add a lightweight summary panel + "构建低价学习池" button to the UI. Panel is read-only on initial load.
- Output file `data/universe/low_price_learning_symbols.csv` is a deletable / rebuildable derivative artifact.
- Strictly offline: no AKShare / Sina / Eastmoney, no `data/daily` scan, no `quantdesk/daily_cache_index` dependency, no auto trigger of `scripts/update_market_data.py`.
- Does **not** change `daily_quant_report` defaults, `update_market_data` defaults, the candidate JSON, or trading boundaries.

## v2.6.7 Stability Patch

Status: stability-only patch, no strategy or trading change.

- Coerce `open` / `high` / `low` / `close` / `volume` / `amount` to numpy `float64` at the entry of `quantdesk/indicators.py::add_indicators` so KDJ / RSI / BOLL / ATR / MACD never raise `DataError: No numeric types to aggregate` on `pd.NA` / object-dtype input.
- Add `quantdesk/update_task_lock.py` to serialize `scripts/update_market_data.py` invocations via `data/cache/update_market_data.lock`. The local API server refuses to launch a second concurrent update task. Stale locks (dead pid) are cleared automatically.
- UI now refuses to launch a second update from any of the four `/api/update-market-data` buttons and the chained `update-and-generate-daily-report` flow; a clear bilingual notice is shown.
- Clarify that `--spot-source cache` only controls the spot snapshot source; daily-bar offline mode (`--no-daily-refresh`) is recorded as a future option in `docs/known_logic_issues.md`.
- Keep report JSON structure, strategy logic, scoring, and trading boundaries unchanged.

## v2.6.6 Daily Cache Index

Status: local cache acceleration layer for incremental updates, no strategy or trading change.

- Add `data/cache/daily_cache_index.json` as a lightweight metadata index for `data/daily/*.csv`.
- Use the index first in incremental updates, then fall back to CSV reads when the index is missing, stale, or damaged.
- Keep the index delete-and-rebuildable without changing CSV format or report JSON structure.
- Expose a read-only `/api/daily-cache-index/summary` summary for UI/status use.
- Keep report JSON structure, strategy logic, scoring, and trading boundaries unchanged.

## v2.6.5 Incremental Parallel Progress

Status: release hardening for market-data update flow, no strategy or trading change.

- Add optional `--workers` for limited parallel daily-cache updates; default `1` preserves serial behavior.
- Add lightweight running progress file `data/cache/update_status_running.json`.
- Add read-only progress API `/api/update-market-data/progress`.
- Show incremental-update progress in the UI during data update and update-then-report flows.
- Keep report JSON structure, strategy logic, scoring, and trading boundaries unchanged.

## v2.6.4 Data Health Recorder Split

Status: low-risk refactor, no new business feature.

- Extract data-health recording / aggregation helpers from `scripts/daily_quant_report.py` into `quantdesk/data_health_recorder.py` (`DataFetchStatus`, `DATA_HEALTH_ROWS`, `DATA_HEALTH_COLUMNS`, `reset_data_health`, `record_data_health`, `data_health_frame`, `data_health_summary`, `data_health_reason_summary`, `attach_data_health`, `data_quality_risk_note`, `data_risk_flag`, `expected_latest_trading_date`, `_health_reference_datetime`, `data_freshness_status_from_quote`, `classify_data_warning`).
- `scripts/daily_quant_report.py` re-exports every moved name so existing tests, scripts, and external imports keep working unchanged.
- API paths, CLI flags, JSON payloads, report structure, and UI behavior are unchanged.
- No new strategy, factor, indicator, broker connection, automated order, or predictive wording is added.

Data Health Recorder Split：抽出 data_health_recorder.py，daily_quant_report.py 保留兼容 shim，API / CLI / JSON 不变。

## v2.6.3 Codebase Slimming

Status: low-risk refactor, no new business feature.

- Extract pure helpers from `scripts/self_check.py` into `quantdesk/self_check_core.py` so checks stay unit-testable.
- Extract stateless watchlist / manual-confirmation / live-monitor request payload builders into `quantdesk/server_payloads.py`; API paths and JSON shapes are unchanged.
- Document a future split map for `daily_quant_report.py`, `backtest.py`, `strategy_trade_replay.py`, and `explanations.py` in `docs/codebase_slimming_plan.md`.
- Add `tests/helpers.py` for shared version / UI text assertions; existing tests keep using `unittest`.
- No new strategy, factor, indicator, or trading capability.

## v2.6.2 Release Hardening

Status: release hardening, no new business feature.

- Clarify report sample size versus full local stock universe / CSV inventory in the UI.
- Add safer wrapping for long report filenames, paths, and export links.
- Keep incremental updates from repeatedly pulling history-short stocks that are already fresh.
- Keep provider summary scoped to each update run.
- Surface walk-forward LOW / INVALID quality warnings more clearly.

## v2.6.1 Stability Cleanup

Status: released as a low-risk maintenance pass.

- Version metadata is centralized in `quantdesk/version.py`.
- Public CLI / UI / API behavior remains unchanged.
- No new business feature is added.

## v2.6.0 Intraday Strategy Monitor

Status: released as the local intraday strategy monitoring layer.

- Local intraday polling with Sina market data when available.
- Virtual trade triggers for observation and manual review only.
- Start / stop / status / single-cycle APIs for the local app server.
- UI entry for intraday monitoring inside the local workbench.
- No broker API, no automatic order placement, no promised outcome.

## v1.6 Strategy Demo Engine

Stage 1 introduces a read-only strategy demonstration layer:

- Check predefined rule conditions only.
- Show the mechanical rule process for learning and review.
- Do not write portfolio state.
- Do not calculate future outcome metrics.
- Do not estimate price direction.
- Do not connect live trading or broker APIs.

## v1.6.1 Strategy Sandbox Completion

The sandbox expands from single-stock rule checks to candidate-wide learning review:

- Show a strategy overview for each current candidate.
- Show which observation conditions are already satisfied.
- Show which observation conditions are not yet satisfied.
- Explain missing fields and feature-completion status.
- Mark rows blocked by data reliability or market session.
- Include at least two local templates: moving-average breakout and moving-average pullback observation.
- Keep the sandbox read-only: no portfolio writes, no outcome metrics, no price-direction estimate.
- Do not calculate win-rate metrics and do not calculate annualized-return metrics.

Later stages may add:

- Stage 2: manual virtual action records into the local paper portfolio.
- Stage 3: strategy review panel.
- Stage 4: local review coach.

All stages keep the learning-sandbox boundary and do not become a real trading system.

## v1.6.2 Data Freshness, Strategy Features, And Stock Classification

- Distinguish theoretical daily-bar date, actual latest complete daily bar, intraday estimate time, report generation time, and mixed freshness.
- Show data-date labels in the main workbench areas so users can tell whether a panel is based on complete daily bars, intraday estimates, fallback data, or mixed data.
- Map real-report `amount_yuan` and `amount_ratio` into strategy-demo activity features, without treating `vol20` as volume.
- Add a stock classification view that groups candidates by strategy status, data status, trend, trading activity, risk, and virtual-portfolio state.
- Keep strategy checks read-only in this stage.

## v1.7 Simulation Loop

- Virtual action confirmation flow for strategy-demo results.
- Strategy source, matched conditions, failed conditions, missing fields, data date, and risk notes are recorded into paper-trading logs.
- Virtual risk lines and virtual position planning are available for simulation review.
- Weekly virtual trading review summarizes local paper records.
- Still no broker API, no automatic order placement, and no promised outcome.

## v1.8 Indicators And Multi-Strategy Framework

- Local indicator module for MA, EMA, MACD, RSI, KDJ, BOLL, ATR, activity ratios, high/low windows, and volatility.
- `config/strategy_demo.yaml` documents strategy-template configuration while built-in strategies remain the safe fallback.
- More predefined strategy demonstrations are available.
- Multi-strategy comparison shows how all local templates review one stock.

## v1.9 Planned Research Credibility Layer

- Candidate replay vs strategy historical replay.
- Benchmark comparison.
- Market-regime layer.
- Fundamental red-flag layer.

## v2.0 Planned Local Decision Assistant

- Local action-plan reminders.
- Manual confirmation queue.
- Watchlist.
- Local monitor script.
- No broker API, no automatic order placement, and no promised outcome.


## v1.9 研究可信度层

状态：已接入本地只读研究可信度框架。

- benchmark：优先读取本地缓存，缺失时降级为 provider_unavailable。
- market_regime：基于本地基准数据判断市场环境相对平稳、承压或未知。
- fundamental_health：财务红线检查保持 optional provider，AKShare 缺失不影响主功能。
- candidate_replay：对当前候选股做候选复盘。
- strategy_historical_replay：对本地历史候选样本做策略历史回放，并提示幸存者偏差。

v1.9 仍不接券商、不自动下单、不承诺收益。v2.0 继续规划实战辅助、人工确认清单、自选股和本地观察提醒。
 
## v2.0 Local Decision Assistant Implementation

Status: in progress as a local-only decision-assistance and simulation-review layer.

- Add local action-plan overview for strategy review results.
- Add a manual confirmation queue for items that need human review.
- Add a watchlist for local observation, tags, strategy binding, and local reminders.
- Add local monitor preview APIs and `scripts/live_monitor.py`.
- Keep all state files out of clean zip: `state/`, `paper_portfolio.json`, `watchlist.json`, `manual_confirmation_queue.json`, and monitor event logs.
- No broker API, no automatic order placement, no account credentials, no promised outcome, and no replacement for the user's own decision.
## v2.7.9 Field Alias Intraday Data Theme Map

- 统一旧字段别名到 `amount_ma20`、`liquidity_status`、`data_confidence_score`、`data_confidence_status`。
- 提供本地 1 分钟线 CSV 检查、导入与模板，不用日线假装盘中触发。
- 提供本地 `stock_theme_map.csv` 题材/板块映射和模板，自动策略沙盒按题材/板块展示候选。
- 自动策略沙盒候选上限调整为长线 20、打板 10，并支持每题材候选数量限制。
## v2.8.0 Auto Theme Map Updater

- 新增 `scripts/update_stock_theme_map.py`，自动生成/维护 `data/universe/stock_theme_map.csv`。
- 默认只处理低价学习池 `included=true` 股票，不默认扫描全 A。
- 优先本地提取板块/题材字段；用户手动触发时可尝试 AKShare 联网补全。
- 保护 `source=manual` 的手工映射，失败时生成 `stock_theme_map_pending.csv`。
- 新增 `/api/theme-map/status`、`/api/theme-map/preview`、`POST /api/theme-map/update`。
- UI 增加“板块/题材映射状态”面板，并明确联网补全只在用户点击后执行。

## v2.8.1 Persistent Auto Portfolio Review

- `state/auto_paper_portfolio.json` 成为连续自动虚拟组合账本，`init` 默认不得覆盖已有状态。
- 新增 `--force-reset`，只有用户显式确认时才允许重置自动虚拟组合。
- `update` 保留昨日持仓，先刷新最新价、盈亏和持有天数，再处理今日候选。
- 已持仓股票不会重复买入；不再入选股票进入 `watch_exit` 或按卖出规则纸面卖出。
- 输出 `daily_actions`、`decision_review`、`yesterday_holding_review`，并持续追加 `results/auto_paper_portfolio_history.json`。
- UI 新增“昨日持仓复盘”，并区分旧 `paper_trading_latest.json` 与新自动虚拟组合状态。
## v2.8.2 Local Daily Feature Engine

- 新增 `quantdesk/daily_feature_engine.py` 和 `scripts/build_daily_features.py`。
- 生成 `data/features/daily_features_latest.csv`，沉淀本地均线、涨跌、波动、成交额、换手、涨停近似、连板近似、新高新低和相对指数强弱特征。
- 默认只处理低价学习池 `included=true` 股票；全市场扫描必须由用户显式点击高级操作或传入 `--only-low-price false`。
- 新增只读 API `/api/daily-features/status`、`/api/daily-features/preview`，以及手动触发的 POST `/api/daily-features/build`。
- 自动策略沙盒读取本地特征表，在候选表展示 20 日涨跌、相对指数强弱、180 日新低、连板近似和换手等字段。
- 保持边界：页面初始加载不生成特征、不联网、不接券商、不真实下单、不构成投资建议。
## v3.3.2 数据更新工作台与预检

Release: v3.3.2 Data Update Workbench and Auto Refresh Preflight

本轮收敛数据入口：用户先在“数据更新”判断本地日线是否落后，再用 data-health 检查新鲜度和覆盖率，最后进入 daily-retain、strategy-validate 和 observation-report。默认不联网；显式联网也只处理指定 symbols 或低价学习池 `included=true`。
## v3.3.5 remediation flow

Release: v3.3.5 Command Output Error Actionability and Daily Remediation Flow

下一步治理重点从“显示 warning”推进到“给出可复制修复命令”。`qd.py fix-plan`、`daily_console_summary.remediation_actions`、`status.remediation_actions`、command-center 和 artifact center 会统一展示 stale data、missing intraday、schema secret-like reference、provider offline skipped、stale report 等问题的修复路径。该能力只做流程引导，不新增策略、不优化参数、不构成投资建议。

## v3.3.6 remediation UI copy and artifact preview polish

Release: v3.3.6 Remediation Ui Copy And Artifact Preview Polish

本轮把 v3.3.5 的 remediation flow 打磨成 UI/CLI 可直接使用的日常体验：所有用户可见区域统一成“我现在该怎么修”“修复建议”“重新生成 / 修复命令”，命令按“离线可运行，不会联网”和“需要显式联网授权”分组，长命令折叠显示但复制完整命令。产物中心继续只读展示 JSON/Markdown 摘要，不读取密钥、不触发任务。下一轮若继续演进，应优先处理更多产物 stale 判定和更细的命令运行历史摘要，而不是新增策略或收益逻辑。
## v3.4.0 Market Intelligence Sector Low Price Board Watch Workbench

- 已完成：`market-intel` 市场观察总控命令。
- 已完成：市场环境、板块题材、消息面、低价纸面观察候选、情景观察曲线。
- 已完成：UI 市场观察页面、命令中心、产物中心、归档和 schema-check 接入。
- 继续保持：默认离线、默认不调用 LLM、默认不扫描全 A、不输出实盘交易指令、不构成投资建议。


# Current Release

v3.5.1 Truthful Attribution Installer And Structure Hardening (`v3.5.1-truthful-attribution-installer-and-structure-hardening`) fixes release honesty gaps from v3.5.0. True Brinson attribution now belongs to `qd attribution`; observation diagnostics remain available through `qd risk-observation` and the compatibility alias `qd risk-attribution`. SourcePortable is Python-required unless a real `QuantDesk.exe` exists. Package grouping migration has advanced into real `research`, `paper`, `intraday`, and `agent` modules, with compatibility aliases where needed.

Historical plan: `docs/roadmaps/v3.5.0_release_plan.md`.

Current next candidates:
- v3.5.2-installer-hardening-on-clean-windows
- v3.5.2-ui-polish
- v3.5.2-real-data-onboarding
- v3.5.2-deeper-package-migration
- v3.5.2-video-tutorials
# v3.7.0.2 Strategy Pipeline Repair

Current small release: repair strategy validation plumbing before the next viability confirmation sprint. Do not treat repair reruns as production-ready strategy conclusions.
## v3.7.0.3 Low Price Pipeline Finalization

- Current focus: finalize low-price research pipeline before low-price viability confirmation.
- Next suggested release: `v3.7.0.4-low-price-strategy-viability-confirmation`.
# v3.7.0.6 Strategy Regression Guard and Report Hygiene

This hygiene release protects strategy viability reports from polluted `latest` outputs, incomplete sensitivity grids, duplicated baseline signatures, and suspicious performance metrics.
