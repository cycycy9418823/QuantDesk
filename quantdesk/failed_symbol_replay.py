from __future__ import annotations

import csv
from pathlib import Path
from typing import Any

from quantdesk.daily_open_workflow import md_table, write_json, write_md
from quantdesk.strategy_lab.research_signal_safety import boundary_payload


def build_replay_queue(root: Path, classifications: list[dict[str, Any]], *, bucket: str = "P0,P1", max_symbols: int = 300) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    for item in classifications:
        retryable = bool(item.get("retryable"))
        reason = str(item.get("final_failure_reason") or "")
        if reason in {"unsupported_market", "delisted_or_no_recent_data"}:
            retryable = False
        provider = "tencent" if reason in {"akshare_remote_disconnect", "tencent_date_missing", "tencent_empty_response"} else "auto"
        rows.append(
            {
                "symbol": item.get("symbol"),
                "provider_symbol": item.get("provider_symbol"),
                "last_failure_reason": reason,
                "retryable": retryable,
                "recommended_provider": provider if retryable else "none",
                "recommended_delay_seconds": 600 if retryable else 0,
                "retry_command": f"python scripts/run_v427_batch_catchup.py --network true --max-symbols {int(max_symbols)} --bucket {bucket} --provider {provider} --resume true" if retryable else "",
                "human_review_required": not retryable and reason not in {"tencent_date_missing"},
            }
        )
    payload = {"task_id": "v427_failed_symbol_replay_queue", "status": "completed", "queue": rows, "retryable_count": sum(1 for row in rows if row["retryable"]), **boundary_payload()}
    write_json(root / "results/data_update/v427_failed_symbol_replay_queue_latest.json", payload)
    write_md(root / "reports/data_update/v427_failed_symbol_replay_queue_latest.md", "# v427 Failed Symbol Replay Queue\n\n" + md_table(rows[:100], ["symbol", "last_failure_reason", "retryable", "recommended_provider", "recommended_delay_seconds", "human_review_required"]))
    csv_path = root / "results/data_update/v427_failed_symbol_replay_queue_latest.csv"
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    with csv_path.open("w", encoding="utf-8", newline="") as handle:
        cols = ["symbol", "provider_symbol", "last_failure_reason", "retryable", "recommended_provider", "recommended_delay_seconds", "retry_command", "human_review_required"]
        writer = csv.DictWriter(handle, fieldnames=cols)
        writer.writeheader()
        writer.writerows([{col: row.get(col, "") for col in cols} for row in rows])
    return payload
