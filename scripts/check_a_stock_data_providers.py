from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from quantdesk.a_stock_data_matrix import build_provider_matrix_payload
from quantdesk.provider_network_smoke import run_provider_network_smoke
from quantdesk.shared import truthy


def _split_csv(value: str) -> list[str]:
    return [item.strip() for item in str(value or "").split(",") if item.strip()]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Check QuantDesk A-share provider matrix.")
    parser.add_argument("--categories", default="", help="Comma-separated categories, e.g. quote,daily,intraday,news.")
    parser.add_argument("--symbols", default="", help="Comma-separated symbols. Default does not scan the full market.")
    parser.add_argument("--target-date", default="")
    parser.add_argument("--network", default="false", help="true/false. Default network=false; false performs no external requests.")
    parser.add_argument("--providers", default="tencent", help="Provider smoke list when --network true, e.g. tencent,mootdx.")
    parser.add_argument("--json", action="store_true", help="Print JSON only.")
    args = parser.parse_args(argv)

    category_aliases = {
        "quote": "market_quote",
        "daily": "daily_kline",
        "intraday": "intraday_kline",
        "reports": "research_report",
        "research": "research_report",
        "fundamental": "fundamentals",
        "fundamentals": "fundamentals",
        "announcement": "announcement",
        "news": "news",
        "hotspot": "hot_theme",
        "theme": "hot_theme",
        "concept": "concept_map",
    }
    categories = [category_aliases.get(item, item) for item in _split_csv(args.categories)]
    symbols = _split_csv(args.symbols)
    network = truthy(args.network)
    payload = build_provider_matrix_payload(
        categories=categories or None,
        symbols=symbols,
        target_date=args.target_date,
        network=network,
        write=True,
    )
    if network:
        smoke = run_provider_network_smoke(
            symbols=symbols,
            target_date=args.target_date,
            network=True,
            limit=3,
            write=True,
            providers=args.providers,
        )
        payload["provider_network_smoke"] = {
            "summary": smoke.get("summary") or {},
            "path": smoke.get("path") or "",
            "steps": smoke.get("steps") or [],
        }
    if args.json:
        print(json.dumps(payload, ensure_ascii=True, indent=2))
    else:
        summary = payload.get("summary") or {}
        print("QuantDesk A-share provider matrix")
        print(f"network={payload.get('network')} full_scan={payload.get('full_scan')} target_date={payload.get('target_date') or '-'}")
        print(f"providers={summary.get('total', 0)} output={payload.get('path')}")
        for category, row in sorted((summary.get("by_category") or {}).items()):
            print(f"- {category}: {', '.join(str(x) for x in row.get('providers', []))}")
        mootdx_rows = [row for row in payload.get("providers", []) if str(row.get("provider") or row.get("name") or "").lower() == "mootdx"]
        if mootdx_rows and not any(bool(row.get("dependency_available")) for row in mootdx_rows):
            print("mootdx: 未安装 mootdx，可继续使用腾讯 provider；如需启用 mootdx，请按 docs/provider_setup.md 安装。")
        if network and payload.get("provider_network_smoke"):
            smoke_summary = payload["provider_network_smoke"].get("summary") or {}
            print(
                "provider smoke: "
                f"ok={smoke_summary.get('ok', 0)} "
                f"partial={smoke_summary.get('partial', 0)} "
                f"skipped={smoke_summary.get('skipped', 0)} "
                f"failed={smoke_summary.get('failed', 0)}"
            )
        print(payload.get("disclaimer", "仅用于本地学习研究，不构成投资建议。"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
