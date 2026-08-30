from __future__ import annotations

import copy
import hashlib
import importlib
import inspect
import json
from dataclasses import replace
from pathlib import Path

import pandas as pd
import pytest

from quantdesk.paper.lifecycle import CandidateDecisionEvent
from quantdesk.paper import paper_trading
from quantdesk.paper.shared_allocation_planner import (
    SharedAllocationConfig,
    plan_shared_paper_allocation,
    run_shared_paper_allocation_plan,
    write_shared_paper_plan_outputs,
)
from quantdesk.risk_position import compute_position_reference
from quantdesk.price_basis import price_data_contract
from quantdesk.version import API_SCHEMA_VERSION


try:
    contracts = importlib.import_module("quantdesk.paper.portfolio_contracts")
except ModuleNotFoundError:
    contracts = None

PaperCapitalProfile = getattr(contracts, "PaperCapitalProfile", None)
PortfolioSnapshot = getattr(contracts, "PortfolioSnapshot", None)
PaperOrderIntent = getattr(contracts, "PaperOrderIntent", None)
PortfolioContractError = getattr(contracts, "PortfolioContractError", RuntimeError)
resolve_paper_capital_profile = getattr(contracts, "resolve_paper_capital_profile", None)
build_portfolio_snapshot = getattr(contracts, "build_portfolio_snapshot", None)
build_paper_order_intent = getattr(contracts, "build_paper_order_intent", None)


def test_portfolio_contract_module_exposes_required_api() -> None:
    assert contracts is not None, "quantdesk.paper.portfolio_contracts must exist"
    assert PaperCapitalProfile is not None
    assert PortfolioSnapshot is not None
    assert PaperOrderIntent is not None
    assert callable(resolve_paper_capital_profile)
    assert callable(build_portfolio_snapshot)
    assert callable(build_paper_order_intent)


@pytest.fixture(autouse=True)
def _skip_contract_tests_until_module_exists(request: pytest.FixtureRequest) -> None:
    if contracts is None and request.node.name != "test_portfolio_contract_module_exposes_required_api":
        pytest.skip("dependent Portfolio Brain contracts wait for the module RED")


def _formal_watch_event(symbol: str = "600000") -> dict:
    return CandidateDecisionEvent(
        event_id=f"watch-{symbol}",
        symbol=symbol,
        occurred_at="2026-07-10T15:01:00+08:00",
        requested_state="paper_watchlist",
        decision_origin="system",
        evidence={
            "eligible": True,
            "evidence_kind": "formal",
            "coverage": 1.0,
            "minimum_coverage": 1.0,
            "pit_eligible": True,
            "price_contract_eligible": True,
        },
        payload={"name": "sample"},
    ).to_dict()


def _cash_snapshot(cash: float = 10_000.0, *, basis: str | None = None):
    profile = resolve_paper_capital_profile()
    return build_portfolio_snapshot(
        {
            "ledger_id": "test-funded-ledger",
            "initial_cash": 10_000.0,
            "cash": cash,
            "positions": [],
        },
        profile,
        as_of="2026-07-10",
        valuation_price_basis=basis,
    )


def test_new_profile_defaults_to_user_10k_and_credit_off() -> None:
    profile = resolve_paper_capital_profile()

    assert profile.profile_id == "user_10k"
    assert profile.scenario_id == "user_10k"
    assert profile.initial_capital == 10_000.0
    assert profile.paper_credit_enabled is False
    assert profile.credit_limit == 0.0
    assert profile.legacy_state_preserved is False


def test_new_100k_requires_explicit_acceptance_scenario() -> None:
    with pytest.raises(PortfolioContractError, match="acceptance_100k"):
        resolve_paper_capital_profile(requested_initial_capital=100_000.0)

    profile = resolve_paper_capital_profile(
        scenario_id="acceptance_100k",
        requested_initial_capital=100_000.0,
    )
    assert profile.initial_capital == 100_000.0
    assert profile.scenario_id == "acceptance_100k"


def test_existing_legacy_100k_state_is_preserved_and_visibly_labeled() -> None:
    state = {
        "initial_cash": 100_000.0,
        "cash": 88_000.0,
        "positions": [],
        "paper_trading_only": True,
    }
    before = copy.deepcopy(state)

    profile = resolve_paper_capital_profile(
        existing_state=state,
        existing_state_verified=True,
    )

    assert state == before
    assert profile.initial_capital == 100_000.0
    assert profile.legacy_state_preserved is True
    assert profile.scenario_id == "legacy_existing"
    assert "Legacy" in profile.visible_label


@pytest.mark.parametrize("capital", [100_000.0, 1_000_000.0])
def test_arbitrary_unmarked_capital_cannot_acquire_legacy_identity(capital: float) -> None:
    with pytest.raises(PortfolioContractError, match="legacy_existing_capital_not_supported"):
        resolve_paper_capital_profile(
            existing_state={"initial_cash": capital, "cash": capital, "positions": []}
        )


def test_anonymous_cash_and_positions_shape_is_not_a_funded_ledger() -> None:
    profile = resolve_paper_capital_profile()

    snapshot = build_portfolio_snapshot(
        {"cash": 999_999.0, "positions": []},
        profile,
        as_of="2026-07-10",
    )

    assert snapshot.allocation_eligible is False
    assert snapshot.available_cash == 0.0
    assert "funded_ledger_identity_missing" in snapshot.reason_codes


