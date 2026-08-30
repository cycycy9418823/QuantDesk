from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from quantdesk.combined_score_backtest import (  # noqa: E402
    extract_score_panel,
    load_combined_score_payload,
    run_combined_score_backtest,
    write_combined_score_backtest_outputs,
)
from quantdesk.point_in_time_universe import load_historical_listings, summarize_pit_universe_status  # noqa: E402

from quantdesk.cli_summary import build_cli_summary, format_human_summary  # noqa: E402
from quantdesk.research.strategy_pipeline_repair import (  # noqa: E402  # noqa: E402
    build_pit_mode_status,
    compute_benchmark_comparison,
    default_strategy_repair_config,
    get_low_price_strategy_viability_config,
)
from quantdesk.shared import truthy  # noqa: E402
from scripts.run_factor_evaluation import build_synthetic_panel, load_local_daily_panel  # noqa: E402


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run research-only backtest for QuantDesk combined_score rankings.")
    parser.add_argument("--network", default="false", help="Defaults to false. This command never fetches market data.")
    parser.add_argument("--summary", nargs="?", const="true", default="false")
    parser.add_argument("--json", default="false")
    parser.add_argument("--combined-score-json", default="results/combined_score_latest.json")
    parser.add_argument("--portfolio-mode", default="top_n", choices=["top_n", "top_quantile", "long_short_quantile"])
    parser.add_argument("--profile", default="low_price", choices=["low_price", "general"])
    parser.add_argument("--top-n", type=int, default=0)
    parser.add_argument("--quantile", type=float, default=0.2)
    parser.add_argument("--long-short", default="false")
    parser.add_argument("--rebalance-frequency", default="monthly", choices=["weekly", "monthly"])
    parser.add_argument("--holding-days", type=int, default=20)
    parser.add_argument("--execution-lag-days", type=int, default=1)
    parser.add_argument("--transaction-cost-bps", type=float, default=10.0)
    parser.add_argument("--slippage-bps", type=float, default=5.0)
    parser.add_argument("--start-date", default="")
    parser.add_argument("--end-date", default="")
    parser.add_argument("--max-symbols", type=int, default=0)
    parser.add_argument("--max-days", type=int, default=320)
    parser.add_argument("--daily-dir", default="data/daily")
    parser.add_argument("--output-dir", default="results")
    parser.add_argument("--report-dir", default="reports")
    parser.add_argument("--synthetic-test-data", default="false", help="Test-only synthetic score and price data; never used by default.")
    parser.add_argument("--use-point-in-time-universe", default="false")
    parser.add_argument("--pit-mode", default="price_coverage", choices=["strict", "price_coverage", "off"])
    parser.add_argument("--historical-listings-csv", default="data/universe/historical_listings.csv")
    parser.add_argument("--write-latest", default="")
    parser.add_argument("--smoke-run", default="false")
    return parser.parse_args(argv)


def _project_path(value: str | Path) -> Path:
    path = Path(value)
    return path if path.is_absolute() else PROJECT_ROOT / path


def _rel(path: str | Path) -> str:
    target = _project_path(path)
    try:
        return str(target.relative_to(PROJECT_ROOT)).replace("\\", "/")
    except Exception:
        return str(target).replace("\\", "/")


def _infer_data_root_from_daily_dir(value: str | Path) -> Path:
    path = Path(value)
    if not path.is_absolute():
        path = PROJECT_ROOT / path
    if path.name in {"daily", "forward_adjusted", "back_adjusted", "raw"}:
        if path.name == "daily":
            return path.parent
        if path.parent.name == "daily":
            return path.parent.parent
    return PROJECT_ROOT / "data"


def _synthetic_score_panel(price_panel: pd.DataFrame) -> pd.DataFrame:
    frame = price_panel.copy()
    frame = frame.sort_values(["symbol", "date"])
    frame["momentum_proxy"] = frame.groupby("symbol")["close"].transform(
        lambda values: values.ffill().pct_change(5, fill_method=None)
    )
    frame["symbol_rank_seed"] = frame["symbol"].astype(str).str.extract(r"(\d+)")[0].fillna("0").astype(int)
    frame["combined_score"] = frame["momentum_proxy"].fillna(0.0) + frame["symbol_rank_seed"] * 0.0001
    return frame[["date", "symbol", "combined_score"]].dropna().reset_index(drop=True)


