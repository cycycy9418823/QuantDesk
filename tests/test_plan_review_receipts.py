from __future__ import annotations

import copy
import json
from datetime import date, timedelta
from pathlib import Path

import pandas as pd
import pytest

from quantdesk import daily_candidate_refresh as refresh
from quantdesk import plan_review_receipts as receipts


def make_plan(trade_date: str = "2026-08-03", plan_id: str = "due-plan", symbols: int = 1) -> dict:
    plan_date = (date.fromisoformat(trade_date) - timedelta(days=1)).isoformat()
    return {
        "ok": True, "plan_id": plan_id, "plan_date": plan_date, "trade_date": trade_date,
        "source_data_cutoff": plan_date, "formal_execution_eligible": True,
        "symbols": [{"symbol": f"60000{i}", "rules": {"exit_rules": [{"price": 9.0}]}} for i in range(symbols)],
        "price_data_validation": {"status": "passed", "formal_execution_eligible": True,
                                  "target_date": plan_date, "blocked_symbols": []},
    }


def archive(root: Path, plan: dict) -> Path:
    path = root / "results/conditional_trade_plans_daily" / f"{plan['trade_date']}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(plan, ensure_ascii=False), encoding="utf-8")
    return path


def write_minutes(root: Path, plan: dict) -> None:
    target = plan["trade_date"]
    times = list(pd.date_range(f"{target} 09:31", f"{target} 11:30", freq="min")) + list(pd.date_range(f"{target} 13:01", f"{target} 15:00", freq="min"))
    for row in plan["symbols"]:
        path = root / "data/intraday/1m" / f"{row['symbol']}.csv"
        path.parent.mkdir(parents=True, exist_ok=True)
        pd.DataFrame([{"symbol": row["symbol"], "datetime": at.strftime("%Y-%m-%d %H:%M:%S"),
                       "open": 10.0, "high": 10.5, "low": 9.5, "close": 10.0,
                       "volume": 1000, "amount": 10000, "source": "deterministic_fixture", "price_basis": "raw"}
                      for at in times]).to_csv(path, index=False)


def v1_replay(plan: dict) -> dict:
    execution_id = f"{plan['plan_id']}:{plan['trade_date']}"
    state = {"cash": 10000, "positions": [], "applied_execution_ids": [execution_id],
             "last_conditional_plan_id": plan["plan_id"], "last_trade_date": plan["trade_date"]}
    symbols = [row["symbol"] for row in plan["symbols"]]
    minute = {"ok": True, "plan_id": plan["plan_id"], "target_trade_date": plan["trade_date"],
              "total_symbols": len(symbols), "available_count": len(symbols),
              "missing_count": 0, "invalid_count": 0, "stale_count": 0, "insufficient_count": 0,
              "available_symbols": symbols, "missing_symbols": [], "invalid_symbols": [],
              "stale_symbols": [], "insufficient_symbols": [],
              "items": [{"symbol": symbol, "ok": True, "status": "available", "target_date_rows": 240,
                         "warnings": [], "errors": []} for symbol in symbols]}
    receipt = refresh.build_conditional_replay_receipt(
        execution_payload={"ok": True, "plan_id": plan["plan_id"], "trade_date": plan["trade_date"],
                           "events": [], "blocked": [], "no_fill": [{"symbol": symbol, "reason": "no trigger"} for symbol in symbols]},
        apply_result={"ok": True, "is_paper_simulation": True, "no_broker": True, "actions": [], "state": state},
        persisted_state=copy.deepcopy(state), minute_evidence=minute,
        captured_at=f"{plan['trade_date']}T18:00:00+08:00",
    )
    assert refresh.verify_conditional_replay_receipt(receipt) == []
    return receipt


def review(root: Path, plan: dict, *, checked_at: str | None = None) -> dict:
    return receipts.build_expiry_review_receipt(root, plan, checked_at=checked_at or f"{plan['trade_date']}T18:10:00+08:00")


def test_complete_raw_review_is_sealed_and_does_not_claim_simulation(tmp_path: Path) -> None:
    plan = make_plan()
    archive(tmp_path, plan)
    write_minutes(tmp_path, plan)
    receipt = review(tmp_path, plan)
    assert receipt["status"] == "passed"
    assert receipt["simulation_status"] == "disabled"
    assert receipt["simulation_executed"] is False
    assert receipts.verify_plan_review_receipt(receipt, plan=plan, project_root=tmp_path) == []
    result = refresh.scan_unreplayed_archived_plans(tmp_path, as_of="2026-08-06", review_receipts=[receipt])
    assert result["unreplayed_count"] == 0
    assert result["plan_review_items"][0]["status"] == "simulation_disabled"
    assert result["evidence_counts"] == {"applied_to_ledger": 0, "intraday_simulation_executed": 0, "expiry_reviewed": 1}


