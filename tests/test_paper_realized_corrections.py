from __future__ import annotations

import copy
import hashlib
import json

import pytest


def legacy_state() -> dict:
    return {
        "initial_cash": 10000.0, "cash": 5196.32, "market_value": 5010.0,
        "total_equity": 10206.32, "realized_pnl": -104.36,
        "unrealized_pnl": 307.65, "legacy_realized_adjustment": -20.0,
        "valuation_date": "2026-10-30", "positions": [{
            "symbol": "600000", "entry_date": "2026-08-05",
            "entry_price": 9.4047, "avg_cost": 9.3186, "quantity": 500,
            "latest_price": 10.02, "market_value": 5010.0,
            "unrealized_pnl": 307.65, "valuation_price_basis": "raw",
            "adjusted_quantity_for_valuation": 500,
        }],
        "closed_positions": [{
            "symbol": "600611", "entry_date": "2026-08-06",
            "exit_date": "2026-09-24", "entry_price": 4.8024,
            "avg_cost": 5.0025, "quantity": 200, "net_amount": 876.12,
            "gross_amount": 882.0, "commission": 5.0,
            "realized_pnl": -84.36, "return_pct": -8.17, "status": "closed",
            "reason": "原纸面记录",
        }], "realized_reductions": [], "actions": [{"action": "hold", "symbol": "600000"}],
    }


def corrected_state() -> dict:
    state = legacy_state()
    row = state["closed_positions"][0]
    fingerprint = hashlib.sha256(json.dumps(row, ensure_ascii=False, sort_keys=True,
        separators=(",", ":"), allow_nan=False).encode("utf-8")).hexdigest()
    original_json = json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    state.update(realized_corrections=[{
        "correction_id": "weighted_cost:" + fingerprint, "record_group": "closed_positions",
        "source_record_digest": fingerprint, "symbol": "600611",
        "entry_date": "2026-08-06", "exit_date": "2026-09-24",
        "original_realized_pnl": -84.36, "corrected_realized_pnl": -124.38,
        "delta": -40.02, "calculation": "net_amount - avg_cost * exit_quantity",
        "quantity_field": "quantity", "authority": "CHARTER.md section 12 decision 19",
        "evidence": {"source_ledger_sha256": "a" * 64, "source_record_index": 0,
            "original_record_sha256": hashlib.sha256(original_json.encode("utf-8")).hexdigest(),
            "original_record_json": original_json, "observed_at": "2026-11-02T19:00:00+08:00"},
    }], realized_corrections_total=-40.02, effective_realized_adjustment=-60.02,
        realized_pnl=-144.38, unrealized_pnl=350.70)
    state["positions"][0]["unrealized_pnl"] = 350.70
    return state


def test_reconciliation_consumes_append_only_correction_once():
    from quantdesk.paper.auto_paper_portfolio import reconcile_paper_account

    state = corrected_state()
    original = copy.deepcopy(state)
    result = reconcile_paper_account(state)
    assert result["realized_pnl"] == -144.38
    assert result["legacy_realized_adjustment"] == -20.0
    assert result["effective_realized_adjustment"] == -60.02
    assert result["accounting_reconciliation"]["pnl_identity"]["difference"] == 0.0
    assert reconcile_paper_account(result)["realized_pnl"] == -144.38
    assert state == original
    assert result["closed_positions"] == state["closed_positions"]


def test_preparation_is_idempotent_preserves_cash_and_original_records():
    from quantdesk.paper.realized_corrections import prepare_cost_correction_state

    state = legacy_state()
    original = copy.deepcopy(state)
    prepared = prepare_cost_correction_state(state, observed_at="2026-11-02T19:00:00+08:00",
        source_ledger_sha256="a" * 64)
    result = prepared["state"]
    assert len(prepared["added_corrections"]) == 1
    assert result["realized_corrections_total"] == -40.02
    assert result["legacy_realized_adjustment"] == -20.0
    assert result["effective_realized_adjustment"] == -60.02
    assert result["realized_pnl"] == -144.38
    assert result["unrealized_pnl"] == 350.70
    assert result["cash"] == original["cash"]
    assert result["total_equity"] == original["total_equity"]
    for key in ("closed_positions", "realized_reductions", "actions", "valuation_date"):
        assert result[key] == original[key]
    assert state == original
    again = prepare_cost_correction_state(result, observed_at="2026-11-02T20:00:00+08:00",
        source_ledger_sha256="c" * 64)
    assert again["added_corrections"] == []
    assert again["state"] == result


