# QuantDesk Changelog

> Complete release history is consolidated here. Individual RELEASE_NOTES_v*.md files are retained but this file is the canonical source.

QuantDesk defaults remain conservative across releases: no broker API, no real orders, no investment advice, no promised returns, `network=false` by default, and optional LLM features disabled by default.

## v3.7.0.6 - Strategy Regression Guard And Report Hygiene

- Added strategy regression audit diagnostics for baseline distinctness,
  metric sanity, report provenance, and latest-output hygiene.
- Added smoke-run versus `write_latest` separation so test runs do not silently
  pollute release/latest strategy reports.
- Added guards for suspicious identical strategy baselines and abnormal
  performance metrics while keeping those warnings visible.
- Kept v3.7.0.5 import alias reconciliation intact and preserved deprecated
  root import compatibility.
- No strategy formula changes, no scoring-weight changes, no backtest return
  logic changes, no broker connection, no real orders, no default network, and
  no default LLM usage.

### Consolidated release-note source detail: `docs/history/RELEASE_NOTES_v3.7.0.6.md`

`v3.7.0.6-strategy-regression-guard-and-report-hygiene` is a small engineering hygiene release for strategy report integrity.

- Adds strategy regression audit diagnostics.
- Adds report provenance and `write_latest` / smoke-run separation for strategy reports.
- Adds baseline distinctness, top-level metric consistency, and performance sanity guards.
- Keeps v3.7.0.5 import alias reconciliation intact; deprecated root import paths remain supported.
- Research only: no broker connection, no real orders, no automatic trading, and no investment advice.

## v3.7.0.5 - Git Deletion Reconciliation

- Added import alias reconciliation for deleted legacy root modules.
- Confirmed deprecated root import paths are covered by compatibility shims instead of restoring old files.
- Reclassified git deletion audit risk so shim-covered legacy paths are not treated as restore-required high risk.
- Rewrote runtime Python imports to subpackage paths while keeping legacy aliases for compatibility.
- Preserved v3.7.0.4 strategy and capital / lot sensitivity results unchanged.

### Consolidated release-note source detail: `docs/history/RELEASE_NOTES_v3.7.0.5.md`

v3.7.0.5 is a pure engineering hygiene release. It reconciles tracked deletions left after root-level modules were migrated into subpackages.

- Root-level legacy modules remain deleted; they are not restored or duplicated.
- Deprecated root import paths are covered by `quantdesk.__init__` compatibility aliases.
- New code should import from subpackages such as `quantdesk.paper`, `quantdesk.intraday`, `quantdesk.agent`, and `quantdesk.research`.
- Git deletion audit now distinguishes shim-covered legacy paths, broken aliases, runtime imports, docs-only references, and shim-definition references.
- Deleted root modules with no moved target and no runtime references are treated as retired legacy deletions, not restore-required failures.
- This release does not change strategy logic, low-price research results, backtest assumptions, broker boundaries, or network defaults.

Research and safety boundaries remain unchanged: no broker connection, no real orders, no automatic trading, no investment advice, and network off by default.

## v3.7.0.4 - Capital Lot Sensitivity And Git Hygiene

- Added read-only git deletion audit with import-reference risk classification.
- Added low-price capital / lot sensitivity grid for 100-share minimum trade constraints.
- Added reporting for invested ratio, cash drag, unfilled lot count, and research direction decision.
- Kept runtime data out of clean release zips and preserved no broker / no real orders / no default network boundaries.

### Consolidated release-note source detail: `docs/history/RELEASE_NOTES_v3.7.0.4.md`

`v3.7.0.4-capital-lot-sensitivity-and-git-hygiene` is an evidence-hardening patch, not a strategy optimization release.

#### Highlights

- Added read-only `maintenance git-deletion-audit` for tracked deletions, untracked files, and import-reference risk.
- Added `strategy-viability capital-lot-sensitivity` for `initial_cash`, `max_positions`, `max_price`, and 100-share lot sensitivity.
- Reports `invested_ratio_avg`, `unfilled_due_to_lot_size_count`, cash drag severity, and next research direction.

#### Boundaries

Research only. No broker, no real orders, no automatic trading, no AI Advisor, no default network, and no runtime data in clean zips.

## v3.7.0.1 - Strategy Viability On Data Backbone

- Added Strategy Viability data precheck for local daily CSV schema, benchmark availability, historical listings, survivorship-bias risk, and partial-daily isolation.
- Added five price-only baseline viability tests with monthly rebalance, one-day execution lag, transaction costs, yearly returns, and decision gate.
- Kept SQLite as shadow layer, CSV fallback, no AI Advisor, no broker, no real orders, and no default network access.

### Consolidated release-note source detail: `docs/history/RELEASE_NOTES_v3.7.0.1.md`

v3.7.0.1 is a small strategy viability sprint on top of the v3.7.0 data backbone.

#### What Changed

- Added data precheck for local daily CSV schema, benchmark availability, historical listings, survivorship-bias risk, and partial-daily isolation.
- Added five research-only price baseline tests: `momentum_20d`, `low_volatility`, `momentum_low_vol`, `trend_continuation`, and `rsi_recovery`.
- Added monthly rebalance simulation with one-day execution lag, new-stock and zero-volume filters, and explicit transaction costs.
- Added Strategy Viability Report with annual return, max drawdown, Sharpe, turnover, yearly returns, cost drag, best strategy, and decision gate.

#### Release Honesty

- This release does not expand the SQLite schema or replace CSV.
- SQLite remains a shadow layer and CSV remains the fallback.
- No AI Advisor, no OpenAI/Claude API, no broker integration, no real orders, and no automatic trading.
- The report must keep weak or failed strategies visible.
- If delisted/ST history is unavailable, survivorship-bias risk is `high` or `unknown`, never `low`.
- SourcePortable still requires Python.

## v3.7.0 - Data Backbone

- Added SQLite shadow layer with WAL, single-writer lock, schema initialization, and batch upsert repositories.
- Added CSV-to-SQLite migration and compare flow with forward/back/raw daily tables kept separate.
- Added DualWriter for CSV + SQLite writes while retaining CSV fallback.
- Added network-gated AKShare/provider chains for daily, fundamentals, sectors, and intraday cache discovery.
- Added Parquet intraday cache manifest support without making DuckDB a dependency.
- Added data layer health, bounded data quality repair, rollback commands, and v3.7.0 release proof tests.

### Consolidated release-note source detail: `docs/history/RELEASE_NOTES_v3.7.0.md`

v3.7.0 is a data backbone release: SQLite shadow layer + AKShare/network-gated provider chain + CSV migration + DualWriter + data health.

#### What Changed

- Added a SQLite shadow layer under `quantdesk/data_store`.
- Added `QuantDeskDB`, schema initialization, WAL, busy timeout, single-writer lock, and batch transactions.
- Added separate daily tables for forward-adjusted, back-adjusted, and raw bars.
- Added `intraday_partial_daily` as an isolated incomplete-data table.
- Added CSV scanner, migration, sample compare, and row hash checks.
- Added DualWriter so CSV and SQLite can be written together while CSV remains the fallback.
- Added safe whitelisted `data-query` support.
- Added `data-health.strategy_readiness` as a readiness audit only: it checks daily-bar coverage, `signal_date < execution_date`, survivorship-bias risk, and partial-daily isolation without running strategy backtests.
- Added AKShare lazy-import provider helpers with `network=false` guard.
- Added provider chains for fundamentals, sectors, and intraday cache discovery.
- Added Parquet intraday cache path/manifest support without making DuckDB a dependency.
- Added data layer health and bounded data quality repair checks.

#### Release Honesty

- SQLite is a shadow layer, not the only data source.
- CSV files are retained as compatibility and rollback paths.
- `read_prefer_sqlite` defaults to `false`.
- DualWriter can be enabled, but CSV fallback remains available.
- AKShare provider calls require `network=true`.
- Financial and sector coverage depends on local data and available provider interfaces.
- mootdx is not treated as a complete financial statement source.
- Minute history remains limited; v3.7.0 stores Parquet/cache manifests rather than forcing minute bars into SQLite main tables.
- No broker integration, no real orders, and no automatic trading.
- SourcePortable still requires Python.
- PyInstaller / NSIS artifacts are honestly skipped when the tools are unavailable.

## v3.6.2 - Real Similar Day Stress And Style Attribution

