from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


def _configure_utf8_stdio() -> None:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except (OSError, ValueError):
                pass


_configure_utf8_stdio()

from quantdesk.operation_log import log_operation  # noqa: E402
from quantdesk.provider_network_smoke import resolve_smoke_symbols  # noqa: E402
from quantdesk.research.parameter_sensitivity_audit import (  # noqa: E402
    DEFAULT_JSON_PATH,
    DEFAULT_REPORT_PATH,
    run_parameter_sensitivity_audit,
    summarize_parameter_sensitivity,
)
from quantdesk.shared import now_text, truthy  # noqa: E402


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run QuantDesk parameter sensitivity and robustness audit.")
    parser.add_argument("--symbol", default="", help="Single symbol.")
    parser.add_argument("--symbols", default="", help="Comma-separated symbols. Empty means low-price included=true sample only.")
    parser.add_argument("--target-date", default="", help="Target date in YYYY-MM-DD.")
    parser.add_argument("--network", default="false", help="Defaults to false. This command does not fetch data.")
    parser.add_argument("--config", default="config/parameter_sensitivity.json")
    parser.add_argument("--max-combinations", type=int, default=50)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--strict", default="true")
    parser.add_argument("--output-dir", default="results")
    parser.add_argument("--report-dir", default="reports")
    return parser.parse_args(argv)


def _select_symbol(args: argparse.Namespace) -> str:
    if args.symbol:
        return str(args.symbol)
    if args.symbols:
        return str(args.symbols).split(",")[0].strip()
    sample = resolve_smoke_symbols("", limit=3)
    return sample[0] if sample else ""


def run(args: argparse.Namespace) -> dict[str, Any]:
    output_dir = Path(args.output_dir)
    report_dir = Path(args.report_dir)
    if not output_dir.is_absolute():
        output_dir = PROJECT_ROOT / output_dir
    if not report_dir.is_absolute():
        report_dir = PROJECT_ROOT / report_dir
    config_path = Path(args.config)
    if not config_path.is_absolute():
        config_path = PROJECT_ROOT / config_path
    payload = run_parameter_sensitivity_audit(
        symbol=_select_symbol(args),
        symbols=args.symbols,
        target_date=args.target_date,
        network=truthy(args.network),
        config_path=config_path if config_path.exists() else None,
        max_combinations=args.max_combinations,
        seed=args.seed,
        strict=truthy(args.strict),
        write=True,
        output_path=output_dir / DEFAULT_JSON_PATH.name,
        report_path=report_dir / DEFAULT_REPORT_PATH.name,
    )
    payload["json_path"] = str((output_dir / DEFAULT_JSON_PATH.name).relative_to(PROJECT_ROOT)) if (output_dir / DEFAULT_JSON_PATH.name).is_relative_to(PROJECT_ROOT) else str(output_dir / DEFAULT_JSON_PATH.name)
    payload["report_path"] = str((report_dir / DEFAULT_REPORT_PATH.name).relative_to(PROJECT_ROOT)) if (report_dir / DEFAULT_REPORT_PATH.name).is_relative_to(PROJECT_ROOT) else str(report_dir / DEFAULT_REPORT_PATH.name)
    return payload


def main(argv: list[str] | None = None) -> int:
    started_at = now_text()
    start_time = time.perf_counter()
    args = _parse_args(argv)
    try:
        payload = run(args)
        summary = summarize_parameter_sensitivity(payload)
        print("QuantDesk 参数敏感性与鲁棒性审计")
        print(f"symbol: {payload.get('symbol') or '-'}")
        print(f"target_date: {payload.get('target_date') or '-'}")
        print(f"network: {payload.get('network')} (本命令不主动抓行情)")
        print(f"parameter_count: {payload.get('parameter_count')}")
        print(f"combination_count: {payload.get('combination_count')}")
        print(f"robustness_score: {payload.get('robustness_score')}")
        print(f"status: {payload.get('status')}")
        for warning in summary.get("top_overfit_warnings") or []:
            print(f"overfit_warning: {warning}")
        for warning in payload.get("warnings") or []:
            print(f"warning: {warning}")
        print(f"json path: {payload.get('json_path') or DEFAULT_JSON_PATH}")
        print(f"report path: {payload.get('report_path') or DEFAULT_REPORT_PATH}")
        print("本命令不是参数优化器，不输出推荐参数，不构成投资建议。")
        log_operation(
            command="param-sensitivity",
            args=sys.argv[1:] if argv is None else argv,
            target_date=args.target_date,
            symbols=args.symbol or args.symbols,
            network=truthy(args.network),
            llm_enabled=False,
            status="warning" if payload.get("status") == "warning" else ("failed" if payload.get("status") == "failed" else "passed"),
            started_at=started_at,
            start_time=start_time,
            output_paths=[payload.get("json_path") or str(DEFAULT_JSON_PATH), payload.get("report_path") or str(DEFAULT_REPORT_PATH)],
            warnings=[*(payload.get("warnings") or []), *(summary.get("top_overfit_warnings") or [])],
            suggested_next_action="若样本不足，先补齐本地回测结果和数据健康，再重新审计参数鲁棒性。",
        )
        return 0 if payload.get("status") in {"passed", "warning"} else 1
    except Exception as exc:  # noqa: BLE001
        log_operation(
            command="param-sensitivity",
            args=sys.argv[1:] if argv is None else argv,
            target_date=getattr(args, "target_date", ""),
            symbols=getattr(args, "symbol", "") or getattr(args, "symbols", ""),
            network=truthy(getattr(args, "network", "false")),
            llm_enabled=False,
            status="failed",
            started_at=started_at,
            start_time=start_time,
            failed_reason=str(exc),
            suggested_next_action="检查 param-sensitivity 参数和输出目录权限后重试。",
        )
        print(f"param-sensitivity failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

