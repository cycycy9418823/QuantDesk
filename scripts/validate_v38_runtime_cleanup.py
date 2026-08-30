from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from quantdesk.maintenance.v38_runtime_cleanup import (  # noqa: E402
    validate_v38_runtime_cleanup_plan,
    write_v38_runtime_cleanup_validation,
)
from quantdesk.shared import read_json  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Validate v3.8 runtime cleanup dry-run plan before apply.")
    parser.add_argument("--plan", default=str(PROJECT_ROOT / "results" / "v38_runtime_cleanup_plan_latest.json"))
    parser.add_argument("--output", default=str(PROJECT_ROOT / "results" / "v38_runtime_cleanup_preapply_validation_latest.json"))
    parser.add_argument("--report", default=str(PROJECT_ROOT / "reports" / "v38_runtime_cleanup_preapply_validation_latest.md"))
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    plan = read_json(args.plan, {})
    payload = validate_v38_runtime_cleanup_plan(plan if isinstance(plan, dict) else {})
    files = write_v38_runtime_cleanup_validation(payload, output_json=args.output, report_md=args.report)
    payload["files_written"] = list(files.values())
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False))
    else:
        print(f"v3.8 runtime cleanup pre-apply validation {payload['status']}: files={payload['file_count']}")
    return 0 if payload.get("status") in {"passed", "warning"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
