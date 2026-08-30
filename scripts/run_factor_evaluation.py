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

from quantdesk.factor_evaluation import evaluate_factors, write_factor_evaluation_outputs  # noqa: E402
from quantdesk.intraday_data_policy import assert_no_partial_daily_for_research  # noqa: E402
from quantdesk.point_in_time_universe import load_historical_listings, summarize_pit_universe_status  # noqa: E402

from quantdesk.cli_summary import build_cli_summary, format_human_summary  # noqa: E402
from quantdesk.corporate_action_repair import repair_corporate_actions  # noqa: E402
from quantdesk.csv_io import read_csv_frame  # noqa: E402
from quantdesk.factors.liquidity import compute_liquidity_factors  # noqa: E402
from quantdesk.factors.momentum import compute_momentum_factors  # noqa: E402
from quantdesk.factors.volatility import compute_volatility_factors  # noqa: E402
from quantdesk.shared import normalize_symbol, now_text, truthy  # noqa: E402
from quantdesk.version import API_SCHEMA_VERSION, APP_VERSION, PACKAGE_VERSION  # noqa: E402

DEFAULT_FACTORS = [
    "momentum_5d",
    "momentum_20d",
    "momentum_60d",
    "reversal_5d",
    "realized_volatility_20d",
    "parkinson_volatility_20d",
    "intraday_range_ratio_20d",
    "turnover_proxy_20d",
    "amihud_illiquidity_20d",
    "volume_price_corr_20d",
]
DAILY_ALIASES = {
    "trade_date": "date",
    "datetime": "date",
    "code": "symbol",
    "ts_code": "symbol",
    "preclose": "prev_close",
    "change_pct": "pct_chg",
}
DISCLAIMER = "Research diagnostics only. Not investment advice. Factor evaluation does not place orders or change trading decisions."


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run QuantDesk offline factor evaluation diagnostics.")
    parser.add_argument("--network", default="false", help="Defaults to false. This command does not fetch market data.")
    parser.add_argument("--summary", nargs="?", const="true", default="false", help="Print compact operator summary.")
    parser.add_argument("--json", default="false", help="Print JSON to stdout.")
    parser.add_argument("--horizons", default="1,5,10,20")
    parser.add_argument("--quantiles", type=int, default=5)
    parser.add_argument("--min-cross-section", type=int, default=10)
    parser.add_argument("--max-symbols", type=int, default=100)
    parser.add_argument("--max-days", type=int, default=260, help="Use the most recent N daily rows per symbol for operator-speed diagnostics.")
    parser.add_argument("--start-date", default="")
    parser.add_argument("--end-date", default="")
    parser.add_argument("--factors", default=",".join(DEFAULT_FACTORS))
    parser.add_argument("--daily-dir", default="data/daily")
    parser.add_argument("--output-dir", default="results")
    parser.add_argument("--report-dir", default="reports")
    parser.add_argument("--synthetic-test-data", default="false", help="Test-only synthetic panel; never used by default.")
    parser.add_argument("--use-point-in-time-universe", default="false")
    parser.add_argument("--historical-listings-csv", default="data/universe/historical_listings.csv")
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


def _read_daily_file(path: Path, fallback_symbol: str) -> pd.DataFrame:
    frame = read_csv_frame(path)
    frame = frame.rename(columns={str(col): DAILY_ALIASES.get(str(col), str(col)) for col in frame.columns})
    frame = frame.loc[:, ~frame.columns.duplicated()].copy()
    if "symbol" not in frame.columns:
        frame["symbol"] = fallback_symbol
    frame["symbol"] = frame["symbol"].map(normalize_symbol)
    if "date" in frame.columns:
        frame["date"] = pd.to_datetime(frame["date"], errors="coerce")
    for column in ("open", "high", "low", "close", "volume", "amount", "turnover"):
        if column in frame.columns:
            frame[column] = pd.to_numeric(frame[column], errors="coerce")
    # data/daily is adjust=raw, so the price columns carry split/dividend cliffs
    # that are not real moves (300259 prints -58% on 2018-05-23 where pct_chg says
    # -7.31%). Factor evaluation computes forward returns from these prices, so an
    # unrepaired cliff enters the panel as a genuine -58% return and corrupts every
    # IC and quantile spread it touches. Dates must be ascending for the
    # back-adjustment anchor to be the latest bar.
    if "date" in frame.columns:
        frame = frame.sort_values("date").reset_index(drop=True)
    repaired, info = repair_corporate_actions(frame)
    if info.get("unverifiable"):
        # A >35% move that pct_chg cannot corroborate cannot be repaired; measuring
        # it anyway would silently poison the factor. Drop the symbol instead.
        empty = pd.DataFrame()
        empty.attrs["warnings"] = [f"corporate_action_unverifiable:{fallback_symbol}"]
        return empty
    if info.get("ca_bars"):
        repaired.attrs["warnings"] = list(repaired.attrs.get("warnings") or []) + [
            f"corporate_action_repaired:{fallback_symbol}:{info['ca_bars']}"
        ]
    return repaired


