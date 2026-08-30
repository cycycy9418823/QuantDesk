from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from quantdesk.research.factor_ic_report import build_factor_ic_report, write_factor_ic_report  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build research-only factor IC report from local CSV.")
    parser.add_argument("--input", default=str(PROJECT_ROOT / "data" / "features" / "daily_features_latest.csv"))
    parser.add_argument("--forward-return-col", default="fwd_ret_1m")
    parser.add_argument("--output", default=str(PROJECT_ROOT / "results" / "factor_ic_report_latest.json"))
    parser.add_argument("--csv-output", default=str(PROJECT_ROOT / "results" / "factor_ic_report_latest.csv"))
    parser.add_argument("--report", default=str(PROJECT_ROOT / "reports" / "factor_ic_report_latest.md"))
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    payload, ic_df = build_factor_ic_report(args.input, forward_return_col=args.forward_return_col)
    files = write_factor_ic_report(payload, ic_df, output_json=args.output, output_csv=args.csv_output, report_md=args.report)
    payload["files_written"] = list(files.values())
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(f"factor IC report {payload['status']}: factors={payload.get('factor_count', 0)} periods={payload.get('ic_period_count', 0)}")
    return 0 if payload.get("status") in {"passed", "warning"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
