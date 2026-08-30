from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from quantdesk.research.capital_lot_sensitivity import (
    run_capital_lot_sensitivity,
    write_capital_lot_sensitivity_outputs,
)
from quantdesk.research.strategy_pipeline_repair import build_repair_rerun_report, write_repair_rerun_outputs
from quantdesk.research.strategy_viability_data_precheck import (
    build_strategy_viability_precheck,
    write_precheck_outputs,
)
from quantdesk.research.strategy_viability_report import (
    build_strategy_viability_report,
    write_strategy_viability_report,
)
from quantdesk.research.strategy_viability_runner import run_strategy_viability_baselines, write_baseline_outputs
from quantdesk.shared import truthy


def _summary(payload: dict) -> dict:
    if payload.get("task_type") == "capital_lot_sensitivity":
        return {
            "ok": payload.get("ok", True),
            "status": payload.get("status", "passed"),
            "task_type": payload.get("task_type"),
            "generated_at": payload.get("generated_at"),
            "app_version": payload.get("app_version"),
            "result_count": payload.get("result_count"),
            "expected_result_count": payload.get("expected_result_count"),
            "strategy_count": payload.get("strategy_count"),
            "strategy_list": payload.get("strategy_list"),
            "missing_strategies": payload.get("missing_strategies"),
            "best_strategy": payload.get("best_strategy"),
            "best_config": payload.get("best_config"),
            "decision": payload.get("decision"),
            "confidence": payload.get("confidence"),
            "capital_sensitivity_summary": payload.get("capital_sensitivity_summary"),
            "position_count_sensitivity_summary": payload.get("position_count_sensitivity_summary"),
            "max_price_sensitivity_summary": payload.get("max_price_sensitivity_summary"),
            "lot_constraint_summary": payload.get("lot_constraint_summary"),
            "survivorship_bias_risk": payload.get("survivorship_bias_risk"),
            "pit_quality": payload.get("pit_quality"),
            "benchmark_available": payload.get("benchmark_available"),
            "warning_count": payload.get("warning_count", len(payload.get("warnings") or [])),
            "warnings_top": payload.get("warnings_top", list(payload.get("warnings") or [])[:10]),
            "output_paths": payload.get("output_paths", {}),
            "report_provenance": payload.get("report_provenance"),
            "disclaimer": payload.get("disclaimer"),
        }
    first_baseline = (payload.get("baselines") or [{}])[0] if isinstance(payload.get("baselines"), list) else {}
    first_metrics = first_baseline.get("metrics") if isinstance(first_baseline, dict) else {}
    if not isinstance(first_metrics, dict):
        first_metrics = {}
    return {
        "ok": payload.get("ok", True),
        "status": payload.get("status", "passed"),
        "task_type": payload.get("task_type"),
        "generated_at": payload.get("generated_at"),
        "app_version": payload.get("app_version"),
        "best_strategy": payload.get("best_strategy"),
        "recommendation": payload.get("recommendation") or (payload.get("decision_gate") or {}).get("recommendation"),
        "survivorship_bias_risk": payload.get("survivorship_bias_risk") or (payload.get("data_precheck") or {}).get("survivorship_bias_risk") or (payload.get("precheck_summary") or {}).get("survivorship_bias_risk"),
        "baseline_count": payload.get("baseline_count", len(payload.get("baselines") or [])),
        "profile": payload.get("profile") or first_baseline.get("profile"),
        "initial_cash": payload.get("initial_cash"),
        "max_positions": payload.get("max_positions"),
        "max_position_pct": payload.get("max_position_pct"),
        "rebalance": payload.get("rebalance") or first_baseline.get("rebalance"),
        "top_n": payload.get("top_n") or first_baseline.get("top_n"),
        "lot_size": payload.get("lot_size") or first_baseline.get("lot_size"),
        "price_filter_enabled": payload.get("price_filter_enabled") or (first_baseline.get("price_filter") or {}).get("enabled"),
        "price_filter_max_price": payload.get("price_filter_max_price") or (first_baseline.get("price_filter") or {}).get("max_price"),
        "price_filter_evaluated_on": payload.get("price_filter_evaluated_on") or (first_baseline.get("price_filter") or {}).get("evaluated_on"),
        "pit_mode": payload.get("pit_mode"),
        "unfilled_due_to_lot_size_count": payload.get("unfilled_due_to_lot_size_count") or first_metrics.get("unfilled_due_to_lot_size_count"),
        "invested_ratio_avg": payload.get("invested_ratio_avg") or first_metrics.get("invested_ratio_avg"),
        "warning_count": payload.get("warning_count", len(payload.get("warnings") or [])),
        "warnings_top": payload.get("warnings_top", list(payload.get("warnings") or [])[:10]),
        "output_paths": payload.get("output_paths", {}),
        "report_provenance": payload.get("report_provenance"),
        "disclaimer": payload.get("disclaimer"),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run Strategy Viability Sprint.")
    sub = parser.add_subparsers(dest="command")
    for name in ("precheck", "run", "report", "capital-lot-sensitivity"):
        child = sub.add_parser(name)
        child.add_argument("--data-root", default="data")
        child.add_argument("--adjustment", default="forward", choices=["forward", "back", "raw"])
        child.add_argument("--pit-mode", default="price_coverage", choices=["strict", "price_coverage", "off"])
        child.add_argument("--profile", default="low_price", choices=["low_price", "general"])
        child.add_argument("--top-n", type=int, default=0)
        child.add_argument("--max-symbols", type=int, default=0)
        child.add_argument("--sample-seed", type=int, default=20260528)
        child.add_argument("--strategies", default="low_volatility,momentum_low_vol,pure_momentum")
        child.add_argument("--full", default="false")
        child.add_argument("--json", default="false")
        child.add_argument("--summary", nargs="?", const="true", default="false")
        child.add_argument("--refresh", default="false")
        child.add_argument("--write-latest", default="")
        child.add_argument("--smoke-run", default="false")
    args = parser.parse_args(argv)
    if not args.command:
        parser.print_help()
        return 0
    cfg = vars(args)
    smoke_run = truthy(getattr(args, "smoke_run", "false"))
    write_latest = truthy(args.write_latest) if str(getattr(args, "write_latest", "")).strip() else (not smoke_run)
    cfg["smoke_run"] = smoke_run
    cfg["write_latest"] = write_latest
    if args.command == "precheck":
        payload = write_precheck_outputs(build_strategy_viability_precheck(cfg))
    elif args.command == "run":
        payload = write_baseline_outputs(run_strategy_viability_baselines(cfg))
    elif args.command == "capital-lot-sensitivity":
        strategies = [item.strip() for item in str(args.strategies or "").split(",") if item.strip()]
        sensitivity_cfg = {
            "data_root": args.data_root,
            "strategies": strategies,
            "full": truthy(args.full),
            "max_symbols": int(args.max_symbols or (20 if smoke_run else 500)),
            "write_latest": write_latest,
            "smoke_run": smoke_run,
        }
        if smoke_run:
            sensitivity_cfg.update({
                "initial_cash_grid": [10000],
                "max_positions_grid": [10],
                "max_price_grid": [15.0],
            })
        payload = write_capital_lot_sensitivity_outputs(run_capital_lot_sensitivity({
            **sensitivity_cfg,
        }))
    else:
        cfg["refresh"] = truthy(args.refresh)
        payload = write_strategy_viability_report(build_strategy_viability_report(cfg))
        baseline = build_strategy_viability_report({**cfg, "refresh": False})
        write_repair_rerun_outputs(
            build_repair_rerun_report(
                {
                    "baselines": baseline.get("baselines") or [],
                    "warnings": baseline.get("warnings") or [],
                    "report_provenance": baseline.get("report_provenance") or {},
                },
                data_root=args.data_root,
                pit_mode=args.pit_mode,
                max_symbols=int(args.max_symbols or 0),
            )
        )
    if truthy(args.summary):
        print(json.dumps(_summary(payload), ensure_ascii=False, separators=(",", ":"), allow_nan=False))
    elif truthy(args.json):
        print(json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False))
    else:
        print(json.dumps(_summary(payload), ensure_ascii=False, indent=2, allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
