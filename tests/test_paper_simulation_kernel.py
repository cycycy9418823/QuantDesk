from __future__ import annotations

import copy
import hashlib
import importlib
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from typing import Any

import pandas as pd
import pytest

from quantdesk.intraday.minute_pool_capability import minute_pool_capability_source_hash
from quantdesk.paper.lifecycle import CandidateDecisionEvent
from quantdesk.paper.market_rules import paper_only_boundary
from quantdesk.paper.portfolio_contracts import (
    build_paper_order_intent,
    build_portfolio_snapshot,
    compute_paper_order_intent_digest,
    resolve_paper_capital_profile,
)
from quantdesk.paper.position_management_engine import apply_conditional_execution_to_state
from quantdesk.paper.state_store import PaperStateStore
from quantdesk.paper.trade_costs import compute_trade_costs
from quantdesk.risk.risk_gate import (
    compute_risk_evidence_source_hash,
    evaluate_paper_risk_gate,
)
from quantdesk.version import API_SCHEMA_VERSION


try:
    kernel = importlib.import_module("quantdesk.paper.simulation_kernel")
except ModuleNotFoundError:
    kernel = None


def test_simulation_kernel_exposes_required_api() -> None:
    assert kernel is not None, "quantdesk.paper.simulation_kernel must exist"
    for name in (
        "ExecutionEvidence",
        "SettlementPolicy",
        "FillEvent",
        "RejectionEvent",
        "simulate_paper_intent",
        "apply_paper_event",
    ):
        assert hasattr(kernel, name), name


@pytest.fixture(autouse=True)
def _skip_until_kernel_exists(request: pytest.FixtureRequest) -> None:
    if kernel is None and request.node.name != "test_simulation_kernel_exposes_required_api":
        pytest.skip("dependent tests wait for the kernel RED")


def _ledger() -> dict[str, Any]:
    return {
        "ledger_id": "phase8-ledger",
        "initial_cash": 10_000.0,
        "cash": 7_000.0,
        "positions": [
            {
                "symbol": "000001",
                "quantity": 100,
                "sellable_quantity": 100,
                "frozen_quantity": 0,
                "market_value": 1_000.0,
                "latest_price": 10.0,
                "avg_cost": 9.5,
                "entry_date": "2026-06-01",
                "last_buy_date": "2026-06-01",
                "price_basis": "raw",
                "sector": "bank",
                "funded": True,
            }
        ],
    }


def _returns(as_of: str, symbols: list[str]) -> pd.DataFrame:
    dates = pd.bdate_range(end=as_of, periods=120)
    return pd.DataFrame(
        {symbol: [((i + offset) % 9 - 4) / 1500 for i in range(120)] for offset, symbol in enumerate(symbols)},
        index=dates,
    )


