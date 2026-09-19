## Current Active Entry

- Active 8000 UI/API surface: `scripts/quant_api_server.py` with
  `app/templates/daily_research.html` + `app/static/js/daily_research.js`.
- Primary page loads read-only 8000 payloads such as `/api/daily/overview`,
  `/api/decision-board`, `/api/strategy-scorecard`, and `/api/ai-trader/dashboard`.
- `python -m ai_trader app` remains the supported 8001 compatibility classroom.
- Historical `/api/results` references below belong to the retired 8765 workbench
  compatibility record unless a section says otherwise.

## v3.7.0.3 Low Price Pipeline Finalization

### low-price pipeline payload

- `app_version`: `3.7.0.3-low-price-pipeline-finalization`
- `data-universe build-listings` writes runtime `data/universe/historical_listings.csv` from local price CSV coverage.
- `pit_quality`: `degraded`
- `list_date_meaning`: first available local price date, not official listing date.
- `strategy-viability run --profile low_price` reports `initial_cash=10000`, `max_positions=10`, `top_n=10`, `lot_size=100`, and signal-date `close <= 15` filtering.
- Outputs remain research-only: no broker, no real orders, no default network.

## v3.7.0.2 Strategy Pipeline Repair

### strategy-pipeline payload

- `task_type`: `strategy_pipeline_precheck|strategy_viability_repair_rerun`
- `app_version`: `3.7.0.2-strategy-pipeline-repair`
- `pit_mode`: `strict|price_coverage|off`
- `survivorship_bias_risk`: `high` or `unknown` unless complete delisting/ST history is available
- `degraded_pit_warning`: present when `price_coverage` is used without historical listings
- `debug_only`: true when `pit_mode=off`
- `benchmark`: local benchmark diagnostics only; missing benchmarks warn but do not fail
- `disclaimer`: research-only pipeline repair validation, not production trading advice

## v3.7.0.1 Strategy Viability On Data Backbone

### strategy-viability payload

- `task_type`: `strategy_viability_precheck|strategy_viability_baselines|strategy_viability_report`
- `app_version`: `3.7.0.1-strategy-viability-on-data-backbone`
- `research_only`: true
- `survivorship_bias_risk`: `high` or `unknown` unless complete delisting/ST history is available
- `partial_daily_isolated`: true
- `baselines`: five price-only strategy baselines with gross and net metrics
- `decision_gate`: `continue_research`, `refine_strategy`, or `stop_and_rethink`

## v3.7.0 Data Backbone

### data-db payload

- `task_type`: `data_db_init|data_db_stats|csv_to_sqlite_migration|data_db_reset|data_db_disable|data_db_enable|data_db_backup`
- `app_version`: `3.7.0-data-backbone`
- `output_paths.db_path`: SQLite shadow DB path
- `disclaimer`: CSV remains the compatibility fallback

### csv_to_sqlite_migration payload

- `csv_counts`: dynamic scan counts
- `migrated_file_count`
- `migrated_row_count`
- `skipped_file_count`
- `table_row_counts`
- `compare_summary`
- `warnings_top`

### data-query payload

- `task_type`: `data_query`
- `kind`: `daily|cross-section|fundamentals|sectors|index`
- `rows`
- `row_count`
- `where` is parsed through a whitelist, not arbitrary SQL

### data-health payload

- `task_type`: `data_layer_health`
- `schema_initialized`
- `wal_enabled`
- `required_tables_missing`
- `required_indexes_missing`
- `strategy_readiness`: research-only readiness audit with daily-bar coverage, `signal_before_execution`, survivorship-bias risk, and `partial_daily_isolated`; it does not run strategy backtests or change default backtest inputs.
- `partial_daily_isolated`
- `table_row_counts`

### data-quality payload

- `task_type`: `data_quality`
- `issue_count`
- `issues`
- `repair_status` when repair is requested
- default repair does not overwrite existing rows

## v3.6.2 Real Similar Day Stress And Style Attribution

- `APP_VERSION`: `3.6.2-real-similar-day-stress-and-style-attribution`
- `API_SCHEMA_VERSION`: `3.6.2`
- All payloads are `research_only=true`, `network=false`, and do not represent broker instructions.

### similar_day payload

Fields: `task_type=similar_day`, `target_date`, `index_symbol`, `feature_keys`, `similar_days`, `summary_stats`, `warnings`, `output_paths`, and `disclaimer`.

Each similar day includes `date`, `similarity`, `features`, `forward_returns`, `max_drawdown_next_20d`, and `max_gain_next_20d`.

### stress_scenarios payload

Fields: `task_type=stress_scenarios`, `position_count`, `scenario_count`, `scenarios`, `worst_scenario`, `warnings`, `output_paths`, and `disclaimer`.

Each scenario includes `scenario_id`, `start_date`, `end_date`, `estimated_portfolio_return`, `estimated_loss_amount`, `max_drawdown_estimate`, `top_loss_contributors`, `sector_breakdown`, and `data_coverage`.

### style_attribution payload

Fields: `task_type=style_attribution`, `style_factors`, `coverage`, `portfolio_style_exposure`, `factor_returns`, `style_contributions`, `residual_alpha`, `dominant_style_tilts`, `industry_exposure_summary`, `warnings`, `output_paths`, and `disclaimer`.

### decision_journal payload

Fields: `task_type=decision_journal`, `items`, `statistics`, `total_decisions`, `reviewed_count`, `pending_count`, `hit_rate`, `by_strategy`, `by_emotion`, `by_confidence_bucket`, `warnings`, `output_paths`, and `disclaimer`.

### daily research snapshots payload

`daily_workflow` may include `research_snapshots` with read-only `similar_day`, `stress_scenarios`, `style_attribution`, and `decision_journal` latest JSON statuses. Missing or stale snapshots warn and do not fail the daily workflow.

## v3.6.1 Real Risk Alerts And Smart Stop Integration

- `APP_VERSION`: `3.6.1-real-risk-alerts-and-smart-stop-integration`
- `API_SCHEMA_VERSION`: `3.6.1`
- New/updated payloads remain `research_only=true`, `paper_only=true`, `network=false`, and do not represent broker instructions.

### position_inputs payload

Fields: `ok`, `status`, `positions`, `position_count`, `source_status`, `input_status`, `warnings`, `research_only`, `network`.

Each normalized position includes `symbol`, `name`, `sector`, `theme`, `quantity`, `reference_price`, `latest_price`, `market_value`, `weight`, `entry_date`, `entry_price`, `strategy`, `source`, and `research_only`.

### smart_stop payload

Fields: `task_type=smart_stop`, `position_count`, `safe_count`, `watch_count`, `triggered_count`, `items`, `position_input_status`, `warning_count`, `warnings_top`, `output_paths`, and `disclaimer`.

Each item includes `atr`, `atr_period`, `atr_multiplier`, `atr_stop_price`, `fixed_stop_price`, `composite_stop_price`, `distance_to_stop_pct`, `trigger_status`, `severity`, `rationale`, and `research_only`.

### risk_alerts payload

Fields: `task_type=risk_alerts`, `position_count`, `alert_count`, `critical_count`, `warning_count_alerts`, `watch_count`, `alerts`, `smart_stop_summary`, `position_input_status`, `notification_ready`, `warnings`, `output_paths`, and `disclaimer`.

Each alert includes `alert_id`, `alert_type`, `severity`, `symbol`, `sector`, `message`, `rationale`, `source`, `triggered_at`, `dedupe_key`, `research_only`, `requires_user_review`, and `recommended_action_label`.

### notification_center payload

Fields: `task_type=notification_center`, `dry_run`, `source`, `message_count`, `sent_count`, `skipped_dedupe_count`, `channels_attempted`, `items`, `secret_leakage_check`, `warnings`, `output_paths`, and `disclaimer`.

### daily risk snapshot payload

`daily_workflow` may include `risk_alert_snapshot` with `status`, `generated_at`, `alert_count`, `critical_count`, `top_alerts`, `smart_stop_summary`, `latest_json`, `latest_report`, and `warnings`.

## v3.6.0 Real Use Quant Platform Completion

- `APP_VERSION`: `3.6.0-real-use-quant-platform-completion`
- `API_SCHEMA_VERSION`: `3.6.0`
- New v3.6.0 task payloads include `ok`, `status`, `task_type`, `generated_at`, `app_version`, `network=false`, `research_only=true`, `paper_only=true`, `warnings`, `errors`, `output_paths`, and `disclaimer`.
- New task types: `stock_picking_flow`, `paper_watchlist`, `daily_candidate_review`, `data_integrity`, `dynamic_factor_model`, `smart_stop`, `risk_alerts`, `notification_center`, `execution_realism`, `style_attribution`, `return_explain`, `stress_test`, `similar_day`, `decision_journal`, `money_flow`, `dragon_tiger`, `fundamental_metrics`, and `local_store`.
- `stock_picking_flow` adds `top_candidates`, `sector_summary`, `strategy_summary`, `risk_summary`, `paper_watchlist_suggestions`, and `review_tasks`.
- `data_integrity` records `price_adjustment`, `adjustment_layout_status`, `corporate_actions_status`, and `price_jump_candidates`.
- `smart_stop` and `risk_alerts` emit warning-only paper risk items; they never modify holdings or create real trading actions.
- `notification_center` defaults to `dry_run=true`; external channels require explicit configuration and secrets are not written into reports.
- `execution_realism` is optional and records assumptions without changing default backtest behavior.

## v3.5.2 UI Home And Strategy Recommender

- `APP_VERSION`: `3.5.2-ui-home-and-strategy-recommender`
- `API_SCHEMA_VERSION`: `3.5.2`
- `strategy_recommendation` payloads are research-only and include `ok`, `status`, `task_type`, `generated_at`, `app_version`, `network=false`, `research_only=true`, `mode`, `symbol`, `sector`, `top`, `input_status`, `recommendations`, `strategy_counts`, `top_sectors`, `risk_flags`, `warnings`, `errors`, `output_paths`, and `disclaimer`.
- Summary JSON adds `recommendation_count`, `top_recommendations`, `strategy_counts`, `warning_count`, `warnings_top`, `risk_flags_top`, and `output_paths`.
- Daily workflow payloads may add `strategy_recommendation_snapshot`, loaded read-only from `results/strategy_recommendation_latest.json`.

## v3.5.1.1 Release Staging Cleanup And Attribution Stress Tests

- `APP_VERSION`: `3.5.1.1-release-staging-cleanup-and-attribution-stress-tests`
- `API_SCHEMA_VERSION`: `3.5.1.1`
- `risk_attribution` payloads retain Brinson-Fachler allocation / selection / interaction fields and add stress-tested behavior for normalized weights, missing industry maps, and multiperiod aggregation.
- Release build status manifests remain honest when PyInstaller or NSIS are unavailable.

## v3.5.0 Battle Ready Research And Portable Release

- `APP_VERSION`: `3.5.0-battle-ready-research-and-portable-release`
- `API_SCHEMA_VERSION`: `3.5.0`
- Adds `task_type=first_run_wizard` summary payloads with config path,
  selected data directory, synthetic sample-data manifest, live-account init
  status, self-check status, warnings, next commands, and research-only
  disclaimer. Existing payload contracts remain additive.

## v3.4.5.1 Engineering Docs And Package Grouping Lite

- `APP_VERSION`: `3.4.5.1-engineering-docs-and-package-grouping-lite`
- `API_SCHEMA_VERSION`: `3.4.5.1`
- Payload contracts are unchanged from v3.4.5.0. This release adds package
  indexes, backtest loader split, self-check render split, and documentation
  navigation without changing trading, scoring, attribution, or backtest math.

## v3.4.5.0 PIT Full And Static Charts

Backtest payloads may include `pit_filter_audit`, `assumptions.pit_universe_filter`,
`assumptions.pit_universe_used`, and `charts`. PIT filtering is off unless the
operator passes `--pit-universe true`.

Static chart output paths are optional. If matplotlib is unavailable, commands
return warnings and continue without pretending charts were generated.

## v3.4.4.3 Experiment Tracker Lite

- `APP_VERSION`: `3.4.4.3-experiment-tracker-lite`
- `API_SCHEMA_VERSION`: `3.4.4.3`

Experiment Tracker Lite writes runtime-only files under:

- `results/experiments/index.csv`
- `results/experiments/exp_YYYYMMDD_HHMMSS_<task>_<counter>.json`

Experiment JSON fields include `experiment_id`, `generated_at`, `app_version`, `task_type`, `params`, `metrics`, `git_commit`, `config_hash`, `data_snapshot_hash`, `output_paths`, `notes`, `tags`, and `disclaimer`.

`qd.py experiments --json true --summary true` returns compact JSON with `task_type=experiments`, `experiment_count`, `latest_experiments`, `filters`, `diff`, `output_paths`, `warning_count`, and `disclaimer`.

Daily workflow payloads may add `experiment_tracker_snapshot`, a read-only snapshot loaded from `results/experiments/index.csv`. Daily summary mode adds `experiment_tracker_status`, `experiment_tracker_count`, and `experiment_tracker_report`.
## v3.4.4.2 Risk Attribution Lite And Live Polish

- `APP_VERSION`: `3.4.4.2-risk-attribution-lite-and-live-polish`
- `API_SCHEMA_VERSION`: `3.4.4.2`

Risk Attribution Lite writes:

- `results/risk_attribution_lite_latest.json`
- `results/risk_attribution_lite_YYYYMMDD_HHMMSS.json`
- `reports/risk_attribution_lite_latest.md`
- `reports/risk_attribution_lite_YYYYMMDD_HHMMSS.md`

Top-level payload fields include `ok`, `status`,
`task_type=risk_attribution_lite`, `generated_at`, `app_version`,
`network=false`, `portfolio_name`, `inputs`, `input_status`,
`sub_account_attribution`, `sector_exposure`,
`factor_support_attribution`, `data_risk_attribution`,
`intraday_context_attribution`, `risk_flags`, `warnings`, `errors`,
`assumptions`, `output_paths`, and `disclaimer`.

CLI summary mode adds `portfolio_name`, `input_status`,
`sub_account_count`, `top_contributors`, `top_sector_exposures`,
`usable_factor_count`, `weak_factor_count`, `pit_universe`,
`intraday_context_available`, and `risk_flags_top`.

Daily workflow payloads may add `risk_attribution_snapshot`, a read-only
snapshot loaded from `results/risk_attribution_lite_latest.json`. Daily
summary mode adds `risk_attribution_status`, `risk_attribution_portfolio`,
`risk_attribution_report`, and `risk_attribution_warning_count`.

`qd live --init` can create `state/intraday_live_account.json` with
`cash=0`, `positions=[]`, `research_only=true`, `no_broker=true`, and
`no_real_orders=true`. The file is runtime-only and excluded from clean zips.

## v3.4.4.1 Intraday Live Workbench And Config Hygiene

- `APP_VERSION`: `3.4.4.1-intraday-live-workbench-and-config-hygiene`
- `API_SCHEMA_VERSION`: `3.4.4.1`

Intraday live workbench writes:

- `results/intraday_live_workbench_latest.json`
- `results/intraday_live_workbench_YYYYMMDD_HHMMSS.json`
- `reports/intraday_live_workbench_latest.md`
- `reports/intraday_live_workbench_YYYYMMDD_HHMMSS.md`

Top-level payload fields include `ok`, `status`,
`task_type=intraday_live_workbench`, `generated_at`, `app_version`, `network`,
`trade_date`, `snapshot_time`, `market_session`, `watchlist_count`,
`quote_count`, `partial_daily`, `intraday_scores`,
`virtual_account_snapshot`, `condition_plan_status`, `sector_heat`,
`risk_flags`, `warnings`, `errors`, `output_paths`, and `disclaimer`.

CLI summary mode adds `partial_daily_row_count`, `intraday_score_count`,
`triggered_condition_count`, `sector_heat_count`, `risk_flags_top`, and stable
latest output paths. `partial_daily` is provisional and forbidden from
backtest, factor evaluation, walk-forward, historical replay, and formal
features.

Daily workflow payloads may add `intraday_live_snapshot`, a read-only snapshot
loaded from `results/intraday_live_workbench_latest.json`. Daily summary mode
adds `intraday_live_status`, `intraday_live_snapshot_time`,
`intraday_live_report`, and `intraday_live_warning_count`.

## v3.4.4.0 Multi Strategy Account Book

- `APP_VERSION`: `3.4.4.0-multi-strategy-account-book`
- `API_SCHEMA_VERSION`: `3.4.4.0`

Multi-strategy account book writes:

- `results/multi_strategy_account_book_latest.json`
- `results/multi_strategy_account_book_YYYYMMDD_HHMMSS.json`
- `reports/multi_strategy_account_book_latest.md`
- `reports/multi_strategy_account_book_YYYYMMDD_HHMMSS.md`

Top-level payload fields include `ok`, `status`,
`task_type=multi_strategy_account_book`, `generated_at`, `app_version`,
`network=false`, `portfolio_name`, `currency`, `initial_cash`, `cash`,
`market_value`, `equity`, `sub_account_count`, `sub_accounts`, `allocation`,
`allocation_cash`, `risk_flags`, `warnings`, `errors`, `assumptions`,
`output_paths`, and `disclaimer`.

CLI summary mode keeps the v3.4.2.5 summary contract and adds
`portfolio_name`, `sub_account_count`, `initial_cash`, `equity`, and
`risk_flags_top`. It does not include the full `sub_accounts` object.

Daily workflow payloads may add `multi_strategy_account_book_snapshot`, a
read-only snapshot loaded from `results/multi_strategy_account_book_latest.json`.
Daily summary mode adds `multi_strategy_book_status`,
`multi_strategy_book_portfolio`, `multi_strategy_book_sub_account_count`, and
`multi_strategy_book_report`.

The account book is research-only / paper-only. It does not connect to brokers,
place real orders, auto-rebalance, generate buy/sell advice, or change existing
strategy, screen, scoring, backtest, or daily trading-conclusion logic.

## v3.4.3.8 True Backtest Assumptions Split And Doc Hygiene

- `APP_VERSION`: `3.4.3.8-true-backtest-assumptions-split-and-doc-hygiene`
- `API_SCHEMA_VERSION`: `3.4.3.8`
- Legacy backtest assumption helpers now live in `quantdesk/backtest_assumptions.py` and remain import-compatible through `quantdesk.backtest`.
- Backtest calculation, default cost/slippage, execution, and matching contracts are unchanged.
- Release zips continue to embed BOM-free `RELEASE_ARTIFACT.json` and use versioned filenames.

## v3.4.3.7 Backtest Assumptions Split And Release Proof Polish

- `APP_VERSION`: `3.4.3.7-backtest-assumptions-split-and-release-proof-polish`
- `API_SCHEMA_VERSION`: `3.4.3.7`
- Backtest payloads may include additive `assumption_metadata`, `risk_flags`, and `disclaimer` fields sourced from `quantdesk/backtest_assumptions.py`; return calculation and matching contracts are unchanged.
- Clean release zips embed BOM-free `RELEASE_ARTIFACT.json` and use versioned artifact filenames as the upload source of truth.

# QuantDesk API Payload Schema

## v3.4.3.6 Research Dashboard Daily Snapshot

- `APP_VERSION`: `3.4.3.7-backtest-assumptions-split-and-release-proof-polish`
- `API_SCHEMA_VERSION`: `3.4.3.6`

Daily workflow payloads add:

- `research_dashboard_snapshot`

