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

from quantdesk.cli_summary import build_cli_summary, format_human_summary  # noqa: E402
from quantdesk.point_in_time_universe import load_historical_listings, summarize_pit_universe_status  # noqa: E402
from quantdesk.research.factor_combiner import build_combined_score, write_combined_score_outputs  # noqa: E402
from quantdesk.shared import read_json, truthy  # noqa: E402
from quantdesk.version import API_SCHEMA_VERSION, APP_VERSION, PACKAGE_VERSION  # noqa: E402
from scripts.run_factor_evaluation import (
    build_synthetic_panel,
    compute_default_factor_panel,
    load_local_daily_panel,
)

DEFAULT_FACTORS = [
    "momentum_20d",
    "volatility_20d",
    "momentum_60d",
    "reversal_5d",
    "realized_volatility_20d",
    "parkinson_volatility_20d",
    "amihud_illiquidity_20d",
    "volume_price_corr_20d",
]


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build QuantDesk research-only combined factor score.")
    parser.add_argument("--network", default="false", help="Defaults to false. This command never fetches market data.")
    parser.add_argument("--summary", nargs="?", const="true", default="false")
    parser.add_argument("--json", default="false")
    parser.add_argument("--start-date", default="")
    parser.add_argument("--end-date", default="")
    parser.add_argument("--max-symbols", type=int, default=200)
    parser.add_argument("--max-days", type=int, default=260)
    parser.add_argument("--factors", default=",".join(DEFAULT_FACTORS))
    parser.add_argument("--weights", default="", help="Manual factor weights, e.g. momentum_20d=1.0,volatility_20d=-1.0")
    parser.add_argument("--standardize", default="zscore", choices=["zscore", "percentile_rank"])
    parser.add_argument("--neutralize-industry", default="true")
    parser.add_argument("--neutralize-market-cap", default="true")
    parser.add_argument("--orthogonalize", default="false")
    parser.add_argument("--weight-method", default="rank_icir", choices=["equal_weight", "ic_mean", "rank_ic_mean", "icir", "rank_icir"])
    parser.add_argument("--allow-negative-weights", default="false")
    parser.add_argument("--factor-health-json", default="results/factor_health_latest.json")
    parser.add_argument("--daily-dir", default="data/daily")
    parser.add_argument("--historical-listings-csv", default="data/universe/historical_listings.csv")
    parser.add_argument("--output-dir", default="results")
    parser.add_argument("--report-dir", default="reports")
    parser.add_argument("--synthetic-test-data", default="false", help="Test-only synthetic panel; never used by default.")
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


def _parse_csv_list(value: str) -> list[str]:
    return [item.strip() for item in str(value or "").split(",") if item.strip()]


def _parse_weights(value: str) -> tuple[dict[str, float], list[str]]:
    weights: dict[str, float] = {}
    errors: list[str] = []
    for item in _parse_csv_list(value):
        if "=" not in item:
            errors.append(f"invalid_weight_token:{item}")
            continue
        key, raw = item.split("=", 1)
        key = key.strip()
        try:
            weights[key] = float(raw.strip())
        except Exception:
            errors.append(f"invalid_weight_value:{item}")
    return weights, errors


def _add_synthetic_exposures(panel: pd.DataFrame) -> pd.DataFrame:
    frame = panel.copy()
    symbols = sorted(frame["symbol"].astype(str).unique())
    industries = {symbol: f"industry_{idx % 3}" for idx, symbol in enumerate(symbols)}
    market_caps = {symbol: 1_000_000_000.0 * (idx + 1) for idx, symbol in enumerate(symbols)}
    frame["industry"] = frame["symbol"].map(industries)
    frame["market_cap"] = frame["symbol"].map(market_caps)
    return frame


def _synthetic_pit_status(reason: str = "synthetic_test_data_pit_not_evaluated") -> dict[str, Any]:
    return {
        "pit_universe": False,
        "pit_quality": "not_evaluated",
        "historical_listings_exists": False,
        "historical_listings_path": "",
        "survivorship_bias_risk": "high",
        "warnings": [reason],
    }


def _ensure_optional_exposures(panel: pd.DataFrame) -> pd.DataFrame:
    frame = panel.copy()
    warnings = [str(item) for item in frame.attrs.get("warnings", []) or []]
    if "market_cap" not in frame.columns:
        warnings.append("missing_market_cap_not_synthesized_from_ohlcv")
    frame.attrs["warnings"] = list(dict.fromkeys(warnings))
    return frame


