from __future__ import annotations

import math
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from quantdesk.research._serialization import json_safe as _json_safe

import pandas as pd

from quantdesk.factor_evaluation import PIT_UNIVERSE_WARNING, pit_universe_assumption
from quantdesk.point_in_time_universe import (
    DEFAULT_LISTINGS_CSV,
    load_historical_listings,
    summarize_pit_universe_status,
)
from quantdesk.shared import now_text, read_json, write_json
from quantdesk.version import API_SCHEMA_VERSION, APP_VERSION, PACKAGE_VERSION

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DISCLAIMER = (
    "Research diagnostics only. Not investment advice. Research dashboard does "
    "not change default screen, backtest, daily workflow, trading signals, "
    "scoring weights, broker boundaries, or real orders."
)
BASE_ASSUMPTIONS = [
    "Research diagnostics only.",
    "Dashboard output does not generate trading advice.",
]


def _dashboard_assumptions(pit_status: dict[str, Any] | None = None) -> list[str]:
    return [*BASE_ASSUMPTIONS, pit_universe_assumption(pit_status)]

MODULES = {
    "factor_evaluation": {
        "task_type": "factor_evaluation",
        "json": "results/factor_health_latest.json",
        "report": "reports/factor_evaluation_report_latest.md",
        "missing_warning": "missing_factor_health",
        "run_command": "python scripts/qd.py factor-eval --network false --summary true",
        "label": "Factor Evaluation",
    },
    "factor_combination": {
        "task_type": "factor_combination",
        "json": "results/combined_score_latest.json",
        "report": "reports/combined_score_report_latest.md",
        "missing_warning": "missing_combined_score",
        "run_command": "python scripts/qd.py factor-combine --network false --summary true",
        "label": "Factor Combination",
    },
    "combined_score_backtest": {
        "task_type": "combined_score_backtest",
        "json": "results/combined_score_backtest_latest.json",
        "report": "reports/combined_score_backtest_report_latest.md",
        "missing_warning": "missing_backtest",
        "run_command": "python scripts/qd.py combined-score-backtest --network false --summary true",
        "label": "Combined Score Backtest",
    },
    "sector_rotation": {
        "task_type": "sector_rotation",
        "json": "results/sector_rotation_latest.json",
        "report": "reports/sector_rotation_report_latest.md",
        "missing_warning": "missing_sector_rotation",
        "run_command": "python scripts/qd.py sector-rotation --network false --summary true",
        "label": "Sector Rotation",
    },
}


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        number = float(value)
    except Exception:
        return default
    return number if math.isfinite(number) else default


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(float(value))
    except Exception:
        return default