The snapshot includes `enabled`, `status`, `readiness_level`, `generated_at`,
`stale`, `available_module_count`, `missing_module_count`,
`stale_module_count`, `key_findings_preview`, `risk_flags_preview`,
`suggested_next_actions_preview`, `top_sectors_preview`,
`usable_factor_count`, `weak_factor_count`, `pit_universe`, `pit_status`,
`backtest_sharpe`, `backtest_max_drawdown`, `latest_report`, `latest_json`,
`warnings`, `warning_count`, and `disclaimer`.

Daily summary mode keeps the existing contract and adds
`research_dashboard_status`, `research_readiness_level`,
`research_warning_count`, `research_dashboard_report`, `pit_universe`, and
`pit_status`.

The snapshot is read-only research diagnostics. It does not alter candidates,
screening, guardrails, scoring weights, backtest matching, daily trading
conclusions, broker boundaries, or real orders.

## v3.4.3.5 Point-in-Time Universe Lite And Project Hygiene

- `APP_VERSION`: `3.4.3.5-point-in-time-universe-lite-and-project-hygiene`
- `API_SCHEMA_VERSION`: `3.4.3.5`

PIT universe status writes:

- `results/pit_universe_status_latest.json`
- `results/pit_universe_status_YYYYMMDD_HHMMSS.json`

Top-level payload fields include `ok`, `status`,
`task_type=point_in_time_universe`, `generated_at`, `app_version`,
`target_date`, `network`, `config`, `pit_status`, `excluded_counts`,
`warnings`, `output_paths`, `suggested_next_action`, and `disclaimer`.

`pit_status` includes `available`, `pit_universe`, `symbol_count`,
`list_date_coverage`, `delist_date_coverage`, `st_coverage`,
`suspend_coverage`, `warnings`, and `suggested_next_action`.

Summary mode follows the v3.4.2.5 CLI summary contract and adds
`pit_universe`, `listings_available`, `list_date_coverage`,
`delist_date_coverage`, `st_coverage`, and `suspend_coverage`.

PIT Universe Lite is diagnostics only. Missing `historical_listings.csv`
produces warning status and does not fabricate point-in-time membership.

## v3.4.3.4 Factor Research Dashboard

- `APP_VERSION`: `3.4.3.4-factor-research-dashboard`
- `API_SCHEMA_VERSION`: `3.4.3.4`

Research dashboard writes:

- `results/research_dashboard_latest.json`
- `results/research_dashboard_YYYYMMDD_HHMMSS.json`
- `reports/research_dashboard_report_YYYYMMDD_HHMMSS.md`
- `reports/research_dashboard_report_latest.md`

Top-level payload fields include `ok`, `status`,
`task_type=factor_research_dashboard`, `generated_at`, `app_version`,
`data_source`, `target_date`, `network`, `config`, `module_health`,
`factor_health_summary`, `combined_score_summary`, `backtest_summary`,
`sector_rotation_summary`, `dashboard_summary`, `warnings`, `assumptions`,
`output_paths`, `suggested_next_action`, and `disclaimer`.

Summary mode follows the v3.4.2.5 CLI summary contract and adds
`available_module_count`, `missing_module_count`, `stale_module_count`,
`strong_factor_count`, `usable_factor_count`, `weak_factor_count`,
`hot_sector_count`, `improving_sector_count`, `readiness_level`,
`research_status`, `module_status_preview`, `key_findings_preview`,
`risk_flags_preview`, and `suggested_next_actions_preview`.

Research dashboard is diagnostics only. It does not alter default screen,
backtest, daily workflow, strategy signals, default scoring weights, broker
boundaries, or real orders. Payloads and reports must include PIT-universe and
survivorship-bias assumptions, and top candidates/sectors remain research
observations only.

## v3.4.3.3 Sector Rotation Engine

- `APP_VERSION`: `3.4.3.3-sector-rotation-engine`
- `API_SCHEMA_VERSION`: `3.4.3.3`

Sector rotation writes:

- `results/sector_rotation_latest.json`
- `results/sector_rotation_YYYYMMDD_HHMMSS.json`
- `reports/sector_rotation_report_YYYYMMDD_HHMMSS.md`
- `reports/sector_rotation_report_latest.md`

Top-level payload fields include `ok`, `status`,
`task_type=sector_rotation`, `generated_at`, `app_version`, `data_source`,
`target_date`, `network`, `config`, `universe`, `benchmark`,
`sector_rankings`, `top_improving_sectors`, `weakening_sectors`, `warnings`,
`assumptions`, `output_paths`, `suggested_next_action`, and `disclaimer`.

Each `sector_rankings[]` row includes `sector_name`, `symbol_count`,
window returns, window relative strength, `current_rank`,
`rank_improvement_5d`, `rank_improvement_20d`, breadth metrics,
`heat_score`, and `heat_status`.

Summary mode follows the v3.4.2.5 CLI summary contract and adds
`symbol_count`, `sector_count`, `date_count`, `sample_count`,
`hot_sector_count`, `improving_sector_count`, `weakening_sector_count`,
`benchmark_method`, `top_sectors_preview`, `top_improving_preview`,
`weakening_preview`, `data_status`, and `pit_universe`.

Sector rotation is research diagnostics only. It does not alter default screen,
backtest, daily workflow trading conclusions, strategy signals, default scoring
weights, broker boundaries, or real orders. Payloads and reports must include
benchmark fallback, PIT-universe, and survivorship-bias assumptions.

## v3.4.3.2 Combined Score Research Backtest

- `APP_VERSION`: `3.4.3.2-combined-score-research-backtest`
- `API_SCHEMA_VERSION`: `3.4.3.2`

Combined-score research backtest writes:

- `results/combined_score_backtest_latest.json`
- `results/combined_score_backtest_YYYYMMDD_HHMMSS.json`
- `reports/combined_score_backtest_report_YYYYMMDD_HHMMSS.md`
- `reports/combined_score_backtest_report_latest.md`

Top-level payload fields include `ok`, `status`,
`task_type=combined_score_backtest`, `generated_at`, `app_version`,
`data_source`, `target_date`, `network`, `config`, `universe`,
`performance`, `portfolio_summary`, `warnings`, `assumptions`,
`output_paths`, `suggested_next_action`, and `disclaimer`.

`performance` includes `gross_return`, `net_return`, `cumulative_return`,
`annualized_return`, `annualized_volatility`, `sharpe_ratio`,
`max_drawdown`, `win_rate`, `average_daily_return`, `best_day`, `worst_day`,
`turnover_mean`, `turnover_median`, `rebalance_count`, and `cost_total`.

Summary mode follows the v3.4.2.5 CLI summary contract and adds
`symbol_count`, `date_count`, `signal_date_count`, `rebalance_count`,
`latest_selected_count`, `portfolio_mode`, `top_n`, `quantile`,
`long_short`, `net_return`, `annualized_return`, `sharpe_ratio`,
`max_drawdown`, `turnover_mean`, `latest_selected_preview`, `data_status`,
and `pit_universe`.

Combined-score backtest is research diagnostics only. It does not alter
default screen, default backtest, daily workflow, strategy signals, default
scoring weights, broker boundaries, or real orders. Payloads and reports must
include execution-lag, transaction-cost, slippage, PIT-universe, and
survivorship-bias assumptions.

## v3.4.3.1 Factor Combiner And Neutralized Score

- `APP_VERSION`: `3.4.3.1-factor-combiner-and-neutralized-score`
- `API_SCHEMA_VERSION`: `3.4.3.1`

Combined score writes:

- `results/combined_score_latest.json`
- `results/combined_score_YYYYMMDD_HHMMSS.json`
- `reports/combined_score_report_YYYYMMDD_HHMMSS.md`
- `reports/combined_score_report_latest.md`

Top-level combined-score payload fields include `ok`, `status`,
`task_type=factor_combination`, `generated_at`, `app_version`, `data_source`,
`target_date`, `network`, `config`, `universe`, `factor_weights`,
`score_summary`, `top_candidates`, `neutralization`, `warnings`,
`assumptions`, `output_paths`, `suggested_next_action`, and `disclaimer`.

Summary mode follows the v3.4.2.5 CLI summary contract and adds
`factor_count`, `symbol_count`, `date_count`, `sample_count`, `scored_count`,
`top_candidate_count`, `weight_method`, `standardize_method`,
`neutralization_status`, `top_candidates_preview`, `data_status`, and
`pit_universe`.

Combined score is research diagnostics only. It does not alter default screen,
backtest, daily workflow, strategy signals, default scoring weights, broker
boundaries, or real orders.

## v3.4.3.0 Factor Evaluation Platform

- `APP_VERSION`: `3.4.3.0-factor-evaluation-platform`
- `API_SCHEMA_VERSION`: `3.4.3.0`

Factor evaluation writes:

- `results/factor_health_latest.json`
- `results/factor_health_YYYYMMDD_HHMMSS.json`
- `reports/factor_evaluation_report_YYYYMMDD_HHMMSS.md`
- `reports/factor_evaluation_report_latest.md`

Top-level factor-health payload fields include `ok`, `status`,
`task_type=factor_evaluation`, `generated_at`, `app_version`, `data_source`,
`target_date`, `network`, `config`, `universe`, `factor_metrics`, `warnings`,
`assumptions`, `output_paths`, `suggested_next_action`, and `disclaimer`.

Each `factor_metrics.<factor>` entry includes `coverage`, `ic_mean`, `ic_std`,
`icir`, `ic_positive_ratio`, `rank_ic_mean`, `rank_ic_std`, `rank_icir`,
`rank_ic_positive_ratio`, `long_short_return_mean`, `monotonicity_score`,
`quantile_returns`, `decay`, `sample_count`, `date_count`, `symbol_count`, and
`health_status`.

Summary mode follows the v3.4.2.5 CLI summary contract and adds
`factor_count`, `symbol_count`, `date_count`, `sample_count`,
`usable_factor_count`, `weak_factor_count`, `invalid_factor_count`,
`best_factors`, `weakest_factors`, `top_rank_ic_factors`, `pit_universe`, and
`data_status`.

All factor evaluation payloads are research diagnostics only. They do not
change trading signals, scoring weights, backtest matching, backtest return
logic, broker boundaries, or default network behavior. Payloads and reports
must include the point-in-time universe limitation / survivorship-bias warning
when PIT reconstruction is not enabled.

## v3.4.2.7 P3 Engineering Cleanup Replay Performance And Market Calendar

- `APP_VERSION`: `3.4.2.7-p3-engineering-cleanup-replay-performance-and-market-calendar`
- `API_SCHEMA_VERSION`: `3.4.2.7`

Historical replay payloads keep their existing fields and add/confirm the
performance metadata:

- `daily_slice_mode`: `in_memory_preloaded`
- `daily_history_load_mode`: `preload_once`
- `daily_history_loaded_symbol_count`: number of local daily histories loaded before the replay loop

Market-clock payloads expose the calendar source through `calendar_source`,
`calendar_quality`, and `calendar_warnings`. Calendar loading order is CSV,
legacy YAML, then `source=unavailable` with a degraded warning; no production
payload should silently rely on hard-coded holiday fallback data.

## v3.4.2.5 CLI Summary Output And Operator Readability

- `APP_VERSION`: `3.4.2.5-cli-summary-output-and-operator-readability`
- `API_SCHEMA_VERSION`: `3.4.2.5`
- Commands that support `--summary true` still write their full latest JSON
  artifacts. When `--json true --summary true` is used, stdout is compact JSON
  with at least `ok`, `status`, `task_type`, `generated_at`, `app_version`,
  `target_date`, `network`, `warning_count`, `warnings_top`, `key_counts`,
  `output_paths`, `suggested_next_action`, and `disclaimer`.
- Summary extensions may include command-specific fields such as
  `missing_daily_count`, `stale_count`, `ready_count`, `mode`, `strategy`,
  `symbols_count`, `trade_count`, `metrics_preview`, `selected_count`,
  `candidate_count`, `theme_count`, `window_count`, `quality_status`,
  `generated_reports_count`, `guardrail_status`, `daily_ready`, and
  `bootstrap_mode`.
- Summary mode is presentation-only. It does not change strategies, scoring,
  backtest return logic, provider behavior, network defaults, broker boundaries,
  LLM defaults, or investment-advice boundaries.

## v3.4.2.4 Windows Python Env And Command Consistency

- `APP_VERSION`: `3.4.2.4-windows-python-env-and-command-consistency`
- `API_SCHEMA_VERSION`: `3.4.2.4`
- `env-doctor --json true` writes `results/python_env_latest.json` and prints JSON with `executable`, `version`, `path_python_available`, `py_launcher_available`, `stdout_encoding`, `filesystem_encoding`, `utf8_mode`, `cwd`, `warnings`, and `suggested_next_action`.
- `env-doctor` is local-only. It does not use network, does not connect to brokers, and does not affect strategies, scoring, or backtest return logic.

## v3.4.2.3 CLI Contract First Run Quant Loop Closure

- `APP_VERSION`: `3.4.2.3-cli-contract-first-run-quant-loop-closure`
- `API_SCHEMA_VERSION`: `3.4.2.3`
- `status --json true` and `fix-plan --json true` print JSON to stdout and remain read-only/offline by default.
- `data-bootstrap --network false` may return `status="initialized"` with `daily_ready=false`, `bootstrap_mode="offline_universe_only"`, and `next_required_step="network_data_update_or_manual_import"`.
- `screen` writes `results/screen/screen_latest.json` and `reports/screen_report_latest.md`; missing or empty features return `status="warning"`, `warning_kind="missing_features"`.
- `backtest` writes `results/backtest/backtest_latest.json` and `reports/backtest_report_latest.md`; missing local daily CSV files return `status="warning"`, `warning_kind="no_daily_data"` instead of placeholder success.
- `walk-forward --network false` is accepted by the CLI contract; the command remains local-only and records network assumptions/warnings.
- Default target-date fields resolve to the latest complete A-share trading day unless the user explicitly passes a target/end date. Explicit future, non-trading, or not-yet-closed dates should be visible in warnings.

## v3.4.2.2 Clean Zip Bootstrap And Calendar Warning Fields

`data-bootstrap` writes `results/data_bootstrap/data_bootstrap_latest.json` and
`reports/data_bootstrap_report_latest.md`. Key fields include `status`,
`preset`, `symbols`, `symbol_count`, `network`, `start_date`, `end_date`,
`created_universe`, `updated_daily_count`, `failed_symbols`, `failed_reason`,
and `suggested_next_action`. `network=false` means no provider calls.

Data freshness payloads may now report `data_freshness_status="no_universe"`
or `"no_data"` and `warning_kind="no_universe"` / `"no_symbols_selected"` so a
fresh clean zip is not mistaken for a healthy current-day dataset. The default
calendar source is `data/calendar/a_share_trading_calendar.csv`; YAML calendar
files are legacy fallback only, and built-in holidays are deprecated fallback.

## v3.3.7 slim command output and symbol display fields

Release: v3.3.7 Data Update Output Slim And Mobile Visual Check

`data-update` and `data-health` payload schemas keep full detail in JSON. Console output is now a view layer only and may omit long `symbols` lists. The canonical payload fields remain unchanged: `symbols_checked`, `stale_symbols`, `missing_symbols`, `failed_symbols`, `updated_symbols`, and provider result lists still carry full local data.

Display-only helpers may summarize symbols with `visible`, `omitted_count`, and `is_truncated`. Use `--json true` or `--verbose true` for full stdout. UI command blocks may show a shortened `--symbols` preview but copy buttons must copy the complete command.

This is presentation metadata only. It does not change strategy logic, scoring, backtest returns, provider behavior, network defaults, LLM defaults, or investment-advice boundaries.

## v3.3.4 Daily Output Summary and Artifact Center Followthrough

- `APP_VERSION`: `3.3.4-daily-output-summary-and-artifact-center-followthrough`
- `API_SCHEMA_VERSION`: `3.3.4`
- `daily_workflow_latest.json` may include `daily_console_summary` with `status`, `headline`, `warning_count`, `failed_count`, `report_path`, `result_path`, and up to three `next_actions`.
- Daily `artifacts` should include `daily_report`, `daily_result`, `data_health`, `guardrails`, and `command_center`. Missing files are represented as missing in the artifact center, not as runtime failures.
- `qd.py daily` prints the console summary by default. Passing `--json true` or `--verbose true` prints the complete JSON payload; the complete JSON is always written to `results/daily_workflow_latest.json`.
- Daily guardrail warnings are governance/data-quality hints only. They do not change strategy parameters, scoring formulas, or backtest return logic and do not constitute investment advice.

## v3.3.3 Entrypoint Consolidation Status and Daily Guardrails

- `APP_VERSION`: `3.3.3-entrypoint-consolidation-status-and-daily-guardrails`
- `API_SCHEMA_VERSION`: `3.3.3`
- `status` writes `results/status_latest.json` and `reports/status_report_latest.md` as local-only advisory outputs. It reads existing local results and operation logs, does not fetch network data, does not call LLMs, does not run strategies, and provides at most three next-action suggestions.
- Daily workflow payloads may include `guardrail_summary` with quick governance statuses such as lookahead, execution assumptions, feature quality, statistical validation, and parameter sensitivity. Guardrail findings are warnings only and do not change strategy parameters, scoring formulas, or backtest return logic.

## v3.3.2.5 Workbench State Consistency Schema Noise and UI Polish

- `APP_VERSION`: `3.3.2.5-workbench-state-consistency-schema-noise-and-ui-polish`
- `API_SCHEMA_VERSION`: `3.3.2.5`

Workbench/data freshness payloads may include `data_scope`, `symbols_requested`, `symbols_checked`, `coverage_date`, `latest_date_min`, `latest_date_max`, `latest_updated_symbols`, `updated_symbols`, `stale_symbols`, and `missing_symbols`. `latest_local_daily_date` is kept for compatibility and should be treated as `coverage_date` or `latest_date_min`, not as “every symbol has the newest date”.

Schema validation payloads include `confirmed_secret_leak_count`, `secret_like_reference_count`, and `secret_findings`. Low-risk `secret_like_reference` findings are hints, not confirmed leakage. All payload guidance remains local research only and 不构成投资建议。

## v3.3.2.4 Workbench Empty State Warning Severity and Actionability Fix

- `APP_VERSION`: `3.3.2.4-workbench-empty-state-warning-severity-and-actionability-fix`
- `API_SCHEMA_VERSION`: `3.3.2.4`
- Workbench next actions may include `category` values such as `data`, `validation`, `review`, or `system`; these are UI grouping hints only and do not change result status semantics.
- Frontend labels may localize metric keys and status values for readability. JSON payloads continue to use the common status enum and existing schema keys.

## v3.3.2.3 Workbench Refresh Command Center and Target Date Semantics Fix

- `APP_VERSION`: `3.3.2.3-workbench-refresh-command-center-and-target-date-semantics-fix`
- `API_SCHEMA_VERSION`: `3.3.2.3`
- `data-update` payloads include `effective_target_date` and `target_date_source`; explicit `--target-date` values remain the effective target date, while automatic complete-trading-day inference is only used when target-date is omitted.
- Workbench overview payloads normalize missing metrics to friendly empty states and read nested data-health `freshness` / `intraday` summaries when available.
- Workbench refresh UI is read-only: it only re-fetches local GET APIs and does not trigger network, provider updates, workflows, LLM calls, or trading actions.

## v3.3.2.2 CI Stability Repo Hygiene and Calendar Cache

- `APP_VERSION`: `3.3.2.2-ci-stability-repo-hygiene-and-calendar-cache`
- `API_SCHEMA_VERSION`: `3.3.2.2`
- Provider diagnostic payloads may report planned roadmap entries as `status="planned"` with `placeholder=true` and `implemented=false`. These rows are not real failures and are excluded from provider failure counts.
- `trading_calendar` payload semantics are unchanged; the implementation now caches CSV loads by path and exposes `clear_calendar_cache()` for tests.

