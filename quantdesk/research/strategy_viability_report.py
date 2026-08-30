from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

from quantdesk.research.strategy_viability_data_precheck import (
    PROJECT_ROOT,
    build_strategy_viability_precheck,
    write_precheck_outputs,
)
from quantdesk.research.strategy_viability_runner import (
    build_report_truthfulness,
    run_strategy_viability_baselines,
    write_baseline_outputs,
)
from quantdesk.version import API_SCHEMA_VERSION, APP_VERSION, PACKAGE_VERSION

RESULT_JSON = PROJECT_ROOT / "results" / "strategy_viability_latest.json"
REPORT_MD = PROJECT_ROOT / "reports" / "strategy_viability_latest.md"
DISCLAIMER = "Research-only Strategy Viability Report. No broker, no real orders, no automatic trading."


def _rel(path: str | Path) -> str:
    target = Path(path)
    try:
        return str(target.relative_to(PROJECT_ROOT)).replace("\\", "/")
    except Exception:
        return str(target).replace("\\", "/")


def _load_json(path: Path) -> dict[str, Any] | None:
    try:
        if path.exists():
            data = json.loads(path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else None
    except Exception:
        return None
    return None


def build_strategy_viability_report(config: dict | None = None) -> dict[str, Any]:
    cfg = config or {}
    write_latest = bool(cfg.get("write_latest", True))
    smoke_run = bool(cfg.get("smoke_run", False))
    data_root = cfg.get("data_root")
    precheck = _load_json(PROJECT_ROOT / "results" / "strategy_viability_precheck_latest.json")
    if not precheck or cfg.get("refresh"):
        precheck = write_precheck_outputs(build_strategy_viability_precheck(cfg))
    baselines = _load_json(PROJECT_ROOT / "results" / "strategy_viability_baselines_latest.json")
    if not baselines or cfg.get("refresh"):
        baselines = write_baseline_outputs(run_strategy_viability_baselines(cfg))
    sensitivity = _load_json(PROJECT_ROOT / "results" / "capital_lot_sensitivity_latest.json")
    warnings = list(dict.fromkeys([*(precheck.get("warnings") or []), *(baselines.get("warnings") or []), *((sensitivity or {}).get("warnings") or [])]))
    decision = baselines.get("decision_gate") or {}
    # Latest-eligibility truthfulness: a report built from a temporary / fixture
    # / synthetic / unknown data root may be written as run-specific diagnostic
    # output but must never overwrite canonical *_latest evidence.
    truthfulness = build_report_truthfulness(
        data_root,
        write_latest=write_latest,
        smoke_run=smoke_run,
        dataset_symbol_count=baselines.get("dataset_symbol_count"),
        dataset_date_start=baselines.get("dataset_date_start"),
        dataset_date_end=baselines.get("dataset_date_end"),
    )
    if not truthfulness.get("latest_eligible") and write_latest:
        warnings = list(dict.fromkeys([*warnings, f"latest_write_blocked:{truthfulness.get('latest_write_blocked_reason')}"]))
    payload = {
        "ok": bool(precheck.get("ok", True) and baselines.get("ok", True)),
        "status": "warning" if warnings else "passed",
        "task_type": "strategy_viability_report",
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "package_version": PACKAGE_VERSION,
        "app_version": APP_VERSION,
        "api_schema_version": API_SCHEMA_VERSION,
        "research_only": True,
        "network": False,
        "profile": baselines.get("profile"),
        "initial_cash": baselines.get("initial_cash"),
        "max_positions": baselines.get("max_positions"),
        "max_position_pct": baselines.get("max_position_pct"),
        "rebalance": baselines.get("rebalance"),
        "top_n": baselines.get("top_n"),
        "lot_size": baselines.get("lot_size"),
        "price_filter_enabled": baselines.get("price_filter_enabled"),
        "price_filter_max_price": baselines.get("price_filter_max_price"),
        "price_filter_evaluated_on": baselines.get("price_filter_evaluated_on"),
        "pit_mode": baselines.get("pit_mode"),
        "report_provenance": {
            "command": "strategy-viability report",
            "args": {k: str(v) for k, v in cfg.items()},
            "profile": baselines.get("profile"),
            "pit_mode": baselines.get("pit_mode"),
            "strategies": [row.get("name") for row in (baselines.get("baselines") or [])],
            "full": True,
            "write_latest": write_latest,
            "smoke_run": smoke_run,
            "data_root_kind": truthfulness["data_root_kind"],
            "is_canonical_data_root": truthfulness["is_canonical_data_root"],
            "resolved_data_root": truthfulness["resolved_data_root"],
            "latest_eligible": truthfulness["latest_eligible"],
            "latest_write_blocked_reason": truthfulness["latest_write_blocked_reason"],
            "dataset_symbol_count": truthfulness["dataset_symbol_count"],
            "dataset_date_start": truthfulness["dataset_date_start"],
            "dataset_date_end": truthfulness["dataset_date_end"],
            "generated_at": datetime.now().isoformat(timespec="seconds"),
        },
        "data_root_kind": truthfulness["data_root_kind"],
        "is_canonical_data_root": truthfulness["is_canonical_data_root"],
        "latest_eligible": truthfulness["latest_eligible"],
        "latest_write_blocked_reason": truthfulness["latest_write_blocked_reason"],
        "execution_evidence": baselines.get("execution_evidence"),
        "baseline_distinctness_status": baselines.get("baseline_distinctness_status"),
        "unfilled_due_to_lot_size_count": baselines.get("unfilled_due_to_lot_size_count"),
        "average_cash_drag": baselines.get("average_cash_drag"),
        "invested_ratio_avg": baselines.get("invested_ratio_avg"),
        "data_precheck": {
            "total_csv_count": precheck.get("total_csv_count"),
            "schema_variant_count": precheck.get("schema_variant_count"),
            "nonstandard_schema_file_count": precheck.get("nonstandard_schema_file_count"),
            "chinese_column_file_count": precheck.get("chinese_column_file_count"),
            "survivorship_bias_risk": precheck.get("survivorship_bias_risk"),
            "st_delist_coverage": precheck.get("st_delist_coverage"),
            "benchmark_available": (precheck.get("benchmark") or {}).get("available"),
            "partial_daily_isolated": (precheck.get("partial_daily") or {}).get("isolated"),
        },
        "baselines": baselines.get("baselines") or [],
        "best_strategy": baselines.get("best_strategy"),
        "decision_gate": decision,
        "recommendation": decision.get("recommendation"),
        "capital_lot_sensitivity": {
            "available": bool(sensitivity),
            "decision": (sensitivity or {}).get("decision"),
            "confidence": (sensitivity or {}).get("confidence"),
            "best_strategy": (sensitivity or {}).get("best_strategy"),
            "best_config": (sensitivity or {}).get("best_config"),
            "capital_sensitivity_summary": (sensitivity or {}).get("capital_sensitivity_summary"),
            "lot_constraint_summary": (sensitivity or {}).get("lot_constraint_summary"),
            "output_paths": (sensitivity or {}).get("output_paths"),
        },
        "next_round_suggestions": [
            (sensitivity or {}).get("next_recommended_version") or "v3.7.1-new-strategy-direction-screening",
            "v3.7.1-strategy-refinement",
            "v3.7.1-point-in-time-fundamentals",
        ],
        "warnings": warnings,
        "errors": [],
        "warning_count": len(warnings),
        "warnings_top": warnings[:10],
        "output_paths": {},
        "disclaimer": DISCLAIMER,
    }
    return payload


def render_strategy_viability_markdown(payload: dict[str, Any]) -> str:
    pre = payload.get("data_precheck") or {}
    lines = [
        "# Strategy Viability Report",
        "",
        DISCLAIMER,
        "",
        "## Data Precheck",
        "",
        f"- total_csv_count: `{pre.get('total_csv_count')}`",
        f"- survivorship_bias_risk: `{pre.get('survivorship_bias_risk')}`",
        f"- schema_variant_count: `{pre.get('schema_variant_count')}`",
        f"- nonstandard_schema_file_count: `{pre.get('nonstandard_schema_file_count')}`",
        f"- ST / delist coverage: `{pre.get('st_delist_coverage')}`",
        f"- benchmark_available: `{pre.get('benchmark_available')}`",
        f"- partial_daily_isolated: `{pre.get('partial_daily_isolated')}`",
        "",
        "## Strategy Results",
        "",
        "| strategy | annual net | max drawdown | sharpe | turnover | net total |",
        "|:--|--:|--:|--:|--:|--:|",
    ]
    for row in payload.get("baselines") or []:
        m = row.get("metrics") or {}
        lines.append(
            f"| {row.get('name')} | {float(m.get('annual_return_net') or 0):.2%} | "
            f"{float(m.get('max_drawdown') or 0):.2%} | {float(m.get('sharpe') or 0):.2f} | "
            f"{float(m.get('average_turnover') or 0):.2f} | {float(m.get('total_return_net') or 0):.2%} |"
        )
    lines.extend([
        "",
        "## Decision Gate",
        "",
        f"- best_strategy: `{payload.get('best_strategy')}`",
        f"- recommendation: `{payload.get('recommendation')}`",
        f"- rationale: `{(payload.get('decision_gate') or {}).get('rationale')}`",
        "",
        "## Capital / Lot Sensitivity",
        "",
        f"- available: `{(payload.get('capital_lot_sensitivity') or {}).get('available')}`",
        f"- decision: `{(payload.get('capital_lot_sensitivity') or {}).get('decision')}`",
        f"- confidence: `{(payload.get('capital_lot_sensitivity') or {}).get('confidence')}`",
        f"- best_strategy: `{(payload.get('capital_lot_sensitivity') or {}).get('best_strategy')}`",
        "",
        "## Next Round",
        "",
    ])
    lines.extend(f"- {item}" for item in payload.get("next_round_suggestions") or [])
    lines.extend(["", "## Warnings", ""])
    warnings = payload.get("warnings") or []
    lines.extend([f"- {item}" for item in warnings] if warnings else ["- none"])
    return "\n".join(lines) + "\n"


def write_strategy_viability_report(payload: dict[str, Any], *, result_path: str | Path = RESULT_JSON, report_path: str | Path = REPORT_MD) -> dict[str, Any]:
    provenance = payload.get("report_provenance") or {}
    # A report that is not latest-eligible (temporary / fixture / synthetic /
    # unknown data root, or a smoke run) must never overwrite the canonical
    # *_latest evidence. Redirect it to a run-specific diagnostic path instead,
    # keyed by generated_at so concurrent diagnostic runs do not collide.
    latest_eligible = bool(payload.get("latest_eligible", provenance.get("latest_eligible", True)))
    is_smoke_run_only = bool(provenance.get("smoke_run") and not provenance.get("write_latest"))
    if is_smoke_run_only:
        result_path = PROJECT_ROOT / "results" / "smoke" / "strategy_viability_smoke_latest.json"
        report_path = PROJECT_ROOT / "reports" / "smoke" / "strategy_viability_smoke_latest.md"
    elif not latest_eligible:
        stamp = str(provenance.get("generated_at") or datetime.now().isoformat(timespec="seconds")).replace(":", "").replace("-", "").replace("T", "_")
        blocked_reason = str(payload.get("latest_write_blocked_reason") or provenance.get("latest_write_blocked_reason") or "not_latest_eligible")
        result_path = PROJECT_ROOT / "results" / "runs" / f"strategy_viability_run_{stamp}.json"
        report_path = PROJECT_ROOT / "reports" / "runs" / f"strategy_viability_run_{stamp}.md"
        payload = dict(payload)
        payload["latest_write_blocked"] = True
        payload["latest_write_blocked_reason"] = blocked_reason
    result = Path(result_path)
    report = Path(report_path)
    result.parent.mkdir(parents=True, exist_ok=True)
    report.parent.mkdir(parents=True, exist_ok=True)
    payload = dict(payload)
    payload["output_paths"] = {"json": _rel(result), "markdown": _rel(report)}
    result.write_text(json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
    report.write_text(render_strategy_viability_markdown(payload), encoding="utf-8")
    return payload
