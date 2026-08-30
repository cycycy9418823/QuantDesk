from __future__ import annotations

import importlib
import json
import threading
from contextlib import contextmanager
from pathlib import Path

import pytest


def _module():
    return importlib.import_module("scripts.run_daily_review_orchestrator")


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def _manifest(run_id: str, target_date: str) -> dict:
    return {
        "authoritative": True,
        "task_type": "daily_data_chain",
        "run_id": run_id,
        "target_date": target_date,
        "status": "blocked",
    }


def _stage4_payload(as_of: str, *, status: str = "maintenance_window_incomplete"):
    return {
        "schema_version": "stage4_maintenance_evidence_v1",
        "task_type": "stage4_maintenance_evidence",
        "as_of": as_of,
        "status": status,
        "reason_codes": ["insufficient_evidence"],
        "account_replay": "not_authorized",
        "synthetic_data_used": False,
        "conclusion_drawn": False,
        "research_only": True,
        "paper_only": True,
        "no_broker": True,
        "is_real_order": False,
    }


def _write_readiness(root: Path) -> None:
    _write_json(
        root / "results" / "data_health" / "round7_forward_pit_readiness_latest.json",
        {
            "milestones": [
                {
                    "relative_to_t0": "T0+8 trading sessions",
                    "calendar_date": "2026-08-06",
                    "status": "achieved",
                },
                {
                    "relative_to_t0": "T0+18 trading sessions",
                    "calendar_date": "2026-08-20",
                    "status": "pending_forward_accumulation",
                },
            ]
        },
    )


