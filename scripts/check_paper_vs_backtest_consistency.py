from __future__ import annotations

import argparse
import json
import locale
import sys
from pathlib import Path
from typing import Any


def _configure_console_encoding() -> None:
    encoding = locale.getpreferredencoding(False) or "utf-8"
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            reconfigure(encoding=encoding, errors="replace")


_configure_console_encoding()

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from quantdesk.paper.paper_vs_backtest_consistency import (  # noqa: E402
    run_paper_vs_backtest_consistency_check,
    summarize_consistency_result,
)
from quantdesk.shared import truthy  # noqa: E402


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Check paper simulation versus backtest consistency.")
    parser.add_argument("--symbol", default="", help="A-share symbol, for example 600744.")
    parser.add_argument("--target-date", default="", help="Target date in YYYY-MM-DD.")
    parser.add_argument("--network", default="false", help="Defaults to false. This checker is local-only.")
    parser.add_argument("--input-dir", default="results")
    parser.add_argument("--output-dir", default="results")
    parser.add_argument("--report-dir", default="reports")
    parser.add_argument("--strict", default="true")
    parser.add_argument("--json", default="false")
    parser.add_argument("--summary", nargs="?", const="true", default="false")
    return parser.parse_args(argv)


def run(args: argparse.Namespace) -> dict[str, Any]:
    return run_paper_vs_backtest_consistency_check(
        symbol=args.symbol,
        target_date=args.target_date,
        input_dir=args.input_dir,
        output_dir=args.output_dir,
        report_dir=args.report_dir,
        network=truthy(args.network),
        strict=truthy(args.strict),
        write=True,
    )


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    payload = run(args)
    if truthy(args.json) or truthy(args.summary):
        print(json.dumps(payload, ensure_ascii=False, separators=(",", ":"), allow_nan=False))
        return 0 if payload.get("ok") else 1
    print("QuantDesk 纸面模拟与回测一致性检查")
    print(f"symbol: {payload.get('symbol') or '-'}")
    print(f"target_date: {payload.get('target_date') or '-'}")
    print(f"network: {payload.get('network')} (v3.2.5 consistency-check 不主动联网)")
    print(f"strict: {payload.get('strict')}")
    print(f"status: {payload.get('status')}")
    print(f"issue_count: {payload.get('issue_count')}")
    print(f"warning_count: {payload.get('warning_count')}")
    for item in (payload.get("issues") or [])[:8]:
        print(f"issue: {item.get('type')} - {item.get('message')}")
    for item in (payload.get("warnings") or [])[:8]:
        print(f"warning: {item.get('type')} - {item.get('message')}")
    print("json: results/paper_vs_backtest_consistency_latest.json")
    print("report: reports/paper_vs_backtest_consistency_report_latest.md")
    print(summarize_consistency_result(payload))
    print("仅用于本地学习研究和纸面模拟诊断，不接券商、不真实下单、不构成投资建议。")
    return 0 if payload.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())

