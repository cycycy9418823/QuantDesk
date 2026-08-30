"""Unified enriched daily-bar layer for QuantDesk.

This package is the single source of truth for:

- the canonical daily-bar column contract (:mod:`quantdesk.enriched.columns`),
- a single column-name normalizer that reconciles the 4+ historical
  normalizers (:mod:`quantdesk.enriched.normalize`), resolving the long-standing
  ``turnover`` (成交额 vs 换手率) semantic collision, and
- explicit forward/back/raw adjustment provenance with a "store raw + adjusted"
  convention and a single-adjustment consistency assertion
  (:mod:`quantdesk.enriched.adjust`).

Design (mirrors the tickflow精华 "选股/回测/监控按列名取信号" idea, adapted to
this project's pandas stack):

- Pure functions, no IO beyond what callers pass in, no imports from heavy
  ``quantdesk`` modules, so it can be imported anywhere without circular-import
  risk.
- Additive: existing modules keep working; new/refactored callers converge on
  this layer rather than each re-implementing column handling.
"""

from __future__ import annotations

from quantdesk.enriched.columns import (
    ADJUSTMENT_MODES,
    ALIAS_TO_CANONICAL,
    CANONICAL_DAILY_COLUMNS,
    CANONICAL_NUMERIC_COLUMNS,
    COLUMN_ALIASES,
    REQUIRED_DAILY_COLUMNS,
    AdjustmentMode,
    canonical_name,
    normalize_adjust_token,
)
from quantdesk.enriched.normalize import (
    normalize_daily_frame,
    normalized_value,
)
from quantdesk.enriched.adjust import (
    ADJUST_HFQ,
    ADJUST_QFQ,
    ADJUST_RAW,
    ADJUST_UNKNOWN,
    AdjustConsistencyError,
    adjust_of_frame,
    assert_adjust_consistency,
    infer_adjust_from_source,
    is_valid_adjust,
    normalize_adjust,
)
from quantdesk.enriched.narrow_store import (
    NARROW_STORE_COLUMNS,
    read_narrow_parquet,
    to_narrow_frame,
    write_narrow_parquet,
)

__all__ = [
    # columns
    "ADJUSTMENT_MODES",
    "ALIAS_TO_CANONICAL",
    "CANONICAL_DAILY_COLUMNS",
    "CANONICAL_NUMERIC_COLUMNS",
    "COLUMN_ALIASES",
    "REQUIRED_DAILY_COLUMNS",
    "AdjustmentMode",
    "canonical_name",
    "normalize_adjust_token",
    # normalize
    "normalize_daily_frame",
    "normalized_value",
    # adjust
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
    # narrow store
    "NARROW_STORE_COLUMNS",
    "read_narrow_parquet",
    "to_narrow_frame",
    "write_narrow_parquet",
]
