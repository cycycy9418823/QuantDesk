from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from quantdesk.intraday.intraday_gap_checker import collect_intraday_required_symbols
from quantdesk.intraday.intraday_import import (
    DISCLAIMER,
    IMPORT_TEMPLATE_PATH,
    ensure_intraday_template_target_is_noncanonical,
)
from quantdesk.shared import normalize_symbol, write_json


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Generate a local 1-minute intraday import template. This command does not download data.")
    parser.add_argument("--source", choices=["current", "plans", "positions", "candidates", "symbols"], default="current")
    parser.add_argument("--target-date", default="")
    parser.add_argument("--output", default=str(IMPORT_TEMPLATE_PATH.relative_to(PROJECT_ROOT)))
    parser.add_argument("--symbols", default="")
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--overwrite", action="store_true")
    return parser


def generate_template(
    *,
    source: str = "current",
    target_date: str = "",
    output: str | Path | None = None,
    symbols: str | list[str] = "",
    limit: int = 0,
    overwrite: bool = False,
) -> dict:
    symbol_list = symbols if isinstance(symbols, list) else [item.strip() for item in str(symbols or "").split(",") if item.strip()]
    collected = collect_intraday_required_symbols(source=source, target_trade_date=target_date, symbols=symbol_list)
    rows = list(collected.get("items") or [])
    if limit and limit > 0:
        rows = rows[:limit]
    target = Path(output or IMPORT_TEMPLATE_PATH)
    if not target.is_absolute():
        target = PROJECT_ROOT / target
    target = ensure_intraday_template_target_is_noncanonical(target)
    if target.exists() and not overwrite:
        return {
            "ok": True,
            "exists": True,
            "path": str(target),
            "total_symbols": len(rows),
            "written_rows": 0,
            "message": "导入模板已存在，默认不覆盖。请使用 --overwrite 明确覆盖。",
            "disclaimer": DISCLAIMER,
        }
    target.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = ["symbol", "name", "datetime", "date", "time", "open", "high", "low", "close", "volume", "amount", "source", "notes"]
    with target.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow({
                "symbol": normalize_symbol(row.get("symbol")),
                "name": row.get("name") or row.get("symbol"),
                "datetime": "",
                "date": target_date or collected.get("target_trade_date") or "",
                "time": "",
                "open": "",
                "high": "",
                "low": "",
                "close": "",
                "volume": "",
                "amount": "",
                "source": "manual_import",
                "notes": "请填入真实 1 分钟线；本行不是假数据。",
            })
    readme_path = target.parent / "README_intraday_import.md"
    readme_path.parent.mkdir(parents=True, exist_ok=True)
    readme_text = "\n".join([
            "# 1 分钟线 CSV 导入说明",
            "",
            "这是给真实 1 分钟线数据使用的导入模板。CSV 里不要写说明行，只保留表头和真实数据。",
            "",
            "最低要求：symbol + datetime + close。",
            "推荐要求：symbol + datetime + open/high/low/close/volume/amount。",
            "如果只有 time，没有 date，请在导入命令里指定 --target-date。",
            "如果只有 price，系统会临时填充 open/high/low/close，但会标记 price-only，可靠性较低。",
            "",
            "支持 QuantDesk、通达信、同花顺、聚宽、掘金、米筐等常见字段，也支持 utf-8、gbk、gb18030。",
            "",
            "示例：",
            "python scripts/import_intraday_1m.py --input data/intraday/import/intraday_1m_import.csv --target-date 2026-05-15 --audit-after-import",
            "python scripts/run_intraday_data_workbench.py --target-date 2026-05-15 --import-path data/intraday/import/intraday_1m_import.csv --execute-after-ready",
            "",
            "不要把日线 CSV 当成分钟线。系统不会用日线收盘价、最高价或最低价假装盘中成交。",
            "风险提示：不接券商、不真实下单、不构成投资建议。",
        ])
    if overwrite or not readme_path.exists():
        readme_path.write_text(readme_text, encoding="utf-8")
    payload = {
        "ok": True,
        "path": str(target),
        "source": source,
        "target_trade_date": target_date or collected.get("target_trade_date") or "",
        "total_symbols": len(rows),
        "written_rows": len(rows),
        "symbols": [normalize_symbol(row.get("symbol")) for row in rows],
        "readme_path": str(readme_path),
        "message": "已生成当前候选/持仓分钟线导入模板。请把真实 1 分钟线填入模板后，再运行导入命令。",
        "disclaimer": DISCLAIMER,
    }
    write_json(PROJECT_ROOT / "results" / "intraday_import_template_latest.json", payload)
    return payload


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    payload = generate_template(
        source=args.source,
        target_date=args.target_date,
        output=args.output,
        symbols=args.symbols,
        limit=args.limit,
        overwrite=args.overwrite,
    )
    print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
    return 0 if payload.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
