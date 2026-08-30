"""Stock-screening helpers for QuantDesk (Phase 2 选股正确性).

This package centralizes the candidate-eligibility rules that were previously
missing or scattered:

- :mod:`quantdesk.screening.candidate_filters` — a single ST / delist / new-listing
  / suspension / one-word-limit eligibility gate, so the screener main path and
  its fallback universe apply identical exclusions (fixing the gap where the
  fallback universe applied none).

It is research-only: no network, no broker, not investment advice.
"""

from __future__ import annotations

from quantdesk.screening.candidate_filters import (
    EligibilityResult,
    evaluate_candidate_eligibility,
    filter_candidate_rows,
    is_excluded_name,
    is_one_word_limit_candidate,
)

__all__ = [
    "EligibilityResult",
    "evaluate_candidate_eligibility",
    "filter_candidate_rows",
    "is_excluded_name",
    "is_one_word_limit_candidate",
]
