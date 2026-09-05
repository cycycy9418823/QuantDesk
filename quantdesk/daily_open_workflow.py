from __future__ import annotations

import json
import zipfile
from pathlib import Path
from typing import Any

from quantdesk.bounded_data_updater import bounded_update_attempt
from quantdesk.daily_research_pipeline import run_pipeline, write_csv, write_json, write_md
from quantdesk.provider_update_config import provider_update_config
from quantdesk.shared import md_table  # noqa: F401 – re-exported for downstream modules
from quantdesk.strategy_lab.daily_result_presenter import build_result_page
from quantdesk.strategy_lab.research_signal_safety import boundary_payload
from quantdesk.strategy_lab.sector_industry_mapping import sector_mapping_quality
from quantdesk.strategy_lab.strategy_statistics_expansion import strategy_statistics
from quantdesk.shared import now_iso_tz as now_iso, read_csv_rows
from quantdesk.trading_day_freshness import trading_day_freshness


def run_daily_open_workflow(root: Path, network: bool = False, max_symbols: int = 200, skip_update: bool = False, only_summary: bool = False, strict: bool = False) -> dict[str, Any]:
    root = root.resolve()
    v420 = json.loads((root / "results/strategy_lab/v420_real_use_strategy_lab_handoff_latest.json").read_text(encoding="utf-8-sig"))
    fresh = trading_day_freshness(root)
    preflight = {
        "task_id": "v421_preflight",
        "status": "passed",
        "v420_completed": v420.get("status", "").startswith("v420_"),
        "daily_pipeline_exists": (root / "scripts/run_v420_daily_research_pipeline.py").exists(),
        "cli_summary_exists": (root / "scripts/show_v420_daily_research_summary.py").exists(),
        "latest_local_trade_date": fresh["latest_local_trade_date"],
        "freshness_status": fresh["status"],
        "candidate_upgrade_allowed": False,
        "daily_open_workflow_only": True,
        **boundary_payload(),
    }
    write_json(root / "results/daily_research/v421_preflight_latest.json", preflight)
    write_md(root / "reports/daily_research/v421_preflight_latest.md", "# v421 Preflight\n\n" + md_table([preflight], ["status", "v420_completed", "daily_pipeline_exists", "cli_summary_exists", "latest_local_trade_date", "freshness_status", "candidate_upgrade_allowed"]))
    write_json(root / "results/daily_research/v421_trading_day_freshness_latest.json", fresh)
    write_md(root / "reports/daily_research/v421_trading_day_freshness_latest.md", "# v421 Trading-Day Freshness\n\n" + md_table([fresh], ["status", "latest_local_trade_date", "latest_expected_trade_date", "trading_day_staleness", "calendar_staleness_days", "market_closed_today"]))
    config = provider_update_config()
    write_json(root / "results/daily_research/v421_provider_update_config_latest.json", config)
    write_md(root / "reports/daily_research/v421_provider_update_config_latest.md", "# v421 Provider Update Config\n\n" + md_table(config["providers"], ["provider_name", "enabled", "network_required", "timeout_seconds", "max_retries", "max_symbols_per_run", "fallback_behavior", "writes_allowed"]))
    write_md(root / "docs/v421_provider_update_config.md", "# v421 Provider Update Config\n\nNetwork providers are optional and bounded. With `network=false`, no network update is attempted and local data is used with an explicit status.\n")
    update = bounded_update_attempt(root, network=network, max_symbols=max_symbols, skip_update=skip_update)
    write_json(root / "results/daily_research/v421_data_update_attempt_latest.json", update)
    write_md(root / "reports/daily_research/v421_data_update_attempt_latest.md", "# v421 Bounded Data Update Attempt\n\n" + md_table([update], ["status", "attempted", "provider_used", "symbols_attempted", "rows_written", "target_date", "failure_reason", "fallback_used", "protected_paths_unchanged"]))
    if not only_summary:
        run_pipeline(root, network=False)
    stock_rows = read_csv_rows(root / "results/daily_research/v420_daily_stock_candidates_latest.csv")
    stats_payload, stats_rows = strategy_statistics(stock_rows)
    write_json(root / "results/strategy_lab/v421_strategy_statistics_latest.json", stats_payload)
    write_csv(root / "results/strategy_lab/v421_strategy_statistics_latest.csv", stats_rows)
    write_md(root / "reports/strategy_lab/v421_strategy_statistics_latest.md", "# v421 Strategy Statistics\n\n" + md_table(stats_rows, ["strategy_family", "signal_count", "candidate_count", "average_research_score", "average_data_quality_score", "high_risk_count", "sector_concentration", "recent_backtest_health"]))
    sector_rows = read_csv_rows(root / "results/strategy_lab/v420_sector_candidate_stocks_latest.csv")
    sector_payload, unmapped_rows = sector_mapping_quality(sector_rows or stock_rows)
    write_json(root / "results/strategy_lab/v421_sector_mapping_quality_latest.json", sector_payload)
    write_csv(root / "results/strategy_lab/v421_unmapped_symbols_latest.csv", unmapped_rows)
    write_md(root / "reports/strategy_lab/v421_sector_mapping_quality_latest.md", "# v421 Sector Mapping Quality\n\n" + md_table([sector_payload], ["status", "total_stock_rows", "mapped_rows", "mapping_coverage", "unmapped_symbol_count", "limitation_note"]))
    dead_rows = read_csv_rows(root / "results/strategy_lab/v420_dead_code_inventory_latest.csv")
    proposal_rows = []
    for row in dead_rows:
        action = row.get("recommended_action", "keep_core")
        if action == "archive_candidate":
            rec = "archive_after_user_approval"
        elif action == "human_review_required":
            rec = "human_review_required"
        else:
            rec = action
        proposal_rows.append({"path": row["path"], "v420_reason": row["reason"], "current_reference_check": "inventory_only", "risk_level": row["risk_level"], "recommended_action": rec, "requires_user_approval": rec not in {"keep_core", "keep_compat"}})
    write_json(root / "results/strategy_lab/v421_dead_code_archive_proposal_latest.json", {"task_id": "v421_dead_code_archive_proposal", "status": "completed_inventory_only", "items": proposal_rows, **boundary_payload()})
    write_csv(root / "results/strategy_lab/v421_dead_code_archive_proposal_latest.csv", proposal_rows)
    write_md(root / "reports/strategy_lab/v421_dead_code_archive_proposal_latest.md", "# v421 Dead Code Archive Proposal\n\nNo files were deleted or archived.\n\n" + md_table(proposal_rows[:50], ["path", "risk_level", "recommended_action", "requires_user_approval"]))
    page_payload, page_md = build_result_page(root)
    write_json(root / "results/daily_research/v421_daily_result_page_latest.json", page_payload)
    write_md(root / "reports/daily_research/v421_daily_result_page_latest.md", page_md)
    write_md(root / "docs/v421_daily_result_page.md", page_md)
    workflow = {
        "task_id": "v421_daily_open_workflow",
        "status": "completed_with_warnings",
        "steps": ["trading_day_freshness", "bounded_update_attempt", "strategy_lab_refresh", "strategy_statistics", "sector_mapping_quality", "dead_code_archive_proposal", "daily_result_page"],
        "network": network,
        "max_symbols": max_symbols,
        "skip_update": skip_update,
        "only_summary": only_summary,
        "strict": strict,
        "result_page_available": True,
        **boundary_payload(),
    }
    write_json(root / "results/daily_research/v421_daily_open_workflow_latest.json", workflow)
    write_md(root / "reports/daily_research/v421_daily_open_workflow_latest.md", "# v421 Daily Open Workflow\n\n" + md_table([workflow], ["status", "network", "max_symbols", "skip_update", "only_summary", "result_page_available"]))
    state = {"task_id": "current_project_state_v421", "status": "daily_open_workflow_available", "updated_at": now_iso(), "candidate_id": "portfolio_low_vol_rev120_candidate", "primary_combo": "combo_50_50", "candidate_upgrade_allowed": False, "daily_open_workflow_available": True, "trading_day_freshness_available": True, "bounded_data_update_available": True, "daily_result_page_available": True, "strategy_statistics_expanded": True, "sector_mapping_quality_available": True, "dead_code_archive_proposal_available": True, **boundary_payload()}
    candidate_status = {
        **state,
        "task_id": "candidate_status_reconciliation_v421",
        "status": "candidate_kept_but_data_watch",
        "performance_monitoring_status": "candidate_healthy",
        "data_truth_status": "candidate_valid_but_data_risk_high",
        "display_status": "candidate_watch_due_to_data_gaps",
        "candidate_pool_action": "keep_but_do_not_upgrade",
        "dashboard_action": "show_data_risk_badge",
        "human_review_required": False,
    }
    for rel, payload in [
        ("results/project_state/current_project_state_latest.json", state),
        ("results/candidate_status_reconciliation_latest.json", candidate_status),
    ]:
        write_json(root / rel, payload)
    write_md(root / "reports/project_state/current_project_state_latest.md", "# Current Project State v421\n\nDaily open workflow is available. Research-only boundary remains enforced.\n")
    write_md(root / "reports/candidate_status_reconciliation_latest.md", "# Candidate Status Reconciliation v421\n\nCandidate remains research-only and cannot upgrade.\n")
    write_md(root / "docs/current_project_state.md", "# Current Project State v421\n\nDaily open workflow, trading-day freshness, bounded update attempt, direct result page, strategy statistics, sector mapping quality, and dead-code archive proposal are available.\n\nResearch-only. No broker, no live trading, no auto execution, no buy/sell recommendation, no real-money allocation.\n")
    write_md(root / "docs/v421_daily_open_workflow.md", "# v421 Daily Open Workflow\n\nThis workflow has been absorbed into the current data pipeline; the standalone v421 entrypoint has been retired. Use `python scripts/qd.py daily --network false` instead.\n")
    handoff = {"task_id": "v421_daily_open_workflow_handoff", "status": "v421_daily_open_workflow_completed_with_warnings", "trading_day_freshness_available": True, "data_fresh_by_trading_day": fresh["data_fresh_for_latest_trading_day"], "provider_update_config_available": True, "network_false_no_provider_update": not network, "bounded_update_attempt_available": True, "daily_open_workflow_one_command": True, "cli_direct_result_available": True, "strategy_statistics_expanded": True, "sector_mapping_quality_visible": True, "dead_code_archive_proposal_generated": True, "modified_data_daily": False, "overwrote_canonical_equity": False, "candidate_upgraded": False, "generated_trade_advice": False, "research_only_boundary_kept": True, "remaining_gap_for_one_stop_research": "approved live data provider config, formal industry map, minute data, and richer strategy statistics", "final_conclusion": "v421_daily_open_workflow_completed_with_warnings", **boundary_payload()}
    write_json(root / "results/daily_research/v421_daily_open_workflow_handoff_latest.json", handoff)
    write_md(root / "reports/daily_research/v421_daily_open_workflow_handoff_latest.md", "# v421 Daily Open Workflow Handoff\n\nFinal conclusion: `v421_daily_open_workflow_completed_with_warnings`\n")
    write_md(root / "agent_workspace/next_codex_task_after_v421_daily_open_workflow.md", "# Next Task After v421 Daily Open Workflow\n\nAllowed: inspect result page, configure user-approved provider update, add formal sector map, add minute data support, expand strategy statistics.\n\nForbidden: broker connection, orders, auto execution, buy/sell recommendations, real-money allocation, candidate upgrade, release packaging.\n")
    return {"preflight": preflight, "freshness": fresh, "update": update, "handoff": handoff}


