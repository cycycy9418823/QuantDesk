from __future__ import annotations

import locale
import os
import runpy
import subprocess
import sys
import time
from pathlib import Path


def _configure_console_encoding() -> None:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            if not stream.isatty():
                reconfigure(encoding="ascii", errors="backslashreplace")
            else:
                encoding = (os.environ.get("PYTHONIOENCODING") or "").split(":", 1)[0] or locale.getpreferredencoding(False) or "utf-8"
                reconfigure(encoding=encoding, errors="replace")


_configure_console_encoding()

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = PROJECT_ROOT / "scripts"
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from quantdesk.operation_log import log_operation  # noqa: E402
from quantdesk.shared import now_text, read_json, truthy  # noqa: E402

IN_PROCESS_DISABLED_COMMANDS = {"app", "live"}

COMMANDS: dict[str, tuple[str, list[str], str]] = {
    "pause": ("run_desk_control.py", ["pause"], "Stop the close pipeline and 20:20 AI writer; leave readers on."),
    "resume": ("run_desk_control.py", ["resume"], "Restore writers last stopped by pause. Does not re-enable old disabled tasks."),
    "doctor": ("self_check.py", [], "Run project self-checks; recommended first in a clean zip."),
    "env-doctor": ("check_python_env.py", [], "Diagnose the active Python runtime and Windows launcher choices."),
    "status": ("show_quantdesk_status.py", [], "Read local status and next actions; no network by default."),
    "fix-plan": ("show_fix_plan.py", [], "Print the current repair plan; read-only and offline by default."),
    "acceptance": ("show_project_acceptance.py", [], "Show finite project acceptance dispositions without conflating alpha, operations, or cloud deployment."),
    "app": ("launcher.py", [], "Start the current daily research app server; add --update-on-start only when explicitly allowing startup data refresh."),
    "self-check": ("self_check.py", [], "Run project self-checks."),
    "data-bootstrap": ("run_data_bootstrap.py", [], "Initialize a tiny demo universe; fetch daily data only with explicit --enable-network."),
    "daily": ("run_daily_workflow.py", ["--strategy", "all"], "Run the daily local review workflow."),
    "market-intel": ("run_market_intelligence.py", [], "Run market observation: regime, sectors, low-price paper candidates, and scenario notes."),
    "mainline-radar": ("run_market_mainline_radar.py", [], "Build local research-only market mainline radar with data-readiness evidence."),
    "data-readiness": ("show_extended_data_readiness.py", [], "Show read-only extended data readiness across daily, metadata, minute, sector, event, fundamental, and moneyflow domains."),
    "data-gates": ("show_extended_data_gates.py", [], "Show one read-only gate chain for Tushare queue, retry plan, staged import audit, and post-import verification."),
    "tushare-queue-status": ("show_tushare_queue_status.py", [], "Show read-only local Tushare continuous download queue status without printing tokens."),
    "tushare-minute-status": ("show_tushare_minute_status.py", [], "Show read-only local Tushare 1-minute download job status without printing tokens."),
    "tushare-minute-coverage": ("show_tushare_minute_coverage.py", [], "Show read-only target-pool coverage for local Tushare 1-minute files; no import or canonical writes."),
    "tushare-minute-quality": ("show_tushare_minute_quality.py", [], "Read-only quality audit for the latest completed local Tushare 1-minute job; no import or canonical writes."),
    "minute-pool-audit": ("run_minute_pool_window_audit.py", [], "Read-only windowed minute-pool quality audit over data/intraday/1m; grades every trading day in --window-start/--window-end with the existing quality standard."),
    "tushare-retry-plan": ("show_tushare_retry_plan.py", [], "Build read-only retry plan from local Tushare queue failures; no auto restart."),
    "import-extended-data": ("import_extended_staged_data.py", [], "Dry-run-first import of staged extended data into canonical local dirs; writes only with --apply true."),
    "audit-extended-import": ("audit_extended_staged_import.py", [], "Read-only preflight audit for staged extended data import before any --apply run."),
    "verify-extended-import": ("verify_extended_import.py", [], "Read-only post-import verification for canonical extended data domains."),
    "staged-provider-audit": ("audit_staged_provider_evidence.py", [], "Read-only evidence audit for mootdx/baostock vendor staging; emits a wait gate while a download runs."),
    "staged-import-plan": ("staged_import_plan.py", [], "Read-only dry-run import plan for staged mootdx/baostock evidence; never runs --apply."),
    "providers": ("check_a_stock_data_providers.py", [], "Check optional A-share data provider availability."),
    "network-smoke": ("run_provider_network_smoke.py", [], "Run explicit provider network smoke tests."),
    "observation-report": ("run_strategy_observation_report.py", [], "Generate a local learning observation report."),
    "strategy-validate": ("run_strategy_validation.py", [], "Check strategy lookahead and execution assumptions."),
    "consistency-check": ("check_paper_vs_backtest_consistency.py", [], "Check paper simulation versus backtest consistency."),
    "feature-audit": ("run_feature_quality_audit.py", [], "Audit local feature quality and coverage."),
    "stats-validate": ("run_backtest_statistical_validation.py", [], "Run statistical validation on existing backtest results."),
    "risk-stat": ("run_risk_stat_validation.py", [], "Build research-only VaR, impact, and statistical validation diagnostics."),
    "param-sensitivity": ("run_parameter_sensitivity_audit.py", [], "Audit parameter sensitivity without optimizing parameters."),
    "schema-check": ("check_result_schemas.py", [], "Validate latest result schemas and safety metadata."),
    "data-update": ("run_data_update.py", [], "Check or update daily data; real fetching requires explicit --enable-network."),
    "data-db": ("run_data_db.py", [], "Manage the SQLite shadow data layer: init, stats, migrate, compare, reset, disable, enable, backup."),
    "data-universe": ("run_data_universe.py", [], "Build or validate runtime historical_listings.csv for degraded PIT research."),
    "build-universe": ("run_build_universe.py", [], "Build a local research-only universe summary from data/daily; no network or broker route."),
    "data-query": ("run_data_query.py", [], "Run safe whitelisted SQLite shadow-layer queries."),
    "data-quality": ("run_data_quality_repair.py", [], "Detect and mark data quality issues; repair is bounded and opt-in."),
    "maintenance": ("run_maintenance.py", [], "Run read-only maintenance diagnostics such as git deletion, import alias, strategy regression, script inventory, and bounded test tier tools."),
    "v38-cleanup-audit": ("run_v38_cleanup_audit.py", [], "Audit v3.8 cleanup targets without deleting files."),
    "v38-daily-pipeline": ("qd_daily_pipeline.py", [], "Run v3.8 research-only daily diagnostics pipeline."),
    "v38-progress-audit": ("run_v38_progress_audit.py", [], "Audit v3.8 plan progress and remaining gaps."),
    "v38-final-readiness": ("run_v38_final_readiness.py", [], "Build v3.8 final readiness audit without release packaging."),
    "v38-runtime-cleanup": ("run_v38_runtime_cleanup.py", [], "Plan or apply v3.8 runtime output cleanup with preservation."),
    "v38-runtime-cleanup-validate": ("validate_v38_runtime_cleanup.py", [], "Validate v3.8 runtime cleanup dry-run plan before apply."),
    "build-features": ("build_daily_features.py", [], "Build daily features from local data/daily CSV files."),
    "screen": ("screen_stocks.py", [], "Run a safe local screen from daily_features_latest.csv."),
    "backtest": ("run_backtest.py", [], "Run the real local backtest entrypoint from data/daily CSV files."),
    "factor-eval": ("run_factor_evaluation.py", [], "Run offline factor effectiveness diagnostics; research only."),
    "factor-panel": ("build_factor_panel.py", [], "Build local research-only factor panel with forward returns."),
    "factor-ic": ("run_factor_ic_report.py", [], "Build research-only cross-sectional factor IC report from local CSV."),
    "alpha-adjudication": ("run_formal_alpha_adjudication.py", [], "Run fail-closed formal alpha adjudication from frozen local evidence."),
    "factor-combine": ("build_combined_score.py", [], "Build research-only neutralized multi-factor combined scores."),
    "combined-score-backtest": ("run_combined_score_backtest.py", [], "Backtest combined_score ranking portfolios for research only."),
    "strategy-pipeline": ("run_strategy_pipeline_precheck.py", [], "Run strategy pipeline repair precheck; research-only and offline by default."),
    "sector-rotation": ("run_sector_rotation.py", [], "Run research-only sector rotation diagnostics."),
    "research-dashboard": ("run_factor_research_dashboard.py", [], "Summarize factor research diagnostics in one research-only dashboard."),
    "pit-universe": ("show_pit_universe_status.py", [], "Show point-in-time universe status for research diagnostics."),
    "multi-strategy-book": ("run_multi_strategy_account_book.py", [], "Build a research-only multi-strategy virtual account book."),
    "live": ("run_intraday_live_workbench.py", [], "Run the research-only intraday live workbench; no broker and no real orders."),
    "strategy-recommend": ("run_strategy_recommender.py", [], "Run research-only strategy recommendation; no broker and no real orders."),
    "strategy-viability": ("run_strategy_viability_sprint.py", [], "Run research-only data precheck, baselines, and capital/lot sensitivity tests."),
    "selected-symbols": ("export_canonical_selected_symbols.py", [], "Export research-only canonical selected-symbol snapshots for holdings materialization."),
    "holdings-reconstruct": ("reconstruct_candidate_holdings.py", [], "Materialize research-only candidate holdings from verified selected-symbol snapshots."),
    "holdings-export": ("export_candidate_holdings.py", [], "Explicit research-only candidate holdings export hook; default behavior is off."),
    "stock-picking-flow": ("run_v360_builder.py", ["stock_picking_flow"], "Run real-use research-only stock picking flow."),
    "paper-watchlist": ("run_v360_builder.py", ["paper_watchlist"], "Manage a paper-only watchlist; no broker and no real orders."),
    "candidate-review": ("run_v360_builder.py", ["daily_candidate_review"], "Review prior paper candidates with local data."),
    "data-integrity": ("run_data_integrity.py", [], "Check adjusted price data layout and corporate-action inputs."),
    "data-audit": ("run_data_audit.py", [], "Audit local daily CSV quality without modifying data files."),
    "dynamic-factor-model": ("run_v360_builder.py", ["dynamic_factor_model"], "Build a research-only dynamic factor weighting model."),
    "execution-realism": ("run_v360_builder.py", ["execution_realism"], "Run research-only execution realism diagnostics."),
    "smart-stop": ("run_v360_builder.py", ["smart_stop"], "Compute paper smart-stop warnings without trading."),
    "risk-alerts": ("run_v360_builder.py", ["risk_alerts"], "Evaluate research-only risk alerts."),
    "notify": ("run_v360_builder.py", ["notification_center"], "Run notification center in dry-run mode by default."),
    "style-attribution": ("run_v360_builder.py", ["style_attribution"], "Run lite Barra-style attribution."),
    "return-explain": ("run_v360_builder.py", ["return_explain"], "Explain return contributors from local snapshots."),
    "stress-test": ("run_v360_builder.py", ["stress_test"], "Run paper stress scenarios."),
    "similar-day": ("run_v360_builder.py", ["similar_day"], "Find similar historical days from local features."),
    "decision-journal": ("run_v360_builder.py", ["decision_journal"], "Manage local research decision journal."),
    "money-flow": ("run_v360_builder.py", ["money_flow"], "Read local money-flow CSV adapter."),
    "dragon-tiger": ("run_v360_builder.py", ["dragon_tiger"], "Read local dragon-tiger CSV adapter."),
    "fundamental-metrics": ("run_v360_builder.py", ["fundamental_metrics"], "Read local fundamental metrics adapter."),
    "local-store": ("run_v360_builder.py", ["local_store"], "Use optional local sqlite query cache."),
    "attribution": ("run_risk_attribution.py", [], "Run true research-only Brinson and factor attribution."),
    "risk-observation": ("run_risk_attribution_lite.py", [], "Run five-dimensional risk observation diagnostics; not Brinson attribution."),
    "risk-attribution": ("run_risk_attribution_lite.py", [], "Run risk-attribution-lite diagnostics; use attribution for true Brinson."),
    "experiments": ("run_experiments.py", [], "List and diff local research experiment logs."),
    "first-run": ("first_run_wizard.py", [], "Run the v3.5.0 first-run wizard; research-only and network off by default."),
    "wizard": ("first_run_wizard.py", [], "Alias for first-run wizard."),
    "walk-forward": ("walk_forward_backtest.py", [], "Run local walk-forward validation."),
    "paper": ("run_auto_paper_portfolio.py", [], "Run the local paper portfolio flow; no broker and no real orders."),
    "shadow-weekly": ("run_risk_gate_shadow_weekly.py", [], "Aggregate the latest five validated daily risk-gate shadow observations; offline and report-only."),
    "stage4-maintenance": ("run_stage4_maintenance_evidence.py", [], "Collect local forward-PIT continuity, shadow counts, frozen-gate evidence, and gated real paper-account replay."),
    "shared-paper-plan": ("run_shared_paper_allocation_plan.py", [], "Plan a shared paper-capital allocation from research recommendations; plan only, no broker and no real orders."),
    "paper-sim": ("run_paper_trading_simulator.py", [], "Run the local paper trading simulator; no broker and no real orders."),
    "sandbox": ("run_auto_strategy_sandbox.py", [], "Run the local strategy sandbox for learning observation only."),
    "dev-preflight": ("dev_preflight.py", [], "Run local data preflight before development or UI work."),
    "data-health": ("run_data_health.py", [], "Check local data health; --db-path routes to SQLite shadow-layer health."),
    "data-health-alerts": ("run_data_health_alert_delivery.py", [], "Preview or explicitly deliver one redacted Server 酱 data-health alert summary."),
    "daily-data-chain": ("run_daily_data_chain.py", [], "Update daily bars, critical minutes, the current analysis-pool minutes, features, and one truthful manifest."),
    "weekly-minute-wide-pool": ("run_weekly_minute_wide_pool.py", [], "Maintain existing canonical minute symbols outside the formal daily pool; offline and no-apply by default."),
    "weekly-daily-wide-pool": ("run_weekly_wide_pool_maintenance.py", [], "Maintain the audited weekly daily-wide and minute-residual pools; offline and no-apply by default."),
    "full-a-reconcile": ("run_full_a_daily_reconciliation.py", [], "Reconcile every current-universe symbol to one complete trade date; offline and no-apply by default."),
    "morning-brief": ("run_morning_brief.py", [], "Build and optionally push the verified local research morning brief; external delivery requires --enable-network --push."),
    "minute-gap-backfill": ("run_round13_minute_gap_backfill.py", [], "Run the audited Round-13 minute gap backfill for the five authorized pairs; offline and no-apply by default."),
    "daily-retain": ("run_daily_data_retention.py", [], "Create a daily data retention manifest."),
    "manifest-check": ("check_reproducibility_manifest.py", [], "Validate a daily retention manifest."),
    "archive-results": ("archive_results.py", [], "Archive existing latest result/report artifacts."),
    "command-center": ("show_command_center_status.py", [], "Read recent command status from operation_log."),
    "llm-smoke": ("check_llm_observation_agent.py", [], "Check optional LLM observation wiring; LLM remains off by default."),
    "agent-report-audit": ("check_agent_report_audit.py", [], "Audit whether learning reports are based on local observation context."),
    "intraday-workbench": ("run_intraday_data_workbench.py", [], "Run the intraday data workbench CLI."),
    "chanlun-fractal": ("run_chanlun_fractal_analysis.py", [], "Run Chanlun fractal recognition."),
    "chanlun-bi": ("run_chanlun_bi_analysis.py", [], "Run Chanlun bi recognition."),
    "chanlun-segment": ("run_chanlun_segment_analysis.py", [], "Run Chanlun segment recognition."),
}


