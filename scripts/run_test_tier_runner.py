from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from quantdesk.maintenance.test_tier_runner import (  # noqa: E402
    TIER_ALIASES,
    VALID_TIERS,
    build_test_tier_runner_payload,
    compact_test_tier_runner_summary,
    dumps_payload,
    write_test_tier_runner_outputs,
)
from quantdesk.shared import truthy  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    raw_argv = list(argv) if argv is not None else sys.argv[1:]
    parser = argparse.ArgumentParser(description="Run bounded unittest tier selections from the test inventory.")
    parser.add_argument(
        "tier_arg",
        nargs="?",
        choices=sorted(VALID_TIERS | set(TIER_ALIASES)),
        help="Optional tier shorthand, for example: tier0, tier1, tier2, tier3, legacy.",
    )
    parser.add_argument("--json", default="false")
    parser.add_argument("--summary", nargs="?", const="true", default="false")
    parser.add_argument("--tier", default="tier0_fast_unit", choices=sorted(VALID_TIERS | set(TIER_ALIASES)))
    parser.add_argument("--category", default="")
    parser.add_argument("--limit", type=int, default=40)
    parser.add_argument("--timeout-seconds", type=int, default=180)
    parser.add_argument("--dry-run", default="true")
    parser.add_argument("--include-tier3", default="false")
    parser.add_argument("--include-legacy", default="false")
    args = parser.parse_args(raw_argv)
    tier = args.tier
    has_explicit_tier = any(item == "--tier" or item.startswith("--tier=") for item in raw_argv)
    if args.tier_arg and not has_explicit_tier:
        tier = args.tier_arg
    payload = build_test_tier_runner_payload(
        tier=tier,
        category=args.category,
        limit=max(0, args.limit),
        timeout_seconds=max(1, args.timeout_seconds),
        dry_run=truthy(args.dry_run),
        include_tier3=truthy(args.include_tier3),
        include_legacy=truthy(args.include_legacy),
    )
    payload = write_test_tier_runner_outputs(payload)
    if truthy(args.summary):
        print(dumps_payload(compact_test_tier_runner_summary(payload)))
    elif truthy(args.json):
        print(dumps_payload(payload))
    else:
        print(dumps_payload(payload, pretty=True))
    return 0 if payload.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
