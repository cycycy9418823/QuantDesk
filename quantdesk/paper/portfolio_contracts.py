"""Portfolio Brain contracts for local research-only paper accounts."""

from __future__ import annotations

import hashlib
import json
import math
from copy import deepcopy
from dataclasses import asdict, dataclass, field, replace
from datetime import date
from typing import Any, Mapping

from quantdesk.paper.lifecycle import CandidateDecisionEvent, LifecycleValidationError
from quantdesk.paper.market_rules import (
    is_valid_order_quantity,
    lot_size_for_symbol,
    round_notional_down_to_lot_quantity,
)
from quantdesk.paper.trade_costs import buy_cash_outflow
from quantdesk.paper.trade_costs import sell_cash_inflow
from quantdesk.price_basis import price_data_contract
from quantdesk.version import API_SCHEMA_VERSION


DEFAULT_USER_CAPITAL = 10_000.0
ACCEPTANCE_CAPITAL = 100_000.0
USER_SCENARIO = "user_10k"
ACCEPTANCE_SCENARIO = "acceptance_100k"
# Research scenario with no candidate price ceiling. Same 100k paper capital as
# the acceptance scenario, but a separate ledger identity so it never shares or
# overwrites the legacy funded ledger.
UNRESTRICTED_SCENARIO = "unrestricted_100k"
UNRESTRICTED_CAPITAL = 100_000.0

_SCENARIO_CAPITAL: dict[str, float] = {
    USER_SCENARIO: DEFAULT_USER_CAPITAL,
    ACCEPTANCE_SCENARIO: ACCEPTANCE_CAPITAL,
    UNRESTRICTED_SCENARIO: UNRESTRICTED_CAPITAL,
}

_SCENARIO_SOURCE: dict[str, str] = {
    USER_SCENARIO: "default_new_account",
    ACCEPTANCE_SCENARIO: "explicit_acceptance_scenario",
    UNRESTRICTED_SCENARIO: "explicit_unrestricted_scenario",
}

_SCENARIO_LABEL: dict[str, str] = {
    USER_SCENARIO: "User 10k Paper Account",
    ACCEPTANCE_SCENARIO: "Acceptance 100k Paper Scenario",
    UNRESTRICTED_SCENARIO: "Unrestricted 100k Research Paper Account",
}


class PortfolioContractError(ValueError):
    """Raised when paper-capital evidence violates the portfolio contract."""


@dataclass(frozen=True)
class PaperCapitalProfile:
    profile_id: str
    scenario_id: str
    initial_capital: float
    currency: str = "CNY"
    paper_credit_enabled: bool = False
    credit_limit: float = 0.0
    simulated_interest_rate: float = 0.0
    source: str = "default_new_account"
    legacy_state_preserved: bool = False
    visible_label: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {**asdict(self), **_paper_boundary()}


@dataclass(frozen=True)
class PortfolioSnapshot:
    as_of: str
    ledger_id: str
    profile_id: str
    initial_capital: float
    cash_balance: float
    available_cash: float
    market_value: float
    gross_exposure: float
    net_exposure: float
    credit_limit: float
    credit_used: float
    credit_available: float
    simulated_interest: float
    net_equity: float
    valuation_price_basis: str
    allocation_eligible: bool
    reason_codes: tuple[str, ...]
    cash_source: str = "missing"
    position_count: int = 0
    schema_version: str = API_SCHEMA_VERSION
    data_cutoff: str = ""
    source_path: str = ""
    source_hash: str = ""
    snapshot_digest: str = ""
    price_data_contract_version: str = ""
    price_policy_validation: Mapping[str, Any] = field(default_factory=dict)
    pit_status: Mapping[str, Any] = field(default_factory=dict)
    coverage: Mapping[str, Any] = field(default_factory=dict)
    evidence_eligible: bool = False
    degraded_codes: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {**asdict(self), **_paper_boundary()}


@dataclass(frozen=True)
class PaperOrderIntent:
    intent_id: str
    source_lifecycle_event_id: str
    symbol: str
    side: str
    quantity: int
    lot_size: int
    reference_price: float
    reference_price_basis: str
    estimated_gross: float
    estimated_fee: float
    estimated_cash_out: float
    available_cash_before: float
    available_cash_after: float
    status: str
    reason_codes: tuple[str, ...]
    risk_gate_status: str = "pending"
    execution_status: str = "not_submitted"
    as_of: str = ""
    schema_version: str = API_SCHEMA_VERSION
    data_cutoff: str = ""
    source_path: str = ""
    source_hash: str = ""
    price_data_contract_version: str = ""
    price_policy_validation: Mapping[str, Any] = field(default_factory=dict)
    pit_status: Mapping[str, Any] = field(default_factory=dict)
    coverage: Mapping[str, Any] = field(default_factory=dict)
    evidence_eligible: bool = False
    degraded_codes: tuple[str, ...] = ()
    portfolio_source_hash: str = ""
    portfolio_snapshot_digest: str = ""
    intent_digest: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {**asdict(self), **_paper_boundary(), "funded": False}


