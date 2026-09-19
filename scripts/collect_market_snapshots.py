"""Collect one explicit A0 full-market five-minute snapshot.

The command is intentionally one-shot. It never installs or triggers a
scheduled task, never writes a latest cache, and never invokes Pipeline/AI.
Network remains disabled unless ``--enable-network`` (or ``--network true``)
is provided by the caller.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from quantdesk.shared import truthy  # noqa: E402
from quantdesk.snapshot_history import collect_market_snapshot, fetch_snapshot_in_process  # noqa: E402


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", default=str(PROJECT_ROOT), help="Injected project root for the archive.")
    parser.add_argument("--network", default="false", help="Opt-in network gate (true/false).")
    parser.add_argument("--enable-network", action="store_true", help="Explicitly authorize provider requests.")
    parser.add_argument("--dry-run", nargs="?", const="true", default="false", help="Plan only; do not call providers or write files.")
    parser.add_argument("--include-close", nargs="?", const="true", default="false", help="Add the optional 15:00 close slot to the expected schedule.")
    parser.add_argument("--slot-time", default="", help="One-shot slot label such as 0930; normally inferred from the clock.")
    parser.add_argument("--json", default="true", help="Print JSON payload (true/false).")
    parser.add_argument("--provider-budget-seconds", type=float, default=150.0, help="Whole provider-process deadline, capped at 240 seconds.")
    return parser


def exit_code(status: str) -> int:
    return {"written": 0, "offline": 0, "dry_run": 0, "closed": 0, "already_exists": 0, "already_running": 4}.get(status, 5)


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    network = bool(args.enable_network or truthy(args.network))
    payload = collect_market_snapshot(
        Path(args.root),
        network=network,
        dry_run=truthy(args.dry_run),
        include_close=truthy(args.include_close),
        slot_time=args.slot_time.strip() or None,
        fetcher=lambda symbols: fetch_snapshot_in_process(symbols, network=network, budget_seconds=args.provider_budget_seconds),
    )
    if truthy(args.json):
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        # Collected / roster-covered / valid-quote / elapsed-missing / pending are
        # reported as separate numbers.  A single "missing" total conflates slots
        # that truly failed with slots that had not happened yet.
        print(
            "a0-snapshot "
            f"status={payload.get('status')} date={payload.get('trade_date')} "
            f"slot={payload.get('slot_time') or '-'} rows={payload.get('row_count', 0)} "
            f"missing_slots={len(payload.get('missing_slots') or [])}"
        )
        summary = payload.get("day_summary") or payload
        if "elapsed_slot_count" in summary:
            print(
                "a0-day "
                f"elapsed={summary.get('elapsed_slot_count')} "
                f"collected={summary.get('collected_slot_count')} "
                f"collected_ratio={summary.get('collected_slot_ratio')} "
                f"roster_coverage_ratio={summary.get('roster_coverage_ratio')} "
                f"valid_quote_ratio={summary.get('valid_quote_ratio')} "
                f"missing_elapsed={summary.get('missing_elapsed_slot_count')} "
                f"pending={summary.get('pending_slot_count')}"
            )
    return exit_code(str(payload.get("status") or ""))


if __name__ == "__main__":
    raise SystemExit(main())
