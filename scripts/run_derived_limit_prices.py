"""CLI for derived limit prices (research-only complement to the official file)."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from quantdesk.data_truth.derived_limit_prices import run  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Derive A-share limit prices locally (research-only).")
    parser.add_argument("--start-date", default="2026-07-03")
    parser.add_argument("--symbols", default="")
    parser.add_argument("--json", default="true")
    args = parser.parse_args()

    symbols = [s.strip() for s in args.symbols.split(",") if s.strip().isdigit()] or None
    report = run(PROJECT_ROOT, start_date=args.start_date, symbols=symbols)
    if args.json.lower() != "false":
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print("rows:", report["rows"], "| sealed match rate:", report["sealed_validation"]["match_rate"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
