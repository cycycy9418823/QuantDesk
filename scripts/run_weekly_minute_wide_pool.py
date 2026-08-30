from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from quantdesk.intraday.weekly_minute_wide_pool import run_weekly_minute_wide_pool
from quantdesk.shared import truthy
from quantdesk.trading_calendar import resolve_target_complete_trade_date


MAX_TASK_LOCK_TIMEOUT_SECONDS = 21600


def _bounded_lock_timeout(value: str) -> int:
    timeout = int(value)
    if timeout < 0 or timeout > MAX_TASK_LOCK_TIMEOUT_SECONDS:
        raise argparse.ArgumentTypeError(
            f"lock timeout must be between 0 and {MAX_TASK_LOCK_TIMEOUT_SECONDS} seconds"
        )
    return timeout


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Maintain existing canonical one-minute symbols outside the formal "
            "daily analysis pool. Offline and no-apply by default."
        )
    )
    parser.add_argument("--project-root", default=str(PROJECT_ROOT))
    parser.add_argument("--target-date", default="")
    parser.add_argument("--network", default="false")
    parser.add_argument("--enable-network", action="store_true")
    parser.add_argument("--apply", nargs="?", const="true", default="false")
    parser.add_argument("--batch-size", type=int, default=1)
    parser.add_argument("--exclude-symbol", action="append", default=[])
    parser.add_argument(
        "--lock-timeout-seconds",
        type=_bounded_lock_timeout,
        default=0,
    )
    parser.add_argument("--json", default="true")
    parser.add_argument("--summary", default="false")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    root = Path(args.project_root).resolve()
    resolved = resolve_target_complete_trade_date(
        args.target_date or None,
        calendar_path=root / "data" / "calendar" / "a_share_trading_calendar.csv",
    )
    payload = run_weekly_minute_wide_pool(
        root,
        target_date=str(resolved.get("target_date") or ""),
        network=bool(args.enable_network or truthy(args.network)),
        apply=truthy(args.apply),
        batch_size=max(1, int(args.batch_size)),
        explicit_exclusions=list(args.exclude_symbol or []),
        exclusion_policy_path=(
            root / "config" / "weekly_minute_wide_pool_exclusions.json"
            if not args.exclude_symbol
            and (root / "config" / "weekly_minute_wide_pool_exclusions.json").is_file()
            else None
        ),
        lock_timeout_seconds=args.lock_timeout_seconds,
    )
    if truthy(args.json):
        print(
            json.dumps(
                payload,
                ensure_ascii=False,
                indent=None if truthy(args.summary) else 2,
            )
        )
    else:
        counts = (payload.get("pool_contract") or {}).get("counts") or {}
        print(
            "weekly-minute-wide-pool "
            f"status={payload.get('status')} target={payload.get('target_date')} "
            f"wide={counts.get('weekly_wide')} run_id={payload.get('run_id', '')}"
        )
        print(DISCLAIMER_TEXT)
    status = str(payload.get("status") or "")
    if status in {"planned_offline", "completed_staging_only", "completed"}:
        return 0
    if status == "partial":
        return 2
    if status == "busy":
        return 3
    return 4


DISCLAIMER_TEXT = "Research Only / Paper Account Only / No Broker Route."


if __name__ == "__main__":
    raise SystemExit(main())
