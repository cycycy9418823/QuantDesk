from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from quantdesk.market_mainline_radar import (  # noqa: E402
    DEFAULT_OUTPUT_PATH,
    DEFAULT_REPORT_PATH,
    write_mainline_radar_outputs,
)


def _truthy(value: Any) -> bool:
    return str(value).strip().lower() in {"1", "true", "yes", "y", "on"}


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build local research-only market mainline radar from local evidence.")
    parser.add_argument("--project-root", default=str(PROJECT_ROOT))
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT_PATH))
    parser.add_argument("--report", default=str(DEFAULT_REPORT_PATH))
    parser.add_argument("--target-date", default="", help="Explicit research as-of date (YYYY-MM-DD).")
    parser.add_argument("--json", nargs="?", const="true", default="false")
    parser.add_argument("--summary", nargs="?", const="true", default="false")
    parser.add_argument("--network", default="false", help="Accepted for qd.py consistency; radar never fetches network data.")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    payload = write_mainline_radar_outputs(
        project_root=Path(args.project_root),
        target_date=args.target_date or None,
        output_path=args.output,
        report_path=args.report,
    )
    if _truthy(args.json):
        print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
        return 0
    print("QuantDesk market mainline radar")
    print(f"status: {payload.get('status')}")
    print(f"data_quality: {(payload.get('data_quality') or {}).get('status')}")
    print(f"mainline_analysis_readiness: {(payload.get('extended_data_readiness') or {}).get('mainline_analysis')}")
    print(f"current_mainlines: {len(payload.get('current_mainlines') or [])}")
    print(f"secondary_lines: {len(payload.get('secondary_lines') or [])}")
    print(f"potential_lines: {len(payload.get('potential_lines') or [])}")
    print(f"output: {payload.get('output_path') or args.output}")
    print(f"report: {payload.get('report_path') or args.report}")
    print("research-only / local data only / no network / no broker / not investment advice")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
