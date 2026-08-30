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
from quantdesk.research.risk_attribution_lite import (  # noqa: E402
    build_risk_attribution_report,
    mark_observation_lite_payload,
    write_risk_attribution_outputs,
)
from quantdesk.shared import truthy  # noqa: E402


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run research-only Risk Attribution Lite diagnostics.")
    parser.add_argument("--multi-strategy-book-json", default="")
    parser.add_argument("--factor-health-json", default="")
    parser.add_argument("--combined-score-json", default="")
    parser.add_argument("--combined-score-backtest-json", default="")
    parser.add_argument("--sector-rotation-json", default="")
    parser.add_argument("--research-dashboard-json", default="")
    parser.add_argument("--pit-universe-json", default="")
    parser.add_argument("--intraday-live-json", default="")
    parser.add_argument("--summary", nargs="?", const="true", default="false")
    parser.add_argument("--json", default="false")
    parser.add_argument("--output-dir", default="results")
    parser.add_argument("--report-dir", default="reports")
    return parser.parse_args(argv)


def _config_from_args(args: argparse.Namespace) -> dict[str, Any]:
    mapping = {
        "multi_strategy_book_json": args.multi_strategy_book_json,
        "factor_health_json": args.factor_health_json,
        "combined_score_json": args.combined_score_json,
        "combined_score_backtest_json": args.combined_score_backtest_json,
        "sector_rotation_json": args.sector_rotation_json,
        "research_dashboard_json": args.research_dashboard_json,
        "pit_universe_json": args.pit_universe_json,
        "intraday_live_json": args.intraday_live_json,
    }
    return {key: value for key, value in mapping.items() if value}


def run(args: argparse.Namespace) -> dict[str, Any]:
    payload = build_risk_attribution_report(_config_from_args(args))
    payload = mark_observation_lite_payload(payload, compatibility_alias=True)
    payload["warnings"] = list(dict.fromkeys([
        *payload.get("warnings", []),
        "qd_risk_attribution_lite_cli_preserves_observation_boundary",
    ]))
    return write_risk_attribution_outputs(payload, output_dir=args.output_dir, report_dir=args.report_dir)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    payload = run(args)
    wants_json = truthy(args.json)
    wants_summary = truthy(args.summary)
    if wants_summary:
        summary = build_cli_summary(payload, task_type="risk_attribution_lite")
        if wants_json:
            print(json.dumps(summary, ensure_ascii=False, separators=(",", ":"), allow_nan=False))
        else:
            print(format_human_summary(summary))
    elif wants_json:
        print(json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False))
    else:
        print(format_human_summary(build_cli_summary(payload, task_type="risk_attribution_lite")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

