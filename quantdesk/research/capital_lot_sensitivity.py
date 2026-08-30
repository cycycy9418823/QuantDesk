from __future__ import annotations

import json
import math
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd

from quantdesk.research.strategy_pipeline_repair import (
    compute_benchmark_comparison,
    get_low_price_strategy_viability_config,
)
from quantdesk.research.strategy_viability_decision import decide_capital_lot_sensitivity
from quantdesk.research.strategy_viability_runner import (
    PROJECT_ROOT,
    _compute_features,
    load_price_panel,
    run_one_baseline,
    validate_performance_metrics_sanity,
)
from quantdesk.version import API_SCHEMA_VERSION, APP_VERSION, PACKAGE_VERSION

RESULT_JSON = PROJECT_ROOT / "results" / "capital_lot_sensitivity_latest.json"
REPORT_MD = PROJECT_ROOT / "reports" / "capital_lot_sensitivity_latest.md"
DISCLAIMER = "Research-only capital and lot sensitivity. No broker, no real orders, no automatic trading."

DEFAULT_INITIAL_CASH_GRID = [10000, 50000, 100000]
DEFAULT_MAX_POSITIONS_GRID = [5, 10, 20]
DEFAULT_MAX_PRICE_GRID = [10.0, 15.0, 20.0]
DEFAULT_STRATEGIES = ["low_volatility", "momentum_low_vol", "momentum_20d"]
FULL_STRATEGIES = ["low_volatility", "momentum_low_vol", "momentum_20d", "trend_continuation", "rsi_recovery", "rsi_recovery_weekly_control"]


def max_position_pct_for_positions(max_positions: int) -> float:
    return 1.0 / max(1, int(max_positions))


def default_capital_lot_grid() -> dict[str, Any]:
    return {
        "initial_cash": list(DEFAULT_INITIAL_CASH_GRID),
        "max_positions": list(DEFAULT_MAX_POSITIONS_GRID),
        "max_price": list(DEFAULT_MAX_PRICE_GRID),
        "rebalance": ["monthly"],
        "lot_size": [100],
    }


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        number = float(value)
    except Exception:
        return default
    return number if math.isfinite(number) else default


def _json_safe(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_json_safe(v) for v in value]
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if hasattr(value, "item"):
        try:
            return _json_safe(value.item())
        except Exception:
            pass
    return value


def _rel(path: str | Path) -> str:
    target = Path(path)
    try:
        return str(target.relative_to(PROJECT_ROOT)).replace("\\", "/")
    except Exception:
        return str(target).replace("\\", "/")


def _strategy_name(name: str) -> str:
    return "momentum_20d" if name in {"pure_momentum", "momentum"} else name


def _best(rows: list[dict[str, Any]], key: str) -> dict[str, Any]:
    return max(rows, key=lambda r: _safe_float(r.get(key), -999.0)) if rows else {}


def _group_summary(rows: list[dict[str, Any]], field: str, value_field: str) -> dict[str, float]:
    out: dict[str, float] = {}
    for value, group in pd.DataFrame(rows).groupby(field) if rows else []:
        vals = pd.to_numeric(group[value_field], errors="coerce").dropna()
        out[str(value)] = float(vals.mean()) if len(vals) else 0.0
    return out


def cash_drag_severity(invested_ratio: float) -> str:
    if invested_ratio < 0.50:
        return "severe"
    if invested_ratio < 0.70:
        return "high"
    if invested_ratio < 0.85:
        return "medium"
    return "low"


