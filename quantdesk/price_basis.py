"""Shared price-adjustment / price-basis labelling helper (Option C).

Option C (dual-track with labels) keeps ``data/daily`` as the raw canonical
daily OHLCV source and never rewrites market data. Instead, every research /
backtest / report / UI payload attaches explicit basis labels so a consumer can
tell which price convention produced a result:

- ``price_basis``: ``raw`` / ``forward_adjusted`` / ``back_adjusted`` /
  ``mixed_or_unknown`` (human-readable Chinese via :data:`PRICE_BASIS_LABELS`).
- ``adjustment_basis``: a structured record of the requested vs. effective
  basis, the canonical adjustment-factor source, and whether a divergent alias
  was detected.
- ``adjustment_warnings``: visible, non-swallowed warnings such as a forward
  request that silently fell back to flat raw ``data/daily``, or a divergent
  ``adjustment_factor.csv`` alias.

This module is intentionally dependency-light. The only project import is the
lazy call into :mod:`quantdesk.data_truth.adjustment_factor_audit`, used to
detect a divergent alias without ever treating the alias as a fallback data
source.
"""

from __future__ import annotations

from collections import Counter
from copy import deepcopy
from pathlib import Path
from typing import Any, Mapping

import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# Canonical authoritative adjustment-factor file. The singular ``adjustment_factor.csv``
# is only ever a local alias for manual audit; it must NOT be read as a fallback
# data source.
ADJUSTMENT_FACTOR_CANONICAL = "data/metadata/adjustment_factors.csv"
ADJUSTMENT_FACTOR_ALIAS = "data/metadata/adjustment_factor.csv"

# Canonical price-basis vocabulary (Option C).
PRICE_BASIS_RAW = "raw"
PRICE_BASIS_FORWARD = "forward_adjusted"
PRICE_BASIS_BACK = "back_adjusted"
PRICE_BASIS_UNKNOWN = "mixed_or_unknown"
PRICE_DATA_CONTRACT_VERSION = "1.0"

# Human-readable Chinese labels used by reporting / UI.
PRICE_BASIS_LABELS: dict[str, str] = {
    PRICE_BASIS_RAW: "原始日线价格",
    PRICE_BASIS_FORWARD: "前复权",
    PRICE_BASIS_BACK: "后复权",
    PRICE_BASIS_UNKNOWN: "混合或未知口径",
}

# Adjustment-basis source vocabulary.
ADJUSTMENT_SOURCE_INPUT = "input_history_as_provided"
ADJUSTMENT_SOURCE_CANONICAL = "canonical_adjustment_factors"
ADJUSTMENT_SOURCE_MISSING = "missing_or_unverified"

# Warning strings (kept stable so tests / UI can match them).
FORWARD_FALLBACK_WARNING = "requested_forward_adjusted_but_using_flat_daily_raw_candidate"
DIVERGENT_ALIAS_WARNING = "divergent_adjustment_factor_alias:data/metadata/adjustment_factor.csv"
UNKNOWN_BASIS_WARNING = "price_basis_mixed_or_unknown: results may mix raw/adjusted prices; verify before use"
RAW_BASIS_NOTE = "price_basis_raw: results use raw (unadjusted) daily prices; ex-dividend/split jumps are real price gaps"

# Maps free-form adjustment strings onto the canonical vocabulary.
_BASIS_ALIASES: dict[str, str] = {
    "": PRICE_BASIS_RAW,
    "raw": PRICE_BASIS_RAW,
    "none": PRICE_BASIS_RAW,
    "unadjusted": PRICE_BASIS_RAW,
    "flat": PRICE_BASIS_RAW,
    "forward": PRICE_BASIS_FORWARD,
    "forward_adjusted": PRICE_BASIS_FORWARD,
    "qfq": PRICE_BASIS_FORWARD,
    "pre": PRICE_BASIS_FORWARD,
    "front": PRICE_BASIS_FORWARD,
    "back": PRICE_BASIS_BACK,
    "back_adjusted": PRICE_BASIS_BACK,
    "hfq": PRICE_BASIS_BACK,
    "post": PRICE_BASIS_BACK,
}

