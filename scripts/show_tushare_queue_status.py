from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from quantdesk.tushare_queue_status import (  # noqa: E402
    DEFAULT_OUTPUT_PATH,
    DEFAULT_REPORT_PATH,
    write_tushare_queue_status_outputs,
)


def _truthy(value: Any) -> bool:
    return str(value).strip().lower() in {"1", "true", "yes", "y", "on"}


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Show read-only Tushare continuous download queue status.")
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
    payload = write_tushare_queue_status_outputs(
        project_root=args.project_root,
        output_path=args.output,
        report_path=args.report,
        include_processes=not _truthy(args.no_process_check),
        stale_after_seconds=args.stale_after_seconds,
    )
    if _truthy(args.json):
        print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
        return 0

    summary = payload.get("log_summary") if isinstance(payload.get("log_summary"), dict) else {}
    queue = payload.get("queue") if isinstance(payload.get("queue"), dict) else {}
    stage_progress = queue.get("stage_progress") if isinstance(queue.get("stage_progress"), dict) else {}
    processes = payload.get("processes") if isinstance(payload.get("processes"), dict) else {}
    print("QuantDesk Tushare queue status")
    print(f"status: {payload.get('status')}")
    print(f"current_stage: {queue.get('current_stage') or '-'}")
    print(f"stage_progress: {stage_progress.get('stage_position_label') or '-'}")
    print(f"stage_completed_count: {stage_progress.get('completed_stage_count', 0)}")
    print(f"stage_remaining_count: {stage_progress.get('remaining_stage_count', 0)}")
    remaining = stage_progress.get("remaining_stage_keys") if isinstance(stage_progress.get("remaining_stage_keys"), list) else []
    print(f"stage_remaining_keys: {', '.join(str(item) for item in remaining) or '-'}")
    print(f"stage_current_elapsed_seconds: {stage_progress.get('current_stage_elapsed_seconds')}")
    print(f"stage_last_event_age_seconds: {stage_progress.get('last_stage_event_age_seconds')}")
    print(f"queue: {queue.get('path') or '-'}")
    print(f"process_checked: {processes.get('checked')}")
    print(f"ok_count: {summary.get('ok_count', 0)}")
    print(f"fail_count: {summary.get('fail_count', 0)}")
    print(f"ssl_eof_count: {summary.get('ssl_eof_count', 0)}")
    print(f"timeout_count: {summary.get('timeout_count', 0)}")
    print(f"traceback_count: {summary.get('traceback_count', 0)}")
    print(f"suggested_next_action: {payload.get('suggested_next_action')}")
    print(f"output: {payload.get('output_path')}")
    print(f"report: {payload.get('report_path')}")
    print("read-only / no token output / no network fetch / no broker")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
