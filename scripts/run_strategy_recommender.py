from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from quantdesk.cli_summary import build_cli_summary, format_human_summary  # noqa: E402
from quantdesk.research.strategy_recommender import (  # noqa: E402
    build_strategy_recommendation_report,
    write_strategy_recommendation_outputs,
)
from quantdesk.shared import truthy  # noqa: E402


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run research-only strategy recommendation.")
    parser.add_argument("--symbol", default="")
    parser.add_argument("--sector", default="")
    parser.add_argument("--top", type=int, default=20)
    parser.add_argument("--candidates-json", default="")
    parser.add_argument("--sector-rotation-json", default="")
    parser.add_argument("--risk-json", default="")
    parser.add_argument("--synthetic-test-data", nargs="?", const="true", default="false")
    parser.add_argument("--json", default="false")
    parser.add_argument("--summary", nargs="?", const="true", default="false")
    parser.add_argument("--output-dir", default="results")
    parser.add_argument("--report-dir", default="reports")
    return parser.parse_args(argv)


def _config_from_args(args: argparse.Namespace) -> dict[str, Any]:
    return {
        "symbol": args.symbol,
        "sector": args.sector,
        "top": args.top,
        "candidates_json": args.candidates_json,
        "sector_rotation_json": args.sector_rotation_json,
        "risk_json": args.risk_json,
        "synthetic_test_data": truthy(args.synthetic_test_data),
    }


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    payload = build_strategy_recommendation_report(_config_from_args(args))
    payload = write_strategy_recommendation_outputs(payload, output_dir=args.output_dir, report_dir=args.report_dir)
    wants_json = truthy(args.json)
    wants_summary = truthy(args.summary)
    if wants_summary:
        summary = build_cli_summary(payload, task_type="strategy_recommendation")
        if wants_json:
            print(json.dumps(summary, ensure_ascii=False, separators=(",", ":"), allow_nan=False))
        else:
            print(format_human_summary(summary))
    elif wants_json:
        print(json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False))
    else:
        print(format_human_summary(build_cli_summary(payload, task_type="strategy_recommendation")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

