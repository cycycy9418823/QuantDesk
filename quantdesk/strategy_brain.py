"""Deterministic Strategy Brain compatibility facade.

This module wraps existing strategy definitions and the legacy research
recommender.  It is deliberately shadow-only: it does not write candidate,
watchlist, portfolio, paper-account, or broker state and it does not change
strategy formulas or scoring weights.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict, dataclass, field
from datetime import date, datetime
import hashlib
import json
import math
from pathlib import Path
from typing import Any, Mapping

from quantdesk.price_basis import PRICE_DATA_CONTRACT_VERSION
from quantdesk.research.strategy_recommender import recommend_strategy_for_symbol
from quantdesk.strategy_registry import ALIASES, load_strategy_registry


_KNOWN_PRICE_BASES = {"raw", "forward_adjusted", "back_adjusted"}
_LEGACY_TO_CANONICAL = {
    "long_term": "strategy_lab_long_term",
    "long_swing": "sandbox_long",
    "board": "sandbox_board",
    "short_swing": "ma_pullback",
}
_BOUNDARY = {
    "research_only": True,
    "paper_only": True,
    "paper_account_only": True,
    "no_broker": True,
    "no_broker_route": True,
    "is_real_order": False,
    "no_real_orders": True,
    "not_investment_advice": True,
    "not_position_advice": True,
}


@dataclass(frozen=True)
class StrategySpec:
    strategy_id: str
    version: str
    name: str
    description: str
    required_features: tuple[str, ...]
    entry_conditions: tuple[dict[str, Any], ...]
    exit_conditions: tuple[dict[str, Any], ...]
    score_weights: dict[str, float]
    market_fit: tuple[str, ...]
    risk_class: str
    evidence_kind: str = "registry_strategy_spec"
    evidence_status: str = "compatibility_spec"
    shadow_only: bool = True
    research_only: bool = True
    paper_only: bool = True
    no_broker: bool = True

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class StrategyBrainConfig:
    shadow_mode: bool = True
    required_candidate_features: tuple[str, ...] = (
        "close",
        "ma20",
        "ma60",
        "liquidity_score",
        "volatility",
    )
    accepted_candidate_price_bases: tuple[str, ...] = (
        "raw",
        "forward_adjusted",
        "back_adjusted",
    )
    accepted_pit_statuses: tuple[str, ...] = ("passed",)
    strategy_spec_version: str = "1.0"
    strict_resolver: bool = True
    allow_state_writes: bool = False
    strategy_id_map: dict[str, str] = field(default_factory=lambda: dict(_LEGACY_TO_CANONICAL))


def _as_mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _append_reason(reasons: list[str], code: str) -> None:
    if code and code not in reasons:
        reasons.append(code)


def _safe_float(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _parse_date(value: Any) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).date()
    except ValueError:
        try:
            return date.fromisoformat(text)
        except ValueError:
            return None


def _stable_hash(value: Any) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return "sha256:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _market_fit_for_strategy(strategy_id: str) -> tuple[str, ...]:
    sid = str(strategy_id or "").lower()
    if any(token in sid for token in ("board", "limit_up", "new_high", "breakout", "volume")):
        return ("BULL", "RECOVERY")
    if any(token in sid for token in ("rsi", "pullback", "reversal", "repair")):
        return ("SIDEWAYS", "RECOVERY")
    if any(token in sid for token in ("long_term", "low_vol", "quality", "value")):
        return ("BULL", "SIDEWAYS", "RECOVERY")
    if any(token in sid for token in ("risk", "avoid", "control")):
        return ("BULL", "BEAR", "SIDEWAYS", "CRASH", "RECOVERY", "UNKNOWN")
    return ("BULL", "SIDEWAYS", "RECOVERY")


def _risk_class_for_strategy(strategy_id: str) -> str:
    sid = str(strategy_id or "").lower()
    if any(token in sid for token in ("board", "limit_up", "new_high")):
        return "high_research_risk"
    if any(token in sid for token in ("long_term", "low_vol", "quality")):
        return "moderate_research_risk"
    return "standard_research_risk"


def _definition_to_spec(
    strategy_id: str,
    definition: Mapping[str, Any],
    *,
    config: StrategyBrainConfig,
) -> StrategySpec:
    required = tuple(str(item) for item in (definition.get("required_indicators") or ()))
    if not required:
        required = config.required_candidate_features
    market_fit = _market_fit_for_strategy(strategy_id)
    risk_class = _risk_class_for_strategy(strategy_id)
    version_hash = _stable_hash(
        {
            "strategy_id": strategy_id,
            "definition": dict(definition),
            "required_features": required,
            "market_fit": market_fit,
            "risk_class": risk_class,
        }
    ).split(":", 1)[1][:16]
    return StrategySpec(
        strategy_id=strategy_id,
        version=f"{config.strategy_spec_version}:{version_hash}",
        name=str(definition.get("name") or strategy_id),
        description=str(definition.get("description") or ""),
        required_features=required,
        entry_conditions=tuple(
            deepcopy(dict(item))
            for item in (definition.get("entry_conditions") or ())
            if isinstance(item, Mapping)
        ),
        exit_conditions=tuple(
            deepcopy(dict(item))
            for item in (definition.get("exit_conditions") or ())
            if isinstance(item, Mapping)
        ),
        score_weights={
            str(key): float(value)
            for key, value in dict(definition.get("score_weights") or {}).items()
        },
        market_fit=market_fit,
        risk_class=risk_class,
    )


def build_strategy_spec_catalog(
    path: str | Path | None = None,
    *,
    config: StrategyBrainConfig | None = None,
) -> dict[str, Any]:
    cfg = config or StrategyBrainConfig()
    registry, warning = load_strategy_registry(path)
    by_id = {
        strategy_id: _definition_to_spec(strategy_id, definition, config=cfg).to_dict()
        for strategy_id, definition in sorted(registry.items())
    }
    warnings = [warning] if warning else []
    source_path = str(
        (Path(path) if path else Path(__file__).resolve().parents[1] / "config" / "strategy_demo.yaml").resolve()
    )
    source_hash = _stable_hash(by_id)
    return {
        "schema_version": "1.0",
        "task_type": "strategy_spec_catalog",
        "status": "warning" if warnings else "passed",
        "ok": bool(by_id),
        "spec_count": len(by_id),
        "by_id": by_id,
        "specs": [by_id[key] for key in sorted(by_id)],
        "warnings": warnings,
        "reason_codes": ["strategy_registry_warning"] if warnings else [],
        "degraded_reasons": ["strategy_registry_warning"] if warnings else [],
        "as_of": None,
        "data_cutoff": None,
        "source_path": source_path,
        "source_hash": source_hash,
        "provenance": {"source_path": source_path, "source_hash": source_hash},
        "coverage": {"kind": "strategy_registry", "spec_count": len(by_id)},
        "pit_status": {"status": "not_applicable_static_strategy_registry"},
        "price_data_contract_version": PRICE_DATA_CONTRACT_VERSION,
        "price_policy_validation": {
            "status": "not_applicable_static_strategy_registry",
            "eligible": True,
        },
        "evidence_eligible": bool(by_id),
        "changes_existing_strategy_formulas": False,
        "changes_scoring_weights": False,
        "writes_state": False,
        **_BOUNDARY,
    }


def resolve_strategy_strict(
    strategy_id: str | None,
    path: str | Path | None = None,
    *,
    config: StrategyBrainConfig | None = None,
) -> dict[str, Any]:
    cfg = config or StrategyBrainConfig()
    requested = str(strategy_id or "").strip()
    canonical = ALIASES.get(requested, requested)
    catalog = build_strategy_spec_catalog(path, config=cfg)
    strategy = _as_mapping(catalog.get("by_id")).get(canonical)
    if not requested:
        reasons = ["missing_strategy_id"]
    elif not isinstance(strategy, Mapping):
        reasons = ["unknown_strategy_id"]
    else:
        reasons = []
    return {
        "schema_version": "1.0",
        "ok": not reasons,
        "status": "passed" if not reasons else "blocked",
        "requested_strategy_id": requested,
        "canonical_strategy_id": canonical if not reasons else None,
        "strategy": deepcopy(dict(strategy)) if isinstance(strategy, Mapping) else None,
        "reason_codes": reasons,
        "degraded_reasons": list(reasons),
        "as_of": None,
        "data_cutoff": None,
        "source_path": catalog.get("source_path"),
        "source_hash": catalog.get("source_hash"),
        "provenance": deepcopy(catalog.get("provenance")),
        "coverage": deepcopy(catalog.get("coverage")),
        "pit_status": deepcopy(catalog.get("pit_status")),
        "price_data_contract_version": PRICE_DATA_CONTRACT_VERSION,
        "price_policy_validation": deepcopy(catalog.get("price_policy_validation")),
        "evidence_eligible": not reasons,
        "legacy_fallback_used": False,
        "writes_state": False,
        **_BOUNDARY,
    }


def _numeric_feature_present(candidate: Mapping[str, Any], feature: str) -> bool:
    aliases = {
        "close": ("close", "latest_close", "price"),
        "ma20": ("ma20", "MA20"),
        "ma60": ("ma60", "MA60"),
        "liquidity_score": ("liquidity_score",),
        "volatility": ("volatility", "vol20"),
    }.get(feature, (feature,))
    for alias in aliases:
        value = _safe_float(candidate.get(alias))
        if value is None:
            continue
        if feature in {"close", "ma20", "ma60"} and value <= 0:
            continue
        if feature in {"liquidity_score", "volatility"} and value < 0:
            continue
        return True
    return False


def _candidate_price_policy_ok(candidate: Mapping[str, Any], config: StrategyBrainConfig) -> bool:
    policy = _as_mapping(candidate.get("price_policy_validation"))
    if not policy:
        return False
    explicit_eligible = policy.get("eligible")
    if explicit_eligible is None:
        explicit_eligible = policy.get("ok")
    if explicit_eligible is not True:
        return False
    explicit_ok = policy.get("ok")
    if explicit_ok is not None and explicit_ok is not True:
        return False
    if str(policy.get("status") or "") not in {"passed", "ok"}:
        return False
    if str(policy.get("contract_version") or "") != PRICE_DATA_CONTRACT_VERSION:
        return False
    if policy.get("price_conversion_performed") is not False:
        return False
    future_target_active = policy.get("future_target_active")
    if future_target_active is not None and future_target_active is not False:
        return False
    if str(policy.get("consumer") or "") != "feature":
        return False
    accepted = str(policy.get("accepted_price_basis") or "")
    observed = str(policy.get("observed_basis") or "")
    allowed = set(config.accepted_candidate_price_bases)
    if accepted not in allowed or accepted not in _KNOWN_PRICE_BASES or observed != accepted:
        return False
    basis_counts = policy.get("basis_counts")
    if not isinstance(basis_counts, Mapping):
        return False
    counted: set[str] = set()
    for basis, count in basis_counts.items():
        numeric_count = _safe_float(count)
        if numeric_count is not None and numeric_count > 0:
            counted.add(str(basis))
    if counted != {accepted}:
        return False
    consumer_allowed = _as_mapping(policy.get("consumer_policy")).get("allowed_basis")
    if not isinstance(consumer_allowed, (list, tuple, set)) or accepted not in {
        str(item) for item in consumer_allowed
    }:
        return False
    if list(policy.get("reason_codes") or []):
        return False
    if list(policy.get("blocked_symbols") or []):
        return False
    return True


def _candidate_pit_ok(candidate: Mapping[str, Any], config: StrategyBrainConfig) -> bool:
    status = str(_as_mapping(candidate.get("pit_status")).get("status") or "")
    return status == "passed" and status in set(config.accepted_pit_statuses)


def _market_envelope_reasons(market: Mapping[str, Any]) -> list[str]:
    reasons: list[str] = []
    if market.get("evidence_eligible") is not True:
        _append_reason(reasons, "market_evidence_ineligible")
    as_of = _parse_date(market.get("as_of") or market.get("target_date"))
    cutoff = _parse_date(market.get("data_cutoff") or market.get("data_date"))
    if as_of is None:
        _append_reason(reasons, "market_as_of_missing_or_invalid")
    if cutoff is None:
        _append_reason(reasons, "market_data_cutoff_missing_or_invalid")
    if as_of is not None and cutoff is not None and cutoff > as_of:
        _append_reason(reasons, "market_data_cutoff_after_as_of")
    provenance = _as_mapping(market.get("provenance"))
    if not str(provenance.get("source_path") or market.get("source_path") or ""):
        _append_reason(reasons, "market_source_path_missing")
    if not str(provenance.get("source_hash") or market.get("source_hash") or ""):
        _append_reason(reasons, "market_source_hash_missing")
    if not _as_mapping(market.get("coverage")):
        _append_reason(reasons, "market_coverage_missing")
    if not str(_as_mapping(market.get("pit_status")).get("status") or ""):
        _append_reason(reasons, "market_pit_status_missing")
    if str(market.get("price_data_contract_version") or "") != PRICE_DATA_CONTRACT_VERSION:
        _append_reason(reasons, "market_price_contract_version_mismatch")
    price_policy = _as_mapping(market.get("price_policy"))
    if price_policy.get("eligible") is not True or str(price_policy.get("status") or "") != "passed":
        _append_reason(reasons, "market_price_policy_ineligible")
    observed = str(price_policy.get("observed_basis") or "")
    accepted = str(price_policy.get("accepted_price_basis") or "")
    if observed not in _KNOWN_PRICE_BASES or accepted != observed:
        _append_reason(reasons, "market_price_policy_basis_mismatch")
    if list(price_policy.get("reason_codes") or []):
        _append_reason(reasons, "market_price_policy_has_reasons")
    future_target_active = price_policy.get("future_target_active")
    if future_target_active is not None and future_target_active is not False:
        _append_reason(reasons, "market_future_price_target_not_active")
    return reasons


def _mainline_evidence_reasons(
    mainline: Mapping[str, Any],
    *,
    as_of: str,
    data_cutoff: str,
) -> list[str]:
    if not mainline:
        return []
    reasons: list[str] = []
    if mainline.get("evidence_eligible") is not True:
        _append_reason(reasons, "mainline_evidence_ineligible")
    if str(mainline.get("status") or "") != "passed":
        _append_reason(reasons, "mainline_status_ineligible")
    expected_as_of = _parse_date(as_of)
    expected_cutoff = _parse_date(data_cutoff)
    observed_as_of = _parse_date(mainline.get("as_of") or mainline.get("target_date"))
    observed_cutoff = _parse_date(mainline.get("data_cutoff"))
    if observed_as_of is None or observed_as_of != expected_as_of:
        _append_reason(reasons, "mainline_evidence_as_of_mismatch")
    if observed_cutoff is None or observed_cutoff != expected_cutoff:
        _append_reason(reasons, "mainline_evidence_data_cutoff_mismatch")
    if list(mainline.get("reason_codes") or []):
        _append_reason(reasons, "mainline_evidence_has_reasons")
    return reasons


def _sector_evidence(
    sector_snapshot: Mapping[str, Any] | None,
    *,
    as_of: str,
    data_cutoff: str,
    config: StrategyBrainConfig,
) -> tuple[dict[str, Any], dict[str, Mapping[str, Any]], list[str]]:
    snapshot = _as_mapping(sector_snapshot)
    if not snapshot:
        return {
            "status": "missing",
            "used_for_formal_fit": False,
            "as_of": "",
        }, {}, []
    pit_status = str(_as_mapping(snapshot.get("pit_status")).get("status") or "")
    snapshot_as_of = str(snapshot.get("as_of") or "")
    snapshot_cutoff = str(snapshot.get("data_cutoff") or "")
    reasons: list[str] = []
    if pit_status != "passed":
        reasons.append("sector_evidence_non_pit")
    if as_of and not snapshot_as_of:
        reasons.append("sector_evidence_as_of_missing")
    if as_of and snapshot_as_of and snapshot_as_of != as_of:
        reasons.append("sector_evidence_as_of_mismatch")
    if data_cutoff and not snapshot_cutoff:
        reasons.append("sector_evidence_data_cutoff_missing")
    if data_cutoff and snapshot_cutoff and snapshot_cutoff != data_cutoff:
        reasons.append("sector_evidence_data_cutoff_mismatch")
    if str(snapshot.get("status") or "") not in {"passed", "ok"}:
        reasons.append("sector_evidence_status_ineligible")
    explicit_eligible = snapshot.get("evidence_eligible")
    if explicit_eligible is not True:
        reasons.append("sector_evidence_ineligible")
    provenance = _as_mapping(snapshot.get("provenance"))
    source_path = str(snapshot.get("source_path") or provenance.get("source_path") or "")
    source_hash = str(snapshot.get("source_hash") or provenance.get("source_hash") or "")
    if not source_path or not source_hash:
        reasons.append("sector_evidence_source_missing")
    if not _as_mapping(snapshot.get("coverage")):
        reasons.append("sector_evidence_coverage_missing")
    if not _candidate_price_policy_ok(snapshot, config):
        reasons.append("sector_evidence_price_policy_ineligible")
    used = not reasons
    raw_sectors = snapshot.get("sectors")
    lookup: dict[str, Mapping[str, Any]] = {}
    if isinstance(raw_sectors, Mapping):
        lookup = {
            str(key): value
            for key, value in raw_sectors.items()
            if isinstance(value, Mapping)
        }
    elif isinstance(raw_sectors, list):
        for row in raw_sectors:
            if not isinstance(row, Mapping):
                continue
            key = str(row.get("sector") or row.get("sector_name") or "")
            if key:
                lookup[key] = row
    return {
        "status": "passed" if used else (pit_status or "blocked"),
        "used_for_formal_fit": used,
        "as_of": snapshot_as_of,
        "data_cutoff": snapshot_cutoff,
        "pit_status": pit_status,
        "reason_codes": reasons,
    }, lookup if used else {}, reasons


def build_strategy_brain_snapshot(
    candidates: list[Mapping[str, Any]] | tuple[Mapping[str, Any], ...] | None,
    market_snapshot: Mapping[str, Any] | None,
    *,
    mainline_snapshot: Mapping[str, Any] | None = None,
    sector_snapshot: Mapping[str, Any] | None = None,
    config: StrategyBrainConfig | None = None,
) -> dict[str, Any]:
    cfg = config or StrategyBrainConfig()
    market = deepcopy(dict(_as_mapping(market_snapshot)))
    as_of = str(market.get("as_of") or market.get("target_date") or "")
    data_cutoff = str(market.get("data_cutoff") or market.get("data_date") or "")
    reasons: list[str] = []
    config_eligible = (
        cfg.shadow_mode is True
        and cfg.allow_state_writes is False
        and cfg.strict_resolver is True
    )
    if not config_eligible:
        _append_reason(reasons, "strategy_brain_unsafe_config")
    market_reasons = _market_envelope_reasons(market)
    for code in market_reasons:
        _append_reason(reasons, code)
    market_eligible = not market_reasons

    mainline = _as_mapping(mainline_snapshot)
    mainline_reasons = _mainline_evidence_reasons(
        mainline,
        as_of=as_of,
        data_cutoff=data_cutoff,
    )
    mainline_used = bool(mainline) and not mainline_reasons
    for code in mainline_reasons:
        _append_reason(reasons, code)

    sector_meta, sector_lookup, sector_reasons = _sector_evidence(
        sector_snapshot,
        as_of=as_of,
        data_cutoff=data_cutoff,
        config=cfg,
    )
    for code in sector_reasons:
        _append_reason(reasons, code)

    decisions: list[dict[str, Any]] = []
    formal_fits: list[dict[str, Any]] = []
    for source_candidate in candidates or []:
        candidate = deepcopy(dict(_as_mapping(source_candidate)))
        symbol = str(candidate.get("symbol") or "")
        decision_reasons: list[str] = []
        if not symbol:
            _append_reason(decision_reasons, "candidate_symbol_missing")
        missing = [
            feature
            for feature in cfg.required_candidate_features
            if not _numeric_feature_present(candidate, feature)
        ]
        if missing:
            _append_reason(decision_reasons, "missing_required_features")
        if not _candidate_price_policy_ok(candidate, cfg):
            _append_reason(decision_reasons, "candidate_price_policy_ineligible")
        if not _candidate_pit_ok(candidate, cfg):
            _append_reason(decision_reasons, "candidate_pit_ineligible")
        candidate_as_of = str(candidate.get("as_of") or "")
        candidate_cutoff = str(candidate.get("data_cutoff") or "")
        candidate_as_of_date = _parse_date(candidate_as_of)
        candidate_cutoff_date = _parse_date(candidate_cutoff)
        market_as_of_date = _parse_date(as_of)
        market_cutoff_date = _parse_date(data_cutoff)
        if candidate_as_of_date is None or (
            market_as_of_date is not None and candidate_as_of_date != market_as_of_date
        ):
            _append_reason(decision_reasons, "candidate_as_of_mismatch")
        if candidate_cutoff_date is None or (
            market_cutoff_date is not None and candidate_cutoff_date != market_cutoff_date
        ):
            _append_reason(decision_reasons, "candidate_data_cutoff_mismatch")
        if (
            candidate_as_of_date is not None
            and candidate_cutoff_date is not None
            and candidate_cutoff_date > candidate_as_of_date
        ):
            _append_reason(decision_reasons, "candidate_data_cutoff_after_as_of")
        if not str(candidate.get("source_path") or ""):
            _append_reason(decision_reasons, "candidate_source_path_missing")
        if not str(candidate.get("source_hash") or ""):
            _append_reason(decision_reasons, "candidate_source_hash_missing")
        if not _as_mapping(candidate.get("coverage")):
            _append_reason(decision_reasons, "candidate_coverage_missing")
        if not market_eligible:
            _append_reason(decision_reasons, "market_evidence_ineligible")
        if not config_eligible:
            _append_reason(decision_reasons, "strategy_brain_unsafe_config")

        sector_name = str(candidate.get("sector") or "")
        sector_row = sector_lookup.get(sector_name)
        legacy = recommend_strategy_for_symbol(
            symbol,
            candidate,
            dict(sector_row) if isinstance(sector_row, Mapping) else None,
        )
        legacy_id = str(legacy.get("primary_strategy") or "")
        mapped_id = cfg.strategy_id_map.get(legacy_id)
        strict = resolve_strategy_strict(mapped_id, config=cfg) if mapped_id else {
            "ok": False,
            "strategy": None,
            "reason_codes": ["legacy_observe_or_avoid"],
        }
        if mapped_id and not strict.get("ok"):
            _append_reason(decision_reasons, "canonical_strategy_unresolved")

        strategy_spec = _as_mapping(strict.get("strategy"))
        strategy_required_missing = [
            str(feature)
            for feature in (strategy_spec.get("required_features") or [])
            if not _numeric_feature_present(candidate, str(feature))
        ]
        if strategy_required_missing:
            _append_reason(decision_reasons, "strategy_required_features_missing")
            missing = list(dict.fromkeys([*missing, *strategy_required_missing]))
        formal_regime = str(market.get("formal_regime") or market.get("regime") or "UNKNOWN")
        market_fit = {str(item) for item in (strategy_spec.get("market_fit") or [])}
        if strategy_spec and formal_regime not in market_fit:
            _append_reason(decision_reasons, "strategy_market_fit_mismatch")

        decision_eligible = not decision_reasons and bool(strict.get("ok"))
        canonical_id = str(strict.get("canonical_strategy_id") or "") if decision_eligible else None
        decision = {
            "schema_version": "1.0",
            "symbol": symbol,
            "candidate_status": "shadow_fit_observation" if decision_eligible else "excluded_pending_research",
            "evidence_eligible": decision_eligible,
            "missing_features": missing,
            "reason_codes": decision_reasons,
            "degraded_reasons": list(decision_reasons),
            "as_of": candidate_as_of,
            "data_cutoff": candidate_cutoff,
            "source_path": str(candidate.get("source_path") or ""),
            "source_hash": str(candidate.get("source_hash") or ""),
            "provenance": {
                "source_path": str(candidate.get("source_path") or ""),
                "source_hash": str(candidate.get("source_hash") or ""),
            },
            "coverage": deepcopy(candidate.get("coverage")),
            "pit_status": deepcopy(candidate.get("pit_status")),
            "price_data_contract_version": str(
                _as_mapping(candidate.get("price_policy_validation")).get("contract_version") or ""
            ),
            "price_policy_validation": deepcopy(candidate.get("price_policy_validation")),
            "legacy_recommendation": legacy,
            "canonical_strategy_id": canonical_id,
            "strategy_spec": deepcopy(strict.get("strategy")) if decision_eligible else None,
            "strategy_version": strategy_spec.get("version") if decision_eligible else None,
            "sector_evidence_used": bool(sector_row),
            "writes_state": False,
            **_BOUNDARY,
        }
        decisions.append(decision)
        if decision_eligible:
            formal_fits.append(
                {
                    "schema_version": "1.0",
                    "symbol": symbol,
                    "strategy_id": canonical_id,
                    "fit_kind": "shadow_only",
                    "confidence": legacy.get("confidence"),
                    "as_of": candidate_as_of,
                    "data_cutoff": candidate_cutoff,
                    "source_path": str(candidate.get("source_path") or ""),
                    "source_hash": str(candidate.get("source_hash") or ""),
                    "provenance": {
                        "source_path": str(candidate.get("source_path") or ""),
                        "source_hash": str(candidate.get("source_hash") or ""),
                    },
                    "strategy_version": strategy_spec.get("version"),
                    "price_data_contract_version": str(
                        _as_mapping(candidate.get("price_policy_validation")).get("contract_version") or ""
                    ),
                    "price_policy_validation": deepcopy(candidate.get("price_policy_validation")),
                    "pit_status": deepcopy(candidate.get("pit_status")),
                    "coverage": deepcopy(candidate.get("coverage")),
                    "evidence_eligible": True,
                    "reason_codes": [],
                    "degraded_reasons": [],
                    "writes_state": False,
                    **_BOUNDARY,
                }
            )

    evidence_eligible = config_eligible and market_eligible and any(
        item["evidence_eligible"] for item in decisions
    )
    status = (
        "passed"
        if evidence_eligible and not reasons
        else "warning"
        if config_eligible and market_eligible
        else "blocked"
    )
    return {
        "schema_version": "1.0",
        "task_type": "strategy_brain_shadow_snapshot",
        "status": status,
        "ok": config_eligible and market_eligible,
        "shadow_mode": True,
        "evidence_eligible": evidence_eligible,
        "as_of": as_of,
        "data_cutoff": data_cutoff,
        "market_regime": market.get("formal_regime") or market.get("regime") or "UNKNOWN",
        "market_evidence": {
            "eligible": market_eligible,
            "reason_codes": deepcopy(list(market.get("reason_codes") or [])),
        },
        "mainline_evidence": {
            "supplied": bool(mainline),
            "used_for_formal_fit": mainline_used,
            "reason_codes": mainline_reasons,
        },
        "sector_evidence": sector_meta,
        "candidate_decisions": decisions,
        "formal_strategy_fits": formal_fits if market_eligible else [],
        "candidate_count": len(decisions),
        "eligible_candidate_count": sum(1 for item in decisions if item["evidence_eligible"]),
        "reason_codes": reasons,
        "degraded_reasons": list(reasons),
        "source_path": str(_as_mapping(market.get("provenance")).get("source_path") or ""),
        "source_hash": str(_as_mapping(market.get("provenance")).get("source_hash") or ""),
        "coverage": deepcopy(market.get("coverage")),
        "provenance": deepcopy(market.get("provenance")),
        "pit_status": deepcopy(market.get("pit_status")),
        "price_data_contract_version": market.get("price_data_contract_version"),
        "price_policy": deepcopy(market.get("price_policy")),
        "price_policy_validation": deepcopy(market.get("price_policy")),
        "writes_state": False,
        "changes_existing_strategy_formulas": False,
        "changes_scoring_weights": False,
        **_BOUNDARY,
    }


__all__ = [
    "StrategySpec",
    "StrategyBrainConfig",
    "build_strategy_spec_catalog",
    "resolve_strategy_strict",
    "build_strategy_brain_snapshot",
]
