"""Intraday paper-intent recording. Looks at the plan's own preset levels,
records a priced "would-have-done" intent, and never trades.

A 5-minute data refresh may call ``observe_intents`` next to the existing
``observe_session`` note-taker. Every intent is anchored to a level the AI
itself declared in its evening plan (``preset_touch_price``,
``abandon_if_open_gap_pct``, ``planned_hold_days``); this module never invents
a level and never writes the ledger, plan, or watchlist. The 20:20 evening
cycle reads the day's intents as evidence and remains the only writer of
plan/ledger.
"""

from __future__ import annotations

import csv
import json
from datetime import datetime
from pathlib import Path
from typing import Any

from ai_trader.ledger import load_ledger, position_map
from ai_trader.paths import (
    DAYTIME_INTENTS_PATH,
    PLAN_LATEST_PATH,
    CyclePaths,
    PROJECT_ROOT,
    assert_not_quantdesk_ledger,
)
from ai_trader.watchlist import load_watchlist, watch_names
from quantdesk.intraday_refresh.snapshot import LIMIT_PCT, SPOT_RELATIVE

INTENT_SCHEMA = "ai_trader_daytime_intent_v1"
MAX_INTENTS_PER_ROUND = 8
SLOTS_RELATIVE = Path("data/snapshots")

_BOUNDARY = {
    "research_only": True,
    "paper_only": True,
    "no_broker": True,
    "is_real_order": False,
    "ledger_written": False,
    "plan_written": False,
    "watchlist_written": False,
}


def _fnum(value: Any) -> float | None:
    try:
        number = float(str(value).strip())
    except (TypeError, ValueError):
        return None
    return number if number == number else None


def _read_json(path: Path) -> dict[str, Any]:
    target = assert_not_quantdesk_ledger(path)
    if not target.is_file():
        return {}
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _load_snapshot_rows(root: Path) -> list[dict[str, Any]]:
    path = root / SPOT_RELATIVE
    if not path.is_file():
        return []
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return [dict(row) for row in csv.DictReader(handle)]