## v3.3.2.1 Data Update Network Flag Console and Encoding Fix

- `APP_VERSION`: `3.3.2.1-data-update-network-flag-console-and-encoding-fix`
- `API_SCHEMA_VERSION`: `3.3.2.1`

`data-update` result payloads continue to follow the common local result contract and now make the network decision explicit:

- `network`: `true` only when the user passes `--network true` or `--enable-network`, unless `--no-network` is also present.
- `auto_update_enabled`: mirrors explicit network authorization for this patch.
- `update_allowed`: `true` only when network/auto-update is enabled and neither `dry_run` nor `preflight_only` is active.
- `dry_run`: `false` by default; `true` only for explicit dry-run/preflight behavior.
- `warnings`: includes a conflict warning when `--no-network` overrides a network-enabling flag.

JSON output is written with UTF-8 and `ensure_ascii=False`; Markdown reports are written as UTF-8. Runtime outputs remain excluded from clean zip.

## v3.3.1 QuantDesk Workbench UI and Workflow Unification

- `APP_VERSION`: `3.3.1-quantdesk-workbench-ui-and-workflow-unification`
- `API_SCHEMA_VERSION`: `3.3.1`

New read-only workbench APIs:

- `GET /api/workbench/overview`
- `GET /api/workbench/command-center`
- `GET /api/workbench/artifacts`
- `GET /api/workbench/next-actions`

These endpoints only read existing `results/`, `reports/`, `operation_log`, `command_center_status_latest.json`, and local data directory presence. They do not run tasks, do not connect to providers, do not call LLMs, and do not produce investment advice.

`overview` returns app/schema metadata, `status_cards`, `workflow_steps`, compact data/validation/observation summaries, and `next_actions`. Missing runtime files are represented as `status=missing` or `warning`, not HTTP 500. `artifacts` returns grouped file metadata with `exists`, `path`, `updated_at`, `size_bytes`, `module`, and `description`.

## v3.3.0 Consolidation and Schema Contracts

- `APP_VERSION`: `3.3.0-consolidation-and-schema-contracts`
- `API_SCHEMA_VERSION`: `3.3.0`

New local governance outputs:

- `results/schema_validation_latest.json`
- `reports/schema_validation_report_latest.md`

Common result payloads should expose `schema_version`, `app_version`, optional `package_version`, `api_schema_version`, `status`, `ok`, `generated_at` or `checked_at`, `target_date`, `symbols`, `warnings`, `failed_reason`, `skipped_reason`, `suggested_next_action`, and `disclaimer`. Existing domain fields remain compatible.

Allowed status values are `passed`, `warning`, `failed`, `skipped`, `missing`, `partial`, and `unavailable`. Compatibility aliases normalize `ok` to `passed`, `error` to `failed`, and `not_available` / `not_implemented` to `unavailable`. `network_disabled` is skipped/offline, not a real provider failure.

`schema-check` validates latest local result files, masks possible secrets, and writes a report. It defaults to `network=false`, does not call LLMs, does not read `config/llm_config.json` or `config/provider_secrets.json`, and does not constitute investment advice.

See also `docs/schema_contracts.md` and `docs/schema_examples/*.json`.

## v3.2.9 Parameter Sensitivity and Robustness Audit

- `APP_VERSION`: `3.2.9-parameter-sensitivity-and-robustness-audit`
- `API_SCHEMA_VERSION`: `3.2.9`

New local-only diagnostic outputs:

- `results/parameter_sensitivity_latest.json`
- `reports/parameter_sensitivity_report_latest.md`

The parameter sensitivity payload contains `status`, `symbol`, `target_date`, `parameter_count`, `combination_count`, `metrics`, `robustness_score`, `overfit_warnings`, `warnings`, and a compact `summary`. It is bounded by `max_combinations`, supports `random_seed`, defaults to `network=false`, and does not fetch行情, tune parameters, recommend best parameters, modify strategy defaults, change scoring formulas, or change backtest return logic.

Daily retention manifests, observation contexts, evidence packs, report audits, data-health dashboards, command-center, archive-results, and the UI may include `parameter_sensitivity_summary`. This summary is diagnostic only and does not constitute investment advice.

## v3.2.8 Data Health Dashboard and Provider Failure Stats

- `APP_VERSION`: `3.2.8-data-health-dashboard-and-provider-failure-stats`
- `API_SCHEMA_VERSION`: `3.2.8`

New local governance outputs:

- `results/operation_log/operation_timeline.jsonl`
- `results/operation_log/latest_operation_status.json`
- `results/data_health/data_health_dashboard_latest.json`
- `reports/data_health_dashboard_report_latest.md`
- `results/archive/{target_date}/archive_manifest.json`
- `results/command_center_status_latest.json`

Operation records contain command, sanitized args, target date, symbols, network/LLM flags, status, timing, output paths, warnings, failed reason, suggested next action, and version metadata. They never include API keys, provider secret contents, real account information, or `config/llm_config.json` contents. Command center reads operation logs only and does not rerun tasks, fetch providers, or call LLMs.

## v3.2.6.1 Feature Audit Empty Data Graceful Warning

- `APP_VERSION`: `3.2.6.1-feature-audit-empty-data-graceful-warning`
- `API_SCHEMA_VERSION`: `3.2.6.1`

When clean zip environments do not contain `data/daily/` or local feature rows, `feature-audit` now returns `status=warning`, `insufficient_data=true`, `skipped_reason`, and `suggested_next_action`. It does not fetch data, does not synthesize rows, and exits successfully so the user receives a data-gap report rather than a program failure.

## v3.2.6 Backtest Statistical Validation And Feature Quality Audit

- `APP_VERSION`: `3.2.6-backtest-statistical-validation-and-feature-quality-audit`
- `API_SCHEMA_VERSION`: `3.2.6`

New local-only diagnostic outputs:

- `results/feature_quality_audit_latest.json`
- `reports/feature_quality_audit_report_latest.md`
- `results/backtest_statistical_validation_latest.json`
- `reports/backtest_statistical_validation_report_latest.md`

The feature audit payload contains `status`, `row_count`, `feature_count`, per-feature `missing_rate`, `zero_rate`, `nan_count`, `inf_count`, coverage summaries, and warnings. The statistical validation payload contains `sample_size`, `permutation_test`, `bootstrap_ci`, `p_value`, confidence interval summaries, warnings, and conservative interpretation text. Both commands default to `network=false`, read local artifacts only, do not change strategy scores or backtest return logic, and do not constitute investment advice.

Daily retention manifests and observation contexts can include `feature_quality_summary` and `statistical_validation_summary`. If samples are insufficient, reports must say the sample is insufficient; if the permutation test is not passed, reports must not imply future effectiveness.

## v3.2.5 Paper Vs Backtest Consistency Check

- `APP_VERSION`: `3.2.5-paper-vs-backtest-consistency-check`
- `API_SCHEMA_VERSION`: `3.2.5`

New local-only consistency outputs:

- `results/paper_vs_backtest_consistency_latest.json`
- `reports/paper_vs_backtest_consistency_report_latest.md`

The consistency payload contains `status`, `issue_count`, `warning_count`, `issues`, `warnings`, and `summary`. It compares existing local backtest/paper/execution/validation/retention artifacts only. It does not fetch data, does not change strategy scores, does not change backtest return logic, and does not constitute investment advice.

## v3.2.4 Daily Data Retention And Reproducibility Manifest

- `APP_VERSION`: `3.2.4-daily-data-retention-and-reproducibility-manifest`
- `API_SCHEMA_VERSION`: `3.2.4`

New local-only retention outputs:

- `results/daily_retention/{target_date}/daily_retention_manifest.json`
- `results/daily_retention/{target_date}/daily_retention_summary.md`

The manifest records app/package/schema versions, target date, explicit symbols, network/LLM flags, calendar quality, command args, provider trace, strategy validation summary, input/output file records, and non-sensitive file hashes. It does not保存 API Key, provider secret contents, real account information, or broker credentials. The workflow is for reproducibility checks only and does not constitute investment advice.

## v3.2.3 Strategy Lookahead And Execution Assumption Guard

- `APP_VERSION`: `3.2.3-strategy-lookahead-and-execution-assumption-guard`
- `API_SCHEMA_VERSION`: `3.2.3`

New local-only validation outputs:

- `results/strategy_validation_latest.json`
- `reports/strategy_validation_report_latest.md`

The validation JSON contains `lookahead`, `execution_assumptions`, `calendar_info`, `issue_count`, `warning_count`, and a compact `summary`. It is diagnostic only: it does not fetch market data, does not change strategy scores, does not change backtest return logic, and does not constitute investment advice.

## v3.2.2 Core Correctness And Doc Sync

- `APP_VERSION`: `3.2.2-core-correctness-and-doc-sync`
- `API_SCHEMA_VERSION`: `3.2.2`

Calendar metadata now reports `calendar_quality`, `path`, `loaded_dates`, `start_date`, `end_date`, `weekend_only`, `coverage_warning`, and `note`. The default local calendar is `data/calendar/a_share_trading_calendar.csv`; missing or out-of-range coverage is reported honestly rather than silently pretending to be complete.

Python package metadata uses dynamic versioning from `quantdesk.version.PACKAGE_VERSION` so package installs do not drift from the app version. mootdx remains optional and best-effort; `network=false` returns skipped and missing dependency returns not_available.

## v3.2.1 Agent Evidence Guard And Report Quality Audit

- `APP_VERSION`: `3.2.1-agent-evidence-guard-and-report-quality-audit`
- `API_SCHEMA_VERSION`: `3.2.1`

Observation reports now include evidence and audit metadata. The report generator remains local-first: `--llm false` and `network=false` are the defaults, and Agent code does not call market providers. Report facts are checked against a local evidence pack extracted from `strategy_observation_context`.

Runtime outputs:

- `results/strategy_observation_context_latest.json`
- `results/strategy_observation_report_latest.md`
- `results/strategy_observation_llm_result_latest.json`
- `results/strategy_observation_report_audit_latest.json`
- optional `results/strategy_observation_evidence_pack_latest.json` when `--show-evidence true`

The LLM/report result payload adds `evidence_pack_used`, `evidence_pack_hash`, `context_hash`, `prompt_hash`, `report_hash`, `grounding_guard_status`, `grounding_score`, `unsupported_issue_count`, `unsupported_issues`, and `sanitized_by_grounding_guard`. The audit record stores safe hashes and guard statuses only; API keys and real account information are never written. Reports remain learning review output only and do not constitute investment advice.

## v3.2.0 Optional LLM Observation Agent

- `APP_VERSION`: `3.2.0-optional-llm-observation-agent`
- `API_SCHEMA_VERSION`: `3.2.0`

Optional LLM observation reports remain disabled by default. `network=false` never calls an LLM. Missing config, disabled provider, missing API key, or provider failure falls back to the offline template report.

Runtime outputs:

- `results/strategy_observation_context_latest.json`
- `results/strategy_observation_report_latest.md`
- `results/strategy_observation_llm_result_latest.json`
- `results/llm_observation_agent_smoke_latest.json`

The LLM result payload includes `provider`, `llm_used`, `fallback_used`, `failed_reason`, `skipped_reason`, `policy_guard_status`, `warnings`, and `disclaimer`. API keys are not written to results or reports. The feature is only for local learning review, does not connect to brokers, does not place real orders, and does not constitute investment advice.

## v3.1.2 Real Mootdx Provider And Fallback Hardening

- `APP_VERSION`: `3.1.2-real-mootdx-provider-and-fallback-hardening`
- `API_SCHEMA_VERSION`: `3.1.2`

`mootdx` is now a real optional provider. It remains non-blocking: missing dependency returns `not_available`, `network=false` returns `skipped`, request failures return `failed_reason`, and fallback continues to Tencent provider where applicable.

Provider smoke supports:

```powershell
python scripts/run_provider_network_smoke.py --providers tencent,mootdx --symbols 600744,002115 --target-date YYYY-MM-DD --network false
```

Smoke records `provider_group`, `provider`, `category`, `status`, `ok`, `failed_reason`, `skipped_reason`, `suggested_next_action`, `provider_chain`, `duration_ms`, `retrieved_at`, and `data_rows`. It does not cache raw market rows and does not constitute investment advice.

## v3.1.1 Real Provider Smoke And Observability

- `APP_VERSION`: `3.1.1-real-provider-smoke-and-observability`
- `API_SCHEMA_VERSION`: `3.1.1`

Provider smoke CLI:

- `python scripts/run_provider_network_smoke.py --symbols 600744,002115 --target-date YYYY-MM-DD --network false`
- `python scripts/qd.py network-smoke --symbols 600744,002115 --target-date YYYY-MM-DD --network true`

Smoke output:

- `results/provider_network_smoke_latest.json`
- `task_type`: `provider_network_smoke`
- `network`: `false` by default; only `true` performs external HTTP checks.
- `steps[]`: each step records `provider`, `category`, `symbol`, `status`, `ok`, `failed_reason`, `skipped_reason`, `warnings`, `data_rows`, `provider_chain`, `duration_ms`, and `retrieved_at`.
- Categories checked per symbol: `realtime quote`, `daily bars`, `intraday bars`, `valuation`.

Tencent provider diagnostics:

- `network_disabled`
- `http_error`
- `timeout`
- `decode_error`
- `parse_error`
- `empty_response`
- `unsupported_symbol`
- `no_rows`
- `date_out_of_scope`

Safety boundary: provider smoke is a data-availability diagnostic only. It does not connect to brokers, does not place real orders, does not scan all A-shares by default, does not fabricate minute bars from daily or realtime data, and does not constitute investment advice.

## v3.1.0 Real Tencent Provider And Entrypoint Simplification

- `APP_VERSION`: `3.1.0-real-tencent-provider-and-entrypoint-simplification`
- `API_SCHEMA_VERSION`: `3.1.0`

Tencent provider:

- `fetch_realtime_quote(symbols, network=False)`：默认跳过，不联网；`network=True` 请求 `qt.gtimg.cn` 并返回标准 quote 字段。
- `fetch_daily_bars(symbol, start_date, end_date, network=False)`：默认跳过；`network=True` 请求 `web.ifzq.gtimg.cn/appstock/app/fqkline/get` 并返回标准 OHLC。
- `fetch_intraday_bars(symbol, target_date, period="1m", network=False)`：默认跳过；历史目标日期会返回清楚的 `skipped_reason`，不会用日线或实时行情冒充分钟线。
- `fetch_valuation(symbol, network=False)`：从实时行情可用字段中提取估值相关字段，缺失字段保持空并写 warning。

Unified CLI:

- `python scripts/qd.py --help`
- `python scripts/qd.py self-check`
- `python scripts/qd.py providers --network false`
- `python scripts/qd.py daily`
- `python scripts/qd.py intraday-workbench --target-date YYYY-MM-DD`

Safety boundary: 默认不联网、不扫描全 A、不接券商、不真实下单、不构成投资建议。

## v3.0.4 Chanlun Segment Engine

- `APP_VERSION`: `3.0.4-chanlun-segment-engine`
- `API_SCHEMA_VERSION`: `3.0.4`

Safety boundary: 缠论线段识别仅用于结构学习与纸面研究，不构成投资建议，不接券商，不真实下单。线段结果不进入自动虚拟组合、条件交易计划或收益统计。

Read-only:

- `GET /api/chanlun/segments/status`
- `GET /api/chanlun/segments/latest`
- `GET /api/chanlun/segments/report/latest-html`

Run:

- `POST /api/chanlun/segments/run`

Request:

```json
{
  "symbol": "600744",
  "timeframe": "daily",
  "start_date": "2025-01-01",
  "end_date": "2026-05-15",
  "mode": "strict",
  "min_separation": 4,
  "min_bi_count": 3,
  "allow_provisional": false
}
```

Response highlights:

- `task_type`: `chanlun_segment_analysis`
- `input_summary.raw_bi_count`
- `input_summary.effective_bi_count`
- `segments[]`
- `rejected_segments[]`
- `diagnostics.segment_count`
- `diagnostics.up_segment_count`
- `diagnostics.down_segment_count`
- `disclaimer`

## v3.0.3 Chanlun Bi Engine

- `APP_VERSION`: `3.0.3-chanlun-bi-engine`
- `API_SCHEMA_VERSION`: `3.0.3`

Safety boundary: 缠论笔识别仅用于结构学习与纸面研究，不构成投资建议，不接券商，不真实下单。笔结果不进入自动虚拟组合、条件交易计划或收益统计。

Read-only:

- `GET /api/chanlun/bi/status`
- `GET /api/chanlun/bi/latest`
- `GET /api/chanlun/bi/report/latest-html`

Run:

- `POST /api/chanlun/bi/run`

Request:

```json
{
  "symbol": "600744",
  "timeframe": "daily",
  "start_date": "2025-01-01",
  "end_date": "2026-05-15",
  "mode": "strict",
  "min_separation": 4,
  "allow_provisional": false
}
```

Response highlights:

- `task_type`: `chanlun_bi_analysis`
- `input_summary.raw_fractal_count`
- `input_summary.effective_fractal_count`
- `bi_list[]`
- `rejected_bi[]`
- `diagnostics.bi_count`
- `diagnostics.up_bi_count`
- `diagnostics.down_bi_count`
- `disclaimer`

## v3.0.2 Chanlun Fractal Engine

- `APP_VERSION`: `3.0.2-chanlun-fractal-engine`
- `API_SCHEMA_VERSION`: `3.0.2`

Safety boundary: 缠论分型仅用于结构学习与纸面研究，不构成投资建议，不接券商，不真实下单。分型结果不进入自动虚拟组合，也不生成交易动作。

Read-only:

- `GET /api/chanlun/fractals/status`
- `GET /api/chanlun/fractals/latest`
- `GET /api/chanlun/fractals/report/latest-html`

Run:

- `POST /api/chanlun/fractals/run`

Request:

```json
{
  "symbol": "600744",
  "timeframe": "daily",
  "start_date": "2025-01-01",
  "end_date": "2026-05-15",
  "mode": "strict",
  "include_resolved": true
}
```

Response highlights:

- `task_type`: `chanlun_fractal_analysis`
- `input_summary.raw_bar_count`
- `input_summary.processed_bar_count`
- `include_relationship.merged_count`
- `fractals[]`
- `diagnostics.top_count`
- `diagnostics.bottom_count`
- `disclaimer`

## v3.0.1 Chanlun Knowledge Base And Rule Roadmap

- `APP_VERSION`: `3.0.1-chanlun-knowledge-base-and-rule-roadmap`
- `API_SCHEMA_VERSION`: `3.0.1`

### `GET /api/chanlun/knowledge/status`
### `GET /api/chanlun/knowledge/latest`
### `GET /api/chanlun/knowledge/report/latest-html`

Read-only Chanlun learning knowledge endpoints. GET requests only read existing
structured JSON/report files and do not parse PDF files, write outputs, connect
to networks, or trigger heavy tasks.

### `POST /api/chanlun/knowledge/build`

Builds the local Chanlun learning knowledge base. Default behavior keeps
`no_fulltext=true`, writes only lesson metadata, concept metadata, rule roadmap,
and a short Markdown summary. Response includes `lesson_count`,
`concept_count`, `programmable_count`, `implementation_status`, and disclaimer.

Safety boundary: 缠论模块仅用于学习和结构化研究，不构成投资建议，不接券商，不真实下单。买卖点仅作为观察信号，不代表买卖建议。

## v3.0.0 A-Stock Data Provider Matrix And Adapter Layer

