from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from quantdesk.chanlun_bi_engine import run_chanlun_bi_analysis


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run Chanlun bi recognition for local structure learning.")
    parser.add_argument("--symbol", default="", help="Stock symbol, for example 600744.")
    parser.add_argument("--timeframe", default="daily", choices=["daily", "1m", "5m", "15m", "30m", "60m"])
    parser.add_argument("--input-path", default="", help="Optional OHLC CSV path. Used when fractal-path is not supplied.")
    parser.add_argument("--fractal-path", default="", help="Optional v3.0.2 fractal JSON path.")
    parser.add_argument("--start-date", default="")
    parser.add_argument("--end-date", default="")
    parser.add_argument("--mode", default="strict", choices=["strict", "loose"])
    parser.add_argument("--min-separation", type=int, default=4)
    parser.add_argument("--allow-provisional", action="store_true", help="Allow provisional bi when an endpoint fractal is not confirmed.")
    parser.add_argument("--report", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--json", action="store_true", help="Print JSON output. Kept for compatibility; JSON is always printed.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    payload = run_chanlun_bi_analysis(
        args.symbol,
        timeframe=args.timeframe,
        start_date=args.start_date or None,
        end_date=args.end_date or None,
        input_path=args.input_path or None,
        fractal_path=args.fractal_path or None,
        mode=args.mode,
        min_separation=args.min_separation,
        allow_provisional=args.allow_provisional,
        report=args.report,
        write=True,
    )
    print(json.dumps(payload, ensure_ascii=True, indent=2))
    return 0 if payload.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
