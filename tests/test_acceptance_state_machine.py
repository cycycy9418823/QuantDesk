from __future__ import annotations

import hashlib
import json
from pathlib import Path

from quantdesk.daily_data_chain import preflight_daily_data_chain, run_daily_data_chain
from quantdesk.project_acceptance import build_project_acceptance


def _write_run(
    root: Path,
    *,
    run_id: str,
    target_date: str,
    status: str,
) -> None:
    run_dir = root / "results" / "data_chain" / "runs" / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    manifest = {
        "run_id": run_id,
        "task_type": "daily_data_chain",
        "daily_data_chain_contract_version": 10,
        "status": status,
        "target_date": target_date,
        "started_at": f"{target_date}T15:45:00",
        "authoritative": True,
        "network": True,
        "dry_run": False,
        "components": {
            "sector_history_archive": {
                "snapshot_hash": "sha256:" + "a" * 64,
            }
        },
    }
    (run_dir / "manifest.json").write_text(
        json.dumps(manifest),
        encoding="utf-8",
    )
    (run_dir / "sector_history_archive_attempt.json").write_text(
        json.dumps({"snapshot_hash": "sha256:" + "a" * 64}),
        encoding="utf-8",
    )


def test_completed_target_is_skipped_before_chain_launch(tmp_path: Path) -> None:
    _write_run(
        tmp_path,
        run_id="daily-data-chain-20260814T074500Z-completed",
        target_date="2026-08-14",
        status="completed",
    )

    result = preflight_daily_data_chain(
        tmp_path,
        target_date="2026-08-14",
        current_run_id="daily-data-chain-20260814T103000Z-retry",
        retry_from_latest=True,
    )

    assert result["action"] == "skip"
    assert result["status"] == "skipped_already_completed"
    assert result["reason_codes"] == ["same_day_run_already_completed"]
    assert result["completed_run_ids"] == [
        "daily-data-chain-20260814T074500Z-completed"
    ]


def test_project_acceptance_keeps_alpha_and_cloud_independent(tmp_path: Path) -> None:
    payload = build_project_acceptance(
        tmp_path,
        as_of="2026-08-15",
        paper_mechanics={
            "status": "passed",
            "acceptance_passed": True,
            "research_only": True,
            "paper_only": True,
            "no_broker": True,
        },
        daily_chain={"status": "completed", "authoritative": True},
        stage4={
            "status": "maintenance_window_incomplete",
            "milestone_dates": {"T0+18": "2026-08-20"},
            "reason_codes": ["shadow_daily_count_gap"],
        },
        factor_verdicts=[
            {"dimension": "risk", "verdict": "keep"},
            {"dimension": "trend", "verdict": "kill"},
        ],
    )

    assert payload["status"] == "local_research_tool_accepted"
    assert payload["ok"] is True
    assert payload["dispositions"]["local_research_tool"]["status"] == "accepted"
    assert payload["dispositions"]["operations_observation"]["status"] == "pending"
    assert (
        payload["dispositions"]["strategy_alpha"]["status"]
        == "not_validated"
    )
    assert payload["dispositions"]["cloud_deployment"]["status"] == "deferred"


def test_acceptance_does_not_infer_terminal_closure_from_stale_maintenance(
    tmp_path: Path,
) -> None:
    payload = build_project_acceptance(
        tmp_path,
        as_of="2026-08-21",
        paper_mechanics={
            "status": "passed",
            "acceptance_passed": True,
            "research_only": True,
            "paper_only": True,
            "no_broker": True,
        },
        daily_chain={"status": "completed", "authoritative": True},
        stage4={
            "status": "maintenance_window_incomplete",
            "as_of": "2026-08-07",
            "milestone_dates": {"T0+18": "2026-08-20"},
            "reason_codes": ["shadow_daily_count_gap"],
        },
        factor_verdicts=[],
    )

    operations = payload["dispositions"]["operations_observation"]
    assert operations["status"] == "terminal_artifact_pending"
    assert "stage4_terminal_evaluation_missing_or_stale" in operations["reason_codes"]
    assert operations["sample_disposition"] == "not_evaluable"


