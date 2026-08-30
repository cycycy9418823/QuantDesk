# QuantDesk Architecture Inventory

Historical snapshot retained for release archaeology and compatibility tests.
The current authority is `docs/architecture_inventory.md`.

Updated through `v3.7.0.5-git-deletion-reconciliation`.

v3.7.0.5 additions:

- `quantdesk/maintenance/import_alias_reconciliation.py`: validates deprecated root import aliases for migrated modules.
- `scripts/run_import_alias_reconciliation.py`: CLI wrapper for alias coverage and scan-only import checks.
- Deprecated compatibility aliases: old root paths such as `quantdesk.paper_trading`, `quantdesk.intraday_providers`, and `quantdesk.agent_policy_guard` are supported by `quantdesk.__init__` but are not active root files.
- New code should import from `quantdesk.paper.*`, `quantdesk.intraday.*`, `quantdesk.agent.*`, and `quantdesk.research.*`.

v3.7.0.4 additions:

- `quantdesk/maintenance/git_deletion_audit.py`: read-only git deletion and import-reference audit.
- `quantdesk/research/capital_lot_sensitivity.py`: low-price capital / position / price-threshold sensitivity.
- `quantdesk/research/strategy_viability_decision.py`: research direction decision logic.
- `scripts/run_git_deletion_audit.py`, `scripts/run_capital_lot_sensitivity.py`: CLI wrappers.

Updated through `v3.7.0.2-strategy-pipeline-repair`

v3.7.0.2 repairs the research-only strategy pipeline:
- `quantdesk/data/schema_normalizer.py` performs load-time CSV schema normalization and quarantine reporting.
- `quantdesk/research/strategy_pipeline_repair.py` owns degraded PIT mode, price-coverage universe windows, benchmark discovery, board-aware limit-rate diagnostics, and pipeline precheck/rerun payloads.
- `qd.py strategy-pipeline precheck`, `factor-combine --weights`, and `combined-score-backtest --pit-mode price_coverage` are wired without changing default broker/order boundaries.

v3.7.0.1 adds the research-only strategy viability sprint on top of the data backbone:
- `quantdesk/research/strategy_viability_data_precheck.py` scans local daily CSV/data-layer readiness before any baseline run.
- `quantdesk/research/strategy_viability_runner.py` runs five price-only baseline strategies with one-day execution lag and trading-cost impact.
- `quantdesk/research/strategy_viability_report.py` writes the combined Strategy Viability Report and decision gate.
- `qd.py strategy-viability precheck|run|report` remains offline by default and never connects brokers, places orders, or changes default backtest behavior.

v3.7.0 adds the SQLite shadow data backbone:
- `quantdesk/data_store` owns schema, repositories, migration, dual writer, query safety, health, and bounded repair.
- `quantdesk/data_providers/*_chain.py` gates external providers behind `network=true`.
- CSV remains the compatibility fallback.

v3.6.2 makes the research-lite layer compute from local data:
- `quantdesk/research/similar_day.py`: market features, similarity search, forward returns
- `quantdesk/research/stress_scenarios.py`: historical scenario windows and paper portfolio stress
- `quantdesk/research/barra_style_attribution.py`: 10-factor exposure/contribution lite
- `quantdesk/research/decision_journal.py`: outcome review and statistics
- `quantdesk/research/daily_research_snapshots.py`: daily read-only research snapshots

Previous update: `v3.6.1-real-risk-alerts-and-smart-stop-integration`

v3.6.1 adds the real paper risk integration layer:
- `quantdesk/risk/position_inputs.py`
- smart stop per-position ATR/fixed/time/sector checks
- risk alerts from smart stop, concentration, liquidity, intraday, and sector snapshots
- notification center risk-alert ingestion and dedupe
- daily risk alert snapshot
- UI Risk Alerts / Smart Stop / Notifications panel

v3.6.0 is retained as a skeleton/lite/local-adapter milestone rather than a deep production completion claim.

v3.6.0 adds the real-use research loop: `stock_picking_flow`,
`paper_watchlist`, `daily_candidate_review`, adjusted-price integrity,
dynamic factor model, smart-stop and risk-alert modules, notification center,
execution realism, multiperiod/style attribution, stress/similar-day tools,
decision journal, local CSV data adapters, sqlite local store, and read-only UI
panels. All are research-only / paper-only and do not connect brokers or create
real orders.

v3.5.2 adds `quantdesk/research/strategy_recommender.py`,
`scripts/run_strategy_recommender.py`, `qd.py strategy-recommend`,
`quantdesk/research/daily_strategy_recommendation_snapshot.py`, a Home /
Overview UI, grouped sidebar navigation, and visual tokens for cards, buttons,
tables, badges, and tabular numeric display. Strategy recommendation is
research-only and does not generate broker orders.

v3.5.1.1 adds release staging hygiene: `scripts/clean_release_staging.ps1`,
`build/build_portable_release.ps1 -KeepStage`, and `self_check.py`
`release_staging_hygiene`. Brinson attribution stress coverage now includes
concentration, zero industry weights, weight normalization, missing industry
maps, multiperiod aggregation, factor extremes, and JSON safety.

