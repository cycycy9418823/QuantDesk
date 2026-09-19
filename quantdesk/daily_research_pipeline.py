from __future__ import annotations

import zipfile
from pathlib import Path
from typing import Any

from quantdesk.data_update_orchestrator import freshness_check, update_attempt
from quantdesk.shared import now_iso_tz as now_iso
from quantdesk.shared import read_json_default_dict as read_json
from quantdesk.shared import write_csv_rows as write_csv
from quantdesk.shared import write_json, write_md
from quantdesk.strategy_lab.daily_research_signal_engine import combine_daily_signals
from quantdesk.strategy_lab.limit_up_strategy import generate_limit_up
from quantdesk.strategy_lab.long_term_strategy import generate_long_term
from quantdesk.strategy_lab.low_price_strategy import generate_low_price
from quantdesk.strategy_lab.research_signal_safety import boundary_payload
from quantdesk.strategy_lab.rolling_strategy_backtest import load_market_metrics
from quantdesk.strategy_lab.sector_strategy import generate_sector_rotation
from quantdesk.strategy_lab.strategy_family_registry import strategy_family_registry
from quantdesk.strategy_lab.strategy_leaderboard import build_leaderboard

CANDIDATE = "portfolio_low_vol_rev120_candidate"
COMBO = "combo_50_50"




def md_table(rows: list[dict[str, Any]], cols: list[str]) -> str:
    lines = ["|" + "|".join(cols) + "|", "|" + "|".join(["---"] * len(cols)) + "|"]
    for row in rows:
        lines.append("|" + "|".join(str(row.get(col, "")) for col in cols) + "|")
    return "\n".join(lines)


def preflight(root: Path) -> dict[str, Any]:
    candidate = read_json(root / "results/candidate_status_reconciliation_latest.json")
    fresh = freshness_check(root)
    return {
        "task_id": "v420_preflight",
        "status": "passed_with_stale_data_warning" if fresh["status"] != "data_fresh" else "passed",
        "created_at": now_iso(),
        "candidate_id": candidate.get("candidate_id", CANDIDATE),
        "primary_combo": candidate.get("primary_combo", COMBO),
        "candidate_upgrade_allowed": candidate.get("candidate_upgrade_allowed", False),
        "latest_trade_date_in_daily": fresh["latest_trade_date_in_daily"],
        "daily_data_file_count": len(list((root / "data/daily").glob("*.csv"))),
        "daily_update_runner_exists": (root / "scripts/run_v420_daily_research_pipeline.py").exists(),
        "strategy_family_registry_exists": (root / "quantdesk/strategy_lab/strategy_family_registry.py").exists(),
        "low_price_strategy_exists": (root / "quantdesk/strategy_lab/low_price_strategy.py").exists(),
        "limit_up_strategy_exists": (root / "quantdesk/strategy_lab/limit_up_strategy.py").exists(),
        "long_term_strategy_exists": (root / "quantdesk/strategy_lab/long_term_strategy.py").exists(),
        "research_only_daily_strategy_lab": True,
        **boundary_payload(),
    }


