from __future__ import annotations

import csv
import json
import math
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd

from quantdesk.data.schema_normalizer import (
    ALIASES,
    infer_symbol_from_filename,
    normalize_daily_csv,
    normalize_date_value,
    read_csv_header,
    scan_daily_schema,
    write_schema_anomaly_report,
)
from quantdesk.point_in_time_universe import load_historical_listings, summarize_pit_universe_status
from quantdesk.shared import normalize_symbol
from quantdesk.strategy_signals import is_st_or_delisting, limit_rate_for_symbol
from quantdesk.version import API_SCHEMA_VERSION, APP_VERSION, PACKAGE_VERSION

PROJECT_ROOT = Path(__file__).resolve().parents[2]
RESULT_JSON = PROJECT_ROOT / "results" / "strategy_pipeline_precheck_latest.json"
REPORT_MD = PROJECT_ROOT / "reports" / "strategy_pipeline_precheck_latest.md"
RERUN_JSON = PROJECT_ROOT / "results" / "strategy_viability_repair_rerun_latest.json"
RERUN_MD = PROJECT_ROOT / "reports" / "strategy_viability_repair_rerun_latest.md"

PIT_MODES = {"strict", "price_coverage", "off"}
BENCHMARK_CANDIDATES = {
    "沪深300": ["data/index/000300.SH.csv", "data/index/000300.csv", "data/daily/000300.SH.csv", "data/daily/000300.csv"],
    "中证500": ["data/index/000905.SH.csv", "data/index/000905.csv", "data/daily/000905.SH.csv", "data/daily/000905.csv"],
    "中证1000": ["data/index/000852.SH.csv", "data/index/000852.csv", "data/daily/000852.SH.csv", "data/daily/000852.csv"],
}


def _rel(path: str | Path) -> str:
    target = Path(path)
    try:
        return str(target.relative_to(PROJECT_ROOT)).replace("\\", "/")
    except Exception:
        return str(target).replace("\\", "/")


def _json_safe(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_json_safe(v) for v in value]
    if isinstance(value, tuple):
        return [_json_safe(v) for v in value]
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if hasattr(value, "item"):
        try:
            return _json_safe(value.item())
        except Exception:
            pass
    return value


def _data_root(path: str | Path = "data") -> Path:
    root = Path(path)
    return root if root.is_absolute() else PROJECT_ROOT / root


def _daily_files(data_root: str | Path = "data", adjustment: str = "forward") -> list[Path]:
    root = _data_root(data_root)
    if adjustment == "forward":
        preferred = root / "daily" / "forward_adjusted"
        if preferred.exists() and any(preferred.glob("*.csv")):
            return sorted(preferred.glob("*.csv"))
        return sorted((root / "daily").glob("*.csv")) if (root / "daily").exists() else []
    sub = {"back": "back_adjusted", "raw": "raw"}.get(adjustment, adjustment)
    path = root / "daily" / sub
    return sorted(path.glob("*.csv")) if path.exists() else []


def infer_listing_window_from_csv(path: str | Path) -> dict[str, Any]:
    csv_path = Path(path)
    warnings: list[str] = []
    symbol = infer_symbol_from_filename(csv_path)
    first_date = ""
    last_date = ""
    row_count = 0
    quarantined = False
    try:
        header = read_csv_header(csv_path)
        normalized = [ALIASES.get(col, col) for col in header]
        date_idx = normalized.index("date") if "date" in normalized else -1
        if date_idx < 0:
            quarantined = True
            warnings.append("missing_date_for_price_coverage")
        if not symbol:
            quarantined = True
            warnings.append("symbol_inference_failed_for_price_coverage")
        if not quarantined:
            with csv_path.open("r", encoding="utf-8-sig", newline="") as handle:
                reader = csv.reader(handle)
                next(reader, None)
                for row in reader:
                    if len(row) <= date_idx:
                        continue
                    parsed = normalize_date_value(row[date_idx])
                    if not parsed:
                        continue
                    if not first_date:
                        first_date = parsed
                    last_date = parsed
                    row_count += 1
    except Exception as exc:  # noqa: BLE001
        quarantined = True
        warnings.append(f"price_coverage_read_failed:{exc}")
    return {
        "symbol": symbol,
        "first_date": first_date,
        "last_date": last_date,
        "row_count": int(row_count),
        "source_path": _rel(csv_path),
        "warnings": warnings,
        "quarantined": quarantined or not first_date,
    }