@pytest.mark.parametrize("indent,ascii_only,newline", [(2, False, "\n"), (4, True, "\r\n")])
def test_prepared_bytes_preserve_original_closed_record_and_actions(indent, ascii_only, newline):
    from quantdesk.paper.realized_corrections import prepare_cost_correction_bytes

    original = json.dumps(legacy_state(), indent=indent, ensure_ascii=ascii_only).replace("\n", newline).encode("utf-8")
    prepared = prepare_cost_correction_bytes(original, observed_at="2026-11-02T19:00:00+08:00")
    result = json.loads(prepared["proposed_bytes"])
    assert prepared["original_arrays_preserved"] is True
    assert result["closed_positions"] == legacy_state()["closed_positions"]
    assert result["actions"] == legacy_state()["actions"]
    assert result["realized_pnl"] == -144.38
    assert result["total_equity"] == 10206.32


@pytest.mark.parametrize("change", ["delta", "duplicate", "record", "effective", "naive_time", "boolean"])
def test_corrupt_corrections_are_invalid_even_when_top_level_identity_balances(change):
    from quantdesk.paper.auto_paper_portfolio import reconcile_paper_account
    from quantdesk.paper.ledger_read_contract import strategy_state_errors

    state = corrected_state()
    if change == "delta":
        state["realized_corrections"][0]["delta"] = -40.01
    elif change == "duplicate":
        state["realized_corrections"].append(copy.deepcopy(state["realized_corrections"][0]))
    elif change == "record":
        state["closed_positions"][0]["avg_cost"] = 5.0026
    elif change == "effective":
        state["effective_realized_adjustment"] = -60.03
    elif change == "naive_time":
        state["realized_corrections"][0]["evidence"]["observed_at"] = "2026-11-02T19:00:00"
    else:
        state["realized_corrections"][0]["delta"] = True
    assert any("correction" in reason for reason in strategy_state_errors(state))
    result = reconcile_paper_account(state)
    assert result["accounting_reconciliation"]["status"] == "invalid"


def test_legacy_without_corrections_keeps_the_old_scalar():
    from quantdesk.paper.auto_paper_portfolio import reconcile_paper_account

    state = legacy_state()
    result = reconcile_paper_account(state)
    assert result["legacy_realized_adjustment"] == -20.0
    assert result["realized_pnl"] == -104.36


def test_preparation_rejects_pnl_that_does_not_match_recorded_price():
    from quantdesk.paper.realized_corrections import prepare_cost_correction_state

    state = legacy_state()
    state["positions"][0].update(latest_price=9.92, market_value=4960.0)
    state.update(cash=5246.32, market_value=4960.0)
    original = copy.deepcopy(state)
    with pytest.raises(ValueError, match="recorded_pnl_unexplained"):
        prepare_cost_correction_state(state, observed_at="2026-11-02T19:00:00+08:00",
            source_ledger_sha256="a" * 64)
    assert state == original


def test_product_projection_consumes_effective_pnl_and_keeps_original_loss():
    from quantdesk.product_integrity import build_product_integrity

    state = corrected_state()
    before = copy.deepcopy(state)
    projection = build_product_integrity(state)
    assert projection["realized_components"]["sum"] == -144.38
    assert projection["realized_components"]["difference"] == 0.0
    assert projection["realized_components"]["legacy_adjustment"] == -20.0
    assert projection["realized_components"]["corrections"] == -40.02
    assert projection["position_costs"][0]["status"] == "consistent"
    assert projection["position_costs"][0]["pnl_cost_price"] == 9.3186
    assert projection["capital_cost_bridge"]["difference"] == 0.0
    conflicts = projection["capital_cost_bridge"]["cost_basis_explanation"]
    assert conflicts["candidate_count"] == 0
    assert conflicts["candidate_adjustment_sum"] == 0.0
    loss = projection["loss_contribution"]["top_losses"][0]
    assert loss["realized_pnl"] == -124.38
    assert loss["recorded_realized_pnl"] == -84.36
    assert state == before


