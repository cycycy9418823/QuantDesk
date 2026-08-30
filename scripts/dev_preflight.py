from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from quantdesk.data_update_orchestrator import DISCLAIMER, run_data_update_preflight  # noqa: E402
from quantdesk.operation_log import log_operation  # noqa: E402
from quantdesk.shared import now_text, truthy  # noqa: E402
from quantdesk.trading_calendar import calendar_info  # noqa: E402
from quantdesk.version import APP_VERSION  # noqa: E402


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run QuantDesk development preflight before validation or UI startup.")
    parser.add_argument("--symbols", default="", help="Comma-separated symbols. Empty means low-price included=true only.")
    parser.add_argument("--target-date", default="", help="Target complete trade date in YYYY-MM-DD.")
    parser.add_argument("--network", default="false", help="Defaults to false. true allows provider calls.")
    parser.add_argument("--enable-network", action="store_true", help="Explicitly allow daily update.")
    parser.add_argument("--no-network", action="store_true", help="Force offline preflight.")
    parser.add_argument("--max-symbols", type=int, default=80)
    return parser.parse_args(argv)


def _network_enabled(args: argparse.Namespace) -> bool:
    if args.no_network:
        return False
    return bool(args.enable_network or truthy(args.network))


def _arg_warnings(args: argparse.Namespace) -> list[str]:
    if args.no_network and (args.enable_network or truthy(args.network)):
        return ["检测到同时传入联网和 --no-network 参数；已按 --no-network 强制离线处理。"]
    return []


def main(argv: list[str] | None = None) -> int:
    started_at = now_text()
    start_time = time.perf_counter()
    args = _parse_args(argv)
    network = _network_enabled(args)
    try:
        payload = run_data_update_preflight(
            args.symbols,
            target_date=args.target_date or None,
            network=network,
            auto_update=network,
            max_symbols=args.max_symbols,
            auto_health=False,
            preflight_only=not network,
            extra_warnings=_arg_warnings(args),
            write=True,
        )
        cal = calendar_info(target_date=payload.get("target_complete_trade_date"))
        print("QuantDesk dev-preflight")
        print(f"app_version: {APP_VERSION}")
        print(f"calendar_quality: {cal.get('calendar_quality')} loaded_dates={cal.get('loaded_dates')}")
        print(f"target_complete_trade_date: {payload.get('target_complete_trade_date') or '-'}")
        print(f"latest_local_daily_date: {payload.get('latest_local_daily_date') or '-'}")
        print(f"trading_day_lag: {payload.get('trading_day_lag')}")
        print(f"network: {str(payload.get('network')).lower()}")
        if not payload.get("network"):
            print("当前仅预检，不联网、不跑策略、不调用 LLM。")
        for warning in (payload.get("warnings") or [])[:20]:
            print(f"warning: {warning}")
        print(f"suggested_next_action: {payload.get('suggested_next_action') or '-'}")
        print("下一步可运行：python scripts/qd.py data-health --symbols 600744,002115 --network false")
        print(DISCLAIMER)
        log_operation(
            command="dev-preflight",
            args=sys.argv[1:] if argv is None else argv,
            target_date=args.target_date,
            symbols=args.symbols,
            network=network,
            llm_enabled=False,
            status=str(payload.get("status") or "warning"),
            started_at=started_at,
            start_time=start_time,
            output_paths=[payload.get("output_path") or "results/data_update/data_update_latest.json", payload.get("report_path") or "reports/data_update_report_latest.md"],
            warnings=payload.get("warnings") or [],
            suggested_next_action=payload.get("suggested_next_action") or "",
        )
        return 1 if payload.get("status") == "failed" else 0
    except Exception as exc:  # noqa: BLE001
        log_operation(
            command="dev-preflight",
            args=sys.argv[1:] if argv is None else argv,
            target_date=getattr(args, "target_date", ""),
            symbols=getattr(args, "symbols", ""),
            network=network,
            llm_enabled=False,
            status="failed",
            started_at=started_at,
            start_time=start_time,
            failed_reason=str(exc),
            suggested_next_action="检查 dev-preflight 参数和本地目录权限后重试。",
        )
        print(f"dev-preflight failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