def _risk_case(symbol: str = "600000", quantity: int = 100) -> dict[str, Any]:
    as_of = "2026-07-10"
    ledger = _ledger()
    if symbol.startswith("688"):
        # Keep the valid 200-share STAR minimum below the unchanged Phase-7
        # concentration threshold; this test is about the board lot contract.
        ledger["cash"] = 9_000.0
    profile = resolve_paper_capital_profile()
    snapshot = build_portfolio_snapshot(ledger, profile, as_of=as_of, valuation_price_basis="raw")
    event = CandidateDecisionEvent(
        event_id=f"phase8-{symbol}",
        symbol=symbol,
        occurred_at=f"{as_of}T09:31:00+08:00",
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
        payload={"name": "phase8"},
    ).to_dict()
    intent = build_paper_order_intent(
        event,
        snapshot,
        reference_price=10.0,
        reference_price_basis="raw",
        requested_quantity=quantity,
    )
    symbols = ["000001", symbol]
    daily_rows = [
        {
            "date": day.date().isoformat(),
            "open": 9.9,
            "high": 10.2,
            "low": 9.8,
            "close": 10.0,
            "volume": 1_000_000,
            "amount": 10_000_000,
        }
        for day in pd.bdate_range(end=as_of, periods=30)
    ]
    components: dict[str, dict[str, Any]] = {
        "price": {
            "symbol": symbol,
            "value": 10.0,
            "price_basis": "raw",
            "consumer": "paper_execution",
            "data_cutoff": as_of,
            "source_path": f"memory://price/{symbol}",
        },
        "sector": {
            "symbol": symbol,
            "value": "bank",
            "by_symbol": {"000001": "bank", symbol: "bank"},
            "data_cutoff": as_of,
            "source_path": "memory://sector",
        },
        "liquidity": {
            "symbol": symbol,
            "amount": 50_000_000.0,
            "liquidity_score": 80.0,
            "data_cutoff": as_of,
            "source_path": "memory://liquidity",
        },
        "returns": {
            "frame": _returns(as_of, symbols),
            "data_cutoff": as_of,
            "source_path": "memory://returns",
        },
        "smart_stop": {
            "daily_rows_by_symbol": {"000001": daily_rows},
            "sector_status_by_symbol": {"000001": {"status": "stable", "heat_score": 60.0}},
            "data_cutoff": as_of,
            "source_path": "memory://smart-stop",
        },
    }
    for component in components.values():
        component["source_hash"] = compute_risk_evidence_source_hash(component)
    risk = evaluate_paper_risk_gate(
        intent,
        snapshot,
        profile=profile,
        funded_ledger=ledger,
        evidence=components,
        as_of=as_of,
    )
    assert risk.passed, risk.reason_codes
    return {"intent": intent, "snapshot": snapshot, "risk": risk, "ledger": ledger}


def _minute_frame(symbol: str, day: str = "2026-07-13") -> pd.DataFrame:
    return pd.DataFrame(
        [
            {"symbol": symbol, "datetime": f"{day} 09:30:00", "open": 10.2, "high": 10.3, "low": 10.1, "close": 10.25, "volume": 1000, "amount": 10250},
            {"symbol": symbol, "datetime": f"{day} 09:31:00", "open": 10.25, "high": 10.35, "low": 10.2, "close": 10.3, "volume": 1200, "amount": 12360},
        ]
    )


def _execution(case: dict[str, Any], *, frame: pd.DataFrame | None = None):
    symbol = case["intent"].symbol
    day = "2026-07-13"
    minute_frame = _minute_frame(symbol, day) if frame is None else frame
    pool = {
        "schema_version": API_SCHEMA_VERSION,
        "task_type": "verified_minute_pool_snapshot",
        "status": "passed",
        "passed": True,
        "quality_status": "passed",
        "target_date": day,
        "symbols": [symbol],
        "symbol_count": 1,
        "usable_count": 1,
        "total_symbols": 1,
        "partial_count": 0,
        "insufficient_count": 0,
        "invalid_count": 0,
        "missing_count": 0,
        "source_snapshot": "latest",
        "latest_quality_status": "passed",
        "snapshot_available": True,
        "snapshot_path": "memory://minute-pool/snapshot.json",
        "job_path": "memory://minute-pool/job",
        "read_only": True,
        "network_used": False,
        "canonical_write": False,
    }
    pool["source_hash"] = minute_pool_capability_source_hash(pool)
    daily = {
        "schema_version": API_SCHEMA_VERSION,
        "status": "passed",
        "verified": True,
        "evidence_kind": "verified_daily_history",
        "symbol": symbol,
        "price_basis": "raw",
        "previous_close": 10.0,
        "previous_trade_date": "2026-07-10",
        "source_path": f"memory://daily/{symbol}",
        "proxy_thresholds_used": False,
    }
    daily["source_hash"] = kernel.compute_execution_component_source_hash(daily)
    calendar = {
        "schema_version": API_SCHEMA_VERSION,
        "status": "passed",
        "verified": True,
        "evidence_kind": "verified_a_share_trading_calendar",
        "signal_date": case["intent"].as_of,
        "next_trade_date": day,
        "source_path": "memory://calendar/a-share",
    }
    calendar["source_hash"] = kernel.compute_execution_component_source_hash(calendar)
    market_metadata = {
        "schema_version": API_SCHEMA_VERSION,
        "status": "passed",
        "verified": True,
        "evidence_kind": "verified_market_status",
        "symbol": symbol,
        "is_st": False,
        "suspended": False,
        "source_path": f"memory://market-status/{symbol}/{day}",
    }
    market_metadata["source_hash"] = kernel.compute_execution_component_source_hash(market_metadata)
    evidence = kernel.ExecutionEvidence(
        symbol=symbol,
        execution_date=day,
        price_basis="raw",
        minute_frame=minute_frame,
        minute_source_path=f"memory://minute/{symbol}/{day}",
        minute_source_hash=kernel.compute_minute_frame_source_hash(minute_frame),
        minute_pool=pool,
        daily_history=daily,
        trading_calendar=calendar,
        market_metadata=market_metadata,
    )
    return replace(evidence, evidence_digest=kernel.compute_execution_evidence_digest(evidence))