- `APP_VERSION`: `3.0.0-a-stock-data-provider-matrix-and-adapter-layer`
- `API_SCHEMA_VERSION`: `3.0.0`

### `GET /api/a-stock-data-providers/status`
### `GET /api/a-stock-data-providers/latest`

只读返回 A 股数据源矩阵。GET 不联网、不写文件、不扫描全 A、不连接券商、不触发下载。响应包含 `providers`、`summary`、`network:false`、`full_scan:false`、`is_real_order:false` 和免责声明。

### `POST /api/a-stock-data-providers/check`

用户触发的数据源矩阵检查。默认 `network=false`，只检查依赖和本地能力；只有请求体显式传入 `network:true` 才记录用户允许联网检查。响应包含：

```json
{
  "task_type": "a_stock_data_provider_matrix",
  "network": false,
  "full_scan": false,
  "is_real_order": false,
  "providers": [],
  "summary": {},
  "disclaimer": "仅用于本地学习研究，不接券商、不真实下单、不构成投资建议。"
}
```

Provider matrix 明确：`mootdx` / 腾讯财经是行情方向优先补充，AKShare 是补充 provider，Tushare 和 Ashare 默认禁用；iwencai 需要本地 API Key 配置且默认关闭。所有 provider 失败都必须保留失败原因和下一步建议，不能把失败结果当成成功数据。

## v2.9.9 Real Intraday Data Workbench And Provider Hardening

- `APP_VERSION`: `2.9.9-real-intraday-data-workbench-and-provider-hardening`
- `API_SCHEMA_VERSION`: `2.9.9`

### `GET /api/intraday/workbench/status`
### `GET /api/intraday/workbench/latest`
### `GET /api/intraday/workbench/report/latest-html`

只读读取真实分钟线工作台最近结果或报告。GET 不联网、不导入 CSV、不执行条件计划、不更新自动虚拟组合。

### `POST /api/intraday/workbench/run`

用户触发真实分钟线工作台。请求体：

```json
{
  "target_date": "2026-05-15",
  "source": "current",
  "symbols": [],
  "try_provider": false,
  "network": false,
  "providers": [],
  "import_path": "data/intraday/import/user.csv",
  "validate_only": false,
  "audit_only": false,
  "execute_after_ready": true,
  "update_portfolio": false,
  "format": "auto",
  "encoding": "auto"
}
```

默认不联网、不扫描全 A、不更新自动虚拟组合；只有 `try_provider=true` 才尝试 provider，只有 `update_portfolio=true` 才更新本地纸面组合。响应包含 `summary`、`provider_summary`、`quality_audit`、`conditional_execution_summary`、`next_actions` 和风险声明。系统不会用日线收盘价、最高价或最低价假装盘中成交。

Current safety contract: provider-backed workbench runs require both
`try_provider:true` and explicit `network:true` or `enable_network:true`.
`try_provider:true` without network authorization returns
`status="blocked_network_disabled"` and does not start provider requests.
`dry_run:true` may keep provider checks offline with `network:false`.

## v2.9.8 Beginner Data Health And Guided Home

- `APP_VERSION`: `2.9.8-beginner-data-health-and-guided-home`
- `API_SCHEMA_VERSION`: `2.9.8`

### `GET /api/data-health/dashboard`

只读聚合首页数据健康状态。该 GET 不联网、不更新日线、不导入分钟线、不运行策略、不改变虚拟组合。响应包含：

- `health_level`: `green/yellow/red`
- `summary_text`
- `cards.daily_data`
- `cards.intraday_data`
- `cards.theme_map`
- `cards.portfolio_review`
- `beginner_next_steps`
- `top_observation_candidates`
- `risk_disclaimer`

### `POST /api/data-health/rebuild`

用户显式点击“刷新健康状态”时重新聚合并写出 `results/data_health_dashboard_latest.json`。该 POST 也不联网、不扫描全 A、不接券商、不真实下单，只保存聚合快照。

## v2.9.7 Real Intraday Provider Capability And Sample Validation

- `APP_VERSION`: `2.9.7-real-intraday-provider-capability-and-sample-validation`
- `API_SCHEMA_VERSION`: `2.9.7`
- 新增 `quantdesk/intraday_provider_capability.py` 和 `scripts/check_intraday_provider_capability.py`：探测 1 分钟线 provider 是否安装依赖、是否支持当日/历史分钟线、是否覆盖指定日期、返回行数、latest datetime、失败原因和 `skipped_reason`。
- 新增 `quantdesk/real_intraday_sample_validation.py` 和 `scripts/run_real_intraday_sample_validation.py`：默认只验证 `600744` / `002115`，provider 成功才写入 `data/intraday/1m/{symbol}.csv`，随后执行条件计划回放。
- `GET /api/intraday/provider-capability/latest`：只读读取 `results/intraday_provider_capability_latest.json`。
- `GET /api/intraday/real-sample/latest`：只读读取 `results/real_intraday_sample_validation_latest.json`。
- provider 没有返回分钟线时不会写假 CSV，不会用日线 close/high/low 代替，不会更新自动虚拟组合。
- 报告必须说明：本次是真实 provider 数据验证，不是合成自检；如果 provider 未返回分钟线，系统不会用日线价格假装成交；不接券商、不真实下单、不构成投资建议。

Current safety contract: `check_intraday_provider_capability` and
`run_real_intraday_sample_validation` default to `network:false`. External
provider checks require explicit `--network true`, `--enable-network`, or an
equivalent function argument. Offline runs keep missing minute bars visible and
do not fabricate CSV data.

## v2.9.6 Intraday Replay Self Validation Harness

- `APP_VERSION`: `2.9.6-intraday-replay-self-validation-harness`
- `API_SCHEMA_VERSION`: `2.9.6`
- 新增 `quantdesk/intraday_replay_self_validation.py`：使用合成 1 分钟线验证条件交易计划、minute close 回放、trigger_time / trigger_price、可靠成交更新组合、no_fill / missing_intraday 不改变模拟现金和组合数量。
- 新增 `scripts/run_intraday_replay_self_validation.py`：CLI 入口，支持 `--keep-artifacts true/false`、`--write-latest`、`--update-portfolio`、`--verbose`。全部场景通过时退出码为 0，否则非 0。
- `GET /api/intraday/self-validation/latest`：只读读取 `results/intraday_replay_self_validation_latest.json`，不生成文件、不联网、不写状态。
- `POST /api/intraday/self-validation/run`：运行本地合成数据自检。默认不覆盖正式 `conditional_execution_latest.json`，不写真实 `data/intraday/1m`，不覆盖 `state/auto_paper_portfolio.json`。
- 输出 `results/intraday_replay_self_validation_latest.json`、`results/conditional_execution_self_validation.json` 和 `reports/intraday_replay_self_validation_report_*.md`。
- 自检仅验证程序链路；不接券商、不真实下单、不保存真实账户信息、不构成投资建议。

## v2.9.5 Historical Intraday Replay And Trading Calendar

- `APP_VERSION`: `2.9.5-historical-intraday-replay-and-trading-calendar`
- `API_SCHEMA_VERSION`: `2.9.5`
- 新增 `quantdesk/trading_calendar.py`：本地 A 股交易日历，支持 `data/calendar/a_share_trading_calendar.csv`。缺失时使用 weekend-only 简化日历并标记 `calendar_quality="weekend_only"`。
- 新增 `quantdesk/historical_replay_engine.py`：历史分钟线 walk-forward 回放引擎。按 signal_date 生成 T+1 条件计划，用 T+1 分钟线验证触发。
- 新增 `scripts/run_historical_intraday_replay.py`：CLI 入口，支持 `--start-date`、`--end-date`、`--strategy`、`--symbols`、`--dry-run`、`--use-intraday-provider`、`--update-state`、`--max-days`、`--limit-symbols`。
- `GET /api/historical-replay/status`：只读读取 `results/historical_intraday_replay_latest.json` 摘要，不存在时返回 `exists: false`，不联网、不写文件。
- `GET /api/historical-replay/latest`：只读返回完整历史回放结果 JSON。
- `GET /api/historical-replay/report/latest-html`：只读返回最新历史回放报告的 HTML 预览。
- `POST /api/historical-replay/run`：用户触发的历史分钟线回放。请求字段包括 `start_date`、`end_date`、`strategy`、`symbols`、`only_low_price`、`initial_cash`、`use_intraday_provider`、`update_state`、`dry_run`、`max_days`、`limit_symbols`。
- POST 响应包含 `task_type: "historical_intraday_replay"`、`scope`、`network`、`full_scan: false`、`is_real_order: false`、`anti_future_leak_check`、`daily_results`、`summary`、`historical_account`、`provider_results` 和免责声明。
- 默认不联网、不更新当前自动虚拟组合、不扫描全 A。缺少分钟线时不假装成交。
- 历史回放采用 walk-forward：每一天只使用当日收盘前已知信息生成下一交易日计划，再用下一交易日分钟线验证触发。系统不会用未来数据生成历史计划，也不会用日线收盘价、最高价或最低价假装盘中成交。
- `intraday_providers.py` 新增 provider 能力字段：`supports_current_day`、`supports_historical_day`、`supported_periods`、`max_history_days`、`requires_sdk`。

Current safety contract: `POST /api/historical-replay/run` requires explicit
`network:true` or `enable_network:true` when `use_intraday_provider:true` would
start provider-backed intraday fetches. `dry_run:true` can keep provider intent
offline with `network:false`. Requests without explicit authorization return
`status="blocked_network_disabled"` and do not start provider requests.

## v2.9.4 Intraday 1m Provider Fallback

- `APP_VERSION`: `2.9.4-intraday-1m-provider-fallback`
- `API_SCHEMA_VERSION`: `2.9.4`
- `GET /api/intraday/update/status`：只读读取 `results/intraday_update_latest.json`，不存在时返回 no_data，不联网、不写文件。
- `POST /api/intraday/update`：用户触发的当前范围 1 分钟线 provider fallback。请求字段包括 `source`、`symbols`、`target_date`、`providers`、`limit`、`dry_run`、`overwrite`、`append`、`execute_after_update`、`update_portfolio_after_execution`。
- POST 响应包含 `task_type: "intraday_1m_provider_update"`、`scope`、`network: true`、`full_scan: false`、`is_real_order: false`、`provider_results`、`updated_symbol_list`、`failed_symbol_list` 和免责声明。
- 分钟线 provider 仅用于本地纸面模拟。外部数据源可能失败、延迟或缺失。系统不会在缺少分钟线时使用收盘价、最高价或最低价假装成交。

Current safety contract: `POST /api/intraday/update` requires explicit
`network:true` or `enable_network:true` before any provider-backed fetch.
`dry_run:true` remains offline and returns `network:false`. Requests without
explicit authorization return `status="blocked_network_disabled"` and do not
start provider requests.

## v2.9.3 Intraday 1m Import And Execution Closure

- `APP_VERSION`: `2.9.3-intraday-1m-import-and-execution-closure`
- `API_SCHEMA_VERSION`: `2.9.3`
- 新增只读 `GET /api/intraday/status`，返回当前条件计划、持仓和候选涉及股票的 1 分钟线缺口状态。
- 新增 `POST /api/intraday/generate-template` 和 `POST /api/intraday/import`，只在用户触发时生成导入模板或导入本地 CSV。
- 新增 `POST /api/conditional-execution/run`，只用冻结计划和本地 1 分钟线重新执行盘中回放，可选 `update_portfolio=true`。
- `conditional_execution_latest.json` 增加 `missing_intraday`、`summary.plan_count`、`summary.available_intraday_count`。缺少分钟线时不使用收盘价、最高价或最低价假装成交。
- 报告口径：完整日线控制收益复盘；分钟线控制盘中触发验证。

## v2.9.2 Shared Cleanup And Contract Enforcement

- `APP_VERSION`: `2.9.2-shared-cleanup-and-contract-enforcement`
- `API_SCHEMA_VERSION`: `2.9.2`
- 主线候选解析统一来自 `candidate_contract.candidate_rows_from_strategy_payload(...)`，`strategy_type` 不再被局部 wrapper 改写。
- `daily_workflow_latest.json.input_sources` 记录：
  - `auto_strategy`: `generated_in_memory`
  - `auto_paper_portfolio`: `strategy_payload_in_memory` 或 `latest_file_fallback`
  - `conditional_trade_plan`: `candidates_in_memory`
  - `conditional_execution`: `plan_payload_in_memory`
- `latest_file_fallback` 表示读取已有 latest 文件，并必须伴随 warning。

## v2.9.1 Shared Core And Candidate Contract

- `APP_VERSION`: `2.9.1-shared-core-and-candidate-contract`
- `API_SCHEMA_VERSION`: `2.9.1`
- Shared helpers: `quantdesk/shared.py`
- Candidate contract: `quantdesk/candidate_contract.py`

Standard candidate rows include:

```json
{
  "symbol": "600744",
  "name": "华银电力",
  "strategy_type": "board",
  "strategy_label": "打板",
  "candidate_source": "auto_strategy_sandbox",
  "rank": 1,
  "score": 88.2,
  "sector": "公用事业",
  "industry": "电力",
  "theme": "绿色电力",
  "primary_theme": "绿色电力",
  "data_latest_date": "2026-05-15",
  "is_target_date_covered": true,
  "data_freshness_label": "目标日期候选",
  "raw": {}
}
```

Daily workflow steps now expose `input_source`:

- `in_memory`: uses objects generated in the current run.
- `latest_file_fallback`: reads an existing latest JSON and emits a warning.
- `skipped` / `unavailable`: no usable current input.

`paper_trading_latest.json` is legacy/manual observation data. The main automatic workflow uses `state/auto_paper_portfolio.json` and `results/auto_paper_portfolio_latest.json`.

## v2.9.0 Data Freshness Gate And Current Candidate Theme Sync

- `APP_VERSION`: `2.9.0-data-freshness-gate-and-current-candidate-theme-sync`
- `API_SCHEMA_VERSION`: `2.9.0`
- New gate: `quantdesk/data_freshness_gate.py`
- Current-candidate theme validation: `quantdesk/theme_current_candidate_validator.py`
- Daily fallback CLI now supports `--symbols 600744,600958` and reports actual symbol lists.

`GET /api/data-freshness/status`

Read-only. It checks local JSON/CSV status only. It does not connect to external providers, write files, rebuild features, run strategy tasks, or update portfolio state.

Response includes:

```json
{
  "target_complete_trade_date": "2026-05-15",
  "local_daily_latest_date": "2026-05-14",
  "daily_feature_date": "2026-05-14",
  "is_daily_stale": true,
  "can_review_yesterday_positions": false,
  "can_update_portfolio_valuation": false,
  "can_verify_intraday_triggers": false,
  "review_mode": "stale_observation",
  "portfolio_review_valid": false,
  "intraday_review_valid": false
}
```

`POST /api/daily-price/update`

Request now also accepts:

```json
{
  "symbols": "600744,600958,002115",
  "rebuild_features": true
}
```

Response now includes:

```json
{
  "processed_symbols": ["600744"],
  "updated_symbol_list": ["600744"],
  "unchanged_symbol_list": [],
  "failed_symbol_list": [],
  "process_note": "本次按 --symbols 仅处理以下股票：600744。",
  "provider_results": [
    {
      "provider": "current_project_default",
      "attempted_symbols": ["600744"],
      "updated_symbol_list": ["600744"],
      "failed_symbol_list": []
    }
  ]
}
```

PowerShell users should read UTF-8 JSON/Markdown with `Get-Content -Encoding UTF8` when the console code page displays Chinese incorrectly.

## v2.8.9 Daily Price Provider Fallback Engine

- `APP_VERSION`: `2.8.9-daily-price-provider-fallback-engine`
- `API_SCHEMA_VERSION`: `2.8.9`
- New engine: `quantdesk/daily_price_update_engine.py`
- New CLI: `python scripts/update_daily_price_fallback.py --dry-run`

`GET /api/daily-price/status`

Read-only. It does not connect to external providers, write `data/daily`, rebuild features, or run the workflow.

`POST /api/daily-price/update`

Request:

```json
{
  "only_low_price": true,
  "target_date": "",
  "providers": ["current_project_default", "akshare_daily", "ashare_daily"],
  "workers": 4,
  "dry_run": false
}
```

Response includes `target_date`, `latest_local_date_before`, `latest_local_date_after`, `updated_symbols`, `failed_symbols`, `provider_results`, `warnings`, and `path`.

Daily workflow `POST /api/daily-workflow/run` accepts:

```json
{
  "allow_daily_price_online_update": false
}
```

The default is false: the workflow diagnoses stale daily data but does not update it unless the user explicitly allows it.

## v2.8.8 Reference Data Skill Adapter

- `APP_VERSION`: `2.8.8-reference-data-skill-adapter`
- `API_SCHEMA_VERSION`: `2.8.8`
- New registry: `quantdesk/data_provider_registry.py`
- New CLI: `python scripts/check_data_providers.py`

`GET /api/data-providers/status`

Read-only provider diagnostics. It does not connect to external data sources, generate files, run updates, scan all A shares, or change portfolio state.

`POST /api/data-providers/check`

Request:

```json
{
  "check_network": false
}
```

Response:

```json
{
  "ok": true,
  "schema_version": "2.8.8",
  "check_network": false,
  "providers": [
    {
      "name": "ashare_daily",
      "type": "daily_price_provider",
      "network_required": true,
      "status": "unavailable",
      "last_error": "missing optional dependency: Ashare"
    }
  ],
  "summary": {}
}
```

Provider status values include `available`, `unavailable`, `unknown`, and planned placeholders (`planned` with `placeholder=true`). Missing optional dependencies such as `Ashare` or `mootdx` must not crash the workflow.

## v2.8.7 Theme Map Import Draft Generator

- `APP_VERSION`: `2.8.7-theme-map-import-draft-generator`
- `API_SCHEMA_VERSION`: `2.8.7`
- New draft output: `data/universe/stock_theme_map_import.csv`
- New CLI: `python scripts/generate_stock_theme_map_import.py --source current`

`POST /api/theme-map/generate-import-draft`

Request:

```json
{
  "source": "current",
  "overwrite": false,
  "limit": 0
}
```

Response:

```json
{
  "ok": true,
  "path": "data/universe/stock_theme_map_import.csv",
  "total": 5,
  "filled_count": 3,
  "pending_manual_count": 2,
  "message": "已生成板块/题材导入草稿。"
}
```

`GET /api/theme-map/import-draft/preview` is read-only and never generates files, imports mappings, connects to brokers, places orders, or triggers a workflow. `POST /api/theme-map/import` imports the draft into the formal `stock_theme_map.csv` using the existing local import path.

## v2.8.6 Unified Workflow Result And Theme Provider Fallback

- `APP_VERSION`: `2.8.6-unified-workflow-result-and-theme-provider-fallback`
- `API_SCHEMA_VERSION`: `2.8.6`
- `quantdesk/theme_map_providers.py`: provider fallback layer for theme/sector mapping.
- `results/daily_workflow_latest.json`: always written by `POST /api/daily-workflow/run`, including partial or failed results.

Daily workflow payload:

```json
{
  "ok": true,
  "run_id": "daily_workflow_20260515_131500",
  "generated_at": "2026-05-15 13:15:00",
  "workflow_status": "completed",
  "steps": [
    {
      "name": "theme_map_status",
      "status": "warning",
      "message": "Theme map unavailable; continuing local review.",
      "started_at": "...",
      "finished_at": "...",
      "outputs": {},
      "error": ""
    }
  ],
  "summary": {
    "report_generated_at": "...",
    "local_daily_latest_date": "2026-05-14",
    "daily_feature_date": "2026-05-14",
    "theme_mapped_count": 0,
    "theme_unknown_count": 80,
    "theme_failed_reason": "external provider failed",
    "long_candidate_count": 0,
    "board_candidate_count": 0,
    "conditional_plan_count": 0,
    "execution_event_count": 0,
    "no_fill_count": 0,
    "missing_intraday_count": 0,
    "portfolio_position_count": 0,
    "today_pnl": 0,
    "total_return_pct": 0
  },
  "warnings": [],
  "errors": [],
  "artifacts": {}
}
```

