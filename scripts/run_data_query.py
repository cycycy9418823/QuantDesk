from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from quantdesk.data_store.query import run_safe_query
from quantdesk.version import APP_VERSION


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("kind", choices=["daily", "cross-section", "fundamentals", "sectors", "index"])
    parser.add_argument("--symbol", default="")
    parser.add_argument("--date", default="")
    parser.add_argument("--where", default="")
    parser.add_argument("--limit", type=int, default=100)
    parser.add_argument("--adjustment", default="forward")
    parser.add_argument("--db-path", default="data/quantdesk.db")
    parser.add_argument("--json", default="false")
    parser.add_argument("--summary", nargs="?", const="true", default="false")
    args = parser.parse_args(argv)
    payload = run_safe_query(args.kind, where=args.where, limit=args.limit, adjustment=args.adjustment, db_path=args.db_path, symbol=args.symbol, date=args.date)
    payload.setdefault("generated_at", datetime.now().isoformat(timespec="seconds"))
    payload.setdefault("app_version", APP_VERSION)
    payload.setdefault("warning_count", len(payload.get("warnings", [])))
    payload.setdefault("warnings_top", payload.get("warnings", [])[:10])
    payload.setdefault("output_paths", {})
    payload.setdefault("disclaimer", "SQLite is a shadow data layer. CSV remains the compatibility fallback.")
    print(json.dumps(payload, ensure_ascii=False, allow_nan=False))
    return 0 if payload.get("ok", True) else 1


if __name__ == "__main__":
    raise SystemExit(main())
