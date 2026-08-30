from __future__ import annotations

import argparse
import hashlib
import json
import sys
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]

BASE_REQUIRED_FILES = [
    "quantdesk/version.py",
    "quantdesk/research/combined_score_backtest.py",
    "scripts/run_combined_score_backtest.py",
    "tests/test_v3432_combined_score_research_backtest.py",
    "quantdesk/research/factor_combiner.py",
    "scripts/build_combined_score.py",
    "quantdesk/research/factor_evaluation.py",
    "scripts/run_factor_evaluation.py",
    "CHANGELOG.md",
    "README.md",
]

VERSION_REQUIRED_FILES = {
    "3.4.3.3": [
        "quantdesk/research/sector_rotation_engine.py",
        "scripts/run_sector_rotation.py",
        "tests/test_v3433_sector_rotation_engine.py",
    ],
    "3.4.3.4": [
        "quantdesk/research/sector_rotation_engine.py",
        "scripts/run_sector_rotation.py",
        "tests/test_v3433_sector_rotation_engine.py",
        "quantdesk/research/factor_research_dashboard.py",
        "scripts/run_factor_research_dashboard.py",
        "tests/test_v3434_factor_research_dashboard.py",
    ],
    "3.4.3.5": [
        "quantdesk/research/sector_rotation_engine.py",
        "scripts/run_sector_rotation.py",
        "tests/test_v3433_sector_rotation_engine.py",
        "quantdesk/research/factor_research_dashboard.py",
        "scripts/run_factor_research_dashboard.py",
        "tests/test_v3434_factor_research_dashboard.py",
        "quantdesk/research/point_in_time_universe.py",
        "scripts/show_pit_universe_status.py",
        "tests/test_v3435_point_in_time_universe_lite_and_project_hygiene.py",
        "docs/faq.md",
        "architecture_inventory.md",
    ],
    "3.4.3.6": [
        "quantdesk/research/sector_rotation_engine.py",
        "scripts/run_sector_rotation.py",
        "tests/test_v3433_sector_rotation_engine.py",
        "quantdesk/research/factor_research_dashboard.py",
        "scripts/run_factor_research_dashboard.py",
        "tests/test_v3434_factor_research_dashboard.py",
        "quantdesk/research/point_in_time_universe.py",
        "scripts/show_pit_universe_status.py",
        "tests/test_v3435_point_in_time_universe_lite_and_project_hygiene.py",
        "quantdesk/daily_research_snapshot.py",
        "tests/test_v3436_research_dashboard_daily_snapshot.py",
        "docs/faq.md",
        "architecture_inventory.md",
    ],
    "3.4.3.7": [
        "quantdesk/research/sector_rotation_engine.py",
        "scripts/run_sector_rotation.py",
        "tests/test_v3433_sector_rotation_engine.py",
        "quantdesk/research/factor_research_dashboard.py",
        "scripts/run_factor_research_dashboard.py",
        "tests/test_v3434_factor_research_dashboard.py",
        "quantdesk/research/point_in_time_universe.py",
        "scripts/show_pit_universe_status.py",
        "tests/test_v3435_point_in_time_universe_lite_and_project_hygiene.py",
        "quantdesk/daily_research_snapshot.py",
        "tests/test_v3436_research_dashboard_daily_snapshot.py",
        "quantdesk/backtest_assumptions.py",
        "tests/test_v3437_backtest_assumptions_split_and_release_proof_polish.py",
        "docs/faq.md",
        "architecture_inventory.md",
    ],
    "3.4.3.8": [
        "quantdesk/research/sector_rotation_engine.py",
        "scripts/run_sector_rotation.py",
        "tests/test_v3433_sector_rotation_engine.py",
        "quantdesk/research/factor_research_dashboard.py",
        "scripts/run_factor_research_dashboard.py",
        "tests/test_v3434_factor_research_dashboard.py",
        "quantdesk/research/point_in_time_universe.py",
        "scripts/show_pit_universe_status.py",
        "tests/test_v3435_point_in_time_universe_lite_and_project_hygiene.py",
        "quantdesk/daily_research_snapshot.py",
        "tests/test_v3436_research_dashboard_daily_snapshot.py",
        "quantdesk/backtest_assumptions.py",
        "tests/test_v3437_backtest_assumptions_split_and_release_proof_polish.py",
        "tests/test_v3438_true_backtest_assumptions_split_and_doc_hygiene.py",
        "docs/faq.md",
        "architecture_inventory.md",
    ],
    "3.4.4.0": [
        "quantdesk/research/sector_rotation_engine.py",
        "scripts/run_sector_rotation.py",
        "tests/test_v3433_sector_rotation_engine.py",
        "quantdesk/research/factor_research_dashboard.py",
        "scripts/run_factor_research_dashboard.py",
        "tests/test_v3434_factor_research_dashboard.py",
        "quantdesk/research/point_in_time_universe.py",
        "scripts/show_pit_universe_status.py",
        "tests/test_v3435_point_in_time_universe_lite_and_project_hygiene.py",
        "quantdesk/daily_research_snapshot.py",
        "tests/test_v3436_research_dashboard_daily_snapshot.py",
        "quantdesk/backtest_assumptions.py",
        "tests/test_v3437_backtest_assumptions_split_and_release_proof_polish.py",
        "tests/test_v3438_true_backtest_assumptions_split_and_doc_hygiene.py",
        "quantdesk/paper/multi_strategy_account_book.py",
        "scripts/run_multi_strategy_account_book.py",
        "tests/test_v3440_multi_strategy_account_book.py",
        "docs/faq.md",
        "architecture_inventory.md",
    ],
    "3.4.4.1": [
        "quantdesk/research/sector_rotation_engine.py",
        "scripts/run_sector_rotation.py",
        "tests/test_v3433_sector_rotation_engine.py",
        "quantdesk/research/factor_research_dashboard.py",
        "scripts/run_factor_research_dashboard.py",
        "tests/test_v3434_factor_research_dashboard.py",
        "quantdesk/research/point_in_time_universe.py",
        "scripts/show_pit_universe_status.py",
        "tests/test_v3435_point_in_time_universe_lite_and_project_hygiene.py",
        "quantdesk/daily_research_snapshot.py",
        "tests/test_v3436_research_dashboard_daily_snapshot.py",
        "quantdesk/backtest_assumptions.py",
        "tests/test_v3437_backtest_assumptions_split_and_release_proof_polish.py",
        "tests/test_v3438_true_backtest_assumptions_split_and_doc_hygiene.py",
        "quantdesk/paper/multi_strategy_account_book.py",
        "scripts/run_multi_strategy_account_book.py",
        "tests/test_v3440_multi_strategy_account_book.py",
        "quantdesk/intraday/intraday_live_engine.py",
        "quantdesk/intraday/intraday_live_snapshot.py",
        "quantdesk/intraday/intraday_score.py",
        "quantdesk/intraday/intraday_data_policy.py",
        "scripts/run_intraday_live_workbench.py",
        "tests/test_v3441_intraday_live_workbench_and_config_hygiene.py",
        "config/multi_strategy_account_book.example.yaml",
        "docs/faq.md",
        "architecture_inventory.md",
    ],
    "3.4.4.2": [
        "quantdesk/research/sector_rotation_engine.py",
        "scripts/run_sector_rotation.py",
        "tests/test_v3433_sector_rotation_engine.py",
        "quantdesk/research/factor_research_dashboard.py",
        "scripts/run_factor_research_dashboard.py",
        "tests/test_v3434_factor_research_dashboard.py",
        "quantdesk/research/point_in_time_universe.py",
        "scripts/show_pit_universe_status.py",
        "tests/test_v3435_point_in_time_universe_lite_and_project_hygiene.py",
        "quantdesk/daily_research_snapshot.py",
        "tests/test_v3436_research_dashboard_daily_snapshot.py",
        "quantdesk/backtest_assumptions.py",
        "tests/test_v3437_backtest_assumptions_split_and_release_proof_polish.py",
        "tests/test_v3438_true_backtest_assumptions_split_and_doc_hygiene.py",
        "quantdesk/paper/multi_strategy_account_book.py",
        "scripts/run_multi_strategy_account_book.py",
        "tests/test_v3440_multi_strategy_account_book.py",
        "quantdesk/intraday/intraday_live_engine.py",
        "quantdesk/intraday/intraday_live_snapshot.py",
        "quantdesk/intraday/intraday_score.py",
        "quantdesk/intraday/intraday_data_policy.py",
        "scripts/run_intraday_live_workbench.py",
        "tests/test_v3441_intraday_live_workbench_and_config_hygiene.py",
        "config/multi_strategy_account_book.example.yaml",
        "quantdesk/research/risk_attribution_lite.py",
        "quantdesk/research/daily_risk_attribution_snapshot.py",
        "scripts/run_risk_attribution_lite.py",
        "tests/test_v3442_risk_attribution_lite_and_live_polish.py",
        "docs/intraday_live_quickstart.md",
        "docs/faq.md",
        "architecture_inventory.md",
    ],
    "3.4.4.3": [
        "quantdesk/research/sector_rotation_engine.py",
        "scripts/run_sector_rotation.py",
        "tests/test_v3433_sector_rotation_engine.py",
        "quantdesk/research/factor_research_dashboard.py",
        "scripts/run_factor_research_dashboard.py",
        "tests/test_v3434_factor_research_dashboard.py",
        "quantdesk/research/point_in_time_universe.py",
        "scripts/show_pit_universe_status.py",
        "tests/test_v3435_point_in_time_universe_lite_and_project_hygiene.py",
        "quantdesk/daily_research_snapshot.py",
        "tests/test_v3436_research_dashboard_daily_snapshot.py",
        "quantdesk/backtest_assumptions.py",
        "tests/test_v3437_backtest_assumptions_split_and_release_proof_polish.py",
        "tests/test_v3438_true_backtest_assumptions_split_and_doc_hygiene.py",
        "quantdesk/paper/multi_strategy_account_book.py",
        "scripts/run_multi_strategy_account_book.py",
        "tests/test_v3440_multi_strategy_account_book.py",
        "quantdesk/intraday/intraday_live_engine.py",
        "quantdesk/intraday/intraday_live_snapshot.py",
        "quantdesk/intraday/intraday_score.py",
        "quantdesk/intraday/intraday_data_policy.py",
        "scripts/run_intraday_live_workbench.py",
        "tests/test_v3441_intraday_live_workbench_and_config_hygiene.py",
        "config/multi_strategy_account_book.example.yaml",
        "quantdesk/research/risk_attribution_lite.py",
        "quantdesk/research/daily_risk_attribution_snapshot.py",
        "scripts/run_risk_attribution_lite.py",
        "tests/test_v3442_risk_attribution_lite_and_live_polish.py",
        "docs/intraday_live_quickstart.md",
        "quantdesk/research/experiment_tracker.py",
        "quantdesk/research/daily_experiment_tracker_snapshot.py",
        "scripts/run_experiments.py",
        "tests/test_v3443_experiment_tracker_lite.py",
        "docs/faq.md",
        "architecture_inventory.md",
    ],
    "3.4.5.0": [
        "quantdesk/research/risk_attribution_lite.py",
        "quantdesk/research/daily_risk_attribution_snapshot.py",
        "scripts/run_risk_attribution_lite.py",
        "tests/test_v3442_risk_attribution_lite_and_live_polish.py",
        "quantdesk/research/experiment_tracker.py",
        "quantdesk/research/daily_experiment_tracker_snapshot.py",
        "scripts/run_experiments.py",
        "tests/test_v3443_experiment_tracker_lite.py",
        "quantdesk/research/chart_renderer.py",
        "scripts/validate_historical_listings.py",
        "scripts/build_historical_listings_template.py",
        "docs/point_in_time_universe.md",
        "docs/examples/historical_listings.example.csv",
        "tests/test_v3450_pit_full_and_static_charts.py",
        "docs/faq.md",
        "architecture_inventory.md",
    ],
    "3.4.5.1": [
        "quantdesk/backtest_loaders.py",
        "quantdesk/self_check_render.py",
        "quantdesk/research/__init__.py",
        "quantdesk/research/README.md",
        "quantdesk/intraday/__init__.py",
        "quantdesk/intraday/README.md",
        "quantdesk/paper/__init__.py",
        "quantdesk/paper/README.md",
        "quantdesk/agent/__init__.py",
        "quantdesk/agent/README.md",
        "docs/index.md",
        "docs/quickstart.md",
        "docs/user_guide.md",
        "docs/architecture.md",
        "tests/test_v3451_engineering_docs_and_package_grouping_lite.py",
        "docs/faq.md",
        "architecture_inventory.md",
    ],
    "3.5.1": [
        "quantdesk/research/risk_attribution.py",
        "quantdesk/research/factor_attribution.py",
        "quantdesk/research/risk_attribution_lite.py",
        "scripts/run_risk_attribution.py",
        "scripts/run_risk_attribution_lite.py",
        "tests/test_v351_true_attribution_release_honesty.py",
        "tests/test_v351_package_migration_truth.py",
        "tests/test_v351_backtest_slimming_regression.py",
        "tests/test_v351_packaging_honesty.py",
        "RELEASE_NOTES_v3.5.1.md",
    ],
    "3.5.1.1": [
        "quantdesk/research/risk_attribution.py",
        "quantdesk/research/factor_attribution.py",
        "quantdesk/research/risk_attribution_lite.py",
        "scripts/run_risk_attribution.py",
        "scripts/run_risk_attribution_lite.py",
        "scripts/clean_release_staging.ps1",
        "docs/release_build.md",
        "tests/test_v351_true_attribution_release_honesty.py",
        "tests/test_v351_package_migration_truth.py",
        "tests/test_v351_backtest_slimming_regression.py",
        "tests/test_v351_packaging_honesty.py",
        "tests/test_v3511_release_staging_cleanup_and_attribution_stress.py",
        "RELEASE_NOTES_v3.5.1.md",
        "RELEASE_NOTES_v3.5.1.1.md",
    ],
    "3.5.2": [
        "quantdesk/research/strategy_recommender.py",
        "quantdesk/research/daily_strategy_recommendation_snapshot.py",
        "quantdesk/research/strategy_recommender.py",
        "scripts/run_strategy_recommender.py",
        "tests/test_v352_ui_home_and_strategy_recommender.py",
        "RELEASE_NOTES_v3.5.2.md",
    ],
    "3.6.0": [
        "quantdesk/research/stock_picking_flow.py",
        "quantdesk/research/paper_watchlist.py",
        "quantdesk/research/daily_candidate_review.py",
        "quantdesk/data/price_adjustment.py",
        "quantdesk/data/corporate_actions.py",
        "quantdesk/research/dynamic_factor_model.py",
        "quantdesk/risk/smart_stop.py",
        "quantdesk/risk/risk_alerts.py",
        "quantdesk/notifications/notification_center.py",
        "quantdesk/research/execution_realism.py",
        "quantdesk/research/multiperiod_attribution.py",
        "quantdesk/research/barra_style_attribution.py",
        "quantdesk/research/stress_scenarios.py",
        "quantdesk/research/similar_day.py",
        "quantdesk/research/decision_journal.py",
        "quantdesk/data/money_flow.py",
        "quantdesk/data/dragon_tiger.py",
        "quantdesk/data/fundamental_metrics.py",
        "quantdesk/storage/local_store.py",
        "scripts/run_v360_builder.py",
        "scripts/run_data_integrity.py",
        "tests/test_v360_real_use_stock_picking_flow.py",
        "tests/test_v360_adjusted_price_data_integrity.py",
        "tests/test_v360_ui_and_release_hardening.py",
        "RELEASE_NOTES_v3.6.0.md",
    ],
    "3.6.1": [
        "quantdesk/risk/position_inputs.py",
        "quantdesk/risk/smart_stop.py",
        "quantdesk/risk/risk_alerts.py",
        "quantdesk/notifications/notification_center.py",
        "quantdesk/notifications/dedupe.py",
        "quantdesk/research/daily_risk_alert_snapshot.py",
        "scripts/run_v360_builder.py",
        "tests/test_v361_real_risk_alerts_and_smart_stop_integration.py",
        "RELEASE_NOTES_v3.6.1.md",
    ],
    "3.6.2": [
        "quantdesk/research/similar_day.py",
        "quantdesk/research/stress_scenarios.py",
        "quantdesk/research/barra_style_attribution.py",
        "quantdesk/research/decision_journal.py",
        "quantdesk/research/daily_research_snapshots.py",
        "scripts/check_text_encoding_hygiene.py",
        "tests/test_v362_real_similar_day_stress_and_style_attribution.py",
        "RELEASE_NOTES_v3.6.2.md",
        "templates/data/index_daily_sample.csv",
        "templates/data/style_exposures_sample.csv",
        "templates/data/style_factor_returns_sample.csv",
        "docs/data_schemas/similar_day_input.md",
        "docs/data_schemas/stress_scenarios_input.md",
        "docs/data_schemas/style_attribution_input.md",
        "docs/data_schemas/decision_journal.md",
    ],
    "3.7.0": [
        "quantdesk/data_store/__init__.py",
        "quantdesk/data_store/sqlite_store.py",
        "quantdesk/data_store/schema.py",
        "quantdesk/data_store/repositories.py",
        "quantdesk/data_store/migration.py",
        "quantdesk/data_store/health.py",
        "quantdesk/data_store/dual_writer.py",
        "quantdesk/data_store/query.py",
        "quantdesk/data_store/locks.py",
        "quantdesk/data_store/hash_utils.py",
        "quantdesk/data_store/intraday_parquet.py",
        "quantdesk/data_store/quality_repair.py",
        "quantdesk/data_providers/akshare_provider.py",
        "quantdesk/data_providers/fundamentals_chain.py",
        "quantdesk/data_providers/sector_chain.py",
        "quantdesk/data_providers/intraday_chain.py",
        "scripts/migrate_csv_to_sqlite.py",
        "scripts/run_data_db.py",
        "scripts/run_data_update.py",
        "scripts/run_data_query.py",
        "scripts/run_data_layer_health.py",
        "scripts/run_data_quality_repair.py",
        "tests/test_v370_data_backbone_sqlite_akshare.py",
        "tests/test_v370_csv_to_sqlite_migration_integrity.py",
        "tests/test_v370_data_provider_chain_and_quality.py",
        "RELEASE_NOTES_v3.7.0.md",
        "docs/data_schemas/sqlite_data_backbone.md",
        "docs/data_schemas/csv_to_sqlite_migration.md",
        "docs/data_schemas/data_provider_chain.md",
        "docs/data_schemas/intraday_parquet.md",
    ],
    "3.7.0.1": [
        "quantdesk/research/strategy_viability_data_precheck.py",
        "quantdesk/research/strategy_viability_runner.py",
        "quantdesk/research/strategy_viability_report.py",
        "scripts/run_strategy_viability_precheck.py",
        "scripts/run_strategy_viability_sprint.py",
        "tests/test_v3701_strategy_viability_on_data_backbone.py",
        "RELEASE_NOTES_v3.7.0.1.md",
        "docs/strategy_viability_sprint.md",
    ],
    "3.7.0.2": [
        "quantdesk/data/schema_normalizer.py",
        "quantdesk/research/strategy_pipeline_repair.py",
        "scripts/run_strategy_pipeline_precheck.py",
        "tests/test_v3702_strategy_pipeline_repair.py",
        "RELEASE_NOTES_v3.7.0.2.md",
        "docs/strategy_pipeline_repair.md",
    ],
    "3.7.0.3": [
        "scripts/build_historical_listings.py",
        "scripts/validate_historical_listings.py",
        "scripts/run_data_universe.py",
        "tests/test_v3703_low_price_pipeline_finalization.py",
        "RELEASE_NOTES_v3.7.0.3.md",
        "docs/low_price_strategy_viability.md",
    ],
    "3.7.0.4": [
        "quantdesk/maintenance/git_deletion_audit.py",
        "scripts/run_git_deletion_audit.py",
        "scripts/run_maintenance.py",
        "quantdesk/research/capital_lot_sensitivity.py",
        "scripts/run_capital_lot_sensitivity.py",
        "tests/test_v3704_capital_lot_sensitivity_and_git_hygiene.py",
        "RELEASE_NOTES_v3.7.0.4.md",
        "docs/capital_lot_sensitivity.md",
        "docs/git_deletion_audit.md",
    ],
    "3.7.0.5": [
        "quantdesk/maintenance/import_alias_reconciliation.py",
        "scripts/run_import_alias_reconciliation.py",
        "tests/test_v3705_git_deletion_reconciliation.py",
        "RELEASE_NOTES_v3.7.0.5.md",
        "docs/import_alias_reconciliation.md",
    ],
    "3.7.0.6": [
        "quantdesk/maintenance/strategy_regression_audit.py",
        "scripts/run_strategy_regression_audit.py",
        "tests/test_v3706_strategy_regression_guard_and_report_hygiene.py",
        "RELEASE_NOTES_v3.7.0.6.md",
        "docs/strategy_regression_guard.md",
    ],
    "3.5.0": [
        "quantdesk/onboarding.py",
        "scripts/first_run_wizard.py",
        "build/QuantDesk.spec",
        "build/quantdesk_installer.nsi",
        "build/build_portable_release.ps1",
        "build/build_installer.ps1",
        "RELEASE_NOTES_v3.5.0.md",
        "docs/quickstart.md",
        "docs/user_guide.md",
        "docs/video_scripts.md",
        "docs/videos/README.md",
        "docs/screenshots/README.md",
        "assets/sample_data_manifest.json",
        "scripts/build_sample_data_package.py",
        "tests/test_v350_battle_ready_portable_release.py",
    ],
}