def test_audit_uses_valid_correction_once_and_refuses_invalid_evidence(tmp_path):
    from quantdesk.paper.realized_corrections import prepare_cost_correction_state
    from scripts.audit_paper_identity import audit

    state = legacy_state()
    state["closed_positions"][0].update(exit_price=4.41, total_fee=5.88)
    state = prepare_cost_correction_state(state, observed_at="2026-11-02T19:00:00+08:00",
        source_ledger_sha256="a" * 64)["state"]
    path = tmp_path / "ledger.json"
    path.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
    before = path.read_bytes()
    report = audit(path)
    assert report["verdict"]["status"] == "consistent"
    assert report["top_level_consistency"]["top_level_realized_equals_detail_sum"] == 0.0
    assert report["decomposition"]["residual_after_cost_basis_unification"] == 0.0
    assert report["closed_positions"]["rows"][0]["realized_pnl_recorded"] == -84.36
    assert report["closed_positions"]["rows"][0]["effective_realized_pnl"] == -124.38
    assert report["realized_correction_evidence"]["historical_cashflow_verified"] is False
    assert path.read_bytes() == before
    state["realized_corrections"][0]["delta"] = -40.01
    path.write_text(json.dumps(state), encoding="utf-8")
    assert audit(path)["verdict"]["status"] == "invalid"


def test_latest_projection_uses_top_level_legacy_base_and_rejects_summary_conflict(tmp_path):
    from quantdesk.evidence_freshness import _paper_account_consistency

    state = corrected_state()
    arrays = {key: copy.deepcopy(state[key]) for key in ("positions", "closed_positions", "realized_reductions")}
    summary = {key: value for key, value in state.items() if key not in arrays}
    summary.pop("legacy_realized_adjustment")
    latest = {**arrays, "summary": summary,
        **{key: state[key] for key in ("legacy_realized_adjustment", "realized_corrections",
            "realized_corrections_total", "effective_realized_adjustment")}}
    path = tmp_path / "latest.json"
    path.write_text(json.dumps(latest), encoding="utf-8")
    assert _paper_account_consistency(path, latest_snapshot=True)["status"] == "consistent"
    latest["summary"]["effective_realized_adjustment"] = -60.03
    path.write_text(json.dumps(latest), encoding="utf-8")
    assert _paper_account_consistency(path, latest_snapshot=True)["status"] == "invalid"


def test_product_projection_rejects_invalid_corrections_even_with_balanced_identity():
    from quantdesk.product_integrity import build_product_integrity

    state = corrected_state()
    state["realized_corrections"][0]["delta"] = -40.01
    projection = build_product_integrity(state)
    assert projection["pnl_identity"]["status"] == "invalid"
    assert projection["realized_components"]["sum"] is None
    assert projection["capital_cost_bridge"]["cost_basis_explanation"]["status"] == "unavailable"
    assert projection["loss_contribution"]["reconciled_total"] is False


def test_summary_uses_same_effective_adjustment_without_changing_closed_records():
    from quantdesk.paper.auto_paper_portfolio import _summary

    state = corrected_state()
    original = copy.deepcopy(state)
    summary = _summary(state, [])
    assert summary["realized_pnl"] == -144.38
    assert summary["legacy_realized_adjustment"] == -20.0
    assert summary["effective_realized_adjustment"] == -60.02
    assert summary["realized_corrections"] == state["realized_corrections"]
    assert state == original


def test_wire_preview_is_idempotent_and_rejects_duplicate_json_keys():
    from quantdesk.paper.realized_corrections import prepare_cost_correction_bytes

    raw = json.dumps(legacy_state(), ensure_ascii=False, indent=4).encode("utf-8")
    first = prepare_cost_correction_bytes(raw, observed_at="2026-11-02T19:00:00+08:00")
    second = prepare_cost_correction_bytes(first["proposed_bytes"], observed_at="2026-11-02T20:00:00+08:00")
    assert second["added_corrections"] == []
    assert second["proposed_bytes"] == first["proposed_bytes"]
    with pytest.raises(ValueError, match="duplicate_json_key"):
        prepare_cost_correction_bytes(raw.replace(b'"initial_cash":', b'"cash":'),
            observed_at="2026-11-02T19:00:00+08:00")