def _simulate(case: dict[str, Any], evidence=None):
    return kernel.simulate_paper_intent(
        case["intent"],
        case["risk"],
        case["snapshot"],
        case["ledger"],
        evidence or _execution(case),
        kernel.SettlementPolicy(),
    )


def _reseal_evidence(evidence):
    return replace(evidence, evidence_digest=kernel.compute_execution_evidence_digest(replace(evidence, evidence_digest="")))


def test_happy_buy_uses_first_minute_open_and_one_fee_calculation() -> None:
    case = _risk_case()
    event = _simulate(case)
    assert isinstance(event, kernel.FillEvent)
    assert event.fill_price == 10.2
    assert event.quantity == 100
    assert event.cash_delta == pytest.approx(-(event.gross_amount + event.total_fee))
    assert event.total_fee == pytest.approx(event.commission + event.stamp_duty + event.transfer_fee + event.slippage)
    assert event.stamp_duty == 0
    assert event.sellable_delta == 0 and event.frozen_delta == 100
    assert event.filled is True
    assert event.cash_before == 7000.0
    assert event.cash_after == pytest.approx(7000.0 + event.cash_delta)
    assert (event.position_before, event.position_after) == (0, 100)
    for key, value in {
        "is_real_order": False, "research_only": True, "paper_only": True,
        "paper_trading_only": True, "no_broker": True, "no_real_orders": True,
        "not_investment_advice": True, "not_position_advice": True,
    }.items():
        assert event.to_dict()[key] is value


@pytest.mark.parametrize(
    ("mutation", "reason"),
    [
        (lambda e: replace(e, price_basis="qfq"), "execution_price_basis_not_raw"),
        (lambda e: replace(e, minute_source_hash="sha256:" + "0" * 64), "minute_source_hash_mismatch"),
        (lambda e: replace(e, minute_pool={**e.minute_pool, "status": "local_available_unverified_pool"}), "minute_pool_not_verified"),
        (lambda e: replace(e, minute_pool={**e.minute_pool, "target_date": "2026-07-12"}), "minute_pool_target_date_mismatch"),
        (lambda e: replace(e, minute_pool={**e.minute_pool, "symbols": ["000001"]}), "minute_pool_symbol_missing"),
        (lambda e: replace(e, minute_pool={**e.minute_pool, "schema_version": "1"}), "minute_pool_schema_invalid"),
        (lambda e: replace(e, minute_pool={**e.minute_pool, "usable_count": 2}), "minute_pool_count_inconsistent"),
        (lambda e: replace(e, minute_pool={**e.minute_pool, "snapshot_path": ""}), "minute_pool_provenance_missing"),
        (lambda e: replace(e, minute_pool={**e.minute_pool, "source_snapshot": "synthetic"}), "minute_pool_source_snapshot_invalid"),
        (lambda e: replace(e, minute_pool={**e.minute_pool, "source_hash": "sha256:" + "0" * 64}), "minute_pool_source_hash_mismatch"),
        (lambda e: replace(e, daily_history={}), "daily_history_missing"),
        (lambda e: replace(e, daily_history={**e.daily_history, "verified": False}), "daily_history_not_verified"),
        (lambda e: replace(e, daily_history={**e.daily_history, "schema_version": "1"}), "daily_history_schema_invalid"),
        (lambda e: replace(e, daily_history={**e.daily_history, "proxy_thresholds_used": True}), "daily_history_proxy_forbidden"),
        (lambda e: replace(e, daily_history={**e.daily_history, "previous_trade_date": "2026-07-13"}), "daily_history_date_invalid"),
        (lambda e: replace(e, daily_history={**e.daily_history, "previous_trade_date": "2026-07-09"}), "daily_history_date_invalid"),
    ],
)
def test_execution_evidence_fail_closed(mutation, reason: str) -> None:
    case = _risk_case()
    evidence = _reseal_evidence(mutation(_execution(case)))
    result = _simulate(case, evidence)
    assert isinstance(result, kernel.RejectionEvent)
    assert reason in result.reason_codes
    assert result.cash_delta == 0 and result.position_delta == 0


