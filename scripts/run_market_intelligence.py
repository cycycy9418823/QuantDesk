from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from quantdesk.cli_summary import build_cli_summary, format_human_summary  # noqa: E402
from quantdesk.market_intelligence_workbench import (  # noqa: E402
    format_market_intelligence_console,
    run_market_intelligence,
)
from quantdesk.operation_log import log_operation  # noqa: E402
from quantdesk.shared import normalize_symbol, now_text, truthy  # noqa: E402


def _parse_symbols(value: str) -> list[str]:
    return [normalize_symbol(item) for item in str(value or "").replace("，", ",").split(",") if normalize_symbol(item)]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run local market intelligence observation workbench.")
    parser.add_argument("--target-date", default="")
    parser.add_argument("--price-limit", type=float, default=20)
    parser.add_argument("--strict-price-limit", type=float, default=15)
    parser.add_argument(
        "--scope",
        choices=["current_analysis_pool", "low_price_included", "specified_symbols", "local_daily_cache", "full_a"],
        default="current_analysis_pool",
    )
    parser.add_argument("--symbols", default="")
    parser.add_argument("--network", default="false")
    parser.add_argument("--enable-network", action="store_true")
    parser.add_argument("--max-symbols", type=int, default=80)
    parser.add_argument("--include-news", default="")
    parser.add_argument("--include-scenario-curves", default="true")
    parser.add_argument("--llm", default="false")
    parser.add_argument("--output-dir", default="results/market_intelligence")
    parser.add_argument("--report-dir", default="reports")
    parser.add_argument("--force", default="false")
    parser.add_argument("--json", default="false")
    parser.add_argument("--summary", nargs="?", const="true", default="false", help="Print compact operator summary while still writing full latest JSON.")
    parser.add_argument("--verbose", default="false")
    return parser


def main(argv: list[str] | None = None) -> int:
    started_at = now_text()
    start_time = time.perf_counter()
    args = build_parser().parse_args(argv)
    network = bool(args.enable_network or truthy(args.network))
    include_news = truthy(args.include_news) if args.include_news != "" else bool(network)
    symbols = _parse_symbols(args.symbols)
    try:
        payload = run_market_intelligence(
            target_date=args.target_date,
            price_limit=args.price_limit,
            strict_price_limit=args.strict_price_limit,
            scope=args.scope,
            symbols=symbols,
            network=network,
            max_symbols=args.max_symbols,
            include_news=include_news,
            include_scenario_curves=truthy(args.include_scenario_curves),
            llm=truthy(args.llm),
            output_dir=args.output_dir,
            report_dir=args.report_dir,
            force=truthy(args.force),
        )
        if truthy(args.summary):
            summary = build_cli_summary(payload, task_type="market_intelligence")
            if truthy(args.json):
                print(json.dumps(summary, ensure_ascii=True, indent=2, default=str))
            else:
                print(format_human_summary(summary))
        elif truthy(args.json) or truthy(args.verbose):
            print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
        else:
            print(format_market_intelligence_console(payload))
        log_operation(
            command="market-intel",
            args=sys.argv[1:] if argv is None else argv,
            target_date=payload.get("target_date") or args.target_date,
            symbols=symbols,
            network=network,
            llm_enabled=truthy(args.llm),
            status=payload.get("status") or "warning",
            started_at=started_at,
            start_time=start_time,
            output_paths=[
                payload.get("output_path"),
                payload.get("report_path"),
                "results/market_intelligence/market_regime_latest.json",
                "results/market_intelligence/sector_theme_latest.json",
                "results/market_intelligence/market_news_latest.json",
                "results/market_intelligence/sector_low_price_candidates_latest.json",
                "results/market_intelligence/scenario_curves_latest.json",
            ],
            warnings=payload.get("warnings"),
            failed_reason=payload.get("failed_reason") or "",
            suggested_next_action=payload.get("suggested_next_action") or "",
        )
        return 0 if payload.get("ok") else 1
    except Exception as exc:  # noqa: BLE001
        log_operation(
            command="market-intel",
            args=sys.argv[1:] if argv is None else argv,
            target_date=args.target_date,
            symbols=symbols,
            network=network,
            llm_enabled=truthy(args.llm),
            status="failed",
            started_at=started_at,
            start_time=start_time,
            failed_reason=str(exc),
            suggested_next_action="检查 market-intel 参数和本地数据文件后重试。",
        )
        print(f"market-intel failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
