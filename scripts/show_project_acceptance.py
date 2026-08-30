"""Show the finite, read-only QuantDesk project acceptance state."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from quantdesk.project_acceptance import build_project_acceptance
from quantdesk.shared import truthy


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-root", default=str(PROJECT_ROOT))
    parser.add_argument("--as-of", default=date.today().isoformat())
    parser.add_argument("--json", default="false")
    parser.add_argument("--summary", default="true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    payload = build_project_acceptance(
        Path(args.project_root),
        as_of=str(args.as_of),
    )
    if truthy(args.json):
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    elif truthy(args.summary):
        dispositions = payload["dispositions"]
        print(f"project-acceptance: status={payload['status']} ok={payload['ok']}")
        for name, item in dispositions.items():
            print(f"  {name}={item.get('status')}")
        print("  Research Only / Paper Account Only / No Broker Route.")
    # This is a diagnostic report, not a gate that hides the independent
    # dispositions behind a process exit code.
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
