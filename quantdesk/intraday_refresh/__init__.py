"""Intraday data plane: dynamic scope, minute collection, health manifest.

Design boundaries (see the 2026-09-01 task brief):
- Full-A 1-minute history is never fetched. A cheap full-market *spot*
  snapshot may overwrite ``data/cache/spot_snapshot_latest.csv``.
- Minute scope is dynamic: AI positions + tonight's committed plan
  (critical), AI watchlist (high), research candidates (normal), plus a
  capped snapshot-mover expansion (limit / unusual / top amount).
- All AI/research source state is READ-ONLY.
- Writes go to ``data/intraday/1m/<symbol>.csv`` following the existing
  schema (symbol,datetime,date,time,open,high,low,close,volume,amount,
  source,updated_at,price_basis). Historical legal bars are never rewritten.
- Single process lock; a second instance returns ``already_running``.
- Fail-closed validation: zero/negative prices, inverted OHLC, future
  timestamps and duplicate (symbol, datetime) rows are rejected, never
  silently repaired. Provider failure never masquerades as fresh data.
- Non-trading time is reported as ``closed`` with zero provider requests.
"""

from quantdesk.intraday_refresh.collector import (
    collect_intraday_refresh,
    load_recent_bars,
    market_session_for,
    reconcile_abandoned_round,
    trading_day_today,
)
from quantdesk.intraday_refresh.scope import build_dynamic_scope
from quantdesk.intraday_refresh.snapshot import (
    refresh_full_market_snapshot,
    select_mover_symbols,
)

__all__ = [
    "build_dynamic_scope",
    "collect_intraday_refresh",
    "load_recent_bars",
    "market_session_for",
    "reconcile_abandoned_round",
    "refresh_full_market_snapshot",
    "select_mover_symbols",
    "trading_day_today",
]