@pytest.mark.parametrize(
    ("frame_mutator", "reason"),
    [
        (lambda f: f.iloc[::-1].reset_index(drop=True), "minute_source_time_order_invalid"),
        (lambda f: f.assign(volume=[0, 1200]), "first_minute_no_volume"),
        (lambda f: f.drop(columns=["volume"]), "minute_quality_gate_failed"),
    ],
)
def test_minute_quality_reverse_schema_and_zero_volume_are_no_fill(frame_mutator, reason: str) -> None:
    case = _risk_case()
    frame = frame_mutator(_minute_frame(case["intent"].symbol))
    result = _simulate(case, _execution(case, frame=frame))
    assert isinstance(result, kernel.RejectionEvent)
    assert reason in result.reason_codes


def test_minute_frame_cannot_substitute_another_pool_members_symbol() -> None:
    case = _risk_case()
    frame = _minute_frame("000001")
    evidence = _execution(case, frame=frame)
    result = _simulate(case, evidence)
    assert isinstance(result, kernel.RejectionEvent)
    assert "minute_frame_symbol_mismatch" in result.reason_codes


def test_verified_last_passed_pool_remains_usable_with_degraded_latest_provenance() -> None:
    case = _risk_case()
    evidence = _execution(case)
    pool = {
        **evidence.minute_pool,
        "source_snapshot": "last_passed",
        "latest_quality_status": "partial",
        "warnings": ["latest larger pool remains partial"],
    }
    pool["source_hash"] = minute_pool_capability_source_hash(pool)
    evidence = _reseal_evidence(replace(evidence, minute_pool=pool))
    assert isinstance(_simulate(case, evidence), kernel.FillEvent)


def test_verified_calendar_must_prove_the_scheduled_next_session() -> None:
    case = _risk_case()
    evidence = _execution(case)
    bad = replace(
        evidence,
        trading_calendar={**evidence.trading_calendar, "next_trade_date": "2026-07-14"},
    )
    result = _simulate(case, _reseal_evidence(bad))
    assert isinstance(result, kernel.RejectionEvent)
    assert "trading_calendar_next_session_mismatch" in result.reason_codes


def test_missing_opening_minute_cannot_be_relabelled_next_open() -> None:
    case = _risk_case()
    frame = _minute_frame(case["intent"].symbol).copy()
    frame["datetime"] = ["2026-07-13 10:00:00", "2026-07-13 10:01:00"]
    result = _simulate(case, _execution(case, frame=frame))
    assert isinstance(result, kernel.RejectionEvent)
    assert "next_open_bar_missing" in result.reason_codes


def test_risk_veto_and_tampering_cannot_be_overridden() -> None:
    case = _risk_case()
    veto = replace(case["risk"], status="vetoed", passed=False, veto=True)
    result = kernel.simulate_paper_intent(case["intent"], veto, case["snapshot"], case["ledger"], _execution(case), kernel.SettlementPolicy())
    assert isinstance(result, kernel.RejectionEvent)
    assert "risk_decision_not_passed" in result.reason_codes

    forged = replace(case["risk"], source_hash="sha256:" + "0" * 64)
    result = kernel.simulate_paper_intent(case["intent"], forged, case["snapshot"], case["ledger"], _execution(case), kernel.SettlementPolicy())
    assert "risk_decision_digest_mismatch" in result.reason_codes

    for risk_mutation in (
        replace(case["risk"], execution_status="filled"),
        replace(case["risk"], source_path="tampered://risk"),
    ):
        result = kernel.simulate_paper_intent(case["intent"], risk_mutation, case["snapshot"], case["ledger"], _execution(case), kernel.SettlementPolicy())
        assert "risk_decision_digest_mismatch" in result.reason_codes

    tampered_snapshot = replace(case["snapshot"], available_cash=999_999.0)
    result = kernel.simulate_paper_intent(case["intent"], case["risk"], tampered_snapshot, case["ledger"], _execution(case), kernel.SettlementPolicy())
    assert "portfolio_snapshot_digest_mismatch" in result.reason_codes


