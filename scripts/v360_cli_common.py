from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Callable

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from quantdesk.cli_summary import build_cli_summary, format_human_summary  # noqa: E402
from quantdesk.platform_common import truthy  # noqa: E402


def parse_common(argv: list[str] | None = None, description: str = "QuantDesk v3.6.x command") -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=description)
    parser.add_argument("--synthetic-test-data", nargs="?", const="true", default="false")
    parser.add_argument("--json", default="false")
    parser.add_argument("--summary", nargs="?", const="true", default="false")
    parser.add_argument("--network", default="false")
    parser.add_argument("--top", type=int, default=20)
    parser.add_argument("--sector", default="")
    parser.add_argument("--strategy", default="")
    parser.add_argument("--symbol", default="")
    parser.add_argument("--date", default="")
    parser.add_argument("--target-date", default="")
    parser.add_argument("--index-csv", default="")
    parser.add_argument("--top-k", type=int, default=5)
    parser.add_argument("--horizons", default="")
    parser.add_argument("--watchlist-path", default="")
    parser.add_argument("--add-to-watchlist", default="false")
    parser.add_argument("--add-from-strategy", action="store_true")
    parser.add_argument("--init", action="store_true")
    parser.add_argument("--review", action="store_true")
    parser.add_argument("--dry-run", nargs="?", const="true", default="true")
    parser.add_argument("--notify", nargs="?", const="true", default="false")
    parser.add_argument("--from-risk-alerts", default="")
    parser.add_argument("--positions-json", default="")
    parser.add_argument("--smart-stop-json", default="")
    parser.add_argument("--intraday-json", default="")
    parser.add_argument("--sector-rotation-json", default="")
    parser.add_argument("--daily-dir", default="")
    parser.add_argument("--index-dir", default="")
    parser.add_argument("--scenario", default="")
    parser.add_argument("--all-scenarios", nargs="?", const="true", default="true")
    parser.add_argument("--exposures-csv", default="")
    parser.add_argument("--factor-returns-csv", default="")
    parser.add_argument("--execution-realism", nargs="?", const="true", default="false")
    parser.add_argument("--max-participation-rate", type=float, default=0.01)
    parser.add_argument("--limit-up-fill-model", nargs="?", const="true", default="false")
    parser.add_argument("--dynamic-slippage", nargs="?", const="true", default="false")
    parser.add_argument("--method", default="")
    parser.add_argument("--path", default="")
    parser.add_argument("--add", action="store_true")
    parser.add_argument("--list", action="store_true")
    parser.add_argument("--summary-report", action="store_true")
    parser.add_argument("--thesis", default="")
    parser.add_argument("--emotion-tag", default="")
    parser.add_argument("--confidence", type=float, default=0.0)
    parser.add_argument("--adjustment", default="forward")
    parser.add_argument("--check-price-adjustment", nargs="?", const="true", default="true")
    return parser.parse_args(argv)


def config_from_args(args: argparse.Namespace) -> dict[str, Any]:
    return {
        "synthetic_test_data": truthy(args.synthetic_test_data),
        "network": truthy(args.network),
        "top": args.top,
        "sector": args.sector,
        "strategy": args.strategy,
        "symbol": args.symbol,
        "date": args.date,
        "target_date": args.target_date,
        "index_csv": args.index_csv,
        "top_k": args.top_k,
        "horizons": args.horizons,
        "watchlist_path": args.watchlist_path or None,
        "add_to_watchlist": truthy(args.add_to_watchlist),
        "add_from_strategy": bool(args.add_from_strategy),
        "init": bool(args.init),
        "review": bool(args.review),
        "dry_run": truthy(args.dry_run),
        "notify": truthy(args.notify),
        "from_risk_alerts": args.from_risk_alerts,
        "positions_json": args.positions_json,
        "smart_stop_json": args.smart_stop_json,
        "intraday_json": args.intraday_json,
        "sector_rotation_json": args.sector_rotation_json,
        "daily_dir": args.daily_dir,
        "index_dir": args.index_dir,
        "scenario": args.scenario,
        "all_scenarios": truthy(args.all_scenarios),
        "exposures_csv": args.exposures_csv,
        "factor_returns_csv": args.factor_returns_csv,
        "execution_realism": truthy(args.execution_realism),
        "max_participation_rate": args.max_participation_rate,
        "limit_up_fill_model": truthy(args.limit_up_fill_model),
        "dynamic_slippage": truthy(args.dynamic_slippage),
        "method": args.method,
        "path": args.path,
        "add": bool(args.add),
        "list": bool(args.list),
        "summary_report": bool(args.summary_report),
        "thesis": args.thesis,
        "emotion_tag": args.emotion_tag,
        "confidence": args.confidence,
        "adjustment": args.adjustment,
        "check_price_adjustment": truthy(args.check_price_adjustment),
    }


def run_builder(builder: Callable[[dict[str, Any]], dict[str, Any]], task_type: str, argv: list[str] | None = None, description: str = "QuantDesk command") -> int:
    args = parse_common(argv, description)
    payload = builder(config_from_args(args))
    wants_json = truthy(args.json)
    wants_summary = truthy(args.summary)
    if wants_summary:
        summary = build_cli_summary(payload, task_type=task_type)
        print(json.dumps(summary, ensure_ascii=False, separators=(",", ":"), allow_nan=False) if wants_json else format_human_summary(summary))
    elif wants_json:
        print(json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False))
    else:
        print(format_human_summary(build_cli_summary(payload, task_type=task_type)))
    return 0
