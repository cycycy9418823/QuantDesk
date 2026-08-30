from __future__ import annotations

import argparse
import contextlib
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from quantdesk.holdings.canonical_selection_export import (  # noqa: E402
    audit_selected_symbol_snapshot,
    export_canonical_selected_symbols,
    locate_selection_generation,
    selected_symbol_export_preflight,
)
from quantdesk.paths import project_root  # noqa: E402
from quantdesk.shared import truthy  # noqa: E402


def _call_with_json_stdout_guard(enabled: bool, func, *args, **kwargs):
    if enabled:
        with contextlib.redirect_stdout(sys.stderr):
            return func(*args, **kwargs)
    return func(*args, **kwargs)


def main() -> int:
    parser = argparse.ArgumentParser(description="Research-only canonical selected-symbol snapshot export; does not change backtests.")
    parser.add_argument("--export-selected-symbols", default="true", help="Set false to run only preflight/locator.")
    parser.add_argument("--json", nargs="?", const="true", default="false", help="Print result payload as JSON.")
    parser.add_argument("--summary", nargs="?", const="true", default="false", help="Print compact operator output.")
    args = parser.parse_args()
    root = project_root()
    json_mode = truthy(args.json)
    summary_mode = truthy(args.summary)
    preflight = selected_symbol_export_preflight(root)
    locator = locate_selection_generation(root)
    if str(args.export_selected_symbols).strip().lower() not in {"1", "true", "yes", "y"}:
        payload = {
            "task_id": "canonical_selected_symbols_export",
            "status": "preflight_only",
            "research_only": True,
            "preflight_status": preflight.get("status"),
            "locator_status": locator.get("status"),
            "row_count": 0,
            "audit_status": "",
            "disclaimer": "Research-only selected-symbol preflight; no broker, no orders, no position advice.",
        }
        if json_mode:
            print(json.dumps(payload, ensure_ascii=False, indent=None if summary_mode else 2))
        else:
            print("selected_symbol_export_status=preflight_only")
        return 0
    payload = _call_with_json_stdout_guard(json_mode, export_canonical_selected_symbols, root)
    audit = audit_selected_symbol_snapshot(root)
    output = {
        **{key: value for key, value in payload.items() if key != "rows"},
        "audit_status": audit.get("status"),
        "usable_for_holdings_materialization": audit.get("usable_for_holdings_materialization"),
    }
    if json_mode:
        print(json.dumps(output, ensure_ascii=False, indent=None if summary_mode else 2))
    else:
        print(f"selected_symbol_export_status={payload['status']} rows={payload['row_count']} audit={audit['status']}")
    return 0 if payload["status"] in {"canonical_selected_symbols_exported", "canonical_selected_symbols_reused_existing_valid_snapshot"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