Historical compatibility marker: Updated through `v3.4.3.8-true-backtest-assumptions-split-and-doc-hygiene`

Historical compatibility marker: Updated through `v3.4.4.0-multi-strategy-account-book`

This historical document is not the current source of truth. Use
`docs/architecture_inventory.md` for the active architecture inventory.

## Research Stack

- Factor evaluation: `quantdesk/factor_evaluation.py`, `scripts/run_factor_evaluation.py`
- Factor combination: `quantdesk/factor_combiner.py`, `scripts/build_combined_score.py`
- Combined score research backtest: `quantdesk/combined_score_backtest.py`, `scripts/run_combined_score_backtest.py`
- Sector rotation: `quantdesk/sector_rotation_engine.py`, `scripts/run_sector_rotation.py`
- Research dashboard: `quantdesk/factor_research_dashboard.py`, `scripts/run_factor_research_dashboard.py`
- PIT universe lite: `quantdesk/point_in_time_universe.py`, `scripts/show_pit_universe_status.py`
- Daily research snapshot: `quantdesk/daily_research_snapshot.py`, read-only daily workflow/report integration
- Multi-strategy account book: `quantdesk/multi_strategy_account_book.py`, `scripts/run_multi_strategy_account_book.py`, `qd.py multi-strategy-book`
- Intraday live workbench: `quantdesk/intraday_live_engine.py`, `quantdesk/intraday_live_snapshot.py`, `quantdesk/intraday_score.py`, `quantdesk/intraday_data_policy.py`, `scripts/run_intraday_live_workbench.py`, `qd.py live`
- Risk attribution lite: `quantdesk/risk_attribution_lite.py`, `scripts/run_risk_attribution_lite.py`, `qd.py risk-attribution`
- Experiment tracker lite: `quantdesk/experiment_tracker.py`, `scripts/run_experiments.py`, `qd.py experiments`
- Strategy recommender: `quantdesk/research/strategy_recommender.py`, `scripts/run_strategy_recommender.py`, `qd.py strategy-recommend`
- Daily strategy recommendation snapshot: `quantdesk/research/daily_strategy_recommendation_snapshot.py`, read-only daily workflow/report integration
- PIT full + charts: `quantdesk/point_in_time_universe.py`, `quantdesk/chart_renderer.py`, `scripts/validate_historical_listings.py`, `scripts/build_historical_listings_template.py`
- Engineering split: `quantdesk/backtest_loaders.py`, `quantdesk/self_check_render.py`
- Package grouping lite: `quantdesk/research/`, `quantdesk/intraday/`, `quantdesk/paper/`, `quantdesk/agent/`
- Battle-ready release: `quantdesk/onboarding.py`, `scripts/first_run_wizard.py`, `scripts/build_sample_data_package.py`, `build/quantdesk.spec`, `build/quantdesk_installer.nsi`

## Current Release Modules

