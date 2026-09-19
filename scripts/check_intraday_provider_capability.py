from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from quantdesk.intraday.intraday_provider_capability import check_intraday_provider_capability
from quantdesk.intraday.intraday_providers import DEFAULT_INTRADAY_PROVIDER_ORDER


def parse_bool(value: str | bool) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "y", "on"}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Check scoped 1-minute intraday provider capability.")
    parser.add_argument("--symbols", default="600744,002115")
    parser.add_argument("--dates", default="")
    parser.add_argument("--providers", default=",".join(DEFAULT_INTRADAY_PROVIDER_ORDER))
    parser.add_argument("--network", default="false", help="true/false; default false performs no external requests")
    parser.add_argument("--enable-network", action="store_true", help="Alias for --network true")
    parser.add_argument("--no-network", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    providers = [item.strip() for item in str(args.providers or "").split(",") if item.strip()]
    network = (parse_bool(args.network) or bool(args.enable_network)) and not bool(args.no_network)
    payload = check_intraday_provider_capability(
        symbols=args.symbols,
        dates=args.dates,
        provider_order=providers,
        network=network,
        write=True,
    )
    print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
    return 0 if payload.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
