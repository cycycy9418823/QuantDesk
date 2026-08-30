from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from quantdesk.status_advisor import (  # noqa: E402
    build_quantdesk_status,
    format_status_console_output,
    write_status_latest,
)


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Show concise QuantDesk local status and next actions.")
    parser.add_argument("--output", default="results/status_latest.json")
    parser.add_argument("--report", default="reports/status_report_latest.md")
    parser.add_argument("--json", nargs="?", const="true", default="false")
    parser.add_argument("--summary", nargs="?", const="true", default="false", help="Accepted for qd.py consistency; status output is already compact.")
    parser.add_argument("--network", default="false", help="Accepted for qd.py consistency; status is read-only.")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    payload = build_quantdesk_status(write=False)
    write_status_latest(payload, json_path=args.output, report_path=args.report)
    if str(args.json).lower() in {"1", "true", "yes", "y", "on"}:
        print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
    else:
        print(format_status_console_output(payload))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