def run_capital_lot_sensitivity(config: dict[str, Any] | None = None) -> dict[str, Any]:
    cfg = config or {}
    data_root = Path(cfg.get("data_root") or "data")
    root = data_root if data_root.is_absolute() else PROJECT_ROOT / data_root
    listings = root / "universe" / "historical_listings.csv"
    warnings: list[str] = []
    if not listings.exists():
        warnings.append("historical_listings_missing_run_data_universe_build_listings_first")
        return {
            "ok": False,
            "status": "failed",
            "task_type": "capital_lot_sensitivity",
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "package_version": PACKAGE_VERSION,
            "app_version": APP_VERSION,
            "api_schema_version": API_SCHEMA_VERSION,
            "message": "Run data-universe build-listings before capital-lot-sensitivity.",
            "historical_listings_exists": False,
            "warnings": warnings,
            "errors": ["historical_listings_missing"],
            "output_paths": {},
            "disclaimer": DISCLAIMER,
        }
    write_latest = bool(cfg.get("write_latest", True))
    smoke_run = bool(cfg.get("smoke_run", False))
    strategies = FULL_STRATEGIES if cfg.get("full") else list(cfg.get("strategies") or DEFAULT_STRATEGIES)
    strategies = [_strategy_name(str(item).strip()) for item in strategies if str(item).strip()]
    strategies = list(dict.fromkeys(strategies))
    initial_cash_grid = [int(x) for x in cfg.get("initial_cash_grid", DEFAULT_INITIAL_CASH_GRID)]
    max_positions_grid = [int(x) for x in cfg.get("max_positions_grid", DEFAULT_MAX_POSITIONS_GRID)]
    max_price_grid = [float(x) for x in cfg.get("max_price_grid", DEFAULT_MAX_PRICE_GRID)]
    sample_seed = int(cfg.get("sample_seed") or 20260528)
    max_symbols = int(cfg.get("max_symbols") or 0)
    if max_symbols <= 0:
        max_symbols = 500
        warnings.append("performance_fallback_sampling:sample_method=random:sample_size=500:sample_seed=20260528")
    panel, load_warnings = load_price_panel(root, adjustment="forward", max_symbols=max_symbols, sample_seed=sample_seed)
    warnings.extend(load_warnings)
    if panel.empty:
        return {
            "ok": False,
            "status": "failed",
            "task_type": "capital_lot_sensitivity",
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "package_version": PACKAGE_VERSION,
            "app_version": APP_VERSION,
            "api_schema_version": API_SCHEMA_VERSION,
            "message": "Price panel is empty; cannot run sensitivity.",
            "historical_listings_exists": True,
            "warnings": warnings + ["price_panel_empty"],
            "errors": ["price_panel_empty"],
            "output_paths": {},
            "disclaimer": DISCLAIMER,
        }
    features = _compute_features(panel)
    results: list[dict[str, Any]] = []
    for initial_cash in initial_cash_grid:
        for max_positions in max_positions_grid:
            for max_price in max_price_grid:
                for strategy in strategies:
                    base = get_low_price_strategy_viability_config()
                    base.update({
                        "profile": "low_price",
                        "initial_cash": initial_cash,
                        "max_positions": max_positions,
                        "max_position_pct": max_position_pct_for_positions(max_positions),
                        "top_n": max_positions,
                        "rebalance": "monthly",
                        "lot_size": 100,
                        "min_trade_lot": 100,
                        "price_filter": {"enabled": True, "field": "close", "max_price": max_price, "evaluated_on": "signal_date", "no_lookahead": True},
                        "price_filter_enabled": True,
                        "price_filter_max_price": max_price,
                        "price_filter_evaluated_on": "signal_date",
                        "sample_method": "random" if max_symbols else "none",
                        "sample_seed": sample_seed,
                    })
                    row = run_one_baseline(features, strategy, top_n=max_positions, strategy_config=base)
                    metrics = row.get("metrics") or {}
                    sanity = validate_performance_metrics_sanity(metrics, row.get("equity_curve") or [])
                    benchmark = compute_benchmark_comparison(_safe_float(metrics.get("annualized_return_net")), root)
                    available_bench = next((v for v in (benchmark.get("comparisons") or {}).values() if v.get("available")), {})
                    result = {
                        "strategy": row.get("name"),
                        "initial_cash": initial_cash,
                        "max_positions": max_positions,
                        "max_position_pct": max_position_pct_for_positions(max_positions),
                        "max_price": max_price,
                        "rebalance": "monthly",
                        "lot_size": 100,
                        "annualized_return_net": _safe_float(metrics.get("annualized_return_net")),
                        "annualized_return_gross": _safe_float(metrics.get("annualized_return_gross")),
                        "max_drawdown": _safe_float(metrics.get("max_drawdown")),
                        "sharpe": _safe_float(metrics.get("sharpe")),
                        "sortino": _safe_float(metrics.get("sortino")),
                        "turnover_annualized": _safe_float(metrics.get("turnover_annualized")),
                        "cost_impact": _safe_float(metrics.get("cost_impact")),
                        "trade_count": int(metrics.get("trade_count") or 0),
                        "unfilled_due_to_lot_size_count": int(metrics.get("unfilled_due_to_lot_size_count") or 0),
                        "invested_ratio_avg": _safe_float(metrics.get("invested_ratio_avg")),
                        "average_cash_drag": _safe_float(metrics.get("average_cash_drag")),
                        "final_cash": _safe_float(metrics.get("final_cash")),
                        "benchmark_symbol": available_bench.get("benchmark_symbol") or "",
                        "benchmark_available": bool(benchmark.get("available")),
                        "excess_return": _safe_float(available_bench.get("excess_return")) if available_bench else None,
                        "information_ratio": available_bench.get("information_ratio") if available_bench else None,
                        "survivorship_bias_risk": "high",
                        "pit_quality": "degraded",
                        "price_filter": row.get("price_filter") or {},
                        "metrics_sanity": sanity,
                        "warnings": list(dict.fromkeys([*(row.get("warnings") or []), *(benchmark.get("warnings") or [])])),
                    }
                    if sanity.get("warnings"):
                        result["warnings"] = list(dict.fromkeys([*(result.get("warnings") or []), *(sanity.get("warnings") or [])]))
                    results.append(result)
    best = _best(results, "sharpe")
    capital_summary = {
        "best_initial_cash": int(_best(results, "sharpe").get("initial_cash") or 0),
        "return_by_initial_cash": _group_summary(results, "initial_cash", "annualized_return_net"),
        "invested_ratio_by_initial_cash": _group_summary(results, "initial_cash", "invested_ratio_avg"),
        "unfilled_by_initial_cash": _group_summary(results, "initial_cash", "unfilled_due_to_lot_size_count"),
    }
    position_summary = {
        "best_max_positions": int(best.get("max_positions") or 0),
        "return_by_max_positions": _group_summary(results, "max_positions", "annualized_return_net"),
        "invested_ratio_by_max_positions": _group_summary(results, "max_positions", "invested_ratio_avg"),
        "drawdown_by_max_positions": _group_summary(results, "max_positions", "max_drawdown"),
        "unfilled_by_max_positions": _group_summary(results, "max_positions", "unfilled_due_to_lot_size_count"),
    }
    price_summary = {
        "best_max_price": float(best.get("max_price") or 0.0),
        "return_by_max_price": _group_summary(results, "max_price", "annualized_return_net"),
        "universe_count_by_max_price": {str(p): float(pd.Series([r.get("price_filter", {}).get("average_price_filtered_universe_count", 0) for r in results if r.get("max_price") == p]).mean()) for p in max_price_grid},
        "unfilled_by_max_price": _group_summary(results, "max_price", "unfilled_due_to_lot_size_count"),
    }
    invested_by_cash = capital_summary["invested_ratio_by_initial_cash"]
    lot_summary = {
        "total_unfilled_count": int(sum(int(r.get("unfilled_due_to_lot_size_count") or 0) for r in results)),
        "avg_invested_ratio_by_cash": invested_by_cash,
        "cash_drag_severity": cash_drag_severity(float(invested_by_cash.get("10000", 0.0))),
        "whether_10000_cash_is_too_small": float(invested_by_cash.get("10000", 0.0)) < 0.70,
    }
    ranking = {
        "by_sharpe": sorted(results, key=lambda r: _safe_float(r.get("sharpe")), reverse=True)[:10],
        "by_annualized_return_net": sorted(results, key=lambda r: _safe_float(r.get("annualized_return_net")), reverse=True)[:10],
        "by_max_drawdown": sorted(results, key=lambda r: _safe_float(r.get("max_drawdown")), reverse=True)[:10],
        "by_excess_return": sorted([r for r in results if r.get("excess_return") is not None], key=lambda r: _safe_float(r.get("excess_return")), reverse=True)[:10],
    }
    benchmark_available = any(r.get("benchmark_available") for r in results)
    expected_result_count = len(initial_cash_grid) * len(max_positions_grid) * len(max_price_grid) * len(strategies)
    missing_strategies = [name for name in (FULL_STRATEGIES if cfg.get("full") else DEFAULT_STRATEGIES) if name not in strategies]
    grid_warnings: list[str] = []
    if len(results) != expected_result_count:
        grid_warnings.append("incomplete_sensitivity_grid")
    if cfg.get("full") and len(strategies) != 6:
        grid_warnings.append("full_strategy_count_mismatch")
    sanity_warnings = list(dict.fromkeys([warning for row in results for warning in (row.get("metrics_sanity") or {}).get("warnings", [])]))
    warnings.extend(grid_warnings)
    warnings.extend(sanity_warnings)
    if smoke_run and write_latest:
        warnings.append("smoke_run_requested_canonical_latest_write")
    status = "passed"
    if grid_warnings:
        status = "failed"
    elif warnings:
        status = "warning"
    payload = {
        "ok": True,
        "status": status,
        "task_type": "capital_lot_sensitivity",
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "package_version": PACKAGE_VERSION,
        "app_version": APP_VERSION,
        "api_schema_version": API_SCHEMA_VERSION,
        "research_only": True,
        "network": False,
        "historical_listings_exists": True,
        "historical_listings_path": _rel(listings),
        "historical_listings_source": "price_csv_coverage",
        "pit_mode": "price_coverage",
        "report_provenance": {
            "command": "strategy-viability capital-lot-sensitivity",
            "args": {k: str(v) for k, v in cfg.items()},
            "profile": "low_price",
            "pit_mode": "price_coverage",
            "strategies": strategies,
            "full": bool(cfg.get("full")),
            "write_latest": write_latest,
            "smoke_run": smoke_run,
            "generated_at": datetime.now().isoformat(timespec="seconds"),
        },
        "pit_quality": "degraded",
        "survivorship_bias_risk": "high",
        "benchmark_available": benchmark_available,
        "historical_st_period_available": False,
        "st_filter_quality": "degraded",
        "low_price_st_risk_warning": True,
        "grid": default_capital_lot_grid(),
        "strategies": strategies,
        "strategy_list": strategies,
        "strategy_count": len(strategies),
        "result_count": len(results),
        "expected_result_count": expected_result_count,
        "missing_strategies": missing_strategies,
        "capital_lot_result_count_status": "passed" if len(results) == expected_result_count else "failed",
        "drawdown_sanity_status": "failed" if any("drawdown_sanity_failed" in (row.get("metrics_sanity") or {}).get("warnings", []) for row in results) else "passed",
        "sharpe_sanity_status": "failed" if any("suspicious_sharpe_ratio" in (row.get("metrics_sanity") or {}).get("warnings", []) for row in results) else "passed",
        "results": results,
        "best_strategy": best.get("strategy"),
        "best_config": best,
        "capital_sensitivity_summary": capital_summary,
        "position_count_sensitivity_summary": position_summary,
        "max_price_sensitivity_summary": price_summary,
        "lot_constraint_summary": lot_summary,
        "strategy_ranking": ranking,
        "warnings": list(dict.fromkeys(warnings)),
        "warning_count": len(list(dict.fromkeys(warnings))),
        "warnings_top": list(dict.fromkeys(warnings))[:10],
        "output_paths": {},
        "disclaimer": DISCLAIMER,
    }
    payload.update(decide_capital_lot_sensitivity(payload))
    return _json_safe(payload)


