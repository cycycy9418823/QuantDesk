from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from quantdesk.maintenance.strategy_regression_audit import (  # noqa: E402
    audit_strategy_regression,
    write_strategy_regression_audit_outputs,
)
from quantdesk.shared import truthy  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Audit strategy result regression and report hygiene.")
    parser.add_argument("--json", default="false")
    parser.add_argument("--summary", nargs="?", const="true", default="false")
    args = parser.parse_args(argv)
    payload = write_strategy_regression_audit_outputs(audit_strategy_regression())
    if truthy(args.summary):
        summary = {
            "ok": payload.get("ok"),
            "status": payload.get("status"),
            "task_type": payload.get("task_type"),
            "baseline_distinctness_status": payload.get("baseline_distinctness_status"),
            "capital_lot_result_count_status": payload.get("capital_lot_result_count_status"),
            "drawdown_sanity_status": payload.get("drawdown_sanity_status"),
            "sharpe_sanity_status": payload.get("sharpe_sanity_status"),
            "top_level_metrics_consistency": payload.get("top_level_metrics_consistency"),
            "report_provenance_status": payload.get("report_provenance_status"),
            "execution_evidence_status": payload.get("execution_evidence_status"),
            "insufficient_execution_evidence": payload.get("insufficient_execution_evidence"),
            "real_strategy_homogeneity": payload.get("real_strategy_homogeneity"),
            "temporary_data_pollutes_latest": payload.get("temporary_data_pollutes_latest"),
            "latest_trustworthy_for_strategy_conclusion": payload.get("latest_trustworthy_for_strategy_conclusion"),
            "reason_codes": payload.get("reason_codes"),
            "suspected_root_causes": payload.get("suspected_root_causes"),
            "recommended_fix_scope": payload.get("recommended_fix_scope"),
            "output_paths": payload.get("output_paths"),
            "warnings": payload.get("warnings"),
        }
        print(json.dumps(summary, ensure_ascii=False, separators=(",", ":"), allow_nan=False))
    elif truthy(args.json):
        print(json.dumps(payload, ensure_ascii=False, separators=(",", ":"), allow_nan=False))
    else:
        print(json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
