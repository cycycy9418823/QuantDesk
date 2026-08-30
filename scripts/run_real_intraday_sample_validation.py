from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from quantdesk.intraday.intraday_providers import DEFAULT_INTRADAY_PROVIDER_ORDER
from quantdesk.real_intraday_sample_validation import run_real_intraday_sample_validation


def parse_bool(value: str | bool) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "y", "on"}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Validate real provider 1-minute sample data through conditional execution.")
    parser.add_argument("--symbols", default="600744,002115")
    parser.add_argument("--target-date", default="")
    parser.add_argument("--providers", default=",".join(DEFAULT_INTRADAY_PROVIDER_ORDER))
    parser.add_argument("--network", default="false", help="true/false; default false performs no external requests")
    parser.add_argument("--enable-network", action="store_true", help="Alias for --network true")
    parser.add_argument("--update-portfolio", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    providers = [item.strip() for item in str(args.providers or "").split(",") if item.strip()]
    network = parse_bool(args.network) or bool(args.enable_network)
    payload = run_real_intraday_sample_validation(
        symbols=args.symbols,
        target_date=args.target_date,
        provider_order=providers,
        update_portfolio=bool(args.update_portfolio),
        network=network,
        write=True,
    )
    print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
    return 0 if payload.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
