"""Weekly review for the owner (CHARTER §8): one week, two books, the worst three closes.

Read-only. Delivered inside the first morning brief of the following week, so no
new scheduled task is needed. Every number is read from existing files; a gap
stays a gap ("读不到") and is never estimated.
"""

from __future__ import annotations

import csv
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Mapping

from quantdesk import owner_view as ov
from quantdesk.research import exit_rule_replay as replay

QUESTION_COUNT = 3


def _closes(path: Path) -> dict[str, float]:
    rows: dict[str, float] = {}
    try:
        with path.open(encoding="utf-8-sig", newline="") as handle:
            for row in csv.DictReader(handle):
                day = ov.plain_day(row.get("date"))
                close = ov.finite(row.get("close"))
                if day and close:
                    rows[day] = close
    except (OSError, UnicodeError, csv.Error):
        return {}
    return rows


def week_bounds(day: str) -> tuple[str, str]:
    parsed = date.fromisoformat(day)
    monday = parsed - timedelta(days=parsed.weekday())
    return monday.isoformat(), (monday + timedelta(days=4)).isoformat()


def is_first_session_of_week(previous_session: str, today: date) -> bool:
    """True when the last session belongs to an earlier ISO week than today."""

    if not ov.plain_day(previous_session):
        return False
    return date.fromisoformat(previous_session).isocalendar()[:2] != today.isocalendar()[:2]


def entry_note(root: Path, symbol: str, entry_day: str) -> str:
    """One stock-specific sentence about the entry, computed from the raw daily file."""

    try:
        bars = replay.load_bars(Path(root) / "data" / "daily" / f"{symbol}.csv")
    except replay.EvidenceError:
        return "入场行情或价格口径未核实，说不清"
    closes = {bar.day: bar.close for bar in bars if bar.close > 0}
    days = sorted(day for day in closes if day <= entry_day)
    if not days or days[-1] != entry_day or len(days) < 21:
        return "入场当时的行情不够 20 天，说不清"
    window = [closes[day] for day in days[-20:]]
    close = closes[entry_day]
    ma20 = sum(window) / 20
    change20 = (close / closes[days[-21]] - 1.0) * 100.0
    return f"入场时收盘 {close:.2f}，比 20 日均线高 {(close / ma20 - 1.0) * 100.0:+.1f}%，此前 20 天涨跌 {change20:+.1f}%"


def build_weekly_review(root: Path, as_of: str) -> dict[str, Any]:
    root = Path(root)
    monday, friday = week_bounds(as_of)
    history = ov.load_json(root / "results" / "auto_paper_portfolio_history.json") or {}
    records = {}
    for item in history.get("items") or []:
        if isinstance(item, Mapping) and ov.plain_day(item.get("valuation_date")) and ov.finite(item.get("total_equity")):
            records[ov.plain_day(item["valuation_date"])] = ov.finite(item["total_equity"])
    latest = ov.load_json(root / "results" / "auto_paper_portfolio_latest.json") or {}
    summary = latest.get("summary") if isinstance(latest.get("summary"), Mapping) else {}
    if ov.plain_day(latest.get("valuation_date")) and ov.finite(summary.get("total_equity")):
        records.setdefault(ov.plain_day(latest["valuation_date"]), ov.finite(summary["total_equity"]))
    index = _closes(root / "data" / "benchmark" / "hs300.csv")
    in_week = sorted(day for day in index if monday <= day <= friday)
    before = sorted(day for day in index if day < monday)
    review: dict[str, Any] = {"week_start": monday, "week_end": friday, "sessions": in_week,
                              "strategy_pct": None, "index_pct": None, "missing_valuation": []}
    if in_week and before:
        start, end = before[-1], in_week[-1]
        review["index_pct"] = round((index[end] / index[start] - 1.0) * 100.0, 4)
        review["start"], review["end"] = start, end
        if start in records and end in records:
            review["strategy_pct"] = round((records[end] / records[start] - 1.0) * 100.0, 4)
        review["missing_valuation"] = [day for day in [start, *in_week] if day not in records]
    closed = []
    for item in latest.get("closed_positions") or []:
        if not isinstance(item, Mapping):
            continue
        exit_day = ov.plain_day(item.get("exit_date"))
        if not (monday <= exit_day <= friday):
            continue
        entry_price = replay.fill_price(item)
        exit_price = ov.finite(item.get("exit_price"))
        pct = ((exit_price / entry_price - 1.0) * 100.0) if entry_price and exit_price else None
        closed.append({"symbol": str(item.get("symbol") or ""), "name": str(item.get("name") or ""),
                       "entry_date": ov.plain_day(item.get("entry_date")), "entry_price": entry_price,
                       "exit_date": exit_day, "exit_price": exit_price, "pct": pct,
                       "pnl": ov.finite(item.get("realized_pnl")) if item.get("realized_pnl") is not None else None,
                       "reason": ov._short_exit_reason(str(item.get("exit_reason") or ""))})
    review["book_readable"] = bool(latest)
    review["closed_count"] = len(closed)
    review["closed_pnl"] = round(sum(row["pnl"] or 0.0 for row in closed), 2) if closed else 0.0
    worst = sorted((row for row in closed if row["pct"] is not None), key=lambda row: row["pct"])[:3]
    for row in worst:
        row["entry_note"] = entry_note(root, row["symbol"], row["entry_date"])
    review["worst"] = worst
    review["opened_count"] = sum(1 for item in latest.get("positions") or []
                                 if isinstance(item, Mapping) and monday <= ov.plain_day(item.get("entry_date")) <= friday)
    review["questions"] = _questions(review)
    return review