def test_acceptance_surfaces_valid_terminal_final_evaluation(tmp_path: Path) -> None:
    adapter = (
        tmp_path
        / "results"
        / "stage4_replay_adapter"
        / "2026-08-20"
        / "adapter_result.json"
    )
    adapter.parent.mkdir(parents=True, exist_ok=True)
    adapter.write_text(
        json.dumps(
            {
                "schema_version": "stage4_tri_account_replay_adapter_v1",
                "task_type": "stage4_tri_account_replay_adapter",
                "status": "real_replay_ready",
                "quality_gate": {"passed": True, "reason_codes": []},
                "fixture_or_synthetic": False,
                "maintenance_eligible": True,
                "source_manifest": {
                    "data_mode": "real_canonical",
                    "seal": "sha256:" + "a" * 64,
                },
                "replay_product": {
                    "window_start": "2026-08-20",
                    "window_end": "2026-08-20",
                    "window_dates": ["2026-08-20"],
                    "as_of": "2026-08-20",
                    "trades": [],
                    "nav_series": [],
                    "effective_sell_sample": 0,
                    "evidence_eligible": False,
                    "fixture_or_synthetic": False,
                    "reason_codes": [],
                },
                "benchmark_by_date": {},
            }
        ),
        encoding="utf-8",
    )
    adapter_hash = "sha256:" + hashlib.sha256(adapter.read_bytes()).hexdigest()
    final = {
        "schema_version": "stage4_final_evaluation_v1",
        "task_type": "stage4_final_evaluation",
        "as_of": "2026-08-20",
        "terminal_status": "closed_inconclusive",
        "maintenance_disposition": "inconclusive",
        "sample_disposition": "insufficient_sample",
        "evaluator_verdict": "insufficient_sample",
        "reason_codes": ["insufficient_sample", "shadow_daily_count_gap"],
        "research_only": True,
        "paper_only": True,
        "no_broker": True,
        "is_real_order": False,
        "source_adapter": {
            "path": adapter.relative_to(tmp_path).as_posix(),
            "sha256": adapter_hash,
        },
        "evaluation": {
            "schema_version": "round9s_stage4_final_evaluator_v1",
            "as_of": "2026-08-20",
            "verdict": "insufficient_sample",
        },
    }
    output = tmp_path / "results" / "data_health"
    output.mkdir(parents=True, exist_ok=True)
    (output / "stage4_final_evaluation_2026-08-20.json").write_text(
        json.dumps(final), encoding="utf-8"
    )
    (output / "stage4_final_evaluation_latest.json").write_text(
        json.dumps(final), encoding="utf-8"
    )
    payload = build_project_acceptance(
        tmp_path,
        as_of="2026-08-21",
        paper_mechanics={
            "status": "passed",
            "acceptance_passed": True,
            "research_only": True,
            "paper_only": True,
            "no_broker": True,
        },
        daily_chain={"status": "completed", "authoritative": True},
        stage4={
            "status": "closed_inconclusive",
            "as_of": "2026-08-20",
            "milestone_dates": {"T0+18": "2026-08-20"},
            "reason_codes": ["shadow_daily_count_gap"],
        },
        factor_verdicts=[],
    )

    operations = payload["dispositions"]["operations_observation"]
    assert operations["status"] == "closed_inconclusive"
    assert operations["sample_disposition"] == "insufficient_sample"
    assert operations["evaluator_verdict"] == "insufficient_sample"


def test_acceptance_rejects_unpaired_latest_terminal_artifact(tmp_path: Path) -> None:
    latest = tmp_path / "results" / "data_health" / "stage4_final_evaluation_latest.json"
    latest.parent.mkdir(parents=True, exist_ok=True)
    latest.write_text(
        json.dumps(
            {
                "schema_version": "stage4_final_evaluation_v1",
                "task_type": "stage4_final_evaluation",
                "as_of": "2026-08-20",
                "terminal_status": "closed_inconclusive",
                "maintenance_disposition": "inconclusive",
                "sample_disposition": "insufficient_sample",
                "evaluator_verdict": "insufficient_sample",
                "reason_codes": [],
                "research_only": True,
                "paper_only": True,
                "no_broker": True,
                "is_real_order": False,
                "source_adapter": {
                    "path": "results/stage4_replay_adapter/2026-08-20/adapter_result.json",
                    "sha256": "sha256:" + "a" * 64,
                },
                "evaluation": {
                    "schema_version": "round9s_stage4_final_evaluator_v1",
                    "as_of": "2026-08-20",
                    "verdict": "insufficient_sample",
                },
            }
        ),
        encoding="utf-8",
    )
    payload = build_project_acceptance(
        tmp_path,
        as_of="2026-08-21",
        paper_mechanics={
            "status": "passed",
            "acceptance_passed": True,
            "research_only": True,
            "paper_only": True,
            "no_broker": True,
        },
        daily_chain={"status": "completed", "authoritative": True},
        stage4={"milestone_dates": {"T0+18": "2026-08-20"}},
        factor_verdicts=[],
    )
    assert payload["dispositions"]["operations_observation"]["status"] == (
        "terminal_artifact_pending"
    )


