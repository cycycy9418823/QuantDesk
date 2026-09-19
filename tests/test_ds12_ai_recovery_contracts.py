"""Adjudicate the authorized recovery contract, allowing explicit safe refusal."""
from __future__ import annotations

import json
import re
from copy import deepcopy
from pathlib import Path

import pytest

from ai_trader import overnight, paths as ai_paths
from ai_trader.execution_evidence import verify_execution_receipt

import _ai_recovery_fixtures as fx

PLAN_ID = "plan_20260910_for_20260911"


@pytest.fixture(autouse=True)
def _isolate_default_runtime_dirs(tmp_path: Path, monkeypatch) -> None:
    """Keep ensure_runtime_dirs off the real repo tree during recovery runs."""

    monkeypatch.setattr(ai_paths, "STATE_DIR", tmp_path / "state" / "ai_trader")
    monkeypatch.setattr(ai_paths, "RESULTS_DIR", tmp_path / "results" / "ai_trader")

RECOVERY_SIGNAL = re.compile(r"recover|unverified|inconsistent|mismatch|conflict|incomplete|missing.effect|effect.missing|待恢复|不一致|证据缺", re.I)


def money_signature(ledger: dict) -> dict:
    return {
        "cash": round(float(ledger["cash"]), 8),
        "positions": sorted((str(p.get("symbol")), p.get("quantity"), p.get("avg_cost")) for p in ledger.get("positions", [])),
        "trades": sorted((str(t.get("plan_id")), str(t.get("symbol")), str(t.get("side")), str(t.get("date")),
                          t.get("quantity"), t.get("price"), t.get("fee"), t.get("cash_delta")) for t in ledger.get("trades", [])),
    }


def signals(*payloads) -> list[str]:
    collected: list[str] = []
    keys = {"status", "recovery_status", "recovery_note", "reason", "error", "errors", "warnings", "warnings_top", "message", "failed_steps"}

    def visit(value, include_strings=False):
        if isinstance(value, dict):
            for key, item in value.items():
                if key in keys:
                    visit(item, True)
        elif isinstance(value, (list, tuple)):
            for item in value:
                visit(item, include_strings)
        elif isinstance(value, str) and include_strings and RECOVERY_SIGNAL.search(value):
            collected.append(value)

    for value in payloads:
        visit(value)
    return collected


def produce(root: Path, scenario: str = "two_filled") -> dict:
    daily = root / "daily"
    fx.write_daily(daily, "600500", prev_close=5.00, open_px=5.02)
    fx.write_daily(daily, "600501", prev_close=7.95, open_px=8.50 if scenario == "legal_partial" else 8.00)
    intents = [fx.buy_intent("600500", quantity=2500, ref_close=5.00)]
    if scenario in {"two_filled", "legal_partial"}:
        intents.append(fx.buy_intent("600501", quantity=1000, ref_close=7.95))
    if scenario == "zero":
        intents = []
    pending = fx.plan(plan_id=PLAN_ID, intents=intents)
    empty = fx.empty_ledger(cash=100_000.0)
    archive = root / "execution_evidence"
    settled, after, actions = overnight.settle_plan(
        deepcopy(pending), empty, as_of=fx.TRADE_DATE, daily_dir=daily, evidence_dir=archive,
    )
    expected = {"zero": 0, "single": 1, "two_filled": 2, "legal_partial": 1}[scenario]
    assert sum(a.get("status") == "filled" for a in actions) == expected, actions
    assert len(fx.trades_for_plan(after, PLAN_ID)) == expected, after
    if scenario == "two_filled":
        assert after["cash"] == 79429.5195
    elif expected == 1:
        assert after["cash"] == 87438.5995
    return {"pending": pending, "settled": settled, "after": after, "empty": empty,
            "actions": actions, "daily": daily, "archive": archive}


def invoke(entry: str, root: Path, plan: dict, ledger: dict, daily: Path, monkeypatch) -> dict:
    before = money_signature(ledger)
    supplied = deepcopy(ledger)
    result_plan, result_ledger, actions, snapshot = {}, supplied, [], {}
    error = None
    runtime = None
    if entry == "evening":
        project = fx.seed_root(root, plan_obj=plan, ledger=ledger, symbols={
            "600500": {"prev_close": 5.00, "open": 5.02},
            "600501": {"prev_close": 7.95, "open": 8.00},
        })
        runtime = fx.cycle_paths(project)
        fx.stub_evening(monkeypatch, overnight, overnight.settle_plan, symbols=["600500", "600501"])
    try:
        if runtime is None:
            result_plan, result_ledger, actions = overnight.settle_plan(
                deepcopy(plan), supplied, as_of=fx.TRADE_DATE, daily_dir=daily, evidence_dir=root / "recovery_evidence",
            )
        else:
            snapshot = overnight.run_evening(paths=runtime, network_news=False)
    except Exception as exc:
        error = {"type": type(exc).__name__, "message": str(exc)}
    if runtime is not None:
        result_ledger = json.loads(runtime.ledger.read_text(encoding="utf-8"))
        result_plan = json.loads(runtime.plan_latest.read_text(encoding="utf-8"))
    return {
        "entry": entry, "error": error, "before": before, "after": money_signature(result_ledger),
        "input_after": money_signature(supplied), "signals": signals(result_plan, result_ledger, actions, snapshot),
        "result_plan": result_plan, "ledger": result_ledger, "actions": actions, "snapshot": snapshot,
    }