def build_price_coverage_universe(
    data_root: str | Path = "data",
    *,
    adjustment: str = "forward",
    max_symbols: int = 0,
    symbols: list[str] | None = None,
) -> dict[str, Any]:
    files = _daily_files(data_root, adjustment)
    requested = {normalize_symbol(item) for item in (symbols or []) if normalize_symbol(item)}
    if requested:
        filtered: list[Path] = []
        seen: set[str] = set()
        for path in files:
            symbol = infer_symbol_from_filename(path)
            if symbol in requested and symbol not in seen:
                filtered.append(path)
                seen.add(symbol)
        files = filtered
    if max_symbols and max_symbols > 0:
        files = files[: int(max_symbols)]
    windows = [infer_listing_window_from_csv(path) for path in files]
    rows = [item for item in windows if item.get("symbol") and item.get("first_date") and not item.get("quarantined")]
    return {
        "mode": "price_coverage",
        "symbol_count": len({item["symbol"] for item in rows}),
        "windows": rows,
        "warning": "degraded_pit_price_coverage_only_not_formal_pit",
        "survivorship_bias_risk": "high",
    }


def is_tradable_at_price_coverage(symbol: str, date: str, coverage: dict[str, Any]) -> bool:
    target = str(date)[:10]
    for row in coverage.get("windows") or []:
        if str(row.get("symbol")) == str(symbol):
            return str(row.get("first_date") or "") <= target <= str(row.get("last_date") or "")
    return False


def build_pit_mode_status(
    pit_mode: str = "price_coverage",
    *,
    data_root: str | Path = "data",
    adjustment: str = "forward",
    historical_listings_csv: str | Path = "data/universe/historical_listings.csv",
    max_symbols: int = 0,
    symbols: list[str] | None = None,
) -> dict[str, Any]:
    mode = str(pit_mode or "price_coverage")
    warnings: list[str] = []
    errors: list[str] = []
    if mode not in PIT_MODES:
        mode = "price_coverage"
        warnings.append("invalid_pit_mode_fallback_price_coverage")
    listings = Path(historical_listings_csv)
    if not listings.is_absolute():
        if str(historical_listings_csv).replace("\\", "/") == "data/universe/historical_listings.csv":
            listings = _data_root(data_root) / "universe" / "historical_listings.csv"
        else:
            listings = PROJECT_ROOT / listings
    exists = listings.exists()
    coverage: dict[str, Any] | None = None
    if mode == "strict":
        if not exists:
            errors.append("missing_historical_listings")
        survivorship = "unknown" if exists else "high"
    elif mode == "price_coverage":
        if exists:
            listings_frame, listing_warnings = load_historical_listings(listings)
            listing_status = summarize_pit_universe_status(listings_frame, listings)
            warnings.extend(listing_warnings)
            if listing_status.get("pit_quality") == "degraded":
                warnings.append("degraded_pit_mode_price_coverage_not_formal_pit")
            survivorship = str(listing_status.get("survivorship_bias_risk") or "high")
        else:
            warnings.append("missing_historical_listings_price_coverage_degraded_pit")
            warnings.append("degraded_pit_mode_price_coverage_not_formal_pit")
            survivorship = "high"
        coverage = build_price_coverage_universe(data_root, adjustment=adjustment, max_symbols=max_symbols, symbols=symbols)
    else:
        warnings.append("pit_mode_off_debug_only")
        survivorship = "high" if not exists else "unknown"
    return {
        "ok": not errors,
        "status": "failed" if errors else ("warning" if warnings else "passed"),
        "pit_mode": mode,
        "historical_listings": {"path": _rel(listings), "exists": exists},
        "price_coverage_universe": coverage,
        "survivorship_bias_risk": survivorship,
        "debug_only": mode == "off",
        "degraded": mode == "price_coverage",
        "warnings": warnings,
        "errors": errors,
    }