Theme-map provider status:

```json
{
  "mapped_count": 12,
  "unknown_count": 68,
  "pending_count": 68,
  "provider_results": [
    {"provider": "local", "status": "warning", "mapped": 0, "failed_reason": "local fields unavailable"},
    {"provider": "akshare_em", "status": "failed", "mapped": 0, "failed_reason": "RemoteDisconnected"},
    {"provider": "per_symbol_info", "status": "ok", "mapped": 12, "failed_reason": ""}
  ]
}
```

New or clarified endpoints:

- `GET /api/daily-workflow/status`: read-only status; never generates files, updates portfolios, scans full A, or networks.
- `GET /api/daily-workflow/latest`: read-only latest workflow JSON.
- `POST /api/daily-workflow/run`: user-triggered workflow run. Default `allow_theme_map_online_update=false`.
- `POST /api/theme-map/import`: imports `data/universe/stock_theme_map_import.csv`; GET never imports.
- `POST /api/theme-map/update`: user-triggered local or online theme-map update. Online provider calls are never made during page initial load.

## v2.8.5 Unified Workflow And Theme Map Fix

- `APP_VERSION`: `2.8.5-unified-workflow-and-theme-map-fix`
- `API_SCHEMA_VERSION`: `2.8.5`
- `quantdesk/daily_workflow_orchestrator.py`: orchestrates the local daily review workflow without networking by default.
- `scripts/run_daily_workflow.py`: CLI entry for the same workflow.
- Workflow output: `results/daily_workflow_latest.json`.
- Workflow report: `reports/daily_workflow_report_*.md`.

### Daily workflow read-only endpoints

- `GET /api/daily-workflow/status`
- `GET /api/daily-workflow/latest`
- `GET /api/daily-workflow/report/latest-html`

These endpoints only read existing status/report files. They do not build features, run the strategy sandbox, update portfolios, scan all A-shares, or connect to the network.

### Daily workflow user-triggered endpoint

- `POST /api/daily-workflow/run`

Default request behavior: `strategy=all`, `allow_auto_init=false`, `enable_intraday=true`, `allow_daily_approx=false`, `max_long_total=20`, `max_board_total=10`, and `allow_theme_map_online_update=false`.

The workflow checks or runs, in order: low-price learning pool, local daily data status, low-price report, daily features, theme/sector map status, auto strategy sandbox, auto paper portfolio state, conditional trade plan, intraday execution, portfolio ledger update, and unified report generation. It defaults to `scope=low_price_included_only`, `network=false`, and `full_scan=false`.

### Theme map diagnostics

Theme map status now returns `mapped_count`, `unknown_count`, `pending_count`, `manual_count`, `local_valid_count`, `akshare_count`, `failed_reason`, `last_update_source`, and `updated_at`.

`mapped_count` only includes rows with real sector, industry, primary theme, concept tags, or theme tags. Rows with `source=local` but all fields unknown remain pending and do not count as mapped. Akshare missing, interface failures, empty responses, and unmatched symbols are reported through `failed_reason` instead of crashing the API.

`nan`, `NaN`, `None`, and empty strings are sanitized before preview/UI display. The tool still does not connect to brokers, does not place real orders, and does not provide investment advice.

## v2.8.4 Conditional Position Management Engine

- `APP_VERSION`: `2.8.4-conditional-position-management-engine`
- `API_SCHEMA_VERSION`: `2.8.4`
- `quantdesk/conditional_trade_plan.py`: builds frozen T+1 conditional plans from local T-day data only.
- `quantdesk/paper/position_management_engine.py`: replays frozen plans against local `data/intraday/1m/{symbol}.csv` using minute close by default.
- Plan output: `results/conditional_trade_plans_latest.json`.
- Execution output: `results/conditional_execution_latest.json`.
- Portfolio output remains `state/auto_paper_portfolio.json`, `results/auto_paper_portfolio_latest.json`, and `results/auto_paper_portfolio_history.json`.

### Read-only endpoints

- `GET /api/conditional-trade-plans/status`
- `GET /api/conditional-trade-plans/preview?limit=20`
- `GET /api/conditional-trade-plans/latest`
- `GET /api/conditional-execution/latest`

These endpoints only read existing files. They do not create plans, execute simulation, update a portfolio, scan all A-shares, or connect to the network.

### User-triggered endpoints

- `POST /api/conditional-trade-plans/build`
- `POST /api/conditional-trade-plans/simulate`
- `POST /api/auto-paper-portfolio/update-from-plan`
- `POST /api/auto-paper-portfolio/update`
- `POST /api/auto-paper-portfolio/run`

POST responses include `task_type=conditional_position_management`, `scope=low_price_included_only`, `network=false`, `full_scan=false`, and `user_triggered=true`.

### Execution boundary

- New candidates can become `buy`, `block`, or `no_fill`; they are not bought at the close by default.
- Existing positions can become `add`, `breakout_add`, `reduce_half`, `take_profit`, `sell_all`, `stop_loss`, `watch_exit`, `hold`, `no_fill`, or `block`.
- Missing intraday data never creates a fake fill. Daily close can be used for valuation, not as a reliable trade fill.
- The tool does not connect to brokers, does not place real orders, and does not provide investment advice.

## v2.7.8 Intraday Swing Execution Engine

- `APP_VERSION`: `2.7.8-intraday-swing-execution-engine`
- `API_SCHEMA_VERSION`: `2.7.8`
- `quantdesk/intraday_swing_strategy.py`: `build_swing_plan` generates pullback_buy / breakout_buy / take_profit / stop_loss zones
- `quantdesk/intraday_execution.py`: `simulate_intraday_plan` replays minute bars chronologically
- Plans use T-day data only; execution uses T+1 intraday data
- Missing intraday data → `is_reliable=false`, no fake fills
- `scripts/run_auto_paper_portfolio.py --mode update --enable-intraday-swing` writes `results/swing_trade_plans_latest.json`, `results/intraday_execution_latest.json`, and `reports/swing_execution_report_*.md`
- `GET /api/swing-plans/latest`: read-only latest swing plan payload
- `GET /api/intraday-execution/latest`: read-only latest intraday execution payload
- `GET /api/swing-execution/report/latest-html`: browser preview / print-to-PDF HTML page
- Not completed in v2.7.8: board-strategy intraday fillability, theme/sector heat grouping, and news heat.
- No broker, no real orders, no investment advice

## v2.7.6 Workflow Unification Auto Portfolio

- `APP_VERSION`: `2.7.6-workflow-unification-auto-portfolio`
- `API_SCHEMA_VERSION`: `2.7.6`

### New endpoints

- `GET /api/auto-paper-portfolio/latest` — read-only, returns the latest auto paper portfolio state. Does not trigger computation.
- `POST /api/auto-paper-portfolio/run` — runs init or update on the auto paper portfolio. Only executes on explicit POST.
- `GET /api/auto-paper-portfolio/artifacts` — returns list of auto paper portfolio output files with paths and existence status.
- `GET /api/auto-paper-portfolio/report/latest-html` — returns text/html page for browser preview / print PDF.

### Boundaries

- No broker API, no real orders, no full-A default scan, no investment advice.
- Default quantity per stock: 100 shares (fixed_100 mode).
- Each action (buy/hold/sell/block) carries a structured reason.

## v2.7.5 Auto Paper Fixed 100 Sizing

- `APP_VERSION`: `2.7.5-auto-paper-fixed-100-sizing`
- `API_SCHEMA_VERSION`: `2.7.5`

`POST /api/auto-strategy/run` accepts `sizing_mode`:

- `fixed_100` (default): each new paper candidate uses 100 shares. `max_position_pct` is only a single-position risk cap.
- `percent_cap`: advanced mode that derives quantity from `initial_cash * max_position_pct / price`, rounded down to 100-share lots.

This remains local paper simulation only. It does not connect to brokers, does not place real orders, and does not constitute investment advice.

## v2.7.4 Auto Strategy Sandbox and Workbench Polish

- `APP_VERSION`: `2.7.4-auto-strategy-sandbox-and-workbench-polish`
- `API_SCHEMA_VERSION`: `2.7.4`

### `POST /api/auto-strategy/run`

Runs the local automatic strategy sandbox on explicit user request. It reads the low-price learning universe and local daily cache, creates long / board paper-simulation candidates, writes local pending orders, and generates review artifacts. It does not connect to brokers and does not place real orders.

### `GET /api/auto-strategy/latest`

Read-only endpoint for `results/auto_strategy_latest.json`. It never starts the sandbox.

### `GET /api/auto-strategy/report/latest-html`

Returns a friendly HTML preview for browser print / save-as-PDF. If no sandbox result exists, it still returns a normal HTML page explaining that the sandbox has not been run.

### `GET /api/auto-strategy/artifacts`

Read-only artifact list for auto-strategy JSON / Markdown / pending-order files.

All auto-strategy outputs are local paper-trading artifacts only and do not constitute investment advice.

## v2.7.3 Low Price Workbench Schema and Defaults

## v2.7.3 Low Price Workbench

QuantDesk v2.7.3 keeps the existing low-price report and paper-trading APIs unchanged. It reorganizes the UI so the low-price workflow is a top-level “低价工作台” instead of being embedded inside the ordinary report-data panel.

- `APP_VERSION`: `2.7.3-low-price-workbench-schema-and-defaults`
- `API_SCHEMA_VERSION`: `2.7.3`
- `/api/results` remains the ordinary daily report payload and does not trigger low-price report generation.
- The low-price workbench uses existing read-only summary/latest endpoints on initial load.
- Explicit button clicks are still required for low-price pool build, low-price daily update, report generation, and paper-simulation runs.
- No broker API, no real order placement, no prediction, and no investment advice are introduced.

## v2.7.2 Report UI Stability and Paper Simulation

QuantDesk v2.7.2 keeps `/api/results` unchanged. It adds a local paper-trading simulator CLI and enhances the low-price report with liquidity / staleness fields.

- `APP_VERSION`: `2.7.2-report-ui-stability-and-paper-simulation`
- `API_SCHEMA_VERSION`: `2.7.2`

### New endpoints

- `GET /api/paper-trading/latest` — read-only, returns the latest paper-trading simulation result from `results/paper_trading_latest.json`. Does not trigger a run.
- `POST /api/paper-trading/run` — runs the paper-trading simulator with the supplied parameters. Only executes on explicit POST.

### Low-price report enhancements

- `summary` gains: `stale_count`, `up_to_date_count`, `liquidity_ok_count`, `liquidity_low_count`, `liquidity_very_low_count`, `liquidity_unknown_count`, `expected_trade_date`, `latest_complete_daily_date`.
- Each `item` gains: `amount_ma20`, `volume_ma20`, `liquidity_status`, `liquidity_note`, `is_stale`, `stale_reason`, `expected_trade_date`, `latest_date`.

### Boundaries

- No broker API, no real orders, no full-A default scan, no investment advice.
- Paper-trading results are local-only virtual simulations.

## v2.7.1 Low Price Report UI Stability

QuantDesk v2.7.1 keeps `/api/results` and the ordinary daily report payload unchanged. It adds read-only APIs for viewing the low-price learning cache report after it has been generated.

- `APP_VERSION`: `2.7.1-low-price-report-ui-stability`
- `API_SCHEMA_VERSION`: `2.7.1`

### `GET /api/learning-universe/low-price/report/latest`

Reads `results/low_price_learning_report_latest.json` only. It does not generate a report, does not connect to the network, and does not scan full A.

Missing report:

```json
{ "ok": true, "exists": false, "message": "尚未生成低价学习池本地报告，请先点击生成报告" }
```

Existing report:

```json
{
  "ok": true,
  "exists": true,
  "result_path": "results/low_price_learning_report_latest.json",
  "report_path": "reports/low_price_learning_report_20260512_153000.md",
  "generated_at": "2026-05-12T15:30:00",
  "summary": {
    "included_count": 80,
    "analyzed_count": 80,
    "readable_count": 70,
    "missing_cache_count": 4,
    "insufficient_history_count": 6
  }
}
```

### `GET /api/learning-universe/low-price/report/latest-markdown`

Returns the latest Markdown report content when available. It is read-only.

### `GET /api/learning-universe/low-price/report/latest-html`

Returns a UTF-8 HTML preview page containing the Markdown text in a print-friendly layout with a "打印 / 另存为 PDF" button. It uses browser printing and does not add any backend PDF dependency.

The low-price report remains separate from the ordinary daily candidate list. Generating or viewing it does not overwrite `/api/results`.

## v2.7.0 Low Price Cache Report

QuantDesk v2.7.0 keeps existing v2.6.x API paths and response fields compatible. It adds one local-only report generation endpoint for the low-price learning universe.

- `APP_VERSION`: `2.7.0-low-price-cache-report`
- `API_SCHEMA_VERSION`: `2.7.0`
- `schema_version` in `/api/results` now reports `2.7.0`.

### `POST /api/learning-universe/low-price/generate-report`

Generates a lightweight report from `data/universe/low_price_learning_symbols.csv` and local `data/daily/{symbol}.csv` files. The endpoint does not connect to AKShare, Sina, Eastmoney, brokers, or any network. It does not scan the full A-share universe.

Optional JSON body:

```json
{
  "limit": 20,
  "min_rows": 120,
  "universe_csv": "data/universe/low_price_learning_symbols.csv"
}
```

Success response shape:

```json
{
  "ok": true,
  "message": "Low-price local cache report generated.",
  "report_type": "low_price_cache_report",
  "files": [
    "reports/low_price_learning_report_20260512_153000.md",
    "results/low_price_learning_report_latest.json"
  ],
  "notice": "只读取低价学习池和本地日线缓存，不联网，不扫描全 A，不构成投资建议。"
}
```

If the low-price universe has not been built, the endpoint returns `ok=false` with a CLI fallback. This endpoint is read-only for market data: it only writes report artifacts.

## v2.6.9 Low Price Universe Daily Update

QuantDesk v2.6.9 keeps every existing API path, request body, and response field compatible with v2.6.8. The release only adds two endpoints behind `/api/learning-universe/low-price/update-daily/`.

- `APP_VERSION`: `2.6.9-low-price-universe-daily-update`
- `API_SCHEMA_VERSION`: `2.6.9`
- `schema_version` in `/api/results` now reports `2.6.9`. Frontends keyed on `2.6.x` continue to work without changes.
- `data/cache/update_status_running.json` adds a new `mode` field set from `--update-mode-tag`. Legacy fields `update_mode`, `universe_mode`, `total_symbols`, `processed_count`, `failed_count`, `running` are unchanged; the new `mode` field falls back to `universe_mode` when no wrapper script sets it.

### `GET /api/learning-universe/low-price/update-daily/summary`

Read-only summary used by the UI to render the daily-update entry point. Never starts a task, never reads `data/daily`, never connects to the network.

When the universe CSV does not exist:

```json
{
  "ok": true,
  "exists": false,
  "universe_path": "data/universe/low_price_learning_symbols.csv",
  "included_count": 0,
  "lock_held": false,
  "message": "低价学习池尚未构建，请先构建低价学习池 / low-price learning universe not built yet"
}
```

When the universe CSV exists:

```json
{
  "ok": true,
  "exists": true,
  "universe_path": "data/universe/low_price_learning_symbols.csv",
  "included_count": 80,
  "lock_held": false,
  "message": "低价学习池包含 80 只 included=true 股票，可执行日线更新 / 80 included symbols available"
}
```

### `POST /api/learning-universe/low-price/update-daily`

Starts the wrapper script `scripts/update_low_price_universe_daily.py` under the existing `data/cache/update_market_data.lock`. Optional JSON body fields: `workers` (1-8, default 4), `limit`, `dry_run`, `rebuild_cache_index`, `universe_csv`.

Refusal envelopes (no task started):

```json
{ "ok": false, "started": false, "message": "低价学习池尚未构建，请先构建低价学习池 / low-price learning universe not built yet" }
```

```json
{ "ok": false, "started": false, "message": "已有市场数据更新任务正在运行，请勿重复点击 / another update_market_data task is already running", "lock": { ... } }
```

```json
{ "ok": false, "started": false, "included_count": 0, "message": "低价学习池中没有 included=true 的股票，未启动更新 / no included symbols, nothing to update" }
```

Success envelope (task finished — `run_generation_command` blocks until the wrapper script returns):

```json
{
  "ok": true,
  "started": true,
  "universe_path": "data/universe/low_price_learning_symbols.csv",
  "included_count": 80,
  "workers": 4,
  "update_status": { "..." : "..." },
  "message": "已完成低价学习池日线更新，included_count=80 / low-price universe daily update finished"
}
```

### Wrapper script CLI

```
python scripts/update_low_price_universe_daily.py \
  --workers 4 \
  [--limit 20] \
  [--dry-run] \
  [--rebuild-cache-index] \
  [--universe-csv data/universe/low_price_learning_symbols.csv]
```

Exit codes: `0` on success / empty-included / dry-run; `2` when the universe CSV is missing; `1` when the underlying update reports failures.

## v2.6.8 Low Price Snapshot Universe

QuantDesk v2.6.8 keeps every existing API path, request body, and response field compatible with v2.6.7. The release only adds two endpoints behind `/api/learning-universe/low-price/`. Both endpoints are strictly offline: they never read `data/daily/*.csv` and never reach the network.

- `APP_VERSION`: `2.6.8-low-price-snapshot-universe`
- `API_SCHEMA_VERSION`: `2.6.8`
- `schema_version` in `/api/results` now reports `2.6.8`. Frontends keyed on `2.6.x` continue to work without changes.

### `GET /api/learning-universe/low-price/summary`

Read-only summary of `data/universe/low_price_learning_symbols.csv`.

When the universe CSV has not been built yet:

```json
{
  "ok": true,
  "exists": false,
  "message": "低价学习池尚未构建 / low-price learning universe has not been built yet",
  "output_path": "data/universe/low_price_learning_symbols.csv",
  "max_price": 20.0,
  "buffer_price": 22.0
}
```

When the universe CSV exists:

```json
{
  "ok": true,
  "exists": true,
  "message": "low-price learning universe summary",
  "output_path": "data/universe/low_price_learning_symbols.csv",
  "summary": {
    "total": 4801,
    "included_count": 1234,
    "buffer_count": 89,
    "excluded_count": 3478,
    "exclusion_reasons": { "above_buffer": 3470, "excluded_name": 5, "invalid_price": 3 },
    "max_price": 20.0,
    "buffer_price": 22.0,
    "min_amount": 0.0,
    "updated_at": "2026-05-12 03:21:52",
    "output_path": "data/universe/low_price_learning_symbols.csv"
  }
}
```

### `POST /api/learning-universe/low-price/build`

Rebuilds the universe CSV from `data/cache/spot_snapshot_latest.csv`. Optional JSON body fields: `max_price`, `buffer_price`, `min_amount`. Returns the same `summary` shape on success and a clear bilingual error on missing spot cache:

```json
{
  "ok": false,
  "exists": false,
  "message": "spot snapshot cache 不存在，请先刷新行情快照或生成样例数据。 / spot snapshot cache not found; please refresh the market snapshot first.",
  "spot_csv": "data/cache/spot_snapshot_latest.csv"
}
```

