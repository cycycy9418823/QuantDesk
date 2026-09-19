"""Read-only daily integrity check for canonical daily price CSVs.

Reports NUL-only inserted records, column-width mismatches, duplicate or
non-canonical dates, symbol/header mismatches, plus optional universe
coverage and stale-before-date gaps. Writes nothing unless --output is given.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import uuid
from pathlib import Path
from typing import Sequence

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from quantdesk.daily_integrity_check import (  # noqa: E402
    load_universe_symbols,
    run_daily_integrity_check,
)
from quantdesk.shared import truthy  # noqa: E402


def _atomic_write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    data = (json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False) + "\n").encode("utf-8")
    try:
        with temporary.open("xb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--daily-dir", default=str(PROJECT_ROOT / "data" / "daily"), type=Path)
    parser.add_argument("--universe", type=Path, help="Optional universe CSV for coverage checks")
    parser.add_argument("--stale-before", default="", help="YYYY-MM-DD; flag files whose last date is earlier")
    parser.add_argument("--output", type=Path, help="Optional JSON report path; nothing is written without it")
    parser.add_argument("--limit", type=int, default=0, help="0 means every daily CSV")
    parser.add_argument("--sample-limit", type=int, default=50)
    parser.add_argument("--json", default="true")
    parser.add_argument("--fail-on-issue", default="true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    daily_dir = Path(args.daily_dir)
    if not daily_dir.is_dir():
        print(f"daily_dir_not_found:{daily_dir}", file=sys.stderr)
        return 2
    universe_symbols = load_universe_symbols(args.universe) if args.universe else None
    if args.universe and not universe_symbols:
        print(f"universe_unreadable_or_empty:{args.universe}", file=sys.stderr)
        return 2

    payload = run_daily_integrity_check(
        daily_dir,
        universe_symbols=universe_symbols,
        limit=max(0, int(args.limit or 0)),
        sample_limit=max(0, int(args.sample_limit or 0)),
        stale_before=str(args.stale_before or "").strip(),
    )
    if args.output:
        _atomic_write_json(Path(args.output), payload)
        payload["report_path"] = str(Path(args.output))

    if truthy(args.json):
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(
            "daily-integrity "
            f"ok={payload['ok']} scanned={payload['scanned_files']} "
            f"flagged={payload['flagged_files']} missing={payload['missing_universe_count']} "
            f"stale={payload['stale_count']} issues={payload['issue_counts'] or '-'}"
        )
    if truthy(args.fail_on_issue) and not payload["ok"]:
        return 3
    if truthy(args.fail_on_issue) and payload["stale_count"]:
        return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
