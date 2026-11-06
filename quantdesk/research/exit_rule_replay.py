"""Local comparisons of recorded picks; never writes either paper ledger.

Completed horizons, current holdings, price returns and fees are kept separate.
Daily cash/equity is a 100-share-per-entry research pool, not the strategy book.
All prices and calendars come from existing files. No network or strategy changes.
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import math
import os
from bisect import bisect_right
from dataclasses import dataclass, field, replace
from datetime import date
from pathlib import Path
from statistics import median
from typing import Any, Callable, Iterable, Mapping
from uuid import uuid4

import pandas as pd

from quantdesk.board_rules import limit_fill_check, limit_ratio
from quantdesk.interprocess_lock import interprocess_file_lock
from quantdesk.paper.trade_costs import TradeCostConfig, compute_trade_costs
from quantdesk.price_basis import classify_frame_price_basis, validate_price_frame_set

COMMISSION_RATE = 0.00025
MIN_COMMISSION = 5.0
STAMP_TAX_RATE = 0.0005
TRANSFER_FEE_RATE = 0.00001
LOT = 100
STOP_PCT = -8.0
TAKE_PROFIT_PCT = 15.0
OUT_OF_SAMPLE_FROM = "2026-11-03"
METHOD_VERSION = "exit-replay-v2-20261103"
RESEARCH_COSTS = TradeCostConfig.cn_current(slippage_bps=0.0)
PROTOCOL = {
    "version": METHOD_VERSION, "out_of_sample_from": OUT_OF_SAMPLE_FROM,
    "horizons": [5, 10, 20], "entry_ma20_groups_pct": [5.0, 10.0],
    "stop_pct": STOP_PCT, "take_profit_pct": TAKE_PROFIT_PCT,
    "lot": LOT, "cost_profile": "cn_current", "slippage_bps": 0.0,
    "endpoint": "completed_market_sessions_only",
    "exit_policy": "close_trigger_then_first_sellable_close",
    "cash_check": "all_buys_before_same_day_sells_conservative",
    "corporate_actions": "raw_price_only_not_fully_verified",
    "sample_unit": "recorded_first_entry_not_independent_stock",
    "hypothesis": "entry_far_above_ma20_may_underperform",
    "causal_or_proven_edge_claim": False,
}
PROTOCOL_SHA256 = hashlib.sha256(json.dumps(PROTOCOL, sort_keys=True).encode()).hexdigest()


class EvidenceError(ValueError):
    """An explicit data reason, never an empty file impersonating valid input."""

    def __init__(self, reason: str, path: Path | str = "") -> None:
        self.reason, self.path = reason, str(path)
        super().__init__(f"{reason}:{path}")


class InputAudit:
    def __init__(self) -> None:
        self.files: dict[str, dict[str, Any]] = {}

    def read(self, path: Path) -> str:
        try:
            before = path.stat()
            raw = path.read_bytes()
            after = path.stat()
        except OSError as exc:
            raise EvidenceError("input_unreadable", path) from exc
        if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
            raise EvidenceError("input_changed_during_read", path)
        digest = hashlib.sha256(raw).hexdigest()
        old = self.files.get(str(path))
        if old and old["sha256"] != digest:
            raise EvidenceError("input_changed_during_read", path)
        self.files[str(path)] = {"sha256": digest, "bytes": len(raw)}
        try:
            return raw.decode("utf-8-sig")
        except UnicodeError as exc:
            raise EvidenceError("input_encoding_invalid", path) from exc

    def verify(self) -> None:
        for name, record in self.files.items():
            try:
                current = hashlib.sha256(Path(name).read_bytes()).hexdigest()
            except OSError as exc:
                raise EvidenceError("input_changed_during_read", name) from exc
            if current != record["sha256"]:
                raise EvidenceError("input_changed_during_read", name)


@dataclass(frozen=True)
class Bar:
    day: str
    open: float | None
    close: float
    change: float | None
    high: float | None = None
    low: float | None = None
    volume: float | None = None
    pct_chg: float | None = None
    pre_close: float | None = None


@dataclass
class Trade:
    symbol: str
    name: str
    entry_date: str
    entry_price: float | None
    recorded_exit_date: str = ""
    recorded_exit_price: float | None = None
    recorded_open: bool = False
    trade_id: str = ""
    entry_price_role: str = "recorded_execution_price"
    session_days: tuple[str, ...] = ()


@dataclass
class Outcome:
    exit_date: str
    exit_price: float
    holding_days: int
    still_open: bool
    reason: str = ""
    planned_exit_date: str = ""
    blocked_exit_days: list[dict[str, str]] = field(default_factory=list)


@dataclass
class VariantResult:
    key: str
    label: str
    rows: list[dict[str, Any]] = field(default_factory=list)


def _number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _finite(value: Any) -> float | None:
    number = _number(value)
    return number if number is not None and number > 0 else None


def parse_day(text: str) -> str:
    return date.fromisoformat(str(text)[:10]).isoformat()


def _parse_bars(text: str, path: Path) -> list[Bar]:
    bars: list[Bar] = []
    seen: set[str] = set()
    for row in csv.DictReader(io.StringIO(text)):
        try:
            day = parse_day(str(row.get("date") or ""))
        except ValueError as exc:
            raise EvidenceError("invalid_bar_date", path) from exc
        if day in seen:
            raise EvidenceError("duplicate_daily_date", path)
        seen.add(day)
        close = _number(row.get("close"))
        if close is None:
            raise EvidenceError("invalid_close", path)
        bars.append(Bar(day, _finite(row.get("open")), close, _number(row.get("change")),
                        _finite(row.get("high")), _finite(row.get("low")), _number(row.get("volume")),
                        _number(row.get("pct_chg")), _finite(row.get("pre_close"))))
    return sorted(bars, key=lambda bar: bar.day)


def load_bars(path: Path, *, audit: InputAudit | None = None) -> list[Bar]:
    """Classify the complete original stock frame BEFORE parsing/filtering/sorting."""
    text = (audit or InputAudit()).read(Path(path))
    try:
        frame = pd.read_csv(io.StringIO(text), dtype=str)
    except (ValueError, pd.errors.ParserError) as exc:
        raise EvidenceError("daily_csv_invalid", path) from exc
    _basis, reason = classify_frame_price_basis(frame)
    validation = validate_price_frame_set({Path(path).stem: frame}, consumer="trade_replay")
    if not validation["eligible"]:
        raise EvidenceError(reason or validation["reason_codes"][0], path)
    return _parse_bars(text, path)


def load_index_bars(path: Path, *, audit: InputAudit | None = None) -> list[Bar]:
    """Read index POINTS for benchmarking; these are not stock execution prices."""
    bars = _parse_bars((audit or InputAudit()).read(Path(path)), path)
    if not bars or any(bar.close <= 0 for bar in bars):
        raise EvidenceError("benchmark_index_invalid", path)
    return bars


def load_sessions(root: Path, index_bars: list[Bar], *, audit: InputAudit) -> tuple[tuple[str, ...], str]:
    path = root / "data/calendar/a_share_trading_calendar.csv"
    if not path.exists():
        return tuple(bar.day for bar in index_bars), "benchmark_sessions_proxy_calendar_missing"
    sessions, seen = [], set()
    for row in csv.DictReader(io.StringIO(audit.read(path))):
        try:
            day = parse_day(str(row.get("date") or ""))
        except ValueError as exc:
            raise EvidenceError("calendar_date_invalid", path) from exc
        token = str(row.get("is_open") or "").lower().strip()
        if day in seen or token not in {"true", "false", "1", "0"}:
            raise EvidenceError("calendar_row_invalid", path)
        seen.add(day)
        if token in {"true", "1"}:
            sessions.append(day)
    if not sessions:
        raise EvidenceError("calendar_empty", path)
    return tuple(sorted(sessions)), "local_calendar"


def limit_pct(symbol: str) -> float:
    return float(limit_ratio(symbol, OUT_OF_SAMPLE_FROM, st_status="not_st")[0] or 0.0) * 100.0


def ex_rights_days(bars: list[Bar], symbol: str, start: str, end: str) -> list[str]:
    """A screening heuristic, NOT proof of corporate-action absence."""
    flagged = []
    for previous, current in zip(bars, bars[1:]):
        if not (start < current.day <= end) or previous.close <= 0 or current.close <= 0:
            continue
        if current.change is not None:
            implied = current.close - current.change
            if implied > 0 and abs(implied / previous.close - 1.0) > 0.02:
                flagged.append(current.day)
                continue
        band = float(limit_ratio(symbol, current.day, st_status="not_st")[0] or 0.0) * 100.0
        if abs(current.close / previous.close - 1.0) * 100.0 > band + 0.6:
            flagged.append(current.day)
    return flagged


def _fee(side: str, amount: float) -> float:
    return float(compute_trade_costs(side=side, gross=amount, config=RESEARCH_COSTS)["total_fee"])


def trade_cost(buy_amount: float, sell_amount: float) -> float:
    return _fee("buy", buy_amount) + _fee("sell", sell_amount)


def _after(bars: list[Bar], day: str) -> list[Bar]:
    return [bar for bar in bars if bar.day > day]


def _sessions(trade: Trade, bars: list[Bar]) -> tuple[str, ...]:
    return trade.session_days or tuple(bar.day for bar in bars)


def _held(trade: Trade, bars: list[Bar], end: str) -> int:
    return sum(trade.entry_date < day <= end for day in _sessions(trade, bars))


def _fill_reason(trade: Trade, bar: Bar, bars: list[Bar], side: str, price: float | None) -> str:
    if any(value is None or value <= 0 for value in (bar.open, bar.high, bar.low, bar.close)):
        return "ohlc_evidence_missing"
    if bar.volume is None:
        return "volume_evidence_missing"
    if bar.volume <= 0:
        return "no_trading_volume"
    if bar.high < max(bar.open, bar.close) or bar.low > min(bar.open, bar.close) or bar.high < bar.low:
        return "ohlc_invalid"
    previous = [item for item in bars if item.day < bar.day and item.close > 0]
    base = bar.pre_close
    if base is None and bar.change is not None:
        base = _finite(bar.close - bar.change)
    if base is None and previous:
        # Do not bridge an unknown missing session using an older close.
        days = _sessions(trade, bars)
        position = bisect_right(days, bar.day) - 1
        if position > 0 and previous[-1].day == days[position - 1]:
            base = previous[-1].close
    check = limit_fill_check(trade.symbol, side, price=price, trade_date=bar.day,
                             pre_close=base, pct_chg=bar.pct_chg if side == "sell" else None,
                             st_status="unknown")
    return "" if check["ok"] else str(check["code"])


def _open_outcome(trade: Trade, bars: list[Bar], reason: str, planned: str = "",
                  blocked: list[dict[str, str]] | None = None) -> Outcome | None:
    available = [bar for bar in bars if bar.day >= trade.entry_date and bar.close > 0]
    if not available:
        return None
    last = available[-1]
    return Outcome(last.day, last.close, _held(trade, bars, last.day), True, reason, planned, blocked or [])


def _sell_when(trade: Trade, bars: list[Bar], trigger: Callable[[Bar], bool], *, planned: str = "") -> Outcome | None:
    pending = False
    blocked: list[dict[str, str]] = []
    for bar in _after(bars, trade.entry_date):
        pending = pending or trigger(bar)
        if not pending:
            continue
        reason = _fill_reason(trade, bar, bars, "sell", bar.close)
        if reason:
            blocked.append({"date": bar.day, "reason": reason})
            continue
        return Outcome(bar.day, bar.close, _held(trade, bars, bar.day), False, "completed", planned, blocked)
    reason = "exit_blocked" if blocked else "not_matured" if planned else "rule_not_triggered"
    return _open_outcome(trade, bars, reason, planned, blocked)


def exit_fixed(days: int) -> Callable[[Trade, list[Bar]], Outcome | None]:
    def rule(trade: Trade, bars: list[Bar]) -> Outcome | None:
        sessions = _sessions(trade, bars)
        position = bisect_right(sessions, trade.entry_date) + days - 1
        planned = sessions[position] if position < len(sessions) else ""
        return (_sell_when(trade, bars, lambda bar: bar.day >= planned, planned=planned)
                if planned else _open_outcome(trade, bars, "not_matured"))
    return rule


def exit_stop_take(stop_pct: float, take_pct: float | None) -> Callable[[Trade, list[Bar]], Outcome | None]:
    def rule(trade: Trade, bars: list[Bar]) -> Outcome | None:
        if not trade.entry_price:
            return None
        def trigger(bar: Bar) -> bool:
            move = (bar.close / trade.entry_price - 1.0) * 100.0
            return bar.close > 0 and (move <= stop_pct or (take_pct is not None and move >= take_pct))
        return _sell_when(trade, bars, trigger)
    return rule


def exit_stop_or_ma(stop_pct: float, window: int = 20) -> Callable[[Trade, list[Bar]], Outcome | None]:
    def rule(trade: Trade, bars: list[Bar]) -> Outcome | None:
        if not trade.entry_price:
            return None
        by_day = {bar.day: bar.close for bar in bars}
        sessions = _sessions(trade, bars)
        def trigger(bar: Bar) -> bool:
            end = bisect_right(sessions, bar.day)
            days = sessions[max(0, end - window):end]
            history = [by_day[day] for day in days if by_day.get(day, 0) > 0]
            below = len(history) == window and bar.close < sum(history) / window
            return bar.close > 0 and ((bar.close / trade.entry_price - 1.0) * 100.0 <= stop_pct or below)
        return _sell_when(trade, bars, trigger)
    return rule


def recorded_outcome(trade: Trade, bars: list[Bar]) -> Outcome | None:
    last = bars[-1].day if bars else ""
    if trade.recorded_open or (trade.recorded_exit_date and trade.recorded_exit_date > last):
        return _open_outcome(trade, bars, "recorded_position_open")
    if not trade.recorded_exit_price or not trade.recorded_exit_date:
        return None
    return Outcome(trade.recorded_exit_date, trade.recorded_exit_price,
                   _held(trade, bars, trade.recorded_exit_date), False, "recorded_exit_unverified")


def next_open_entry(trade: Trade, bars: list[Bar]) -> Trade | None:
    sessions = _sessions(trade, bars)
    position = bisect_right(sessions, trade.entry_date)
    if position >= len(sessions):
        return None
    first = next((bar for bar in bars if bar.day == sessions[position]), None)
    if first is None or _fill_reason(trade, first, bars, "buy", first.open):
        return None
    return replace(trade, entry_date=first.day, entry_price=first.open, entry_price_role="next_open",
                   recorded_exit_date="", recorded_exit_price=None, recorded_open=True)


VARIANTS: tuple[tuple[str, str, Callable[[Trade, list[Bar]], Outcome | None]], ...] = (
    ("recorded", "账本退出时点（每笔100股重放）", recorded_outcome),
    ("stop8", "只用-8%止损（收盘触发）", exit_stop_take(STOP_PCT, None)),
    ("hold5", "固定拿 5 个交易日", exit_fixed(5)),
    ("hold10", "固定拿 10 个交易日", exit_fixed(10)),
    ("hold20", "固定拿 20 个交易日", exit_fixed(20)),
    ("stop8_take15", "-8%止损+15%止盈", exit_stop_take(STOP_PCT, TAKE_PROFIT_PCT)),
    ("stop8_ma20", "-8%止损+跌破20日均线", exit_stop_or_ma(STOP_PCT, 20)),
)
EXTRA_VARIANTS = (
    ("hold10_next_open", "次日开盘买，再拿10个交易日"),
    ("hold10_signal_close", "信号当天收盘买，拿10个交易日（价格近似）"),
    ("hold10_next_open_same_exit", "次日开盘买，和信号收盘组同日卖"),
)


def benchmark_return(index_bars: list[Bar], start: str, end: str, *, start_field: str = "close") -> float | None:
    by_day = {bar.day: bar for bar in index_bars}
    if start not in by_day or end not in by_day:
        return None
    start_price = getattr(by_day[start], start_field)
    return (by_day[end].close / start_price - 1.0) * 100.0 if _finite(start_price) else None


def entry_features(trade: Trade, bars: list[Bar]) -> dict[str, Any]:
    end = bisect_right(_sessions(trade, bars), trade.entry_date)
    days = _sessions(trade, bars)[max(0, end - 21):end]
    closes = {bar.day: bar.close for bar in bars if bar.day <= trade.entry_date and bar.close > 0}
    if len(days) != 21 or any(day not in closes for day in days):
        return {"ma20_deviation_pct": None, "ma20_group": "missing_history"}
    average = sum(closes[day] for day in days[-20:]) / 20.0
    gap = (closes[trade.entry_date] / average - 1.0) * 100.0
    return {"ma20_deviation_pct": round(gap, 4), "ma20_group": "le5" if gap <= 5 else "5to10" if gap <= 10 else "gt10",
            "prior20_return_pct": round((closes[trade.entry_date] / closes[days[0]] - 1.0) * 100.0, 4)}


def _row(trade: Trade, used: Trade, outcome: Outcome, bars: list[Bar], index_bars: list[Bar],
         last_day: str) -> dict[str, Any]:
    buy, sell = float(used.entry_price) * LOT, outcome.exit_price * LOT
    buy_fee, estimated_sell_fee = _fee("buy", buy), _fee("sell", sell)
    sell_fee = 0.0 if outcome.still_open else estimated_sell_fee
    gross = (sell / buy - 1.0) * 100.0
    cost_pct = (buy_fee + sell_fee) / buy * 100.0
    end = last_day if outcome.still_open else outcome.exit_date
    available = {bar.day for bar in bars}
    missing = [day for day in _sessions(used, bars) if used.entry_date <= day <= end and day not in available]
    signal_bar = next(bar for bar in bars if bar.day == trade.entry_date)
    bench = benchmark_return(index_bars, used.entry_date, outcome.exit_date,
                             start_field="open" if used.entry_price_role == "next_open" else "close")
    return {
        "trade_id": trade.trade_id, "symbol": trade.symbol, "name": trade.name,
        "signal_date": trade.entry_date, "entry_date": used.entry_date, "entry_price": used.entry_price,
        "entry_price_role": used.entry_price_role, "exit_date": outcome.exit_date if not outcome.still_open else "",
        "observation_date": outcome.exit_date, "exit_price": outcome.exit_price,
        "holding_days": outcome.holding_days, "still_open": outcome.still_open,
        "reason": outcome.reason, "planned_exit_date": outcome.planned_exit_date,
        "blocked_exit_days": outcome.blocked_exit_days, "missing_sessions": missing,
        "eligible_metrics": not missing and (not outcome.still_open or outcome.exit_date == last_day),
        "gross_return_pct": round(gross, 6), "fee_drag_pct": round(cost_pct, 6),
        "return_pct": round(gross - cost_pct, 6), "pnl": round(sell - buy - buy_fee - sell_fee, 4),
        "buy_fee": buy_fee, "sell_fee": sell_fee, "cost": round(buy_fee + sell_fee, 4),
        "estimated_liquidation_sell_fee": estimated_sell_fee if outcome.still_open else 0.0,
        "benchmark_pct": bench, "signal_benchmark_pct": benchmark_return(index_bars, trade.entry_date, outcome.exit_date),
        "entry_price_gap_pct": round(abs(signal_bar.close / float(trade.entry_price) - 1.0) * 100.0, 4),
        "out_of_sample": trade.entry_date >= OUT_OF_SAMPLE_FROM, **entry_features(trade, bars),
    }


def evaluate(trades: Iterable[Trade], bars_for: Callable[[str], list[Bar]], index_bars: list[Bar],
             *, last_day: str, sessions: tuple[str, ...] = ()) -> dict[str, Any]:
    results = {key: VariantResult(key, label) for key, label, _rule in VARIANTS}
    results.update({key: VariantResult(key, label) for key, label in EXTRA_VARIANTS})
    excluded, unusable, not_fillable, close_not_fillable = [], [], [], []
    for number, original in enumerate(trades):
        if original.entry_date > last_day:
            continue
        trade = replace(original, trade_id=original.trade_id or f"{original.symbol}:{original.entry_date}:{number}", session_days=sessions)
        common = {"trade_id": trade.trade_id, "symbol": trade.symbol, "name": trade.name, "entry_date": trade.entry_date}
        if not _finite(trade.entry_price):
            unusable.append({**common, "reason": "recorded_execution_price_missing"})
            continue
        bars = [bar for bar in bars_for(trade.symbol) if bar.day <= last_day]
        entry = next((bar for bar in bars if bar.day == trade.entry_date and bar.close > 0), None)
        if entry is None:
            unusable.append({**common, "reason": "entry_daily_missing"})
            continue
        attempts = [(key, trade, rule(trade, bars)) for key, _label, rule in VARIANTS]
        close_entry = replace(trade, entry_price=entry.close, entry_price_role="signal_close_proxy")
        close_outcome = None
        close_reason = _fill_reason(trade, entry, bars, "buy", entry.close)
        if not close_reason:
            close_outcome = exit_fixed(10)(close_entry, bars)
            attempts.append(("hold10_signal_close", close_entry, close_outcome))
        else:
            close_not_fillable.append({**common, "reason": close_reason})
        shifted = next_open_entry(trade, bars)
        if shifted is None:
            days = _sessions(trade, bars)
            position = bisect_right(days, trade.entry_date)
            next_day = days[position] if position < len(days) else ""
            first = next((bar for bar in bars if bar.day == next_day), None)
            reason = ("next_session_not_observed" if not next_day or next_day > last_day else
                      _fill_reason(trade, first, bars, "buy", first.open) if first else "next_session_quote_missing")
            not_fillable.append({**common, "next_session": next_day, "reason": reason})
        else:
            attempts.append(("hold10_next_open", shifted, exit_fixed(10)(shifted, bars)))
            if close_outcome is not None:
                same = replace(close_outcome, holding_days=_held(shifted, bars, close_outcome.exit_date))
                attempts.append(("hold10_next_open_same_exit", shifted, same))
        for key, used, outcome in attempts:
            if outcome is None:
                unusable.append({**common, "variant": key, "reason": "outcome_price_missing"})
                continue
            row = _row(trade, used, outcome, bars, index_bars, last_day)
            flags = ex_rights_days(bars, trade.symbol, trade.entry_date, outcome.exit_date)
            if flags:
                excluded.append({**row, "variant": key, "ex_rights_days": flags})
            else:
                results[key].rows.append(row)
    return {"variants": results, "excluded": excluded, "unusable": unusable,
            "not_fillable": not_fillable, "close_not_fillable": close_not_fillable}


def summarize(rows: list[dict[str, Any]], *, capital: float = 100000.0,
              completed_only: bool = True) -> dict[str, Any]:
    selected = [row for row in rows if row.get("eligible_metrics", True) and (not completed_only or not row["still_open"])]
    result: dict[str, Any] = {"trades": len(selected), "observed": len(rows),
        "still_open": sum(row["still_open"] for row in rows),
        "withheld_data_gap": sum(not row.get("eligible_metrics", True) for row in rows),
        "max_drawdown_pct": None, "drawdown_status": "requires_daily_equity_not_exit_pnl_sum"}
    if not selected:
        return result
    returns = [row["return_pct"] for row in selected]
    paired = [row for row in selected if row.get("benchmark_pct") is not None]
    def average(values: list[float]) -> float | None:
        return round(sum(values) / len(values), 6) if values else None
    result.update({
        "total_pnl": round(sum(row["pnl"] for row in selected), 4),
        "avg_return_pct": average(returns), "median_return_pct": round(median(returns), 6),
        "avg_gross_return_pct": average([row["gross_return_pct"] for row in selected]),
        "avg_fee_drag_pct": average([row["fee_drag_pct"] for row in selected]),
        "win_rate_pct": round(sum(value > 0 for value in returns) / len(returns) * 100.0, 2),
        "avg_holding_days": round(sum(row["holding_days"] for row in selected) / len(selected), 2),
        "total_cost": round(sum(row["cost"] for row in selected), 4), "benchmark_pairs": len(paired),
        "delayed_exits": sum(bool(row.get("blocked_exit_days")) for row in selected),
        "avg_benchmark_pct": average([row["benchmark_pct"] for row in paired]),
        "avg_gross_excess_pct": average([row["gross_return_pct"] - row["benchmark_pct"] for row in paired]),
        "avg_net_excess_pct": average([row["return_pct"] - row["benchmark_pct"] for row in paired]),
        "beat_benchmark_pct": round(sum(row["return_pct"] > row["benchmark_pct"] for row in paired) / len(paired) * 100, 2) if paired else None,
    })
    return result


def paired_comparison(left: list[dict[str, Any]], right: list[dict[str, Any]]) -> dict[str, Any]:
    a = {row["trade_id"]: row for row in left if not row["still_open"] and row["eligible_metrics"]}
    b = {row["trade_id"]: row for row in right if not row["still_open"] and row["eligible_metrics"]}
    common = sorted(a.keys() & b.keys())
    if not common:
        return {"pairs": 0, "left_only": len(a), "right_only": len(b)}
    def delta(field: str) -> float:
        return round(sum(b[key][field] - a[key][field] for key in common) / len(common), 6)
    return {"pairs": len(common), "left_only": len(a.keys() - b.keys()), "right_only": len(b.keys() - a.keys()),
            "different_exit_dates": sum(a[key]["exit_date"] != b[key]["exit_date"] for key in common),
            "gross_delta_pct": delta("gross_return_pct"), "fee_delta_pct": delta("fee_drag_pct"),
            "net_delta_pct": delta("return_pct"), "trade_ids": common}


def daily_portfolio(rows: list[dict[str, Any]], bars_by_symbol: Mapping[str, list[Bar]],
                    sessions: tuple[str, ...], last_day: str, *, capital: float = 100000.0) -> dict[str, Any]:
    """Daily cash + observed holdings. Missing marks or borrowing suppress formal drawdown."""
    selected = [row for row in rows if row["eligible_metrics"]]
    if not selected:
        return {"status": "no_evaluable_entries", "max_drawdown_pct": None, "curve": []}
    start = min(row["entry_date"] for row in selected)
    prices = {symbol: {bar.day: bar.close for bar in bars} for symbol, bars in bars_by_symbol.items()}
    curve, missing = [], []
    peak, drawdown, minimum_cash = capital, 0.0, capital
    for day in (value for value in sessions if start <= value <= last_day):
        cash, holdings = capital, 0.0
        sold_today = 0.0
        absent = []
        for row in selected:
            if day < row["entry_date"]:
                continue
            cash -= row["entry_price"] * LOT + row["buy_fee"]
            if row["exit_date"] and day >= row["exit_date"]:
                cash += row["exit_price"] * LOT - row["sell_fee"]
                if day == row["exit_date"]:
                    sold_today += row["exit_price"] * LOT - row["sell_fee"]
            else:
                mark = prices.get(row["symbol"], {}).get(day)
                if not _finite(mark):
                    absent.append(row["trade_id"])
                else:
                    holdings += mark * LOT
        # Conservative funding: later same-day sales cannot finance an earlier buy.
        minimum_cash = min(minimum_cash, cash - sold_today)
        equity = None if absent else cash + holdings
        if absent:
            missing.append({"date": day, "trade_ids": absent})
        else:
            peak = max(peak, equity)
            drawdown = max(drawdown, (peak - equity) / peak * 100.0)
        curve.append({"date": day, "cash": round(cash, 4), "holdings": None if absent else round(holdings, 4),
                      "equity": round(equity, 4) if equity is not None else None})
    status = "missing_daily_marks" if missing else "insufficient_capital" if minimum_cash < 0 else "observed_daily_equity"
    return {"status": status, "capital": capital, "lot": LOT, "entries": len(selected),
            "withheld_entries": len(rows) - len(selected), "minimum_cash": round(minimum_cash, 4),
            "max_drawdown_pct": round(drawdown, 6) if status == "observed_daily_equity" else None,
            "curve": curve, "missing_marks": missing, "is_actual_strategy_equity": False,
            "cash_check": "buys_before_same_day_sells"}


def fill_price(item: Mapping[str, Any]) -> float | None:
    """Never substitute a fee-inclusive cost for an undocumented raw fill."""
    return _finite(item.get("execution_price"))


def trades_from_ledger(ledger: Mapping[str, Any]) -> list[Trade]:
    trades = []
    for kind in ("closed_positions", "positions"):
        for number, item in enumerate(ledger.get(kind) or []):
            if not isinstance(item, Mapping):
                continue
            symbol, day = str(item.get("symbol") or ""), str(item.get("entry_date") or "")[:10]
            if not symbol.isdigit() or len(symbol) != 6:
                raise EvidenceError("ledger_symbol_invalid")
            try:
                parse_day(day)
            except ValueError as exc:
                raise EvidenceError("ledger_entry_date_invalid") from exc
            trades.append(Trade(symbol, str(item.get("name") or ""), day, fill_price(item),
                                str(item.get("exit_date") or "")[:10], _finite(item.get("exit_price")),
                                kind == "positions", f"{kind}:{number}:{symbol}:{day}"))
    return trades


def _cohorts(result: dict[str, Any]) -> dict[str, Any]:
    rows = {key: item.rows for key, item in result["variants"].items()}
    completed = {key: {row["trade_id"]: row for row in rows[key] if not row["still_open"] and row["eligible_metrics"]}
                 for key in ("hold5", "hold10", "hold20")}
    ids = sorted(set.intersection(*(set(value) for value in completed.values())))
    policy_keys = [key for key, _label, _rule in VARIANTS] + ["hold10_next_open"]
    policy_ids = sorted(set.intersection(*({row["trade_id"] for row in rows[key] if row["eligible_metrics"]} for key in policy_keys)))
    return {"fixed_common": {"trade_ids": ids, "count": len(ids), "summaries": {
        key: summarize([value[ident] for ident in ids]) for key, value in completed.items()}},
        "rule_common": {"trade_ids": policy_ids, "count": len(policy_ids)},
        "next_open_same_exit": paired_comparison(rows["hold10_signal_close"], rows["hold10_next_open_same_exit"]),
        "next_open_full_policy": paired_comparison(rows["hold10_signal_close"], rows["hold10_next_open"])}


def _hypothesis_groups(rows: list[dict[str, Any]]) -> dict[str, Any]:
    return {sample: {group: summarize([row for row in rows if row["out_of_sample"] == (sample == "out_of_sample")
                                      and row["ma20_group"] == group])
                    for group in ("le5", "5to10", "gt10", "missing_history")}
            for sample in ("in_sample", "out_of_sample")}


def build_report(root: Path, as_of: str) -> tuple[str, dict[str, Any]]:
    root, as_of = Path(root).resolve(), parse_day(as_of)
    audit = InputAudit()
    data: dict[str, Any] = {"as_of": as_of, "method_version": METHOD_VERSION,
        "out_of_sample_from": OUT_OF_SAMPLE_FROM, "protocol": PROTOCOL, "protocol_sha256": PROTOCOL_SHA256,
        "research_only": True, "paper_only": True, "formal_comparison_ready": False,
        "price_basis": "raw", "benchmark_role": "index_points", "corporate_actions_status": "not_fully_verified",
        "source_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest()}
    try:
        ledger = json.loads(audit.read(root / "state/auto_paper_portfolio.json"))
        if not isinstance(ledger, Mapping):
            raise EvidenceError("ledger_invalid")
        trades = [trade for trade in trades_from_ledger(ledger) if trade.entry_date <= as_of]
        index = [bar for bar in load_index_bars(root / "data/benchmark/hs300.csv", audit=audit) if bar.day <= as_of]
        if not index or index[-1].day != as_of:
            raise EvidenceError("benchmark_as_of_missing")
        sessions, calendar_status = load_sessions(root, index, audit=audit)
        if as_of not in sessions or any(trade.entry_date not in sessions for trade in trades):
            raise EvidenceError("requested_calendar_session_missing")
        bars = {symbol: load_bars(root / "data/daily" / f"{symbol}.csv", audit=audit) for symbol in sorted({trade.symbol for trade in trades})}
        result = evaluate(trades, bars.__getitem__, index, last_day=as_of, sessions=sessions)
        comparisons = _cohorts(result)
        policy_ids = set(comparisons["rule_common"]["trade_ids"])
        pool_rows = {key: [row for row in variant.rows if row["trade_id"] in policy_ids] for key, variant in result["variants"].items()}
        labels = [(key, label) for key, label, _rule in VARIANTS] + list(EXTRA_VARIANTS)
        summaries = {key: {"label": label, **{sample: summarize([row for row in result["variants"][key].rows
            if row["out_of_sample"] == (sample == "out_of_sample")]) for sample in ("in_sample", "out_of_sample")},
            "marked_all": summarize(result["variants"][key].rows, completed_only=False),
            "matched_marked": summarize(pool_rows[key], completed_only=False)} for key, label in labels}
        data.update({"status": "research_partial", "calendar_status": calendar_status, "trade_count": len(trades),
            "summaries": summaries, "excluded_ex_rights": result["excluded"], "unusable": result["unusable"],
            "not_fillable_next_open": result["not_fillable"], "rows": {key: variant.rows for key, variant in result["variants"].items()},
            "not_fillable_signal_close": result["close_not_fillable"],
            "comparisons": comparisons,
            "daily_portfolios": {key: daily_portfolio(rows, bars, sessions, as_of) for key, rows in pool_rows.items()},
            "entry_hypothesis": {key: _hypothesis_groups(result["variants"][key].rows) for key in ("hold5", "hold10", "hold20")}})
        audit.verify()
    except (EvidenceError, json.JSONDecodeError) as exc:
        for key in ("comparisons", "daily_portfolios", "entry_hypothesis"):
            data.pop(key, None)
        data.update(status="blocked", reason=getattr(exc, "reason", "ledger_json_invalid"),
                    issue_path=getattr(exc, "path", ""), summaries={}, rows={})
    data["input_files"] = audit.files
    data["input_verification"] = "stable_sha256" if data["status"] != "blocked" else "not_verified"
    return render_report(data), data


def _pct(value: Any) -> str:
    return "—" if value is None else f"{value:+.2f}%"


def conclusion(summaries: Mapping[str, Any]) -> str:
    details = []
    for key, days in (("hold5", 5), ("hold10", 10), ("hold20", 20)):
        item = summaries.get(key, {}).get("in_sample", {})
        if item.get("benchmark_pairs"):
            details.append(f"拿{days}天的成熟{item['trades']}笔：扣费前相对指数{_pct(item['avg_gross_excess_pct'])}，"
                           f"费用拖累{item['avg_fee_drag_pct']:.2f}个百分点，扣费后相对指数{_pct(item['avg_net_excess_pct'])}")
    return ("；".join(details) + "。" if details else "成熟且有基准的样本不够。") + (
        "股价表现、费用、持有时间和入场位置要一起查；这张表不能把亏损归因给选股或卖法，更不能证明追高是原因。")


def render_report(data: Mapping[str, Any]) -> str:
    lines = [f"# 卖法对比表 · 截至 {data['as_of']}", "", "纸面 · 未验证 · 研究对照，不改策略账。", ""]
    if data["status"] == "blocked":
        return "\n".join(lines + [f"资料不能核实，停止计算：{data['reason']}。", "未输出收益或回撤，原报告不代表本次结果。", ""])
    summaries = data["summaries"]
    lines += [conclusion(summaries), "",
        f"读取{data['trade_count']}笔账本初次入场，每笔100股。已到期、还拿着、缺资料分别列；原始成交价缺失的交易不拿成本价代替。",
        "费用：万2.5佣金，每边最低5元；卖出印花税0.05%；过户费每边0.001%；未假设滑点。未平仓只扣实际买入费用，不提前收卖出费用。",
        "", "## 已满持有期限的样本内结果", "",
        "目标持有N个市场交易日；到期跌停等不能卖时顺延到首个可卖收盘，延期笔数和实际平均天数也列出。", "",
        "| 持有期 | 成熟笔数 | 未结束 | 缺行情暂不计算 | 延期笔数 | 实际平均天数 | 扣费前平均 | 费用拖累（百分点） | 扣费后平均 | 同期指数平均 |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|"]
    for key in ("hold5", "hold10", "hold20"):
        s = summaries[key]["in_sample"]
        lines.append(f"| {summaries[key]['label']} | {s['trades']} | {s['still_open']} | {s['withheld_data_gap']} | "
                     f"{s.get('delayed_exits', 0)} | {s.get('avg_holding_days', 0):.2f} | "
                     f"{_pct(s.get('avg_gross_return_pct'))} | {s.get('avg_fee_drag_pct', 0):.2f} | "
                     f"{_pct(s.get('avg_return_pct'))} | {_pct(s.get('avg_benchmark_pct'))} |")
    common = data["comparisons"]["fixed_common"]
    lines += ["", f"### 同一批成熟样本（{common['count']}笔）", "", "5、10、20天都已结束且有完整期间行情才进入这一组，避免不同入场批次混着比。", "",
              "| 持有期 | 扣费前平均 | 扣费后平均 | 扣费前相对指数 | 扣费后相对指数 |", "|---|---:|---:|---:|---:|"]
    for key, s in common["summaries"].items():
        lines.append(f"| {summaries[key]['label']} | {_pct(s.get('avg_gross_return_pct'))} | {_pct(s.get('avg_return_pct'))} | "
                     f"{_pct(s.get('avg_gross_excess_pct'))} | {_pct(s.get('avg_net_excess_pct'))} |")
    lines += ["", "## 各卖法截至当日的研究持仓池", "",
        f"同一批可重放的{data['comparisons']['rule_common']['count']}笔，含未平仓估值，不能当成已满期限收益。每笔100股、起始10万元，不是正式策略账；重复买同一股票也按独立入场计算。", "",
        "| 卖法 | 可计算笔数 | 未平仓 | 合计盈亏 | 已计费用 | 每日持仓净值最大回撤 | 回撤核实状态 |",
        "|---|---:|---:|---:|---:|---:|---|"]
    for key in [key for key, _label, _rule in VARIANTS] + ["hold10_next_open"]:
        s, pool = summaries[key]["matched_marked"], data["daily_portfolios"][key]
        pool_reason = {"observed_daily_equity": "日净值已计算", "missing_daily_marks": "有缺日估值，回撤未核实",
                       "insufficient_capital": f"资金短缺{max(0, -pool.get('minimum_cash', 0)):,.2f}元",
                       "no_evaluable_entries": "样本不足"}[pool["status"]]
        lines.append(f"| {summaries[key]['label']} | {s['trades']} | {s['still_open']} | {s.get('total_pnl', 0):+,.2f}元 | "
                     f"{s.get('total_cost', 0):,.2f}元 | {'—' if pool.get('max_drawdown_pct') is None else format(pool['max_drawdown_pct'], '.2f') + '%'} | {pool_reason} |")
    lines += ["", "每日回撤包含未平仓浮亏和现金；资金检查保守地按同日先买后卖，不能借后来的卖款买前面的票。资料缺失或资金不足不报回撤。", "",
              "## 次日开盘比较：先配对，再分开价格与持有时点", ""]
    for key, label in (("next_open_same_exit", "同一批、同一天卖：入场价格与费用的影响"),
                       ("next_open_full_policy", "次日开盘后再拿10天：整个时点政策的影响")):
        pair = data["comparisons"][key]
        if pair["pairs"]:
            lines.append(f"- {label}：成熟配对{pair['pairs']}笔；开盘组相对收盘组扣费前{_pct(pair['gross_delta_pct'])}、"
                         f"费用差{pair['fee_delta_pct']:+.2f}个百分点、扣费后{_pct(pair['net_delta_pct'])}；"
                         f"退出日期不同{pair['different_exit_dates']}笔。")
        else:
            lines.append(f"- {label}：还没有可配对的成熟交易。")
    lines += ["- 信号收盘组使用当天收盘价，不用早期盘中账本价冒充收盘价。买在次日开盘的基准也从次日指数开盘点位算。",
              f"- 收盘价买入受限{len(data['not_fillable_signal_close'])}笔，次日开盘受限/尚无下一日资料{len(data['not_fillable_next_open'])}笔，原件分别列明。配对结果只描述可比较的子集，不能推广到不能买入的票。", "",
              "## 入场离20日均线远的猜想", "", "按入场当时已有行情分为≤5%、5%–10%、>10%；观察以后的新入场，不修改实际选股条件。", "",
              "| 持有期 | 偏离组 | 历史成熟笔数 | 历史扣费前相对指数 | 历史扣费后相对指数 | 新样本成熟 | 新样本未结束 | 新样本扣费前相对指数 | 新样本扣费后相对指数 |",
              "|---|---|---:|---:|---:|---:|---:|---:|---:|"]
    for key in ("hold5", "hold10", "hold20"):
        groups = data["entry_hypothesis"][key]
        for group, label in (("le5", "≤5%"), ("5to10", "5%–10%"), ("gt10", ">10%")):
            inside, outside = groups["in_sample"][group], groups["out_of_sample"][group]
            lines.append(f"| {summaries[key]['label']} | {label} | {inside['trades']} | {_pct(inside.get('avg_gross_excess_pct'))} | "
                         f"{_pct(inside.get('avg_net_excess_pct'))} | {outside['trades']} | {outside['still_open']} | "
                         f"{_pct(outside.get('avg_gross_excess_pct'))} | {_pct(outside.get('avg_net_excess_pct'))} |")
    lines += ["", f"新样本（样本外）起点{OUT_OF_SAMPLE_FROM}，方法号{METHOD_VERSION}；未成熟结果单列。组别样本小、交易重叠、市场环境和股票类型不同，不能说是因果。", "",
        "## 仍有哪些限制", "",
        "- 这是原始价格研究，没有把现金分红、送转和复权因子全部核清；没有检出大跳变也不能证明没有公司行动。疑似跳变单列，不补价格。",
        "- 当天收盘知道信号后无法保证按那个收盘价买卖，收盘成交仍是研究近似；实际卖出规则和旧账加仓现金流没有由此复原。",
        "- 当前股票名称不能证明历史ST状态；涨跌停使用项目的按日期规则和保守未知ST口径。",
        f"- 交易日口径：{('现有本地日历' if data['calendar_status'] == 'local_calendar' else '指数已有日期代理；日历缺失')}。缺交易日行情不等于停牌，不将它当作已完成窗口。",
        "- 前瞻方法在11/3收盘前封存；历史结论只是样本内描述，未来交易尚未发生或未到期就是未验证。", "",
        "## 暂不能重放的原件", ""]
    for row in data["unusable"]:
        lines.append(f"- {row['name']}（{row['symbol']}）{row['entry_date']}：{row['reason']}" + (f"，{row['variant']}" if row.get('variant') else ""))
    for row in data["not_fillable_next_open"]:
        lines.append(f"- 次日开盘 {row['name']}（{row['symbol']}）{row['entry_date']}：{row['reason']}")
    for row in data["not_fillable_signal_close"]:
        lines.append(f"- 信号当天收盘 {row['name']}（{row['symbol']}）{row['entry_date']}：{row['reason']}")
    gap_rows = [(key, row) for key, rows in data["rows"].items() for row in rows if not row["eligible_metrics"]]
    lines += ["", f"期间缺行情或估值陈旧：{len(gap_rows)}条方案记录；疑似公司行动剔除：{len(data['excluded_ex_rights'])}条方案记录。每笔和输入SHA见同名JSON。", ""]
    return "\n".join(lines)


def write_report(root: Path, as_of: str, output_dir: Path) -> dict[str, Any]:
    """Write only explicit report destinations, with a shared report lock and atomic files."""
    output_dir = Path(output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    with interprocess_file_lock(output_dir / ".exit_rule_replay.lock", timeout_seconds=0):
        markdown, data = build_report(root, as_of)
        paths = [output_dir / f"exit_rule_replay_{as_of}.md", output_dir / f"exit_rule_replay_{as_of}.json"]
        contents = [markdown, json.dumps(data, ensure_ascii=False, indent=1)]
        for path, content in zip(paths, contents):
            temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
            try:
                temporary.write_text(content, encoding="utf-8")
                os.replace(temporary, path)
            finally:
                temporary.unlink(missing_ok=True)
    return {"status": data["status"], "as_of": as_of, "reason": data.get("reason", ""),
            "markdown": str(paths[0]), "json": str(paths[1]), "trades": data.get("trade_count", 0),
            "method_version": METHOD_VERSION, "protocol_sha256": PROTOCOL_SHA256}
