"""Explicit updates through Data Close Pipeline (Charter sections 3 and 5.1).

Opening the app never generates candidates or changes a paper account.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from quantdesk.market_clock import expected_settlement_date, to_shanghai_datetime
from quantdesk.pipeline_terminal import pipeline_failure_details
from quantdesk.shared import now_text, truthy
from quantdesk.utils import atomic_write_json


BOUNDARY = {"research_only": True, "paper_only": True, "no_broker": True,
            "no_live_trading": True, "no_auto_execution": True}


def resolve_startup_target_date(target_date: str | None = None) -> tuple[str | None, str]:
    if target_date:
        return target_date, "explicit"
    expected = expected_settlement_date(to_shanghai_datetime())
    return (expected.isoformat(), "latest_completed_market_session") if expected else (None, "unresolved")


def write_startup_payload(root: Path, payload: dict[str, Any]) -> Path:
    return atomic_write_json(root / "results" / "startup_update_latest.json", payload)


def build_failure_payload(root: Path, exc: BaseException, *, enable_network: bool, max_symbols: int) -> dict[str, Any]:
    return {"task_id": "startup_data_update", "status": "failed", "ok": False,
            "scope": "full_a", "daily_writer": "data_close_pipeline",
            "startup_update_attempted": bool(enable_network),
            "provider_network_authorized": bool(enable_network), "network": bool(enable_network),
            "message": f"Data Close Pipeline failed: {type(exc).__name__}: {exc}",
            "failure_details": pipeline_failure_details({"status": "failed", "error_type": type(exc).__name__}),
            "updated_at": now_text(), **BOUNDARY}


def run_startup_data_update(
    root: Path, *, enable_network: bool, max_symbols: int = 80,
    target_date: str | None = None, providers: list[str] | None = None,
    rebuild_features: bool = True, dry_run: bool = False,
    generate_candidates: bool = False, candidate_max_scan_symbols: int = 300,
    candidate_max_candidates: int = 18,
) -> dict[str, Any]:
    """Delegate an explicit update; legacy pool options do not narrow full A."""
    from scripts.run_data_close_pipeline import run_pipeline
    from scripts.run_daily_data_refresh import _complete_live_session

    target, target_source = resolve_startup_target_date(target_date)
    network = bool(enable_network and not dry_run)
    payload: dict[str, Any] = {
        "task_id": "startup_data_update", "status": "planned_offline", "ok": False,
        "scope": "full_a", "only_low_price": False, "full_scan": False,
        "daily_writer": "data_close_pipeline", "max_symbols": 0,
        "target_date": target or "", "target_date_source": target_source,
        "network": network, "provider_network_authorized": bool(enable_network),
        "dry_run": not network, "startup_update_attempted": False,
        "updated_symbols": 0, "failed_symbols": None,
        "stale_count_after": None, "missing_count_after": None,
        "latest_local_date_after": None, "updated_at": now_text(), "path": "",
        "candidate_generation": {"status": "skipped", "reason": "explicit_research_only"},
        "message": "未运行收盘更新，仅读取已有数据。", **BOUNDARY,
    }
    if not network:
        return payload
    if not target or not _complete_live_session(target):
        payload.update(status="awaiting_close", message="本次未更新：实时收盘快照尚不可用，仅读取已有日线。")
        return payload

    report = run_pipeline(root=root, network=True, target_date=target,
                          trigger="manual", alert_push=False, alert_network=False)
    status = str(report.get("status") or "failed")
    market = report.get("stages", {}).get("market_refresh", {}).get("payload", {}).get("stdout_json", {})
    bars = market.get("families", {}).get("bars_and_valuation", {}).get("bars", {})
    payload.update(
        status=status, ok=status in {"completed_all_executed", "completed_with_skips"},
        startup_update_attempted=True, pipeline=report, run_id=report.get("run_id"),
        failure_details=pipeline_failure_details(report),
        updated_symbols=bars.get("appended", 0),
        existing_target_symbols=bars.get("already_had_bar", 0),
        path="results/startup_update_latest.json",
        message=f"收盘更新状态：{status}；本轮新增 {bars.get('appended', 0)} 只日线。",
    )
    write_startup_payload(root, payload)
    return payload


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", default=str(ROOT))
    parser.add_argument("--enable-network", action="store_true")
    parser.add_argument("--target-date", default="")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--json", default="true")
    # Preserve old launcher syntax; Pipeline owns its pool/provider policy.
    parser.add_argument("--max-symbols", type=int, default=80)
    parser.add_argument("--providers", default="")
    parser.add_argument("--rebuild-features", default="true")
    parser.add_argument("--generate-candidates", default="false")
    parser.add_argument("--candidate-max-scan-symbols", type=int, default=300)
    parser.add_argument("--candidate-max-candidates", type=int, default=18)
    args = parser.parse_args(argv)
    root = Path(args.root).resolve()
    try:
        if args.enable_network and not args.dry_run:
            from scripts.run_data_close_pipeline import _protect_child_process_tree
            _protect_child_process_tree()
        payload = run_startup_data_update(root, enable_network=args.enable_network,
                                         target_date=args.target_date or None, dry_run=args.dry_run)
    except Exception as exc:
        payload = build_failure_payload(root, exc, enable_network=args.enable_network, max_symbols=0)
    if truthy(args.json):
        print(json.dumps(payload, ensure_ascii=False))
    else:
        print(f"startup_update status={payload['status']}: {payload['message']}")
    return 1 if payload["status"] in {"failed", "blocked"} else 0


if __name__ == "__main__":
    raise SystemExit(main())