def test_universe_guarded_writer_is_repository_owned() -> None:
    script = Path(__file__).resolve().parents[1] / "scripts" / "refresh_universe_baseline.py"
    text = script.read_text(encoding="utf-8")
    assert "E:\\codex\\claude" not in text
    assert "--project-root" in text
    assert "pre_change_snapshot_hash_mismatch" in text
    assert "restored_after_failure" in text


def test_run_chain_returns_skip_without_creating_a_new_run(tmp_path: Path) -> None:
    _write_run(
        tmp_path,
        run_id="daily-data-chain-20260814T074500Z-completed",
        target_date="2026-08-14",
        status="completed",
    )

    result = run_daily_data_chain(
        tmp_path,
        target_date="2026-08-14",
        network=True,
        retry_from_latest=True,
        trigger="windows_scheduled_task",
    )

    assert result["status"] == "skipped_already_completed"
    assert result["ok"] is True
    assert not (tmp_path / "state" / "daily_data_chain.lock.json").exists()


def _accept_with_chain_dir(root: Path, as_of: str = "2026-08-18") -> dict:
    """Acceptance built from on-disk chain artifacts (no injected chain)."""

    return build_project_acceptance(
        root,
        as_of=as_of,
        paper_mechanics={
            "status": "passed",
            "acceptance_passed": True,
            "research_only": True,
            "paper_only": True,
            "no_broker": True,
        },
        stage4={
            "status": "maintenance_window_incomplete",
            "milestone_dates": {"T0+18": "2026-08-20"},
            "reason_codes": ["shadow_daily_count_gap"],
        },
        factor_verdicts=[{"dimension": "risk", "verdict": "keep"}],
    )


def _write_latest_attempt(root: Path, *, run_id: str, target_date: str, status: str) -> None:
    latest = root / "results" / "data_chain" / "daily_data_chain_latest.json"
    latest.parent.mkdir(parents=True, exist_ok=True)
    latest.write_text(
        json.dumps(
            {
                "run_id": run_id,
                "status": status,
                "target_date": target_date,
                "daily_data_chain_contract_version": 10,
                "authoritative": True,
            }
        ),
        encoding="utf-8",
    )


def test_latest_running_attempt_is_not_masked_by_older_success(tmp_path: Path) -> None:
    """A running newest attempt must not inherit an older run's success."""

    _write_run(
        tmp_path,
        run_id="daily-data-chain-20260814T074500Z-completed",
        target_date="2026-08-14",
        status="completed",
    )
    _write_latest_attempt(
        tmp_path,
        run_id="daily-data-chain-20260818T125918Z-running",
        target_date="2026-08-18",
        status="running",
    )

    payload = _accept_with_chain_dir(tmp_path)
    local = payload["dispositions"]["local_research_tool"]
    evidence = payload["evidence"]

    assert local["status"] == "pending"
    assert local["daily_chain_status"] == "running"
    assert "latest_daily_chain_attempt_still_running" in local["reason_codes"]
    # The older success is still reported, but only as "last success".
    assert evidence["daily_chain"]["status"] == "running"
    assert evidence["daily_chain"]["target_date"] == "2026-08-18"
    assert evidence["daily_chain"]["last_successful"]["status"] == "completed"
    assert evidence["daily_chain"]["last_successful"]["target_date"] == "2026-08-14"
    assert evidence["daily_chain"]["superseded_attempt"]["status"] == "running"
    assert payload["ok"] is False


