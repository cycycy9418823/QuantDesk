from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from quantdesk.extended_data_readiness import (  # noqa: E402
    DEFAULT_OUTPUT_PATH,
    DEFAULT_REPORT_PATH,
    write_extended_data_readiness_outputs,
)


def _truthy(value: Any) -> bool:
    return str(value).strip().lower() in {"1", "true", "yes", "y", "on"}


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Show read-only extended local/staging data readiness.")
    parser.add_argument("--project-root", default=str(PROJECT_ROOT), help="Project root to inspect; mainly for tests.")
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT_PATH))
    parser.add_argument("--report", default=str(DEFAULT_REPORT_PATH))
    parser.add_argument("--json", nargs="?", const="true", default="false")
    parser.add_argument("--summary", nargs="?", const="true", default="false")
    parser.add_argument("--network", default="false", help="Accepted for qd.py consistency; this command never fetches network data.")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    payload = write_extended_data_readiness_outputs(
        project_root=args.project_root,
        output_path=args.output,
        report_path=args.report,
    )
    if _truthy(args.json):
        print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
        return 0
    capabilities = payload.get("capabilities") if isinstance(payload.get("capabilities"), dict) else {}
    print("QuantDesk extended data readiness")
    print(f"status: {payload.get('status')}")
    print(f"mainline_analysis: {capabilities.get('mainline_analysis') or 'degraded'}")
    print(f"paper_intraday_validation: {capabilities.get('paper_intraday_validation') or 'degraded'}")
    print(f"output: {payload.get('output_path')}")
    print(f"report: {payload.get('report_path')}")
    print("read-only / research-only / no broker / no network fetch")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
