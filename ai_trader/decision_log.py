"""C-line decision log: structured records, daily-bar statement gate, owner page.

Reads only ``data/daily/{symbol}.csv``. Does not read research-chain products,
the 914 filter list, or candidate JSON. Does not call a model.
"""

from __future__ import annotations

import csv
import html
import json
import os
import re
import subprocess
import tempfile
import time
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from hashlib import sha1
from math import isfinite
from pathlib import Path
from typing import Any, Iterable

import pandas as pd

from ai_trader.paths import (
    CYCLE_LATEST_PATH,
    JOURNAL_PATH,
    PROJECT_ROOT,
    RESULTS_DIR,
    STATE_DIR,
    CyclePaths,
)
from ai_trader.prompt_pack import load_prompt_pack
from ai_trader.evaluation_contract import (
    EXCLUSION_LABELS, complete_outcome_evidence, declared_verification,
    decision_origin, eligibility, with_fill_evidence,
)
from quantdesk.price_basis import PRICE_BASIS_RAW, validate_price_frame_set
from quantdesk.trading_calendar import is_trade_day, load_a_share_trading_calendar
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
# 主体/时段本身定不下来的命中是"未核验"：安全拒收，但不得写成"已证实与数据不符"（T11）。
RESULT_UNVERIFIED = "未核验"
PAGE_ORDER = {
    RESULT_WRONG: 0,
    RESULT_MISMATCH: 1,
    RESULT_UNVERIFIED: 2,
    RESULT_EXPIRED_NO_EVIDENCE: 3,
    RESULT_PENDING: 4,
    RESULT_RIGHT: 5,
    RESULT_NA: 6,
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
    r"(?P<verb>涨(?:幅)?|跌(?:幅)?|回调|[+＋\-－])\s*约?\s*(?P<pct>[+-]?\d+(?:\.\d+)?)\s*%"
)
_N_SESSIONS = re.compile(r"近\s*(\d+)\s*个?交易日|近\s*(\d+)\s*日")
_CLAIM_TIME = re.compile(
    r"\d{4}-\d{2}-\d{2}|近\s*\d+\s*个?交易日|近\s*\d+\s*日|"
    r"今天|今日|当日|昨天|昨日|前天|近期|最近|明天|明日|后天|次日|往后|未来"
)
_CLAUSE_BOUNDARY = re.compile(r"[，,；;。！？!?\n]|但是|但|然而|不过")
_FUTURE_TIME = {"明天", "明日", "后天", "次日", "往后", "未来"}
_PREDICTION_CONTEXT = re.compile(r"预计|预判|预期|目标价?|将会?|有望|看涨|看跌")
_INTENT_FACT_FIELDS = ("llm_reason", "reason", "thesis", "recommendation_note")
# Any percent / point / yuan number the specific recognisers above did not claim
# is still a factual-looking statement. It is recorded as "未核验" instead of
# being let through as if it had been checked (charter §2 invariant 3).
_FACT_NUMBER = re.compile(r"(?<![\d.])([+\-]?\d+(?:\.\d+)?)\s*(?:%|％|个百分点|个点|元)")
# Clauses that state a plan or condition (stop-loss, give-up gap, target) are
# not claims about what already happened, so the catch-all leaves them alone.
_PLAN_CONTEXT = re.compile(r"放弃|止损|止盈|目标|打算|计划|如果|若|一旦|挂单|买点|卖点|加仓|减仓|仓位")
# The program appends news headlines to ``reason`` as "消息：…。"; that is outside
# text, not the trader's own statement, and is not checked as one.
_NEWS_SEGMENT = re.compile(r"消息：[^。]*。?")
TZ_SHANGHAI = timezone(timedelta(hours=8))
_DAILY_PRICE_CONSUMER = "paper_execution"
@dataclass(frozen=True)
class _DailyRowsCacheEntry:
    signature: tuple[int, int, int, int, int]
    rows: list[dict[str, Any]]


_DAILY_ROWS_CACHE: dict[tuple[str, str], _DailyRowsCacheEntry] = {}


@dataclass
class StatementGateResult:
    accepted_intents: list[dict[str, Any]]
    rejections: list[dict[str, Any]] = field(default_factory=list)
    market_note_ok: bool = False
    original_intents: list[dict[str, Any]] = field(default_factory=list)
    market_note_rejections: list[dict[str, Any]] = field(default_factory=list)
    market_note_status: str = "未核验"


def statement_gate_summary(gate: StatementGateResult) -> dict[str, Any]:
    """Describe what was proposed and why evidence was refused, without guessing."""
    hits = [hit for row in gate.rejections for hit in (row.get("statement_rejections") or [])]
    unverified = any(hit.get("evidence_status") == "unverified" for hit in hits)
    mismatch = any(hit.get("evidence_status") != "unverified" for hit in hits)
    status = "mixed" if unverified and mismatch else (
        "unverified" if unverified else ("mismatch" if mismatch else "accepted")
    )
    return {
        "status": status,
        "original_intent_count": len(gate.original_intents),
        "original_buy_count": sum(row.get("side") == "buy" for row in gate.original_intents),
        "original_sell_count": sum(row.get("side") == "sell" for row in gate.original_intents),
        "accepted_intent_count": len(gate.accepted_intents),
        "rejected_intent_count": sum(row.get("side") in {"buy", "sell"} for row in gate.rejections),
        # 照常放行、但理由里有核不了/说错的说法的意图（目前只有卖出）。
        "flagged_intents": [
            {"symbol": row.get("symbol"), "side": row.get("side"), "label": row.get("statement_label")}
            for row in gate.accepted_intents if row.get("statement_flags")
        ],
        "market_note_ok": gate.market_note_ok,
        "market_note_status": gate.market_note_status,
        "market_note_rejections": deepcopy(gate.market_note_rejections),
        "original_intents": deepcopy(gate.original_intents),
    }


def _now_iso() -> str:
    return datetime.now(TZ_SHANGHAI).replace(microsecond=0).isoformat()


def _as_day(value: Any) -> str:
    return str(value or "")[:10]


