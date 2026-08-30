from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from quantdesk.maintenance.pytest_suite_runner import (  # noqa: E402
    SUITE_ALIASES,
    VALID_SUITES,
    build_pytest_suite_runner_payload,
    compact_pytest_suite_runner_summary,
    dumps_payload,
    write_pytest_suite_runner_outputs,
)
from quantdesk.shared import truthy  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    raw_argv = list(argv) if argv is not None else sys.argv[1:]
    parser = argparse.ArgumentParser(
        description="Run bounded pytest suites split by core/api/research/v3-v4 governance/slow surfaces."
    )
    parser.add_argument(
        "suite_arg",
        nargs="?",
        choices=sorted(VALID_SUITES | set(SUITE_ALIASES)),
        help="Optional suite shorthand, for example: core, api, research, v3-v4, slow, all.",
    )
    parser.add_argument("--json", default="false")
    parser.add_argument("--summary", nargs="?", const="true", default="false")
    parser.add_argument("--suite", default="core", choices=sorted(VALID_SUITES | set(SUITE_ALIASES)))
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--timeout-seconds", type=int, default=180)
    parser.add_argument("--maxfail", type=int, default=10)
    parser.add_argument("--dry-run", default="true")
    parser.add_argument("--stop-on-failure", default="false")
    args = parser.parse_args(raw_argv)

    suite = args.suite
    has_explicit_suite = any(item == "--suite" or item.startswith("--suite=") for item in raw_argv)
    if args.suite_arg and not has_explicit_suite:
        suite = args.suite_arg

    payload = build_pytest_suite_runner_payload(
        suite=suite,
        limit=max(0, args.limit),
        timeout_seconds=max(1, args.timeout_seconds),
        maxfail=max(1, args.maxfail),
        dry_run=truthy(args.dry_run),
        stop_on_failure=truthy(args.stop_on_failure),
    )
    payload = write_pytest_suite_runner_outputs(payload)

    if truthy(args.summary):
        print(dumps_payload(compact_pytest_suite_runner_summary(payload)))
    elif truthy(args.json):
        print(dumps_payload(payload))
    else:
        print(dumps_payload(payload, pretty=True))
    return 0 if payload.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
