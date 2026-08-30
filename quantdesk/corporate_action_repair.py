"""Repair unadjusted corporate-action cliffs in daily bars, using ``pct_chg``.

Every file in ``data/daily`` carries ``adjust=raw``, so the OHLC columns contain
split/dividend cliffs that are **not real price moves**, while the provider's own
``pct_chg`` column records the true economic return. Verified example:

    300259, 2018-05-23:  close 10.64 -> 4.44  (raw -58.3%)
    300259, 2018-05-23:  pct_chg = -7.31%     (the truth)

Measured over a 120-file sample on 2026-08-23: **84% of files** contain at least
one such cliff (527 cliff bars in 380,056). The worst are -90.5% (600621),
-69.2% (300429), -64.0% (300382).

Left unrepaired these poison every backward-looking indicator:
- moving averages are wrong for the whole lookback window after a cliff,
- RSI / Bollinger bands stay deformed for ~20 bars, manufacturing phantom
  "oversold" readings,
- percent-change momentum reads a -58% corporate action as a real crash,
- one-word-limit-down detectors misfire on the cliff.

``pct_chg`` is reliable ground truth (median ``|raw_ret - pct_chg|`` = 1.5e-5), so
it is used to back out a factor. A factor is applied **only** where raw and
stated returns disagree by more than ``CA_THRESHOLD``, which keeps ordinary
rounding noise from accumulating into a drift.

Anchoring matters, and it is what makes this safe to apply in production paths:
the series is adjusted **backwards from the last bar**, so the most recent bar
keeps its actually-quoted price and only history is restated. Anything that reads
just the latest price (execution fills, position sizing, mark-to-market, display)
is therefore unaffected by construction, while anything that looks back gets
correct returns.

The original quoted close is always preserved in ``raw_close``: tick size and
penny-stock status are properties of the price as actually quoted, never of the
restated series. Callers that need execution reality must read ``raw_close``.

Local, pure computation. Never mutates files on disk.
"""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd

# Disagreement above this is treated as a corporate action rather than rounding.
CA_THRESHOLD = 0.02
# A move this large that ``pct_chg`` cannot corroborate is an unrepairable cliff.
UNVERIFIABLE_MOVE = 0.35
RAW_CLOSE_COLUMN = "raw_close"

__all__ = [
    "CA_THRESHOLD",
    "RAW_CLOSE_COLUMN",
    "UNVERIFIABLE_MOVE",
    "repair_corporate_actions",
]


def repair_corporate_actions(frame: pd.DataFrame) -> tuple[pd.DataFrame, dict[str, Any]]:
    """Return (repaired copy, info) for a date-ascending daily frame.

    ``info`` carries ``ca_bars`` (how many cliffs were repaired), ``unverifiable``
    (a large move that ``pct_chg`` could not corroborate — the caller should treat
    the symbol as unusable rather than measure it), and ``reason``.

    The input is never mutated. When there is nothing to repair the frame is
    returned with ``raw_close`` added and ``ca_bars == 0``, so callers can apply
    this unconditionally.
    """

    info: dict[str, Any] = {"ca_bars": 0, "unverifiable": False, "reason": ""}
    if frame is None or frame.empty or "close" not in frame.columns:
        info["unverifiable"] = True
        info["reason"] = "missing_close_column"
        return (frame if frame is not None else pd.DataFrame()), info

    out = frame.copy()
    close = pd.to_numeric(out["close"], errors="coerce")
    out[RAW_CLOSE_COLUMN] = close
    if len(out) < 2:
        return out, info

    raw_ret = close / close.shift(1) - 1.0
    if "pct_chg" not in out.columns:
        # No ground truth column at all. That is not evidence of a corporate
        # action, so refusing the symbol outright would reject any data source
        # lacking pct_chg (including synthetic fixtures and first-day listings,
        # where a >35% move is legitimate). Flag it as unverified and let the
        # caller decide; fail-closed is reserved for the case below, where the
        # column exists and a large move contradicts or omits its own value.
        if bool((raw_ret.abs() > UNVERIFIABLE_MOVE).any()):
            info["unverified_large_move"] = True
            info["reason"] = "no_pct_chg_column_large_move_unchecked"
        return out, info

    stated = pd.to_numeric(out["pct_chg"], errors="coerce") / 100.0
    # A big move with no stated return to corroborate it cannot be repaired, and
    # silently measuring it would be worse than refusing.
    if bool(((raw_ret.abs() > UNVERIFIABLE_MOVE) & stated.isna() & raw_ret.notna()).any()):
        info["unverifiable"] = True
        info["reason"] = "unverifiable_large_move"
        return out, info

    is_ca = ((raw_ret - stated).abs() > CA_THRESHOLD) & raw_ret.notna() & stated.notna()
    info["ca_bars"] = int(is_ca.sum())
    if info["ca_bars"] == 0:
        return out, info

    ratio = ((1.0 + raw_ret) / (1.0 + stated)).where(is_ca, 1.0)
    ratio = ratio.replace([np.inf, -np.inf], 1.0).fillna(1.0)
    ratio_values = ratio.to_numpy(dtype=float)

    # Back-adjust: the last bar keeps its quoted price; history is restated so
    # that each adjusted step reproduces the stated return.
    factors = np.ones(len(out), dtype=float)
    for i in range(len(out) - 2, -1, -1):
        factors[i] = factors[i + 1] * ratio_values[i + 1]
    for column in ("open", "high", "low", "close"):
        if column in out.columns:
            out[column] = pd.to_numeric(out[column], errors="coerce") * factors
    return out, info
