"""Unified daily-bar fetch facade (Phase 0.5 数据源收敛).

Historically this codebase grew **three** parallel ways to pull a daily bar:

1. ``daily_price_providers.fetch_daily(provider_name, ...)`` — single-source
   dispatch, one provider at a time.
2. ``a_stock_data_adapter.get_daily_bars(...)`` — the high-level adapter that
   already chains local cache + network providers with failover.
3. the ``data_providers/*_chain.py`` provider chains.

Each normalised columns and inferred the adjustment convention slightly
differently, which is exactly the drift Phase 0 set out to remove.

This module is the **single new entry point** callers should converge on.  It
is deliberately a thin *facade* (strangler-fig): it delegates the actual
fetching + failover to the existing, battle-tested ``get_daily_bars`` adapter
and then runs the result through the unified :mod:`quantdesk.enriched` layer so
that *every* caller of this facade gets:

- one canonical column schema (``quantdesk.enriched.normalize_daily_frame``),
- the long-standing ``turnover`` (成交额 vs 换手率) collision already resolved,
- an explicit, asserted single adjustment convention on the way out
  (``quantdesk.enriched.adjust``), instead of each call site re-guessing.

It performs no network IO of its own and adds no new provider; it only unifies
the *shape and provenance* of whatever the adapter returns.  Read-only,
research/paper use only; never places orders.
"""

from __future__ import annotations

from typing import Any

import pandas as pd

from quantdesk.enriched import (
    ADJUST_UNKNOWN,
    AdjustConsistencyError,
    adjust_of_frame,
    infer_adjust_from_source,
    normalize_adjust,
    normalize_daily_frame,
)

__all__ = ["fetch_daily_unified", "UnifiedDailyResult"]


class UnifiedDailyResult(dict):
    """Lightweight dict subclass so callers can use either attribute-ish keys.

    Keeps full backward compatibility with the adapter's dict payload while
    advertising the extra unified fields we add (``frame``, ``adjust``).
    """


def _adapter_get_daily_bars(symbol: str, start_date: str, end_date: str, *, network: bool) -> dict[str, Any]:
    # Imported lazily to avoid importing the heavy adapter (and its provider
    # imports) at module import time / in environments where it is unused.
    from quantdesk.a_stock_data_adapter import get_daily_bars

    return get_daily_bars(symbol, start_date, end_date, network=network)


def _rows_to_frame(payload: dict[str, Any]) -> pd.DataFrame:
    data = payload.get("data")
    if isinstance(data, pd.DataFrame):
        return data.copy()
    if isinstance(data, list):
        return pd.DataFrame(data)
    # Some adapter shapes nest rows under "rows" / "bars".
    for key in ("rows", "bars", "daily", "frame"):
        value = payload.get(key)
        if isinstance(value, pd.DataFrame):
            return value.copy()
        if isinstance(value, list):
            return pd.DataFrame(value)
    return pd.DataFrame()


def _infer_adjust(payload: dict[str, Any], frame: pd.DataFrame) -> str:
    # 1) explicit adjust column wins (already asserted single by normalize layer)
    if not frame.empty and "adjust" in frame.columns:
        try:
            token = adjust_of_frame(frame)
        except AdjustConsistencyError:
            return ADJUST_UNKNOWN
        if token and token != ADJUST_UNKNOWN:
            return token
    # 2) explicit payload field
    for key in ("adjust", "adjustment", "adjust_mode"):
        token = normalize_adjust(payload.get(key))
        if token and token != ADJUST_UNKNOWN:
            return token
    # 3) infer from the source / provider tag
    for key in ("source", "provider", "data_source", "effective_source"):
        token = infer_adjust_from_source(payload.get(key))
        if token and token != ADJUST_UNKNOWN:
            return token
    # 4) infer from the last provider-chain step that succeeded
    chain = payload.get("provider_chain") or payload.get("chain") or []
    if isinstance(chain, list):
        for step in reversed(chain):
            if isinstance(step, dict):
                token = infer_adjust_from_source(step.get("name") or step.get("provider") or step.get("step"))
                if token and token != ADJUST_UNKNOWN:
                    return token
    return ADJUST_UNKNOWN


def fetch_daily_unified(
    symbol: str,
    start_date: str = "",
    end_date: str = "",
    *,
    network: bool = False,
) -> UnifiedDailyResult:
    """Fetch a daily bar through the single unified facade.

    Returns a dict (``UnifiedDailyResult``) that preserves the adapter payload
    keys and additionally guarantees:

    - ``frame``: a :class:`pandas.DataFrame` with the canonical enriched schema
      (or empty frame if nothing was fetched),
    - ``adjust``: the resolved adjustment convention token (``raw``/``qfq``/
      ``hfq``/``unknown``) — derived once here, not re-guessed by callers,
    - ``adjust_consistent``: ``True`` unless the frame mixed conventions
      (the enriched layer raised, which we catch and surface as a warning).

    The underlying failover (local cache → network providers) is unchanged; we
    only normalise the shape + provenance of the result.
    """

    payload = _adapter_get_daily_bars(symbol, start_date, end_date, network=network)
    if not isinstance(payload, dict):
        payload = {"status": "failed", "data": [], "warnings": ["adapter_returned_non_dict"]}

    result = UnifiedDailyResult(payload)
    warnings = list(result.get("warnings") or [])

    raw_frame = _rows_to_frame(payload)
    adjust_consistent = True
    try:
        frame = normalize_daily_frame(raw_frame) if not raw_frame.empty else raw_frame
        if not frame.empty and "adjust" in frame.columns:
            try:
                adjust_of_frame(frame)
            except AdjustConsistencyError as exc:
                adjust_consistent = False
                warnings.append(f"adjust_consistency_failed:{type(exc).__name__}:{exc}")
    except Exception as exc:  # AdjustConsistencyError or malformed input
        adjust_consistent = False
        warnings.append(f"normalize_failed:{type(exc).__name__}:{exc}")
        frame = raw_frame

    adjust_token = _infer_adjust(payload, frame)
    if adjust_token == ADJUST_UNKNOWN:
        warnings.append(f"adjust_convention_unknown:{symbol}")

    result["frame"] = frame
    result["adjust"] = adjust_token
    result["adjust_consistent"] = adjust_consistent
    result["warnings"] = warnings
    result.setdefault("symbol", symbol)
    result.setdefault("unified_facade", True)
    return result
