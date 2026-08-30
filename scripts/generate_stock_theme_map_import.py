"""Generate data/universe/stock_theme_map_import.csv draft.

This script prepares a local import draft so users do not need to create CSV
files by hand in PowerShell. It does not connect to brokers, does not place
orders, and does not constitute investment advice.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from quantdesk.theme_map_import_draft import generate_import_draft


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Generate stock_theme_map_import.csv draft from current candidates, holdings, pending rows, or low-price pool."
    )
    parser.add_argument("--source", choices=["current", "pending", "low-price", "all"], default="current")
    parser.add_argument("--overwrite", action="store_true", default=False)
    parser.add_argument("--limit", type=int, default=0, help="Limit symbols (0=all).")
    parser.add_argument("--output", default="data/universe/stock_theme_map_import.csv")
    args = parser.parse_args(argv)

    output = Path(args.output)
    output_path = output if output.is_absolute() else PROJECT_ROOT / output
    result = generate_import_draft(
        source=args.source,
        overwrite=args.overwrite,
        limit=max(0, int(args.limit or 0)),
        output_path=output_path,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
