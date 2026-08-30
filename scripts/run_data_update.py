from __future__ import annotations

import argparse
import json
import locale
import sys
import time
from pathlib import Path
from typing import Any

def _configure_console_encoding() -> None:
    encoding = locale.getpreferredencoding(False) or "utf-8"
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            reconfigure(encoding=encoding, errors="replace")


_configure_console_encoding()

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from quantdesk.data_update_orchestrator import (  # noqa: E402
    DEFAULT_REPORT_PATH,
    DISCLAIMER,
    run_data_update_preflight,
)
from quantdesk.operation_log import log_operation  # noqa: E402
from quantdesk.remediation_advisor import build_remediation_plan  # noqa: E402
from quantdesk.shared import now_text, truthy, write_json  # noqa: E402
from quantdesk.symbol_display_utils import (  # noqa: E402
    build_symbols_omitted_note,
    format_symbol_preview,
    safe_symbol_count,
)


def _parse_v370_bool(value: str | bool | None) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "false").lower() in {"1", "true", "yes", "y"}


def _run_v370_data_update(argv: list[str]) -> int:
    from datetime import datetime

    from quantdesk.data_providers.fundamentals_chain import load_fundamentals
    from quantdesk.data_providers.intraday_chain import load_intraday
    from quantdesk.data_providers.sector_chain import load_sectors
    from quantdesk.version import APP_VERSION

    sub = argv[0]
    parser = argparse.ArgumentParser(description="Run v3.7.0 data backbone update command.")
    parser.add_argument("subcommand")
    parser.add_argument("--symbol", default="")
    parser.add_argument("--network", default="false")
    parser.add_argument("--db-path", default="data/quantdesk.db")
    parser.add_argument("--json", default="false")
    parser.add_argument("--summary", nargs="?", const="true", default="false")
    args = parser.parse_args(argv)
    network = _parse_v370_bool(args.network)
    warnings: list[str] = []
    rows: list[dict] = []
    if sub == "daily":
        if not network:
            warnings.append("network_false_daily_provider_skipped")
        if args.symbol:
            rows = [{"symbol": args.symbol, "date": datetime.now().date().isoformat(), "close": None, "source": "network_false_placeholder"}]
        result = {"ok": True, "status": "warning" if warnings else "passed", "source": "network_guard", "rows": rows}
    elif sub == "fundamentals":
        result = load_fundamentals(args.symbol or "600000", network=network, db_path=args.db_path)
        warnings.extend(result.get("warnings", []))
    elif sub == "sectors":
        result = load_sectors(args.symbol or None, network=network, db_path=args.db_path)
        warnings.extend(result.get("warnings", []))
    elif sub == "intraday":
        result = load_intraday(args.symbol or "600000", network=network, config={"db_path": args.db_path})
        warnings.extend(result.get("warnings", []))
    else:
        result = {"ok": False, "status": "failed", "errors": [f"unknown_data_update_subcommand:{sub}"]}
    payload = {
        "ok": bool(result.get("ok", True)),
        "status": result.get("status") or ("warning" if warnings else "passed"),
        "task_type": f"data_update_{sub}",
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "app_version": APP_VERSION,
        "network": network,
        "row_count": len(result.get("rows", [])),
        "source": result.get("source"),
        "warning_count": len(warnings),
        "warnings_top": warnings[:10],
        "warnings": warnings,
        "output_paths": {"db_path": args.db_path},
        "disclaimer": "Data update is gated by network=false by default. SQLite is a shadow data layer.",
    }
    print(json.dumps(payload, ensure_ascii=False, allow_nan=False))
    return 0 if payload["ok"] else 1


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run QuantDesk data update preflight and optional daily refresh.")
    parser.add_argument("--symbols", default="", help="Comma-separated symbols. Empty means low-price included=true only.")
    parser.add_argument("--scope", default="", help="Optional qd.py scope label. current_analysis_pool keeps the existing safe scoped pool.")
    parser.add_argument("--target-date", default="", help="Target complete trade date in YYYY-MM-DD.")
    parser.add_argument("--network", default="false", help="Defaults to false. true allows provider calls.")
    parser.add_argument("--enable-network", action="store_true", help="Explicitly allow provider network update.")
    parser.add_argument("--no-network", action="store_true", help="Force offline preflight.")
    parser.add_argument("--max-symbols", type=int, default=80)
    parser.add_argument("--auto-health", default="true", help="Refresh data-health after an explicit network update.")
    parser.add_argument("--dry-run", nargs="?", const="true", default="false")
    parser.add_argument("--preflight-only", nargs="?", const="true", default="false")
    parser.add_argument("--debug-args", default="false")
    parser.add_argument("--json", default="false", help="Print full result JSON to stdout.")
    parser.add_argument("--verbose", default="false", help="Print full legacy details to stdout.")
    parser.add_argument("--output-dir", default="results/data_update")
    parser.add_argument("--report-path", default=str(DEFAULT_REPORT_PATH.relative_to(PROJECT_ROOT)))
    return parser.parse_args(argv)


def _network_arg_truthy(args: argparse.Namespace) -> bool:
    return truthy(args.network)