OUTPUT_PATHS: dict[str, list[str]] = {
    "doctor": ["dist/quantdesk_clean.zip"],
    "env-doctor": ["results/python_env_latest.json"],
    "status": ["results/status_latest.json", "reports/status_report_latest.md"],
    "fix-plan": ["results/fix_plan_latest.json"],
    "daily": ["results/daily_workflow_latest.json", "reports/daily_quant_report_latest.md"],
    "market-intel": [
        "results/market_intelligence/market_intelligence_latest.json",
        "reports/market_intelligence_report_latest.md",
        "results/market_intelligence/mainline_radar_latest.json",
        "reports/mainline_radar_latest.md",
    ],
    "mainline-radar": [
        "results/market_intelligence/mainline_radar_latest.json",
        "reports/mainline_radar_latest.md",
    ],
    "data-readiness": [
        "results/data_health/extended_data_readiness_latest.json",
        "reports/extended_data_readiness_latest.md",
    ],
    "data-gates": [
        "results/data_health/extended_data_gates_latest.json",
        "reports/extended_data_gates_latest.md",
    ],
    "tushare-queue-status": [
        "results/data_health/tushare_queue_status_latest.json",
        "reports/tushare_queue_status_latest.md",
    ],
    "tushare-minute-status": [
        "results/data_health/tushare_minute_status_latest.json",
        "reports/tushare_minute_status_latest.md",
    ],
    "tushare-minute-coverage": [
        "results/data_health/tushare_minute_coverage_latest.json",
        "reports/tushare_minute_coverage_latest.md",
    ],
    "tushare-minute-quality": [
        "results/data_health/tushare_minute_quality_latest.json",
        "reports/tushare_minute_quality_latest.md",
    ],
    "minute-pool-audit": [
        "results/data_health/minute_pool_window_audit_latest.json",
    ],
    "tushare-retry-plan": [
        "results/data_health/tushare_retry_plan_latest.json",
        "reports/tushare_retry_plan_latest.md",
    ],
    "import-extended-data": ["results/data_health/extended_staged_import_plan_latest.json"],
    "audit-extended-import": [
        "results/data_health/extended_staged_import_audit_latest.json",
        "reports/extended_staged_import_audit_latest.md",
    ],
    "verify-extended-import": [
        "results/data_health/extended_import_verification_latest.json",
        "reports/extended_import_verification_latest.md",
    ],
    "staged-provider-audit": [
        "results/data_health/staged_provider_evidence_audit_latest.json",
        "reports/staged_provider_evidence_audit_latest.md",
    ],
    "staged-import-plan": [
        "results/data_health/staged_import_plan_latest.json",
        "reports/staged_import_plan_latest.md",
    ],
    "data-bootstrap": ["results/data_bootstrap/data_bootstrap_latest.json", "reports/data_bootstrap_report_latest.md"],
    "data-universe": ["results/historical_listings_build_latest.json", "reports/historical_listings_build_latest.md"],
    "build-universe": ["results/universe/build_universe_latest.json", "reports/universe/build_universe_latest.md"],
    "self-check": ["dist/quantdesk_clean.zip"],
    "providers": ["results/a_stock_data_providers_latest.json"],
    "network-smoke": ["results/provider_network_smoke_latest.json"],
    "observation-report": [
        "results/strategy_observation_context_latest.json",
        "results/strategy_observation_report_latest.md",
        "results/strategy_observation_report_audit_latest.json",
    ],
    "strategy-validate": ["results/strategy_validation_latest.json", "reports/strategy_validation_report_latest.md"],
    "consistency-check": [
        "results/paper_vs_backtest_consistency_latest.json",
        "reports/paper_vs_backtest_consistency_report_latest.md",
    ],
    "feature-audit": ["results/feature_quality_audit_latest.json", "reports/feature_quality_audit_report_latest.md"],
    "stats-validate": [
        "results/backtest_statistical_validation_latest.json",
        "reports/backtest_statistical_validation_report_latest.md",
    ],
    "risk-stat": ["results/risk_stat_validation_latest.json", "reports/risk_stat_validation_latest.md"],
    "param-sensitivity": [
        "results/parameter_sensitivity_latest.json",
        "reports/parameter_sensitivity_report_latest.md",
    ],
    "schema-check": [
        "results/schema_validation_latest.json",
        "reports/schema_validation_report_latest.md",
    ],
    "data-update": [
        "results/data_update/data_update_latest.json",
        "reports/data_update_report_latest.md",
    ],
    "data-db": ["data/quantdesk.db"],
    "data-query": [],
    "data-quality": ["data/quantdesk.db"],
    "maintenance": [
        "results/git_deletion_audit_latest.json",
        "reports/git_deletion_audit_latest.md",
        "results/import_alias_reconciliation_latest.json",
        "reports/import_alias_reconciliation_latest.md",
        "results/strategy_regression_audit_latest.json",
        "reports/strategy_regression_audit_latest.md",
        "results/import_rewrite_latest.json",
        "reports/import_rewrite_latest.md",
        "results/maintenance/script_inventory_latest.json",
        "reports/maintenance/script_inventory_latest.md",
        "results/maintenance/test_inventory_latest.json",
        "reports/maintenance/test_inventory_latest.md",
        "results/maintenance/test_discovery_diagnostics_latest.json",
        "reports/maintenance/test_discovery_diagnostics_latest.md",
        "results/maintenance/test_tier_runner_latest.json",
        "reports/maintenance/test_tier_runner_latest.md",
        "results/maintenance/pytest_suite_runner_latest.json",
        "reports/maintenance/pytest_suite_runner_latest.md",
        "results/maintenance/frontend_api_contract_latest.json",
        "reports/maintenance/frontend_api_contract_latest.md",
        "results/maintenance/dependency_contract_latest.json",
        "reports/maintenance/dependency_contract_latest.md",
        "results/maintenance/data_backtest_contract_latest.json",
        "reports/maintenance/data_backtest_contract_latest.md",
        "results/maintenance/coverage_contract_latest.json",
        "reports/maintenance/coverage_contract_latest.md",
        "reports/coverage/coverage.xml",
    ],
    "v38-cleanup-audit": ["results/v38_cleanup_audit_latest.json", "reports/v38_cleanup_audit_latest.md"],
    "v38-daily-pipeline": ["results/v38_daily_pipeline_latest.json", "reports/v38_daily_pipeline_latest.md"],
    "v38-progress-audit": ["results/v38_progress_audit_latest.json", "reports/v38_progress_audit_latest.md"],
    "v38-final-readiness": ["results/v38_final_readiness_latest.json", "reports/v38_final_readiness_latest.md"],
    "v38-runtime-cleanup": ["results/v38_runtime_cleanup_plan_latest.json", "reports/v38_runtime_cleanup_plan_latest.md"],
    "v38-runtime-cleanup-validate": [
        "results/v38_runtime_cleanup_preapply_validation_latest.json",
        "reports/v38_runtime_cleanup_preapply_validation_latest.md",
    ],
    "build-features": ["data/features/daily_features_latest.csv", "data/features/daily_features_summary.json"],
    "screen": ["results/screen/screen_latest.json", "reports/screen_report_latest.md"],
    "backtest": ["results/backtest/backtest_latest.json", "reports/backtest_report_latest.md"],
    "factor-eval": ["results/factor_health_latest.json", "reports/factor_evaluation_report_latest.md"],
    "factor-panel": ["results/factor_panel_latest.csv", "results/factor_panel_latest.json", "reports/factor_panel_latest.md"],
    "factor-ic": ["results/factor_ic_report_latest.json", "results/factor_ic_report_latest.csv", "reports/factor_ic_report_latest.md"],
    "factor-combine": ["results/combined_score_latest.json", "reports/combined_score_report_latest.md"],
    "combined-score-backtest": ["results/combined_score_backtest_latest.json", "reports/combined_score_backtest_report_latest.md"],
    "strategy-pipeline": ["results/strategy_pipeline_precheck_latest.json", "reports/strategy_pipeline_precheck_latest.md"],
    "sector-rotation": ["results/sector_rotation_latest.json", "reports/sector_rotation_report_latest.md"],
    "research-dashboard": ["results/research_dashboard_latest.json", "reports/research_dashboard_report_latest.md"],
    "pit-universe": ["results/pit_universe_status_latest.json"],
    "multi-strategy-book": ["results/multi_strategy_account_book_latest.json", "reports/multi_strategy_account_book_latest.md"],
    "live": ["results/intraday_live_workbench_latest.json", "reports/intraday_live_workbench_latest.md"],
    "strategy-recommend": ["results/strategy_recommendation_latest.json", "reports/strategy_recommendation_latest.md"],
    "strategy-viability": [
        "results/strategy_viability_precheck_latest.json",
        "results/strategy_viability_baselines_latest.json",
        "results/strategy_viability_latest.json",
        "results/capital_lot_sensitivity_latest.json",
        "reports/strategy_viability_latest.md",
        "reports/capital_lot_sensitivity_latest.md",
    ],
    "selected-symbols": [
        "results/holdings/canonical_selected_symbols_latest.csv",
        "results/holdings/canonical_selected_symbols_latest.json",
        "results/holdings/selected_symbol_snapshot_quality_audit_latest.json",
        "reports/holdings/canonical_selected_symbols_latest.md",
    ],
    "holdings-reconstruct": [
        "results/holdings/candidate_holdings_reconstruction_latest.json",
        "results/holdings/candidate_holdings_snapshot_latest.csv",
        "results/holdings/holdings_snapshot_quality_audit_latest.json",
        "reports/holdings/candidate_holdings_reconstruction_latest.md",
    ],
    "holdings-export": [
        "results/holdings/candidate_holdings_snapshot_latest.csv",
        "results/holdings/candidate_holdings_snapshot_latest.json",
        "reports/holdings/candidate_holdings_snapshot_latest.md",
    ],
    "stock-picking-flow": ["results/stock_picking_flow_latest.json", "reports/stock_picking_flow_latest.md"],
    "paper-watchlist": ["results/paper_watchlist_latest.json", "reports/paper_watchlist_latest.md"],
    "candidate-review": ["results/daily_candidate_review_latest.json", "reports/daily_candidate_review_latest.md"],
    "data-integrity": ["results/data_integrity_latest.json", "reports/data_integrity_latest.md"],
    "dynamic-factor-model": ["results/dynamic_factor_model_latest.json", "reports/dynamic_factor_model_latest.md"],
    "execution-realism": ["results/execution_realism_latest.json", "reports/execution_realism_latest.md"],
    "smart-stop": ["results/smart_stop_latest.json", "reports/smart_stop_latest.md"],
    "risk-alerts": ["results/risk_alerts_latest.json", "reports/risk_alerts_latest.md"],
    "notify": ["results/notification_center_latest.json", "reports/notification_center_latest.md"],
    "style-attribution": ["results/style_attribution_latest.json", "reports/style_attribution_latest.md"],
    "return-explain": ["results/return_explain_latest.json", "reports/return_explain_latest.md"],
    "stress-test": ["results/stress_test_latest.json", "reports/stress_test_latest.md"],
    "similar-day": ["results/similar_day_latest.json", "reports/similar_day_latest.md"],
    "decision-journal": ["results/decision_journal_latest.json", "reports/decision_journal_latest.md"],
    "money-flow": ["results/money_flow_latest.json", "reports/money_flow_latest.md"],
    "dragon-tiger": ["results/dragon_tiger_latest.json", "reports/dragon_tiger_latest.md"],
    "fundamental-metrics": ["results/fundamental_metrics_latest.json", "reports/fundamental_metrics_latest.md"],
    "local-store": ["results/local_store_latest.json", "reports/local_store_latest.md"],
    "attribution": ["results/risk_attribution_latest.json", "reports/risk_attribution_latest.md"],
    "risk-observation": ["results/risk_attribution_lite_latest.json", "reports/risk_attribution_lite_latest.md"],
    "risk-attribution": ["results/risk_attribution_lite_latest.json", "reports/risk_attribution_lite_latest.md"],
    "experiments": ["results/experiments/index.csv"],
    "first-run": ["~/.quantdesk/config.yaml", "state/intraday_live_account.json"],
    "wizard": ["~/.quantdesk/config.yaml", "state/intraday_live_account.json"],
    "walk-forward": ["results/walk_forward_latest.json"],
    "paper": ["results/auto_paper_portfolio_latest.json"],
    "shadow-weekly": [
        "reports/risk_gate_shadow/risk_gate_shadow_weekly_latest.json",
        "reports/risk_gate_shadow/risk_gate_shadow_weekly_latest.md",
    ],
    "stage4-maintenance": [
        "results/data_health/stage4_maintenance_evidence_latest.json",
        "results/data_health/stage4_final_evaluation_latest.json",
    ],
    "shared-paper-plan": [
        "results/shared_paper_plan_latest.json",
        "reports/shared_paper_plan_latest.md",
    ],
    "paper-sim": ["results/paper_trading_simulator_latest.json"],
    "sandbox": ["results/auto_strategy_latest.json"],
    "dev-preflight": [
        "results/data_update/data_update_latest.json",
        "reports/data_update_report_latest.md",
    ],
    "data-health": [
        "results/data_health/data_health_dashboard_latest.json",
        "reports/data_health_dashboard_report_latest.md",
        "data/quantdesk.db",
    ],
    "data-health-alerts": [
        "results/notifications/data_health",
        "state/data_health_alert_delivery.json",
    ],
    "daily-data-chain": ["results/data_chain/daily_data_chain_latest.json"],
    "weekly-minute-wide-pool": ["results/data_chain/weekly_minute_wide_pool/runs"],
    "weekly-daily-wide-pool": ["results/data_chain/weekly_daily_wide_pool/runs"],
    "full-a-reconcile": [
        "results/data_chain/full_a_daily_reconciliation_latest.json",
        "results/data_chain/full_a_daily_reconciliation/runs",
    ],
    "morning-brief": [
        "results/morning_brief/morning_brief_latest.json",
        "reports/morning_brief/morning_brief_latest.md",
    ],
    "minute-gap-backfill": ["results/data_chain/round13_minute_gap_backfill/runs"],
    "daily-retain": ["results/daily_retention"],
    "manifest-check": ["results/daily_retention"],
    "archive-results": ["results/archive"],
    "command-center": ["results/command_center_status_latest.json"],
    "llm-smoke": ["results/llm_observation_agent_smoke_latest.json"],
    "agent-report-audit": ["results/strategy_observation_report_audit_latest.json"],
}


