"""Read-only snapshot views for the paper-trader prototype."""

from __future__ import annotations

import csv
import hashlib
import io
import re
from collections import deque
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
from typing import Any, Iterator

DECISION_SLOTS = ("1000", "1330", "1445")
NEXT_SLOT = {"1000": "1005", "1330": "1335", "1445": "1450"}
SLOTS = tuple(
    f"{minute // 60:02d}{minute % 60:02d}"
    for start, stop in ((570, 690), (780, 900))
    for minute in range(start, stop, 5)
)
SYMBOL = re.compile(r"[0-9]{6}")
BEIJING = timezone(timedelta(hours=8))
FIVE_MINUTES = timedelta(minutes=5)
TWO_MINUTES = timedelta(minutes=2)
CENT = Decimal("0.01")
ZERO = Decimal(0)
STRENGTH_RATIO = Decimal("1.2")
PLUNGE_PCT = Decimal("-4")
SLOT_MOVE_PCT = Decimal("1.5")


def number(value: Any) -> Decimal | None:
    try:
        parsed = Decimal(str(value).strip().replace(",", ""))
    except (ValueError, InvalidOperation):
        return None
    return parsed if parsed.is_finite() else None


def decimal_text(value: Decimal | None) -> str | None:
    return format(value, "f") if value is not None else None


def timestamp(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=BEIJING)


def slot_start(day: str, slot: str) -> datetime | None:
    if slot not in SLOTS:
        return None
    return timestamp(f"{day}T{slot[:2]}:{slot[2:]}:00+08:00")


def board(symbol: str) -> str | None:
    if not isinstance(symbol, str) or not SYMBOL.fullmatch(symbol):
        return None
    if symbol.startswith(("600", "601", "603", "605", "000", "001", "002", "003")):
        return "main"
    if symbol.startswith(("300", "301")):
        return "chinext"
    return None


def eligibility(symbol: str, name: str | None) -> str | None:
    if board(symbol) is None:
        return "board_not_allowed"
    normalized = "".join((name or "").split()).upper()
    if not normalized:
        return "name_missing"
    if "ST" in normalized or "退" in normalized:
        return "st_or_delisting"
    return None


@dataclass(frozen=True)
class Quote:
    symbol: str
    name: str
    price: Decimal | None
    pre_close: Decimal | None
    volume: Decimal | None
    amount: Decimal | None
    quote_date: str
    quote_time: str
    fetched_at: str
    source: str
    basis: str
    snapshot_slot: str
    pct_chg: Decimal | None = None
    change: Decimal | None = None
    open: Decimal | None = None
    high: Decimal | None = None
    low: Decimal | None = None

    @property
    def pct(self) -> Decimal | None:
        if self.pct_chg is not None:
            return self.pct_chg
        if self.price is not None and self.pre_close:
            return (self.price - self.pre_close) * 100 / self.pre_close
        return None

    def evidence(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol, "name": self.name,
            "price": decimal_text(self.price), "pre_close": decimal_text(self.pre_close),
            "pct_chg": decimal_text(self.pct), "volume": decimal_text(self.volume),
            "amount": decimal_text(self.amount), "quote_date": self.quote_date,
            "quote_time": self.quote_time, "fetched_at": self.fetched_at,
            "source": self.source, "price_basis": self.basis,
            "snapshot_slot": self.snapshot_slot,
        }


@dataclass
class Frame:
    day: str
    slot: str
    rows: dict[str, Quote]
    sha256: str
    row_count: int
    warnings: list[str] = field(default_factory=list)
    decision_at: str | None = None

    def evidence(self) -> dict[str, Any]:
        return {
            "day": self.day, "slot": self.slot, "sha256": self.sha256,
            "row_count": self.row_count, "unique_symbols": len(self.rows),
            "decision_at": self.decision_at, "warnings": list(self.warnings),
        }


def limits(quote: Quote) -> tuple[Decimal, Decimal] | None:
    if quote.pre_close is None or quote.pre_close <= 0:
        return None
    rate = Decimal("0.20") if board(quote.symbol) == "chinext" else Decimal("0.10")
    return (
        (quote.pre_close * (1 - rate)).quantize(CENT, rounding=ROUND_HALF_UP),
        (quote.pre_close * (1 + rate)).quantize(CENT, rounding=ROUND_HALF_UP),
    )


