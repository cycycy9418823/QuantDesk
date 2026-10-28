"""Stage 4 tri-account replay — acceptance-gate tests (§8 验收门).

Covers the six gates the main task book §8 mandates, plus the tiny end-to-end
fixture that proves the intent -> gate -> fill chain works when evidence is
eligible.  Every write goes to ``tmp_path``; the real paper ledger, real
``state/`` and real ``data/features/`` are never touched (the session tripwire
in ``conftest.py`` enforces this).

Boundary: Research Only / Paper Account Only / No Broker Route / not advice.
"""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from typing import Any

import pandas as pd
import pytest
from tests.minute_evidence_fixture import final_minute_fixture

import quantdesk.paper.stage4_tri_account_replay as stage4_replay
from quantdesk.intraday.minute_pool_capability import minute_pool_capability_source_hash
from quantdesk.paper.lifecycle import CandidateDecisionEvent
from quantdesk.paper.portfolio_contracts import (
    build_paper_order_intent,
    build_portfolio_snapshot,
    resolve_paper_capital_profile,
)
from quantdesk.paper.simulation_kernel import (
    ExecutionEvidence,
    FillEvent,
    RejectionEvent,
    SettlementPolicy,
    compute_execution_component_source_hash,
    compute_execution_evidence_digest,
    compute_minute_frame_source_hash,
    simulate_paper_intent,
)
from quantdesk.paper.stage4_replay_evidence import (
    build_execution_evidence,
    build_single_symbol_minute_pool,
    build_verified_daily_history,
    build_verified_trading_calendar,
)
from quantdesk.paper.stage4_tri_account_replay import (
    _fresh_account_ledger,
    replay_account,
)
from quantdesk.paper.trade_costs import buy_cash_outflow
from quantdesk.research.affordable_leaders_universe import build_affordable_leaders_universe
from quantdesk.risk.risk_gate import evaluate_paper_risk_gate
from quantdesk.risk.pre_trade_gate import PreTradeGateConfig
from quantdesk.risk.risk_gate_shadow import collect_local_risk_evidence
from quantdesk.trader_profile import (
    default_research_paper_profile,
    load_research_paper_profile,
    validate_research_paper_profile,
)
from quantdesk.version import API_SCHEMA_VERSION


PROJECT_ROOT = Path(__file__).resolve().parents[1]
PROFILE_DIR = PROJECT_ROOT / "config" / "experiment_profiles"


def test_stage4_buy_without_candidate_source_uses_symbol_daily_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class CollectCalled(RuntimeError):
        pass

    daily_dir = tmp_path / "daily"
    daily_dir.mkdir()
    daily_path = daily_dir / "600000.csv"
    daily_path.write_text(
        "date,symbol,close,amount,adjust\n2026-07-10,600000,10.0,1000000,raw\n",
        encoding="utf-8",
    )
    captured: dict[str, object] = {}

    def capture_collect(**kwargs: object):
        captured.update(kwargs)
        raise CollectCalled

    monkeypatch.setattr(stage4_replay, "collect_local_risk_evidence", capture_collect)
    monkeypatch.setattr(stage4_replay, "next_trade_day", lambda *_args, **_kwargs: "2026-07-13")
    monkeypatch.setattr(
        stage4_replay,
        "derive_candidate_evidence",
        lambda *_args, **_kwargs: {
            "eligible": True,
            "evidence_kind": "formal",
            "coverage": 1.0,
            "minimum_coverage": 1.0,
            "pit_eligible": True,
            "price_contract_eligible": True,
            "derivation": {"reason_codes": []},
        },
    )

    with pytest.raises(CollectCalled):
        stage4_replay._evaluate_candidate_buy(
            account_id="A",
            ledger=stage4_replay._fresh_account_ledger("A"),
            candidate={
                "symbol": "600000",
                "name": "fixture",
                "close": 10.0,
                "signal_date": "2026-07-10",
                "data_latest_date": "2026-07-10",
            },
            signal_date="2026-07-10",
            industry="Bank",
            window_pool={},
            st_delist=None,
            daily_dir=daily_dir,
            intraday_dir=tmp_path / "intraday",
            calendar_path=tmp_path / "calendar.csv",
            ledger_source_path="memory://stage4-replay/A",
        )

    assert captured["candidate_source_path"] == daily_path
    assert captured["price_source_path"] == daily_path
    assert captured["ledger_source_path"] == "memory://stage4-replay/A"


