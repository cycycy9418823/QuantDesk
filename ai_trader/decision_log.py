"""C-line decision log: structured records, daily-bar statement gate, owner page.

Reads only ``data/daily/{symbol}.csv``. Does not read research-chain products,
the 914 filter list, or candidate JSON. Does not call a model.
"""

from __future__ import annotations

import csv
import html
import json
import re
import subprocess
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from hashlib import sha1
from pathlib import Path
from typing import Any, Iterable

from ai_trader.paths import (
    CYCLE_LATEST_PATH,
    JOURNAL_PATH,
    PROJECT_ROOT,
    RESULTS_DIR,
    STATE_DIR,
    CyclePaths,
)
from ai_trader.prompt_pack import load_prompt_pack
from quantdesk.trading_calendar import is_trade_day
from quantdesk.utils import atomic_write_json

SCHEMA = "ai_trader_decision_v1"
DISCLAIMER = "纸面 · 未验证"
VERIFICATION_DERIVED_NOTE = "验证条件为程序推导，非 AI 声明"
RESULT_WRONG = "错"
RESULT_RIGHT = "对"
RESULT_PENDING = "未到期"
RESULT_MISMATCH = "陈述与数据不符"
RESULT_EXPIRED_NO_EVIDENCE = "已到期 · 证据不足"
RESULT_NA = "不适用"
PAGE_ORDER = {
    RESULT_WRONG: 0,
    RESULT_MISMATCH: 1,
    RESULT_EXPIRED_NO_EVIDENCE: 2,
    RESULT_PENDING: 3,
    RESULT_RIGHT: 4,
    RESULT_NA: 5,
}
STYLE_HOLD_DAYS = {
    "打板": 2,
    "短线": 5,
    "波段": 15,
    "中线": 60,
    "长期": 250,
}
_SYMBOL_PCT = re.compile(r"(?<!\d)(\d{6})\s*(?:\()?\s*([+-]?\d+(?:\.\d+)?)\s*%")
_CLOSE_PRICE = re.compile(r"(?:收盘价?|现价)\s*[为是:]?\s*(\d+(?:\.\d+)?)(?:\s*元)?")
_OPEN_PRICE = re.compile(r"开盘价?\s*[为是:]?\s*(\d+(?:\.\d+)?)(?:\s*元)?")
_UNTAGGED_PCT = re.compile(
    r"(?:近期|今天|今日|当日)?(涨(?:幅)?|跌(?:幅)?|回调|[+＋\-－])\s*约?\s*([+-]?\d+(?:\.\d+)?)\s*%"
)
_PREDICTION_CONTEXT = re.compile(r"(预计|预判|预期|目标价?|明天|次日|往后|未来|将会?|有望|看涨|看跌)")
_INTENT_FACT_FIELDS = ("llm_reason", "reason", "thesis", "recommendation_note")
TZ_SHANGHAI = timezone(timedelta(hours=8))
_DAILY_ROWS_CACHE: dict[tuple[str, str], list[dict[str, Any]]] = {}


@dataclass
class StatementGateResult:
    accepted_intents: list[dict[str, Any]]
    rejections: list[dict[str, Any]] = field(default_factory=list)
    market_note_ok: bool = True


def _now_iso() -> str:
    return datetime.now(TZ_SHANGHAI).replace(microsecond=0).isoformat()


def _as_day(value: Any) -> str:
    return str(value or "")[:10]


