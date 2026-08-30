"""Run the shared paper-capital allocation planner (research-only, paper-only).

Stage 1: turn existing research recommendations into an auditable shared
paper-capital plan over a small default budget. It only *plans* paper seats; it
does not place real orders, connect a broker, mutate paper state, or execute the
plan in this stage.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from quantdesk.paper.shared_allocation_planner import (
    DEFAULT_INITIAL_CASH,
    DEFAULT_LOT_SIZE,
    run_shared_paper_allocation_plan,
)
from quantdesk.shared import truthy


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Shared paper-capital allocation planner; no broker, no real orders, plan only."
    )
    parser.add_argument("--initial-cash", type=float, default=DEFAULT_INITIAL_CASH)
    parser.add_argument("--lot-size", type=int, default=DEFAULT_LOT_SIZE)
    parser.add_argument("--strategy", choices=["all", "long", "swing", "board"], default="all")
    parser.add_argument("--data-root", default="", help="Data root; defaults to canonical data/.")
    parser.add_argument("--write-latest", default="true", help="Whether to request canonical latest write.")
    parser.add_argument("--smoke-run", action="store_true", help="Mark this run as a smoke run (never canonical latest).")
    parser.add_argument("--no-write", action="store_true", help="Build the plan without writing any file.")
    parser.add_argument("--json", default="true", help="Print JSON payload.")
    parser.add_argument("--summary", default="false", help="Print a compact summary instead of the full payload.")
    return parser


def _summary(payload: dict) -> dict:
    return {
        "ok": payload.get("ok"),
        "task_type": payload.get("task_type"),
        "plan_status": payload.get("plan_status"),
        "as_of": payload.get("as_of"),
        "data_date": payload.get("data_date"),
        "initial_cash": payload.get("initial_cash"),
        "occupied_cash": payload.get("occupied_cash"),
        "remaining_cash": payload.get("remaining_cash"),
        "lot_size": payload.get("lot_size"),
        "lot_infeasible_count": payload.get("lot_infeasible_count"),
        "selected_count": payload.get("selected_count"),
        "blocked_or_waiting_count": payload.get("blocked_or_waiting_count"),
        "data_request_suggested_count": payload.get("data_request_suggested_count"),
        "research_recommendation_count": payload.get("research_recommendation_count"),
        "latest_eligible": payload.get("latest_eligible"),
        "latest_write_blocked_reason": payload.get("latest_write_blocked_reason"),
        "data_root_kind": payload.get("data_root_kind"),
        "output_paths": payload.get("output_paths"),
        "disclaimer": payload.get("disclaimer"),
    }


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    payload = run_shared_paper_allocation_plan(
        initial_cash=args.initial_cash,
        lot_size=args.lot_size,
        strategy=args.strategy,
        data_root=args.data_root or None,
        write_latest=truthy(args.write_latest),
        smoke_run=bool(args.smoke_run),
        write=not args.no_write,
    )
    if truthy(args.summary):
        print(json.dumps(_summary(payload), ensure_ascii=False, indent=2, default=str))
    elif truthy(args.json):
        print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
    return 0 if payload.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
