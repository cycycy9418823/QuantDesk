from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from quantdesk.data_audit import write_data_audit  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Audit local daily CSV quality. Research-only; no data mutation.")
    parser.add_argument("--data-root", default=str(PROJECT_ROOT / "data" / "daily"))
    parser.add_argument("--output", default=str(PROJECT_ROOT / "results" / "data_audit.json"))
    parser.add_argument("--report", default=str(PROJECT_ROOT / "reports" / "data_audit.md"))
    parser.add_argument("--calendar-path", default="")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    payload = write_data_audit(args.data_root, args.output, args.report, calendar_path=args.calendar_path or None)
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(f"data audit {payload['status']}: {payload['survivable_count']}/{payload['stock_count']} survivable")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