def _paper_boundary() -> dict[str, bool]:
    return {
        "research_only": True,
        "paper_only": True,
        "is_real_order": False,
        "no_broker": True,
        "live_execution_locked": True,
        "not_investment_advice": True,
    }


def _finite(value: Any, *, field: str, nonnegative: bool = False) -> float:
    if isinstance(value, bool):
        raise PortfolioContractError(f"{field}_invalid")
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise PortfolioContractError(f"{field}_invalid") from exc
    if not math.isfinite(number) or (nonnegative and number < 0):
        raise PortfolioContractError(f"{field}_invalid")
    return number


def _optional_finite(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _state_account(state: Mapping[str, Any] | None) -> Mapping[str, Any]:
    source = state if isinstance(state, Mapping) else {}
    account = source.get("account")
    return account if isinstance(account, Mapping) else source


def _state_initial_capital(state: Mapping[str, Any] | None) -> float | None:
    account = _state_account(state)
    if "initial_cash" in account:
        return _optional_finite(account.get("initial_cash"))
    if "initial_capital" in account:
        return _optional_finite(account.get("initial_capital"))
    return None


def _canonical_digest(payload: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _has_funded_ledger_identity(
    state: Mapping[str, Any],
    *,
    profile: PaperCapitalProfile | None = None,
) -> bool:
    """Require an explicit paper-ledger identity or the one supported legacy shape."""

    account = _state_account(state)
    explicit_id = str(state.get("ledger_id") or state.get("portfolio_id") or "").strip()
    if explicit_id:
        return True
    schema_version = str(state.get("schema_version") or "").strip()
    profile_id = str(account.get("profile_id") or "").strip()
    scenario_id = str(account.get("scenario_id") or "").strip()
    if schema_version and (
        state.get("paper_trading_only") is True
        or (profile_id, scenario_id)
        in {
            (USER_SCENARIO, USER_SCENARIO),
            (ACCEPTANCE_SCENARIO, ACCEPTANCE_SCENARIO),
            (UNRESTRICTED_SCENARIO, UNRESTRICTED_SCENARIO),
            ("legacy_preserved", "legacy_existing"),
        }
    ):
        return True
    existing_capital = _state_initial_capital(state)
    return bool(
        profile is not None
        and profile.scenario_id == "legacy_existing"
        and state.get("paper_trading_only") is True
        and existing_capital is not None
        and abs(existing_capital - ACCEPTANCE_CAPITAL) <= 1e-9
    )


def _snapshot_integrity_digest(snapshot: PortfolioSnapshot) -> str:
    payload = asdict(snapshot)
    payload.pop("snapshot_digest", None)
    return f"sha256:{_canonical_digest(payload)}"


def compute_paper_order_intent_digest(intent: PaperOrderIntent) -> str:
    """Return a digest covering every intent field except the digest itself."""

    if not isinstance(intent, PaperOrderIntent):
        raise PortfolioContractError("paper_order_intent_type_invalid")
    payload = asdict(intent)
    payload.pop("intent_digest", None)
    return f"sha256:{_canonical_digest(payload)}"


def _price_contract_version() -> str:
    return str(price_data_contract().get("version") or "")


def _price_policy_validation(
    *,
    consumer: str,
    observed_basis: str,
    eligible: bool,
    reason_codes: tuple[str, ...],
) -> dict[str, Any]:
    contract = price_data_contract()
    policy = (contract.get("current_enforced") or {}).get(consumer) or {}
    return {
        "status": "passed" if eligible else "failed",
        "eligible": bool(eligible),
        "contract_version": str(contract.get("version") or ""),
        "consumer": consumer,
        "consumer_policy": deepcopy(policy),
        "observed_basis": observed_basis,
        "accepted_price_basis": observed_basis if eligible else None,
        "reason_codes": list(reason_codes),
    }


def _parse_iso_day(value: Any) -> date | None:
    try:
        return date.fromisoformat(str(value or "")[:10])
    except (TypeError, ValueError):
        return None


def resolve_paper_capital_profile(
    *,
    existing_state: Mapping[str, Any] | None = None,
    scenario_id: str | None = None,
    requested_initial_capital: float | None = None,
    paper_credit_enabled: bool = False,
    credit_limit: float = 0.0,
    simulated_interest_rate: float = 0.0,
    existing_state_verified: bool = False,
) -> PaperCapitalProfile:
    if not isinstance(existing_state_verified, bool):
        raise PortfolioContractError("existing_state_verified_must_be_boolean")
    if not isinstance(paper_credit_enabled, bool):
        raise PortfolioContractError("paper_credit_enabled_must_be_boolean")
    limit = _finite(credit_limit, field="credit_limit", nonnegative=True)
    interest_rate = _finite(
        simulated_interest_rate,
        field="simulated_interest_rate",
        nonnegative=True,
    )

    existing_account = _state_account(existing_state)
    has_existing_capital = "initial_cash" in existing_account or "initial_capital" in existing_account
    existing_capital = _state_initial_capital(existing_state)
    if has_existing_capital and existing_capital is None:
        raise PortfolioContractError("existing_initial_capital_invalid")
    if existing_capital is not None:
        if existing_capital <= 0:
            raise PortfolioContractError("existing_initial_capital_invalid")
        account = _state_account(existing_state)
        existing_profile = str(account.get("profile_id") or "").strip()
        existing_scenario = str(account.get("scenario_id") or "").strip()
        if existing_profile or existing_scenario:
            allowed_existing = {
                (scenario_name, scenario_name): scenario_capital
                for scenario_name, scenario_capital in _SCENARIO_CAPITAL.items()
            }
            if (existing_profile, existing_scenario) == ("legacy_preserved", "legacy_existing"):
                if (
                    abs(existing_capital - ACCEPTANCE_CAPITAL) > 1e-9
                    or not existing_state_verified
                ):
                    raise PortfolioContractError("legacy_existing_capital_not_supported")
                return PaperCapitalProfile(
                    profile_id="legacy_preserved",
                    scenario_id="legacy_existing",
                    initial_capital=round(existing_capital, 2),
                    paper_credit_enabled=paper_credit_enabled,
                    credit_limit=round(limit, 2),
                    simulated_interest_rate=interest_rate,
                    source="existing_funded_ledger",
                    legacy_state_preserved=True,
                    visible_label=str(
                        account.get("capital_profile_label")
                        or f"Legacy {existing_capital:,.0f} Paper State (Preserved)"
                    ),
                )
            expected_existing = allowed_existing.get((existing_profile, existing_scenario))
            if expected_existing is None or abs(existing_capital - expected_existing) > 1e-9:
                raise PortfolioContractError("existing_capital_profile_mismatch")
            return PaperCapitalProfile(
                profile_id=existing_profile,
                scenario_id=existing_scenario,
                initial_capital=round(existing_capital, 2),
                paper_credit_enabled=paper_credit_enabled,
                credit_limit=round(limit, 2),
                simulated_interest_rate=interest_rate,
                source="existing_funded_ledger",
                legacy_state_preserved=False,
                visible_label=str(account.get("capital_profile_label") or existing_profile),
            )
        if (
            abs(existing_capital - ACCEPTANCE_CAPITAL) > 1e-9
            or not existing_state_verified
        ):
            raise PortfolioContractError("legacy_existing_capital_not_supported")
        return PaperCapitalProfile(
            profile_id="legacy_preserved",
            scenario_id="legacy_existing",
            initial_capital=round(existing_capital, 2),
            paper_credit_enabled=paper_credit_enabled,
            credit_limit=round(limit, 2),
            simulated_interest_rate=interest_rate,
            source="existing_funded_ledger",
            legacy_state_preserved=True,
            visible_label=f"Legacy {existing_capital:,.0f} Paper State (Preserved)",
        )

    scenario = str(scenario_id or USER_SCENARIO).strip()
    if scenario not in _SCENARIO_CAPITAL:
        raise PortfolioContractError(f"scenario_id_not_allowed:{scenario}")
    expected = _SCENARIO_CAPITAL[scenario]
    capital = expected if requested_initial_capital is None else _finite(
        requested_initial_capital,
        field="requested_initial_capital",
        nonnegative=True,
    )
    if abs(capital - expected) > 1e-9:
        # Preserved wording: a 100k request under the 10k user scenario still
        # points the caller at the acceptance scenario by name.
        if capital == ACCEPTANCE_CAPITAL:
            raise PortfolioContractError("100000_requires_scenario_id=acceptance_100k")
        raise PortfolioContractError(f"scenario_capital_mismatch:{scenario}")
    return PaperCapitalProfile(
        profile_id=scenario,
        scenario_id=scenario,
        initial_capital=round(capital, 2),
        paper_credit_enabled=paper_credit_enabled,
        credit_limit=round(limit, 2),
        simulated_interest_rate=interest_rate,
        source=_SCENARIO_SOURCE[scenario],
        legacy_state_preserved=False,
        visible_label=_SCENARIO_LABEL[scenario],
    )


def build_portfolio_snapshot(
    ledger: Mapping[str, Any] | None,
    profile: PaperCapitalProfile,
    *,
    as_of: str,
    valuation_price_basis: str | None = None,
) -> PortfolioSnapshot:
    source = dict(ledger or {}) if isinstance(ledger, Mapping) else {}
    reasons: list[str] = []
    if not str(as_of or "").strip():
        reasons.append("as_of_missing")
    elif _parse_iso_day(as_of) is None:
        reasons.append("as_of_invalid")

    account = _state_account(source)
    is_watchlist = bool(source.get("watchlist_id")) or (
        isinstance(source.get("items"), list)
        and "cash" not in source
        and not isinstance(source.get("account"), Mapping)
    )
    candidate_markers = {
        "symbol",
        "code",
        "strategy_type",
        "score",
        "decision_origin",
        "effective_state",
        "requested_state",
        "funded",
    }
    looks_like_candidate = any(key in source for key in candidate_markers)
    has_position_ledger = isinstance(source.get("positions"), list)
    has_cash_evidence = "available_cash" in account or "cash" in account
    has_ledger_identity = _has_funded_ledger_identity(source, profile=profile)
    if source and not has_ledger_identity:
        reasons.append("funded_ledger_identity_missing")
    is_funded_ledger = bool(
        source
        and not is_watchlist
        and not looks_like_candidate
        and has_ledger_identity
        and has_position_ledger
        and has_cash_evidence
    )
    if not is_funded_ledger:
        reasons.append("not_a_funded_ledger")
        account = {}

    cash_source = "missing"
    cash_value: float | None = None
    if "available_cash" in account:
        cash_value = _optional_finite(account.get("available_cash"))
        cash_source = "funded_ledger.available_cash"
    elif "cash" in account:
        cash_value = _optional_finite(account.get("cash"))
        cash_source = "funded_ledger.cash"
    if cash_value is None:
        reasons.append("funded_ledger_cash_missing")
        cash_value = 0.0
    if cash_value < 0:
        reasons.append("negative_cash_balance")
        if not profile.paper_credit_enabled:
            reasons.append("negative_cash_without_credit")

    ledger_initial = _state_initial_capital(source)
    has_ledger_initial = "initial_cash" in account or "initial_capital" in account
    if has_ledger_initial and ledger_initial is None:
        reasons.append("ledger_initial_capital_invalid")
    elif ledger_initial is not None and abs(ledger_initial - profile.initial_capital) > 1e-6:
        reasons.append("capital_profile_mismatch")

    positions_raw = source.get("positions") if is_funded_ledger else []
    positions = [dict(row) for row in positions_raw if isinstance(row, Mapping)] if isinstance(positions_raw, list) else []
    market_value = 0.0
    funded_count = 0
    carried_forward_count = 0
    unknown_count = 0
    for row in positions:
        state = str(row.get("effective_state") or row.get("lifecycle_state") or "").strip()
        if row.get("funded") is False or state in {
            "paper_watchlist",
            "manual_observation",
            "excluded_pending_research",
            "research_candidate",
        }:
            reasons.append("unfunded_row_in_ledger")
            continue
        value = _optional_finite(row.get("market_value") or row.get("estimated_value"))
        if value is None:
            quantity = _optional_finite(row.get("quantity") or row.get("shares"))
            price = _optional_finite(row.get("latest_price") or row.get("mark_price"))
            value = quantity * price if quantity is not None and price is not None else None
        if value is None or value < 0:
            # No fresh mark: carry the last trustworthy frozen valuation forward
            # (explicitly marked) instead of under-counting equity; if none is
            # trustworthy, record the position as unknown and mark the snapshot
            # incomplete rather than dropping it silently.
            quantity = _optional_finite(row.get("quantity") or row.get("shares"))
            prior = _optional_finite(
                row.get("last_tradable_price") or row.get("settled_price")
            )
            if quantity is not None and prior is not None and prior > 0:
                value = quantity * prior
                reasons.append("position_market_value_carried_forward")
                carried_forward_count += 1
            else:
                reasons.append("position_market_value_missing")
                unknown_count += 1
                continue
        row_basis = str(row.get("price_basis") or row.get("valuation_price_basis") or "").strip().lower()
        if row_basis != "raw":
            reasons.append("position_price_basis_not_raw")
        market_value += value
        funded_count += 1

    if unknown_count:
        # A position with no trustworthy valuation must never be silently dropped
        # while a seemingly complete equity is still reported.
        reasons.append("equity_incomplete_unknown_positions")
    basis_text = str(valuation_price_basis or "").strip().lower()
    if funded_count:
        if basis_text != "raw":
            reasons.append("valuation_price_basis_not_raw")
        effective_basis = basis_text or "mixed_or_unknown"
    else:
        effective_basis = "cash_only"

    credit_used = 0.0
    if "credit_used" in account:
        parsed_credit_used = _optional_finite(account.get("credit_used"))
        if parsed_credit_used is None or parsed_credit_used < 0:
            reasons.append("credit_used_invalid")
        else:
            credit_used = parsed_credit_used
    interest = 0.0
    if "simulated_interest" in account:
        parsed_interest = _optional_finite(account.get("simulated_interest"))
        if parsed_interest is None or parsed_interest < 0:
            reasons.append("simulated_interest_invalid")
        else:
            interest = parsed_interest
    if not profile.paper_credit_enabled:
        if credit_used > 0:
            reasons.append("credit_used_while_disabled")
        credit_available = 0.0
    else:
        if credit_used > profile.credit_limit:
            reasons.append("credit_limit_exceeded")
        credit_available = max(profile.credit_limit - credit_used, 0.0)

    available = max(cash_value, 0.0) + credit_available
    gross_exposure = market_value
    # The current paper ledger is long-only. Financing is a separate liability,
    # not a short market position, so it affects equity but not net exposure.
    net_exposure = market_value
    net_equity = cash_value + market_value - credit_used - interest
    if net_equity <= 0:
        reasons.append("net_equity_not_positive")
    reason_tuple = tuple(dict.fromkeys(reasons))
    evidence_eligible = not reason_tuple
    ledger_id = str(source.get("ledger_id") or source.get("portfolio_id") or "paper_funded_ledger")
    provenance_source_path = (
        (source.get("provenance") or {}).get("source_path")
        if isinstance(source.get("provenance"), Mapping)
        else ""
    )
    source_path = str(provenance_source_path or "").strip() or f"memory://funded-ledger/{ledger_id}"
    source_hash = f"sha256:{_canonical_digest(source)}"
    policy_basis = effective_basis
    policy_ok = evidence_eligible and policy_basis in {"raw", "cash_only"}
    policy_reasons = () if policy_ok else reason_tuple or ("paper_valuation_price_basis_invalid",)
    snapshot = PortfolioSnapshot(
        as_of=str(as_of),
        ledger_id=ledger_id,
        profile_id=profile.profile_id,
        initial_capital=round(profile.initial_capital, 2),
        cash_balance=round(cash_value, 2),
        available_cash=round(available, 2),
        market_value=round(market_value, 2),
        gross_exposure=round(gross_exposure, 2),
        net_exposure=round(net_exposure, 2),
        credit_limit=round(profile.credit_limit, 2),
        credit_used=round(credit_used, 2),
        credit_available=round(credit_available, 2),
        simulated_interest=round(interest, 4),
        net_equity=round(net_equity, 2),
        valuation_price_basis=effective_basis,
        allocation_eligible=evidence_eligible,
        reason_codes=reason_tuple,
        cash_source=cash_source,
        position_count=funded_count,
        data_cutoff=str(as_of),
        source_path=source_path,
        source_hash=source_hash,
        price_data_contract_version=_price_contract_version(),
        price_policy_validation=_price_policy_validation(
            consumer="paper_valuation",
            observed_basis=policy_basis,
            eligible=policy_ok,
            reason_codes=policy_reasons,
        ),
        pit_status={"status": "not_applicable_existing_funded_ledger"},
        coverage={
            "kind": "funded_ledger_positions",
            "position_count": funded_count,
            "row_count": len(positions),
            "carried_forward_position_count": carried_forward_count,
            "unknown_position_count": unknown_count,
            "valuation_complete": unknown_count == 0,
        },
        evidence_eligible=evidence_eligible,
        degraded_codes=reason_tuple,
    )
    return replace(snapshot, snapshot_digest=_snapshot_integrity_digest(snapshot))


def build_paper_order_intent(
    lifecycle_event: Mapping[str, Any],
    snapshot: PortfolioSnapshot,
    *,
    reference_price: float,
    reference_price_basis: str | None,
    side: str = "buy",
    max_notional: float | None = None,
    requested_quantity: int | None = None,
) -> PaperOrderIntent:
    event = dict(lifecycle_event) if isinstance(lifecycle_event, Mapping) else {}
    reasons: list[str] = []
    try:
        verified_event = CandidateDecisionEvent.from_dict(event).to_dict()
        if verified_event != event:
            reasons.append("lifecycle_event_invalid")
    except (LifecycleValidationError, TypeError, ValueError):
        reasons.append("lifecycle_event_invalid")
    event_evidence = event.get("evidence")
    if not isinstance(event_evidence, Mapping):
        event_evidence = {}
        reasons.append("lifecycle_evidence_invalid")
    event_payload = event.get("payload")
    if not isinstance(event_payload, Mapping):
        event_payload = {}
        reasons.append("lifecycle_event_invalid")
    symbol = str(event.get("symbol") or "").strip()
    event_id = str(event.get("event_id") or "").strip()
    normalized_side = str(side or "").strip().lower()
    if event.get("effective_state") != "paper_watchlist" or event.get("decision_origin") != "system":
        reasons.append("lifecycle_state_not_fundable")
    if event_evidence.get("eligible") is not True:
        reasons.append("lifecycle_evidence_not_eligible")
    if event_evidence.get("price_contract_eligible") is not True:
        reasons.append("lifecycle_price_contract_not_eligible")
    if event_evidence.get("pit_eligible") is not True:
        reasons.append("lifecycle_pit_not_eligible")
    if not event_id or not symbol:
        reasons.append("lifecycle_identity_missing")
    if normalized_side != "buy":
        reasons.append("intent_side_not_supported")
    if not snapshot.allocation_eligible:
        reasons.append("portfolio_snapshot_not_eligible")
    if snapshot.evidence_eligible is not True:
        reasons.append("portfolio_snapshot_evidence_not_eligible")
    snapshot_policy = snapshot.price_policy_validation
    snapshot_pit = snapshot.pit_status
    snapshot_coverage = snapshot.coverage
    snapshot_as_of_day = _parse_iso_day(snapshot.as_of)
    snapshot_cutoff_day = _parse_iso_day(snapshot.data_cutoff)
    snapshot_numeric_values = (
        snapshot.initial_capital,
        snapshot.cash_balance,
        snapshot.available_cash,
        snapshot.market_value,
        snapshot.gross_exposure,
        snapshot.net_exposure,
        snapshot.credit_limit,
        snapshot.credit_used,
        snapshot.credit_available,
        snapshot.simulated_interest,
        snapshot.net_equity,
    )
    snapshot_envelope_valid = bool(
        snapshot_as_of_day is not None
        and snapshot_cutoff_day is not None
        and snapshot_cutoff_day <= snapshot_as_of_day
        and snapshot.source_path
        and str(snapshot.source_hash).startswith("sha256:")
        and len(str(snapshot.source_hash)) == 71
        and snapshot.price_data_contract_version == _price_contract_version()
        and snapshot.valuation_price_basis in {"raw", "cash_only"}
        and isinstance(snapshot_policy, Mapping)
        and snapshot_policy.get("status") == "passed"
        and snapshot_policy.get("eligible") is True
        and snapshot_policy.get("contract_version") == _price_contract_version()
        and snapshot_policy.get("consumer") == "paper_valuation"
        and snapshot_policy.get("observed_basis") == snapshot.valuation_price_basis
        and snapshot_policy.get("accepted_price_basis") == snapshot.valuation_price_basis
        and isinstance(snapshot_pit, Mapping)
        and snapshot_pit.get("status") == "not_applicable_existing_funded_ledger"
        and isinstance(snapshot_coverage, Mapping)
        and snapshot_coverage.get("kind") == "funded_ledger_positions"
        and snapshot_coverage.get("position_count") == snapshot.position_count
        and all(_optional_finite(value) is not None for value in snapshot_numeric_values)
        and snapshot.available_cash >= 0
        and snapshot.net_equity > 0
    )
    if snapshot.snapshot_digest != _snapshot_integrity_digest(snapshot):
        reasons.append("portfolio_snapshot_digest_mismatch")
        snapshot_envelope_valid = False
    if not snapshot_envelope_valid:
        reasons.append("portfolio_snapshot_envelope_invalid")

    event_day = _parse_iso_day(event.get("occurred_at"))
    portfolio_day = snapshot_cutoff_day or snapshot_as_of_day
    if event_day is None:
        reasons.append("lifecycle_event_date_invalid")
    elif portfolio_day is not None and event_day > portfolio_day:
        reasons.append("lifecycle_event_after_portfolio_cutoff")

    price = _optional_finite(reference_price)
    if price is None or price <= 0:
        reasons.append("reference_price_invalid")
        price = 0.0
    basis = str(reference_price_basis or "").strip().lower()
    if basis != "raw":
        reasons.append("execution_price_basis_not_raw")

    lot = lot_size_for_symbol(symbol, name=str(event_payload.get("name") or ""))
    quantity = 0
    available = max(float(snapshot.available_cash), 0.0)
    if not reasons:
        if requested_quantity is None:
            budget_value = available if max_notional is None else _optional_finite(max_notional)
            if budget_value is None or budget_value <= 0:
                reasons.append("allocation_notional_invalid")
            else:
                budget = min(available, budget_value)
                quantity = round_notional_down_to_lot_quantity(budget, price, lot_size=lot)
        else:
            if isinstance(requested_quantity, bool):
                reasons.append("requested_quantity_invalid")
            else:
                try:
                    numeric_quantity = float(requested_quantity)
                except (TypeError, ValueError):
                    numeric_quantity = math.nan
                if not math.isfinite(numeric_quantity) or not numeric_quantity.is_integer():
                    reasons.append("requested_quantity_invalid")
                    quantity = 0
                else:
                    quantity = int(numeric_quantity)
                if not is_valid_order_quantity(quantity, lot_size=lot, board_type="star" if symbol.startswith("688") else "main"):
                    if "requested_quantity_invalid" not in reasons:
                        reasons.append("requested_quantity_invalid")
                    quantity = 0

    cost = buy_cash_outflow(price=price, quantity=quantity) if price > 0 and quantity > 0 else {
        "gross": 0.0,
        "total_fee": 0.0,
        "cash_out": 0.0,
    }
    while quantity > 0 and float(cost["cash_out"]) > available:
        quantity = max(0, quantity - lot)
        cost = buy_cash_outflow(price=price, quantity=quantity) if quantity else {
            "gross": 0.0,
            "total_fee": 0.0,
            "cash_out": 0.0,
        }
    if quantity <= 0 and "requested_quantity_invalid" not in reasons:
        reasons.append("zero_lot_quantity")

    status = "blocked" if reasons else "pending_risk_review"
    payload_for_id = {
        "event_id": event_id,
        "event_digest": event.get("content_digest"),
        "profile_id": snapshot.profile_id,
        "as_of": snapshot.as_of,
        "portfolio_source_hash": snapshot.source_hash,
        "available_cash": snapshot.available_cash,
        "symbol": symbol,
        "side": normalized_side,
        "quantity": quantity,
        "price": price,
        "basis": basis,
    }
    cash_out = float(cost.get("cash_out") or 0.0)
    reason_tuple = tuple(dict.fromkeys(reasons))
    event_digest = str(event.get("content_digest") or "").strip()
    if len(event_digest) != 64 or any(char not in "0123456789abcdefABCDEF" for char in event_digest):
        event_digest = _canonical_digest(event)
    intent_source_digest = _canonical_digest({
        "lifecycle_event_digest": event_digest.lower(),
        "portfolio_source_hash": snapshot.source_hash,
        "intent_inputs": payload_for_id,
    })
    policy_ok = status == "pending_risk_review" and basis == "raw"
    pit_ok = event_evidence.get("pit_eligible") is True
    intent = PaperOrderIntent(
        intent_id=f"paper_intent_{_canonical_digest(payload_for_id)[:20]}",
        source_lifecycle_event_id=event_id,
        symbol=symbol,
        side=normalized_side,
        quantity=quantity,
        lot_size=lot,
        reference_price=round(price, 4),
        reference_price_basis=basis or "mixed_or_unknown",
        estimated_gross=round(float(cost.get("gross") or 0.0), 4),
        estimated_fee=round(float(cost.get("total_fee") or 0.0), 4),
        estimated_cash_out=round(cash_out, 4),
        available_cash_before=round(available, 2),
        # ``cash_out`` carries 4 decimals (buy_cash_outflow rounds to 4 because
        # the fee is sub-cent), and the risk gate recomputes
        # ``max(cash_before - cash_out, 0.0)`` then compares with abs_tol=1e-6.
        # Rounding this to 2 decimals made the two disagree by up to 5e-4, so
        # ``cash_flow_valid`` was unsatisfiable and every buy intent drew
        # ``paper_intent_envelope_invalid``. ``before`` stays at 2 decimals
        # because it must equal the snapshot's ``available_cash`` exactly.
        available_cash_after=round(max(available - cash_out, 0.0), 4),
        status=status,
        reason_codes=reason_tuple,
        risk_gate_status="pending" if status == "pending_risk_review" else "not_evaluated",
        execution_status="not_submitted",
        as_of=snapshot.as_of,
        data_cutoff=snapshot.data_cutoff or snapshot.as_of,
        source_path=f"lifecycle://{event_id or 'missing'}",
        source_hash=f"sha256:{intent_source_digest}",
        price_data_contract_version=_price_contract_version(),
        price_policy_validation=_price_policy_validation(
            consumer="paper_execution",
            observed_basis=basis or "mixed_or_unknown",
            eligible=policy_ok,
            reason_codes=() if policy_ok else reason_tuple,
        ),
        pit_status={"status": "passed" if pit_ok else "blocked"},
        coverage={
            "kind": "candidate_decision_event",
            "coverage": event_evidence.get("coverage"),
            "minimum_coverage": event_evidence.get("minimum_coverage"),
        },
        evidence_eligible=status == "pending_risk_review",
        degraded_codes=reason_tuple,
        portfolio_source_hash=snapshot.source_hash,
        portfolio_snapshot_digest=snapshot.snapshot_digest,
    )
    return replace(intent, intent_digest=compute_paper_order_intent_digest(intent))


def build_paper_exit_intent(
    funded_ledger: Mapping[str, Any],
    snapshot: PortfolioSnapshot,
    *,
    symbol: str,
    reference_price: float,
    reference_price_basis: str | None,
    requested_quantity: int | None = None,
) -> PaperOrderIntent:
    """Build a formal paper-only sell intent from a funded ledger position."""

    ledger = funded_ledger if isinstance(funded_ledger, Mapping) else {}
    normalized_symbol = str(symbol or "").strip()
    reasons: list[str] = []
    if not normalized_symbol:
        reasons.append("symbol_missing")
    if not snapshot.allocation_eligible:
        reasons.append("portfolio_snapshot_not_eligible")
    if snapshot.evidence_eligible is not True:
        reasons.append("portfolio_snapshot_evidence_not_eligible")
    if snapshot.snapshot_digest != _snapshot_integrity_digest(snapshot):
        reasons.append("portfolio_snapshot_digest_mismatch")
    if snapshot.source_hash != f"sha256:{_canonical_digest(dict(ledger))}":
        reasons.append("portfolio_snapshot_ledger_mismatch")

    price = _optional_finite(reference_price)
    if price is None or price <= 0:
        reasons.append("reference_price_invalid")
        price = 0.0
    basis = str(reference_price_basis or "").strip().lower()
    if basis != "raw":
        reasons.append("execution_price_basis_not_raw")

    positions = ledger.get("positions") if isinstance(ledger.get("positions"), list) else []
    position = next(
        (
            dict(row)
            for row in positions
            if isinstance(row, Mapping)
            and str(row.get("symbol") or row.get("code") or "").strip() == normalized_symbol
            and row.get("funded") is not False
        ),
        None,
    )
    held = 0
    sellable = 0
    avg_cost = 0.0
    if position is None:
        reasons.append("funded_position_missing")
    else:
        held_value = _optional_finite(position.get("quantity") if "quantity" in position else position.get("shares"))
        sellable_value = _optional_finite(
            position.get("sellable_quantity")
            if position.get("sellable_quantity") is not None
            else position.get("available_shares")
        )
        avg_value = _optional_finite(
            position.get("avg_cost")
            or position.get("entry_price")
            or position.get("buy_price")
        )
        if held_value is None or not held_value.is_integer() or held_value <= 0:
            reasons.append("funded_position_quantity_invalid")
        else:
            held = int(held_value)
        if sellable_value is None or not sellable_value.is_integer() or sellable_value < 0:
            reasons.append("sellable_quantity_invalid")
        else:
            sellable = min(int(sellable_value), held)
        if avg_value is None or avg_value <= 0:
            reasons.append("avg_cost_invalid")
        else:
            avg_cost = float(avg_value)

    lot = lot_size_for_symbol(normalized_symbol, name=str((position or {}).get("name") or ""))
    board = "star" if normalized_symbol.startswith("688") else "main"
    quantity = 0
    if requested_quantity is None:
        quantity = sellable - (sellable % max(lot, 1))
    elif isinstance(requested_quantity, bool):
        reasons.append("requested_quantity_invalid")
    else:
        try:
            requested = float(requested_quantity)
        except (TypeError, ValueError):
            requested = math.nan
        if not math.isfinite(requested) or not requested.is_integer() or requested <= 0:
            reasons.append("requested_quantity_invalid")
        else:
            quantity = int(requested)
    if quantity > sellable:
        reasons.append("sellable_quantity_insufficient")
    if quantity <= 0 and "requested_quantity_invalid" not in reasons:
        reasons.append("zero_lot_quantity")
    elif quantity > 0 and not is_valid_order_quantity(quantity, lot_size=lot, board_type=board, held_shares=held):
        reasons.append("requested_quantity_invalid")
        quantity = 0

    flow = (
        sell_cash_inflow(price=float(price), quantity=quantity, avg_cost=avg_cost)
        if price > 0 and quantity > 0
        else {"gross": 0.0, "total_fee": 0.0, "cash_in": 0.0, "realized_pnl": 0.0}
    )
    cash_before = float(snapshot.available_cash)
    cash_in = float(flow.get("cash_in") or 0.0)
    status = "blocked" if reasons else "pending_risk_review"
    reason_tuple = tuple(dict.fromkeys(reasons))
    position_digest = _canonical_digest(position or {})
    payload_for_id = {
        "ledger_source_hash": snapshot.source_hash,
        "snapshot_digest": snapshot.snapshot_digest,
        "symbol": normalized_symbol,
        "side": "sell",
        "quantity": quantity,
        "price": price,
        "basis": basis,
        "position_digest": position_digest,
        "as_of": snapshot.as_of,
    }
    source_digest = _canonical_digest(
        {
            "ledger_source_hash": snapshot.source_hash,
            "snapshot_digest": snapshot.snapshot_digest,
            "position_digest": position_digest,
            "intent_inputs": payload_for_id,
        }
    )
    policy_ok = status == "pending_risk_review" and basis == "raw"
    intent = PaperOrderIntent(
        intent_id=f"paper_exit_intent_{_canonical_digest(payload_for_id)[:20]}",
        source_lifecycle_event_id=f"funded_position:{normalized_symbol}",
        symbol=normalized_symbol,
        side="sell",
        quantity=quantity,
        lot_size=lot,
        reference_price=round(float(price), 4),
        reference_price_basis=basis or "mixed_or_unknown",
        estimated_gross=round(float(flow.get("gross") or 0.0), 4),
        estimated_fee=round(float(flow.get("total_fee") or 0.0), 4),
        estimated_cash_out=round(-cash_in, 4),
        available_cash_before=round(cash_before, 2),
        # Same 4-decimal reconciliation as the buy path: ``cash_in`` carries 4
        # decimals and the risk gate compares ``cash_before + cash_in`` with
        # abs_tol=1e-6, so a 2-decimal value here is unsatisfiable.
        available_cash_after=round(cash_before + cash_in, 4),
        status=status,
        reason_codes=reason_tuple,
        risk_gate_status="pending" if status == "pending_risk_review" else "not_evaluated",
        execution_status="not_submitted",
        as_of=snapshot.as_of,
        data_cutoff=snapshot.data_cutoff or snapshot.as_of,
        source_path=f"funded-ledger://{snapshot.ledger_id}/{normalized_symbol}",
        source_hash=f"sha256:{source_digest}",
        price_data_contract_version=_price_contract_version(),
        price_policy_validation=_price_policy_validation(
            consumer="paper_execution",
            observed_basis=basis or "mixed_or_unknown",
            eligible=policy_ok,
            reason_codes=() if policy_ok else reason_tuple,
        ),
        pit_status={"status": "not_applicable_funded_position_exit"},
        coverage={
            "kind": "funded_position_exit",
            "held_quantity": held,
            "sellable_quantity": sellable,
            "avg_cost": round(avg_cost, 4),
            "position_digest": position_digest,
        },
        evidence_eligible=status == "pending_risk_review",
        degraded_codes=reason_tuple,
        portfolio_source_hash=snapshot.source_hash,
        portfolio_snapshot_digest=snapshot.snapshot_digest,
    )
    return replace(intent, intent_digest=compute_paper_order_intent_digest(intent))


__all__ = [
    "PaperCapitalProfile",
    "PortfolioSnapshot",
    "PaperOrderIntent",
    "PortfolioContractError",
    "resolve_paper_capital_profile",
    "build_portfolio_snapshot",
    "build_paper_order_intent",
    "build_paper_exit_intent",
    "compute_paper_order_intent_digest",
]
