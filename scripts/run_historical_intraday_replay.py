from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from quantdesk.historical_replay_engine import run_historical_intraday_replay
from quantdesk.shared import normalize_symbol, truthy


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run historical walk-forward intraday replay. Paper simulation only; not investment advice.")
    parser.add_argument("--start-date", required=True)
    parser.add_argument("--end-date", required=True)
    parser.add_argument("--strategy", choices=["all", "long", "board"], default="all")
    parser.add_argument("--symbols", default="")
    parser.add_argument("--only-low-price", default="true")
    parser.add_argument("--initial-cash", type=float, default=100000)
    parser.add_argument("--use-intraday-provider", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--update-state", action="store_true")
    parser.add_argument("--max-days", type=int, default=0)
    parser.add_argument("--limit-symbols", type=int, default=0)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    symbols = [normalize_symbol(item) for item in str(args.symbols or "").split(",") if item.strip()]
    payload = run_historical_intraday_replay(
        args.start_date,
        args.end_date,
        strategy=args.strategy,
        symbols=symbols or None,
        only_low_price=truthy(args.only_low_price),
        initial_cash=args.initial_cash,
        use_intraday_provider=bool(args.use_intraday_provider),
        dry_run=bool(args.dry_run),
        update_state=bool(args.update_state),
        max_days=int(args.max_days or 0),
        limit_symbols=int(args.limit_symbols or 0),
    )
    print(json.dumps({
        "ok": payload.get("ok"),
        "dry_run": payload.get("dry_run"),
        "start_date": payload.get("start_date"),
        "end_date": payload.get("end_date"),
        "summary": payload.get("summary", {}),
        "report_path": payload.get("report_path", ""),
        "use_intraday_provider": payload.get("use_intraday_provider"),
        "updated_current_auto_portfolio": payload.get("updated_current_auto_portfolio"),
        "disclaimer": payload.get("disclaimer"),
        "no_fake_fill_note": payload.get("no_fake_fill_note"),
    }, ensure_ascii=False, indent=2, default=str))
    return 0 if payload.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