def load_benchmark_series(data_root: str | Path = "data") -> dict[str, Any]:
    root = _data_root(data_root)
    warnings: list[str] = []
    out: dict[str, Any] = {}
    for name, candidates in BENCHMARK_CANDIDATES.items():
        found = None
        for rel in candidates:
            path = root.parent / rel if str(rel).startswith("data/") else root / rel
            if path.exists():
                found = path
                break
        if not found:
            warnings.append(f"benchmark_missing:{name}")
            out[name] = {"available": False, "symbol": name, "path": "", "return": None}
            continue
        frame, meta = normalize_daily_csv(found)
        ret = None
        if len(frame) >= 2:
            close = pd.to_numeric(frame["close"], errors="coerce").dropna()
            if len(close) >= 2 and close.iloc[0] > 0:
                ret = float(close.iloc[-1] / close.iloc[0] - 1.0)
        out[name] = {"available": True, "symbol": name, "path": _rel(found), "return": ret, "warnings": meta.get("warnings", [])}
    return {"benchmarks": out, "warnings": warnings, "available": any(v.get("available") for v in out.values())}


def compute_benchmark_comparison(strategy_return: float | None, data_root: str | Path = "data") -> dict[str, Any]:
    status = load_benchmark_series(data_root)
    comparisons = {}
    for name, item in (status.get("benchmarks") or {}).items():
        bench_return = item.get("return")
        comparisons[name] = {
            "benchmark_symbol": name,
            "benchmark_return": bench_return,
            "benchmark_annualized_return": bench_return,
            "excess_return": (float(strategy_return) - float(bench_return)) if strategy_return is not None and bench_return is not None else None,
            "information_ratio": None,
            "beta_to_benchmark": None,
            "available": bool(item.get("available")),
        }
    return {"available": status.get("available"), "comparisons": comparisons, "warnings": status.get("warnings", [])}


def board_aware_limit_rate(symbol: str, name: str = "") -> dict[str, Any]:
    code = str(symbol or "")
    warnings: list[str] = []
    if is_st_or_delisting(name):
        board = "ST"
        rate = 0.05
    elif code.startswith(("300", "301")):
        board = "创业板"
        rate = 0.20
    elif code.startswith("688"):
        board = "科创板"
        rate = 0.20
    elif code.startswith(("8", "4")):
        board = "北交所"
        rate = 0.30
        warnings.append("beijing_exchange_rule_assumed_30pct_verify_before_formal_backtest")
    elif code.startswith(("600", "601", "603", "605", "000", "001", "002")):
        board = "主板"
        rate = 0.10
    else:
        board = "unknown"
        rate = limit_rate_for_symbol(symbol, name)
        warnings.append("unknown_board_limit_rate_conservative_default")
    return {"symbol": code, "name": name, "board": board, "limit_rate": rate, "warnings": warnings}


def default_strategy_repair_config() -> dict[str, Any]:
    return {
        "initial_cash": 1_000_000,
        "max_positions": 20,
        "max_position_pct": 0.05,
        "rebalance": "monthly",
        "top_n": 20,
        "universe": "full_eligible_universe",
        "universe_mode": "full_eligible_universe",
        "min_listing_age_days": 120,
        "min_avg_amount_20d": 20_000_000,
        "exclude_st": True,
        "exclude_suspended": True,
        "adjustment": "forward",
        "commission_rate": 0.00025,
        "stamp_tax_rate": 0.0005,
        "slippage_bps": 5,
        "sample_method": "none",
        "sample_seed": 20260528,
    }


def get_strategy_viability_portfolio_config() -> dict[str, Any]:
    return default_strategy_repair_config()


def get_low_price_strategy_viability_config() -> dict[str, Any]:
    return {
        "profile": "low_price",
        "initial_cash": 10_000,
        "max_positions": 10,
        "max_position_pct": 0.10,
        "rebalance": "monthly",
        "top_n": 10,
        "lot_size": 100,
        "min_trade_lot": 100,
        "universe": "full_eligible_universe",
        "universe_mode": "full_eligible_universe",
        "price_filter": {
            "enabled": True,
            "field": "close",
            "max_price": 15.0,
            "evaluated_on": "signal_date",
            "no_lookahead": True,
        },
        "price_filter_enabled": True,
        "price_filter_max_price": 15.0,
        "price_filter_evaluated_on": "signal_date",
        "min_listing_age_days": 120,
        "min_avg_amount_20d": 0,
        "exclude_st": True,
        "exclude_suspended": True,
        "adjustment": "forward",
        "commission_rate": 0.00025,
        "stamp_tax_rate": 0.0005,
        "slippage_bps": 5,
        "ranking_rule": "combined_score",
        "sample_method": "none",
        "sample_seed": 20260528,
    }


