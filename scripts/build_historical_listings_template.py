from __future__ import annotations

import argparse
import csv
from pathlib import Path

COLUMNS = [
    "symbol",
    "name",
    "list_date",
    "delist_date",
    "st_start_date",
    "st_end_date",
    "suspension_start_date",
    "suspension_end_date",
    "suspension_periods",
    "market",
    "board",
    "source",
    "updated_at",
]


def main() -> int:
    parser = argparse.ArgumentParser(description="Build a historical listings CSV template. No network access.")
    parser.add_argument("--output", default="docs/examples/historical_listings.example.csv")
    args = parser.parse_args()
    path = Path(args.output)
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = [
        {
            "symbol": "600000",
            "name": "Example Listed",
            "list_date": "2000-01-01",
            "delist_date": "",
            "st_start_date": "",
            "st_end_date": "",
            "suspension_start_date": "",
            "suspension_end_date": "",
            "suspension_periods": "",
            "market": "SH",
            "board": "main",
            "source": "example_template",
            "updated_at": "2026-05-26",
        }
    ]
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=COLUMNS)
        writer.writeheader()
        writer.writerows(rows)
    print(str(path).replace("\\", "/"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