def _arg_value(args: list[str], name: str, default: str = "") -> str:
    prefix = f"{name}="
    for index, item in enumerate(args):
        if item == name and index + 1 < len(args):
            return args[index + 1]
        if item.startswith(prefix):
            return item.split("=", 1)[1]
    return default


def _normalize_aliases(args: list[str], *, preserve_network_flags: bool = False) -> list[str]:
    out: list[str] = []
    for item in args:
        if item == "--no-network" and not preserve_network_flags:
            out.extend(["--network", "false"])
        elif item == "--enable-network" and not preserve_network_flags:
            out.extend(["--network", "true"])
        elif item == "--no-llm":
            out.extend(["--llm", "false"])
        elif item == "--enable-llm":
            out.extend(["--llm", "true"])
        else:
            out.append(item)
    return out


def _network_enabled_from_args(args: list[str]) -> bool:
    """Return qd.py's view of network flags for operation_log only."""

    if "--no-network" in args:
        return False
    if "--enable-network" in args:
        return True
    value = "false"
    for index, item in enumerate(args):
        if item == "--network" and index + 1 < len(args):
            value = args[index + 1]
        elif item.startswith("--network="):
            value = item.split("=", 1)[1]
    return truthy(value)


def _run_child_script_in_process(script_path: Path, args: list[str], child_env: dict[str, str]) -> int:
    """Run a qd target script inside the current interpreter for bounded tests."""
    old_argv = sys.argv[:]
    old_environ = os.environ.copy()
    old_cwd = Path.cwd()
    inserted_scripts_dir = False
    if str(SCRIPTS_DIR) not in sys.path:
        sys.path.insert(0, str(SCRIPTS_DIR))
        inserted_scripts_dir = True
    try:
        os.chdir(PROJECT_ROOT)
        os.environ.clear()
        os.environ.update(child_env)
        sys.argv = [str(script_path), *args]
        try:
            runpy.run_path(str(script_path), run_name="__main__")
        except SystemExit as exc:
            code = exc.code
            if code is None:
                return 0
            if isinstance(code, int):
                return code
            return 1
        return 0
    finally:
        sys.argv = old_argv
        os.chdir(old_cwd)
        os.environ.clear()
        os.environ.update(old_environ)
        if inserted_scripts_dir:
            try:
                sys.path.remove(str(SCRIPTS_DIR))
            except ValueError:
                pass


