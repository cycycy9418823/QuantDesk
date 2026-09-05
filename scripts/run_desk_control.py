"""CLI for qd pause / qd resume."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from quantdesk.desk_control import pause_writers, resume_writers  # noqa: E402
from quantdesk.shared import truthy  # noqa: E402


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Pause or resume QuantDesk writers.")
    parser.add_argument("action", choices=["pause", "resume"])
    parser.add_argument("--apply", default="true", help="true actually changes scheduled tasks.")
    parser.add_argument("--json", default="true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    apply = truthy(args.apply)
    if args.action == "pause":
        payload = pause_writers(PROJECT_ROOT, apply=apply)
    else:
        payload = resume_writers(PROJECT_ROOT, apply=apply)
    if truthy(args.json):
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(f"{payload['command']} apply={payload['apply']} paused={payload.get('paused_tasks') or payload.get('resumed_tasks')}")
        print(payload.get("note") or "")
    return 0 if payload.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