def _f(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not isfinite(number):
        return None
    return number


def _price_close_enough(claimed: float, actual: float) -> bool:
    return (
        isfinite(claimed)
        and isfinite(actual)
        and abs(claimed - actual) <= max(0.02, abs(actual) * 0.005)
    )


def _pct_close_enough(claimed: float, actual: float | None) -> bool:
    """Compare percent claims as percent. Do not guess that 40 means 0.40."""

    if actual is None:
        return False
    return abs(claimed - actual) <= 1.0


@dataclass(frozen=True)
class _ClaimScope:
    evidence_date: str
    period: str = ""
    sessions: int | None = None
    forecast: bool = False
    ambiguous: bool = False


def _scope_period_unspecified(scope: _ClaimScope) -> bool:
    """True when a claim states no period at all.

    Such a claim must be reported as unverified rather than guessed as "today"
    or auto-widened to five sessions (B3).  It is distinct from ``ambiguous``
    tokens such as 近期, and from the recursion inside a numbered-period scope
    (whose returned scope carries a non-empty ``period``).
    """

    return not scope.forecast and not scope.period and not scope.ambiguous


def _claim_scope(text: str, start: int, decision_date: str) -> _ClaimScope:
    # Time belongs to the current clause, including intervening subject words.
    prefix = _CLAUSE_BOUNDARY.split(text[:start])[-1]
    times = list(_CLAIM_TIME.finditer(prefix))
    if not times:
        return _ClaimScope(decision_date, forecast=bool(_PREDICTION_CONTEXT.search(prefix)))
    token = re.sub(r"\s+", "", times[-1].group())
    if token in _FUTURE_TIME:
        return _ClaimScope(decision_date, period=token, forecast=True)
    if token in {"近期", "最近"}:
        return _ClaimScope(decision_date, period=token, ambiguous=True)
    numbered = _N_SESSIONS.fullmatch(token)
    if numbered:
        sessions = int(numbered.group(1) or numbered.group(2))
        endpoint = _claim_scope(prefix, times[-1].start(), decision_date)
        return _ClaimScope(
            endpoint.evidence_date,
            period=token,
            sessions=sessions,
            forecast=endpoint.forecast,
            ambiguous=sessions < 1 or endpoint.ambiguous,
        )
    if token in {"今天", "今日", "当日"}:
        return _ClaimScope(decision_date, period=token)
    if token in {"昨天", "昨日", "前天"}:
        offset = 2 if token == "前天" else 1
        day = (date.fromisoformat(decision_date) - timedelta(days=offset)).isoformat()
        return _ClaimScope(day, period=token)
    try:
        day = date.fromisoformat(token).isoformat()
    except ValueError:
        return _ClaimScope(token, period=token, ambiguous=True)
    return _ClaimScope(day, period=token, forecast=day > decision_date)


def _intent_fact_texts(intent: dict[str, Any]) -> list[str]:
    texts: list[str] = []
    fields = ("reason",) if intent.get("side") == "sell" else _INTENT_FACT_FIELDS
    for key in fields:
        value = str(intent.get(key) or "").strip()
        if key == "reason":
            value = _NEWS_SEGMENT.sub("", value).strip()
        if value and value not in texts:
            texts.append(value)
    return texts


def _row_date(row: dict[str, str]) -> str:
    raw = str(row.get("date") or row.get("日期") or "").strip().strip('"')
    return raw[:10].replace("/", "-")


def _calendar_is_open(day: date, open_by_date: dict[str, bool]) -> bool:
    return bool(open_by_date.get(day.isoformat(), day.weekday() < 5))


def _previous_session(day: str, open_by_date: dict[str, bool]) -> str:
    cursor = date.fromisoformat(day) - timedelta(days=1)
    for _ in range(371):
        if _calendar_is_open(cursor, open_by_date):
            return cursor.isoformat()
        cursor -= timedelta(days=1)
    raise RuntimeError("trading calendar lookup exceeded 370 days")


def _daily_file_signature(path: Path) -> tuple[int, int, int, int, int] | None:
    try:
        stat = path.stat()
    except FileNotFoundError:
        return None
    return (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns)



def iter_daily_rows(symbol: str, daily_dir: Path) -> list[dict[str, Any]]:
    key = (str(symbol), str(Path(daily_dir)))
    path = Path(daily_dir) / f"{symbol}.csv"
    signature = _daily_file_signature(path)
    if signature is None or not path.is_file():
        _DAILY_ROWS_CACHE.pop(key, None)
        return []
    cached = _DAILY_ROWS_CACHE.get(key)
    if cached is not None and cached.signature == signature:
        return [dict(row) for row in cached.rows]
    _DAILY_ROWS_CACHE.pop(key, None)
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            raw_rows = list(csv.DictReader(handle))
    except FileNotFoundError:
        return []
    # Classify the entire original frame, including malformed-date rows and
    # every original column, before aliases, filtering, sorting or arithmetic.
    validation = validate_price_frame_set(
        {str(symbol): pd.DataFrame(raw_rows)}, consumer=_DAILY_PRICE_CONSUMER
    )
    evidence = validation["per_symbol"][str(symbol)]
    basis = evidence["price_basis"]
    basis_reason = evidence["reason_code"]
    if _daily_file_signature(path) != signature:
        # A concurrent update cannot supply one stable version of the evidence.
        basis_reason = "daily_file_changed_during_read"
    rows: list[dict[str, Any]] = []
    for raw in raw_rows:
        day = _row_date(raw)
        try:
            day = date.fromisoformat(day).isoformat()
        except ValueError:
            continue
        rows.append(
            {
                "date": day,
                "open": _f(raw.get("open") or raw.get("开盘")),
                "high": _f(raw.get("high") or raw.get("最高")),
                "low": _f(raw.get("low") or raw.get("最低")),
                "close": _f(raw.get("close") or raw.get("收盘")),
                "pct_chg": _f(raw.get("pct_chg") or raw.get("涨跌幅")),
                "adjust": raw.get("adjust") or "",
                "price_basis": basis,
                "price_basis_reason": basis_reason,
            }
        )
    rows.sort(key=lambda item: item["date"])
    if basis_reason:
        if basis_reason != "daily_file_changed_during_read":
            _DAILY_ROWS_CACHE[key] = _DailyRowsCacheEntry(signature, rows)
        return [dict(row) for row in rows]
    # Reuse one parsed lookup so each historical row does not resolve a path.
    open_by_date = load_a_share_trading_calendar().get("is_open_by_date") or {}
    prev_close: float | None = None
    prev_date = ""
    for row in rows:
        if (
            row.get("pct_chg") is None
            and row.get("close") is not None
            and prev_close
            and prev_date == _previous_session(row["date"], open_by_date)
        ):
            row["pct_chg"] = (float(row["close"]) / prev_close - 1.0) * 100.0
        if row.get("close") is not None:
            prev_close = float(row["close"])
            prev_date = row["date"]
    _DAILY_ROWS_CACHE[key] = _DailyRowsCacheEntry(signature, rows)
    return [dict(row) for row in rows]


def load_daily_bar(symbol: str, as_of: str, daily_dir: Path) -> dict[str, Any] | None:
    day = _as_day(as_of)
    for row in iter_daily_rows(symbol, daily_dir):
        if row["date"] == day:
            return row
    return None


def _seen_snapshot(symbol: str, as_of: str, daily_dir: Path) -> dict[str, Any]:
    """当晚核验时真正看到的日线快照，随被拒项一起落盘。

    只在决策当晚取一次；日后行情被修正时，重建历史用的是这份快照，而不是重读日线。
    这个键不参与 ``judgment``/``identity``/``decision_id`` 的推导。
    """

    bar = load_daily_bar(symbol, as_of, daily_dir) if symbol else None
    return {
        "as_of": as_of,
        "open": None if bar is None else bar.get("open"),
        "high": None if bar is None else bar.get("high"),
        "low": None if bar is None else bar.get("low"),
        "close": None if bar is None else bar.get("close"),
        "pct_chg": None if bar is None else bar.get("pct_chg"),
    }


_SEEN_PRICE_KEYS = ("open", "high", "low", "close", "pct_chg")


def _seen_field(
    intent: dict[str, Any],
    *,
    recorded: dict[str, Any] | None,
    bar: dict[str, Any] | None,
    decision_date: str,
) -> dict[str, Any]:
    """记录的 ``seen``：当晚落盘的所见 > 现读日线 > 标缺失。

    当晚真的看到了行情，就用它并标 ``recorded_at_decision``；当晚本来就没有日线
    （``seen_at_decision`` 各项都为 null）同样标 ``missing``，不因为后来补上了行情
    或行情被修正就改写当时所见。缺失时价格一律留 ``None``，不拿当前行情或当前时钟补造。
    """

    ref_close = _f(intent.get("ref_close"))
    as_of = _as_day((recorded or {}).get("as_of")) or decision_date
    if recorded is not None and any(recorded.get(key) is not None for key in _SEEN_PRICE_KEYS):
        return {
            "source": "recorded_at_decision",
            "as_of": as_of,
            "open": recorded.get("open"),
            "high": recorded.get("high"),
            "low": recorded.get("low"),
            "close": recorded.get("close"),
            "pct_chg": recorded.get("pct_chg"),
            "ref_close": ref_close,
        }
    if recorded is None and bar is not None:
        return {
            "source": "data/daily",
            "as_of": decision_date,
            "open": bar.get("open"),
            "high": bar.get("high"),
            "low": bar.get("low"),
            "close": bar.get("close"),
            "pct_chg": bar.get("pct_chg"),
            "ref_close": ref_close,
        }
    return {
        "source": "missing",
        "as_of": as_of,
        "open": None,
        "high": None,
        "low": None,
        "close": None,
        "pct_chg": None,
        "ref_close": ref_close,
    }


def _closes_from(symbol: str, start: str, daily_dir: Path, count: int) -> list[tuple[str, float]]:
    out: list[tuple[str, float]] = []
    rows = iter_daily_rows(symbol, daily_dir)
    if any(_basis_untrusted(row) for row in rows):
        return out
    for row in rows:
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
    if days < 1 or any(_basis_untrusted(row) for row in rows):
        return None
    upto = [row for row in rows if row["date"] <= as_of and row.get("close")]
    if len(upto) <= days or upto[-1]["date"] != as_of:
        return None
    window = upto[-1 - days :]
    cursor = date.fromisoformat(window[0]["date"])
    open_by_date = load_a_share_trading_calendar().get("is_open_by_date") or {}
    expected_dates: list[str] = []
    while cursor.isoformat() <= as_of:
        if _calendar_is_open(cursor, open_by_date):
            expected_dates.append(cursor.isoformat())
        cursor += timedelta(days=1)
    if expected_dates != [row["date"] for row in window]:
        return None
    prior = float(window[0]["close"])
    last = float(window[-1]["close"])
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
    """命中项。``claim_indeterminate`` 标记"这句话本身没法定死"（主体/时段定不下来）：
    命中项的 label 保持既有语义（既有回归钉着它），记录与展示层据此把主结论写成"未核验"。"""
    payload = {"kind": kind, "detail": detail, "label": RESULT_MISMATCH}
    payload.update(extra)
    return payload




def _pct_claim_hits(
    claimed: float,
    scope: _ClaimScope,
    rows: list[dict[str, Any]],
    *,
    kind: str,
    symbol: str = "",
) -> list[dict[str, Any]]:
    if scope.forecast:
        return []
    evidence: dict[str, Any] = {"claimed": claimed, "evidence_date": scope.evidence_date}
    if symbol:
        evidence["symbol"] = symbol
    if scope.ambiguous or _scope_period_unspecified(scope):
        return [
            _mismatch(
                "claimed_pct_ambiguous_period",
                "陈述的涨跌时段不明确，不能核对。",
                claim_indeterminate=True,
                evidence_status="unverified",
                **evidence,
            )
        ]
    blocked = _basis_untrusted(rows[0]) if rows else ""
    if blocked:
        return [
            _mismatch(
                "claimed_price_basis_untrusted",
                f"{symbol} 日线价格口径不可用作原始核对（{blocked}）",
                actual=None,
                evidence_status="unverified",
                price_basis=rows[0].get("price_basis"),
                reason_code=blocked,
                **evidence,
            )
        ]
    bar = next((row for row in rows if row["date"] == scope.evidence_date), None)
    if scope.sessions is not None:
        actual = _change_n(rows, scope.evidence_date, scope.sessions)
    else:
        actual = None if bar is None else bar.get("pct_chg")
    if actual is None:
        return [
            _mismatch(
                "claimed_pct_no_bar" if bar is None and symbol else "claimed_pct_missing_evidence",
                f"陈述{scope.evidence_date}时段涨跌，证据不足。",
                actual=None,
                evidence_status="unverified",
                **evidence,
            )
        ]
    if _pct_close_enough(claimed, actual):
        return []
    return [
        _mismatch(
            kind,
            f"陈述{claimed}%，该票对应时段日线涨跌{actual}",
            actual=actual,
            **evidence,
        )
    ]




def _signed_pct_claim(verb: str, raw: str) -> float:
    magnitude = abs(float(raw))
    token = str(verb or "")
    if token.startswith("跌") or token.startswith("回调") or token in {"-", "－"}:
        return -magnitude
    if raw.strip().startswith("-") or raw.strip().startswith("－"):
        return -magnitude
    return magnitude




def _basis_untrusted(row: dict[str, Any] | None) -> str:
    if not row:
        return "unknown_price_basis"
    reason = str(row.get("price_basis_reason") or "")
    basis = str(row.get("price_basis") or "")
    if reason in {"mixed_price_basis", "unknown_price_basis"}:
        return reason
    if basis != PRICE_BASIS_RAW:
        return reason or "consumer_price_basis_not_allowed"
    return ""


# T11：只有"证券简称形态"的中文词才当作陈述主体。清单能绑定时永远优先用清单；清单缺这条
# 或重名绑不上时，这句话就是"主体缺证"，不得退回提议标的去核（禁止猜证券）。
# 这里不用宽泛中文分词，避免把"近5个交易日"这类时段词当成票名。
SECURITY_NAME_SUFFIXES = (
    "银行", "证券", "保险", "信托", "期货",
    "科技", "软件", "网络", "信息", "数据", "智能", "通信", "电子", "半导体", "光电",
    "医药", "生物", "制药", "医疗", "健康", "器械",
    "能源", "电力", "石油", "石化", "化工", "燃气", "水务", "环保", "光伏", "风电", "锂业", "稀土",
    "煤业", "矿业", "有色", "钢铁", "水泥", "建材", "建筑", "地产", "置业",
    "汽车", "重工", "机械", "电气", "电器", "家电", "装备",
    "食品", "乳业", "酒业", "饮料", "农业", "种业", "牧业", "渔业", "纺织", "服装", "纸业", "包装",
    "物流", "航运", "航空", "机场", "港口", "高速", "公路", "铁路", "旅游", "酒店", "餐饮",
)
_CLAIM_WORDS = ("收盘", "开盘", "最高", "最低", "报收", "涨", "跌", "价格", "成交", "换手", "市值", "估值")


# T11：陈述主体识别的词表。先把时间/量词从**紧贴陈述之前**的中文词里剥掉，再用通用词过滤；
# 只有剥完还剩下的短中文词才算"像证券简称的主体"。不靠后缀表，也不给某只票开特例。
_SUBJECT_TIME_WORDS = (
    "今天", "今日", "当日", "当天", "昨天", "昨日", "前天", "近期", "最近", "目前", "现在",
    "明天", "明日", "后天", "次日", "往后", "未来", "本周", "上周", "本月", "上月", "今年",
    "去年", "以来", "至今", "此前", "之后", "随后", "早前", "上午", "下午", "盘中", "当晚",
)
_SUBJECT_PERIOD_WORDS = ("个交易日", "交易日", "季度", "小时", "分钟", "个月", "个", "只", "款", "家")
_SUBJECT_GENERIC_WORDS = (
    "股票", "个股", "板块", "市场", "两市", "大盘", "指数", "行业", "概念", "题材", "龙头",
    "多只", "多数", "部分", "个别", "该股", "本票", "标的", "组合", "持仓", "仓位", "账户",
    "现金", "资金", "情绪", "趋势", "位置", "区间", "均价", "成交", "换手", "市值", "估值",
    "涨跌", "涨幅", "跌幅", "价格", "走势", "形态", "结构", "风险", "机会", "建议", "观察",
    "关注", "研究", "记录", "理由", "逻辑", "观点", "判断", "计划", "目标", "策略", "交易",
    "票", "股", "整体", "全部", "所有", "上述", "以上", "此类", "其余", "收盘", "开盘",
)


def _subject_like_token(prefix: str) -> str:
    """紧贴陈述之前的主体词；剥掉时间/量词，通用词不算主体，长度 2–6 才算。

    在**原文**上操作（不先切成纯中文串），所以「万科A」「ST海航」这类带字母的主体也能取到。
    """
    text = (prefix or "").strip()
    changed = True
    while text and changed:
        changed = False
        for word in _SUBJECT_TIME_WORDS + _SUBJECT_PERIOD_WORDS:
            if text.endswith(word) and len(text) > len(word):
                text = text[: -len(word)]
                changed = True
        trimmed = text.rstrip("0123456789.%-（）() ")
        if trimmed != text:
            text = trimmed
            changed = True
    match = re.search(r"([A-Za-z0-9\u4e00-\u9fff]+)$", text)
    chunk = match.group(1) if match else ""
    if not (2 <= len(chunk) <= 6) or chunk[0].isdigit():
        return ""
    if chunk in set(_SUBJECT_TIME_WORDS) | set(_SUBJECT_PERIOD_WORDS):
        return ""
    if any(word in chunk for word in _SUBJECT_GENERIC_WORDS + _SUBJECT_TIME_WORDS + _SUBJECT_PERIOD_WORDS):
        return ""
    if not re.search(r"[\u4e00-\u9fff]", chunk):
        return ""
    return chunk


def _unresolved_subject_in(prefix: str, following: str = "") -> str:
    """句子里"像证券简称"却绑定不到代码的中文主体；没有则空串。

    只在清单绑定失败后才调用：能绑上清单的主体根本走不到这里。两道判据互为补充：
    ① ``_subject_like_token``：紧贴陈述之前的中文主体词（不依赖后缀表，因此不限某几只票）；
    ② 后缀形态扫描：长句里出现的证券简称（如"浦发银行今天缩量"这种带别的话的句子）。
    """
    token = _subject_like_token(prefix)
    if token:
        return token
    for run in re.findall(r"[\u4e00-\u9fff]{2,}", prefix or ""):
        for size in (5, 4, 3, 2):
            for start in range(0, len(run) - size + 1):
                token = run[start : start + size]
                tail = run[start + size :] + (following or "")
                if not tail or not any(word in tail for word in _CLAIM_WORDS):
                    continue  # 主体后面必须紧跟一句价格/涨跌陈述，才算在说证券
                if len(token) > 2 and any(
                    token.endswith(suffix) and len(token) > len(suffix)
                    for suffix in SECURITY_NAME_SUFFIXES
                ):
                    return token
    return ""



def _verify_fact_text(
    text: str, *, decision_date: str, daily_dir: Path,
    default_symbol: str = "", name_to_symbol: dict[str, str] | None = None,
) -> tuple[list[dict[str, Any]], int]:
    """One recognizer for both market notes and per-intent factual prose.

    Only explicit subjects (or the intent's own subject) bind evidence. No
    recognized historical facts means unverified, not a successful comparison.
    """
    claims: list[tuple[int, int, str, float]] = []
    tagged = list(_SYMBOL_PCT.finditer(text))
    for match in tagged:
        claims.append((match.start(2), match.end(2), "pct", float(match.group(2))))
    for match in _UNTAGGED_PCT.finditer(text):
        if not any(start <= match.start("pct") < end for start, end, _, _ in claims):
            claims.append((match.start(), match.end(), "pct_untagged", _signed_pct_claim(match.group("verb"), match.group("pct"))))
    for pattern, field_name in ((_CLOSE_PRICE, "close"), (_OPEN_PRICE, "open")):
        for match in pattern.finditer(text):
            claims.append((match.start(), match.end(), field_name, float(match.group(1))))
    hits: list[dict[str, Any]] = []
    checked = 0
    for start, end, kind, claimed in sorted(claims):
        scope = _claim_scope(text, start, decision_date)
        if scope.forecast:
            continue
        checked += 1
        prefix = _CLAUSE_BOUNDARY.split(text[:start])[-1]
        subjects = [(m.start(), m.group()) for m in re.finditer(r"(?<!\d)\d{6}(?!\d)", prefix)]
        for name, symbol in (name_to_symbol or {}).items():
            if name and re.fullmatch(r"\d{6}", str(symbol)):
                subjects.extend((m.start(), str(symbol)) for m in re.finditer(re.escape(name), prefix))
        # 主体绑定保持既有语义：显式代码、或清单内名称优先；都没有时用提议标的自己
        # （名称映射只从运行时清单来，见 ``load_name_to_symbol``；不替它猜别的票）。
        # 主体绑定保持既有语义：显式代码、或清单内名称优先；都没有时，若句子里出现了
        # "像证券简称"的中文主体却绑不上（清单缺这条或重名），就明确缺证——**不退回提议
        # 标的**拿它的日线把这句话核成"已核对"（T11；清单只来自 ``load_name_to_symbol``）。
        symbol = max(subjects, key=lambda item: item[0])[1] if subjects else ""
        if not symbol:
            unresolved = _unresolved_subject_in(prefix, text[start:])
            if unresolved:
                hits.append(_mismatch(
                    "claimed_subject_unresolved",
                    f"陈述出现“{unresolved}”，运行时证券清单绑定不到唯一代码，未核验。",
                    claimed=claimed,
                    subject_text=unresolved,
                    evidence_status="unverified",
                    claim_indeterminate=True,
                    evidence_date=scope.evidence_date,
                ))
                continue
            symbol = default_symbol
        if not symbol:
            hits.append(_mismatch("claimed_subject_unresolved", "陈述主体无法绑定日线，未核验。",
                                  claimed=claimed, evidence_status="unverified",
                                  claim_indeterminate=True, evidence_date=scope.evidence_date))
            continue
        rows = iter_daily_rows(symbol, daily_dir)
        if kind.startswith("pct"):
            hits.extend(_pct_claim_hits(claimed, scope, rows, kind="claimed_" + kind, symbol=symbol))
            continue
        label = "收盘" if kind == "close" else "开盘"
        extra = {"claimed": claimed, "symbol": symbol, "evidence_date": scope.evidence_date}
        if scope.ambiguous or _scope_period_unspecified(scope):
            hits.append(_mismatch(f"claimed_{kind}_ambiguous_period", f"{label}价对应的时段不明确，不能核对。",
                                  claim_indeterminate=True,
                                  evidence_status="unverified", **extra))
            continue
        bar = next((r for r in rows if r["date"] == scope.evidence_date), None)
        blocked = _basis_untrusted(rows[0]) if rows else ""
        if blocked:
            hits.append(_mismatch("claimed_price_basis_untrusted", f"{symbol} 日线价格口径不可用作原始核对（{blocked}）",
                                  evidence_status="unverified", reason_code=blocked, **extra))
            continue
        actual = None if bar is None else bar.get(kind)
        if actual is None or not _price_close_enough(claimed, float(actual)):
            hits.append(_mismatch(f"claimed_{kind}", f"陈述{scope.evidence_date}{label}{claimed}，日线{label}{actual}",
                                  actual=actual, **extra, **({"evidence_status": "unverified"} if actual is None else {})))
    # Catch-all: a percent/point/yuan number none of the recognisers above claimed
    # is never treated as checked. Forecasts and plan/condition clauses are skipped.
    for match in _FACT_NUMBER.finditer(text):
        pos = match.start(1)
        if any(start <= pos < end for start, end, _, _ in claims):
            continue
        scope = _claim_scope(text, pos, decision_date)
        clause = _clause_around(text, pos)
        if scope.forecast or _PLAN_CONTEXT.search(clause):
            continue
        checked += 1
        hits.append(_mismatch(
            "claimed_unrecognized",
            f"“{match.group(0)}”这句说法程序认不出该怎么核对，未核验。",
            claimed_text=match.group(0),
            clause=clause[:80],
            evidence_status="unverified",
            claim_indeterminate=True,
            evidence_date=scope.evidence_date,
        ))
    return hits, checked


def _clause_around(text: str, pos: int) -> str:
    starts = [item.end() for item in _CLAUSE_BOUNDARY.finditer(text, 0, pos)]
    start = starts[-1] if starts else 0
    following = _CLAUSE_BOUNDARY.search(text, pos)
    return text[start: following.start() if following else len(text)]


def verify_intent_against_daily(
    intent: dict[str, Any],
    *,
    decision_date: str,
    daily_dir: Path,
    market_note: str = "",
    name_to_symbol: dict[str, str] | None = None,
) -> list[dict[str, Any]]:
    symbol = str(intent.get("symbol") or "").strip()
    if not symbol:
        return [_mismatch("missing_symbol", "决策没有代码")]
    rows = iter_daily_rows(symbol, daily_dir)
    bar = next((row for row in rows if row["date"] == _as_day(decision_date)), None)
    if bar is None:
        return [
            _mismatch(
                "daily_bar_missing",
                f"{symbol} 在 {decision_date} 没有日线",
                symbol=symbol,
                evidence_date=decision_date,
                evidence_status="unverified",
            )
        ]
    hits: list[dict[str, Any]] = []
    blocked = _basis_untrusted(bar)
    if blocked:
        return [
            _mismatch(
                "claimed_price_basis_untrusted",
                f"{symbol} 日线价格口径不可用作原始核对（{blocked}）",
                symbol=symbol,
                actual=None,
                evidence_date=decision_date,
                evidence_status="unverified",
                price_basis=bar.get("price_basis"),
                reason_code=blocked,
            )
        ]
    ref = _f(intent.get("ref_close"))
    close = bar.get("close")
    if ref is not None and (close is None or not _price_close_enough(ref, float(close))):
        hits.append(
            _mismatch(
                "ref_close",
                f"{symbol} 记录收盘{ref}，日线收盘{close}",
                symbol=symbol,
                claimed=ref,
                actual=close,
                evidence_date=decision_date,
                **({"evidence_status": "unverified"} if close is None else {}),
            )
        )
    session = intent.get("session") if isinstance(intent.get("session"), dict) else {}
    session_close = _f(session.get("daily_close"))
    if session_close is not None and (close is None or not _price_close_enough(session_close, float(close))):
        hits.append(
            _mismatch(
                "session_daily_close",
                f"{symbol} 会话记录收盘{session_close}，日线收盘{close}",
                symbol=symbol,
                claimed=session_close,
                actual=close,
                evidence_date=decision_date,
                **({"evidence_status": "unverified"} if close is None else {}),
            )
        )
    for fact in _intent_fact_texts(intent):
        hits.extend(_verify_fact_text(fact, decision_date=decision_date, daily_dir=daily_dir,
                                      default_symbol=symbol, name_to_symbol=name_to_symbol)[0])
    hits.extend(verify_market_note(market_note, decision_date=decision_date, daily_dir=daily_dir,
                                   name_to_symbol=name_to_symbol))
    return hits


def verify_market_note(market_note: str, *, decision_date: str, daily_dir: Path,
                       name_to_symbol: dict[str, str] | None = None) -> list[dict[str, Any]]:
    return _verify_fact_text(market_note, decision_date=decision_date, daily_dir=daily_dir,
                             name_to_symbol=name_to_symbol)[0]


NAME_COLUMN_ALIASES = ("name", "名称", "证券名称", "stock_name", "security_name")
SYMBOL_COLUMN_ALIASES = ("symbol", "code", "代码", "证券代码", "ts_code", "stock_code")
# 运行时**自己的**可信证券清单（只读；缺失即视为缺证，绝不联网猜票）。
NAME_MAP_SOURCES = (
    Path("data") / "metadata" / "security_master.csv",
    Path("data") / "universe" / "a_share_symbols.csv",
    Path("data") / "universe" / "universe.csv",
    Path("data") / "universe" / "low_price_learning_symbols.csv",
)


def _project_root_for(*, root: str | Path | None = None, daily_dir: Path | None = None) -> Path | None:
    """定位项目根：给了 root 就用它；否则从 daily_dir 往上找带 ``data/`` 的那一层。"""

    if root is not None:
        return Path(root)
    if daily_dir is None:
        return None
    resolved = Path(daily_dir).resolve()
    for candidate in resolved.parents:
        if (candidate / "data").is_dir():
            return candidate
    return None


def load_name_to_symbol(*, root: str | Path | None = None,
                        daily_dir: Path | None = None) -> dict[str, str]:
    """从运行时自己的可信证券清单建立 名称→代码 映射（只读，缺证即空映射）。

    - 只读 ``data/metadata``、``data/universe`` 下的清单；不联网、不打网络补丁；
    - **重名不入表**：同一个名称映射到多个代码时宁可让那句话保持未核验，也不替它挑一只票；
    - 清单缺失/无名称列时返回空映射，调用方据此把 unknown 保持 unverified（缺证明确）。
    """

    base = _project_root_for(root=root, daily_dir=daily_dir)
    if base is None:
        return {}
    candidates: dict[str, set[str]] = {}
    for relative in NAME_MAP_SOURCES:
        path = base / relative
        if not path.is_file():
            continue
        try:
            with path.open(encoding="utf-8-sig", errors="replace", newline="") as handle:
                reader = csv.DictReader(handle)
                fields = {str(name).strip().lower(): name for name in (reader.fieldnames or [])}
                name_key = next((fields[alias] for alias in NAME_COLUMN_ALIASES if alias in fields), None)
                symbol_key = next((fields[alias] for alias in SYMBOL_COLUMN_ALIASES if alias in fields), None)
                if not name_key or not symbol_key:
                    continue
                for row in reader:
                    name = str(row.get(name_key) or "").strip()
                    digits = re.sub(r"\D", "", str(row.get(symbol_key) or ""))
                    if name and len(digits) == 6:
                        candidates.setdefault(name, set()).add(digits)
        except OSError:
            continue
    return {name: next(iter(symbols)) for name, symbols in candidates.items() if len(symbols) == 1}


def gate_plan_statements(plan: dict[str, Any], *, daily_dir: Path,
                         name_to_symbol: dict[str, str] | None = None) -> StatementGateResult:
    decision_date = _as_day(plan.get("decision_date") or plan.get("as_of"))
    llm = plan.get("llm") if isinstance(plan.get("llm"), dict) else {}
    market_note = str(llm.get("market_note") or "")
    note_hits, note_fact_count = _verify_fact_text(market_note, decision_date=decision_date,
                                                  daily_dir=daily_dir, name_to_symbol=name_to_symbol)
    accepted: list[dict[str, Any]] = []
    rejections: list[dict[str, Any]] = []
    for intent in plan.get("intents") or []:
        if not isinstance(intent, dict):
            continue
        # 按买卖分开的闸门（所有者规则 2）：每条意图只看**它自己**的理由。盘面点评的命中只做标注
        # （market_note_status / market_note_rejections），不再并到当晚每一条意图上——
        # 以前点评里一个认不出的数字（如「920627一周68%」）就会把当晚全部买卖都拦下。
        hits = verify_intent_against_daily(
            intent,
            decision_date=decision_date,
            daily_dir=daily_dir,
            market_note="",
            name_to_symbol=name_to_symbol,
        )
        if hits and str(intent.get("side") or "") == "sell":
            # 卖出照常放行：核不了/说错的卖出理由只标注，不拦单（卖出只减仓，不新增风险）。
            row = dict(intent)
            row["statement_flags"] = hits
            row["statement_unverified"] = [
                hit for hit in hits if hit.get("evidence_status") == "unverified"
            ]
            row["statement_label"] = _flag_label(hits)
            accepted.append(row)
            continue
        if hits:
            # 既有语义：只要有一句核对不上就不放行（s2、时间语义、反编造的固定期望都钉在这里）。
            # T11 只做**标注分层**：把"未核验"与"确定不符"的命中都随拒绝行留下，让记录与页面
            # 能说清是"无法核验"还是"确实说错"，而不是把两者混成一句"陈述与数据不符"。
            row = dict(intent)
            row["statement_rejections"] = hits
            row["statement_unverified"] = [
                hit for hit in hits if hit.get("evidence_status") == "unverified"
            ]
            rejections.append(row)
        else:
            accepted.append(intent)
    if not (plan.get("intents") or []) and note_hits:
        rejections.append(
            {
                "symbol": "",
                "side": "hold",
                "statement_rejections": note_hits,
                "statement_unverified": [
                    hit for hit in note_hits if hit.get("evidence_status") == "unverified"
                ],
            }
        )
    return StatementGateResult(
        accepted_intents=accepted,
        rejections=rejections,
        market_note_ok=bool(note_fact_count) and not note_hits,
        market_note_status=("陈述与数据不符" if any(h.get("evidence_status") != "unverified" for h in note_hits)
                            else ("未核验" if note_hits or not note_fact_count else "已核对")),
        original_intents=deepcopy([row for row in (plan.get("intents") or []) if isinstance(row, dict)]),
        market_note_rejections=deepcopy(note_hits),
    )


STATEMENT_FLAG_UNVERIFIED = "含未核验说法"
STATEMENT_FLAG_MISMATCH = "含与数据不符说法"


def _flag_label(hits: list[dict[str, Any]]) -> str:
    """放行的卖出带着什么说法：未核验 / 与数据不符（两者都有就都写上）。"""

    labels = []
    if any(hit.get("evidence_status") == "unverified" for hit in hits):
        labels.append(STATEMENT_FLAG_UNVERIFIED)
    if any(hit.get("evidence_status") != "unverified" for hit in hits):
        labels.append(STATEMENT_FLAG_MISMATCH)
    return "、".join(labels)


def _verification_for_intent(intent: dict[str, Any], *, trade_date: str) -> dict[str, Any]:
    declared = declared_verification(intent.get("verification"))
    if declared is not None:
        window = trade_days_from(trade_date, declared["n_sessions"])
        relation = "站上或等于" if declared["kind"] == "stand_above" else "跌破"
        return {
            **declared,
            "source": "model_declared" if intent.get("decision_origin") == "model" else "declared_unconfirmed",
            "due_date": window[-1] if window else "",
            "text": f"{declared['n_sessions']}个交易日内原始收盘价{relation}{declared['level']}；到期核对完整区间",
        }
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
    # 当晚落盘的所见优先：后来的行情修正不得改写历史记录里的 seen（PRODUCT-S2-REJECTION-HISTORY）。
    recorded_seen = intent.get("seen_at_decision") if isinstance(intent.get("seen_at_decision"), dict) else None
    bar = None if recorded_seen is not None or not symbol else load_daily_bar(symbol, decision_date, daily_dir)
    llm = plan.get("llm") if isinstance(plan.get("llm"), dict) else {}
    stamp = pack.version_stamp()
    recorded_prompt = str(plan.get("prompt_version") or llm.get("prompt_version") or "").strip()
    recorded_menu = str(plan.get("menu_version") or llm.get("menu_version") or "").strip()
    for key in ("prompt_id", "menu_id"):
        recorded_id = str(plan.get(key) or llm.get(key) or "").strip()
        stamp[key] = recorded_id if backfill else recorded_id or stamp[key]
    if backfill:
        stamp["prompt_version"] = recorded_prompt
        stamp["menu_version"] = recorded_menu
    else:
        stamp["prompt_version"] = recorded_prompt or stamp["prompt_version"]
        stamp["menu_version"] = recorded_menu or stamp["menu_version"]
    mismatches = list(mismatches or [])
    # T11：整条记录只有"主体/时段定不下来"的命中时，主结论是"未核验"而不是"已证实不符"。
    unverified_only = bool(mismatches) and all(
        bool(hit.get("claim_indeterminate")) for hit in mismatches if isinstance(hit, dict)
    )
    if mismatches:
        result = RESULT_UNVERIFIED if unverified_only else RESULT_MISMATCH
        verification = {
            "kind": "rejected",
            "source": "statement_gate",
            "n_sessions": None,
            "level": None,
            "due_date": "",
            "text": RESULT_UNVERIFIED if unverified_only else RESULT_MISMATCH,
            "unverified_only": unverified_only,
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
    judgment = str(intent.get("reason") or "").strip() if side == "sell" else str(
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
    # 落盘时刻：优先用随意图/被拒项一起存下的当晚时刻，其次才是计划本身；
    # 两者都没有就留空（历史复盘不得被重建那一刻的时钟补造，PRODUCT-S2-REJECTION-HISTORY）。
    explicit_recorded = str(
        intent.get("recorded_at")
        or plan.get("recorded_at")
        or plan.get("created_at")
        or plan.get("decided_at")
        or ""
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
        "plan_id": str(plan.get("plan_id") or ""),
        "ledger_id": str(plan.get("ledger_id") or ""),
        "recorded_at": recorded_at,
        "recorded_at_source": "original" if explicit_recorded else "missing" if backfill else "capture",
        "structured_at": structured_at or _now_iso(),
        "backfill": bool(backfill),
        "provenance": "historical_review" if backfill else "prior_record",
        "decision_date": decision_date,
        "trade_date": trade_date,
        "symbol": symbol,
        "side": side,
        "judgment": judgment,
        "entry_thesis": deepcopy(intent.get("entry_thesis")) if side == "sell" else None,
        "style": str(intent.get("style") or "") or None,
        "planned_hold_days": int(intent.get("planned_hold_days") or 0) or _hold_days(intent) if symbol else None,
        "seen": _seen_field(intent, recorded=recorded_seen, bar=bar, decision_date=decision_date),
        "verification": verification,
        "result": result,
        "result_as_of": "",
        "result_evidence": mismatches,
        # 未核验（主体/事实无法绑定日线）随记录保留：既不算已核对，也不算确定矛盾（T11）。
        "statement_unverified": deepcopy(intent.get("statement_unverified") or []),
        # 照常放行的卖出带的说法标注（"含未核验说法"等）；没有就是空串。
        "statement_label": str(intent.get("statement_label") or ""),
        "statement_flags": deepcopy(intent.get("statement_flags") or []),
        "prompt_id": stamp["prompt_id"],
        "prompt_version": stamp["prompt_version"],
        "menu_id": stamp["menu_id"],
        "menu_version": stamp["menu_version"],
        **{key: str(plan.get(key) or llm.get(key) or "") for key in (
            "prompt_sha256", "menu_sha256", "effective_prompt_sha256", "prompt_selection",
            "prompt_approval_status", "prompt_builder_version")},
        "prompt_provenance": "recorded" if recorded_prompt and recorded_menu else "current_default_unconfirmed" if not backfill else "unknown",
        "llm_used": llm.get("used") if isinstance(llm.get("used"), bool) else None,
        "llm_model": str(llm.get("model") or ""),
        "decision_origin": str(intent.get("decision_origin") or ("rule" if llm.get("used") is False else "unknown")),
        "committed": bool(plan.get("committed", True)) and result not in {RESULT_MISMATCH, RESULT_UNVERIFIED},
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


def _atomic_write_bytes(path: Path, data: bytes) -> None:
    """Whole-file replacement: temp file in the same folder, fsync, then replace.

    A crash at any point leaves either the old file or the new file, never a
    half-written one.  Windows can briefly refuse the replace while a reader has
    the file open, so the replace is retried a few times before failing loudly.
    """

    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        for attempt in range(5):
            try:
                os.replace(temp_name, path)
                break
            except PermissionError:
                if attempt == 4:
                    raise
                time.sleep(0.05 * (attempt + 1))
    except BaseException:
        try:
            os.unlink(temp_name)
        except OSError:
            pass
        raise


def _quarantine_unreadable_lines(path: Path) -> int:
    """Copy lines that are not JSON objects to ``<name>.corrupt.jsonl`` before a rewrite.

    Reading skips such lines; without this step the next whole-file rewrite would
    erase them for good.  The quarantine file is append-only and fsynced.
    """

    if not path.is_file():
        return 0
    bad: list[bytes] = []
    for raw in path.read_bytes().splitlines():
        line = raw.strip()
        if not line:
            continue
        try:
            row = json.loads(line.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            bad.append(raw)
            continue
        if not isinstance(row, dict):
            bad.append(raw)
    if not bad:
        return 0
    quarantine = path.with_name(path.stem + ".corrupt.jsonl")
    stamp = _now_iso()
    with quarantine.open("ab") as handle:
        for raw in bad:
            handle.write(json.dumps({"quarantined_at": stamp, "source": path.name,
                                     "raw_line": raw.decode("utf-8", errors="replace")},
                                    ensure_ascii=False).encode("utf-8") + b"\n")
        handle.flush()
        os.fsync(handle.fileno())
    return len(bad)


def write_decision_records(path: Path, records: list[dict[str, Any]]) -> None:
    path = Path(path)
    _quarantine_unreadable_lines(path)
    payload = "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in records)
    _atomic_write_bytes(path, payload.encode("utf-8"))


def _judgment_fingerprint(row: dict[str, Any]) -> tuple[str, ...]:
    verification = row.get("verification") if isinstance(row.get("verification"), dict) else {}
    return (
        str(row.get("judgment") or ""),
        str(verification.get("kind") or ""),
        str(verification.get("n_sessions") or ""),
        str(verification.get("level") or ""),
        str(row.get("planned_hold_days") or ""),
        str(verification.get("source") or ""),
        str(verification.get("price_basis") or ""),
        str(verification.get("due_date") or ""),
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
            fingerprint = json.dumps(_judgment_fingerprint(incoming_row), ensure_ascii=False)
            incoming_row["decision_id"] = f"{key}_rev_{sha1(fingerprint.encode('utf-8')).hexdigest()[:12]}"
            # Rebuilding the same judgment must preserve its first recorded copy,
            # including older revisions whose ids were based on a run timestamp.
            duplicate = any(
                item.get("revises") == key and _judgment_fingerprint(item) == _judgment_fingerprint(incoming_row)
                for item in by_id.values()
            )
            if not duplicate:
                by_id[str(incoming_row["decision_id"])] = incoming_row
            continue
        if old and old.get("result") in {RESULT_MISMATCH, RESULT_UNVERIFIED}:
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
        if old:
            # New schema fields cannot retroactively certify an old call. Fill
            # evidence is joined separately from the read-only ledger below.
            for field_name in (
                "plan_id", "ledger_id", "recorded_at_source", "provenance", "backfill", "seen",
                "decision_origin", "llm_used", "llm_model", "prompt_id", "prompt_version",
                "menu_id", "menu_version", "prompt_sha256", "menu_sha256", "effective_prompt_sha256",
                "prompt_selection", "prompt_approval_status", "prompt_builder_version", "prompt_provenance",
            ):
                if field_name in old:
                    incoming_row[field_name] = deepcopy(old[field_name])
                else:
                    incoming_row.pop(field_name, None)
        by_id[key] = incoming_row
    records = list(by_id.values())
    records.sort(key=lambda item: (str(item.get("decision_date") or ""), str(item.get("decision_id") or "")))
    write_decision_records(path, records)
    return records


def _apply_verdict(
    record: dict[str, Any],
    verdict: str,
    as_of: str,
    evidence: list[dict[str, Any]],
    *,
    recheck_only: bool,
) -> dict[str, Any]:
    """把这次核对落到记录上。

    普通记录照旧写 ``result``/``result_as_of``/``result_evidence``；**到期缺证**的记录
    只另加 ``recheck``（补证后的关联评价），原判定与原有证据一个字节都不动（T21）。
    """

    if not recheck_only:
        record["result"] = verdict
        record["result_as_of"] = as_of
        record["result_evidence"] = evidence
        return record
    detail = dict(evidence[0]) if evidence else {}
    if verdict in {RESULT_RIGHT, RESULT_WRONG}:
        record["recheck"] = {"as_of": as_of, "result": verdict, "evidence_status": "verified", **detail}
    else:
        record["recheck"] = {
            "as_of": as_of,
            "result": None,
            "evidence_status": "unverified",
            "reason": "补证后仍不足以判定，未知不计成绩",
            **detail,
        }
    return record


def score_decision(record: dict[str, Any], *, daily_dir: Path, as_of: str) -> dict[str, Any]:
    updated = dict(record)
    result = str(updated.get("result") or "")
    if result in {RESULT_MISMATCH, RESULT_UNVERIFIED, RESULT_RIGHT, RESULT_WRONG, RESULT_NA}:
        return updated
    # T21：到期缺证不是终局。补证后允许**另加**一次评价（``recheck``），但原判定、原预测、
    # 原编号、原证据一律不改；仍缺证就标 unverified —— 未知既不算成功也不算失败。
    recheck_only = result == RESULT_EXPIRED_NO_EVIDENCE
    verification = dict(updated.get("verification") or {})
    symbol = str(updated.get("symbol") or "")
    n_days = int(verification.get("n_sessions") or 0)
    level = _f(verification.get("level"))
    start = _as_day(updated.get("trade_date") or updated.get("decision_date"))
    if not symbol or n_days <= 0 or level is None or not start:
        if not recheck_only:
            updated["result"] = RESULT_NA
        return updated
    window = trade_days_from(start, n_days)
    due = str(verification.get("due_date") or (window[-1] if window else ""))
    rows = iter_daily_rows(symbol, daily_dir)
    blocked = _basis_untrusted(rows[0]) if rows else ""
    if blocked:
        return _apply_verdict(
            updated,
            RESULT_EXPIRED_NO_EVIDENCE if due and as_of >= due else RESULT_PENDING,
            as_of,
            [{
                "kind": "price_basis_untrusted",
                "evidence_status": "unverified",
                "symbol": symbol,
                "price_basis": rows[0].get("price_basis"),
                "reason_code": blocked,
                "consumer": _DAILY_PRICE_CONSUMER,
                "due_date": due,
            }],
            recheck_only=recheck_only,
        )
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
    due_passed = bool(due) and as_of >= due
    if hit and not due_passed:
        return _apply_verdict(
            updated, RESULT_PENDING, as_of,
            [{"kind": "early_hit", "level": level, "observed": observed,
              "have": len(observed), "need": n_days, "due_date": due,
              "note": "提前命中只记观察证据，到期前不判对"}],
            recheck_only=recheck_only,
        )
    if len(observed) < n_days:
        if due_passed:
            return _apply_verdict(
                updated, RESULT_EXPIRED_NO_EVIDENCE, as_of,
                [{"kind": "expired_missing_bars", "have": len(observed), "need": n_days, "due_date": due}],
                recheck_only=recheck_only,
            )
        return _apply_verdict(
            updated, RESULT_PENDING, as_of,
            [{"kind": "window_open", "have": len(observed), "need": n_days}],
            recheck_only=recheck_only,
        )
    return _apply_verdict(
        updated, RESULT_RIGHT if hit else RESULT_WRONG, as_of,
        [{"kind": "window_hit" if hit else "window_miss", "level": level, "observed": observed,
          "due_date": due, "price_basis": "raw"}],
        recheck_only=recheck_only,
    )


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


def _review_date(value: Any) -> date | None:
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def decision_review_summary(records: list[dict[str, Any]], *, as_of: Any) -> dict[str, Any]:
    """Keep historical verdicts visible; count only evidenced, mature model calls."""
    cutoff = _review_date(as_of)
    labels = {"model": "模型判断", "rule": "固定规则", "unknown": "来源未知"}
    groups: dict[str, dict[str, Any]] = {}
    for origin, label in labels.items():
        groups[origin] = {
            "label": label, "records": 0, "mature_evaluable": 0 if cutoff else None,
            "mature_correct": 0 if cutoff else None, "mature_wrong": 0 if cutoff else None,
            "early_hits": 0 if cutoff else None, "pending": 0 if cutoff else None,
            "due_evidence_gap": 0, "maturity_unknown": 0, "unaccepted": 0,
            "not_applicable": 0, "statement_mismatch": 0, "statement_unverified": 0, "accuracy": None,
            "excluded": 0, "diagnostic_mature": 0, "eligible_before_maturity": 0,
            # T21：补证后的重评单独计数（原观察期样本不受影响，分母真实）。
            "rechecked": 0, "rechecked_correct": 0, "rechecked_wrong": 0, "recheck_gap": 0,
        }
    contexts: dict[str, dict[str, Any]] = {}
    historical = 0
    exclusion_counts: dict[str, int] = {}
    for row in records:
        origin = decision_origin(row)
        group = groups[origin]
        contract = eligibility(row)
        exclusion_reasons = list(contract["exclusion_reasons"])
        in_denominator = False
        verification = row.get("verification") if isinstance(row.get("verification"), dict) else {}
        due = _review_date(verification.get("due_date"))
        result_at = _review_date(row.get("result_as_of"))
        result = row.get("result")
        phase, note = "maturity_unknown", "观察截止日期或核对日期不明"
        if _is_backfill(row):
            historical += 1
            phase, note = "historical", "事后补写，单独展示"
        else:
            group["records"] += 1
            if row.get("committed") is False:
                phase, note = "unaccepted", "判断未被接收，不计成绩"
                group[phase] += 1
            elif result == RESULT_MISMATCH:
                phase, note = "statement_mismatch", "陈述与数据不符，不计预测成绩"
                group[phase] += 1
            elif result == RESULT_UNVERIFIED:
                phase, note = "statement_unverified", "陈述未核验，不计预测成绩"
                group[phase] += 1
            elif not row.get("symbol") or result == RESULT_NA or verification.get("kind") == "none":
                phase, note = "not_applicable", "本条没有可评价的个股条件"
                group[phase] += 1
            elif cutoff is None or due is None or (result_at and result_at > cutoff):
                group["maturity_unknown"] += 1
            elif due > cutoff:
                early = any(isinstance(item, dict) and item.get("kind") == "early_hit"
                            for item in row.get("result_evidence") or [])
                if (result == RESULT_RIGHT and result_at is not None) or early:
                    phase, note = "early_hit", "提前命中，观察期尚未结束"
                    group["early_hits"] += 1
                else:
                    phase, note = "pending", "仍在观察期内"
                    group[phase] += 1
            elif result in (RESULT_RIGHT, RESULT_WRONG) and result_at is not None:
                group["diagnostic_mature"] += 1
                if result_at < due:
                    exclusion_reasons.append("verdict_before_maturity")
                if not complete_outcome_evidence(row):
                    exclusion_reasons.append("outcome_evidence_incomplete")
                if not exclusion_reasons:
                    phase, note = "mature_evaluable", "模型事前声明、实际纸面成交及到期完整证据已确认"
                    in_denominator = True
                    group[phase] += 1
                    group["mature_correct" if result == RESULT_RIGHT else "mature_wrong"] += 1
                else:
                    phase, note = "diagnostic_only", "保留原判定；不计可信模型成绩"
            else:
                phase, note = "due_evidence_gap", "观察期已结束，结果或证据尚未齐全"
                group[phase] += 1
                # T21：补证后的重评另算一档——成功/失败进重评分母，仍缺证只记 gap。
                recheck = row.get("recheck") if isinstance(row.get("recheck"), dict) else None
                if recheck:
                    verdict = recheck.get("result")
                    if verdict in (RESULT_RIGHT, RESULT_WRONG):
                        group["rechecked"] += 1
                        group["rechecked_correct" if verdict == RESULT_RIGHT else "rechecked_wrong"] += 1
                        phase, note = "rechecked", "补证后重评；原判定与原编号不变"
                    elif verdict is None:
                        group["recheck_gap"] += 1
            if contract["eligible"]:
                group["eligible_before_maturity"] += 1
            if exclusion_reasons:
                group["excluded"] += 1
        for reason in set(exclusion_reasons):
            exclusion_counts[reason] = exclusion_counts.get(reason, 0) + 1
        decision_id = row.get("decision_id")
        if isinstance(decision_id, str) and decision_id:
            contexts[decision_id] = {
                "origin": origin, "origin_label": labels[origin], "observation_status": phase,
                "label": note, "due_date": due.isoformat() if due else None,
                "model": str(row.get("llm_model") or ""),
                "in_accuracy_denominator": in_denominator,
                "exclusion_reasons": exclusion_reasons,
                "exclusion_labels": [EXCLUSION_LABELS[reason] for reason in exclusion_reasons],
                "fill_status": (row.get("fill_evidence") or {}).get("status", "unconfirmed"),
            }
    for group in groups.values():
        if group["mature_evaluable"]:
            group["accuracy"] = group["mature_correct"] / group["mature_evaluable"]
    denominator_note = (
        "可信准确率分母仅含：模型逐项判断、实际模型名称、带时区的事前记录、模型明确声明的"
        "原始价格验证条件、按计划编号核实的实际纸面成交，以及观察期结束后的完整日线证据。"
        "规则判断、来源未知、程序推导条件、提前命中、缺证与历史补写保留展示，均不计入。"
    )
    return {
        "as_of": cutoff.isoformat() if cutoff else None, "maturity_known": cutoff is not None,
        "historical_records": historical, "groups": groups, "record_context": contexts,
        "exclusion_counts": exclusion_counts,
        "credible_model": {"denominator": groups["model"]["mature_evaluable"],
                           "correct": groups["model"]["mature_correct"], "accuracy": groups["model"]["accuracy"]},
        "denominator_note": denominator_note,
        "note": "提前命中只记观察；旧记录原判定保持不变。" + denominator_note,
    }


def _result_names() -> tuple[str, ...]:
    return (
        RESULT_WRONG,
        RESULT_MISMATCH,
        RESULT_UNVERIFIED,
        RESULT_EXPIRED_NO_EVIDENCE,
        RESULT_PENDING,
        RESULT_RIGHT,
        RESULT_NA,
    )


def render_owner_page(
    records: list[dict[str, Any]],
    *,
    html_path: Path,
    md_path: Path,
    intents: list[dict[str, Any]] | None = None,
    as_of: str | None = None,
) -> None:
    ordered = _sort_for_page(records)
    live = [row for row in ordered if not _is_backfill(row)]
    review = [row for row in ordered if _is_backfill(row)]
    live_counts = _count_results(live)
    review_counts = _count_results(review)
    names = _result_names()
    review_summary = decision_review_summary(records, as_of=as_of)
    credible = review_summary["credible_model"]
    accuracy = credible["accuracy"]
    accuracy_text = "证据不足" if accuracy is None else f"{accuracy:.1%}"
    credible_text = (f"可信模型到期样本 {credible['denominator'] if credible['denominator'] is not None else '未知'}"
                     f" · 正确 {credible['correct'] if credible['correct'] is not None else '未知'} · 准确率 {accuracy_text}")
    lines = [
        f"# C 线决策对照 · {DISCLAIMER}",
        "",
        "未存模型验证条件的旧记录：" + VERIFICATION_DERIVED_NOTE + "。价格只对照 `data/daily/`。这不是订单。",
        "历史复盘没有事前落盘时间，不与当晚预判混算对/错。",
        review_summary["denominator_note"],
        credible_text,
        "下列原始结果计数只用于核对历史，不代表模型准确率。",
        "",
        "事前记录："
        + " · ".join(f"{name} {live_counts.get(name, 0)}" for name in names),
        "历史复盘："
        + " · ".join(f"{name} {review_counts.get(name, 0)}" for name in names),
        "",
        "| 结果 | 来源 | 决策日 | 代码 | 方向 | 风格/天数 | 判断 | 验证条件 | 版本 | 落盘时刻 | 评估资格 |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    html_rows: list[str] = []
    for row in ordered:
        result = str(row.get("result") or "")
        style = str(row.get("style") or "未标")
        hold = row.get("planned_hold_days") or ""
        ver = str((row.get("verification") or {}).get("text") or "")
        version = f"{row.get('prompt_version') or '未知'}/{row.get('menu_version') or '未知'}"
        context = review_summary["record_context"].get(str(row.get("decision_id") or ""), {})
        origin = ("历史复盘" if _is_backfill(row) else "事前") + " / " + str(context.get("origin_label") or "来源未知")
        evaluation_note = "计入可信分母" if context.get("in_accuracy_denominator") else "；".join(
            context.get("exclusion_labels") or [context.get("label") or "证据不足"])
        recorded = str(row.get("recorded_at") or "未知")
        judgment = str(row.get("judgment") or "").replace("|", "\\|").replace("\n", " ")
        if len(judgment) > 80:
            judgment = judgment[:77] + "…"
        lines.append(
            f"| {result} | {origin} | {row.get('decision_date')} | {row.get('symbol') or '—'} | "
            f"{row.get('side')} | {style}/{hold} | {judgment} | {ver} | {version} | {recorded} | {evaluation_note} |"
        )
        color = {
            RESULT_WRONG: "#8b1e1e",
            RESULT_MISMATCH: "#8b4513",
            RESULT_UNVERIFIED: "#5c4b1d",
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
            f"<td>{html.escape(evaluation_note)}</td>"
            "</tr>"
        )
    intent_rows = list(intents or [])
    if intent_rows:
        lines += [
            "",
            f"## 白天盘中意图（未成交，仅记录）· {len(intent_rows)} 条",
            "",
            "对照的是 AI 自己当晚计划的点位；价格来自 `data/snapshots/` 的 5 分钟槽与实时快照。"
            "**没有成交，未改账本。**",
            "",
            "| 时刻 | 代码 | 方向 | 触发条件 | 计划点位 | 现价 | 当日低/高 | 备注 |",
            "| --- | --- | --- | --- | --- | --- | --- | --- |",
        ]
        for item in intent_rows:
            low = item.get("observed_low")
            high = item.get("observed_high")
            band = f"{low if low is not None else '-'} / {high if high is not None else '-'}"
            note = str(item.get("note") or "").replace("|", "\\|").replace("\n", " ")
            lines.append(
                f"| {item.get('at') or ''} | {item.get('symbol') or ''} | {item.get('side') or ''} | "
                f"{item.get('precondition') or ''} | "
                f"{item.get('level') if item.get('level') is not None else '-'} | "
                f"{item.get('observed_price') if item.get('observed_price') is not None else '-'} | "
                f"{band} | {note} |"
            )
    _atomic_write_bytes(Path(md_path), ("\n".join(lines) + "\n").encode("utf-8"))
    html_doc = (
        "<!doctype html><html lang='zh-CN'><head><meta charset='utf-8'>"
        f"<title>C线决策对照 · {html.escape(DISCLAIMER)}</title>"
        "<style>body{font-family:sans-serif;margin:24px;background:#111;color:#eee}"
        "table{border-collapse:collapse;width:100%}td,th{border:1px solid #444;padding:6px 8px;font-size:14px;vertical-align:top}"
        "th{text-align:left;background:#222}h1{font-size:20px}</style></head><body>"
        f"<h1>C 线决策对照 · {html.escape(DISCLAIMER)}</h1>"
        f"<p>未存模型验证条件的旧记录：<strong>{html.escape(VERIFICATION_DERIVED_NOTE)}</strong></p>"
        f"<p>{html.escape(review_summary['denominator_note'])}</p>"
        f"<p><strong>{html.escape(credible_text)}</strong></p>"
        "<p>错的排前面。它会对照 <code>data/daily/</code> 核对当晚说的价格和涨跌。"
        "对不上的决策会被拒收，不会当成有效判断。这不是订单。"
        "历史复盘没有事前落盘时间，不对入当晚对/错统计。</p>"
        "<p>下列原始结果计数用于核对历史，不代表模型准确率。</p><p>事前记录："
        + " · ".join(f"{html.escape(name)} {live_counts.get(name, 0)}" for name in names)
        + "</p><p>历史复盘："
        + " · ".join(f"{html.escape(name)} {review_counts.get(name, 0)}" for name in names)
        + "</p>"
        "<table><thead><tr>"
        "<th>结果</th><th>来源</th><th>决策日</th><th>代码</th><th>方向</th><th>风格/天数</th>"
        "<th>判断</th><th>验证条件</th><th>提示词/菜单</th><th>落盘时刻</th><th>评估资格</th>"
        "</tr></thead><tbody>"
        + "".join(html_rows)
        + "</tbody></table>"
        + (
            "<h2>白天盘中意图（未成交，仅记录）· "
            + str(len(intent_rows))
            + " 条</h2><p>对照的是 AI 自己当晚计划的点位；价格来自 "
            "<code>data/snapshots/</code> 的 5 分钟槽与实时快照。<strong>没有成交，未改账本。</strong></p>"
            "<table><thead><tr><th>时刻</th><th>代码</th><th>方向</th><th>触发条件</th>"
            "<th>计划点位</th><th>现价</th><th>当日低/高</th><th>备注</th></tr></thead><tbody>"
            + "".join(
                "<tr>"
                f"<td>{html.escape(str(item.get('at') or ''))}</td>"
                f"<td>{html.escape(str(item.get('symbol') or ''))}</td>"
                f"<td>{html.escape(str(item.get('side') or ''))}</td>"
                f"<td>{html.escape(str(item.get('precondition') or ''))}</td>"
                f"<td>{html.escape(str(item.get('level') if item.get('level') is not None else '-'))}</td>"
                f"<td>{html.escape(str(item.get('observed_price') if item.get('observed_price') is not None else '-'))}</td>"
                f"<td>{html.escape(str(item.get('observed_low') if item.get('observed_low') is not None else '-'))}"
                f" / {html.escape(str(item.get('observed_high') if item.get('observed_high') is not None else '-'))}</td>"
                f"<td>{html.escape(str(item.get('note') or ''))}</td>"
                "</tr>"
                for item in intent_rows
            )
            + "</tbody></table>"
            if intent_rows
            else ""
        )
        + "</body></html>"
    )
    _atomic_write_bytes(Path(html_path), html_doc.encode("utf-8"))


def records_from_plan(
    plan: dict[str, Any],
    *,
    daily_dir: Path,
    rejections: list[dict[str, Any]] | None = None,
    backfill: bool = False,
    name_to_symbol: dict[str, str] | None = None,
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
    # 当晚被拒的原提议**始终**并入：调用方给的 rejections 只是这一轮按当前日线重算
    # 的结果，不能因为它在场就把计划/日记里存着的当晚原拒绝丢掉
    # （R10-0928-REJECTION-MERGE）。同一个 (symbol, side) 只算一次，先出现的当晚原件
    # 优先，既不会丢历史，也不会把同一拒绝算两遍。
    merged_rejections: list[dict[str, Any]] = []
    merged_keys: set[tuple[str, str]] = set()
    for intent in [*_plan_rejections(plan), *(rejections or [])]:
        if not isinstance(intent, dict):
            continue
        merged_key = (str(intent.get("symbol") or ""), str(intent.get("side") or ""))
        if merged_key in merged_keys:
            continue
        merged_keys.add(merged_key)
        merged_rejections.append(intent)
    for intent in merged_rejections:
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
        mismatches = [] if gate_plan_statements(plan, daily_dir=daily_dir, name_to_symbol=name_to_symbol).market_note_ok else [
            item
            for row in (rejections or [])
            for item in (row.get("statement_rejections") or [])
        ]
        if not mismatches:
            mismatches = verify_market_note(
                str(((plan.get("llm") or {}) if isinstance(plan.get("llm"), dict) else {}).get("market_note") or ""),
                decision_date=_as_day(plan.get("decision_date")),
                daily_dir=daily_dir,
                name_to_symbol=name_to_symbol,
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
    rejected = tomorrow.get("statement_rejections") or row.get("statement_rejections") or []
    return {
        "plan_id": str(tomorrow.get("plan_id") or row.get("plan_id") or ""),
        "ledger_id": str(tomorrow.get("ledger_id") or row.get("ledger_id") or ""),
        "decision_date": _as_day(row.get("as_of")),
        "trade_date": _as_day(tomorrow.get("trade_date") or row.get("trade_date")),
        "intents": list(tomorrow.get("intents") or []),
        # 当晚被拒的原提议随日记一起回放：最新计划隔天就被覆盖，只剩日记时也要还原。
        "statement_rejections": [dict(item) for item in rejected if isinstance(item, dict)],
        # 当晚落盘时刻随日记保存；没有就留空（标缺失，不用读日记那一刻的时钟补造）。
        "recorded_at": str(
            tomorrow.get("recorded_at") or row.get("recorded_at") or row.get("generated_at") or ""
        ).strip(),
        "llm": llm,
        "committed": True,
        "prompt_version": row.get("prompt_version") or llm.get("prompt_version"),
        "menu_version": row.get("menu_version") or llm.get("menu_version"),
        **{key: row.get(key) or llm.get(key) for key in (
            "prompt_id", "menu_id", "prompt_sha256", "menu_sha256", "effective_prompt_sha256",
            "prompt_selection", "prompt_approval_status", "prompt_builder_version")},
    }


def _plan_rejections(plan: dict[str, Any]) -> list[dict[str, Any]]:
    """当晚被拒的原提议（``apply_gate_to_plan`` 存下的 ``statement_rejections`` 形状）。

    存的是 ``hits``，回放时还原成 ``statement_rejections`` 走原来的落库路径；只搬键，
    不造 ``reason``/``judgment`` 等会改变 identity 的字段。
    """

    out: list[dict[str, Any]] = []
    for row in plan.get("statement_rejections") or []:
        if not isinstance(row, dict):
            continue
        # 以原提议整体为基底回放（reason/style/planned_hold_days/ref_close 都要在），
        # 这样重建出的记录与当晚落库时字段一致、identity 与 decision_id 不变；
        # 只把命中明细按原样带上。
        base = row.get("original_intent") if isinstance(row.get("original_intent"), dict) else {}
        item = dict(base)
        item["symbol"] = row.get("symbol", item.get("symbol"))
        item["side"] = row.get("side", item.get("side"))
        item["statement_rejections"] = list(row.get("statement_rejections") or row.get("hits") or [])
        # 当晚落盘的所见与落盘时刻随原提议一起回放（旧记录没有这些键，就按缺失处理）。
        seen = row.get("seen_at_decision")
        if isinstance(seen, dict):
            item["seen_at_decision"] = dict(seen)
        recorded = str(row.get("recorded_at") or "").strip()
        if recorded and not str(item.get("recorded_at") or "").strip():
            item["recorded_at"] = recorded
        out.append(item)
    return out


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
    intents: list[dict[str, Any]] | None = None,
    ledger_path: Path | None = None,
) -> dict[str, Any]:
    scored = score_decision_log(records, daily_dir=daily_dir, as_of=as_of)
    if ledger_path is not None:
        from ai_trader.ledger import LedgerMissingError, load_ledger

        try:
            book = load_ledger(ledger_path, missing_ok=False)
            ledger_status = "ok"
        except LedgerMissingError:
            book, ledger_status = None, "missing"
        except (OSError, ValueError, RuntimeError):
            book, ledger_status = None, "unreadable"
        scored = with_fill_evidence(scored, book, ledger_status=ledger_status)
    review_summary = decision_review_summary(scored, as_of=as_of)
    write_decision_records(paths["log"], scored)
    latest = {
        "schema": SCHEMA,
        "disclaimer": DISCLAIMER,
        "as_of": as_of,
        "count": len(scored),
        "records": scored,
        "review_summary": review_summary,
    }
    atomic_write_json(paths["latest"], latest)
    render_owner_page(scored, html_path=paths["html"], md_path=paths["md"], intents=intents, as_of=as_of)
    manifest = git_run_manifest()
    manifest.update(
        {
            "as_of": as_of,
            "daily_dir": str(daily_dir),
            "record_count": len(scored),
            "review_summary": review_summary,
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
    from ai_trader.daytime_intent import load_daytime_intents_for

    intents = load_daytime_intents_for(as_of, intents_path=runtime.daytime_intents)
    return refresh_owner_outputs(merged, paths=paths, daily_dir=daily_dir, as_of=as_of, intents=intents,
                                 ledger_path=runtime.ledger)


def _attach_post_night_recheck(
    rows: list[dict[str, Any]],
    rejections: list[dict[str, Any]],
    *,
    as_of: str,
) -> list[dict[str, Any]]:
    """把"事后按当前日线复核"的结果挂在记录上，不改当晚裁定。

    记录保留当晚的 judgment / committed / identity / decision_id，只加一个独立状态键
    ``post_night_recheck``（``as_of`` ＝ 被复核的那个计划日）：现在的日线已经和当时那句
    话对不上，这件事要看得见；但反编造检查是"当时的陈述对当时的数据"，不拿后来的行情
    回写历史（R10-0928-HISTORY-REGATE）。该键不参与 judgment/identity/decision_id。
    """

    hits_by_symbol: dict[str, list[Any]] = {}
    for row in rejections or []:
        if not isinstance(row, dict):
            continue
        hits = list(row.get("statement_rejections") or [])
        symbol = str(row.get("symbol") or "")
        if symbol and hits:
            hits_by_symbol.setdefault(symbol, []).extend(hits)
    for row in rows:
        hits = hits_by_symbol.get(str(row.get("symbol") or ""))
        if not hits or not isinstance(row, dict):
            continue
        row["post_night_recheck"] = {
            "as_of": as_of,
            "verdict": RESULT_MISMATCH,
            "note": "事后按当前日线复核与当晚陈述不符；当晚裁定与编号保持不变",
            "hits": hits,
        }
    return rows


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
    name_map = load_name_to_symbol(daily_dir=daily_dir)
    for plan in plans:
        if not plan.get("decision_date"):
            continue
        gated = gate_plan_statements(plan, daily_dir=daily_dir, name_to_symbol=name_map)
        all_rejections.extend(gated.rejections)
        # 重建历史以**当晚已存下的裁定**为准：计划/日记里的 intents 就是当晚通过的、
        # statement_rejections 就是当晚被拒的。事后修正过的日线不得回写当晚的判断与
        # identity/decision_id（R10-0928-HISTORY-REGATE）。
        stored_intents = [dict(item) for item in (plan.get("intents") or []) if isinstance(item, dict)]
        if not stored_intents:
            # 旧计划没存 intents（或当晚全被拒）时退回原重算口径，不改既有行为。
            stored_intents = list(gated.accepted_intents)
        accepted_symbols = {str(item.get("symbol") or "") for item in stored_intents}
        # 当晚已通过的提议事后与日线不符：只记复核状态，不另生成一条"新拒绝"顶掉原判。
        recheck = [row for row in gated.rejections if str(row.get("symbol") or "") in accepted_symbols]
        extra_rejections = [row for row in gated.rejections
                            if str(row.get("symbol") or "") not in accepted_symbols]
        incoming.extend(
            _attach_post_night_recheck(
                records_from_plan(
                    {**plan, "intents": stored_intents},
                    daily_dir=daily_dir,
                    rejections=extra_rejections,
                    backfill=True,
                    name_to_symbol=name_map,
                ),
                recheck,
                as_of=_as_day(plan.get("decision_date")),
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
        ledger_path=state / "ledger.json",
    )
    payload["rejection_count"] = len(all_rejections)
    return payload


def apply_gate_to_plan(plan: dict[str, Any], *, daily_dir: Path,
                       name_to_symbol: dict[str, str] | None = None) -> StatementGateResult:
    gated = gate_plan_statements(plan, daily_dir=daily_dir, name_to_symbol=name_to_symbol)
    plan["statement_review"] = statement_gate_summary(gated)
    plan["intents"] = gated.accepted_intents
    decision_date = _as_day(plan.get("decision_date") or plan.get("as_of"))
    # 落盘时刻在决策当晚取一次：写进计划、每个通过项与被拒项，日记也会带上。
    # 重建历史时只读这份；旧记录没有这个键就只能留空，绝不拿重建那一刻的时钟补造。
    night_recorded = str(
        plan.get("recorded_at") or plan.get("created_at") or plan.get("decided_at") or ""
    ).strip() or _now_iso()
    plan["recorded_at"] = night_recorded
    for intent in plan["intents"]:
        if not isinstance(intent, dict):
            continue
        # 通过的提议也要存当晚所见（PRODUCT-S2-REJECTION-HISTORY）：已经存过的不覆盖，
        # 免得同一晚的补跑把当时所见换成后来修正过的日线。
        if not isinstance(intent.get("seen_at_decision"), dict):
            intent["seen_at_decision"] = _seen_snapshot(str(intent.get("symbol") or ""), decision_date, daily_dir)
        if not str(intent.get("recorded_at") or "").strip():
            intent["recorded_at"] = night_recorded
    if gated.rejections:
        previous = {
            (str(row.get("symbol") or ""), str(row.get("side") or "")): row.get("seen_at_decision")
            for row in (plan.get("statement_rejections") or [])
            if isinstance(row, dict) and isinstance(row.get("seen_at_decision"), dict)
        }
        saved: list[dict[str, Any]] = []
        for row in gated.rejections:
            # 当晚真正看到的行情：计划/日记落盘用一份，同时挂在被拒提议本身上，
            # 让“立即记录”那条路径（records_from_plan(rejections=...)）用同一份快照，
            # 同一晚的拒绝不会因为走哪条路而换一个 seen 来源，也不会被事后修正的日线改写。
            key = (str(row.get("symbol") or ""), str(row.get("side") or ""))
            seen = previous.get(key) or _seen_snapshot(key[0], decision_date, daily_dir)
            original = deepcopy(row)
            original.pop("seen_at_decision", None)
            original.pop("recorded_at", None)
            row["seen_at_decision"] = dict(seen)
            row["recorded_at"] = night_recorded
            saved.append(
                {
                    "symbol": row.get("symbol"),
                    "side": row.get("side"),
                    "label": RESULT_UNVERIFIED if (row.get("statement_rejections") or []) and all(
                        hit.get("claim_indeterminate") for hit in (row.get("statement_rejections") or [])
                    ) else RESULT_MISMATCH,
                    "hits": row.get("statement_rejections"),
                    # 原提议另存于**新键**：重建历史时按原样还原理由与当时所见。
                    # 新键不参与 judgment/identity/decision_id（r6 的回归就是让原理由
                    # 走了 reason 键、把已有拒绝的 decision_id 改掉）。
                    "original_reason": (row.get("llm_reason") or row.get("thesis")
                                        or row.get("reason") or ""),
                    "original_intent": original,
                    "seen_at_decision": dict(seen),
                    # 被拒项自带当晚落盘时刻，重建时不必依赖日志行里的时间。
                    "recorded_at": night_recorded,
                }
            )
        plan["statement_rejections"] = saved
    return gated


__all__ = [
    "DISCLAIMER",
    "VERIFICATION_DERIVED_NOTE",
    "RESULT_EXPIRED_NO_EVIDENCE",
    "RESULT_MISMATCH",
    "RESULT_UNVERIFIED",
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