# Process-level cache for the (relatively cheap but repeated) divergent-alias
# audit, keyed by metadata dir + file stat signatures so it re-checks on change.
_ALIAS_AUDIT_CACHE: dict[tuple[Any, ...], dict[str, Any]] = {}

_KNOWN_RESEARCH_BASES = [PRICE_BASIS_RAW, PRICE_BASIS_FORWARD, PRICE_BASIS_BACK]

_CURRENT_ENFORCED_POLICY: dict[str, Any] = {
    "feature": {
        "price_role": "research_signal_price",
        "required_basis": "single_known_basis",
        "allowed_basis": list(_KNOWN_RESEARCH_BASES),
        "cross_symbol_consistency": True,
        "mixed_or_unknown": "block",
    },
    "backtest": {
        "signal_price_role": "research_signal_price",
        "fill_price_role": "research_simulation_price",
        "architecture": "single_frame_compatibility",
        "required_basis": "single_known_basis",
        "allowed_basis": list(_KNOWN_RESEARCH_BASES),
        "fill_price_basis": "inherits_validated_input_basis",
        "cross_symbol_consistency": True,
        "mixed_or_unknown": "block_before_simulation",
        "real_execution_price_claim": False,
    },
    "rule_replay": {
        "price_role": "historical_signal_price",
        "required_basis": "single_known_basis",
        "allowed_basis": list(_KNOWN_RESEARCH_BASES),
        "cross_symbol_consistency": True,
        "mixed_or_unknown": "block_formal_metrics",
        "execution": "none",
    },
    "historical_intraday_replay": {
        "signal_threshold_basis": PRICE_BASIS_RAW,
        "execution_price_basis": PRICE_BASIS_RAW,
        "execution_price_field": "minute.close",
        "required_basis": PRICE_BASIS_RAW,
        "allowed_basis": [PRICE_BASIS_RAW],
        "cross_symbol_consistency": True,
        "mixed_or_unknown": "block_before_plan",
        "adjusted_signal_to_raw_bridge": "not_implemented",
    },
    "trade_replay": {
        "architecture": "single_frame",
        "required_basis": PRICE_BASIS_RAW,
        "allowed_basis": [PRICE_BASIS_RAW],
        "cross_symbol_consistency": True,
        "mixed_or_unknown": "block_before_simulation",
        "execution_price_fields": ["close", "next_open"],
        "adjusted_signal_to_raw_bridge": "not_implemented",
    },
    "execution": {
        "price_role": "real_execution_price",
        "required_basis": PRICE_BASIS_RAW,
        "allowed_basis": [PRICE_BASIS_RAW],
        "cross_symbol_consistency": True,
        "mixed_or_unknown": "block",
        "source_role": "unadjusted_daily_or_intraday_market_bar",
        "broker_route": False,
    },
    "paper_execution": {
        "price_role": "real_execution_price",
        "required_basis": PRICE_BASIS_RAW,
        "allowed_basis": [PRICE_BASIS_RAW],
        "cross_symbol_consistency": True,
        "mixed_or_unknown": "block",
        "allowed_sources": [
            "quality_gated_intraday_close",
            "unadjusted_daily_open",
            "unadjusted_daily_close",
        ],
        "slippage_application": "after_raw_base_price",
        "broker_route": False,
        "real_order": False,
    },
    "paper_valuation": {
        "latest_price_basis": PRICE_BASIS_RAW,
        "corporate_action_policy": (
            "raw_price_plus_canonical_adjustment_factor_for_quantity_and_cost_basis"
        ),
    },
}

