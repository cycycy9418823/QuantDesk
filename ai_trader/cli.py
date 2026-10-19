"""CLI for the independent AI paper trader."""

from __future__ import annotations

import argparse
import json
import sys

from pathlib import Path

from ai_trader.app import main as run_app
from ai_trader.cycle import run_cycle
from ai_trader.decision_log import ingest_state
from ai_trader.ledger import init_ledger, load_ledger
from ai_trader.paths import (
    DEFAULT_DAILY_DIR,
    FORBIDDEN_QUANTDESK_LEDGER,
    LEDGER_PATH,
    RESULTS_DIR,
    SNAPSHOT_PATH,
    STATE_DIR,
    profile_paths,
)


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
    app.add_argument("--allow-remote", action="store_true", help="显式允许非回环绑定；默认仅本机访问")
    comparison = sub.add_parser("compare-prompts", help="离线比较活动提示词与候选；不调用模型或改账本")
    comparison.add_argument("--context-json", required=True)
    comparison.add_argument("--out-dir", required=True)
    comparison.add_argument("--candidate", choices=["independent_v1"], default="independent_v1")
    decisions = sub.add_parser(
        "decisions",
        help="C线：把已有20:20日记核成可对账记录并刷新对照页，不调模型、不改菜单、不跑周期",
    )
    decisions.add_argument("--daily-dir", default="")
    decisions.add_argument("--state-dir", default="")
    decisions.add_argument("--plan", default="")
    decisions.add_argument("--journal", default="")
    decisions.add_argument("--out-dir", default="")
    decisions.add_argument("--as-of", default="")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "init":
        runtime = profile_paths()
        ledger = init_ledger(reset=False, path=runtime.ledger)
        _print(
            {
                "ok": True,
                "ledger_path": str(runtime.ledger),
                "forbidden_untouched": str(FORBIDDEN_QUANTDESK_LEDGER),
                "cash": ledger.get("cash"),
                "positions": ledger.get("positions"),
            }
        )
        return 0
    if args.command == "reset":
        if not args.yes:
            raise SystemExit("reset 需要 --yes，避免误清账")
        ledger = init_ledger(reset=True, path=profile_paths().ledger)
        _print({"ok": True, "cash": ledger.get("cash"), "reset": True})
        return 0
    if args.command == "run":
        _print(run_cycle(paths=profile_paths(), network=not args.offline))
        return 0
    if args.command == "status":
        snapshot_path = profile_paths().snapshot
        if snapshot_path.is_file():
            _print(json.loads(snapshot_path.read_text(encoding="utf-8")))
        else:
            _print({"ok": True, "status": "not_started", "ledger": load_ledger(profile_paths().ledger)})
        return 0
    if args.command == "app":
        run_app(host=args.host, port=args.port, allow_remote=args.allow_remote)
        return 0
    if args.command == "compare-prompts":
        from ai_trader.prompt_comparison import write_prompt_comparison

        context = json.loads(Path(args.context_json).read_text(encoding="utf-8"))
        if not isinstance(context, dict):
            raise SystemExit("--context-json 必须是本地 JSON 对象")
        _print(write_prompt_comparison(context, Path(args.out_dir), candidate=args.candidate))
        return 0
    if args.command == "decisions":
        runtime = profile_paths()
        payload = ingest_state(
            daily_dir=Path(args.daily_dir) if args.daily_dir else DEFAULT_DAILY_DIR,
            state_dir=Path(args.state_dir) if args.state_dir else runtime.ledger.parent,
            plan_path=Path(args.plan) if args.plan else runtime.plan_latest,
            journal_path=Path(args.journal) if args.journal else runtime.journal_latest,
            out_dir=Path(args.out_dir) if args.out_dir else runtime.decision_page_md.parent,
            as_of=args.as_of or None,
        )
        summary = {
            "ok": payload.get("ok"),
            "as_of": payload.get("as_of"),
            "disclaimer": "纸面 · 未验证",
            "counts": (payload.get("manifest") or {}).get("counts"),
            "page_html": (payload.get("paths") or {}).get("html"),
            "page_md": (payload.get("paths") or {}).get("md"),
            "log": (payload.get("paths") or {}).get("log"),
            "git_head": (payload.get("manifest") or {}).get("git_head"),
            "dirty_file_count": (payload.get("manifest") or {}).get("dirty_file_count"),
            "rejection_count": payload.get("rejection_count"),
        }
        _print(summary)
        return 0
    raise SystemExit(f"unknown command {args.command}")


if __name__ == "__main__":
    raise SystemExit(main())
