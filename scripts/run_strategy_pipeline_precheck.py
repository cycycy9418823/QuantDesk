from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from quantdesk.research.strategy_pipeline_repair import (
    build_strategy_pipeline_precheck,
    write_pipeline_precheck_outputs,
)
from quantdesk.shared import truthy


def _summary(payload: dict) -> dict:
    return {
        "ok": payload.get("ok", True),
        "status": payload.get("status", "passed"),
        "task_type": payload.get("task_type"),
        "generated_at": payload.get("generated_at"),
        "app_version": payload.get("app_version"),
        "pit_mode": (payload.get("pipeline_repair_summary") or {}).get("pit_mode"),
        "survivorship_bias_risk": (payload.get("pipeline_repair_summary") or {}).get("survivorship_bias_risk"),
        "total_csv": (payload.get("schema_anomaly") or {}).get("total_csv"),
        "warning_count": payload.get("warning_count", len(payload.get("warnings") or [])),
        "warnings_top": payload.get("warnings_top", list(payload.get("warnings") or [])[:10]),
        "output_paths": payload.get("output_paths", {}),
        "disclaimer": payload.get("disclaimer"),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run v3.7.0.2 strategy pipeline repair precheck.")
    sub = parser.add_subparsers(dest="command")
    child = sub.add_parser("precheck")
    child.add_argument("--data-root", default="data")
    child.add_argument("--adjustment", default="forward")
    child.add_argument("--pit-mode", default="price_coverage", choices=["strict", "price_coverage", "off"])
    child.add_argument("--json", default="false")
    child.add_argument("--summary", nargs="?", const="true", default="false")
    args = parser.parse_args(argv)
    if not args.command:
        parser.print_help()
        return 0
    payload = write_pipeline_precheck_outputs(build_strategy_pipeline_precheck(vars(args)))
    if truthy(args.summary):
        print(json.dumps(_summary(payload), ensure_ascii=False, separators=(",", ":")))
    elif truthy(args.json):
        print(json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False))
    else:
        print(json.dumps(_summary(payload), ensure_ascii=False, indent=2, allow_nan=False))
    return 0 if payload.get("ok", True) else 1


if __name__ == "__main__":
    raise SystemExit(main())
