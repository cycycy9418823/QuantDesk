"""Accounting can serve readers and previews without loading execution or I/O."""
from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
import subprocess
import sys

import pytest

from quantdesk.paper.accounting import reconciliation as accounting


def fixture_state() -> dict:
    return {
        "initial_cash": 1000.0, "cash": 800.0, "market_value": 200.0,
        "total_equity": 1000.0, "realized_pnl": -3.0, "unrealized_pnl": 3.0,
        "positions": [{"symbol": "600000", "quantity": 100, "market_value": 200.0,
            "unrealized_pnl": 3.0, "management_state": "normal", "watch_status": "normal"}],
        "closed_positions": [{"symbol": "600001", "realized_pnl": 4.0}],
        "realized_reductions": [{"symbol": "600000", "realized_pnl": -2.0}],
        "legacy_realized_adjustment": -5.0,
    }


def preview_state() -> dict:
    return {
        "initial_cash": 1000.0, "cash": 1000.0, "market_value": 0.0,
        "total_equity": 1000.0, "realized_pnl": 0.0, "unrealized_pnl": 0.0,
        "legacy_realized_adjustment": 0.0, "positions": [], "closed_positions": [],
        "realized_reductions": [], "actions": [], "valuation_date": "2026-11-04",
    }


@pytest.mark.parametrize("entry", ["accounting", "freshness", "correction"])
def test_reader_and_preview_imports_do_not_load_execution_or_store(entry):
    code = r'''
import importlib.abc, json, sys
forbidden = {"quantdesk.paper.auto_paper_portfolio", "quantdesk.auto_paper_portfolio",
    "quantdesk.daily_candidate_refresh", "quantdesk.paper.state_store",
    "quantdesk.paper.ledger_read_contract", "quantdesk.paper.position_management_engine"}
class Forbid(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname in forbidden: raise AssertionError("forbidden dependency: " + fullname)
sys.meta_path.insert(0, Forbid())
entry, value = json.loads(sys.stdin.read())
if entry == "freshness":
    import quantdesk.evidence_freshness as module
    result = module.reconcile_paper_account(value)
elif entry == "correction":
    from quantdesk.paper.realized_corrections import prepare_cost_correction_state
    result = prepare_cost_correction_state(value, observed_at="2026-11-05T18:00:00+08:00", source_ledger_sha256="a" * 64)["state"]
else:
    from quantdesk.paper.accounting import reconcile_paper_account
    result = reconcile_paper_account(value)
assert not (forbidden & sys.modules.keys())
assert result["total_equity"] == 1000.0
print("isolated accounting ok")
'''
    value = preview_state() if entry == "correction" else fixture_state()
    result = subprocess.run([sys.executable, "-B", "-c", code],
        input=json.dumps([entry, value]), text=True, capture_output=True, timeout=30,
        cwd=Path(__file__).resolve().parents[1])
    assert result.returncode == 0, result.stdout + result.stderr
    assert "isolated accounting ok" in result.stdout


def test_accounting_operations_have_no_io_or_input_mutation(monkeypatch):
    from quantdesk.paper.realized_corrections import prepare_cost_correction_state
    import builtins
    import io
    import socket

    def denied(*args, **kwargs):
        raise AssertionError("Accounting attempted I/O")

    source = fixture_state()
    before = deepcopy(source)
    for owner, name in ((builtins, "open"), (io, "open"), (socket, "socket"), (subprocess, "Popen")):
        monkeypatch.setattr(owner, name, denied)
    assert accounting.strategy_state_errors(source) == []
    assert accounting.strategy_pnl_identity(source)["status"] == "consistent"
    result = accounting.reconcile_paper_account(source)
    assert result["accounting_reconciliation"]["status"] == "consistent"
    assert result["realized_pnl"] == -3.0
    assert source == before
    result["positions"][0]["quantity"] = 200
    assert source["positions"][0]["quantity"] == 100
    prepared = prepare_cost_correction_state(preview_state(),
        observed_at="2026-11-05T18:00:00+08:00", source_ledger_sha256="a" * 64)
    assert prepared["state"]["total_equity"] == 1000.0


def test_old_public_and_private_names_still_resolve():
    from quantdesk import auto_paper_portfolio as alias
    from quantdesk.paper import auto_paper_portfolio as engine
    from quantdesk.paper import ledger_read_contract as ledger
    from quantdesk import evidence_freshness as freshness

    assert alias is engine
    for name in ("reconcile_paper_account", "_normalized_management_state", "_paper_state_shape_errors",
                 "_paper_correction_fields", "_parseable_paper_number", "_PAPER_POSITION_NUMERIC_FIELDS",
                 "_PAPER_CLOSED_NUMERIC_FIELDS", "_PAPER_TOP_LEVEL_NUMERIC_FIELDS"):
        assert getattr(engine, name) is getattr(accounting, name)
    for name in ("number", "strategy_pnl_identity", "strategy_state_errors"):
        assert getattr(ledger, name) is getattr(accounting, name)
    assert freshness.reconcile_paper_account is accounting.reconcile_paper_account
    assert freshness.number is accounting.number


def test_writer_injection_stays_in_execution_layer(tmp_path, monkeypatch):
    from quantdesk.paper import auto_paper_portfolio as engine
    called = []
    sentinel = {"accounting_reconciliation": {"status": "invalid"}}
    monkeypatch.setattr(engine, "reconcile_paper_account", lambda state: called.append(state) or sentinel)
    monkeypatch.setattr(engine, "_auto_paper_state_store", lambda: pytest.fail("Invalid data reached storage"))
    monkeypatch.setattr(engine, "AUTO_PAPER_PORTFOLIO_STATE_JSON", tmp_path / "state.json")
    with pytest.raises(ValueError, match="paper_account_invalid"):
        engine._write_auto_paper_state(fixture_state())
    assert len(called) == 1
    assert not (tmp_path / "state.json").exists()


def test_raw_validation_and_projection_keep_different_missing_data_rules():
    source = {"positions": [], "closed_positions": []}
    assert accounting.strategy_state_errors(source) == [
        "paper_state_cash_missing_or_invalid", "paper_state_initial_cash_missing_or_invalid"]
    assert accounting._paper_state_shape_errors(source) == []
    result = accounting.reconcile_paper_account(source)
    assert result["accounting_reconciliation"]["status"] == "unverified"
    assert result["accounting_reconciliation"]["pnl_identity"]["missing_fields"] == ["initial_cash"]
    assert source == {"positions": [], "closed_positions": []}


@pytest.mark.parametrize("field", ["credit_used", "simulated_interest"])
def test_financing_remains_unverified_in_balanced_projection(field):
    source = {**fixture_state(), field: 1.0}
    result = accounting.reconcile_paper_account(source)
    assert result["accounting_reconciliation"]["status"] == "unverified"
    assert result["accounting_reconciliation"]["pnl_identity"]["unreconciled_fields"] == [field]