VERSION_APP_CODENAMES = {
    "3.4.3.2": "combined-score-research-backtest",
    "3.4.3.3": "sector-rotation-engine",
    "3.4.3.4": "factor-research-dashboard",
    "3.4.3.5": "point-in-time-universe-lite-and-project-hygiene",
    "3.4.3.6": "research-dashboard-daily-snapshot",
    "3.4.3.7": "backtest-assumptions-split-and-release-proof-polish",
    "3.4.3.8": "true-backtest-assumptions-split-and-doc-hygiene",
    "3.4.4.0": "multi-strategy-account-book",
    "3.4.4.1": "intraday-live-workbench-and-config-hygiene",
    "3.4.4.2": "risk-attribution-lite-and-live-polish",
    "3.4.4.3": "experiment-tracker-lite",
    "3.4.5.0": "pit-full-and-static-charts",
    "3.4.5.1": "engineering-docs-and-package-grouping-lite",
    "3.5.0": "battle-ready-research-and-portable-release",
    "3.5.1": "truthful-attribution-installer-and-structure-hardening",
    "3.5.1.1": "release-staging-cleanup-and-attribution-stress-tests",
    "3.5.2": "ui-home-and-strategy-recommender",
    "3.6.0": "real-use-quant-platform-completion",
    "3.6.1": "real-risk-alerts-and-smart-stop-integration",
    "3.6.2": "real-similar-day-stress-and-style-attribution",
    "3.7.0": "data-backbone",
    "3.7.0.1": "strategy-viability-on-data-backbone",
    "3.7.0.2": "strategy-pipeline-repair",
    "3.7.0.3": "low-price-pipeline-finalization",
    "3.7.0.4": "capital-lot-sensitivity-and-git-hygiene",
    "3.7.0.5": "git-deletion-reconciliation",
    "3.7.0.6": "strategy-regression-guard-and-report-hygiene",
}

