"""Derive candidate lifecycle evidence from canonical disk evidence, fail-closed.

Round 15 fix for the intent-layer blocker.

Why this module exists
----------------------
``quantdesk/paper/lifecycle.py::evaluate_candidate_eligibility`` requires five
fields on a candidate's ``evidence`` mapping before a decision event may enter
an automated ``paper_watchlist`` state: ``eligible``, ``evidence_kind``,
``coverage``, ``pit_eligible`` and ``price_contract_eligible``. Production
candidate artifacts carried **no evidence block at all**, so the mapping
resolved to ``{}``, all five checks failed closed, ``effective_state`` fell to
``excluded_pending_research``, and ``build_paper_order_intent`` emitted
``lifecycle_state_not_fundable`` plus three sibling codes. Because quantity is
only computed when no reason exists, ``zero_lot_quantity`` followed, and the
risk gate's ``not intent.reason_codes`` condition then produced
``paper_intent_envelope_invalid`` and its siblings. Nine reason codes, one root
cause: the evidence block was never produced by anything in production.

The only prior production writer of these fields,
``quantdesk/paper/stage4_tri_account_replay.py``, hardcoded them as constants
(``eligible=True``, ``evidence_kind="formal"``, ``coverage=1.0``). That let
Stage 4 pass its own ``intent_not_fundable`` gate on evidence that was asserted
rather than proven. This module replaces that constant with a derivation, so
both the shadow path and Stage 4 answer the same question from the same disk
evidence.

What "formal" means here (ruling, 2026-08-08)
--------------------------------------------
``evidence_kind="formal"`` is granted only when **all five** criteria below are
proven from canonical artifacts. Each criterion records its own proof so a
reader can re-derive it:

1. ``close`` equals the canonical raw daily close at ``as_of`` (abs delta
   <= ``CLOSE_TOLERANCE``).
2. The canonical daily frame classifies as ``raw`` via the Price Data Contract
   (``classify_frame_price_basis``); any other basis is unprovable, never
   coerced.
3. ``data_latest_date <= as_of`` and ``is_target_date_covered`` is True.
4. ``sector`` resolves from that date's own dated membership snapshot, with
   both ``effective_from`` and ``observed_at`` at or before ``as_of``.
5. ``amount_ma20`` is independently reproducible from canonical rows using the
   upstream formula ``rolling(20, min_periods=20).mean()``.

``coverage`` is the proven fraction of those five criteria and
``minimum_coverage`` is 1.0, so anything short of full proof fails the
eligibility contract. A shortfall yields ``evidence_kind="partial_formal"``,
``eligible=False`` and per-criterion reason codes — the gate keeps failing
closed and ``would_pass`` stays 0 rather than resting on a stamped claim.

Boundaries
----------
Read-only: reads ``data/daily/`` and ``data/sector_history/`` and returns a
mapping. Writes nothing, mutates no ledger, and touches no T0
qualification-gate baseline file. Strictly as-of — no row after ``as_of`` is
ever read, so the derivation cannot introduce look-ahead.
"""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any, Mapping

import pandas as pd

from quantdesk.csv_io import read_csv_frame
from quantdesk.price_basis import classify_frame_price_basis

__all__ = [
    "CLOSE_TOLERANCE",
    "REQUIRED_CRITERIA",
    "derive_candidate_evidence",
]

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DAILY_DIR = PROJECT_ROOT / "data" / "daily"
SECTOR_HISTORY_DIR = PROJECT_ROOT / "data" / "sector_history"

# Absolute tolerance for matching a candidate's close against the canonical
# raw close. Canonical daily closes are stored at 2 decimals; 1e-6 admits
# float round-tripping only, never a genuinely different price.
CLOSE_TOLERANCE = 1e-6

# Relative tolerance for reproducing amount_ma20. The upstream producer keeps
# full float precision while candidate artifacts may round on serialisation.
AMOUNT_MA20_REL_TOLERANCE = 1e-6

# Rolling window and minimum periods, matching the upstream producer in
# quantdesk/auto_strategy_sandbox.py:156 exactly. A shorter effective window
# would silently change the meaning of the field.
AMOUNT_MA20_WINDOW = 20
AMOUNT_MA20_MIN_PERIODS = 20

REQUIRED_CRITERIA = (
    "close_matches_canonical_raw",
    "price_basis_raw",
    "as_of_clean",
    "sector_dated_membership",
    "amount_ma20_reproducible",
)

EVIDENCE_KIND_FORMAL = "formal"
EVIDENCE_KIND_PARTIAL = "partial_formal"
MINIMUM_COVERAGE = 1.0