def _degraded_pit_mode_status(pit_mode: str, reason: str) -> dict[str, Any]:
    mode = str(pit_mode or "price_coverage")
    if mode not in {"strict", "price_coverage", "off"}:
        mode = "price_coverage"
    warnings = [reason]
    errors: list[str] = []
    if mode == "strict":
        errors.append("pit_strict_unavailable_for_synthetic_or_smoke_run")
    elif mode == "off":
        warnings.append("pit_mode_off_debug_only")
    else:
        warnings.append("degraded_pit_mode_price_coverage_not_formal_pit")
    return {
        "ok": not errors,
        "status": "failed" if errors else "warning",
        "pit_mode": mode,
        "historical_listings": {"path": "", "exists": False},
        "price_coverage_universe": {"mode": "not_evaluated", "symbol_count": 0, "windows": []},
        "survivorship_bias_risk": "high",
        "debug_only": mode == "off",
        "degraded": mode == "price_coverage",
        "warnings": warnings,
        "errors": errors,
    }


def _score_symbols(score_panel: pd.DataFrame, *, max_symbols: int = 0) -> list[str]:
    if score_panel.empty or "symbol" not in score_panel.columns:
        return []
    symbols = sorted({str(item) for item in score_panel["symbol"].dropna().astype(str) if str(item).strip()})
    if int(max_symbols or 0) > 0:
        return symbols[: int(max_symbols)]
    return symbols


