from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from quantdesk.tushare_minute_status import (  # noqa: E402
    DEFAULT_OUTPUT_PATH,
    DEFAULT_REPORT_PATH,
    write_tushare_minute_status_outputs,
)


def _truthy(value: Any) -> bool:
    return str(value).strip().lower() in {"1", "true", "yes", "y", "on"}


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Show read-only Tushare 1-minute download job status.")
    parser.add_argument("--project-root", default=str(PROJECT_ROOT))
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT_PATH))
    parser.add_argument("--report", default=str(DEFAULT_REPORT_PATH))
    parser.add_argument("--stale-after-seconds", type=int, default=900)
    parser.add_argument("--no-process-check", nargs="?", const="true", default="false")
    parser.add_argument("--json", nargs="?", const="true", default="false")
    parser.add_argument("--summary", nargs="?", const="true", default="false")
    parser.add_argument("--network", default="false", help="Accepted for qd.py consistency; this command never fetches network data.")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    payload = write_tushare_minute_status_outputs(
        project_root=args.project_root,
        output_path=args.output,
        report_path=args.report,
        include_processes=not _truthy(args.no_process_check),
        stale_after_seconds=args.stale_after_seconds,
    )
    if _truthy(args.json):
        print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
        return 0

    job = payload.get("job") if isinstance(payload.get("job"), dict) else {}
    summary = payload.get("summary") if isinstance(payload.get("summary"), dict) else {}
    logs = payload.get("log_summary") if isinstance(payload.get("log_summary"), dict) else {}
    last = logs.get("last_symbol_done") if isinstance(logs.get("last_symbol_done"), dict) else {}
    latest_quality = payload.get("latest_completed_quality") if isinstance(payload.get("latest_completed_quality"), dict) else {}
    print("QuantDesk Tushare minute status")
    print(f"status: {payload.get('status')}")
    print(f"job: {job.get('path') or '-'}")
    print(f"summary_status: {summary.get('status') or '-'}")
    print(f"symbol_count: {summary.get('symbol_count') or logs.get('symbol_count') or 0}")
    print(f"symbol_done_count: {logs.get('symbol_done_count', 0)}")
    print(f"progress_pct: {logs.get('progress_pct', 0.0)}")
    print(f"last_symbol: {last.get('symbol') or '-'}")
    print(f"rate_limit_count: {logs.get('rate_limit_count', 0)}")
    print(f"permission_count: {logs.get('permission_count', 0)}")
    print(f"traceback_count: {logs.get('traceback_count', 0)}")
    print(f"latest_completed_quality_status: {latest_quality.get('status') or '-'}")
    print(
        "latest_completed_quality_usable: "
        f"{latest_quality.get('usable_count', 0)}/{latest_quality.get('total_symbols', 0)}"
    )
    print(f"latest_completed_quality_invalid_count: {latest_quality.get('invalid_count', 0)}")
    print(f"latest_completed_quality_missing_count: {latest_quality.get('missing_count', 0)}")
    print(f"latest_completed_quality_target_date: {latest_quality.get('target_date') or '-'}")
    print(f"latest_completed_quality_suggested_next_action: {latest_quality.get('suggested_next_action') or '-'}")
    print(f"suggested_next_action: {payload.get('suggested_next_action')}")
    print(f"output: {payload.get('output_path')}")
    print(f"report: {payload.get('report_path')}")
    print("read-only / no token output / no network fetch / no broker")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
