from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from quantdesk.daily_open_workflow import md_table, write_json, write_md
from quantdesk.market_session_detector import detect_market_session


def build_data_update_status(root: Path) -> tuple[dict[str, Any], str]:
    session = detect_market_session(root)
    gate_path = root / "results/data_update/v424_work_start_update_gate_latest.json"
    gate = json.loads(gate_path.read_text(encoding="utf-8-sig")) if gate_path.exists() else {}
    user = gate.get("user_summary", {})
    payload = {
        "task_id": "v424_data_update_status",
        "status": "completed",
        "current_local_time": session["current_local_time"],
        "session": session["session"],
        "latest_local_trade_date": session["latest_local_trade_date"],
        "expected_latest_completed_trade_date": session["expected_latest_completed_trade_date"],
        "needs_update": not bool(session["data_fresh_for_latest_trading_day"]),
        "last_update_status": (gate.get("update_result") or {}).get("status", ""),
        "last_update_failure_reason": user.get("failure_reason", ""),
        "today_candidates_based_on_latest_data": bool(session["data_fresh_for_latest_trading_day"]),
        "stale_warning": "" if session["data_fresh_for_latest_trading_day"] else "Current results are based on stale local data; rerun with --network true after user approval.",
        "research_only": True,
        "no_broker": True,
        "no_live_trading": True,
        "no_auto_execution": True,
        "no_buy_sell_advice": True,
        "no_real_money_allocation": True,
    }
    md = "# v424 Data Update Status\n\n" + md_table([payload], ["current_local_time", "session", "latest_local_trade_date", "expected_latest_completed_trade_date", "needs_update", "last_update_status", "last_update_failure_reason"]) + "\n\nResearch-only. No live trading, broker connection, auto execution, buy/sell recommendation, or real-money allocation.\n"
    write_json(root / "results/data_update/v424_data_update_status_latest.json", payload)
    write_md(root / "reports/data_update/v424_data_update_status_latest.md", md)
    return payload, md