### Output CSV columns (`data/universe/low_price_learning_symbols.csv`)

`symbol`, `name`, `price`, `amount`, `included`, `in_buffer`, `excluded_reason`, `source`, `updated_at`. UTF-8 with BOM. Safe to delete and rebuild.

## v2.6.7 Stability Patch

QuantDesk v2.6.7 keeps every API path, request body, and response field compatible with v2.6.6. The release adds a duplicate-update guard on top of `POST /api/update-market-data` and is otherwise behavior-preserving.

- `APP_VERSION`: `2.6.7-stability-patch`
- `API_SCHEMA_VERSION`: `2.6.7`
- `schema_version` in `/api/results` now reports `2.6.7`. Frontends keyed on `2.6.x` continue to work without changes.
- `POST /api/update-market-data` now returns `{ "ok": false, "message": "已有市场数据更新任务正在运行 / another update_market_data task is already running", "lock": { ... } }` when `data/cache/update_market_data.lock` is held by a live process. The lock payload includes `pid`, `started_at`, `command`, `hostname`, `epoch`.
- `GET /api/update-market-data/progress` is unchanged. The UI checks this endpoint before any update button click and aborts with a bilingual notice when `running` is `true`.
- Indicator math in `quantdesk/indicators.py` is numerically stable on `pd.NA` / object dtype input; no schema change.

## v2.6.6 Daily Cache Index

QuantDesk v2.6.6 keeps existing report payload fields compatible while adding a lightweight local index summary for `data/daily/*.csv`.

- `APP_VERSION`: `2.6.6-daily-cache-index`
- `API_SCHEMA_VERSION`: `2.6.6`
- `data/cache/daily_cache_index.json` is a local acceleration layer only; CSV files remain the authoritative source.
- `scripts/update_market_data.py` may use the index to skip already-fresh symbols without re-reading every CSV.
- `GET /api/daily-cache-index/summary` returns a read-only summary and never triggers an update.

### GET /api/daily-cache-index/summary

No request body. Example response:

```json
{
  "ok": true,
  "exists": true,
  "message": "daily cache index summary",
  "index_path": "data/cache/daily_cache_index.json",
  "symbol_count": 5513,
  "latest_indexed_at": "2026-05-12T09:30:00",
  "latest_date_distribution": {
    "2026-05-10": 4800,
    "2026-05-08": 713
  },
  "data_grade_distribution": {
    "BACKTEST_READY": 4200,
    "RESEARCH_READY": 900,
    "SCORABLE": 413
  },
  "min_date": "2015-01-05",
  "max_date": "2026-05-10"
}
```

The summary is informational only. It does not scan the daily CSV directory and does not write any files.

## v2.6.5 Incremental Parallel Progress

QuantDesk v2.6.5 keeps existing report payload fields compatible while adding a read-only progress contract for market-data updates.

- `APP_VERSION`: `2.6.5-incremental-parallel-progress`
- `API_SCHEMA_VERSION`: `2.6.5`
- `scripts/update_market_data.py --workers N` accepts `1..8`; `1` is the default and preserves serial behavior.
- `data/cache/update_status_running.json` stores lightweight running progress, not full update results.
- `GET /api/update-market-data/progress` returns progress only and never triggers an update, CSV scan, report generation, or network call.

### GET /api/update-market-data/progress

No request body. Example response:

```json
{
  "ok": true,
  "running": true,
  "processed_count": 120,
  "total_symbols": 5513,
  "success_count": 80,
  "skipped_count": 38,
  "failed_count": 2,
  "already_fresh_count": 38,
  "current_symbol": "600000",
  "last_symbol": "000001",
  "last_outcome": "cache_hit",
  "workers": 4,
  "progress_percent": 2.18
}
```

If no update is running, the endpoint returns `ok: true`, `running: false`, and a friendly message. If the running file is unreadable, it returns `ok: false`, `running: false`, and does not start any task.

## v2.6.4 Data Health Recorder Split

QuantDesk v2.6.4 keeps every API path, request body, and response field identical to v2.6.3. The release focuses on moving data-health recording helpers out of `scripts/daily_quant_report.py` and into a dedicated module.

- `APP_VERSION`: `2.6.4-data-health-recorder-split`
- `API_SCHEMA_VERSION`: `2.6.4`
- `schema_version` in `/api/results` now reports `2.6.4`. Frontends keyed on `2.6.x` continue to work without changes.
- New module `quantdesk/data_health_recorder.py` hosts the `DataFetchStatus` dataclass, `DATA_HEALTH_ROWS` / `DATA_HEALTH_COLUMNS`, and every data-health helper. `scripts/daily_quant_report.py` keeps all moved names as thin re-exports for backwards compatibility.
- No change to the `data_health_rows`, `data_health_summary`, or `data_health_reason_summary` payload fields that already shipped in earlier releases.

Data Health Recorder Split：抽出 data_health_recorder.py，daily_quant_report.py 保留兼容 shim，API / CLI / JSON 不变。

## v2.6.3 Codebase Slimming

QuantDesk v2.6.3 keeps every API path, request body, and response field identical to v2.6.2. The release focuses on codebase slimming and shared helpers; no API contract changes.

- `APP_VERSION`: `2.6.3-codebase-slimming`
- `API_SCHEMA_VERSION`: `2.6.3`
- `schema_version` in `/api/results` now reports `2.6.3`. Frontends keyed on `2.6.x` continue to work without changes.
- New module `quantdesk/server_payloads.py` re-exports stateless request normalizers for watchlist, manual-confirmation queue, and live-monitor calls. Endpoint paths and JSON shapes are unchanged.
- New module `quantdesk/self_check_core.py` hosts pure helpers used by `scripts/self_check.py`. CLI behavior and PASS / WARN / FAIL semantics are unchanged.

## v2.6.2 Release Hardening

QuantDesk v2.6.2 keeps API behavior compatible while hardening release wording and lightweight status display.

- `APP_VERSION`: `2.6.2-release-hardening`
- `API_SCHEMA_VERSION`: `2.6.2`
- `report_run_status.sample_count` means the current report sample size only; it is not the full A-share universe count.
- `global_history_status.csv_count` / `global_history_status.universe_total` describe the local historical data library and stock universe.
- `incremental_update_status.already_fresh_count` tracks symbols skipped because local daily data is already fresh.
- Walk-forward quality values such as `LOW` / `INVALID` should be shown with sample-bias and historical-data warnings.

## v2.6.1 Stability Cleanup

QuantDesk v2.6.1 keeps the API surface stable while centralizing version metadata in `quantdesk/version.py`.

- `APP_VERSION`: `2.6.1-stability-cleanup`
- `API_SCHEMA_VERSION`: `2.6.1`
- No new business capability is added in this cleanup release.

## v2.6.0 Intraday Strategy Monitor

`/api/results` and the intraday-monitor endpoints now expose local intraday monitoring status for observation only:

- `quantdesk/intraday_monitor.py` drives the local monitor loop.
- `/api/intraday-monitor/start`
- `/api/intraday-monitor/stop`
- `/api/intraday-monitor/status`
- `/api/intraday-monitor/cycle`

The monitor tracks local polling status, trigger summaries, and manual-review signals. It does not connect to brokers, does not automate orders, does not promise outcomes, and does not constitute investment advice.

这些字段用于前端展示“观察、检查、风险提示”和报告审计信息，属于辅助决策材料，不构成投资建议，也不承诺结果。

## 顶层元信息

所有 `/api/results` payload 顶层都包含以下元信息，方便前端判断数据来源和兼容 schema：

- `schema_version`: API payload schema 版本。
- `generated_at`: payload 生成时间，统一使用 `Asia/Shanghai`。
- `data_mode`: `sample` / `report` / `empty`，表示样例数据、正式报告或尚未加载。
- `report_date`: 报告数据日期；没有报告时为空字符串。

## report_sections

报告章节状态列表。

- `name`: 章节名称。
- `status`: `available` / `missing` / `not generated`。
- `count`: 可选，章节内项目数量，例如候选决策卡片数量。

## daily_brief

今日操作简报。

- `status`: `available` / `not_available`。
- `market_temperature`: 市场温度摘要。
- `data_quality_status`: 数据质量状态。
- `backtest_risk_status`: 回测风险状态。
- `candidate_count`: 候选股数量。
- `high_risk_candidate_count`: 高风险候选数量。
- `suggested_mode`: 保守模式标签，例如“只观察”“条件满足后再评估”。
- `disclaimer`: 辅助检查声明。

## candidate_cards

候选股结构化决策卡片数组。

- `code`: 股票代码。
- `name`: 股票名称。
- `action_label`: 保守状态标签。
- `confidence_note`: 分数和判断限制说明。
- `why_watch`: 关注原因列表。
- `key_risks`: 主要风险列表。
- `data_status`: 数据状态。
- `pre_trade_checklist`: 操作前检查清单。
- `invalid_conditions`: 策略失效条件。
- `stop_loss_note`: 止损参考说明。
- `position_note`: 仓位和位置提示。
- `do_not_trade_if`: 不适合参与的条件。
- `review_note`: 辅助检查声明。

## learning_appendix

学习附录结构。

- `enabled`: 是否启用。
- `items`: 学习条目数组。
- `items[].title`: 条目标题。
- `items[].content`: 条目说明。

## risk_dashboard

聚合风险仪表盘。

- `data_quality`: 数据健康摘要。
- `backtest_risk`: 回测风险摘要原始指标。
- `blocked_trades`: blocked trade 原因统计。
- `industry_concentration`: 行业集中度提示。
- `turnover_limit`: 换手率限制提示。
- `warnings`: 风险提示列表。

## backtest_summary_rows

回测 summary CSV 的行数组。

- `metric`: 指标名。
- `value`: 指标值。

## backtest_assumptions

回测假设 JSON。

- 包含信号时点、成交时点、费用、滑点、涨跌停、停牌、组合限制、报告配置等字段。

## backtest_audit_files

回测审计文件字典。

- `trades`
- `equity_curve`
- `blocked_trades`
- `positions`
- `rebalance_log`
- `summary`
- `risk_summary`
- `assumptions`

每个文件项包含：

- `name`: 文件名或 `not generated`。
- `path`: 本地路径，未生成时为空。

## data_health_rows

数据健康详情行数组，来自最新 `*_data_health.csv`。

常见字段包括：

- `symbol`
- `name`
- `effective_source`
- `fresh_fetch_success`
- `cache_present`
- `cache_merged`
- `served_from_cache`
- `fallback_used`
- `latest_date`
- `expected_latest_date`
- `stale_days`
- `row_count`
- `warning_level`
- `warning_message`

## data_health_summary

数据健康摘要。

- `total`
- `ok`
- `warning`
- `error`
- `cache_present`
- `cache_merged`
- `served_from_cache`
- `fallback_used`
- `stale`

## news_health_rows

Rows from the latest `*_news_health.csv`. They explain whether news fetching was
attempted, skipped, successful, empty, or failed.

Common fields:

- `symbol`
- `name`
- `source`
- `attempted`
- `skipped`
- `skip_reason`
- `fetch_success`
- `item_count`
- `latest_title`
- `latest_time`
- `warning_level`
- `warning_message`
- `error_message`
- `fetched_at`

## news_health_summary

Aggregated news-fetch status:

- `total`
- `attempted`
- `skipped`
- `ok`
- `info`
- `warning`
- `error`
- `no_news`
- `failed`
- `sample_mode_skipped`

## Paper Portfolio APIs

Paper portfolio APIs are virtual review records only. They do not connect to a
broker, do not place orders, and do not represent real account returns.

- `GET /api/market-clock`: simplified A-share market session and expected settlement date.
- `GET /api/paper-portfolio`: virtual account, positions, trades, summary, and disclaimer.
- `POST /api/paper-buy`: record a virtual buy.
- `POST /api/paper-sell`: record a virtual sell.
- `POST /api/paper-reset`: reset the virtual portfolio.
- `POST /api/paper-refresh-marks`: refresh virtual marks from the current report payload.

Position fields include:

- `quote_date`
- `settlement_date`
- `settlement_status`
- `settlement_message`
- `estimate_only`
- `settled_price`
- `estimated_price`
- `data_source`
- `data_warning_level`

## /api/integrations

Optional external tool registry. These tools are detected locally only; QuantDesk
does not install them, does not copy third-party source code, and does not call
their strategy, training, broker, or live trading features.

Response fields:

- `integrations`: array of optional tools.
- `integrations[].name`: display name, such as `AKShare`, `vectorbt`, `Backtrader`, `Microsoft Qlib`, `FinRL / FinRL-X`.
- `integrations[].package`: Python package name used for local detection.
- `integrations[].installed`: whether the package is installed in the current Python environment.
- `integrations[].version`: installed package version, or empty when missing.
- `integrations[].purpose`: how the tool could fit QuantDesk later.
- `integrations[].status`: `available` or `optional_not_installed`.
- `integrations[].license_note`: license and redistribution caution.
- `integrations[].warning`: user-facing risk and stability warning.
- `integrations[].recommended_priority`: `high`, `medium`, or `low`.
- `integrations[].integration_type`: `data_provider` or `research`.
- `disclaimer`: optional dependency notice. Missing tools do not affect the main workflow.

## /api/analyze-stock

`POST /api/analyze-stock` performs local stock search analysis from the current
report/sample payload. It does not connect to a broker, does not place orders,
and does not provide investment advice.

Request:

```json
{"query": "300057"}
```

Response fields:

- `ok`: whether a single matching stock was analyzed.
- `query`: original query.
- `symbol`: normalized six-digit stock code.
- `name`: stock name.
- `analysis_mode`: `intraday_observation` or `after_close_review`.
- `analysis_mode_label`: localized label such as `盘中观察` or `收盘后复盘`.
- `market_session`: simplified A-share market session.
- `price_context.current_price`: current or latest known price.
- `price_context.change_pct`: latest known percentage change.
- `price_context.volume`: latest known volume.
- `price_context.amount`: latest known amount.
- `price_context.quote_date`: quote date.
- `price_context.quote_time`: quote time when available.
- `price_context.is_intraday_estimate`: true when the result is estimate-only.
- `price_context.mode_message`: user-facing settlement/estimate note.
- `technical_snapshot`: MA20, MA60, MA120, ATR, RSI, MACD, and BOLL fields when available.
- `decision_card.action_label`: conservative observation label.
- `decision_card.focus_reasons`: reasons to watch.
- `decision_card.main_risks`: risk checks.
- `decision_card.pre_buy_checks`: pre-trade checklist.
- `decision_card.invalidation_conditions`: strategy invalidation checks.
- `data_status`: data quality text.
- `news_status`: news status text.
- `matches`: returned when a name query matches multiple stocks.
- `message`: returned when no single analysis is available.
- `disclaimer`: states that the result is only for observation and auxiliary analysis.

### trust_assessment

`trust_assessment` is a conservative reliability assessment. It does not predict
price direction and is not investment advice.

- `raw_score`: original strategy score, kept separate from reliability.
- `trust_score`: 0-100 conservative reliability score.
- `trust_level`: `高风险`, `谨慎观察`, `可观察`, `条件较好`, or `深度复核`.
- `action_state`: `暂不参考`, `只观察`, `虚拟验证`, `条件满足后再评估`, or `深度复核`.
- `reliability_breakdown`: data, news, trend, liquidity, risk distance, market context, and information completeness checks.
- `beginner_explanation`: plain-language explanation for non-professional users.
- `required_checks`: checks to complete before relying on the analysis.
- `blocking_issues`: issues that make the stock temporarily unsuitable for reference.
- `disclaimer`: says the trust score does not predict price direction and does not constitute investment advice.

### timing_advice

`timing_advice` explains market-session context such as pre-open, intraday
observation, lunch break, after-close review, and market holiday review.

- `market_session`
- `timing_state`
- `timing_label`
- `ready_for_review`: whether the tool considers the row ready for deeper review; this is not an action instruction.
- `suggested_action`
- `reasons`
- `wait_conditions`
- `pre_action_checklist`
- `invalidation_conditions`
- `risk_warnings`
- `next_review_time`
- `disclaimer`

### candidate_cards trust fields

Each `candidate_cards[]` item may include `raw_score`, `trust_score`,
`trust_level`, `action_state`, `timing_label`, `timing_state`,
`reliability_breakdown`, `beginner_explanation`, `required_checks`,
`blocking_issues`, `trust_disclaimer`, `trust_assessment`, and
`timing_advice`. These fields are auxiliary checks. They do not change candidate
sorting or the original scoring formula.

## market_calendar_status

Local CN A-share calendar status. QuantDesk uses `config/trading_calendar_cn.yaml`
and does not fetch an online calendar. For example, `2026-05-05` is a Labor Day
market holiday, so the expected latest daily bar is `2026-04-30`.

- `market`: `CN_A_SHARE`
- `market_session`: `pre_open`, `morning_session`, `lunch_break`, `afternoon_session`, `after_close`, `non_trading_day`, or `market_holiday`
- `is_market_holiday`: whether the local calendar marks the date as a holiday
- `expected_latest_date`: expected latest completed daily bar
- `expected_settlement_date`: same date used by the settlement layer
- `previous_trading_day`: latest previous A-share trading day
- `calendar_note`: local calendar maintenance note
- `timezone`: `Asia/Shanghai`
- `note`: user-facing caveat

## data_health_reason_summary

Aggregated reason counts from `data_health_rows`. This lets the UI explain why
Data Health is warning instead of only showing a total warning count.

- `market_holiday_using_last_trading_day`
- `waiting_for_close`
- `waiting_for_quote_update`
- `latest_complete_daily_bar`
- `stale`
- `no_data`
- `cache_only`
- `fallback_used`
- `fetch_failed`
- `other`

## /api/market-clock calendar fields

`GET /api/market-clock` returns the same local calendar fields used by the UI
and paper-portfolio settlement:

- `now`
- `market_session`
- `expected_settlement_date`
- `expected_latest_date`
- `is_market_holiday`
- `previous_trading_day`
- `calendar_note`
- `timezone`
- `note`

## /api/strategy-demo/strategies

Read-only strategy demonstration registry. This endpoint does not write
`paper_portfolio.json`, does not generate real-world actions, and does not
return price targets, win-rate metrics, annualized-return metrics, or future
price forecasts.

- `schema_version`
- `app_version`
- `generated_at`
- `data_mode`: `sample`, `report`, or `empty`
- `strategies`: available local demonstration strategies
- `strategies[].name`
- `strategies[].description`
- `strategies[].entry_label`: user-facing observation-condition label
- `strategies[].exit_label`: user-facing invalidation-condition label
- `strategies[].why`: plain-language explanation of why the rule checks those fields
- `disclaimer`

## /api/strategy-demo/check

Read-only rule check for one stock and one predefined strategy. The result only
shows whether mechanical conditions are met, not whether the stock should be
acted on.

- `schema_version`
- `app_version`
- `generated_at`
- `data_mode`
- `result.symbol`
- `result.name`
- `result.strategy_name`
- `result.strategy_description`
- `result.check_type`
- `result.check_type_label`
- `result.matched`: conditions already satisfied
- `result.failed`: conditions not yet satisfied
- `result.missing`: fields missing from the current payload
- `result.all_matched`
- `result.blocked_by_trust`
- `result.blocked_by_timing`
- `result.block_reason`
- `result.features`: normalized feature row used by the rule checker
- `result.feature_coverage`: field coverage status such as `ok`, `derived`, `demo_fallback`, or `missing`
- `result.feature_notes`: notes about field mapping or sample-mode demo completion
- `result.coverage_level`: `complete`, `partial`, or `missing_required`
- `result.explanation`
- `result.disclaimer`