FORBIDDEN_PREFIXES = (
    "dist/",
    "results/",
    "reports/",
    "data/daily/",
    "data/index/",
    "data/factors/",
    "data/intraday/",
    "data/universe/",
    "data/features/",
    "data/paper/",
    "logs/",
    "state/",
    "test_runtime/",
)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _read_text(zf: zipfile.ZipFile, name: str) -> str:
    return zf.read(name).decode("utf-8-sig", errors="replace")


def _read_release_artifact(zf: zipfile.ZipFile) -> dict[str, Any]:
    raw = zf.read("RELEASE_ARTIFACT.json")
    if raw.startswith(b"\xef\xbb\xbf"):
        raise AssertionError("RELEASE_ARTIFACT.json must be UTF-8 without BOM")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise AssertionError(f"RELEASE_ARTIFACT.json is not valid UTF-8: {exc}") from exc
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise AssertionError(f"RELEASE_ARTIFACT.json is not valid JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise AssertionError("RELEASE_ARTIFACT.json must contain a JSON object")
    return payload


def _parse_bool(value: str | bool | None) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    return str(value).strip().lower() in {"1", "true", "yes", "y", "on"}


def verify_clean_zip(
    zip_path: Path,
    expected_version: str,
    *,
    expected_app_version: str | None = None,
    require_release_artifact: bool = False,
) -> dict[str, Any]:
    if not zip_path.exists():
        raise AssertionError(f"zip not found: {zip_path}")
    if zip_path.name.startswith("quantdesk_clean_v") and expected_version not in zip_path.name:
        raise AssertionError(f"versioned zip filename missing expected version {expected_version}")
    with zipfile.ZipFile(zip_path) as zf:
        names = zf.namelist()
        bad = zf.testzip()
        if bad is not None:
            raise AssertionError(f"zip bad member: {bad}")
        required_files = list(BASE_REQUIRED_FILES) + list(VERSION_REQUIRED_FILES.get(expected_version, []))
        if require_release_artifact:
            required_files.append("RELEASE_ARTIFACT.json")
        missing = [name for name in required_files if name not in names]
        if missing:
            raise AssertionError(f"required files missing: {missing}")
        version_text = _read_text(zf, "quantdesk/version.py")
        required_version_lines = [
            f'PACKAGE_VERSION = "{expected_version}"',
            f'API_SCHEMA_VERSION = "{expected_version}"',
        ]
        for line in required_version_lines:
            if line not in version_text:
                raise AssertionError(f"version.py missing {line!r}")
        codename = VERSION_APP_CODENAMES.get(expected_version)
        if codename and codename not in version_text:
            raise AssertionError(f"version.py missing {codename} APP_VERSION")
        if expected_app_version and f'APP_VERSION = "{expected_app_version}"' not in version_text:
            raise AssertionError(f"version.py missing APP_VERSION {expected_app_version!r}")
        if expected_version not in _read_text(zf, "README.md"):
            raise AssertionError(f"README.md missing {expected_version}")
        if expected_version not in _read_text(zf, "CHANGELOG.md"):
            raise AssertionError(f"CHANGELOG.md missing {expected_version}")
        if "architecture_inventory.md" in names and expected_version not in _read_text(zf, "architecture_inventory.md"):
            raise AssertionError(f"architecture_inventory.md missing {expected_version}")
        release_artifact: dict[str, Any] = {}
        if require_release_artifact:
            release_artifact = _read_release_artifact(zf)
            if release_artifact.get("package_version") != expected_version:
                raise AssertionError("RELEASE_ARTIFACT.json package_version mismatch")
            expected_artifact_app = expected_app_version or f"{expected_version}-{VERSION_APP_CODENAMES.get(expected_version, '')}".rstrip("-")
            if release_artifact.get("app_version") != expected_artifact_app:
                raise AssertionError("RELEASE_ARTIFACT.json app_version mismatch")
            if release_artifact.get("api_schema_version") != expected_version:
                raise AssertionError("RELEASE_ARTIFACT.json api_schema_version mismatch")
            artifact_required = release_artifact.get("required_files")
            if not isinstance(artifact_required, list) or not artifact_required:
                raise AssertionError("RELEASE_ARTIFACT.json required_files missing")
            if expected_version >= "3.5.1":
                minimum_release_files = [
                    "quantdesk/research/daily_research_snapshot.py",
                    "tests/test_v3436_research_dashboard_daily_snapshot.py",
                    "quantdesk/research/point_in_time_universe.py",
                    "quantdesk/research/factor_research_dashboard.py",
                ]
            else:
                minimum_release_files = [
                    "quantdesk/daily_research_snapshot.py",
                    "tests/test_v3436_research_dashboard_daily_snapshot.py",
                    "quantdesk/point_in_time_universe.py",
                    "quantdesk/factor_research_dashboard.py",
                ]
            if expected_version >= "3.4.3.7":
                minimum_release_files.append("quantdesk/backtest_assumptions.py")
            if expected_version >= "3.4.3.8":
                minimum_release_files.append("tests/test_v3438_true_backtest_assumptions_split_and_doc_hygiene.py")
            if expected_version >= "3.4.4.0":
                minimum_release_files.extend([
                    "quantdesk/paper/multi_strategy_account_book.py",
                    "scripts/run_multi_strategy_account_book.py",
                    "tests/test_v3440_multi_strategy_account_book.py",
                ])
            if expected_version >= "3.4.4.1":
                minimum_release_files.extend([
                    "quantdesk/intraday/intraday_live_engine.py",
                    "quantdesk/intraday/intraday_live_snapshot.py",
                    "quantdesk/intraday/intraday_score.py",
                    "quantdesk/intraday/intraday_data_policy.py",
                    "scripts/run_intraday_live_workbench.py",
                    "tests/test_v3441_intraday_live_workbench_and_config_hygiene.py",
                    "config/multi_strategy_account_book.example.yaml",
                ])
            if expected_version >= "3.4.4.2":
                minimum_release_files.extend([
                    "quantdesk/research/risk_attribution_lite.py",
                    "quantdesk/research/daily_risk_attribution_snapshot.py",
                    "scripts/run_risk_attribution_lite.py",
                    "tests/test_v3442_risk_attribution_lite_and_live_polish.py",
                    "docs/intraday_live_quickstart.md",
                ])
            if expected_version >= "3.4.4.3":
                minimum_release_files.extend([
                    "quantdesk/research/experiment_tracker.py",
                    "quantdesk/research/daily_experiment_tracker_snapshot.py",
                    "scripts/run_experiments.py",
                    "tests/test_v3443_experiment_tracker_lite.py",
                ])
            if expected_version >= "3.4.5.0":
                minimum_release_files.extend([
                    "quantdesk/research/chart_renderer.py",
                    "scripts/validate_historical_listings.py",
                    "scripts/build_historical_listings_template.py",
                    "docs/point_in_time_universe.md",
                    "tests/test_v3450_pit_full_and_static_charts.py",
                ])
            if expected_version >= "3.4.5.1":
                minimum_release_files.extend([
                    "quantdesk/backtest_loaders.py",
                    "quantdesk/self_check_render.py",
                    "quantdesk/research/__init__.py",
                    "quantdesk/intraday/__init__.py",
                    "quantdesk/paper/__init__.py",
                    "quantdesk/agent/__init__.py",
                    "docs/index.md",
                    "docs/quickstart.md",
                    "docs/architecture.md",
                    "tests/test_v3451_engineering_docs_and_package_grouping_lite.py",
                ])
            if expected_version >= "3.5.0":
                minimum_release_files.extend([
                    "quantdesk/onboarding.py",
                    "scripts/first_run_wizard.py",
                    "build/QuantDesk.spec",
                    "build/quantdesk_installer.nsi",
                    "build/build_portable_release.ps1",
                    "build/build_installer.ps1",
                    "RELEASE_NOTES_v3.5.0.md",
                    "assets/sample_data_manifest.json",
                    "scripts/build_sample_data_package.py",
                    "tests/test_v350_battle_ready_portable_release.py",
                ])
            if expected_version >= "3.5.1.1":
                minimum_release_files.extend([
                    "scripts/clean_release_staging.ps1",
                    "docs/release_build.md",
                    "tests/test_v3511_release_staging_cleanup_and_attribution_stress.py",
                    "RELEASE_NOTES_v3.5.1.1.md",
                ])
            if expected_version >= "3.5.2":
                minimum_release_files.extend([
                    "quantdesk/research/strategy_recommender.py",
                    "scripts/run_strategy_recommender.py",
                    "tests/test_v352_ui_home_and_strategy_recommender.py",
                    "RELEASE_NOTES_v3.5.2.md",
                ])
            if expected_version >= "3.6.0":
                minimum_release_files.extend([
                    "quantdesk/research/stock_picking_flow.py",
                    "quantdesk/data/price_adjustment.py",
                    "quantdesk/risk/smart_stop.py",
                    "quantdesk/notifications/notification_center.py",
                    "scripts/run_v360_builder.py",
                    "tests/test_v360_real_use_stock_picking_flow.py",
                    "RELEASE_NOTES_v3.6.0.md",
                ])
            if expected_version >= "3.6.1":
                minimum_release_files.extend([
                    "quantdesk/risk/position_inputs.py",
                    "quantdesk/notifications/dedupe.py",
                    "tests/test_v361_real_risk_alerts_and_smart_stop_integration.py",
                    "RELEASE_NOTES_v3.6.1.md",
                ])
            if expected_version >= "3.6.2":
                minimum_release_files.extend([
                    "quantdesk/research/similar_day.py",
                    "quantdesk/research/stress_scenarios.py",
                    "quantdesk/research/barra_style_attribution.py",
                    "quantdesk/research/decision_journal.py",
                    "tests/test_v362_real_similar_day_stress_and_style_attribution.py",
                    "scripts/check_text_encoding_hygiene.py",
                    "RELEASE_NOTES_v3.6.2.md",
                    "templates/data/index_daily_sample.csv",
                    "templates/data/style_exposures_sample.csv",
                    "templates/data/style_factor_returns_sample.csv",
                    "docs/data_schemas/similar_day_input.md",
                    "docs/data_schemas/stress_scenarios_input.md",
                    "docs/data_schemas/style_attribution_input.md",
                    "docs/data_schemas/decision_journal.md",
                ])
            if expected_version >= "3.7.0":
                minimum_release_files.extend([
                    "quantdesk/data_store/sqlite_store.py",
                    "quantdesk/data_store/schema.py",
                    "quantdesk/data_store/repositories.py",
                    "quantdesk/data_store/migration.py",
                    "quantdesk/data_store/health.py",
                    "quantdesk/data_store/dual_writer.py",
                    "quantdesk/data_store/query.py",
                    "quantdesk/data_store/locks.py",
                    "quantdesk/data_store/hash_utils.py",
                    "quantdesk/data_store/intraday_parquet.py",
                    "quantdesk/data_store/quality_repair.py",
                    "quantdesk/data_providers/akshare_provider.py",
                    "quantdesk/data_providers/fundamentals_chain.py",
                    "quantdesk/data_providers/sector_chain.py",
                    "quantdesk/data_providers/intraday_chain.py",
                    "scripts/migrate_csv_to_sqlite.py",
                    "scripts/run_data_db.py",
                    "scripts/run_data_query.py",
                    "scripts/run_data_layer_health.py",
                    "scripts/run_data_quality_repair.py",
                    "tests/test_v370_data_backbone_sqlite_akshare.py",
                    "tests/test_v370_csv_to_sqlite_migration_integrity.py",
                    "tests/test_v370_data_provider_chain_and_quality.py",
                    "RELEASE_NOTES_v3.7.0.md",
                    "docs/data_schemas/sqlite_data_backbone.md",
                    "docs/data_schemas/csv_to_sqlite_migration.md",
                    "docs/data_schemas/data_provider_chain.md",
                    "docs/data_schemas/intraday_parquet.md",
                ])
            if expected_version >= "3.7.0.1":
                minimum_release_files.extend([
                    "quantdesk/research/strategy_viability_data_precheck.py",
                    "quantdesk/research/strategy_viability_runner.py",
                    "quantdesk/research/strategy_viability_report.py",
                    "scripts/run_strategy_viability_precheck.py",
                    "scripts/run_strategy_viability_sprint.py",
                    "tests/test_v3701_strategy_viability_on_data_backbone.py",
                    "RELEASE_NOTES_v3.7.0.1.md",
                    "docs/strategy_viability_sprint.md",
                ])
            if expected_version >= "3.7.0.2":
                minimum_release_files.extend([
                    "quantdesk/data/schema_normalizer.py",
                    "quantdesk/research/strategy_pipeline_repair.py",
                    "scripts/run_strategy_pipeline_precheck.py",
                    "tests/test_v3702_strategy_pipeline_repair.py",
                    "RELEASE_NOTES_v3.7.0.2.md",
                    "docs/strategy_pipeline_repair.md",
                ])
            if expected_version >= "3.7.0.3":
                minimum_release_files.extend([
                    "scripts/build_historical_listings.py",
                    "scripts/validate_historical_listings.py",
                    "scripts/run_data_universe.py",
                    "tests/test_v3703_low_price_pipeline_finalization.py",
                    "RELEASE_NOTES_v3.7.0.3.md",
                    "docs/low_price_strategy_viability.md",
                ])
            if expected_version >= "3.7.0.4":
                minimum_release_files.extend([
                    "quantdesk/maintenance/git_deletion_audit.py",
                    "scripts/run_git_deletion_audit.py",
                    "scripts/run_maintenance.py",
                    "quantdesk/research/capital_lot_sensitivity.py",
                    "scripts/run_capital_lot_sensitivity.py",
                    "tests/test_v3704_capital_lot_sensitivity_and_git_hygiene.py",
                    "RELEASE_NOTES_v3.7.0.4.md",
                    "docs/capital_lot_sensitivity.md",
                    "docs/git_deletion_audit.md",
                ])
            if expected_version >= "3.7.0.5":
                minimum_release_files.extend([
                    "quantdesk/maintenance/import_alias_reconciliation.py",
                    "scripts/run_import_alias_reconciliation.py",
                    "tests/test_v3705_git_deletion_reconciliation.py",
                    "RELEASE_NOTES_v3.7.0.5.md",
                    "docs/import_alias_reconciliation.md",
                ])
            if expected_version >= "3.7.0.6":
                minimum_release_files.extend([
                    "quantdesk/maintenance/strategy_regression_audit.py",
                    "scripts/run_strategy_regression_audit.py",
                    "tests/test_v3706_strategy_regression_guard_and_report_hygiene.py",
                    "RELEASE_NOTES_v3.7.0.6.md",
                    "docs/strategy_regression_guard.md",
                ])
            missing_minimum = [name for name in minimum_release_files if name not in artifact_required]
            if missing_minimum:
                raise AssertionError(f"RELEASE_ARTIFACT required_files missing proof entries: {missing_minimum}")
            moved_release_file_aliases = {
                "quantdesk/daily_research_snapshot.py": "quantdesk/research/daily_research_snapshot.py",
                "quantdesk/point_in_time_universe.py": "quantdesk/research/point_in_time_universe.py",
                "quantdesk/factor_research_dashboard.py": "quantdesk/research/factor_research_dashboard.py",
                "quantdesk/risk_attribution_lite.py": "quantdesk/research/risk_attribution_lite.py",
                "quantdesk/daily_risk_attribution_snapshot.py": "quantdesk/research/daily_risk_attribution_snapshot.py",
                "quantdesk/experiment_tracker.py": "quantdesk/research/experiment_tracker.py",
                "quantdesk/daily_experiment_tracker_snapshot.py": "quantdesk/research/daily_experiment_tracker_snapshot.py",
                "quantdesk/chart_renderer.py": "quantdesk/research/chart_renderer.py",
                "quantdesk/multi_strategy_account_book.py": "quantdesk/paper/multi_strategy_account_book.py",
                "quantdesk/intraday_live_engine.py": "quantdesk/intraday/intraday_live_engine.py",
                "quantdesk/intraday_live_snapshot.py": "quantdesk/intraday/intraday_live_snapshot.py",
                "quantdesk/intraday_score.py": "quantdesk/intraday/intraday_score.py",
                "quantdesk/intraday_data_policy.py": "quantdesk/intraday/intraday_data_policy.py",
            }
            missing_artifact_required = [
                name
                for name in artifact_required
                if name not in names and moved_release_file_aliases.get(name) not in names
            ]
            if missing_artifact_required:
                raise AssertionError(f"RELEASE_ARTIFACT required files missing from zip: {missing_artifact_required}")
        forbidden = [
            name
            for name in names
            if name.startswith(FORBIDDEN_PREFIXES)
            or "__pycache__/" in name
            or name.endswith(".zip")
            or name.endswith(".exe")
            or name.endswith(".parquet")
            or name.endswith(".duckdb")
            or name.endswith(".sqlite")
            or name.endswith(".db")
            or "QuantDesk_v" in name and "_stage" in name
        ]
        if forbidden:
            raise AssertionError(f"forbidden paths present: {forbidden[:20]}")
    file_hash = _sha256(zip_path)
    app_version = expected_app_version or f"{expected_version}-{VERSION_APP_CODENAMES.get(expected_version, '')}".rstrip("-")
    return {
        "package_version": expected_version,
        "app_version": app_version,
        "api_schema_version": expected_version,
        "zip_filename": zip_path.name,
        "generic_zip_filename": "quantdesk_clean.zip",
        "zip_path": str(zip_path),
        "entries": len(names),
        "bad_count": 0,
        "sha256": file_hash,
        "required_files_present": True,
        "forbidden_paths_present": False,
        "release_artifact_embedded": "RELEASE_ARTIFACT.json" in names,
        "release_artifact_utf8_no_bom": True if require_release_artifact else ("RELEASE_ARTIFACT.json" in names),
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "source_root": str(PROJECT_ROOT),
    }


def _write_manifest_files(zip_path: Path, manifest: dict[str, Any]) -> None:
    if zip_path.name == "quantdesk_clean.zip":
        manifest_path = zip_path.with_name("quantdesk_clean_manifest.json")
        checksum_path = zip_path.with_name("quantdesk_clean.sha256")
    else:
        manifest_path = zip_path.with_name(f"{zip_path.stem}_manifest.json")
        checksum_path = zip_path.with_name(f"{zip_path.stem}.sha256")
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    checksum_path.write_text(f"{manifest['sha256']}  {zip_path.name}\n", encoding="utf-8")


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Verify QuantDesk clean release zip and write upload manifest/checksum.")
    parser.add_argument("--zip", required=True, help="Path to quantdesk_clean.zip")
    parser.add_argument("--expected-version", required=True, help="Expected PACKAGE_VERSION/API_SCHEMA_VERSION")
    parser.add_argument("--expected-app-version", default="", help="Expected APP_VERSION")
    parser.add_argument("--require-release-artifact", default="false", help="Require and validate RELEASE_ARTIFACT.json")
    parser.add_argument("--no-write-manifest", action="store_true", help="Verify only; do not write dist manifest/checksum files.")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    zip_path = Path(args.zip).resolve()
    try:
        manifest = verify_clean_zip(
            zip_path,
            args.expected_version,
            expected_app_version=args.expected_app_version or None,
            require_release_artifact=_parse_bool(args.require_release_artifact),
        )
        if not args.no_write_manifest:
            _write_manifest_files(zip_path, manifest)
    except Exception as exc:  # noqa: BLE001
        print(f"verify_clean_zip_failed: {exc}", file=sys.stderr)
        return 1
    print("zip_ok=1")
    print(f"zip_path={zip_path}")
    print(f"entries={manifest['entries']}")
    print("bad_count=0")
    print(f"sha256={manifest['sha256']}")
    print(f"package_version={manifest['package_version']}")
    print(f"app_version={manifest['app_version']}")
    print(f"api_schema_version={manifest['api_schema_version']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

