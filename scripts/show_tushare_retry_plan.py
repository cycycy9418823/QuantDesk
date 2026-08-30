from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from quantdesk.tushare_retry_plan import (  # noqa: E402
    DEFAULT_OUTPUT_PATH,
    DEFAULT_REPORT_PATH,
    write_tushare_retry_plan_outputs,
)


def _truthy(value: Any) -> bool:
    return str(value).strip().lower() in {"1", "true", "yes", "y", "on"}


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build a read-only Tushare retry plan from local queue logs.")
    parser.add_argument("--project-root", default=str(PROJECT_ROOT))
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT_PATH))
    parser.add_argument("--report", default=str(DEFAULT_REPORT_PATH))
    parser.add_argument("--max-events", type=int, default=10000)
    parser.add_argument("--max-examples-per-group", type=int, default=5)
    parser.add_argument("--json", nargs="?", const="true", default="false")
    parser.add_argument("--summary", nargs="?", const="true", default="false")
    parser.add_argument("--network", default="false", help="Accepted for qd.py consistency; this command never fetches network data.")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    payload = write_tushare_retry_plan_outputs(
        project_root=args.project_root,
        output_path=args.output,
        report_path=args.report,
        max_events=args.max_events,
        max_examples_per_group=args.max_examples_per_group,
    )
    if _truthy(args.json):
        print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
        return 0

    summary = payload.get("summary") if isinstance(payload.get("summary"), dict) else {}
    queue = payload.get("queue") if isinstance(payload.get("queue"), dict) else {}
    print("QuantDesk Tushare retry plan")
    print(f"status: {payload.get('status')}")
    print(f"retry_gate: {payload.get('retry_gate')}")
    print(f"queue_status: {queue.get('status') or '-'}")
    print(f"current_stage: {queue.get('current_stage') or '-'}")
    print(f"fail_event_count: {summary.get('fail_event_count', 0)}")
    print(f"retry_event_count: {summary.get('retry_event_count', 0)}")
    print(f"ssl_eof_count: {summary.get('ssl_eof_count', 0)}")
    print(f"timeout_count: {summary.get('timeout_count', 0)}")
    print(f"permission_count: {summary.get('permission_count', 0)}")
    print(f"rate_limit_count: {summary.get('rate_limit_count', 0)}")
    print(f"retry_candidate_groups: {len(payload.get('retry_candidates') or [])}")
    print(f"safe_to_auto_restart: {payload.get('safe_to_auto_restart')}")
    print(f"suggested_next_action: {payload.get('suggested_next_action')}")
    print(f"output: {payload.get('output_path')}")
    print(f"report: {payload.get('report_path')}")
    print("read-only / no token output / no network fetch / no auto restart / no broker")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