def _should_run_child_in_process(command: str) -> bool:
    if command in IN_PROCESS_DISABLED_COMMANDS:
        return False
    return truthy(os.environ.get("QUANTDESK_QD_IN_PROCESS", "false"))


def _json_output_requested(args: list[str]) -> bool:
    return truthy(_arg_value(args, "--json", "false"))


def _collect_log_warnings(payload: dict) -> list[str]:
    warnings: list[str] = []
    seen: set[str] = set()

    def add(value: object) -> None:
        text = str(value or "").strip()
        if text and text not in seen:
            seen.add(text)
            warnings.append(text)

    for key in ("warnings", "warnings_top", "warning_messages", "warning_kinds"):
        value = payload.get(key)
        if isinstance(value, (list, tuple)):
            for item in value:
                add(item)
        elif value:
            add(value)

    if not warnings:
        try:
            warning_count = int(payload.get("warning_count") or 0)
        except (TypeError, ValueError):
            warning_count = 0
        if warning_count > 0:
            warning_kind = str(payload.get("warning_kind") or "").strip()
            add(f"{warning_kind or 'warning_count'}={warning_count}")

    return warnings[:20]


def print_help() -> None:
    print("QuantDesk unified entrypoint")
    print("")
    print("Usage: python scripts/qd.py <command> [args...]")
    print("Windows note: 'python' is a placeholder for your active interpreter; use py or set QUANTDESK_PYTHON for .cmd helpers if needed.")
    print("Safety defaults: no broker API, no real orders, no investment advice, network=false, LLM=false.")
    print("Use --enable-network or --network true only when you explicitly allow data fetching.")
    print("--summary true prints compact operator-readable output where supported; combine with --json true for compact JSON.")
    print("\u4e0d\u77e5\u9053\u8be5\u8dd1\u4ec0\u4e48\uff1f\u5148\u8fd0\u884c python scripts/qd.py status")
    print("Python unsure? Run python scripts/qd.py env-doctor --json true first.")
    print("")
    print("Recommended first-run loop:")
    print("  1. doctor")
    print("  2. env-doctor --json true")
    print("  3. data-bootstrap --preset low_price_demo --network false")
    print("  4. data-update --enable-network --scope current_analysis_pool  (or manually import data/daily CSV files)")
    print("  5. data-health --network false --json true --summary true")
    print("  6. build-features")
    print("  7. screen --json true")
    print("  8. backtest --network false --json true")
    print("  9. walk-forward --network false")
    print(" 10. paper")
    print(" 11. daily --network false --with-guardrails true")
    print(" 12. app")
    print("")
    print("Recommended daily loop:")
    print("  python scripts/qd.py status")
    print("  python scripts/qd.py fix-plan")
    print("  python scripts/qd.py daily-data-chain --enable-network --json true --summary true")
    print("  python scripts/qd.py daily --network false --with-guardrails true")
    print("  python scripts/qd.py app")
    print("  python scripts/qd.py app --update-on-start --update-max-symbols 80  (explicit startup refresh)")
    print("")
    groups = [
        (
            "\u63a8\u8350\u65b0\u624b\u547d\u4ee4 / Beginner commands",
            [
                ("pause", "Stop the close pipeline and 20:20 AI. Readers stay on."),
                ("resume", "Restore only what pause stopped."),
                ("status --json true", "Return local status as JSON."),
                ("fix-plan --json true", "Return local repair plan as JSON."),
                ("env-doctor --json true", "Check Python executable, PATH python, py launcher, and encodings."),
                ("doctor", "Run self checks."),
                ("data-bootstrap --preset low_price_demo --network false", "Create/check the demo universe only; no market data is fetched."),
                ("data-bootstrap --preset low_price_demo --enable-network", "Create the demo universe and explicitly try to fetch daily data."),
                ("data-update --enable-network --scope current_analysis_pool", "Explicitly fetch/fill daily data for the current analysis pool."),
                ("data-health --network false --json true --summary true", "Check local data health with compact JSON stdout."),
                ("data-universe build-listings --network false --write true --json true --summary true", "Build runtime degraded PIT historical listings from local CSV."),
                ("data-universe validate-listings --json true --summary true", "Validate runtime historical listings for PIT research."),
                ("build-features", "Build local features from data/daily."),
                ("screen --json true --summary true", "Run local feature screening with compact JSON stdout."),
                ("backtest --network false --json true --summary true", "Run local backtest from data/daily with compact JSON stdout."),
                ("factor-eval --network false --json true --summary true", "Run offline factor IC/Rank IC diagnostics with compact JSON stdout."),
                ("factor-combine --weights \"momentum_20d=1.0\" --network false --json true --summary true", "Build research-only weighted combined scores with compact JSON stdout."),
                ("combined-score-backtest --pit-mode price_coverage --network false --json true --summary true", "Backtest combined-score ranking portfolios with degraded PIT warning when needed."),
                ("strategy-pipeline precheck --json true --summary true", "Check PIT, schema, benchmark, and strategy pipeline readiness."),
                ("maintenance git-deletion-audit --json true --summary true", "Audit tracked deletions and untracked files without modifying git state."),
                ("maintenance import-alias-reconciliation --json true --summary true", "Verify deprecated root import aliases without restoring old modules."),
                ("maintenance strategy-regression-audit --json true --summary true", "Audit strategy result regression and latest-report provenance."),
                ("maintenance script-inventory --json true --summary true", "Inventory qd.py entrypoints, raw scripts, and versioned historical scripts."),
                ("maintenance test-inventory --json true --summary true", "Inventory test files by fast/domain/CLI/release tiers."),
                ("maintenance test-discovery-diagnostics --json true --summary true", "Probe active-tier unittest discovery/import hot spots before any broad regression or legacy sweep."),
                ("maintenance test-tier-runner --tier tier0_fast_unit --dry-run true --json true --summary true", "Plan or run a bounded tier selection from the test inventory."),
                ("maintenance pytest-suite-runner --suite all --dry-run true --json true --summary true", "Plan or run pytest split by core/api/research/v3-v4 governance/slow suites."),
                ("maintenance frontend-api-contract --json true --summary true", "Compare frontend API calls with local server routes."),
                ("maintenance dependency-contract --json true --summary true", "Verify pyproject and requirements dependency declarations stay aligned."),
                ("maintenance data-backtest-contract --json true --summary true", "Audit latest data/backtest output assumptions, artifact hashes, and manifest state."),
                ("maintenance coverage-contract --json true --summary true", "Run bounded pytest-cov coverage for maintenance governance tests."),
                ("strategy-viability capital-lot-sensitivity --json true --summary true", "Measure capital, position-count, price-threshold, and lot-size effects."),
                ("selected-symbols --summary true", "Export verified selected-symbol snapshots for research-only holdings materialization."),
                ("holdings-reconstruct --summary true", "Materialize candidate holdings from verified selected-symbol snapshots."),
                ("sector-rotation --network false --json true --summary true", "Run sector rotation diagnostics with compact JSON stdout."),
                ("research-dashboard --network false --json true --summary true", "Summarize factor research health with compact JSON stdout."),
                ("pit-universe --json true --summary true", "Inspect point-in-time universe availability with compact JSON stdout."),
                ("multi-strategy-book --json true --summary true", "Build a paper-only multi-strategy account book with compact JSON stdout."),
                ("live --once --json true --summary true", "Refresh the research-only intraday live workbench once with compact JSON stdout."),
                ("live --init --json true --summary true", "Initialize the research-only intraday live account state template."),
                ("strategy-recommend --synthetic-test-data true --json true --summary true", "Run research-only strategy recommendation demo with compact JSON stdout."),
                ("attribution --synthetic-test-data true --json true --summary true", "Run true Brinson/factor attribution demo with compact JSON stdout."),
                ("risk-observation --json true --summary true", "Run risk observation diagnostics with compact JSON stdout."),
                ("experiments --json true --summary true", "List recent research experiments with compact JSON stdout."),
                ("first-run --json true --summary true", "Run v3.5.0 first-run setup with compact JSON stdout."),
                ("walk-forward --network false --summary true", "Run local walk-forward validation with compact stdout."),
                ("paper", "Run local paper portfolio workflow."),
                ("app", "Start the current daily research app; no startup provider update unless --update-on-start is explicit."),
            ],
        ),
        (
            "\u5e38\u7528\u9a8c\u8bc1 / Validation and governance",
            [
                ("strategy-validate --target-date YYYY-MM-DD --network false", "Check lookahead and execution assumptions."),
                ("consistency-check --symbol 600744 --target-date YYYY-MM-DD --network false", "Check paper/backtest consistency."),
                ("feature-audit --symbols 600744,002115 --target-date YYYY-MM-DD --network false", "Audit feature quality."),
                ("stats-validate --symbol 600744 --target-date YYYY-MM-DD --network false", "Run statistical validation."),
                ("param-sensitivity --symbol 600744 --target-date YYYY-MM-DD --network false", "Audit parameter sensitivity."),
                ("factor-eval --network false --summary true", "Evaluate factor IC, Rank IC, decay, and quantile returns for research only."),
                ("factor-combine --network false --summary true", "Build neutralized combined factor scores for research only."),
                ("combined-score-backtest --network false --summary true", "Run research-only Top-N/quantile combined-score backtest."),
                ("sector-rotation --network false --summary true", "Run research-only sector strength, breadth, and rank-change diagnostics."),
                ("research-dashboard --network false --summary true", "Review factor-eval, factor-combine, score-backtest, and sector-rotation together."),
                ("pit-universe --summary true", "Show PIT universe coverage and survivorship-bias diagnostics."),
                ("multi-strategy-book --summary true", "Build a research-only multi-strategy virtual account book."),
                ("live --once --summary true", "Refresh provisional intraday observation; partial data never feeds backtests."),
                ("strategy-recommend --summary true", "Recommend research-only strategy style without generating orders."),
                ("attribution --summary true", "Run true Brinson/factor attribution without generating trading advice."),
                ("risk-observation --summary true", "Explain observation diagnostics without calling them true attribution."),
                ("experiments --summary true", "Review local research experiment logs."),
                ("wizard --summary true", "Run first-run setup using safe local defaults."),
                ("schema-check --results-dir results --network false", "Validate result schemas."),
                ("shadow-weekly --network false --json true --summary true", "Aggregate five validated shadow observations without enabling the risk gate."),
                ("stage4-maintenance --network false --json true --summary true", "Collect Stage 4 maintenance evidence and gated real paper-account replay without outcome conclusions."),
                ("morning-brief --network false --dry-run true --json true", "Build the local research morning brief preview without external delivery."),
                ("weekly-daily-wide-pool --network false --json true --summary true", "Stage the weekly daily-wide pool contract offline without applying."),
                ("minute-gap-backfill --network false --json true --summary true", "Stage the five authorized minute gap backfills offline without applying."),
            ],
        ),
        (
            "Maintenance and optional flows",
            [
                ("command-center", "Read recent command status."),
                ("providers --network false", "Check provider availability without network smoke tests."),
                ("data-gates --summary true", "Show the current extended-data gate chain in one read-only command."),
                ("tushare-queue-status --summary true", "Read local Tushare download queue logs/status; no token output."),
                ("tushare-minute-status --summary true", "Read local Tushare 1-minute download logs/status; no token output."),
                ("tushare-minute-coverage --summary true", "Read local 1-minute target-pool coverage; no import or data writes."),
                ("tushare-minute-quality --summary true", "Audit the latest completed local 1-minute job in canonical data; no import or data writes."),
                ("tushare-retry-plan --summary true", "Group local Tushare FAIL/RETRY logs into a read-only retry plan; no auto restart."),
                ("verify-extended-import --summary true", "Verify canonical extended-data import presence after a user-approved import."),
                ("market-intel --target-date YYYY-MM-DD --network false --scope current_analysis_pool", "Run market observation, paper-only."),
                ("observation-report --target-date YYYY-MM-DD --network false --llm false", "Generate a local learning report."),
                ("holdings-export --export-holdings false", "Preview explicit holdings export hook; default is off."),
                ("archive-results --target-date YYYY-MM-DD --dry-run true", "Preview artifact archive."),
                ("llm-smoke --network false", "Check optional LLM wiring only; LLM remains off unless enabled."),
            ],
        ),
    ]
    for title, rows in groups:
        print(f"{title}:")
        for command, description in rows:
            print(f"  {command:<88} {description}")
        print("")
    print("All commands:")
    for name, (_script, _defaults, description) in COMMANDS.items():
        print(f"  {name:<20} {description}")
    print("")
    print("Advanced scripts remain in scripts/. For normal use, prefer qd.py.")
    print("QuantDesk does not connect to brokers, place real orders, or provide investment advice.")