def test_missing_minutes_are_reviewed_but_stay_explicitly_insufficient(tmp_path: Path) -> None:
    plan = make_plan(symbols=2)
    archive(tmp_path, plan)
    receipt = review(tmp_path, plan)
    assert receipt["review_completed"] is True
    assert receipt["status"] == "evidence_insufficient"
    assert receipts.verify_plan_review_receipt(receipt, plan=plan, project_root=tmp_path) == []
    result = refresh.scan_unreplayed_archived_plans(tmp_path, as_of="2026-08-06", review_receipts=[receipt])
    assert result["unreplayed_count"] == 1
    assert result["unreplayed"][0]["status"] == "evidence_insufficient"
    assert result["unreplayed"][0]["evidence"]["expiry_reviewed"] is True
    assert any("600000:minute_file_missing" in reason for reason in result["unreplayed"][0]["missing_evidence_reasons"])


@pytest.mark.parametrize("field,value,reason", [
    ("plan_hash", "0" * 64, "receipt_plan_hash_mismatch"),
    ("receipt_hash", "0" * 64, "receipt_hash_mismatch"),
    ("execution_id", "other:2026-08-03", "receipt_plan_identity_mismatch"),
    ("validator_version", "unknown", "receipt_validator_version_invalid"),
    ("book_id", "ai_trader", "receipt_book_identity_invalid"),
    ("checked_at", "2026-08-01T18:00:00+08:00", "receipt_due_state_mismatch"),
])
def test_tampered_receipts_never_cover(tmp_path: Path, field: str, value: str, reason: str) -> None:
    plan = make_plan()
    archive(tmp_path, plan)
    write_minutes(tmp_path, plan)
    receipt = review(tmp_path, plan)
    receipt[field] = value
    assert reason in receipts.verify_plan_review_receipt(receipt, plan=plan, project_root=tmp_path)
    result = refresh.scan_unreplayed_archived_plans(tmp_path, as_of="2026-08-06", review_receipts=[receipt])
    assert result["unreplayed_count"] == 1


def test_resigned_fake_pass_cannot_promote_missing_inputs(tmp_path: Path) -> None:
    plan = make_plan()
    receipt = review(tmp_path, plan)
    receipt.update(status="passed", review_passed=True, reason_codes=[])
    receipt["inputs"][0].update(status="passed", complete_session=True, reason_codes=[], price_basis="raw", sha256="0" * 64)
    receipts._seal(receipt)
    errors = receipts.verify_plan_review_receipt(receipt, plan=plan, project_root=tmp_path)
    assert "receipt_input_changed" in errors


def test_plan_change_or_input_change_invalidates_old_receipt(tmp_path: Path) -> None:
    plan = make_plan()
    archive(tmp_path, plan)
    write_minutes(tmp_path, plan)
    receipt = review(tmp_path, plan)
    changed = copy.deepcopy(plan)
    changed["symbols"][0]["rules"]["exit_rules"][0]["price"] = 8.0
    assert "receipt_plan_hash_mismatch" in receipts.verify_plan_review_receipt(receipt, plan=changed, project_root=tmp_path)
    path = tmp_path / receipt["inputs"][0]["path"]
    path.write_text(path.read_text(encoding="utf-8").replace("1000", "1100"), encoding="utf-8")
    assert "receipt_input_changed" in receipts.verify_plan_review_receipt(receipt, plan=plan, project_root=tmp_path)


def test_original_minute_basis_is_checked_before_target_date_filter(tmp_path: Path) -> None:
    plan = make_plan()
    write_minutes(tmp_path, plan)
    path = tmp_path / "data/intraday/1m/600000.csv"
    frame = pd.read_csv(path)
    outside = frame.iloc[0].copy()
    outside["datetime"] = "2026-08-02 09:31:00"
    outside["price_basis"] = "qfq"
    pd.concat([pd.DataFrame([outside]), frame], ignore_index=True).to_csv(path, index=False)
    receipt = review(tmp_path, plan)
    assert receipt["status"] == "evidence_insufficient"
    assert receipt["inputs"][0]["price_basis"] == "mixed_or_unknown"