def test_existing_zero_capital_is_rejected_instead_of_reinitialized_to_10k() -> None:
    with pytest.raises(PortfolioContractError, match="existing_initial_capital_invalid"):
        resolve_paper_capital_profile(
            existing_state={"initial_cash": 0.0, "cash": 0.0, "positions": []}
        )


@pytest.mark.parametrize(
    "state",
    [
        {
            "profile_id": "user_10k",
            "scenario_id": "user_10k",
            "initial_cash": 100_000.0,
            "cash": 88_000.0,
            "positions": [],
        },
        {
            "account": {
                "profile_id": "acceptance_100k",
                "scenario_id": "user_10k",
                "initial_cash": 100_000.0,
                "cash": 88_000.0,
            },
            "positions": [],
        },
    ],
)
def test_existing_named_profile_must_match_its_scenario_and_capital(state: dict) -> None:
    with pytest.raises(PortfolioContractError, match="existing_capital_profile_mismatch"):
        resolve_paper_capital_profile(existing_state=state)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"paper_credit_enabled": "false"},
        {"credit_limit": -1},
        {"credit_limit": float("nan")},
        {"simulated_interest_rate": -0.1},
    ],
)
def test_invalid_credit_profile_fails_closed(kwargs: dict) -> None:
    with pytest.raises(PortfolioContractError):
        resolve_paper_capital_profile(**kwargs)


def test_snapshot_uses_funded_ledger_cash_not_initial_capital() -> None:
    profile = resolve_paper_capital_profile()
    ledger = {"ledger_id": "test-funded-ledger", "initial_cash": 10_000.0, "cash": 600.0, "positions": []}

    snapshot = build_portfolio_snapshot(ledger, profile, as_of="2026-07-10")

    assert snapshot.cash_balance == 600.0
    assert snapshot.available_cash == 600.0
    assert snapshot.initial_capital == 10_000.0
    assert snapshot.allocation_eligible is True


def test_snapshot_requires_a_valid_explicit_as_of_date() -> None:
    profile = resolve_paper_capital_profile()
    snapshot = build_portfolio_snapshot(
        {"ledger_id": "test-funded-ledger", "initial_cash": 10_000.0, "cash": 10_000.0, "positions": []},
        profile,
        as_of="not-a-date",
    )

    assert snapshot.allocation_eligible is False
    assert "as_of_invalid" in snapshot.reason_codes


def test_snapshot_missing_cash_fails_closed_instead_of_using_initial_capital() -> None:
    profile = resolve_paper_capital_profile()

    snapshot = build_portfolio_snapshot(
        {"ledger_id": "test-funded-ledger", "initial_cash": 10_000.0, "positions": []},
        profile,
        as_of="2026-07-10",
    )

    assert snapshot.available_cash == 0.0
    assert snapshot.allocation_eligible is False
    assert "funded_ledger_cash_missing" in snapshot.reason_codes


def test_zero_equity_snapshot_is_not_allocation_eligible() -> None:
    profile = resolve_paper_capital_profile()
    snapshot = build_portfolio_snapshot(
        {"ledger_id": "test-funded-ledger", "initial_cash": 10_000.0, "cash": 0.0, "positions": []},
        profile,
        as_of="2026-07-10",
    )

    assert snapshot.allocation_eligible is False
    assert "net_equity_not_positive" in snapshot.reason_codes


def test_snapshot_invalid_ledger_initial_capital_fails_closed() -> None:
    profile = resolve_paper_capital_profile()
    snapshot = build_portfolio_snapshot(
        {
            "ledger_id": "test-funded-ledger",
            "initial_cash": "not-a-number",
            "cash": 10_000.0,
            "positions": [],
        },
        profile,
        as_of="2026-07-10",
    )

    assert snapshot.allocation_eligible is False
    assert "ledger_initial_capital_invalid" in snapshot.reason_codes


def test_watchlist_shape_cannot_masquerade_as_funded_ledger() -> None:
    profile = resolve_paper_capital_profile()
    forged = {
        "watchlist_id": "paper_watchlist",
        "items": [{"symbol": "600000", "cash": 1_000_000, "quantity": 10_000, "funded": True}],
    }

    snapshot = build_portfolio_snapshot(forged, profile, as_of="2026-07-10")

    assert snapshot.available_cash == 0.0
    assert snapshot.market_value == 0.0
    assert snapshot.allocation_eligible is False
    assert "not_a_funded_ledger" in snapshot.reason_codes


def test_candidate_shape_with_forged_funding_fields_cannot_masquerade_as_ledger() -> None:
    profile = resolve_paper_capital_profile()
    forged_candidate = {
        "symbol": "600000",
        "strategy_type": "long",
        "score": 99.0,
        "funded": True,
        "initial_cash": 10_000.0,
        "cash": 9_999.0,
        "positions": [],
    }

    snapshot = build_portfolio_snapshot(forged_candidate, profile, as_of="2026-07-10")

    assert snapshot.available_cash == 0.0
    assert snapshot.allocation_eligible is False
    assert "not_a_funded_ledger" in snapshot.reason_codes


@pytest.mark.parametrize("basis", [None, "", "mixed_or_unknown", "qfq", "forward_adjusted"])
def test_funded_positions_require_explicit_raw_valuation_basis(basis: str | None) -> None:
    profile = resolve_paper_capital_profile()
    ledger = {
        "ledger_id": "test-funded-ledger",
        "initial_cash": 10_000.0,
        "cash": 8_000.0,
        "positions": [{"symbol": "600000", "quantity": 100, "market_value": 2_000.0}],
    }

    snapshot = build_portfolio_snapshot(
        ledger,
        profile,
        as_of="2026-07-10",
        valuation_price_basis=basis,
    )

    assert snapshot.allocation_eligible is False
    assert "valuation_price_basis_not_raw" in snapshot.reason_codes


