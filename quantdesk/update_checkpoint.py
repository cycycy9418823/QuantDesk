from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

from quantdesk.daily_open_workflow import md_table, write_json, write_md
from quantdesk.strategy_lab.research_signal_safety import boundary_payload


def new_run_id() -> str:
    return "v426_" + datetime.now().strftime("%Y%m%d_%H%M%S")


def build_checkpoint(
    *,
    run_id: str,
    started_at: str,
    last_symbol_processed: str = "",
    completed_symbols: list[str] | None = None,
    failed_symbols: list[str] | None = None,
    skipped_symbols: list[str] | None = None,
    rows_written: int = 0,
    max_symbols: int = 300,
    priority_only: bool = True,
) -> dict[str, Any]:
    completed_symbols = completed_symbols or []
    failed_symbols = failed_symbols or []
    skipped_symbols = skipped_symbols or []
    resume_command = (
        "python scripts/run_v426_full_market_daily_catchup.py --network true "
        f"--max-symbols {int(max_symbols)} --priority-only {str(priority_only).lower()} --resume true"
        + (f" --start-after {last_symbol_processed}" if last_symbol_processed else "")
    )
    return {
        "task_id": "v426_update_checkpoint",
        "status": "completed",
        "run_id": run_id,
        "started_at": started_at,
        "updated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "last_symbol_processed": last_symbol_processed,
        "completed_symbols": completed_symbols,
        "failed_symbols": failed_symbols,
        "skipped_symbols": skipped_symbols,
        "rows_written": int(rows_written),
        "can_resume": True,
        "resume_command": resume_command,
        **boundary_payload(),
    }


def write_checkpoint(root: Path, payload: dict[str, Any]) -> dict[str, Any]:
    write_json(root / "results/data_update/v426_update_checkpoint_latest.json", payload)
    write_md(
        root / "reports/data_update/v426_update_checkpoint_latest.md",
        "# v426 Update Checkpoint\n\n"
        + md_table([payload], ["run_id", "last_symbol_processed", "rows_written", "can_resume", "resume_command"]),
    )
    return payload