def _finite(value: Any) -> float | None:
    """Coerce to a finite float, refusing bools and null-like sentinels."""

    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _iso_day(value: Any) -> str | None:
    text = str(value or "").strip()
    return text[:10] if len(text) >= 10 else None


def _candidate_close(candidate: Mapping[str, Any]) -> float | None:
    """Read the candidate's own reference close.

    Checks the projected top-level field first, then the nested ``source_item``
    the strategy row carries. Both are the candidate's own claim; the canonical
    frame is what proves or refutes it.
    """

    source_item = candidate.get("source_item")
    nested = source_item if isinstance(source_item, Mapping) else {}
    for holder in (candidate, nested):
        for key in ("close", "latest_price", "price"):
            number = _finite(holder.get(key))
            if number is not None and number > 0:
                return number
    return None


def _rows_at_or_before(frame: pd.DataFrame, as_of: str) -> pd.DataFrame:
    """Return canonical rows dated at or before ``as_of``, chronologically.

    This is the single as-of choke point: every criterion downstream reads only
    from this frame, so no future row can reach the derivation.
    """

    if frame.empty or "date" not in frame.columns:
        return frame.iloc[0:0]
    dates = frame["date"].astype(str).str[:10]
    subset = frame.loc[dates <= as_of].copy()
    subset["__day"] = dates.loc[subset.index]
    return subset.sort_values("__day", kind="mergesort")


def _cache_key(prefix: str, path: Path) -> str:
    try:
        resolved = path.resolve(strict=False)
    except (OSError, RuntimeError):
        resolved = path
    return f"{prefix}:{str(resolved).casefold()}"


