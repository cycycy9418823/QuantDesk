"""CLI: windowed minute-pool quality audit (Stage 2, read-only, offline).

Usage:
    python scripts/run_minute_pool_window_audit.py \
        --window-start 2026-01-05 --window-end 2026-07-17 [--symbols 600028,600744]

Grades every (symbol, trading-day) pair in the window using the existing
intraday quality auditor, and writes
results/data_health/minute_pool_window_audit_latest.json.
No network, no canonical writes, research only.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from quantdesk.intraday.minute_pool_window_audit import audit_minute_pool_window
from quantdesk.shared import truthy


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--window-start", required=True, help="窗口起始交易日 YYYY-MM-DD")
    parser.add_argument("--window-end", required=True, help="窗口结束交易日 YYYY-MM-DD")
    parser.add_argument("--symbols", default="", help="逗号分隔的 symbol 子集；默认全池")
    parser.add_argument("--intraday-dir", default="", help="1m 目录覆盖（默认 data/intraday/1m）")
    parser.add_argument("--output", default="", help="快照输出路径覆盖")
    parser.add_argument("--max-exception-dates", type=int, default=30)
    parser.add_argument("--json", dest="as_json", default="false")
    parser.add_argument("--summary", default="true")
    parser.add_argument("--network", default="false", help="兼容参数；本命令永不联网")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    symbols = [item.strip() for item in str(args.symbols or "").split(",") if item.strip()]
    payload = audit_minute_pool_window(
        window_start=args.window_start,
        window_end=args.window_end,
        symbols=symbols or None,
        intraday_dir=Path(args.intraday_dir) if args.intraday_dir else None,
        output_path=Path(args.output) if args.output else None,
        max_exception_dates=args.max_exception_dates,
    )
    if truthy(args.as_json):
        slim = {k: v for k, v in payload.items() if k != "symbols"}
        slim["symbol_row_count"] = len(payload.get("symbols") or [])
        print(json.dumps(slim, ensure_ascii=False, indent=2))
    elif truthy(args.summary):
        print(
            f"minute-pool-window-audit: {payload['window_start']}..{payload['window_end']} "
            f"({payload['trading_day_count']} trading days) symbols={payload['total_symbols']} "
            f"fully_usable={payload['fully_usable_symbol_count']} -> {payload.get('path', '')}"
        )
        per_day = payload.get("per_day_usable_counts") or {}
        if per_day:
            first_day = payload["window_start"]
            last_day = payload["window_end"]
            print(
                f"  usable on first day {first_day}: {per_day.get(first_day, 0)}; "
                f"usable on last day {last_day}: {per_day.get(last_day, 0)}"
            )
        print("  read-only / no network / no canonical write; research only, not investment advice.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