def test_cash_only_snapshot_does_not_fabricate_a_price_basis() -> None:
    snapshot = _cash_snapshot(10_000.0)

    assert snapshot.allocation_eligible is True
    assert snapshot.valuation_price_basis == "cash_only"


def test_position_level_basis_cannot_contradict_raw_snapshot_claim() -> None:
    profile = resolve_paper_capital_profile()
    snapshot = build_portfolio_snapshot(
        {
            "ledger_id": "test-funded-ledger",
            "initial_cash": 10_000.0,
            "cash": 8_000.0,
            "positions": [
                {
                    "symbol": "600000",
                    "quantity": 100,
                    "market_value": 2_000.0,
                    "price_basis": "qfq",
                }
            ],
        },
        profile,
        as_of="2026-07-10",
        valuation_price_basis="raw",
    )

    assert snapshot.allocation_eligible is False
    assert "position_price_basis_not_raw" in snapshot.reason_codes


def test_raw_funded_position_snapshot_carries_the_common_evidence_envelope() -> None:
    profile = resolve_paper_capital_profile()
    snapshot = build_portfolio_snapshot(
        {
            "ledger_id": "paper-ledger-1",
            "initial_cash": 10_000.0,
            "cash": 8_000.0,
            "positions": [
                {
                    "symbol": "600000",
                    "quantity": 100,
                    "market_value": 2_000.0,
                    "price_basis": "raw",
                }
            ],
        },
        profile,
        as_of="2026-07-10",
        valuation_price_basis="raw",
    )

    payload = snapshot.to_dict()
    assert snapshot.allocation_eligible is True
    assert payload["schema_version"]
    assert payload["data_cutoff"] == "2026-07-10"
    assert payload["source_path"] == "memory://funded-ledger/paper-ledger-1"
    assert payload["source_hash"].startswith("sha256:")
    assert payload["price_data_contract_version"]
    assert payload["price_policy_validation"]["status"] == "passed"
    assert payload["pit_status"]["status"] == "not_applicable_existing_funded_ledger"
    assert payload["coverage"]["position_count"] == 1
    assert payload["evidence_eligible"] is True
    assert payload["degraded_codes"] == ()


def test_credit_disabled_does_not_increase_purchasing_power() -> None:
    profile = resolve_paper_capital_profile(
        paper_credit_enabled=False,
        credit_limit=5_000.0,
    )
    snapshot = build_portfolio_snapshot(
        {"ledger_id": "test-funded-ledger", "initial_cash": 10_000.0, "cash": 1_000.0, "credit_used": 0.0, "positions": []},
        profile,
        as_of="2026-07-10",
    )

    assert snapshot.available_cash == 1_000.0
    assert snapshot.credit_available == 0.0
    assert snapshot.credit_used == 0.0


def test_credit_fields_are_separate_from_capital_and_exposure() -> None:
    profile = resolve_paper_capital_profile(
        paper_credit_enabled=True,
        credit_limit=5_000.0,
        simulated_interest_rate=0.08,
    )
    snapshot = build_portfolio_snapshot(
        {
            "ledger_id": "test-funded-ledger",
            "initial_cash": 10_000.0,
            "cash": 500.0,
            "credit_used": 2_000.0,
            "simulated_interest": 12.0,
            "positions": [],
        },
        profile,
        as_of="2026-07-10",
    )

    assert snapshot.initial_capital == 10_000.0
    assert snapshot.credit_limit == 5_000.0
    assert snapshot.credit_used == 2_000.0
    assert snapshot.credit_available == 3_000.0
    assert snapshot.simulated_interest == 12.0
    assert snapshot.available_cash == 3_500.0


def test_credit_liability_does_not_masquerade_as_negative_market_exposure() -> None:
    profile = resolve_paper_capital_profile(
        paper_credit_enabled=True,
        credit_limit=5_000.0,
    )
    snapshot = build_portfolio_snapshot(
        {
            "ledger_id": "test-funded-ledger",
            "initial_cash": 10_000.0,
            "cash": 500.0,
            "credit_used": 2_000.0,
            "positions": [
                {
                    "symbol": "600000",
                    "quantity": 500,
                    "market_value": 5_000.0,
                    "price_basis": "raw",
                }
            ],
        },
        profile,
        as_of="2026-07-10",
        valuation_price_basis="raw",
    )

    assert snapshot.gross_exposure == 5_000.0
    assert snapshot.net_exposure == 5_000.0
    assert snapshot.credit_used == 2_000.0
    assert snapshot.net_equity == 3_500.0


def test_credit_used_above_limit_blocks_allocation() -> None:
    profile = resolve_paper_capital_profile(
        paper_credit_enabled=True,
        credit_limit=1_000.0,
    )
    snapshot = build_portfolio_snapshot(
        {
            "ledger_id": "test-funded-ledger",
            "initial_cash": 10_000.0,
            "cash": 0.0,
            "credit_used": 1_500.0,
            "positions": [],
        },
        profile,
        as_of="2026-07-10",
    )

    assert snapshot.allocation_eligible is False
    assert "credit_limit_exceeded" in snapshot.reason_codes