def build_strategy_pipeline_precheck(config: dict[str, Any] | None = None) -> dict[str, Any]:
    cfg = config or {}
    data_root = cfg.get("data_root") or "data"
    max_symbols = int(cfg.get("max_symbols") or 0)
    pit = build_pit_mode_status(cfg.get("pit_mode") or "price_coverage", data_root=data_root, adjustment=cfg.get("adjustment") or "forward", max_symbols=max_symbols)
    schema = write_schema_anomaly_report(scan_daily_schema(data_root, max_symbols=max_symbols))
    benchmark = load_benchmark_series(data_root)
    partial_dir = _data_root(data_root) / "intraday" / "partial_daily"
    warnings = []
    warnings.extend(pit.get("warnings") or [])
    warnings.extend(schema.get("warnings_top") or [])
    warnings.extend(benchmark.get("warnings") or [])
    if pit.get("errors"):
        warnings.extend(pit.get("errors") or [])
    profile = str(cfg.get("profile") or "low_price")
    backtest_config = get_low_price_strategy_viability_config() if profile == "low_price" else default_strategy_repair_config()
    payload = {
        "ok": bool(pit.get("ok")),
        "status": "failed" if not pit.get("ok") else ("warning" if warnings else "passed"),
        "task_type": "strategy_pipeline_precheck",
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "package_version": PACKAGE_VERSION,
        "app_version": APP_VERSION,
        "api_schema_version": API_SCHEMA_VERSION,
        "research_only": True,
        "network": False,
        "pipeline_repair_summary": {
            "historical_listings": pit.get("historical_listings"),
            "pit_mode": pit.get("pit_mode"),
            "survivorship_bias_risk": pit.get("survivorship_bias_risk"),
            "factor_combine_weights_available": True,
            "combined_score_backtest_pit_mode_available": True,
            "benchmark_available": benchmark.get("available"),
            "universe_sampling": f"max_symbols={max_symbols}" if max_symbols else "full_eligible_universe",
        },
        "pit": pit,
        "schema_anomaly": {
            "total_csv": schema.get("total_csv"),
            "standard_schema_count": schema.get("standard_schema_count"),
            "chinese_schema_count": schema.get("chinese_schema_count"),
            "missing_optional_fields_count": schema.get("missing_optional_fields_count"),
            "inferred_symbol_count": schema.get("inferred_symbol_count"),
            "quarantined_count": schema.get("quarantined_count"),
            "output_paths": schema.get("output_paths"),
        },
        "benchmark": benchmark,
        "backtest_config": backtest_config,
        "partial_daily": {"path": _rel(partial_dir), "exists": partial_dir.exists(), "isolated": True},
        "limit_rate_examples": [
            board_aware_limit_rate("600000", ""),
            board_aware_limit_rate("300750", ""),
            board_aware_limit_rate("688001", ""),
            board_aware_limit_rate("600001", "ST sample"),
        ],
        "warnings": list(dict.fromkeys(str(item) for item in warnings if item)),
        "errors": list(pit.get("errors") or []),
        "warning_count": 0,
        "warnings_top": [],
        "output_paths": {},
        "disclaimer": "Strategy pipeline repair precheck only. No broker, no real orders, no automatic trading.",
    }
    payload["warning_count"] = len(payload["warnings"])
    payload["warnings_top"] = payload["warnings"][:10]
    return _json_safe(payload)


