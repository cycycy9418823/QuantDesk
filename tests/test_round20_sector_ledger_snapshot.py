from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

import quantdesk.paper.position_management_engine as position
from quantdesk.risk.risk_gate_shadow import _ledger_core


def _state() -> dict[str, Any]:
    return {
        "schema_version": "3.7.0.6",
        "initial_cash": 20_000.0,
        "cash": 20_000.0,
        "positions": [
            {
                "symbol": "600000",
                "strategy_type": "long",
                "quantity": 100,
                "sellable_quantity": 0,
                "frozen_quantity": 100,
                "entry_date": "2026-08-11",
                "last_buy_date": "2026-08-11",
                "entry_price": 10.0,
                "avg_cost": 10.0,
                "latest_price": 10.0,
                "market_value": 1_000.0,
                "sector": "bank",
            }
        ],
        "closed_positions": [],
        "applied_execution_ids": [],
    }


def _buy_event(symbol: str, price: float) -> dict[str, Any]:
    candidate = {
        "symbol": symbol,
        "name": symbol,
        "strategy_type": "long",
        "data_latest_date": "2026-08-11",
        "sector": "utilities",
    }
    return {
        "symbol": symbol,
        "name": symbol,
        "strategy_type": "long",
        "trade_date": "2026-08-12",
        "action": "buy",
        "trigger_price": price,
        "quantity": 100,
        "is_reliable": True,
        "candidate_envelope": {
            "candidate": candidate,
            "batch_path": "candidate.json",
        },
    }


def _shadow_pass(**kwargs: Any) -> dict[str, Any]:
    return {
        "evaluated": True,
        "status": "would_pass",
        "would_pass": True,
        "would_block": False,
        "insufficient_evidence": False,
        "reason_codes": [],
        "missing_components": [],
        "symbol": kwargs["candidate"]["symbol"],
        "side": "buy",
    }


