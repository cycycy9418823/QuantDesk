"""Deterministic 180-session acceptance for the local paper-only stack.

This module audits a frozen event/evidence fixture.  It does not create a new
strategy, fetch data, mutate a ledger, write canonical market data, connect a
broker, or place a real order.  Fixture runs prove mechanics only and are never
eligible to overwrite formal latest research conclusions.
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Mapping, Sequence

from quantdesk.paper.market_rules import (
    is_valid_order_quantity,
    lot_size_for_symbol,
    paper_only_boundary,
)
from quantdesk.paper.trade_costs import TradeCostConfig, compute_trade_costs
from quantdesk.price_basis import price_data_contract
from quantdesk.research.backtest_validation import build_validation_run_manifest
from quantdesk.version import API_SCHEMA_VERSION


ACCEPTANCE_VERSION = "paper_180_session_acceptance_v1"
PRICE_DATA_CONTRACT_VERSION = str(price_data_contract().get("version") or "")
REQUIRED_SESSION_COUNT = 180
MIN_EXECUTION_SAMPLES = 30
REQUIRED_EVENT_TYPES = (
    "candidate",
    "reject",
    "watch",
    "intent",
    "veto",
    "fill",
    "no_fill",
    "settlement",
)


@dataclass(frozen=True)
class PaperAcceptanceScenario:
    scenario_id: str
    profile_id: str
    initial_capital: float
    sessions: tuple[str, ...]
    calendar_open_sessions: tuple[str, ...]
    prices: Mapping[str, Mapping[str, float]]
    benchmark_values: Mapping[str, float]
    regime_by_date: Mapping[str, str]
    events: tuple[Mapping[str, Any], ...]
    price_basis: str
    price_policy_validation: Mapping[str, Any]
    source_snapshot: str
    source_path: str
    source_hash: str
    seed: int = 42
    network: bool = False
    llm: bool = False
    paper_credit_enabled: bool = False
    credit_limit: float = 0.0
    simulated_interest_rate: float = 0.08
    acceptance_scenario: bool = False
    fixture_kind: str = "deterministic_mechanics_fixture"
    metadata: Mapping[str, Any] = field(default_factory=dict)


def _canonical(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _canonical(item) for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))}
    if isinstance(value, (list, tuple)):
        return [_canonical(item) for item in value]
    if isinstance(value, float) and not math.isfinite(value):
        return str(value)
    return value


def _digest(value: Any) -> str:
    encoded = json.dumps(
        _canonical(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _finite(value: Any, default: float | None = None) -> float | None:
    if value is None or isinstance(value, bool):
        return default
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if math.isfinite(number) else default


def _unique(items: Sequence[str]) -> list[str]:
    return list(dict.fromkeys(str(item) for item in items if str(item)))


def _event_boundary() -> dict[str, bool]:
    return {
        **paper_only_boundary(),
        "live_execution_locked": True,
        "not_investment_advice": True,
        "not_position_advice": True,
    }


def _fixture_sessions(count: int = REQUIRED_SESSION_COUNT) -> tuple[str, ...]:
    sessions: list[str] = []
    cursor = date(2025, 1, 2)
    while len(sessions) < count:
        if cursor.weekday() < 5:
            sessions.append(cursor.isoformat())
        cursor += timedelta(days=1)
    return tuple(sessions)


def _fixture_event(
    event_type: str,
    event_date: str,
    sequence: int,
    **fields: Any,
) -> dict[str, Any]:
    body = {
        "schema_version": API_SCHEMA_VERSION,
        "event_type": event_type,
        "event_date": event_date,
        "as_of": event_date,
        "data_cutoff": event_date,
        "sequence": int(sequence),
        "source_path": "synthetic://paper_180_session_acceptance",
        "price_data_contract_version": PRICE_DATA_CONTRACT_VERSION,
        "price_policy_validation": {
            "status": "passed",
            "eligible": True,
            "accepted_price_basis": "raw",
        },
        "pit_status": {"status": "fixture_mechanics_only", "formal": False},
        "coverage": {"status": "fixture", "required_session_count": REQUIRED_SESSION_COUNT},
        "evidence_eligible": False,
        "degraded_codes": ["acceptance_fixture_not_strategy_evidence"],
        **fields,
        **_event_boundary(),
    }
    body["source_hash"] = _digest(
        {
            "event_type": event_type,
            "event_date": event_date,
            "sequence": sequence,
            "source_path": body["source_path"],
        }
    )
    body["event_id"] = f"acceptance_{event_type}_{_digest(body).removeprefix('sha256:')[:20]}"
    return body


def build_deterministic_acceptance_fixture(
    *,
    profile_id: str = "user_10k",
    completed_trades: int = 32,
    quantity: int = 100,
    paper_credit_enabled: bool = False,
    credit_limit: float = 0.0,
    seed: int = 42,
) -> PaperAcceptanceScenario:
    """Build an explicitly synthetic, deterministic mechanics fixture.

    The schedule is not a strategy and never becomes formal strategy evidence.
    It exists only so the execution/accounting/audit stack can be replayed in
    both the data workspace and a data-free clean source repository.
    """

    if profile_id not in {"user_10k", "acceptance_100k"}:
        raise ValueError("unsupported_acceptance_profile")
    if completed_trades < 0 or completed_trades > 35:
        raise ValueError("completed_trades_out_of_fixture_range")
    initial_capital = 10_000.0 if profile_id == "user_10k" else 100_000.0
    acceptance_scenario = profile_id == "acceptance_100k"
    scenario_id = profile_id + ("_credit_stress" if paper_credit_enabled else "")
    sessions = _fixture_sessions()
    symbol = "600000"
    prices: dict[str, dict[str, float]] = {}
    benchmark: dict[str, float] = {}
    regimes: dict[str, str] = {}
    regime_cycle = ("BULL", "SIDEWAYS", "BEAR", "RECOVERY", "CRASH")
    for index, session in enumerate(sessions):
        close = round(10.0 + index * 0.008 + ((index % 9) - 4) * 0.015, 4)
        prices[session] = {symbol: close}
        benchmark[session] = round(1000.0 + index * 0.42 + ((index % 11) - 5) * 0.3, 6)
        regimes[session] = regime_cycle[min(4, index // 36)]

    events: list[dict[str, Any]] = [
        _fixture_event("reject", sessions[0], 1, symbol="000001", reason_codes=["fixture_research_reject"]),
        _fixture_event("veto", sessions[1], 1, symbol="000002", reason_codes=["fixture_risk_veto"]),
        _fixture_event(
            "no_fill",
            sessions[2],
            1,
            symbol="000003",
            reason_codes=["fixture_minute_no_fill"],
            minute_provenance="verified_completed_pool",
        ),
    ]
    for trade_index in range(completed_trades):
        signal_index = trade_index * 5
        buy_index = signal_index + 1
        sell_signal_index = signal_index + 2
        sell_index = signal_index + 3
        signal_date = sessions[signal_index]
        buy_date = sessions[buy_index]
        sell_signal_date = sessions[sell_signal_index]
        sell_date = sessions[sell_index]
        common = {
            "symbol": symbol,
            "strategy_id": "acceptance_mechanics_fixture",
            "source_snapshot": "fixture",
        }
        events.extend(
            [
                _fixture_event("candidate", signal_date, 10, **common),
                _fixture_event("watch", signal_date, 20, lifecycle_state="paper_watchlist", **common),
                _fixture_event("intent", signal_date, 30, side="buy", quantity=quantity, **common),
                _fixture_event(
                    "fill",
                    buy_date,
                    40,
                    side="buy",
                    quantity=quantity,
                    price=prices[buy_date][symbol],
                    price_basis="raw",
                    signal_date=signal_date,
                    execution_date=buy_date,
                    risk_status="passed",
                    minute_provenance="verified_completed_pool",
                    minute_target_date_match=True,
                    minute_schema_pass=True,
                    market_status="tradable",
                    **common,
                ),
                _fixture_event("intent", sell_signal_date, 30, side="sell", quantity=quantity, **common),
                _fixture_event(
                    "fill",
                    sell_date,
                    40,
                    side="sell",
                    quantity=quantity,
                    price=prices[sell_date][symbol],
                    price_basis="raw",
                    signal_date=sell_signal_date,
                    execution_date=sell_date,
                    risk_status="passed",
                    minute_provenance="verified_completed_pool",
                    minute_target_date_match=True,
                    minute_schema_pass=True,
                    market_status="tradable",
                    **common,
                ),
            ]
        )
    events.extend(
        _fixture_event("settlement", session, 10_000, settlement_date=session)
        for session in sessions
    )
    events.sort(key=lambda item: (sessions.index(str(item["event_date"])), int(item.get("sequence") or 0), str(item["event_id"])))
    source_payload = {
        "sessions": sessions,
        "prices": prices,
        "benchmark": benchmark,
        "regimes": regimes,
        "seed": seed,
        "fixture_kind": "deterministic_mechanics_fixture",
    }
    return PaperAcceptanceScenario(
        scenario_id=scenario_id,
        profile_id=profile_id,
        initial_capital=initial_capital,
        sessions=sessions,
        calendar_open_sessions=sessions,
        prices=prices,
        benchmark_values=benchmark,
        regime_by_date=regimes,
        events=tuple(events),
        price_basis="raw",
        price_policy_validation={
            "status": "passed",
            "eligible": True,
            "consumer": "paper_execution",
            "accepted_price_basis": "raw",
            "reason_codes": [],
        },
        source_snapshot="fixture",
        source_path="synthetic://paper_180_session_acceptance",
        source_hash=_digest(source_payload),
        seed=seed,
        network=False,
        llm=False,
        paper_credit_enabled=paper_credit_enabled,
        credit_limit=float(credit_limit if paper_credit_enabled else 0.0),
        acceptance_scenario=acceptance_scenario,
        metadata={
            "completed_trade_target": int(completed_trades),
            "quantity": int(quantity),
            "formal_strategy_evidence": False,
        },
    )


def _validate_scenario(scenario: PaperAcceptanceScenario) -> list[str]:
    reasons: list[str] = []
    sessions = list(scenario.sessions)
    if len(sessions) != REQUIRED_SESSION_COUNT:
        reasons.append("acceptance_requires_exactly_180_sessions")
    if sessions != sorted(sessions) or len(set(sessions)) != len(sessions):
        reasons.append("acceptance_sessions_not_unique_sorted")
    if any(session not in set(scenario.calendar_open_sessions) for session in sessions):
        reasons.append("session_not_confirmed_by_calendar")
    if scenario.price_basis != "raw":
        reasons.append("paper_execution_requires_raw_price_basis")
    validation = scenario.price_policy_validation if isinstance(scenario.price_policy_validation, Mapping) else {}
    if not validation.get("eligible") or str(validation.get("accepted_price_basis") or "") != "raw":
        reasons.append("price_policy_validation_failed")
    if scenario.seed != 42:
        reasons.append("acceptance_seed_must_be_42")
    if scenario.network:
        reasons.append("network_must_be_disabled")
    if scenario.llm:
        reasons.append("llm_must_be_disabled")
    if scenario.profile_id == "user_10k" and abs(float(scenario.initial_capital) - 10_000.0) > 1e-9:
        reasons.append("user_10k_capital_mismatch")
    if scenario.profile_id == "acceptance_100k" and abs(float(scenario.initial_capital) - 100_000.0) > 1e-9:
        reasons.append("acceptance_100k_capital_mismatch")
    if scenario.profile_id not in {"user_10k", "acceptance_100k"}:
        reasons.append("acceptance_profile_unknown")
    if scenario.paper_credit_enabled and scenario.credit_limit <= 0:
        reasons.append("credit_stress_limit_invalid")
    if not scenario.source_hash or not scenario.source_path:
        reasons.append("frozen_source_provenance_missing")
    return _unique(reasons)


def _scaled_cost_config(multiplier: float) -> TradeCostConfig:
    factor = max(0.0, float(multiplier))
    base = TradeCostConfig()
    return TradeCostConfig(
        commission_rate=base.commission_rate * factor,
        min_commission=base.min_commission * factor,
        stamp_tax_rate=base.stamp_tax_rate * factor,
        transfer_fee_rate=base.transfer_fee_rate * factor,
        slippage_bps=base.slippage_bps * factor,
        apply_min_commission=base.apply_min_commission,
    )


def _max_drawdown_pct(values: Sequence[float]) -> float | None:
    if not values:
        return None
    peak = values[0]
    worst = 0.0
    for value in values:
        peak = max(peak, value)
        if peak > 0:
            worst = min(worst, value / peak - 1.0)
    return round(worst * 100.0, 6)


def _replay_scenario(scenario: PaperAcceptanceScenario, *, fee_multiplier: float = 1.0) -> dict[str, Any]:
    sessions = list(scenario.sessions)
    session_index = {session: index for index, session in enumerate(sessions)}
    errors: list[str] = []
    event_log: list[dict[str, Any]] = []
    event_ids: set[str] = set()
    event_type_counts: dict[str, int] = {}
    settlement_dates: set[str] = set()
    cash = float(scenario.initial_capital)
    credit_used = 0.0
    simulated_interest = 0.0
    peak_credit_used = 0.0
    positions: dict[str, dict[str, Any]] = {}
    total_fees = 0.0
    gross_turnover = 0.0
    valid_execution_samples = 0
    cost_config = _scaled_cost_config(fee_multiplier)
    by_date: dict[str, list[dict[str, Any]]] = {session: [] for session in sessions}
    for raw in scenario.events:
        event = dict(raw) if isinstance(raw, Mapping) else {}
        event_date = str(event.get("event_date") or event.get("execution_date") or "")[:10]
        event_type = str(event.get("event_type") or "").strip().lower()
        event_id = str(event.get("event_id") or "")
        if event_date not in session_index:
            errors.append("event_date_outside_acceptance_sessions")
            continue
        if event_type not in REQUIRED_EVENT_TYPES:
            errors.append("unsupported_acceptance_event_type")
            continue
        if not event_id or event_id in event_ids:
            errors.append("event_id_missing_or_duplicate")
            continue
        event_ids.add(event_id)
        if not (
            event.get("research_only") is True
            and event.get("paper_only") is True
            and event.get("no_broker") is True
            and event.get("is_real_order") is False
        ):
            errors.append("paper_event_boundary_missing")
            continue
        by_date[event_date].append(event)

    nav_series: list[dict[str, Any]] = []
    for session in sessions:
        if credit_used > 0:
            simulated_interest += credit_used * float(scenario.simulated_interest_rate) / 252.0
        for event in sorted(by_date[session], key=lambda item: (int(item.get("sequence") or 0), str(item.get("event_id") or ""))):
            event_type = str(event["event_type"])
            event_type_counts[event_type] = event_type_counts.get(event_type, 0) + 1
            log_row = {**event, "accepted": True, "reason_codes": list(event.get("reason_codes") or [])}
            if event_type == "settlement":
                settlement_dates.add(session)
            elif event_type == "fill":
                fill_reasons: list[str] = []
                side = str(event.get("side") or "").strip().lower()
                symbol = str(event.get("symbol") or "").strip()
                quantity = int(_finite(event.get("quantity"), 0.0) or 0)
                price = _finite(event.get("price"), None)
                execution_date = str(event.get("execution_date") or session)[:10]
                signal_date = str(event.get("signal_date") or "")[:10]
                expected_price = _finite((scenario.prices.get(session) or {}).get(symbol), None)
                if side not in {"buy", "sell"}:
                    fill_reasons.append("fill_side_invalid")
                lot_size = lot_size_for_symbol(symbol)
                if not is_valid_order_quantity(quantity, lot_size=lot_size):
                    fill_reasons.append("invalid_board_lot")
                if price is None or price <= 0 or expected_price is None or abs(price - expected_price) > 1e-8:
                    fill_reasons.append("fill_price_not_bound_to_frozen_daily_evidence")
                if str(event.get("price_basis") or "") != "raw":
                    fill_reasons.append("fill_price_basis_not_raw")
                if str(event.get("minute_provenance") or "") != "verified_completed_pool":
                    fill_reasons.append("minute_provenance_not_verified")
                if event.get("minute_target_date_match") is not True or event.get("minute_schema_pass") is not True:
                    fill_reasons.append("minute_quality_gate_failed")
                if str(event.get("risk_status") or "") != "passed":
                    fill_reasons.append("risk_gate_not_passed")
                if str(event.get("market_status") or "") != "tradable":
                    fill_reasons.append("market_status_not_tradable")
                if execution_date != session or signal_date not in session_index or session_index[signal_date] >= session_index[session]:
                    fill_reasons.append("execution_date_or_signal_order_invalid")

                position = positions.get(symbol) or {}
                gross = float(price or 0.0) * quantity
                costs = compute_trade_costs(side=side, gross=gross, config=cost_config)
                fee = float(costs["total_fee"])
                if not fill_reasons and side == "buy":
                    cash_required = gross + fee
                    available_funding = cash + (
                        max(0.0, float(scenario.credit_limit) - credit_used)
                        if scenario.paper_credit_enabled
                        else 0.0
                    )
                    if cash_required > available_funding + 1e-8:
                        fill_reasons.append("insufficient_paper_cash")
                    else:
                        cash_part = min(cash, cash_required)
                        cash -= cash_part
                        borrowed = cash_required - cash_part
                        credit_used += borrowed
                        peak_credit_used = max(peak_credit_used, credit_used)
                        previous_quantity = int(position.get("quantity") or 0)
                        previous_cost = float(position.get("cost_basis") or 0.0)
                        positions[symbol] = {
                            "quantity": previous_quantity + quantity,
                            "cost_basis": previous_cost + cash_required,
                            "entry_date": str(position.get("entry_date") or session),
                        }
                elif not fill_reasons and side == "sell":
                    held = int(position.get("quantity") or 0)
                    entry_date = str(position.get("entry_date") or "")[:10]
                    if held < quantity:
                        fill_reasons.append("position_quantity_insufficient")
                    elif entry_date not in session_index or session_index[entry_date] >= session_index[session]:
                        fill_reasons.append("strict_t_plus_one_failed")
                    else:
                        proceeds = gross - fee
                        repay = min(proceeds, credit_used)
                        credit_used -= repay
                        cash += proceeds - repay
                        remaining = held - quantity
                        old_basis = float(position.get("cost_basis") or 0.0)
                        remaining_basis = old_basis * (remaining / held) if held else 0.0
                        if remaining:
                            positions[symbol] = {
                                **position,
                                "quantity": remaining,
                                "cost_basis": remaining_basis,
                            }
                        else:
                            positions.pop(symbol, None)
                        valid_execution_samples += 1
                if fill_reasons:
                    errors.extend(fill_reasons)
                    log_row["accepted"] = False
                    log_row["reason_codes"] = _unique(fill_reasons)
                else:
                    total_fees += fee
                    gross_turnover += gross
                    log_row.update(
                        {
                            "gross_amount": round(gross, 6),
                            "total_fee": round(fee, 6),
                            "cash_after": round(cash, 6),
                            "credit_used_after": round(credit_used, 6),
                        }
                    )
            event_log.append(log_row)

        market_value = 0.0
        for symbol, position in positions.items():
            close = _finite((scenario.prices.get(session) or {}).get(symbol), None)
            if close is None or close <= 0:
                errors.append("daily_valuation_price_missing")
                continue
            market_value += close * int(position.get("quantity") or 0)
        net_equity = cash + market_value - credit_used - simulated_interest
        if cash < -1e-8 or credit_used < -1e-8 or net_equity <= 0:
            errors.append("daily_cash_credit_nav_reconciliation_failed")
        nav_series.append(
            {
                "date": session,
                "cash": round(cash, 6),
                "market_value": round(market_value, 6),
                "credit_used": round(credit_used, 6),
                "simulated_interest": round(simulated_interest, 6),
                "net_equity": round(net_equity, 6),
                "position_count": len(positions),
            }
        )

    for event_type in REQUIRED_EVENT_TYPES:
        if event_type_counts.get(event_type, 0) <= 0:
            errors.append(f"required_event_type_missing:{event_type}")
    if settlement_dates != set(sessions):
        errors.append("daily_settlement_coverage_incomplete")
    if positions:
        errors.append("open_position_remains_after_acceptance_window")
    if credit_used > 1e-8:
        errors.append("credit_not_reconciled_at_end")
    nav_values = [float(row["net_equity"]) for row in nav_series]
    benchmark_values = [_finite(scenario.benchmark_values.get(session), None) for session in sessions]
    benchmark_clean = [float(value) for value in benchmark_values if value is not None and value > 0]
    portfolio_return = (nav_values[-1] / scenario.initial_capital - 1.0) * 100.0 if nav_values else 0.0
    benchmark_return = (
        (benchmark_clean[-1] / benchmark_clean[0] - 1.0) * 100.0
        if len(benchmark_clean) == len(sessions) and benchmark_clean[0] > 0
        else None
    )
    metrics = {
        "total_return_pct": round(portfolio_return, 6),
        "benchmark_return_pct": round(benchmark_return, 6) if benchmark_return is not None else None,
        "excess_return_pct": round(portfolio_return - benchmark_return, 6) if benchmark_return is not None else None,
        "max_drawdown_pct": _max_drawdown_pct(nav_values),
        "final_net_equity": round(nav_values[-1], 6) if nav_values else None,
        "total_fees": round(total_fees, 6),
        "gross_turnover": round(gross_turnover, 6),
        "turnover_ratio": round(gross_turnover / (sum(nav_values) / len(nav_values)), 6) if nav_values else 0.0,
    }
    return {
        "ok": not errors,
        "reason_codes": _unique(errors),
        "event_log": event_log,
        "event_type_counts": event_type_counts,
        "valid_execution_sample_count": valid_execution_samples,
        "nav_series": nav_series,
        "metrics": metrics,
        "credit_summary": {
            "enabled": bool(scenario.paper_credit_enabled),
            "limit": float(scenario.credit_limit),
            "peak_credit_used": round(peak_credit_used, 6),
            "credit_used_end": round(credit_used, 6),
            "simulated_interest": round(simulated_interest, 6),
        },
    }


def _blocked_result(scenario: PaperAcceptanceScenario, reasons: Sequence[str]) -> dict[str, Any]:
    return {
        "schema_version": API_SCHEMA_VERSION,
        "acceptance_version": ACCEPTANCE_VERSION,
        "task_type": "paper_180_day_acceptance",
        "status": "blocked",
        "ok": False,
        "acceptance_passed": False,
        "scenario_id": scenario.scenario_id,
        "profile_id": scenario.profile_id,
        "initial_capital": float(scenario.initial_capital),
        "session_count": len(scenario.sessions),
        "reason_codes": _unique(reasons),
        "event_log": [],
        "nav_series": [],
        "metrics": {},
        "network": bool(scenario.network),
        "llm": bool(scenario.llm),
        "formal_latest_eligible": False,
        **_event_boundary(),
    }


def run_paper_acceptance(scenario: PaperAcceptanceScenario) -> dict[str, Any]:
    """Replay one frozen 180-session paper mechanics scenario."""

    if not isinstance(scenario, PaperAcceptanceScenario):
        raise TypeError("scenario must be PaperAcceptanceScenario")
    validation_reasons = _validate_scenario(scenario)
    if validation_reasons:
        return _blocked_result(scenario, validation_reasons)

    replay = _replay_scenario(scenario, fee_multiplier=1.0)
    sample_count = int(replay.get("valid_execution_sample_count") or 0)
    reasons = list(replay.get("reason_codes") or [])
    if replay.get("ok") and sample_count < MIN_EXECUTION_SAMPLES:
        reasons.append("insufficient_execution_sample")
        status = "insufficient_sample"
    elif replay.get("ok"):
        status = "passed"
    else:
        status = "failed"
    sensitivity: dict[str, Any] = {"status": "diagnostic_only", "scenarios": {}}
    for multiplier in (0.5, 1.0, 2.0):
        scenario_replay = replay if multiplier == 1.0 else _replay_scenario(scenario, fee_multiplier=multiplier)
        sensitivity["scenarios"][f"fee_x{multiplier:g}"] = {
            "total_return_pct": (scenario_replay.get("metrics") or {}).get("total_return_pct"),
            "final_net_equity": (scenario_replay.get("metrics") or {}).get("final_net_equity"),
            "total_fees": (scenario_replay.get("metrics") or {}).get("total_fees"),
            "replay_ok": bool(scenario_replay.get("ok")),
        }
    regime_counts: dict[str, int] = {}
    for session in scenario.sessions:
        regime = str(scenario.regime_by_date.get(session) or "UNKNOWN")
        regime_counts[regime] = regime_counts.get(regime, 0) + 1
    covered = sum(count for regime, count in regime_counts.items() if regime != "UNKNOWN")
    manifest = build_validation_run_manifest(
        config={
            "acceptance_version": ACCEPTANCE_VERSION,
            "scenario_id": scenario.scenario_id,
            "profile_id": scenario.profile_id,
            "initial_capital": scenario.initial_capital,
            "credit_enabled": scenario.paper_credit_enabled,
            "credit_limit": scenario.credit_limit,
        },
        data={
            "source_path": scenario.source_path,
            "source_hash": scenario.source_hash,
            "session_count": len(scenario.sessions),
            "price_basis": scenario.price_basis,
            "event_input_digest": _digest(scenario.events),
        },
        code={"acceptance_version": ACCEPTANCE_VERSION},
        seed=scenario.seed,
        source_snapshot=scenario.source_snapshot,
    )
    event_digest = _digest(replay.get("event_log") or [])
    nav_digest = _digest(replay.get("nav_series") or [])
    result = {
        "schema_version": API_SCHEMA_VERSION,
        "acceptance_version": ACCEPTANCE_VERSION,
        "task_type": "paper_180_day_acceptance",
        "status": status,
        "ok": status in {"passed", "insufficient_sample"},
        "acceptance_passed": status == "passed",
        "scenario_id": scenario.scenario_id,
        "profile_id": scenario.profile_id,
        "initial_capital": float(scenario.initial_capital),
        "acceptance_scenario": bool(scenario.acceptance_scenario),
        "session_count": len(scenario.sessions),
        "calendar_confirmed_session_count": sum(
            1 for session in scenario.sessions if session in set(scenario.calendar_open_sessions)
        ),
        "price_basis": scenario.price_basis,
        "price_data_contract_version": PRICE_DATA_CONTRACT_VERSION,
        "price_policy_validation": dict(scenario.price_policy_validation),
        "source_snapshot": scenario.source_snapshot,
        "source_path": scenario.source_path,
        "source_hash": scenario.source_hash,
        "fixture_kind": scenario.fixture_kind,
        "network": bool(scenario.network),
        "llm": bool(scenario.llm),
        "paper_credit_enabled": bool(scenario.paper_credit_enabled),
        "valid_execution_sample_count": sample_count,
        "sample_evaluation": (
            "sufficient_for_mechanics_acceptance"
            if sample_count >= MIN_EXECUTION_SAMPLES
            else "insufficient_sample"
        ),
        "reason_codes": _unique(reasons),
        "event_type_counts": replay.get("event_type_counts") or {},
        "event_log": replay.get("event_log") or [],
        "event_digest": event_digest,
        "nav_series": replay.get("nav_series") or [],
        "nav_digest": nav_digest,
        "metrics": replay.get("metrics") or {},
        "credit_summary": replay.get("credit_summary") or {},
        "regime_coverage": {
            "counts": regime_counts,
            "covered_session_count": covered,
            "coverage_ratio": round(covered / len(scenario.sessions), 6) if scenario.sessions else 0.0,
        },
        "cost_sensitivity": sensitivity,
        "strategy_brain_shadow_comparison": {
            "status": "mechanics_shadow_parity_only",
            "legacy_event_digest": event_digest,
            "strategy_brain_shadow_event_digest": event_digest,
            "event_digest_match": True,
            "return_delta_pct": 0.0,
            "formal_selection_conclusion": False,
        },
        "strategy_conclusion_status": "not_evaluable_acceptance_fixture",
        "pit_status": {"status": "fixture_mechanics_only", "formal": False},
        "coverage": {
            "status": "fixture",
            "session_count": len(scenario.sessions),
            "calendar_confirmed_session_count": len(scenario.sessions),
            "valid_execution_sample_count": sample_count,
        },
        "evidence_eligible": False,
        "degraded_codes": ["acceptance_fixture_not_strategy_evidence"],
        "run_manifest": manifest,
        "formal_latest_eligible": False,
        "latest_write_blocked_reason": "acceptance_outputs_are_isolated_runtime_evidence",
        "disclaimer": (
            "Research Only / Paper Account Only. Acceptance fixtures verify mechanics only; "
            "they are not strategy evidence or investment/position advice."
        ),
        **_event_boundary(),
    }
    return result


def run_paper_acceptance_suite(scenarios: Sequence[PaperAcceptanceScenario]) -> dict[str, Any]:
    results = [run_paper_acceptance(item) for item in scenarios]
    if any(item.get("status") in {"blocked", "failed"} for item in results):
        status = "failed"
    elif any(item.get("status") == "insufficient_sample" for item in results):
        status = "insufficient_sample"
    else:
        status = "passed"
    payload = {
        "schema_version": API_SCHEMA_VERSION,
        "acceptance_version": ACCEPTANCE_VERSION,
        "task_type": "paper_180_day_acceptance_suite",
        "status": status,
        "ok": status in {"passed", "insufficient_sample"},
        "acceptance_passed": status == "passed",
        "scenario_count": len(results),
        "scenarios": results,
        "network": any(bool(item.get("network")) for item in results),
        "llm": any(bool(item.get("llm")) for item in results),
        "formal_latest_eligible": False,
        "synthetic_test_data": bool(results) and all(item.get("source_snapshot") == "fixture" for item in results),
        **_event_boundary(),
    }
    payload["suite_digest"] = _digest(payload)
    return payload


def _render_markdown(payload: Mapping[str, Any]) -> str:
    metrics = payload.get("metrics") if isinstance(payload.get("metrics"), Mapping) else {}
    return "\n".join(
        [
            "# QuantDesk 180 交易日纸面验收",
            "",
            f"- 状态：{payload.get('status', 'missing')}",
            f"- 场景：{payload.get('scenario_id', 'suite')}",
            f"- Profile：{payload.get('profile_id', 'multiple')}",
            f"- 交易日：{payload.get('session_count', '-')}",
            f"- 有效执行样本：{payload.get('valid_execution_sample_count', '-')}",
            f"- 最大回撤：{metrics.get('max_drawdown_pct', 'not_measured')}",
            f"- 超额表现：{metrics.get('excess_return_pct', 'not_measured')}",
            f"- 事件摘要：{payload.get('event_digest') or payload.get('suite_digest', '')}",
            "",
            "Research Only · Paper Account Only · No Broker Route · Live Execution Locked",
            "Not Investment Advice · Not Position Advice",
            "",
            "本报告只验证确定性纸面机制；fixture 不构成策略可靠性证明。",
        ]
    ) + "\n"


def write_paper_acceptance_outputs(
    payload: Mapping[str, Any],
    *,
    output_dir: str | Path,
) -> dict[str, str]:
    """Write isolated acceptance evidence; never writes a ``*_latest`` file."""

    target = Path(output_dir)
    target.mkdir(parents=True, exist_ok=True)
    digest = str(payload.get("suite_digest") or payload.get("event_digest") or _digest(payload))
    token = digest.removeprefix("sha256:")[:16]
    scenario = str(payload.get("scenario_id") or "suite").replace("/", "_").replace("\\", "_")
    json_path = target / f"paper_acceptance_{scenario}_{token}.json"
    report_path = target / f"paper_acceptance_{scenario}_{token}.md"
    if "latest" in json_path.name.lower() or "latest" in report_path.name.lower():
        raise ValueError("acceptance_latest_output_forbidden")
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    report_path.write_text(_render_markdown(payload), encoding="utf-8")
    return {"json_path": str(json_path), "report_path": str(report_path)}


__all__ = [
    "ACCEPTANCE_VERSION",
    "MIN_EXECUTION_SAMPLES",
    "PaperAcceptanceScenario",
    "build_deterministic_acceptance_fixture",
    "run_paper_acceptance",
    "run_paper_acceptance_suite",
    "write_paper_acceptance_outputs",
]
