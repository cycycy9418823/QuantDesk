from __future__ import annotations

from pathlib import Path
from typing import Any

from quantdesk.dashboard_candidate_cards import write_dashboard_cards
from quantdesk.daily_open_workflow import md_table, run_daily_open_workflow, write_json, write_md


def refresh_research_outputs(root: Path, *, reason: str = "manual_gate", network: bool = False, max_symbols: int = 300) -> dict[str, Any]:
    failed_steps: list[str] = []
    workflow = {}
    dashboard = {}
    try:
        workflow = run_daily_open_workflow(root, network=False, max_symbols=max_symbols)
    except Exception as exc:
        failed_steps.append(f"daily_open_workflow:{exc}")
    try:
        dashboard = write_dashboard_cards(root)
    except Exception as exc:
        failed_steps.append(f"dashboard_cards:{exc}")
    candidate_count = 0
    sector_count = 0
    leaderboard_rows = 0
    try:
        import csv

        with (root / "results/daily_research/v420_daily_stock_candidates_latest.csv").open("r", encoding="utf-8-sig", newline="") as handle:
            candidate_count = sum(1 for _ in csv.DictReader(handle))
        with (root / "results/daily_research/v420_daily_sector_candidates_latest.csv").open("r", encoding="utf-8-sig", newline="") as handle:
            sector_count = sum(1 for _ in csv.DictReader(handle))
        with (root / "results/strategy_lab/v420_strategy_leaderboard_latest.csv").open("r", encoding="utf-8-sig", newline="") as handle:
            leaderboard_rows = sum(1 for _ in csv.DictReader(handle))
    except Exception as exc:
        failed_steps.append(f"counts:{exc}")
    payload = {
        "task_id": "v424_research_outputs_refresh",
        "status": "completed" if not failed_steps else "completed_with_warnings",
        "reason": reason,
        "refresh_attempted": True,
        "refresh_success": not failed_steps,
        "failed_steps": failed_steps,
        "candidate_count": candidate_count,
        "sector_count": sector_count,
        "leaderboard_rows": leaderboard_rows,
        "result_page_updated": (root / "reports/daily_research/v421_daily_result_page_latest.md").exists(),
        "dashboard_cards_status": dashboard.get("status", "unknown"),
        "daily_workflow_status": (workflow.get("handoff") or {}).get("status", "unknown") if workflow else "unknown",
        "research_only": True,
        "no_broker": True,
        "no_live_trading": True,
        "no_auto_execution": True,
        "no_buy_sell_advice": True,
        "no_real_money_allocation": True,
    }
    write_json(root / "results/data_update/v424_research_outputs_refresh_latest.json", payload)
    write_md(root / "reports/data_update/v424_research_outputs_refresh_latest.md", "# v424 Research Outputs Refresh\n\n" + md_table([payload], ["status", "refresh_attempted", "refresh_success", "candidate_count", "sector_count", "leaderboard_rows", "result_page_updated"]))
    return payload