def test_stage4_buy_sizes_intent_to_profile_position_ceiling(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    daily_dir = tmp_path / "daily"
    daily_dir.mkdir()
    daily_path = daily_dir / "600000.csv"
    daily_path.write_text(
        "date,symbol,close,amount,adjust\n2026-07-10,600000,9.63,1000000,raw\n",
        encoding="utf-8",
    )
    captured: dict[str, Any] = {}

    monkeypatch.setattr(
        stage4_replay,
        "next_trade_day",
        lambda *_args, **_kwargs: "2026-07-13",
    )
    monkeypatch.setattr(
        stage4_replay,
        "derive_candidate_evidence",
        lambda *_args, **_kwargs: {
            "eligible": True,
            "evidence_kind": "formal",
            "coverage": 1.0,
            "minimum_coverage": 1.0,
            "pit_eligible": True,
            "price_contract_eligible": True,
            "derivation": {"reason_codes": []},
        },
    )
    monkeypatch.setattr(
        stage4_replay,
        "collect_local_risk_evidence",
        lambda **_kwargs: ({}, {}),
    )

    def capture_gate(intent: Any, _snapshot: Any, **kwargs: Any) -> Any:
        captured["intent"] = intent
        captured["pre_trade_config"] = kwargs.get("pre_trade_config")
        return type(
            "Decision",
            (),
            {
                "passed": False,
                "status": "blocked",
                "reason_codes": ("fixture_stop",),
                "decision_digest": "fixture",
            },
        )()

    monkeypatch.setattr(stage4_replay, "evaluate_paper_risk_gate", capture_gate)
    config = PreTradeGateConfig(
        max_position_pct=0.20,
        max_positions=5,
        max_total_exposure=0.80,
    )

    outcome = stage4_replay._evaluate_candidate_buy(
        account_id="A",
        ledger=stage4_replay._fresh_account_ledger("A", initial_capital=10_000.0),
        candidate={
            "symbol": "600000",
            "name": "fixture",
            "close": 9.63,
            "signal_date": "2026-07-10",
            "data_latest_date": "2026-07-10",
        },
        signal_date="2026-07-10",
        industry="Bank",
        window_pool={},
        st_delist=None,
        daily_dir=daily_dir,
        intraday_dir=tmp_path / "intraday",
        calendar_path=tmp_path / "calendar.csv",
        ledger_source_path="memory://stage4-replay/A",
        pre_trade_config=config,
    )

    intent = captured["intent"]
    assert outcome.stage == "gate"
    assert intent.quantity == 200
    assert intent.estimated_gross <= 2_000.0
    assert captured["pre_trade_config"] is config


def test_forward_replay_threads_validated_profile_ceilings_to_buy_gate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    signal_day = "2026-07-10"
    execution_day = "2026-07-13"
    symbol = "600000"
    captured: dict[str, Any] = {}
    profile = default_research_paper_profile(
        max_single_position_pct=0.15,
        max_paper_holdings=3,
        max_total_exposure_pct=0.60,
    )

    monkeypatch.setattr(
        stage4_replay,
        "long_signal_from_row",
        lambda *_args, **_kwargs: {
            "symbol": symbol,
            "name": "Fixture",
            "close": 10.0,
        },
    )

    def capture_buy(**kwargs: Any) -> stage4_replay.CandidateOutcome:
        captured.update(kwargs)
        return stage4_replay.CandidateOutcome(
            "A", symbol, signal_day, execution_day, "gate", "captured"
        )

    monkeypatch.setattr(stage4_replay, "_evaluate_candidate_buy", capture_buy)

    stage4_replay.replay_forward_account(
        account_id="A",
        strategy=stage4_replay.STRATEGY_LONG,
        symbols=[symbol],
        names={symbol: "Fixture"},
        universe_label="fixture",
        window_dates=[signal_day, execution_day],
        window_pool={},
        sector_snapshots={signal_day: _forward_sector_snapshot(symbol)},
        sector_snapshot_paths={signal_day: tmp_path / "snapshot.json"},
        sector_status_paths={signal_day: tmp_path / "status.json"},
        st_delist=None,
        minute_snapshot_path=tmp_path / "minute_audit.json",
        daily_dir=tmp_path / "daily",
        intraday_dir=tmp_path / "intraday",
        calendar_path=tmp_path / "calendar.csv",
        initial_capital=profile.paper_capital,
        research_profile=profile,
        frame_cache={symbol: _forward_replay_frame(dates=[signal_day, execution_day])},
    )

    config = captured["pre_trade_config"]
    assert config.max_position_pct == 0.15
    assert config.max_positions == 3
    assert config.max_total_exposure == 0.60


# --------------------------------------------------------------------------- #
# Gate 1: profile load + validation                                            #
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("account", ["A", "B", "C"])
def test_experiment_profiles_load_and_validate_as_user_10k(account: str) -> None:
    profile = load_research_paper_profile(PROFILE_DIR / f"exp_{account}_10k.json")
    validation = validate_research_paper_profile(profile)
    assert validation["ok"], validation["reason_codes"]
    assert profile.profile_id == "user_10k"
    assert profile.paper_capital == 10_000.0
    assert profile.network_enabled is False
    assert profile.llm_enabled is False
    assert profile.broker_route is None
    assert profile.research_only and profile.paper_only and profile.no_broker


def test_experiment_profiles_reject_broker_or_target_price(tmp_path: Path) -> None:
    bad = tmp_path / "exp_bad.json"
    bad.write_text(
        json.dumps(
            {
                "profile_id": "user_10k",
                "paper_capital": 10_000.0,
                "broker_route": "some_broker",
                "target_price": 12.5,
            }
        ),
        encoding="utf-8",
    )
    profile = load_research_paper_profile(bad)
    validation = validate_research_paper_profile(profile)
    assert validation["ok"] is False
    assert "broker_route_forbidden" in validation["reason_codes"]
    assert "target_price_forbidden" in validation["reason_codes"]


# --------------------------------------------------------------------------- #
# Gate 2: B-pool construction (affordability + per-industry proxy)             #
# --------------------------------------------------------------------------- #
def _write_daily_csv(daily_dir: Path, symbol: str, *, close: float, amount: float, rows: int = 30) -> None:
    daily_dir.mkdir(parents=True, exist_ok=True)
    dates = pd.bdate_range(end="2026-07-10", periods=rows)
    frame = pd.DataFrame(
        {
            "date": [d.date().isoformat() for d in dates],
            "symbol": symbol,
            "open": close,
            "high": close * 1.01,
            "low": close * 0.99,
            "close": close,
            "volume": 1_000_000,
            "amount": amount,
            "adjust": "raw",
        }
    )
    frame.to_csv(daily_dir / f"{symbol}.csv", index=False, encoding="utf-8")


def test_affordable_leaders_pool_filters_unaffordable_and_flags_proxy(tmp_path: Path) -> None:
    daily_dir = tmp_path / "daily"
    theme_map = tmp_path / "theme_map.csv"
    # Two cheap affordable names in the same industry, one expensive name.
    _write_daily_csv(daily_dir, "600000", close=9.0, amount=900_000_000)
    _write_daily_csv(daily_dir, "600001", close=12.0, amount=800_000_000)
    _write_daily_csv(daily_dir, "600002", close=400.0, amount=1_000_000_000)  # 1 lot = 40k > 30% of 10k
    theme_map.write_text(
        "symbol,industry\n600000,银行\n600001,银行\n600002,白酒\n",
        encoding="utf-8",
    )
    universe = build_affordable_leaders_universe(
        as_of="2026-07-10",
        daily_dir=daily_dir,
        theme_map_path=theme_map,
        security_master_path=tmp_path / "missing_master.csv",
        paper_capital=10_000.0,
        liquidity_top_n=10,
        max_lot_cost_pct=0.30,
        per_industry_k=1,
        symbols=["600000", "600001", "600002"],
    )
    picked = set(universe.symbols)
    # 600002 (400 yuan/share -> 40k a lot) must be filtered as unaffordable.
    assert "600002" not in picked
    # per_industry_k=1 keeps only the most liquid 银行 name (600000, higher amount).
    assert "600000" in picked
    assert "600001" not in picked
    manifest = universe.to_manifest()
    assert manifest["proxy_non_fundamental"] is True
    for row in manifest["rows"]:
        assert row["proxy_non_fundamental"] is True
        assert row["lot_cost_pct"] <= 0.30


# --------------------------------------------------------------------------- #
# Gate 3: window freeze — a (symbol, date) the window audit does not certify   #
#          yields a fail-closed no-fill (no minute pool).                      #
# --------------------------------------------------------------------------- #
def test_minute_pool_single_symbol_requires_window_coverage() -> None:
    covered_pool = {
        "snapshot_valid": True,
        "passed": True,
        "window_start": "2026-06-23",
        "window_end": "2026-07-03",
        "usable_by_symbol": {"000001": {"2026-06-25"}},
    }
    # Not covered on this date -> fail closed.
    pool, reasons = build_single_symbol_minute_pool(
        "000001", "2026-06-26", window_pool=covered_pool,
    )
    assert pool is None
    assert "minute_pool_not_covered" in reasons


# --------------------------------------------------------------------------- #
# Gate 4 + tiny fixture: cash conservation + intent -> gate -> FILL end-to-end #
# --------------------------------------------------------------------------- #
def _tiny_fill_world(tmp_path: Path) -> dict[str, Any]:
    """Build a tiny synthetic world where a buy legitimately fills.

    Two symbols share one sector so the risk-gate sector/returns/smart_stop
    components can assemble; a fresh 10k ledger holds one seed position so the
    smart_stop position loop has real daily rows.  All data is synthetic and
    lives only under ``tmp_path``.
    """

    as_of = "2026-07-10"
    exec_day = "2026-07-13"
    symbol = "600000"
    peer = "000001"
    daily_dir = tmp_path / "daily"
    intraday_dir = tmp_path / "intraday"
    daily_dir.mkdir(parents=True, exist_ok=True)
    (intraday_dir / "1m").mkdir(parents=True, exist_ok=True)

    dates = pd.bdate_range(end=as_of, periods=140)
    for sym, base in ((symbol, 10.0), (peer, 12.0)):
        frame = pd.DataFrame(
            {
                "date": [d.date().isoformat() for d in dates],
                "symbol": sym,
                "open": [round(base + i * 0.001, 4) for i in range(len(dates))],
                "high": [round(base + i * 0.001 + 0.05, 4) for i in range(len(dates))],
                "low": [round(base + i * 0.001 - 0.05, 4) for i in range(len(dates))],
                "close": [round(base + i * 0.001, 4) for i in range(len(dates))],
                "volume": 2_000_000,
                "amount": 60_000_000,
                "adjust": "raw",
            }
        )
        frame.to_csv(daily_dir / f"{sym}.csv", index=False, encoding="utf-8")

    minute = pd.DataFrame(
        [
            {"symbol": symbol, "datetime": f"{exec_day} 09:30:00", "open": 10.2, "high": 10.3, "low": 10.1, "close": 10.25, "volume": 1000, "amount": 10250, "adjust": "raw"},
            {"symbol": symbol, "datetime": f"{exec_day} 09:31:00", "open": 10.25, "high": 10.35, "low": 10.2, "close": 10.3, "volume": 1200, "amount": 12360, "adjust": "raw"},
        ]
    )
    final_minute_fixture(minute.rename(columns={"adjust": "price_basis"})).assign(adjust="raw").to_csv(
        intraday_dir / "1m" / f"{symbol}.csv", index=False, encoding="utf-8")

    return {
        "as_of": as_of,
        "exec_day": exec_day,
        "symbol": symbol,
        "peer": peer,
        "daily_dir": daily_dir,
        "intraday_dir": intraday_dir,
    }


def _eligible_risk_evidence(world: dict[str, Any], ledger: dict[str, Any]) -> dict[str, Any]:
    """Assemble the five gate components synthetically (all eligible)."""

    from quantdesk.risk.risk_gate import compute_risk_evidence_source_hash

    as_of = world["as_of"]
    symbol = world["symbol"]
    peer = world["peer"]
    dates = pd.bdate_range(end=as_of, periods=140)
    returns_frame = pd.DataFrame(
        {
            symbol: [((i % 9) - 4) / 1500 for i in range(len(dates))],
            peer: [((i % 7) - 3) / 1500 for i in range(len(dates))],
        },
        index=dates,
    )
    daily_rows = [
        {
            "date": d.date().isoformat(),
            "open": 11.9, "high": 12.2, "low": 11.8, "close": 12.0,
            "volume": 1_000_000, "amount": 12_000_000,
        }
        for d in pd.bdate_range(end=as_of, periods=30)
    ]
    components: dict[str, dict[str, Any]] = {
        "price": {
            "symbol": symbol, "value": 10.2, "price_basis": "raw",
            "consumer": "paper_execution", "data_cutoff": as_of,
            "source_path": f"memory://price/{symbol}",
        },
        "sector": {
            "symbol": symbol, "value": "bank",
            "by_symbol": {symbol: "bank", peer: "bank"},
            "data_cutoff": as_of, "source_path": "memory://sector",
        },
        "liquidity": {
            "symbol": symbol, "amount": 60_000_000.0, "liquidity_score": 60.0,
            "data_cutoff": as_of, "source_path": "memory://liquidity",
        },
        "returns": {
            "frame": returns_frame, "data_cutoff": as_of,
            "source_path": "memory://returns",
        },
        "smart_stop": {
            "daily_rows_by_symbol": {peer: daily_rows},
            "sector_status_by_symbol": {peer: {"status": "stable", "heat_score": 60.0}},
            "data_cutoff": as_of, "source_path": "memory://smart-stop",
        },
    }
    for component in components.values():
        component["source_hash"] = compute_risk_evidence_source_hash(component)
    return components


def _seed_ledger_with_peer(world: dict[str, Any]) -> dict[str, Any]:
    ledger = _fresh_account_ledger("fixture")
    ledger["positions"] = [
        {
            "symbol": world["peer"], "quantity": 100, "sellable_quantity": 100,
            "frozen_quantity": 0, "market_value": 1_200.0, "latest_price": 12.0,
            "avg_cost": 11.5, "entry_date": "2026-06-01", "last_buy_date": "2026-06-01",
            "price_basis": "raw", "sector": "bank", "funded": True,
        }
    ]
    ledger["account"]["cash"] = 8_800.0
    ledger["account"]["available_cash"] = 8_800.0
    ledger["cash"] = 8_800.0
    return ledger


def test_tiny_fixture_intent_gate_fill_end_to_end(tmp_path: Path) -> None:
    """The mandated stack must produce a real FILL when evidence is eligible.

    This isolates 'kernel fill mechanics work' from the real-data outcome
    (all-insufficient because local sector evidence is non-PIT).
    """

    world = _tiny_fill_world(tmp_path)
    ledger = _seed_ledger_with_peer(world)
    as_of = world["as_of"]
    symbol = world["symbol"]

    profile = resolve_paper_capital_profile(existing_state=ledger, existing_state_verified=True)
    snapshot = build_portfolio_snapshot(ledger, profile, as_of=as_of, valuation_price_basis="raw")
    assert snapshot.evidence_eligible is True

    event = CandidateDecisionEvent(
        event_id=f"fixture-{symbol}",
        symbol=symbol,
        occurred_at=f"{as_of}T15:00:00+08:00",
        requested_state="paper_watchlist",
        decision_origin="system",
        evidence={
            "eligible": True, "evidence_kind": "formal", "coverage": 1.0,
            "minimum_coverage": 1.0, "pit_eligible": True, "price_contract_eligible": True,
        },
        payload={"name": "fixture", "sector": "bank", "industry": "bank"},
    ).to_dict()
    intent = build_paper_order_intent(
        event, snapshot, reference_price=10.2, reference_price_basis="raw",
        requested_quantity=100,
    )
    assert intent.status == "pending_risk_review", intent.reason_codes

    evidence = _eligible_risk_evidence(world, ledger)
    decision = evaluate_paper_risk_gate(
        intent, snapshot, profile=profile, funded_ledger=ledger,
        evidence=evidence, as_of=as_of,
    )
    assert decision.passed, decision.reason_codes

    # Build a window pool that covers (symbol, exec_day), then real evidence.
    window_pool = {
        "snapshot_valid": True, "passed": True,
        "window_start": "2026-07-06", "window_end": "2026-07-17",
        "usable_by_symbol": {symbol: {world["exec_day"]}},
    }
    from quantdesk.data_truth.pit_st_delist_loader import StDelistData

    er = build_execution_evidence(
        symbol, as_of,
        window_pool=window_pool,
        st_delist=StDelistData(),
        daily_dir=world["daily_dir"],
        intraday_dir=world["intraday_dir"],
        calendar_path=PROJECT_ROOT / "data" / "calendar" / "a_share_trading_calendar.csv",
        snapshot_path=tmp_path / "window_audit.json",
    )
    assert er.ok, er.reason_codes
    event_out = simulate_paper_intent(
        intent, decision, snapshot, ledger, er.evidence, SettlementPolicy(),
    )
    assert isinstance(event_out, FillEvent), getattr(event_out, "reason_codes", None)
    # Cash conservation: cash_after == cash_before + cash_delta and the fee is
    # a single application of the cost model.
    assert event_out.fill_price == 10.2  # first-minute open (next-open contract)
    assert event_out.quantity == 100
    assert event_out.cash_after == pytest.approx(event_out.cash_before + event_out.cash_delta)
    expected_out = buy_cash_outflow(price=10.2, quantity=100)
    assert event_out.cash_delta == pytest.approx(-float(expected_out["cash_out"]))
    assert event_out.total_fee == pytest.approx(
        event_out.commission + event_out.stamp_duty + event_out.transfer_fee + event_out.slippage
    )
    assert event_out.frozen_delta == 100 and event_out.sellable_delta == 0


def test_tiny_fixture_determinism_same_inputs_same_fill(tmp_path: Path) -> None:
    """Same inputs must produce the same fill price / quantity / digests."""

    world = _tiny_fill_world(tmp_path)
    ledger = _seed_ledger_with_peer(world)
    as_of = world["as_of"]
    symbol = world["symbol"]
    profile = resolve_paper_capital_profile(existing_state=ledger, existing_state_verified=True)
    snapshot = build_portfolio_snapshot(ledger, profile, as_of=as_of, valuation_price_basis="raw")
    event = CandidateDecisionEvent(
        event_id=f"fixture-{symbol}", symbol=symbol,
        occurred_at=f"{as_of}T15:00:00+08:00", requested_state="paper_watchlist",
        decision_origin="system",
        evidence={
            "eligible": True, "evidence_kind": "formal", "coverage": 1.0,
            "minimum_coverage": 1.0, "pit_eligible": True, "price_contract_eligible": True,
        },
        payload={"name": "fixture", "sector": "bank", "industry": "bank"},
    ).to_dict()
    intent = build_paper_order_intent(
        event, snapshot, reference_price=10.2, reference_price_basis="raw",
        requested_quantity=100,
    )
    evidence = _eligible_risk_evidence(world, ledger)
    decision = evaluate_paper_risk_gate(
        intent, snapshot, profile=profile, funded_ledger=ledger,
        evidence=evidence, as_of=as_of,
    )
    from quantdesk.data_truth.pit_st_delist_loader import StDelistData

    window_pool = {
        "snapshot_valid": True, "passed": True,
        "window_start": "2026-07-06", "window_end": "2026-07-17",
        "usable_by_symbol": {symbol: {world["exec_day"]}},
    }
    calendar = PROJECT_ROOT / "data" / "calendar" / "a_share_trading_calendar.csv"
    fills = []
    for _ in range(2):
        er = build_execution_evidence(
            symbol, as_of, window_pool=window_pool, st_delist=StDelistData(),
            daily_dir=world["daily_dir"], intraday_dir=world["intraday_dir"],
            calendar_path=calendar, snapshot_path=tmp_path / "window_audit.json",
        )
        event_out = simulate_paper_intent(
            intent, decision, snapshot, ledger, er.evidence, SettlementPolicy(),
        )
        assert isinstance(event_out, FillEvent)
        fills.append(event_out)
    assert fills[0].fill_price == fills[1].fill_price
    assert fills[0].quantity == fills[1].quantity
    assert fills[0].content_digest == fills[1].content_digest
    assert fills[0].event_id == fills[1].event_id


def _forward_replay_frame(*, dates: list[str]) -> pd.DataFrame:
    """Minimal enriched frame for path-routing contract tests."""

    rows = len(dates)
    return pd.DataFrame(
        {
            "date": pd.to_datetime(dates),
            "open": [10.0] * rows,
            "high": [10.2] * rows,
            "low": [9.8] * rows,
            "close": [10.0] * rows,
            "volume": [1_000_000] * rows,
            "amount": [10_000_000] * rows,
            "amount_ma20": [10_000_000] * rows,
            "adjust": ["raw"] * rows,
        }
    )


def _forward_sector_snapshot(symbol: str) -> dict[str, Any]:
    return {
        "sector_membership": [
            {"symbol": symbol, "name": "Fixture", "sector": "Bank"}
        ]
    }


def test_forward_buy_uses_signal_day_derived_pit_status_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    signal_day = "2026-07-10"
    execution_day = "2026-07-13"
    symbol = "600000"
    raw_snapshot_path = tmp_path / "sector_history" / signal_day / "snapshot.json"
    derived_status_path = tmp_path / "pit_sector_status" / f"{signal_day}.json"
    captured: dict[str, Any] = {}

    monkeypatch.setattr(
        stage4_replay,
        "long_signal_from_row",
        lambda *_args, **_kwargs: {
            "symbol": symbol,
            "name": "Fixture",
            "close": 10.0,
        },
    )

    def capture_buy(**kwargs: Any) -> stage4_replay.CandidateOutcome:
        captured.update(kwargs)
        return stage4_replay.CandidateOutcome(
            "A", symbol, signal_day, execution_day, "gate", "captured"
        )

    monkeypatch.setattr(stage4_replay, "_evaluate_candidate_buy", capture_buy)

    stage4_replay.replay_forward_account(
        account_id="A",
        strategy=stage4_replay.STRATEGY_LONG,
        symbols=[symbol],
        names={symbol: "Fixture"},
        universe_label="fixture",
        window_dates=[signal_day, execution_day],
        window_pool={},
        sector_snapshots={signal_day: _forward_sector_snapshot(symbol)},
        sector_snapshot_paths={signal_day: raw_snapshot_path},
        sector_status_paths={signal_day: derived_status_path},
        st_delist=None,
        minute_snapshot_path=tmp_path / "minute_audit.json",
        daily_dir=tmp_path / "daily",
        intraday_dir=tmp_path / "intraday",
        calendar_path=tmp_path / "calendar.csv",
        frame_cache={symbol: _forward_replay_frame(dates=[signal_day, execution_day])},
    )

    assert captured["sector_rotation_path"] == derived_status_path
    assert captured["sector_rotation_path"] != raw_snapshot_path


def test_forward_account_reuses_caller_owned_source_cache_across_candidates(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dates = ["2026-07-09", "2026-07-10", "2026-07-13"]
    signal_days = dates[:-1]
    symbol = "600000"
    caller_cache: dict[str, dict[str, Any]] = {}
    lifecycle_cache: dict[str, Any] = {}
    observed_caches: list[object] = []
    observed_lifecycle_caches: list[object] = []

    monkeypatch.setattr(
        stage4_replay,
        "long_signal_from_row",
        lambda *_args, **_kwargs: {
            "symbol": symbol,
            "name": "Fixture",
            "close": 10.0,
        },
    )

    def capture_buy(**kwargs: Any) -> stage4_replay.CandidateOutcome:
        observed_caches.append(kwargs.get("source_cache"))
        observed_lifecycle_caches.append(kwargs.get("lifecycle_source_cache"))
        return stage4_replay.CandidateOutcome(
            "A",
            symbol,
            kwargs["signal_date"],
            "2026-07-13",
            "gate",
            "captured",
        )

    monkeypatch.setattr(stage4_replay, "_evaluate_candidate_buy", capture_buy)

    stage4_replay.replay_forward_account(
        account_id="A",
        strategy=stage4_replay.STRATEGY_LONG,
        symbols=[symbol],
        names={symbol: "Fixture"},
        universe_label="fixture",
        window_dates=dates,
        window_pool={},
        sector_snapshots={day: _forward_sector_snapshot(symbol) for day in signal_days},
        sector_snapshot_paths={day: tmp_path / day / "snapshot.json" for day in signal_days},
        sector_status_paths={day: tmp_path / day / "status.json" for day in signal_days},
        st_delist=None,
        minute_snapshot_path=tmp_path / "minute_audit.json",
        daily_dir=tmp_path / "daily",
        intraday_dir=tmp_path / "intraday",
        calendar_path=tmp_path / "calendar.csv",
        frame_cache={symbol: _forward_replay_frame(dates=dates)},
        source_cache=caller_cache,
        lifecycle_source_cache=lifecycle_cache,
    )

    assert observed_caches == [caller_cache, caller_cache]
    assert observed_lifecycle_caches == [lifecycle_cache, lifecycle_cache]


def test_real_replay_blocks_original_mixed_price_basis_before_deduplication(
    tmp_path: Path,
) -> None:
    daily_dir = tmp_path / "daily"
    daily_dir.mkdir()
    (daily_dir / "600000.csv").write_text(
        "date,symbol,open,high,low,close,volume,amount,adjust\n"
        "2026-07-10,600000,10,10.2,9.8,10,1000,1000000,qfq\n"
        "2026-07-10,600000,10,10.2,9.8,10,1000,1000000,raw\n"
        "2026-07-13,600000,10,10.2,9.8,10,1000,1000000,raw\n",
        encoding="utf-8",
    )
    frame = stage4_replay._read_daily_enriched("600000", daily_dir)

    result = stage4_replay.replay_forward_account(
        account_id="A",
        strategy=stage4_replay.STRATEGY_LONG,
        symbols=["600000"],
        names={"600000": "Fixture"},
        universe_label="fixture",
        window_dates=["2026-07-10", "2026-07-13"],
        window_pool={},
        sector_snapshots={},
        sector_snapshot_paths={},
        st_delist=None,
        minute_snapshot_path=tmp_path / "minute.json",
        daily_dir=daily_dir,
        intraday_dir=tmp_path / "intraday",
        calendar_path=tmp_path / "calendar.csv",
        frame_cache={"600000": frame},
        data_mode="real_canonical",
    )

    assert result.price_contract["eligible"] is False
    assert "price_basis" in " ".join(result.price_contract["reason_codes"])
    assert result.candidate_count == 0


def test_forward_sell_uses_signal_day_derived_pit_status_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    signal_day = "2026-07-10"
    execution_day = "2026-07-13"
    symbol = "600000"
    raw_snapshot_path = tmp_path / "sector_history" / signal_day / "snapshot.json"
    derived_status_path = tmp_path / "pit_sector_status" / f"{signal_day}.json"
    captured: dict[str, Any] = {}

    ledger = _fresh_account_ledger("A")
    ledger["positions"] = [
        {
            "symbol": symbol,
            "quantity": 100,
            "sellable_quantity": 100,
            "frozen_quantity": 0,
            "latest_price": 10.0,
            "market_value": 1_000.0,
            "avg_cost": 9.5,
            "entry_date": "2026-06-01",
            "last_buy_date": "2026-06-01",
            "price_basis": "raw",
            "sector": "Bank",
            "funded": True,
        }
    ]
    monkeypatch.setattr(stage4_replay, "_fresh_account_ledger", lambda *_args, **_kwargs: ledger)
    monkeypatch.setattr(stage4_replay, "evaluate_exit_signals", lambda *_args, **_kwargs: (True, "fixture_exit"))
    monkeypatch.setattr(stage4_replay, "long_signal_from_row", lambda *_args, **_kwargs: None)

    def capture_sell(**kwargs: Any) -> stage4_replay.CandidateOutcome:
        captured.update(kwargs)
        return stage4_replay.CandidateOutcome(
            "A", symbol, signal_day, execution_day, "exit_gate", "captured"
        )

    monkeypatch.setattr(stage4_replay, "_evaluate_position_sell", capture_sell)

    stage4_replay.replay_forward_account(
        account_id="A",
        strategy=stage4_replay.STRATEGY_LONG,
        symbols=[symbol],
        names={symbol: "Fixture"},
        universe_label="fixture",
        window_dates=[signal_day, execution_day],
        window_pool={},
        sector_snapshots={signal_day: _forward_sector_snapshot(symbol)},
        sector_snapshot_paths={signal_day: raw_snapshot_path},
        sector_status_paths={signal_day: derived_status_path},
        st_delist=None,
        minute_snapshot_path=tmp_path / "minute_audit.json",
        daily_dir=tmp_path / "daily",
        intraday_dir=tmp_path / "intraday",
        calendar_path=tmp_path / "calendar.csv",
        max_holding_days=1,
        frame_cache={symbol: _forward_replay_frame(dates=[signal_day, execution_day])},
    )

    assert captured["sector_rotation_path"] == derived_status_path
    assert captured["sector_rotation_path"] != raw_snapshot_path


def test_forward_missing_derived_pit_status_does_not_fallback_to_raw_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    signal_day = "2026-07-10"
    execution_day = "2026-07-13"
    symbol = "600000"
    raw_snapshot_path = tmp_path / "sector_history" / signal_day / "snapshot.json"
    captured: dict[str, Any] = {}

    monkeypatch.setattr(
        stage4_replay,
        "long_signal_from_row",
        lambda *_args, **_kwargs: {
            "symbol": symbol,
            "name": "Fixture",
            "close": 10.0,
        },
    )

    def capture_buy(**kwargs: Any) -> stage4_replay.CandidateOutcome:
        captured.update(kwargs)
        return stage4_replay.CandidateOutcome(
            "A", symbol, signal_day, execution_day, "gate", "captured"
        )

    monkeypatch.setattr(stage4_replay, "_evaluate_candidate_buy", capture_buy)

    stage4_replay.replay_forward_account(
        account_id="A",
        strategy=stage4_replay.STRATEGY_LONG,
        symbols=[symbol],
        names={symbol: "Fixture"},
        universe_label="fixture",
        window_dates=[signal_day, execution_day],
        window_pool={},
        sector_snapshots={signal_day: _forward_sector_snapshot(symbol)},
        sector_snapshot_paths={signal_day: raw_snapshot_path},
        sector_status_paths={},
        st_delist=None,
        minute_snapshot_path=tmp_path / "minute_audit.json",
        daily_dir=tmp_path / "daily",
        intraday_dir=tmp_path / "intraday",
        calendar_path=tmp_path / "calendar.csv",
        frame_cache={symbol: _forward_replay_frame(dates=[signal_day, execution_day])},
    )

    assert captured["sector_rotation_path"] is None
    assert captured["sector_rotation_path"] != raw_snapshot_path


# --------------------------------------------------------------------------- #
# Gate 5: C-account (and any account) with no minute coverage -> no fill       #
# --------------------------------------------------------------------------- #
def test_uncovered_execution_date_is_fail_closed_no_fill(tmp_path: Path) -> None:
    world = _tiny_fill_world(tmp_path)
    from quantdesk.data_truth.pit_st_delist_loader import StDelistData

    # Window pool that does NOT cover the resolved execution day.
    empty_pool = {
        "snapshot_valid": True, "passed": False,
        "window_start": "2026-07-06", "window_end": "2026-07-17",
        "usable_by_symbol": {},
    }
    er = build_execution_evidence(
        world["symbol"], world["as_of"],
        window_pool=empty_pool, st_delist=StDelistData(),
        daily_dir=world["daily_dir"], intraday_dir=world["intraday_dir"],
        calendar_path=PROJECT_ROOT / "data" / "calendar" / "a_share_trading_calendar.csv",
        snapshot_path=tmp_path / "window_audit.json",
    )
    assert er.ok is False
    assert "minute_pool_not_covered" in er.reason_codes
    assert er.evidence is None


# --------------------------------------------------------------------------- #
# Gate 6: real-data replay honestly yields zero fills (all insufficient)       #
#          — the documented PIT-sector wall, recorded not asserted.            #
# --------------------------------------------------------------------------- #
def test_verified_daily_history_binds_previous_trade_date_to_signal(tmp_path: Path) -> None:
    """previous_trade_date must equal the signal day (next-open contract)."""

    world = _tiny_fill_world(tmp_path)
    daily, reasons = build_verified_daily_history(
        world["symbol"], world["as_of"], world["exec_day"], daily_dir=world["daily_dir"],
    )
    assert daily is not None, reasons
    assert daily["previous_trade_date"] == world["as_of"]
    assert daily["price_basis"] == "raw"
    assert daily["proxy_thresholds_used"] is False
    # tampering with the block must break the source hash binding.
    tampered = dict(daily)
    tampered["previous_close"] = 999.0
    assert tampered["source_hash"] != compute_execution_component_source_hash(tampered)
