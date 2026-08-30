"""Own paper ledger. Never shares identity with QuantDesk auto_paper_portfolio."""

from __future__ import annotations

import json
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ai_trader.mandate import BOUNDARY, DEFAULT_MANDATE, TraderMandate
from ai_trader.paths import LEDGER_PATH, assert_not_quantdesk_ledger, ensure_runtime_dirs
from quantdesk.utils import atomic_write_json

LEDGER_SCHEMA = "ai_trader_ledger_v1"


def session_days_held(acquired_on: str, as_of: str) -> int:
    """Trading sessions a position has been held for.

    Lives here rather than in ``overnight`` so ``trader`` can use it too: exit
    decisions and closed-trade attribution both need it, and importing back from
    ``overnight`` would be circular.
    """

    import pandas as pd

    try:
        start = pd.Timestamp(str(acquired_on)[:10])
        end = pd.Timestamp(str(as_of)[:10])
    except (TypeError, ValueError):
        return 0
    if pd.isna(start) or pd.isna(end) or end < start:
        return 0
    return max(int(len(pd.bdate_range(start, end)) - 1), 0)


def _now() -> str:
    return datetime.now(timezone.utc).astimezone().replace(microsecond=0).isoformat()


def empty_ledger(mandate: TraderMandate = DEFAULT_MANDATE, *, created_at: str | None = None) -> dict[str, Any]:
    stamp = created_at or _now()
    return {
        "schema_version": LEDGER_SCHEMA,
        "ledger_id": mandate.ledger_id,
        "mandate": mandate.to_dict(),
        "created_at": stamp,
        "updated_at": stamp,
        "as_of": None,
        "initial_cash": mandate.initial_cash,
        "cash": mandate.initial_cash,
        "positions": [],
        "trades": [],
        "equity": mandate.initial_cash,
        "market_value": 0.0,
        "unrealized_pnl": 0.0,
        "realized_pnl": 0.0,
        "warnings": [],
        "boundary": dict(BOUNDARY),
    }


def load_ledger(path: Path | None = None) -> dict[str, Any]:
    target = assert_not_quantdesk_ledger(path or LEDGER_PATH)
    if not target.is_file():
        return empty_ledger()
    payload = json.loads(target.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("ai_trader ledger is not an object")
    if str(payload.get("ledger_id") or "") != DEFAULT_MANDATE.ledger_id:
        raise ValueError("ai_trader ledger_id mismatch; refusing to load foreign book")
    return payload


def save_ledger(ledger: dict[str, Any], path: Path | None = None) -> Path:
    target = assert_not_quantdesk_ledger(path or LEDGER_PATH)
    ensure_runtime_dirs()
    payload = deepcopy(ledger)
    payload["updated_at"] = _now()
    payload["boundary"] = dict(BOUNDARY)
    atomic_write_json(target, payload)
    return target


def init_ledger(*, reset: bool = False, path: Path | None = None) -> dict[str, Any]:
    target = path or LEDGER_PATH
    if target.is_file() and not reset:
        return load_ledger(target)
    ledger = empty_ledger()
    save_ledger(ledger, target)
    return ledger


def position_map(ledger: dict[str, Any]) -> dict[str, dict[str, Any]]:
    rows = ledger.get("positions") if isinstance(ledger, dict) else []
    out: dict[str, dict[str, Any]] = {}
    for row in rows or []:
        if isinstance(row, dict) and row.get("symbol"):
            out[str(row["symbol"])] = dict(row)
    return out


def mark_to_market(
    ledger: dict[str, Any],
    prices: dict[str, float],
    *,
    as_of: str,
) -> dict[str, Any]:
    market_value = 0.0
    unrealized = 0.0
    warnings: list[str] = list(ledger.get("warnings") or [])
    next_positions: list[dict[str, Any]] = []
    for row in ledger.get("positions") or []:
        if not isinstance(row, dict):
            continue
        symbol = str(row.get("symbol") or "")
        qty = int(row.get("quantity") or 0)
        avg = float(row.get("avg_cost") or 0.0)
        price = prices.get(symbol)
        stale_mark = False
        if (price is None or price <= 0) and qty > 0:
            # A position missing from tonight's price dict has NOT become
            # worthless. Writing it to zero silently vaporises equity: a book of
            # cash 20,000 plus two holdings worth 53,000 reported equity 31,000
            # instead of 73,000, i.e. a 42,000 loss that never happened, in the
            # very number used to judge the trader. A suspended name carries its
            # last known mark, which is also ordinary accounting practice.
            fallback = float(row.get("market_price") or 0.0) or avg
            if fallback > 0:
                price = fallback
                stale_mark = True
        if price is None or price <= 0 or qty <= 0:
            warnings.append(f"missing_mark_price:{symbol}")
            marked = dict(row)
            marked["market_price"] = None
            marked["market_value"] = 0.0
            marked["unrealized_pnl"] = 0.0
            next_positions.append(marked)
            continue
        value = round(price * qty, 4)
        pnl = round((price - avg) * qty, 4)
        market_value += value
        unrealized += pnl
        marked = dict(row)
        marked["market_price"] = round(price, 4)
        marked["market_value"] = value
        marked["unrealized_pnl"] = pnl
        marked["weight"] = 0.0
        if stale_mark:
            # Surfaced rather than hidden: the value is carried, not observed.
            marked["stale_mark"] = True
            warnings.append(f"stale_mark_price:{symbol}")
        next_positions.append(marked)
    cash = float(ledger.get("cash") or 0.0)
    equity = round(cash + market_value, 4)
    for row in next_positions:
        value = float(row.get("market_value") or 0.0)
        row["weight"] = round(value / equity, 6) if equity > 0 else 0.0
    updated = deepcopy(ledger)
    updated["as_of"] = as_of
    updated["positions"] = next_positions
    updated["cash"] = round(cash, 4)
    updated["market_value"] = round(market_value, 4)
    updated["equity"] = equity
    updated["unrealized_pnl"] = round(unrealized, 4)
    updated["warnings"] = warnings[-50:]
    return updated