def load_local_daily_panel(
    daily_dir: str | Path,
    *,
    max_symbols: int = 100,
    max_days: int = 260,
    start_date: str = "",
    end_date: str = "",
    symbols: list[str] | None = None,
) -> tuple[pd.DataFrame, list[str]]:
    root = _project_path(daily_dir)
    assert_no_partial_daily_for_research(root, caller="factor_evaluation")
    warnings: list[str] = []
    if not root.exists():
        return pd.DataFrame(), [f"no_daily_data: daily directory not found: {_rel(root)}"]
    frames: list[pd.DataFrame] = []
    paths = sorted(root.glob("*.csv"))
    requested_symbols = [normalize_symbol(item) for item in (symbols or [])]
    requested = {item for item in requested_symbols if item}
    if requested:
        matched: list[Path] = []
        seen: set[str] = set()
        for path in paths:
            symbol = normalize_symbol(path.stem.replace("_qfq", ""))
            if symbol in requested and symbol not in seen:
                matched.append(path)
                seen.add(symbol)
        missing = sorted(requested - seen)
        if missing:
            preview = ",".join(missing[:10])
            suffix = f",+{len(missing) - 10}" if len(missing) > 10 else ""
            warnings.append(f"daily_file_missing_for_requested_symbols:{preview}{suffix}")
        paths = matched
    elif int(max_symbols or 0) > 0:
        paths = paths[: int(max_symbols)]
    else:
        paths = paths[:1]
        warnings.append("max_symbols_zero_without_requested_symbols: loaded first daily file only")
    for path in paths:
        symbol = normalize_symbol(path.stem.replace("_qfq", ""))
        try:
            frame = _read_daily_file(path, symbol)
        except Exception as exc:  # noqa: BLE001
            warnings.append(f"daily_file_read_failed:{path.name}:{exc}")
            continue
        warnings.extend(str(item) for item in frame.attrs.get("warnings", []) if item)
        required = {"date", "symbol", "close"}
        if not required.issubset(frame.columns):
            warnings.append(f"daily_required_columns_missing:{path.name}")
            continue
        frame = frame.sort_values("date")
        if max_days and int(max_days) > 0:
            frame = frame.tail(int(max_days))
        if "open" not in frame.columns:
            frame["open"] = frame["close"]
        if "high" not in frame.columns:
            frame["high"] = frame["close"]
        if "low" not in frame.columns:
            frame["low"] = frame["close"]
        frames.append(frame)
    if not frames:
        return pd.DataFrame(), warnings or ["no_daily_data: no readable daily CSV files were found"]
    panel = pd.concat(frames, ignore_index=True)
    panel = panel.dropna(subset=["date", "symbol", "close"])
    panel = panel[panel["close"] > 0]
    if start_date:
        panel = panel[panel["date"] >= pd.to_datetime(start_date, errors="coerce")]
    if end_date:
        panel = panel[panel["date"] <= pd.to_datetime(end_date, errors="coerce")]
    return panel.sort_values(["symbol", "date"]).reset_index(drop=True), warnings


def build_synthetic_panel(symbol_count: int = 24, days: int = 90) -> pd.DataFrame:
    dates = pd.bdate_range("2026-01-02", periods=days)
    rows: list[dict[str, Any]] = []
    for index in range(symbol_count):
        symbol = f"{index + 1:06d}"
        drift = 0.0005 + index * 0.00008
        base = 10 + index * 0.2
        close = base
        for day_index, date in enumerate(dates):
            close *= 1 + drift + ((day_index % 7) - 3) * 0.0004
            rows.append({
                "date": date,
                "symbol": symbol,
                "open": close * 0.995,
                "high": close * 1.015,
                "low": close * 0.985,
                "close": close,
                "volume": 100000 + index * 5000 + day_index * 100,
                "amount": close * (100000 + index * 5000 + day_index * 100),
            })
    return pd.DataFrame(rows)


def _synthetic_pit_status(reason: str = "synthetic_test_data_pit_not_evaluated") -> dict[str, Any]:
    return {
        "pit_universe": False,
        "pit_quality": "not_evaluated",
        "historical_listings_exists": False,
        "historical_listings_path": "",
        "survivorship_bias_risk": "high",
        "warnings": [reason],
    }


def compute_default_factor_panel(panel: pd.DataFrame) -> tuple[pd.DataFrame, list[str]]:
    warnings: list[str] = []
    frame = compute_momentum_factors(panel)
    warnings.extend(str(item) for item in frame.attrs.get("warnings", []) if item)
    frame = compute_volatility_factors(frame)
    warnings.extend(str(item) for item in frame.attrs.get("warnings", []) if item)
    frame = compute_liquidity_factors(frame)
    warnings.extend(str(item) for item in frame.attrs.get("warnings", []) if item)
    return frame, list(dict.fromkeys(warnings))


