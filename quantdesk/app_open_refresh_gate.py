from __future__ import annotations

from pathlib import Path
from typing import Any

from quantdesk.daily_open_workflow import md_table, write_json, write_md
from quantdesk.research_outputs_refresh import refresh_research_outputs
from quantdesk.shared import read_json as shared_read_json
from quantdesk.work_start_update_gate import run_work_start_update_gate


def _read_json(path: Path) -> dict[str, Any]:
    loaded = shared_read_json(path, {})
    return loaded if isinstance(loaded, dict) else {}


def _research_outputs_exist(root: Path) -> bool:
    required = [
        "results/daily_research/v420_daily_stock_candidates_latest.csv",
        "results/daily_research/v420_daily_sector_candidates_latest.csv",
        "results/strategy_lab/v420_strategy_leaderboard_latest.csv",
        "results/dashboard_candidate_cards_latest.json",
    ]
    return all((root / rel).exists() for rel in required)


def _skipped_research_refresh(root: Path, *, reason: str) -> dict[str, Any]:
    previous = _read_json(root / "results/data_update/v424_research_outputs_refresh_latest.json")
    payload = {
        "task_id": "v424_research_outputs_refresh",
        "status": "skipped_no_new_data",
        "reason": reason,
        "refresh_attempted": False,
        "refresh_success": True,
        "failed_steps": [],
        "candidate_count": previous.get("candidate_count", 0),
        "sector_count": previous.get("sector_count", 0),
        "leaderboard_rows": previous.get("leaderboard_rows", 0),
        "result_page_updated": (root / "reports/daily_research/v421_daily_result_page_latest.md").exists(),
        "dashboard_cards_status": previous.get("dashboard_cards_status", "preserved"),
        "daily_workflow_status": previous.get("daily_workflow_status", "preserved"),
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


def run_app_open_refresh(
    root: Path,
    *,
    network: bool = False,
    max_symbols: int = 300,
    dry_run: bool = False,
    provider: str = "akshare",
    timeout_seconds: int = 12,
) -> dict[str, Any]:
    gate = run_work_start_update_gate(
        root,
        network=network,
        max_symbols=max_symbols,
        scope="daily",
        dry_run=dry_run,
        provider=provider,
        timeout_seconds=timeout_seconds,
    )
    rows_written = int(gate["user_summary"].get("rows_written") or 0)
    is_latest = bool(gate["user_summary"].get("is_latest"))
    if rows_written > 0 or not is_latest or not _research_outputs_exist(root):
        refresh = refresh_research_outputs(root, reason="app_open_refresh", network=False, max_symbols=max_symbols)
    else:
        refresh = _skipped_research_refresh(root, reason="app_open_refresh_no_new_data")
    page = {
        "task_id": "v424_open_app_result_page",
        "status": "completed",
        "current_data_date": gate["user_summary"]["current_data_date"],
        "expected_latest_completed_trade_date": gate["user_summary"]["expected_latest_completed_trade_date"],
        "is_latest": gate["user_summary"]["is_latest"],
        "update_attempted": gate["user_summary"]["update_attempted"],
        "symbols_attempted": gate["user_summary"].get("symbols_attempted", 0),
        "symbols_updated": gate["user_summary"].get("symbols_updated", 0),
        "rows_written": gate["user_summary"]["rows_written"],
        "rows_skipped": gate["user_summary"].get("rows_skipped", 0),
        "provider_failure_count": gate["user_summary"].get("provider_failure_count", 0),
        "failure_reason": gate["user_summary"]["failure_reason"],
        "daily_research_refresh_status": refresh.get("status"),
        "candidate_count": refresh.get("candidate_count"),
        "sector_count": refresh.get("sector_count"),
        "research_only": True,
    }
    payload = {"task_id": "v424_app_open_refresh", "status": "completed_with_warnings" if gate["status"] != "completed" else "completed", "gate": gate, "research_refresh": refresh, "open_app_result_page": page, "timeout_seconds": int(timeout_seconds), "research_only": True, "no_broker": True, "no_live_trading": True, "no_auto_execution": True, "no_buy_sell_advice": True, "no_real_money_allocation": True}
    write_json(root / "results/data_update/v424_app_open_refresh_latest.json", payload)
    write_md(root / "reports/data_update/v424_app_open_refresh_latest.md", "# v424 App-Open Refresh\n\n" + md_table([page], ["current_data_date", "expected_latest_completed_trade_date", "is_latest", "update_attempted", "symbols_attempted", "symbols_updated", "rows_written", "rows_skipped", "provider_failure_count", "failure_reason", "candidate_count", "sector_count"]))
    write_json(root / "results/daily_research/v424_open_app_result_page_latest.json", page)
    write_md(root / "reports/daily_research/v424_open_app_result_page_latest.md", "# v424 Open-App Result Page\n\n" + md_table([page], ["current_data_date", "expected_latest_completed_trade_date", "is_latest", "update_attempted", "symbols_attempted", "symbols_updated", "rows_written", "rows_skipped", "provider_failure_count", "failure_reason", "candidate_count", "sector_count"]) + "\n\nResearch-only. No live trading, broker connection, auto execution, buy/sell recommendation, or real-money allocation.\n")
    return payload