def _load_membership(
    as_of: str,
    *,
    history_dir: Path,
    source_cache: dict[str, Any] | None = None,
) -> tuple[dict[str, dict], str | None]:
    """Load the dated sector membership captured on ``as_of`` itself.

    Only that date's own snapshot is consulted. Reading a neighbouring date's
    capture would substitute one day's membership for another's, which is the
    exact look-ahead the PIT archive exists to prevent.
    """

    snapshot_path = Path(history_dir) / as_of / "snapshot.json"
    cache = source_cache if source_cache is not None else {}
    cache_key = _cache_key("candidate_lifecycle_membership", snapshot_path)
    if cache_key in cache:
        cached = cache.get(cache_key)
        if isinstance(cached, Mapping):
            index = cached.get("index")
            source_path = cached.get("source_path")
            if isinstance(index, Mapping):
                return {
                    str(symbol): dict(row)
                    for symbol, row in index.items()
                    if isinstance(row, Mapping)
                }, (str(source_path) if source_path else None)
        return {}, None
    if not snapshot_path.exists():
        cache[cache_key] = {"index": {}, "source_path": None}
        return {}, None
    try:
        payload = json.loads(snapshot_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        cache[cache_key] = {"index": {}, "source_path": None}
        return {}, None
    rows = payload.get("sector_membership")
    if not isinstance(rows, list):
        cache[cache_key] = {"index": {}, "source_path": None}
        return {}, None
    index: dict[str, dict] = {}
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        symbol = str(row.get("symbol") or "").strip()
        if symbol:
            index[symbol] = dict(row)
    try:
        relative = snapshot_path.relative_to(PROJECT_ROOT)
        source_path = str(relative).replace("\\", "/")
    except ValueError:
        source_path = str(snapshot_path).replace("\\", "/")
    cache[cache_key] = {"index": index, "source_path": source_path}
    return index, source_path


def _load_daily_frame(
    daily_path: Path,
    *,
    source_cache: dict[str, Any] | None = None,
) -> pd.DataFrame:
    cache = source_cache if source_cache is not None else {}
    cache_key = _cache_key("candidate_lifecycle_daily", daily_path)
    if cache_key in cache:
        cached = cache.get(cache_key)
        return cached if isinstance(cached, pd.DataFrame) else pd.DataFrame()
    frame = read_csv_frame(daily_path)
    cache[cache_key] = frame
    return frame


def derive_candidate_evidence(
    candidate: Mapping[str, Any],
    *,
    as_of: str,
    daily_dir: Path | str | None = None,
    sector_history_dir: Path | str | None = None,
    source_cache: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Derive a candidate's lifecycle evidence from canonical disk evidence.

    Returns the five fields ``evaluate_candidate_eligibility`` consumes, plus a
    ``derivation`` block recording each criterion's verdict and proof so the
    result is re-derivable rather than asserted. Any criterion that cannot be
    proven leaves ``eligible=False`` and ``evidence_kind="partial_formal"``, so
    the caller keeps failing closed.
    """

    daily_root = Path(daily_dir) if daily_dir is not None else DAILY_DIR
    history_root = (
        Path(sector_history_dir) if sector_history_dir is not None else SECTOR_HISTORY_DIR
    )
    as_of_day = _iso_day(as_of)
    symbol = str(candidate.get("symbol") or candidate.get("code") or "").strip()

    criteria: dict[str, dict[str, Any]] = {}
    reason_codes: list[str] = []
    source_paths: list[str] = []

    def record(name: str, proven: bool, **proof: Any) -> None:
        criteria[name] = {"proven": bool(proven), **proof}
        if not proven:
            reason_codes.append(f"candidate_evidence_unproven:{name}")

    if not symbol or as_of_day is None:
        for name in REQUIRED_CRITERIA:
            record(name, False, reason="symbol_or_as_of_missing")
        return _assemble(criteria, reason_codes, source_paths, symbol=symbol, as_of=as_of_day)

    # --- canonical daily frame (single as-of choke point) -----------------
    daily_path = None
    for suffix in (".csv", ".CSV"):
        probe = daily_root / f"{symbol}{suffix}"
        if probe.exists() and probe.is_file():
            daily_path = probe
            break
    if daily_path is None:
        for name in REQUIRED_CRITERIA:
            record(name, False, reason="canonical_daily_missing")
        return _assemble(criteria, reason_codes, source_paths, symbol=symbol, as_of=as_of_day)

    try:
        relative_daily = str(daily_path.relative_to(PROJECT_ROOT)).replace("\\", "/")
    except ValueError:
        relative_daily = str(daily_path).replace("\\", "/")
    source_paths.append(relative_daily)
    frame = _load_daily_frame(daily_path, source_cache=source_cache)

    # Criterion 2: Price Data Contract classification on the *original* frame,
    # before any filtering, matching how the gate classifies elsewhere.
    observed_basis, basis_reason = classify_frame_price_basis(frame)
    record(
        "price_basis_raw",
        observed_basis == "raw",
        observed_basis=observed_basis,
        basis_reason=basis_reason or "",
        source_path=relative_daily,
        derivation="classify_frame_price_basis(canonical daily frame)",
    )

    rows = _rows_at_or_before(frame, as_of_day)
    canonical_close: float | None = None
    canonical_last_day: str | None = None
    if not rows.empty:
        last = rows.iloc[-1]
        canonical_last_day = str(last.get("__day") or "")[:10] or None
        canonical_close = _finite(last.get("close"))

    # Criterion 1: the candidate's own close must equal the canonical raw close
    # observed at as_of. A mismatch means the candidate priced off something
    # other than canonical raw history, which the contract cannot certify.
    cand_close = _candidate_close(candidate)
    close_delta = (
        abs(cand_close - canonical_close)
        if cand_close is not None and canonical_close is not None
        else None
    )
    record(
        "close_matches_canonical_raw",
        close_delta is not None and close_delta <= CLOSE_TOLERANCE,
        candidate_close=cand_close,
        canonical_close=canonical_close,
        abs_delta=close_delta,
        tolerance=CLOSE_TOLERANCE,
        canonical_last_date_at_or_before_as_of=canonical_last_day,
        source_path=relative_daily,
        derivation="close of the last canonical raw daily row at or before as_of",
    )

    # Criterion 3: the candidate's declared coverage dates must not reach past
    # as_of, and the producer must have marked the target date covered.
    data_latest = _iso_day(candidate.get("data_latest_date"))
    feature_latest = _iso_day(candidate.get("daily_feature_latest_date"))
    target_covered = candidate.get("is_target_date_covered") is True
    as_of_clean = bool(
        target_covered
        and data_latest is not None
        and data_latest <= as_of_day
        and (feature_latest is None or feature_latest <= as_of_day)
    )
    record(
        "as_of_clean",
        as_of_clean,
        data_latest_date=data_latest,
        daily_feature_latest_date=feature_latest,
        is_target_date_covered=target_covered,
        as_of=as_of_day,
        derivation="candidate coverage dates compared against as_of",
    )

    # Criterion 4: sector must come from that date's own dated membership
    # capture, with both effective_from and observed_at at or before as_of.
    membership, membership_path = _load_membership(
        as_of_day,
        history_dir=history_root,
        source_cache=source_cache,
    )
    row = membership.get(symbol) or {}
    if membership_path:
        source_paths.append(membership_path)
    pit_sector = str(row.get("sector") or "").strip()
    effective_from = _iso_day(row.get("effective_from"))
    observed_at = _iso_day(row.get("observed_at"))
    sector_proven = bool(
        pit_sector
        and effective_from is not None
        and effective_from <= as_of_day
        and observed_at is not None
        and observed_at <= as_of_day
    )
    record(
        "sector_dated_membership",
        sector_proven,
        pit_sector=pit_sector or None,
        effective_from=effective_from,
        observed_at=observed_at,
        membership_source=str(row.get("source") or "") or None,
        source_path=membership_path,
        derivation="symbol lookup in the as_of date's own sector_history snapshot",
    )

    # Criterion 5: amount_ma20 must be reproducible from canonical rows with
    # the upstream formula. min_periods is enforced, so a short history yields
    # no value rather than a mean over fewer days.
    cand_amount = _finite(candidate.get("amount_ma20"))
    derived_amount: float | None = None
    if not rows.empty and "amount" in rows.columns:
        amounts = pd.to_numeric(rows["amount"], errors="coerce")
        rolled = amounts.rolling(
            AMOUNT_MA20_WINDOW, min_periods=AMOUNT_MA20_MIN_PERIODS
        ).mean()
        if len(rolled):
            derived_amount = _finite(rolled.iloc[-1])
    amount_delta = (
        abs(cand_amount - derived_amount)
        if cand_amount is not None and derived_amount is not None
        else None
    )
    amount_tolerance = (
        max(abs(derived_amount) * AMOUNT_MA20_REL_TOLERANCE, 1e-6)
        if derived_amount is not None
        else None
    )
    record(
        "amount_ma20_reproducible",
        amount_delta is not None
        and amount_tolerance is not None
        and amount_delta <= amount_tolerance,
        candidate_amount_ma20=cand_amount,
        derived_amount_ma20=derived_amount,
        abs_delta=amount_delta,
        tolerance=amount_tolerance,
        rows_used=int(len(rows)),
        source_path=relative_daily,
        derivation=(
            f"rolling({AMOUNT_MA20_WINDOW}, min_periods={AMOUNT_MA20_MIN_PERIODS}).mean() "
            "over amount of canonical raw daily rows at or before as_of"
        ),
    )

    return _assemble(
        criteria,
        reason_codes,
        source_paths,
        symbol=symbol,
        as_of=as_of_day,
        pit_sector=pit_sector or None,
    )


def _assemble(
    criteria: Mapping[str, Mapping[str, Any]],
    reason_codes: list[str],
    source_paths: list[str],
    *,
    symbol: str,
    as_of: str | None,
    pit_sector: str | None = None,
) -> dict[str, Any]:
    """Assemble the eligibility payload from per-criterion verdicts.

    ``coverage`` is the proven fraction of REQUIRED_CRITERIA against a
    ``minimum_coverage`` of 1.0, so a partial proof cannot satisfy the
    eligibility contract. ``pit_eligible`` and ``price_contract_eligible`` are
    reported against the criteria that actually bear on each, so a consumer can
    see which dimension failed instead of one opaque flag.
    """

    proven = {name: bool(criteria.get(name, {}).get("proven")) for name in REQUIRED_CRITERIA}
    proven_count = sum(1 for value in proven.values() if value)
    coverage = proven_count / float(len(REQUIRED_CRITERIA))
    all_proven = proven_count == len(REQUIRED_CRITERIA)

    price_contract_eligible = bool(
        proven["close_matches_canonical_raw"]
        and proven["price_basis_raw"]
        and proven["amount_ma20_reproducible"]
    )
    pit_eligible = bool(proven["as_of_clean"] and proven["sector_dated_membership"])

    return {
        # --- fields evaluate_candidate_eligibility consumes ---------------
        "eligible": all_proven,
        "evidence_kind": EVIDENCE_KIND_FORMAL if all_proven else EVIDENCE_KIND_PARTIAL,
        "coverage": coverage,
        "minimum_coverage": MINIMUM_COVERAGE,
        "pit_eligible": pit_eligible,
        "price_contract_eligible": price_contract_eligible,
        # --- re-derivation record ----------------------------------------
        "derivation": {
            "schema_version": "candidate_lifecycle_evidence_v1",
            "symbol": symbol,
            "as_of": as_of,
            "ruling": "2026-08-08 strict derivation: formal requires all five criteria proven",
            "required_criteria": list(REQUIRED_CRITERIA),
            "proven_count": proven_count,
            "criteria": {name: dict(value) for name, value in criteria.items()},
            "reason_codes": list(dict.fromkeys(reason_codes)),
            "source_paths": list(dict.fromkeys(source_paths)),
            "pit_sector": pit_sector,
            "research_only": True,
            "paper_only": True,
            "is_real_order": False,
            "no_broker": True,
        },
    }
