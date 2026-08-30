from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from quantdesk.extended_staged_data_importer import (  # noqa: E402
    DEFAULT_AUDIT_OUTPUT_PATH,
    DEFAULT_AUDIT_REPORT_PATH,
    build_extended_staged_import_audit,
)


def _truthy(value: Any) -> bool:
    return str(value).strip().lower() in {"1", "true", "yes", "y", "on"}


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Read-only preflight audit for staged extended market-data import.")
    parser.add_argument("--project-root", default=str(PROJECT_ROOT))
    parser.add_argument("--output", default=str(DEFAULT_AUDIT_OUTPUT_PATH))
    parser.add_argument("--report", default=str(DEFAULT_AUDIT_REPORT_PATH))
    parser.add_argument(
        "--max-files-per-rule",
        type=int,
        default=0,
        help="0 means full scan. Use a positive number only for fast bounded diagnostics.",
    )
    parser.add_argument("--json", nargs="?", const="true", default="false")
    parser.add_argument("--summary", nargs="?", const="true", default="false")
    parser.add_argument("--network", default="false", help="Accepted for qd.py consistency; audit never fetches network data.")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    payload = build_extended_staged_import_audit(
        project_root=args.project_root,
        max_files_per_rule=args.max_files_per_rule,
        output_path=args.output,
        report_path=args.report,
    )
    if _truthy(args.json):
        print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
        return 0
    print("QuantDesk extended staged import audit")
    print(f"status: {payload.get('status')}")
    print(f"preapply_gate: {payload.get('preapply_gate')}")
    print(f"planned_file_count: {payload.get('planned_file_count')}")
    print(f"duplicate_destination_count: {payload.get('duplicate_destination_count')}")
    print(f"existing_destination_count: {payload.get('existing_destination_count')}")
    queue = payload.get("download_queue") if isinstance(payload.get("download_queue"), dict) else {}
    print(f"queue_status: {queue.get('status') or '-'}")
    print(f"queue_stage: {queue.get('current_stage') or '-'}")
    print(f"queue_liveness: {payload.get('queue_liveness') or '-'}")
    print(f"queue_process_found: {payload.get('queue_process_found')}")
    print(f"suggested_next_action: {payload.get('suggested_next_action') or '-'}")
    print(f"apply_command_after_review: {payload.get('apply_command_after_review') or '-'}")
    print(f"full_audit_command: {payload.get('full_audit_command') or '-'}")
    print(f"source_scan_full_audit_command: {payload.get('source_scan_full_audit_command') or '-'}")
    domain_counts = payload.get("domain_counts")
    if isinstance(domain_counts, dict) and domain_counts:
        print("domain_counts:")
        for key in sorted(domain_counts):
            print(f"  {key}: {domain_counts[key]}")
    missing = payload.get("missing_planned_domains") or []
    print(f"missing_planned_domains: {', '.join(missing) if missing else '-'}")
    print(f"output: {payload.get('output_path') or args.output}")
    print(f"report: {payload.get('report_path') or args.report}")
    print("read-only / research-only / local data only / no network / no broker")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