def quote_issue(quote: Quote | None, day: str) -> str | None:
    if quote is None:
        return "quote_missing"
    if quote.basis != "raw":
        return "price_basis_not_raw"
    if quote.quote_date != day:
        return "quote_date_mismatch"
    if quote.price is None or quote.price <= 0:
        return "invalid_price"
    if quote.pre_close is None or quote.pre_close <= 0:
        return "invalid_pre_close"
    if quote.volume is None or quote.volume <= 0:
        return "suspended_or_no_volume"
    nominal = slot_start(day, quote.snapshot_slot)
    fetched = timestamp(quote.fetched_at)
    observed = timestamp(f"{day}T{quote.quote_time}+08:00")
    # Per-symbol quote timestamps can lead the nominal fetch slot by up to two minutes.
    if not (nominal and fetched and observed
            and nominal <= fetched < nominal + FIVE_MINUTES
            and nominal - TWO_MINUTES <= observed <= fetched):
        return "invalid_quote_time"
    return None


def visible_quotes(frame: Frame | None, cutoff: datetime | None = None) -> dict[str, Quote]:
    if frame is None or not frame.decision_at:
        return {}
    completed = timestamp(frame.decision_at)
    if completed is None:
        return {}
    if cutoff is not None:
        completed = min(completed, cutoff)
    return {
        code: quote for code, quote in frame.rows.items()
        if quote_issue(quote, frame.day) is None and timestamp(quote.fetched_at) <= completed
    }


def dated_rows(path: Path, day: str, *, inclusive: bool = False) -> Iterator[dict[str, str]]:
    with path.open(encoding="utf-8-sig", newline="") as stream:
        header = next(csv.reader(stream), [])
        if not header or header[0].strip() != "date":
            raise ValueError("unexpected_schema")
        previous = ""
        for line in stream:
            # Stop at the first excluded date before CSV-parsing its data cells.
            row_date = line.partition(",")[0].strip().strip('"')
            if not row_date:
                continue
            if row_date > day or (row_date == day and not inclusive):
                break
            if not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", row_date):
                raise ValueError("invalid_date")
            if row_date <= previous:
                raise ValueError("not_date_ordered")
            previous = row_date
            values = next(csv.reader([line]))
            if len(values) != len(header):
                raise ValueError("malformed_row")
            yield dict(zip(header, values))


def raw_daily_issue(row: dict[str, str], symbol: str) -> str | None:
    row_symbol = row.get("symbol", "").strip()
    if row_symbol and row_symbol != symbol:
        return "symbol_mismatch"
    if row.get("adjust", "").strip().lower() != "raw":
        return "basis_not_raw"
    return None


def average(values: list[Decimal | None], length: int) -> Decimal | None:
    return sum(values, ZERO) / length if len(values) == length and None not in values else None


def ratio(numerator: Decimal | None, denominator: Decimal | None) -> Decimal | None:
    if numerator is None or denominator is None or denominator <= 0:
        return None
    return numerator / denominator


