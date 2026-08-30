from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from quantdesk.maintenance.v38_cleanup_audit import (  # noqa: E402
    build_v38_cleanup_audit,
    write_cleanup_safety_manifest,
    write_v38_cleanup_audit,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Audit v3.8 cleanup targets without deleting files.")
    parser.add_argument("--output", default=str(PROJECT_ROOT / "results" / "v38_cleanup_audit_latest.json"))
    parser.add_argument("--report", default=str(PROJECT_ROOT / "reports" / "v38_cleanup_audit_latest.md"))
    parser.add_argument("--manifest-output", default=str(PROJECT_ROOT / "results" / "v38_cleanup_safety_manifest_latest.json"))
    parser.add_argument("--manifest-report", default=str(PROJECT_ROOT / "reports" / "v38_cleanup_safety_manifest_latest.md"))
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    payload = build_v38_cleanup_audit()
    files = write_v38_cleanup_audit(payload, output_json=args.output, report_md=args.report)
    manifest = payload.get("cleanup_safety_manifest") or {}
    manifest_files = write_cleanup_safety_manifest(manifest, output_json=args.manifest_output, report_md=args.manifest_report)
    payload["files_written"] = [*files.values(), *manifest_files.values()]
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False))
    else:
        print(f"v3.8 cleanup audit {payload['status']}: no files deleted")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