def _questions(review: Mapping[str, Any]) -> list[str]:
    questions: list[str] = []
    worst = review.get("worst") or []
    beyond = [row for row in worst if row["pct"] is not None and row["pct"] < ov.STOP_LINE_PCT - 1.0]
    if beyond:
        row = beyond[0]
        questions.append(f"{row['name']}的止损线是 -8%，实际亏了 {abs(row['pct']):.1f}%。为什么会亏得比止损线还多？（提示：程序只在收盘后检查）")
    quick = [row for row in worst if row.get("entry_date") and row.get("exit_date")
             and (date.fromisoformat(row["exit_date"]) - date.fromisoformat(row["entry_date"])).days <= 4]
    if quick:
        questions.append(f"{quick[0]['name']}买进没几天就止损了。在「刚站上 20 日均线」时进场，算不算追高？")
    strategy, index = review.get("strategy_pct"), review.get("index_pct")
    if strategy is not None and index is not None:
        relation = "跑赢" if strategy > index else "跑输"
        questions.append(f"这周策略账 {strategy:+.1f}%，沪深300 {index:+.1f}%，{relation}了。对比表把股价表现和费用拆开后，哪些原因还需要验证？")
    questions.append("对比表固定拿5天的样本，哪些已经满5天？扣费前后的差距有多大？历史平均不能直接当成上周账本收益。")
    return questions[:QUESTION_COUNT]


def render_weekly_review(review: Mapping[str, Any]) -> str:
    start, end = review.get("week_start", ""), review.get("week_end", "")
    lines = [f"**上周回顾（{ov.short_day(start)}–{ov.short_day(end)}）**"]
    strategy, index = review.get("strategy_pct"), review.get("index_pct")
    lines.append(f"- 策略账 {ov.signed_pct(strategy, 1)}，沪深300 {ov.signed_pct(index, 1)}")
    if review.get("missing_valuation"):
        lines.append(f"- 缺估值记录的日子：{'、'.join(ov.short_day(day) for day in review['missing_valuation'])}（不补）")
    if review.get("book_readable"):
        lines.append(f"- 新开 {review.get('opened_count', 0)} 只；平掉 {review.get('closed_count', 0)} 只，合计 {review.get('closed_pnl', 0.0):+,.0f} 元")
    else:
        lines.append("- 新开和平掉的记录：读不到（策略账展示文件不可读）")
    worst = review.get("worst") or []
    if worst:
        lines.append("- 亏得最多的：")
        for row in worst:
            lines.append(f"  - {row['name']}：{ov.short_day(row['entry_date'])} 进 {row['entry_price']:.2f}，"
                         f"{ov.short_day(row['exit_date'])} 出 {row['exit_price']:.2f}，{row['pct']:+.1f}%（{row['reason']}）。"
                         f"{row.get('entry_note', '')}")
    lines += ["", "**想三个问题**（在 Claude 对话里随口回答就行，我照录原话，你确认后存进学习日志）"]
    lines += [f"{number}. {text}" for number, text in enumerate(review.get("questions") or [], 1)]
    return "\n".join(ov.clean_line(line) for line in lines)