def test_suspension_and_raw_open_limit_are_blocked_by_shared_market_rules() -> None:
    case = _risk_case()
    base = _execution(case)
    status = {**base.market_metadata, "suspended": True}
    status["source_hash"] = kernel.compute_execution_component_source_hash(status)
    suspended = _reseal_evidence(replace(base, market_metadata=status))
    assert "paper_market_rule_blocked" in _simulate(case, suspended).reason_codes

    frame = _minute_frame(case["intent"].symbol).copy()
    frame.loc[0, ["open", "high", "low", "close"]] = [11.0, 11.05, 10.95, 11.0]
    limit = _execution(case, frame=frame)
    assert "paper_market_rule_blocked" in _simulate(case, limit).reason_codes


def test_star_board_requires_two_hundred_share_minimum() -> None:
    case = _risk_case("688001", 200)
    assert isinstance(_simulate(case), kernel.FillEvent)
    profile = resolve_paper_capital_profile()
    snapshot = build_portfolio_snapshot(_ledger(), profile, as_of="2026-07-10", valuation_price_basis="raw")
    event = CandidateDecisionEvent(
        event_id="bad-star", symbol="688001", occurred_at="2026-07-10T09:31:00+08:00",
        requested_state="paper_watchlist", decision_origin="system",
        evidence={"eligible": True, "evidence_kind": "formal", "coverage": 1.0, "minimum_coverage": 1.0, "pit_eligible": True, "price_contract_eligible": True},
    ).to_dict()
    intent = build_paper_order_intent(event, snapshot, reference_price=10, reference_price_basis="raw", requested_quantity=100)
    assert intent.status == "blocked" and intent.quantity == 0


def _stores(tmp_path: Path, ledger: dict[str, Any]):
    event_store = PaperStateStore(tmp_path / "events.json", schema_version=None)
    legacy_store = PaperStateStore(tmp_path / "legacy.json", schema_version=None)
    legacy_store.write(copy.deepcopy(ledger))
    return event_store, legacy_store


def test_apply_shadow_dual_write_duplicate_and_golden_parity(tmp_path: Path) -> None:
    case = _risk_case()
    event = _simulate(case)
    legacy_projection = apply_conditional_execution_to_state(
        copy.deepcopy(case["ledger"]),
        {
            "plan_id": "phase8-golden",
            "trade_date": event.execution_date,
            "events": [{
                "action": "buy", "symbol": event.symbol, "name": "phase8",
                "strategy_type": "long", "is_reliable": True,
                "trigger_price": event.fill_price, "quantity": event.quantity,
                "trade_date": event.execution_date,
            }],
            "no_fill": [], "blocked": [],
        },
        daily_dir=tmp_path / "missing_daily",
    )["state"]
    event_store, legacy_store = _stores(tmp_path, case["ledger"])
    result = kernel.apply_paper_event(event, event_store=event_store, legacy_store=legacy_store, expected_ledger_source_hash=case["snapshot"].source_hash)
    assert result["status"] == "applied"
    state = legacy_store.read()
    position = next(row for row in state["positions"] if row["symbol"] == "600000")
    assert state["cash"] == pytest.approx(7000 + event.cash_delta)
    assert position["quantity"] == 100
    assert position["sellable_quantity"] == 0 and position["frozen_quantity"] == 100
    assert position["avg_cost"] == pytest.approx((-event.cash_delta) / 100)
    assert state["total_fees"] == pytest.approx(event.total_fee)
    legacy_position = next(row for row in legacy_projection["positions"] if row["symbol"] == "600000")
    assert state["cash"] == pytest.approx(legacy_projection["cash"])
    assert position["quantity"] == legacy_position["quantity"]
    assert position["sellable_quantity"] == legacy_position["sellable_quantity"]
    assert position["frozen_quantity"] == legacy_position["frozen_quantity"]
    assert position["avg_cost"] == pytest.approx(legacy_position["avg_cost"])
    assert event.total_fee == pytest.approx(legacy_projection["actions"][0]["total_fee"])
    duplicate = kernel.apply_paper_event(event, event_store=event_store, legacy_store=legacy_store, expected_ledger_source_hash=case["snapshot"].source_hash)
    assert duplicate["status"] == "duplicate_noop"
    assert legacy_store.read()["cash"] == state["cash"]


