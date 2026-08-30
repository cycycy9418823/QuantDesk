from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from quantdesk.tushare_minute_quality import (  # noqa: E402
    DEFAULT_OUTPUT_PATH,
    DEFAULT_REPORT_PATH,
    write_tushare_minute_quality_outputs,
)


def _truthy(value: Any) -> bool:
    return str(value).strip().lower() in {"1", "true", "yes", "y", "on"}


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Read-only quality audit for a completed local Tushare 1-minute job.")
    parser.add_argument("--project-root", default=str(PROJECT_ROOT))
    parser.add_argument("--job-dir", default="", help="Optional completed minute job dir; defaults to latest completed job.")
    parser.add_argument("--canonical-dir", default="", help="Optional canonical 1m dir; defaults to data/intraday/1m.")
    parser.add_argument("--target-date", default="", help="Optional YYYY-MM-DD/YYYYMMDD; defaults to job target_end.")
    parser.add_argument("--max-items", type=int, default=30)
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT_PATH))
    parser.add_argument("--report", default=str(DEFAULT_REPORT_PATH))
    parser.add_argument("--json", nargs="?", const="true", default="false")
    parser.add_argument("--summary", nargs="?", const="true", default="false")
    parser.add_argument("--network", default="false", help="Accepted for qd.py consistency; this command never fetches network data.")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    payload = write_tushare_minute_quality_outputs(
        project_root=args.project_root,
        job_dir=args.job_dir or None,
        canonical_dir=args.canonical_dir or None,
        target_date=args.target_date,
        max_items=max(0, int(args.max_items)),
        output_path=args.output,
        report_path=args.report,
    )
    if _truthy(args.json):
        print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
        return 0

    job = payload.get("job") if isinstance(payload.get("job"), dict) else {}
    quality = payload.get("quality_summary") if isinstance(payload.get("quality_summary"), dict) else {}
    download = payload.get("download_status_counts") if isinstance(payload.get("download_status_counts"), dict) else {}
    print("QuantDesk Tushare minute quality audit")
    print(f"status: {payload.get('status')}")
    print(f"job: {job.get('path') or '-'}")
    print(f"summary_status: {job.get('summary_status') or '-'}")
    print(f"target_date: {payload.get('target_date') or '-'}")
    print(f"total_symbols: {payload.get('total_symbols') or 0}")
    print(
        "quality_summary: "
        f"usable={quality.get('usable', 0)}, partial={quality.get('partial', 0)}, "
        f"insufficient={quality.get('insufficient', 0)}, invalid={quality.get('invalid', 0)}, "
        f"missing={quality.get('missing', 0)}"
    )
    if download:
        print("download_status_counts: " + ", ".join(f"{key}={download[key]}" for key in sorted(download)))
    print(f"suggested_next_action: {payload.get('suggested_next_action') or '-'}")
    print(f"output: {payload.get('output_path')}")
    print(f"report: {payload.get('report_path')}")
    print("read-only / no token output / no network fetch / no canonical data write / no broker")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