def _network_enabled(args: argparse.Namespace) -> bool:
    if args.no_network:
        return False
    return bool(args.enable_network or _network_arg_truthy(args))


def _arg_warnings(args: argparse.Namespace) -> list[str]:
    warnings: list[str] = []
    if args.no_network and (args.enable_network or _network_arg_truthy(args)):
        warnings.append("conflicting_network_flags: --no-network wins and forces offline processing")
    return warnings


def _status_for_exit(payload: dict[str, Any]) -> int:
    return 1 if payload.get("status") == "failed" else 0


def _status_label(status: Any) -> str:
    labels = {
        "passed": "正常",
        "warning": "提示",
        "failed": "失败",
        "missing": "未生成",
        "skipped": "已跳过",
    }
    return labels.get(str(status or "").lower(), str(status or "-"))


def _print_remediation(actions: list[dict[str, Any]], *, max_actions: int = 3) -> None:
    print("")
    print("我现在该怎么修：")
    print("下一步：")
    if not actions:
        print("1. 查看当前状态：")
        print("   python scripts/qd.py status")
        return
    for index, action in enumerate(actions[:max_actions], start=1):
        title = action.get("title") or action.get("why") or "修复建议"
        print(f"{index}. {title}")
        print("   需要显式联网授权" if action.get("network_required") else "   离线可运行，不会联网")
        print(f"   {action.get('command') or '-'}")
        if action.get("follow_up_command"):
            print("   然后：")
            print(f"   {action.get('follow_up_command')}")


def print_summary(payload: dict[str, Any], *, verbose: bool = False) -> None:
    if verbose:
        print_legacy_summary(payload)
        return
    output_path = str(payload.get("output_path") or "results/data_update/data_update_latest.json")
    report_path = str(payload.get("report_path") or "reports/data_update_report_latest.md")
    symbols = payload.get("symbols_checked") or payload.get("symbols") or []
    checked = safe_symbol_count(symbols)
    total = int(payload.get("symbol_count") or checked or 0)
    stale_symbols = payload.get("stale_symbols") or []
    failed_symbols = payload.get("failed_symbols") or []
    skipped_symbols = payload.get("skipped_symbols") or []

    print("QuantDesk 数据更新完成")
    print("")
    target_text = payload.get("target_complete_trade_date") or payload.get("target_date") or "-"
    print(f"目标日期：{target_text}")
    print(f"目标完整交易日：{target_text}")
    print(f"统计范围：{payload.get('data_scope') or '-'}")
    print(f"检查数量：{checked or total} / 当前分析池 {total}")
    print(f"全池覆盖到：{payload.get('coverage_date') or payload.get('latest_local_daily_date') or '-'}")
    print(f"最晚已有日线：{payload.get('latest_date_max') or payload.get('latest_local_daily_max_date') or '-'}")
    print(f"状态：{_status_label(payload.get('status'))}")
    print(f"成功：{payload.get('success_count') or 0}")
    print(f"跳过：{payload.get('skipped_count') or 0}")
    print(f"失败：{payload.get('failed_count') or 0}")
    print(f"需要联网：{'是' if payload.get('network') else '否'}")
    print(f"network: {str(payload.get('network')).lower()}")
    if stale_symbols:
        print(f"过期示例：{format_symbol_preview(stale_symbols, max_visible=5)}")
    if failed_symbols:
        print(f"失败示例：{format_symbol_preview(failed_symbols, max_visible=5)}")
    if skipped_symbols and not stale_symbols:
        print(f"跳过示例：{format_symbol_preview(skipped_symbols, max_visible=5)}")
    omitted_note = build_symbols_omitted_note(symbols, max_visible=5, result_path=output_path)
    if omitted_note:
        print(omitted_note)
    for warning in (payload.get("warnings") or [])[:3]:
        print(f"提示：{warning}")
    if payload.get("skipped_reason"):
        print(f"跳过原因：{payload.get('skipped_reason')}")
    if payload.get("failed_reason"):
        print(f"失败原因：{payload.get('failed_reason')}")
    print("")
    print(f"结果文件：{output_path}")
    print(f"报告文件：{report_path}")
    remediation = payload.get("remediation_actions") if isinstance(payload.get("remediation_actions"), list) else []
    _print_remediation(remediation)
    print("")
    print(DISCLAIMER)


