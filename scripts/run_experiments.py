from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from quantdesk.experiment_tracker import summarize_experiment_index  # noqa: E402

from quantdesk.cli_summary import build_cli_summary, format_human_summary  # noqa: E402
from quantdesk.shared import truthy  # noqa: E402


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="List and diff local QuantDesk research experiment logs.")
    parser.add_argument("--task-type", default="")
    parser.add_argument("--min-sharpe", type=float, default=None)
    parser.add_argument("--after", default="")
    parser.add_argument("--before", default="")
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument("--top", type=int, default=0)
    parser.add_argument("--metric", default="")
    parser.add_argument("--diff", nargs=2, metavar=("EXP_A", "EXP_B"))
    parser.add_argument("--json", nargs="?", const="true", default="false")
    parser.add_argument("--summary", nargs="?", const="true", default="false")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    limit = int(args.top or args.limit or 20)
    payload = summarize_experiment_index(
        task_type=args.task_type or None,
        min_sharpe=args.min_sharpe,
        after=args.after or None,
        before=args.before or None,
        limit=limit,
        metric=args.metric or None,
        diff_ids=list(args.diff or []),
    )
    if truthy(args.summary):
        summary = build_cli_summary(payload, task_type="experiments")
        if truthy(args.json):
            print(json.dumps(summary, ensure_ascii=True, separators=(",", ":"), allow_nan=False, default=str))
        else:
            print(format_human_summary(summary))
    elif truthy(args.json):
        print(json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False, default=str))
    else:
        print(format_human_summary(build_cli_summary(payload, task_type="experiments")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