def test_negative_cash_is_not_treated_as_an_implicit_credit_draw() -> None:
    profile = resolve_paper_capital_profile(
        paper_credit_enabled=True,
        credit_limit=5_000.0,
    )
    snapshot = build_portfolio_snapshot(
        {
            "ledger_id": "test-funded-ledger",
            "initial_cash": 10_000.0,
            "cash": -100.0,
            "credit_used": 100.0,
            "positions": [],
        },
        profile,
        as_of="2026-07-10",
    )

    assert snapshot.allocation_eligible is False
    assert "negative_cash_balance" in snapshot.reason_codes


@pytest.mark.parametrize(
    ("field", "value", "reason"),
    [
        ("credit_used", True, "credit_used_invalid"),
        ("credit_used", float("nan"), "credit_used_invalid"),
        ("simulated_interest", True, "simulated_interest_invalid"),
        ("simulated_interest", "not-a-number", "simulated_interest_invalid"),
    ],
)
def test_malformed_ledger_credit_evidence_fails_closed(
    field: str,
    value: object,
    reason: str,
) -> None:
    profile = resolve_paper_capital_profile(
        paper_credit_enabled=True,
        credit_limit=5_000.0,
    )
    ledger = {
        "ledger_id": "test-funded-ledger",
        "initial_cash": 10_000.0,
        "cash": 1_000.0,
        "positions": [],
        field: value,
    }

    snapshot = build_portfolio_snapshot(ledger, profile, as_of="2026-07-10")

    assert snapshot.allocation_eligible is False
    assert reason in snapshot.reason_codes


def test_intent_is_fee_aware_and_waits_for_risk_instead_of_creating_position() -> None:
    snapshot = _cash_snapshot(1_100.0)
    event = _formal_watch_event("600000")
    event_before = copy.deepcopy(event)

    intent = build_paper_order_intent(
        event,
        snapshot,
        reference_price=10.0,
        reference_price_basis="raw",
        max_notional=1_100.0,
    )

    assert event == event_before
    assert intent.quantity == 100
    assert intent.estimated_cash_out <= snapshot.available_cash
    assert intent.status == "pending_risk_review"
    assert intent.risk_gate_status == "pending"
    assert intent.execution_status == "not_submitted"
    assert not hasattr(intent, "position")


def test_star_intent_uses_200_share_minimum_and_zero_never_falls_back() -> None:
    snapshot = _cash_snapshot(5_000.0)

    intent = build_paper_order_intent(
        _formal_watch_event("688001"),
        snapshot,
        reference_price=40.0,
        reference_price_basis="raw",
        max_notional=5_000.0,
    )

    assert intent.lot_size == 200
    assert intent.quantity == 0
    assert intent.status == "blocked"
    assert "zero_lot_quantity" in intent.reason_codes


@pytest.mark.parametrize("basis", [None, "", "mixed_or_unknown", "qfq"])
def test_intent_blocks_non_raw_or_missing_execution_basis(basis: str | None) -> None:
    intent = build_paper_order_intent(
        _formal_watch_event(),
        _cash_snapshot(),
        reference_price=10.0,
        reference_price_basis=basis,
    )

    assert intent.quantity == 0
    assert intent.status == "blocked"
    assert "execution_price_basis_not_raw" in intent.reason_codes


def test_manual_observation_cannot_create_order_intent() -> None:
    event = CandidateDecisionEvent(
        event_id="manual-1",
        symbol="600000",
        occurred_at="2026-07-10T15:01:00+08:00",
        requested_state="paper_watchlist",
        decision_origin="manual",
        evidence={},
        payload={"name": "sample"},
    ).to_dict()

    intent = build_paper_order_intent(
        event,
        _cash_snapshot(),
        reference_price=10.0,
        reference_price_basis="raw",
    )

    assert intent.status == "blocked"
    assert "lifecycle_state_not_fundable" in intent.reason_codes


def test_tampered_lifecycle_event_cannot_create_order_intent() -> None:
    event = _formal_watch_event()
    event["symbol"] = "000001"

    intent = build_paper_order_intent(
        event,
        _cash_snapshot(),
        reference_price=10.0,
        reference_price_basis="raw",
    )

    assert intent.status == "blocked"
    assert "lifecycle_event_invalid" in intent.reason_codes


def test_malformed_lifecycle_payload_blocks_without_raising() -> None:
    event = _formal_watch_event()
    event["payload"] = "not-a-mapping"

    intent = build_paper_order_intent(
        event,
        _cash_snapshot(),
        reference_price=10.0,
        reference_price_basis="raw",
    )

    assert intent.status == "blocked"
    assert intent.quantity == 0
    assert "lifecycle_event_invalid" in intent.reason_codes


def test_pending_intent_carries_common_evidence_envelope() -> None:
    intent = build_paper_order_intent(
        _formal_watch_event(),
        _cash_snapshot(),
        reference_price=10.0,
        reference_price_basis="raw",
        requested_quantity=100,
    )

    payload = intent.to_dict()
    assert intent.status == "pending_risk_review"
    assert payload["schema_version"]
    assert payload["as_of"] == "2026-07-10"
    assert payload["data_cutoff"] == "2026-07-10"
    assert payload["source_path"] == "lifecycle://watch-600000"
    assert payload["source_hash"].startswith("sha256:")
    assert payload["price_data_contract_version"]
    assert payload["price_policy_validation"]["status"] == "passed"
    assert payload["pit_status"]["status"] == "passed"
    assert payload["coverage"]["kind"] == "candidate_decision_event"
    assert payload["evidence_eligible"] is True
    assert payload["degraded_codes"] == ()