def test_latest_blocked_attempt_is_not_masked_by_older_success(tmp_path: Path) -> None:
    _write_run(
        tmp_path,
        run_id="daily-data-chain-20260814T074500Z-completed",
        target_date="2026-08-14",
        status="completed",
    )
    _write_latest_attempt(
        tmp_path,
        run_id="daily-data-chain-20260817T103000Z-blocked",
        target_date="2026-08-17",
        status="blocked",
    )

    payload = _accept_with_chain_dir(tmp_path)
    local = payload["dispositions"]["local_research_tool"]

    assert local["status"] == "pending"
    assert local["daily_chain_status"] == "blocked"
    assert "latest_daily_chain_attempt_blocked" in local["reason_codes"]
    assert payload["evidence"]["daily_chain"]["last_successful"]["target_date"] == "2026-08-14"
    assert payload["ok"] is False


def test_latest_failed_attempt_is_not_masked_by_older_success(tmp_path: Path) -> None:
    _write_run(
        tmp_path,
        run_id="daily-data-chain-20260814T074500Z-completed",
        target_date="2026-08-14",
        status="completed",
    )
    _write_latest_attempt(
        tmp_path,
        run_id="daily-data-chain-20260817T103000Z-failed",
        target_date="2026-08-17",
        status="failed",
    )

    payload = _accept_with_chain_dir(tmp_path)
    local = payload["dispositions"]["local_research_tool"]

    assert local["status"] == "pending"
    assert local["daily_chain_status"] == "failed"
    assert "latest_daily_chain_attempt_failed" in local["reason_codes"]
    assert payload["ok"] is False


def test_latest_completed_attempt_is_accepted_and_is_its_own_last_success(tmp_path: Path) -> None:
    _write_latest_attempt(
        tmp_path,
        run_id="daily-data-chain-20260818T074500Z-completed",
        target_date="2026-08-18",
        status="completed",
    )

    payload = _accept_with_chain_dir(tmp_path)
    local = payload["dispositions"]["local_research_tool"]
    evidence = payload["evidence"]

    assert local["status"] == "accepted"
    assert local["daily_chain_status"] == "completed"
    assert local["reason_codes"] == []
    assert local["latest_attempt_is_successful"] is True
    assert evidence["daily_chain"]["last_successful"]["run_id"] == evidence["daily_chain"]["run_id"]
    assert evidence["daily_chain"]["superseded_attempt"] == {}
    assert payload["ok"] is True


def test_acceptance_uses_dynamic_daily_chain_state_over_stale_terminal_snapshot(
    tmp_path: Path, monkeypatch
) -> None:
    """A dead producer with a stale terminal file must remain abandoned."""

    _write_latest_attempt(
        tmp_path,
        run_id="daily-data-chain-20260818T074500Z-completed",
        target_date="2026-08-18",
        status="completed",
    )
    dynamic = {
        "run_id": "daily-data-chain-20260818T125918Z-abandoned",
        "target_date": "2026-08-18",
        "status": "abandoned",
        "authoritative": False,
    }
    monkeypatch.setattr(
        "quantdesk.project_acceptance.latest_daily_data_chain",
        lambda root: dynamic,
    )

    payload = _accept_with_chain_dir(tmp_path)
    local = payload["dispositions"]["local_research_tool"]

    assert local["status"] == "pending"
    assert local["daily_chain_status"] == "abandoned"
    assert "latest_daily_chain_attempt_abandoned" in local["reason_codes"]
    assert payload["evidence"]["daily_chain"]["run_id"] == dynamic["run_id"]


def test_keep_without_formal_alpha_evidence_is_not_validated(tmp_path: Path) -> None:
    payload = build_project_acceptance(
        tmp_path,
        as_of="2026-08-15",
        paper_mechanics={
            "status": "passed",
            "acceptance_passed": True,
            "research_only": True,
            "paper_only": True,
            "no_broker": True,
        },
        daily_chain={"status": "completed", "authoritative": True},
        factor_verdicts=[{"dimension": "trend", "verdict": "keep"}],
    )

    alpha = payload["dispositions"]["strategy_alpha"]
    assert alpha["status"] == "not_validated"
    assert alpha["formal_evidence"] is False