def test_apply_cas_conflict_event_tamper_rejection_and_dry_run(tmp_path: Path) -> None:
    case = _risk_case()
    fill = _simulate(case)
    event_store, legacy_store = _stores(tmp_path, case["ledger"])
    changed = legacy_store.read()
    changed["cash"] += 1
    legacy_store.write(changed)
    cas = kernel.apply_paper_event(fill, event_store=event_store, legacy_store=legacy_store, expected_ledger_source_hash=case["snapshot"].source_hash)
    assert cas["status"] == "cas_mismatch" and not event_store.path.exists()

    event_store, legacy_store = _stores(tmp_path / "dry", case["ledger"])
    dry = kernel.apply_paper_event(fill, event_store=event_store, legacy_store=legacy_store, expected_ledger_source_hash=case["snapshot"].source_hash, dry_run=True)
    assert dry["status"] == "dry_run" and not event_store.path.exists()

    tampered = replace(fill, fill_price=fill.fill_price + 1)
    rejected = kernel.apply_paper_event(tampered, event_store=event_store, legacy_store=legacy_store, expected_ledger_source_hash=case["snapshot"].source_hash)
    assert rejected["status"] == "event_integrity_failed" and not event_store.path.exists()

    rejection = _simulate(case, _reseal_evidence(replace(_execution(case), price_basis="qfq")))
    before = legacy_store.read()
    logged = kernel.apply_paper_event(rejection, event_store=event_store, legacy_store=legacy_store, expected_ledger_source_hash=case["snapshot"].source_hash)
    assert logged["status"] == "rejection_logged"
    assert legacy_store.read() == before


def test_apply_recomputes_fill_deltas_and_rejection_zero_delta_semantics(tmp_path: Path) -> None:
    case = _risk_case()
    fill = _simulate(case)
    event_store, legacy_store = _stores(tmp_path / "fill", case["ledger"])
    bad_fill = kernel._seal_event(replace(fill, event_id="", content_digest="", position_delta=0))
    result = kernel.apply_paper_event(
        bad_fill, event_store=event_store, legacy_store=legacy_store,
        expected_ledger_source_hash=case["snapshot"].source_hash,
    )
    assert result["status"] == "projection_blocked"
    assert "fill_before_after_mismatch" in result["reason_codes"]

    rejection = _simulate(case, _reseal_evidence(replace(_execution(case), price_basis="qfq")))
    bad_rejection = kernel._seal_event(replace(rejection, event_id="", content_digest="", cash_delta=1.0))
    event_store, legacy_store = _stores(tmp_path / "reject", case["ledger"])
    result = kernel.apply_paper_event(
        bad_rejection, event_store=event_store, legacy_store=legacy_store,
        expected_ledger_source_hash=case["snapshot"].source_hash,
    )
    assert result["status"] == "event_semantics_failed"


def test_apply_conflicting_same_event_id_fails_closed(tmp_path: Path) -> None:
    case = _risk_case()
    event = _simulate(case)
    event_store, legacy_store = _stores(tmp_path, case["ledger"])
    kernel.apply_paper_event(event, event_store=event_store, legacy_store=legacy_store, expected_ledger_source_hash=case["snapshot"].source_hash)
    conflict = replace(event, content_digest="sha256:" + "f" * 64)
    result = kernel.apply_paper_event(conflict, event_store=event_store, legacy_store=legacy_store, expected_ledger_source_hash=case["snapshot"].source_hash)
    assert result["status"] == "event_id_conflict"


