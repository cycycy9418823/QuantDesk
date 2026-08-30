from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from quantdesk.research.capital_lot_sensitivity import (  # noqa: E402
    run_capital_lot_sensitivity,
    write_capital_lot_sensitivity_outputs,
)
from quantdesk.shared import truthy  # noqa: E402


def _split_csv(value: str) -> list[str]:
    return [item.strip() for item in str(value or "").split(",") if item.strip()]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run low-price capital and lot sensitivity.")
    parser.add_argument("--data-root", default="data")
    parser.add_argument("--strategies", default="low_volatility,momentum_low_vol,pure_momentum")
    parser.add_argument("--full", default="false")
    parser.add_argument("--json", default="false")
    parser.add_argument("--summary", nargs="?", const="true", default="false")
    parser.add_argument("--max-symbols", type=int, default=500)
    parser.add_argument("--write-latest", default="")
    parser.add_argument("--smoke-run", default="false")
    args = parser.parse_args(argv)
    smoke_run = truthy(args.smoke_run)
    write_latest = truthy(args.write_latest) if str(args.write_latest).strip() else (not smoke_run)
    payload = write_capital_lot_sensitivity_outputs(run_capital_lot_sensitivity({
        "data_root": args.data_root,
        "strategies": _split_csv(args.strategies),
        "full": truthy(args.full),
        "max_symbols": args.max_symbols,
        "write_latest": write_latest,
        "smoke_run": smoke_run,
    }))
    if truthy(args.summary):
        summary = {
            "ok": payload.get("ok"),
            "status": payload.get("status"),
            "task_type": payload.get("task_type"),
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
            "lot_constraint_summary": payload.get("lot_constraint_summary"),
            "warnings_top": payload.get("warnings_top"),
            "output_paths": payload.get("output_paths"),
            "report_provenance": payload.get("report_provenance"),
            "disclaimer": payload.get("disclaimer"),
        }
        print(json.dumps(summary, ensure_ascii=False, separators=(",", ":"), allow_nan=False))
    elif truthy(args.json):
        print(json.dumps(payload, ensure_ascii=False, separators=(",", ":"), allow_nan=False))
    else:
        print(json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False))
    return 0 if payload.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
