from __future__ import annotations

import copy
import hashlib
import importlib
from dataclasses import replace
from typing import Any

import pandas as pd
import pytest

from quantdesk.paper import portfolio_contracts
from quantdesk.paper.lifecycle import CandidateDecisionEvent
from quantdesk.paper.portfolio_contracts import (
    build_paper_order_intent,
    build_portfolio_snapshot,
    resolve_paper_capital_profile,
)
from quantdesk.risk.pre_trade_gate import PreTradeGateConfig


try:
    risk_gate = importlib.import_module("quantdesk.risk.risk_gate")
except ModuleNotFoundError:
    risk_gate = None

RiskGateDecision = getattr(risk_gate, "RiskGateDecision", None)
evaluate_paper_risk_gate = getattr(risk_gate, "evaluate_paper_risk_gate", None)
compute_risk_evidence_source_hash = getattr(
    risk_gate, "compute_risk_evidence_source_hash", None
)
compute_paper_order_intent_digest = getattr(
    portfolio_contracts, "compute_paper_order_intent_digest", None
)


def test_risk_gate_module_exposes_required_api() -> None:
    assert risk_gate is not None, "quantdesk.risk.risk_gate must exist"
    assert RiskGateDecision is not None
    assert callable(evaluate_paper_risk_gate)
    assert callable(compute_risk_evidence_source_hash)
    assert callable(compute_paper_order_intent_digest)


@pytest.fixture(autouse=True)
def _skip_until_risk_gate_exists(request: pytest.FixtureRequest) -> None:
    if risk_gate is None and request.node.name != "test_risk_gate_module_exposes_required_api":
        pytest.skip("dependent deterministic risk-gate tests wait for the module RED")


def _sha256(label: str) -> str:
    return f"sha256:{hashlib.sha256(label.encode('utf-8')).hexdigest()}"


def _formal_watch_event(symbol: str, *, occurred_at: str) -> dict[str, Any]:
    return CandidateDecisionEvent(
        event_id=f"risk-watch-{symbol}-{occurred_at[:10]}",
        symbol=symbol,
        occurred_at=occurred_at,
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
        payload={"name": "risk-gate sample"},
    ).to_dict()


def _funded_ledger() -> dict[str, Any]:
    return {
        "ledger_id": "risk-gate-funded-ledger",
        "initial_cash": 10_000.0,
        "cash": 7_000.0,
        "positions": [
            {
                "symbol": "000001",
                "name": "funded sample",
                "quantity": 100,
                "market_value": 1_000.0,
                "latest_price": 10.0,
                "entry_price": 9.5,
                "entry_date": "2026-06-01",
                "price_basis": "raw",
                "sector": "bank",
                "funded": True,
            }
        ],
    }
def _returns_frame(as_of: str, *, periods: int = 120) -> pd.DataFrame:
    index = pd.bdate_range(end=as_of, periods=periods)
    return pd.DataFrame(
        {
            "000001": [((idx % 9) - 4) / 1000 for idx in range(periods)],
            "600000": [((idx % 7) - 3) / 1200 for idx in range(periods)],
        },
        index=index,
    )


