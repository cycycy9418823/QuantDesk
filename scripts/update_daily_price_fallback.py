from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from quantdesk.daily_price_update_engine import DEFAULT_PROVIDERS, processing_notice, update_daily_prices
from quantdesk.shared import truthy


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Update low-price daily bars through provider fallback.")
    parser.add_argument("--only-low-price", default="true", help="true/false. Default true; never scans all A by default.")
    parser.add_argument("--target-date", default="", help="YYYY-MM-DD. Empty means infer latest complete A-share day.")
    parser.add_argument(
        "--providers",
        default=",".join(DEFAULT_PROVIDERS),
        help="Comma-separated providers, e.g. current_project_default,akshare_daily,ashare_daily.",
    )
    parser.add_argument("--workers", type=int, default=1)
    parser.add_argument(
        "--sleep",
        type=float,
        default=1.0,
        help="Minimum seconds between network symbols; network runs are serial by default.",
    )
    parser.add_argument("--network", default="false", help="true/false. Default false; provider requests require explicit opt-in.")
    parser.add_argument("--enable-network", action="store_true", help="Alias for --network true.")
    parser.add_argument("--dry-run", action="store_true", help="Do not write data/daily and do not connect providers.")
    parser.add_argument("--limit", type=int, default=0, help="0 means no limit.")
    parser.add_argument("--symbols", default="", help="Comma-separated symbols to update, e.g. 600744,600958,002115.")
    parser.add_argument("--rebuild-features", action="store_true", help="Rebuild daily_features after successful updates.")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    providers = [item.strip() for item in str(args.providers or "").split(",") if item.strip()]
    network = (truthy(args.network) or bool(args.enable_network)) and not bool(args.dry_run)
    sleep_seconds = max(0.0, float(args.sleep or 0.0))
    if network:
        sleep_seconds = max(1.0, sleep_seconds)
    payload = update_daily_prices(
        only_low_price=truthy(args.only_low_price),
        target_date=args.target_date or None,
        providers=providers,
        workers=max(1, min(int(args.workers or 1), 8)),
        network=network,
        dry_run=bool(args.dry_run),
        limit=max(0, int(args.limit or 0)),
        rebuild_features=bool(args.rebuild_features),
        symbols=args.symbols or None,
        sleep_seconds=sleep_seconds,
    )
    notice = payload.get("process_note") or processing_notice(
        limit=max(0, int(args.limit or 0)),
        symbols=args.symbols or None,
        processed_symbols=payload.get("processed_symbols") or [],
    )
    print(notice, file=sys.stderr)
    print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