- Similar Day now performs local index CSV feature computation and historical neighbor search instead of returning fixed examples.
- Stress Scenarios now estimate paper portfolio impact from historical scenario windows using symbol history, sector proxy, or index beta proxy.
- Barra Style Attribution Lite now computes 10-factor exposure, contribution, coverage, dominant tilts, and residual alpha when local inputs allow.
- Decision Journal restores add/list/review flows and adds outcome/statistics summaries by strategy, emotion, and confidence bucket.
- UI and daily workflow expose risk overlay and v3.6.2 research snapshots without adding trading controls.
- Added text encoding hygiene checks and BOM cleanup.

### Consolidated release-note source detail: `docs/history/RELEASE_NOTES_v3.6.2.md`

`v3.6.2-real-similar-day-stress-and-style-attribution` turns the remaining research-lite modules into local-data computations.

- Similar Day reads local index CSV history, computes market features, searches historical neighbors, and reports forward returns.
- Stress Scenarios use historical scenario windows and estimate paper portfolio impact from symbol history, sector proxy, or index beta proxy.
- Style Attribution computes 10-factor portfolio exposure and contribution from local CSV inputs.
- Decision Journal restores add/list/review flows and adds outcome/statistics summaries by strategy, emotion, and confidence bucket.
- UI markers expose risk overlay, Similar Day, Stress Test, Style Attribution, and Decision Journal results.
- Text encoding hygiene now checks and can remove UTF-8 BOM from source text files.

Known limitations:
- Missing local data produces warnings, not fabricated real results.
- Synthetic mode is explicit and marked as synthetic.
- All outputs are research-only. No broker, no real orders, no automated trading.
- SourcePortable still requires Python unless a real standalone artifact is produced.

## v3.6.1 - Real Risk Alerts And Smart Stop Integration

- Added a unified paper position input layer for paper portfolios, paper accounts, paper watchlists, multi-strategy books, intraday workbench snapshots, and stock-picking flow candidates.
- Smart stop now evaluates paper positions with ATR trailing stop, fixed-percent stop, time stop, and sector-decay context without changing holdings.
- Risk alerts now consume smart stop, concentration, liquidity, intraday, sector, and market snapshots and emit notification-ready research alerts.
- Notification center now consumes risk alerts, writes local JSONL dry-run messages, and supports dedupe cooldown state without leaking secrets.
- Daily workflow and UI now surface risk alert and smart stop snapshots.
- Clarified that v3.6.0 was a skeleton/lite/local-adapter release; v3.6.1 begins real module-to-module risk integration.

### Consolidated release-note source detail: `docs/history/RELEASE_NOTES_v3.6.1.md`

Release string: `3.6.1-real-risk-alerts-and-smart-stop-integration`

v3.6.1 turns the v3.6.0 risk skeleton into a paper-only daily risk loop. It remains local and research-only: no broker, no real orders, no automatic trading, no automatic position changes, and `network=false` by default.

#### Risk Integration

- Added `quantdesk/risk/position_inputs.py` to load paper portfolio, paper account, paper watchlist, multi-strategy book, intraday live workbench, and stock-picking flow snapshots through one normalized position schema.
- Smart stop now evaluates each paper position with ATR trailing stop, fixed percent stop, time stop, and sector decay context.
- Risk alerts now consume smart stop, paper concentration, liquidity, intraday drawdown, sector weakening, and optional market snapshots.
- Position management can attach additive `risk_overlay` metadata without changing conditional plan decisions.

#### Notification Pipeline

- Notification center can consume `results/risk_alerts_latest.json`.
- Added local dedupe state with configurable cooldown.
- Local file JSONL notifications are implemented; external channels remain disabled by default.
- `qd risk-alerts --notify true --dry-run true` writes risk alerts and a dry-run notification report.

#### Daily And UI

- Daily workflow reads risk alert and smart stop snapshots as warnings, not blockers.
- UI Home / risk panel surfaces Risk Alerts, Smart Stop, and Notifications cards.
- Missing latest JSON files remain warnings and show command hints.

#### Safety

- No broker connection.
- No real orders.
- No automatic position changes.
- No secret/token logging.
- SourcePortable remains Python-required unless true standalone artifacts exist.

## v3.6.0 - Real Use Quant Platform Completion

- Added a real-use research loop: data integrity, sector context, strategy recommendation, stock-picking flow, paper watchlist, candidate review, smart-stop warnings, risk alerts, notifications, and decision journal.
- Added adjusted-price data governance for raw / forward-adjusted / backward-adjusted daily data plus corporate-action schema checks.
- Added extended factor library modules, dynamic factor model, execution-realism assumptions, stress scenarios, similar-day lookup, multiperiod attribution, and lite style attribution.
- Added local CSV adapters for money flow, dragon-tiger, fundamentals, industry classification, index membership history, and optional sqlite local store.
- UI adds read-only v3.6.0 panels and Home cards without broker, order, or real-trading controls.
- SourcePortable remains Python-required unless optional standalone artifacts are genuinely created; PyInstaller and NSIS status remain honest.

### Consolidated release-note source detail: `docs/history/RELEASE_NOTES_v3.6.0.md`

Release string: `3.6.0-real-use-quant-platform-completion`

This release turns QuantDesk into a broader personal quantitative research and paper-risk platform. It remains research-only and paper-only: no broker connection, no real orders, no automatic trading, no default network access, and no buy/sell/order instructions.

#### New Real-Use Flow

- Stock picking flow combines local data health, sector rotation, strategy recommendation, candidate review tasks, and paper watchlist suggestions.
- Paper watchlist stores local observation candidates under `state/` and is excluded from clean zips.
- Candidate review summarizes synthetic or local follow-up performance without becoming advice.

#### Data Integrity

- Added raw / forward-adjusted / back-adjusted price layout policy.
- Added corporate actions schema helpers for dividends and splits.
- `qd data-integrity` reports missing local data as warnings, not fake data.

#### Risk And Alerts

- Added smart stop, concentration checks, liquidity warnings, and risk alerts.
- These modules only warn. They do not close positions, send orders, or mutate broker state.

#### Notification Center

- Notifications are disabled by default and dry-run by default.
- Local file notifications are supported; external channels remain opt-in and skipped unless configured.
- Secrets are not written to reports.

#### Research Extensions

- Added dynamic factor model lite and value/quality/microstructure/sentiment/event factor helpers.
- Added optional execution realism assumptions, partial fill, limit-up fill model, slippage curve, and China A fee helpers without changing default backtest behavior.
- Added multiperiod attribution, style attribution lite, return explain, stress scenarios, similar day, and decision journal.
- Added local CSV adapters for money flow, dragon tiger, fundamentals, industry classification, and index membership history.
- Added optional SQLite local store. DuckDB remains optional and is not required.

#### Packaging

SourcePortable remains Python-required unless true standalone artifacts exist. PyInstaller and NSIS status manifests are honest skipped/passed/failed records and must not be faked.

#### Known Limitations And Release Honesty

- v3.6.0 is a skeleton / lite implementation release: it established modules, CLIs, payloads, UI panels, tests, and release proof.
- `risk_alerts` in v3.6.0 is synthetic-first and is not fully connected to a real paper portfolio.
- `smart_stop` in v3.6.0 provides ATR calculations but is not fully integrated into position management.
- `notification_center` in v3.6.0 is dry-run/local-first and does not auto-consume risk alerts by default.
- `similar_day` and `stress_scenarios` are lite placeholders and will be deepened later.
- No broker, no real orders, no automatic trading, and SourcePortable still requires Python.

## v3.5.2 - UI Home And Strategy Recommender

- Added a Home / Overview UI, grouped sidebar navigation, and a lighter visual token system for cards, buttons, tables, badges, and monospaced numbers.
- Added `quantdesk/research/strategy_recommender.py` and `qd strategy-recommend` for research-only long-term / swing / board / observe / avoid classification.
- Daily workflow now reads the latest strategy recommendation snapshot without running it or changing candidate logic.
- SourcePortable remains Python-required; no PyInstaller or NSIS hardening is included in this release.
- No broker connection, no real orders, no automatic trading, no scoring changes, and no backtest math changes.

### Consolidated release-note source detail: `docs/history/RELEASE_NOTES_v3.5.2.md`

Release string: `3.5.2-ui-home-and-strategy-recommender`

This is a UI and research-workflow maintenance release. It does not add trading execution, broker integration, installer hardening, real-data onboarding, or automatic order generation.

#### Highlights

- Added a Home / Overview workbench and grouped sidebar navigation.
- Added a restrained visual system for cards, buttons, badges, tables, and tabular numeric display.
- Added `quantdesk/research/strategy_recommender.py` and `qd strategy-recommend`.
- Added read-only daily workflow integration for `strategy_recommendation_snapshot`.
- Added UI display for latest strategy recommendation output.

#### Strategy Recommendation

