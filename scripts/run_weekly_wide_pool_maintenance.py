from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from quantdesk.shared import truthy  # noqa: E402
from quantdesk.trading_calendar import resolve_target_complete_trade_date  # noqa: E402
from quantdesk.weekly_daily_wide_pool import (  # noqa: E402
    run_weekly_daily_wide_pool,
    run_weekly_wide_pool_maintenance,
)


MAX_TASK_LOCK_TIMEOUT_SECONDS = 21600


def _bounded_lock_timeout(value: str) -> int:
    timeout = int(value)
    if timeout < 0 or timeout > MAX_TASK_LOCK_TIMEOUT_SECONDS:
        raise argparse.ArgumentTypeError(
            f"lock timeout must be between 0 and {MAX_TASK_LOCK_TIMEOUT_SECONDS} seconds"
        )
    return timeout


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Maintain the audited weekly daily-wide and minute-residual pools."
    )
    parser.add_argument("--target-date", default="")
    parser.add_argument("--network", default="false")
    parser.add_argument("--enable-network", action="store_true")
    parser.add_argument("--apply", nargs="?", const="true", default="false")
    parser.add_argument("--daily-workers", type=int, default=8)
    parser.add_argument("--minute-batch-size", type=int, default=1)
    parser.add_argument("--daily-only", action="store_true")
    parser.add_argument(
        "--lock-timeout-seconds",
        type=_bounded_lock_timeout,
        default=0,
    )
    parser.add_argument("--run-id", default="")
    parser.add_argument("--json", default="true")
    parser.add_argument("--summary", nargs="?", const="true", default="false")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    resolved = resolve_target_complete_trade_date(
        args.target_date or None,
        calendar_path=PROJECT_ROOT / "data" / "calendar" / "a_share_trading_calendar.csv",
    )
    target = str(resolved.get("target_date") or "")[:10]
    network = bool(args.enable_network or truthy(args.network))
    apply = truthy(args.apply)
    if apply:
        from quantdesk.full_a_daily_reconciliation import run_full_a_daily_reconciliation

        payload = run_full_a_daily_reconciliation(
            project_root=PROJECT_ROOT, target_date=target, audit_only=True,
        )
        payload["legacy_weekly_request"] = True
        payload["maintenance_mode"] = "daily_coverage_audit"
    elif args.daily_only:
        # 排除策略必须与联合路径同源：否则 --daily-only 会在 4603 只全池上跑，
        # 被那 4 只永久取不到数的票挡住，与"已排除"的报告口径互相矛盾。
        daily_policy = PROJECT_ROOT / "config" / "weekly_daily_wide_pool_exclusions.json"
        payload = run_weekly_daily_wide_pool(
            PROJECT_ROOT,
            target_date=target,
            network=network,
            apply=apply,
            run_id=args.run_id or None,
            workers=args.daily_workers,
            lock_timeout_seconds=args.lock_timeout_seconds,
            exclusion_policy_path=daily_policy if daily_policy.is_file() else None,
        )
    else:
        payload = run_weekly_wide_pool_maintenance(
            PROJECT_ROOT,
            target_date=target,
            network=network,
            apply=apply,
            run_id=args.run_id or None,
            daily_workers=args.daily_workers,
            minute_batch_size=args.minute_batch_size,
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
        if args.daily_only:
            contract = dict(payload.get("contract") or {})
            count = dict(contract.get("counts") or {}).get("weekly_daily_wide", 0)
            print(
                "weekly-daily-wide-pool "
                f"status={payload.get('status')} target={payload.get('target_date')} "
                f"daily={count}"
            )
        else:
            print(
                "weekly-wide-pool "
                f"status={payload.get('status')} target={payload.get('target_date')} "
                f"daily={dict(payload.get('counts') or {}).get('daily_wide', 0)} "
                f"minute={dict(payload.get('counts') or {}).get('minute_residual', 0)}"
            )
    return 0 if payload.get("status") in {
        "completed",
        "completed_staging_only",
        "planned_offline",
        "completed_all_executed",
        "completed_with_skips",
        "completed_with_warnings",
    } else 4


if __name__ == "__main__":
    raise SystemExit(main())