def test_logged_fill_can_replay_legacy_projection_after_interruption(tmp_path: Path) -> None:
    case = _risk_case()
    event = _simulate(case)
    event_store, legacy_store = _stores(tmp_path, case["ledger"])
    event_store.write({
        "events": [event.to_dict()],
        "event_digests": {event.event_id: event.content_digest},
        "projection_status": {event.event_id: "logged"},
        **paper_only_boundary(),
    })
    result = kernel.apply_paper_event(
        event,
        event_store=event_store,
        legacy_store=legacy_store,
        expected_ledger_source_hash=case["snapshot"].source_hash,
    )
    assert result["status"] == "applied"
    assert len(event_store.read()["events"]) == 1
    assert event.event_id in legacy_store.read()["applied_event_ids"]


def test_applied_legacy_projection_reconciles_stale_logged_status(tmp_path: Path) -> None:
    case = _risk_case()
    event = _simulate(case)
    event_store, legacy_store = _stores(tmp_path, case["ledger"])
    assert kernel.apply_paper_event(
        event, event_store=event_store, legacy_store=legacy_store,
        expected_ledger_source_hash=case["snapshot"].source_hash,
    )["status"] == "applied"
    log = event_store.read()
    log["projection_status"][event.event_id] = "logged"
    event_store.write(log)
    result = kernel.apply_paper_event(
        event, event_store=event_store, legacy_store=legacy_store,
        expected_ledger_source_hash=case["snapshot"].source_hash,
    )
    assert result["status"] == "reconciled_duplicate"
    assert event_store.read()["projection_status"][event.event_id] == "applied"


def test_legacy_shares_shape_stays_consistent_when_projecting_an_add_buy() -> None:
    case = _risk_case()
    event = _simulate(case)
    state = {
        "ledger_id": "manual-shares",
        "cash": 7000.0,
        "positions": [{
            "symbol": event.symbol,
            "shares": 100,
            "available_shares": 100,
            "buy_price": 9.5,
            "buy_date": "2026-06-01",
        }],
    }
    event = replace(
        event,
        cash_before=7000.0,
        cash_after=7000.0 + event.cash_delta,
        position_before=100,
        position_after=200,
        sellable_before=100,
        sellable_after=100,
        frozen_before=0,
        frozen_after=100,
        ledger_source_hash=kernel.compute_ledger_source_hash(state),
    )
    event = kernel._seal_event(event)
    projected, reasons = kernel._project_fill(state, event)
    assert reasons == ()
    assert projected is not None
    position = projected["positions"][0]
    assert position["shares"] == position["quantity"] == 200
    assert position["available_shares"] == position["sellable_quantity"] == 100
    assert position["frozen_quantity"] == 100


def test_legacy_shares_shape_and_realized_pnl_stay_consistent_on_partial_sell() -> None:
    case = _risk_case()
    buy = _simulate(case)
    state = {
        "ledger_id": "legacy-sell",
        "cash": 7000.0,
        "realized_pnl": 0.0,
        "positions": [{
            "symbol": buy.symbol,
            "shares": 200,
            "available_shares": 200,
            "buy_price": 9.5,
            "buy_date": "2026-06-01",
        }],
    }
    costs = compute_trade_costs(side="sell", gross=buy.fill_price * 100)
    cash_delta = buy.fill_price * 100 - float(costs["total_fee"])
    sell = replace(
        buy,
        event_id="", content_digest="", side="sell", quantity=100,
        gross_amount=buy.fill_price * 100,
        commission=float(costs["commission"]), stamp_duty=float(costs["stamp_duty"]),
        transfer_fee=float(costs["transfer_fee"]), slippage=float(costs["slippage"]),
        total_fee=float(costs["total_fee"]), cash_delta=cash_delta,
        position_delta=-100, sellable_delta=-100, frozen_delta=0,
        cash_before=7000.0, cash_after=7000.0 + cash_delta,
        position_before=200, position_after=100,
        sellable_before=200, sellable_after=100,
        frozen_before=0, frozen_after=0,
        ledger_source_hash=kernel.compute_ledger_source_hash(state),
    )
    sell = kernel._seal_event(sell)
    projected, reasons = kernel._project_fill(state, sell)
    assert reasons == () and projected is not None
    position = projected["positions"][0]
    assert position["quantity"] == position["shares"] == 100
    assert position["sellable_quantity"] == position["available_shares"] == 100
    assert projected["realized_pnl"] == pytest.approx((buy.fill_price - 9.5) * 100 - float(costs["total_fee"]))


