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
from quantdesk.research.risk_attribution import (  # noqa: E402
    build_risk_attribution_report,
    write_risk_attribution_outputs,
)
from quantdesk.shared import truthy  # noqa: E402


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run true research-only Brinson and factor attribution.")
    parser.add_argument("--portfolio-returns", default="")
    parser.add_argument("--portfolio-weights", default="")
    parser.add_argument("--benchmark-weights", default="")
    parser.add_argument("--industry-map", default="")
    parser.add_argument("--factor-exposures", default="")
    parser.add_argument("--factor-returns", default="")
    parser.add_argument("--start-date", default="")
    parser.add_argument("--end-date", default="")
    parser.add_argument("--synthetic-test-data", nargs="?", const="true", default="false")
    parser.add_argument("--summary", nargs="?", const="true", default="false")
    parser.add_argument("--json", default="false")
    parser.add_argument("--output-dir", default="results")
    parser.add_argument("--report-dir", default="reports")
    return parser.parse_args(argv)


def _config_from_args(args: argparse.Namespace) -> dict[str, Any]:
    return {
        "portfolio_returns": args.portfolio_returns or args.portfolio_weights,
        "portfolio_weights": args.portfolio_weights,
        "benchmark_weights": args.benchmark_weights,
        "industry_map": args.industry_map,
        "factor_exposures": args.factor_exposures,
        "factor_returns": args.factor_returns,
        "start_date": args.start_date,
        "end_date": args.end_date,
        "synthetic_test_data": truthy(args.synthetic_test_data),
    }


def run(args: argparse.Namespace) -> dict[str, Any]:
    payload = build_risk_attribution_report(_config_from_args(args))
    return write_risk_attribution_outputs(payload, output_dir=args.output_dir, report_dir=args.report_dir)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    payload = run(args)
    wants_json = truthy(args.json)
    wants_summary = truthy(args.summary)
    if wants_summary:
        summary = build_cli_summary(payload, task_type="risk_attribution")
        if wants_json:
            print(json.dumps(summary, ensure_ascii=False, separators=(",", ":"), allow_nan=False))
        else:
            print(format_human_summary(summary))
    elif wants_json:
        print(json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False))
    else:
        print(format_human_summary(build_cli_summary(payload, task_type="risk_attribution")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
