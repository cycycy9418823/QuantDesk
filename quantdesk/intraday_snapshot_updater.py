from __future__ import annotations

import csv
from datetime import datetime
from pathlib import Path
from typing import Any

from quantdesk.daily_open_workflow import md_table, write_json, write_md
from quantdesk.market_session_detector import detect_market_session
from quantdesk.providers.tencent_provider import fetch_realtime_quote


INTRADAY_SESSIONS = {"morning_session", "lunch_break", "afternoon_session", "near_close"}


def run_intraday_snapshot_update(root: Path, *, network: bool = False, max_symbols: int = 80, provider: str = "bounded_optional_provider", timeout_seconds: int = 30) -> dict[str, Any]:
    session = detect_market_session(root)
    attempted = bool(network and session["session"] in INTRADAY_SESSIONS)
    rows_written = 0
    failure_reason = ""
    provider_attempts: list[dict[str, Any]] = []
    snapshot_path = root / "data/intraday_snapshots/latest_snapshot.csv"
    if not network:
        status = "skipped_network_false"
        failure_reason = "network=false"
    elif session["session"] not in INTRADAY_SESSIONS:
        status = "skipped_session_not_intraday"
        failure_reason = f"session={session['session']}"
    else:
        symbols = [path.stem for path in sorted((root / "data/daily").glob("*.csv"))[: max(1, min(int(max_symbols), 80))]]
        quote = fetch_realtime_quote(symbols, network=True, timeout=min(int(timeout_seconds), 30))
        provider_attempts.append({"provider": "tencent_realtime", "status": quote.get("status"), "ok": quote.get("ok"), "errors": quote.get("errors", [])})
        snapshot_path.parent.mkdir(parents=True, exist_ok=True)
        with snapshot_path.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=["captured_at", "symbol", "last_price", "source", "intraday_proxy"])
            writer.writeheader()
            for row in quote.get("data") or []:
                writer.writerow(
                    {
                        "captured_at": datetime.now().astimezone().isoformat(timespec="seconds"),
                        "symbol": row.get("symbol", ""),
                        "last_price": row.get("price", ""),
                        "source": "tencent_realtime",
                        "intraday_proxy": "false",
                    }
                )
                rows_written += 1
        if rows_written:
            status = "completed"
        else:
            status = "provider_failed_fallback_local"
            failure_reason = quote.get("failed_reason") or "; ".join(quote.get("errors") or []) or "bounded intraday provider returned no rows"
    payload = {
        "task_id": "v424_intraday_snapshot_update",
        "status": status,
        "attempted": attempted,
        "network": bool(network),
        "session": session["session"],
        "provider": provider,
        "timeout_seconds": int(timeout_seconds),
        "max_symbols": int(max_symbols),
        "rows_written": rows_written,
        "snapshot_file": str(snapshot_path.relative_to(root)).replace("\\", "/"),
        "failure_reason": failure_reason,
        "provider_attempts": provider_attempts,
        "writes_data_daily": False,
        "intraday_proxy": rows_written == 0,
        "research_only": True,
        "no_broker": True,
        "no_live_trading": True,
        "no_auto_execution": True,
        "no_buy_sell_advice": True,
        "no_real_money_allocation": True,
    }
    write_json(root / "results/data_update/v424_intraday_snapshot_update_latest.json", payload)
    write_md(root / "reports/data_update/v424_intraday_snapshot_update_latest.md", "# v424 Intraday Snapshot Update\n\n" + md_table([payload], ["status", "attempted", "session", "network", "rows_written", "failure_reason", "writes_data_daily"]))
    return payload
