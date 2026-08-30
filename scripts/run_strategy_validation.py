from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from quantdesk.shared import truthy  # noqa: E402
from quantdesk.strategy_validation_suite import (  # noqa: E402
    run_strategy_validation_suite,
    summarize_validation_result,
)


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run QuantDesk strategy correctness validation.")
    parser.add_argument("--symbol", default="", help="A-share symbol, for example 600744.")
    parser.add_argument("--target-date", default="", help="Target date in YYYY-MM-DD.")
    parser.add_argument("--network", default="false", help="Defaults to false. v3.2.3 does not fetch data.")
    parser.add_argument("--input-dir", default="results", help="Directory containing existing local result JSON files.")
    parser.add_argument("--output-dir", default="results", help="Directory for strategy_validation_latest.json.")
    parser.add_argument("--strict", default="true", help="If true, severe issues return failed.")
    parser.add_argument("--format", choices=["json", "md", "both"], default="both")
    return parser.parse_args(argv)


def _rel(path: str | Path) -> str:
    value = Path(path)
    try:
        return str(value.relative_to(PROJECT_ROOT)).replace("\\", "/")
    except Exception:
        return str(value)


def run(args: argparse.Namespace) -> dict[str, Any]:
    network = truthy(args.network)
    strict = truthy(args.strict)
    payload = run_strategy_validation_suite(
        symbol=args.symbol,
        target_date=args.target_date,
        input_dir=args.input_dir,
        output_dir=args.output_dir,
        report_dir="reports",
        network=network,
        strict=strict,
        write=True,
        output_format=args.format,
    )
    payload["json_path"] = _rel(Path(args.output_dir) / "strategy_validation_latest.json")
    payload["report_path"] = _rel(PROJECT_ROOT / "reports" / "strategy_validation_report_latest.md")
    return payload


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    payload = run(args)
    summary = payload.get("summary") if isinstance(payload.get("summary"), dict) else {}
    print("QuantDesk 策略正确性验证")
    print(f"symbol: {payload.get('symbol') or '-'}")
    print(f"target_date: {payload.get('target_date') or '-'}")
    print(f"network: {payload.get('network')} (v3.2.3 不主动联网抓行情)")
    print(f"strict: {payload.get('strict')}")
    print(f"lookahead_status: {summary.get('lookahead_status')}")
    print(f"execution_assumption_status: {summary.get('execution_assumption_status')}")
    print(f"calendar_quality: {summary.get('calendar_quality')}")
    print(f"issue_count: {payload.get('issue_count')}")
    print(f"warning_count: {payload.get('warning_count')}")
    for warning in payload.get("warnings") or []:
        print(f"warning: {warning}")
    print(f"json: {payload.get('json_path')}")
    print(f"report: {payload.get('report_path')}")
    print(summarize_validation_result(payload))
    print("仅用于本地学习研究和纸面模拟诊断，不构成投资建议。")
    return 0 if payload.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
