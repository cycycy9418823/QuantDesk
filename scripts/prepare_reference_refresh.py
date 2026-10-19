"""Plan or prepare four reference-table updates; never replace live metadata."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from quantdesk.data_truth.reference_refresh import ReferenceRefreshConfig, plan_reference_refresh, prepare_reference_refresh


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--staging-dir", type=Path, required=True, help="Dated provider CSVs with reference_fetch.v1 receipts; no token is read.")
    parser.add_argument("--metadata-dir", type=Path, default=ROOT / "data/metadata")
    parser.add_argument("--dates", nargs="+", required=True, help="At most 5 explicit trading dates, YYYY-MM-DD.")
    parser.add_argument("--prepare-to", type=Path, help="Opt in to isolated candidate copies. Omit for a read-only plan.")
    parser.add_argument("--budget-seconds", type=float, default=300.0)
    args = parser.parse_args(argv)
    config = ReferenceRefreshConfig(args.staging_dir, args.metadata_dir, tuple(args.dates), budget_seconds=args.budget_seconds)
    try:
        result = prepare_reference_refresh(config, args.prepare_to) if args.prepare_to else plan_reference_refresh(config)
        result = {key: value for key, value in result.items() if not key.startswith("_")}
    except (OSError, ValueError) as exc:
        result = {"status": "blocked", "canonical_write": False, "error": f"{type(exc).__name__}:{exc}"}
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 2 if result["status"] == "blocked" else 0


if __name__ == "__main__":
    raise SystemExit(main())
