"""CLI for the independent AI paper trader."""

from __future__ import annotations

import argparse
import json
import sys

from ai_trader.app import main as run_app
from ai_trader.cycle import run_cycle
from ai_trader.ledger import init_ledger, load_ledger
from ai_trader.paths import FORBIDDEN_QUANTDESK_LEDGER, LEDGER_PATH, SNAPSHOT_PATH


def _print(payload: object) -> None:
    json.dump(payload, sys.stdout, ensure_ascii=False, indent=2)
    sys.stdout.write("\n")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m ai_trader",
        description="独立10万纸面AI交易员。不写 QuantDesk 旧账，不接券商。",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("init", help="开一本10万空账，已存在则不覆盖")
    reset = sub.add_parser("reset", help="清空这只交易员的账，重开10万现金")
    reset.add_argument("--yes", action="store_true")
    run = sub.add_parser("run", help="今晚复盘：验收上一笔开盘计划，并布置次日开盘指令")
    run.add_argument("--offline", action="store_true", help="不联网，只读本地缓存公告")
    sub.add_parser("status", help="打印最新快照")
    app = sub.add_parser("app", help="打开8001教室页")
    app.add_argument("--host", default="127.0.0.1")
    app.add_argument("--port", type=int, default=8001)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "init":
        ledger = init_ledger(reset=False)
        _print(
            {
                "ok": True,
                "ledger_path": str(LEDGER_PATH),
                "forbidden_untouched": str(FORBIDDEN_QUANTDESK_LEDGER),
                "cash": ledger.get("cash"),
                "positions": ledger.get("positions"),
            }
        )
        return 0
    if args.command == "reset":
        if not args.yes:
            raise SystemExit("reset 需要 --yes，避免误清账")
        ledger = init_ledger(reset=True)
        _print({"ok": True, "cash": ledger.get("cash"), "reset": True})
        return 0
    if args.command == "run":
        _print(run_cycle(network=not args.offline))
        return 0
    if args.command == "status":
        if SNAPSHOT_PATH.is_file():
            _print(json.loads(SNAPSHOT_PATH.read_text(encoding="utf-8")))
        else:
            _print({"ok": True, "status": "not_started", "ledger": load_ledger()})
        return 0
    if args.command == "app":
        run_app(host=args.host, port=args.port)
        return 0
    raise SystemExit(f"unknown command {args.command}")


if __name__ == "__main__":
    raise SystemExit(main())
