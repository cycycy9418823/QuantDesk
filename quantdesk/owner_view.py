"""Owner-facing plain-language summaries (CHARTER §7, §8).

Read-only helpers shared by the morning brief and the health line. Every number
here comes from an existing artifact on disk; a missing or unreadable input
turns into an explicit "读不到" sentence, never into a fabricated zero.
"""

from __future__ import annotations

import csv
import json
import math
import re
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any, Mapping
from zoneinfo import ZoneInfo

from quantdesk.snapshot_history import expected_snapshot_slots, read_verified_snapshot_archive

# CHARTER §12 #18 (2026-10-31): a sheet counts when >= 99% of its rows carry a
# usable same-day price; a day is green with at least 46 such sheets.
SNAPSHOT_SHEET_VALID_RATIO = 0.99
SNAPSHOT_MIN_GOOD_SHEETS = 46
SNAPSHOT_EXPECTED_SHEETS = 48
# The strategy book exits at -8% from the buy price; -6% is "close to it".
STOP_LINE_PCT = -8.0
NEAR_STOP_PCT = -6.0
# CHARTER §12 #13 (2026-10-31): the AI books are on hold, rule fallback runs nightly.
AI_ON_HOLD_SINCE = "2026-10-31"
AI_BOOKS = (("默认档", "state/ai_trader"), ("第二档", "state/ai_trader_glm"))
PAPER_NOTE = "纸面研究，不是买卖建议。"
# Same list the morning brief enforces: the push never reads like an order.
PROHIBITED_TERMS = ("买入", "卖出", "仓位", "目标价", "止损价")
OPEN_ACTIONS = {"buy", "add", "breakout_add"}
CLOSE_ACTIONS = {"sell", "sell_all", "stop_loss", "reduce_half", "take_profit"}
HEALTH_HISTORY = Path("results") / "health_line" / "history"


