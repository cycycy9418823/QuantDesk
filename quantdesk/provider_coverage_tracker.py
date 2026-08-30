from __future__ import annotations

import csv
import inspect
from pathlib import Path
from typing import Any

from quantdesk.bounded_market_data_provider import fetch_bounded_daily_row
from quantdesk.daily_open_workflow import md_table, write_json, write_md
from quantdesk.strategy_lab.research_signal_safety import boundary_payload


def coverage_record(symbol: str, expected_date: str, *, provider: str = "akshare", network: bool = False, timeout_seconds: int = 30) -> dict[str, Any]:
    if not network:
        return {
            "symbol": symbol,
            "provider_attempted": False,
            "akshare_status": "skipped_network_false",
            "tencent_status": "skipped_network_false",
            "selected_provider": "",
            "failure_reason": "network=false",
            "data_date_returned": "",
            "row_count_returned": 0,
            "usable_for_write": False,
            "skip_reason": "network=false",
        }
    fetch_kwargs: dict[str, Any] = {"provider": provider, "timeout_seconds": timeout_seconds}
    if "network" in inspect.signature(fetch_bounded_daily_row).parameters:
        fetch_kwargs["network"] = network
    fetched = fetch_bounded_daily_row(symbol, expected_date, **fetch_kwargs)
    statuses = {str(item.get("provider")): str(item.get("status")) for item in fetched.get("attempts", []) or []}
    errors = [str(item.get("error") or item.get("warnings") or "") for item in fetched.get("attempts", []) or [] if item.get("status") != "ok"]
    row = dict(fetched.get("row") or {})
    return {
        "symbol": symbol,
        "provider_attempted": True,
        "akshare_status": statuses.get("akshare_daily", "not_attempted"),
        "tencent_status": statuses.get("tencent_daily", "not_attempted"),
        "selected_provider": fetched.get("provider_used", ""),
        "failure_reason": "; ".join(item for item in errors if item),
        "data_date_returned": row.get("date", ""),
        "row_count_returned": 1 if fetched.get("status") == "ok" else 0,
        "usable_for_write": fetched.get("status") == "ok" and row.get("date") == expected_date,
        "skip_reason": "" if fetched.get("status") == "ok" else "provider_failed_or_date_not_returned",
    }


def write_provider_coverage(root: Path, records: list[dict[str, Any]]) -> dict[str, Any]:
    payload = {
        "task_id": "v426_provider_coverage",
        "status": "completed",
        "records": records,
        "symbol_count": len(records),
        "akshare_success": sum(1 for row in records if row.get("selected_provider") == "akshare_daily"),
        "tencent_success": sum(1 for row in records if row.get("selected_provider") == "tencent_daily"),
        "both_failed": sum(1 for row in records if row.get("provider_attempted") and not row.get("usable_for_write")),
        **boundary_payload(),
    }
    write_json(root / "results/data_update/v426_provider_coverage_latest.json", payload)
    write_md(root / "reports/data_update/v426_provider_coverage_latest.md", "# v426 Provider Coverage\n\n" + md_table(records[:100], ["symbol", "akshare_status", "tencent_status", "selected_provider", "data_date_returned", "usable_for_write", "skip_reason"]))
    csv_path = root / "results/data_update/v426_provider_coverage_latest.csv"
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    with csv_path.open("w", encoding="utf-8", newline="") as handle:
        cols = ["symbol", "provider_attempted", "akshare_status", "tencent_status", "selected_provider", "failure_reason", "data_date_returned", "row_count_returned", "usable_for_write", "skip_reason"]
        writer = csv.DictWriter(handle, fieldnames=cols)
        writer.writeheader()
        writer.writerows([{col: row.get(col, "") for col in cols} for row in records])
    return payload