def main(argv: list[str] | None = None) -> int:
    args = list(argv if argv is not None else sys.argv[1:])
    if not args or args[0] in {"help", "-h", "--help"}:
        print_help()
        return 0
    command = args.pop(0)
    preserve_network_flags = command in {"data-update", "daily-data-chain", "weekly-minute-wide-pool", "weekly-daily-wide-pool", "full-a-reconcile", "morning-brief", "data-health-alerts", "minute-gap-backfill", "dev-preflight", "data-db", "data-universe", "build-universe", "data-query", "data-health", "data-quality"}
    args = _normalize_aliases(args, preserve_network_flags=preserve_network_flags)
    started_at = now_text()
    start_time = time.perf_counter()
    if command not in COMMANDS:
        print(f"Unknown command: {command}", file=sys.stderr)
        print("Run: python scripts/qd.py help", file=sys.stderr)
        log_operation(
            command=command,
            args=args,
            status="failed",
            started_at=started_at,
            start_time=start_time,
            failed_reason="unknown_command",
            suggested_next_action="Run python scripts/qd.py help to list available commands.",
        )
        return 2
    script, defaults, _description = COMMANDS[command]
    script_path = SCRIPTS_DIR / script
    if not script_path.exists():
        print(f"Entrypoint script is missing: {script_path}", file=sys.stderr)
        print(f"Try the raw command after restoring scripts/: python scripts/{script}", file=sys.stderr)
        log_operation(
            command=command,
            args=args,
            status="failed",
            started_at=started_at,
            start_time=start_time,
            failed_reason=f"missing script: {script}",
            suggested_next_action="Restore the scripts directory from a clean zip, then retry.",
        )
        return 2
    cmd = [sys.executable, str(script_path), *defaults, *args]
    child_env = os.environ.copy()
    child_env["QUANTDESK_WRAPPED_BY_QD"] = "1"
    if _json_output_requested(args):
        child_env["PYTHONIOENCODING"] = "ascii:backslashreplace"
    else:
        child_env.setdefault("PYTHONIOENCODING", "utf-8")
    if _should_run_child_in_process(command):
        returncode = _run_child_script_in_process(script_path, [*defaults, *args], child_env)
    else:
        completed = subprocess.run(cmd, cwd=PROJECT_ROOT, env=child_env)  # noqa: S603 - local known scripts only
        returncode = int(completed.returncode)
    troubleshooting_command = subprocess.list2cmdline(["python", "scripts/qd.py", command, *args])
    if returncode != 0:
        print(f"Command failed. Re-run for troubleshooting: {troubleshooting_command}", file=sys.stderr)
    log_status = "passed" if returncode == 0 else "failed"
    log_warnings: list[str] = []
    latest_paths = {
        "status": PROJECT_ROOT / "results" / "status_latest.json",
        "fix-plan": PROJECT_ROOT / "results" / "fix_plan_latest.json",
        "daily": PROJECT_ROOT / "results" / "daily_workflow_latest.json",
        "market-intel": PROJECT_ROOT / "results" / "market_intelligence" / "market_intelligence_latest.json",
        "mainline-radar": PROJECT_ROOT / "results" / "market_intelligence" / "mainline_radar_latest.json",
        "data-readiness": PROJECT_ROOT / "results" / "data_health" / "extended_data_readiness_latest.json",
        "data-gates": PROJECT_ROOT / "results" / "data_health" / "extended_data_gates_latest.json",
        "tushare-queue-status": PROJECT_ROOT / "results" / "data_health" / "tushare_queue_status_latest.json",
        "tushare-minute-status": PROJECT_ROOT / "results" / "data_health" / "tushare_minute_status_latest.json",
        "tushare-minute-coverage": PROJECT_ROOT / "results" / "data_health" / "tushare_minute_coverage_latest.json",
        "tushare-minute-quality": PROJECT_ROOT / "results" / "data_health" / "tushare_minute_quality_latest.json",
        "minute-pool-audit": PROJECT_ROOT / "results" / "data_health" / "minute_pool_window_audit_latest.json",
        "tushare-retry-plan": PROJECT_ROOT / "results" / "data_health" / "tushare_retry_plan_latest.json",
        "import-extended-data": PROJECT_ROOT / "results" / "data_health" / "extended_staged_import_plan_latest.json",
        "audit-extended-import": PROJECT_ROOT / "results" / "data_health" / "extended_staged_import_audit_latest.json",
        "verify-extended-import": PROJECT_ROOT / "results" / "data_health" / "extended_import_verification_latest.json",
        "staged-provider-audit": PROJECT_ROOT / "results" / "data_health" / "staged_provider_evidence_audit_latest.json",
        "staged-import-plan": PROJECT_ROOT / "results" / "data_health" / "staged_import_plan_latest.json",
        "data-health": PROJECT_ROOT / "results" / "data_health" / "data_health_dashboard_latest.json",
        "data-health-alerts": PROJECT_ROOT / "results" / "notifications" / "data_health",
        "shadow-weekly": PROJECT_ROOT / "reports" / "risk_gate_shadow" / "risk_gate_shadow_weekly_latest.json",
        "stage4-maintenance": PROJECT_ROOT / "results" / "data_health" / "stage4_maintenance_evidence_latest.json",
        "stage4-final-evaluation": PROJECT_ROOT / "results" / "data_health" / "stage4_final_evaluation_latest.json",
        "daily-data-chain": PROJECT_ROOT / "results" / "data_chain" / "daily_data_chain_latest.json",
        "weekly-daily-wide-pool": PROJECT_ROOT / "results" / "data_chain" / "weekly_daily_wide_pool" / "runs",
        "full-a-reconcile": PROJECT_ROOT / "results" / "data_chain" / "full_a_daily_reconciliation_latest.json",
        "morning-brief": PROJECT_ROOT / "results" / "morning_brief" / "morning_brief_latest.json",
        "minute-gap-backfill": PROJECT_ROOT / "results" / "data_chain" / "round13_minute_gap_backfill" / "runs",
        "data-bootstrap": PROJECT_ROOT / "results" / "data_bootstrap" / "data_bootstrap_latest.json",
        "env-doctor": PROJECT_ROOT / "results" / "python_env_latest.json",
        "data-update": PROJECT_ROOT / "results" / "data_update" / "data_update_latest.json",
        "data-db": PROJECT_ROOT / "data" / "quantdesk.db",
        "data-universe": PROJECT_ROOT / "results" / "historical_listings_build_latest.json",
        "build-universe": PROJECT_ROOT / "results" / "universe" / "build_universe_latest.json",
        "build-features": PROJECT_ROOT / "data" / "features" / "daily_features_summary.json",
        "screen": PROJECT_ROOT / "results" / "screen" / "screen_latest.json",
        "backtest": PROJECT_ROOT / "results" / "backtest" / "backtest_latest.json",
        "factor-eval": PROJECT_ROOT / "results" / "factor_health_latest.json",
        "factor-panel": PROJECT_ROOT / "results" / "factor_panel_latest.json",
        "factor-ic": PROJECT_ROOT / "results" / "factor_ic_report_latest.json",
        "alpha-adjudication": PROJECT_ROOT / "results" / "alpha_adjudication" / "alpha_latest.json",
        "factor-combine": PROJECT_ROOT / "results" / "combined_score_latest.json",
        "v38-cleanup-audit": PROJECT_ROOT / "results" / "v38_cleanup_audit_latest.json",
        "v38-daily-pipeline": PROJECT_ROOT / "results" / "v38_daily_pipeline_latest.json",
        "v38-progress-audit": PROJECT_ROOT / "results" / "v38_progress_audit_latest.json",
        "v38-final-readiness": PROJECT_ROOT / "results" / "v38_final_readiness_latest.json",
        "v38-runtime-cleanup": PROJECT_ROOT / "results" / "v38_runtime_cleanup_plan_latest.json",
        "v38-runtime-cleanup-validate": PROJECT_ROOT / "results" / "v38_runtime_cleanup_preapply_validation_latest.json",
        "risk-stat": PROJECT_ROOT / "results" / "risk_stat_validation_latest.json",
        "combined-score-backtest": PROJECT_ROOT / "results" / "combined_score_backtest_latest.json",
        "strategy-pipeline": PROJECT_ROOT / "results" / "strategy_pipeline_precheck_latest.json",
        "sector-rotation": PROJECT_ROOT / "results" / "sector_rotation_latest.json",
        "research-dashboard": PROJECT_ROOT / "results" / "research_dashboard_latest.json",
        "pit-universe": PROJECT_ROOT / "results" / "pit_universe_status_latest.json",
        "multi-strategy-book": PROJECT_ROOT / "results" / "multi_strategy_account_book_latest.json",
        "strategy-recommend": PROJECT_ROOT / "results" / "strategy_recommendation_latest.json",
        "selected-symbols": PROJECT_ROOT / "results" / "holdings" / "canonical_selected_symbols_latest.json",
        "holdings-reconstruct": PROJECT_ROOT / "results" / "holdings" / "candidate_holdings_reconstruction_latest.json",
        "holdings-export": PROJECT_ROOT / "results" / "holdings" / "candidate_holdings_snapshot_latest.json",
        "stock-picking-flow": PROJECT_ROOT / "results" / "stock_picking_flow_latest.json",
        "paper-watchlist": PROJECT_ROOT / "results" / "paper_watchlist_latest.json",
        "candidate-review": PROJECT_ROOT / "results" / "daily_candidate_review_latest.json",
        "data-integrity": PROJECT_ROOT / "results" / "data_integrity_latest.json",
        "data-audit": PROJECT_ROOT / "results" / "data_audit.json",
        "dynamic-factor-model": PROJECT_ROOT / "results" / "dynamic_factor_model_latest.json",
        "execution-realism": PROJECT_ROOT / "results" / "execution_realism_latest.json",
        "smart-stop": PROJECT_ROOT / "results" / "smart_stop_latest.json",
        "risk-alerts": PROJECT_ROOT / "results" / "risk_alerts_latest.json",
        "notify": PROJECT_ROOT / "results" / "notification_center_latest.json",
        "style-attribution": PROJECT_ROOT / "results" / "style_attribution_latest.json",
        "return-explain": PROJECT_ROOT / "results" / "return_explain_latest.json",
        "stress-test": PROJECT_ROOT / "results" / "stress_test_latest.json",
        "similar-day": PROJECT_ROOT / "results" / "similar_day_latest.json",
        "decision-journal": PROJECT_ROOT / "results" / "decision_journal_latest.json",
        "money-flow": PROJECT_ROOT / "results" / "money_flow_latest.json",
        "dragon-tiger": PROJECT_ROOT / "results" / "dragon_tiger_latest.json",
        "fundamental-metrics": PROJECT_ROOT / "results" / "fundamental_metrics_latest.json",
        "local-store": PROJECT_ROOT / "results" / "local_store_latest.json",
        "experiments": PROJECT_ROOT / "results" / "experiments" / "index.csv",
        "walk-forward": PROJECT_ROOT / "results" / "walk_forward_latest.json",
        "dev-preflight": PROJECT_ROOT / "results" / "data_update" / "data_update_latest.json",
        "param-sensitivity": PROJECT_ROOT / "results" / "parameter_sensitivity_latest.json",
        "schema-check": PROJECT_ROOT / "results" / "schema_validation_latest.json",
    }
    if command in latest_paths and returncode == 0:
        latest = read_json(latest_paths[command], {})
        if isinstance(latest, dict):
            latest_status = str(latest.get("status") or latest.get("workflow_status") or log_status)
            log_status = "warning" if latest_status == "partial" else latest_status
            log_warnings = _collect_log_warnings(latest)
    log_operation(
        command=command,
        args=args,
        target_date=_arg_value(args, "--target-date"),
        symbols=_arg_value(args, "--symbols") or _arg_value(args, "--symbol"),
        network=_network_enabled_from_args(args),
        llm_enabled=truthy(_arg_value(args, "--llm", "false")),
        status=log_status,
        started_at=started_at,
        start_time=start_time,
        output_paths=OUTPUT_PATHS.get(command, []),
        warnings=log_warnings,
        failed_reason="" if returncode == 0 else f"exit_code={returncode}",
        suggested_next_action="" if returncode == 0 else f"Re-run for troubleshooting: {troubleshooting_command}",
    )
    return int(returncode)


if __name__ == "__main__":
    raise SystemExit(main())
