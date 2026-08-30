"""CLI script for exporting trade plans.

Usage:
    python scripts/export_trade_plan.py --symbols 600000,000001
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path

import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from quantdesk.config import load_config  # noqa: E402
from quantdesk.csv_io import read_csv_frame  # noqa: E402
from quantdesk.data_quality import assess_daily_frame_grade  # noqa: E402
from quantdesk.risk_position import compute_position_reference  # noqa: E402
from quantdesk.trade_plan import (  # noqa: E402
    build_trade_plan,
    export_trade_plan_csv,
    export_trade_plan_markdown,
)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Export trade plan for manual confirmation.")
    parser.add_argument("--symbols", default="", help="Comma-separated symbols.")
    parser.add_argument("--strategy", default="ma_breakout")
    parser.add_argument("--capital", type=float, default=100000.0)
    parser.add_argument("--output-dir", default="")
    parser.add_argument("--config", default="")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    config = load_config(args.config or None)
    data_cfg = config.get("data", {}) if isinstance(config.get("data"), dict) else {}
    daily_dir = Path(str(data_cfg.get("daily_cache_dir", PROJECT_ROOT / "data" / "daily")))
    if not daily_dir.is_absolute():
        daily_dir = PROJECT_ROOT / daily_dir

    symbols = [s.strip() for s in args.symbols.split(",") if s.strip()]
    if not symbols:
        print("No symbols provided. Use --symbols 600000,000001")
        return 1

    candidates: list[dict] = []
    for symbol in symbols:
        csv_path = daily_dir / f"{symbol}.csv"
        if not csv_path.exists():
            print(f"SKIP {symbol}: no local daily data")
            continue
        try:
            df = read_csv_frame(csv_path)
            if any(item.startswith("csv_read_failed:") for item in list(df.attrs.get("warnings") or [])):
                print(f"SKIP {symbol}: read error")
                continue
            if "date" in df.columns:
                df["date"] = pd.to_datetime(df["date"], errors="coerce")
        except Exception:
            print(f"SKIP {symbol}: read error")
            continue

        assessment = assess_daily_frame_grade(df, provider_status="local_daily_cache")
        pos_ref = compute_position_reference(
            df, symbol=symbol,
            total_capital=args.capital,
            data_grade=assessment.data_grade,
        )
        close = pd.to_numeric(df.get("close"), errors="coerce") if "close" in df.columns else None
        ref_price = float(close.iloc[-1]) if close is not None and not close.empty else 0.0

        candidates.append({
            "symbol": symbol,
            "name": "",
            "strategy": args.strategy,
            "reference_price": ref_price,
            "data_grade": assessment.data_grade,
            "backtest_quality": "",
            "atr14": pos_ref.atr14,
        })

    plan = build_trade_plan(candidates)

    output_dir = Path(args.output_dir) if args.output_dir else PROJECT_ROOT / "results" / "trade_plans"
    if not output_dir.is_absolute():
        output_dir = PROJECT_ROOT / output_dir
    output_dir.mkdir(parents=True, exist_ok=True)
    now = datetime.now().strftime("%Y%m%d_%H%M%S")
    csv_path = export_trade_plan_csv(plan, output_dir / f"trade_plan_{now}.csv")
    md_path = export_trade_plan_markdown(plan, output_dir / f"trade_plan_{now}.md")

    print(f"Trade plan exported: {len(plan.items)} items")
    print(f"  CSV: {csv_path}")
    print(f"  MD:  {md_path}")
    for w in plan.warnings:
        print(f"  WARN: {w}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
