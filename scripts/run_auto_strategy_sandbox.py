"""Run the local automatic strategy sandbox.

This CLI reads the low-price learning universe and local daily bars, creates
long / board paper-simulation candidates, writes local pending orders, and
generates review artifacts. It does not connect to brokers or place real
orders and it does not constitute investment advice.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from quantdesk.auto_strategy_sandbox import run_auto_strategy_cycle
from quantdesk.shared import truthy


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Local auto strategy sandbox. Does NOT connect to brokers, "
            "does NOT place real orders, and is not investment advice."
        )
    )
    parser.add_argument("--mode", choices=["live", "backtest"], default="live")
    parser.add_argument("--strategy", choices=["all", "long", "board"], default="all")
    parser.add_argument("--initial-cash", type=float, default=10000)
    parser.add_argument("--max-long-positions", type=int, default=5)
    parser.add_argument("--max-board-positions", type=int, default=2)
    parser.add_argument("--max-long-total", type=int, default=20)
    parser.add_argument("--max-board-total", type=int, default=10)
    parser.add_argument("--max-long-per-theme", type=int, default=3)
    parser.add_argument("--max-board-per-theme", type=int, default=2)
    parser.add_argument("--max-position-pct", type=float, default=10)
    parser.add_argument("--sizing-mode", choices=["fixed_100", "percent_cap"], default="fixed_100")
    parser.add_argument("--lookback-days", type=int, default=120)
    parser.add_argument("--long-exit-missing-days", type=int, default=2)
    parser.add_argument("--long-stop-loss-pct", type=float, default=-8)
    parser.add_argument("--long-take-profit-pct", type=float, default=15)
    parser.add_argument("--board-max-hold-days", type=int, default=3)
    parser.add_argument("--board-stop-loss-pct", type=float, default=-5)
    parser.add_argument("--sell-on-data-blocked", default="true")
    parser.add_argument("--sell-on-very-low-liquidity", default="true")
    parser.add_argument("--dry-run", action="store_true", help="Only print candidates and plans; do not write state.")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    payload = run_auto_strategy_cycle(
        mode=args.mode,
        strategy=args.strategy,
        initial_cash=args.initial_cash,
        max_long_positions=args.max_long_positions,
        max_board_positions=args.max_board_positions,
        max_long_total=args.max_long_total,
        max_board_total=args.max_board_total,
        max_long_per_theme=args.max_long_per_theme,
        max_board_per_theme=args.max_board_per_theme,
        max_position_pct=args.max_position_pct,
        sizing_mode=args.sizing_mode,
        lookback_days=args.lookback_days,
        long_exit_missing_days=args.long_exit_missing_days,
        long_stop_loss_pct=args.long_stop_loss_pct,
        long_take_profit_pct=args.long_take_profit_pct,
        board_max_hold_days=args.board_max_hold_days,
        board_stop_loss_pct=args.board_stop_loss_pct,
        sell_on_data_blocked=truthy(args.sell_on_data_blocked),
        sell_on_very_low_liquidity=truthy(args.sell_on_very_low_liquidity),
        dry_run=args.dry_run,
    )
    print(json.dumps({
        "ok": payload.get("ok"),
        "dry_run": payload.get("dry_run"),
        "summary": payload.get("summary", {}),
        "disclaimer": payload.get("disclaimer"),
        "broker_boundary": payload.get("broker_boundary"),
    }, ensure_ascii=False, indent=2))
    return 0 if payload.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
