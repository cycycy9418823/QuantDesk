from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from quantdesk.holdings.candidate_holdings_reconstruction import attempt_holdings_reconstruction, locate_holdings_logic
from quantdesk.paths import project_root
from quantdesk.shared import truthy


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Research-only candidate holdings reconstruction from verified selected-symbol snapshots. "
            "No broker, no orders, no position advice."
        )
    )
    parser.add_argument("--json", nargs="?", const="true", default="false", help="Print the result payload as JSON.")
    parser.add_argument("--summary", nargs="?", const="true", default="false", help="Print a compact status line.")
    args = parser.parse_args()

    root = project_root()
    payload = attempt_holdings_reconstruction(root, locate_holdings_logic(root))
    if truthy(args.json):
        print(json.dumps(payload, ensure_ascii=False, indent=None if truthy(args.summary) else 2))
    else:
        status = payload.get("status", "unknown")
        row_count = payload.get("row_count", 0)
        quality = payload.get("holdings_quality_status", "")
        if quality:
            print(f"holdings_reconstruction_status={status} rows={row_count} quality={quality}")
        else:
            print(f"holdings_reconstruction_status={status} rows={row_count}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
