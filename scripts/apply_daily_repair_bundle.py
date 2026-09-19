"""Apply a prepared daily-repair bundle with a locked compare-and-swap.

Dry-run by default. A canonical write needs BOTH --apply and
--i-confirm-no-task-running, so authorizing a review never silently
authorizes a write.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Sequence

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from quantdesk.daily_repair_apply import apply_daily_repair_bundle  # noqa: E402
from quantdesk.shared import truthy  # noqa: E402


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle", required=True, type=Path)
    parser.add_argument("--daily-dir", default=str(PROJECT_ROOT / "data" / "daily"), type=Path)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--i-confirm-no-task-running", action="store_true")
    parser.add_argument("--allow-incomplete", default="true")
    parser.add_argument("--lock-timeout-seconds", type=float, default=120.0)
    parser.add_argument("--output", type=Path, help="Optional JSON report path")
    parser.add_argument("--json", default="true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        report = apply_daily_repair_bundle(
            bundle_dir=args.bundle,
            daily_dir=args.daily_dir,
            apply=bool(args.apply),
            confirm_no_writer=bool(args.i_confirm_no_task_running),
            allow_incomplete=truthy(args.allow_incomplete),
            lock_timeout_seconds=float(args.lock_timeout_seconds),
        )
    except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
        print(f"{type(exc).__name__}: {exc}", file=sys.stderr)
        return 2

    if args.output:
        from scripts.check_daily_integrity import _atomic_write_json

        _atomic_write_json(Path(args.output), report)

    if truthy(args.json):
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(f"daily-repair-apply mode={report['mode']} total={report['total']} summary={report['summary']}")
    return 3 if report["summary"].get("refused") else 0


if __name__ == "__main__":
    raise SystemExit(main())
