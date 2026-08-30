from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from quantdesk.intraday.intraday_providers import DEFAULT_INTRADAY_PROVIDER_ORDER
from quantdesk.intraday.intraday_update_engine import update_intraday_1m_for_symbols
from quantdesk.shared import normalize_symbol


def parse_bool(value: str | bool) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "y", "on"}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Update scoped local 1-minute intraday data via provider fallback. User-triggered only.")
    parser.add_argument("--source", choices=["current", "plans", "positions", "candidates", "symbols"], default="current")
    parser.add_argument("--symbols", default="")
    parser.add_argument("--target-date", default="")
    parser.add_argument("--providers", default=",".join(DEFAULT_INTRADAY_PROVIDER_ORDER))
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--network", default="false", help="true/false; default false never calls external providers")
    parser.add_argument("--enable-network", action="store_true", help="Alias for --network true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--overwrite", action="store_true")
    parser.add_argument(
        "--append",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Append fetched provider rows to the local intraday CSV by default; use --no-append for an explicit replace-style update.",
    )
    parser.add_argument("--execute-after-update", action="store_true")
    parser.add_argument("--update-portfolio-after-execution", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    symbols = [normalize_symbol(item) for item in str(args.symbols or "").split(",") if item.strip()]
    providers = [item.strip() for item in str(args.providers or "").split(",") if item.strip()]
    network = parse_bool(args.network) or bool(args.enable_network)
    payload = update_intraday_1m_for_symbols(
        symbols=symbols,
        target_date=args.target_date,
        provider_order=providers,
        source="symbols" if symbols else args.source,
        overwrite=bool(args.overwrite),
        append=bool(args.append),
        dry_run=bool(args.dry_run),
        max_symbols=int(args.limit or 0),
        network=network,
        execute_after_update=bool(args.execute_after_update),
        update_portfolio_after_execution=bool(args.update_portfolio_after_execution),
    )
    print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
    return 0 if payload.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())

