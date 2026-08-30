#!/usr/bin/env python3
"""CLI: Portfolio health check — daily risk report for holdings.

Usage:
    python scripts/portfolio_health_check.py --holdings holdings.csv [--cash 50000] [--output report.md]

holdings.csv format: symbol,name,shares,avg_cost,current_price[,data_grade]

All results are for local risk reference only. Not investment advice.
"""

from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from quantdesk.portfolio_health import (
    HoldingHealth,
    check_holding_health,
    check_portfolio_health,
    portfolio_health_to_markdown,
)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="持仓体检报告")
    parser.add_argument("--holdings", required=True, help="持仓CSV文件 (symbol,name,shares,avg_cost,current_price)")
    parser.add_argument("--cash", type=float, default=0, help="现金余额")
    parser.add_argument("--output", "-o", default="", help="输出Markdown文件路径")
    parser.add_argument("--json", action="store_true", help="同时输出JSON")
    return parser.parse_args(argv)


def load_holdings_csv(path: Path) -> list[dict[str, str]]:
    rows = []
    with open(path, encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for row in reader:
            rows.append(row)
    return rows


def _sector_from_row(row: dict[str, str]) -> str:
    """Use user-provided sector/industry when available."""
    for key in ("sector", "industry", "板块", "行业"):
        value = row.get(key)
        if value:
            return str(value).strip()
    return ""


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    holdings_path = Path(args.holdings)
    if not holdings_path.exists():
        print(f"文件不存在: {holdings_path}")
        return 1

    raw = load_holdings_csv(holdings_path)
    holdings: list[HoldingHealth] = []
    for row in raw:
        h = check_holding_health(
            symbol=str(row.get("symbol", "")).zfill(6),
            name=str(row.get("name", "")),
            shares=int(float(row.get("shares", 0))),
            avg_cost=float(row.get("avg_cost", 0)),
            current_price=float(row.get("current_price", 0)),
            data_grade=str(row.get("data_grade", "")),
            sector=_sector_from_row(row),
        )
        holdings.append(h)

    report = check_portfolio_health(holdings, cash=args.cash)
    md = portfolio_health_to_markdown(report)

    if args.output:
        out = Path(args.output)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(md, encoding="utf-8")
        print(f"报告已保存: {out}")
    else:
        print(md)

    if args.json:
        import json
        json_path = Path(args.output).with_suffix(".json") if args.output else Path("portfolio_health.json")
        json_path.write_text(json.dumps(report.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"JSON已保存: {json_path}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
