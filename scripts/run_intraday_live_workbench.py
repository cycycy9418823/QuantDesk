from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from quantdesk.intraday_live_engine import (  # noqa: E402
    DEFAULT_LATEST_JSON,
    build_intraday_live_workbench,
    init_intraday_live_account_state,
    load_intraday_live_config,
    load_intraday_live_latest_snapshot,
    write_intraday_live_workbench_outputs,
)

from quantdesk.cli_summary import build_cli_summary, format_human_summary  # noqa: E402
from quantdesk.shared import truthy  # noqa: E402


def _non_blank_path(value: str) -> str:
    if not value.strip():
        raise argparse.ArgumentTypeError("path must not be empty or whitespace")
    return value


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the research-only Intraday Live Workbench.")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--once", action="store_true", help="Build one intraday live snapshot and exit.")
    mode.add_argument("--status", action="store_true", help="Read latest snapshot/status only; no network.")
    mode.add_argument("--refresh", action="store_true", help="Refresh once and write latest artifacts.")
    mode.add_argument("--loop", action="store_true", help="Loop in the foreground until Ctrl+C or max iterations.")
    mode.add_argument("--init", action="store_true", help="Initialize state/intraday_live_account.json; no broker and no real orders.")
    parser.add_argument("--force-init", default="false", help="Overwrite an existing intraday live account state when used with --init.")
    parser.add_argument("--network", default="false")
    parser.add_argument("--watchlist", default="")
    parser.add_argument("--spot-refresh-seconds", type=int, default=None)
    parser.add_argument("--minute-bar-refresh-seconds", type=int, default=None)
    parser.add_argument("--max-iterations", type=int, default=None)
    parser.add_argument("--json", default="false")
    parser.add_argument("--summary", nargs="?", const="true", default="false")
    parser.add_argument("--partial-daily-dir", type=_non_blank_path, default=None)
    parser.add_argument("--spot-snapshot-dir", type=_non_blank_path, default=None)
    parser.add_argument("--output-dir", default="results")
    parser.add_argument("--report-dir", default="reports")
    return parser.parse_args(argv)


def _config_from_args(args: argparse.Namespace) -> dict[str, Any]:
    overrides: dict[str, Any] = {}
    if args.watchlist:
        overrides["watchlist"] = [item.strip() for item in args.watchlist.split(",") if item.strip()]
    if args.spot_refresh_seconds is not None:
        overrides["spot_refresh_seconds"] = args.spot_refresh_seconds
    if args.minute_bar_refresh_seconds is not None:
        overrides["minute_bar_refresh_seconds"] = args.minute_bar_refresh_seconds
    if args.max_iterations is not None:
        overrides["loop_max_iterations"] = args.max_iterations
    if args.partial_daily_dir is not None:
        overrides["partial_daily_dir"] = args.partial_daily_dir
    if args.spot_snapshot_dir is not None:
        overrides["spot_snapshot_dir"] = args.spot_snapshot_dir
    return load_intraday_live_config(overrides)


def _run_once(args: argparse.Namespace) -> dict[str, Any]:
    config = _config_from_args(args)
    payload = build_intraday_live_workbench(config, once=True, network=truthy(args.network))
    return write_intraday_live_workbench_outputs(payload, output_dir=args.output_dir, report_dir=args.report_dir)


def _run_status(args: argparse.Namespace) -> dict[str, Any]:
    snapshot = load_intraday_live_latest_snapshot(DEFAULT_LATEST_JSON)
    payload = {
        "ok": snapshot.get("status") != "failed",
        "status": snapshot.get("status") or "missing",
        "task_type": "intraday_live_workbench",
        "generated_at": snapshot.get("snapshot_time") or "",
        "app_version": "",
        "network": False,
        "trade_date": "",
        "snapshot_time": snapshot.get("snapshot_time") or "",
        "market_session": snapshot.get("market_session") or "",
        "watchlist_count": snapshot.get("watchlist_count", 0),
        "quote_count": snapshot.get("quote_count", 0),
        "partial_daily": {"row_count": 0, "is_complete": False},
        "intraday_scores": [{}] * int(snapshot.get("intraday_score_count") or 0),
        "condition_plan_status": {"triggered_count": snapshot.get("triggered_condition_count", 0)},
        "sector_heat": {"items": [{}] * int(snapshot.get("sector_heat_count") or 0)},
        "risk_flags": snapshot.get("risk_flags", []),
        "warnings": snapshot.get("warnings", []),
        "errors": [],
        "output_paths": {"latest_json": snapshot.get("latest_json") or DEFAULT_LATEST_JSON, "latest_report": snapshot.get("latest_report") or ""},
        "live_account_initialized": snapshot.get("live_account_initialized", False),
        "suggested_next_action": "Run python scripts/qd.py live --init to create a research-only intraday virtual account state." if not snapshot.get("live_account_initialized") else "",
        "disclaimer": snapshot.get("disclaimer") or "Intraday data is provisional. Research-only. Not investment advice.",
    }
    return payload


def _run_loop(args: argparse.Namespace) -> dict[str, Any]:
    config = _config_from_args(args)
    interval = max(1, int(config.get("spot_refresh_seconds") or 60))
    max_iterations = args.max_iterations if args.max_iterations is not None else int(config.get("loop_max_iterations") or 0)
    iteration = 0
    latest: dict[str, Any] = {}
    while True:
        iteration += 1
        payload = build_intraday_live_workbench(config, once=False, network=truthy(args.network))
        payload["loop_iteration"] = iteration
        latest = write_intraday_live_workbench_outputs(payload, output_dir=args.output_dir, report_dir=args.report_dir)
        if max_iterations and iteration >= max_iterations:
            break
        time.sleep(interval)
    return latest


def run(args: argparse.Namespace) -> dict[str, Any]:
    if args.init:
        return init_intraday_live_account_state(force=truthy(args.force_init))
    if args.status:
        return _run_status(args)
    if args.loop:
        return _run_loop(args)
    return _run_once(args)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    payload = run(args)
    summary = build_cli_summary(payload, task_type=str(payload.get("task_type") or "intraday_live_workbench"))
    wants_json = truthy(args.json)
    wants_summary = truthy(args.summary)
    if wants_summary:
        if wants_json:
            print(json.dumps(summary, ensure_ascii=False, separators=(",", ":"), allow_nan=False))
        else:
            print(format_human_summary(summary))
    elif wants_json:
        print(json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False))
    else:
        print(format_human_summary(summary))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
