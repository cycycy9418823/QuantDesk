"""A small personal watchlist. This is what a newbie actually looks at."""

from __future__ import annotations

import json
from copy import deepcopy
from datetime import date
from typing import Any

from ai_trader.mandate import DEFAULT_MANDATE
from ai_trader.paths import WATCHLIST_PATH, assert_not_quantdesk_ledger
from quantdesk.utils import atomic_write_json

SCHEMA = "ai_trader_watchlist_v1"


def empty_watchlist() -> dict[str, Any]:
    return {"schema_version": SCHEMA, "items": []}


def load_watchlist(path: Any = None) -> dict[str, Any]:
    target = assert_not_quantdesk_ledger(path or WATCHLIST_PATH)
    if not target.is_file():
        return empty_watchlist()
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return empty_watchlist()
    if not isinstance(payload, dict):
        return empty_watchlist()
    payload.setdefault("items", [])
    return payload


def save_watchlist(book: dict[str, Any], path: Any = None) -> None:
    target = assert_not_quantdesk_ledger(path or WATCHLIST_PATH)
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = deepcopy(book)
    payload["schema_version"] = SCHEMA
    atomic_write_json(target, payload)


def watch_names(book: dict[str, Any]) -> list[str]:
    names: list[str] = []
    for row in book.get("items") or []:
        if isinstance(row, dict) and row.get("symbol"):
            symbol = str(row["symbol"])
            if symbol not in names:
                names.append(symbol)
    return names


def touch_watchlist(
    book: dict[str, Any],
    symbols: list[str],
    *,
    as_of: str,
    reason: str,
    keep: list[str] | None = None,
    limit: int | None = None,
) -> dict[str, Any]:
    cap = int(limit or DEFAULT_MANDATE.max_watchlist)
    kept = {str(item) for item in (keep or []) if item}
    by_symbol: dict[str, dict[str, Any]] = {}
    for row in book.get("items") or []:
        if isinstance(row, dict) and row.get("symbol"):
            by_symbol[str(row["symbol"])] = dict(row)
    for symbol in symbols:
        code = str(symbol or "")
        if not code:
            continue
        existing = by_symbol.get(code, {"symbol": code, "added_on": as_of})
        existing["last_seen"] = as_of
        existing["reason"] = reason
        by_symbol[code] = existing
    next_items: list[dict[str, Any]] = []
    for code, row in by_symbol.items():
        last = str(row.get("last_seen") or row.get("added_on") or "")
        if code in kept or (last and _calendar_gap(last, as_of) <= 10):
            next_items.append(row)
    next_items.sort(key=lambda row: str(row.get("last_seen") or ""), reverse=True)
    pinned = [row for row in next_items if str(row.get("symbol")) in kept]
    others = [row for row in next_items if str(row.get("symbol")) not in kept]
    merged = pinned + others
    out = dict(book)
    out["items"] = merged[:cap]
    return out


def _calendar_gap(start: str, end: str) -> int:
    try:
        left = date.fromisoformat(str(start)[:10])
        right = date.fromisoformat(str(end)[:10])
    except ValueError:
        return 0
    return abs((right - left).days)
