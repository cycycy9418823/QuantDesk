"""Ask the AI trader a question and get an answer, in its own voice.

This is the read side of the trader's brain. The nightly cycle (overnight.py)
is where decisions happen; this module only *explains*. It deliberately cannot
buy, sell, or touch the ledger:

- It imports no write path. Nothing here opens the ledger for writing.
- The answer is free text, not a plan. Nothing downstream parses it into orders.
- Position changes stay on the one audited path: the 20:20 nightly run, where
  every lot/cash/turnover/limit rule is enforced. A chat reply that could move
  the account would bypass exactly the constraints that make the book truthful.

So "what do you think of my holding" gets a real opinion; "sell it then" gets an
explanation of what it would do tonight, not a fill.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

from ai_trader.llm import _call, load_llm_config
from ai_trader.paths import (
    JOURNAL_PATH,
    LEDGER_PATH,
    PLAN_LATEST_PATH,
    STATE_DIR,
)
from ai_trader.philosophy import PHILOSOPHY_PATH

CHAT_LOG_PATH = STATE_DIR / "chat.jsonl"

# How much prior conversation to replay. Enough that it remembers the thread,
# bounded so a long history cannot crowd the account context out of the prompt.
HISTORY_TURNS = 8
MAX_QUESTION_CHARS = 2000


def _read_json(path: Path) -> Any:
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def load_history(limit: int = HISTORY_TURNS) -> list[dict[str, Any]]:
    """Recent chat turns, oldest first. Missing or corrupt lines are skipped."""

    if not CHAT_LOG_PATH.is_file():
        return []
    rows: list[dict[str, Any]] = []
    try:
        for raw in CHAT_LOG_PATH.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except (json.JSONDecodeError, ValueError):
                continue
            if isinstance(row, dict) and row.get("question"):
                rows.append(row)
    except OSError:
        return []
    return rows[-limit:] if limit > 0 else rows


def append_turn(question: str, answer: str, model: str) -> None:
    """Append one exchange. Best-effort: a log failure must not lose the answer."""

    STATE_DIR.mkdir(parents=True, exist_ok=True)
    row = {
        "at": datetime.now().isoformat(timespec="seconds"),
        "question": question,
        "answer": answer,
        "model": model,
    }
    try:
        with CHAT_LOG_PATH.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
    except OSError:
        pass


def _account_context() -> list[str]:
    """The trader's own current state, so answers reference the real book."""

    lines: list[str] = []
    ledger = _read_json(LEDGER_PATH) or {}
    if ledger:
        equity = ledger.get("equity")
        cash = ledger.get("cash")
        positions = ledger.get("positions") or []
        lines.append(
            f"你的账户：权益 {equity}，现金 {cash}，持仓 {len(positions)} 只。"
        )
        for pos in positions[:20]:
            if not isinstance(pos, dict):
                continue
            symbol = pos.get("symbol") or "?"
            qty = pos.get("quantity")
            cost = pos.get("avg_cost")
            mark = pos.get("market_price")
            pnl = pos.get("unrealized_pnl")
            style = pos.get("style") or "未标注"
            held = pos.get("days_held")
            lines.append(
                f"  持仓 {symbol}：{qty} 股，成本 {cost}，现价 {mark}，"
                f"浮动盈亏 {pnl}，做法 {style}，已持有 {held} 天。"
            )

    philosophy = _read_json(PHILOSOPHY_PATH) or {}
    stance = philosophy.get("stance") or {}
    if stance.get("summary"):
        styles = "、".join(stance.get("preferred_styles") or []) or "未定"
        lines.append(f"你自己写下的投资主张：{stance['summary']}（偏好：{styles}）")
    if stance.get("reasoning"):
        lines.append(f"你当时的理由：{stance['reasoning']}")
    ledger_by_style = philosophy.get("style_ledger") or {}
    if isinstance(ledger_by_style, dict) and ledger_by_style:
        parts = []
        for style, stat in ledger_by_style.items():
            if isinstance(stat, dict):
                parts.append(f"{style}: {stat}")
        if parts:
            lines.append("你各种做法到目前的真实战绩：" + "；".join(parts[:8]))
    notes = philosophy.get("notes") or []
    if isinstance(notes, list) and notes:
        recent = [str(n) for n in notes[-3:] if n]
        if recent:
            lines.append("你最近记下的教训：" + "；".join(recent))

    plan = _read_json(PLAN_LATEST_PATH) or {}
    if plan:
        as_of = plan.get("as_of") or plan.get("date")
        intents = plan.get("intents") or plan.get("buys") or []
        lines.append(f"你最近一次计划（{as_of}）里有 {len(intents)} 条买入意图。")
        llm_block = plan.get("llm") or {}
        if isinstance(llm_block, dict) and llm_block.get("reason"):
            lines.append(f"上次大脑未生效的原因：{llm_block.get('reason')}")

    return lines


