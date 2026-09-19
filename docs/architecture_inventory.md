# Architecture Inventory

Updated through `v3.7.0.5-git-deletion-reconciliation`

- v3.7.0.5 git deletion reconciliation: deprecated root module paths are compatibility aliases only; active code should import from subpackages.

- v3.7.0 data backbone: SQLite shadow layer, CSV migration, DualWriter, provider chains, Parquet manifest, and data quality health checks.

- v3.6.2 research realization: Similar Day local feature matching, historical stress windows, style exposure/contribution lite, decision journal statistics, and daily research snapshots.

- v3.6.1 risk integration: `quantdesk/risk/position_inputs.py`, smart stop per-position status, risk alert pipeline, notification dedupe, and daily risk snapshot.

Historical compatibility marker: Updated through `v3.4.3.8-true-backtest-assumptions-split-and-doc-hygiene`

Historical compatibility marker: Updated through `v3.4.4.0-multi-strategy-account-book`

This file is the canonical current architecture inventory. The longer
`docs/history/architecture_inventory.md` is retained as release history and a
compatibility record; it is not the current authority.

## Current Research Stack

- Factor evaluation: `quantdesk/factor_evaluation.py`
- Factor combination: `quantdesk/factor_combiner.py`
- Combined score research backtest: `quantdesk/combined_score_backtest.py`
- Sector rotation: `quantdesk/sector_rotation_engine.py`
- Research dashboard: `quantdesk/factor_research_dashboard.py`
- PIT universe lite: `quantdesk/point_in_time_universe.py`
- Daily research snapshot: `quantdesk/daily_research_snapshot.py`
- Backtest assumptions split: `quantdesk/backtest_assumptions.py`
- Multi-strategy account book: `quantdesk/multi_strategy_account_book.py`
- Intraday live workbench: `quantdesk/intraday_live_engine.py`
- Partial daily snapshot policy: `quantdesk/intraday_live_snapshot.py`, `quantdesk/intraday_data_policy.py`
- Intraday provisional score: `quantdesk/intraday_score.py`
- Risk attribution lite: `quantdesk/risk_attribution_lite.py`
- Daily risk attribution snapshot: `quantdesk/daily_risk_attribution_snapshot.py`
- Strategy recommender: `quantdesk/research/strategy_recommender.py`
- Daily strategy recommendation snapshot: `quantdesk/research/daily_strategy_recommendation_snapshot.py`
- v3.6.0 real-use loop: `quantdesk/research/stock_picking_flow.py`, `quantdesk/research/paper_watchlist.py`, `quantdesk/research/daily_candidate_review.py`
- v3.6.0 data/risk/platform modules: `quantdesk/data/price_adjustment.py`, `quantdesk/risk/smart_stop.py`, `quantdesk/risk/risk_alerts.py`, `quantdesk/notifications/notification_center.py`, `quantdesk/storage/local_store.py`
- Active UI home/overview and grouped navigation: `app/templates/daily_research.html`, `app/static/js/daily_research.js`
- AI paper trader compatibility classroom: `ai_trader/dashboard.py`, `scripts/server/routes/ai_trader.py`, `python -m ai_trader app` (8001)
- Retired/frozen 8765 compatibility UI: `app/index.html`, `app/static/app.js`, `app/static/styles.css`

## Scripts

- `scripts/qd.py`
- `scripts/run_daily_workflow.py`
- `scripts/run_factor_evaluation.py`
- `scripts/build_combined_score.py`
- `scripts/run_combined_score_backtest.py`
- `scripts/run_sector_rotation.py`
- `scripts/run_factor_research_dashboard.py`
- `scripts/show_pit_universe_status.py`
- `scripts/run_multi_strategy_account_book.py`
- `scripts/run_intraday_live_workbench.py`
- `scripts/run_risk_attribution_lite.py`
- `scripts/verify_clean_zip.py`

## Core Modules

- `quantdesk/backtest.py`
- `quantdesk/backtest_assumptions.py`
- `quantdesk/daily_workflow_orchestrator.py`
- `quantdesk/cli_summary.py`
- `quantdesk/multi_strategy_account_book.py`
- `quantdesk/intraday_live_engine.py`
- `quantdesk/version.py`

## Safety Constraints

- Research and backtest modules remain offline-first unless a command explicitly opts into network access.
- Daily research snapshots, research dashboards, factor studies, and backtests are read-only diagnostics and do not constitute investment advice.
- Clean zip release checks reject runtime outputs, local data, temporary factor files, nested zips, and stale release proof metadata.

## Compatibility Anchors

Historical compatibility note: v3.2.2 core correctness inventory tokens are preserved here as anchors only; the current inventory remains updated through v3.4.4.0.

- Provider stack: `providers/*`, `provider_result_contract.py`, `provider_network_smoke.py`
- Calendar stack: `trading_calendar.py`
- Intraday and Chanlun stack: `intraday_*`, `chanlun_*`
- Strategy observation and agent safety stack: `strategy_observation_contract.py`, `agent_policy_guard.py`, `agent_safe_bridge.py`, `agent_evidence_pack.py`, `agent_hallucination_guard.py`, `agent_report_audit.py`
- LLM configuration stack: `llm_config.py`, `llm_providers/*`
- Research and backtest outputs are diagnostics only and do not constitute investment advice.

## Release Hygiene

Clean zip exports use a versioned artifact name, embed `RELEASE_ARTIFACT.json`, and exclude runtime outputs such as `dist/`, `results/`, `reports/`, local data directories, caches, and temporary factor output files.

