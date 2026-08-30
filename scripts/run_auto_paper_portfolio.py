"""Run the automatic local paper portfolio."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from quantdesk.paper.auto_paper_portfolio import run_auto_paper_portfolio


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Local paper portfolio; no broker connection and no real orders.")
    parser.add_argument("--mode", choices=["init", "update"], default="init")
    parser.add_argument("--initial-cash", type=float, default=10000)
    parser.add_argument("--scenario-id", choices=["user_10k", "acceptance_100k"], default="user_10k")
    parser.add_argument("--quantity-per-stock", type=int, default=100)
    parser.add_argument("--strategy", choices=["all", "long", "board"], default="all")
    parser.add_argument("--enable-intraday-swing", action=argparse.BooleanOptionalAction, default=False)
    parser.add_argument("--intraday-source", choices=["1m"], default="1m")
    parser.add_argument("--allow-daily-approx", action=argparse.BooleanOptionalAction, default=False)
    parser.add_argument("--build-conditional-plan", action="store_true")
    parser.add_argument("--simulate-conditional-plan", action="store_true")
    parser.add_argument("--lookback-days", type=int, default=250)
    parser.add_argument("--trade-date", default="")
    parser.add_argument("--plan-date", default="")
    parser.add_argument("--allow-same-day-rerun", action="store_true")
    parser.add_argument("--force-reset", action="store_true", help="Allow init to reset an existing automatic paper portfolio.")
    parser.add_argument("--dry-run", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    payload = run_auto_paper_portfolio(
        mode=args.mode,
        initial_cash=args.initial_cash,
        scenario_id=args.scenario_id,
        quantity_per_stock=args.quantity_per_stock,
        strategy=args.strategy,
        enable_intraday_swing=args.enable_intraday_swing,
        intraday_source=args.intraday_source,
        allow_daily_approx=args.allow_daily_approx,
        force_reset=args.force_reset,
        dry_run=args.dry_run,
        build_conditional_plan=args.build_conditional_plan,
        simulate_conditional_plan=args.simulate_conditional_plan,
        plan_date=args.plan_date,
        trade_date=args.trade_date,
        lookback_days=args.lookback_days,
        allow_same_day_rerun=args.allow_same_day_rerun,
    )
    print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
    return 0 if payload.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
