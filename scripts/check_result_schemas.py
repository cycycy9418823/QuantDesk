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

from quantdesk.operation_log import log_operation  # noqa: E402
from quantdesk.result_payload_validator import (  # noqa: E402
    DEFAULT_REPORT_PATH,
    validate_results_directory,
)
from quantdesk.shared import now_text, truthy  # noqa: E402


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Validate QuantDesk result JSON schema contracts.")
    parser.add_argument("--results-dir", default="results")
    parser.add_argument("--reports-dir", default="reports")
    parser.add_argument("--strict", default="false")
    parser.add_argument("--network", default="false")
    parser.add_argument("--json", default="false")
    parser.add_argument("--summary", default="false")
    parser.add_argument("--output", default="results/schema_validation_latest.json")
    return parser.parse_args(argv)


def run(args: argparse.Namespace) -> dict[str, Any]:
    output = Path(args.output)
    if not output.is_absolute():
        output = PROJECT_ROOT / output
    report_dir = Path(args.reports_dir)
    if not report_dir.is_absolute():
        report_dir = PROJECT_ROOT / report_dir
    report_path = report_dir / DEFAULT_REPORT_PATH.name
    return validate_results_directory(
        results_dir=args.results_dir,
        reports_dir=args.reports_dir,
        strict=truthy(args.strict),
        output_path=output,
        report_path=report_path,
        write=True,
    )


def print_summary(payload: dict[str, Any]) -> None:
    print("QuantDesk schema-check 结果 payload 合约检查")
    print(f"checked_files: {payload.get('checked_files')}")
    print(f"passed_count: {payload.get('passed_count')}")
    print(f"warning_count: {payload.get('warning_count')}")
    print(f"failed_count: {payload.get('failed_count')}")
    print(f"missing_count: {payload.get('missing_count')}")
    print(f"secret_leak_count: {payload.get('secret_leak_count')}")
    print(f"高风险密钥泄露: {payload.get('confirmed_secret_leak_count', payload.get('secret_leak_count', 0))}")
    print(f"低风险敏感词引用: {payload.get('secret_like_reference_count', 0)}")
    print(f"status: {payload.get('status')}")
    print(f"json path: {payload.get('output_path') or 'results/schema_validation_latest.json'}")
    print(f"report path: {payload.get('report_path') or 'reports/schema_validation_report_latest.md'}")
    print("本命令只读本地 results/reports，不联网、不调用 LLM、不读取密钥配置。")


def main(argv: list[str] | None = None) -> int:
    started_at = now_text()
    start_time = time.perf_counter()
    args = _parse_args(argv)
    try:
        payload = run(args)
        if truthy(args.json):
            print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
        else:
            print_summary(payload)
        log_operation(
            command="schema-check",
            args=sys.argv[1:] if argv is None else argv,
            network=truthy(args.network),
            llm_enabled=False,
            status=payload.get("status") or "warning",
            started_at=started_at,
            start_time=start_time,
            output_paths=[payload.get("output_path"), payload.get("report_path")],
            warnings=payload.get("warnings"),
            failed_reason=payload.get("failed_reason") or "",
            suggested_next_action=payload.get("suggested_next_action") or "",
        )
        return 0 if payload.get("status") in {"passed", "warning", "missing"} else 1
    except Exception as exc:  # noqa: BLE001
        log_operation(
            command="schema-check",
            args=sys.argv[1:] if argv is None else argv,
            network=truthy(getattr(args, "network", "false")),
            llm_enabled=False,
            status="failed",
            started_at=started_at,
            start_time=start_time,
            failed_reason=str(exc),
            suggested_next_action="检查 schema-check 参数和输出目录权限后重试。",
        )
        print(f"schema-check failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