def render_capital_lot_sensitivity_markdown(payload: dict[str, Any]) -> str:
    best = payload.get("best_config") or {}
    lines = [
        "# QuantDesk v3.7.0.4 Capital / Lot Sensitivity Report",
        "",
        DISCLAIMER,
        "",
        "## 1. Executive Summary",
        "",
        f"- best_strategy: `{payload.get('best_strategy')}`",
        f"- best_config: cash={best.get('initial_cash')} positions={best.get('max_positions')} max_price={best.get('max_price')}",
        f"- whether_10000_cash_is_too_small: `{(payload.get('lot_constraint_summary') or {}).get('whether_10000_cash_is_too_small')}`",
        f"- decision: `{payload.get('decision')}`",
        f"- confidence: `{payload.get('confidence')}`",
        "",
        "## 3. Data / PIT Status",
        "",
        f"- historical_listings exists: `{payload.get('historical_listings_exists')}`",
        f"- source: `{payload.get('historical_listings_source')}`",
        f"- pit_quality: `{payload.get('pit_quality')}`",
        f"- survivorship_bias_risk: `{payload.get('survivorship_bias_risk')}`",
        f"- benchmark_available: `{payload.get('benchmark_available')}`",
        "",
        "## 4. Experiment Design",
        "",
        f"- strategies: `{', '.join(payload.get('strategies') or [])}`",
        f"- initial_cash grid: `{(payload.get('grid') or {}).get('initial_cash')}`",
        f"- max_positions grid: `{(payload.get('grid') or {}).get('max_positions')}`",
        f"- max_price grid: `{(payload.get('grid') or {}).get('max_price')}`",
        "- lot_size: `100`",
        "- rebalance: `monthly`",
        "- price filter timing: `signal_date`, no latest-close prefilter",
        "",
        "## 5. Capital Sensitivity",
        "",
        "| initial_cash | return | invested_ratio | unfilled |",
        "|--:|--:|--:|--:|",
    ]
    cap = payload.get("capital_sensitivity_summary") or {}
    for cash, ret in (cap.get("return_by_initial_cash") or {}).items():
        inv = (cap.get("invested_ratio_by_initial_cash") or {}).get(cash, 0)
        unf = (cap.get("unfilled_by_initial_cash") or {}).get(cash, 0)
        lines.append(f"| {cash} | {float(ret):.2%} | {float(inv):.2%} | {float(unf):.1f} |")
    lines.extend([
        "",
        "## 8. Strategy Comparison",
        "",
        "| strategy | cash | positions | max_price | annual net | max drawdown | sharpe | invested | unfilled |",
        "|:--|--:|--:|--:|--:|--:|--:|--:|--:|",
    ])
    for row in (payload.get("strategy_ranking") or {}).get("by_sharpe", [])[:12]:
        lines.append(
            f"| {row.get('strategy')} | {row.get('initial_cash')} | {row.get('max_positions')} | {row.get('max_price')} | "
            f"{float(row.get('annualized_return_net') or 0):.2%} | {float(row.get('max_drawdown') or 0):.2%} | "
            f"{float(row.get('sharpe') or 0):.2f} | {float(row.get('invested_ratio_avg') or 0):.2%} | {row.get('unfilled_due_to_lot_size_count')} |"
        )
    lines.extend([
        "",
        "## 11. Decision",
        "",
        f"- decision: `{payload.get('decision')}`",
        f"- confidence: `{payload.get('confidence')}`",
        f"- reasons: `{payload.get('reasons')}`",
        f"- next_recommended_version: `{payload.get('next_recommended_version')}`",
        "",
        "## 12. Limitations",
        "",
        "- historical_listings is price_csv_coverage degraded PIT.",
        "- list_date is first available local price date, not official listing date.",
        "- historical ST periods and delist history are unavailable.",
        "- Research only; not investment advice.",
    ])
    return "\n".join(lines) + "\n"


def write_capital_lot_sensitivity_outputs(payload: dict[str, Any], *, result_path: str | Path = RESULT_JSON, report_path: str | Path = REPORT_MD) -> dict[str, Any]:
    provenance = payload.get("report_provenance") or {}
    if provenance.get("smoke_run") and not provenance.get("write_latest"):
        result_path = PROJECT_ROOT / "results" / "smoke" / "capital_lot_sensitivity_smoke_latest.json"
        report_path = PROJECT_ROOT / "reports" / "smoke" / "capital_lot_sensitivity_smoke_latest.md"
    result = Path(result_path)
    report = Path(report_path)
    result.parent.mkdir(parents=True, exist_ok=True)
    report.parent.mkdir(parents=True, exist_ok=True)
    payload = dict(payload)
    payload["output_paths"] = {"json": _rel(result), "markdown": _rel(report)}
    result.write_text(json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
    report.write_text(render_capital_lot_sensitivity_markdown(payload), encoding="utf-8")
    return payload
