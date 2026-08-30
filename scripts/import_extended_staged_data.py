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
    DEFAULT_OUTPUT_PATH,
    build_extended_staged_import_plan,
)


def _truthy(value: Any) -> bool:
    return str(value).strip().lower() in {"1", "true", "yes", "y", "on"}


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Dry-run-first import of extended staged Tushare data into canonical local dirs.")
    parser.add_argument("--project-root", default=str(PROJECT_ROOT))
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT_PATH))
    parser.add_argument("--apply", nargs="?", const="true", default="false", help="Default false. Required for canonical writes.")
    parser.add_argument("--allow-running-queue", nargs="?", const="true", default="false", help="Dangerous override: allow --apply while the Tushare queue is still running.")
    parser.add_argument("--overwrite", nargs="?", const="true", default="false")
    parser.add_argument(
        "--max-files-per-rule",
        type=int,
        default=0,
        help="0 means full scan/import. Use a positive number only for bounded dry-run diagnostics.",
    )
    parser.add_argument("--json", nargs="?", const="true", default="false")
    parser.add_argument("--summary", nargs="?", const="true", default="false")
    parser.add_argument("--network", default="false", help="Accepted for qd.py consistency; importer never fetches network data.")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    payload = build_extended_staged_import_plan(
        project_root=args.project_root,
        apply=_truthy(args.apply),
        allow_running_queue=_truthy(args.allow_running_queue),
        overwrite=_truthy(args.overwrite),
        max_files_per_rule=args.max_files_per_rule,
        output_path=args.output,
    )
    if _truthy(args.json):
        print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
        return 0
    mode = "BLOCKED" if payload.get("status") == "blocked" else ("APPLY" if payload.get("apply") else "DRY-RUN")
    print(f"QuantDesk extended staged data import: {mode}")
    if payload.get("blocked_reason"):
        print(f"blocked_reason: {payload.get('blocked_reason')}")
        print(f"suggested_next_action: {payload.get('suggested_next_action')}")
    print(f"planned_file_count: {payload.get('planned_file_count')}")
    print(f"copied_file_count: {payload.get('copied_file_count')}")
    print(f"skipped_existing_count: {payload.get('skipped_existing_count')}")
    print(f"duplicate_destination_count: {payload.get('duplicate_destination_count')}")
    domain_counts = payload.get("domain_counts")
    if isinstance(domain_counts, dict) and domain_counts:
        print("domain_counts:")
        for key in sorted(domain_counts):
            print(f"  {key}: {domain_counts[key]}")
    print(f"output: {payload.get('output_path') or args.output}")
    print("research-only / local data only / no network / no broker")
    if not payload.get("apply"):
        print("No canonical files were written. Add --apply true only after reviewing the plan.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
