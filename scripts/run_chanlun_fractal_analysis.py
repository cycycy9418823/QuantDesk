from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from quantdesk.chanlun_fractal_engine import run_chanlun_fractal_analysis
from quantdesk.shared import truthy


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run Chanlun fractal structure analysis.")
    parser.add_argument("--symbol", default="", help="A-share symbol, e.g. 600744")
    parser.add_argument("--timeframe", default="daily", choices=["daily", "1m", "5m", "15m", "30m", "60m"])
    parser.add_argument("--input-path", default="")
    parser.add_argument("--start-date", default="")
    parser.add_argument("--end-date", default="")
    parser.add_argument("--mode", default="strict", choices=["strict", "loose"])
    parser.add_argument("--include-resolved", default="true")
    parser.add_argument("--min-separation", type=int, default=1)
    parser.add_argument("--report", default="true")
    parser.add_argument("--json", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    payload = run_chanlun_fractal_analysis(
        args.symbol,
        timeframe=args.timeframe,
        start_date=args.start_date or None,
        end_date=args.end_date or None,
        input_path=args.input_path or None,
        mode=args.mode,
        include_resolved=truthy(args.include_resolved),
        min_separation=args.min_separation,
        report=truthy(args.report),
        write=True,
    )
    print(json.dumps(payload, ensure_ascii=True, indent=2))
    return 0 if payload.get("ok") or payload.get("status") == "no_data" else 1


if __name__ == "__main__":
    raise SystemExit(main())