def _empty_payload(args: argparse.Namespace, warnings: list[str]) -> dict[str, Any]:
    generated_at = now_text()
    return {
        "ok": True,
        "status": "warning",
        "task_type": "factor_evaluation",
        "generated_at": generated_at,
        "app_version": APP_VERSION,
        "package_version": PACKAGE_VERSION,
        "schema_version": API_SCHEMA_VERSION,
        "api_schema_version": API_SCHEMA_VERSION,
        "data_source": "local_daily",
        "target_date": args.end_date or generated_at[:10],
        "network": False,
        "config": {
            "horizons": [int(item) for item in _parse_csv_list(args.horizons)],
            "quantiles": int(args.quantiles),
            "min_cross_section": int(args.min_cross_section),
        },
        "universe": {"symbol_count": 0, "date_count": 0, "sample_count": 0, "pit_universe": False},
        "factor_metrics": {},
        "warnings": list(dict.fromkeys(warnings)),
        "assumptions": [
            "Backtest/research universe may have survivorship bias because point-in-time universe reconstruction is not enabled."
        ],
        "output_paths": {},
        "suggested_next_action": "Import local daily CSV files under data/daily, then rerun factor-eval.",
        "disclaimer": DISCLAIMER,
    }


def run(args: argparse.Namespace) -> dict[str, Any]:
    network = truthy(args.network)
    warnings: list[str] = []
    data_source = "local_daily"
    synthetic_test_data = truthy(args.synthetic_test_data)
    if network:
        warnings.append("network_requested_ignored: factor evaluation is offline and does not fetch market data")
    if synthetic_test_data:
        panel = build_synthetic_panel(symbol_count=12, days=75)
        data_source = "synthetic_test_data"
        warnings.append("synthetic_test_data: test-only sample data; do not use as production research evidence")
    else:
        panel, load_warnings = load_local_daily_panel(
            args.daily_dir,
            max_symbols=args.max_symbols,
            max_days=args.max_days,
            start_date=args.start_date,
            end_date=args.end_date,
        )
        warnings.extend(load_warnings)
    if synthetic_test_data:
        pit_warnings = ["synthetic_test_data_pit_not_evaluated"]
        pit_status = _synthetic_pit_status()
    else:
        listings, pit_warnings = load_historical_listings(args.historical_listings_csv)
        pit_status = summarize_pit_universe_status(listings, args.historical_listings_csv)
    if truthy(args.use_point_in_time_universe):
        warnings.extend(pit_warnings)
        if not pit_status.get("pit_universe"):
            warnings.append("pit_universe_requested_but_unavailable")
    if panel.empty:
        payload = _empty_payload(args, warnings or ["no_daily_data: no local daily data available"])
        payload["universe"]["pit_universe"] = bool(pit_status.get("pit_universe"))
        payload["universe"]["pit_status"] = pit_status
    else:
        factor_panel, factor_warnings = compute_default_factor_panel(panel)
        warnings.extend(factor_warnings)
        horizons = tuple(int(item) for item in _parse_csv_list(args.horizons))
        requested_factors = _parse_csv_list(args.factors) or list(DEFAULT_FACTORS)
        payload = evaluate_factors(
            factor_panel,
            requested_factors,
            horizons=horizons,
            quantiles=args.quantiles,
            min_cross_section=args.min_cross_section,
            pit_universe_status=pit_status,
        )
        payload["data_source"] = data_source
        payload["network"] = False
        payload["warnings"] = list(dict.fromkeys(list(payload.get("warnings") or []) + warnings))
        if data_source == "synthetic_test_data":
            payload["status"] = "warning"
        if payload["status"] == "passed" and len(payload["warnings"]) > 1:
            payload["status"] = "warning"
    payload = write_factor_evaluation_outputs(
        payload,
        output_dir=args.output_dir,
        report_dir=args.report_dir,
    )
    payload["output_paths"] = {key: _rel(value) for key, value in (payload.get("output_paths") or {}).items()}
    return payload


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    payload = run(args)
    wants_json = truthy(args.json)
    wants_summary = truthy(args.summary)
    if wants_summary:
        summary = build_cli_summary(payload, task_type="factor_evaluation")
        if wants_json:
            print(json.dumps(summary, ensure_ascii=True, separators=(",", ":")))
        else:
            print(format_human_summary(summary))
    elif wants_json:
        print(json.dumps(payload, ensure_ascii=True, indent=2))
    else:
        print(format_human_summary(build_cli_summary(payload, task_type="factor_evaluation")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
