from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from quantdesk.remediation_advisor import (  # noqa: E402
    build_remediation_plan,
    format_remediation_console,
    write_fix_plan_latest,
)


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Show QuantDesk local fix plan without running tasks.")
    parser.add_argument("--output", default="results/fix_plan_latest.json")
    parser.add_argument("--max-actions", type=int, default=5)
    parser.add_argument("--json", nargs="?", const="true", default="false")
    parser.add_argument("--summary", nargs="?", const="true", default="false", help="Accepted for qd.py consistency; fix-plan output is already compact.")
    parser.add_argument("--network", default="false", help="Accepted for qd.py consistency; fix-plan is read-only.")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    plan = build_remediation_plan(limit=max(1, int(args.max_actions or 5)))
    write_fix_plan_latest(plan, PROJECT_ROOT / args.output)
    if str(args.json).lower() in {"1", "true", "yes", "y", "on"}:
        print(json.dumps(plan, ensure_ascii=False, indent=2, default=str))
    else:
        print(format_remediation_console(plan, max_actions=max(1, int(args.max_actions or 5))))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