Strategy recommendation is research-only. It classifies candidates into styles such as `long_term`, `long_swing`, `board`, `short_swing`, `observe`, and `avoid` using local sector status, symbol features, and risk flags.

It does not connect to brokers, does not place real orders, does not rebalance portfolios, and does not produce buy/sell/order instructions.

#### Packaging

SourcePortable remains Python-required:

- `artifact_type=source_portable`
- `python_required=true`
- `standalone=false`
- `contains_python_runtime=false`
- `contains_quantdesk_exe=false`

PyInstaller and NSIS remain optional external tools. If unavailable, their status manifests must stay honest and skipped rather than faking an executable or setup file.

#### Known Limitations

- Real universe recommendation requires valid local data files.
- Synthetic mode is a demo only.
- No clean Windows installer hardening is included in this release.
- Video tutorials are not bundled.

## v3.5.1.1 - Release Staging Cleanup And Attribution Stress Tests

- Build script now cleans SourcePortable and StandalonePortable stage directories by default after successful packaging.
- Added `-KeepStage` for packaging debug sessions.
- Added `scripts/clean_release_staging.ps1` for historical release staging cleanup.
- Added Brinson attribution stress tests for concentrated portfolios, zero industry weights, non-unit weights, missing industry maps, multiperiod aggregation, and JSON safety.
- No feature behavior changes, no broker connection, and no real orders.

### Consolidated release-note source detail: `docs/history/RELEASE_NOTES_v3.5.1.1.md`

This is a maintenance release for release packaging hygiene and attribution stress coverage.

#### What Changed

- SourcePortable build staging directories are removed by default after successful packaging.
- `build/build_portable_release.ps1` supports `-KeepStage` for packaging debug sessions.
- `scripts/clean_release_staging.ps1` removes historical release staging directories under `dist/`.
- Brinson attribution now has stress tests for concentrated portfolios, zero industry weights, weight normalization, missing industry maps, and multiperiod aggregation.

#### Safety Boundaries

- No new trading features.
- No broker connection.
- No real orders.
- Network remains off by default.
- SourcePortable remains Python-required.

#### Packaging Notes

`QuantDesk_v3.5.1.1_SourcePortable.zip` requires Python 3.10+ and dependencies. It is not a no-Python standalone executable.

`QuantDesk.exe` and `QuantDesk_Setup_v3.5.1.1.exe` are generated only if PyInstaller and NSIS are installed and succeed. If unavailable, build scripts write skipped status manifests instead of fake executables.

#### Known Limitations

- PyInstaller and NSIS are optional external tools.
- Clean Windows machine validation is not included in this maintenance release.
- Video tutorials remain scripts/placeholders unless real mp4 files are added.

## v3.5.0 - Battle Ready Research And Portable Release

- Release string: `3.5.0-battle-ready-research-and-portable-release`.
- Added first-run onboarding via `quantdesk/onboarding.py`, `scripts/first_run_wizard.py`, and `qd.py first-run` / `qd.py wizard`.
- Added portable and installer build scripts, PyInstaller spec, and NSIS installer template with graceful unavailable-tool warnings.
- Added synthetic sample-data manifest and package builder.
- Added v3.5.0 release notes, video scripts, and screenshot checklist.
- No broker connection, no real orders, no default network, no scoring changes, and no backtest return/matching changes.

### Consolidated release-note source detail: `docs/history/RELEASE_NOTES_v3.5.0.md`

Release: `3.5.0-battle-ready-research-and-portable-release`

#### Overview

QuantDesk v3.5.0 packages the research platform into source clean zip and
portable-release workflows. It includes risk attribution, experiment tracking,
PIT universe diagnostics, chart-renderer fallback, package indexes, first-run
wizard, sample-data manifest, and build scripts.

#### Install

Use the portable zip when no installer is available. Run:

```powershell
python scripts\qd.py first-run --json true --summary true
python scripts\qd.py daily --network false --with-guardrails true --summary true
```

#### Boundaries

QuantDesk does not connect to brokers. It has no broker connection, no broker
API, does not place real orders, has no real orders execution path, no
auto-trading, and no investment advice. Network mode is off unless explicitly
enabled.

#### PIT Data

PIT filtering is off by default. Users must provide historical listings data to
use `--pit-universe true`; QuantDesk does not fabricate listing or delisting
history.

#### Partial Daily

`partial_daily` is provisional intraday context only. It never enters backtest,
factor, walk-forward, or historical replay flows.

#### Attribution, Experiments, Charts

Risk attribution is explanatory and not causal proof. Experiment tracking stores
local metadata under `results/experiments/`. Chart rendering uses matplotlib
only when available and may return warnings instead of PNG files.

#### Known Limits

PyInstaller and NSIS are optional external tools. If unavailable, the build
scripts keep the portable zip and installer script as the supported fallback.

#### SHA256

Final hashes are generated in `dist/` during release export/build.

## v3.4.5.1 - Engineering Docs And Package Grouping Lite

- Release string: `3.4.5.1-engineering-docs-and-package-grouping-lite`.
- Added `quantdesk/backtest_loaders.py` and kept `quantdesk.backtest` re-export compatibility for benchmark and industry-map helpers.
- Added `quantdesk/self_check_render.py` for self-check output rendering without changing pass/warn/fail logic.
- Added package index directories for research, intraday, paper, and agent modules.
- Added docs index and quickstart navigation.
- Research safety boundaries are unchanged: no broker connection, no real orders, no default network, no scoring changes, and no backtest return/matching changes.

## v3.4.5.0 - PIT Full And Static Charts

- Release string: `3.4.5.0-pit-full-and-static-charts`.
- Added `quantdesk/chart_renderer.py` for static PNG research charts with matplotlib fallback warnings.
- Extended `quantdesk/point_in_time_universe.py` with tradability checks, suspension-period parsing, PIT filter audit, and PIT vs non-PIT comparison helpers.
- Added explicit `python scripts/qd.py backtest --pit-universe true`; default remains `false`.
- Added historical listings template/validation scripts and `docs/point_in_time_universe.md`.
- No broker connection, no real orders, no PIT data fabrication, no strategy changes, and no backtest matching/return/cost changes.

## v3.4.4.3 - Experiment Tracker Lite

- Release string: `3.4.4.3-experiment-tracker-lite`.
- Added `quantdesk/experiment_tracker.py` for research-only experiment records, stable config hashes, JSON-safe metrics, and `results/experiments/index.csv`.
- Added `scripts/run_experiments.py` and `python scripts/qd.py experiments` for list/filter/top/diff summaries.
- Added daily read-only `experiment_tracker_snapshot` and Markdown section.
- Added non-blocking auto logging for factor evaluation, combined-score backtest, sector rotation, multi-strategy account book, risk attribution, and backtest outputs.
- No broker connection, no real orders, no automatic trading, no strategy changes, and no backtest logic changes.

## v3.4.4.2 - Risk Attribution Lite And Live Polish

- Release string: `3.4.4.2-risk-attribution-lite-and-live-polish`.
- Added `quantdesk/risk_attribution_lite.py` for research-only sub-account contribution, sector exposure, factor support, PIT/data risk, and intraday context attribution.
- Added `scripts/run_risk_attribution_lite.py` and `python scripts/qd.py risk-attribution`.
- Daily workflow now reads the latest risk attribution JSON as a read-only `risk_attribution_snapshot` and never runs attribution automatically.
- Added `python scripts/qd.py live --init` / `scripts/run_intraday_live_workbench.py --init` to create the empty research-only intraday virtual account state template.
- Added `docs/intraday_live_quickstart.md` with Windows commands, foreground `--loop`, Ctrl+C stop guidance, and partial_daily audit boundaries.
- No broker connection, no real orders, no automatic trading, no account-book writeback, no strategy changes, and no backtest logic changes.

## v3.4.4.1 - Intraday Live Workbench And Config Hygiene

- Release string: `3.4.4.1-intraday-live-workbench-and-config-hygiene`.
- Added Intraday Live Workbench modules for provisional spot snapshots, partial daily snapshots, intraday_score, data isolation policy, virtual intraday valuation preview, condition status, and sector heat preview.
- Added `scripts/run_intraday_live_workbench.py` and `python scripts/qd.py live` with `--once`, `--status`, `--refresh`, and foreground `--loop`.
- Daily workflow now reads the latest intraday live JSON as a read-only snapshot and never runs the live command automatically.
- UI adds a read-only Intraday Live Workbench / 盘中工作台 section.
- `partial_daily` is explicitly forbidden from backtest, factor evaluation, walk-forward, and historical replay.
- Added `config/multi_strategy_account_book.example.yaml`.

