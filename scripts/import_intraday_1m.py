from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from quantdesk.intraday.intraday_import import (
    check_intraday_1m,
    create_intraday_template,
    import_intraday_1m,
    import_intraday_1m_file,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Import or check local 1-minute intraday CSV files. This command does not download data.")
    parser.add_argument("--input", help="Input CSV with one or more symbols.")
    parser.add_argument("--symbol", help="A-share symbol, for example 601778.")
    parser.add_argument("--target-date", default="")
    parser.add_argument("--source", default="", help="Source label for --input mode, or legacy source CSV path when --input is omitted.")
    parser.add_argument("--source-label", default="manual_import")
    parser.add_argument("--overwrite", action="store_true")
    parser.add_argument(
        "--append",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Append to existing local intraday CSV by default; use --no-append to replace during an explicit import.",
    )
    parser.add_argument("--validate-only", action="store_true")
    parser.add_argument("--format", default="auto", choices=["auto", "quantdesk", "tdx", "ths", "jq", "gm", "rq", "price-only"])
    parser.add_argument("--encoding", default="auto", choices=["auto", "utf-8", "utf-8-sig", "gbk", "gb18030"])
    parser.add_argument("--audit-after-import", action="store_true")
    parser.add_argument("--execute-after-import", action="store_true")
    parser.add_argument("--update-portfolio-after-execution", action="store_true")
    parser.add_argument("--write-workbench-result", action="store_true")
    parser.add_argument("--check", action="store_true", help="Check data/intraday/1m/{symbol}.csv")
    parser.add_argument("--create-template", action="store_true", help="Create templates/intraday_1m_template.csv")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.create_template:
        path = create_intraday_template()
        print(json.dumps({"ok": True, "template_path": str(path), "message": "分钟线导入模板已生成"}, ensure_ascii=False, indent=2))
        return 0
    if args.input:
        payload = import_intraday_1m_file(
            args.input,
            target_date=args.target_date,
            symbol=args.symbol or "",
            source=args.source or args.source_label or "manual_import",
            append=bool(args.append),
            overwrite=bool(args.overwrite),
            validate_only=bool(args.validate_only),
            encoding=str(args.encoding or "auto"),
            input_format=str(args.format or "auto"),
            audit_after_import=bool(args.audit_after_import),
            execute_after_import=bool(args.execute_after_import),
            update_portfolio_after_execution=bool(args.update_portfolio_after_execution),
        )
        print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
        return 0 if payload.get("ok") else 1
    if not args.symbol:
        print(json.dumps({"ok": False, "message": "请提供 --symbol，或使用 --input 导入 CSV，或使用 --create-template 生成模板。"}, ensure_ascii=False, indent=2))
        return 2
    if args.check or not args.source:
        payload = check_intraday_1m(args.symbol, target_date=args.target_date)
    else:
        payload = import_intraday_1m(args.symbol, args.source)
    print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
    return 0 if payload.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
