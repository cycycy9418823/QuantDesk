"""Build QuantDesk local daily feature table.

The command reads local CSV caches only.  It never downloads data, connects to
brokers, places orders, or produces investment advice.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from quantdesk.daily_feature_engine import build_daily_features
from quantdesk.shared import truthy


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Build local daily quantitative features. Default scope is the "
            "low-price learning universe included=true symbols only."
        )
    )
    parser.add_argument("--only-low-price", default="true", help="true/false. Default true.")
    parser.add_argument("--limit", type=int, default=0, help="0 means no limit.")
    parser.add_argument("--symbols", default="", help="Comma-separated symbols. Overrides low-price universe when provided.")
    parser.add_argument("--network", default="false", help="Accepted for qd.py consistency; this command never fetches data.")
    parser.add_argument("--json", nargs="?", const="true", default="true", help="Print JSON payload. Default true.")
    parser.add_argument("--output", default="data/features/daily_features_latest.csv")
    parser.add_argument("--summary-output", default="data/features/daily_features_summary.json")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    payload = build_daily_features(
        only_low_price=truthy(args.only_low_price),
        output_path=(PROJECT_ROOT / args.output).resolve() if not Path(args.output).is_absolute() else Path(args.output),
        summary_path=(PROJECT_ROOT / args.summary_output).resolve() if not Path(args.summary_output).is_absolute() else Path(args.summary_output),
        limit=max(0, int(args.limit or 0)),
        symbols=args.symbols or None,
    )
    payload.setdefault("network", False)
    payload.setdefault("warnings", [])
    if truthy(args.network):
        payload["warnings"].append("network flag ignored: build-features only reads local data/daily files.")
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0 if payload.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