def _spot_index(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for row in rows:
        symbol = str(row.get("symbol") or "").strip()
        if len(symbol) == 6 and symbol.isdigit():
            out[symbol] = row
    return out


def _latest_slot_rows(root: Path, as_of: str) -> list[dict[str, Any]]:
    """Rows from the newest immutable 5-minute A0 slot of this date, if any.

    The exchange quote's ``high``/``low`` are day-cumulative, so the newest
    slot alone carries the day's high/low so far. Absence is not an error:
    the caller then falls back to the live spot snapshot.
    """

    directory = root / SLOTS_RELATIVE / str(as_of)[:10]
    if not directory.is_dir():
        return []
    files = sorted(directory.glob("*.csv"))
    if not files:
        return []
    try:
        with files[-1].open(encoding="utf-8-sig", newline="") as handle:
            return [dict(row) for row in csv.DictReader(handle)]
    except OSError:
        return []


def load_daytime_intents_for(as_of: str, *, intents_path: Path | None = None) -> list[dict[str, Any]]:
    path = assert_not_quantdesk_ledger(intents_path or DAYTIME_INTENTS_PATH)
    if not path.is_file():
        return []
    out: list[dict[str, Any]] = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    for line in lines:
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(record, dict) and str(record.get("as_of") or "")[:10] == str(as_of)[:10]:
            out.append(record)
    return out


def _intent_key(record: dict[str, Any]) -> str:
    return f"{record.get('symbol')}:{record.get('side')}:{record.get('precondition')}"


def observe_intents(
    project_root: Path | None = None,
    *,
    now: datetime | None = None,
    snapshot_rows: list[dict[str, Any]] | None = None,
    paths: CyclePaths | None = None,
    write: bool = True,
) -> dict[str, Any]:
    """Record at most a handful of new intents against the plan's own levels."""

    root = Path(project_root or PROJECT_ROOT).resolve()
    runtime = paths or CyclePaths()
    current = now or datetime.now()
    as_of = current.strftime("%Y-%m-%d")
    stamp = current.isoformat(timespec="seconds")
    rows = snapshot_rows if snapshot_rows is not None else _load_snapshot_rows(root)
    spots = _spot_index(rows)
    slots = _spot_index(_latest_slot_rows(root, as_of))

    plan = _read_json(runtime.plan_latest if runtime.plan_latest else PLAN_LATEST_PATH)
    declared = [item for item in (plan.get("intents") or []) if isinstance(item, dict)]
    ledger = load_ledger(runtime.ledger)
    held = position_map(ledger)
    watch = set(watch_names(load_watchlist(runtime.watchlist)))

    seen = {_intent_key(item) for item in load_daytime_intents_for(as_of, intents_path=runtime.daytime_intents)}
    events: list[dict[str, Any]] = []

    def _emit(
        *,
        symbol: str,
        name: str,
        side: str,
        precondition: str,
        level: float | None,
        price: float | None,
        pct: float | None,
        note: str,
        low: float | None = None,
        high: float | None = None,
    ) -> None:
        key = f"{symbol}:{side}:{precondition}"
        if key in seen or len(events) >= MAX_INTENTS_PER_ROUND:
            return
        seen.add(key)
        events.append(
            {
                "schema_version": INTENT_SCHEMA,
                "as_of": as_of,
                "at": stamp,
                "symbol": symbol,
                "name": name,
                "side": side,
                "precondition": precondition,
                "level": level,
                "observed_price": price,
                "observed_pct_chg": pct,
                "observed_low": low,
                "observed_high": high,
                "status": "intent_only",
                "fill_model": "none",
                "filled": False,
                "note": note,
                **_BOUNDARY,
            }
        )

    for item in declared:
        symbol = str(item.get("symbol") or "").strip()
        side = str(item.get("side") or "").lower()
        spot = spots.get(symbol) or slots.get(symbol)
        if not spot:
            continue
        name = str(spot.get("name") or symbol)
        price = _fnum(spot.get("price"))
        pct = _fnum(spot.get("pct_chg"))
        slot = slots.get(symbol) or {}
        day_low = _fnum(slot.get("low"))
        day_high = _fnum(slot.get("high"))
        if side == "buy":
            level = _fnum(item.get("preset_touch_price"))
            touched = level is not None and (
                (day_low is not None and day_low <= level) or (price is not None and price <= level)
            )
            if touched:
                _emit(
                    symbol=symbol, name=name, side="buy", precondition="preset_touch_price",
                    level=level, price=price, pct=pct, low=day_low, high=day_high,
                    note=f"当日最低 {day_low if day_low is not None else '-'}、现价 "
                         f"{price if price is not None else '-'} 已触及计划买点 {level:.2f}，"
                         "仅记意图，不按此价成交。",
                )
            gap_cap = _fnum(item.get("abandon_if_open_gap_pct"))
            ref_close = _fnum(item.get("ref_close"))
            day_open = _fnum(spot.get("open"))
            if gap_cap and ref_close and day_open and ref_close > 0:
                gap = (day_open / ref_close - 1.0) * 100.0
                if gap > gap_cap:
                    _emit(
                        symbol=symbol, name=name, side="buy", precondition="open_gap_abandon",
                        level=ref_close, price=day_open, pct=pct, low=day_low, high=day_high,
                        note=f"高开 {gap:+.2f}% 超过放弃线 {gap_cap:.1f}%，记放弃意图，不成交。",
                    )
        elif side == "sell":
            level = _fnum(item.get("preset_touch_price"))
            touched = level is not None and (
                (day_high is not None and day_high >= level) or (price is not None and price >= level)
            )
            if touched:
                _emit(
                    symbol=symbol, name=name, side="sell", precondition="preset_touch_price",
                    level=level, price=price, pct=pct, low=day_low, high=day_high,
                    note=f"当日最高 {day_high if day_high is not None else '-'}、现价 "
                         f"{price if price is not None else '-'} 已触及计划卖点 {level:.2f}，"
                         "仅记意图，不按此价成交。",
                )

    for symbol, position in held.items():
        planned = position.get("planned_hold_days") or position.get("planned_holding_days")
        try:
            planned_days = int(planned) if planned not in (None, "") else 0
            held_days = int(position.get("holding_days") or 0)
        except (TypeError, ValueError):
            continue
        if planned_days <= 0 or held_days < planned_days:
            continue
        spot = spots.get(symbol)
        name = str((spot or {}).get("name") or symbol)
        _emit(
            symbol=symbol, name=name, side="review", precondition="planned_hold_days",
            level=float(planned_days), price=_fnum((spot or {}).get("price")),
            pct=_fnum((spot or {}).get("pct_chg")),
            note=f"持仓已达计划天数 {held_days}/{planned_days}，记复核意图，不自动平仓。",
        )

    for symbol in sorted(watch - set(held)):
        spot = spots.get(symbol)
        if not spot:
            continue
        pct = _fnum(spot.get("pct_chg"))
        if pct is None or abs(pct) < LIMIT_PCT:
            continue
        name = str(spot.get("name") or symbol)
        side = "buy" if pct > 0 else "sell"
        _emit(
            symbol=symbol, name=name, side=side, precondition="watch_limit_touch",
            level=None, price=_fnum(spot.get("price")), pct=pct,
            note=f"自选 {symbol} {name} {'涨停附近' if pct > 0 else '跌停附近'}（{pct:+.2f}%），仅记意图。",
        )

    if write and events:
        intents_path = assert_not_quantdesk_ledger(runtime.daytime_intents)
        intents_path.parent.mkdir(parents=True, exist_ok=True)
        with intents_path.open("a", encoding="utf-8") as handle:
            for event in events:
                handle.write(json.dumps(event, ensure_ascii=False) + "\n")

    return {
        "ok": True,
        "as_of": as_of,
        "snapshot_rows": len(rows),
        "declared_intents": len(declared),
        "new_intents": len(events),
        "events": events,
        **_BOUNDARY,
    }


__all__ = ["INTENT_SCHEMA", "load_daytime_intents_for", "observe_intents"]
