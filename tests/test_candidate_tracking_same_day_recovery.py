"""A verified retry batch must reach both formal and compatibility tracking."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from quantdesk import candidate_tracking as compatibility
from quantdesk import daily_candidate_refresh as refresh
from quantdesk import formal_candidate_tracking as formal
from tests.test_formal_candidate_tracking_materializer import (
    _passed_manifest, _strategy_payload, _write_daily_history,
)

DAY = "2026-08-03"
RETRY = "daily-data-chain-20260803T120000Z-retry"


def _retry_fixture(root: Path) -> tuple[dict, Path, Path]:
    first = _passed_manifest(DAY)
    batch = refresh.build_formal_candidate_batch(
        data_chain_manifest=first, strategy_payload=_strategy_payload(DAY),
        captured_at=f"{DAY}T18:00:00+08:00",
    )
    assert refresh.publish_formal_candidate_batch(root, batch)["applied"]
    retry = _passed_manifest(DAY)
    retry["run_id"] = RETRY
    published = refresh.run_formal_candidate_refresh(
        project_root=root, data_chain_manifest=retry,
        strategy_payload=_strategy_payload(DAY),
        captured_at=f"{DAY}T20:00:00+08:00", apply=True,
    )
    assert published["reason_codes"] == ["candidate_dated_snapshot_conflict"]
    batch = json.loads((root / published["staging_path"]).read_text(encoding="utf-8"))
    retry["components"]["candidate_refresh_gate"] = refresh.candidate_refresh_gate(retry)
    retry["components"]["candidate_refresh"] = published
    archive = root / "results/data_chain/runs" / RETRY / "manifest.json"
    archive.parent.mkdir(parents=True)
    archive.write_text(json.dumps(retry), encoding="utf-8")
    _write_daily_history(root, rows=[(DAY, 10.0), ("2026-08-04", 11.0)])
    return batch, archive, root / published["staging_path"]


@pytest.mark.parametrize("module", [formal, compatibility])
def test_verified_retry_reaches_tracking_without_rewriting_candidates(tmp_path, module):
    batch, archive, staged = _retry_fixture(tmp_path)
    dated = tmp_path / "results/candidate_refresh/daily" / f"{DAY}.json"
    protected = {path: path.read_bytes() for path in (dated, staged, archive)}
    result = module.materialize_formal_candidate_tracking(
        tmp_path, target_date=DAY, authoritative_chain_run_id=RETRY,
    )
    assert result["status"] in {"completed", "completed_with_warnings"}
    assert result["authoritative_chain_run_id"] == RETRY
    assert result["batch_provenance_hash"] == batch["provenance"]["hash"]
    assert result["candidate_count"] == 1
    assert result["source_path"] == staged.relative_to(tmp_path).as_posix()
    assert result["source_mode"] == "run_scoped_conflict_recovery"
    assert result["entries"][0]["horizons"]["1d"]["return"] == pytest.approx(0.1)
    for path, data in protected.items():
        assert path.read_bytes() == data
    again = module.materialize_formal_candidate_tracking(
        tmp_path, target_date=DAY, authoritative_chain_run_id=RETRY,
    )
    assert again["publish_status"] == "idempotent"
    if module is compatibility:
        rows = compatibility.read_csv_rows(tmp_path / "data/tracking/candidate_ledger.csv")
        assert len(rows) == 1
        assert rows[0]["source_run_id"] == RETRY
        assert rows[0]["source_file"] == staged.relative_to(tmp_path).as_posix()


@pytest.mark.parametrize("module", [formal, compatibility])
@pytest.mark.parametrize("fault", [
    "archive_missing", "archive_invalid", "archive_wrong_run", "archive_wrong_date",
    "unfinished_chain", "not_authoritative", "extra_failure", "wrong_staging_path",
    "dated_missing", "dated_tampered", "staged_tampered", "staged_missing",
])
def test_recovery_refuses_unverified_evidence_and_writes_nothing(tmp_path, module, fault):
    _, archive, staged = _retry_fixture(tmp_path)
    dated = tmp_path / "results/candidate_refresh/daily" / f"{DAY}.json"
    manifest = json.loads(archive.read_text(encoding="utf-8"))
    if fault == "archive_missing":
        archive.unlink()
    elif fault == "archive_invalid":
        archive.write_text("{broken", encoding="utf-8")
    elif fault == "dated_missing":
        dated.unlink()
    elif fault in {"dated_tampered", "staged_tampered"}:
        path = dated if fault == "dated_tampered" else staged
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["candidates"][0]["symbol"] = "600009"
        path.write_text(json.dumps(payload), encoding="utf-8")
    elif fault == "staged_missing":
        staged.unlink()
    else:
        if fault == "archive_wrong_run":
            manifest["run_id"] = "foreign-run"
        elif fault == "archive_wrong_date":
            manifest["target_date"] = "2026-08-04"
        elif fault == "unfinished_chain":
            manifest["status"] = "running"
        elif fault == "not_authoritative":
            manifest["authoritative"] = False
        elif fault == "extra_failure":
            manifest["components"]["candidate_refresh"]["reason_codes"].append("lock_busy")
        elif fault == "wrong_staging_path":
            manifest["components"]["candidate_refresh"]["staging_path"] = "../outside.json"
        archive.write_text(json.dumps(manifest), encoding="utf-8")
    # A valid mutable pointer must not rescue missing/damaged archive evidence.
    latest = tmp_path / "results/data_chain/daily_data_chain_latest.json"
    latest.write_text(json.dumps(_passed_manifest(DAY)), encoding="utf-8")
    result = module.materialize_formal_candidate_tracking(
        tmp_path, target_date=DAY, authoritative_chain_run_id=RETRY,
    )
    assert result["status"] == "blocked"
    assert not (tmp_path / "results/candidate_tracking").exists()
    assert not (tmp_path / "data/tracking").exists()


@pytest.mark.parametrize("module", [formal, compatibility])
def test_recovery_never_replaces_existing_tracking_history(tmp_path, module):
    _retry_fixture(tmp_path)
    first_run = _passed_manifest(DAY)["run_id"]
    first = module.materialize_formal_candidate_tracking(
        tmp_path, target_date=DAY, authoritative_chain_run_id=first_run,
    )
    assert first["applied"] is True
    paths = list((tmp_path / "results/candidate_tracking").rglob("*.json"))
    paths += list((tmp_path / "data/tracking").glob("*.csv"))
    before = {path: path.read_bytes() for path in paths}
    result = module.materialize_formal_candidate_tracking(
        tmp_path, target_date=DAY, authoritative_chain_run_id=RETRY,
    )
    assert result["status"] == "blocked"
    assert "candidate_tracking_dated_snapshot_conflict" in result["reason_codes"]
    assert all(path.read_bytes() == content for path, content in before.items())


def test_projection_refuses_batch_changed_after_formal_publication(tmp_path, monkeypatch):
    batch, _, _ = _retry_fixture(tmp_path)
    original = formal.load_tracking_formal_candidate_batch
    calls = 0

    def changed(*args, **kwargs):
        nonlocal calls
        calls += 1
        resolved, metadata = original(*args, **kwargs)
        if calls == 2:
            resolved["provenance"]["hash"] = "f" * 64
        return resolved, metadata

    monkeypatch.setattr(formal, "load_tracking_formal_candidate_batch", changed)
    result = compatibility.materialize_formal_candidate_tracking(
        tmp_path, target_date=DAY, authoritative_chain_run_id=RETRY,
    )
    assert result["reason_codes"] == ["formal_candidate_tracking_batch_changed"]
    assert not (tmp_path / "data/tracking").exists()
    artifact = json.loads((tmp_path / "results/candidate_tracking/daily" / f"{DAY}.json").read_text())
    assert artifact["batch_provenance_hash"] == batch["provenance"]["hash"]


def test_review_tracking_step_builds_real_views_for_verified_retry(tmp_path):
    from scripts.run_daily_review_orchestrator import run_candidate_tracking_and_validate

    batch, _, _ = _retry_fixture(tmp_path)
    result = run_candidate_tracking_and_validate(tmp_path, DAY, RETRY, detailed=True)
    assert result == 0
    artifact = json.loads((tmp_path / "results/candidate_tracking/latest.json").read_text())
    assert artifact["batch_provenance_hash"] == batch["provenance"]["hash"]
    assert artifact["authoritative_chain_run_id"] == RETRY
