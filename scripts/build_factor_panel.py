from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from quantdesk.research.factor_panel_builder import build_factor_panel, write_factor_panel  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build local research-only factor panel with forward returns.")
    parser.add_argument("--daily-dir", default=str(PROJECT_ROOT / "data" / "daily"))
    parser.add_argument("--symbols", default="", help="Comma-separated symbols. Overrides limit_symbols when provided.")
    parser.add_argument("--limit-symbols", type=int, default=200)
    parser.add_argument("--frequency", default="M")
    parser.add_argument("--max-dates", type=int, default=36)
    parser.add_argument("--forward-days", type=int, default=20)
    parser.add_argument("--output", default=str(PROJECT_ROOT / "results" / "factor_panel_latest.csv"))
    parser.add_argument("--summary", default=str(PROJECT_ROOT / "results" / "factor_panel_latest.json"))
    parser.add_argument("--report", default=str(PROJECT_ROOT / "reports" / "factor_panel_latest.md"))
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    symbols = [item.strip() for item in args.symbols.split(",") if item.strip()] or None
    payload, panel = build_factor_panel(
        args.daily_dir,
        symbols=symbols,
        limit_symbols=args.limit_symbols,
        frequency=args.frequency,
        max_dates=args.max_dates,
        forward_days=args.forward_days,
    )
    files = write_factor_panel(payload, panel, output_csv=args.output, output_json=args.summary, report_md=args.report)
    payload["files_written"] = list(files.values())
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(f"factor panel {payload['status']}: rows={payload.get('row_count', 0)} symbols={payload.get('symbol_count', 0)}")
    return 0 if payload.get("status") in {"passed", "warning"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
