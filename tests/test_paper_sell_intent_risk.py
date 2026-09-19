from __future__ import annotations

import copy
import hashlib
import importlib
from dataclasses import replace
from typing import Any

import pandas as pd
import pytest

from quantdesk.intraday.minute_pool_capability import minute_pool_capability_source_hash
from quantdesk.paper.portfolio_contracts import (
    build_paper_exit_intent,
    build_portfolio_snapshot,
    compute_paper_order_intent_digest,
    resolve_paper_capital_profile,
)
from quantdesk.paper.trade_costs import sell_cash_inflow
from quantdesk.risk.risk_gate import compute_risk_evidence_source_hash, evaluate_paper_risk_gate
from quantdesk.version import API_SCHEMA_VERSION


kernel = importlib.import_module("quantdesk.paper.simulation_kernel")


def _ledger(*, sellable: int = 200, last_buy_date: str = "2026-06-01") -> dict[str, Any]:
    return {
        "ledger_id": "sell-intent-ledger",
        "initial_cash": 10_000.0,
        "cash": 7_000.0,
        "positions": [
            {
                "symbol": "600000",
                "quantity": 300,
                "sellable_quantity": sellable,
                "frozen_quantity": 300 - sellable,
                "market_value": 3_000.0,
                "latest_price": 10.0,
                "avg_cost": 9.0,
                "entry_date": "2026-06-01",
                "last_buy_date": last_buy_date,
                "price_basis": "raw",
                "sector": "bank",
                "funded": True,
            },
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
            },
        ],
    }


def _returns(as_of: str) -> pd.DataFrame:
    index = pd.bdate_range(end=as_of, periods=120)
    return pd.DataFrame(
        {
            "600000": [((idx % 7) - 3) / 1200 for idx in range(120)],
            "000001": [((idx % 9) - 4) / 1000 for idx in range(120)],
        },
        index=index,
    )


def _daily_rows(as_of: str) -> list[dict[str, Any]]:
    return [
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


def _case(*, sellable: int = 200, last_buy_date: str = "2026-06-01", quantity: int = 100) -> dict[str, Any]:
    as_of = "2026-07-10"
    ledger = _ledger(sellable=sellable, last_buy_date=last_buy_date)
    profile = resolve_paper_capital_profile()
    snapshot = build_portfolio_snapshot(ledger, profile, as_of=as_of, valuation_price_basis="raw")
    intent = build_paper_exit_intent(
        ledger,
        snapshot,
        symbol="600000",
        reference_price=10.0,
        reference_price_basis="raw",
        requested_quantity=quantity,
    )
    evidence = {
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
            "by_symbol": {"600000": "bank", "000001": "bank"},
            "data_cutoff": as_of,
            "source_path": "memory://risk/sector",
        },
        "liquidity": {
            "symbol": "600000",
            "amount": 50_000_000.0,
            "liquidity_score": 80.0,
            "data_cutoff": as_of,
            "source_path": "memory://risk/liquidity",
        },
        "returns": {
            "frame": _returns(as_of),
            "data_cutoff": as_of,
            "source_path": "memory://risk/returns",
        },
        "smart_stop": {
            "daily_rows_by_symbol": {"600000": _daily_rows(as_of), "000001": _daily_rows(as_of)},
            "sector_status_by_symbol": {
                "600000": {"status": "stable", "heat_score": 60.0},
                "000001": {"status": "stable", "heat_score": 60.0},
            },
            "data_cutoff": as_of,
            "source_path": "memory://risk/smart-stop",
        },
    }
    for component in evidence.values():
        component["source_hash"] = compute_risk_evidence_source_hash(component)
    return {
        "as_of": as_of,
        "ledger": ledger,
        "profile": profile,
        "snapshot": snapshot,
        "intent": intent,
        "evidence": evidence,
    }


def _minute_frame(day: str = "2026-07-13") -> pd.DataFrame:
    return pd.DataFrame(
        [
            {"symbol": "600000", "datetime": f"{day} 09:30:00", "open": 10.2, "high": 10.3, "low": 10.1, "close": 10.25, "volume": 1000, "amount": 10250, "adjust": "raw"},
            {"symbol": "600000", "datetime": f"{day} 09:31:00", "open": 10.25, "high": 10.35, "low": 10.2, "close": 10.3, "volume": 1200, "amount": 12360, "adjust": "raw"},
        ]
    )


