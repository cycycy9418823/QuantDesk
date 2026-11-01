from __future__ import annotations

import json
import os

import pytest

from scripts.audit_paper_identity import audit, main, render


def write_state(tmp_path, **changes):
    state = {"cash": 100, "market_value": 0, "total_equity": 100,
             "initial_cash": 100, "realized_pnl": 0, "unrealized_pnl": 0,
             "legacy_realized_adjustment": 0, "positions": [],
             "closed_positions": [], "realized_reductions": []}
    state.update(changes)
    path = tmp_path / "state.json"
    path.write_text(json.dumps(state), encoding="utf-8")
    return path


def test_empty_valid_ledger_is_audited_without_crashing(tmp_path):
    report = audit(write_state(tmp_path))
    assert report["balance"]["ok"] is True
    assert report["pnl_identity"]["ok"] is True
    assert report["realized_reductions"]["recorded_realized_pnl_sum"] == 0
    assert report["verdict"]["repairable_by_avg_cost_unification"] is None


def test_missing_position_values_never_become_zero_or_pass(tmp_path):
    report = audit(write_state(tmp_path, positions=[{"symbol": "600000"}]))
    assert report["balance"]["ok"] is False
    assert report["balance"]["market_value_detail_sum"] is None
    assert report["balance"]["market_value_latest_price_times_quantity"] is None
    assert report["top_level_consistency"]["top_level_unrealized_equals_position_sum"] is None
    assert report["decomposition"]["delta_sums_are_partial"] is True
    assert report["decomposition"]["residual_after_cost_basis_unification"] is None
    assert "n/a" in render(report)


def test_missing_reduction_amount_does_not_become_zero(tmp_path):
    path = write_state(tmp_path, realized_reductions=[{"symbol": "600000"}])
    report = audit(path)
    assert report["realized_reductions"]["recorded_realized_pnl_sum"] is None
    assert report["verdict"]["status"] != "consistent"
    assert main(["--state", str(path), "--quiet"]) == 1


def test_missing_positions_list_is_not_the_same_as_an_empty_list(tmp_path):
    report = audit(write_state(tmp_path, positions=None))
    assert report["balance"]["ok"] is False


def test_audit_never_overwrites_its_input_ledger(tmp_path):
    path = write_state(tmp_path)
    original = path.read_bytes()
    assert main(["--state", str(path), "--json-out", str(path), "--quiet"]) == 2
    assert path.read_bytes() == original


def test_audit_refuses_hardlink_to_input_ledger(tmp_path):
    path = write_state(tmp_path)
    alias = tmp_path / "alias.json"
    try:
        os.link(path, alias)
    except OSError as exc:
        pytest.skip(f"hard links unavailable: {exc}")
    original = path.read_bytes()
    assert main(["--state", str(path), "--json-out", str(alias), "--quiet"]) == 2
    assert path.read_bytes() == original
