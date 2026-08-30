"""CLI to generate/update data/universe/stock_theme_map.csv.

Does NOT connect to brokers, does NOT place orders, does NOT constitute
investment advice.  AKShare network access only happens when --source is
akshare or auto, and only when the user explicitly runs this script.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from quantdesk.theme_map_updater import update_theme_map


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Update stock_theme_map.csv. Does NOT connect to brokers or place orders."
    )
    parser.add_argument("--source", choices=["local", "akshare", "auto", "import"], default="auto",
                        help="Data source: local (no network), akshare/auto (network when user triggered), import (local CSV).")
    parser.add_argument("--input", default="data/universe/stock_theme_map_import.csv",
                        help="CSV path used when --source import.")
    parser.add_argument("--overwrite-manual", action="store_true", default=False,
                        help="Overwrite source=manual entries.")
    parser.add_argument("--only-low-price", type=str, default="true",
                        help="Only process low_price_learning_symbols included=true.")
    parser.add_argument("--output", default="data/universe/stock_theme_map.csv")
    parser.add_argument("--pending-output", default="data/universe/stock_theme_map_pending.csv")
    parser.add_argument("--limit", type=int, default=0, help="Limit symbols (0=all).")
    args = parser.parse_args(argv)

    only_lp = args.only_low_price.lower() in ("true", "1", "yes")
    output_path = Path(args.output) if Path(args.output).is_absolute() else PROJECT_ROOT / args.output
    pending_path = Path(args.pending_output) if Path(args.pending_output).is_absolute() else PROJECT_ROOT / args.pending_output
    input_path = Path(args.input) if Path(args.input).is_absolute() else PROJECT_ROOT / args.input

    result = update_theme_map(
        source=args.source,
        overwrite_manual=args.overwrite_manual,
        only_low_price=only_lp,
        output_path=output_path,
        pending_path=pending_path,
        input_path=input_path,
        limit=args.limit,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