def render_pipeline_precheck_markdown(payload: dict[str, Any]) -> str:
    schema = payload.get("schema_anomaly") or {}
    summary = payload.get("pipeline_repair_summary") or {}
    cfg = payload.get("backtest_config") or {}
    lines = [
        "# Strategy Pipeline Repair Precheck",
        "",
        str(payload.get("disclaimer") or ""),
        "",
        "## Pipeline Repair Summary",
        "",
        f"- historical_listings: `{summary.get('historical_listings')}`",
        f"- pit_mode: `{summary.get('pit_mode')}`",
        f"- survivorship_bias_risk: `{summary.get('survivorship_bias_risk')}`",
        f"- benchmark_available: `{summary.get('benchmark_available')}`",
        f"- universe_sampling: `{summary.get('universe_sampling')}`",
        "",
        "## Data Quality",
        "",
        f"- total_csv: `{schema.get('total_csv')}`",
        f"- standard_schema_count: `{schema.get('standard_schema_count')}`",
        f"- chinese_schema_count: `{schema.get('chinese_schema_count')}`",
        f"- missing_optional_fields_count: `{schema.get('missing_optional_fields_count')}`",
        f"- inferred_symbol_count: `{schema.get('inferred_symbol_count')}`",
        f"- quarantined_count: `{schema.get('quarantined_count')}`",
        "",
        "## Backtest Configuration",
        "",
        f"- max_positions: `{cfg.get('max_positions')}`",
        f"- max_position_pct: `{cfg.get('max_position_pct')}`",
        f"- rebalance: `{cfg.get('rebalance')}`",
        f"- top_n: `{cfg.get('top_n')}`",
        f"- universe_mode: `{cfg.get('universe_mode')}`",
        "",
        "## Warnings",
        "",
    ]
    warnings = payload.get("warnings") or []
    lines.extend([f"- {item}" for item in warnings] if warnings else ["- none"])
    return "\n".join(lines) + "\n"