class Market:
    """Adapter for local snapshot, historical, calendar, and valuation CSVs."""

    eligibility = staticmethod(eligibility)
    quote_issue = staticmethod(quote_issue)
    limits = staticmethod(limits)
    next_slot = staticmethod(NEXT_SLOT.get)

    def __init__(self, root: Path):
        self.data = Path(root) / "data"
        self._frames: dict[tuple[str, str], Frame] = {}
        self._calendar: dict[str, bool] | None = None
        self._valuations: dict[str, tuple[str | None, dict[str, dict[str, str]]]] = {}

    def frame(self, day: str, slot: str) -> Frame | None:
        if date.fromisoformat(day).isoformat() != day:
            raise ValueError("snapshot date must use YYYY-MM-DD")
        nominal = slot_start(day, slot)
        if nominal is None:
            raise ValueError(f"unsupported snapshot slot: {slot}")
        key = (day, slot)
        if key in self._frames:
            return self._frames[key]
        path = self.data / "snapshots" / day / f"{slot}.csv"
        if not path.is_file():
            return None
        try:
            raw = path.read_bytes()
            rows = list(csv.DictReader(io.StringIO(raw.decode("utf-8-sig"))))
        except (OSError, UnicodeError, csv.Error):
            return None
        quotes: dict[str, Quote] = {}
        warnings: dict[str, None] = {}
        fetched_times: list[datetime] = []
        for row in rows:
            symbol = (row.get("symbol") or "").strip()
            if not SYMBOL.fullmatch(symbol):
                warnings["invalid_symbol_rows_present"] = None
                continue
            if (row.get("snapshot_date") or "").strip() != day:
                warnings["snapshot_date_mismatch_rows_present"] = None
                continue
            if (row.get("snapshot_time") or "").strip() != slot:
                warnings["snapshot_slot_mismatch_rows_present"] = None
                continue
            if symbol in quotes:
                warnings["duplicate_symbol_rows_present"] = None
                continue
            fetched_at = (row.get("fetched_at") or "").strip()
            fetched = timestamp(fetched_at)
            if fetched and nominal <= fetched < nominal + FIVE_MINUTES:
                fetched_times.append(fetched)
            quotes[symbol] = Quote(
                symbol=symbol, name=(row.get("name") or "").strip(),
                price=number(row.get("price")), pre_close=number(row.get("pre_close")),
                volume=number(row.get("volume")), amount=number(row.get("amount")),
                quote_date=(row.get("quote_date") or "").strip(),
                quote_time=(row.get("quote_time") or "").strip(), fetched_at=fetched_at,
                source=(row.get("source") or "").strip(),
                basis=(row.get("price_basis") or "").strip(), snapshot_slot=slot,
                pct_chg=number(row.get("pct_chg")),
                change=number(row.get("change")), open=number(row.get("open")),
                high=number(row.get("high")), low=number(row.get("low")),
            )
        if len(quotes) != len(rows):
            warnings["some_rows_unavailable"] = None
        ready = max(fetched_times).isoformat() if fetched_times else None
        result = Frame(day, slot, quotes, hashlib.sha256(raw).hexdigest(), len(rows), list(warnings), ready)
        self._frames[key] = result
        return result

    def is_open(self, day: str) -> bool:
        return self._trading_calendar().get(day, False)

    def previous_day(self, day: str) -> str | None:
        return max((d for d, opened in self._trading_calendar().items() if opened and d < day), default=None)

    def _trading_calendar(self) -> dict[str, bool]:
        if self._calendar is None:
            path = self.data / "calendar" / "a_share_trading_calendar.csv"
            self._calendar = {
                row["date"].strip(): (row.get("is_open") or "").strip().lower() == "true"
                for row in self._csv_rows(path) if row.get("date")
            }
        return self._calendar

    @staticmethod
    def _csv_rows(path: Path) -> list[dict[str, str]]:
        try:
            with path.open(encoding="utf-8-sig", newline="") as stream:
                return list(csv.DictReader(stream))
        except (OSError, UnicodeError, csv.Error):
            return []

    def benchmark(self, day: str, inclusive: bool = False) -> dict[str, Any] | None:
        try:
            last = deque(dated_rows(self.data / "benchmark" / "hs300.csv", day, inclusive=inclusive), maxlen=1)
        except (OSError, UnicodeError, ValueError, csv.Error):
            return None
        if not last:
            return None
        row = last[0]
        return {"date": row["date"], "close": decimal_text(number(row.get("close"))),
                "pct_chg": decimal_text(number(row.get("pct_chg")))}

    def screen(self, day: str, slot: str, holdings: dict[str, Any] | list[dict[str, Any]],
               watchlist: list[str]) -> dict[str, Any]:
        if slot not in DECISION_SLOTS:
            raise ValueError(f"unsupported decision slot: {slot}")
        current = self.frame(day, slot)
        prior_day = self.previous_day(day)
        previous = self.frame(prior_day, slot) if current and prior_day else None
        intraday = self.frame(day, SLOTS[SLOTS.index(slot) - 1]) if current else None
        visible = visible_quotes(current)
        previous_visible = scoped(visible_quotes(previous))
        intraday_visible = visible_quotes(intraday, timestamp(current.decision_at or "")) if current else {}
        scoped_count = len(scoped(current.rows)) if current else 0
        accepted = [q for q in scoped(visible).values() if q.pct is not None and q.amount is not None]
        up = sum(quote.pct > 0 for quote in accepted)
        down = sum(quote.pct < 0 for quote in accepted)
        amount = sum((quote.amount for quote in accepted), ZERO)
        previous_amount = sum((quote.amount or ZERO for quote in previous_visible.values()), ZERO)
        unavailable = scoped_count - len(accepted)
        warnings = list(current.warnings) if current else ["snapshot_missing"]
        if current and previous is None:
            warnings.append("previous_same_slot_snapshot_missing")
        if unavailable:
            warnings.append(f"unavailable_or_post_cutoff_quotes:{unavailable}")
        bands = [(quote, limits(quote)) for quote in accepted]
        return {
            "day": day, "slot": slot, "evidence": current.evidence() if current else None,
            "temperature": {
                "total": len(accepted), "up": up, "down": down,
                "flat": len(accepted) - up - down, "unavailable": unavailable,
                "out_of_scope": len(current.rows) - scoped_count if current else 0,
                "limit_up": sum(bool(band and quote.price == band[1]) for quote, band in bands),
                "limit_down": sum(bool(band and quote.price == band[0]) for quote, band in bands),
                "average_pct": decimal_text(average([q.pct for q in accepted], len(accepted))) if accepted else None,
                "amount": decimal_text(amount) if current else None,
                "previous_day": prior_day,
                "previous_same_slot_amount": decimal_text(previous_amount) if previous else None,
                "amount_ratio": decimal_text(ratio(amount, previous_amount)),
            },
            "radar": radar(bands, previous_visible, intraday_visible),
            "holdings": holding_view(holdings, visible, day) if current else [],
            "watchlist": [watchlist_card(code, current.rows.get(code), visible.get(code), day)
                          for code in watchlist] if current else [],
            "benchmark": self.benchmark(day), "warnings": warnings,
        }

    def inspect(self, day: str, slot: str, symbols: list[str]) -> dict[str, Any]:
        if slot not in DECISION_SLOTS:
            raise ValueError(f"inspection is only available at decision slots: {slot}")
        result: dict[str, Any] = {"day": day, "slot": slot, "valuation_date": None, "symbols": {}, "warnings": []}
        current = self.frame(day, slot)
        if current is None:
            result["warnings"].append("snapshot_missing")
            return result
        selected = list(dict.fromkeys(map(str, symbols)))[:5]
        if len(symbols) > 5:
            result["warnings"].append("inspect_symbols_capped_at_5")
        valuation_day, valuations = self._valuation(day)
        result["valuation_date"] = valuation_day
        cutoff = timestamp(current.decision_at or "")
        trajectory: dict[str, list[dict[str, Any]]] = {code: [] for code in selected}
        for prior_slot in SLOTS[:SLOTS.index(slot) + 1] if cutoff and selected else ():
            frame = current if prior_slot == slot else self.frame(day, prior_slot)
            if frame is None:
                continue
            ready = timestamp(frame.decision_at or "")
            for code in selected:
                quote = frame.rows.get(code)
                if ready and quote_issue(quote, day) is None and timestamp(quote.fetched_at) <= min(cutoff, ready):
                    trajectory[code].append({
                        "snapshot_slot": prior_slot, "quote_time": quote.quote_time,
                        "price": decimal_text(quote.price),
                    })
        for code in selected:
            quote = current.rows.get(code)
            issue = quote_issue(quote, day)
            if issue is None and (not cutoff or timestamp(quote.fetched_at) > cutoff):
                issue = "quote_after_decision_cutoff"
            result["symbols"][code] = (
                {"warnings": [issue]} if issue else
                self._inspection(code, day, quote, valuation_day, valuations.get(code), trajectory[code])
            )
        return result

    def _valuation(self, day: str) -> tuple[str | None, dict[str, dict[str, str]]]:
        if day not in self._valuations:
            paths = (self.data / "metadata" / "valuation_snapshots").glob("*.csv")
            cutoff = day.replace("-", "")
            path = max((p for p in paths if re.fullmatch(r"[0-9]{8}", p.stem) and p.stem < cutoff), default=None)
            if path is None:
                self._valuations[day] = (None, {})
            else:
                stamp = path.stem
                prior = f"{stamp[:4]}-{stamp[4:6]}-{stamp[6:]}"
                rows = self._csv_rows(path)
                self._valuations[day] = (prior, {
                    row["symbol"].strip(): row for row in rows
                    if SYMBOL.fullmatch((row.get("symbol") or "").strip())
                    and (row.get("trade_date") or "").strip()
                    == (row.get("quote_date") or "").strip() == prior
                })
        return self._valuations[day]

    def _history(self, code: str, day: str) -> tuple[list[dict[str, str]], list[str]]:
        rows: deque[dict[str, str]] = deque(maxlen=20)
        try:
            for row in dated_rows(self.data / "daily" / f"{code}.csv", day):
                if issue := raw_daily_issue(row, code):
                    return [], [f"daily_history_{issue}"]
                rows.append(row)
        except FileNotFoundError:
            return [], ["daily_history_missing"]
        except (OSError, UnicodeError, csv.Error):
            return [], ["daily_history_unreadable"]
        except ValueError as error:
            return [], [f"daily_history_{error}"]
        return list(rows), []

    def _inspection(
        self, code: str, day: str, quote: Quote, valuation_day: str | None,
        valuation: dict[str, str] | None, trajectory: list[dict[str, Any]],
    ) -> dict[str, Any]:
        rows, warnings = self._history(code, day)
        closes = [number(row.get("close")) for row in rows]
        volumes = [number(row.get("volume")) for row in rows]
        ma5, ma20 = average(closes[-5:], 5), average(closes[-20:], 20)
        if ma5 is None or ma20 is None:
            warnings.append("insufficient_or_missing_daily_closes_for_moving_average")
        if valuation is None:
            warnings.append("prior_valuation_missing")
        return {
            "quote": quote.evidence(),
            "history": [{"date": row["date"], **{
                key: decimal_text(number(row.get(key))) for key in ("open", "high", "low", "close", "volume")
            }} for row in rows],
            "ma5": decimal_text(ma5), "ma20": decimal_text(ma20),
            "volume_ratio_5d": decimal_text(ratio(average(volumes[-5:], 5), average(volumes[-10:-5], 5))),
            "valuation": {"date": valuation_day, **{
                key: decimal_text(number((valuation or {}).get(key))) for key in ("pe", "pb")
            }},
            "trajectory": trajectory, "warnings": warnings,
        }

    def archive(self, day: str) -> list[dict[str, Any]]:
        return [{"available": True, **frame.evidence()} if (frame := self.frame(day, slot)) else
                {"day": day, "slot": slot, "available": False} for slot in SLOTS]

    def close(self, day: str, symbols: list[str]) -> dict[str, Any]:
        target = date.fromisoformat(day)
        now = datetime.now(BEIJING)
        if target > now.date() or (target == now.date() and now.time() < time(15)):
            raise ValueError("closing prices are available only after the trading day has closed")
        official = {code: self._official_close(code, day) for code in dict.fromkeys(map(str, symbols))}
        fallback = self.frame(day, "1455") if any(price is None for price in official.values()) else None
        visible = visible_quotes(fallback)
        result = {}
        for code, price in official.items():
            if price is not None:
                basis, source, warnings = "official_raw_daily_close", "daily", []
            elif code in visible:
                price = visible[code].price
                basis, source, warnings = (
                    "1455_snapshot_proxy", "snapshot",
                    ["official_raw_daily_close_unavailable"],
                )
            else:
                quote = fallback.rows.get(code) if fallback else None
                basis, source, warnings = "missing", None, [quote_issue(quote, day) or "close_unavailable"]
            result[code] = {"price": decimal_text(price), "basis": basis, "source": source,
                            "date": day, "warnings": warnings}
        return {"day": day, "symbols": result}

    def _official_close(self, code: str, day: str) -> Decimal | None:
        if not SYMBOL.fullmatch(code):
            return None
        try:
            for row in dated_rows(self.data / "daily" / f"{code}.csv", day, inclusive=True):
                if row["date"].strip() == day and raw_daily_issue(row, code) is None:
                    price = number(row.get("close"))
                    return price if price is not None and price > 0 else None
        except (OSError, UnicodeError, ValueError, csv.Error):
            return None
        return None