def test_store_is_immutable_idempotent_and_keeps_changed_input_version(tmp_path: Path) -> None:
    plan = make_plan()
    archive(tmp_path, plan)
    write_minutes(tmp_path, plan)
    first = receipts.publish_plan_review_receipt(tmp_path, review(tmp_path, plan), plan=plan)
    path = Path(first["path"])
    initial_bytes = path.read_bytes()
    second = receipts.publish_plan_review_receipt(tmp_path, review(tmp_path, plan, checked_at="2026-08-03T20:00:00+08:00"), plan=plan)
    assert second["status"] == "idempotent"
    assert path.read_bytes() == initial_bytes
    source = tmp_path / "data/intraday/1m/600000.csv"
    source.write_text(source.read_text(encoding="utf-8").replace("1000", "1100"), encoding="utf-8")
    third = receipts.publish_plan_review_receipt(tmp_path, review(tmp_path, plan, checked_at="2026-08-04T18:00:00+08:00"), plan=plan)
    assert third["status"] == "completed" and third["path"] != first["path"]
    assert path.read_bytes() == initial_bytes
    assert third["receipt"]["reconstruction"] == "retrospective_reconstruction"
    scan = refresh.scan_unreplayed_archived_plans(tmp_path, as_of="2026-08-06")
    assert scan["unreplayed_count"] == 0


def test_old_v1_receipt_is_diagnostic_while_bound_replay_keeps_all_facts(tmp_path: Path) -> None:
    plan = make_plan()
    archive(tmp_path, plan)
    write_minutes(tmp_path, plan)
    old = v1_replay(plan)
    scan = refresh.scan_unreplayed_archived_plans(tmp_path, as_of="2026-08-06", replay_receipts=[old])
    assert scan["unreplayed_count"] == 1
    assert scan["unreplayed"][0]["status"] == "legacy_unbound"
    bound = receipts.bind_plan_replay_receipt(tmp_path, plan, old, checked_at="2026-08-03T18:10:00+08:00")
    replay_only = refresh.scan_unreplayed_archived_plans(tmp_path, as_of="2026-08-06", replay_receipts=[bound])
    assert replay_only["evidence_counts"] == {"applied_to_ledger": 0, "intraday_simulation_executed": 1, "expiry_reviewed": 0}
    result = refresh.scan_unreplayed_archived_plans(tmp_path, as_of="2026-08-06", review_receipts=[review(tmp_path, plan)], replay_receipts=[bound], applied_execution_ids=[old["execution_id"]])
    assert result["unreplayed_count"] == 0
    assert result["evidence_counts"] == {"applied_to_ledger": 1, "intraday_simulation_executed": 1, "expiry_reviewed": 1}


def test_before_close_review_is_not_due_and_never_published(tmp_path: Path) -> None:
    plan = make_plan()
    archive(tmp_path, plan)
    result = receipts.review_archived_plan(tmp_path, trade_date=plan["trade_date"], checked_at="2026-08-03T10:00:00+08:00", write=True)
    assert result["receipt"]["status"] == "not_due"
    assert not (tmp_path / receipts.STORE_PATH).exists()


def test_store_write_failure_is_visible(tmp_path: Path, monkeypatch) -> None:
    plan = make_plan()
    write_minutes(tmp_path, plan)
    def fail(*args):
        raise OSError("fixture disk failure")
    monkeypatch.setattr(refresh, "_exclusive_write_json", fail)
    result = receipts.publish_plan_review_receipt(tmp_path, review(tmp_path, plan), plan=plan)
    assert result["status"] == "blocked"
    assert "receipt_archive_write_failed:OSError" in result["reason_codes"]


def test_snapshot_reviews_only_requested_due_plan_and_keeps_older_gap(tmp_path: Path) -> None:
    older = make_plan("2026-07-31", "older")
    current = make_plan()
    archive(tmp_path, older)
    archive(tmp_path, current)
    write_minutes(tmp_path, current)
    snapshot = receipts.plan_review_snapshot(
        tmp_path, trade_date=current["trade_date"], checked_at="2026-08-03T18:30:00+08:00", write=True,
    )
    assert snapshot["plan_review_summary"]["reviewed_count"] == 1
    assert snapshot["plan_review_summary"]["unresolved_count"] == 1
    assert {row["plan_id"]: row["status"] for row in snapshot["plan_review_items"]} == {
        "older": "unreviewed", "due-plan": "simulation_disabled",
    }
    stored = receipts.load_plan_review_receipts(tmp_path)
    assert [row["plan_id"] for row in stored["review_receipts"]] == ["due-plan"]
    assert receipts.load_plan_review_snapshot(tmp_path)["plan_review_summary"] == snapshot["plan_review_summary"]