## v3.4.4.0 - Multi Strategy Account Book

- Release string: `3.4.4.0-multi-strategy-account-book`.
- Added `quantdesk/multi_strategy_account_book.py` for a research-only / paper-only virtual portfolio account book.
- Added YAML-driven sub-account definitions with weights, strategy tags, independent cash, positions, trades, and equity fields.
- Added portfolio-level aggregation, JSON output, Markdown report, CLI summary, and `scripts/run_multi_strategy_account_book.py`.
- Added `python scripts/qd.py multi-strategy-book` and daily read-only `multi_strategy_account_book_snapshot`.
- The daily snapshot is display-only and does not change candidates, trade plans, scoring, guardrails, or recommendations.
- No broker connection, no real orders, no automatic rebalancing, and no changes to existing strategy or backtest logic.

## v3.4.3.8 - True Backtest Assumptions Split And Doc Hygiene

- Release string: `3.4.3.8-true-backtest-assumptions-split-and-doc-hygiene`.
- Truly migrated `default_backtest_assumptions`, `backtest_assumptions_from_config`, and `portfolio_backtest_assumptions_from_config` from `backtest.py` into `quantdesk/backtest_assumptions.py`.
- `backtest.py` keeps import/re-export compatibility while shrinking below 1900 lines.
- Backtest return calculation, default costs, slippage, execution price assumptions, and matching logic are unchanged.
- Fixed architecture inventory drift, ROADMAP next-candidate version drift, expanded `docs/faq.md`, and cleaned root `tmp_factor_out.txt` / `tmp_factor_err.txt`.
- Release proof remains versioned, BOM-free, and verified by `scripts/verify_clean_zip.py`.

## v3.4.3.7 - Backtest Assumptions Split And Release Proof Polish

- Release string: `3.4.3.7-backtest-assumptions-split-and-release-proof-polish`.
- Added `quantdesk/backtest_assumptions.py` for pure backtest assumption text, disclaimer, metadata, risk flags, and Markdown rendering helpers.
- Wired `backtest.py` to reuse the new helpers while preserving existing APIs and keeping return calculation, default costs, slippage, and matching logic unchanged.
- Kept release artifacts versioned and added stricter `RELEASE_ARTIFACT.json` validation.
- `RELEASE_ARTIFACT.json` is now written as UTF-8 without BOM, and `verify_clean_zip.py` fails on BOM release proof files.
- This release is engineering maintainability and release reliability work, not a strategy change.

## v3.4.3.6 - Research Dashboard Daily Snapshot

- Release string: `3.4.3.7-backtest-assumptions-split-and-release-proof-polish`.
- Added `quantdesk/daily_research_snapshot.py` for read-only daily display of `research_dashboard_latest.json`.
- Daily workflow payloads now include `research_dashboard_snapshot`.
- Daily Markdown reports now include a `Research Dashboard Snapshot` section with readiness, module availability, factor health, backtest metrics, sector highlights, PIT status, risk flags, and research next actions.
- Daily CLI summary output exposes research dashboard status, readiness, report link, and `pit_universe`.
- Missing or stale research dashboard payloads produce warnings only and do not change default strategy, scoring weights, backtest matching, or daily trading conclusions.

## v3.4.3.5 - Point-in-Time Universe Lite And Project Hygiene

- Release string: `3.4.3.5-point-in-time-universe-lite-and-project-hygiene`.
- Added Point-in-Time Universe Lite in `quantdesk/point_in_time_universe.py`.
- Added `scripts/show_pit_universe_status.py` and `python scripts/qd.py pit-universe`.
- Added optional `data/universe/historical_listings.csv` schema for list, delist, ST, and suspension metadata.
- Research payloads can surface `pit_universe` status; missing listing metadata remains a warning and does not traceback.
- `factor_evaluation`, `combined_score_backtest`, and `factor_research_dashboard` recognize PIT status without changing default strategy, score weights, or backtest return logic.
- Completed root `test_runtime/` gitignore coverage, refreshed `architecture_inventory.md`, slimmed `README.md`, and added `docs/user_guide.md` / `docs/faq.md`.

## v3.4.3.4 - Factor Research Dashboard

- Release string: `3.4.3.4-factor-research-dashboard`.
- Added `quantdesk/factor_research_dashboard.py` to summarize factor evaluation, factor combine, combined-score research backtest, and sector rotation payloads.
- Added `scripts/run_factor_research_dashboard.py` and `python scripts/qd.py research-dashboard`.
- Added `research_dashboard_latest.json`, timestamped research dashboard JSON, and Markdown research dashboard reports.
- Added missing/stale module checks, module health summaries, key findings, risk flags, and research next actions.
- Preserved safety boundaries: no broker API, no real orders, no default network, no default strategy changes, no scoring-weight changes, no screen/backtest/daily trading-conclusion changes, and no changes to existing backtest matching logic.

## v3.4.3.3 - Sector Rotation Engine

- Release string: `3.4.3.3-sector-rotation-engine`.
- Added `quantdesk/sector_rotation_engine.py` for research-only sector strength, relative-strength, breadth, rank-change, and heat-score diagnostics.
- Added `scripts/run_sector_rotation.py` and `python scripts/qd.py sector-rotation`.
- Added `sector_rotation_latest.json`, timestamped sector-rotation JSON, and Markdown sector-rotation reports.
- Supported sector relative strength versus equal-weight universe benchmark, rank improvement, breadth metrics, top improving sectors, weakening sectors, and auditable heat status.
- Preserved safety boundaries: no broker API, no real orders, no default network, no default strategy changes, no scoring-weight changes, no screen/backtest/daily trading-conclusion changes, and no changes to existing backtest matching logic.

## v3.4.3.2 - Combined Score Research Backtest

- Release string: `3.4.3.2-combined-score-research-backtest`.
- Added `quantdesk/combined_score_backtest.py` for research-only combined-score ranking backtests.
- Added `scripts/run_combined_score_backtest.py` and `python scripts/qd.py combined-score-backtest`.
- Added `combined_score_backtest_latest.json`, timestamped backtest JSON, and Markdown combined-score backtest reports.
- Supported Top-N, Top-Quantile, and Long-Short Quantile research portfolios with execution lag, transaction costs, slippage, turnover, drawdown, and return diagnostics.
- Preserved safety boundaries: no broker API, no real orders, no default network, no default strategy changes, no scoring-weight changes, no screen/backtest/daily integration, and no changes to existing backtest matching logic.

## v3.4.3.1 - Factor Combiner And Neutralized Score

- Release string: `3.4.3.1-factor-combiner-and-neutralized-score`.
- Added `quantdesk/factor_combiner.py` for research-only multi-factor score construction.
- Added `scripts/build_combined_score.py` and `python scripts/qd.py factor-combine`.
- Added `combined_score_latest.json`, timestamped combined-score JSON, and Markdown combined-score reports.
- Supported cross-sectional winsorize, z-score, percentile rank, industry neutralization, market-cap neutralization, optional Gram-Schmidt orthogonalization, and factor-health IC weighting.
- Preserved safety boundaries: no broker API, no real orders, no default network, no strategy signal changes, no default scoring-weight changes, and no backtest return or matching changes.

## v3.4.3.0 - Factor Evaluation Platform

- Release string: `3.4.3.0-factor-evaluation-platform`.
- Added an offline factor evaluation platform for research diagnostics only.
- Added momentum, volatility, and liquidity factor modules with stable DataFrame contracts.
- Added `factor_health_latest.json`, timestamped factor-health JSON, and Markdown factor evaluation reports.
- Added IC, Rank IC, ICIR, IC decay, quantile return, long-short return, coverage, and factor health diagnostics.
- Added `scripts/run_factor_evaluation.py` and `python scripts/qd.py factor-eval`.
- Preserved safety boundaries: no broker API, no real orders, no investment advice, no default network, no strategy signal changes, no scoring-weight changes, and no backtest return or matching changes.

## v3.4.2.7 - P3 Engineering Cleanup Replay Performance And Market Calendar

- Release string: `3.4.2.7-p3-engineering-cleanup-replay-performance-and-market-calendar`.
- Reworked historical intraday replay to preload local daily CSV histories once and use in-memory DataFrame slices capped at each `signal_date`.
- Removed the old per-signal-date temporary CSV directory path from the replay engine while preserving output structure and walk-forward anti-look-ahead checks.
- Stopped market-clock calendar loading from silently using hard-coded holiday fallback data; missing CSV/YAML now produces an explicit degraded warning.
- Added market-calendar source visibility to `self_check` so CSV, legacy YAML, and unavailable states are clear.
- Confirmed clean zip and release zip files are generated artifacts, ignored by Git, and excluded from clean exports.
- Safety boundaries stay unchanged: no broker API, no real orders, no investment advice, no default network, no default LLM, no strategy, scoring, signal, matching, or backtest-return changes.

