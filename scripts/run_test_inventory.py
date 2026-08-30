from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from quantdesk.maintenance.test_inventory import (  # noqa: E402
    compact_test_inventory_summary,
    dumps_payload,
    write_test_inventory_outputs,
)
from quantdesk.shared import truthy  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Inventory test files by tier and maintenance risk without modifying tests.")
    parser.add_argument("--json", default="false")
    parser.add_argument("--summary", nargs="?", const="true", default="false")
    args = parser.parse_args(argv)
    payload = write_test_inventory_outputs()
    if truthy(args.summary):
        print(dumps_payload(compact_test_inventory_summary(payload)))
    elif truthy(args.json):
        print(dumps_payload(payload))
    else:
        print(dumps_payload(payload, pretty=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
