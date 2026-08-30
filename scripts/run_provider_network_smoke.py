from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from quantdesk.provider_network_smoke import print_smoke_summary, run_provider_network_smoke
from quantdesk.shared import truthy


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run scoped provider network smoke diagnostics.")
    parser.add_argument("--symbols", default="", help="Comma-separated symbols. Default samples low-price included=true pool.")
    parser.add_argument("--target-date", default="", help="Target date for daily/intraday checks.")
    parser.add_argument("--network", default="false", help="true/false. Default false never opens network connections.")
    parser.add_argument("--providers", default="tencent", help="Comma-separated providers: tencent,mootdx. Default tencent.")
    parser.add_argument("--limit", type=int, default=3, help="Low-price sample limit when --symbols is omitted.")
    parser.add_argument("--timeout", type=int, default=8)
    parser.add_argument("--json", action="store_true", help="Print JSON payload instead of Chinese summary.")
    args = parser.parse_args(argv)

    payload = run_provider_network_smoke(
        symbols=args.symbols,
        target_date=args.target_date,
        network=truthy(args.network),
        limit=args.limit,
        timeout=args.timeout,
        providers=args.providers,
    )
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print_smoke_summary(payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
