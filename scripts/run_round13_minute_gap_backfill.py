from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from quantdesk.intraday.historical_gap_backfill import (  # noqa: E402
    run_round13_minute_gap_backfill,
)
from quantdesk.shared import truthy  # noqa: E402


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Recover only the five authorized Round 13 minute gaps."
    )
    parser.add_argument("--network", default="false")
    parser.add_argument("--enable-network", action="store_true")
    parser.add_argument("--apply", nargs="?", const="true", default="false")
    parser.add_argument("--run-id", default="")
    parser.add_argument("--json", default="true")
    parser.add_argument("--summary", nargs="?", const="true", default="false")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    payload = run_round13_minute_gap_backfill(
        PROJECT_ROOT,
        network=bool(args.enable_network or truthy(args.network)),
        apply=truthy(args.apply),
        run_id=args.run_id or None,
    )
    if truthy(args.json):
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        summary = dict(dict(payload.get("quality_audit") or {}).get("summary") or {})
        print(
            "round13-minute-gap-backfill "
            f"status={payload.get('status')} requested={summary.get('requested', 5)} "
            f"passed={summary.get('passed', 0)} blocked={summary.get('blocked', 0)}"
        )
    return 0 if payload.get("status") in {
        "completed",
        "completed_staging_only",
        "planned_offline",
    } else 4


if __name__ == "__main__":
    raise SystemExit(main())