The API is local and read-only. It does not call broker APIs, does not write
virtual portfolio state, and does not run network-heavy research tools.

## /api/strategy-demo/overview

Read-only strategy sandbox overview for all current candidate cards. This
endpoint does not write `paper_portfolio.json`, does not generate reports, and
does not fetch additional network data.

Request body:

- `strategy`: predefined local strategy name, for example `均线突破`

Response fields:

- `schema_version`
- `app_version`
- `generated_at`
- `data_mode`
- `report_date`
- `result.strategy_name`
- `result.total`
- `result.summary.all_matched`
- `result.summary.partial`
- `result.summary.blocked`
- `result.summary.missing_required`
- `result.rows[].symbol`
- `result.rows[].name`
- `result.rows[].status`
- `result.rows[].status_label`
- `result.rows[].matched_count`
- `result.rows[].failed_count`
- `result.rows[].missing_count`
- `result.rows[].blocked_by_trust`
- `result.rows[].blocked_by_timing`
- `result.rows[].short_explanation`
- `result.rows[].detail`: same shape as `/api/strategy-demo/check`
- `disclaimer`

The overview result only represents mechanical rule checking. It does not
return target prices, win-rate metrics, annualized-return metrics, or future
price forecasts.

## /api/stock-classifier/overview

Classifies current candidate cards for local learning, simulation planning, and
risk review. The endpoint is local and read-only; it does not write
`paper_portfolio.json` and does not trigger any external trading action.

Request body:

- `strategy`: predefined local strategy name, for example `均线突破`

Response fields:

- `schema_version`
- `app_version`
- `generated_at`
- `data_mode`
- `report_date`
- `strategy_name`
- `summary`
- `rows[].symbol`
- `rows[].name`
- `rows[].primary_category`
- `rows[].strategy_category`: `观察条件全部满足`, `部分满足`, `已阻断`, or `缺少关键字段`
- `rows[].data_category`: `数据完整`, `数据部分缺失`, `数据可信度不足`, `样例补全`, `等待数据更新`, or `混合数据`
- `rows[].trend_category`
- `rows[].activity_category`
- `rows[].risk_category`
- `rows[].portfolio_category`
- `rows[].tags`
- `rows[].explanation`
- `rows[].data_date_label`
- `disclaimer`

Strategy feature rows may include:

- `activity_value`
- `activity_avg_20`
- `activity_ratio_20`
- `activity_source`: `amount`, `volume`, `demo_fallback`, or `unknown`

`activity_*` fields are used so real-report amount fields such as
`amount_yuan` and `amount_ratio` can be reviewed by the strategy sandbox.
`vol20` is price volatility and is not treated as trading activity.

## /api/paper-trading/plan-entry

Builds a local virtual-entry plan from a strategy-demo result. This endpoint is
read-only and does not write `paper_portfolio.json`.

Request body:

- `symbol`
- `strategy`
- `quantity`, optional
- `risk_budget_pct`, optional

Response fields:

- `plan.symbol`
- `plan.name`
- `plan.strategy_name`
- `plan.plan_action`
- `plan.plan_action_label`
- `plan.quantity`
- `plan.price`
- `plan.risk_line`
- `plan.position_plan`
- `plan.matched_conditions`
- `plan.failed_conditions`
- `plan.missing_fields`
- `plan.data_date_label`
- `strategy_result`

## /api/paper-trading/confirm-entry

Writes the plan into the local virtual portfolio after the user confirms in the
UI. The record stores strategy source, matched conditions, failed conditions,
missing fields, data date, risk notes, and a paper-only disclaimer. It does not
connect to a broker and does not place a real order.

## /api/paper-trading/plan-exit and /api/paper-trading/confirm-exit

Mirror the entry plan/confirm flow for local virtual-exit records. These
records are paper-only and are used for simulation review.

## /api/paper-review/weekly

Returns a weekly summary of local virtual records:

- `virtual_entry_count`
- `virtual_exit_count`
- `virtual_position_count`
- `virtual_estimated_pnl`
- `virtual_settled_pnl`
- `top_strategy`
- `common_missing_fields`
- `common_risk_review_reasons`
- `disclaimer`

## /api/strategy-demo/compare-stock

Compares all local strategy templates for one stock. The endpoint is read-only
and does not write any portfolio state.

Request body:

- `symbol`
- `strategies`, optional list of strategy names

Response fields:

- `result.symbol`
- `result.name`
- `result.strategies[].strategy_name`
- `result.strategies[].status`
- `result.strategies[].status_label`
- `result.strategies[].matched_count`
- `result.strategies[].failed_count`
- `result.strategies[].missing_count`
- `result.strategies[].plan_action`
- `result.strategies[].plan_action_label`
- `result.strategies[].rank`
- `result.strategies[].short_explanation`
- `result.strategies[].risk_notes`
- `result.ranking_note`
- `disclaimer`

The comparison is for learning how different rules inspect the same stock. It
does not return target prices, win-rate metrics, annualized-return metrics, or
future price forecasts.


## v1.9 研究可信度字段

`/api/results` 顶层新增本地只读研究字段：

- `benchmark_snapshot`：本地基准快照，包含 `benchmark_name`、`benchmark_symbol`、`benchmark_return`、`benchmark_curve`、`provider_status`、`data_date`、`disclaimer`。
- `market_regime`：市场环境提示，包含 `market_regime`、`market_regime_label`、`index_name`、`index_close`、`index_ma60`、`provider_status`、`explanation`。
- `fundamental_health_rows`：候选股财务红线检查结果。AKShare 未安装时返回 `provider_unavailable`，不影响主功能。
- `fundamental_health_summary`：财务状态聚合，包含 `total`、`ok`、`warning`、`red_flag`、`provider_unavailable`、`missing`、`unknown`。
- `candidate_replay`：候选复盘，只对当前候选池做规则回放。
- `strategy_replay`：策略历史回放（规则层），对本地历史候选样本做规则满足情况检查，并包含 `survivorship_bias_warning`、`replay_mode=rule_replay`、`strategy_trade_replay_status=available`（完整交易路径回放由 `/api/research/strategy-trade-replay` 独立提供）。
- `research_summary`：研究可信度摘要，包含基准状态、市场环境、财务红线摘要、候选复盘数量、策略历史回放（规则层）样本天数和研究说明。

这些字段只用于本地研究复核。历史回放统计不代表未来收益或概率；没有完整历史股票池时，应优先查看幸存者偏差提示。当前策略历史回放为规则层历史回放，不等同于完整交易路径回测。

## GET /api/market-regime

返回当前本地市场环境摘要：

- `schema_version`
- `app_version`
- `generated_at`
- `data_mode`
- `report_date`
- `market_regime`
- `disclaimer`

## POST /api/fundamental-health/check

请求：

```json
{"symbol": "000001"}
```

返回单只股票财务红线检查结果。该 API 不安装 AKShare，不联网，不接券商；provider 不可用时返回 `provider_unavailable`。

## POST /api/research/candidate-replay

请求：

```json
{"strategy": "均线突破"}
```

返回当前候选池的候选复盘结果。它不是完整历史回测，只说明当前候选如何被规则检查。

## POST /api/research/strategy-replay

请求：

```json
{"strategy": "均线突破", "sample_days": 12}
```

返回本地历史候选样本上的策略历史回放（规则层）。若没有完整历史股票池，`survivorship_bias_warning=true`。返回中包含 `replay_mode=rule_replay`，`strategy_trade_replay_status=available` 表示完整交易路径回放已由独立接口 `/api/research/strategy-trade-replay` 提供。

## POST /api/research/strategy-trade-replay

请求：

```json
{
  "symbol": "000001",
  "strategy": "均线突破",
  "initial_cash": 100000,
  "max_position_ratio": 0.1,
  "buy_price_mode": "close",
  "sell_price_mode": "close",
  "commission_rate": 0.00025,
  "slippage_rate": 0.0005,
  "market_rules": "simple",
  "min_commission": null,
  "stamp_tax_rate": null,
  "transfer_fee_rate": null,
  "enforce_lot_size": null,
  "lot_size": null,
  "enforce_t_plus_one": null,
  "enforce_price_limit": null,
  "enforce_suspension_check": null
}
```

返回基于本地历史日线的交易路径回放（trade replay）。它用 `replay_mode=trade_replay`、`trade_replay_status=simulated` 标识；始终返回 `disclaimer`，提示仅基于本地历史数据做模拟、不接券商、不自动下单、不构成投资建议，历史结果不代表未来收益。

结果 `result` 中包含：

- `strategy_id`、`strategy_name`、`symbol`、`name`、`data_mode`、`history_source_symbol`、`history_source_note`、`history_rows`、`history_start_date`、`history_end_date`。
- `initial_cash`、`final_cash`、`final_equity`、`total_return`、`max_drawdown`。
- `trade_count`、`closed_trade_count`、`open_trade_count`、`win_rate`、`average_trade_return`、`holding_days_avg`。
- `buy_price_mode`、`sell_price_mode`、`commission_rate`、`slippage_rate`、`position_size`、`max_position_ratio`。
- `market_rules`（`simple` / `cn_a_share`）、`fee_model`（`simple_commission` / `cn_a_share_simplified`）、`quantity_rule`（`simple_quantity` / `100_share_lot`）。
- `min_commission`、`stamp_tax_rate`、`transfer_fee_rate`、`enforce_lot_size`、`lot_size`、`enforce_t_plus_one`、`enforce_price_limit`、`enforce_suspension_check`、`blocked_order_count`、`market_rule_warnings[]`。
- `trades[]`：每笔交易的 `trade_id`、`entry_signal_date`、`entry_execution_date`、`entry_execution_policy`、`entry_date`（兼容别名）、`entry_price`、`quantity`、`exit_signal_date`、`exit_execution_date`、`exit_execution_policy`、`exit_date`（兼容别名）、`exit_price`、`exit_signal`、`pnl`、`return_pct`、`holding_days`、`status`、`matched_conditions`、`failed_conditions`、`missing_fields`、`risk_notes`、`risk_line_type`、`risk_line_price`、`risk_triggered`、`mark_to_market_date`、`mark_to_market_price`、`market_rules`、`fee_model`、`quantity_rule`、`entry_blocked_reason`、`exit_blocked_reason`、`market_rule_warnings`、`disclaimer`。
- `equity_curve[]`：逐日 `date`、`close`、`cash`、`position_value`、`market_value`（兼容别名）、`equity`、`drawdown`、`position_quantity`、`position_active`、`risk_line_type`、`risk_line_price`、`risk_triggered`、`entry_signal`、`exit_signal`、`data_freshness`。
- `warnings[]`、`assumptions[]`、`errors[]`、`strategy_warning`。
- `disclaimer`：同样带本地模拟免责声明。

执行层规则：信号在 `signal_date` 收盘后确认；`close` 模式表示在信号日收盘成交，`next_open` 模式表示在下一条有效行情的开盘成交，缺失下一条有效行情时不会伪造成交，只会返回 warning；始终按 `commission_rate` 扣费、按 `slippage_rate` 平移执行价；单策略单股票在虚拟组合中最多保留一笔同时虚拟持仓中的记录；仍在虚拟持仓中时按策略风险线（`ma20` / `atr_trailing` / `recent_low` / `manual`）检查是否触发，ATR 回撤用虚拟持仓中观察到的最高价计算。

### market_rules 约束

- `market_rules="simple"`（默认）：沿用 v2.2.1 的简化交易路径模拟。`fee_model=simple_commission`，`quantity_rule=simple_quantity`；不强制 100 股整数手，不强制 T+1，不做涨跌停和疑似停牌判断；`assumptions` 中继续声明未完整模拟 A 股真实交易约束。
- `market_rules="cn_a_share"`：启用本地简化 A 股交易规则。`fee_model=cn_a_share_simplified`，`quantity_rule=100_share_lot`。默认值：`enforce_lot_size=true`、`lot_size=100`、`enforce_t_plus_one=true`、`enforce_price_limit=true`、`enforce_suspension_check=true`、`min_commission=5.0`、`stamp_tax_rate=0.0005`，可以通过请求字段单独覆写。任何被规则阻止的模拟成交都会进入 `warnings` 并累计到 `blocked_order_count`，不会产生虚假 `trade`。

A 股规则层仍然只是**本地历史模拟**：不接券商、不自动下单、不构成投资建议；实际交易规则以交易所、券商、数据源公告为准。规则层历史回放（`rule_replay`）不受 `market_rules` 影响。

## v2.3.1 Data Provider Fallback

### `/api/results` 新增字段

- `spot_snapshot_meta`：最近一次 spot 快照的元信息，字段包括
  - `spot_snapshot_source`（`eastmoney_spot` / `sina_spot` / `cache` / `hist_fallback`）
  - `spot_snapshot_mode`（`full_market` / `hist_fallback` / `cache`）
  - `universe_scope`
  - `universe_sources[]`
  - `hist_fallback_symbols[]`
  - `provider_errors[]`
  - `data_freshness_warning`
  - `latest_date`
  - `fetched_at` / `cache_fetched_at`
  - `row_count`
  - `cache_csv_exists` / `cache_meta_exists`

任一 fallback 成功都会返回 `spot_snapshot_source` / `universe_scope` / `data_freshness_warning`，用于前端和测试识别“本次不是完整全市场扫描”。

### `POST /api/generate-daily-report`

请求：`{}`

成功时返回 `{ ok: true, message, notice, command, return_code, stdout_excerpt, stderr_excerpt, output_excerpt, output, files, report_files }`。

失败时返回 `{ ok: false, message, notice, command, return_code, stdout_excerpt, stderr_excerpt, output_excerpt, troubleshooting[], error }`。`troubleshooting` 是一个简短建议清单，用于指引网络 / AKShare / 离线样例报告等排查，不会只返回 `Command exited with code 1`。

### `POST /api/generate-sample-report`

结构与上面相同；离线样例不联网，只读取本地 JSON 和 `results/sample_report/`。
## v2.0 Local Decision Assistant APIs

All v2.0 endpoints return `schema_version`, `app_version`, `generated_at`, `data_mode`, `report_date`, and a disclaimer when applicable. These endpoints are local-only helpers for simulation review and manual confirmation. They do not connect to broker APIs, do not place orders, and do not replace the user's own decision.

### `POST /api/action-plan/overview`

Returns local plan-action rows derived from current candidate rows and strategy-demo results.

Fields:

- `result.summary`: counts by plan action and review status.
- `result.rows`: per-stock rows with `symbol`, `name`, `strategy_name`, `plan_action`, `plan_action_label`, `manual_confirmation_level`, `manual_confirmation_level_label`, `risk_notes`, `data_date_label`, and `detail`.
- `disclaimer`: local simulation and manual-confirmation boundary.

### `POST /api/action-plan/add-to-confirmation`

Adds a plan row to the local manual confirmation queue. This writes only `state/manual_confirmation_queue.json` and never touches any broker or trading software.

### `POST /api/action-plan/update-status`

Updates a manual confirmation item status. Supported statuses include `pending`, `reviewed`, `ignored`, `simulated`, and `manually_executed_outside_quantdesk`.

### `GET /api/manual-confirmation-queue`

Returns the local manual confirmation queue with summary counts.

### `GET /api/watchlist`

Returns the local watchlist and summary counts.

### `POST /api/watchlist/add`

Adds or replaces a local watchlist item.

### `POST /api/watchlist/remove`

Removes a local watchlist item.

### `POST /api/live-monitor/once`

Runs one local monitor pass against the current payload and watchlist. It can produce local alert objects, but it does not connect to broker APIs and does not place orders.


## v2.3.2 Local Daily Cache API

### `POST /api/update-market-data`

Calls `scripts/update_market_data.py` to update `data/daily/{symbol}.csv` from a daily provider chain: local daily cache, Eastmoney `stock_zh_a_hist`, then Sina `stock_zh_a_daily`. This API only updates the local cache. It does not connect to brokers, does not place orders, and does not constitute investment advice.

Current safety contract: provider-backed update runs require explicit `network:true`
or `enable_network:true` in the POST body. Requests without explicit network
authorization return `status="blocked_network_disabled"` and do not start a
provider request. Offline dry-run/cache-index checks remain local-only.
`all_a_share:true` is treated as explicit full-market scope and is never
implied by default.

Request fields:

- `symbols`: string or array, for example `"600000,000001"`.
- `start_date`: `YYYYMMDD`
- `end_date`: `YYYYMMDD`
- `adjust`: default `qfq`
- `limit`: optional symbol cap
- `force`: re-download the requested range
- `include_local_universe`: when true, merge watchlist / paper portfolio / local pools / config fallback symbols with explicit `symbols`

Response fields:

- `ok`
- `message`
- `return_code`
- `stdout_excerpt`
- `stderr_excerpt`
- `output_excerpt`
- `update_status`

`update_status.results[*]` includes `provider_used`, `provider_errors`, `latest_date`, `row_count`, `output_path`, and `skipped`. If Eastmoney fails but Sina daily succeeds, `provider_used` is `sina_daily` and `provider_errors.eastmoney_hist` records the Eastmoney failure.

`/api/results` also includes `market_data_update_status`, read from `data/cache/update_status.json`, so the UI can show the latest local daily-cache update result.

### v2.3.3 Universe Cache Additions

`scripts/update_a_share_universe.py` maintains `data/universe/a_share_symbols.csv`. The file is local runtime data and is excluded from clean zip exports.

`POST /api/update-market-data` accepts the additional fields below:

- `all_a_share`: run against `data/universe/a_share_symbols.csv`
- `universe_file`: optional custom CSV universe file
- `resume`: skip symbols already marked ok or skipped in the previous `update_status.json`
- `retry_failed`: retry only failed symbols from the previous status file

`update_status` also includes:

- `universe_mode`
- `universe_file`
- `universe_total`
- `processed_count`
- `remaining_count`
- `provider_summary`

The provider chain remains `local_daily_cache -> eastmoney_hist -> sina_daily -> all_failed`. Failed symbols are recorded per symbol and do not stop the rest of the universe update.

History backfill/incremental fields:

- `backfill`: long-history backfill mode; fresh short caches are expanded instead of skipped
- `incremental`: daily incremental mode with a recent overlap window
- `min_history_rows`: minimum expected rows before local history is considered complete
- `lookback_days`: incremental overlap window in calendar days

`update_status` adds `update_mode`, `min_history_rows`, `history_short_count`, `date_fresh_but_history_short`, and per-symbol `min_date`, `max_date`, `row_count`, and `history_short`.

### v2.4.0 Data Foundation

`/api/results` includes `data_foundation_status`, derived from `data/cache/update_status.json` and the latest `*_data_health.csv`.

Important fields:

- `data_grade_distribution`: counts for `UNAVAILABLE`, `OBSERVE_ONLY`, `SCORABLE`, `RESEARCH_READY`, and `BACKTEST_READY`
- `csv_count`: local `data/daily/*.csv` count
- `universe_total`, `processed_count`, `remaining_count`
- `success_count`, `failed_count`, `skipped_count`
- `history_short_count`
- `provider_summary`
- `provider_unavailable`
- `last_error`
- `last_error_type`

Per-symbol update rows and data-health rows may include:

- `data_grade`
- `data_grade_label`
- `data_grade_reason`
- `research_allowed`
- `backtest_allowed`
- `provider_error_type`

`NETWORK_BLOCKED_OR_PERMISSION_DENIED` is used for Windows socket permission failures such as `WinError 10013`. Data grades act as a gate: `BACKTEST_READY` can enter backtest-style simulation, `RESEARCH_READY` can enter strategy research, `SCORABLE` is limited to basic scoring, `OBSERVE_ONLY` is observation-only, and `UNAVAILABLE` is excluded. These fields are risk controls only; they do not predict prices and do not provide investment advice.

