from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from quantdesk.staged_import_plan import (  # noqa: E402
    DEFAULT_OUTPUT_PATH,
    DEFAULT_REPORT_PATH,
    write_staged_import_plan_outputs,
)


def _truthy(value: Any) -> bool:
    return str(value).strip().lower() in {"1", "true", "yes", "y", "on"}


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Read-only dry-run import-plan builder for staged mootdx / baostock vendor "
            "evidence. Never fetches network data, never imports, never writes canonical "
            "data. --apply true is explicitly rejected (real apply is not implemented)."
        )
    )
    parser.add_argument("--project-root", default=str(PROJECT_ROOT), help="Project root to inspect; mainly for tests.")
    parser.add_argument("--provider", default="mootdx", choices=["mootdx", "baostock"], help="Which staged provider pack to plan.")
    parser.add_argument("--staging-dir", default="", help="Explicit staging directory; overrides auto-detection of the newest pack.")
    parser.add_argument("--max-symbols", type=int, default=60, help="Max symbols/files to deep-compare (bounded scans are labelled).")
    parser.add_argument("--apply", default="false", help="Must stay false. --apply true is rejected; real apply is not implemented.")
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT_PATH))
    parser.add_argument("--report", default=str(DEFAULT_REPORT_PATH))
    parser.add_argument("--json", nargs="?", const="true", default="false")
    parser.add_argument("--summary", nargs="?", const="true", default="false")
    parser.add_argument("--network", default="false", help="Accepted for qd.py consistency; this command never fetches network data.")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    if _truthy(args.network):
        print("staged-import-plan is read-only and never fetches network data; ignoring --network true.", file=sys.stderr)

    apply_requested = _truthy(args.apply)
    payload = write_staged_import_plan_outputs(
        provider=args.provider,
        staging_dir=args.staging_dir or None,
        project_root=args.project_root,
        max_symbols=args.max_symbols,
        apply=apply_requested,
        output_path=args.output,
        report_path=args.report,
    )

    if _truthy(args.json):
        print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
    else:
        print("QuantDesk staged import plan (dry-run only)")
        print(f"provider: {payload.get('provider_requested')} / detected={payload.get('provider_detected', '-')}")
        print(f"audit_recommended_action: {payload.get('audit_recommended_action', '-')}")
        print(f"recommended_action: {payload.get('recommended_action')}")
        print(f"formal_eligible: {payload.get('formal_eligible')}  apply_executed: {payload.get('apply_executed')}")
        if payload.get("apply_rejected"):
            print(f"apply_rejected: {payload.get('message')}")
        plan = payload.get("plan") or {}
        if payload.get("provider_detected") == "mootdx" and plan:
            totals = plan.get("totals", {})
            print(
                f"mootdx totals: new={totals.get('new_rows')} dup={totals.get('exact_duplicate_rows')} "
                f"conflict={totals.get('price_conflict_rows')} bad_ohlcv={totals.get('bad_ohlcv_rows')} "
                f"out_of_session={totals.get('out_of_session_rows')}"
            )
        elif payload.get("provider_detected") == "baostock" and plan:
            for name, ds in (plan.get("datasets") or {}).items():
                print(f"  {name}: new={ds.get('new_rows')} dup={ds.get('duplicate_rows')} reason={ds.get('reason_codes')}")
            af = plan.get("adjustment_factors", {})
            print(f"  adjustment_factors: {af.get('status')} (blocked={af.get('blocked')})")
        print(f"output: {payload.get('output_path')}")
        print(f"report: {payload.get('report_path')}")

    # Non-zero exit only when apply was requested (rejected) so scripts/CI catch misuse.
    if payload.get("apply_rejected"):
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