## v3.4.2.5 - CLI Summary Output And Operator Readability

- Release string: `3.4.2.5-cli-summary-output-and-operator-readability`.
- Added `quantdesk/cli_summary.py` as a shared compact summary contract for operator-readable command output.
- Added explicit `--summary true` support for data-health, data-bootstrap, screen, backtest, market-intel, walk-forward, and daily while preserving full latest JSON artifacts.
- Kept `--json true` backward-compatible: it still prints the complete payload unless `--summary true` is explicitly requested.
- Updated qd.py help, self_check, README, AGENTS, roadmap, and schema docs with summary-mode guidance.
- Safety boundaries stay unchanged: no broker API, no real orders, no investment advice, no default network, no default LLM, no strategy, scoring, or backtest-return changes.

## v3.4.2.4 - Windows Python Env And Command Consistency

- Added `python scripts/qd.py env-doctor --json true` and `scripts/check_python_env.py` for local Python executable, PATH `python`, Windows `py` launcher, encoding, UTF-8 mode, and working-directory diagnostics.
- Updated Windows helper `.cmd` scripts to prefer `QUANTDESK_PYTHON`, then `.venv`, then `python`, with a clear conda/launcher setup hint when Python is unavailable.
- Clarified README and AGENTS guidance that `python` is a placeholder command; users may run `python`, `py`, or a full `python.exe` path without making any local path mandatory.
- Extended `self_check.py` with Python launcher/environment checks. Missing PATH `python` is a WARN, not a FAIL, when the active interpreter is valid.
- Safety boundaries stay unchanged: no broker API, no real orders, no investment advice, no default network, no default LLM, no strategy, scoring, or backtest-return changes.

## v3.4.2.3 - CLI Contract First Run Quant Loop Closure

- Added `python scripts/qd.py build-features` and pointed `qd.py backtest` at a real local backtest CLI instead of a placeholder.
- Made recommended `qd.py` commands executable: `status --json true`, `fix-plan --json true`, and `walk-forward --network false` now keep the JSON/CLI contract.
- Backtest now reads local `data/daily/{symbol}.csv` only, writes `results/backtest/backtest_latest.json` and `reports/backtest_report_latest.md`, and returns `warning/no_daily_data` when local daily data is absent.
- Screen now returns a structured `missing_features` warning when `data/features/daily_features_latest.csv` is absent, and writes stable latest JSON/report paths when features exist.
- Unified default target-date resolution around the latest complete A-share trading day, so Saturday `2026-05-23` resolves to `2026-05-22` unless the user explicitly overrides it.
- Marked offline `data-bootstrap` as universe-only with `daily_ready=false`, `bootstrap_mode=offline_universe_only`, and `next_required_step=network_data_update_or_manual_import`.
- Deduplicated operation logging when `qd.py` wraps child scripts by setting `QUANTDESK_WRAPPED_BY_QD=1`.
- Safety boundaries stay unchanged: no broker API, no real orders, no investment advice, no default network, no default LLM, no strategy or scoring changes.

## v3.4.2.2 - Clean Zip Bootstrap Calendar Unification And Warning Transparency

- Added `data-bootstrap` as the first-run entrypoint for a tiny demo universe and optional explicit network daily-data fetch.
- Made clean-zip `status` / `fix-plan` prioritize bootstrap before daily workflows when no universe or local data exists.
- Unified the trading-calendar default on `data/calendar/a_share_trading_calendar.csv` with YAML as legacy fallback only.
- Improved warning transparency for empty universes, no data, invalid walk-forward holding dates, signal-history corruption, and theme-map provider failures.
- Kept runtime state out of clean zip exports, including results, reports, daily/intraday data, features, logs, state, dist, and zip files.

## v3.4.2.1 - Dependency Config Provider Hygiene And Paper Lock

- Added `PyYAML>=6,<7` to `requirements.txt` and moved optional `akshare` / `mootdx` provider stacks to `requirements-data.txt`.
- Replaced the implicit `requests` dependency in Sina quote fallback with `urllib.request`.
- Updated intraday provider User-Agent construction to use `PACKAGE_VERSION`.
- Pointed `config/default.yaml` at `data/calendar/a_share_trading_calendar.csv`.
- Added an advisory file lock around automatic paper-portfolio state writes.
- Added `data_sufficient` / `data_warning` score-component metadata for all-NaN indicator rows without changing score values.
- Kept planned providers visible as gray `planned` / `placeholder=true` diagnostics rather than real failures.

## v3.4.2 - Dashboard State Clarity Command Safety And Market Intel Report Polish

- Shortened home-card symbol previews to 3-5 examples and moved complete lists to JSON.
- Added `analysis_pool_count` and `truncation_reason` so the UI can say “本次检查 80 / 当前分析池 120” instead of implying 80 is the whole pool.
- Split the sidebar into current data status, latest market observation, and latest daily review, with stale daily reports called out separately.
- Made stale-data remediation safer: online daily-data repair uses `--enable-network`, while offline `data-update --network false` is labeled as a check that will not fill data.
- Improved the market observation card/page/report with top sectors, top candidates, board paper-observation counts, data gaps, candidate reasons, and risk flags.
- Kept all market-intel output as paper observation and learning research only.

## v3.4.1 - Market Intel Scope Sync Candidate Explainability And Dashboard Polish

- Added shared scope policy fields to `data-update`, `data-health`, and `market-intel`: `data_scope`, `scope_label`, `scope_source`, `requested_count`, `selected_count`, `max_symbols`, `truncated`, `symbols_preview`, and `symbols_omitted_count`.
- Improved workbench mismatch detection when `data-update` / `data-health` / `market-intel` use different stock ranges, with resync commands for `current_analysis_pool`.
- Expanded the home and market observation UI with hot sector names, candidate rows, reasons, risk flags, minute-line warnings, report paths, and copyable market-intel commands.
- Kept long symbols lists summarized in UI cards while preserving full lists in JSON.
- Enhanced market-intel remediation for missing sector mapping, empty candidates, missing minute data, local-sample scope, and offline news skipping.
- Updated market-intel Markdown reports with data scope, top sectors, candidate explanations, data gaps, minute-line state, scenario-not-prediction language, and non-investment-advice disclaimers.

## v3.3.7 - Data Update Output Slim And Mobile Visual Check

- Changed
  - `python scripts/qd.py data-update` and `python scripts/qd.py data-health` now print short daily-use summaries by default instead of long symbol lists or full JSON.
  - Full details stay in `results/data_update/data_update_latest.json` and `results/data_health/data_health_dashboard_latest.json`; use `--json true` or `--verbose true` to inspect complete payloads.
  - Long `symbols` lists are summarized in CLI and UI, while copy buttons still keep the full command payload.
  - Home, command center, artifact center, and remediation command blocks gained extra mobile/narrow-screen overflow guards.
- Safety
  - No strategy, scoring formula, backtest return logic, broker integration, trading action, default network behavior, or LLM default changed. All flow suggestions remain non-investment advice.

## v3.3.6 - Remediation Ui Copy And Artifact Preview Polish

- Changed
  - 首页、命令中心和产物中心统一使用“我现在该怎么修 / 修复建议 / 重新生成 / 修复命令”的用户语言。
  - 修复命令按“离线可运行，不会联网”和“需要显式联网授权”分组显示；UI 只展示命令，不自动执行。
  - 复制命令、复制后续命令、复制全部修复命令走统一前端 helper，长命令默认折叠且复制完整命令。
  - 产物中心 JSON 预览优先展示摘要字段，Markdown 预览默认显示前 80 行，并支持复制路径/摘要。
- Safety
  - 本轮不新增策略、不改评分公式、不改回测收益逻辑、不接券商、不真实下单；所有修复建议仍只是流程建议，不构成投资建议。

## v3.3.5 - Command Output Error Actionability And Daily Remediation Flow

- Added
  - `quantdesk/remediation_advisor.py` converts stale data, missing intraday, guardrail, schema, provider, and artifact warnings into copyable `qd.py` remediation actions.
  - `python scripts/qd.py fix-plan` prints a read-only repair plan and writes `results/fix_plan_latest.json`.
- Changed
  - `daily`, `status`, `command-center`, data update, data health, and the artifact center now surface remediation commands without running them.
  - `data-update` and `data-health` accept `--scope current_analysis_pool` as a safe scoped command label; defaults remain offline and never scan all A shares.
