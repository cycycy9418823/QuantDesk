from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from quantdesk.maintenance.import_alias_reconciliation import run_import_alias_reconciliation  # noqa: E402
from quantdesk.shared import truthy  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Audit deprecated root import aliases without restoring legacy modules.")
    parser.add_argument("--json", default="false")
    parser.add_argument("--summary", nargs="?", const="true", default="false")
    parser.add_argument("--scan-only", default="false")
    args = parser.parse_args(argv)
    payload = run_import_alias_reconciliation(scan_only=truthy(args.scan_only))
    if truthy(args.summary):
        summary = {
            "ok": payload.get("ok"),
            "status": payload.get("status"),
            "task_type": payload.get("task_type"),
            "shim_mechanism": payload.get("shim_mechanism"),
            "deleted_module_count": payload.get("deleted_module_count"),
            "alias_import_ok_count": payload.get("alias_import_ok_count"),
            "alias_import_fail_count": payload.get("alias_import_fail_count"),
            "from_import_ok_count": payload.get("from_import_ok_count"),
            "from_import_fail_count": payload.get("from_import_fail_count"),
            "retired_deleted_module_count": payload.get("retired_deleted_module_count"),
            "uncovered_deleted_module_count": payload.get("uncovered_deleted_module_count"),
            "failed_aliases": payload.get("failed_aliases"),
            "uncovered_deleted_modules": payload.get("uncovered_deleted_modules"),
            "retired_deleted_modules": payload.get("retired_deleted_modules"),
            "old_import_reference_count": payload.get("old_import_reference_count"),
            "rewrite_candidate_count": len(payload.get("rewrite_candidates") or []),
            "recommendation": payload.get("recommendation"),
            "next_phase_scope": payload.get("next_phase_scope"),
            "warnings": payload.get("warnings"),
            "output_paths": payload.get("output_paths"),
        }
        print(json.dumps(summary, ensure_ascii=False, separators=(",", ":"), allow_nan=False))
    elif truthy(args.json):
        print(json.dumps(payload, ensure_ascii=False, separators=(",", ":"), allow_nan=False))
    else:
        print(json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False))
    return 0 if payload.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