def test_intent_identity_binds_the_funded_ledger_snapshot() -> None:
    event = _formal_watch_event()
    first = build_paper_order_intent(
        event,
        _cash_snapshot(10_000.0),
        reference_price=10.0,
        reference_price_basis="raw",
        requested_quantity=100,
    )
    second = build_paper_order_intent(
        event,
        _cash_snapshot(5_000.0),
        reference_price=10.0,
        reference_price_basis="raw",
        requested_quantity=100,
    )

    assert first.intent_id != second.intent_id
    assert first.source_hash != second.source_hash


def test_intent_revalidates_the_complete_portfolio_snapshot_envelope() -> None:
    valid = _cash_snapshot(10_000.0)
    forged = replace(
        valid,
        allocation_eligible=True,
        evidence_eligible=True,
        valuation_price_basis="mixed_or_unknown",
        source_hash="",
        price_data_contract_version="wrong",
        price_policy_validation={"status": "failed", "eligible": False},
        pit_status={"status": "blocked"},
        coverage={},
    )

    intent = build_paper_order_intent(
        _formal_watch_event(),
        forged,
        reference_price=10.0,
        reference_price_basis="raw",
        requested_quantity=100,
    )

    assert intent.status == "blocked"
    assert intent.quantity == 0
    assert "portfolio_snapshot_envelope_invalid" in intent.reason_codes


@pytest.mark.parametrize(
    "changes",
    [
        {"source_hash": ""},
        {"price_data_contract_version": "wrong"},
        {"price_policy_validation": {"status": "failed", "eligible": False}},
        {"valuation_price_basis": "mixed_or_unknown"},
        {"pit_status": {"status": "blocked"}},
        {"coverage": {}},
        {"data_cutoff": "2026-07-11"},
        {"available_cash": float("nan")},
        {"net_equity": float("inf")},
    ],
)
def test_each_invalid_snapshot_envelope_component_blocks_intent(changes: dict) -> None:
    forged = replace(_cash_snapshot(10_000.0), **changes)

    intent = build_paper_order_intent(
        _formal_watch_event(),
        forged,
        reference_price=10.0,
        reference_price_basis="raw",
        requested_quantity=100,
    )

    assert intent.status == "blocked"
    assert "portfolio_snapshot_envelope_invalid" in intent.reason_codes


@pytest.mark.parametrize(
    "changes",
    [
        {"available_cash": 999_999.0},
        {"initial_capital": 999_999.0},
        {"credit_used": 5_000.0},
        {"gross_exposure": 5_000.0},
    ],
)
def test_snapshot_decision_field_tampering_blocks_intent(changes: dict) -> None:
    forged = replace(_cash_snapshot(10_000.0), **changes)

    intent = build_paper_order_intent(
        _formal_watch_event(),
        forged,
        reference_price=10.0,
        reference_price_basis="raw",
        requested_quantity=100,
    )

    assert intent.status == "blocked"
    assert intent.quantity == 0
    assert "portfolio_snapshot_digest_mismatch" in intent.reason_codes


def test_lifecycle_event_after_portfolio_cutoff_blocks_intent() -> None:
    future_event = CandidateDecisionEvent(
        event_id="future-watch-600000",
        symbol="600000",
        occurred_at="2099-01-01T09:30:00+08:00",
        requested_state="paper_watchlist",
        decision_origin="system",
        evidence={
            "eligible": True,
            "evidence_kind": "formal",
            "coverage": 1.0,
            "minimum_coverage": 1.0,
            "pit_eligible": True,
            "price_contract_eligible": True,
        },
        payload={"name": "sample"},
    ).to_dict()

    intent = build_paper_order_intent(
        future_event,
        _cash_snapshot(),
        reference_price=10.0,
        reference_price_basis="raw",
        requested_quantity=100,
    )

    assert intent.status == "blocked"
    assert intent.quantity == 0
    assert "lifecycle_event_after_portfolio_cutoff" in intent.reason_codes


def test_requested_quantity_must_be_an_integer_and_is_never_truncated() -> None:
    intent = build_paper_order_intent(
        _formal_watch_event(),
        _cash_snapshot(),
        reference_price=10.0,
        reference_price_basis="raw",
        requested_quantity=100.5,
    )

    assert intent.status == "blocked"
    assert intent.quantity == 0
    assert "requested_quantity_invalid" in intent.reason_codes


def _planner_candidate(symbol: str, *, price: float, price_basis: str = "raw") -> dict:
    return {
        "symbol": symbol,
        "name": "sample",
        "strategy_type": "long",
        "close": price,
        "score": 90.0,
        "price_basis": price_basis,
        "adjustment_basis": "none_raw",
        "adjustment_warnings": [],
        "data_quality": "raw",
        "latest_date": "2026-07-10",
    }


def _formal_planner_payload(**candidate_groups: list[dict]) -> dict:
    payload = {
        "schema_version": API_SCHEMA_VERSION,
        "generated_at": "2026-07-10T15:00:00+08:00",
        "as_of": "2026-07-10T15:00:00+08:00",
        "data_cutoff": "2026-07-10",
        "source_path": "memory://unit/formal-strategy-evidence",
        "price_data_contract_version": str(price_data_contract().get("version") or ""),
        "price_policy_validation": {
            "status": "passed",
            "eligible": True,
            "contract_version": str(price_data_contract().get("version") or ""),
            "consumer": "paper_valuation",
            "consumer_policy": (price_data_contract().get("current_enforced") or {}).get("paper_valuation") or {},
            "observed_basis": "raw",
            "accepted_price_basis": "raw",
            "reason_codes": [],
        },
        "pit_status": {"status": "passed", "eligible": True},
        "coverage": {
            "kind": "specified_symbols",
            "candidate_count": sum(len(rows) for rows in candidate_groups.values()),
        },
        "evidence_eligible": True,
        "long_candidates": candidate_groups.get("long_candidates", []),
        "board_candidates": candidate_groups.get("board_candidates", []),
        "standard_candidates": candidate_groups.get("standard_candidates", []),
    }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    payload["source_hash"] = "sha256:" + hashlib.sha256(encoded.encode("utf-8")).hexdigest()
    return payload


