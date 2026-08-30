from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from quantdesk.tushare_minute_coverage import (  # noqa: E402
    DEFAULT_OUTPUT_PATH,
    DEFAULT_REPORT_PATH,
    write_tushare_minute_coverage_outputs,
)


def _truthy(value: Any) -> bool:
    return str(value).strip().lower() in {"1", "true", "yes", "y", "on"}


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Show read-only Tushare 1-minute target-pool coverage.")
    parser.add_argument("--project-root", default=str(PROJECT_ROOT))
    parser.add_argument("--target-pool", default="", help="Optional target-pool JSON path; defaults to latest .tmp/minute_target_pool_project_real_*.json.")
    parser.add_argument("--job-dir", default="", help="Optional minute job dir; defaults to latest data/vendor_staging/tushare_proxy_minutes_250d*.")
    parser.add_argument("--canonical-dir", default="", help="Optional canonical 1m dir; defaults to data/intraday/1m.")
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT_PATH))
    parser.add_argument("--report", default=str(DEFAULT_REPORT_PATH))
    parser.add_argument("--max-sample", type=int, default=30)
    parser.add_argument("--count-rows", nargs="?", const="true", default="false", help="Optional slow row counting for CSV files; default false.")
    parser.add_argument("--json", nargs="?", const="true", default="false")
    parser.add_argument("--summary", nargs="?", const="true", default="false")
    parser.add_argument("--network", default="false", help="Accepted for qd.py consistency; this command never fetches network data.")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    payload = write_tushare_minute_coverage_outputs(
        project_root=args.project_root,
        target_pool_path=args.target_pool or None,
        job_dir=args.job_dir or None,
        canonical_dir=args.canonical_dir or None,
        output_path=args.output,
        report_path=args.report,
        count_rows=_truthy(args.count_rows),
        max_sample=max(0, int(args.max_sample)),
    )
    if _truthy(args.json):
        print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
        return 0

    target = payload.get("target_pool") if isinstance(payload.get("target_pool"), dict) else {}
    job = payload.get("minute_job") if isinstance(payload.get("minute_job"), dict) else {}
    completed = payload.get("latest_completed_job") if isinstance(payload.get("latest_completed_job"), dict) else {}
    counts = payload.get("coverage_counts") if isinstance(payload.get("coverage_counts"), dict) else {}
    print("QuantDesk Tushare minute coverage")
    print(f"status: {payload.get('status')}")
    print(f"target_pool: {target.get('path') or '-'}")
    print(f"target_symbol_count: {target.get('symbol_count') or 0}")
    print(f"minute_job: {job.get('path') or '-'}")
    print(f"job_summary_status: {job.get('summary_status') or '-'}")
    print(f"target_end: {job.get('target_end') or '-'}")
    print(f"canonical_current: {counts.get('canonical_current', 0)}")
    print(f"canonical_file_count: {payload.get('canonical_file_count', 0)}")
    print(f"latest_completed_job: {completed.get('path') or '-'}")
    print(
        "latest_completed_job_canonical_current: "
        f"{completed.get('canonical_current_count', 0)}/{completed.get('symbol_count', 0)}"
    )
    print(f"staged_current_not_imported: {counts.get('staged_current_not_imported', 0)}")
    print(f"current_available_count_source: {payload.get('current_available_count_source') or '-'}")
    print(f"canonical_current_available_count: {payload.get('canonical_current_available_count', 0)}")
    print(f"staged_current_not_imported_count: {payload.get('staged_current_not_imported_count', 0)}")
    print(f"missing_or_stale_count: {payload.get('missing_or_stale_count', 0)}")
    print(f"current_available_ratio: {payload.get('current_available_ratio', 0.0)}")
    print(f"canonical_current_available_ratio: {payload.get('canonical_current_available_ratio', 0.0)}")
    print(f"staged_current_not_imported_ratio: {payload.get('staged_current_not_imported_ratio', 0.0)}")
    print(f"suggested_next_action: {payload.get('suggested_next_action')}")
    print(f"output: {payload.get('output_path')}")
    print(f"report: {payload.get('report_path')}")
    print("read-only / no token output / no network fetch / no canonical data write / no broker")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