def test_formal_tradable_alpha_evidence_can_validate_alpha(tmp_path: Path) -> None:
    source = tmp_path / "results" / "factor" / "formal.json"
    source.parent.mkdir(parents=True)
    source.write_text('{"evidence":"formal"}', encoding="utf-8")
    source_hash = "sha256:" + hashlib.sha256(source.read_bytes()).hexdigest()
    prereg = tmp_path / "docs" / "internal" / "formal_alpha_preregistration.json"
    prereg.parent.mkdir(parents=True, exist_ok=True)
    prereg.write_text(
        '{"schema_version":"formal_alpha_preregistration_v1"}',
        encoding="utf-8",
    )
    prereg_hash = "sha256:" + hashlib.sha256(prereg.read_bytes()).hexdigest()
    payload = build_project_acceptance(
        tmp_path,
        as_of="2026-08-15",
        paper_mechanics={
            "status": "passed",
            "acceptance_passed": True,
            "research_only": True,
            "paper_only": True,
            "no_broker": True,
        },
        daily_chain={"status": "completed", "authoritative": True},
        factor_verdicts=[
            {
                "dimension": "trend",
                "verdict": "keep",
                "formal_evidence_eligible": True,
                "evidence_schema_version": "formal_tradable_alpha_adjudication_v1",
                "adjudication_id": "alpha-fixture-001",
                "publication_status": "approved",
                "preregistration": {
                    "path": "docs/internal/formal_alpha_preregistration.json",
                    "source_hash": prereg_hash,
                    "schema_version": "formal_alpha_preregistration_v1",
                },
                "provenance": {
                    "source_path": "results/factor/formal.json",
                    "source_hash": source_hash,
                },
                "alpha_evidence": {
                    "evidence_kind": "formal",
                    "tradable": True,
                    "independent_holdout": True,
                    "net_of_costs": True,
                    "tradeability_checked": True,
                },
                "holdout": {"independent": True, "status": "passed"},
                "cost_model": {"net_of_costs": True, "slippage_bps": 5.0},
                "tradability": {"checked": True, "coverage": 1.0},
                "gates": {"holdout": True, "cost": True, "tradability": True},
            }
        ],
    )

    alpha = payload["dispositions"]["strategy_alpha"]
    assert alpha["status"] == "validated"
    assert alpha["formal_evidence"] is True


def test_formal_alpha_requires_matching_provenance_hash(tmp_path: Path) -> None:
    source = tmp_path / "results" / "factor" / "formal.json"
    source.parent.mkdir(parents=True)
    source.write_text('{"evidence":"formal"}', encoding="utf-8")
    prereg = tmp_path / "docs" / "internal" / "formal_alpha_preregistration.json"
    prereg.parent.mkdir(parents=True, exist_ok=True)
    prereg.write_text(
        '{"schema_version":"formal_alpha_preregistration_v1"}',
        encoding="utf-8",
    )
    prereg_hash = "sha256:" + hashlib.sha256(prereg.read_bytes()).hexdigest()
    payload = build_project_acceptance(
        tmp_path,
        as_of="2026-08-15",
        paper_mechanics={
            "status": "passed",
            "acceptance_passed": True,
            "research_only": True,
            "paper_only": True,
            "no_broker": True,
        },
        daily_chain={"status": "completed", "authoritative": True},
        factor_verdicts=[
            {
                "dimension": "trend",
                "verdict": "keep",
                "formal_evidence_eligible": True,
                "evidence_schema_version": "formal_tradable_alpha_adjudication_v1",
                "adjudication_id": "alpha-fixture-001",
                "publication_status": "approved",
                "preregistration": {
                    "path": "docs/internal/formal_alpha_preregistration.json",
                    "source_hash": prereg_hash,
                    "schema_version": "formal_alpha_preregistration_v1",
                },
                "provenance": {
                    "source_path": "results/factor/formal.json",
                    "source_hash": "sha256:" + "a" * 64,
                },
                "alpha_evidence": {
                    "evidence_kind": "formal",
                    "tradable": True,
                    "independent_holdout": True,
                    "net_of_costs": True,
                    "tradeability_checked": True,
                },
                "holdout": {"independent": True, "status": "passed"},
                "cost_model": {"net_of_costs": True, "slippage_bps": 5.0},
                "tradability": {"checked": True, "coverage": 1.0},
                "gates": {"holdout": True, "cost": True, "tradability": True},
            }
        ],
    )

    alpha = payload["dispositions"]["strategy_alpha"]
    assert alpha["status"] == "not_validated"
    assert alpha["formal_evidence"] is False