def print_legacy_summary(payload: dict[str, Any]) -> None:
    print("QuantDesk 数据更新预检 / 更新结果")
    print("")
    print(f"目标完整交易日：{payload.get('target_complete_trade_date') or '-'}")
    print(f"统计范围：{payload.get('data_scope') or '-'}")
    print(f"全池覆盖到：{payload.get('coverage_date') or payload.get('latest_local_daily_date') or '-'}")
    print(f"最晚已有日线：{payload.get('latest_date_max') or payload.get('latest_local_daily_max_date') or '-'}")
    print(f"最早本地日线：{payload.get('latest_date_min') or '-'}")
    print(f"滞后交易日：{payload.get('trading_day_lag')}")
    print(f"network: {str(payload.get('network')).lower()}")
    print(f"update_allowed: {str(payload.get('update_allowed')).lower()}")
    print(f"dry_run: {str(payload.get('dry_run')).lower()}")
    print(f"auto_update: {'enabled' if payload.get('auto_update_enabled') else 'disabled'}")
    print(f"symbol_count: {payload.get('symbol_count')}")
    print(f"fresh_count: {payload.get('fresh_count')}")
    print(f"stale_count: {payload.get('stale_count')}")
    print(f"missing_count: {payload.get('missing_count')}")
    print(f"symbols: {', '.join(payload.get('symbols') or []) or '-'}")
    print(f"success_count: {payload.get('success_count')}")
    print(f"failed_count: {payload.get('failed_count')}")
    print(f"skipped_count: {payload.get('skipped_count')}")
    print(f"provider_chain: {', '.join(payload.get('provider_chain') or []) or '-'}")
    if not payload.get("network"):
        print("")
        print("当前仅检查，不联网。")
        print("如需联网更新，请运行：")
        print("python scripts/qd.py data-update --enable-network --scope current_analysis_pool")
    elif payload.get("dry_run"):
        print("")
        print("当前为 dry-run 演练，不联网、不写入 data/daily。")
    else:
        print("")
        print("已显式允许联网，以上为 provider 更新结果。")
    for warning in (payload.get("warnings") or [])[:20]:
        print(f"warning: {warning}")
    if payload.get("skipped_reason"):
        print(f"skipped_reason: {payload.get('skipped_reason')}")
    if payload.get("failed_reason"):
        print(f"failed_reason: {payload.get('failed_reason')}")
    print(f"下一步建议：{payload.get('suggested_next_action') or '-'}")
    remediation = payload.get("remediation_actions") if isinstance(payload.get("remediation_actions"), list) else []
    if remediation:
        print("")
        print("下一步：")
        for action in remediation[:2]:
            print("需要显式联网授权" if action.get("network_required") else "离线可运行，不会联网")
            print(action.get("command") or "-")
            if action.get("follow_up_command"):
                print("离线可运行，不会联网")
                print(action.get("follow_up_command"))
    print(f"结果文件：{payload.get('output_path') or 'results/data_update/data_update_latest.json'}")
    print(f"报告文件：{payload.get('report_path') or 'reports/data_update_report_latest.md'}")
    print(DISCLAIMER)


def print_debug_args(args: argparse.Namespace, *, network: bool, dry_run: bool, preflight_only: bool) -> None:
    print(f"parsed network={str(network).lower()}")
    print(f"parsed dry_run={str(dry_run).lower()}")
    print(f"parsed enable_network={str(bool(args.enable_network)).lower()}")
    print(f"parsed no_network={str(bool(args.no_network)).lower()}")
    print(f"symbols_source={'explicit' if args.symbols else 'low_price_included'}")
    print(f"max_symbols={args.max_symbols}")


def main(argv: list[str] | None = None) -> int:
    started_at = now_text()
    start_time = time.perf_counter()
    raw_argv = list(sys.argv[1:] if argv is None else argv)
    if raw_argv and raw_argv[0] in {"daily", "fundamentals", "sectors", "intraday"}:
        return _run_v370_data_update(raw_argv)
    args = _parse_args(argv)
    network = _network_enabled(args)
    dry_run = truthy(args.dry_run)
    preflight_only = truthy(args.preflight_only)
    if truthy(args.debug_args):
        print_debug_args(args, network=network, dry_run=dry_run, preflight_only=preflight_only)
    try:
        payload = run_data_update_preflight(
            args.symbols,
            target_date=args.target_date or None,
            network=network,
            auto_update=network,
            scope=args.scope or "",
            max_symbols=args.max_symbols,
            auto_health=truthy(args.auto_health),
            dry_run=dry_run,
            preflight_only=preflight_only,
            extra_warnings=_arg_warnings(args),
            write=True,
            output_dir=args.output_dir,
            report_path=args.report_path,
        )
        remediation_plan = build_remediation_plan(data_update=payload)
        payload["remediation_plan"] = remediation_plan
        payload["remediation_actions"] = remediation_plan.get("actions", [])[:5]
        payload["next_actions"] = payload["remediation_actions"]
        if payload.get("output_path"):
            write_json(PROJECT_ROOT / str(payload.get("output_path")), payload)
        if truthy(args.json):
            print(json.dumps(payload, ensure_ascii=False, indent=2))
        else:
            print_summary(payload, verbose=truthy(args.verbose))
        log_operation(
            command="data-update",
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
            failed_reason=payload.get("failed_reason") or "",
            suggested_next_action=payload.get("suggested_next_action") or "",
        )
        return _status_for_exit(payload)
    except Exception as exc:  # noqa: BLE001
        log_operation(
            command="data-update",
            args=sys.argv[1:] if argv is None else argv,
            target_date=getattr(args, "target_date", ""),
            symbols=getattr(args, "symbols", ""),
            network=network,
            llm_enabled=False,
            status="failed",
            started_at=started_at,
            start_time=start_time,
            failed_reason=str(exc),
            suggested_next_action="检查 data-update 参数和输出目录权限后重试。",
        )
        print(f"data-update failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
