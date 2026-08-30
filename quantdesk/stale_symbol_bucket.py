from __future__ import annotations

import csv
from datetime import date
from pathlib import Path
from typing import Any

from quantdesk.daily_open_workflow import md_table, write_json, write_md
from quantdesk.market_session_detector import detect_market_session
from quantdesk.strategy_lab.research_signal_safety import boundary_payload
from quantdesk.symbol_market_normalizer import normalize_symbol_market
from quantdesk.update_queue_builder import fast_last_date, priority_symbol_groups


def _stale_days(latest: str, expected: str) -> int:
    try:
        return max(0, (date.fromisoformat(expected) - date.fromisoformat(latest)).days)
    except Exception:
        return 9999


def build_stale_buckets(root: Path, *, max_symbols: int = 300, buckets: set[str] | None = None) -> dict[str, Any]:
    root = root.resolve()
    expected = detect_market_session(root)["expected_latest_completed_trade_date"]
    groups = priority_symbol_groups(root)
    rows: list[dict[str, Any]] = []
    for path in sorted((root / "data/daily").glob("*.csv")):
        symbol = path.stem
        latest = fast_last_date(path)
        if latest and latest >= expected:
            continue
        norm = normalize_symbol_market(symbol)
        if symbol in groups["P0"]:
            bucket = "P0"
            reason = "current_daily_candidates_or_holdings"
        elif symbol in groups["P1"]:
            bucket = "P1"
            reason = "selected_or_sector_mapped"
        elif norm["support_status"] == "supported":
            bucket = "P2"
            reason = "normal_a_share_stale"
        else:
            bucket = "P3"
            reason = norm["support_status"]
        rows.append(
            {
                "symbol": symbol,
                "latest_local_date": latest,
                "expected_latest_date": expected,
                "stale_days": _stale_days(latest, expected),
                "bucket": bucket,
                "reason": reason,
                "provider_symbol": norm["provider_symbol"],
                "support_status": norm["support_status"],
                "update_priority": {"P0": 0, "P1": 1, "P2": 2, "P3": 3}[bucket],
            }
        )
    if buckets:
        rows = [row for row in rows if row["bucket"] in buckets]
    rows.sort(key=lambda row: (row["update_priority"], -int(row["stale_days"]), row["symbol"]))
    selected = rows[: max(0, int(max_symbols))]
    payload = {"task_id": "v427_stale_symbol_buckets", "status": "completed", "expected_latest_date": expected, "total_stale_symbols": len(rows), "selected_symbols": len(selected), "buckets": selected, **boundary_payload()}
    write_json(root / "results/data_update/v427_stale_symbol_buckets_latest.json", payload)
    write_md(root / "reports/data_update/v427_stale_symbol_buckets_latest.md", "# v427 Stale Symbol Buckets\n\n" + md_table(selected[:100], ["symbol", "latest_local_date", "expected_latest_date", "stale_days", "bucket", "reason", "provider_symbol", "support_status"]))
    csv_path = root / "results/data_update/v427_stale_symbol_buckets_latest.csv"
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    with csv_path.open("w", encoding="utf-8", newline="") as handle:
        cols = ["symbol", "latest_local_date", "expected_latest_date", "stale_days", "bucket", "reason", "provider_symbol", "support_status", "update_priority"]
        writer = csv.DictWriter(handle, fieldnames=cols)
        writer.writeheader()
        writer.writerows([{col: row.get(col, "") for col in cols} for row in selected])
    return payload