def _recent_journal(limit: int = 3) -> list[str]:
    """The last few nightly notes, so it can refer to what it actually said."""

    if not JOURNAL_PATH.is_file():
        return []
    rows: list[dict[str, Any]] = []
    try:
        for raw in JOURNAL_PATH.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except (json.JSONDecodeError, ValueError):
                continue
            if isinstance(row, dict):
                rows.append(row)
    except OSError:
        return []
    out: list[str] = []
    for row in rows[-limit:]:
        date = row.get("date") or "?"
        headline = row.get("headline") or ""
        thought = row.get("thought") or ""
        out.append(f"{date}：{headline} —— {thought}"[:400])
    return out


def _build_chat_prompt(question: str, history: list[dict[str, Any]]) -> tuple[str, str]:
    system = (
        "你是一名 A 股纸面交易员，正在和你的账户主人对话。"
        "你自己管着一个 10 万的纸面账户，风格由你自己决定，已经写下过自己的投资主张。"
        "\n\n"
        "这是纯纸面模拟，不接券商，不是投资建议。"
        "\n\n"
        "**你现在只能说话，不能下单。** 仓位只在每晚 20:20 的复盘流程里变动，"
        "那里有铁面的现金/持仓/涨跌停约束。所以如果主人让你买或卖，"
        "你可以说你今晚打算怎么做、为什么，但不要假装已经成交了。"
        "\n\n"
        "说人话，用中文，直接回答。别背免责声明，别客套。"
        "如果你不知道或者数据不支持，就直说不知道。"
        "如果主人问的是你自己的决定，就老实解释你当时怎么想的，"
        "包括你后来觉得判断错了的地方。"
    )

    lines: list[str] = []
    context = _account_context()
    if context:
        lines.append("【你现在的真实状态】")
        lines.extend(context)
        lines.append("")

    journal = _recent_journal()
    if journal:
        lines.append("【你最近几篇复盘笔记】")
        lines.extend(journal)
        lines.append("")

    if history:
        lines.append("【你和主人之前的对话】")
        for turn in history:
            q = str(turn.get("question") or "").strip()
            a = str(turn.get("answer") or "").strip()
            if q:
                lines.append(f"主人问：{q}")
            if a:
                lines.append(f"你答：{a[:600]}")
        lines.append("")

    lines.append("【主人现在问你】")
    lines.append(question.strip())

    return system, "\n".join(lines)


def answer_question(
    question: str,
    diagnostics: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Answer one question as the trader. None => brain unavailable.

    ``diagnostics`` receives the failure reason. Without it, "the endpoint is
    down" and "the trader had nothing to say" look identical in the UI, which is
    the same trap that once made a token-exhausted response look like silence.
    """

    text = (question or "").strip()
    if not text:
        if diagnostics is not None:
            diagnostics["reason"] = "empty_question"
        return None
    if len(text) > MAX_QUESTION_CHARS:
        text = text[:MAX_QUESTION_CHARS]

    # An interactive question IS the explicit opt-in for the *LLM gate*, so
    # the scheduled-run flag is not required here. A NON-loopback endpoint
    # still needs its own external-allow flag; the concrete reason (e.g.
    # external_llm_not_explicitly_allowed) is carried back in diagnostics.
    cfg = load_llm_config(explicit_optin=True, diagnostics=diagnostics)
    if cfg is None:
        if diagnostics is not None and not diagnostics.get("reason"):
            diagnostics["reason"] = "unconfigured"
        return None

    history = load_history()
    system, user = _build_chat_prompt(text, history)
    reply = _call(system, user, cfg, diagnostics)
    if not reply:
        return None

    append_turn(text, reply, cfg["model"])
    return {"answer": reply, "model": cfg["model"]}


__all__ = ["CHAT_LOG_PATH", "answer_question", "load_history"]