- Safety
  - Remediation guidance is workflow-only and is not investment advice, trading advice, parameter optimization, or a broker integration.

## v3.3.4 - Daily Output Summary And Artifact Center Followthrough

- Changed
  - `python scripts/qd.py daily` now prints a short human-readable summary by default instead of dumping the full workflow JSON to stdout.
  - Full JSON remains available with `--json true` or `--verbose true`, and is always written to `results/daily_workflow_latest.json`.
  - Daily Markdown output is standardized at `reports/daily_quant_report_latest.md` while timestamped workflow reports are still kept.
- Added
  - `quantdesk/daily_output_summary.py` builds `daily_console_summary`, artifact paths, guardrail text, and next actions.
  - Daily artifacts now expose `daily_report`, `daily_result`, `data_health`, `guardrails`, and `command_center`.
  - `status` and command-center read recent daily status, warning/failed counts, and report/result paths.
  - Artifact center has a “每日流程” group for daily/status/command-center outputs.
- Safety / Boundary
  - Warning-only guardrails remain governance checks. They do not change strategy parameters, scoring formulas, backtest return logic, network defaults, LLM defaults, broker boundaries, or trading behavior.

## v3.3.3 - Entrypoint Consolidation Status And Daily Guardrails

- Added
  - `python scripts/qd.py status` for a concise local status summary and at most 3 next actions. It is read-only: no network, no LLM, no strategy execution, no trading action.
  - `quantdesk/status_advisor.py` writes `results/status_latest.json` and `reports/status_report_latest.md`.
  - `quantdesk/time_utils.py` centralizes common timestamp helpers and recent `_now/_now_text` copies were removed.
  - `docs/script_entrypoint_inventory.md` and `docs/daily_workflow_guardrails.md`.
- Changed
  - `qd.py help` now starts with “不知道该跑什么？先运行 python scripts/qd.py status” and groups commands into beginner, validation, maintenance, and advanced sections.
  - `qd.py daily` accepts `--with-guardrails true|false`; quick guardrails are enabled by default and append `guardrail_summary` without changing strategy parameters, scoring, or backtest return logic.
  - Command center includes the new `status` command in local operation summaries.
- Preserved
  - The five legacy/advanced bare scripts remain for compatibility where old UI routes and tests still import them, but they are no longer recommended entrypoints.

## v3.3.2.5 - Workbench State Consistency Schema Noise And UI Polish

- Fixed
  - 今日工作台不再把指定股票、低价学习池、data-update 和 data-health 的日期口径混在一起；新增 `data_scope`、`coverage_date`、`latest_date_min`、`latest_date_max`、`symbols_checked`、`updated_symbols`、`stale_symbols`、`missing_symbols` 等只读字段。
  - 首页会说明“部分股票已更新但全池仍 stale”不是数据回退。
  - schema-check 将高风险 `confirmed_secret_leak` 与低风险 `secret_like_reference` 分开，避免把 `provider_secrets.json`、`llm_config.json`、placeholder 和 masked value 当成确认泄露。
- Changed
  - 产物中心按模块分组，missing 显示为灰色“未生成”，提供复制路径和 JSON/Markdown 预览。
  - 新手模式隐藏高级入口，长命令默认缩短展示并提供“复制完整命令”。
- Safety / Boundary
  - 仍然不接券商、不真实下单、不默认联网、不默认扫描全 A、不调用 LLM、不改策略、评分公式或回测收益逻辑。

## v3.3.2.4 - Workbench Empty State Warning Severity And Actionability Fix

- Changed
  - 今日工作台 status cards now render Chinese metric labels instead of raw field keys such as `target_complete_trade_date`.
  - Status labels are shown as user-facing text such as “已通过 / 提示 / 未生成 / 已跳过” while the raw payload status remains unchanged.
  - “下一步建议” is now presented as “我现在该做什么”.
  - Next actions include lightweight categories (`data`, `validation`, `review`, `system`) so warnings can be visually separated by meaning.
- Safety / Boundary
  - UI-only/read-only payload refinement. No strategy, score, return logic, provider protocol, default network, LLM, broker, or trading behavior changed.

## v3.3.2.3 - Workbench Refresh Command Center And Target Date Semantics Fix

- Fixed
  - Explicit `data-update --target-date YYYY-MM-DD` is now recorded as both `target_complete_trade_date` and `effective_target_date`; automatic complete-trading-day inference only applies when no target date is supplied.
  - 今日工作台 can refresh local status through a read-only button that only re-reads local workbench GET APIs.
  - 命令中心 uses a readable table layout, so commands such as `dev-preflight` are no longer squeezed into vertical text.
  - 首页状态卡片 normalize missing metric values to friendly empty-state text instead of showing raw `null`.
- Changed
  - Workbench data-health aggregation reads nested `freshness` / `intraday` payloads from current data-health results.
  - Next actions now distinguish missing intraday data from stale daily data, and prompt users to rerun validation/report steps when local data status is newer than existing validation outputs.
- Safety / Boundary
  - The refresh button is read-only: no provider calls, no workflow execution, no LLM calls, no trading actions.

## v3.3.2.2 - CI Stability Repo Hygiene And Calendar Cache

- Fixed
  - Advanced version checks now include `3.3.2.2`, and old whitelist tests no longer block the current patch version.
  - `self_check` capability checks for data freshness, shared candidate contracts, and intraday 1m closure no longer depend on historical comments in `quantdesk/version.py`.
  - `position_management_engine._normalize_intraday_frame` no longer contains unreachable legacy normalization code after its early return.
  - Planned provider placeholders now report `status=planned` / `placeholder=true` and are skipped by provider failure statistics instead of being shown as real failures.
- Changed
  - `trading_calendar` caches loaded calendar CSVs by path and exposes `clear_calendar_cache()` for tests.
  - `pyproject.toml` now declares core runtime dependencies including pandas and numpy, while pytest and mootdx remain optional/dev dependencies.
  - Root verification artifacts and temp directories are ignored and removed from the working tree hygiene set.
- Preserved
  - v3.3.2.1 `data-update` network flag handling, dry-run default, console summary, and UTF-8 output behavior remain covered by regression tests.

## v3.3.2.1 - Data Update Network Flag Console And Encoding Fix

- Fixed
  - `qd.py data-update --network true` and `--enable-network` now preserve the explicit network intent through the CLI wrapper and data-update parser.
  - `--no-network` takes precedence when conflicting network flags are provided, and the result warnings explain the conflict.
  - `data-update` now keeps `dry_run=false` unless `--dry-run true` is explicitly passed.
  - Console output always prints a Chinese summary including target date, latest local daily date, network/update_allowed/dry_run, counts, provider chain, result path, report path, and next action.
  - Data-update JSON and Markdown outputs remain UTF-8 with `ensure_ascii=False`; troubleshooting now documents `Get-Content ... -Encoding UTF8` for Windows PowerShell.
- Safety / Boundary
  - The default remains `network=false`; explicit network only updates user-specified symbols or the low-price learning pool, never full A by default.

## v3.3.1 - QuantDesk Workbench UI And Workflow Unification

- Added
  - Read-only workbench APIs: `/api/workbench/overview`, `/api/workbench/command-center`, `/api/workbench/artifacts`, and `/api/workbench/next-actions`.
  - `quantdesk/workbench_overview.py` for local status aggregation without running tasks.
  - UI sections for 今日工作台, 产物中心, independent 命令中心, and beginner/expert mode state.
- Changed
  - Full command center is no longer part of the default home overview; it has its own navigation entry.
  - Home page now emphasizes six status cards, a linear daily workflow, and data/process next actions.
  - Clean zip empty states are treated as normal missing/warning states.
- Fixed
  - Result files are easier to locate via the artifact center instead of hunting through `results/` and `reports/`.
- Safety / Boundary
  - Workbench APIs are GET-only and read-only; they do not connect to providers, call LLMs, place orders, or generate advice.
- Validation
  - Tests cover workbench API registration, missing-results behavior, artifact grouping, localStorage keys, command-center independence, status classes, frontend syntax, and clean zip exclusions.

## v3.3.0 - Consolidation And Schema Contracts

- Added
  - `quantdesk/schema_contracts.py` for lightweight common result payload helpers.
  - `quantdesk/result_payload_validator.py` and `qd.py schema-check` for local schema/status/warnings/disclaimer/secret-leak checks.
  - `docs/schema_contracts.md` and small JSON examples under `docs/schema_examples/`.
- Changed
  - `command-center`, `archive-results`, `qd.py help`, and operation logging can surface `schema-check`.
  - Core diagnostics now have a documented common contract for gradual migration without removing old fields.