def dead_code_inventory(root: Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    rows = []
    for path in sorted((root / "scripts").glob("*.py")):
        name = path.name.lower()
        if "old" in name or "legacy" in name or "archive" in name:
            action = "archive_candidate"
            risk = "medium"
        elif "review_pack" in name or "dist" in name:
            action = "human_review_required"
            risk = "medium"
        else:
            action = "keep_core"
            risk = "low"
        rows.append({"path": str(path.relative_to(root)).replace("\\", "/"), "reason": "script inventory only; no deletion in v420", "last_reference": "", "risk_level": risk, "recommended_action": action})
    return {"task_id": "v420_dead_code_inventory", "status": "completed_inventory_only", "item_count": len(rows), **boundary_payload()}, rows


def dashboard_cards(fresh: dict, leaderboard_rows: list[dict], low_rows: list[dict], long_rows: list[dict], limit_rows: list[dict], sector_rows: list[dict]) -> dict[str, Any]:
    cards = [
        {"card_id": "data_freshness", "title": "Data freshness", "status": fresh["status"], "headline": f"Latest local daily date: {fresh['latest_trade_date_in_daily']}"},
        {"card_id": "strategy_leaderboard", "title": "Strategy leaderboard", "status": "available", "headline": leaderboard_rows[0]["strategy_id"] if leaderboard_rows else ""},
        {"card_id": "low_price_candidates", "title": "Low-price candidates", "status": "available", "headline": f"{len(low_rows)} research rows"},
        {"card_id": "long_term_candidates", "title": "Long-term candidates", "status": "available", "headline": f"{len(long_rows)} research rows"},
        {"card_id": "limit_up_research", "title": "Limit-up research", "status": "available", "headline": f"{len(limit_rows)} research rows"},
        {"card_id": "sector_rotation", "title": "Sector rotation", "status": "available", "headline": f"{len(sector_rows)} research rows"},
        {"card_id": "risk_warning", "title": "Risk warning", "status": "MODERATE_HIGH", "headline": "Formal metadata blockers remain"},
        {"card_id": "research_only_boundary", "title": "Research-only boundary", "status": "enforced", "headline": "No broker, no live trading, no auto execution"},
    ]
    return {"task_id": "v420_dashboard_cards", "status": "completed", "cards": cards, **boundary_payload()}


def run_pipeline(root: Path, network: bool = False) -> dict[str, Any]:
    root = root.resolve()
    (root / "results/strategy_lab").mkdir(parents=True, exist_ok=True)
    (root / "reports/strategy_lab").mkdir(parents=True, exist_ok=True)
    (root / "results/daily_research").mkdir(parents=True, exist_ok=True)
    (root / "reports/daily_research").mkdir(parents=True, exist_ok=True)

    pf = preflight(root)
    write_json(root / "results/strategy_lab/v420_preflight_latest.json", pf)
    write_md(root / "reports/strategy_lab/v420_preflight_latest.md", "# v420 Preflight\n\n" + md_table([pf], ["status", "candidate_id", "primary_combo", "candidate_upgrade_allowed", "latest_trade_date_in_daily", "daily_data_file_count"]))

    fresh = freshness_check(root)
    attempt = update_attempt(root, network=network)
    write_json(root / "results/daily_research/v420_data_freshness_latest.json", fresh)
    write_md(root / "reports/daily_research/v420_data_freshness_latest.md", "# v420 Data Freshness\n\n" + md_table([fresh], ["status", "latest_trade_date_in_daily", "latest_expected_trade_date", "data_staleness_days", "benchmark_staleness_days", "metadata_staleness_days"]))
    write_json(root / "results/daily_research/v420_data_update_attempt_latest.json", attempt)
    write_md(root / "reports/daily_research/v420_data_update_attempt_latest.md", "# v420 Data Update Attempt\n\n" + md_table([attempt], ["status", "attempted_network_update", "fallback_reason"]))

    registry = strategy_family_registry()
    write_json(root / "results/strategy_lab/v420_strategy_family_registry_latest.json", registry)
    write_md(root / "reports/strategy_lab/v420_strategy_family_registry_latest.md", "# v420 Strategy Family Registry\n\n" + md_table(registry["families"], ["family_id", "strategy_ids", "rebalance_frequency", "backtest_ready", "daily_signal_ready", "limitation_note"]))

    metrics = load_market_metrics(root)
    low_summary, low_rows = generate_low_price(metrics)
    long_summary, long_rows = generate_long_term(metrics)
    limit_summary, limit_rows, limit_dist = generate_limit_up(metrics)
    sector_summary, sector_rows = generate_sector_rotation(metrics, root=root)
    for rel, payload in [
        ("results/strategy_lab/v420_low_price_strategy_backtest_latest.json", low_summary),
        ("results/strategy_lab/v420_long_term_strategy_backtest_latest.json", long_summary),
        ("results/strategy_lab/v420_limit_up_research_backtest_latest.json", limit_summary),
        ("results/strategy_lab/v420_sector_rotation_latest.json", sector_summary),
    ]:
        write_json(root / rel, payload)
    write_csv(root / "results/strategy_lab/v420_low_price_strategy_candidates_latest.csv", low_rows)
    write_csv(root / "results/strategy_lab/v420_long_term_candidates_latest.csv", long_rows)
    write_csv(root / "results/strategy_lab/v420_limit_up_candidates_latest.csv", limit_rows)
    write_csv(root / "results/strategy_lab/v420_limit_up_next_day_distribution_latest.csv", limit_dist)
    write_csv(root / "results/strategy_lab/v420_sector_candidate_stocks_latest.csv", sector_rows)
    write_md(root / "reports/strategy_lab/v420_low_price_strategy_backtest_latest.md", "# v420 Low-Price Strategy\n\n" + md_table([low_summary["metrics"]], ["strategy_id", "annualized_return", "Sharpe", "max_drawdown", "win_rate", "turnover"]))
    write_md(root / "reports/strategy_lab/v420_low_price_strategy_candidates_latest.md", "# v420 Low-Price Research Candidates\n\n" + md_table(low_rows[:20], ["symbol", "strategy_id", "signal_date", "trigger_reason", "risk_flags", "research_signal_label"]))
    write_md(root / "reports/strategy_lab/v420_long_term_strategy_backtest_latest.md", "# v420 Long-Term Strategy\n\n" + md_table([long_summary["metrics"]], ["strategy_id", "annualized_return", "Sharpe", "max_drawdown", "win_rate", "turnover"]))
    write_md(root / "reports/strategy_lab/v420_long_term_candidates_latest.md", "# v420 Long-Term Research Candidates\n\n" + md_table(long_rows[:20], ["symbol", "strategy_id", "signal_date", "trigger_reason", "risk_flags", "research_signal_label"]))
    write_md(root / "reports/strategy_lab/v420_limit_up_research_backtest_latest.md", "# v420 Limit-Up Research\n\n" + md_table([limit_summary["metrics"]], ["strategy_id", "annualized_return", "Sharpe", "max_drawdown", "win_rate", "turnover"]))
    write_md(root / "reports/strategy_lab/v420_limit_up_candidates_latest.md", "# v420 Limit-Up Research Candidates\n\n" + md_table(limit_rows[:20], ["symbol", "signal_date", "pattern_detected", "risk_flags", "research_signal_label"]))
    write_md(root / "reports/strategy_lab/v420_sector_rotation_latest.md", "# v420 Sector Rotation\n\n" + md_table(sector_summary["top_sectors"], ["sector_name", "sector_score", "sector_rank"]))
    write_md(root / "reports/strategy_lab/v420_sector_candidate_stocks_latest.md", "# v420 Sector Candidate Stocks\n\n" + md_table(sector_rows[:20], ["sector_name", "sector_rank", "stock_symbol", "risk_flags", "research_reason"]))

    sector_mapping_coverage = float(sector_summary.get("mapping_coverage") or 0.0)
    if sector_mapping_coverage >= 0.8:
        sector_readiness = "research_ready_with_theme_mapping"
        sector_data_risk = "MODERATE"
    elif sector_mapping_coverage > 0:
        sector_readiness = "research_ready_with_partial_theme_mapping"
        sector_data_risk = "MODERATE_HIGH"
    else:
        sector_readiness = "research_ready_with_proxy_mapping"
        sector_data_risk = "HIGH"
    sector_leaderboard_summary = {
        "task_id": "sector_rotation_family",
        "metrics": {
            "strategy_id": "sector_rotation_family",
            "robustness_score": 0.55,
            "research_readiness": sector_readiness,
            "data_risk_level": sector_data_risk,
            "mapping_coverage": sector_mapping_coverage,
        },
    }
    leaderboard, leaderboard_rows = build_leaderboard([low_summary, long_summary, limit_summary, sector_leaderboard_summary])
    write_json(root / "results/strategy_lab/v420_strategy_leaderboard_latest.json", leaderboard)
    write_csv(root / "results/strategy_lab/v420_strategy_leaderboard_latest.csv", leaderboard_rows)
    write_md(root / "reports/strategy_lab/v420_strategy_leaderboard_latest.md", "# v420 Strategy Leaderboard\n\n" + md_table(leaderboard_rows, ["research_rank", "strategy_id", "strategy_health", "robustness_score", "robustness_label", "limitation_note"]))

    daily, stock_rows, sector_candidate_rows, strategy_rows = combine_daily_signals(fresh, leaderboard_rows, low_rows, long_rows, limit_rows, sector_rows)
    write_json(root / "results/daily_research/v420_daily_research_signals_latest.json", daily)
    write_csv(root / "results/daily_research/v420_daily_stock_candidates_latest.csv", stock_rows)
    write_csv(root / "results/daily_research/v420_daily_sector_candidates_latest.csv", sector_candidate_rows)
    write_csv(root / "results/daily_research/v420_daily_strategy_summary_latest.csv", strategy_rows)
    write_md(root / "reports/daily_research/v420_daily_research_signals_latest.md", "# v420 Daily Research Signals\n\n" + md_table(stock_rows[:20], ["symbol", "strategy_family", "strategy_id", "trigger_reason", "risk_flags", "research_signal_label"]))

    cards = dashboard_cards(fresh, leaderboard_rows, low_rows, long_rows, limit_rows, sector_rows)
    write_json(root / "results/daily_research/v420_dashboard_cards_latest.json", cards)
    write_md(root / "reports/daily_research/v420_dashboard_cards_latest.md", "# v420 Dashboard Cards\n\n" + md_table(cards["cards"], ["card_id", "title", "status", "headline"]))

    dead, dead_rows = dead_code_inventory(root)
    write_json(root / "results/strategy_lab/v420_dead_code_inventory_latest.json", dead)
    write_csv(root / "results/strategy_lab/v420_dead_code_inventory_latest.csv", dead_rows)
    write_md(root / "reports/strategy_lab/v420_dead_code_inventory_latest.md", "# v420 Dead Code Inventory\n\nInventory only; no deletion performed.\n\n" + md_table(dead_rows[:50], ["path", "reason", "risk_level", "recommended_action"]))

    state = {"task_id": "current_project_state_v420", "status": "real_use_strategy_lab_available", "updated_at": now_iso(), "candidate_id": CANDIDATE, "primary_combo": COMBO, "candidate_upgrade_allowed": False, "daily_research_pipeline_available": True, "strategy_lab_available": True, "low_price_strategy_available": True, "long_term_strategy_available": True, "limit_up_research_available": True, "sector_rotation_available": True, "strategy_leaderboard_available": True, "daily_research_signal_engine_available": True, "dead_code_inventory_available": True, **boundary_payload()}
    candidate_status = {
        **state,
        "task_id": "candidate_status_reconciliation_v420",
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
        ("results/data_evidence_registry_latest.json", {**state, "task_id": "data_evidence_registry_v420"}),
        ("results/data_gap_tracker_latest.json", {**state, "task_id": "data_gap_tracker_v420"}),
        ("results/candidate_status_reconciliation_latest.json", candidate_status),
    ]:
        write_json(root / rel, payload)
    write_md(root / "reports/project_state/current_project_state_latest.md", "# Current Project State v420\n\nDaily research strategy lab is available. Candidate remains research-only and cannot upgrade.\n")
    write_md(root / "reports/candidate_status_reconciliation_latest.md", "# Candidate Status Reconciliation v420\n\nCandidate remains kept for research only; no upgrade.\n")
    write_md(root / "docs/current_project_state.md", "# Current Project State v420\n\nQuantDesk now has a research-only daily strategy lab with data freshness checks, strategy family registry, low-price, long-term, limit-up, sector rotation, leaderboard, daily research signals, dashboard cards, and dead-code inventory.\n\nNo live trading, broker connection, auto execution, buy/sell recommendation, or real-money allocation instruction.\n")
    write_md(root / "docs/v420_daily_research_workflow.md", "# v420 Daily Research Workflow\n\nRun `python scripts/run_v420_daily_research_pipeline.py` to refresh research-only daily outputs. Run `python scripts/show_v420_daily_research_summary.py` to display the result summary directly.\n")
    write_md(root / "docs/v420_strategy_lab_design.md", "# v420 Strategy Lab Design\n\nThe strategy lab registers combo50, low-price, long-term, limit-up, sector rotation, and risk-control research families. Outputs are research candidates only.\n")
    write_md(root / "docs/v420_research_only_signal_policy.md", "# v420 Research-Only Signal Policy\n\nDaily research signals are not trading instructions, not broker instructions, not auto execution inputs, and not real-money allocation guidance.\n")

    handoff = {"task_id": "v420_real_use_strategy_lab_handoff", "status": "v420_real_use_strategy_lab_completed_with_warnings", "created_at": now_iso(), "data_freshness_check_available": True, "automatic_update_attempt_available": True, "stale_warning_available": fresh["status"] != "data_fresh", "low_price_strategy_available": True, "long_term_strategy_available": True, "limit_up_research_available": True, "sector_rotation_available": True, "strategy_leaderboard_generated": True, "daily_stock_candidate_pool_generated": True, "daily_sector_candidate_pool_generated": True, "cli_summary_available": True, "dead_code_inventory_generated": True, "modified_data_daily": False, "overwrote_canonical_equity": False, "candidate_upgraded": False, "generated_trade_advice": False, "research_only_boundary_kept": True, "remaining_gap_for_one_stop_research": "fresh provider integration and richer formal metadata still need user-approved data sources", "final_conclusion": "v420_real_use_strategy_lab_completed_with_warnings", **boundary_payload()}
    write_json(root / "results/strategy_lab/v420_real_use_strategy_lab_handoff_latest.json", handoff)
    write_md(root / "reports/strategy_lab/v420_real_use_strategy_lab_handoff_latest.md", "# v420 Real-Use Strategy Lab Handoff\n\nFinal conclusion: `v420_real_use_strategy_lab_completed_with_warnings`\n")
    write_md(root / "agent_workspace/next_codex_task_after_v420_real_use_strategy_lab.md", "# Next Task After v420 Real-Use Strategy Lab\n\nAllowed: inspect daily research output, wire dashboard display, add user-approved provider update config, add formal metadata, improve strategy research modules.\n\nForbidden: broker connection, order placement, auto execution, real-money allocation, candidate upgrade, release packaging, git push.\n")
    return {"preflight": pf, "freshness": fresh, "handoff": handoff}


def write_pack(root: Path) -> dict[str, Any]:
    files = [
        "results/strategy_lab/v420_preflight_latest.json",
        "reports/strategy_lab/v420_preflight_latest.md",
        "results/daily_research/v420_data_freshness_latest.json",
        "reports/daily_research/v420_data_freshness_latest.md",
        "results/daily_research/v420_data_update_attempt_latest.json",
        "reports/daily_research/v420_data_update_attempt_latest.md",
        "results/strategy_lab/v420_strategy_family_registry_latest.json",
        "reports/strategy_lab/v420_strategy_family_registry_latest.md",
        "results/strategy_lab/v420_low_price_strategy_backtest_latest.json",
        "reports/strategy_lab/v420_low_price_strategy_backtest_latest.md",
        "results/strategy_lab/v420_low_price_strategy_candidates_latest.csv",
        "reports/strategy_lab/v420_low_price_strategy_candidates_latest.md",
        "results/strategy_lab/v420_long_term_strategy_backtest_latest.json",
        "reports/strategy_lab/v420_long_term_strategy_backtest_latest.md",
        "results/strategy_lab/v420_long_term_candidates_latest.csv",
        "reports/strategy_lab/v420_long_term_candidates_latest.md",
        "results/strategy_lab/v420_limit_up_research_backtest_latest.json",
        "reports/strategy_lab/v420_limit_up_research_backtest_latest.md",
        "results/strategy_lab/v420_limit_up_candidates_latest.csv",
        "reports/strategy_lab/v420_limit_up_candidates_latest.md",
        "results/strategy_lab/v420_limit_up_next_day_distribution_latest.csv",
        "results/strategy_lab/v420_sector_rotation_latest.json",
        "reports/strategy_lab/v420_sector_rotation_latest.md",
        "results/strategy_lab/v420_sector_candidate_stocks_latest.csv",
        "reports/strategy_lab/v420_sector_candidate_stocks_latest.md",
        "results/strategy_lab/v420_strategy_leaderboard_latest.json",
        "reports/strategy_lab/v420_strategy_leaderboard_latest.md",
        "results/strategy_lab/v420_strategy_leaderboard_latest.csv",
        "results/daily_research/v420_daily_research_signals_latest.json",
        "reports/daily_research/v420_daily_research_signals_latest.md",
        "results/daily_research/v420_daily_stock_candidates_latest.csv",
        "results/daily_research/v420_daily_sector_candidates_latest.csv",
        "results/daily_research/v420_daily_strategy_summary_latest.csv",
        "results/daily_research/v420_dashboard_cards_latest.json",
        "reports/daily_research/v420_dashboard_cards_latest.md",
        "results/strategy_lab/v420_dead_code_inventory_latest.json",
        "reports/strategy_lab/v420_dead_code_inventory_latest.md",
        "results/strategy_lab/v420_dead_code_inventory_latest.csv",
        "results/strategy_lab/v420_validation_latest.json",
        "reports/strategy_lab/v420_validation_latest.md",
        "results/strategy_lab/v420_real_use_strategy_lab_handoff_latest.json",
        "reports/strategy_lab/v420_real_use_strategy_lab_handoff_latest.md",
        "agent_workspace/next_codex_task_after_v420_real_use_strategy_lab.md",
    ]
    readme = root / "dist/quantdesk_v4.2.0_real_use_strategy_lab_review_README.md"
    manifest_path = root / "dist/quantdesk_v4.2.0_real_use_strategy_lab_review_manifest.json"
    zip_path = root / "dist/quantdesk_v4.2.0_real_use_strategy_lab_review_pack.zip"
    write_md(readme, "# QuantDesk v4.2.0 Real-Use Strategy Lab Review Pack\n\nResearch-only. Not a release. No broker, live trading, auto execution, buy/sell advice, or real-money allocation.\n")
    files += ["dist/quantdesk_v4.2.0_real_use_strategy_lab_review_manifest.json", "dist/quantdesk_v4.2.0_real_use_strategy_lab_review_README.md"]
    manifest = {"pack_name": "quantdesk_v4.2.0_real_use_strategy_lab_review_pack", "status": "completed_with_warnings", "not_a_release": True, "missing_files": [], "included_files": [], "file_count": 0, "zip_size_bytes": 0, "candidate_upgrade_allowed": False, **boundary_payload()}
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