def _daily_rows(as_of: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for idx, day in enumerate(pd.bdate_range(end=as_of, periods=30)):
        close = 10.0 + idx * 0.01
        rows.append(
            {
                "date": day.date().isoformat(),
                "open": close - 0.02,
                "high": close + 0.08,
                "low": close - 0.08,
                "close": close,
                "volume": 1_000_000,
                "amount": close * 1_000_000,
            }
        )
    return rows


def _evidence(as_of: str) -> dict[str, Any]:
    payload = {
        "price": {
            "symbol": "600000",
            "value": 10.0,
            "price_basis": "raw",
            "consumer": "paper_execution",
            "data_cutoff": as_of,
            "source_path": "memory://risk/price/600000",
        },
        "sector": {
            "symbol": "600000",
            "value": "bank",
            "by_symbol": {"000001": "bank", "600000": "bank"},
            "data_cutoff": as_of,
            "source_path": "memory://risk/sector/600000",
        },
        "liquidity": {
            "symbol": "600000",
            "amount": 50_000_000.0,
            "liquidity_score": 80.0,
            "data_cutoff": as_of,
            "source_path": "memory://risk/liquidity/600000",
        },
        "returns": {
            "frame": _returns_frame(as_of),
            "data_cutoff": as_of,
            "source_path": "memory://risk/returns",
        },
        "smart_stop": {
            "daily_rows_by_symbol": {"000001": _daily_rows(as_of)},
            "sector_status_by_symbol": {
                "000001": {"status": "stable", "heat_score": 60.0, "rank_drop_5d": 0}
            },
            "data_cutoff": as_of,
            "source_path": "memory://risk/smart-stop",
        },
    }
    for name, component in payload.items():
        component["source_hash"] = (
            compute_risk_evidence_source_hash(component)
            if callable(compute_risk_evidence_source_hash)
            else _sha256(f"{name}:{as_of}")
        )
    return payload


def _reseal_component(case: dict[str, Any], component: str) -> None:
    if callable(compute_risk_evidence_source_hash):
        case["evidence"][component]["source_hash"] = compute_risk_evidence_source_hash(
            case["evidence"][component]
        )


def _case(*, as_of: str = "2026-07-10", requested_quantity: int = 100) -> dict[str, Any]:
    ledger = _funded_ledger()
    profile = resolve_paper_capital_profile()
    snapshot = build_portfolio_snapshot(
        ledger,
        profile,
        as_of=as_of,
        valuation_price_basis="raw",
    )
    event = _formal_watch_event("600000", occurred_at=f"{as_of}T09:31:00+08:00")
    intent = build_paper_order_intent(
        event,
        snapshot,
        reference_price=10.0,
        reference_price_basis="raw",
        requested_quantity=requested_quantity,
    )
    return {
        "intent": intent,
        "snapshot": snapshot,
        "profile": profile,
        "funded_ledger": ledger,
        "evidence": _evidence(as_of),
        "as_of": as_of,
    }


def _rebind_case_to_ledger(case: dict[str, Any], ledger: dict[str, Any]) -> None:
    snapshot = build_portfolio_snapshot(
        ledger,
        case["profile"],
        as_of=case["as_of"],
        valuation_price_basis="raw",
    )
    event = _formal_watch_event(
        case["intent"].symbol,
        occurred_at=f"{case['as_of']}T09:31:00+08:00",
    )
    case["funded_ledger"] = ledger
    case["snapshot"] = snapshot
    case["intent"] = build_paper_order_intent(
        event,
        snapshot,
        reference_price=case["intent"].reference_price,
        reference_price_basis="raw",
        requested_quantity=case["intent"].quantity,
    )


def _evaluate(case: dict[str, Any], **overrides: Any):
    kwargs = {
        "profile": case["profile"],
        "funded_ledger": case["funded_ledger"],
        "evidence": case["evidence"],
        "as_of": case["as_of"],
    }
    kwargs.update(overrides)
    return evaluate_paper_risk_gate(case["intent"], case["snapshot"], **kwargs)


def _payload(decision: Any) -> dict[str, Any]:
    assert isinstance(decision, RiskGateDecision)
    payload = decision.to_dict()
    assert isinstance(payload, dict)
    return payload


def test_complete_trusted_evidence_passes_without_creating_an_order() -> None:
    case = _case()
    payload = _payload(_evaluate(case))

    assert payload["status"] == "passed"
    assert payload["passed"] is True
    assert payload["veto"] is False
    assert payload["evidence_eligible"] is True
    assert payload["reason_codes"] == ()
    assert set(payload["check_results"]) >= {"pre_trade", "concentration", "liquidity", "var"}
    assert payload.get("execution_status") in {None, "not_submitted"}
    assert payload["price_data_contract_version"] == case["intent"].price_data_contract_version
    assert payload["price_policy_validation"]["status"] == "passed"
    assert payload["price_policy_validation"]["consumer"] == "paper_execution"
    assert payload["pit_status"]["status"] == "passed"
    assert payload["coverage"]["kind"] == "candidate_decision_event"
    assert case["intent"].portfolio_source_hash == case["snapshot"].source_hash
    assert case["intent"].portfolio_snapshot_digest == case["snapshot"].snapshot_digest
    assert case["intent"].intent_digest == compute_paper_order_intent_digest(case["intent"])
    assert payload["intent_digest"] == case["intent"].intent_digest
    assert payload["snapshot_digest"] == case["snapshot"].snapshot_digest
    assert payload["ledger_source_hash"] == case["snapshot"].source_hash
    assert payload["evidence_summary"]
    assert payload["config_digest"].startswith("sha256:")


def test_old_intent_cannot_be_reused_with_a_different_funded_ledger_snapshot() -> None:
    case = _case()
    old_intent = case["intent"]
    changed_ledger = copy.deepcopy(case["funded_ledger"])
    changed_ledger["ledger_id"] = "different-funded-ledger"
    changed_snapshot = build_portfolio_snapshot(
        changed_ledger,
        case["profile"],
        as_of=case["as_of"],
        valuation_price_basis="raw",
    )
    case["funded_ledger"] = changed_ledger
    case["snapshot"] = changed_snapshot
    case["intent"] = old_intent

    payload = _payload(_evaluate(case))

    assert payload["status"] == "insufficient_evidence"
    assert "paper_intent_portfolio_binding_mismatch" in payload["reason_codes"]


def test_intent_digest_catches_fee_price_and_policy_tampering() -> None:
    fee_case = _case()
    fee_case["intent"] = replace(
        fee_case["intent"], estimated_fee=fee_case["intent"].estimated_fee + 1.0
    )
    policy_case = _case()
    policy_case["intent"] = replace(
        policy_case["intent"],
        price_policy_validation={
            **policy_case["intent"].price_policy_validation,
            "accepted_price_basis": "qfq",
        },
    )
    price_case = _case()
    price_case["intent"] = replace(
        price_case["intent"], reference_price=price_case["intent"].reference_price + 0.01
    )

    for case in (fee_case, price_case, policy_case):
        payload = _payload(_evaluate(case))
        assert payload["status"] == "insufficient_evidence"
        assert "paper_intent_digest_mismatch" in payload["reason_codes"]


def test_business_evidence_values_are_bound_into_the_decision_digest() -> None:
    baseline = _payload(_evaluate(_case()))

    sector_case = _case()
    sector_case["evidence"]["sector"]["value"] = "non_bank"
    sector_case["evidence"]["sector"]["by_symbol"]["600000"] = "non_bank"
    _reseal_component(sector_case, "sector")
    sector_payload = _payload(_evaluate(sector_case))

    liquidity_case = _case()
    liquidity_case["evidence"]["liquidity"]["amount"] = 40_000_000.0
    _reseal_component(liquidity_case, "liquidity")
    liquidity_payload = _payload(_evaluate(liquidity_case))

    assert sector_payload["decision_digest"] != baseline["decision_digest"]
    assert liquidity_payload["decision_digest"] != baseline["decision_digest"]
    assert sector_payload["source_hash"] != baseline["source_hash"]
    assert liquidity_payload["source_hash"] != baseline["source_hash"]


def test_business_content_tampering_is_detected_against_declared_source_hash() -> None:
    cases: list[tuple[dict[str, Any], str]] = []
    price_case = _case()
    price_case["evidence"]["price"]["value"] = 10.01
    cases.append((price_case, "price_source_hash_mismatch"))
    sector_case = _case()
    sector_case["evidence"]["sector"]["by_symbol"]["000001"] = "insurance"
    cases.append((sector_case, "sector_source_hash_mismatch"))
    liquidity_case = _case()
    liquidity_case["evidence"]["liquidity"]["amount"] = 40_000_000.0
    cases.append((liquidity_case, "liquidity_source_hash_mismatch"))
    returns_case = _case()
    returns_case["evidence"]["returns"]["frame"].iloc[0, 0] += 0.0001
    cases.append((returns_case, "returns_source_hash_mismatch"))
    stop_case = _case()
    stop_case["evidence"]["smart_stop"]["daily_rows_by_symbol"]["000001"][0]["close"] += 0.01
    cases.append((stop_case, "smart_stop_source_hash_mismatch"))

    for case, expected_reason in cases:
        payload = _payload(_evaluate(case))
        assert payload["status"] == "insufficient_evidence"
        assert expected_reason in payload["reason_codes"]


def test_anonymous_or_watchlist_shape_cannot_supply_risk_positions() -> None:
    invalid_ledgers = [
        {"cash": 7_000.0, "positions": copy.deepcopy(_funded_ledger()["positions"])},
        {"watchlist_id": "watch-1", "items": copy.deepcopy(_funded_ledger()["positions"])},
    ]
    for ledger in invalid_ledgers:
        payload = _payload(_evaluate(_case(), funded_ledger=ledger))
        assert payload["status"] == "insufficient_evidence"
        assert payload["veto"] is True
        assert "funded_ledger_identity_missing" in payload["reason_codes"]


def test_tampered_snapshot_is_rejected_even_when_eligibility_flags_stay_true() -> None:
    case = _case()
    case["snapshot"] = replace(case["snapshot"], available_cash=999_999.0)

    payload = _payload(_evaluate(case))

    assert payload["status"] == "insufficient_evidence"
    assert "portfolio_snapshot_digest_mismatch" in payload["reason_codes"]


def test_missing_cash_or_equity_fails_closed_before_legacy_checks_can_skip() -> None:
    mutations = [
        ("available_cash", None, "cash_evidence_missing"),
        ("net_equity", None, "equity_evidence_missing"),
    ]
    for field, value, reason in mutations:
        case = _case()
        case["snapshot"] = replace(case["snapshot"], **{field: value})
        payload = _payload(_evaluate(case))
        assert payload["status"] == "insufficient_evidence"
        assert reason in payload["reason_codes"]


def test_each_required_risk_component_requires_complete_provenance() -> None:
    for component in ("price", "sector", "liquidity", "returns", "smart_stop"):
        case = _case()
        case["evidence"][component].pop("source_path")
        payload = _payload(_evaluate(case))
        assert payload["status"] == "insufficient_evidence"
        assert f"{component}_provenance_missing" in payload["reason_codes"]


def test_future_dated_risk_evidence_is_rejected_component_by_component() -> None:
    for component in ("price", "sector", "liquidity", "returns", "smart_stop"):
        case = _case()
        case["evidence"][component]["data_cutoff"] = "2026-07-11"
        payload = _payload(_evaluate(case))
        assert payload["status"] == "insufficient_evidence"
        assert f"{component}_evidence_after_as_of" in payload["reason_codes"]


def test_malformed_provenance_hash_is_rejected_component_by_component() -> None:
    for component in ("price", "sector", "liquidity", "returns", "smart_stop"):
        case = _case()
        case["evidence"][component]["source_hash"] = "sha256:not-a-real-hash"
        payload = _payload(_evaluate(case))
        assert payload["status"] == "insufficient_evidence"
        assert f"{component}_source_hash_invalid" in payload["reason_codes"]


def test_sector_provenance_must_cover_and_match_every_funded_asset() -> None:
    missing_target = _case()
    missing_target["evidence"]["sector"]["by_symbol"].pop("600000")
    missing_funded = _case()
    missing_funded["evidence"]["sector"]["by_symbol"].pop("000001")
    mismatch = _case()
    mismatch["evidence"]["sector"]["by_symbol"]["000001"] = "insurance"

    for case in (missing_target, missing_funded):
        payload = _payload(_evaluate(case))
        assert payload["status"] == "insufficient_evidence"
        assert "sector_asset_missing" in payload["reason_codes"]
    payload = _payload(_evaluate(mismatch))
    assert payload["status"] == "insufficient_evidence"
    assert "funded_position_sector_mismatch" in payload["reason_codes"]


def test_nan_sector_labels_are_not_accepted_as_classification_text() -> None:
    case = _case()
    case["evidence"]["sector"]["value"] = float("nan")
    case["evidence"]["sector"]["by_symbol"]["600000"] = float("nan")
    _reseal_component(case, "sector")

    payload = _payload(_evaluate(case))

    assert payload["status"] == "insufficient_evidence"
    assert {
        "sector_evidence_invalid",
        "sector_asset_missing",
    }.intersection(payload["reason_codes"])


def test_nan_source_path_is_not_accepted_as_provenance_text() -> None:
    case = _case()
    case["evidence"]["sector"]["source_path"] = float("nan")
    _reseal_component(case, "sector")

    payload = _payload(_evaluate(case))

    assert payload["status"] == "insufficient_evidence"
    assert "sector_provenance_missing" in payload["reason_codes"]


def test_funded_position_without_its_own_sector_evidence_fails_closed() -> None:
    case = _case()
    ledger = copy.deepcopy(case["funded_ledger"])
    ledger["positions"][0].pop("sector")
    _rebind_case_to_ledger(case, ledger)

    payload = _payload(_evaluate(case))

    assert payload["status"] == "insufficient_evidence"
    assert "funded_position_sector_missing" in payload["reason_codes"]


def test_non_raw_execution_price_evidence_is_never_formal() -> None:
    case = _case()
    case["evidence"]["price"]["price_basis"] = "qfq"

    payload = _payload(_evaluate(case))

    assert payload["status"] == "insufficient_evidence"
    assert "execution_price_basis_not_raw" in payload["reason_codes"]


def test_missing_or_nonfinite_return_assets_cannot_be_hidden_by_fillna_zero() -> None:
    variants = []
    missing = _case()
    missing["evidence"]["returns"]["frame"] = missing["evidence"]["returns"]["frame"].drop(columns=["000001"])
    variants.append((missing, "returns_asset_missing"))
    for value in (float("nan"), float("inf")):
        nonfinite = _case()
        nonfinite["evidence"]["returns"]["frame"].iloc[0, 0] = value
        variants.append((nonfinite, "returns_non_finite"))

    for case, reason in variants:
        payload = _payload(_evaluate(case))
        assert payload["status"] == "insufficient_evidence"
        assert reason in payload["reason_codes"]


def test_var_requires_at_least_one_hundred_complete_observations() -> None:
    case = _case()
    case["evidence"]["returns"]["frame"] = _returns_frame(case["as_of"], periods=99)
    _reseal_component(case, "returns")

    payload = _payload(_evaluate(case))

    assert payload["status"] == "insufficient_evidence"
    assert "returns_insufficient_observations" in payload["reason_codes"]


def test_pre_trade_failure_vetoes_the_intent() -> None:
    payload = _payload(_evaluate(_case(requested_quantity=600)))

    assert payload["status"] == "vetoed"
    assert payload["veto"] is True
    assert "pre_trade_gate_veto" in payload["reason_codes"]


def test_concentration_alert_is_a_veto_not_a_soft_warning() -> None:
    relaxed_pretrade = PreTradeGateConfig(
        max_position_pct=1.0,
        max_industry_pct=1.0,
        max_positions=10,
        max_turnover_per_rebalance=2.0,
        max_total_exposure=2.0,
        enforce=True,
    )

    payload = _payload(
        _evaluate(_case(requested_quantity=300), pre_trade_config=relaxed_pretrade)
    )

    assert payload["status"] == "vetoed"
    assert "concentration_veto" in payload["reason_codes"]


def test_low_liquidity_vetoes_but_does_not_create_a_position_instruction() -> None:
    case = _case()
    case["evidence"]["liquidity"].update({"amount": 100_000.0, "liquidity_score": 10.0})
    _reseal_component(case, "liquidity")

    payload = _payload(_evaluate(case))

    assert payload["status"] == "vetoed"
    assert "liquidity_veto" in payload["reason_codes"]
    assert "position" not in payload


def test_smart_stop_uses_explicit_as_of_and_exposes_only_exit_review_reasons() -> None:
    first = _payload(_evaluate(_case(as_of="2026-07-10")))
    later = _payload(_evaluate(_case(as_of="2026-07-13")))

    first_review = first["paper_exit_reviews"][0]
    later_review = later["paper_exit_reviews"][0]
    assert first_review["holding_days"] == 39
    assert later_review["holding_days"] == 42
    assert first_review["symbol"] == "000001"
    assert first_review["status"] in {"safe", "watch", "triggered", "unknown"}
    assert isinstance(first_review["reasons"], list)
    forbidden = {
        "stop",
        "stop_price",
        "atr_stop_price",
        "fixed_stop_price",
        "composite_stop_price",
        "target",
        "target_price",
        "quantity",
        "latest_price",
        "risk_line",
    }
    assert forbidden.isdisjoint(first_review)


def test_llm_review_can_never_override_a_deterministic_veto() -> None:
    case = _case()
    case["evidence"]["liquidity"].update({"amount": 1.0, "liquidity_score": 1.0})
    _reseal_component(case, "liquidity")

    payload = _payload(
        _evaluate(
            case,
            llm_review={"decision": "pass", "override_veto": True, "confidence": 1.0},
        )
    )

    assert payload["status"] == "vetoed"
    assert payload["veto"] is True
    assert payload["llm_override_applied"] is False
    assert "liquidity_veto" in payload["reason_codes"]


def test_identical_inputs_produce_a_stable_bound_decision_digest() -> None:
    case = _case()

    first = _payload(_evaluate(case))
    second = _payload(_evaluate(case))

    assert first["decision_digest"] == second["decision_digest"]
    assert first["decision_digest"].startswith("sha256:")
    assert len(first["decision_digest"]) == 71


def test_config_and_rebuilt_ledger_are_bound_into_decision_digest() -> None:
    baseline_case = _case()
    baseline = _payload(_evaluate(baseline_case))
    non_enforcing = PreTradeGateConfig(enforce=False)
    config_variant = _payload(
        _evaluate(_case(), pre_trade_config=non_enforcing)
    )
    ledger_variant_case = _case()
    changed_ledger = copy.deepcopy(ledger_variant_case["funded_ledger"])
    changed_ledger["ledger_id"] = "digest-variant-ledger"
    _rebind_case_to_ledger(ledger_variant_case, changed_ledger)
    ledger_variant = _payload(_evaluate(ledger_variant_case))

    assert config_variant["status"] == "passed"
    assert config_variant["config_digest"] != baseline["config_digest"]
    assert config_variant["decision_digest"] != baseline["decision_digest"]
    assert ledger_variant["status"] == "passed"
    assert ledger_variant["snapshot_digest"] != baseline["snapshot_digest"]
    assert ledger_variant["decision_digest"] != baseline["decision_digest"]


def test_decision_cutoff_is_the_earliest_verified_input_cutoff() -> None:
    case = _case()
    case["evidence"]["price"]["data_cutoff"] = "2026-07-09"
    _reseal_component(case, "price")

    payload = _payload(_evaluate(case))

    assert payload["status"] == "passed"
    assert payload["data_cutoff"] == "2026-07-09"


def test_smart_stop_gate_path_never_falls_back_to_file_io(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    smart_stop_module = importlib.import_module("quantdesk.risk.smart_stop")

    def _unexpected_io(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("risk gate must use only supplied in-memory smart-stop evidence")

    monkeypatch.setattr(smart_stop_module, "load_daily_rows_for_position", _unexpected_io)
    monkeypatch.setattr(smart_stop_module, "_sector_for", _unexpected_io)

    payload = _payload(_evaluate(_case()))

    assert payload["status"] == "passed"


def test_evaluation_is_pure_and_does_not_mutate_ledger_or_evidence() -> None:
    case = _case()
    ledger_before = copy.deepcopy(case["funded_ledger"])
    evidence_before = copy.deepcopy(case["evidence"])

    _evaluate(case)

    assert case["funded_ledger"] == ledger_before
    pd.testing.assert_frame_equal(
        case["evidence"]["returns"]["frame"],
        evidence_before["returns"]["frame"],
    )
    current_without_frame = copy.deepcopy(case["evidence"])
    before_without_frame = copy.deepcopy(evidence_before)
    current_without_frame["returns"].pop("frame")
    before_without_frame["returns"].pop("frame")
    assert current_without_frame == before_without_frame


def test_every_decision_carries_the_eight_shared_paper_only_boundaries() -> None:
    payload = _payload(_evaluate(_case()))

    assert {
        key: payload[key]
        for key in (
            "is_real_order",
            "research_only",
            "paper_only",
            "paper_trading_only",
            "no_broker",
            "no_real_orders",
            "not_investment_advice",
            "not_position_advice",
        )
    } == {
        "is_real_order": False,
        "research_only": True,
        "paper_only": True,
        "paper_trading_only": True,
        "no_broker": True,
        "no_real_orders": True,
        "not_investment_advice": True,
        "not_position_advice": True,
    }