- Fixed
  - Missing runtime result files in clean zip environments are reported as `missing` / `warning`, not program failures.
- Safety / Boundary
  - `schema-check` is read-only, defaults to `network=false`, does not call LLMs, and does not read secret config files.
- Validation
  - Tests cover status normalization, missing schema version, illegal status, warnings shape, secret masking, CLI no-network behavior, command-center/archive integration, docs, examples, and clean zip exclusions.

## v3.2.9 - Parameter Sensitivity And Robustness Audit

- Added
  - `parameter_sensitivity_audit` for bounded parameter perturbation diagnostics and robustness scoring.
  - `qd.py param-sensitivity` and `scripts/run_parameter_sensitivity_audit.py`.
  - `config/parameter_sensitivity.example.json` and `docs/parameter_sensitivity_audit.md`.
- Changed
  - Daily retention, observation context, evidence pack, report audit, data-health, command-center, archive-results, and UI can surface a compact parameter sensitivity summary.
- Fixed
  - Parameter robustness is reported as a conservative diagnostic with `narrow_peak`, `parameter_cliff`, insufficient-sample, and drawdown-sensitivity warnings rather than as an optimization result.
- Safety / Boundary
  - No strategy, scoring formula, backtest return logic, or default strategy parameter is changed. No best-parameter recommendation is emitted.
- Validation
  - Tests cover grid limits, seed reproducibility, overfit warning detection, CLI no-network behavior, manifest/context/archive/command-center wiring, reports, and clean zip exclusions.

## v3.2.8 - Data Health Dashboard And Provider Failure Stats

- Added
  - `data-health` CLI for local daily freshness, intraday coverage, provider failure stats, and data_grade trend.
  - `data_freshness_summary`, `intraday_coverage_dashboard`, `provider_failure_stats`, and `data_grade_trend`.
  - `docs/data_health_dashboard.md`.
- Changed
  - Daily retention, observation context, evidence pack, audit, command-center, archive-results, and UI can surface data-health summaries.
- Fixed
  - `network_disabled` is reported as offline skipped rather than a real provider failure.
- Safety / Boundary
  - No provider request is made by default; even `network=true` on this command is reserved for future compatibility and does not fetch data in v3.2.8.
- Validation
  - Tests cover missing local daily/intraday data, provider failed vs skipped, no-network CLI behavior, archive plans, command-center status, and clean zip exclusions.

## v3.2.7 - Governance Command Center And Doc Consolidation

- Added
  - `operation_log` for local command timelines under `results/operation_log/`.
  - `archive-results` for dated local result/report archiving under `results/archive/{target_date}/`.
  - `command-center` for reading recent command status without rerunning tasks.
  - Troubleshooting quick reference and architecture overview docs.
- Changed
  - README now starts with quick start, recommended commands, and `qd.py` as the single recommended entrypoint.
  - `qd.py` supports `--no-network`, `--enable-network`, `--no-llm`, and `--enable-llm` aliases while keeping old flags.
- Fixed
  - Results are easier to locate and archive without changing strategy logic.
- Safety / Boundary
  - Operation logs sanitize secrets and never store API keys or provider secret contents.
  - Command center reads local files only; it does not run providers or LLMs.
- Validation
  - New governance tests cover operation log, archive dry-run, command center, docs, and clean zip exclusions.

## v3.2.6.1 - Feature Audit Empty Data Graceful Warning

- Added
  - Explicit empty-data `skipped_reason` and `suggested_next_action` in feature audit payloads.
- Changed
  - `feature-audit` in clean zip environments with no `data/daily/` exits `0` with `status=warning`.
- Fixed
  - Empty local data is no longer treated as a program failure.
- Safety / Boundary
  - No data is synthesized; no network request is made.
- Validation
  - Tests cover empty data, warning status, and exit code `0`.

## v3.2.6 - Backtest Statistical Validation And Feature Quality Audit

- Added
  - `feature_quality_audit` for missing rates, NaN/Inf counts, zero rates, and coverage.
  - `backtest_statistical_validation` for permutation tests and bootstrap confidence intervals.
- Changed
  - Daily retention and observation context can include feature/statistical summaries.
- Fixed
  - Reports now state insufficient sample or weak random-baseline evidence conservatively.
- Safety / Boundary
  - No strategy, scoring, parameter, or return-logic change.
- Validation
  - Tests cover reproducible permutation tests and sample-size warnings.

## v3.2.5 - Paper Vs Backtest Consistency Check

- Added
  - `paper_vs_backtest_consistency` and `qd.py consistency-check`.
- Changed
  - Observation context surfaces consistency risks.
- Fixed
  - `no_fill`, `missing_intraday`, reliable-event evidence, and board-candidate risk are checked together.
- Safety / Boundary
  - Diagnostic only; no paper portfolio mutation.
- Validation
  - Tests cover consistency warnings and clean zip exclusions.

## v3.2.4 - Daily Data Retention And Reproducibility Manifest

- Added
  - `daily-retain` workflow and reproducibility manifest.
  - `manifest-check` CLI.
- Changed
  - Manifest records versions, symbols, network flags, provider trace, validation summary, paths, and hashes.
- Fixed
  - Runtime daily records stay out of clean zip.
- Safety / Boundary
  - Manifest does not store API keys, provider secrets, or real account information.
- Validation
  - Tests cover default offline behavior and manifest comparison.

## v3.2.3 - Strategy Lookahead And Execution Assumption Guard

- Added
  - Lookahead guard, execution assumption guard, and `strategy-validate`.
- Changed
  - Observation reports include validation summaries.
- Fixed
  - Daily high/low/close intraday proxy risks and missing-minute fills are explicitly flagged.
- Safety / Boundary
  - No optimization and no new strategy.
- Validation
  - Tests cover future-date leakage and execution assumption risks.

## v3.2.2 - Core Correctness And Doc Sync

- Added
  - Local A-share trading calendar CSV.
  - Trading calendar builder script.
- Changed
  - Python package version is dynamic from `quantdesk.version.PACKAGE_VERSION`.
- Fixed
  - mootdx duplicate skeletons removed.
  - Board-candidate paper execution assumption documented.
- Safety / Boundary
  - Provider failure does not fabricate data.
- Validation
  - Tests cover calendar coverage, pyproject versioning, mootdx definitions, and docs.

## v3.2.1 - Agent Evidence Guard And Report Quality Audit

- Added
  - Evidence pack, hallucination guard, and report audit.
- Changed
  - LLM/offline reports carry hashes and grounding metadata.
- Fixed
  - Unsupported numbers, dates, symbols, provider claims, and trading claims are flagged.
- Safety / Boundary
  - Agent remains read-only and cannot modify strategy/provider/portfolio state.
- Validation
  - Tests cover grounding, audit output, and clean zip exclusions.

## v3.2.0 - Optional LLM Observation Agent

- Added
  - Optional offline-stub/OpenAI-compatible/Ollama provider layer.
  - `llm-smoke` and `observation-report --llm`.
- Changed
  - Missing config or API key falls back to offline template.
- Fixed
  - LLM output goes through policy guard.
- Safety / Boundary
  - LLM disabled by default; no SDK hard dependency.
- Validation
  - Tests mock all provider calls and require no API key.

## v3.1.x - Real Providers And Observation Contract

- Added
  - Tencent provider entrypoint, provider smoke, optional mootdx provider, and strategy observation contract.
- Changed
  - `qd.py` became the preferred unified entrypoint for provider and observation tasks.
- Fixed
  - Provider failures are explicit and fallback behavior is observable.
- Safety / Boundary
  - `network=false` returns skipped; no fake provider success.
- Validation
  - Provider and observation regression tests remain local/mock-based.

## v3.0.x - Provider Matrix And Chanlun Learning

- Added
  - A-stock data provider matrix and adapter layer.
  - Chanlun knowledge, fractal, bi, and segment learning modules.
- Changed
  - Provider capabilities are documented as matrix entries instead of ad hoc calls.
- Fixed
  - Chanlun outputs are framed as learning observation signals.
- Safety / Boundary
  - No Chanlun buy/sell advice, no broker connection, no default network.
- Validation
  - Tests cover provider matrix and Chanlun engines.

## Legacy v2.x

v2.x built the local report UI, data health, local daily cache, low-price learning pool, paper simulation, intraday import/replay, and foundational safety language. Details are kept in historical docs and tests; current day-to-day usage should start from `python scripts/qd.py help`.
## v3.3.2-data-update-workbench-and-auto-refresh-preflight

