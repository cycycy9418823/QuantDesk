"""Deterministic, research-only Market Brain facade.

The facade consumes caller-supplied evidence or an in-memory price frame.  It
does not load files, call providers, write artifacts, mutate paper state, or
change any existing strategy/backtest formula.  Formal capability and paper
exposure fields are fail-closed behind evidence, freshness, and Price Data
Contract gates.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
import math
from typing import Any, Mapping

import numpy as np
import pandas as pd

from quantdesk.price_basis import (
    PRICE_BASIS_BACK,
    PRICE_BASIS_FORWARD,
    PRICE_BASIS_RAW,
    PRICE_BASIS_UNKNOWN,
    classify_frame_price_basis,
    observed_price_basis_labels,
    price_data_contract,
    validate_price_frame_set,
)


REGIMES = ("BULL", "BEAR", "SIDEWAYS", "CRASH", "RECOVERY", "UNKNOWN")
_KNOWN_PRICE_BASES = {
    PRICE_BASIS_RAW,
    PRICE_BASIS_FORWARD,
    PRICE_BASIS_BACK,
}
_SCORE_KEYS = ("trend", "momentum", "breadth", "volume", "sector", "stability", "stress")

_REGIME_LABELS = {
    "BULL": "偏强研究环境",
    "BEAR": "偏弱研究环境",
    "SIDEWAYS": "震荡研究环境",
    "CRASH": "压力冲击研究环境",
    "RECOVERY": "修复观察环境",
    "UNKNOWN": "证据不足，环境未知",
}

_RESEARCH_CAPABILITIES: dict[str, tuple[str, ...]] = {
    "BULL": ("trend_breakout", "trend_continuation"),
    "BEAR": ("relative_strength_observation", "risk_off_research"),
    "SIDEWAYS": ("range_observation", "mean_reversion_research"),
    "CRASH": ("stress_observation",),
    "RECOVERY": ("recovery_confirmation", "rsi_recovery_observation"),
    "UNKNOWN": (),
}

_DISCLAIMER = (
    "Research Only; Paper Account Only; No Broker Route; Live Execution Locked; "
    "Not Investment Advice; Not Position Advice."
)


@dataclass(frozen=True)
class MarketRegimeConfig:
    """Typed thresholds for deterministic Market Brain classification."""

    min_history_rows: int = 60
    min_breadth_sample_count: int = 1
    min_full_market_sample_count: int = 1000
    min_full_market_coverage_ratio: float = 0.80
    max_evidence_age_days: int = 0

    momentum_lookback_rows: int = 5
    short_ma_window_rows: int = 20
    long_ma_window_rows: int = 60
    volatility_window_rows: int = 20
    drawdown_window_rows: int = 20

    bull_return_5d_min: float = 0.01
    bull_up_ratio_min: float = 0.55
    bull_volume_ratio_min: float = 1.0
    bull_strong_sector_ratio_min: float = 0.50
    bull_volatility_20d_max: float = 0.04

    bear_return_5d_max: float = -0.01
    bear_up_ratio_max: float = 0.35
    bear_strong_sector_ratio_max: float = 0.30
    bear_confirmation_min: int = 2

    crash_drawdown_20d_max: float = -0.10
    crash_return_5d_max: float = -0.06
    crash_up_ratio_max: float = 0.20
    crash_volatility_20d_min: float = 0.05
    crash_volume_ratio_min: float = 1.40
    crash_stress_confirmation_min: int = 2

    recovery_return_5d_min: float = 0.02
    recovery_up_ratio_min: float = 0.55
    recovery_volume_ratio_min: float = 1.0
    recovery_strong_sector_ratio_min: float = 0.45

    momentum_score_scale: float = 0.05
    volume_score_floor: float = 0.50
    volume_score_ceiling: float = 1.50
    volatility_stress_scale: float = 0.08
    drawdown_stress_scale: float = 0.15
    minimum_diagnostic_confidence: float = 0.20
    maximum_confidence: float = 0.95
    stale_diagnostic_confidence_cap: float = 0.35
    local_sample_confidence_cap: float = 0.60
    score_round_digits: int = 4

    bull_paper_exposure_ceiling: float = 0.80
    recovery_paper_exposure_ceiling: float = 0.50
    sideways_paper_exposure_ceiling: float = 0.35
    bear_paper_exposure_ceiling: float = 0.20
    crash_paper_exposure_ceiling: float = 0.10
    unknown_paper_exposure_ceiling: float = 0.0

    full_market_scopes: tuple[str, ...] = (
        "full_market",
        "all_a_share",
        "all_a",
        "market_wide",
    )


def _as_mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _safe_float(value: Any) -> float | None:
    if isinstance(value, (bool, np.bool_)):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _strict_bool(value: Any) -> bool | None:
    """Accept only real booleans; strings such as ``"false"`` are untrusted."""

    return value if isinstance(value, bool) else None


def _first_number(data: Mapping[str, Any], keys: tuple[str, ...]) -> float | None:
    for key in keys:
        if key in data:
            value = _safe_float(data.get(key))
            if value is not None:
                return value
    return None


def _reason_list(value: Any) -> list[str]:
    if isinstance(value, str):
        candidates = [value]
    elif isinstance(value, set):
        candidates = sorted(str(item) for item in value)
    elif isinstance(value, (list, tuple)):
        candidates = [str(item) for item in value]
    else:
        candidates = []
    return list(dict.fromkeys(item.strip() for item in candidates if item.strip()))


def _append_reason(reasons: list[str], code: str) -> None:
    if code and code not in reasons:
        reasons.append(code)


def _parse_date(value: Any) -> pd.Timestamp | None:
    parsed = pd.to_datetime(value, errors="coerce")
    if pd.isna(parsed):
        return None
    timestamp = pd.Timestamp(parsed)
    if timestamp.tzinfo is not None:
        # Preserve the calendar date asserted by the evidence source.  Turning
        # an Asia/Shanghai timestamp into UTC before normalising can move a
        # future A-share observation back into the target date.
        timestamp = timestamp.tz_localize(None)
    return timestamp.normalize()


def _date_text(value: Any) -> str:
    parsed = _parse_date(value)
    return parsed.strftime("%Y-%m-%d") if parsed is not None else ""


def _bounded(value: float, lower: float = 0.0, upper: float = 1.0) -> float:
    return min(upper, max(lower, float(value)))


def _mean(values: tuple[float, ...] | list[float]) -> float:
    return float(sum(values) / len(values)) if values else 0.0


def _config_reason_codes(config: MarketRegimeConfig) -> list[str]:
    reasons: list[str] = []
    positive_int_fields = (
        "min_history_rows",
        "min_breadth_sample_count",
        "min_full_market_sample_count",
        "momentum_lookback_rows",
        "short_ma_window_rows",
        "long_ma_window_rows",
        "volatility_window_rows",
        "drawdown_window_rows",
        "bear_confirmation_min",
        "crash_stress_confirmation_min",
    )
    for field_name in positive_int_fields:
        value = getattr(config, field_name)
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            _append_reason(reasons, f"invalid_config_{field_name}")
    if (
        isinstance(config.max_evidence_age_days, bool)
        or not isinstance(config.max_evidence_age_days, int)
        or config.max_evidence_age_days < 0
    ):
        _append_reason(reasons, "invalid_config_max_evidence_age_days")
    positive_float_fields = (
        "momentum_score_scale",
        "volatility_stress_scale",
        "drawdown_stress_scale",
    )
    for field_name in positive_float_fields:
        value = _safe_float(getattr(config, field_name))
        if value is None or value <= 0:
            _append_reason(reasons, f"invalid_config_{field_name}")
    finite_numeric_fields = (
        "bull_return_5d_min",
        "bull_up_ratio_min",
        "bull_volume_ratio_min",
        "bull_strong_sector_ratio_min",
        "bull_volatility_20d_max",
        "bear_return_5d_max",
        "bear_up_ratio_max",
        "bear_strong_sector_ratio_max",
        "crash_drawdown_20d_max",
        "crash_return_5d_max",
        "crash_up_ratio_max",
        "crash_volatility_20d_min",
        "crash_volume_ratio_min",
        "recovery_return_5d_min",
        "recovery_up_ratio_min",
        "recovery_volume_ratio_min",
        "recovery_strong_sector_ratio_min",
    )
    for field_name in finite_numeric_fields:
        if _safe_float(getattr(config, field_name)) is None:
            _append_reason(reasons, f"invalid_config_{field_name}")
    volume_floor = _safe_float(config.volume_score_floor)
    volume_ceiling = _safe_float(config.volume_score_ceiling)
    if (
        volume_floor is None
        or volume_ceiling is None
        or volume_ceiling <= volume_floor
    ):
        _append_reason(reasons, "invalid_config_volume_score_range")
    unit_interval_fields = (
        "min_full_market_coverage_ratio",
        "minimum_diagnostic_confidence",
        "maximum_confidence",
        "stale_diagnostic_confidence_cap",
        "local_sample_confidence_cap",
        "bull_paper_exposure_ceiling",
        "recovery_paper_exposure_ceiling",
        "sideways_paper_exposure_ceiling",
        "bear_paper_exposure_ceiling",
        "crash_paper_exposure_ceiling",
        "unknown_paper_exposure_ceiling",
    )
    for field_name in unit_interval_fields:
        value = _safe_float(getattr(config, field_name))
        if value is None or not 0.0 <= value <= 1.0:
            _append_reason(reasons, f"invalid_config_{field_name}")
    if (
        isinstance(config.score_round_digits, bool)
        or not isinstance(config.score_round_digits, int)
        or config.score_round_digits < 0
    ):
        _append_reason(reasons, "invalid_config_score_round_digits")
    ratio_fields = (
        "bull_up_ratio_min",
        "bull_strong_sector_ratio_min",
        "bear_up_ratio_max",
        "bear_strong_sector_ratio_max",
        "crash_up_ratio_max",
        "recovery_up_ratio_min",
        "recovery_strong_sector_ratio_min",
    )
    for field_name in ratio_fields:
        value = _safe_float(getattr(config, field_name))
        if value is not None and not 0.0 <= value <= 1.0:
            _append_reason(reasons, f"invalid_config_{field_name}")
    return_fields = (
        "bull_return_5d_min",
        "bear_return_5d_max",
        "crash_drawdown_20d_max",
        "crash_return_5d_max",
        "recovery_return_5d_min",
    )
    for field_name in return_fields:
        value = _safe_float(getattr(config, field_name))
        if value is not None and not -1.0 < value <= 1.0:
            _append_reason(reasons, f"invalid_config_{field_name}")
    positive_threshold_fields = (
        "bull_volume_ratio_min",
        "recovery_volume_ratio_min",
        "crash_volume_ratio_min",
    )
    nonnegative_threshold_fields = (
        "bull_volatility_20d_max",
        "crash_volatility_20d_min",
    )
    for field_name in positive_threshold_fields:
        value = _safe_float(getattr(config, field_name))
        if value is not None and value <= 0:
            _append_reason(reasons, f"invalid_config_{field_name}")
    for field_name in nonnegative_threshold_fields:
        value = _safe_float(getattr(config, field_name))
        if value is not None and value < 0:
            _append_reason(reasons, f"invalid_config_{field_name}")
    scopes = config.full_market_scopes
    if not isinstance(scopes, (tuple, list, set)) or not scopes or any(
        not isinstance(item, str) or not item.strip() for item in scopes
    ):
        _append_reason(reasons, "invalid_config_full_market_scopes")
    return reasons


def _normalise_price_policy(
    value: Any,
    *,
    contract_version: str,
) -> tuple[dict[str, Any], list[str], dict[str, Any]]:
    raw = deepcopy(dict(value)) if isinstance(value, Mapping) else {}
    if not raw:
        policy = {
            "contract_version": contract_version,
            "consumer": "market_brain",
            "status": "blocked",
            "eligible": False,
            "observed_basis": PRICE_BASIS_UNKNOWN,
            "accepted_price_basis": PRICE_BASIS_UNKNOWN,
            "reason_codes": ["missing_price_policy_validation"],
            "future_target_active": False,
        }
        return policy, list(policy["reason_codes"]), raw

    supplied_reasons = _reason_list(raw.get("reason_codes"))
    status = str(raw.get("status") or "").strip().lower()
    eligibility_values = [
        raw[key]
        for key in ("eligible", "ok")
        if key in raw
    ]

    accepted_basis = str(
        raw.get("accepted_price_basis")
        or raw.get("observed_basis")
        or raw.get("price_basis")
        or PRICE_BASIS_UNKNOWN
    )
    observed_basis = str(raw.get("observed_basis") or accepted_basis)
    supplied_version_value = raw.get("contract_version")
    supplied_version = (
        supplied_version_value
        if isinstance(supplied_version_value, str)
        else ""
    )
    reasons = list(supplied_reasons)

    if "contract_version" not in raw:
        _append_reason(reasons, "missing_price_contract_version")
    if not str(raw.get("consumer") or "").strip():
        _append_reason(reasons, "missing_price_policy_consumer")

    parsed_eligibility = [_strict_bool(item) for item in eligibility_values]
    if not parsed_eligibility:
        eligible = False
        _append_reason(reasons, "missing_price_policy_eligibility")
    elif any(item is None for item in parsed_eligibility):
        eligible = False
        _append_reason(reasons, "invalid_price_policy_eligibility")
    elif len(set(parsed_eligibility)) > 1:
        eligible = False
        _append_reason(reasons, "conflicting_price_policy_eligibility")
    else:
        eligible = parsed_eligibility[0] is True

    if not status:
        eligible = False
        _append_reason(reasons, "missing_price_policy_status")
    elif status not in {"pass", "passed", "ok", "eligible", "success"}:
        eligible = False
    if observed_basis not in _KNOWN_PRICE_BASES:
        eligible = False
        _append_reason(
            reasons,
            "unknown_price_basis"
            if observed_basis == PRICE_BASIS_UNKNOWN
            else "consumer_price_basis_not_allowed",
        )
    if accepted_basis not in _KNOWN_PRICE_BASES:
        eligible = False
        _append_reason(
            reasons,
            "unknown_price_basis"
            if accepted_basis == PRICE_BASIS_UNKNOWN
            else "consumer_price_basis_not_allowed",
        )
    if observed_basis != accepted_basis:
        eligible = False
        _append_reason(reasons, "observed_accepted_price_basis_mismatch")
    basis_counts = raw.get("basis_counts")
    if isinstance(basis_counts, Mapping):
        counted_bases = {
            str(basis)
            for basis, count in basis_counts.items()
            if (_safe_float(count) or 0.0) > 0
        }
        if counted_bases != {observed_basis}:
            eligible = False
            _append_reason(reasons, "price_policy_basis_counts_mismatch")
    blocked_symbols = raw.get("blocked_symbols")
    if isinstance(blocked_symbols, (list, tuple, set)) and blocked_symbols:
        eligible = False
        _append_reason(reasons, "price_policy_has_blocked_symbols")
    consumer_policy = raw.get("consumer_policy")
    allowed = _as_mapping(consumer_policy).get("allowed_basis")
    if not isinstance(consumer_policy, Mapping):
        eligible = False
        _append_reason(reasons, "missing_consumer_price_policy")
    elif not isinstance(allowed, (list, tuple, set)) or not allowed:
        eligible = False
        _append_reason(reasons, "invalid_consumer_price_policy")
    else:
        allowed_bases = {str(item) for item in allowed}
        if accepted_basis not in allowed_bases or observed_basis not in allowed_bases:
            eligible = False
            _append_reason(reasons, "consumer_price_basis_not_allowed")
    if supplied_version != contract_version:
        eligible = False
        _append_reason(reasons, "price_contract_version_mismatch")

    flag_sources = (raw, _as_mapping(raw.get("price_roles")))
    for flag_source in flag_sources:
        for key in ("future_target_active", "dual_track_bridge_active"):
            if key not in flag_source:
                continue
            flag = _strict_bool(flag_source.get(key))
            if flag is None:
                eligible = False
                _append_reason(reasons, f"invalid_{key}")
            elif flag:
                eligible = False
                _append_reason(reasons, "future_price_target_not_active")
        if "price_conversion_performed" in flag_source:
            conversion_flag = _strict_bool(
                flag_source.get("price_conversion_performed")
            )
            if conversion_flag is None:
                eligible = False
                _append_reason(reasons, "invalid_price_conversion_flag")
            elif conversion_flag:
                eligible = False
                _append_reason(reasons, "price_conversion_not_allowed")
    if reasons:
        eligible = False
    if not eligible:
        _append_reason(reasons, "price_policy_validation_failed")

    policy = {
        "contract_version": contract_version,
        "consumer": "market_brain",
        "source_consumer": str(raw.get("consumer") or "market_brain"),
        "status": "passed" if eligible else "blocked",
        "eligible": eligible,
        "observed_basis": observed_basis,
        "accepted_price_basis": accepted_basis,
        "reason_codes": reasons,
        "future_target_active": False,
    }
    return policy, reasons, raw


def _component_gate_reasons(
    evidence: Mapping[str, Any],
    *,
    target_date: pd.Timestamp | None,
    accepted_price_basis: str,
    data_scope: str,
    contract_version: str,
    config: MarketRegimeConfig,
) -> list[str]:
    reasons: list[str] = []
    for component_name in ("index", "breadth", "volume", "sector"):
        component = _as_mapping(evidence.get(component_name))
        if not component:
            continue
        auxiliary_component = component_name in {"breadth", "volume", "sector"}

        component_date_value = next(
            (
                component[key]
                for key in ("data_date", "data_cutoff", "as_of", "date")
                if key in component
                and component.get(key) is not None
                and component.get(key) != ""
            ),
            None,
        )
        if component_date_value is not None:
            component_date = _parse_date(component_date_value)
            if component_date is None:
                _append_reason(reasons, f"invalid_{component_name}_evidence_date")
            elif target_date is not None and component_date > target_date:
                _append_reason(reasons, f"future_{component_name}_evidence")
            elif (
                target_date is not None
                and (target_date - component_date).days > config.max_evidence_age_days
            ):
                _append_reason(reasons, f"stale_{component_name}_evidence")
        elif auxiliary_component:
            _append_reason(reasons, f"missing_{component_name}_evidence_date")

        component_scope = str(component.get("data_scope") or "").strip()
        if component_scope and data_scope and component_scope != data_scope:
            _append_reason(reasons, "cross_component_data_scope_mismatch")
        elif auxiliary_component and not component_scope:
            _append_reason(reasons, f"missing_{component_name}_data_scope")

        if auxiliary_component:
            provenance = component.get("provenance")
            provenance_map = _as_mapping(provenance)
            source_path = str(provenance_map.get("source_path") or "").strip()
            source_hash = str(provenance_map.get("source_hash") or "").strip()
            if not isinstance(provenance, Mapping):
                _append_reason(reasons, f"missing_{component_name}_provenance")
            elif (
                not source_path
                or not source_hash.startswith("sha256:")
                or len(source_hash) != 71
            ):
                _append_reason(reasons, f"invalid_{component_name}_provenance")

        component_policy_value = component.get("price_policy_validation")
        if component_policy_value is not None:
            component_policy, component_reasons, _raw = _normalise_price_policy(
                component_policy_value,
                contract_version=contract_version,
            )
            for reason in component_reasons:
                _append_reason(reasons, f"{component_name}_{reason}")
            component_basis = str(
                component_policy.get("accepted_price_basis") or PRICE_BASIS_UNKNOWN
            )
            if component_basis != accepted_price_basis:
                _append_reason(reasons, "cross_component_price_basis_mismatch")
        elif auxiliary_component:
            _append_reason(
                reasons,
                f"missing_{component_name}_price_policy_validation",
            )
        elif component.get("price_basis") not in {None, ""}:
            component_basis = str(component.get("price_basis"))
            if component_basis not in _KNOWN_PRICE_BASES:
                _append_reason(reasons, f"{component_name}_unknown_price_basis")
            elif component_basis != accepted_price_basis:
                _append_reason(reasons, "cross_component_price_basis_mismatch")
    return reasons


def _extract_component_evidence(
    evidence: Mapping[str, Any],
    *,
    config: MarketRegimeConfig,
) -> tuple[dict[str, Any], list[str]]:
    errors: list[str] = []
    index = _as_mapping(evidence.get("index"))
    close = _first_number(index, ("close", "index_close", "benchmark_close"))
    ma20 = _first_number(index, ("ma20", "index_ma20", "benchmark_ma20"))
    ma60 = _first_number(index, ("ma60", "index_ma60", "benchmark_ma60"))
    return_5d = _first_number(index, ("return_5d", "index_return_5d", "benchmark_return_5d"))
    drawdown_20d = _first_number(
        index,
        ("drawdown_20d", "recent_drawdown_20d", "benchmark_drawdown_20d"),
    )
    volatility_20d = _first_number(
        index,
        ("volatility_20d", "vol20", "index_vol20", "benchmark_vol20"),
    )
    index_values = (close, ma20, ma60, return_5d, drawdown_20d, volatility_20d)
    if not index or any(value is None for value in index_values):
        errors.append("missing_index_evidence")
    elif (
        close <= 0
        or ma20 <= 0
        or ma60 <= 0
        or return_5d <= -1.0
        or volatility_20d < 0
        or drawdown_20d <= -1.0
        or drawdown_20d > 0
    ):
        errors.append("invalid_index_evidence")

    breadth = _as_mapping(evidence.get("breadth"))
    up_count = _first_number(breadth, ("up_count",))
    down_count = _first_number(breadth, ("down_count",))
    flat_count = _first_number(breadth, ("flat_count",))
    sample_count = _first_number(breadth, ("sample_count",))
    minimum_breadth_sample = (
        config.min_breadth_sample_count
        if isinstance(config.min_breadth_sample_count, int)
        and not isinstance(config.min_breadth_sample_count, bool)
        and config.min_breadth_sample_count > 0
        else 1
    )
    if not breadth or up_count is None or down_count is None or sample_count is None:
        errors.append("missing_breadth_evidence")
        up_ratio = None
        down_ratio = None
    elif (
        up_count < 0
        or down_count < 0
        or (flat_count is not None and flat_count < 0)
        or sample_count <= 0
        or sample_count < minimum_breadth_sample
        or up_count + down_count + (flat_count or 0.0) > sample_count + 1e-9
    ):
        errors.append("invalid_breadth_evidence")
        up_ratio = None
        down_ratio = None
    else:
        up_ratio = up_count / sample_count
        down_ratio = down_count / sample_count
        if flat_count is None:
            flat_count = max(0.0, sample_count - up_count - down_count)

    volume = _as_mapping(evidence.get("volume"))
    volume_ratio = _first_number(volume, ("volume_ratio", "activity_ratio", "turnover_ratio"))
    if not volume or volume_ratio is None:
        errors.append("missing_volume_evidence")
    elif volume_ratio <= 0:
        errors.append("invalid_volume_evidence")

    sector = _as_mapping(evidence.get("sector"))
    strong_sector_ratio = _first_number(
        sector,
        ("strong_sector_ratio", "positive_sector_ratio"),
    )
    if not sector or strong_sector_ratio is None:
        errors.append("missing_sector_evidence")
    elif not 0.0 <= strong_sector_ratio <= 1.0:
        errors.append("invalid_sector_evidence")

    return {
        "index": {
            "close": close,
            "ma20": ma20,
            "ma60": ma60,
            "return_5d": return_5d,
            "drawdown_20d": drawdown_20d,
            "volatility_20d": volatility_20d,
        },
        "breadth": {
            "up_count": up_count,
            "down_count": down_count,
            "flat_count": flat_count,
            "sample_count": sample_count,
            "up_ratio": up_ratio,
            "down_ratio": down_ratio,
        },
        "volume": {"volume_ratio": volume_ratio},
        "sector": {"strong_sector_ratio": strong_sector_ratio},
    }, errors


def _component_scores(
    components: Mapping[str, Any],
    *,
    config: MarketRegimeConfig,
) -> dict[str, float]:
    index = _as_mapping(components.get("index"))
    breadth = _as_mapping(components.get("breadth"))
    volume = _as_mapping(components.get("volume"))
    sector = _as_mapping(components.get("sector"))

    close = float(index["close"])
    ma20 = float(index["ma20"])
    ma60 = float(index["ma60"])
    return_5d = float(index["return_5d"])
    drawdown_20d = float(index["drawdown_20d"])
    volatility_20d = float(index["volatility_20d"])
    up_ratio = float(breadth["up_ratio"])
    volume_ratio = float(volume["volume_ratio"])
    strong_sector_ratio = float(sector["strong_sector_ratio"])

    if close > ma20 > ma60:
        trend = 1.0
    elif close > ma20 and ma20 <= ma60:
        trend = 0.70
    elif close >= ma20 or ma20 >= ma60:
        trend = 0.55
    elif close < ma20 < ma60:
        trend = 0.0
    else:
        trend = 0.35

    momentum = _bounded(0.5 + return_5d / (2.0 * config.momentum_score_scale))
    volume_score_range = config.volume_score_ceiling - config.volume_score_floor
    if volume_score_range <= 0:
        volume_score = 0.0
    else:
        volume_score = _bounded((volume_ratio - config.volume_score_floor) / volume_score_range)
    volatility_stability = 1.0 - _bounded(volatility_20d / config.volatility_stress_scale)
    drawdown_stability = 1.0 - _bounded(
        abs(min(0.0, drawdown_20d)) / config.drawdown_stress_scale
    )
    stability = _mean((volatility_stability, drawdown_stability))
    stress = 1.0 - stability

    values = {
        "trend": trend,
        "momentum": momentum,
        "breadth": _bounded(up_ratio),
        "volume": volume_score,
        "sector": _bounded(strong_sector_ratio),
        "stability": _bounded(stability),
        "stress": _bounded(stress),
    }
    return {
        key: round(_bounded(value), config.score_round_digits)
        for key, value in values.items()
    }


def _classify_regime(
    components: Mapping[str, Any],
    *,
    previous_regime: str,
    config: MarketRegimeConfig,
) -> tuple[str, list[str]]:
    index = _as_mapping(components.get("index"))
    breadth = _as_mapping(components.get("breadth"))
    volume = _as_mapping(components.get("volume"))
    sector = _as_mapping(components.get("sector"))

    close = float(index["close"])
    ma20 = float(index["ma20"])
    ma60 = float(index["ma60"])
    return_5d = float(index["return_5d"])
    drawdown_20d = float(index["drawdown_20d"])
    volatility_20d = float(index["volatility_20d"])
    up_ratio = float(breadth["up_ratio"])
    volume_ratio = float(volume["volume_ratio"])
    strong_sector_ratio = float(sector["strong_sector_ratio"])

    crash_stress_count = sum(
        (
            return_5d <= config.crash_return_5d_max,
            up_ratio <= config.crash_up_ratio_max,
            volatility_20d >= config.crash_volatility_20d_min,
            volume_ratio >= config.crash_volume_ratio_min,
        )
    )
    if (
        drawdown_20d <= config.crash_drawdown_20d_max
        and crash_stress_count >= config.crash_stress_confirmation_min
    ):
        return "CRASH", ["crash_drawdown_confirmed", "crash_stress_confirmed"]

    if (
        previous_regime in {"BEAR", "CRASH"}
        and close > ma20
        and ma20 < ma60
        and return_5d >= config.recovery_return_5d_min
        and up_ratio >= config.recovery_up_ratio_min
        and volume_ratio >= config.recovery_volume_ratio_min
        and strong_sector_ratio >= config.recovery_strong_sector_ratio_min
    ):
        return "RECOVERY", [
            "recovery_prior_regime_confirmed",
            "recovery_breadth_confirmed",
        ]

    if (
        close > ma20 > ma60
        and return_5d >= config.bull_return_5d_min
        and up_ratio >= config.bull_up_ratio_min
        and volume_ratio >= config.bull_volume_ratio_min
        and strong_sector_ratio >= config.bull_strong_sector_ratio_min
        and volatility_20d <= config.bull_volatility_20d_max
    ):
        return "BULL", [
            "bull_trend_confirmed",
            "bull_momentum_confirmed",
            "bull_breadth_confirmed",
        ]

    bear_confirmation_count = sum(
        (
            return_5d <= config.bear_return_5d_max,
            up_ratio <= config.bear_up_ratio_max,
            strong_sector_ratio <= config.bear_strong_sector_ratio_max,
        )
    )
    if (
        close < ma20 < ma60
        and bear_confirmation_count >= config.bear_confirmation_min
    ):
        return "BEAR", ["bear_trend_confirmed", "bear_breadth_or_sector_confirmed"]

    return "SIDEWAYS", ["sideways_mixed_evidence"]


def _regime_confidence(
    regime: str,
    scores: Mapping[str, float],
    *,
    config: MarketRegimeConfig,
) -> float:
    trend = float(scores["trend"])
    momentum = float(scores["momentum"])
    breadth = float(scores["breadth"])
    volume = float(scores["volume"])
    sector = float(scores["sector"])
    stability = float(scores["stability"])
    stress = float(scores["stress"])

    if regime == "BULL":
        raw = _mean((trend, momentum, breadth, sector, stability))
    elif regime == "RECOVERY":
        raw = _mean((trend, momentum, breadth, volume, sector))
    elif regime in {"BEAR", "CRASH"}:
        raw = _mean((1.0 - trend, 1.0 - momentum, 1.0 - breadth, 1.0 - sector, stress))
    else:
        balance = _mean(
            tuple(1.0 - abs(value - 0.5) * 2.0 for value in (trend, momentum, breadth, sector))
        )
        raw = _mean((balance, stability))
    confidence = min(
        config.maximum_confidence,
        max(config.minimum_diagnostic_confidence, raw),
    )
    return round(_bounded(confidence), config.score_round_digits)


def _paper_exposure_ceiling(regime: str, config: MarketRegimeConfig) -> float:
    values = {
        "BULL": config.bull_paper_exposure_ceiling,
        "RECOVERY": config.recovery_paper_exposure_ceiling,
        "SIDEWAYS": config.sideways_paper_exposure_ceiling,
        "BEAR": config.bear_paper_exposure_ceiling,
        "CRASH": config.crash_paper_exposure_ceiling,
        "UNKNOWN": config.unknown_paper_exposure_ceiling,
    }
    return round(_bounded(values.get(regime, config.unknown_paper_exposure_ceiling)), 4)


def _empty_scores() -> dict[str, None]:
    return {key: None for key in _SCORE_KEYS}


def build_market_brain_snapshot(
    evidence: Mapping[str, Any] | None,
    *,
    config: MarketRegimeConfig | None = None,
) -> dict[str, Any]:
    """Build a deterministic Market Brain snapshot from explicit evidence.

    Stale evidence may retain ``regime`` as a backward-compatible diagnostic,
    while ``formal_regime`` is ``UNKNOWN`` and formal eligibility, research
    capabilities, and paper exposure are all disabled.  Future evidence is a
    hard look-ahead block and therefore always produces ``UNKNOWN``.
    """

    cfg = config or MarketRegimeConfig()
    source = _as_mapping(evidence)
    contract = price_data_contract()
    contract_version = str(contract.get("version") or "")
    visible_reasons: list[str] = []
    hard_blocking_reasons: list[str] = []

    def hard_block(code: str) -> None:
        _append_reason(hard_blocking_reasons, code)
        _append_reason(visible_reasons, code)

    config_reasons = _config_reason_codes(cfg)
    for code in config_reasons:
        hard_block(code)

    for code in _reason_list(source.get("precheck_reason_codes")):
        hard_block(code)

    components, component_errors = _extract_component_evidence(source, config=cfg)
    for code in component_errors:
        hard_block(code)

    price_policy, price_reasons, raw_price_validation = _normalise_price_policy(
        source.get("price_policy_validation"),
        contract_version=contract_version,
    )
    basis_labels = observed_price_basis_labels(
        str(price_policy.get("accepted_price_basis") or PRICE_BASIS_UNKNOWN),
        reason_codes=list(price_policy.get("reason_codes") or []),
    )
    for code in price_reasons:
        hard_block(code)

    target_date = _parse_date(source.get("target_date"))
    data_date = _parse_date(source.get("data_date"))
    if target_date is None:
        hard_block("missing_or_invalid_target_date")
    if data_date is None:
        hard_block("missing_or_invalid_market_evidence_date")

    stale_evidence = False
    if target_date is not None and data_date is not None:
        if data_date > target_date:
            hard_block("future_market_evidence")
        elif (target_date - data_date).days > (
            cfg.max_evidence_age_days if not config_reasons else 0
        ):
            stale_evidence = True
            _append_reason(visible_reasons, "stale_market_evidence")

    data_scope = str(source.get("data_scope") or "").strip()
    if not data_scope:
        hard_block("missing_data_scope")
    configured_scopes = (
        cfg.full_market_scopes
        if isinstance(cfg.full_market_scopes, (tuple, list, set))
        else ()
    )
    full_market_scope_values = {
        item.strip().lower()
        for item in configured_scopes
        if isinstance(item, str) and item.strip()
    }
    scope_is_full_market = data_scope.lower() in full_market_scope_values
    is_local_sample = bool(data_scope) and not scope_is_full_market
    if is_local_sample:
        _append_reason(visible_reasons, "local_sample_not_full_market")

    if not config_reasons:
        for code in _component_gate_reasons(
            source,
            target_date=target_date,
            accepted_price_basis=str(
                price_policy.get("accepted_price_basis") or PRICE_BASIS_UNKNOWN
            ),
            data_scope=data_scope,
            contract_version=contract_version,
            config=cfg,
        ):
            hard_block(code)

    breadth_sample_count = _safe_float(
        _as_mapping(components.get("breadth")).get("sample_count")
    )
    supplied_coverage = source.get("coverage")
    coverage_ratio = (
        _safe_float(_as_mapping(supplied_coverage).get("ratio"))
        if isinstance(supplied_coverage, Mapping)
        else None
    )
    coverage_sample_count = (
        _safe_float(_as_mapping(supplied_coverage).get("sample_count"))
        if isinstance(supplied_coverage, Mapping)
        else None
    )
    coverage_kind = str(
        _as_mapping(supplied_coverage).get("kind") or ""
    ).strip().lower()
    supplied_provenance_for_scope = source.get("provenance")
    scope_provenance = _as_mapping(supplied_provenance_for_scope)
    scope_source_path = str(scope_provenance.get("source_path") or "").strip()
    scope_source_hash = str(scope_provenance.get("source_hash") or "").strip()
    full_market_substantiated = bool(
        isinstance(supplied_coverage, Mapping)
        and coverage_kind in full_market_scope_values
        and coverage_ratio is not None
        and coverage_sample_count is not None
        and breadth_sample_count is not None
        and abs(coverage_sample_count - breadth_sample_count) <= 1e-9
        and isinstance(supplied_provenance_for_scope, Mapping)
        and scope_source_path
        and scope_source_hash.startswith("sha256:")
        and len(scope_source_hash) == 71
        and scope_provenance.get("coverage_verified") is True
    )
    if not config_reasons and scope_is_full_market and not full_market_substantiated:
        hard_block("full_market_coverage_unsubstantiated")
    if not config_reasons and scope_is_full_market and (
        breadth_sample_count is None
        or breadth_sample_count < cfg.min_full_market_sample_count
        or coverage_ratio is None
        or coverage_ratio < cfg.min_full_market_coverage_ratio
    ):
        hard_block("full_market_coverage_insufficient")

    previous_regime = str(source.get("previous_regime") or "").strip().upper()
    if previous_regime not in set(REGIMES) - {"UNKNOWN"}:
        previous_regime = ""

    if hard_blocking_reasons:
        regime = "UNKNOWN"
        classification_reasons: list[str] = []
        scores: dict[str, float] | dict[str, None] = _empty_scores()
        confidence = 0.0
    else:
        scores = _component_scores(components, config=cfg)
        regime, classification_reasons = _classify_regime(
            components,
            previous_regime=previous_regime,
            config=cfg,
        )
        confidence = _regime_confidence(regime, scores, config=cfg)
        if stale_evidence:
            confidence = min(confidence, cfg.stale_diagnostic_confidence_cap)
        if is_local_sample:
            confidence = min(confidence, cfg.local_sample_confidence_cap)
        confidence = round(_bounded(confidence), cfg.score_round_digits)
        for code in classification_reasons:
            _append_reason(visible_reasons, code)
        if regime == "RECOVERY" and previous_regime:
            _append_reason(
                visible_reasons,
                f"transition_from_{previous_regime.lower()}_to_recovery",
            )

    evidence_eligible = not hard_blocking_reasons and not stale_evidence
    capabilities = list(_RESEARCH_CAPABILITIES[regime]) if evidence_eligible else []
    paper_ceiling = _paper_exposure_ceiling(regime, cfg) if evidence_eligible else 0.0

    if hard_blocking_reasons:
        status = "blocked"
    elif stale_evidence:
        status = "partial"
    elif is_local_sample:
        status = "warning"
    else:
        status = "passed"

    transition_reasons = [
        code
        for code in visible_reasons
        if code.startswith("transition_")
        or code.startswith("recovery_")
        or code.endswith("_confirmed")
        or code == "sideways_mixed_evidence"
    ]
    boundary = {
        "research_only": True,
        "paper_only": True,
        "paper_account_only": True,
        "no_broker": True,
        "no_broker_route": True,
        "is_real_order": False,
        "no_real_orders": True,
        "live_execution_locked": True,
        "qmt_order_route_locked": True,
        "not_investment_advice": True,
        "not_position_advice": True,
    }
    target_text = _date_text(source.get("target_date"))
    data_date_text = _date_text(source.get("data_date"))
    if isinstance(supplied_coverage, Mapping):
        coverage = deepcopy(dict(supplied_coverage))
    else:
        coverage = {
            "kind": data_scope or "missing",
            "sample_count": _as_mapping(components.get("breadth")).get("sample_count"),
            "ratio": None,
            "scope_is_full_market": scope_is_full_market,
        }
    supplied_provenance = source.get("provenance")
    provenance = (
        deepcopy(dict(supplied_provenance))
        if isinstance(supplied_provenance, Mapping)
        else {}
    )
    provenance.setdefault("source_type", "caller_supplied_evidence")
    provenance.setdefault("source_path", None)
    provenance.setdefault("source_hash", None)
    supplied_network_used = _strict_bool(provenance.get("network_used"))
    provenance["network_used"] = (
        supplied_network_used if supplied_network_used is not None else False
    )
    provenance["facade_network_used"] = False
    if "canonical_write" in provenance:
        provenance["source_canonical_write"] = provenance.get("canonical_write")
    provenance["canonical_write"] = False
    supplied_pit_status = source.get("pit_status")
    pit_status = (
        deepcopy(dict(supplied_pit_status))
        if isinstance(supplied_pit_status, Mapping)
        else {"status": "not_supplied", "formal_pit_applicable": False}
    )
    return {
        "schema_version": "1.0",
        "task_type": "market_brain_research_snapshot",
        "status": status,
        "ok": evidence_eligible,
        "regime": regime,
        "market_regime": regime,
        "formal_regime": regime if evidence_eligible else "UNKNOWN",
        "regime_label": _REGIME_LABELS[regime],
        "confidence": confidence,
        "evidence_eligible": evidence_eligible,
        "target_date": target_text,
        "data_date": data_date_text,
        "as_of": target_text,
        "data_cutoff": data_date_text,
        "data_scope": data_scope,
        "scope_is_full_market": scope_is_full_market,
        "coverage": coverage,
        "provenance": provenance,
        "pit_status": pit_status,
        "component_scores": scores,
        "component_evidence": components,
        "reason_codes": visible_reasons,
        "degraded_reasons": list(visible_reasons),
        "previous_regime": previous_regime or None,
        "transition": {
            "from_regime": previous_regime or None,
            "to_regime": regime,
            "reason_codes": transition_reasons,
            "recovery_source_allowed": previous_regime in {"BEAR", "CRASH"},
        },
        "recommended_strategies": capabilities,
        "compatible_research_strategies": capabilities,
        "strategy_capability": {
            "mode": "research_compatibility_only",
            "capabilities": capabilities,
            "changes_existing_strategy_formulas": False,
            "changes_scoring_weights": False,
            "changes_backtest_or_paper_behavior": False,
            "is_investment_instruction": False,
        },
        "paper_exposure_ceiling": paper_ceiling,
        "max_paper_exposure": paper_ceiling,
        "paper_exposure_unit": "fraction_of_paper_account_only",
        "price_data_contract_version": contract_version,
        "price_data_contract": contract,
        "price_policy_validation": raw_price_validation,
        "price_policy": price_policy,
        **basis_labels,
        "price_roles": {
            "current_enforced": "single_known_research_signal_price_basis",
            "future_target": "forward_adjusted_signal_plus_raw_execution",
            "future_target_active": False,
            "price_conversion_performed": False,
        },
        "disclaimer": _DISCLAIMER,
        **boundary,
    }


def _frame_preflight(
    frame: pd.DataFrame | None,
) -> tuple[str, str, dict[str, Any]]:
    observed_basis, classification_reason = classify_frame_price_basis(frame)  # type: ignore[arg-type]
    validation = validate_price_frame_set(
        {"market_index": frame},  # type: ignore[dict-item]
        consumer="feature",
    )
    reasons = _reason_list(validation.get("reason_codes"))
    if classification_reason:
        _append_reason(reasons, classification_reason)
    eligible = bool(validation.get("eligible")) and not classification_reason
    payload = deepcopy(validation)
    payload.update(
        {
            "ok": eligible,
            "eligible": eligible,
            "status": "passed" if eligible else "blocked",
            "observed_basis": observed_basis,
            "accepted_price_basis": (
                observed_basis if eligible else PRICE_BASIS_UNKNOWN
            ),
            "reason_codes": reasons,
            "consumer": "market_brain",
            "consumer_policy_source": "feature",
            "market_brain_price_role": "research_signal_price",
        }
    )
    return observed_basis, classification_reason, payload


def build_market_brain_from_frame(
    frame: pd.DataFrame | None,
    *,
    target_date: str,
    breadth: Mapping[str, Any] | None = None,
    volume: Mapping[str, Any] | None = None,
    sector: Mapping[str, Any] | None = None,
    data_scope: str = "local_sample",
    previous_regime: str | None = None,
    config: MarketRegimeConfig | None = None,
) -> dict[str, Any]:
    """Build a Market Brain snapshot from an in-memory index-price frame.

    The original frame is classified and validated before copying, cutoff,
    sorting, duplicate removal, or indicator calculation, as required by the
    Price Data Contract.
    """

    cfg = config or MarketRegimeConfig()
    source_row_count = len(frame) if isinstance(frame, pd.DataFrame) else 0
    observed_basis, classification_reason, validation = _frame_preflight(frame)

    base_evidence: dict[str, Any] = {
        "target_date": target_date,
        "data_date": "",
        "data_scope": data_scope,
        "breadth": dict(breadth or {}),
        "volume": dict(volume or {}),
        "sector": dict(sector or {}),
        "previous_regime": previous_regime,
        "price_policy_validation": validation,
        "provenance": {
            "source_type": "local_in_memory_frame",
            "source_path": None,
            "source_hash": None,
        },
        "pit_status": {
            "status": "not_applicable_benchmark_regime",
            "formal_pit_applicable": False,
        },
    }
    precheck_reasons: list[str] = []
    if classification_reason:
        _append_reason(precheck_reasons, classification_reason)
    for code in _config_reason_codes(cfg):
        _append_reason(precheck_reasons, code)

    target_timestamp = _parse_date(target_date)
    if target_timestamp is None:
        _append_reason(precheck_reasons, "missing_or_invalid_target_date")
    if not isinstance(frame, pd.DataFrame) or frame.empty:
        _append_reason(precheck_reasons, "missing_index_price_frame")
    elif "date" not in frame.columns:
        _append_reason(precheck_reasons, "missing_frame_date_column")
    elif "close" not in frame.columns:
        _append_reason(precheck_reasons, "missing_frame_close_column")

    as_of_row_count = 0
    latest_observation_date = ""
    if not validation.get("eligible") or precheck_reasons or target_timestamp is None:
        base_evidence["precheck_reason_codes"] = precheck_reasons
        result = build_market_brain_snapshot(base_evidence, config=cfg)
    else:
        normalised = frame.copy(deep=True)
        # Scalar normalisation handles aware and naive dates uniformly while
        # retaining each source date's stated local calendar day.
        normalised["date"] = normalised["date"].map(_parse_date)
        if normalised["date"].isna().any():
            _append_reason(precheck_reasons, "invalid_frame_date_value")
        normalised = normalised.dropna(subset=["date"])
        normalised = normalised.loc[normalised["date"] <= target_timestamp]
        normalised = normalised.sort_values("date", kind="stable")
        normalised = normalised.drop_duplicates(subset=["date"], keep="last")
        as_of_row_count = len(normalised)
        boolean_close = normalised["close"].map(lambda value: isinstance(value, bool))
        normalised["close"] = pd.to_numeric(normalised["close"], errors="coerce")
        if (
            boolean_close.any()
            or normalised["close"].isna().any()
            or (normalised["close"] <= 0).any()
        ):
            _append_reason(precheck_reasons, "invalid_frame_close_value")
        usable = normalised.dropna(subset=["close"])
        usable = usable.loc[usable["close"] > 0]

        if not usable.empty:
            latest_observation_date = pd.Timestamp(usable["date"].iloc[-1]).strftime("%Y-%m-%d")
            base_evidence["data_date"] = latest_observation_date
        required_rows = max(
            cfg.min_history_rows,
            cfg.momentum_lookback_rows + 1,
            cfg.short_ma_window_rows,
            cfg.long_ma_window_rows,
            cfg.volatility_window_rows + 1,
            cfg.drawdown_window_rows,
        ) if not _config_reason_codes(cfg) else 1
        if len(usable) < required_rows:
            _append_reason(precheck_reasons, "insufficient_index_history")
        if not precheck_reasons:
            closes = usable["close"].astype(float)
            recent_returns = (
                closes.pct_change(fill_method=None)
                .dropna()
                .tail(cfg.volatility_window_rows)
            )
            volatility_20d = (
                float(recent_returns.std(ddof=0)) if not recent_returns.empty else None
            )
            close = float(closes.iloc[-1])
            ma20 = float(closes.tail(cfg.short_ma_window_rows).mean())
            ma60 = float(closes.tail(cfg.long_ma_window_rows).mean())
            return_5d = float(
                close / closes.iloc[-(cfg.momentum_lookback_rows + 1)] - 1.0
            )
            drawdown_20d = float(
                close / closes.tail(cfg.drawdown_window_rows).max() - 1.0
            )
            index_payload = {
                "close": close,
                "ma20": ma20,
                "ma60": ma60,
                "return_5d": return_5d,
                "drawdown_20d": drawdown_20d,
                "volatility_20d": volatility_20d,
            }
            if any(_safe_float(value) is None for value in index_payload.values()):
                _append_reason(precheck_reasons, "invalid_derived_index_evidence")
            else:
                base_evidence["index"] = index_payload

        if precheck_reasons:
            base_evidence["precheck_reason_codes"] = precheck_reasons
        result = build_market_brain_snapshot(base_evidence, config=cfg)

    return {
        **result,
        "source_type": "local_in_memory_frame",
        "source_row_count": source_row_count,
        "as_of_row_count": as_of_row_count,
        "latest_observation_date": latest_observation_date,
        "frame_preflight_completed": True,
        "price_basis_validated_before_transformation": True,
        "price_policy": {
            **dict(result.get("price_policy") or {}),
            "observed_basis": observed_basis,
        },
    }


__all__ = [
    "MarketRegimeConfig",
    "build_market_brain_snapshot",
    "build_market_brain_from_frame",
]