def run(args: argparse.Namespace) -> dict[str, Any]:
    warnings: list[str] = []
    profile_config = get_low_price_strategy_viability_config() if args.profile == "low_price" else default_strategy_repair_config()
    smoke_run = truthy(args.smoke_run)
    synthetic_test_data = truthy(args.synthetic_test_data)
    write_latest = truthy(args.write_latest) if str(args.write_latest).strip() else (not smoke_run)
    if int(args.top_n or 0) <= 0:
        args.top_n = int(profile_config.get("top_n") or 10)
    if args.profile == "low_price":
        args.rebalance_frequency = "monthly"
        args.holding_days = 20
    data_source = "local_daily"
    if truthy(args.network):
        warnings.append("network_requested_ignored: combined-score-backtest is offline and does not fetch market data")
    if synthetic_test_data:
        price_panel = build_synthetic_panel(symbol_count=30, days=100)
        score_panel = _synthetic_score_panel(price_panel)
        data_source = "synthetic_test_data"
        warnings.append("synthetic_test_data: test-only sample data; do not use as production research evidence")
    else:
        score_path = _project_path(args.combined_score_json)
        if score_path.exists():
            payload = load_combined_score_payload(score_path)
            score_panel = extract_score_panel(payload)
            warnings.extend(payload.get("warnings") or [])
            warnings.extend(score_panel.attrs.get("warnings") or [])
            data_source = str(payload.get("data_source") or "local_daily")
        else:
            score_panel = pd.DataFrame()
            warnings.append(f"missing_combined_score: {_rel(score_path)}")
            data_source = "none"
        requested_symbols = _score_symbols(score_panel, max_symbols=args.max_symbols)
        price_panel, load_warnings = load_local_daily_panel(
            args.daily_dir,
            max_symbols=args.max_symbols,
            max_days=args.max_days,
            start_date=args.start_date,
            end_date=args.end_date,
            symbols=requested_symbols,
        )
        warnings.extend(load_warnings)
    data_root_for_repair = _infer_data_root_from_daily_dir(args.daily_dir)
    if synthetic_test_data or smoke_run:
        pit_reason = "synthetic_test_data_pit_not_evaluated" if synthetic_test_data else "smoke_run_pit_not_evaluated"
        pit_mode_status = _degraded_pit_mode_status(args.pit_mode, pit_reason)
        pit_warnings: list[str] = []
        pit_status = {
            "pit_universe": False,
            "pit_quality": "not_evaluated",
            "historical_listings_path": "",
            "survivorship_bias_risk": "high",
            "warnings": [pit_reason],
        }
    else:
        pit_symbols = _score_symbols(score_panel, max_symbols=args.max_symbols)
        pit_mode_status = build_pit_mode_status(
            args.pit_mode,
            data_root=data_root_for_repair,
            historical_listings_csv=args.historical_listings_csv,
            max_symbols=args.max_symbols,
            symbols=pit_symbols,
        )
        listings, pit_warnings = load_historical_listings(args.historical_listings_csv)
        pit_status = summarize_pit_universe_status(listings, args.historical_listings_csv)
    listing_risk = str(pit_status.get("survivorship_bias_risk") or "high")
    mode_risk = str(pit_mode_status.get("survivorship_bias_risk") or "high")
    combined_survivorship_risk = "high" if "high" in {listing_risk, mode_risk} else mode_risk
    pit_status.update({
        "pit_mode": args.pit_mode,
        "mode_status": pit_mode_status,
        # A mode label must never downgrade incomplete listing evidence.
        "survivorship_bias_risk": combined_survivorship_risk,
    })
    warnings.extend(pit_mode_status.get("warnings") or [])
    if pit_mode_status.get("errors"):
        warnings.extend(pit_mode_status.get("errors") or [])
        if args.pit_mode == "strict":
            score_panel = pd.DataFrame()
    pit_listings_for_engine: pd.DataFrame | None = None
    if truthy(args.use_point_in_time_universe):
        warnings.extend(pit_warnings)
        if not pit_status.get("pit_universe"):
            warnings.append("pit_universe_requested_but_unavailable")
        else:
            # P0-3: hand the historical listings to the engine so point-in-time
            # membership is enforced at EACH signal date, not only at end_date.
            pit_listings_for_engine = listings
    payload = run_combined_score_backtest(
        score_panel,
        price_panel,
        rebalance_frequency=args.rebalance_frequency,
        holding_days=args.holding_days,
        execution_lag_days=args.execution_lag_days,
        portfolio_mode=args.portfolio_mode,
        top_n=args.top_n,
        quantile=args.quantile,
        long_short=truthy(args.long_short) or args.portfolio_mode == "long_short_quantile",
        transaction_cost_bps=args.transaction_cost_bps,
        slippage_bps=args.slippage_bps,
        pit_universe_status=pit_status,
        historical_listings=pit_listings_for_engine,
        profile=args.profile,
        initial_cash=float(profile_config.get("initial_cash") or 1_000_000),
        max_position_pct=float(profile_config.get("max_position_pct") or 0.05),
        lot_size=int(profile_config.get("lot_size") or 1),
        price_filter=profile_config.get("price_filter") or {},
    )
    payload["data_source"] = data_source if payload.get("data_source") != "none" else payload.get("data_source")
    payload["network"] = False
    payload["pit_mode"] = args.pit_mode
    payload["profile"] = args.profile
    payload["initial_cash"] = profile_config.get("initial_cash")
    payload["max_positions"] = profile_config.get("max_positions")
    payload["max_position_pct"] = profile_config.get("max_position_pct")
    payload["rebalance"] = args.rebalance_frequency
    payload["top_n"] = args.top_n
    payload["lot_size"] = profile_config.get("lot_size")
    payload["price_filter_enabled"] = profile_config.get("price_filter_enabled")
    payload["price_filter_max_price"] = profile_config.get("price_filter_max_price")
    payload["price_filter_evaluated_on"] = profile_config.get("price_filter_evaluated_on")
    payload["universe_mode"] = profile_config.get("universe_mode")
    payload["sample_method"] = "random" if int(args.max_symbols or 0) else "none"
    payload["sample_seed"] = 20260528
    payload["survivorship_bias_risk"] = pit_status.get("survivorship_bias_risk")
    payload["report_provenance"] = {
        "command": "combined-score-backtest",
        "args": {key: str(value) for key, value in vars(args).items()},
        "profile": args.profile,
        "pit_mode": args.pit_mode,
        "strategies": [],
        "full": True,
        "write_latest": write_latest,
        "smoke_run": smoke_run,
        "generated_at": payload.get("generated_at"),
    }
    annual = (payload.get("performance") or {}).get("annualized_return")
    payload["benchmark"] = compute_benchmark_comparison(annual, data_root_for_repair)
    if args.pit_mode == "strict" and pit_mode_status.get("errors"):
        payload["ok"] = False
        payload["status"] = "failed"
    payload["warnings"] = list(dict.fromkeys(str(item) for item in list(payload.get("warnings") or []) + warnings if item))
    if data_source == "synthetic_test_data":
        payload["status"] = "warning"
    if payload["warnings"]:
        payload["status"] = "warning"
    output_dir = "results/smoke" if smoke_run and not write_latest else args.output_dir
    report_dir = "reports/smoke" if smoke_run and not write_latest else args.report_dir
    payload = write_combined_score_backtest_outputs(payload, output_dir=output_dir, report_dir=report_dir)
    payload["output_paths"] = {key: _rel(value) for key, value in (payload.get("output_paths") or {}).items()}
    return payload


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    payload = run(args)
    wants_json = truthy(args.json)
    wants_summary = truthy(args.summary)
    if wants_summary:
        summary = build_cli_summary(payload, task_type="combined_score_backtest")
        if wants_json:
            print(json.dumps(summary, ensure_ascii=True, separators=(",", ":"), allow_nan=False))
        else:
            print(format_human_summary(summary))
    elif wants_json:
        print(json.dumps(payload, ensure_ascii=True, indent=2, allow_nan=False))
    else:
        print(format_human_summary(build_cli_summary(payload, task_type="combined_score_backtest")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
