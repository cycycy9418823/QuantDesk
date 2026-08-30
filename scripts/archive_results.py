from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from quantdesk.operation_log import log_operation  # noqa: E402
from quantdesk.result_archive import (  # noqa: E402
    archive_latest_outputs,
    build_archive_plan,
    summarize_archive_result,
    write_archive_manifest,
)
from quantdesk.shared import now_text, truthy  # noqa: E402


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Archive QuantDesk latest local results into a dated folder.")
    parser.add_argument("--target-date", required=True, help="Target date in YYYY-MM-DD.")
    parser.add_argument("--results-dir", default="results")
    parser.add_argument("--reports-dir", default="reports")
    parser.add_argument("--archive-dir", default="results/archive")
    parser.add_argument("--include-reports", default="true")
    parser.add_argument("--dry-run", default="false")
    return parser.parse_args(argv)


def run(args: argparse.Namespace) -> dict[str, Any]:
    plan = build_archive_plan(
        target_date=args.target_date,
        results_dir=args.results_dir,
        reports_dir=args.reports_dir,
        archive_dir=args.archive_dir,
        include_reports=truthy(args.include_reports),
        dry_run=truthy(args.dry_run),
    )
    payload = archive_latest_outputs(plan)
    write_archive_manifest(payload)
    return payload


def main(argv: list[str] | None = None) -> int:
    started_at = now_text()
    start_time = time.perf_counter()
    args = _parse_args(argv)
    try:
        payload = run(args)
        summary = summarize_archive_result(payload)
        print("QuantDesk 结果归档")
        print(f"target_date: {summary.get('target_date')}")
        print(f"dry_run: {summary.get('dry_run')}")
        print(f"archived_count: {summary.get('archived_count')}")
        print(f"skipped_count: {summary.get('skipped_count')}")
        print(f"archive_path: {summary.get('archive_path')}")
        print(f"manifest_path: {summary.get('manifest_path')}")
        for warning in summary.get("warnings") or []:
            print(f"warning: {warning}")
        print("仅归档本地结果和报告，不归档密钥配置，不构成投资建议。")
        log_operation(
            command="archive-results",
            args=sys.argv[1:] if argv is None else argv,
            target_date=args.target_date,
            network=False,
            llm_enabled=False,
            status="warning" if summary.get("warnings") else "passed",
            started_at=started_at,
            start_time=start_time,
            output_paths=[summary.get("archive_path"), summary.get("manifest_path")],
            warnings=summary.get("warnings"),
        )
        return 0
    except Exception as exc:  # noqa: BLE001
        log_operation(
            command="archive-results",
            args=sys.argv[1:] if argv is None else argv,
            target_date=getattr(args, "target_date", ""),
            network=False,
            llm_enabled=False,
            status="failed",
            started_at=started_at,
            start_time=start_time,
            failed_reason=str(exc),
            suggested_next_action="检查 archive-results 参数和输出目录权限后重试。",
        )
        print(f"archive-results failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

