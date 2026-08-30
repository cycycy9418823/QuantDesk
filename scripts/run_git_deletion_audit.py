from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from quantdesk.maintenance.git_deletion_audit import (  # noqa: E402
    run_git_deletion_audit,
    write_git_deletion_audit_outputs,
)
from quantdesk.shared import truthy  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Audit tracked deletions and untracked files without modifying git state.")
    parser.add_argument("--json", default="false")
    parser.add_argument("--summary", nargs="?", const="true", default="false")
    args = parser.parse_args(argv)
    payload = write_git_deletion_audit_outputs(run_git_deletion_audit())
    if truthy(args.summary):
        summary = {
            "ok": payload.get("ok"),
            "status": payload.get("status"),
            "task_type": payload.get("task_type"),
            "deleted_tracked_count": payload.get("deleted_tracked_count"),
            "untracked_count": payload.get("untracked_count"),
            "modified_count": payload.get("modified_count"),
            "unknown_risk_count": payload.get("unknown_risk_count"),
            "high_risk_deleted_files": payload.get("high_risk_deleted_files"),
            "likely_legacy_removed_files": (payload.get("likely_legacy_removed_files") or [])[:20],
            "manual_review_required": payload.get("manual_review_required"),
            "recommendation": payload.get("recommendation"),
            "warnings": payload.get("warnings"),
            "output_paths": payload.get("output_paths"),
            "disclaimer": payload.get("disclaimer"),
        }
        print(json.dumps(summary, ensure_ascii=False, separators=(",", ":"), allow_nan=False))
    elif truthy(args.json):
        print(json.dumps(payload, ensure_ascii=False, separators=(",", ":"), allow_nan=False))
    else:
        print(json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False))
    return 0 if payload.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())