- 新增 `quantdesk/data_update_orchestrator.py`，提供离线数据更新预检、显式联网补日线、结果 JSON/Markdown 输出。
- 新增 `python scripts/qd.py data-update` 和 `python scripts/qd.py dev-preflight`；默认 `network=false`，不联网、不跑策略、不调用 LLM。
- UI 左侧栏新增“数据更新”一级入口，今日工作台流程加入“更新 / 检查数据”。
- Workbench API 新增 `/api/workbench/data-update`，GET 只读，不触发更新。
- daily-retain manifest 与 observation context 增加 `stale_data_warning`。
- command-center、archive-results、产物中心接入 data-update 结果。
# v3.4.0-market-intelligence-sector-low-price-board-watch-workbench

- 新增 `qd.py market-intel`：默认离线、默认不调用 LLM、默认不扫描全 A。
- 新增市场环境、板块/题材热点、消息面 skipped/offline、板块内低价纸面观察候选、情景观察曲线模块。
- 新增市场观察 UI 页面和首页/命令中心/产物中心接入。
- `archive-results` 与 `schema-check` 已纳入 market_intelligence latest JSON 和报告。
- 情景曲线明确标记 `scenario_observation_not_prediction`，不是价格预测。
- 所有市场观察报告均声明不构成投资建议，不接券商、不真实下单、不编造新闻或板块热度。

# v3.5.1-truthful-attribution-installer-and-structure-hardening

- Added true Brinson-Fachler attribution via `qd attribution`, including allocation, selection, interaction, and Lite linear factor contribution.
- Renamed the old five-dimensional diagnostics path to risk observation; `qd risk-attribution` remains only a compatibility alias.
- Renamed Python-required portable release to SourcePortable and added honest PyInstaller/NSIS status manifests.
- Migrated real modules into `quantdesk.research`, `quantdesk.paper`, `quantdesk.intraday`, and `quantdesk.agent` while preserving selected legacy import compatibility.
- Slimmed `backtest.py` below the v3.5.1 hard limit without changing return, matching, cost, or slippage logic.
# v3.7.0.2 Strategy Pipeline Repair

- Added degraded PIT `price_coverage` mode for viability screening.
- Added CSV schema normalization and anomaly reports.
- Added manual `factor-combine --weights`.
- Added `combined-score-backtest --pit-mode`.
- Added benchmark and board-aware limit-rate diagnostics.
## v3.7.0.3 - Low Price Pipeline Finalization

- Added offline runtime historical listings build/validate commands for degraded price CSV coverage.
- Added low-price strategy viability profile with signal-date price filtering and 100-share lot constraints.
- Kept outputs research-only with survivorship, ST, benchmark, and cash-drag warnings.
# v3.7.0.6 - Strategy Regression Guard and Report Hygiene

- Added strategy regression audit for baseline distinctness, capital-lot grid completeness, metrics sanity, report provenance, and import rewrite impact.
- Added smoke-output separation through `--write-latest` and `--smoke-run`.
- Preserved v3.7.0.5 import alias reconciliation without restoring deprecated root modules.

### Consolidated release-note source detail: `docs/history/RELEASE_NOTES_v3.7.0.3.md`

QuantDesk v3.7.0.3 finalizes the research-only low-price strategy pipeline.

- Builds runtime `data/universe/historical_listings.csv` from local price CSV coverage without network access.
- Treats generated listings as degraded `price_csv_coverage`, not official listing, delisting, or historical ST data.
- Uses `list_date = first available local price date`; this is not the official listing date.
- Adds the `low_price` strategy viability profile: `initial_cash=10000`, `max_positions=10`, monthly rebalance, `top_n=10`, `lot_size=100`, and signal-date `close <= 15` filtering.
- Keeps `factor-combine --weights` intact and reuses the existing combined-score path.
- Reports benchmark availability, degraded PIT warnings, survivorship-bias risk, ST limitations, lot-size unfilled positions, and cash drag.
- Remains research-only: no broker, no real orders, no automatic trading, no AI Advisor, and no default network.

Runtime data, including `data/universe/historical_listings.csv`, is excluded from clean release zips.



## v3.7.0.2 - Strategy Pipeline Repair


### Consolidated release-note source detail: `docs/history/RELEASE_NOTES_v3.7.0.2.md`

This is a narrow repair release for the research-only strategy validation pipeline.

- Adds `pit_mode=strict|price_coverage|off`; strategy viability defaults to degraded `price_coverage` when `historical_listings.csv` is missing.
- Adds load-time CSV schema normalization and schema anomaly reporting without overwriting source CSV files.
- Adds `factor-combine --weights` for simple manual factor experiments.
- Adds `combined-score-backtest --pit-mode price_coverage` with explicit survivorship-bias warnings.
- Adds local benchmark discovery for CSI 300 / CSI 500 / CSI 1000 when files are present.
- Adds board-aware limit-rate helpers for ST, main board, ChiNext, STAR, and guarded unknown-board cases.

No broker integration, no real orders, no automatic trading, no AI Advisor, and no default network access are introduced.


## v3.5.1 - Truthful Attribution Installer And Structure Hardening


### Consolidated release-note source detail: `docs/history/RELEASE_NOTES_v3.5.1.md`

#### What changed since v3.5.0

v3.5.1 closes the main release honesty gaps from v3.5.0: true Brinson attribution is now separate from observation diagnostics, package names describe what is actually bundled, and module grouping now contains real migrated modules.

#### Release honesty fixes

- SourcePortable is named `QuantDesk_v3.5.1_SourcePortable.zip` and requires Python 3.10+.
- `QuantDesk.exe` is generated only if PyInstaller is available and succeeds.
- `QuantDesk_Setup_v3.5.1.exe` is generated only if NSIS `makensis` is available and succeeds.
- Build scripts write `build_pyinstaller_status.json` and `build_installer_status.json` instead of faking success.
- Video tutorials are scripts/placeholders unless real mp4 files exist.

#### True Brinson attribution

`qd attribution` runs true research-only Brinson-Fachler attribution with allocation, selection, and interaction effects. It can also run a synthetic known-answer demo with `--synthetic-test-data true`.

#### Risk observation compatibility

`qd risk-observation` runs the old five-dimensional diagnostics. `qd risk-attribution` remains a compatibility alias for observation diagnostics and clearly says it is not Brinson attribution.

#### Packaging modes

- Source clean zip: `quantdesk_clean_v3.5.1-truthful-attribution-installer-and-structure-hardening.zip`
- SourcePortable: `QuantDesk_v3.5.1_SourcePortable.zip`
- Standalone portable: `QuantDesk_v3.5.1_StandalonePortable.zip`, only if PyInstaller succeeds.
- Installer: `QuantDesk_Setup_v3.5.1.exe`, only if NSIS succeeds and `QuantDesk.exe` exists.

#### Known limitations

- SourcePortable requires Python 3.10+ and dependencies; it is not a no-Python standalone executable.
- QuantDesk.exe is generated only if PyInstaller is available and succeeds.
- QuantDesk_Setup_v3.5.1.exe is generated only if NSIS makensis is available and succeeds.
- If PyInstaller/NSIS are unavailable, build scripts produce warning manifests, not fake exe files.
- Video tutorials are currently scripts/placeholders unless real mp4 files exist.
- `qd risk-observation` is diagnostics; `qd attribution` is true Brinson/factor attribution.
- PIT historical listings require user-provided or explicitly built data; default remains off.
- Network remains false by default.
- No broker, no real orders.
- No clean Windows machine validation is claimed for this release.

#### Upgrade notes

Use `qd attribution --synthetic-test-data true --summary true` to verify true attribution locally. Existing observation workflows can keep using `qd risk-attribution`, but new docs recommend `qd risk-observation` for clarity.

#### Safety boundaries

QuantDesk remains research-only. It does not connect to brokers, place real orders, auto trade, auto rebalance, or provide investment advice. Partial intraday data remains excluded from backtest/factor/walk-forward research.

#### Artifact list

- `dist/quantdesk_clean_v3.5.1-truthful-attribution-installer-and-structure-hardening.zip`
- `dist/QuantDesk_v3.5.1_SourcePortable.zip`
- `dist/build_pyinstaller_status.json`
- `dist/build_installer_status.json`
- Optional: `dist/QuantDesk.exe`
- Optional: `dist/QuantDesk_v3.5.1_StandalonePortable.zip`
- Optional: `dist/QuantDesk_Setup_v3.5.1.exe`

#### SHA256 checklist

SHA256 files are generated in `dist/` during export/build. Verify them before upload and do not upload `QuantDesk_v3.5.1_Portable.zip` unless a standalone manifest proves it contains an executable/runtime.
