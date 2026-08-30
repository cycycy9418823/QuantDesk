"""Explicit price-adjustment provenance for QuantDesk daily bars.

Background
----------
Today the on-disk ``data/daily/*.csv`` files carry **no** adjustment column, the
three-tier directory layout (``raw`` / ``forward_adjusted`` / ``back_adjusted``)
does not exist, yet 278 ``{symbol}_qfq.parquet`` forward-adjusted files sit
alongside the raw CSVs.  Consumers (backtest, paper, screener) implicitly assume
"whatever is in data/daily is usable", so the adjustment convention is in
practice *unknown* — which silently corrupts price-based math on ex-dividend
days (false gaps, wrong limit-up/down on adjusted prices).

This module makes the adjustment convention **explicit** rather than guessed:

- :data:`ADJUST_QFQ` / :data:`ADJUST_HFQ` / :data:`ADJUST_RAW` / :data:`ADJUST_UNKNOWN`
- :func:`assert_adjust_consistency` — fail loud when a frame mixes adjustment
  conventions, or when a price-sensitive operation receives an unknown one.
- :func:`infer_adjust_from_source` — conservative source→convention inference,
  reusing the legacy rule from ``daily_price_providers`` but returning
  :data:`ADJUST_UNKNOWN` instead of silently blank.

It deliberately does **not** invent adjustment factors.  When a true factor
table is required, use the audited helpers in
``quantdesk.data_truth.price_adjustment_consistency`` /
``quantdesk.data_truth.adjustment_factor_audit`` (which correctly flag their
output as research-grade proxy, not formal truth).

Read/compute only; no IO side effects beyond what the caller passes in.
"""

from __future__ import annotations

import pandas as pd

ADJUST_QFQ = "qfq"      # 前复权 (forward-adjusted)
ADJUST_HFQ = "hfq"      # 后复权 (back-adjusted)
ADJUST_RAW = "raw"      # 不复权 (unadjusted) — required for limit-up/down checks
ADJUST_UNKNOWN = "unknown"  # 口径未知 — price math must treat as untrusted

_VALID = {ADJUST_QFQ, ADJUST_HFQ, ADJUST_RAW, ADJUST_UNKNOWN}


class AdjustConsistencyError(ValueError):
    """Raised when adjustment conventions conflict or are unknown where required."""


def infer_adjust_from_source(source: object) -> str:
    """Conservatively infer the adjustment convention from a source tag.

    Mirrors the legacy ``_infer_adjust_from_source`` rule but returns
    :data:`ADJUST_UNKNOWN` (empty) rather than guessing for unrecognised
    sources, so the unknown case stays visible instead of masquerading as raw.
    """

    text = str(source or "").strip().lower()
    if not text:
        return ADJUST_UNKNOWN
    if "qfq" in text or "forward" in text or "前复权" in text:
        return ADJUST_QFQ
    if "hfq" in text or "backward" in text or "back_adj" in text or "后复权" in text:
        return ADJUST_HFQ
    if "tencent" in text:
        # tencent daily is delivered forward-adjusted in this project.
        return ADJUST_QFQ
    if text.startswith("current_project_default:") or text == "akshare_daily" or "raw" in text:
        return ADJUST_RAW
    return ADJUST_UNKNOWN


def normalize_adjust(value: object) -> str:
    """Coerce any adjust marker to a valid token (see :mod:`.columns`)."""

    from quantdesk.enriched.columns import normalize_adjust_token

    return normalize_adjust_token(value)


def adjust_of_frame(df: pd.DataFrame) -> str:
    """Return the single adjustment convention of *df*, or raise on conflict.

    An empty/unknown set collapses to :data:`ADJUST_UNKNOWN`.
    """

    if df is None or len(df) == 0 or "adjust" not in df.columns:
        return ADJUST_UNKNOWN
    tokens = {normalize_adjust(v) for v in df["adjust"].dropna().tolist()}
    tokens.discard(ADJUST_UNKNOWN)
    if len(tokens) > 1:
        raise AdjustConsistencyError(
            f"frame mixes adjustment conventions: {sorted(tokens)}"
        )
    return next(iter(tokens), ADJUST_UNKNOWN)


def assert_adjust_consistency(
    df: pd.DataFrame,
    *,
    expected: str | None = None,
    require_known: bool = False,
    context: str = "",
) -> str:
    """Assert *df* carries a single, optionally specific, adjustment convention.

    Parameters
    ----------
    df:
        Daily-bar frame (should already carry an ``adjust`` column; missing
        column is treated as unknown).
    expected:
        If given, the frame's convention must equal it.
    require_known:
        When True, an unknown convention is an error (use for limit-up/down or
        ex-dividend-sensitive math, where guessing is unsafe).
    context:
        Caller label for the error message.

    Returns
    -------
    str
        The resolved convention token.
    """

    actual = adjust_of_frame(df)
    where = f" [{context}]" if context else ""
    if require_known and actual == ADJUST_UNKNOWN:
        raise AdjustConsistencyError(
            f"price-adjustment convention is unknown but required{where}; "
            "do not run adjustment-sensitive math on untagged bars."
        )
    if expected is not None:
        expected_tok = normalize_adjust(expected)
        if actual != ADJUST_UNKNOWN and actual != expected_tok:
            raise AdjustConsistencyError(
                f"expected {expected_tok!r} bars but frame is {actual!r}{where}."
            )
    return actual


def is_valid_adjust(value: object) -> bool:
    return normalize_adjust(value) in _VALID


__all__ = [
    "ADJUST_HFQ",
    "ADJUST_QFQ",
    "ADJUST_RAW",
    "ADJUST_UNKNOWN",
    "AdjustConsistencyError",
    "adjust_of_frame",
    "assert_adjust_consistency",
    "infer_adjust_from_source",
    "is_valid_adjust",
    "normalize_adjust",
]
