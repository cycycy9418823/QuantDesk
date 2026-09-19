"""Read-only side-by-side report for every AI-trader profile.

Reads each profile's ledger, decision log and latest plan and writes one
comparison artifact. It never writes any book and never feeds one book's
conclusions into another; the only thing the books share is market data.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from ai_trader.paths import (  # noqa: E402
    configured_profiles,
    profile_env_file,
    profile_paths,
)

COMPARE_DIR = PROJECT_ROOT / "results" / "ai_trader_compare"


def _read_json(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _model_for(profile: str) -> str:
    env = profile_env_file(profile)
    if not env.is_file():
        return ""
    match = re.search(r'export\s+AI_TRADER_LLM_MODEL\s*=\s*"?([^"\n]+)"?', env.read_text(encoding="utf-8"))
    return match.group(1).strip() if match else ""


def book_summary(profile: str) -> dict[str, Any]:
    runtime = profile_paths(profile)
    ledger = _read_json(runtime.ledger)
    decision = _read_json(runtime.decision_latest)
    plan = _read_json(runtime.plan_latest)
    positions = ledger.get("positions") or []
    held = sorted(str(row.get("symbol")) for row in positions if isinstance(row, dict) and row.get("symbol"))
    intents = [
        {"symbol": row.get("symbol"), "side": row.get("side")}
        for row in (plan.get("intents") or [])
        if isinstance(row, dict)
    ]
    return {
        "profile": profile or "default",
        "ledger_id": ledger.get("ledger_id") or "",
        "model": _model_for(profile),
        "as_of": ledger.get("as_of") or "",
        "updated_at": ledger.get("updated_at") or "",
        "cash": ledger.get("cash"),
        "equity": ledger.get("equity"),
        "market_value": ledger.get("market_value"),
        "unrealized_pnl": ledger.get("unrealized_pnl"),
        "realized_pnl": ledger.get("realized_pnl"),
        "position_count": len(positions),
        "held_symbols": held,
        "trade_count": len(ledger.get("trades") or []),
        "decision_count": decision.get("count"),
        "decision_results": (decision.get("counts") or None),
        "plan_id": plan.get("plan_id") or "",
        "plan_trade_date": plan.get("trade_date") or "",
        "plan_intents": intents,
        "paths": {
            "state": str(runtime.ledger.parent),
            "results": str(runtime.decision_page_md.parent),
        },
    }


def build_report(profiles: list[str] | None = None) -> dict[str, Any]:
    names = list(profiles) if profiles else configured_profiles()
    books = [book_summary(name) for name in names]
    lines = [
        "# AI 交易员 多脑对比 · 纸面 · 未验证",
        "",
        "> 只读汇总。两个脑 **只共享同一份行情数据**，账本、计划、日志、决策页各自隔离，互不可见。",
        "> 这不是投资建议，也不是订单；成交一律按次日开盘价（`next_open_only`）。",
        "",
        f"- 生成时间：{datetime.now().astimezone().isoformat(timespec='seconds')}",
        f"- 参与档位：{', '.join(book['profile'] for book in books)}",
        "",
        "## 账本对照",
        "",
        "| 档位 | 模型 | 账本ID | as_of | 现金 | 持仓市值 | 权益 | 浮动盈亏 | 已实现 | 持仓数 | 成交数 |",
        "| --- | --- | --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for book in books:
        lines.append(
            f"| {book['profile']} | {book['model'] or '-'} | {book['ledger_id'] or '-'} | {book['as_of'] or '-'} | "
            f"{book['cash']} | {book['market_value']} | {book['equity']} | {book['unrealized_pnl']} | "
            f"{book['realized_pnl']} | {book['position_count']} | {book['trade_count']} |"
        )

    lines += ["", "## 今晚计划（各自）", "", "| 档位 | plan_id | 交易日 | 计划操作 |", "| --- | --- | --- | --- |"]
    for book in books:
        intents = ", ".join(f"{item['symbol']}:{item['side']}" for item in book["plan_intents"]) or "-"
        lines.append(f"| {book['profile']} | {book['plan_id'] or '-'} | {book['plan_trade_date'] or '-'} | {intents} |")

    lines += ["", "## 持仓差异", ""]
    if len(books) == 2:
        left, right = books
        left_set, right_set = set(left["held_symbols"]), set(right["held_symbols"])
        lines += [
            f"- `{left['profile']}` 独有：{', '.join(sorted(left_set - right_set)) or '-'}",
            f"- `{right['profile']}` 独有：{', '.join(sorted(right_set - left_set)) or '-'}",
            f"- 共同持有：{', '.join(sorted(left_set & right_set)) or '-'}",
        ]
        try:
            delta = float(left.get("equity") or 0) - float(right.get("equity") or 0)
            lines.append(f"- 权益差（{left['profile']} - {right['profile']}）：{delta:.2f}")
        except (TypeError, ValueError):
            pass
    else:
        lines.append("- 需要恰好两个档位才能算差异。")

    lines += [
        "",
        "## 说明",
        "",
        "- 两边使用同一 mandate、同一门禁、同一成交模型；差异只来自脑子（模型）。",
        "- 样本量小的时候，差异基本是噪声；不要据此下结论。",
    ]
    return {"generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "books": books, "markdown": "\n".join(lines) + "\n"}


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("x", encoding="utf-8") as handle:
            handle.write(text)
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out-dir", default=str(COMPARE_DIR), type=Path)
    parser.add_argument("--json", default="true")
    args = parser.parse_args(argv)
    report = build_report()
    out_dir = Path(args.out_dir)
    _atomic_write(out_dir / "latest.md", report["markdown"])
    _atomic_write(out_dir / "latest.json", json.dumps(
        {key: value for key, value in report.items() if key != "markdown"}, ensure_ascii=False, indent=2) + "\n")
    if str(args.json).strip().lower() in {"1", "true", "yes", "on"}:
        print(json.dumps({"books": report["books"], "markdown_path": str(out_dir / "latest.md")}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