def _verified_minute_pool(symbols: set[str]) -> dict:
    payload = {
        "schema_version": API_SCHEMA_VERSION,
        "task_type": "verified_minute_pool_snapshot",
        "passed": True,
        "status": "passed",
        "quality_status": "passed",
        "snapshot_available": True,
        "snapshot_path": "results/data_health/unit_minute_quality.json",
        "source_snapshot": "latest",
        "job_path": "data/staging/unit_minute_job",
        "target_date": "2026-07-10",
        "total_symbols": len(symbols),
        "usable_count": len(symbols),
        "partial_count": 0,
        "insufficient_count": 0,
        "invalid_count": 0,
        "missing_count": 0,
        "symbol_count": len(symbols),
        "symbols": symbols,
        "read_only": True,
        "network_used": False,
        "canonical_write": False,
    }
    canonical = {**payload, "symbols": sorted(symbols)}
    encoded = json.dumps(canonical, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    payload["source_hash"] = "sha256:" + hashlib.sha256(encoded.encode("utf-8")).hexdigest()
    return payload


def test_shared_planner_uses_funded_cash_and_board_lot() -> None:
    project_data = Path(__file__).resolve().parents[1] / "data"
    payload = {"long_candidates": [_planner_candidate("688001", price=40.0)]}

    result = plan_shared_paper_allocation(
        auto_strategy_payload=payload,
        paper_state={"ledger_id": "test-funded-ledger", "initial_cash": 10_000.0, "cash": 5_000.0, "positions": []},
        minute_pool={"passed": True, "symbols": {"688001"}},
        config=SharedAllocationConfig(initial_cash=10_000.0, data_root=project_data),
    )

    assert result["initial_cash"] == 5_000.0
    assert result["selected_for_paper_plan"] == []
    blocked = next(row for row in result["blocked_or_waiting"] if row["symbol"] == "688001")
    assert blocked["lot_size"] == 200


def test_shared_planner_blocks_mixed_price_basis() -> None:
    project_data = Path(__file__).resolve().parents[1] / "data"
    result = plan_shared_paper_allocation(
        auto_strategy_payload={"long_candidates": [_planner_candidate("600000", price=10.0, price_basis="mixed_or_unknown")]},
        paper_state={"ledger_id": "test-funded-ledger", "initial_cash": 10_000.0, "cash": 10_000.0, "positions": []},
        minute_pool={"passed": True, "symbols": {"600000"}},
        config=SharedAllocationConfig(initial_cash=10_000.0, data_root=project_data),
    )

    assert result["selected_for_paper_plan"] == []
    assert result["blocked_or_waiting"][0]["status"] == "blocked_data"
    assert result["evidence_eligible"] is False
    assert result["latest_eligible"] is False


def test_shared_planner_does_not_allocate_without_a_funded_ledger() -> None:
    project_data = Path(__file__).resolve().parents[1] / "data"
    result = plan_shared_paper_allocation(
        auto_strategy_payload={"long_candidates": [_planner_candidate("600000", price=10.0)]},
        paper_state={},
        minute_pool={"passed": True, "symbols": {"600000"}},
        config=SharedAllocationConfig(initial_cash=10_000.0, data_root=project_data),
    )

    assert result["selected_for_paper_plan"] == []
    assert result["initial_cash"] == 0.0
    assert result["available_cash_source"] == "funded_ledger_missing"
    assert result["plan_status"] == "blocked_funded_ledger_missing"
    assert result["evidence_eligible"] is False
    assert result["latest_eligible"] is False
    assert result["latest_write_blocked_reason"] == "plan_evidence_not_eligible"


def test_shared_planner_rejects_candidate_shaped_cash_as_funded_ledger() -> None:
    project_data = Path(__file__).resolve().parents[1] / "data"
    result = plan_shared_paper_allocation(
        auto_strategy_payload={"long_candidates": [_planner_candidate("600000", price=10.0)]},
        paper_state={
            "symbol": "000001",
            "strategy_type": "long",
            "funded": True,
            "cash": 10_000.0,
            "positions": [],
        },
        minute_pool={"passed": True, "symbols": {"600000"}},
        config=SharedAllocationConfig(initial_cash=10_000.0, data_root=project_data),
    )

    assert result["selected_for_paper_plan"] == []
    assert result["available_cash_source"] == "funded_ledger_invalid_shape"
    assert result["plan_status"] == "blocked_funded_ledger_missing"


@pytest.mark.parametrize("cash", [True, -1.0])
def test_shared_planner_rejects_boolean_or_negative_funded_cash(cash: object) -> None:
    project_data = Path(__file__).resolve().parents[1] / "data"
    result = plan_shared_paper_allocation(
        auto_strategy_payload={"long_candidates": [_planner_candidate("600000", price=10.0)]},
        paper_state={"ledger_id": "test-funded-ledger", "initial_cash": 10_000.0, "cash": cash, "positions": []},
        minute_pool={"passed": True, "symbols": {"600000"}},
        config=SharedAllocationConfig(initial_cash=10_000.0, data_root=project_data),
    )

    assert result["selected_for_paper_plan"] == []
    assert result["available_cash_source"] == "funded_ledger.cash_invalid"
    assert result["plan_status"] == "blocked_funded_ledger_missing"
    assert result["evidence_eligible"] is False


def test_shared_planner_blocks_cross_symbol_basis_conflict_and_qfq_funding() -> None:
    project_data = Path(__file__).resolve().parents[1] / "data"
    result = plan_shared_paper_allocation(
        auto_strategy_payload={
            "long_candidates": [
                _planner_candidate("600000", price=10.0, price_basis="raw"),
                _planner_candidate("000001", price=10.0, price_basis="forward_adjusted"),
            ]
        },
        paper_state={"ledger_id": "test-funded-ledger", "initial_cash": 10_000.0, "cash": 10_000.0, "positions": []},
        minute_pool={"passed": True, "symbols": {"600000", "000001"}},
        config=SharedAllocationConfig(initial_cash=10_000.0, data_root=project_data),
    )

    assert result["selected_for_paper_plan"] == []
    assert result["plan_status"] == "blocked_cross_symbol_price_basis"
    assert result["price_policy_validation"]["status"] == "failed"
    assert "cross_symbol_price_basis_mismatch" in result["degraded_codes"]
    assert result["latest_eligible"] is False


def test_shared_planner_carries_the_common_evidence_envelope() -> None:
    project_data = Path(__file__).resolve().parents[1] / "data"
    result = plan_shared_paper_allocation(
        auto_strategy_payload=_formal_planner_payload(
            long_candidates=[_planner_candidate("600000", price=10.0)]
        ),
        paper_state={
            "ledger_id": "paper-ledger-1",
            "initial_cash": 10_000.0,
            "cash": 10_000.0,
            "positions": [],
        },
        minute_pool=_verified_minute_pool({"600000"}),
        config=SharedAllocationConfig(initial_cash=10_000.0, data_root=project_data),
    )

    assert result["schema_version"]
    assert result["data_cutoff"] == "2026-07-10"
    assert result["source_path"] == "memory://shared-paper-allocation"
    assert result["source_hash"].startswith("sha256:")
    assert result["price_data_contract_version"]
    assert result["price_policy_validation"]["status"] == "passed"
    assert result["pit_status"]["status"] == "not_applicable_current_paper_planning"
    assert result["coverage"]["candidate_count"] == 1
    assert result["evidence_eligible"] is True
    assert result["degraded_codes"] == ()


def test_shared_plan_source_hash_binds_minute_pool_and_config() -> None:
    project_data = Path(__file__).resolve().parents[1] / "data"
    kwargs = {
        "auto_strategy_payload": _formal_planner_payload(
            board_candidates=[
                {
                    **_planner_candidate("600000", price=10.0),
                    "strategy_type": "board",
                }
            ]
        ),
        "paper_state": {"ledger_id": "test-funded-ledger", "initial_cash": 10_000.0, "cash": 10_000.0, "positions": []},
        "config": SharedAllocationConfig(initial_cash=10_000.0, data_root=project_data),
    }

    covered = plan_shared_paper_allocation(
        **kwargs,
        minute_pool=_verified_minute_pool({"600000"}),
    )
    missing = plan_shared_paper_allocation(
        **kwargs,
        minute_pool=_verified_minute_pool(set()),
    )

    assert covered["selected_count"] == 1
    assert missing["selected_count"] == 0
    assert covered["source_hash"] != missing["source_hash"]


def test_shared_plan_writer_rechecks_evidence_eligibility_before_latest(
    tmp_path: Path,
) -> None:
    payload = {
        "latest_eligible": True,
        "evidence_eligible": False,
        "plan_status": "blocked_funded_ledger_missing",
        "data_root_kind": "canonical",
    }
    results = tmp_path / "results"
    reports = tmp_path / "reports"
    results.mkdir()
    reports.mkdir()
    latest = results / "shared_paper_plan_latest.json"
    latest.write_text('{"sentinel": true}', encoding="utf-8")

    written = write_shared_paper_plan_outputs(
        payload,
        results_dir=results,
        reports_dir=reports,
    )

    assert written["latest_write_target"] == "run_specific"
    assert json.loads(latest.read_text(encoding="utf-8")) == {"sentinel": True}


def test_shared_planner_corrupt_funded_state_fails_closed_instead_of_using_config_cash(
    tmp_path: Path,
) -> None:
    strategy_path = tmp_path / "strategy.json"
    state_path = tmp_path / "paper_state.json"
    strategy_path.write_text(
        json.dumps({"long_candidates": [_planner_candidate("600000", price=10.0)]}),
        encoding="utf-8",
    )
    state_path.write_text("{bad json", encoding="utf-8")

    result = run_shared_paper_allocation_plan(
        initial_cash=10_000.0,
        data_root=Path(__file__).resolve().parents[1] / "data",
        auto_strategy_path=strategy_path,
        paper_state_path=state_path,
        minute_pool={"passed": True, "symbols": {"600000"}},
        write=False,
    )

    assert result["selected_for_paper_plan"] == []
    assert result["plan_status"] == "blocked_paper_state_untrusted"
    assert result["funded_state_trustworthy"] is False
    assert any("paper_state" in warning for warning in result["data_quality_warnings"])


def test_risk_position_zero_quantity_does_not_fall_back_to_100() -> None:
    frame = pd.DataFrame(
        {
            "close": [100.0] * 14,
            "high": [101.0] * 14,
            "low": [99.0] * 14,
        }
    )

    result = compute_position_reference(
        frame,
        symbol="600000",
        total_capital=500.0,
        max_position_pct=0.1,
    )

    assert result.position_shares == 0
    assert result.usable is False
    assert "整手" in result.reason


def test_manual_paper_account_new_default_is_user_10k() -> None:
    portfolio = paper_trading.create_default_paper_portfolio()

    assert portfolio["account"]["initial_cash"] == 10_000.0
    assert portfolio["account"]["cash"] == 10_000.0
    assert portfolio["account"]["scenario_id"] == "user_10k"
    assert portfolio["account"]["profile_id"] == "user_10k"


def test_manual_paper_account_100k_requires_acceptance_scenario() -> None:
    with pytest.raises(PortfolioContractError):
        paper_trading.create_default_paper_portfolio(initial_cash=100_000.0)

    portfolio = paper_trading.create_default_paper_portfolio(
        initial_cash=100_000.0,
        scenario_id="acceptance_100k",
    )
    assert portfolio["account"]["initial_cash"] == 100_000.0
    assert portfolio["account"]["scenario_id"] == "acceptance_100k"


def test_existing_legacy_100k_paper_file_loads_without_reset_and_gets_visible_label(
    tmp_path: Path,
) -> None:
    path = tmp_path / "paper_portfolio.json"
    payload = {
        "account": {"initial_cash": 100_000.0, "cash": 88_000.0, "currency": "CNY"},
        "positions": [],
        "trades": [],
        "paper_trading_only": True,
    }
    path.write_text(json.dumps(payload), encoding="utf-8")

    loaded = paper_trading.load_paper_portfolio(path)

    assert loaded["account"]["initial_cash"] == 100_000.0
    assert loaded["account"]["cash"] == 88_000.0
    assert loaded["account"]["scenario_id"] == "legacy_existing"
    assert loaded["account"]["legacy_state_preserved"] is True
    assert "Legacy" in loaded["account"]["capital_profile_label"]
    assert json.loads(path.read_text(encoding="utf-8")) == payload


def test_auto_paper_new_account_default_signature_is_10k() -> None:
    from quantdesk.paper import auto_paper_portfolio
    from quantdesk import auto_strategy_sandbox

    signature = inspect.signature(auto_paper_portfolio.run_auto_paper_portfolio)
    assert signature.parameters["initial_cash"].default == 10_000.0
    pending_signature = inspect.signature(auto_strategy_sandbox.create_auto_pending_orders)
    cycle_signature = inspect.signature(auto_strategy_sandbox.run_auto_strategy_cycle)
    assert pending_signature.parameters["initial_cash"].default == 10_000.0
    assert cycle_signature.parameters["initial_cash"].default == 10_000.0


def test_legacy_paper_simulator_cli_and_generated_command_default_to_user_10k() -> None:
    from scripts import run_paper_trading_simulator
    from quantdesk import auto_strategy_sandbox

    parser = run_paper_trading_simulator.build_arg_parser()
    source = inspect.getsource(auto_strategy_sandbox.run_auto_strategy_cycle)

    assert parser.parse_args(["--mode", "backtest"]).initial_cash is None
    assert parser.parse_args(["--mode", "live"]).initial_cash is None
    assert run_paper_trading_simulator.resolve_simulation_initial_cash(
        mode="backtest",
        requested_initial_cash=None,
        scenario_id="user_10k",
    ) == 100_000.0
    assert run_paper_trading_simulator.resolve_simulation_initial_cash(
        mode="live",
        requested_initial_cash=None,
        scenario_id="user_10k",
    ) == 10_000.0
    with pytest.raises(PortfolioContractError, match="acceptance_100k"):
        run_paper_trading_simulator.resolve_simulation_initial_cash(
            mode="live",
            requested_initial_cash=100_000.0,
            scenario_id="user_10k",
        )
    assert run_paper_trading_simulator.resolve_simulation_initial_cash(
        mode="live",
        requested_initial_cash=100_000.0,
        scenario_id="acceptance_100k",
    ) == 100_000.0
    assert "--initial-cash 100000" not in source


@pytest.mark.parametrize("value", [0.0, -1.0, float("nan"), float("inf")])
def test_paper_simulator_rejects_invalid_backtest_capital(value: float) -> None:
    from scripts import run_paper_trading_simulator

    with pytest.raises(PortfolioContractError, match="backtest_initial_cash_invalid"):
        run_paper_trading_simulator.resolve_simulation_initial_cash(
            mode="backtest",
            requested_initial_cash=value,
            scenario_id="user_10k",
        )


def test_paper_simulator_rejects_unknown_mode() -> None:
    from scripts import run_paper_trading_simulator

    with pytest.raises(PortfolioContractError, match="simulation_mode_invalid"):
        run_paper_trading_simulator.resolve_simulation_initial_cash(
            mode="unknown",
            requested_initial_cash=None,
            scenario_id="user_10k",
        )


def test_legacy_pending_order_planner_checks_fee_inclusive_cash() -> None:
    from quantdesk.auto_strategy_sandbox import create_auto_pending_orders

    result = create_auto_pending_orders(
        [
            {
                "symbol": "600000",
                "name": "sample",
                "strategy_type": "long",
                "close": 10.0,
                "source_item": {"close": 10.0},
            }
        ],
        initial_cash=1_001.0,
        max_position_pct=100.0,
        sizing_mode="fixed_100",
    )

    assert result["orders"] == []
    assert result["blocked_orders"][0]["blocked_reason"] == "blocked_cash_fee_inclusive"
