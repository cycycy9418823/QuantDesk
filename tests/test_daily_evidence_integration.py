"""Refresh -> review -> backup -> isolated restore, with no live effects."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from quantdesk.daily_evidence_contract import audit_daily_evidence, build_requirements, digest, json_bytes, load_ref, publish_assessment
from quantdesk.daily_review_orchestrator import run_evening_review
from quantdesk.paper_state_backup import build_backup_evidence_closure, pack_backup_evidence, verify_backup_archive
from quantdesk.process_deadline import ProcessDeadlineResult
from test_daily_evidence_contract import CODE, TARGET, fixture_project, put, ready_factor, response, snapshot


class FixtureLease:
    def __init__(self, target=TARGET): self.target, self.run_id = target, "synthetic-pipeline"
    def assert_active(self): pass
    def __enter__(self): return self
    def __exit__(self, *args): return False


def ai_books(root):
    put(root, "state/ai_trader/ledger.json", {"ledger_id": "ai_trader_100k", "positions": [], "as_of": TARGET})
    put(root, "state/ai_trader_glm/ledger.json", {"ledger_id": "ai_trader_100k_glm", "positions": [], "as_of": TARGET})


def test_current_pipeline_checks_candidates_then_preserves_review_and_restores_dependencies(tmp_path, monkeypatch, capsys):
    from scripts import run_data_close_pipeline as pipeline, run_daily_data_refresh as refresh
    from quantdesk import adjustment_factor_evidence as factors
    from scripts import audit_daily_evidence as audit_cli
    root = tmp_path / "project"
    book = fixture_project(root, quantity=0)
    book["valuation_date"] = "2026-10-29"
    put(root, "state/auto_paper_portfolio.json", book)
    ai_books(root)
    candidate = root / f"results/candidate_refresh/daily/{TARGET}.json"
    candidate.unlink()
    daily = root / f"data/daily/{CODE}.csv"
    daily.write_text("\n".join(line for line in daily.read_text().splitlines() if not line.startswith(TARGET)) + "\n")
    refresh._append_daily_row(daily, f"{TARGET},{CODE},10,10,10,10,100,raw,synthetic_test_fixture", header=daily.read_text().splitlines()[0], lease=FixtureLease())
    requests = []
    def fetch(tasks, timeout):
        requests.extend(tasks)
        packets = {}
        for index, task in enumerate(tasks):
            packet = response(task["symbol"])
            packets[index] = {"raw": packet.raw, "proof": packet.proof}
        return ProcessDeadlineResult(packets, {}, [], False, [])
    original_maintenance = factors.maintain_adjustment_evidence
    monkeypatch.setattr(factors, "maintain_adjustment_evidence", lambda root, req, **kw: original_maintenance(root, req, **kw, fetch_batch=fetch))
    monkeypatch.setattr(refresh, "PROJECT_ROOT", root)
    monkeypatch.setattr(refresh, "RESULTS_DIR", root / "results/data_refresh")
    monkeypatch.setattr(refresh, "MarketWriterLease", lambda *a, **kw: FixtureLease(kw.get("target", TARGET)))
    monkeypatch.setattr(refresh, "_complete_live_session", lambda target: True)
    calls = []
    def owned(root, stage, command, **kw):
        calls.append((stage, command))
        if stage == "research_chain":
            assert not requests
            put(root, f"results/candidate_refresh/daily/{TARGET}.json", {"target_date": TARGET, "candidates": [{"symbol": CODE}]})
            return "completed", [], {"stdout_json": {"status": "completed"}}
        assert "--evidence-only" in command
        assert refresh.main(command[3:]) == 0
        printed = json.loads(capsys.readouterr().out)
        return printed["status"], [], {"stdout_json": printed}
    monkeypatch.setattr(pipeline, "_run_owned_subprocess", owned)
    stage = pipeline.stage_research_chain(root, network=True, timeout=120, target=TARGET,
        pipeline_run_id="synthetic-pipeline", pipeline_lock=object())
    assert [stage for stage, command in calls] == ["research_chain", "market_refresh"]
    assert len(requests) == 1
    before_review = audit_daily_evidence(root, TARGET)
    assert before_review["current"]["ledger_current"] is False
    assert before_review["windows"][0]["factors"]["status"] == "ready"
    expected_book = {**book, "valuation_date": TARGET, "cash": 8995,
        "positions": [{"symbol": CODE, "strategy_type": "long", "quantity": 100, "entry_date": TARGET,
                       "entry_price": 10.05, "execution_price": 10}],
        "actions": [{"action": "buy", "symbol": CODE, "strategy_type": "long", "quantity": 100, "price": 10,
                     "total_fee": 5, "decision_date": TARGET, "execution_price_source_sha256": digest(daily.read_bytes())}]}
    def deterministic_daily():
        put(root, "state/auto_paper_portfolio.json", expected_book)
        return 0
    review = run_evening_review(project_root=root, target_date=TARGET, authoritative_chain_run_id="synthetic-pipeline",
        daily_runner=deterministic_daily, now=datetime(2026, 10, 30, 12, 5, tzinfo=timezone.utc))
    assert review["research_evidence"]["status"] == "ready"
    assert (root / "state/auto_paper_portfolio.json").read_bytes() == json_bytes(expected_book)
    after_review = audit_daily_evidence(root, TARGET)
    assert after_review["current"]["status"] == "ready"
    archive = tmp_path / "evidence.tar.gz"
    packed = pack_backup_evidence(root, archive, target_date=TARGET)
    assert packed["ok"] and packed["research_business_scope"]["material_restore_ready"]
    restored = tmp_path / "restored"
    recovered = verify_backup_archive(archive, isolated_restore_root=restored, target_date=TARGET)
    assert recovered["isolated_restore_verified"]
    assert (restored / "state/auto_paper_portfolio.json").read_bytes() == json_bytes(expected_book)
    assert not (restored / "data/daily").exists()
    # Recovery uses the sealed price/calendar originals, not data from source.
    requirements = after_review["requirements"]
    assert audit_daily_evidence(restored, TARGET, requirements=requirements)["current"]["status"] == "ready"
    before = snapshot(restored)
    assert audit_cli.main(["--project-root", str(restored), "--target-date", TARGET, "--use-sealed-requirements", "--network", "false"]) == 0
    checked = json.loads(capsys.readouterr().out)
    assert checked["current"]["status"] == "ready"
    assert snapshot(restored) == before


def test_old_book_in_new_verified_backup_is_not_current_research(tmp_path):
    from scripts.ops import write_health_line as health
    root = tmp_path / "project"
    book = fixture_project(root)
    book["valuation_date"] = "2026-10-29"
    put(root, "state/auto_paper_portfolio.json", book)
    stamp = "20261030T140000Z"
    archive = root.parent / "tmp/backup" / f"quantdesk_backup_{stamp}.tar.gz"
    packed = pack_backup_evidence(root, archive, seed_paths=["state/auto_paper_portfolio.json"], target_date=TARGET)
    assert packed["ok"] and not packed["research_business_scope"]["ledger_current"]
    put(root, "logs/ops/backup_push.log", {"stamp": "snapshot_"+stamp, "all_hashes_match": True, "archive_sha256": digest(archive.read_bytes())})
    cell, note = health.backup_cell(root, TARGET)
    assert cell == "备份 ✗"
    assert "文件复制成功" in note and "2026-10-29" in note


def test_review_exit_zero_with_old_book_cannot_inherit_yesterdays_material_green(tmp_path):
    root = tmp_path / "project"
    book = fixture_project(root)
    book["valuation_date"] = "2026-10-29"
    put(root, "state/auto_paper_portfolio.json", book)
    result = run_evening_review(project_root=root, target_date=TARGET, authoritative_chain_run_id="no-new-book",
        daily_runner=lambda: 0, now=datetime(2026, 10, 30, 12, 5, tzinfo=timezone.utc))
    assert result["daily_returncode"] == 0
    assert result["research_evidence"]["status"] == "incomplete"
    assert "strategy_book_business_date_not_current" in result["research_evidence"]["reason_codes"]


def test_review_failure_has_a_dated_evidence_receipt_without_rewriting_the_book(tmp_path):
    root = tmp_path / "project"
    fixture_project(root)
    before = (root / "state/auto_paper_portfolio.json").read_bytes()
    result = run_evening_review(project_root=root, target_date=TARGET, authoritative_chain_run_id="failed-review",
        daily_runner=lambda: 7, now=datetime(2026, 10, 30, 12, 5, tzinfo=timezone.utc))
    assert result["daily_returncode"] == 7
    ref = result["research_evidence"]["daily_evidence_reference"]
    receipt = json.loads(load_ref(root, ref))
    assert receipt["process_status"] == "failed" and receipt["target_date"] == TARGET
    assert receipt["current"]["status"] == "incomplete"
    assert (root / "state/auto_paper_portfolio.json").read_bytes() == before


def test_new_evidence_reference_cannot_be_missing_from_backup_closure(tmp_path):
    from quantdesk.strategy_evidence_archive import archive_strategy_review
    root = tmp_path / "project"
    book = fixture_project(root)
    raw = json_bytes(book)
    result = archive_strategy_review(root, target_date=TARGET, run_id="synthetic", before_raw=raw, after_raw=raw,
        started_at=TARGET+"T20:00:00+08:00", recorded_at=TARGET+"T20:01:00+08:00", process_status="completed",
        requirements=build_requirements(root, TARGET), assert_write=lambda: None)
    doc = json.loads((root / result["manifest_path"]).read_bytes())
    (root / doc["price_sources"][CODE]["capture_path"]).unlink()
    closure = build_backup_evidence_closure(root, seed_paths=[f"results/strategy_evidence/{TARGET}/latest.json"])
    assert closure["ok"] is False
    assert closure["errors"]


def test_health_and_project_status_use_the_same_checklist_and_preserve_other_bytes(tmp_path):
    from scripts.ops import write_health_line as health
    from quantdesk.project_status import read_project_status
    fixture_project(tmp_path)
    tail = b"\r\n<!-- WRITE_LOCK_BEGIN -->\nClaude lock\r\n<!-- WRITE_LOCK_END -->\n\n## other\r\nKEEP  \r\n"
    prefix = ("# STATUS · 更新于 2026-10-30 22:30 · 更新者：health_line\r\n\r\n## 今日健康行\r\n\r\n日线 ✓1/1\r\n健康业务日期 2026-10-30\r\n").encode()
    (tmp_path / "STATUS.md").write_bytes(prefix + tail)
    assessment = audit_daily_evidence(tmp_path, TARGET)
    publish_assessment(tmp_path, assessment, stage="evening_review", run_id="synthetic", process_status="completed",
        recorded_at=TARGET+"T20:05:00+08:00", assert_write=lambda: None)
    line, explains, _ = health.build_line(tmp_path, TARGET)
    assert "研究资料✗" in line
    assert any("因子缺1组" in note for note in explains)
    health.rewrite_status(tmp_path, "# STATUS · 更新于 2026-10-30 22:30 · 更新者：health_line", line, " · ".join(explains))
    assert (tmp_path / "STATUS.md").read_bytes().endswith(tail)
    before = snapshot(tmp_path)
    status = read_project_status(tmp_path)
    assert status["daily_evidence"]["requirements_sha256"] == assessment["requirements_sha256"]
    assert status["daily_evidence"]["current"]["status"] == "incomplete"
    assert snapshot(tmp_path) == before


def test_backup_does_not_accept_real_daily_files_as_a_loose_dependency(tmp_path):
    fixture_project(tmp_path)
    closure = build_backup_evidence_closure(tmp_path, seed_paths=[f"data/daily/{CODE}.csv"])
    assert not closure["ok"]
    assert any("not_allowlisted" in value for value in closure["errors"])


def test_resume_version_and_material_hashes_do_not_reuse_pre_alignment_completion(tmp_path):
    from scripts import run_data_close_pipeline as pipeline
    fixture_project(tmp_path)
    assert pipeline._stage_version("research_chain").endswith(":4")
    original_input = pipeline._input_manifest_for("research_chain", tmp_path, TARGET)
    put(tmp_path, "state/daily_evidence/requests.json", {"schema": "daily_evidence_requests.v1", "windows": []})
    assert pipeline._input_manifest_for("research_chain", tmp_path, TARGET) != original_input
    original_output = pipeline._output_manifest_for("research_chain", tmp_path)
    put(tmp_path, "data/metadata/adjustment_evidence/index.json", {"schema": "adjustment_evidence_index.v1", "symbols": {}})
    assert pipeline._output_manifest_for("research_chain", tmp_path) != original_output


def test_weekend_backup_of_friday_book_is_not_a_stale_business_date(tmp_path):
    from scripts.ops import write_health_line as health
    from quantdesk.daily_evidence_contract import inspect_backup_readiness
    root = tmp_path / "project"
    fixture_project(root)
    stamp = "20261101T140000Z"
    archive = root.parent / "tmp/backup" / f"quantdesk_backup_{stamp}.tar.gz"
    pack_backup_evidence(root, archive, seed_paths=["state/auto_paper_portfolio.json"], target_date=TARGET)
    put(root, "logs/ops/backup_push.log", {"stamp": "snapshot_"+stamp, "all_hashes_match": True, "archive_sha256": digest(archive.read_bytes())})
    checked = inspect_backup_readiness(root, "2026-11-01")
    assert checked["expected_business_date"] == TARGET
    assert checked["research_business_scope"]["ledger_current"] is True
    assert health.backup_cell(root, "2026-11-01")[0] == "备份 ✓"


def test_a_known_backup_hash_failure_cannot_keep_the_copy_green(tmp_path):
    from scripts.ops import write_health_line as health
    root = tmp_path / "project"
    fixture_project(root, quantity=0)
    stamp = "20261030T140000Z"
    archive = root.parent / "tmp/backup" / f"quantdesk_backup_{stamp}.tar.gz"
    pack_backup_evidence(root, archive, seed_paths=["state/auto_paper_portfolio.json"], target_date=TARGET)
    put(root, "logs/ops/backup_push.log", {"stamp": "snapshot_"+stamp,
        "all_hashes_match": True, "archive_sha256": digest(archive.read_bytes())})
    with archive.open("ab") as handle:
        handle.write(b"synthetic changed backup bytes")
    cell, note = health.backup_cell(root, TARGET)
    assert cell == "备份 ✗"
    assert "校验失败" in note


def test_latest_incomplete_assessment_overrides_a_retained_success_in_backup(tmp_path):
    from quantdesk.strategy_evidence_archive import archive_strategy_review
    root = tmp_path / "project"
    book = fixture_project(root, quantity=0)
    ready_factor(root)
    raw = json_bytes(book)
    for run_id in ("first", "latest"):
        if run_id == "latest":
            (root / "data/metadata/adjustment_evidence/index.json").unlink()
        archive_strategy_review(root, target_date=TARGET, run_id=run_id, before_raw=raw, after_raw=raw,
            started_at=TARGET+"T20:00:00+08:00", recorded_at=TARGET+"T20:01:00+08:00",
            process_status="completed", requirements=build_requirements(root, TARGET), assert_write=lambda: None)
        value = audit_daily_evidence(root, TARGET)
        assert value["current"]["status"] == ("ready" if run_id == "first" else "incomplete")
        ref = publish_assessment(root, value, stage="evening_review", run_id=run_id, process_status="completed",
            recorded_at=TARGET+"T20:05:00+08:00", assert_write=lambda: None)
        if run_id == "first": old_success = ref
    archive = tmp_path / "retained-history.tar.gz"
    result = pack_backup_evidence(root, archive, target_date=TARGET, seed_paths=[
        "state/auto_paper_portfolio.json", f"results/daily_evidence/{TARGET}/latest.json",
        f"results/strategy_evidence/{TARGET}/latest.json", old_success["capture_path"]])
    assert result["ok"] is True
    assert result["research_business_scope"]["ledger_current"] is True
    assert result["research_business_scope"]["material_restore_ready"] is False


@pytest.mark.parametrize("historical_basis", ["raw", "qfq"])
def test_registered_historical_prices_are_preserved_and_do_not_change_current_fill_scope(tmp_path, historical_basis):
    from quantdesk.strategy_evidence_archive import archive_strategy_review, archived_price_bytes
    root = tmp_path / "project"
    book = fixture_project(root, quantity=0)
    ai_books(root)
    historical_code = "600825"
    original = (root / f"data/daily/{CODE}.csv").read_text()
    (root / f"data/daily/{historical_code}.csv").write_text(
        original.replace(CODE, historical_code).replace(",raw,", f",{historical_basis},"), encoding="utf-8")
    ready_factor(root)
    ready_factor(root, code=historical_code)
    put(root, "state/daily_evidence/requests.json", {"schema": "daily_evidence_requests.v1", "windows": [{
        "request_id": "registered-history", "scope": "historical", "purpose": "material_recovery",
        "start_date": "2026-10-29", "end_date": TARGET, "symbols": [historical_code]}]})
    raw = json_bytes(book)
    archive_strategy_review(root, target_date=TARGET, run_id="registered-window", before_raw=raw, after_raw=raw,
        started_at=TARGET+"T20:00:00+08:00", recorded_at=TARGET+"T20:01:00+08:00",
        process_status="completed", requirements=build_requirements(root, TARGET), assert_write=lambda: None)
    before = audit_daily_evidence(root, TARGET)
    assert before["current"]["status"] == "ready"
    publish_assessment(root, before, stage="evening_review", run_id="registered-window", process_status="completed",
        recorded_at=TARGET+"T20:05:00+08:00", assert_write=lambda: None)
    archive = tmp_path / "registered-window.tar.gz"
    packed = pack_backup_evidence(root, archive, target_date=TARGET)
    assert packed["ok"], packed["errors"]
    restored = tmp_path / "restored"
    assert verify_backup_archive(archive, isolated_restore_root=restored, target_date=TARGET)["isolated_restore_verified"]
    after = audit_daily_evidence(restored, TARGET, requirements=before["requirements"])
    assert after["current"]["status"] == "ready"
    assert archived_price_bytes(restored, TARGET, historical_code) == (root / f"data/daily/{historical_code}.csv").read_bytes()
    assert after["historical"]["status"] == before["historical"]["status"]
    assert after["historical"]["formal_comparison_ready"] is False
    if historical_basis == "raw":
        prior = next(row for row in before["windows"] if row["scope"] == "historical")
        recovered = next(row for row in after["windows"] if row["scope"] == "historical")
        assert recovered["prices"]["sha256"] == prior["prices"]["sha256"]