_PRICE_DATA_CONTRACT: dict[str, Any] = {
    "version": PRICE_DATA_CONTRACT_VERSION,
    "enforcement_mode": "fail_closed",
    "basis_vocabulary": {
        PRICE_BASIS_RAW: "unadjusted_exchange_price",
        PRICE_BASIS_FORWARD: "qfq_research_view",
        PRICE_BASIS_BACK: "hfq_research_view",
        PRICE_BASIS_UNKNOWN: "blocked",
    },
    "current_enforced": _CURRENT_ENFORCED_POLICY,
    "future_target": {
        "feature": PRICE_BASIS_FORWARD,
        "backtest_signal": PRICE_BASIS_FORWARD,
        "backtest_research_return": "forward_adjusted_total_return_view",
        "replay_signal": PRICE_BASIS_FORWARD,
        "execution": "raw_real_execution_price",
        "simulation": "dual_track_adjusted_signal_plus_raw_execution",
        "signal_to_execution_bridge": {
            "required": True,
            "factor_source": ADJUSTMENT_FACTOR_CANONICAL,
            "as_of_date_aware": True,
            "divergent_alias_allowed": False,
        },
    },
    "migration_note": (
        "Current backtests use one validated frame for research signals and simulated fills; "
        "non-raw fills are research proxies and are not real execution-price claims. Moving "
        "research consumers to mandatory forward-adjusted prices plus raw execution requires "
        "a separate audited dual-track migration approval."
    ),
}


def normalize_price_basis(adjustment: Any) -> str:
    """Map a free-form adjustment string onto the canonical price-basis vocabulary."""

    key = str(adjustment or "").strip().lower()
    return _BASIS_ALIASES.get(key, PRICE_BASIS_UNKNOWN)


def classify_frame_price_basis(frame: pd.DataFrame) -> tuple[str, str]:
    """Classify one frame without guessing or converting its prices."""

    from quantdesk.enriched.adjust import (
        ADJUST_HFQ,
        ADJUST_QFQ,
        ADJUST_RAW,
        ADJUST_UNKNOWN,
        AdjustConsistencyError,
        adjust_of_frame,
        normalize_adjust,
    )

    if frame is not None:
        preflight_reason = str(frame.attrs.get("price_basis_reason") or "")
        if preflight_reason in {"mixed_price_basis", "unknown_price_basis"}:
            preflight_basis = str(
                frame.attrs.get("observed_price_basis") or PRICE_BASIS_UNKNOWN
            )
            return preflight_basis, preflight_reason
    if frame is None or frame.empty or "adjust" not in frame.columns:
        return PRICE_BASIS_UNKNOWN, "unknown_price_basis"
    normalized = [normalize_adjust(value) for value in frame["adjust"].tolist()]
    known_tokens = {token for token in normalized if token != ADJUST_UNKNOWN}
    if len(known_tokens) > 1:
        return PRICE_BASIS_UNKNOWN, "mixed_price_basis"
    if not normalized or ADJUST_UNKNOWN in normalized:
        return PRICE_BASIS_UNKNOWN, "unknown_price_basis"
    try:
        actual = adjust_of_frame(frame)
    except AdjustConsistencyError:
        return PRICE_BASIS_UNKNOWN, "mixed_price_basis"
    mapping = {
        ADJUST_RAW: PRICE_BASIS_RAW,
        ADJUST_QFQ: PRICE_BASIS_FORWARD,
        ADJUST_HFQ: PRICE_BASIS_BACK,
    }
    basis = mapping.get(actual, PRICE_BASIS_UNKNOWN)
    if basis == PRICE_BASIS_UNKNOWN:
        return basis, "unknown_price_basis"
    return basis, ""