def test_same_ledger_concurrent_events_are_serialized_by_cas(tmp_path: Path) -> None:
    case = _risk_case()
    first = _simulate(case)
    second = kernel._seal_event(replace(first, event_id="", content_digest="", execution_datetime="2026-07-13 09:30:01"))
    event_store, legacy_store = _stores(tmp_path, case["ledger"])

    def apply(event):
        return kernel.apply_paper_event(
            event, event_store=event_store, legacy_store=legacy_store,
            expected_ledger_source_hash=case["snapshot"].source_hash,
        )["status"]

    with ThreadPoolExecutor(max_workers=2) as pool:
        statuses = list(pool.map(apply, (first, second)))
    assert sorted(statuses) == ["applied", "cas_mismatch"]
    assert len(event_store.read()["events"]) == 1


def test_apply_layer_blocks_same_day_sell_even_for_resealed_fill(tmp_path: Path) -> None:
    case = _risk_case()
    ledger = copy.deepcopy(case["ledger"])
    ledger["positions"][0]["entry_date"] = "2026-07-13"
    ledger["positions"][0]["last_buy_date"] = "2026-07-13"
    event_store, legacy_store = _stores(tmp_path, ledger)
    buy = _simulate(case)
    sell = replace(
        buy,
        event_id="",
        content_digest="",
        symbol="000001",
        side="sell",
        quantity=100,
        cash_delta=1000.0,
        position_delta=-100,
        sellable_delta=-100,
        frozen_delta=0,
        ledger_source_hash=kernel.compute_ledger_source_hash(ledger),
    )
    sell = kernel._seal_event(sell)
    result = kernel.apply_paper_event(
        sell,
        event_store=event_store,
        legacy_store=legacy_store,
        expected_ledger_source_hash=kernel.compute_ledger_source_hash(ledger),
    )
    assert result["status"] == "projection_blocked"
    assert "t_plus_one_blocked" in result["reason_codes"]


def test_persisted_event_row_tampering_is_detected_before_duplicate_noop(tmp_path: Path) -> None:
    case = _risk_case()
    event = _simulate(case)
    event_store, legacy_store = _stores(tmp_path, case["ledger"])
    assert kernel.apply_paper_event(
        event, event_store=event_store, legacy_store=legacy_store,
        expected_ledger_source_hash=case["snapshot"].source_hash,
    )["status"] == "applied"
    log = event_store.read()
    log["events"][0]["fill_price"] += 1.0
    event_store.write(log)
    result = kernel.apply_paper_event(
        event, event_store=event_store, legacy_store=legacy_store,
        expected_ledger_source_hash=case["snapshot"].source_hash,
    )
    assert result["status"] == "event_log_integrity_failed"

    # Derived paper-only labels are intentionally excluded from the business
    # digest, so the log validator must check their exact central values.
    log = event_store.read()
    log["events"][0]["fill_price"] = event.fill_price
    log["events"][0]["paper_only"] = False
    event_store.write(log)
    result = kernel.apply_paper_event(
        event, event_store=event_store, legacy_store=legacy_store,
        expected_ledger_source_hash=case["snapshot"].source_hash,
    )
    assert result["status"] == "event_log_integrity_failed"
    assert "event_log_boundary_mismatch" in result["reason_codes"]

    log = event_store.read()
    log["events"][0]["paper_only"] = True
    log["is_real_order"] = True
    event_store.write(log)
    result = kernel.apply_paper_event(
        event, event_store=event_store, legacy_store=legacy_store,
        expected_ledger_source_hash=case["snapshot"].source_hash,
    )
    assert result["status"] == "event_log_integrity_failed"
    assert "event_log_boundary_mismatch" in result["reason_codes"]
