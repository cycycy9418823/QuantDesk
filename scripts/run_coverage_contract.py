from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from quantdesk.maintenance.coverage_contract import (  # noqa: E402
    DEFAULT_TEST_MODULES,
    build_coverage_contract,
    compact_coverage_contract_summary,
    dumps_payload,
    write_coverage_contract_outputs,
)
from quantdesk.shared import truthy  # noqa: E402


def _split_tests(value: str) -> list[str]:
    if not str(value or "").strip():
        return list(DEFAULT_TEST_MODULES)
    return [item.strip() for item in str(value).split(",") if item.strip()]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run bounded pytest-cov coverage contract for maintenance governance tests.")
    parser.add_argument("--json", default="false")
    parser.add_argument("--summary", nargs="?", const="true", default="false")
    parser.add_argument("--tests", default="")
    parser.add_argument("--timeout-seconds", type=int, default=180)
    parser.add_argument("--dry-run", default="false")
    parser.add_argument("--min-line-percent", type=float, default=0.0)
    parser.add_argument("--require-pytest-cov", default="false")
    args = parser.parse_args(argv)
    payload = build_coverage_contract(
        test_modules=_split_tests(args.tests),
        timeout_seconds=max(1, args.timeout_seconds),
        dry_run=truthy(args.dry_run),
        min_line_percent=max(0.0, float(args.min_line_percent)),
        require_pytest_cov=truthy(args.require_pytest_cov),
    )
    payload = write_coverage_contract_outputs(payload)
    if truthy(args.summary):
        print(dumps_payload(compact_coverage_contract_summary(payload)))
    elif truthy(args.json):
        print(dumps_payload(payload))
    else:
        print(dumps_payload(payload, pretty=True))
    return 0 if payload.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
