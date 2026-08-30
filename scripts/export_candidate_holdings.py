from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from quantdesk.holdings.candidate_holdings_export import export_candidate_holdings
from quantdesk.paths import project_root
from quantdesk.shared import truthy


def _truthy(value: str) -> bool:
    return str(value).strip().lower() in {"1", "true", "yes", "y"}


def main() -> int:
    parser = argparse.ArgumentParser(description="Research-only candidate holdings export hook; default behavior is off.")
    parser.add_argument("--candidate-id", default="portfolio_low_vol_rev120_candidate")
    parser.add_argument("--combo-id", default="combo_50_50")
    parser.add_argument("--export-holdings", default="false", help="Must be true to write holdings output.")
    parser.add_argument("--selected-snapshot", default="", help="Verified selected-symbol snapshot CSV/JSON. Required for real export.")
    parser.add_argument("--output", default="results/holdings/candidate_holdings_snapshot_latest.csv")
    parser.add_argument("--json-output", default="results/holdings/candidate_holdings_snapshot_latest.json")
    parser.add_argument("--report", default="reports/holdings/candidate_holdings_snapshot_latest.md")
    parser.add_argument("--json", nargs="?", const="true", default="false", help="Print result payload as JSON.")
    parser.add_argument("--summary", nargs="?", const="true", default="false", help="Print compact operator output.")
    args = parser.parse_args()
    root = project_root()
    selected = Path(args.selected_snapshot) if args.selected_snapshot else None
    if selected is not None and not selected.is_absolute():
        selected = root / selected
    result = export_candidate_holdings(
        root,
        export_holdings=_truthy(args.export_holdings),
        selected_snapshot=selected,
        output_csv=root / args.output,
        output_json=root / args.json_output,
        report_path=root / args.report,
        candidate_id=args.candidate_id,
        combo_id=args.combo_id,
    )
    payload = {
        "task_id": "candidate_holdings_export",
        "status": result.status,
        "research_only": True,
        "candidate_id": args.candidate_id,
        "combo_id": args.combo_id,
        "row_count": len(result.rows),
        "reason": result.reason,
        "source": result.source,
        "output_csv": args.output,
        "disclaimer": "Research-only holdings export hook; no broker, no orders, no position advice.",
    }
    if truthy(args.json):
        print(json.dumps(payload, ensure_ascii=False, indent=None if truthy(args.summary) else 2))
    else:
        print(f"holdings_export_status={result.status} rows={len(result.rows)}")
    return 0 if result.status in {"export_disabled", "holdings_requires_selected_snapshot_input", "holdings_snapshot_exported"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
