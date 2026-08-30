from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from quantdesk.maintenance.v38_runtime_cleanup import (  # noqa: E402
    build_v38_runtime_cleanup_plan,
    write_v38_runtime_cleanup_plan,
)
from quantdesk.shared import truthy  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Plan or apply v3.8 runtime output cleanup with copy-first preservation.")
    parser.add_argument("--apply", default="false", help="true applies copy-first cleanup; default false is dry-run.")
    parser.add_argument("--preserve-root", default=str(PROJECT_ROOT / "results" / "v38_runtime_cleanup_preserved"))
    parser.add_argument("--output", default=str(PROJECT_ROOT / "results" / "v38_runtime_cleanup_plan_latest.json"))
    parser.add_argument("--report", default=str(PROJECT_ROOT / "reports" / "v38_runtime_cleanup_plan_latest.md"))
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    payload = build_v38_runtime_cleanup_plan(preserve_root=args.preserve_root, apply=truthy(args.apply))
    files = write_v38_runtime_cleanup_plan(payload, output_json=args.output, report_md=args.report)
    payload["files_written"] = list(files.values())
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False))
    else:
        print(f"v3.8 runtime cleanup {payload['status']}: files={payload['file_count']} apply={payload['apply']}")
    return 0 if payload.get("status") in {"dry_run", "applied"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
