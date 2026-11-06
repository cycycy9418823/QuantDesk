from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


def _configure_utf8_stdio() -> None:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except (OSError, ValueError):
                pass


_configure_utf8_stdio()

from quantdesk.research.backtest_statistical_validation import (  # noqa: E402
    DEFAULT_JSON_PATH,
    DEFAULT_REPORT_PATH,
    run_backtest_statistical_validation,
    summarize_statistical_validation,
)
from quantdesk.shared import truthy  # noqa: E402


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run QuantDesk backtest statistical validation.")
    parser.add_argument("--symbol", default="")
    parser.add_argument("--target-date", default="")
    parser.add_argument("--backtest-result", default="results/backtest/backtest_latest.json")
    parser.add_argument("--output-path", default=str(DEFAULT_JSON_PATH), help="Validation JSON destination.")
    parser.add_argument("--report-path", default=str(DEFAULT_REPORT_PATH), help="Validation Markdown destination.")
    parser.add_argument("--iterations", type=int, default=500)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--network", default="false", help="Defaults to false. This command does not fetch data.")
    parser.add_argument("--strict", default="true")
    return parser.parse_args(argv)


def run(args: argparse.Namespace) -> dict[str, Any]:
    return run_backtest_statistical_validation(
        symbol=args.symbol,
        target_date=args.target_date,
        backtest_result_path=args.backtest_result,
        iterations=args.iterations,
        seed=args.seed,
        network=truthy(args.network),
        strict=truthy(args.strict),
        write=True,
        output_path=getattr(args, "output_path", DEFAULT_JSON_PATH),
        report_path=getattr(args, "report_path", DEFAULT_REPORT_PATH),
    )


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    payload = run(args)
    summary = summarize_statistical_validation(payload)
    print("QuantDesk 回测统计验证")
    print(f"network: {payload.get('network')} (本命令不主动联网)")
    print(f"statistical validation status: {payload.get('status')}")
    print(payload.get("interpretation") or "")
    print(f"sample_size: {payload.get('sample_size')}")
    print(f"p_value: {summary.get('p_value')}")
    print(f"confidence interval: {summary.get('bootstrap_ci_summary')}")
    for warning in payload.get("warnings") or []:
        print(f"warning: {warning}")
    print(f"json path: {args.output_path}")
    print(f"report path: {args.report_path}")
    print("统计检验仅基于历史样本，不代表未来收益。")
    # "insufficient_sample" is an honest diagnostic outcome; "missing" means no
    # return series could be read from the requested file, so nothing was tested.
    return 0 if payload.get("status") in {"passed", "warning", "insufficient_sample"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
