"""Intraday observation notes. Looks, writes notes, never trades.

A 5-minute data refresh may call ``observe_session``. Notes accumulate for
the 20:20 evening cycle. This module must not write the ledger, plan, or
watchlist.
"""

from __future__ import annotations

import csv
import json
from datetime import datetime
from pathlib import Path
from typing import Any

from ai_trader.ledger import load_ledger, position_map
from ai_trader.paths import (
    DAYTIME_NOTES_PATH,
    DAYTIME_SESSION_PATH,
    PLAN_LATEST_PATH,
    CyclePaths,
    PROJECT_ROOT,
    assert_not_quantdesk_ledger,
)
from ai_trader.watchlist import load_watchlist, watch_names
from quantdesk.intraday_refresh.snapshot import LIMIT_PCT, SPOT_RELATIVE
from quantdesk.utils import atomic_write_json

NOTE_SCHEMA = "ai_trader_daytime_note_v1"
MAX_EVENTS_PER_ROUND = 10
POSITION_NOTE_PCT = 2.0


def _bucket(pct: float) -> str:
    sign = "up" if pct >= 0 else "down"
    mag = abs(pct)
    if mag >= LIMIT_PCT:
        size = "limit"
    elif mag >= 7:
        size = "7"
    elif mag >= 5:
        size = "5"
    elif mag >= 3:
        size = "3"
    elif mag >= POSITION_NOTE_PCT:
        size = "2"
    else:
        size = "flat"
    return f"{sign}:{size}"


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


def load_daytime_notes_for(as_of: str, *, notes_path: Path | None = None) -> list[dict[str, Any]]:
    path = assert_not_quantdesk_ledger(notes_path or DAYTIME_NOTES_PATH)
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
            note = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(note, dict) and str(note.get("as_of") or "")[:10] == str(as_of)[:10]:
            out.append(note)
    return out


def observe_session(
    project_root: Path | None = None,
    *,
    now: datetime | None = None,
    snapshot_rows: list[dict[str, Any]] | None = None,
    paths: CyclePaths | None = None,
    write: bool = True,
) -> dict[str, Any]:
    """Emit at most a handful of new observations. Never mutates trading state."""

    root = Path(project_root or PROJECT_ROOT).resolve()
    runtime = paths or CyclePaths()
    current = now or datetime.now()
    as_of = current.strftime("%Y-%m-%d")
    stamp = current.isoformat(timespec="seconds")
    rows = snapshot_rows if snapshot_rows is not None else _load_snapshot_rows(root)
    spots = _spot_index(rows)

    ledger = load_ledger(runtime.ledger)
    held = position_map(ledger)
    plan = _read_json(runtime.plan_latest if runtime.plan_latest else PLAN_LATEST_PATH)
    plan_symbols = {
        str(row.get("symbol") or "")
        for row in (plan.get("intents") or [])
        if isinstance(row, dict) and str(row.get("action") or row.get("side") or "buy").lower() in {"buy", "b"}
    }
    plan_symbols.discard("")
    watch = set(watch_names(load_watchlist(runtime.watchlist)))

    session = _read_json(runtime.daytime_session)
    if str(session.get("as_of") or "") != as_of:
        session = {"as_of": as_of, "seen": [], "last_pct": {}}
    seen = set(str(item) for item in (session.get("seen") or []))
    last_pct = {
        str(key): float(val)
        for key, val in dict(session.get("last_pct") or {}).items()
        if str(key)
    }

    events: list[dict[str, Any]] = []

    def _emit(event_key: str, text: str, *, symbol: str, kind: str) -> None:
        if event_key in seen or len(events) >= MAX_EVENTS_PER_ROUND:
            return
        seen.add(event_key)
        events.append(
            {
                "schema_version": NOTE_SCHEMA,
                "as_of": as_of,
                "at": stamp,
                "symbol": symbol,
                "kind": kind,
                "text": text,
                "research_only": True,
                "paper_only": True,
                "committed": False,
                "disclaimer": "盘中观察笔记，不是下单，不改账本。",
            }
        )

    for symbol, pos in held.items():
        spot = spots.get(symbol)
        if not spot:
            continue
        try:
            pct = float(spot.get("pct_chg") or 0)
            price = float(spot.get("price") or 0)
        except (TypeError, ValueError):
            continue
        name = str(spot.get("name") or symbol)
        bucket = _bucket(pct)
        key = f"{symbol}:position:{bucket}"
        prev = last_pct.get(symbol)
        last_pct[symbol] = pct
        if abs(pct) < POSITION_NOTE_PCT:
            continue
        if prev is not None and _bucket(prev) == bucket:
            continue
        _emit(
            key,
            f"持仓 {symbol} {name} 现价 {price:.2f}，今日 {pct:+.2f}%（观察，未改仓）。",
            symbol=symbol,
            kind="position_move",
        )

    watched = (plan_symbols | watch) - set(held)
    for symbol in sorted(watched):
        spot = spots.get(symbol)
        if not spot:
            continue
        try:
            pct = float(spot.get("pct_chg") or 0)
        except (TypeError, ValueError):
            continue
        if abs(pct) < LIMIT_PCT:
            continue
        name = str(spot.get("name") or symbol)
        side = "涨停附近" if pct > 0 else "跌停附近"
        where = "计划票" if symbol in plan_symbols else "自选"
        _emit(
            f"{symbol}:watch_limit",
            f"{where} {symbol} {name} {side}（{pct:+.2f}%），仅记录。",
            symbol=symbol,
            kind="watch_limit",
        )

    board_new = 0
    for row in rows:
        if board_new >= 3:
            break
        symbol = str(row.get("symbol") or "")
        if symbol in held or symbol in watched:
            continue
        try:
            pct = float(row.get("pct_chg") or 0)
        except (TypeError, ValueError):
            continue
        if pct < LIMIT_PCT:
            continue
        name = str(row.get("name") or symbol)
        key = f"{symbol}:board_limit"
        if key in seen:
            continue
        _emit(
            key,
            f"板上新涨停 {symbol} {name}（{pct:+.2f}%），未纳入自选，仅白天看见。",
            symbol=symbol,
            kind="board_limit",
        )
        board_new += 1

    if write and events:
        notes_path = assert_not_quantdesk_ledger(runtime.daytime_notes)
        notes_path.parent.mkdir(parents=True, exist_ok=True)
        with notes_path.open("a", encoding="utf-8") as handle:
            for event in events:
                handle.write(json.dumps(event, ensure_ascii=False) + "\n")
    if write:
        atomic_write_json(
            assert_not_quantdesk_ledger(runtime.daytime_session),
            {"as_of": as_of, "seen": sorted(seen), "last_pct": last_pct},
        )

    return {
        "ok": True,
        "as_of": as_of,
        "snapshot_rows": len(rows),
        "new_notes": len(events),
        "events": events,
        "ledger_written": False,
        "plan_written": False,
        "research_only": True,
        "paper_only": True,
    }