def load_json(path: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def finite(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def plain_day(value: Any) -> str:
    text = str(value or "").strip()[:10]
    try:
        return date.fromisoformat(text).isoformat()
    except ValueError:
        return ""


def short_day(day: str) -> str:
    """``2026-11-02`` -> ``11/2`` for phone text."""

    try:
        parsed = date.fromisoformat(day)
    except (TypeError, ValueError):
        return day or "?"
    return f"{parsed.month}/{parsed.day}"


def signed_pct(value: float | None, digits: int = 2) -> str:
    return "读不到" if value is None else f"{value:+.{digits}f}%"


def clean_line(text: str) -> str:
    """Drop a line rather than publish anything that reads like an order."""

    return "" if any(term in text for term in PROHIBITED_TERMS) else text


def _count(value: Any) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else 0


# --------------------------------------------------------------------------
# Snapshot sheets under the owner's rule
# --------------------------------------------------------------------------

def snapshot_sheet_quality(root: Path, day: str) -> dict[str, Any]:
    """Count sheets whose usable-price share reaches 99% (CHARTER §12 #18).

    Usable = a unique expected symbol with a finite positive price and an exact
    same-day quote date. Archive integrity is checked before counting CSV rows;
    sidecar quality counters and full-OHLCV completeness are not this policy.
    """

    result: dict[str, Any] = {
        "day": day, "sheets": 0, "good": 0, "expected": SNAPSHOT_EXPECTED_SHEETS,
        "min_ratio": None, "worst_slot": "", "ok": False, "invalid_slots": {},
    }
    if not isinstance(day, str) or not day or plain_day(day) != day:
        return result
    folder = Path(root) / "data" / "snapshots" / day
    ratios: list[tuple[float, str]] = []
    for slot in expected_snapshot_slots():
        if not (folder / f"{slot}.csv").exists() and not (folder / f"{slot}.json").exists():
            continue
        result["sheets"] += 1
        try:
            meta, rows = read_verified_snapshot_archive(Path(root), day, slot)
        except (OSError, UnicodeError, ValueError, TypeError, KeyError, csv.Error) as exc:
            result["invalid_slots"][slot] = f"{type(exc).__name__}:{exc}"[:200]
            ratios.append((0.0, slot))
            continue
        expected = set(meta["expected_symbols"])
        usable = {
            row["symbol"] for row in rows
            if row["symbol"] in expected and row["quote_date"] == day
            and (finite(row["price"]) or 0) > 0
        }
        ratios.append((len(usable) / len(expected) if expected else 0.0, slot))
    if ratios:
        worst = min(ratios)
        result["min_ratio"] = round(worst[0], 4)
        result["worst_slot"] = worst[1]
        result["good"] = sum(1 for ratio, _ in ratios if ratio >= SNAPSHOT_SHEET_VALID_RATIO)
    result["ok"] = result["good"] >= SNAPSHOT_MIN_GOOD_SHEETS
    return result


# --------------------------------------------------------------------------
# Backup copy (the push itself, not research completeness)
# --------------------------------------------------------------------------

def _stamp_day(stamp: str) -> str:
    digits = "".join(ch for ch in str(stamp or "") if ch.isdigit())
    return plain_day(f"{digits[:4]}-{digits[4:6]}-{digits[6:8]}") if len(digits) >= 8 else ""


def backup_copy_status(root: Path, day: str) -> dict[str, Any]:
    """Did the nightly push for ``day`` finish with every file hash matching?"""

    log = Path(root) / "logs" / "ops" / "backup_push.log"
    result: dict[str, Any] = {"day": day, "ok": False, "last_success": "", "file_count": None, "seen": False}
    try:
        lines = log.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return result
    for raw in lines:
        try:
            row = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if not isinstance(row, dict) or row.get("exit_code") is None:
            continue
        when = _stamp_day(str(row.get("stamp") or ""))
        # A later optional project-recovery failure does not undo the verified
        # ledger copy. Old receipts have no separate paper verification field.
        success = row.get("paper_backup_verified") is True or (
            "paper_backup_verified" not in row and row.get("exit_code") == 0
            and row.get("all_hashes_match") is True
        )
        if success and when:
            result["last_success"] = max(result["last_success"], when)
        if when == day:
            result["seen"] = True
            if success:
                result["ok"] = True
                result["file_count"] = row.get("file_count")
    return result


def backup_health_status(root: Path, day: str, *, now: datetime | None = None) -> dict[str, Any]:
    """Judge the ledger copy and the owner's two-day offsite receipt contract."""
    paper = backup_copy_status(root, day)
    receipt = load_json(Path(root) / "results/offsite_backup/offsite_backup_latest.json") or {}
    zone = ZoneInfo("Asia/Shanghai")
    current = now or datetime.now(zone)
    current = current.replace(tzinfo=zone) if current.tzinfo is None else current.astimezone(zone)
    reference = min(current, datetime.combine(date.fromisoformat(day), time.max, tzinfo=zone))
    finished: datetime | None = None
    try:
        finished = datetime.fromisoformat(str(receipt.get("finished_at") or "").replace("Z", "+00:00"))
        if finished.tzinfo is None:
            finished = None
    except ValueError:
        pass
    fresh = finished is not None and timedelta(0) <= reference - finished <= timedelta(days=2)
    offsite_ok = receipt.get("status") == "ok" and fresh
    reasons = []
    if not paper["ok"]:
        reasons.append(f"账本备份在{day}尚无成功核对（上次成功 {paper['last_success'] or '没有记录'}）")
    if not offsite_ok:
        reasons.append("异地备份回执缺失、失败、时间异常或已超过2天")
    cloud_confirmed = receipt.get("cloud_upload_confirmed") is True
    return {"ok": paper["ok"] and offsite_ok, "paper": paper, "offsite_ok": offsite_ok,
            "offsite_finished_at": receipt.get("finished_at"), "cloud_upload_confirmed": cloud_confirmed,
            "reason": "；".join(reasons),
            "note": "账本备份已核对；异地回执在2天内" if paper["ok"] and offsite_ok else "",
            "cloud_note": "云端上传已确认" if cloud_confirmed else "只核对同步目录副本，云端上传尚未确认"}


def execution_basis_digest(root: Path, day: str) -> dict[str, Any]:
    """Count recorded fills, never infer minute use merely from file coverage."""
    data = load_json(Path(root) / "results/auto_paper_portfolio_latest.json") or {}
    if data.get("valuation_date") != day or not isinstance(data.get("daily_actions"), list):
        return {"verified": False, "message": f"{day}成交依据计数未核实"}
    counts = {"minute": 0, "daily_approx": 0, "daily_close": 0, "unknown": 0}
    rows = []
    for row in data["daily_actions"]:
        if not isinstance(row, dict) or row.get("action") not in OPEN_ACTIONS | CLOSE_ACTIONS:
            continue
        quantity = finite(row.get("quantity"))
        if quantity is None or quantity <= 0 or row.get("decision_date") != day:
            continue
        model = row.get("fill_model")
        if model in {"intraday_1m_close", "intraday_1m", "minute_1m"}:
            category = "minute"
        elif model == "daily_approx":
            category = "daily_approx"
        elif row.get("order_source") == "auto_strategy_sandbox" and row.get("execution_source") != "intraday_swing" and not model:
            category = "daily_close"
        else:
            category = "unknown"
        counts[category] += 1
        rows.append({"symbol": row.get("symbol"), "action": row.get("action"), "quantity": quantity,
                     "price": row.get("price"), "basis": category})
    message = (f"{day}已记录成交：分钟{counts['minute']}笔、盘中日线近似{counts['daily_approx']}笔、"
               f"日终规则{counts['daily_close']}笔、依据未核实{counts['unknown']}笔；按动作记录分类，不由分钟覆盖率推断")
    return {"verified": True, **counts, "rows": rows, "message": message}


# --------------------------------------------------------------------------
# Strategy book, benchmark and AI books
# --------------------------------------------------------------------------

def _first_explicit_valuation(root: Path) -> tuple[str, float | None]:
    """Earliest history record that names its own valuation date (strict reading)."""

    history = load_json(Path(root) / "results" / "auto_paper_portfolio_history.json") or {}
    items = history.get("items") if isinstance(history.get("items"), list) else []
    best: tuple[str, float | None] = ("", None)
    for item in items:
        if not isinstance(item, Mapping):
            continue
        day = plain_day(item.get("valuation_date"))
        equity = finite(item.get("total_equity"))
        if day and equity and (not best[0] or day < best[0]):
            best = (day, equity)
    return best


def _short_exit_reason(reason: str) -> str:
    if "8%" in reason:
        return "跌破 -8% 止损线"
    if "支撑" in reason:
        return "跌破关键支撑"
    if "止盈" in reason:
        return "到了止盈线"
    return clean_line(reason[:20]) or "规则触发"


def strategy_digest(root: Path, as_of: str) -> dict[str, Any]:
    latest = load_json(Path(root) / "results" / "auto_paper_portfolio_latest.json")
    if latest is None:
        return {"available": False, "reason": "策略账展示文件读不到"}
    summary = latest.get("summary") if isinstance(latest.get("summary"), Mapping) else {}
    valuation_day = plain_day(latest.get("valuation_date"))
    digest: dict[str, Any] = {
        "available": True,
        "valuation_date": valuation_day,
        "current": bool(as_of) and valuation_day == as_of,
        "total_equity": finite(summary.get("total_equity")),
        "total_return_pct": finite(summary.get("total_return_pct")),
        "daily_pnl": finite(summary.get("daily_pnl")),
        "daily_return_pct": finite(summary.get("daily_return_pct")),
        "accounting_status": str(summary.get("accounting_status") or ""),
        "opened": [], "closed_today": [], "holdings": [], "closed_count": 0, "closed_wins": 0,
    }
    identity = summary.get("pnl_identity") if isinstance(summary.get("pnl_identity"), Mapping) else {}
    digest["pnl_difference"] = finite(identity.get("difference"))
    for action in latest.get("daily_actions") or []:
        if not isinstance(action, Mapping) or plain_day(action.get("decision_date")) != valuation_day:
            continue
        kind = str(action.get("action") or "")
        row = {
            "symbol": str(action.get("symbol") or ""),
            "name": str(action.get("name") or action.get("symbol") or ""),
            "price": finite(action.get("price")),
            "quantity": action.get("quantity"),
            "reason": str(action.get("reason") or ""),
            "pnl": finite(action.get("pnl")),
        }
        if kind in OPEN_ACTIONS:
            digest["opened"].append(row)
        elif kind in CLOSE_ACTIONS:
            row["reason"] = _short_exit_reason(row["reason"])
            digest["closed_today"].append(row)
    for position in latest.get("positions") or []:
        if not isinstance(position, Mapping):
            continue
        pct = finite(position.get("unrealized_return_pct"))
        if pct is None:
            pct = finite(position.get("return_pct"))
        digest["holdings"].append({
            "symbol": str(position.get("symbol") or ""),
            "name": str(position.get("name") or position.get("symbol") or ""),
            "return_pct": pct,
            "holding_days": position.get("holding_days"),
        })
    closed = [item for item in latest.get("closed_positions") or [] if isinstance(item, Mapping)]
    digest["closed_count"] = len(closed)
    digest["closed_wins"] = sum(1 for item in closed if (finite(item.get("realized_pnl")) or 0.0) > 0)
    anchor_day, anchor_equity = _first_explicit_valuation(root)
    digest["anchor_date"] = anchor_day
    if anchor_equity and digest["total_equity"] is not None and anchor_day and anchor_day < valuation_day:
        digest["since_anchor_pct"] = round((digest["total_equity"] / anchor_equity - 1.0) * 100.0, 4)
    else:
        digest["since_anchor_pct"] = None
    return digest


def _benchmark_rows(root: Path) -> dict[str, tuple[float, float | None]]:
    path = Path(root) / "data" / "benchmark" / "hs300.csv"
    rows: dict[str, tuple[float, float | None]] = {}
    try:
        with path.open(encoding="utf-8-sig", newline="") as handle:
            for row in csv.DictReader(handle):
                day = plain_day(row.get("date"))
                close = finite(row.get("close"))
                if day and close:
                    rows[day] = (close, finite(row.get("pct_chg")))
    except (OSError, UnicodeError, csv.Error):
        return {}
    return rows


def benchmark_digest(root: Path, as_of: str, anchor: str = "") -> dict[str, Any]:
    rows = _benchmark_rows(root)
    result: dict[str, Any] = {"available": as_of in rows, "daily_pct": None, "since_anchor_pct": None}
    if as_of not in rows:
        return result
    close, pct = rows[as_of]
    if pct is None:
        earlier = sorted(day for day in rows if day < as_of)
        if earlier:
            pct = (close / rows[earlier[-1]][0] - 1.0) * 100.0
    result["daily_pct"] = round(pct, 4) if pct is not None else None
    if anchor and anchor in rows and anchor < as_of:
        result["since_anchor_pct"] = round((close / rows[anchor][0] - 1.0) * 100.0, 4)
    return result


def ai_digest(root: Path, as_of: str) -> list[dict[str, Any]]:
    books: list[dict[str, Any]] = []
    for label, folder in AI_BOOKS:
        ledger = load_json(Path(root) / folder / "ledger.json")
        if ledger is None:
            books.append({"label": label, "available": False})
            continue
        journal = load_json(Path(root) / folder / "journal_latest.json") or {}
        llm = journal.get("llm") if isinstance(journal.get("llm"), Mapping) else {}
        equity = finite(ledger.get("equity"))
        if equity is None:
            equity = finite(ledger.get("total_equity"))
        initial = finite(ledger.get("initial_cash")) or 100000.0
        trades = [item for item in ledger.get("trades") or [] if isinstance(item, Mapping)]
        books.append({
            "label": label,
            "available": equity is not None,
            "as_of": plain_day(ledger.get("as_of")),
            "equity": equity,
            "return_pct": round((equity / initial - 1.0) * 100.0, 4) if equity is not None else None,
            "last_trade": max((plain_day(item.get("date")) for item in trades), default=""),
            "model_used": llm.get("used") is True and plain_day(journal.get("as_of")) == as_of,
        })
    return books


# --------------------------------------------------------------------------
# Health line as stored for the owner
# --------------------------------------------------------------------------

def health_digest(root: Path, day: str) -> dict[str, Any]:
    """Prefer the owner line the health writer stored; fall back to the old line."""

    stored = load_json(Path(root) / HEALTH_HISTORY / f"{day}.json")
    if stored and stored.get("owner_line"):
        return {"owner_line": str(stored["owner_line"]), "plain": str(stored.get("owner_plain") or ""),
                "red_cells": [str(item) for item in stored.get("owner_red_sentences") or []], "source": "owner"}
    try:
        text = (Path(root) / HEALTH_HISTORY / f"{day}.md").read_text(encoding="utf-8")
    except OSError:
        return {"owner_line": "", "plain": "", "red_cells": [], "technical_line": "", "source": "missing"}
    technical = next((line[len("- 健康行："):] for line in text.splitlines() if line.startswith("- 健康行：")), "")
    return {"owner_line": "", "plain": "", "red_cells": [], "technical_line": technical, "source": "legacy"}


def daily_counts_from_line(line: str) -> tuple[int, int] | None:
    match = re.search(r"日线 [✓✗](\d+)/(\d+)", line or "")
    return (int(match.group(1)), int(match.group(2))) if match else None


# --------------------------------------------------------------------------
# Morning brief text
# --------------------------------------------------------------------------

def build_owner_digest(root: Path, as_of: str, paper_account: Mapping[str, Any] | None = None) -> dict[str, Any]:
    strategy = strategy_digest(root, as_of)
    account = paper_account if isinstance(paper_account, Mapping) else {}
    if not strategy.get("available") and finite(account.get("total_equity")) is not None:
        # The brief's own scoped account summary is the fallback headline when the
        # strategy display file is unreadable; per-stock detail stays absent.
        strategy = {
            "available": True, "valuation_date": plain_day(account.get("valuation_date")),
            "current": plain_day(account.get("valuation_date")) == as_of,
            "total_equity": finite(account.get("total_equity")), "total_return_pct": None,
            "daily_pnl": finite(account.get("daily_change")), "daily_return_pct": finite(account.get("daily_return_pct")),
            "opened": [], "closed_today": [], "holdings": [], "closed_count": 0, "closed_wins": 0,
            "anchor_date": "", "since_anchor_pct": None, "source": "brief_paper_account",
        }
    anchor = str(strategy.get("anchor_date") or "")
    return {
        "as_of": as_of,
        "strategy": strategy,
        "benchmark": benchmark_digest(root, as_of, anchor),
        "ai": ai_digest(root, as_of),
        "health": health_digest(root, as_of),
        "snapshot": snapshot_sheet_quality(root, as_of),
        "backup": backup_copy_status(root, as_of),
    }


def _yuan(value: float | None) -> str:
    return "读不到" if value is None else f"{value:+,.0f} 元"


def _price(value: float | None) -> str:
    return "?" if value is None else f"{value:.2f}"


def _question(digest: Mapping[str, Any], ordinal: int) -> str:
    strategy = digest.get("strategy") or {}
    bench = digest.get("benchmark") or {}
    holdings = [item for item in strategy.get("holdings") or [] if item.get("return_pct") is not None]
    best = max(holdings, key=lambda item: item["return_pct"], default=None)
    pool: list[str] = []
    if strategy.get("closed_count", 0) >= 10 and strategy.get("closed_wins") == 0 and best and best["return_pct"] >= 20:
        pool.append(f"{strategy['closed_count']} 笔平仓全是亏的，可{best['name']}赚了 {best['return_pct']:.0f}% 一直拿着。"
                    "规则是「赚的拿着、亏的砍掉」，为什么总账还跑输大盘？")
    daily, bench_daily = strategy.get("daily_return_pct"), bench.get("daily_pct")
    if daily is not None and bench_daily is not None and daily < 0 <= bench_daily:
        pool.append(f"昨天沪深300 {bench_daily:+.2f}%，策略账却 {daily:+.2f}%。拖后腿的是哪几只？它们有什么共同点？")
    reasons = {item["reason"] for item in strategy.get("opened") or [] if item.get("reason")}
    if len(strategy.get("opened") or []) >= 2 and len(reasons) == 1:
        pool.append(f"昨天新开的 {len(strategy['opened'])} 只理由一模一样。只凭这一条规则，能挑出好股票吗？")
    pool.append("如果每只票都固定拿 10 天就平掉，结果会比现在好还是差？（对比表做好后会回答）")
    return pool[ordinal % len(pool)]


def render_owner_brief(digest: Mapping[str, Any], *, today: date) -> tuple[str, str]:
    """Return (title, body) of the plain-language push. Raises on malformed input."""

    as_of = str(digest.get("as_of") or "")
    strategy = digest.get("strategy") or {}
    bench = digest.get("benchmark") or {}
    day_label = short_day(as_of)
    title_date = f"{today.month}/{today.day}"
    lines: list[str] = [f"**昨天（{day_label}）两本账**"]

    # 1. Two books
    if not strategy.get("available"):
        lines.append(f"- 策略账：{strategy.get('reason') or '读不到'}")
        title = f"早报 {title_date}｜策略账读不到"
    elif not strategy.get("current"):
        lines.append(f"- 策略账：没有更新到 {day_label}，估值还停在 {short_day(str(strategy.get('valuation_date') or ''))}")
        title = f"早报 {title_date}｜策略账没更新"
    else:
        lines.append(f"- 策略账 {_yuan(strategy.get('daily_pnl'))}（{signed_pct(strategy.get('daily_return_pct'))}），"
                     f"沪深300 {signed_pct(bench.get('daily_pct'))}")
        lines.append(f"- 策略账累计 {signed_pct(strategy.get('total_return_pct'), 1)}（本金 10 万）")
        if strategy.get("since_anchor_pct") is not None and bench.get("since_anchor_pct") is not None:
            lines.append(f"- {short_day(str(strategy['anchor_date']))} 以来：策略账 {signed_pct(strategy['since_anchor_pct'], 1)}，"
                         f"同期沪深300 {signed_pct(bench['since_anchor_pct'], 1)}")
        title = (f"早报 {title_date}｜策略 {signed_pct(strategy.get('daily_return_pct'))} "
                 f"沪深300 {signed_pct(bench.get('daily_pct'))}")
    ai_books = [book for book in digest.get("ai") or [] if book.get("available")]
    if ai_books:
        any_model = any(book.get("model_used") for book in ai_books)
        state = "模型参与了" if any_model else f"暂缓中（{short_day(AI_ON_HOLD_SINCE)} 决定，模型没参与，规则代管）"
        parts = "，".join(f"{book['label']}累计 {signed_pct(book.get('return_pct'), 1)}" for book in ai_books)
        lines.append(f"- AI 账：{state}。{parts}")
    else:
        lines.append("- AI 账：读不到")

    # 2. What the strategy book did yesterday
    if strategy.get("available") and strategy.get("current"):
        lines += ["", "**策略账昨天做了什么**"]
        opened = strategy.get("opened") or []
        closed = strategy.get("closed_today") or []
        if opened:
            names = "、".join(f"{item['name']} {_price(item.get('price'))}" for item in opened[:5])
            more = f" 等 {len(opened)} 只" if len(opened) > 5 else ""
            lines.append(f"- 新开 {len(opened)} 只：{names}{more}")
            reasons = sorted({item["reason"] for item in opened if item.get("reason")})
            if len(reasons) == 1:
                reason = clean_line(reasons[0].replace("长线规则满足：", "").rstrip("。"))
                if reason:
                    lines.append(f"- 理由都是：{reason}")
        if closed:
            names = "、".join(f"{item['name']}（{item['reason']}，{_yuan(item.get('pnl'))}）" for item in closed[:4])
            lines.append(f"- 平掉 {len(closed)} 只：{names}")
        if not opened and not closed:
            lines.append("- 没有新开，也没有平掉")
        holdings = [item for item in strategy.get("holdings") or [] if item.get("return_pct") is not None]
        if holdings:
            best = max(holdings, key=lambda item: item["return_pct"])
            worst = min(holdings, key=lambda item: item["return_pct"])
            lines.append(f"- 拿得最好：{best['name']} {best['return_pct']:+.1f}%（{best.get('holding_days') or '?'} 天）；"
                         f"最差：{worst['name']} {worst['return_pct']:+.1f}%（{worst.get('holding_days') or '?'} 天）")

    # 3. Worth a look (at most three, never padded)
    watch: list[str] = []
    near = sorted((item for item in strategy.get("holdings") or []
                   if item.get("return_pct") is not None and STOP_LINE_PCT < item["return_pct"] <= NEAR_STOP_PCT),
                  key=lambda item: item["return_pct"])
    if strategy.get("current") and near:
        names = "、".join(f"{item['name']}（{item['return_pct']:+.1f}%）" for item in near[:3])
        watch.append(f"{names}离 {STOP_LINE_PCT:.0f}% 止损线不远，今天收盘后可能被平掉")
    health = digest.get("health") or {}
    if health.get("red_cells"):
        watch.append("健康行有红格：" + "；".join(health["red_cells"][:2]))
    if strategy.get("available") and not strategy.get("current"):
        watch.append("昨晚策略账没有更新，收盘或复盘可能没跑完")
    if watch:
        lines += ["", "**今天值得看**"]
        lines.extend(f"{index}. {item}" for index, item in enumerate(watch[:3], 1))

    # 4. One question
    if strategy.get("available"):
        lines += ["", "**想一想**", _question(digest, today.toordinal())]

    # 5. Data status and boundary note
    lines.append("")
    if health.get("owner_line"):
        lines.append(f"健康行：{health['owner_line']}")
    else:
        snapshot = digest.get("snapshot") or {}
        backup = digest.get("backup") or {}
        counts = daily_counts_from_line(str(health.get("technical_line") or ""))
        pieces = []
        if counts:
            pieces.append(f"日线 {counts[0]}/{counts[1]}")
        if snapshot.get("sheets"):
            pieces.append(f"快照 {snapshot['good']}/{snapshot['expected']} 张合格")
        pieces.append("备份 ✓" if backup.get("ok") else "备份 ✗")
        lines.append("数据：" + " · ".join(pieces))
    lines.append(f"{PAPER_NOTE}细节在电脑上的 8000 首页。")

    body = "\n".join(line for line in (clean_line(item) if item else item for item in lines) if line is not None)
    return title[:60], body


def stale_owner_brief(as_of: str, expected: str, *, today: date) -> tuple[str, str]:
    """Short notice used when the newest data is not the previous session's."""

    title = f"早报 {today.month}/{today.day}｜没有新资料"
    body = (f"今天没有新资料：资料停在 {short_day(as_of) if as_of else '读不到'}，"
            f"应该是 {short_day(expected) if expected else '读不到'}。多半是昨晚收盘更新没跑完，"
            f"今晚会重试。\n\n{PAPER_NOTE}")
    return title, body