def validate_price_frame_set(
    frames: Mapping[str, pd.DataFrame],
    *,
    consumer: str,
) -> dict[str, Any]:
    """Require one known price basis across a consumer's complete frame set."""

    consumer_name = str(consumer or "")
    policy = _CURRENT_ENFORCED_POLICY.get(consumer_name)
    per_symbol: dict[str, dict[str, Any]] = {}
    basis_counts: Counter[str] = Counter()
    reason_counts: Counter[str] = Counter()
    blocked_symbols: list[str] = []
    reason_codes: list[str] = []
    for symbol, frame in frames.items():
        basis, reason = classify_frame_price_basis(frame)
        if not reason:
            basis_counts[basis] += 1
        if policy is None:
            reason = "unknown_price_consumer"
        elif not reason and basis not in set(policy.get("allowed_basis") or []):
            reason = "consumer_price_basis_not_allowed"
        eligible = not reason
        per_symbol[str(symbol)] = {
            "price_basis": basis,
            "reason_code": reason,
            "eligible": eligible,
        }
        if reason:
            blocked_symbols.append(str(symbol))
            reason_counts[reason] += 1
            if reason not in reason_codes:
                reason_codes.append(reason)

    if policy is None and "unknown_price_consumer" not in reason_codes:
        reason_codes.append("unknown_price_consumer")
        reason_counts["unknown_price_consumer"] += 1
    elif not frames:
        reason_codes.append("no_price_frames")
        reason_counts["no_price_frames"] += 1
    elif not blocked_symbols and len(basis_counts) > 1:
        reason_codes.append("cross_symbol_price_basis_mismatch")
        reason_counts["cross_symbol_price_basis_mismatch"] += len(frames)
        blocked_symbols = [str(symbol) for symbol in frames]
        for row in per_symbol.values():
            row["eligible"] = False
            row["reason_code"] = "cross_symbol_price_basis_mismatch"

    eligible = not reason_codes
    accepted_basis = (
        next(iter(basis_counts))
        if eligible and len(basis_counts) == 1
        else PRICE_BASIS_UNKNOWN
    )
    return {
        "contract_version": PRICE_DATA_CONTRACT_VERSION,
        "consumer": consumer_name,
        "consumer_policy": deepcopy(policy) if policy is not None else {},
        "status": "passed" if eligible else "blocked",
        "eligible": eligible,
        "accepted_price_basis": accepted_basis,
        "basis_counts": dict(sorted(basis_counts.items())),
        "reason_codes": reason_codes,
        "reason_counts": dict(sorted(reason_counts.items())),
        "blocked_symbols": blocked_symbols,
        "per_symbol": per_symbol,
        "canonical_write": False,
        "price_conversion_performed": False,
    }


def price_data_contract() -> dict[str, Any]:
    """Return a defensive copy of the machine-readable Price Data Contract."""

    return deepcopy(_PRICE_DATA_CONTRACT)


def observed_price_basis_labels(
    basis: str,
    *,
    reason_codes: list[str] | None = None,
) -> dict[str, Any]:
    """Label a basis already observed and validated from input frames.

    Unlike :func:`price_basis_labels`, this helper never performs requested
    directory selection or forward-adjusted fallback.  It therefore cannot
    turn an observed qfq frame back into a raw label merely because a sibling
    adjusted-data directory is absent.
    """

    canonical = str(basis or PRICE_BASIS_UNKNOWN)
    if canonical not in {
        PRICE_BASIS_RAW,
        PRICE_BASIS_FORWARD,
        PRICE_BASIS_BACK,
        PRICE_BASIS_UNKNOWN,
    }:
        canonical = PRICE_BASIS_UNKNOWN
    reasons = [str(item) for item in (reason_codes or []) if str(item)]
    warnings = list(dict.fromkeys(reasons))
    if canonical == PRICE_BASIS_UNKNOWN and UNKNOWN_BASIS_WARNING not in warnings:
        warnings.append(UNKNOWN_BASIS_WARNING)
    return {
        "price_basis": canonical,
        "price_basis_label": describe_price_basis(canonical),
        "adjustment_basis": {
            "requested": "observed_frame",
            "effective": canonical,
            "source": ADJUSTMENT_SOURCE_INPUT if canonical != PRICE_BASIS_UNKNOWN else ADJUSTMENT_SOURCE_MISSING,
            "observation_mode": True,
            "forward_fallback_to_raw": False,
            "canonical_factor_file": ADJUSTMENT_FACTOR_CANONICAL,
            "alias_factor_used_as_fallback": False,
        },
        "adjustment_warnings": warnings,
    }


def describe_price_basis(basis: str) -> str:
    """Return the human-readable Chinese label for a canonical price basis."""

    return PRICE_BASIS_LABELS.get(str(basis), PRICE_BASIS_LABELS[PRICE_BASIS_UNKNOWN])


