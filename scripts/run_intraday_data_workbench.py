from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from quantdesk.intraday.intraday_data_workbench import run_intraday_data_workbench
from quantdesk.intraday.intraday_providers import DEFAULT_INTRADAY_PROVIDER_ORDER
from quantdesk.shared import normalize_symbol


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run the real intraday 1-minute data workbench. User-triggered only.")
    parser.add_argument("--target-date", required=True)
    parser.add_argument("--source", choices=["current", "plans", "positions", "candidates", "symbols"], default="current")
    parser.add_argument("--symbols", default="")
    parser.add_argument("--try-provider", action="store_true")
    parser.add_argument("--providers", default=",".join(DEFAULT_INTRADAY_PROVIDER_ORDER))
    parser.add_argument("--import-path", default="")
    parser.add_argument("--format", default="auto", choices=["auto", "quantdesk", "tdx", "ths", "jq", "gm", "rq", "price-only"])
    parser.add_argument("--encoding", default="auto", choices=["auto", "utf-8", "utf-8-sig", "gbk", "gb18030"])
    parser.add_argument("--validate-only", action="store_true")
    parser.add_argument("--audit-only", action="store_true")
    parser.add_argument(
        "--execute-after-ready",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Run local paper-only conditional execution simulation after usable intraday data is found; use --no-execute-after-ready for audit-only operation.",
    )
    parser.add_argument("--update-portfolio", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    symbols = [normalize_symbol(item) for item in str(args.symbols or "").split(",") if item.strip()]
    providers = [item.strip() for item in str(args.providers or "").split(",") if item.strip()]
    payload = run_intraday_data_workbench(
        target_date=args.target_date,
        source="symbols" if symbols else args.source,
        symbols=symbols,
        try_provider=bool(args.try_provider),
        provider_order=providers,
        import_path=args.import_path or None,
        validate_only=bool(args.validate_only),
        audit_only=bool(args.audit_only),
        execute_after_ready=bool(args.execute_after_ready),
        update_portfolio=bool(args.update_portfolio),
        format=args.format,
        encoding=args.encoding,
        dry_run=bool(args.dry_run),
    )
    print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
    return 0 if payload.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())