def _execution(case: dict[str, Any], *, day: str = "2026-07-13"):
    frame = _minute_frame(day)
    pool = {
        "schema_version": API_SCHEMA_VERSION,
        "task_type": "verified_minute_pool_snapshot",
        "status": "passed",
        "passed": True,
        "quality_status": "passed",
        "target_date": day,
        "symbols": ["600000"],
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
        "symbol": "600000",
        "price_basis": "raw",
        "previous_close": 10.0,
        "previous_trade_date": "2026-07-10",
        "source_path": "memory://daily/600000",
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
    metadata = {
        "schema_version": API_SCHEMA_VERSION,
        "status": "passed",
        "verified": True,
        "evidence_kind": "verified_market_status",
        "symbol": "600000",
        "is_st": False,
        "suspended": False,
        "source_path": f"memory://market-status/600000/{day}",
    }
    metadata["source_hash"] = kernel.compute_execution_component_source_hash(metadata)
    evidence = kernel.ExecutionEvidence(
        symbol="600000",
        execution_date=day,
        price_basis="raw",
        minute_frame=frame,
        minute_source_path=f"memory://minute/600000/{day}",
        minute_source_hash=kernel.compute_minute_frame_source_hash(frame),
        minute_pool=pool,
        daily_history=daily,
        trading_calendar=calendar,
        market_metadata=metadata,
    )
    return replace(evidence, evidence_digest=kernel.compute_execution_evidence_digest(evidence))


def _risk(case: dict[str, Any]):
    return evaluate_paper_risk_gate(
        case["intent"],
        case["snapshot"],
        profile=case["profile"],
        funded_ledger=case["ledger"],
        evidence=case["evidence"],
        as_of=case["as_of"],
    )


def test_portfolio_contract_exposes_formal_sell_intent_builder() -> None:
    assert callable(build_paper_exit_intent)


def test_build_paper_exit_intent_binds_funded_position_and_sell_cash_inflow() -> None:
    case = _case()
    intent = case["intent"]
    expected = sell_cash_inflow(price=10.0, quantity=100, avg_cost=9.0)

    assert intent.side == "sell"
    assert intent.status == "pending_risk_review"
    assert intent.quantity == 100
    assert intent.available_cash_before == 7000.0
    assert intent.available_cash_after == pytest.approx(7000.0 + expected["cash_in"])
    assert intent.estimated_gross == pytest.approx(expected["gross"])
    assert intent.estimated_fee == pytest.approx(expected["total_fee"])
    assert intent.estimated_cash_out == pytest.approx(-expected["cash_in"])
    assert intent.coverage["kind"] == "funded_position_exit"
    assert intent.pit_status["status"] == "not_applicable_funded_position_exit"
    assert intent.intent_digest == compute_paper_order_intent_digest(intent)


def test_build_paper_exit_intent_blocks_unknown_oversell_and_non_raw_basis() -> None:
    missing = build_paper_exit_intent(
        _ledger(),
        build_portfolio_snapshot(_ledger(), resolve_paper_capital_profile(), as_of="2026-07-10", valuation_price_basis="raw"),
        symbol="000999",
        reference_price=10.0,
        reference_price_basis="raw",
        requested_quantity=100,
    )
    oversell = _case(sellable=100, quantity=200)["intent"]
    non_raw = build_paper_exit_intent(
        _ledger(),
        build_portfolio_snapshot(_ledger(), resolve_paper_capital_profile(), as_of="2026-07-10", valuation_price_basis="raw"),
        symbol="600000",
        reference_price=10.0,
        reference_price_basis="qfq",
        requested_quantity=100,
    )

    assert missing.status == "blocked"
    assert "funded_position_missing" in missing.reason_codes
    assert oversell.status == "blocked"
    assert "sellable_quantity_insufficient" in oversell.reason_codes
    assert non_raw.status == "blocked"
    assert "execution_price_basis_not_raw" in non_raw.reason_codes


def test_sell_intent_passes_risk_and_executes_through_public_kernel_api() -> None:
    case = _case()
    risk = _risk(case)
    event = kernel.simulate_paper_intent(
        case["intent"],
        risk,
        case["snapshot"],
        case["ledger"],
        _execution(case),
        kernel.SettlementPolicy(),
    )

    assert risk.status == "passed", risk.reason_codes
    assert isinstance(event, kernel.FillEvent)
    assert event.side == "sell"
    assert event.cash_delta > 0
    assert event.position_delta == -100
    assert event.sellable_delta == -100
    assert event.stamp_duty > 0
    assert event.cash_after == pytest.approx(7000.0 + event.cash_delta)


def test_sell_intent_risk_still_vetoes_low_liquidity_but_not_reduced_exposure() -> None:
    case = _case(quantity=200)
    passed = _risk(case)
    low_liquidity = copy.deepcopy(case)
    low_liquidity["evidence"]["liquidity"]["amount"] = 1.0
    low_liquidity["evidence"]["liquidity"]["liquidity_score"] = 1.0
    low_liquidity["evidence"]["liquidity"]["source_hash"] = compute_risk_evidence_source_hash(
        low_liquidity["evidence"]["liquidity"]
    )
    veto = _risk(low_liquidity)

    assert passed.status == "passed", passed.reason_codes
    assert "concentration_veto" not in passed.reason_codes
    assert veto.status == "vetoed"
    assert "liquidity_veto" in veto.reason_codes


def test_same_day_sell_is_blocked_by_kernel_t_plus_one() -> None:
    case = _case(last_buy_date="2026-07-13")
    risk = _risk(case)
    event = kernel.simulate_paper_intent(
        case["intent"],
        risk,
        case["snapshot"],
        case["ledger"],
        _execution(case, day="2026-07-13"),
        kernel.SettlementPolicy(),
    )

    assert risk.status == "passed", risk.reason_codes
    assert isinstance(event, kernel.RejectionEvent)
    assert "t_plus_one_blocked" in event.reason_codes