def _write_execution(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def test_t1_normalized_shadow_state_uses_exact_immutable_ledger_snapshot(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    state = _state()
    ledger_path = tmp_path / "state" / "auto_paper_portfolio.json"
    ledger_path.parent.mkdir(parents=True)
    ledger_path.write_text(json.dumps(state), encoding="utf-8")
    captured: list[dict[str, Any]] = []

    monkeypatch.setattr(position, "verify_candidate_envelope", lambda _value: [])
    monkeypatch.setattr(
        position,
        "evaluate_shadow_risk_order",
        lambda **kwargs: captured.append(kwargs) or _shadow_pass(**kwargs),
    )

    execution = {
        "plan_id": "plan_20260811_for_20260812",
        "trade_date": "2026-08-12",
        "events": [_buy_event("600611", 5.15)],
        "no_fill": [],
        "blocked": [],
    }
    execution_path = tmp_path / "results" / "execution.json"
    _write_execution(execution_path, execution)
    position.apply_conditional_execution_to_state(
        state,
        execution,
        risk_gate_shadow_enabled=True,
        risk_gate_shadow_records=[],
        risk_gate_shadow_write_errors=False,
        ledger_source_path=ledger_path,
        execution_source_path=execution_path,
        risk_gate_shadow_ledger_snapshot_dir=tmp_path / "results" / "ledger_evidence",
    )

    assert len(captured) == 1
    call = captured[0]
    snapshot_path = Path(call["ledger_source_path"])
    assert snapshot_path.is_file()
    snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
    assert _ledger_core(snapshot) == _ledger_core(call["state"])
    assert snapshot["positions"][0]["sellable_quantity"] == 100
    provenance = snapshot["risk_gate_shadow_ledger_snapshot"]
    assert provenance["event_ordinal"] == 1
    assert provenance["symbol"] == "600611"
    assert provenance["base_ledger_source"]["path"] == str(ledger_path)
    assert provenance["base_ledger_source"]["file_sha256"].startswith("sha256:")
    assert json.loads(ledger_path.read_text(encoding="utf-8"))["positions"][0]["sellable_quantity"] == 0


def test_each_sequential_buy_binds_its_exact_pretrade_ledger(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    state = _state()
    ledger_path = tmp_path / "state" / "auto_paper_portfolio.json"
    ledger_path.parent.mkdir(parents=True)
    ledger_path.write_text(json.dumps(state), encoding="utf-8")
    captured: list[dict[str, Any]] = []

    monkeypatch.setattr(position, "verify_candidate_envelope", lambda _value: [])
    monkeypatch.setattr(
        position,
        "evaluate_shadow_risk_order",
        lambda **kwargs: captured.append(kwargs) or _shadow_pass(**kwargs),
    )

    execution = {
        "plan_id": "plan_20260811_for_20260812",
        "trade_date": "2026-08-12",
        "events": [_buy_event("600611", 5.15), _buy_event("600272", 16.57)],
        "no_fill": [],
        "blocked": [],
    }
    execution_path = tmp_path / "results" / "execution.json"
    _write_execution(execution_path, execution)
    position.apply_conditional_execution_to_state(
        state,
        execution,
        risk_gate_shadow_enabled=True,
        risk_gate_shadow_records=[],
        risk_gate_shadow_write_errors=False,
        ledger_source_path=ledger_path,
        execution_source_path=execution_path,
        risk_gate_shadow_ledger_snapshot_dir=tmp_path / "results" / "ledger_evidence",
    )

    assert len(captured) == 2
    first_path = Path(captured[0]["ledger_source_path"])
    second_path = Path(captured[1]["ledger_source_path"])
    assert first_path != second_path
    first = json.loads(first_path.read_text(encoding="utf-8"))
    second = json.loads(second_path.read_text(encoding="utf-8"))
    assert _ledger_core(first) == _ledger_core(captured[0]["state"])
    assert _ledger_core(second) == _ledger_core(captured[1]["state"])
    assert all(row["symbol"] != "600611" for row in first["positions"])
    assert any(row["symbol"] == "600611" for row in second["positions"])
    assert second["cash"] < first["cash"]
    assert len(list((tmp_path / "results" / "ledger_evidence").glob("*.json"))) == 2


def test_snapshot_source_mismatch_fails_shadow_closed_without_blocking_paper_apply(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    state = _state()
    ledger_path = tmp_path / "state" / "auto_paper_portfolio.json"
    ledger_path.parent.mkdir(parents=True)
    tampered = _state()
    tampered["cash"] += 1.0
    ledger_path.write_text(json.dumps(tampered), encoding="utf-8")
    execution = {
        "plan_id": "plan_20260811_for_20260812",
        "trade_date": "2026-08-12",
        "events": [_buy_event("600611", 5.15)],
        "no_fill": [],
        "blocked": [],
    }
    execution_path = tmp_path / "results" / "execution.json"
    _write_execution(execution_path, execution)
    records: list[dict[str, Any]] = []

    monkeypatch.setattr(position, "verify_candidate_envelope", lambda _value: [])
    monkeypatch.setattr(
        position,
        "evaluate_shadow_risk_order",
        lambda **_kwargs: pytest.fail("source mismatch reached evaluator"),
    )

    result = position.apply_conditional_execution_to_state(
        state,
        execution,
        risk_gate_shadow_enabled=True,
        risk_gate_shadow_records=records,
        risk_gate_shadow_write_errors=False,
        ledger_source_path=ledger_path,
        execution_source_path=execution_path,
        risk_gate_shadow_ledger_snapshot_dir=tmp_path / "results" / "ledger_evidence",
    )

    assert result["actions"][0]["action"] == "buy"
    assert result["state"]["cash"] < state["cash"]
    assert len(records) == 1
    assert records[0]["status"] == "shadow_error"
    assert "risk_gate_shadow_base_ledger_source_mismatch" in records[0]["shadow_error"]
    assert not list((tmp_path / "results" / "ledger_evidence").glob("*.json"))


def test_execution_source_mismatch_fails_shadow_closed(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    state = _state()
    ledger_path = tmp_path / "state" / "auto_paper_portfolio.json"
    ledger_path.parent.mkdir(parents=True)
    ledger_path.write_text(json.dumps(state), encoding="utf-8")
    execution = {
        "plan_id": "plan_20260811_for_20260812",
        "trade_date": "2026-08-12",
        "events": [_buy_event("600611", 5.15)],
        "no_fill": [],
        "blocked": [],
    }
    execution_path = tmp_path / "results" / "execution.json"
    _write_execution(execution_path, {**execution, "events": []})
    records: list[dict[str, Any]] = []

    monkeypatch.setattr(position, "verify_candidate_envelope", lambda _value: [])
    monkeypatch.setattr(
        position,
        "evaluate_shadow_risk_order",
        lambda **_kwargs: pytest.fail("execution source mismatch reached evaluator"),
    )

    result = position.apply_conditional_execution_to_state(
        state,
        execution,
        risk_gate_shadow_enabled=True,
        risk_gate_shadow_records=records,
        risk_gate_shadow_write_errors=False,
        ledger_source_path=ledger_path,
        execution_source_path=execution_path,
        risk_gate_shadow_ledger_snapshot_dir=tmp_path / "results" / "ledger_evidence",
    )

    assert result["actions"][0]["action"] == "buy"
    assert records[0]["status"] == "shadow_error"
    assert "risk_gate_shadow_execution_source_mismatch" in records[0]["shadow_error"]
