"""Run QuantDesk unified daily review workflow.

This command is local paper simulation only. It does not connect to brokers,
does not place real orders, and does not provide investment advice.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from quantdesk.cli_summary import build_cli_summary, format_human_summary
from quantdesk.daily_output_summary import format_daily_console_summary
from quantdesk.daily_workflow_orchestrator import run_daily_workflow
from quantdesk.shared import truthy


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run the local unified daily review workflow.")
    parser.add_argument("--strategy", choices=["all", "long", "board"], default="all")
    parser.add_argument("--allow-auto-init", action=argparse.BooleanOptionalAction, default=False)
    # Round 21: intraday swing is a third fill path; default off. Enable explicitly
    # with --enable-intraday; ledger actions then carry fill_model=daily_approx.
    parser.add_argument("--enable-intraday", action=argparse.BooleanOptionalAction, default=False)
    parser.add_argument("--allow-daily-approx", action=argparse.BooleanOptionalAction, default=False)
    parser.add_argument("--max-long-total", type=int, default=20)
    parser.add_argument("--max-board-total", type=int, default=10)
    parser.add_argument("--allow-theme-map-online-update", action=argparse.BooleanOptionalAction, default=False)
    parser.add_argument("--allow-daily-price-online-update", action=argparse.BooleanOptionalAction, default=False)
    parser.add_argument("--network", default="false", help="Accepted for qd.py consistency. Defaults to false; does not enable provider calls by itself.")
    parser.add_argument("--with-guardrails", default="true", help="Run quick governance guardrails and append warnings. Default true.")
    parser.add_argument("--guardrails-mode", choices=["quick"], default="quick")
    parser.add_argument("--run-strategy-lab", action=argparse.BooleanOptionalAction, default=False, help="Run the v420 strategy lab pipeline as an extra orchestrator step. Default false.")
    parser.add_argument("--json", default="false", help="Print full JSON payload to stdout. Default false.")
    parser.add_argument("--summary", nargs="?", const="true", default="false", help="Print compact operator summary while still writing full latest JSON.")
    parser.add_argument("--verbose", default="false", help="Print full JSON payload to stdout. Default false.")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    payload = run_daily_workflow(
        strategy=args.strategy,
        allow_auto_init=args.allow_auto_init,
        enable_intraday=args.enable_intraday,
        allow_daily_approx=args.allow_daily_approx,
        max_long_total=args.max_long_total,
        max_board_total=args.max_board_total,
        allow_theme_map_online_update=args.allow_theme_map_online_update,
        allow_daily_price_online_update=args.allow_daily_price_online_update,
        with_guardrails=truthy(args.with_guardrails),
        guardrails_mode=args.guardrails_mode,
        run_strategy_lab=args.run_strategy_lab,
    )
    if truthy(args.summary):
        summary = build_cli_summary(payload, task_type="daily_workflow")
        if truthy(args.json):
            print(json.dumps(summary, ensure_ascii=True, separators=(",", ":"), default=str))
        else:
            print(format_human_summary(summary))
    elif truthy(args.json) or truthy(args.verbose):
        print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
    else:
        print(format_daily_console_summary(payload))
    return 0 if payload.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