def _f(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number:
        return None
    return number


def _price_close_enough(claimed: float, actual: float) -> bool:
    return abs(claimed - actual) <= max(0.02, abs(actual) * 0.005)


def _pct_close_enough(claimed: float, actual: float | None) -> bool:
    """Compare percent claims as percent. Do not guess that 40 means 0.40."""

    if actual is None:
        return False
    return abs(claimed - actual) <= 1.0


def _is_prediction_context(text: str, start: int) -> bool:
    window = str(text or "")[max(0, start - 16) : start]
    return bool(_PREDICTION_CONTEXT.search(window))


def _intent_fact_texts(intent: dict[str, Any]) -> list[str]:
    texts: list[str] = []
    for key in _INTENT_FACT_FIELDS:
        value = str(intent.get(key) or "").strip()
        if value and value not in texts:
            texts.append(value)
    return texts


def _row_date(row: dict[str, str]) -> str:
    raw = str(row.get("date") or row.get("日期") or "").strip().strip('"')
    return raw[:10].replace("/", "-")


def iter_daily_rows(symbol: str, daily_dir: Path) -> list[dict[str, Any]]:
    key = (str(symbol), str(Path(daily_dir)))
    cached = _DAILY_ROWS_CACHE.get(key)
    if cached is not None:
        return cached
    path = Path(daily_dir) / f"{symbol}.csv"
    if not path.is_file():
        _DAILY_ROWS_CACHE[key] = []
        return []
    rows: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        for raw in reader:
            day = _row_date(raw)
            if len(day) != 10:
                continue
            close = _f(raw.get("close") or raw.get("收盘"))
            pct = _f(raw.get("pct_chg") or raw.get("涨跌幅"))
            rows.append(
                {
                    "date": day,
                    "open": _f(raw.get("open") or raw.get("开盘")),
                    "high": _f(raw.get("high") or raw.get("最高")),
                    "low": _f(raw.get("low") or raw.get("最低")),
                    "close": close,
                    "pct_chg": pct,
                }
            )
    rows.sort(key=lambda item: item["date"])
    prev_close: float | None = None
    for row in rows:
        if row.get("pct_chg") is None and row.get("close") is not None and prev_close:
            row["pct_chg"] = (float(row["close"]) / prev_close - 1.0) * 100.0
        if row.get("close") is not None:
            prev_close = float(row["close"])
    _DAILY_ROWS_CACHE[key] = rows
    return rows


def load_daily_bar(symbol: str, as_of: str, daily_dir: Path) -> dict[str, Any] | None:
    day = _as_day(as_of)
    for row in iter_daily_rows(symbol, daily_dir):
        if row["date"] == day:
            return row
    return None


def _closes_from(symbol: str, start: str, daily_dir: Path, count: int) -> list[tuple[str, float]]:
    out: list[tuple[str, float]] = []
    for row in iter_daily_rows(symbol, daily_dir):
        if row["date"] < start:
            continue
        close = row.get("close")
        if close is None:
            continue
        out.append((str(row["date"]), float(close)))
        if len(out) >= count:
            break
    return out


def _change_n(rows: list[dict[str, Any]], as_of: str, days: int = 5) -> float | None:
    upto = [row for row in rows if row["date"] <= as_of and row.get("close")]
    if len(upto) <= days:
        return None
    prior = float(upto[-1 - days]["close"])
    last = float(upto[-1]["close"])
    if prior <= 0:
        return None
    return (last / prior - 1.0) * 100.0


def trade_days_from(start: str, n: int) -> list[str]:
    if n <= 0:
        return []
    days: list[str] = []
    cursor = date.fromisoformat(_as_day(start))
    guard = 0
    while len(days) < n and guard < 800:
        if is_trade_day(cursor):
            days.append(cursor.isoformat())
        cursor += timedelta(days=1)
        guard += 1
    return days


def _hold_days(intent: dict[str, Any]) -> int:
    try:
        planned = int(intent.get("planned_hold_days") or 0)
    except (TypeError, ValueError):
        planned = 0
    if planned > 0:
        return max(1, min(planned, 750))
    style = str(intent.get("style") or "").strip()
    return STYLE_HOLD_DAYS.get(style, 5)


def _mismatch(kind: str, detail: str, **extra: Any) -> dict[str, Any]:
    payload = {"kind": kind, "detail": detail, "label": RESULT_MISMATCH}
    payload.update(extra)
    return payload


def _check_price_claims(text: str, bar: dict[str, Any]) -> list[dict[str, Any]]:
    hits: list[dict[str, Any]] = []
    close = bar.get("close")
    open_px = bar.get("open")
    for match in _CLOSE_PRICE.finditer(text or ""):
        if _is_prediction_context(text, match.start()):
            continue
        claimed = float(match.group(1))
        if close is None or not _price_close_enough(claimed, float(close)):
            hits.append(
                _mismatch(
                    "claimed_close",
                    f"陈述收盘{claimed}，日线收盘{close}",
                    claimed=claimed,
                    actual=close,
                )
            )
    for match in _OPEN_PRICE.finditer(text or ""):
        if _is_prediction_context(text, match.start()):
            continue
        claimed = float(match.group(1))
        if open_px is None or not _price_close_enough(claimed, float(open_px)):
            hits.append(
                _mismatch(
                    "claimed_open",
                    f"陈述开盘{claimed}，日线开盘{open_px}",
                    claimed=claimed,
                    actual=open_px,
                )
            )
    return hits


def _check_symbol_pct_claims(text: str, decision_date: str, daily_dir: Path) -> list[dict[str, Any]]:
    hits: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for match in _SYMBOL_PCT.finditer(text or ""):
        if _is_prediction_context(text, match.start()):
            continue
        symbol, raw = match.group(1), match.group(2)
        key = (symbol, raw)
        if key in seen:
            continue
        seen.add(key)
        claimed = float(raw)
        bar = load_daily_bar(symbol, decision_date, daily_dir)
        if bar is None:
            hits.append(_mismatch("claimed_pct_no_bar", f"{symbol} 陈述{claimed}%，日线无{decision_date}", symbol=symbol))
            continue
        actual = bar.get("pct_chg")
        if actual is None and bar.get("close") is not None:
            rows = iter_daily_rows(symbol, daily_dir)
            change = _change_n(rows, decision_date, 1)
            actual = change
        n_day = _change_n(iter_daily_rows(symbol, daily_dir), decision_date, 5)
        if _pct_close_enough(claimed, actual) or _pct_close_enough(claimed, n_day):
            continue
        hits.append(
            _mismatch(
                "claimed_pct",
                f"{symbol} 陈述{claimed}%，日线涨跌{actual}",
                symbol=symbol,
                claimed=claimed,
                actual=actual,
            )
        )
    return hits


def _signed_pct_claim(verb: str, raw: str) -> float:
    magnitude = abs(float(raw))
    token = str(verb or "")
    if token.startswith("跌") or token.startswith("回调") or token in {"-", "－"}:
        return -magnitude
    if raw.strip().startswith("-") or raw.strip().startswith("－"):
        return -magnitude
    return magnitude


def _check_untagged_pct(text: str, bar: dict[str, Any] | None, rows: list[dict[str, Any]], as_of: str) -> list[dict[str, Any]]:
    if not text or bar is None:
        return []
    hits: list[dict[str, Any]] = []
    for match in _UNTAGGED_PCT.finditer(text):
        if _is_prediction_context(text, match.start()):
            continue
        claimed = _signed_pct_claim(match.group(1), match.group(2))
        if _pct_close_enough(claimed, bar.get("pct_chg")):
            continue
        day1 = _change_n(rows, as_of, 1)
        n_day = _change_n(rows, as_of, 5)
        if _pct_close_enough(claimed, day1) or _pct_close_enough(claimed, n_day):
            continue
        hits.append(
            _mismatch(
                "claimed_pct_untagged",
                f"陈述{claimed}%，该票日线涨跌{bar.get('pct_chg')}",
                claimed=claimed,
                actual=bar.get("pct_chg"),
            )
        )
    return hits


def verify_intent_against_daily(
    intent: dict[str, Any],
    *,
    decision_date: str,
    daily_dir: Path,
    market_note: str = "",
) -> list[dict[str, Any]]:
    symbol = str(intent.get("symbol") or "").strip()
    if not symbol:
        return [_mismatch("missing_symbol", "决策没有代码")]
    bar = load_daily_bar(symbol, decision_date, daily_dir)
    if bar is None:
        return [_mismatch("daily_bar_missing", f"{symbol} 在 {decision_date} 没有日线", symbol=symbol)]
    hits: list[dict[str, Any]] = []
    ref = _f(intent.get("ref_close"))
    close = bar.get("close")
    if ref is not None and close is not None and not _price_close_enough(ref, float(close)):
        hits.append(
            _mismatch(
                "ref_close",
                f"{symbol} 记录收盘{ref}，日线收盘{close}",
                symbol=symbol,
                claimed=ref,
                actual=close,
            )
        )
    session = intent.get("session") if isinstance(intent.get("session"), dict) else {}
    session_close = _f(session.get("daily_close"))
    if session_close is not None and close is not None and not _price_close_enough(session_close, float(close)):
        hits.append(
            _mismatch(
                "session_daily_close",
                f"{symbol} 会话记录收盘{session_close}，日线收盘{close}",
                symbol=symbol,
            )
        )
    rows = iter_daily_rows(symbol, daily_dir)
    for fact in _intent_fact_texts(intent):
        hits.extend(_check_price_claims(fact, bar))
        hits.extend(_check_untagged_pct(fact, bar, rows, decision_date))
        hits.extend(_check_symbol_pct_claims(fact, decision_date, daily_dir))
    hits.extend(_check_symbol_pct_claims(market_note, decision_date, daily_dir))
    return hits


def verify_market_note(market_note: str, *, decision_date: str, daily_dir: Path) -> list[dict[str, Any]]:
    return _check_symbol_pct_claims(market_note, decision_date, daily_dir)


def gate_plan_statements(plan: dict[str, Any], *, daily_dir: Path) -> StatementGateResult:
    decision_date = _as_day(plan.get("decision_date") or plan.get("as_of"))
    llm = plan.get("llm") if isinstance(plan.get("llm"), dict) else {}
    market_note = str(llm.get("market_note") or "")
    note_hits = verify_market_note(market_note, decision_date=decision_date, daily_dir=daily_dir)
    accepted: list[dict[str, Any]] = []
    rejections: list[dict[str, Any]] = []
    llm_used = bool(llm.get("used"))
    for intent in plan.get("intents") or []:
        if not isinstance(intent, dict):
            continue
        hits = verify_intent_against_daily(
            intent,
            decision_date=decision_date,
            daily_dir=daily_dir,
            market_note="",
        )
        if llm_used:
            hits.extend(note_hits)
        if hits:
            row = dict(intent)
            row["statement_rejections"] = hits
            rejections.append(row)
        else:
            accepted.append(intent)
    if not (plan.get("intents") or []) and note_hits:
        rejections.append(
            {
                "symbol": "",
                "side": "hold",
                "statement_rejections": note_hits,
            }
        )
    return StatementGateResult(
        accepted_intents=accepted,
        rejections=rejections,
        market_note_ok=not note_hits,
    )


def _verification_for_intent(intent: dict[str, Any], *, trade_date: str) -> dict[str, Any]:
    n_days = _hold_days(intent)
    level = _f(intent.get("ref_close"))
    side = str(intent.get("side") or "buy")
    window = trade_days_from(trade_date, n_days)
    due = window[-1] if window else ""
    if side == "sell":
        text = f"{n_days}个交易日内收盘不跌破{level}就是错了"
        kind = "break_below"
    else:
        text = f"{n_days}个交易日内收盘不站上{level}就是错了"
        kind = "stand_above"
    return {
        "kind": kind,
        "source": "derived_from_declared_horizon",
        "n_sessions": n_days,
        "level": level,
        "due_date": due,
        "text": text,
    }


def build_decision_record(
    intent: dict[str, Any],
    *,
    plan: dict[str, Any],
    daily_dir: Path,
    mismatches: list[dict[str, Any]] | None = None,
    structured_at: str | None = None,
    backfill: bool = False,
) -> dict[str, Any]:
    pack = load_prompt_pack()
    decision_date = _as_day(plan.get("decision_date") or plan.get("as_of"))
    trade_date = _as_day(plan.get("trade_date"))
    symbol = str(intent.get("symbol") or "").strip()
    side = str(intent.get("side") or "hold")
    bar = load_daily_bar(symbol, decision_date, daily_dir) if symbol else None
    llm = plan.get("llm") if isinstance(plan.get("llm"), dict) else {}
    stamp = pack.version_stamp()
    recorded_prompt = str(plan.get("prompt_version") or llm.get("prompt_version") or "").strip()
    recorded_menu = str(plan.get("menu_version") or llm.get("menu_version") or "").strip()
    if backfill:
        stamp["prompt_version"] = recorded_prompt
        stamp["menu_version"] = recorded_menu
    else:
        stamp["prompt_version"] = recorded_prompt or stamp["prompt_version"]
        stamp["menu_version"] = recorded_menu or stamp["menu_version"]
    mismatches = list(mismatches or [])
    if mismatches:
        result = RESULT_MISMATCH
        verification = {
            "kind": "rejected",
            "source": "statement_gate",
            "n_sessions": None,
            "level": None,
            "due_date": "",
            "text": RESULT_MISMATCH,
        }
    elif not symbol:
        result = RESULT_NA
        verification = {
            "kind": "none",
            "source": "no_symbol_call",
            "n_sessions": None,
            "level": None,
            "due_date": "",
            "text": "这晚没有个股开平仓判断，只核对盘面陈述。",
        }
    else:
        result = RESULT_PENDING
        verification = _verification_for_intent(intent, trade_date=trade_date or decision_date)
    judgment = str(
        intent.get("llm_reason")
        or intent.get("thesis")
        or intent.get("reason")
        or llm.get("market_note")
        or ""
    ).strip()
    hold_days = int(intent.get("planned_hold_days") or 0) or _hold_days(intent) if symbol else None
    identity = "|".join(
        [
            decision_date,
            symbol or "HOLD",
            side,
            judgment,
            str((verification or {}).get("n_sessions") or hold_days or ""),
            str((verification or {}).get("level") or ""),
        ]
    )
    digest = sha1(identity.encode("utf-8")).hexdigest()[:10]
    decision_id = f"dec_{decision_date.replace('-', '')}_{symbol or 'HOLD'}_{side}_{digest}"
    explicit_recorded = str(
        plan.get("recorded_at") or plan.get("created_at") or plan.get("decided_at") or ""
    ).strip()
    if explicit_recorded:
        recorded_at = explicit_recorded
    elif backfill:
        recorded_at = ""
    else:
        recorded_at = _now_iso()
    return {
        "schema": SCHEMA,
        "disclaimer": DISCLAIMER,
        "decision_id": decision_id,
        "recorded_at": recorded_at,
        "structured_at": structured_at or _now_iso(),
        "backfill": bool(backfill),
        "provenance": "historical_review" if backfill else "prior_record",
        "decision_date": decision_date,
        "trade_date": trade_date,
        "symbol": symbol,
        "side": side,
        "judgment": judgment,
        "style": str(intent.get("style") or "") or None,
        "planned_hold_days": int(intent.get("planned_hold_days") or 0) or _hold_days(intent) if symbol else None,
        "seen": {
            "source": "data/daily",
            "as_of": decision_date,
            "open": None if bar is None else bar.get("open"),
            "high": None if bar is None else bar.get("high"),
            "low": None if bar is None else bar.get("low"),
            "close": None if bar is None else bar.get("close"),
            "pct_chg": None if bar is None else bar.get("pct_chg"),
            "ref_close": _f(intent.get("ref_close")),
        },
        "verification": verification,
        "result": result,
        "result_as_of": "",
        "result_evidence": mismatches,
        "prompt_id": stamp["prompt_id"],
        "prompt_version": stamp["prompt_version"],
        "menu_id": stamp["menu_id"],
        "menu_version": stamp["menu_version"],
        "llm_used": bool(llm.get("used")),
        "llm_model": str(llm.get("model") or ""),
        "committed": bool(plan.get("committed", True)) and result != RESULT_MISMATCH,
    }


def load_decision_records(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    out: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(row, dict):
            out.append(row)
    return out


def write_decision_records(path: Path, records: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in records)
    path.write_text(payload, encoding="utf-8")


def _judgment_fingerprint(row: dict[str, Any]) -> tuple[str, str, str, str, str]:
    verification = row.get("verification") if isinstance(row.get("verification"), dict) else {}
    return (
        str(row.get("judgment") or ""),
        str(verification.get("kind") or ""),
        str(verification.get("n_sessions") or ""),
        str(verification.get("level") or ""),
        str(row.get("planned_hold_days") or ""),
    )


def upsert_decision_records(path: Path, incoming: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    by_id: dict[str, dict[str, Any]] = {}
    for row in load_decision_records(path):
        key = str(row.get("decision_id") or "")
        if key:
            by_id[key] = row
    for row in incoming:
        key = str(row.get("decision_id") or "")
        if not key:
            continue
        old = by_id.get(key)
        incoming_row = dict(row)
        if old and _judgment_fingerprint(old) != _judgment_fingerprint(incoming_row):
            incoming_row["revises"] = key
            incoming_row["decision_id"] = f"{key}_rev_{sha1(str(incoming_row.get('structured_at') or _now_iso()).encode('utf-8')).hexdigest()[:8]}"
            by_id[str(incoming_row["decision_id"])] = incoming_row
            continue
        if old and old.get("result") == RESULT_MISMATCH:
            continue
        if old and old.get("result") in {RESULT_RIGHT, RESULT_WRONG} and incoming_row.get("result") == RESULT_PENDING:
            incoming_row["result"] = old["result"]
            incoming_row["result_as_of"] = old.get("result_as_of") or ""
            incoming_row["result_evidence"] = old.get("result_evidence") or incoming_row.get("result_evidence")
            incoming_row["recorded_at"] = old.get("recorded_at") or incoming_row.get("recorded_at")
            incoming_row["judgment"] = old.get("judgment")
            incoming_row["verification"] = old.get("verification")
        elif old:
            incoming_row["recorded_at"] = old.get("recorded_at") or incoming_row.get("recorded_at")
            incoming_row["judgment"] = old.get("judgment") or incoming_row.get("judgment")
            incoming_row["verification"] = old.get("verification") or incoming_row.get("verification")
        by_id[key] = incoming_row
    records = list(by_id.values())
    records.sort(key=lambda item: (str(item.get("decision_date") or ""), str(item.get("decision_id") or "")))
    write_decision_records(path, records)
    return records


def score_decision(record: dict[str, Any], *, daily_dir: Path, as_of: str) -> dict[str, Any]:
    updated = dict(record)
    result = str(updated.get("result") or "")
    if result in {RESULT_MISMATCH, RESULT_RIGHT, RESULT_WRONG, RESULT_NA, RESULT_EXPIRED_NO_EVIDENCE}:
        return updated
    verification = dict(updated.get("verification") or {})
    symbol = str(updated.get("symbol") or "")
    n_days = int(verification.get("n_sessions") or 0)
    level = _f(verification.get("level"))
    start = _as_day(updated.get("trade_date") or updated.get("decision_date"))
    if not symbol or n_days <= 0 or level is None or not start:
        updated["result"] = RESULT_NA
        return updated
    window = trade_days_from(start, n_days)
    due = str(verification.get("due_date") or (window[-1] if window else ""))
    closes = dict(_closes_from(symbol, start, daily_dir, n_days + 5))
    observed: list[tuple[str, float]] = []
    for day in window:
        if day > as_of:
            break
        if day in closes:
            observed.append((day, closes[day]))
    kind = str(verification.get("kind") or "stand_above")
    hit = False
    for day, close in observed:
        if kind == "break_below":
            if close < float(level):
                hit = True
                break
        elif close >= float(level):
            hit = True
            break
    if hit:
        updated["result"] = RESULT_RIGHT
        updated["result_as_of"] = as_of
        updated["result_evidence"] = [{"kind": "window_hit", "level": level, "observed": observed[: n_days]}]
        return updated
    due_passed = bool(due) and as_of >= due
    if len(observed) < n_days:
        if due_passed:
            updated["result"] = RESULT_EXPIRED_NO_EVIDENCE
            updated["result_as_of"] = as_of
            updated["result_evidence"] = [
                {"kind": "expired_missing_bars", "have": len(observed), "need": n_days, "due_date": due}
            ]
            return updated
        updated["result"] = RESULT_PENDING
        updated["result_as_of"] = as_of
        updated["result_evidence"] = [{"kind": "window_open", "have": len(observed), "need": n_days}]
        return updated
    updated["result"] = RESULT_WRONG
    updated["result_as_of"] = as_of
    updated["result_evidence"] = [{"kind": "window_miss", "level": level, "observed": observed}]
    return updated


def score_decision_log(records: list[dict[str, Any]], *, daily_dir: Path, as_of: str) -> list[dict[str, Any]]:
    return [score_decision(row, daily_dir=daily_dir, as_of=as_of) for row in records]


def _sort_for_page(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(
        records,
        key=lambda row: (
            PAGE_ORDER.get(str(row.get("result") or ""), 9),
            str(row.get("decision_date") or ""),
            str(row.get("symbol") or ""),
        ),
        reverse=False,
    )


def _is_backfill(row: dict[str, Any]) -> bool:
    return bool(row.get("backfill") or row.get("provenance") == "historical_review")


def _count_results(records: list[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for row in records:
        key = str(row.get("result") or RESULT_NA)
        counts[key] = counts.get(key, 0) + 1
    return counts


def _result_names() -> tuple[str, ...]:
    return (
        RESULT_WRONG,
        RESULT_MISMATCH,
        RESULT_EXPIRED_NO_EVIDENCE,
        RESULT_PENDING,
        RESULT_RIGHT,
        RESULT_NA,
    )


def render_owner_page(records: list[dict[str, Any]], *, html_path: Path, md_path: Path) -> None:
    ordered = _sort_for_page(records)
    live = [row for row in ordered if not _is_backfill(row)]
    review = [row for row in ordered if _is_backfill(row)]
    live_counts = _count_results(live)
    review_counts = _count_results(review)
    names = _result_names()
    lines = [
        f"# C 线决策对照 · {DISCLAIMER}",
        "",
        VERIFICATION_DERIVED_NOTE + "。错的排前面。价格只对照 `data/daily/`。这不是订单。",
        "历史复盘没有事前落盘时间，不与当晚预判混算对/错。",
        "",
        "事前记录："
        + " · ".join(f"{name} {live_counts.get(name, 0)}" for name in names),
        "历史复盘："
        + " · ".join(f"{name} {review_counts.get(name, 0)}" for name in names),
        "",
        "| 结果 | 来源 | 决策日 | 代码 | 方向 | 风格/天数 | 判断 | 验证条件 | 版本 | 落盘时刻 |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    html_rows: list[str] = []
    for row in ordered:
        result = str(row.get("result") or "")
        style = str(row.get("style") or "未标")
        hold = row.get("planned_hold_days") or ""
        ver = str((row.get("verification") or {}).get("text") or "")
        version = f"{row.get('prompt_version') or '未知'}/{row.get('menu_version') or '未知'}"
        origin = "历史复盘" if _is_backfill(row) else "事前"
        recorded = str(row.get("recorded_at") or "未知")
        judgment = str(row.get("judgment") or "").replace("|", "\\|").replace("\n", " ")
        if len(judgment) > 80:
            judgment = judgment[:77] + "…"
        lines.append(
            f"| {result} | {origin} | {row.get('decision_date')} | {row.get('symbol') or '—'} | "
            f"{row.get('side')} | {style}/{hold} | {judgment} | {ver} | {version} | {recorded} |"
        )
        color = {
            RESULT_WRONG: "#8b1e1e",
            RESULT_MISMATCH: "#8b4513",
            RESULT_EXPIRED_NO_EVIDENCE: "#5c4b1d",
            RESULT_PENDING: "#5c4b1d",
            RESULT_RIGHT: "#1e4d2b",
            RESULT_NA: "#444",
        }.get(result, "#333")
        html_rows.append(
            "<tr>"
            f"<td style='color:{color};font-weight:700'>{html.escape(result)}</td>"
            f"<td>{html.escape(origin)}</td>"
            f"<td>{html.escape(str(row.get('decision_date') or ''))}</td>"
            f"<td>{html.escape(str(row.get('symbol') or '—'))}</td>"
            f"<td>{html.escape(str(row.get('side') or ''))}</td>"
            f"<td>{html.escape(f'{style} / {hold}')}</td>"
            f"<td>{html.escape(judgment)}</td>"
            f"<td>{html.escape(ver)}</td>"
            f"<td>{html.escape(version)}</td>"
            f"<td>{html.escape(recorded)}</td>"
            "</tr>"
        )
    md_path.parent.mkdir(parents=True, exist_ok=True)
    md_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    html_doc = (
        "<!doctype html><html lang='zh-CN'><head><meta charset='utf-8'>"
        f"<title>C线决策对照 · {html.escape(DISCLAIMER)}</title>"
        "<style>body{font-family:sans-serif;margin:24px;background:#111;color:#eee}"
        "table{border-collapse:collapse;width:100%}td,th{border:1px solid #444;padding:6px 8px;font-size:14px;vertical-align:top}"
        "th{text-align:left;background:#222}h1{font-size:20px}</style></head><body>"
        f"<h1>C 线决策对照 · {html.escape(DISCLAIMER)}</h1>"
        f"<p><strong>{html.escape(VERIFICATION_DERIVED_NOTE)}</strong></p>"
        "<p>错的排前面。它会对照 <code>data/daily/</code> 核对当晚说的价格和涨跌。"
        "对不上的决策会被拒收，不会当成有效判断。这不是订单。"
        "历史复盘没有事前落盘时间，不对入当晚对/错统计。</p>"
        "<p>事前记录："
        + " · ".join(f"{html.escape(name)} {live_counts.get(name, 0)}" for name in names)
        + "</p><p>历史复盘："
        + " · ".join(f"{html.escape(name)} {review_counts.get(name, 0)}" for name in names)
        + "</p>"
        "<table><thead><tr>"
        "<th>结果</th><th>来源</th><th>决策日</th><th>代码</th><th>方向</th><th>风格/天数</th>"
        "<th>判断</th><th>验证条件</th><th>提示词/菜单</th><th>落盘时刻</th>"
        "</tr></thead><tbody>"
        + "".join(html_rows)
        + "</tbody></table></body></html>"
    )
    html_path.parent.mkdir(parents=True, exist_ok=True)
    html_path.write_text(html_doc, encoding="utf-8")


def records_from_plan(
    plan: dict[str, Any],
    *,
    daily_dir: Path,
    rejections: list[dict[str, Any]] | None = None,
    backfill: bool = False,
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for intent in plan.get("intents") or []:
        if not isinstance(intent, dict):
            continue
        out.append(
            build_decision_record(
                intent,
                plan=plan,
                daily_dir=daily_dir,
                backfill=backfill,
            )
        )
    for intent in rejections or []:
        if not isinstance(intent, dict):
            continue
        out.append(
            build_decision_record(
                intent,
                plan=plan,
                daily_dir=daily_dir,
                mismatches=list(intent.get("statement_rejections") or []),
                backfill=backfill,
            )
        )
    if not out:
        hold = {
            "symbol": "",
            "side": "hold",
            "llm_reason": str(((plan.get("llm") or {}) if isinstance(plan.get("llm"), dict) else {}).get("market_note") or ""),
            "style": "",
            "planned_hold_days": 0,
        }
        mismatches = [] if gate_plan_statements(plan, daily_dir=daily_dir).market_note_ok else [
            item
            for row in (rejections or [])
            for item in (row.get("statement_rejections") or [])
        ]
        if not mismatches:
            mismatches = verify_market_note(
                str(((plan.get("llm") or {}) if isinstance(plan.get("llm"), dict) else {}).get("market_note") or ""),
                decision_date=_as_day(plan.get("decision_date")),
                daily_dir=daily_dir,
            )
        out.append(
            build_decision_record(
                hold,
                plan=plan,
                daily_dir=daily_dir,
                mismatches=mismatches,
                backfill=backfill,
            )
        )
    return out


def _read_json(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def load_journal_rows(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(item, dict):
            rows.append(item)
    return rows


def latest_journal_by_date(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_day: dict[str, dict[str, Any]] = {}
    for row in rows:
        day = _as_day(row.get("as_of"))
        if day:
            by_day[day] = row
    return [by_day[key] for key in sorted(by_day)]


def plan_from_journal_row(row: dict[str, Any]) -> dict[str, Any]:
    tomorrow = row.get("tomorrow_plan") if isinstance(row.get("tomorrow_plan"), dict) else {}
    llm = row.get("llm") if isinstance(row.get("llm"), dict) else {}
    return {
        "decision_date": _as_day(row.get("as_of")),
        "trade_date": _as_day(tomorrow.get("trade_date") or row.get("trade_date")),
        "intents": list(tomorrow.get("intents") or []),
        "llm": llm,
        "committed": True,
        "prompt_version": row.get("prompt_version") or llm.get("prompt_version"),
        "menu_version": row.get("menu_version") or llm.get("menu_version"),
    }


def git_run_manifest(root: Path | None = None) -> dict[str, Any]:
    repo = Path(root or PROJECT_ROOT)
    head = ""
    dirty = -1
    try:
        head = subprocess.check_output(
            ["git", "rev-parse", "HEAD"],
            cwd=str(repo),
            text=True,
            stderr=subprocess.DEVNULL,
        ).strip()
        status = subprocess.check_output(
            ["git", "status", "--porcelain"],
            cwd=str(repo),
            text=True,
            stderr=subprocess.DEVNULL,
        )
        dirty = len([line for line in status.splitlines() if line.strip()])
    except (OSError, subprocess.CalledProcessError):
        pass
    return {"git_head": head, "dirty_file_count": dirty}


def c_line_output_paths(runtime: CyclePaths | None = None) -> dict[str, Path]:
    if runtime is None:
        return {
            "log": STATE_DIR / "decision_log.jsonl",
            "latest": STATE_DIR / "decision_latest.json",
            "html": RESULTS_DIR / "decision_page.html",
            "md": RESULTS_DIR / "decision_page.md",
            "manifest": RESULTS_DIR / "decision_run_manifest.json",
        }
    isolated = Path(runtime.journal_log) != Path(JOURNAL_PATH)
    if isolated:
        state = Path(runtime.journal_log).parent
        results = Path(runtime.cycle_latest).parent if runtime.cycle_latest else state
        return {
            "log": state / "decision_log.jsonl",
            "latest": state / "decision_latest.json",
            "html": results / "decision_page.html",
            "md": results / "decision_page.md",
            "manifest": results / "decision_run_manifest.json",
        }
    log = getattr(runtime, "decision_log", STATE_DIR / "decision_log.jsonl")
    latest = getattr(runtime, "decision_latest", STATE_DIR / "decision_latest.json")
    html = getattr(runtime, "decision_page", RESULTS_DIR / "decision_page.html")
    md = getattr(runtime, "decision_page_md", RESULTS_DIR / "decision_page.md")
    return {
        "log": Path(log),
        "latest": Path(latest),
        "html": Path(html),
        "md": Path(md),
        "manifest": Path(getattr(runtime, "cycle_latest", CYCLE_LATEST_PATH)).parent / "decision_run_manifest.json",
    }


def refresh_owner_outputs(
    records: list[dict[str, Any]],
    *,
    paths: dict[str, Path],
    daily_dir: Path,
    as_of: str,
    extra_manifest: dict[str, Any] | None = None,
) -> dict[str, Any]:
    scored = score_decision_log(records, daily_dir=daily_dir, as_of=as_of)
    write_decision_records(paths["log"], scored)
    latest = {
        "schema": SCHEMA,
        "disclaimer": DISCLAIMER,
        "as_of": as_of,
        "count": len(scored),
        "records": scored,
    }
    atomic_write_json(paths["latest"], latest)
    render_owner_page(scored, html_path=paths["html"], md_path=paths["md"])
    manifest = git_run_manifest()
    manifest.update(
        {
            "as_of": as_of,
            "daily_dir": str(daily_dir),
            "record_count": len(scored),
            "counts": {
                name: sum(1 for row in scored if row.get("result") == name and not _is_backfill(row))
                for name in _result_names()
            },
            "historical_review_counts": {
                name: sum(1 for row in scored if row.get("result") == name and _is_backfill(row))
                for name in _result_names()
            },
            "page_html": str(paths["html"]),
            "page_md": str(paths["md"]),
            "log": str(paths["log"]),
        }
    )
    if extra_manifest:
        manifest.update(extra_manifest)
    atomic_write_json(paths["manifest"], manifest)
    return {"ok": True, "as_of": as_of, "records": scored, "manifest": manifest, "paths": {k: str(v) for k, v in paths.items()}}


def record_evening_and_refresh_page(
    plan: dict[str, Any],
    *,
    daily_dir: Path,
    runtime: CyclePaths,
    as_of: str,
    rejections: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    paths = c_line_output_paths(runtime)
    incoming = records_from_plan(plan, daily_dir=daily_dir, rejections=rejections, backfill=False)
    merged = upsert_decision_records(paths["log"], incoming)
    return refresh_owner_outputs(merged, paths=paths, daily_dir=daily_dir, as_of=as_of)


def ingest_state(
    *,
    daily_dir: Path,
    state_dir: Path | None = None,
    plan_path: Path | None = None,
    journal_path: Path | None = None,
    out_dir: Path,
    as_of: str | None = None,
) -> dict[str, Any]:
    """Build/score the owner page from already-written 20:20 artifacts. No LLM."""

    state = Path(state_dir or STATE_DIR)
    journal_rows = load_journal_rows(Path(journal_path or (state / "journal.jsonl")))
    plans = [plan_from_journal_row(row) for row in latest_journal_by_date(journal_rows)]
    latest_plan = _read_json(Path(plan_path or (state / "plan_latest.json")))
    if latest_plan.get("decision_date"):
        plans.append(latest_plan)
    incoming: list[dict[str, Any]] = []
    all_rejections: list[dict[str, Any]] = []
    for plan in plans:
        if not plan.get("decision_date"):
            continue
        gated = gate_plan_statements(plan, daily_dir=daily_dir)
        all_rejections.extend(gated.rejections)
        incoming.extend(
            records_from_plan(
                {**plan, "intents": gated.accepted_intents},
                daily_dir=daily_dir,
                rejections=gated.rejections,
                backfill=True,
            )
        )
    if as_of:
        day = _as_day(as_of)
    else:
        day = max((_as_day(row.get("decision_date")) for row in incoming), default="")
    out = Path(out_dir)
    paths = {
        "log": out / "decision_log.jsonl",
        "latest": out / "decision_latest.json",
        "html": out / "decision_page.html",
        "md": out / "decision_page.md",
        "manifest": out / "decision_run_manifest.json",
    }
    merged = upsert_decision_records(paths["log"], incoming)
    payload = refresh_owner_outputs(
        merged,
        paths=paths,
        daily_dir=daily_dir,
        as_of=day,
        extra_manifest={"source": "ingest_state", "rejection_count": len(all_rejections)},
    )
    payload["rejection_count"] = len(all_rejections)
    return payload


def apply_gate_to_plan(plan: dict[str, Any], *, daily_dir: Path) -> StatementGateResult:
    gated = gate_plan_statements(plan, daily_dir=daily_dir)
    plan["intents"] = gated.accepted_intents
    if gated.rejections:
        plan["statement_rejections"] = [
            {
                "symbol": row.get("symbol"),
                "side": row.get("side"),
                "label": RESULT_MISMATCH,
                "hits": row.get("statement_rejections"),
            }
            for row in gated.rejections
        ]
    return gated


__all__ = [
    "DISCLAIMER",
    "VERIFICATION_DERIVED_NOTE",
    "RESULT_EXPIRED_NO_EVIDENCE",
    "RESULT_MISMATCH",
    "RESULT_PENDING",
    "RESULT_RIGHT",
    "RESULT_WRONG",
    "SCHEMA",
    "StatementGateResult",
    "apply_gate_to_plan",
    "build_decision_record",
    "c_line_output_paths",
    "gate_plan_statements",
    "ingest_state",
    "load_daily_bar",
    "record_evening_and_refresh_page",
    "render_owner_page",
    "score_decision",
    "score_decision_log",
    "verify_intent_against_daily",
]