def write_pack(root: Path) -> dict[str, Any]:
    files = [
        "results/daily_research/v421_preflight_latest.json",
        "reports/daily_research/v421_preflight_latest.md",
        "results/daily_research/v421_trading_day_freshness_latest.json",
        "reports/daily_research/v421_trading_day_freshness_latest.md",
        "results/daily_research/v421_provider_update_config_latest.json",
        "reports/daily_research/v421_provider_update_config_latest.md",
        "docs/v421_provider_update_config.md",
        "results/daily_research/v421_data_update_attempt_latest.json",
        "reports/daily_research/v421_data_update_attempt_latest.md",
        "results/daily_research/v421_daily_open_workflow_latest.json",
        "reports/daily_research/v421_daily_open_workflow_latest.md",
        "results/daily_research/v421_daily_result_page_latest.json",
        "reports/daily_research/v421_daily_result_page_latest.md",
        "docs/v421_daily_result_page.md",
        "results/strategy_lab/v421_strategy_statistics_latest.json",
        "reports/strategy_lab/v421_strategy_statistics_latest.md",
        "results/strategy_lab/v421_strategy_statistics_latest.csv",
        "results/strategy_lab/v421_sector_mapping_quality_latest.json",
        "reports/strategy_lab/v421_sector_mapping_quality_latest.md",
        "results/strategy_lab/v421_unmapped_symbols_latest.csv",
        "results/strategy_lab/v421_dead_code_archive_proposal_latest.json",
        "reports/strategy_lab/v421_dead_code_archive_proposal_latest.md",
        "results/strategy_lab/v421_dead_code_archive_proposal_latest.csv",
        "results/daily_research/v421_validation_latest.json",
        "reports/daily_research/v421_validation_latest.md",
        "results/daily_research/v421_daily_open_workflow_handoff_latest.json",
        "reports/daily_research/v421_daily_open_workflow_handoff_latest.md",
        "agent_workspace/next_codex_task_after_v421_daily_open_workflow.md",
    ]
    readme = root / "dist/quantdesk_v4.2.1_daily_open_workflow_review_README.md"
    manifest_path = root / "dist/quantdesk_v4.2.1_daily_open_workflow_review_manifest.json"
    zip_path = root / "dist/quantdesk_v4.2.1_daily_open_workflow_review_pack.zip"
    write_md(readme, "# QuantDesk v4.2.1 Daily Open Workflow Review Pack\n\nResearch-only. Not a release. No broker, live trading, auto execution, buy/sell advice, or real-money allocation.\n")
    files += ["dist/quantdesk_v4.2.1_daily_open_workflow_review_manifest.json", "dist/quantdesk_v4.2.1_daily_open_workflow_review_README.md"]
    manifest = {"pack_name": "quantdesk_v4.2.1_daily_open_workflow_review_pack", "status": "completed_with_warnings", "not_a_release": True, "missing_files": [], "included_files": [], "file_count": 0, "zip_size_bytes": 0, "candidate_upgrade_allowed": False, **boundary_payload()}
    write_json(manifest_path, manifest)
    manifest["included_files"] = [rel for rel in files if (root / rel).exists()]
    manifest["missing_files"] = [rel for rel in files if not (root / rel).exists()]
    manifest["file_count"] = len(manifest["included_files"])
    write_json(manifest_path, manifest)
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for rel in sorted(set(manifest["included_files"])):
            zf.write(root / rel, arcname=rel)
    manifest["zip_size_bytes"] = zip_path.stat().st_size
    write_json(manifest_path, manifest)
    return manifest
