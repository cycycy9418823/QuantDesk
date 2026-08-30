from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from quantdesk.multi_strategy_account_book import (  # noqa: E402
    build_multi_strategy_account_book,
    load_multi_strategy_config,
    write_multi_strategy_account_book_outputs,
)

from quantdesk.cli_summary import build_cli_summary, format_human_summary  # noqa: E402
from quantdesk.shared import truthy  # noqa: E402


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build a research-only multi-strategy virtual account book.")
    parser.add_argument("--config", default="", help="Optional YAML config path. Defaults to config/multi_strategy_account_book.yaml or config/default.yaml.")
    parser.add_argument("--initial-cash", type=float, default=None)
    parser.add_argument("--portfolio-name", default="")
    parser.add_argument("--summary", nargs="?", const="true", default="false")
    parser.add_argument("--json", default="false")
    parser.add_argument("--output-dir", default="results")
    parser.add_argument("--report-dir", default="reports")
    return parser.parse_args(argv)


def run(args: argparse.Namespace) -> dict[str, Any]:
    config, load_warnings = load_multi_strategy_config(args.config or None)
    if args.initial_cash is not None:
        config["initial_cash"] = args.initial_cash
    if args.portfolio_name:
        config["portfolio_name"] = args.portfolio_name
    payload = build_multi_strategy_account_book(config)
    payload["warnings"] = list(dict.fromkeys([str(item) for item in list(payload.get("warnings") or []) + load_warnings if item]))
    if payload.get("warnings") and payload.get("status") == "passed":
        payload["status"] = "warning"
    payload["ok"] = payload.get("status") != "failed"
    return write_multi_strategy_account_book_outputs(payload, output_dir=args.output_dir, report_dir=args.report_dir)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    payload = run(args)
    wants_json = truthy(args.json)
    wants_summary = truthy(args.summary)
    if wants_summary:
        summary = build_cli_summary(payload, task_type="multi_strategy_account_book")
        if wants_json:
            print(json.dumps(summary, ensure_ascii=False, separators=(",", ":"), allow_nan=False))
        else:
            print(format_human_summary(summary))
    elif wants_json:
        print(json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False))
    else:
        print(format_human_summary(build_cli_summary(payload, task_type="multi_strategy_account_book")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
