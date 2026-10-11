"""Chinese daily note for the human watching the trader."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ai_trader.mandate import BOUNDARY
from ai_trader.paths import JOURNAL_LATEST_PATH, JOURNAL_PATH, assert_not_quantdesk_ledger, ensure_runtime_dirs
from quantdesk.utils import atomic_write_json


def build_journal(
    *,
    as_of: str,
    research: dict[str, Any],
    actions: list[dict[str, Any]],
    ledger: dict[str, Any],
    warnings: list[str],
    trade_date: str = "",
    llm: dict[str, Any] | None = None,
    philosophy: dict[str, Any] | None = None,
    committed_buy_symbols: list[str] | None = None,
) -> dict[str, Any]:
    live = research.get("live")
    evaluated = list(research.get("evaluated") or [])
    filled = [row for row in actions if row.get("status") == "filled"]
    rejected = [row for row in actions if row.get("status") == "rejected"]
    equity = float(ledger.get("equity") or 0.0)
    cash = float(ledger.get("cash") or 0.0)
    initial = float(ledger.get("initial_cash") or 100000.0)
    pnl = round(equity - initial, 2)
    if live:
        headline = f"今天用「{live.get('title')}」管账。"
        thought = str(live.get("reason") or "")
    else:
        headline = "没有方法通过成本门。若排得出名单，会开未验证观察仓，而不是永远空仓。"
        thought = "我试了几类常见想法，扣掉买卖成本后样本外都站不住。观察仓会单独标记，不当成已验证方法。"
    lessons: list[str] = []
    for item in evaluated:
        status = str(item.get("status") or "")
        if status in {"rejected", "killed", "insufficient_evidence"}:
            lessons.append(f"{item.get('title')}：{item.get('reason')}")
    if live:
        lessons.insert(0, f"目前唯一活着的方法是：{live.get('title')}。后面几天用真实行情记账，亏了就杀掉。")
    if not lessons:
        lessons.append("今天没有新结论。")
    # When the trader's own brain made the call, the journal must show ITS reasoning
    # and ITS lesson. Previously this block was absent entirely, so the note the user
    # reads each night showed only the rule engine's narration — the decision was the
    # AI's, but the record made it look like a hardcoded ranking.
    brain = dict(llm or {})
    stance = dict((philosophy or {}).get("stance") or {})
    if brain.get("used"):
        picked = list(brain.get("picked") or [])
        styles = dict(brain.get("styles") or {})
        sold = list(brain.get("sells_requested") or [])
        # `picked` is what the brain PROPOSED; account constraints may have
        # kept only part of it. The journal must never present a withheld
        # proposal as a submitted order.
        committed = list(committed_buy_symbols) if committed_buy_symbols is not None else picked
        committed_set = set(committed)
        parts: list[str] = []
        committed_picks = [sym for sym in picked if sym in committed_set]
        withheld_picks = [sym for sym in picked if sym not in committed_set]
        if committed_picks:
            parts.append(
                "提交买入 " + "、".join(
                    f"{sym}({styles.get(sym) or '未标注'})" for sym in committed_picks
                )
            )
        elif picked:
            parts.append("没有新开仓")
        if sold:
            parts.append("卖出 " + "、".join(sold))
        if not parts:
            parts.append("今晚不动手，继续观望")
        headline = f"我自己决定：{'；'.join(parts)}。"
        for sym in withheld_picks:
            headline += (
                f"（曾提议 {sym}({styles.get(sym) or '未标注'})，"
                "未通过账户约束，仅作记录、未提交。）"
            )
        thought = str(brain.get("market_note") or "") or thought
        if brain.get("lesson"):
            lessons.insert(0, f"我今天想明白的：{brain['lesson']}")
        if stance.get("summary"):
            lessons.insert(0, f"我现在的投资主张：{stance['summary']}")
    elif brain.get("unavailable_reason"):
        # Silent degradation to the rule engine must be visible, or the user cannot
        # tell whether they are reading the AI's judgement or a fallback ranking.
        headline = f"（大脑没接上，今晚按规则引擎走）{headline}"
        lessons.insert(0, f"大脑未启用原因：{brain['unavailable_reason']}")
    return {
        "as_of": as_of,
        # The session tonight's plan will execute against. Previously absent here,
        # so readers of journal_latest.json saw trade_date: None while the plan had
        # it — the record you read to check the trader was missing the field that
        # says which session it acted on.
        "trade_date": str(trade_date or ""),
        "headline": headline,
        "thought": thought,
        "methods": [
            {
                "hypothesis_id": item.get("hypothesis_id"),
                "title": item.get("title"),
                "status": item.get("status"),
                "reason": item.get("reason"),
                "holdout_mean_net": item.get("holdout_mean_net"),
            }
            for item in evaluated
        ],
        "actions": actions,
        "filled_count": len(filled),
        "rejected_count": len(rejected),
        "account": {
            "equity": equity,
            "cash": cash,
            "market_value": float(ledger.get("market_value") or 0.0),
            "pnl": pnl,
            "pnl_pct": round(pnl / initial, 6) if initial else 0.0,
            "holdings": len(ledger.get("positions") or []),
        },
        "lessons": lessons,
        # Who actually decided tonight, and why it may not have been the brain.
        # Without this the journal reads identically whether the LLM chose the
        # trades or silently fell back to the rule engine.
        "llm": {
            "used": bool((llm or {}).get("used")),
            "model": str((llm or {}).get("model") or ""),
            "unavailable_reason": str((llm or {}).get("unavailable_reason") or ""),
            "market_note": str((llm or {}).get("market_note") or ""),
            "picked": list((llm or {}).get("picked") or []),
            "styles": dict((llm or {}).get("styles") or {}),
            "sells_requested": list((llm or {}).get("sells_requested") or []),
            "lesson": str((llm or {}).get("lesson") or ""),
        },
        # Its own current stance, so one file answers "what does it believe now".
        # `summary`/`preferred_styles` live under `stance`, and `revisions` is the
        # append-only list of past stances -- reading either from the top level
        # silently blanked the belief and crashed on the list.
        "philosophy": {
            "summary": str(stance.get("summary") or ""),
            "preferred_styles": list(stance.get("preferred_styles") or []),
            "revisions": len(list((philosophy or {}).get("revisions") or [])),
            "notes": len(list((philosophy or {}).get("notes") or [])),
        },
        "warnings": warnings[-20:],
        "boundary": dict(BOUNDARY),
        "disclaimer": "纸面模拟，不是投资建议，不接券商。盈亏只说明这只交易员自己的本事。",
    }


def _journal_identity(note: dict[str, Any]) -> str:
    """Stable per-evening identity so a same-cycle retry is not re-journalled."""

    return "|".join(
        [
            str(note.get("as_of") or ""),
            str(note.get("run_id") or ""),
            str(note.get("plan_id") or ""),
        ]
    ).strip("|")


def append_journal(
    note: dict[str, Any],
    *,
    latest_path: Path | None = None,
    log_path: Path | None = None,
) -> dict[str, Any]:
    if latest_path is None or log_path is None:
        ensure_runtime_dirs()
    latest = assert_not_quantdesk_ledger(latest_path or JOURNAL_LATEST_PATH)
    log = assert_not_quantdesk_ledger(log_path or JOURNAL_PATH)
    latest.parent.mkdir(parents=True, exist_ok=True)
    log.parent.mkdir(parents=True, exist_ok=True)
    identity = _journal_identity(note)
    if identity and log.is_file():
        for line in log.read_text(encoding="utf-8").splitlines():
            text = line.strip()
            if not text:
                continue
            try:
                existing = json.loads(text)
            except json.JSONDecodeError:
                continue
            if isinstance(existing, dict) and _journal_identity(existing) == identity:
                # Same evening/run already journalled (e.g. a crash-retry): reuse that
                # record for BOTH the log and ``journal_latest``.  Otherwise a re-entry
                # (already-settled plan → no new fills tonight) would overwrite
                # "今晚成交 1 笔" with "0 笔", and the newest view would contradict both
                # the history row and the ledger (T19).
                atomic_write_json(latest, existing)
                return existing
    atomic_write_json(latest, note)
    with log.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(note, ensure_ascii=False) + "\n")
    return note
