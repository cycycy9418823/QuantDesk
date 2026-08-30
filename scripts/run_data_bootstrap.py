from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from quantdesk.cli_summary import build_cli_summary, format_human_summary  # noqa: E402
from quantdesk.data_bootstrap import DISCLAIMER, build_data_bootstrap  # noqa: E402
from quantdesk.operation_log import log_operation  # noqa: E402
from quantdesk.shared import now_text, truthy  # noqa: E402


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Initialize a tiny demo universe and optional daily cache. Explicit --enable-network is required for provider calls."
    )
    parser.add_argument("--preset", default="low_price_demo", choices=["low_price_demo"])
    parser.add_argument("--symbols", default="", help="Comma-separated symbols. Overrides preset symbols.")
    parser.add_argument("--start", default="2023-01-01")
    parser.add_argument("--end", default="")
    parser.add_argument("--target-date", default="", help="Alias for --end; defaults to latest complete A-share trading day.")
    parser.add_argument("--adjust", default="qfq")
    parser.add_argument("--network", default="false", help="Defaults to false. true allows provider calls.")
    parser.add_argument("--enable-network", action="store_true", help="Explicitly allow provider network update.")
    parser.add_argument("--no-network", action="store_true", help="Force offline bootstrap.")
    parser.add_argument("--json", default="false")
    parser.add_argument("--summary", nargs="?", const="true", default="false", help="Print compact operator summary while still writing full latest JSON.")
    return parser.parse_args(argv)


def _network_enabled(args: argparse.Namespace) -> bool:
    if args.no_network:
        return False
    return bool(args.enable_network or truthy(args.network))


def _print_summary(payload: dict[str, object]) -> None:
    print("QuantDesk data-bootstrap completed")
    print("")
    print(f"preset: {payload.get('preset')}")
    print(f"network: {str(payload.get('network')).lower()} (default is false)")
    print(f"symbols: {', '.join(payload.get('symbols') or []) or '-'}")
    print(f"created_universe: {payload.get('created_universe')}")
    print(f"updated_daily_count: {payload.get('updated_daily_count')}")
    print(f"daily_ready: {payload.get('daily_ready')}")
    print(f"bootstrap_mode: {payload.get('bootstrap_mode')}")
    if payload.get("failed_symbols"):
        print(f"failed_symbols: {', '.join(payload.get('failed_symbols') or [])}")
    if payload.get("failed_reason"):
        print(f"failed_reason: {payload.get('failed_reason')}")
    for warning in (payload.get("warnings") or [])[:5]:
        print(f"warning: {warning}")
    paths = payload.get("output_paths") if isinstance(payload.get("output_paths"), dict) else {}
    print("")
    print(f"json: {paths.get('json') or 'results/data_bootstrap/data_bootstrap_latest.json'}")
    print(f"report: {paths.get('report') or 'reports/data_bootstrap_report_latest.md'}")
    print(f"next: {payload.get('suggested_next_action')}")
    print("")
    print(DISCLAIMER)


def main(argv: list[str] | None = None) -> int:
    started_at = now_text()
    start_time = time.perf_counter()
    args = _parse_args(argv)
    network = _network_enabled(args)
    try:
        payload = build_data_bootstrap(
            preset=args.preset,
            symbols=args.symbols,
            start_date=args.start,
            end_date=args.target_date or args.end,
            adjust=args.adjust,
            network=network,
            write=True,
        )
        if truthy(args.summary):
            summary = build_cli_summary(payload, task_type="data_bootstrap")
            if truthy(args.json):
                print(json.dumps(summary, ensure_ascii=True, indent=2, default=str))
            else:
                print(format_human_summary(summary))
        elif truthy(args.json):
            print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
        else:
            _print_summary(payload)
        log_operation(
            command="data-bootstrap",
            args=sys.argv[1:] if argv is None else argv,
            target_date=str(payload.get("end_date") or ""),
            symbols=",".join(payload.get("symbols") or []),
            network=network,
            llm_enabled=False,
            status=str(payload.get("status") or "warning"),
            started_at=started_at,
            start_time=start_time,
            output_paths=list((payload.get("output_paths") or {}).values()),
            warnings=payload.get("warnings") if isinstance(payload.get("warnings"), list) else [],
            failed_reason=str(payload.get("failed_reason") or ""),
            suggested_next_action=str(payload.get("suggested_next_action") or ""),
        )
        return 0 if payload.get("ok") else 1
    except Exception as exc:  # noqa: BLE001
        log_operation(
            command="data-bootstrap",
            args=sys.argv[1:] if argv is None else argv,
            network=network,
            llm_enabled=False,
            status="failed",
            started_at=started_at,
            start_time=start_time,
            failed_reason=str(exc),
            suggested_next_action="python scripts/qd.py data-bootstrap --preset low_price_demo --network false",
        )
        print(f"data-bootstrap failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
