from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from quantdesk.maintenance.test_discovery_diagnostics import (  # noqa: E402
    build_test_discovery_diagnostics,
    compact_test_discovery_diagnostics_summary,
    dumps_payload,
    write_test_discovery_diagnostics_outputs,
)
from quantdesk.shared import truthy  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Diagnose unittest discovery/import hot spots without modifying tests.")
    parser.add_argument("--json", default="false")
    parser.add_argument("--summary", nargs="?", const="true", default="false")
    parser.add_argument("--sample-limit", type=int, default=12)
    parser.add_argument("--per-file-timeout-seconds", type=float, default=8.0)
    parser.add_argument("--discover-timeout-seconds", type=float, default=45.0)
    parser.add_argument("--timed-test-timeout-seconds", type=float, default=24.0)
    parser.add_argument("--no-probes", action="store_true")
    parser.add_argument("--no-timed-test-probes", action="store_true")
    parser.add_argument("--include-legacy", default="false")
    args = parser.parse_args(argv)
    payload = build_test_discovery_diagnostics(
        sample_limit=max(0, args.sample_limit),
        per_file_timeout_seconds=max(1.0, args.per_file_timeout_seconds),
        discover_timeout_seconds=max(1.0, args.discover_timeout_seconds),
        timed_test_timeout_seconds=max(1.0, args.timed_test_timeout_seconds),
        include_probes=not args.no_probes,
        include_timed_test_probes=not args.no_timed_test_probes,
        include_legacy=truthy(args.include_legacy),
    )
    payload = write_test_discovery_diagnostics_outputs(payload)
    if truthy(args.summary):
        print(dumps_payload(compact_test_discovery_diagnostics_summary(payload)))
    elif truthy(args.json):
        print(dumps_payload(payload))
    else:
        print(dumps_payload(payload, pretty=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