def assert_reconciled_or_refused(observation: dict, expected_complete: dict, path: Path) -> None:
    observation["expected_complete"] = money_signature(expected_complete)
    path.write_text(json.dumps(observation, ensure_ascii=False, indent=2), encoding="utf-8")
    if observation["error"]:
        assert observation["error"]["type"] in {"RuntimeError", "ValueError"}, observation
        assert RECOVERY_SIGNAL.search(observation["error"]["message"]), observation
        assert observation["after"] == observation["before"], observation
        return
    if observation["signals"]:
        assert observation["after"] == observation["before"], observation
        return
    assert observation["after"] == observation["expected_complete"], observation


@pytest.mark.parametrize("scenario", ["zero", "single", "two_filled", "legal_partial"])
def test_complete_results_reenter_without_duplicate_money(tmp_path: Path, scenario: str) -> None:
    real = produce(tmp_path / "original", scenario)
    expected = money_signature(real["after"])
    current = deepcopy(real["after"])
    for _ in range(3):
        _, current, actions = overnight.settle_plan(
            deepcopy(real["settled"]), current, as_of=fx.TRADE_DATE,
            daily_dir=real["daily"], evidence_dir=tmp_path / "reentry_evidence",
        )
        assert actions == []
        assert money_signature(current) == expected
    (tmp_path / "positive_observation.json").write_text(json.dumps({
        "scenario": scenario, "intents": len(real["pending"]["intents"]),
        "original_actions": real["actions"], "expected_money": expected, "actual_money": money_signature(current),
    }, ensure_ascii=False, indent=2), encoding="utf-8")


def test_complete_two_fill_reverse_interruption_is_idempotent(tmp_path: Path) -> None:
    real = produce(tmp_path / "original")
    recovered_plan, after, actions = overnight.settle_plan(
        deepcopy(real["pending"]), deepcopy(real["after"]), as_of=fx.TRADE_DATE,
        daily_dir=real["daily"], evidence_dir=tmp_path / "reverse_evidence",
    )
    assert recovered_plan["status"] == "settled" and actions == []
    assert money_signature(after) == money_signature(real["after"])


@pytest.mark.parametrize("entry", ["settle", "evening"])
def test_two_frozen_fills_with_one_ledger_effect_are_not_silently_complete(tmp_path: Path, monkeypatch, entry: str) -> None:
    full = produce(tmp_path / "full")
    one = produce(tmp_path / "one", "single")
    assert len([a for a in full["settled"]["actions"] if a["status"] == "filled"]) == 2
    observation = invoke(entry, tmp_path, full["settled"], one["after"], full["daily"], monkeypatch)
    observation["injected_precondition"] = "real two-fill settled plan paired with real one-fill ledger; not asserted to arise from a normal process crash"
    assert_reconciled_or_refused(observation, full["after"], tmp_path / "partial_observation.json")


@pytest.mark.parametrize("entry", ["settle", "evening"])
@pytest.mark.parametrize("field,replacement", [("symbol", "600999"), ("side", "sell"), ("date", "2026-09-10")])
def test_same_plan_id_with_wrong_trade_identity_is_not_complete(tmp_path: Path, monkeypatch, entry: str, field: str, replacement: str) -> None:
    real = produce(tmp_path / "original", "single")
    wrong = deepcopy(real["after"])
    wrong["trades"][0][field] = replacement
    observation = invoke(entry, tmp_path, real["settled"], wrong, real["daily"], monkeypatch)
    observation["injected_precondition"] = {"field": field, "wrong_value": replacement, "plan_id_unchanged": PLAN_ID}
    assert_reconciled_or_refused(observation, real["after"], tmp_path / "identity_observation.json")


@pytest.mark.parametrize("evidence_state", ["real_producer", "archive_missing", "embedded_corrupt", "legacy_flags_without_receipt"])
def test_unledgered_recovery_never_reprices_frozen_fills_from_current_csv(tmp_path: Path, monkeypatch, evidence_state: str) -> None:
    real = produce(tmp_path / "original", "single")
    plan = deepcopy(real["settled"])
    filled = [a for a in plan["actions"] if a.get("status") == "filled"]
    assert len(filled) == 1 and filled[0]["price"] == 5.02
    evidence = filled[0]["price_evidence"]
    assert verify_execution_receipt(evidence["receipt"]) is True
    archived = Path(evidence["archive_path"])
    assert archived.is_file()
    if evidence_state == "archive_missing":
        archived.unlink()
        assert not archived.exists()
    elif evidence_state == "embedded_corrupt":
        evidence["receipt"]["content"]["original_row"]["open"] = "99.0"
        assert verify_execution_receipt(evidence["receipt"]) is False
    elif evidence_state == "legacy_flags_without_receipt":
        # Explicit malformed/legacy-shaped evidence, not a natural producer
        # output: a claimed raw flag plus matching top-level identity is not a
        # frozen financial receipt and must not permit price reinterpretation.
        filled[0]["price_evidence"] = {"plan_id": PLAN_ID, "price_basis": "raw"}
        archived.unlink()
        assert not archived.exists() and "receipt" not in filled[0]["price_evidence"]
    fx.write_daily(real["daily"], "600500", prev_close=5.00, open_px=5.10)
    observation = invoke("settle", tmp_path, plan, real["empty"], real["daily"], monkeypatch)
    observation.update(evidence_state=evidence_state, original_fill_price=5.02, current_csv_open=5.10,
                       archive_exists=archived.exists(), top_level_plan_id=filled[0]["price_evidence"].get("plan_id"))
    assert_reconciled_or_refused(observation, real["after"], tmp_path / "receipt_observation.json")
