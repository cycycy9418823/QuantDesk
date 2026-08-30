"""Read-only preflight for the scheduled daily data chain."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from quantdesk.daily_data_chain import preflight_daily_data_chain
from quantdesk.shared import truthy


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-root", default=str(PROJECT_ROOT))
    parser.add_argument("--target-date", default="")
    parser.add_argument("--current-run-id", default="")
    parser.add_argument("--retry-from-latest", default="true")
    parser.add_argument("--json", default="true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    payload = preflight_daily_data_chain(
        Path(args.project_root),
        target_date=args.target_date or None,
        current_run_id=args.current_run_id or None,
        retry_from_latest=truthy(args.retry_from_latest),
    )
    if truthy(args.json):
        print(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
    else:
        print(
            "daily-data-chain-preflight "
            f"status={payload.get('status')} "
            f"target={payload.get('target_date')} "
            f"action={payload.get('action')}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
