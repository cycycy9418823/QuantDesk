from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from quantdesk.data_store.migration import migrate_csv_to_sqlite


def parse_bool(value) -> bool:
    return str(value or "false").lower() in {"1", "true", "yes", "y"}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db-path", default="data/quantdesk.db")
    parser.add_argument("--data-root", default="data")
    parser.add_argument("--adjustment", default="forward")
    parser.add_argument("--dry-run", nargs="?", const="true", default="true")
    parser.add_argument("--sample", type=int, default=0)
    parser.add_argument("--full", nargs="?", const="true", default="false")
    parser.add_argument("--compare", nargs="?", const="true", default="false")
    parser.add_argument("--batch-size", type=int, default=1000)
    parser.add_argument("--strict", nargs="?", const="true", default="false")
    parser.add_argument("--json", default="false")
    parser.add_argument("--summary", nargs="?", const="true", default="false")
    args = parser.parse_args(argv)
    cfg = vars(args)
    for key in ["dry_run", "full", "compare", "strict"]:
        cfg[key] = parse_bool(cfg[key])
    payload = migrate_csv_to_sqlite(cfg)
    print(json.dumps(payload, ensure_ascii=False, allow_nan=False))
    return 0 if payload.get("ok", True) else 1


if __name__ == "__main__":
    raise SystemExit(main())