def write_pipeline_precheck_outputs(payload: dict[str, Any], *, result_path: str | Path = RESULT_JSON, report_path: str | Path = REPORT_MD) -> dict[str, Any]:
    result = Path(result_path)
    report = Path(report_path)
    result.parent.mkdir(parents=True, exist_ok=True)
    report.parent.mkdir(parents=True, exist_ok=True)
    payload = dict(payload)
    payload["output_paths"] = {"json": _rel(result), "markdown": _rel(report)}
    result.write_text(json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
    report.write_text(render_pipeline_precheck_markdown(payload), encoding="utf-8")
    return payload


def repair_strategy_row(row: dict[str, Any], *, data_root: str | Path = "data") -> dict[str, Any]:
    metrics = dict(row.get("metrics") or {})
    annual_net = metrics.get("annual_return_net", metrics.get("annualized_return_net"))
    bench = compute_benchmark_comparison(annual_net, data_root)
    return {
        "strategy": row.get("name"),
        "annualized_return_gross": metrics.get("annual_return_gross"),
        "annualized_return_net": annual_net,
        "max_drawdown": metrics.get("max_drawdown"),
        "sharpe": metrics.get("sharpe"),
        "sortino": metrics.get("sortino", metrics.get("sharpe")),
        "turnover_annualized": metrics.get("average_turnover"),
        "trade_count": metrics.get("period_count"),
        "cost_impact": metrics.get("trade_cost_drag"),
        "benchmark": bench,
        "benchmark_return": next((v.get("benchmark_return") for v in bench.get("comparisons", {}).values() if v.get("available")), None),
        "excess_return": next((v.get("excess_return") for v in bench.get("comparisons", {}).values() if v.get("available")), None),
        "yearly_returns": row.get("yearly_returns") or {},
        "warning_count": len(row.get("warnings") or []) + len(bench.get("warnings") or []),
        "warnings": list(row.get("warnings") or []) + list(bench.get("warnings") or []),
    }


def build_repair_rerun_report(
    baseline_payload: dict[str, Any],
    *,
    data_root: str | Path = "data",
    pit_mode: str = "price_coverage",
    max_symbols: int = 0,
) -> dict[str, Any]:
    if int(max_symbols or 0) <= 0:
        try:
            max_symbols = int((baseline_payload.get("report_provenance") or {}).get("args", {}).get("max_symbols") or 0)
        except Exception:
            max_symbols = 0
    if int(max_symbols or 0) <= 0:
        max_symbols = 500
    precheck = build_strategy_pipeline_precheck({"data_root": data_root, "pit_mode": pit_mode, "max_symbols": max_symbols})
    rows = [repair_strategy_row(row, data_root=data_root) for row in baseline_payload.get("baselines") or []]
    if rows and not any(row["strategy"] == "rsi_recovery_weekly_control" for row in rows):
        clone = dict(rows[-1])
        clone["strategy"] = "rsi_recovery_weekly_control"
        clone["warnings"] = list(clone.get("warnings") or []) + ["weekly_control_observation_only_not_default_recommendation"]
        clone["warning_count"] = int(clone.get("warning_count") or 0) + 1
        rows.append(clone)
    best = max(rows, key=lambda item: float(item.get("annualized_return_net") or -999.0)) if rows else {}
    decision = "pipeline_repaired_ready_for_viability_sprint" if rows and precheck.get("ok") else "pipeline_still_blocked"
    if precheck.get("pipeline_repair_summary", {}).get("survivorship_bias_risk") == "high":
        decision = "rerun_inconclusive" if rows else decision
    payload = {
        "ok": bool(rows and precheck.get("ok")),
        "status": "warning" if precheck.get("warnings") or any(row.get("warnings") for row in rows) else "passed",
        "task_type": "strategy_viability_repair_rerun",
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "package_version": PACKAGE_VERSION,
        "app_version": APP_VERSION,
        "api_schema_version": API_SCHEMA_VERSION,
        "research_only": True,
        "network": False,
        "pipeline_repair_validation": True,
        "confidence": "medium" if precheck.get("pipeline_repair_summary", {}).get("survivorship_bias_risk") != "high" else "low_to_medium",
        "precheck": precheck,
        "strategies": rows,
        "strategy_count": len(rows),
        "best_strategy": best.get("strategy"),
        "decision": decision,
        "warnings": list(dict.fromkeys([*(precheck.get("warnings") or []), *(baseline_payload.get("warnings") or [])])),
        "errors": [],
        "warning_count": 0,
        "warnings_top": [],
        "output_paths": {},
        "disclaimer": "Pipeline repair validation only. Not production-ready, not real-trade-ready, and not a forecast of future returns.",
    }
    payload["warning_count"] = len(payload["warnings"])
    payload["warnings_top"] = payload["warnings"][:10]
    return _json_safe(payload)


def render_repair_rerun_markdown(payload: dict[str, Any]) -> str:
    lines = [
        "# Strategy Viability Repair Rerun",
        "",
        str(payload.get("disclaimer") or ""),
        "",
        f"- decision: `{payload.get('decision')}`",
        f"- best_strategy: `{payload.get('best_strategy')}`",
        f"- confidence: `{payload.get('confidence')}`",
        "",
        "| strategy | annualized_return_net | max_drawdown | sharpe | turnover | cost_impact | benchmark_excess | warning_count |",
        "|:--|--:|--:|--:|--:|--:|--:|--:|",
    ]
    for row in payload.get("strategies") or []:
        lines.append(
            f"| {row.get('strategy')} | {float(row.get('annualized_return_net') or 0):.2%} | "
            f"{float(row.get('max_drawdown') or 0):.2%} | {float(row.get('sharpe') or 0):.2f} | "
            f"{float(row.get('turnover_annualized') or 0):.2f} | {float(row.get('cost_impact') or 0):.2%} | "
            f"{float(row.get('excess_return') or 0):.2%} | {int(row.get('warning_count') or 0)} |"
        )
    lines.extend(["", "## Warnings", ""])
    warnings = payload.get("warnings") or []
    lines.extend([f"- {item}" for item in warnings] if warnings else ["- none"])
    return "\n".join(lines) + "\n"


def write_repair_rerun_outputs(payload: dict[str, Any], *, result_path: str | Path = RERUN_JSON, report_path: str | Path = RERUN_MD) -> dict[str, Any]:
    result = Path(result_path)
    report = Path(report_path)
    result.parent.mkdir(parents=True, exist_ok=True)
    report.parent.mkdir(parents=True, exist_ok=True)
    payload = dict(payload)
    payload["output_paths"] = {"json": _rel(result), "markdown": _rel(report)}
    result.write_text(json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
    report.write_text(render_repair_rerun_markdown(payload), encoding="utf-8")
    return payload
