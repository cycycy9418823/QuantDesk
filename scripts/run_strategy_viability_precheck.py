from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from quantdesk.research.strategy_viability_data_precheck import (
    build_strategy_viability_precheck,
    write_precheck_outputs,
)
from quantdesk.shared import truthy


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run Strategy Viability data precheck.")
    parser.add_argument("--data-root", default="data")
    parser.add_argument("--json", default="false")
    parser.add_argument("--summary", nargs="?", const="true", default="false")
    args = parser.parse_args(argv)
    payload = write_precheck_outputs(build_strategy_viability_precheck(vars(args)))
    if truthy(args.summary):
        summary = {
            "ok": payload["ok"],
            "status": payload["status"],
            "task_type": payload["task_type"],
            "generated_at": payload["generated_at"],
            "app_version": payload["app_version"],
            "total_csv_count": payload["total_csv_count"],
            "survivorship_bias_risk": payload["survivorship_bias_risk"],
            "nonstandard_schema_file_count": payload["nonstandard_schema_file_count"],
            "partial_daily_isolated": payload["partial_daily"]["isolated"],
            "warning_count": payload["warning_count"],
            "warnings_top": payload["warnings_top"],
            "output_paths": payload["output_paths"],
            "disclaimer": payload["disclaimer"],
        }
        print(json.dumps(summary if truthy(args.json) else summary, ensure_ascii=False, separators=(",", ":")))
    else:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
