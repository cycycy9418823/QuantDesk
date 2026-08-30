from __future__ import annotations

from collections import Counter
from pathlib import Path
from typing import Any

from quantdesk.daily_open_workflow import md_table, write_json, write_md
from quantdesk.market_session_detector import detect_market_session
from quantdesk.strategy_lab.research_signal_safety import boundary_payload
from quantdesk.update_queue_builder import fast_last_date


def build_coverage_status(root: Path) -> dict[str, Any]:
    root = root.resolve()
    session = detect_market_session(root)
    expected = session["expected_latest_completed_trade_date"]
    daily_paths = sorted((root / "data/daily").glob("*.csv"))
    dist = Counter(fast_last_date(path) or "<blank>" for path in daily_paths)
    execution = {}
    coverage = {}
    refresh = {}
    for rel, target in [
        ("results/data_update/v426_catchup_execution_latest.json", "execution"),
        ("results/data_update/v426_provider_coverage_latest.json", "coverage"),
        ("results/data_update/v426_research_refresh_latest.json", "refresh"),
    ]:
        path = root / rel
        if path.exists():
            import json

            value = json.loads(path.read_text(encoding="utf-8-sig"))
            if target == "execution":
                execution = value
            elif target == "coverage":
                coverage = value
            else:
                refresh = value
    failures = Counter()
    for row in execution.get("records", []) or []:
        if row.get("status") == "updated":
            continue
        reason = str(row.get("failure_reason") or row.get("status") or "")
        if reason:
            failures[reason[:120]] += 1
    payload = {
        "task_id": "v426_update_coverage_status",
        "status": "completed",
        "latest_expected_trading_day": expected,
        "data_daily_total": len(daily_paths),
        "updated_to_latest_count": dist.get(expected, 0),
        "still_stale_count": max(0, len(daily_paths) - dist.get(expected, 0)),
        "this_run_symbols_updated": execution.get("symbols_updated", 0),
        "this_run_rows_written": execution.get("rows_written", 0),
        "akshare_success_count": coverage.get("akshare_success", 0),
        "tencent_fallback_success_count": coverage.get("tencent_success", 0),
        "provider_both_failed_count": coverage.get("both_failed", 0),
        "top_failure_reasons": [{"reason": key, "count": value} for key, value in failures.most_common(10)],
        "latest_candidates_based_on_latest_data": bool(refresh.get("refresh_success")),
        "next_recommended_command": "python scripts/run_v427_batch_catchup.py --network true --max-symbols 300 --bucket P0,P1 --provider auto --resume true",
        **boundary_payload(),
    }
    write_json(root / "results/data_update/v426_update_coverage_status_latest.json", payload)
    write_md(
        root / "reports/data_update/v426_update_coverage_status_latest.md",
        "# v426 Update Coverage Status\n\n"
        + md_table([payload], ["latest_expected_trading_day", "data_daily_total", "updated_to_latest_count", "still_stale_count", "this_run_symbols_updated", "this_run_rows_written", "akshare_success_count", "tencent_fallback_success_count", "provider_both_failed_count"])
        + "\n\nNext command: `"
        + payload["next_recommended_command"]
        + "`\n",
    )
    return payload


def format_coverage_status(payload: dict[str, Any]) -> str:
    lines = [
        "QuantDesk v4.2.6 Update Coverage Status",
        f"Latest expected trading day: {payload.get('latest_expected_trading_day')}",
        f"data/daily total: {payload.get('data_daily_total')}",
        f"Updated to latest: {payload.get('updated_to_latest_count')}",
        f"Still stale: {payload.get('still_stale_count')}",
        f"This run symbols updated: {payload.get('this_run_symbols_updated')}",
        f"This run rows written: {payload.get('this_run_rows_written')}",
        f"AKShare success: {payload.get('akshare_success_count')}",
        f"Tencent fallback success: {payload.get('tencent_fallback_success_count')}",
        f"Provider both failed: {payload.get('provider_both_failed_count')}",
        f"Latest candidates based on refreshed outputs: {payload.get('latest_candidates_based_on_latest_data')}",
        "Top failure reasons:",
    ]
    reasons = payload.get("top_failure_reasons") or []
    if reasons:
        lines.extend(f"- {row.get('reason')}: {row.get('count')}" for row in reasons)
    else:
        lines.append("- none")
    lines.append(f"Next recommended command: {payload.get('next_recommended_command')}")
    lines.append("Research-only. No broker, orders, auto execution, buy/sell advice, or real-money allocation.")
    return "\n".join(lines)