def _resolve_data_root(data_root: str | Path | None) -> Path:
    if data_root is None:
        return PROJECT_ROOT / "data"
    root = Path(data_root)
    return root if root.is_absolute() else PROJECT_ROOT / root


def _forward_dir_has_data(data_root: str | Path | None) -> bool:
    """Whether ``data/daily/forward_adjusted`` exists and holds any CSV.

    This mirrors ``strategy_viability_runner._daily_dir`` fallback semantics so
    the label layer can tell a real forward-adjusted read from a silent fallback
    to flat raw ``data/daily``.
    """

    forward_dir = _resolve_data_root(data_root) / "daily" / "forward_adjusted"
    try:
        if not forward_dir.exists():
            return False
        return any(forward_dir.glob("*.csv"))
    except OSError:
        return False


def _stat_signature(path: Path) -> tuple[Any, ...]:
    try:
        stat = path.stat()
        return (path.name, True, stat.st_size, int(stat.st_mtime))
    except OSError:
        return (path.name, False, 0, 0)


def detect_divergent_adjustment_factor_alias(data_root: str | Path | None = None) -> dict[str, Any]:
    """Audit the adjustment-factor file set for a divergent alias.

    Uses :func:`quantdesk.data_truth.adjustment_factor_audit.audit_adjustment_factor_file_set`
    without ever loading the full CSVs and without treating the alias as data.
    Returns a small dict: ``{divergent, alias_present, canonical_present,
    file_set_status, warnings}``.
    """

    root = _resolve_data_root(data_root)
    metadata_dir = root / "metadata"
    canonical = root / "metadata" / "adjustment_factors.csv"
    alias = root / "metadata" / "adjustment_factor.csv"
    cache_key = (str(metadata_dir), _stat_signature(canonical), _stat_signature(alias))
    cached = _ALIAS_AUDIT_CACHE.get(cache_key)
    if cached is not None:
        return dict(cached)

    result: dict[str, Any] = {
        "divergent": False,
        "alias_present": alias.exists(),
        "canonical_present": canonical.exists(),
        "file_set_status": "unknown",
        "warnings": [],
    }
    if not metadata_dir.exists():
        result["file_set_status"] = "metadata_dir_missing"
        _ALIAS_AUDIT_CACHE[cache_key] = dict(result)
        return dict(result)

    try:
        from quantdesk.data_truth.adjustment_factor_audit import audit_adjustment_factor_file_set

        audit = audit_adjustment_factor_file_set(metadata_dir)
    except Exception as exc:  # noqa: BLE001 - never let auditing break payloads
        result["file_set_status"] = f"audit_unavailable:{type(exc).__name__}"
        _ALIAS_AUDIT_CACHE[cache_key] = dict(result)
        return dict(result)

    status = str(audit.get("file_set_status") or "unknown")
    result["file_set_status"] = status
    if status == "canonical_with_divergent_alias":
        result["divergent"] = True
        result["warnings"].append(DIVERGENT_ALIAS_WARNING)
    _ALIAS_AUDIT_CACHE[cache_key] = dict(result)
    return dict(result)


