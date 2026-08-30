"""CLI script for running walk-forward validation.

Usage:
    python scripts/walk_forward_backtest.py --strategy ma_breakout --symbols 600000,000001
    python scripts/walk_forward_backtest.py --strategy ma_breakout --all-a-share
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from quantdesk.intraday_data_policy import assert_no_partial_daily_for_research  # noqa: E402

from quantdesk.backtest_audit import save_audit_report  # noqa: E402
from quantdesk.backtest_quality import evaluate_backtest_quality  # noqa: E402
from quantdesk.cli_summary import build_cli_summary, format_human_summary  # noqa: E402
from quantdesk.config import load_config  # noqa: E402
from quantdesk.csv_io import read_csv_frame  # noqa: E402
from quantdesk.shared import now_text, truthy, write_json  # noqa: E402
from quantdesk.trading_calendar import resolve_target_complete_trade_date  # noqa: E402
from quantdesk.version import API_SCHEMA_VERSION, APP_VERSION, PACKAGE_VERSION  # noqa: E402
from quantdesk.walk_forward import run_walk_forward  # noqa: E402

DISCLAIMER = (
    "Local walk-forward validation only. QuantDesk does not connect to brokers, "
    "place real orders, or provide investment advice."
)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run walk-forward validation for a QuantDesk strategy.")
    parser.add_argument("--strategy", default="ma_breakout", help="Strategy ID.")
    parser.add_argument("--symbols", default="", help="Comma-separated symbols.")
    parser.add_argument("--all-a-share", action="store_true", help="Use universe file.")
    parser.add_argument("--start-date", default="20150101")
    parser.add_argument("--end-date", default="")
    parser.add_argument("--train-years", type=int, default=3)
    parser.add_argument("--test-years", type=int, default=1)
    parser.add_argument("--cost-bps", type=float, default=5.0)
    parser.add_argument("--slippage-bps", type=float, default=5.0)
    parser.add_argument("--max-position-pct", type=float, default=None, help="Maximum simulated capital fraction per symbol; defaults to backtest.max_position_pct from config.")
    parser.add_argument("--min-data-grade", default="BACKTEST_READY")
    parser.add_argument("--max-symbols", type=int, default=0)
    parser.add_argument("--output-dir", default="")
    parser.add_argument("--config", default="")
    parser.add_argument("--network", default="false", help="Accepted for qd.py consistency; walk-forward remains local-only.")
    parser.add_argument("--enable-network", action="store_true")
    parser.add_argument("--no-network", action="store_true")
    parser.add_argument("--json", nargs="?", const="true", default="false")
    parser.add_argument("--summary", nargs="?", const="true", default="false", help="Print compact operator summary while still writing full latest JSON.")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    target_info = resolve_target_complete_trade_date(args.end_date or None)
    if not args.end_date:
        args.end_date = str(target_info.get("target_complete_trade_date") or target_info.get("target_date") or "").replace("-", "")
    network_requested = bool(args.enable_network or truthy(args.network)) and not bool(args.no_network)
    cli_warnings = [str(item) for item in (target_info.get("target_date_warnings") or []) if item]
    if network_requested:
        cli_warnings.append("network flag ignored: walk-forward only reads local daily data.")
    config = load_config(args.config or None)
    backtest_cfg = config.get("backtest", {}) if isinstance(config.get("backtest"), dict) else {}
    max_position_pct_raw = args.max_position_pct
    if max_position_pct_raw is None:
        max_position_pct_raw = backtest_cfg.get("max_position_pct", 0.95)
    try:
        max_position_pct = float(max_position_pct_raw)
    except (TypeError, ValueError):
        max_position_pct = 0.95
        cli_warnings.append("invalid max_position_pct; using 0.95 fallback.")
    data_cfg = config.get("data", {}) if isinstance(config.get("data"), dict) else {}
    daily_dir = Path(str(data_cfg.get("daily_cache_dir", PROJECT_ROOT / "data" / "daily")))
    if not daily_dir.is_absolute():
        daily_dir = PROJECT_ROOT / daily_dir
    assert_no_partial_daily_for_research(daily_dir, caller="walk_forward")

    if args.symbols:
        symbols = [s.strip() for s in args.symbols.split(",") if s.strip()]
    elif args.all_a_share:
        universe_path = PROJECT_ROOT / "data" / "universe" / "a_share_symbols.csv"
        if universe_path.exists():
            df = read_csv_frame(universe_path, dtype=str)
            cli_warnings.extend(str(item) for item in df.attrs.get("warnings", []) if item)
            symbols = [str(r.get("symbol") or r.get("code") or "").strip() for _, r in df.iterrows()]
            symbols = [s for s in symbols if s]
        else:
            symbols = []
    else:
        symbols = ["600000", "000001", "600519", "300750"]

    if not symbols:
        payload = {
            "ok": True,
            "schema_version": API_SCHEMA_VERSION,
            "app_version": APP_VERSION,
            "package_version": PACKAGE_VERSION,
            "api_schema_version": API_SCHEMA_VERSION,
            "task_type": "walk_forward",
            "status": "warning",
            "warning_kind": "no_symbols_selected",
            "target_date": args.end_date,
            "network": False,
            "warnings": cli_warnings + ["No symbols found."],
            "suggested_next_action": "python scripts/qd.py data-bootstrap --preset low_price_demo --network false",
            "output_paths": {"json": "results/walk_forward_latest.json"},
            "generated_at": now_text(),
            "disclaimer": DISCLAIMER,
        }
        write_json(PROJECT_ROOT / "results" / "walk_forward_latest.json", payload)
        if truthy(args.summary):
            summary_payload = build_cli_summary(payload, task_type="walk_forward")
            print(json.dumps(summary_payload, ensure_ascii=True, indent=2, default=str) if truthy(args.json) else format_human_summary(summary_payload))
        else:
            print(json.dumps(payload, ensure_ascii=False, indent=2, default=str) if truthy(args.json) else payload["suggested_next_action"])
        return 0

    if not truthy(args.json) and not truthy(args.summary):
        print(f"Running walk-forward: strategy={args.strategy} symbols={len(symbols)} "
              f"train={args.train_years}y test={args.test_years}y")

    summary = run_walk_forward(
        daily_dir=daily_dir,
        symbols=symbols,
        strategy_id=args.strategy,
        start_date=args.start_date,
        end_date=args.end_date,
        train_years=args.train_years,
        test_years=args.test_years,
        min_data_grade=args.min_data_grade,
        cost_bps=args.cost_bps,
        slippage_bps=args.slippage_bps,
        max_position_pct=max_position_pct,
        max_symbols=args.max_symbols,
    )

    summary.warnings.extend(cli_warnings)
    quality = evaluate_backtest_quality(summary)
    summary.quality_grade = quality.quality_grade

    output_dir = Path(args.output_dir) if args.output_dir else PROJECT_ROOT / "results" / "walk_forward"
    if not output_dir.is_absolute():
        output_dir = PROJECT_ROOT / output_dir

    json_path, md_path = save_audit_report(output_dir, summary, quality)
    latest_payload = {
        "ok": True,
        "schema_version": API_SCHEMA_VERSION,
        "app_version": APP_VERSION,
        "package_version": PACKAGE_VERSION,
        "api_schema_version": API_SCHEMA_VERSION,
        "task_type": "walk_forward",
        "status": "passed" if summary.total_windows > 0 else "warning",
        "target_date": args.end_date,
        "generated_at": now_text(),
        "network": False,
        "assumptions": {"network": False, "data_source": "local daily csv only", "max_position_pct": max_position_pct},
        "warnings": list(summary.warnings),
        "walk_forward_summary": summary.to_dict(),
        "backtest_quality": quality.to_dict(),
        "output_paths": {"json": str(json_path.relative_to(PROJECT_ROOT)).replace("\\", "/"), "report": str(md_path.relative_to(PROJECT_ROOT)).replace("\\", "/")},
        "suggested_next_action": "python scripts/qd.py daily --network false --with-guardrails true",
        "disclaimer": DISCLAIMER,
    }
    write_json(PROJECT_ROOT / "results" / "walk_forward_latest.json", latest_payload)

    if truthy(args.summary):
        summary_payload = build_cli_summary(latest_payload, task_type="walk_forward")
        print(json.dumps(summary_payload, ensure_ascii=True, indent=2, default=str) if truthy(args.json) else format_human_summary(summary_payload))
    elif truthy(args.json):
        print(json.dumps(latest_payload, ensure_ascii=False, indent=2, default=str))
    else:
        print(f"Quality: {quality.quality_grade} ({quality.score}/100)")
        print(f"Windows: {summary.total_windows} (positive={summary.positive_windows})")
        print(f"Trades: {summary.total_trades}")
        print(f"Median annual return: {summary.median_annual_return:.2%}")
        print(f"Worst max drawdown: {summary.worst_max_drawdown:.2%}")
        print(f"Reports: {json_path}, {md_path}")
        if quality.warnings:
            for w in quality.warnings:
                print(f"  WARN: {w}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
