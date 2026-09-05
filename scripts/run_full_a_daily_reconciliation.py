"""Reconcile the current A-share daily universe for one complete trade date.

Offline and non-applying by default: the coordinator only performs canonical
writes when BOTH --enable-network and --apply are given, matching the plan's
requirement that a scheduled run never silently fetches or writes.

Exit codes are distinct so a scheduled task log tells you which state it hit:
  0  completed / planned_offline / skipped_existing_run
  3  deferred_upstream_busy  (daily-data lock held by another writer)
  4  partial                 (some symbols still lack a target bar)
  5  blocked / unknown       (never treated as success)
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from quantdesk.full_a_daily_reconciliation import run_full_a_daily_reconciliation  # noqa: E402
from quantdesk.shared import truthy  # noqa: E402
from quantdesk.trading_calendar import resolve_target_complete_trade_date  # noqa: E402


def default_target_date() -> str:
    """Latest COMPLETE A-share trading date.

    A scheduled run fires on calendar days, including weekends and holidays
    when the market never opened. Resolving through the project calendar keeps
    the target on a real session instead of inventing one: asked on a Sunday it
    answers with the preceding Friday.
    """
    resolved = resolve_target_complete_trade_date()
    return str(resolved.get("target_complete_trade_date") or "")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target-date", default="")
    parser.add_argument("--network", default="false")
    parser.add_argument("--enable-network", action="store_true")
    parser.add_argument("--apply", nargs="?", const="true", default="")
    parser.add_argument("--run-id", default="")
    parser.add_argument("--lock-timeout-seconds", type=float, default=0.0)
    parser.add_argument("--json", default="true")
    # Server chan authorization for the post-run health alert. The coordinator
    # defaults every alert gate to off, so without these the dispatcher runs but
    # can only ever preview - the scheduled task would never notify anyone.
    # Kept as separate flags from --enable-network/--apply so authorizing a data
    # write never silently authorizes an outbound message.
    parser.add_argument("--enable-alert-network", action="store_true")
    parser.add_argument("--alert-push", nargs="?", const="true", default="")
    parser.add_argument("--alert-dry-run", default="")
    parser.add_argument("--pipeline-run-id", default="")
    return parser


_EXIT_BY_STATUS = {
    "completed": 0,
    "planned_offline": 0,
    "skipped_existing_run": 0,
    "deferred_upstream_busy": 3,
    "partial": 4,
    "blocked": 5,
}


def exit_code_for(status: str) -> int:
    # Unknown states must never read as success.
    return _EXIT_BY_STATUS.get(str(status or ""), 5)


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    target = args.target_date.strip() or default_target_date()
    network = bool(args.enable_network or truthy(args.network))
    apply_writes = truthy(args.apply) if str(args.apply).strip() else False

    alert_network = bool(args.enable_alert_network)
    alert_push = truthy(args.alert_push) if str(args.alert_push).strip() else False
    # Mirrors the delivery CLI: dry-run unless the caller both authorizes the
    # network and asks to push, and an explicit --alert-dry-run always wins.
    if str(args.alert_dry_run).strip():
        alert_dry_run = truthy(args.alert_dry_run)
    else:
        alert_dry_run = not (alert_network and alert_push)

    payload = run_full_a_daily_reconciliation(
        project_root=PROJECT_ROOT,
        target_date=target,
        network=network,
        apply=apply_writes,
        run_id=args.run_id.strip() or None,
        lock_timeout_seconds=args.lock_timeout_seconds,
        alert_network=alert_network,
        alert_push=alert_push,
        alert_dry_run=alert_dry_run,
    )

    pipeline_run_id = str(args.pipeline_run_id or "").strip()
    latest = PROJECT_ROOT / "results" / "data_chain" / "full_a_daily_reconciliation_latest.json"
    if pipeline_run_id:
        payload["pipeline_run_id"] = pipeline_run_id
        if latest.is_file():
            try:
                disk = json.loads(latest.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                disk = None
            if isinstance(disk, dict):
                disk["pipeline_run_id"] = pipeline_run_id
                latest.write_text(json.dumps(disk, ensure_ascii=False, indent=2), encoding="utf-8")
            dated = payload.get("manifest_path")
            if dated:
                dated_path = PROJECT_ROOT / str(dated)
                if dated_path.is_file():
                    dated_path.write_text(
                        json.dumps(disk if isinstance(disk, dict) else payload, ensure_ascii=False, indent=2),
                        encoding="utf-8",
                    )
    if truthy(args.json):
        printed = dict(payload)
        if latest.is_file():
            printed["artifact_path"] = "results/data_chain/full_a_daily_reconciliation_latest.json"
            printed["artifact_sha256"] = "sha256:" + hashlib.sha256(latest.read_bytes()).hexdigest()
        print(json.dumps(printed, ensure_ascii=False, indent=2))
    else:
        coverage = payload.get("coverage") or {}
        delivery = payload.get("data_health_alert_delivery") or {}
        print(
            "full-a-reconcile "
            f"status={payload.get('status')} target={payload.get('target_date')} "
            f"pending={len((payload.get('partition') or {}).get('pending_symbols') or [])} "
            f"missing={coverage.get('missing_count', '-')} "
            f"stale={coverage.get('stale_count', '-')} "
            f"alert={delivery.get('status', '-')}/sent={delivery.get('external_sent_count', '-')} "
            f"reasons={','.join(payload.get('reason_codes') or []) or '-'}"
        )
    return exit_code_for(payload.get("status"))


if __name__ == "__main__":
    raise SystemExit(main())