- `quantdesk/chart_renderer.py`: matplotlib PNG renderer for equity curves, monthly returns, factor IC, industry exposure, quantile returns, and sector strength charts with warning fallback.
- `scripts/validate_historical_listings.py`: validates local PIT historical listings CSV without network access.
- `scripts/build_historical_listings_template.py`: writes a local historical listings example template.
- `docs/point_in_time_universe.md`: operator guide for PIT data boundaries and explicit `--pit-universe true`.
- `quantdesk/experiment_tracker.py`: research-only experiment logging, JSON-safe per-run records, index CSV listing, filtering, and diff helpers.
- `quantdesk/daily_experiment_tracker_snapshot.py`: daily read-only snapshot helper for `results/experiments/index.csv`.
- `scripts/run_experiments.py`: CLI entrypoint for `qd.py experiments`.
- Daily workflow experiment tracker snapshot: read-only latest-index preview in daily payloads, Markdown reports, and CLI summaries.
- `quantdesk/risk_attribution_lite.py`: research-only diagnostic attribution for sub-account contribution, sector exposure, factor support, PIT/data risk, and provisional intraday context.
- `quantdesk/daily_risk_attribution_snapshot.py`: daily read-only snapshot helper for `results/risk_attribution_lite_latest.json`.
- `scripts/run_risk_attribution_lite.py`: CLI entrypoint for `qd.py risk-attribution`.
- `scripts/run_intraday_live_workbench.py`: CLI entrypoint for `qd.py live`, including explicit `--init` for `state/intraday_live_account.json`.
- `docs/intraday_live_quickstart.md`: operator quickstart for `qd live --init`, `--once`, foreground `--loop`, Ctrl+C stop, and partial_daily audit boundaries.
- `quantdesk/intraday_live_engine.py`: research-only intraday workbench that builds provisional quote snapshots, partial daily snapshots, intraday scores, virtual valuation previews, condition status, sector heat preview, JSON output, and Markdown reports.
- `quantdesk/intraday_live_snapshot.py`: schema helpers for `data/intraday/partial_daily/YYYY-MM-DD.csv` and `data/intraday/spot_snapshots/YYYY-MM-DD/HHMMSS.csv`; `is_complete` is always false for partial daily.
- `quantdesk/intraday_score.py`: provisional scoring from partial daily rows with `score_basis=partial_daily_snapshot` and `is_provisional=true`.
- `quantdesk/intraday_data_policy.py`: guard helpers that forbid partial daily data in backtest, factor evaluation, walk-forward, and historical replay.
- `scripts/run_intraday_live_workbench.py`: CLI entrypoint for `qd.py live`.
- `state/intraday_live_account.json`: optional runtime-only virtual valuation state; excluded from clean zips.
- UI Intraday Live Workbench panel: read-only panel in `app/index.html` and `app/static/app.js`.
- `config/multi_strategy_account_book.example.yaml`: reset/template config with no broker and no real orders.
- `quantdesk/multi_strategy_account_book.py`: research-only / paper-only virtual portfolio account book with YAML config validation, sub-account cash allocation, optional local position CSV loading, JSON output, Markdown reporting, and daily snapshot helpers.
- `scripts/run_multi_strategy_account_book.py`: CLI entrypoint for `multi-strategy-book` summaries and full payload/report generation.
- `config/default.yaml`: includes `multi_strategy_account_book` and `daily_multi_strategy_account_book` defaults.
- `quantdesk/daily_workflow_orchestrator.py`: reads the latest multi-strategy account book as a daily read-only snapshot without changing candidates or trading conclusions.
- `quantdesk/backtest_assumptions.py`: legacy-compatible backtest assumption helpers, research disclaimer builders, assumption metadata, risk flags, and Markdown rendering helpers.
- `quantdesk/backtest.py`: imports and re-exports the assumption helpers for compatibility while keeping execution, matching, cost, and return calculations unchanged.
- `scripts/verify_clean_zip.py`: verifies versioned clean zips, release proof fields, required files, forbidden paths, and BOM-free `RELEASE_ARTIFACT.json`.
- `export_project.ps1`: writes BOM-free `RELEASE_ARTIFACT.json`, creates a versioned clean zip, and copies the same bytes to `quantdesk_clean.zip`.

## Existing Core Research Modules

- `quantdesk/factor_evaluation.py`
- `quantdesk/factor_combiner.py`
- `quantdesk/combined_score_backtest.py`
- `quantdesk/sector_rotation_engine.py`
- `quantdesk/factor_research_dashboard.py`
- `quantdesk/point_in_time_universe.py`
- `quantdesk/daily_research_snapshot.py`
- `quantdesk/backtest_assumptions.py`
- `quantdesk/multi_strategy_account_book.py`
- `quantdesk/intraday_live_engine.py`
- `quantdesk/intraday_live_snapshot.py`
- `quantdesk/intraday_score.py`
- `quantdesk/intraday_data_policy.py`

## Runtime And Release Hygiene

- Runtime outputs live in `results/`, `reports/`, `logs/`, `state/`, and `test_runtime/`.
- Local market data lives under `data/daily/`, `data/intraday/`, `data/universe/`, and `data/features/`.
- Clean release exports exclude runtime state, local data, `data/paper/`, caches, temp factor outputs, and nested zip files.
- Upload the versioned artifact `dist/quantdesk_clean_v<APP_VERSION>.zip`; `dist/quantdesk_clean.zip` is a compatibility copy only.

## Deferred Structural Work

- `backtest.py` still owns execution, matching, equity curve, and portfolio simulation logic.
- Future slimming can extract data loaders or report writers without changing default backtest behavior.
- `app/static/app.js` and `scripts/quant_app_server.py` are not split in this release.

# v3.5.1 Architecture Inventory Update

v3.5.1-truthful-attribution-installer-and-structure-hardening moves real modules into `quantdesk.research`, `quantdesk.paper`, `quantdesk.intraday`, and `quantdesk.agent`. Legacy imports are preserved through package aliases where practical.

New true attribution modules:
- `quantdesk/research/risk_attribution.py`
- `quantdesk/research/factor_attribution.py`
- `quantdesk/research/risk_observation_lite.py`

Packaging honesty files:
- `build/build_portable_release.ps1`
- `build/build_installer.ps1`
- `RELEASE_NOTES_v3.5.1.md`
## v3.7.0.3 Low Price Pipeline Finalization

Adds runtime-only price coverage historical listings, low-price research profile, signal-date price filtering, and 100-share lot-size feasibility reporting. Runtime data remains excluded from release artifacts.
# v3.7.0.6 Strategy Regression Guard and Report Hygiene

- `quantdesk/maintenance/strategy_regression_audit.py` audits strategy report integrity.
- Deprecated root module aliases from v3.7.0.5 remain compatibility shims; new code should use subpackages.

## Research Candidate Monitoring Boundary

Research-only. Not trading advice. No live trading. No broker connection. No auto execution. No buy/sell recommendation. No real-money allocation instruction.
This section documents only the research candidate monitoring/dashboard review layer.