def _as_dict(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def _as_list(value: Any) -> list[Any]:
    return list(value) if isinstance(value, list) else []


def _project_path(path: str | Path) -> Path:
    target = Path(path)
    return target if target.is_absolute() else PROJECT_ROOT / target


def _display_path(path: str | Path) -> str:
    target = _project_path(path)
    try:
        return str(target.relative_to(PROJECT_ROOT)).replace("\\", "/")
    except Exception:
        return str(target).replace("\\", "/")


def _parse_dt(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        return pd.to_datetime(value, errors="coerce").to_pydatetime()
    except Exception:
        return None


def load_latest_research_payloads(config: dict[str, Any] | None = None) -> dict[str, Any]:
    config = config or {}
    inputs = _as_dict(config.get("inputs"))
    payloads: dict[str, Any] = {"payloads": {}, "warnings": []}
    for name, meta in MODULES.items():
        path = _project_path(inputs.get(f"{name}_json") or inputs.get(meta["json"].split("/")[-1].replace("_latest.json", "_json")) or meta["json"])
        if name == "factor_evaluation":
            path = _project_path(inputs.get("factor_health_json") or meta["json"])
        elif name == "factor_combination":
            path = _project_path(inputs.get("combined_score_json") or meta["json"])
        elif name == "combined_score_backtest":
            path = _project_path(inputs.get("combined_score_backtest_json") or meta["json"])
        elif name == "sector_rotation":
            path = _project_path(inputs.get("sector_rotation_json") or meta["json"])
        if not path.exists():
            warning = f"{meta['missing_warning']}: {_display_path(path)}"
            payloads["warnings"].append(warning)
            payloads["payloads"][name] = {"available": False, "path": _display_path(path), "payload": {}, "warnings": [warning]}
            continue
        loaded = read_json(path, None)
        if not isinstance(loaded, dict):
            warning = f"invalid_json:{name}:{_display_path(path)}"
            payloads["warnings"].append(warning)
            payloads["payloads"][name] = {"available": False, "path": _display_path(path), "payload": {}, "warnings": [warning]}
            continue
        warnings = []
        task_type = str(loaded.get("task_type") or "")
        if task_type and task_type != meta["task_type"]:
            warnings.append(f"task_type_mismatch:{name}:{task_type}")
        payloads["payloads"][name] = {"available": True, "path": _display_path(path), "payload": loaded, "warnings": warnings}
        payloads["warnings"].extend(warnings)
    return payloads


def summarize_module_health(payloads: dict[str, Any], *, stale_hours: int = 48) -> dict[str, Any]:
    warnings: list[str] = []
    now = datetime.now()
    health: dict[str, Any] = {}
    payload_map = _as_dict(payloads.get("payloads"))
    for name, meta in MODULES.items():
        entry = _as_dict(payload_map.get(name))
        payload = _as_dict(entry.get("payload"))
        available = bool(entry.get("available"))
        module_warnings = list(entry.get("warnings") or [])
        status = "missing"
        generated_at = ""
        stale: bool | str = False
        if available:
            raw_status = str(payload.get("status") or "warning")
            generated_at = str(payload.get("generated_at") or "")
            dt = _parse_dt(generated_at)
            if dt is None:
                status = "warning"
                stale = "unknown"
                module_warnings.append(f"generated_at_missing:{name}")
            else:
                stale = now - dt > timedelta(hours=max(1, int(stale_hours)))
                status = "stale" if stale else raw_status
                if stale:
                    module_warnings.append(f"stale_payload:{name}:{generated_at}")
            warning_count = len(_as_list(payload.get("warnings"))) + len(module_warnings)
        else:
            warning_count = len(module_warnings) or 1
        output_paths = _as_dict(payload.get("output_paths"))
        latest_json = entry.get("path") or output_paths.get("latest_json") or meta["json"]
        latest_report = output_paths.get("latest_report") or output_paths.get("report") or meta["report"]
        key_metrics = _module_key_metrics(name, payload)
        summary_text = _module_summary_text(name, status, key_metrics)
        health[name] = {
            "available": available,
            "status": status,
            "generated_at": generated_at,
            "warning_count": int(warning_count),
            "key_metrics": key_metrics,
            "latest_json": str(latest_json).replace("\\", "/"),
            "latest_report": str(latest_report).replace("\\", "/"),
            "stale": stale,
            "summary_text": summary_text,
            "run_command": meta["run_command"],
        }
        warnings.extend(module_warnings)
    return {"module_health": health, "warnings": list(dict.fromkeys(str(item) for item in warnings if item))}


def _module_key_metrics(name: str, payload: dict[str, Any]) -> dict[str, Any]:
    if not payload:
        return {}
    if name == "factor_evaluation":
        summary = summarize_factor_health(payload)
        return {"factor_count": summary["factor_count"], "usable_count": summary["usable_count"], "weak_count": summary["weak_count"]}
    if name == "factor_combination":
        summary = summarize_combined_score(payload)
        return {"scored_count": summary["scored_count"], "latest_symbol_count": summary["latest_symbol_count"], "weight_method": summary["weight_method"]}
    if name == "combined_score_backtest":
        summary = summarize_backtest(payload)
        return {"net_return": summary["net_return"], "sharpe_ratio": summary["sharpe_ratio"], "max_drawdown": summary["max_drawdown"]}
    if name == "sector_rotation":
        summary = summarize_sector_rotation(payload)
        return {"sector_count": summary["sector_count"], "hot_sector_count": summary["hot_sector_count"], "improving_sector_count": summary["improving_sector_count"]}
    return {}


def _module_summary_text(name: str, status: str, metrics: dict[str, Any]) -> str:
    if name == "factor_evaluation":
        return f"{status}: {metrics.get('usable_count', 0)} usable factors, {metrics.get('weak_count', 0)} weak factors."
    if name == "factor_combination":
        return f"{status}: {metrics.get('scored_count', 0)} scored rows using {metrics.get('weight_method') or '-'}."
    if name == "combined_score_backtest":
        return f"{status}: Sharpe={metrics.get('sharpe_ratio', 0)}, MaxDD={metrics.get('max_drawdown', 0)}."
    if name == "sector_rotation":
        return f"{status}: {metrics.get('hot_sector_count', 0)} hot sectors, {metrics.get('improving_sector_count', 0)} improving."
    return status


def summarize_factor_health(payload: dict[str, Any] | None) -> dict[str, Any]:
    if not payload:
        return {"factor_count": 0, "strong_count": 0, "usable_count": 0, "weak_count": 0, "invalid_count": 0, "insufficient_data_count": 0, "best_factors": [], "weakest_factors": [], "top_rank_ic_factors": [], "main_warnings": ["missing_factor_health"]}
    metrics = _as_dict(payload.get("factor_metrics"))
    rows = []
    counts = {"strong_count": 0, "usable_count": 0, "weak_count": 0, "invalid_count": 0, "insufficient_data_count": 0}
    for name, raw in metrics.items():
        item = _as_dict(raw)
        status = str(item.get("health_status") or "")
        if status == "strong":
            counts["strong_count"] += 1
        elif status == "usable":
            counts["usable_count"] += 1
        elif status == "weak":
            counts["weak_count"] += 1
        elif status == "invalid":
            counts["invalid_count"] += 1
        elif status == "insufficient_data":
            counts["insufficient_data_count"] += 1
        rows.append({"factor": str(name), "rank_ic_mean": _safe_float(item.get("rank_ic_mean")), "ic_mean": _safe_float(item.get("ic_mean")), "health_status": status})
    best = sorted(rows, key=lambda item: abs(_safe_float(item.get("rank_ic_mean"))), reverse=True)
    weakest = sorted(rows, key=lambda item: _safe_float(item.get("rank_ic_mean")))
    return {
        "factor_count": len(metrics),
        **counts,
        "best_factors": best[:3],
        "weakest_factors": weakest[:3],
        "top_rank_ic_factors": best[:5],
        "main_warnings": _as_list(payload.get("warnings"))[:5],
    }


def summarize_combined_score(payload: dict[str, Any] | None) -> dict[str, Any]:
    if not payload:
        return {"factor_count": 0, "scored_count": 0, "latest_date": "", "latest_symbol_count": 0, "score_mean": 0.0, "score_std": 0.0, "top_candidates_preview": [], "weight_method": "", "neutralization_status": {}, "main_warnings": ["missing_combined_score"]}
    config = _as_dict(payload.get("config"))
    score = _as_dict(payload.get("score_summary"))
    top = _as_list(payload.get("top_candidates"))
    warnings = _as_list(payload.get("warnings"))
    if top and not payload.get("score_panel"):
        warnings = list(warnings) + ["score_panel_unavailable: top_candidates only; dashboard uses preview rankings"]
    neutralization = _as_dict(payload.get("neutralization"))
    return {
        "factor_count": len(_as_list(config.get("factors"))),
        "scored_count": _safe_int(score.get("score_count")),
        "latest_date": str(score.get("latest_date") or payload.get("target_date") or ""),
        "latest_symbol_count": _safe_int(score.get("latest_symbol_count")),
        "score_mean": _safe_float(score.get("score_mean")),
        "score_std": _safe_float(score.get("score_std")),
        "top_candidates_preview": top[:5],
        "weight_method": str(config.get("weight_method") or ""),
        "neutralization_status": {
            "industry": neutralization.get("industry_status") or "",
            "market_cap": neutralization.get("market_cap_status") or "",
        },
        "main_warnings": warnings[:5],
    }


def summarize_backtest(payload: dict[str, Any] | None) -> dict[str, Any]:
    if not payload:
        return {"net_return": 0.0, "annualized_return": 0.0, "sharpe_ratio": 0.0, "max_drawdown": 0.0, "turnover_mean": 0.0, "rebalance_count": 0, "cost_total": 0.0, "latest_selected_preview": [], "assumptions": [], "main_warnings": ["missing_backtest"]}
    perf = _as_dict(payload.get("performance"))
    portfolio = _as_dict(payload.get("portfolio_summary"))
    config = _as_dict(payload.get("config"))
    warnings = _as_list(payload.get("warnings"))
    if bool(config.get("long_short")):
        warnings = list(warnings) + ["long_short_research_only: A-share short-sale constraints may make this non-executable"]
    return {
        "net_return": _safe_float(perf.get("net_return")),
        "annualized_return": _safe_float(perf.get("annualized_return")),
        "sharpe_ratio": _safe_float(perf.get("sharpe_ratio")),
        "max_drawdown": _safe_float(perf.get("max_drawdown")),
        "turnover_mean": _safe_float(perf.get("turnover_mean")),
        "rebalance_count": _safe_int(perf.get("rebalance_count")),
        "cost_total": _safe_float(perf.get("cost_total")),
        "latest_selected_preview": _as_list(portfolio.get("latest_selected_symbols"))[:5],
        "assumptions": _as_list(payload.get("assumptions")),
        "main_warnings": warnings[:5],
    }


def summarize_sector_rotation(payload: dict[str, Any] | None) -> dict[str, Any]:
    if not payload:
        return {"sector_count": 0, "hot_sector_count": 0, "improving_sector_count": 0, "weakening_sector_count": 0, "top_sectors_preview": [], "top_improving_preview": [], "weakening_preview": [], "benchmark_method": "", "main_warnings": ["missing_sector_rotation"]}
    universe = _as_dict(payload.get("universe"))
    rankings = _as_list(payload.get("sector_rankings"))
    improving = _as_list(payload.get("top_improving_sectors"))
    weakening = _as_list(payload.get("weakening_sectors"))
    hot_count = sum(1 for item in rankings if _as_dict(item).get("heat_status") == "hot")
    improving_count = sum(1 for item in rankings if _as_dict(item).get("heat_status") in {"hot", "improving"})
    weakening_count = sum(1 for item in rankings if _as_dict(item).get("heat_status") in {"weakening", "cold"})
    benchmark = _as_dict(payload.get("benchmark"))
    return {
        "sector_count": _safe_int(universe.get("sector_count"), len(rankings)),
        "hot_sector_count": hot_count,
        "improving_sector_count": improving_count,
        "weakening_sector_count": weakening_count,
        "top_sectors_preview": rankings[:5],
        "top_improving_preview": improving[:5],
        "weakening_preview": weakening[:5],
        "benchmark_method": str(benchmark.get("method") or _as_dict(payload.get("config")).get("benchmark_method") or ""),
        "main_warnings": _as_list(payload.get("warnings"))[:5],
    }


def build_research_dashboard_summary(module_summaries: dict[str, Any], *, pit_status: dict[str, Any] | None = None) -> dict[str, Any]:
    module_health = _as_dict(module_summaries.get("module_health"))
    factor = _as_dict(module_summaries.get("factor_health_summary"))
    combo = _as_dict(module_summaries.get("combined_score_summary"))
    backtest = _as_dict(module_summaries.get("backtest_summary"))
    sector = _as_dict(module_summaries.get("sector_rotation_summary"))
    missing = [name for name, item in module_health.items() if not _as_dict(item).get("available")]
    stale = [name for name, item in module_health.items() if _as_dict(item).get("stale") is True or _as_dict(item).get("status") == "stale"]
    failed = [name for name, item in module_health.items() if _as_dict(item).get("status") == "failed"]
    warning_modules = [name for name, item in module_health.items() if _as_dict(item).get("status") in {"warning", "stale", "missing"}]
    pit = _as_dict(pit_status)
    pit_available = bool(pit.get("pit_universe"))
    risk_flags = [] if pit_available else [PIT_UNIVERSE_WARNING]
    if missing:
        risk_flags.append("missing_modules: " + ", ".join(missing))
    if stale:
        risk_flags.append("stale_modules: " + ", ".join(stale))
    if _safe_int(factor.get("weak_count")) or _safe_int(factor.get("insufficient_data_count")):
        risk_flags.append("factor_health_has_weak_or_insufficient_factors")
    if _safe_int(combo.get("scored_count")) == 0:
        risk_flags.append("combined_score_has_no_scored_rows")
    if _safe_int(backtest.get("rebalance_count")) == 0:
        risk_flags.append("combined_score_backtest_has_no_rebalances")
    if not sector.get("top_sectors_preview"):
        risk_flags.append("sector_rotation_has_no_sector_rankings")
    key_findings = [
        f"Factor evaluation: {_safe_int(factor.get('usable_count'))} usable, {_safe_int(factor.get('weak_count'))} weak.",
        f"Combined score: {_safe_int(combo.get('scored_count'))} scored rows, weight_method={combo.get('weight_method') or '-'}.",
        f"Backtest: Sharpe={_safe_float(backtest.get('sharpe_ratio')):.4f}, MaxDD={_safe_float(backtest.get('max_drawdown')):.4f}.",
        f"Sector rotation: {_safe_int(sector.get('hot_sector_count'))} hot sectors, {_safe_int(sector.get('improving_sector_count'))} improving.",
    ]
    actions = []
    for name in missing:
        actions.append(MODULES[name]["run_command"])
    if stale:
        actions.append("Re-run stale research modules with updated local daily data.")
    if _safe_int(factor.get("weak_count")):
        actions.append("Review weak factors before changing research weights.")
    if _safe_int(backtest.get("rebalance_count")) < 5:
        actions.append("Validate combined_score with longer history before trusting ranking diagnostics.")
    if not sector.get("top_sectors_preview"):
        actions.append("Check sector map coverage and rerun sector-rotation.")
    if pit_available:
        actions.append("Review PIT listing coverage before enabling PIT filters in research modules.")
    else:
        actions.append("Consider PIT universe reconstruction to reduce survivorship-bias risk.")
    if failed:
        research_status = "failed"
        readiness = "blocked"
    elif missing or stale or warning_modules:
        research_status = "warning"
        readiness = "insufficient_data" if len(missing) >= 2 else "watch"
    else:
        research_status = "passed"
        readiness = "ready"
    return {
        "research_status": research_status,
        "readiness_level": readiness,
        "key_findings": key_findings,
        "risk_flags": list(dict.fromkeys(risk_flags)),
        "suggested_next_actions": list(dict.fromkeys(actions)),
        "missing_modules": missing,
        "stale_modules": stale,
    }


def build_factor_research_dashboard(
    payloads: dict[str, Any] | None = None,
    *,
    stale_hours: int = 48,
    synthetic_payloads: bool = False,
    pit_status: dict[str, Any] | None = None,
) -> dict[str, Any]:
    payloads = payloads or load_latest_research_payloads({})
    payload_map = _as_dict(payloads.get("payloads"))
    health_result = summarize_module_health(payloads, stale_hours=stale_hours)
    module_health = _as_dict(health_result.get("module_health"))
    factor = summarize_factor_health(_as_dict(_as_dict(payload_map.get("factor_evaluation")).get("payload")))
    combo = summarize_combined_score(_as_dict(_as_dict(payload_map.get("factor_combination")).get("payload")))
    backtest = summarize_backtest(_as_dict(_as_dict(payload_map.get("combined_score_backtest")).get("payload")))
    sector = summarize_sector_rotation(_as_dict(_as_dict(payload_map.get("sector_rotation")).get("payload")))
    module_summaries = {
        "module_health": module_health,
        "factor_health_summary": factor,
        "combined_score_summary": combo,
        "backtest_summary": backtest,
        "sector_rotation_summary": sector,
    }
    if pit_status is None:
        listings, _pit_warnings = load_historical_listings(DEFAULT_LISTINGS_CSV)
        pit_status = summarize_pit_universe_status(listings, DEFAULT_LISTINGS_CSV)
    dashboard = build_research_dashboard_summary(module_summaries, pit_status=pit_status)
    warnings = []
    warnings.extend(_as_list(payloads.get("warnings")))
    warnings.extend(_as_list(health_result.get("warnings")))
    for summary in (factor, combo, backtest, sector):
        warnings.extend(_as_list(summary.get("main_warnings"))[:3])
    warnings.extend(_as_list(_as_dict(pit_status).get("warnings"))[:3])
    target_dates = []
    for entry in payload_map.values():
        payload = _as_dict(_as_dict(entry).get("payload"))
        if payload.get("target_date"):
            target_dates.append(str(payload.get("target_date")))
    available_count = sum(1 for item in module_health.values() if _as_dict(item).get("available"))
    status = dashboard["research_status"]
    if synthetic_payloads:
        status = "warning"
        warnings.append("synthetic_test_data: test-only research dashboard payloads; do not use as production evidence")
    if available_count == 0:
        status = "warning"
        warnings.append("all_research_modules_missing_or_unreadable")
    payload = {
        "ok": True,
        "status": status,
        "task_type": "factor_research_dashboard",
        "generated_at": now_text(),
        "app_version": APP_VERSION,
        "package_version": PACKAGE_VERSION,
        "schema_version": API_SCHEMA_VERSION,
        "api_schema_version": API_SCHEMA_VERSION,
        "data_source": "synthetic_test_data" if synthetic_payloads else "local_research_payloads",
        "synthetic_payloads": bool(synthetic_payloads),
        "target_date": max(target_dates) if target_dates else "",
        "network": False,
        "config": {
            "stale_hours": int(stale_hours),
            "required_modules": list(MODULES),
        },
        "module_health": module_health,
        "factor_health_summary": factor,
        "combined_score_summary": combo,
        "backtest_summary": backtest,
        "sector_rotation_summary": sector,
        "pit_status": _as_dict(pit_status),
        "dashboard_summary": dashboard,
        "warnings": list(dict.fromkeys(str(item) for item in warnings if item)),
        "assumptions": _dashboard_assumptions(_as_dict(pit_status)),
        "output_paths": {},
        "suggested_next_action": "; ".join(dashboard.get("suggested_next_actions") or []) or "Review research dashboard report.",
        "disclaimer": DISCLAIMER,
    }
    return _json_safe(payload)


def build_research_dashboard_report(payload: dict[str, Any]) -> str:
    health = _as_dict(payload.get("module_health"))
    dashboard = _as_dict(payload.get("dashboard_summary"))
    factor = _as_dict(payload.get("factor_health_summary"))
    combo = _as_dict(payload.get("combined_score_summary"))
    backtest = _as_dict(payload.get("backtest_summary"))
    sector = _as_dict(payload.get("sector_rotation_summary"))
    pit = _as_dict(payload.get("pit_status"))
    paths = _as_dict(payload.get("output_paths"))
    lines = [
        "# QuantDesk Factor Research Dashboard",
        "",
        f"- generated_at: `{payload.get('generated_at') or '-'}`",
        f"- app_version: `{payload.get('app_version') or '-'}`",
        f"- data_source: `{payload.get('data_source') or '-'}`",
        "",
        "## Dashboard Status",
        "",
        f"- status: `{payload.get('status') or '-'}`",
        f"- research_status: `{dashboard.get('research_status') or '-'}`",
        f"- readiness_level: `{dashboard.get('readiness_level') or '-'}`",
        "",
        "## Module Health",
        "",
        "| Module | Status | Generated At | Warnings | Report |",
        "|---|---|---|---:|---|",
    ]
    for name, meta in MODULES.items():
        item = _as_dict(health.get(name))
        lines.append(f"| {meta['label']} | {item.get('status') or 'missing'} | {item.get('generated_at') or '-'} | {int(item.get('warning_count') or 0)} | {item.get('latest_report') or meta['report']} |")
    lines.extend([
        "",
        "## Factor Health",
        "",
        f"- factor_count: `{factor.get('factor_count', 0)}`",
        f"- strong_count: `{factor.get('strong_count', 0)}`",
        f"- usable_count: `{factor.get('usable_count', 0)}`",
        f"- weak_count: `{factor.get('weak_count', 0)}`",
        "",
        "## Combined Score",
        "",
        f"- scored_count: `{combo.get('scored_count', 0)}`",
        f"- latest_date: `{combo.get('latest_date') or '-'}`",
        f"- latest_symbol_count: `{combo.get('latest_symbol_count', 0)}`",
        f"- weight_method: `{combo.get('weight_method') or '-'}`",
        "- top_candidates_preview is a research ranking only, not trading advice.",
        "",
        "## Combined Score Backtest",
        "",
        f"- net_return: `{_safe_float(backtest.get('net_return')):.6f}`",
        f"- annualized_return: `{_safe_float(backtest.get('annualized_return')):.6f}`",
        f"- sharpe_ratio: `{_safe_float(backtest.get('sharpe_ratio')):.6f}`",
        f"- max_drawdown: `{_safe_float(backtest.get('max_drawdown')):.6f}`",
        f"- turnover_mean: `{_safe_float(backtest.get('turnover_mean')):.6f}`",
        f"- rebalance_count: `{backtest.get('rebalance_count', 0)}`",
        "",
        "## Sector Rotation",
        "",
        f"- sector_count: `{sector.get('sector_count', 0)}`",
        f"- hot_sector_count: `{sector.get('hot_sector_count', 0)}`",
        f"- improving_sector_count: `{sector.get('improving_sector_count', 0)}`",
        f"- weakening_sector_count: `{sector.get('weakening_sector_count', 0)}`",
        f"- benchmark_method: `{sector.get('benchmark_method') or '-'}`",
        "",
        "## PIT Universe",
        "",
        f"- available: `{bool(pit.get('available', False))}`",
        f"- pit_universe: `{bool(pit.get('pit_universe', False))}`",
        f"- symbol_count: `{pit.get('symbol_count', 0)}`",
        f"- list_date_coverage: `{pit.get('list_date_coverage', 0.0)}`",
        "",
        "## Key Findings",
        "",
    ])
    lines.extend([f"- {item}" for item in _as_list(dashboard.get("key_findings"))] or ["- none"])
    lines.extend(["", "## Risk Flags", ""])
    lines.extend([f"- {item}" for item in _as_list(dashboard.get("risk_flags"))] or ["- none"])
    lines.extend(["", "## Suggested Next Actions", ""])
    lines.extend([f"- {item}" for item in _as_list(dashboard.get("suggested_next_actions"))] or ["- none"])
    missing = _as_list(dashboard.get("missing_modules"))
    stale = _as_list(dashboard.get("stale_modules"))
    lines.extend(["", "## Missing / Stale Modules", ""])
    lines.extend([f"- missing: {', '.join(missing) if missing else 'none'}", f"- stale: {', '.join(stale) if stale else 'none'}"])
    if missing:
        for name in missing:
            if name in MODULES:
                lines.append(f"- run: `{MODULES[name]['run_command']}`")
    lines.extend(["", "## Output Links", ""])
    for key, value in paths.items():
        lines.append(f"- {key}: `{value}`")
    lines.extend(["", "## Assumptions", ""])
    lines.extend([f"- {item}" for item in _as_list(payload.get("assumptions"))] or ["- none"])
    pit_note = (
        "Point-in-time universe metadata is available; filtering remains opt-in for legacy research workflows."
        if pit.get("pit_universe")
        else PIT_UNIVERSE_WARNING
    )
    lines.extend(["", "## PIT Universe Limitation", "", pit_note, "", "## Disclaimer", "", str(payload.get("disclaimer") or DISCLAIMER)])
    return "\n".join(lines) + "\n"


def write_research_dashboard_outputs(
    payload: dict[str, Any],
    *,
    output_dir: str | Path = "results",
    report_dir: str | Path = "reports",
) -> dict[str, Any]:
    output_path = Path(output_dir)
    report_path = Path(report_dir)
    output_path.mkdir(parents=True, exist_ok=True)
    report_path.mkdir(parents=True, exist_ok=True)
    stamp = now_text().replace("-", "").replace(":", "").replace("T", "_").replace(" ", "_")
    latest_json = output_path / "research_dashboard_latest.json"
    timestamp_json = output_path / f"research_dashboard_{stamp}.json"
    report = report_path / f"research_dashboard_report_{stamp}.md"
    latest_report = report_path / "research_dashboard_report_latest.md"
    payload = _json_safe(dict(payload))
    payload["output_paths"] = {
        "latest_json": str(latest_json).replace("\\", "/"),
        "timestamp_json": str(timestamp_json).replace("\\", "/"),
        "report": str(report).replace("\\", "/"),
        "latest_report": str(latest_report).replace("\\", "/"),
    }
    write_json(latest_json, payload)
    write_json(timestamp_json, payload)
    text = build_research_dashboard_report(payload)
    report.write_text(text, encoding="utf-8")
    latest_report.write_text(text, encoding="utf-8")
    return payload


def build_synthetic_research_payloads() -> dict[str, Any]:
    generated = now_text()
    return {
        "payloads": {
            "factor_evaluation": {"available": True, "path": "synthetic/factor_health_latest.json", "warnings": [], "payload": {
                "task_type": "factor_evaluation", "status": "warning", "generated_at": generated, "target_date": "2026-05-22",
                "warnings": [PIT_UNIVERSE_WARNING],
                "output_paths": {"latest_json": "results/factor_health_latest.json", "latest_report": "reports/factor_evaluation_report_latest.md"},
                "factor_metrics": {
                    "momentum_20d": {"rank_ic_mean": 0.08, "ic_mean": 0.05, "health_status": "usable"},
                    "volatility_20d": {"rank_ic_mean": -0.02, "ic_mean": -0.01, "health_status": "weak"},
                    "liquidity_20d": {"rank_ic_mean": 0.04, "ic_mean": 0.03, "health_status": "strong"},
                },
            }},
            "factor_combination": {"available": True, "path": "synthetic/combined_score_latest.json", "warnings": [], "payload": {
                "task_type": "factor_combination", "status": "warning", "generated_at": generated, "target_date": "2026-05-22",
                "warnings": [PIT_UNIVERSE_WARNING],
                "config": {"factors": ["momentum_20d", "volatility_20d", "liquidity_20d"], "weight_method": "rank_icir"},
                "score_summary": {"score_count": 300, "latest_date": "2026-05-22", "latest_symbol_count": 30, "score_mean": 0.01, "score_std": 0.8},
                "neutralization": {"industry_status": "applied", "market_cap_status": "applied"},
                "top_candidates": [{"symbol": "000001", "combined_score": 1.2, "coverage": 1.0}],
                "output_paths": {"latest_json": "results/combined_score_latest.json", "latest_report": "reports/combined_score_report_latest.md"},
            }},
            "combined_score_backtest": {"available": True, "path": "synthetic/combined_score_backtest_latest.json", "warnings": [], "payload": {
                "task_type": "combined_score_backtest", "status": "warning", "generated_at": generated, "target_date": "2026-05-22",
                "warnings": [], "config": {"long_short": False},
                "performance": {"net_return": 0.12, "annualized_return": 0.08, "sharpe_ratio": 0.74, "max_drawdown": -0.113, "turnover_mean": 0.38, "rebalance_count": 20, "cost_total": 0.03},
                "portfolio_summary": {"latest_selected_symbols": ["000001", "000002"]},
                "assumptions": _dashboard_assumptions(),
                "output_paths": {"latest_json": "results/combined_score_backtest_latest.json", "latest_report": "reports/combined_score_backtest_report_latest.md"},
            }},
            "sector_rotation": {"available": True, "path": "synthetic/sector_rotation_latest.json", "warnings": [], "payload": {
                "task_type": "sector_rotation", "status": "warning", "generated_at": generated, "target_date": "2026-05-22",
                "warnings": [], "benchmark": {"method": "equal_weight_universe"},
                "universe": {"sector_count": 3},
                "sector_rankings": [
                    {"sector_name": "Healthcare", "heat_score": 1.2, "heat_status": "hot", "rank_improvement_5d": 2},
                    {"sector_name": "Semiconductor", "heat_score": 0.6, "heat_status": "improving", "rank_improvement_5d": 1},
                    {"sector_name": "Property", "heat_score": -0.6, "heat_status": "weakening", "rank_improvement_5d": -2},
                ],
                "top_improving_sectors": [{"sector_name": "Healthcare", "rank_improvement_5d": 2}],
                "weakening_sectors": [{"sector_name": "Property", "rank_improvement_5d": -2}],
                "output_paths": {"latest_json": "results/sector_rotation_latest.json", "latest_report": "reports/sector_rotation_report_latest.md"},
            }},
        },
        "warnings": [],
    }