def test_main_maps_chain_wait_timeout_to_stable_nonzero(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    module = _module()
    orchestrator_calls: list[str] = []
    @contextmanager
    def timeout_context(**_kwargs):
        raise TimeoutError("daily_data_chain_wait_timeout")
        yield

    monkeypatch.setattr(module, "locked_authoritative_chain_event", timeout_context)
    monkeypatch.setattr(
        module,
        "run_evening_review",
        lambda **_kwargs: orchestrator_calls.append("review"),
    )

    returncode = module.main(["--project-root", str(tmp_path)])

    assert returncode == 4
    assert orchestrator_calls == []
    assert "daily_data_chain_wait_timeout" in capsys.readouterr().err


def test_stage4_exit_zero_with_adapter_error_is_retryable_failure(
    tmp_path: Path,
) -> None:
    module = _module()
    output = tmp_path / "results" / "data_health"
    _write_readiness(tmp_path)
    payload = _stage4_payload("2026-08-06")
    payload["reason_codes"] = ["stage4_replay_adapter_error"]
    payload["adapter_error_type"] = "RuntimeError"
    _write_json(output / "stage4_maintenance_evidence_2026-08-06.json", payload)
    _write_json(output / "stage4_maintenance_evidence_latest.json", payload)

    returncode = module.run_stage4_and_validate(
        ["unused"],
        tmp_path,
        "2026-08-06",
        process_runner=lambda *_args, **_kwargs: type("P", (), {"returncode": 0})(),
    )

    assert returncode != 0


def test_stage4_maintenance_gate_failure_is_truthful_success(tmp_path: Path) -> None:
    module = _module()
    output = tmp_path / "results" / "data_health"
    _write_readiness(tmp_path)
    payload = _stage4_payload("2026-08-06")
    payload["reason_codes"] = [
        "stage4_maintenance_gate_failed:shadow_counts",
        "insufficient_evidence",
    ]
    _write_json(output / "stage4_maintenance_evidence_2026-08-06.json", payload)
    _write_json(output / "stage4_maintenance_evidence_latest.json", payload)

    returncode = module.run_stage4_and_validate(
        ["unused"],
        tmp_path,
        "2026-08-06",
        process_runner=lambda *_args, **_kwargs: type("P", (), {"returncode": 0})(),
    )

    assert returncode == 0


def test_terminal_stage4_exit_zero_without_final_evaluation_is_retryable(
    tmp_path: Path,
) -> None:
    module = _module()
    output = tmp_path / "results" / "data_health"
    _write_readiness(tmp_path)
    payload = _stage4_payload("2026-08-20", status="closed_inconclusive")
    _write_json(output / "stage4_maintenance_evidence_2026-08-20.json", payload)
    _write_json(output / "stage4_maintenance_evidence_latest.json", payload)

    returncode = module.run_stage4_and_validate(
        ["unused"],
        tmp_path,
        "2026-08-20",
        process_runner=lambda *_args, **_kwargs: type("P", (), {"returncode": 0})(),
    )

    assert returncode == 78


def test_post_terminal_retry_reuses_fixed_t0_plus_18_as_of(
    tmp_path: Path, monkeypatch
) -> None:
    module = _module()
    _write_readiness(tmp_path)
    captured: list[list[str]] = []

    monkeypatch.setattr(
        module,
        "validate_stage4_terminal_evidence_pair",
        lambda _root, target_date: (target_date == "2026-08-20", ""),
    )

    def process_runner(command, **_kwargs):
        captured.append(list(command))
        return type("P", (), {"returncode": 0})()

    returncode = module.run_stage4_and_validate(
        ["python", "stage4-maintenance", "--as-of", "2026-08-21"],
        tmp_path,
        "2026-08-21",
        process_runner=process_runner,
    )

    assert returncode == 0
    as_of_index = captured[0].index("--as-of")
    assert captured[0][as_of_index + 1] == "2026-08-20"


def test_real_os_lock_prevents_consuming_stale_latest(tmp_path: Path) -> None:
    from quantdesk.interprocess_lock import interprocess_file_lock

    module = _module()
    latest = tmp_path / "results" / "data_chain" / "daily_data_chain_latest.json"
    metadata = tmp_path / "state" / "daily_data_chain.lock.json"
    os_lock = tmp_path / "state" / "daily_data_chain.lock"
    _write_json(latest, _manifest("old-run", "2026-08-05"))
    _write_json(metadata, {"run_id": "old-run", "status": "released"})
    holder_ready = threading.Event()
    release_holder = threading.Event()
    selected: list[dict] = []

    def hold_new_chain() -> None:
        with interprocess_file_lock(os_lock, timeout_seconds=0):
            _write_json(metadata, {"run_id": "new-run", "pid": 4242, "status": "running"})
            holder_ready.set()
            assert release_holder.wait(5)
            _write_json(latest, _manifest("new-run", "2026-08-06"))
            _write_json(metadata, {"run_id": "new-run", "status": "released"})

    holder = threading.Thread(target=hold_new_chain)
    holder.start()
    assert holder_ready.wait(5)

    def consume() -> None:
        with module.locked_authoritative_chain_event(
            project_root=tmp_path,
            manifest_path=latest,
            timeout_seconds=5,
            poll_seconds=0.01,
        ) as manifest:
            selected.append(manifest)

    consumer = threading.Thread(target=consume)
    consumer.start()
    consumer.join(0.1)
    assert selected == []
    release_holder.set()
    holder.join(5)
    consumer.join(5)

    assert not holder.is_alive()
    assert not consumer.is_alive()
    assert selected[0]["run_id"] == "new-run"


def test_consumer_holds_chain_lock_after_selecting_manifest(tmp_path: Path) -> None:
    from quantdesk.interprocess_lock import interprocess_file_lock

    module = _module()
    latest = tmp_path / "results" / "data_chain" / "daily_data_chain_latest.json"
    metadata = tmp_path / "state" / "daily_data_chain.lock.json"
    os_lock = tmp_path / "state" / "daily_data_chain.lock"
    _write_json(latest, _manifest("run-a", "2026-08-06"))
    _write_json(metadata, {"run_id": "run-a", "status": "released"})

    with module.locked_authoritative_chain_event(
        project_root=tmp_path,
        manifest_path=latest,
        timeout_seconds=0,
    ) as selected:
        assert selected["run_id"] == "run-a"
        with pytest.raises(TimeoutError, match="interprocess_file_lock_timeout"):
            with interprocess_file_lock(os_lock, timeout_seconds=0):
                pass


def test_chain_lock_context_does_not_relabel_body_timeout(tmp_path: Path) -> None:
    module = _module()
    latest = tmp_path / "results" / "data_chain" / "daily_data_chain_latest.json"
    metadata = tmp_path / "state" / "daily_data_chain.lock.json"
    _write_json(latest, _manifest("run-a", "2026-08-06"))
    _write_json(metadata, {"run_id": "run-a", "status": "released"})

    with pytest.raises(TimeoutError, match="child_operation_timeout"):
        with module.locked_authoritative_chain_event(
            project_root=tmp_path,
            manifest_path=latest,
            timeout_seconds=0,
        ):
            raise TimeoutError("child_operation_timeout")


def test_main_runs_review_inside_authoritative_chain_lock(
    tmp_path: Path, monkeypatch
) -> None:
    module = _module()
    inside = False

    @contextmanager
    def fake_locked_event(**_kwargs):
        nonlocal inside
        inside = True
        try:
            yield _manifest("run-a", "2026-08-06")
        finally:
            inside = False

    def fake_review(**_kwargs):
        assert inside is True
        return {"state": "daily_completed", "event_key": "event"}

    monkeypatch.setattr(module, "locked_authoritative_chain_event", fake_locked_event)
    monkeypatch.setattr(module, "run_evening_review", fake_review)

    assert module.main(["--project-root", str(tmp_path)]) == 0
    assert inside is False


def test_candidate_tracking_validation_consumes_existing_batch_only(
    tmp_path: Path, monkeypatch
) -> None:
    module = _module()
    calls: list[str] = []
    observed_provenance: list[dict[str, str]] = []

    import quantdesk.candidate_tracking as tracking
    import quantdesk.evidence_freshness as freshness

    monkeypatch.setattr(
        tracking,
        "materialize_formal_candidate_tracking",
        lambda root, **kwargs: calls.append("materialize")
        or {
            "status": "completed",
            "source_run_id": "daily-data-chain-20260819T103626Z-fixture",
            "source_hash": "fixture-hash",
            "artifact_hash": "artifact-hash",
        },
    )
    monkeypatch.setattr(
        tracking,
        "build_candidate_performance",
        lambda root, *, provenance=None: calls.append("performance")
        or observed_provenance.append(dict(provenance or {})),
    )
    monkeypatch.setattr(
        tracking,
        "build_tracking_summary",
        lambda root, *, provenance=None: calls.append("summary")
        or observed_provenance.append(dict(provenance or {})),
    )
    monkeypatch.setattr(
        freshness,
        "refresh_persisted_evidence_freshness",
        lambda root, **kwargs: calls.append("freshness")
        or observed_provenance.append(dict(kwargs.get("provenance") or {})),
    )

    assert (
        module.run_candidate_tracking_and_validate(
            tmp_path,
            "2026-08-19",
            "daily-data-chain-20260819T103626Z-fixture",
        )
        == 0
    )
    assert calls == ["materialize", "performance", "summary", "freshness"]
    assert len(observed_provenance) == 3
    assert all(
        item["trigger"] == "scheduled_daily_review"
        and item["authoritative_chain_run_id"]
        == "daily-data-chain-20260819T103626Z-fixture"
        and item["source_hash"] == "fixture-hash"
        for item in observed_provenance
    )