def test_portfolio_get_reads_only_published_review_snapshot(tmp_path: Path, monkeypatch) -> None:
    from quantdesk.paper import auto_paper_portfolio as paper

    plan = make_plan()
    archive(tmp_path, plan)
    receipts.plan_review_snapshot(tmp_path, trade_date=plan["trade_date"], checked_at="2026-08-03T18:30:00+08:00", write=True)
    monkeypatch.setattr(paper, "RESULTS_DIR", tmp_path / "results")
    monkeypatch.setattr(paper, "AUTO_PAPER_PORTFOLIO_LATEST_JSON", tmp_path / "results/auto_paper_portfolio_latest.json")
    monkeypatch.setattr(receipts, "inspect_plan_minute_inputs", lambda *_args, **_kwargs: pytest.fail("GET re-audited input files"))
    before = {str(path): path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()}
    result = paper.auto_paper_portfolio_latest()
    after = {str(path): path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()}
    assert result["plan_review_summary"]["evidence_insufficient_count"] == 1
    assert result["exists"] is False
    assert before == after


def test_input_change_during_simulation_cannot_claim_replayed(tmp_path: Path) -> None:
    plan = make_plan()
    archive(tmp_path, plan)
    write_minutes(tmp_path, plan)
    before = receipts.inspect_plan_minute_inputs(tmp_path, plan)
    path = tmp_path / "data/intraday/1m/600000.csv"
    path.write_text(path.read_text(encoding="utf-8").replace("1000", "1100"), encoding="utf-8")
    receipt = receipts.bind_plan_replay_receipt(tmp_path, plan, v1_replay(plan), checked_at="2026-08-03T18:10:00+08:00", input_evidence=before)
    assert receipt["simulation_executed"] is False
    assert "replay_input_changed_during_execution" in receipt["reason_codes"]
    scan = refresh.scan_unreplayed_archived_plans(tmp_path, as_of="2026-08-04", replay_receipts=[receipt])
    assert scan["unreplayed_count"] == 1 and scan["evidence_counts"]["intraday_simulation_executed"] == 0


def test_replay_archive_failure_is_preserved_in_published_summary(tmp_path: Path, monkeypatch) -> None:
    from quantdesk.paper import auto_paper_portfolio as paper

    plan = make_plan()
    archive(tmp_path, plan)
    write_minutes(tmp_path, plan)
    monkeypatch.setattr(paper, "RESULTS_DIR", tmp_path / "results")
    monkeypatch.setattr(paper, "INTRADAY_DIR", tmp_path / "data/intraday")
    monkeypatch.setattr(paper, "now_text", lambda: "2026-08-03T18:30:00+08:00")
    monkeypatch.setattr(paper, "publish_plan_review_receipt", lambda *args, **kwargs: {"status": "blocked", "reason_codes": ["fixture_disk_failure"]})
    payload: dict = {}
    paper._attach_bound_replay_review(payload, source_plan=plan, replay_receipt=v1_replay(plan),
                                     input_evidence=receipts.inspect_plan_minute_inputs(tmp_path, plan),
                                     applied_ids=[f"{plan['plan_id']}:{plan['trade_date']}"], dry_run=False)
    assert payload["plan_review_summary"]["status"] == "attention"
    assert "fixture_disk_failure" in payload["plan_review_summary"]["reason_codes"]
    assert "fixture_disk_failure" in receipts.load_plan_review_snapshot(tmp_path)["plan_review_summary"]["reason_codes"]


def test_daily_writer_reviews_due_plan_when_simulation_disabled(tmp_path: Path, monkeypatch) -> None:
    from quantdesk import daily_workflow_orchestrator as workflow
    from tests.test_round18_candidate_envelope_wiring import _patch_daily_workflow, TARGET_DATE

    _patch_daily_workflow(monkeypatch, tmp_path, formal_load_result={"status": "missing", "batch": None, "reason_codes": ["formal_candidate_batch_missing"]})
    monkeypatch.setattr(workflow, "now_text", lambda: f"{TARGET_DATE}T20:00:00+08:00")
    plan = make_plan(TARGET_DATE)
    path = archive(tmp_path, plan)
    write_minutes(tmp_path, plan)
    protected = {str(p): p.read_bytes() for p in (path, tmp_path / "data/intraday/1m/600000.csv")}
    result = workflow.run_daily_workflow(with_guardrails=False, enable_intraday=False, target_date=TARGET_DATE)
    assert result["plan_review_summary"]["reviewed_count"] == 1
    assert result["plan_review_summary"]["replayed_count"] == 0
    assert result["plan_review_items"][0]["status"] == "simulation_disabled"
    assert receipts.load_plan_review_snapshot(tmp_path)["plan_review_summary"]["reviewed_count"] == 1
    assert protected == {p: Path(p).read_bytes() for p in protected}