def build_adjustment_basis(adjustment: Any, *, data_root: str | Path | None = None) -> dict[str, Any]:
    """Return the structured adjustment-basis record for a requested adjustment.

    Never reads or rewrites market data. It records the requested vs. effective
    basis (accounting for the forward -> flat-raw fallback), the canonical
    adjustment-factor source, and whether a divergent alias exists. The alias is
    reported for audit only and is explicitly never used as a fallback source.
    """

    requested = normalize_price_basis(adjustment)
    root = _resolve_data_root(data_root)
    canonical = root / "metadata" / "adjustment_factors.csv"
    alias = root / "metadata" / "adjustment_factor.csv"

    effective = requested
    forward_fallback_to_raw = False
    if requested == PRICE_BASIS_FORWARD and not _forward_dir_has_data(data_root):
        # forward requested but no forward_adjusted CSVs -> flat raw data/daily.
        effective = PRICE_BASIS_RAW
        forward_fallback_to_raw = True

    alias_info = detect_divergent_adjustment_factor_alias(data_root)

    if effective == PRICE_BASIS_RAW:
        source = ADJUSTMENT_SOURCE_INPUT
    elif effective in (PRICE_BASIS_FORWARD, PRICE_BASIS_BACK):
        source = ADJUSTMENT_SOURCE_CANONICAL if canonical.exists() else ADJUSTMENT_SOURCE_MISSING
    else:
        source = ADJUSTMENT_SOURCE_MISSING

    return {
        "requested": requested,
        "effective": effective,
        "source": source,
        "daily_history_basis": PRICE_BASIS_RAW,  # data/daily is always raw canonical
        "forward_fallback_to_raw": forward_fallback_to_raw,
        "canonical_factor_file": ADJUSTMENT_FACTOR_CANONICAL,
        "canonical_factor_available": canonical.exists(),
        "alias_factor_file": ADJUSTMENT_FACTOR_ALIAS,
        "alias_factor_present": alias.exists(),
        "alias_factor_used_as_fallback": False,  # alias is audit-only, never a data source
        "alias_factor_divergent": bool(alias_info.get("divergent")),
        "adjustment_factor_file_set_status": alias_info.get("file_set_status"),
    }


def basis_warnings(
    adjustment: Any,
    *,
    data_root: str | Path | None = None,
    extra_warnings: list[str] | None = None,
) -> list[str]:
    """Aggregate visible, deduplicated price-basis warnings.

    Includes the forward -> flat-raw fallback warning, the divergent-alias
    warning, and a raw/mixed basis note, plus any caller-supplied extras.
    """

    basis = build_adjustment_basis(adjustment, data_root=data_root)
    warnings: list[str] = []
    if basis.get("forward_fallback_to_raw"):
        warnings.append(FORWARD_FALLBACK_WARNING)
    if basis.get("alias_factor_divergent"):
        warnings.append(DIVERGENT_ALIAS_WARNING)
    effective = basis.get("effective")
    if effective == PRICE_BASIS_UNKNOWN:
        warnings.append(UNKNOWN_BASIS_WARNING)
    elif effective == PRICE_BASIS_RAW:
        warnings.append(RAW_BASIS_NOTE)
    for item in extra_warnings or []:
        if item:
            warnings.append(str(item))
    return list(dict.fromkeys(warnings))


def price_basis_labels(
    adjustment: Any,
    *,
    data_root: str | Path | None = None,
    extra_warnings: list[str] | None = None,
) -> dict[str, Any]:
    """Return the Option C label bundle to merge into any payload.

    Keys: ``price_basis`` (effective canonical basis), ``price_basis_label``
    (Chinese), ``adjustment_basis`` (structured record), ``adjustment_warnings``
    (visible warning list). Merging this into a payload never changes existing
    return / scoring / strategy fields; it only appends basis metadata.
    """

    basis = build_adjustment_basis(adjustment, data_root=data_root)
    effective = str(basis.get("effective") or PRICE_BASIS_UNKNOWN)
    return {
        "price_basis": effective,
        "price_basis_label": describe_price_basis(effective),
        "adjustment_basis": basis,
        "adjustment_warnings": basis_warnings(
            adjustment, data_root=data_root, extra_warnings=extra_warnings
        ),
    }


__all__ = [
    "PROJECT_ROOT",
    "ADJUSTMENT_FACTOR_CANONICAL",
    "ADJUSTMENT_FACTOR_ALIAS",
    "PRICE_BASIS_RAW",
    "PRICE_BASIS_FORWARD",
    "PRICE_BASIS_BACK",
    "PRICE_BASIS_UNKNOWN",
    "PRICE_DATA_CONTRACT_VERSION",
    "PRICE_BASIS_LABELS",
    "FORWARD_FALLBACK_WARNING",
    "DIVERGENT_ALIAS_WARNING",
    "normalize_price_basis",
    "classify_frame_price_basis",
    "validate_price_frame_set",
    "price_data_contract",
    "observed_price_basis_labels",
    "describe_price_basis",
    "detect_divergent_adjustment_factor_alias",
    "build_adjustment_basis",
    "basis_warnings",
    "price_basis_labels",
]