### Daily report cache fields

The report metadata may include:

- `daily_data_source`
- `local_daily_cache_hit_count`
- `local_daily_cache_miss_count`
- `local_daily_cache_latest_date`
- `local_daily_cache_warning`

The cache is not realtime market data. When full-market spot snapshots fail, QuantDesk can still generate a daily report from the local daily cache and local candidate pool, with explicit fallback warnings.

### v2.4.1 Research Validation

`data_foundation_status` adds the following fields:

- `date_fresh_count`: symbols whose latest date is up-to-date
- `date_fresh_but_history_short`: symbols that are date-fresh but have fewer rows than `min_history_rows` (now correctly recalculated after merge)
- `failed_by_error_type`: `{ "NETWORK_REMOTE_DISCONNECTED": N, "PROVIDER_SCHEMA_MISMATCH": N, ... }`
- `failed_by_market`: `{ "SH": N, "SZ": N, "BSE": N, ... }`
- `bse_total`, `bse_success_count`, `bse_failed_count`, `bse_provider_unsupported_count`
- `research_validation_ready`: `true` if at least one symbol is `RESEARCH_READY` or `BACKTEST_READY`

Provider error types (per-symbol `provider_error_type`):

- `NETWORK_BLOCKED_OR_PERMISSION_DENIED`
- `NETWORK_REMOTE_DISCONNECTED`
- `PROVIDER_EMPTY_RESPONSE`
- `PROVIDER_SCHEMA_MISMATCH`
- `PROVIDER_UNSUPPORTED_SYMBOL`
- `BSE_MAPPING_MISSING`
- `UNKNOWN_PROVIDER_ERROR`

Market classification (`classify_market`):

- `SH` (600xxx), `SZ` (000xxx/002xxx), `STAR` (688xxx), `CHINEXT` (300xxx/301xxx), `BSE` (920xxx/83xxxx/87xxxx/43xxxx), `UNKNOWN`

New CLI scripts:

- `scripts/walk_forward_backtest.py` — walk-forward validation with rolling train/test windows
- `scripts/export_trade_plan.py` — trade plan export (CSV + Markdown) for manual confirmation

New modules:

- `quantdesk/walk_forward.py` — walk-forward engine
- `quantdesk/backtest_quality.py` — backtest quality scoring (INVALID/LOW/MEDIUM/HIGH)
- `quantdesk/backtest_audit.py` — strategy audit report generator
- `quantdesk/backtest_metrics.py` — backtest performance metrics
- `quantdesk/factors/` — basic price-volume and fundamental factor framework
- `quantdesk/trade_plan.py` — trade plan builder and exporter
- `quantdesk/risk_position.py` — conservative ATR-based position sizing reference

All results are for local research and simulation review only. They do not constitute investment advice, do not connect to brokers, and do not auto-execute trades.

### v2.5.0 Practical Tools

`data_foundation_status` adds:

- `practical_tools_ready`: `true` when strategy compare, portfolio health, stock screener, and signal tracker modules are available
- `practical_tools`: `["strategy_compare", "portfolio_health", "stock_screener", "signal_tracker"]`

### v2.5.1 Incremental Data Flow

Top-level `/api/results` adds separate data-flow status blocks:

- `global_history_status`: full local history library summary. Includes `universe_total`, `csv_count`, `min_date`, `max_date`, `expected_latest_trading_day`, `success_count`, `failed_count`, `history_short_count`, `data_grade_distribution`, `provider_summary`, `failed_by_error_type`, and `failed_by_market`.
- `incremental_update_status`: latest update-run summary. Includes `last_update_mode`, `last_update_started_at`, `last_update_finished_at`, `target_latest_trading_day`, `updated_count`, `already_fresh_count`, `failed_count`, `skipped_count`, `lookback_days`, and `provider_summary`.
- `report_run_status`: current report-run summary. Includes `report_file`, `report_date`, `sample_count`, `analyzed_count`, `long_candidates_count`, `board_candidates_count`, `warning_count`, `skipped_count`, and `report_mode`.

`report_run_status.sample_count` is only the current report sample size. It is not the full A-share universe count.

Operational endpoints:

- `POST /api/update-a-share-universe`: updates the local A-share universe file only.
- `POST /api/update-market-data`: runs local daily data update only when explicit `network=true` or `enable_network=true` is present. Use `incremental=true` for daily updates, `backfill=true` for historical backfill, and `retry_failed=true` for failed-symbol retries.
- `POST /api/generate-daily-report`: generates the report from local cache defaults and does not run `update_market_data.py`.
- `POST /api/update-and-generate-daily-report`: runs incremental update first, then generates the report. It requires both explicit `network=true`/`enable_network=true` and explicit `all_a_share=true`; neither provider access nor full-market scope is implied by default.

New modules:

- `quantdesk/strategy_compare.py` — multi-strategy comparison with per-regime (bull/bear/sideways) breakdown
- `quantdesk/portfolio_health.py` — daily portfolio risk check (stop-loss distance, concentration, sector exposure)
- `quantdesk/stock_screener.py` — configurable condition-based stock filter (AND logic, supports numeric/string/data_grade comparisons)
- `quantdesk/signal_tracker.py` — signal recording, outcome evaluation, and historical win rate statistics

New CLI scripts:

- `scripts/portfolio_health_check.py` — portfolio health check from CSV
- `scripts/screen_stocks.py` — stock screener from CSV/JSON candidates

All tools are for local research only. They do not constitute investment advice, do not connect to brokers, and do not auto-execute trades.

### v3.1.3 Strategy Observation Contract And Agent Safe Bridge

This release adds local-only observation payloads for future agent explanation. No real LLM is called and no network request is made by the observation report command.

Runtime outputs:

- `results/strategy_observation_context_latest.json`
- `results/strategy_observation_report_latest.md`

Context fields:

```json
{
  "schema_version": "3.1.3",
  "purpose": "learning_observation",
  "not_investment_advice": true,
  "data_health": {},
  "provider_chain": [],
  "strategy_outputs": {},
  "intraday_status": {},
  "paper_portfolio_status": {},
  "chanlun": {},
  "allowed_agent_actions": ["explain", "summarize", "risk_notes", "data_gap_notes", "next_observation_steps", "experiment_hypotheses"],
  "forbidden_agent_actions": ["buy_recommendation", "sell_recommendation", "position_sizing", "target_price", "stop_loss_price", "real_order"]
}
```

CLI:

`python scripts/qd.py observation-report --symbol 600744 --target-date YYYY-MM-DD --network false`

The report is a deterministic offline template. It is only for learning research, does not connect to brokers, does not place real orders, and does not constitute investment advice.

### v2.5.2 Architecture Docs & UI Navigation

### New architecture documents

- `docs/architecture_inventory.md` — module inventory with I/O, network, and safety annotations
- `docs/data_flow_map.md` — end-to-end data flow from backfill to UI rendering
- `docs/status_contract.md` — status field definitions (global_history_status, incremental_update_status, report_run_status, cache_inventory)
- `docs/ui_action_contract.md` — button permission matrix (network, heavy task, confirmation)
- `docs/known_logic_issues.md` — known and potential logic issues

### UI primary tab navigation

The workspace now has 8 primary tabs (`概览`, `数据中心`, `今日筛选`, `研究验证`, `实用工具`, `持仓与计划`, `报告`, `系统诊断`).

Frontend changes:

- `app/index.html`: `<nav class="primary-nav" id="primaryNav">` with `.primary-tab[data-primary-tab]` buttons
- `app/static/app.js`: `PRIMARY_TAB_SECTIONS`, `switchPrimaryTab()`, `restorePrimaryTab()`, hash-based routing
- `app/static/styles.css`: `.primary-nav`, `.primary-tab`, `.primary-tab.active` styles

Server version after the visible text health fix: `APP_VERSION = "2.5.4-visible-text-health"`, `API_SCHEMA_VERSION = "2.5.4"`.

### v2.5.3 UTF-8 UI Render Fix

This release only changes UI encoding and rendering behavior.

- HTML is served as `text/html; charset=utf-8`.
- JavaScript is served as `application/javascript; charset=utf-8`.
- CSS is served as `text/css; charset=utf-8`.
- JSON is served as `application/json; charset=utf-8`.
- `app/index.html` declares `<meta charset="UTF-8">`.
- Primary tab labels are plain text: 概览、数据中心、今日筛选、研究验证、实用工具、持仓与计划、报告、系统诊断.
- No strategy, broker, automated order, investment-advice, ML, minute-line, tick, DuckDB, or SQLite migration feature is added by v2.5.3.

### v2.5.4 Visible Text Health

QuantDesk v2.5.4 removes remaining visible question-mark placeholders, malformed closing-tag fragments, and stale encoded labels from the UI shell.

- `/api/results` payloads remain backward compatible.
- Frontend text rendering now sanitizes legacy encoded result text and shows “旧结果文案编码异常，请重新生成报告” instead of displaying broken placeholders.
- Old result files may trigger a warning, but clean source files must not expose broken UI labels.
- No strategy, broker, automated order, investment-advice, ML, minute-line, tick, DuckDB, or SQLite migration feature is added by v2.5.4.
## v2.7.9 字段别名、分钟线与题材映射

### v2.7.9 Field Alias Intraday Data Theme Map

- `APP_VERSION`: `2.7.9-field-alias-intraday-data-theme-map`
- `API_SCHEMA_VERSION`: `2.7.9`

- 自动策略沙盒结果新增 `theme_sector_summary`，并写入 `results/theme_sector_summary_latest.json`。
- 候选项可包含 `sector`、`industry`、`primary_theme`、`theme_tags`、`theme_confidence`；缺少本地映射时返回“未知板块 / 未知题材”。
- 字段兼容层将 `activity_avg_20` 映射为 `amount_ma20`，将 `data_reliability_score` 映射为 `data_confidence_score`。
- 本地 1 分钟线导入命令为 `python scripts/import_intraday_1m.py --create-template` 或 `--symbol 601778 --source path/to/file.csv`，目标文件为 `data/intraday/1m/{symbol}.csv`。
### v2.8.0 Auto Theme Map Updater

`APP_VERSION = "2.8.0-auto-theme-map-updater"`  
`API_SCHEMA_VERSION = "2.8.0"`

新增本地板块/题材映射维护接口：

- `GET /api/theme-map/status`：只读映射状态，不联网，不触发更新。
- `GET /api/theme-map/preview`：只读预览 `data/universe/stock_theme_map.csv` 前若干行。
- `POST /api/theme-map/update`：手动触发映射更新。`source=local` 只做本地提取；`source=akshare` 会访问外部数据源；页面初始加载不会调用该 POST。

输出文件：

- `data/universe/stock_theme_map.csv`
- `data/universe/stock_theme_map_pending.csv`

字段：

`symbol,name,sector,industry,concept_tags,theme_tags,primary_theme,source,updated_at`

该能力只用于学习分组和本地模拟解释，不接券商、不真实下单、不构成投资建议。

### v2.8.1 Persistent Auto Portfolio Review

`APP_VERSION = "2.8.1-persistent-auto-portfolio-review"`  
`API_SCHEMA_VERSION = "2.8.1"`

自动虚拟组合改为连续账本：

- `state/auto_paper_portfolio.json`：连续状态源，保存现金、持仓、已关闭持仓、`daily_actions`、`decision_review` 和 `yesterday_holding_review`。
- `results/auto_paper_portfolio_latest.json`：最新快照，供 UI 只读展示。
- `results/auto_paper_portfolio_history.json`：每日权益历史，update/init 成功后追加。

`POST /api/auto-paper-portfolio/run`：

- `mode=init`：仅第一次建仓。已有 state 时默认返回 `ok=false, error="portfolio_exists"`，不会覆盖。
- `force_reset=true`：显式强制重置，允许 `init` 覆盖已有 state。
- `mode=update`：保留昨日持仓，先刷新本地日线最新价和浮动盈亏，复盘昨日动作，再处理今日候选。

新增字段：

- `daily_actions[]`：今日 buy / hold / sell / block / watch_exit 动作。
- `decision_review`：昨日 buy / hold / sell / watch_exit 的今日表现复盘。
- `yesterday_holding_review`：昨日持仓今日价格、涨跌、浮动盈亏和复盘结论。

旧 `results/paper_trading_latest.json` 仍是手动/旧版观察仓，不作为自动虚拟组合持仓依据。该流程仍不接券商、不真实下单、不构成投资建议。
### v2.8.2 Local Daily Feature Engine

新增本地日线特征增强接口。所有接口只读取或生成本地 `data/features/daily_features_latest.csv`，不联网、不接券商、不真实下单、不构成投资建议。

- `GET /api/daily-features/status`：只读，返回特征表是否存在、行数、最新日期、生成时间、处理范围和路径。
- `GET /api/daily-features/preview?limit=20`：只读，预览特征表前若干行。
- `POST /api/daily-features/build`：手动触发特征生成，请求体示例：

```json
{
  "only_low_price": true,
  "limit": 0
}
```

`only_low_price=true` 时只处理 `data/universe/low_price_learning_symbols.csv` 中 `included=true` 的股票；`false` 时扫描已有 `data/daily` 本地缓存，这是用户显式触发的高级操作。页面初始加载不得调用 POST，也不会自动扫描全 A。

输出 CSV 字段包含：`symbol,name,latest_date,row_count,open,high,low,close,prev_close,pct_chg,volume,amount,turnover,ma5,ma10,ma20,ma60,ma120,return_20_pct,volatility_20_pct,is_limit_up_like,consecutive_limit_up,is_new_low_180,relative_index_return_20_pct,liquidity_status` 等。
## v3.3.2 Data Update Workbench and Auto Refresh Preflight

`results/data_update/data_update_latest.json` 使用通用 result contract，包含 `schema_version`、`app_version`、`status`、`warnings`、`target_complete_trade_date`、`latest_local_daily_date`、`trading_day_lag`、`network`、`update_allowed`、`success_count`、`failed_count`、`skipped_count`、`suggested_next_action` 和 `disclaimer`。
## v3.3.5 remediation fields

Release: v3.3.5 Command Output Error Actionability and Daily Remediation Flow

Several latest payloads may include:

- `remediation_plan`: overall local repair plan.
- `remediation_actions`: ranked copyable actions.
- `warning_kind`: normalized warning type such as `data_stale`, `intraday_missing`, `schema_secret_like_reference`, or `schema_confirmed_secret_leak`.
- `severity`: `info`, `warning`, `high`, or `critical`.
- `network_required`: true only when the suggested command requires explicit network authorization.
- `follow_up_command`: the next local command to run after the primary action.

These fields are workflow remediation metadata. They are not trading signals and do not constitute investment advice.

## v3.3.6 remediation UI preview fields

Release: v3.3.6 Remediation Ui Copy And Artifact Preview Polish

Artifact-center items may include:

- `remediation_command`: one copyable command for regenerating or repairing the artifact.
- `remediation_network_required`: whether that command needs explicit network authorization.
- `preview_type`: `json`, `markdown`, `jsonl`, `none`, or `unavailable`.
- `preview_summary`: JSON summary fields for `schema_version`, `app_version`, `status`, `generated_at`, `target_date`, `warning_count`, `failed_count`, and `remediation_action_count`.
- `preview_summary_text`: a short Markdown summary for copy buttons.
- `preview_line_count` and `preview_truncated`: preview length metadata.

These fields are read-only UI metadata. They must not contain API Keys, real account information, trading advice, buy/sell instructions, target prices, or stop prices.
## v3.4.0 Market Intelligence Sector Low Price Board Watch Workbench

## Market Intelligence Payloads

v3.4.0 adds:

- `results/market_intelligence/market_intelligence_latest.json`
- `results/market_intelligence/market_regime_latest.json`
- `results/market_intelligence/sector_theme_latest.json`
- `results/market_intelligence/market_news_latest.json`
- `results/market_intelligence/sector_low_price_candidates_latest.json`
- `results/market_intelligence/scenario_curves_latest.json`

Common fields: `schema_version`, `app_version`, `status`, `warnings`, `target_date`, `network`, `disclaimer`.

Important semantic fields:

- `data_scope=local_sample` means the conclusion is based on local samples only.
- `market_news.status=skipped` is normal when `network=false`.
- `sector_low_price_candidates.price_limit` defaults to `20`.
- `sector_low_price_candidates.strict_price_limit` defaults to `15`.
- `scenario_curves.curve_type=scenario_observation_not_prediction`.

Market intelligence payloads are research artifacts only and do not constitute investment advice.
## v3.4.2.1 Dependency Config Provider Hygiene And Paper Lock

- `score_components` rows may include `data_sufficient` and `data_warning`.
  When all key indicators are missing, existing score values remain unchanged
  but `data_sufficient=false` flags the score as a placeholder.
- Provider diagnostics keep planned adapters as `status=planned` with
  `placeholder=true`; provider failure summaries skip those rows.
- Core dependency installation is `requirements.txt`; optional provider
  installation is `requirements-data.txt` or `pip install .[data]`.

## v3.4.2 Dashboard State Clarity Command Safety And Market Intel Report Polish

Dashboard-oriented payloads may now include:

- `analysis_pool_count`: total locally available symbols in the selected safe pool.
- `selected_count`: symbols actually checked in this run.
- `max_symbols`: the configured cap for this command.
- `truncated`: true when the run checked only part of the current analysis pool.
- `truncation_reason`: user-facing explanation such as `max-symbols=80` limiting the run.

Home cards should display only a short `symbols_preview` and `symbols_omitted_count`; complete symbol lists stay in JSON. Stale-data remediation must distinguish offline checks from online repairs: `data-update --network false` checks local state only and does not fill data, while `data-update --enable-network --scope current_analysis_pool` is the explicit online daily-data update path. Market-intel reports remain paper observation only and must not contain buy/sell instructions, position sizing, target prices, stop prices, deterministic predictions, or promised returns.

## v3.4.1 Market Intel Scope Sync Candidate Explainability And Dashboard Polish

`data-update`, `data-health`, and `market-intel` payloads expose a shared scope summary:

- `data_scope`
- `scope_label`
- `scope_source`
- `requested_count`
- `selected_count`
- `max_symbols`
- `truncated`
- `symbols_preview`
- `symbols_omitted_count`

Workbench overview compares latest data-update, data-health, and market-intel scopes. If they differ, the UI reports that the stock ranges are different and suggests rerunning data-health and market-intel with `--scope current_analysis_pool`.

# v3.5.1 Schema Notes

v3.5.1 Truthful Attribution Installer And Structure Hardening

v3.5.1-truthful-attribution-installer-and-structure-hardening adds `risk_attribution` payloads for true Brinson/factor attribution and `risk_observation_lite` payloads for compatibility diagnostics.
# v3.7.0.4 Capital Lot Sensitivity And Git Hygiene

Adds `git_deletion_audit` and `capital_lot_sensitivity` JSON payloads. Both are research-only diagnostics, keep `network=false` by default, and never produce real orders.
# v3.7.0.5 Git Deletion Reconciliation

Maintenance payloads include `import_alias_reconciliation`, `git_deletion_current_status`, and refined `git_deletion_audit` results. Deprecated root module paths are reported as compatibility aliases when covered by `quantdesk.__init__`; they are not restore-required runtime risks.
# v3.7.0.6 Strategy Regression Guard and Report Hygiene

Strategy report payloads now include `report_provenance`, grid integrity fields, baseline distinctness status, and performance sanity status. Smoke runs should write to `results/smoke/` and `reports/smoke/`.