def test_cli_default_is_read_only_and_external_preview_is_reviewable(tmp_path, capsys):
    from scripts.prepare_paper_cost_corrections import main

    source = tmp_path / "ledger.json"
    source.write_text(json.dumps(legacy_state(), ensure_ascii=False, indent=4), encoding="utf-8")
    original = source.read_bytes()
    assert main(["--state", str(source)]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["installed"] is False
    assert report["formal_state_written"] is False
    assert "proposed_ledger_path" not in report
    assert list(tmp_path.iterdir()) == [source]
    output = tmp_path / "preview"
    assert main(["--state", str(source), "--output-dir", str(output)]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["after"]["effective_realized_adjustment"] == -60.02
    assert report["source_unchanged"] is True
    assert (output / "original_ledger.json").read_bytes() == original
    assert source.read_bytes() == original
    assert json.loads((output / "proposed_ledger.json").read_bytes())["realized_pnl"] == -144.38
    before = {path.name: path.read_bytes() for path in output.iterdir()}
    assert main(["--state", str(source), "--output-dir", str(output)]) == 2
    assert "output_already_exists" in capsys.readouterr().out
    assert {path.name: path.read_bytes() for path in output.iterdir()} == before


def test_cli_refuses_runtime_output_inside_input_project(tmp_path, capsys):
    from scripts.prepare_paper_cost_corrections import main

    project = tmp_path / "project"
    project.mkdir()
    (project / "CHARTER.md").write_text("Synthetic isolated project", encoding="utf-8")
    source = project / "state" / "ledger.json"
    source.parent.mkdir()
    source.write_text(json.dumps(legacy_state()), encoding="utf-8")
    original = source.read_bytes()
    output = project / "results" / "preview"
    assert main(["--state", str(source), "--output-dir", str(output)]) == 2
    assert "outside_project" in capsys.readouterr().out
    assert not output.exists()
    assert source.read_bytes() == original


def test_cli_rejects_invalid_source_before_creating_outputs(tmp_path, capsys):
    from scripts.prepare_paper_cost_corrections import main

    source = tmp_path / "ledger.json"
    state = corrected_state()
    state["realized_corrections"][0]["evidence"]["original_record_sha256"] = "b" * 64
    source.write_text(json.dumps(state), encoding="utf-8")
    output = tmp_path / "preview"
    original = source.read_bytes()
    assert main(["--state", str(source), "--output-dir", str(output)]) == 2
    assert "existing_evidence_invalid" in capsys.readouterr().out
    assert not output.exists()
    assert source.read_bytes() == original


def test_adjusted_position_projection_uses_declared_quantity_and_cost():
    from quantdesk.product_integrity import position_cost_evidence

    position = {"quantity": 200, "avg_cost": 15.0, "entry_price": 10.0,
        "latest_price": 10.0, "price_adjustment_status": "adjusted",
        "adjusted_avg_cost": 7.5, "adjusted_quantity_for_valuation": 400,
        "unrealized_pnl": 1000.0}
    result = position_cost_evidence(position)
    assert result["pnl_cost_price"] == 15.0
    assert result["valuation_cost_price"] == 7.5
    assert result["recomputed_unrealized_pnl"] == 1000.0
    assert result["status"] == "consistent"


@pytest.mark.parametrize("summary_delta", [0.0, 1.0])
def test_latest_writer_checks_summary_correction_with_original_record_context(tmp_path, monkeypatch, summary_delta):
    from quantdesk.paper import auto_paper_portfolio as paper

    state = corrected_state()
    original = copy.deepcopy(state)
    summary = paper._summary(state, [])
    summary["realized_pnl"] += summary_delta
    monkeypatch.setattr(paper, "AUTO_PAPER_PORTFOLIO_LATEST_JSON", tmp_path / "latest.json")
    written = paper._write_auto_paper_latest({**state, "summary": summary})
    expected = "consistent" if summary_delta == 0.0 else "mismatch"
    assert written["summary"]["pnl_identity"]["status"] == expected
    assert written["summary"]["realized_pnl"] == summary["realized_pnl"]
    assert written["closed_positions"] == state["closed_positions"]
    assert state == original