def run(args: argparse.Namespace) -> dict[str, Any]:
    warnings: list[str] = []
    data_source = "local_daily"
    synthetic_test_data = truthy(args.synthetic_test_data)
    if truthy(args.network):
        warnings.append("network_requested_ignored: factor-combine is offline and does not fetch market data")
    if synthetic_test_data:
        panel = _add_synthetic_exposures(build_synthetic_panel(symbol_count=12, days=75))
        data_source = "synthetic_test_data"
        warnings.append("synthetic_test_data: test-only sample data; do not use as production research evidence")
        pit_status = _synthetic_pit_status()
        pit_warnings = ["synthetic_test_data_pit_not_evaluated"]
    else:
        panel, load_warnings = load_local_daily_panel(
            args.daily_dir,
            max_symbols=args.max_symbols,
            max_days=args.max_days,
            start_date=args.start_date,
            end_date=args.end_date,
        )
        warnings.extend(load_warnings)
        panel = _ensure_optional_exposures(panel)
        warnings.extend(str(item) for item in panel.attrs.get("warnings", []) or [])
        listings, pit_warnings = load_historical_listings(args.historical_listings_csv)
        pit_status = summarize_pit_universe_status(listings, args.historical_listings_csv)
    warnings.extend(pit_warnings)
    factor_columns = _parse_csv_list(args.factors) or list(DEFAULT_FACTORS)
    if panel.empty:
        payload = build_combined_score(
            pd.DataFrame(),
            factor_columns,
            standardize_method=args.standardize,
            neutralize_industry=truthy(args.neutralize_industry),
            neutralize_market_cap=truthy(args.neutralize_market_cap),
            orthogonalize=truthy(args.orthogonalize),
            weight_method=args.weight_method,
            pit_universe_status=pit_status,
        )
    else:
        factor_panel, factor_warnings = compute_default_factor_panel(panel)
        if "volatility_20d" not in factor_panel.columns and "realized_volatility_20d" in factor_panel.columns:
            factor_panel["volatility_20d"] = factor_panel["realized_volatility_20d"]
        warnings.extend(factor_warnings)
        factor_health_path = _project_path(args.factor_health_json)
        factor_health: dict[str, Any] | None = None
        if synthetic_test_data and args.factor_health_json == "results/factor_health_latest.json":
            warnings.append("synthetic_test_data_factor_health_not_loaded: fallback to equal_weight")
        elif factor_health_path.exists():
            loaded = read_json(factor_health_path, {})
            factor_health = loaded if isinstance(loaded, dict) else None
        else:
            warnings.append(f"missing_factor_health: {args.factor_health_json}; fallback to equal_weight")
        manual_weights, weight_errors = _parse_weights(args.weights)
        warnings.extend(weight_errors)
        if manual_weights:
            unknown = sorted(set(manual_weights) - set(factor_panel.columns))
            if unknown:
                payload = {
                    "ok": False,
                    "status": "failed",
                    "task_type": "factor_combination",
                    "app_version": APP_VERSION,
                    "package_version": PACKAGE_VERSION,
                    "api_schema_version": API_SCHEMA_VERSION,
                    "factor_names": list(manual_weights),
                    "raw_weights": manual_weights,
                    "normalized_weights": {},
                    "cross_section_count": 0,
                    "insufficient_cross_section": True,
                    "warnings": warnings,
                    "errors": [f"unknown_factor:{','.join(unknown)}"],
                    "output_paths": {},
                    "disclaimer": "Research diagnostics only. No broker, no real orders.",
                }
                payload = write_combined_score_outputs(payload, output_dir=args.output_dir, report_dir=args.report_dir)
                payload["output_paths"] = {key: _rel(value) for key, value in (payload.get("output_paths") or {}).items()}
                return payload
            factor_columns = list(manual_weights)
            args.weight_method = "manual"
        payload = build_combined_score(
            factor_panel,
            factor_columns,
            standardize_method=args.standardize,
            neutralize_industry=truthy(args.neutralize_industry),
            neutralize_market_cap=truthy(args.neutralize_market_cap),
            orthogonalize=truthy(args.orthogonalize),
            weight_method=args.weight_method,
            factor_health=factor_health,
            allow_negative_weights=truthy(args.allow_negative_weights),
            manual_weights=manual_weights,
            pit_universe_status=pit_status,
        )
    payload["data_source"] = data_source if payload.get("data_source") != "none" else payload.get("data_source")
    payload["network"] = False
    payload["warnings"] = list(dict.fromkeys(list(payload.get("warnings") or []) + warnings))
    if data_source == "synthetic_test_data":
        payload["status"] = "warning"
    if len(payload["warnings"]) > 1:
        payload["status"] = "warning"
    payload = write_combined_score_outputs(payload, output_dir=args.output_dir, report_dir=args.report_dir)
    payload["output_paths"] = {key: _rel(value) for key, value in (payload.get("output_paths") or {}).items()}
    return payload


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    payload = run(args)
    wants_json = truthy(args.json)
    wants_summary = truthy(args.summary)
    if wants_summary:
        summary = build_cli_summary(payload, task_type="factor_combination")
        if wants_json:
            print(json.dumps(summary, ensure_ascii=True, separators=(",", ":")))
        else:
            print(format_human_summary(summary))
    elif wants_json:
        print(json.dumps(payload, ensure_ascii=True, indent=2))
    else:
        print(format_human_summary(build_cli_summary(payload, task_type="factor_combination")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