def scoped(quotes: dict[str, Quote]) -> dict[str, Quote]:
    return {code: quote for code, quote in quotes.items() if eligibility(code, quote.name) is None}


def quote_card(
    quote: Quote, amount_ratio: Decimal | None = None, move: Decimal | None = None,
) -> dict[str, Any]:
    return {
        **quote.evidence(),
        "amount_ratio": decimal_text(amount_ratio),
        "slot_change_pct": decimal_text(move),
    }


def radar(bands: list[tuple[Quote, tuple[Decimal, Decimal] | None]],
          previous: dict[str, Quote], intraday: dict[str, Quote]) -> dict[str, Any]:
    groups: dict[str, list[tuple[Decimal, dict[str, Any]]]] = {
        "active": [], "strength": [], "plunge": [], "rebound": [],
    }
    for quote, band in bands:
        groups["active"].append((quote.amount, quote_card(quote)))
        old, prior = previous.get(quote.symbol), intraday.get(quote.symbol)
        expansion = ratio(quote.amount, old.amount) if old else None
        move = (quote.price - prior.price) * 100 / prior.price if prior else None
        at_limit_up = bool(band and quote.price == band[1])
        if quote.pct > 0 and expansion is not None and expansion >= STRENGTH_RATIO and not at_limit_up:
            groups["strength"].append((quote.pct, quote_card(quote, expansion)))
        if quote.pct <= PLUNGE_PCT or (move is not None and move <= -SLOT_MOVE_PCT):
            groups["plunge"].append((move if move is not None else quote.pct, quote_card(quote, move=move)))
        if move is not None and move >= SLOT_MOVE_PCT:
            groups["rebound"].append((move, quote_card(quote, move=move)))
    return {
        **{key: [card for _, card in sorted(rows, key=lambda item: item[0], reverse=key != "plunge")[:10]]
           for key, rows in groups.items()},
        "thresholds": {"strength_amount_ratio_min": str(STRENGTH_RATIO),
                       "daily_plunge_pct_max": str(PLUNGE_PCT), "slot_move_pct_abs_min": str(SLOT_MOVE_PCT)},
    }


