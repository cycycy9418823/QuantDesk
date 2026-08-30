from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from quantdesk.extended_import_verifier import (  # noqa: E402
    DEFAULT_OUTPUT_PATH,
    DEFAULT_REPORT_PATH,
    write_extended_import_verification_outputs,
)


def _truthy(value: Any) -> bool:
    return str(value).strip().lower() in {"1", "true", "yes", "y", "on"}


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Verify canonical extended-data import status without writing data.")
    parser.add_argument("--project-root", default=str(PROJECT_ROOT))
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT_PATH))
    parser.add_argument("--report", default=str(DEFAULT_REPORT_PATH))
    parser.add_argument("--json", nargs="?", const="true", default="false")
    parser.add_argument("--summary", nargs="?", const="true", default="false")
    parser.add_argument("--network", default="false", help="Accepted for qd.py consistency; this command never fetches network data.")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    payload = write_extended_import_verification_outputs(
        project_root=args.project_root,
        output_path=args.output,
        report_path=args.report,
    )
    if _truthy(args.json):
        print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
        return 0

    print("QuantDesk extended import verification")
    print(f"status: {payload.get('status')}")
    print(f"post_import_gate: {payload.get('post_import_gate')}")
    print(f"canonical_presence_ratio: {payload.get('canonical_presence_ratio')}")
    print(f"canonical_present_domains: {','.join(payload.get('canonical_present_domains') or []) or '-'}")
    print(f"missing_canonical_domains: {','.join(payload.get('missing_canonical_domains') or []) or '-'}")
    print(f"staged_not_imported_domains: {','.join(payload.get('staged_not_imported_domains') or []) or '-'}")
    print(f"suggested_next_action: {payload.get('suggested_next_action')}")
    print(f"output: {payload.get('output_path')}")
    print(f"report: {payload.get('report_path')}")
    print("read-only / research-only / no network fetch / no canonical writes / no broker")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