def watchlist_card(
    code: str, quote: Quote | None, visible: Quote | None, day: str,
) -> dict[str, Any]:
    if visible and eligibility(code, visible.name) is None:
        return quote_card(visible)
    reason = "quote_missing" if quote is None else (
        eligibility(code, quote.name) or quote_issue(quote, day) or "quote_after_decision_cutoff"
    )
    return {"symbol": code, "available": False, "reason": reason}


def holding_view(holdings: dict[str, Any] | list[dict[str, Any]],
                 quotes: dict[str, Quote], day: str) -> list[dict[str, Any]]:
    rows = holdings.items() if isinstance(holdings, dict) else (
        (str(row["symbol"]), row) for row in holdings if row.get("symbol")
    )
    result = []
    for code, value in rows:
        position = value if isinstance(value, dict) else {"qty": value}
        quote = quotes.get(code)
        qty = number(position.get("qty"))
        avg_cost = number(position.get("avg_cost"))
        if avg_cost is None:
            avg_cost = ratio(number(position.get("total_cost")), qty)
        market_value = quote.price * qty if quote and qty is not None else None
        pnl = market_value - avg_cost * qty if market_value is not None and avg_cost is not None else None
        result.append({
            "symbol": code, "name": quote.name if quote else "", "qty": decimal_text(qty),
            "sellable_qty": position.get("sellable_qty"), "price": decimal_text(quote.price) if quote else None,
            "market_value": decimal_text(market_value), "pnl": decimal_text(pnl),
            "quote_issue": quote_issue(quote, day),
        })
    return result
