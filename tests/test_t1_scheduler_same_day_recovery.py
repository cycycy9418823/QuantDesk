"""Regression cover for the 2026-08-11 scheduler double-failure (T1).

Observed timeline, taken from three real run manifests under
``results/data_chain/runs/``.  Every value asserted below is embedded as a
constant so the test keeps its meaning once those runs are archived.

* ``daily-data-chain-20260811T074502Z-a8e2a29d76`` (15:45,
  ``windows_scheduled_task``) -- ``600789`` hit a provider slice timeout, so
  ``daily_update`` ended ``partial`` with ``failed_symbol_list=['600789']``,
  ``session_expectation`` blocked, and the chain ended ``blocked`` with
  ``retry_symbols=['600789']``.  It had *already* sealed the day's forward
  sector snapshot (``sector_history_archive`` passed, hash
  ``sha256:8e20fa6249...``) and published the day's immutable dated candidate
  batch while its own gate still reported ``upstream_manifest_status='blocked'``.
* ``daily-data-chain-20260811T103003Z-c95ef543f2`` (18:30,
  ``windows_scheduled_task``) -- refetched only ``600789`` and reached 914/914,
  but its candidate refresh correctly refused to overwrite the dated snapshot
  (``['candidate_dated_snapshot_conflict']``), and its sector re-capture
  degraded with ``sector_history_same_day_conflict`` because no recovery
  lineage was available.  Chain ``blocked`` -> scheduler ``LastTaskResult=4``.
* ``daily-data-chain-20260811T125133Z-ba0cb6d7f0`` (20:51, manual) -- same
  append-only conflict, but recovered through ``run_scoped_conflict_recovery``
  and froze ``plan_20260811_for_20260812``.

The gap pinned down here: ``recovery_source_run_id`` was only ever derived from
a stale ``running`` lock, so a predecessor that ended cleanly as ``blocked``
produced no lineage at all.  Recovery must now be available from the latest
same-target-date blocked run, and must stay refused when the day is already
closed, when the target date differs, or when a source's own hashes disagree.
Nothing here may relax the append-only gates: acceptance only unlocks
*verified reuse* of an artifact the source already sealed.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest

from quantdesk import daily_candidate_refresh as refresh
from quantdesk.daily_candidate_refresh import (
    candidate_refresh_gate,
    resolve_chain_bound_formal_candidate_batch,
    run_formal_candidate_refresh,
)
from quantdesk.daily_data_chain import (
    DAILY_DATA_CHAIN_CONTRACT_VERSION,
    RECOVERY_SOURCE_SELECTION_SCHEMA_VERSION,
    _resolve_same_day_recovery_source,
)
from tests.test_daily_candidate_refresh import _passed_manifest, _strategy_payload
from tests.test_daily_data_chain import _write_sealed_sector_recovery_fixture

TARGET_DATE = "2026-08-11"
FIRST_RUN_ID = "daily-data-chain-20260811T074502Z-a8e2a29d76"
RETRY_RUN_ID = "daily-data-chain-20260811T103003Z-c95ef543f2"
MANUAL_RUN_ID = "daily-data-chain-20260811T125133Z-ba0cb6d7f0"
FIRST_STARTED_AT = "2026-08-11T15:45:03"
RETRY_STARTED_AT = "2026-08-11T18:30:03"
MANUAL_STARTED_AT = "2026-08-11T20:51:33"
TIMEOUT_SYMBOL = "600789"
SEALED_SNAPSHOT_HASH = (
    "sha256:8e20fa62493839d68239947e9cce225c7b879a11801d2824ade3c40ff575caf1"
)
UNIVERSE_SHA256 = "5158fb897b9c4eb9"
FIRST_FEATURE_SHA256 = "47a839fa63c236e4"
RETRY_FEATURE_SHA256 = "5966e9b4918356b3"
MANUAL_FEATURE_SHA256 = "c492523bad8073e5"


def _run_manifest(
    run_id: str,
    *,
    status: str,
    started_at: str,
    target_date: str = TARGET_DATE,
    snapshot_hash: str = SEALED_SNAPSHOT_HASH,
    trigger: str = "windows_scheduled_task",
) -> dict[str, Any]:
    """Build the subset of a real run manifest the resolver actually reads."""

    archive: dict[str, Any] = {
        "status": "passed" if snapshot_hash else "degraded",
        "written": bool(snapshot_hash),
        "evidence_eligible": bool(snapshot_hash),
        "reason_codes": [] if snapshot_hash else ["sector_history_same_day_conflict"],
    }
    if snapshot_hash:
        archive["snapshot_hash"] = snapshot_hash
    return {
        "run_id": run_id,
        "task_type": "daily_data_chain",
        "daily_data_chain_contract_version": DAILY_DATA_CHAIN_CONTRACT_VERSION,
        "status": status,
        "trigger": trigger,
        "started_at": started_at,
        "generated_at": started_at,
        "target_date": target_date,
        "authoritative": True,
        "network": True,
        "dry_run": False,
        "universe": {"current_analysis_pool": {"sha256": UNIVERSE_SHA256}},
        "components": {"sector_history_archive": archive},
    }


def _seal_run(
    root: Path,
    manifest: dict[str, Any],
    *,
    attempt_hash: Any = ...,
    write_attempt: bool = True,
) -> Path:
    """Write one run directory exactly as the chain writes it on disk.

    ``write_attempt=False`` keeps an attempt sidecar that a richer fixture
    already wrote (the full sealed snapshot payload) instead of replacing it
    with this helper's manifest-shaped subset.
    """

    run_dir = root / "results" / "data_chain" / "runs" / str(manifest["run_id"])
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    if not write_attempt:
        return run_dir
    archive = dict(manifest["components"]["sector_history_archive"])
    if attempt_hash is not ...:
        archive["snapshot_hash"] = attempt_hash
    (run_dir / "sector_history_archive_attempt.json").write_text(
        json.dumps(archive, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return run_dir


def test_first_round_single_symbol_timeout_becomes_the_retry_recovery_source(
    tmp_path: Path,
) -> None:
    """The 15:45 run ended cleanly ``blocked``; the 18:30 retry must see it."""

    _seal_run(
        tmp_path,
        _run_manifest(FIRST_RUN_ID, status="blocked", started_at=FIRST_STARTED_AT),
    )

    source, selection = _resolve_same_day_recovery_source(
        tmp_path,
        target_date=TARGET_DATE,
        current_run_id=RETRY_RUN_ID,
        retry_from_latest=True,
    )

    assert source == FIRST_RUN_ID
    assert selection["recovery_kind"] == "same_day_blocked_run"
    assert selection["schema_version"] == RECOVERY_SOURCE_SELECTION_SCHEMA_VERSION
    assert selection["source_run_id"] == FIRST_RUN_ID
    assert selection["target_date"] == TARGET_DATE
    assert selection["current_run_id"] == RETRY_RUN_ID
    assert selection["scanned_run_count"] == 1
    assert selection["same_day_run_count"] == 1
    assert selection["eligible_run_ids"] == [FIRST_RUN_ID]
    assert selection["rejected"] == []
    assert selection["reason_codes"] == []


def test_latest_eligible_same_day_blocked_run_wins(tmp_path: Path) -> None:
    """Two sealed blocked predecessors: only the newest may be continued from."""

    _seal_run(
        tmp_path,
        _run_manifest(FIRST_RUN_ID, status="blocked", started_at=FIRST_STARTED_AT),
    )
    _seal_run(
        tmp_path,
        _run_manifest(RETRY_RUN_ID, status="blocked", started_at=RETRY_STARTED_AT),
    )

    source, selection = _resolve_same_day_recovery_source(
        tmp_path,
        target_date=TARGET_DATE,
        current_run_id=MANUAL_RUN_ID,
        retry_from_latest=True,
    )

    assert source == RETRY_RUN_ID
    assert selection["eligible_run_ids"] == [FIRST_RUN_ID, RETRY_RUN_ID]
    assert selection["same_day_run_count"] == 2


def test_completed_same_day_run_closes_the_day_for_recovery(tmp_path: Path) -> None:
    """Once 20:51 completed, no later run may reopen 08-11 through recovery."""

    _seal_run(
        tmp_path,
        _run_manifest(FIRST_RUN_ID, status="blocked", started_at=FIRST_STARTED_AT),
    )
    _seal_run(
        tmp_path,
        _run_manifest(
            MANUAL_RUN_ID,
            status="completed",
            started_at=MANUAL_STARTED_AT,
            snapshot_hash="",
            trigger="manual",
        ),
    )

    source, selection = _resolve_same_day_recovery_source(
        tmp_path,
        target_date=TARGET_DATE,
        current_run_id="daily-data-chain-20260811T140000Z-fourthrun00",
        retry_from_latest=True,
    )

    assert source == ""
    assert selection["recovery_kind"] == "none"
    assert selection["reason_codes"] == ["same_day_run_already_completed"]
    assert selection["completed_run_ids"] == [MANUAL_RUN_ID]
    assert selection["eligible_run_ids"] == []


def test_different_target_date_is_never_a_recovery_source(tmp_path: Path) -> None:
    _seal_run(
        tmp_path,
        _run_manifest(FIRST_RUN_ID, status="blocked", started_at=FIRST_STARTED_AT),
    )

    source, selection = _resolve_same_day_recovery_source(
        tmp_path,
        target_date="2026-08-12",
        current_run_id="daily-data-chain-20260812T074502Z-nextdayrun0",
        retry_from_latest=True,
    )

    assert source == ""
    assert selection["scanned_run_count"] == 1
    assert selection["same_day_run_count"] == 0
    assert selection["reason_codes"] == ["no_same_day_recovery_source"]


def test_source_hash_mismatch_refuses_recovery(tmp_path: Path) -> None:
    """A manifest that disagrees with its own sidecar cannot be trusted."""

    _seal_run(
        tmp_path,
        _run_manifest(FIRST_RUN_ID, status="blocked", started_at=FIRST_STARTED_AT),
        attempt_hash="sha256:" + "0" * 64,
    )

    source, selection = _resolve_same_day_recovery_source(
        tmp_path,
        target_date=TARGET_DATE,
        current_run_id=RETRY_RUN_ID,
        retry_from_latest=True,
    )

    assert source == ""
    assert selection["reason_codes"] == ["no_eligible_same_day_recovery_source"]
    assert selection["rejected"] == [
        {"run_id": FIRST_RUN_ID, "reason_codes": ["source_hash_mismatch"]}
    ]


def test_predecessor_without_sealed_snapshot_is_not_a_recovery_source(
    tmp_path: Path,
) -> None:
    """The 18:30 run sealed nothing, so it offers nothing to continue from."""

    _seal_run(
        tmp_path,
        _run_manifest(
            RETRY_RUN_ID,
            status="blocked",
            started_at=RETRY_STARTED_AT,
            snapshot_hash="",
        ),
    )

    source, selection = _resolve_same_day_recovery_source(
        tmp_path,
        target_date=TARGET_DATE,
        current_run_id=MANUAL_RUN_ID,
        retry_from_latest=True,
    )

    assert source == ""
    assert selection["rejected"] == [
        {
            "run_id": RETRY_RUN_ID,
            "reason_codes": ["source_sector_snapshot_unavailable"],
        }
    ]


def test_abandoned_lock_keeps_priority_and_skips_the_scan(tmp_path: Path) -> None:
    """A run that died mid-flight owns artifacts no later run can republish."""

    _seal_run(
        tmp_path,
        _run_manifest(RETRY_RUN_ID, status="blocked", started_at=RETRY_STARTED_AT),
    )

    source, selection = _resolve_same_day_recovery_source(
        tmp_path,
        target_date=TARGET_DATE,
        current_run_id=MANUAL_RUN_ID,
        abandoned_lock_run_id=FIRST_RUN_ID,
        retry_from_latest=True,
    )

    assert source == FIRST_RUN_ID
    assert selection["recovery_kind"] == "abandoned_lock"
    assert selection["same_day_run_count"] == 0
    assert selection["reason_codes"] == []


def test_recovery_requires_retry_from_latest(tmp_path: Path) -> None:
    """A plain (non-retry) run never inherits another run's lineage."""

    _seal_run(
        tmp_path,
        _run_manifest(FIRST_RUN_ID, status="blocked", started_at=FIRST_STARTED_AT),
    )

    source, selection = _resolve_same_day_recovery_source(
        tmp_path,
        target_date=TARGET_DATE,
        current_run_id=RETRY_RUN_ID,
        retry_from_latest=False,
    )

    assert source == ""
    assert selection["reason_codes"] == ["retry_from_latest_disabled"]
    assert selection["same_day_run_count"] == 0


def test_run_directory_name_must_equal_the_manifest_run_id(tmp_path: Path) -> None:
    manifest = _run_manifest(
        FIRST_RUN_ID, status="blocked", started_at=FIRST_STARTED_AT
    )
    manifest["run_id"] = "daily-data-chain-20260811T074502Z-imposter000"
    run_dir = tmp_path / "results" / "data_chain" / "runs" / FIRST_RUN_ID
    run_dir.mkdir(parents=True)
    (run_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False), encoding="utf-8"
    )
    (run_dir / "sector_history_archive_attempt.json").write_text(
        json.dumps({"snapshot_hash": SEALED_SNAPSHOT_HASH}), encoding="utf-8"
    )

    source, selection = _resolve_same_day_recovery_source(
        tmp_path,
        target_date=TARGET_DATE,
        current_run_id=RETRY_RUN_ID,
        retry_from_latest=True,
    )

    assert source == ""
    assert selection["rejected"] == [
        {"run_id": FIRST_RUN_ID, "reason_codes": ["source_run_id_identity_mismatch"]}
    ]


@pytest.mark.parametrize(
    ("mutation", "reason"),
    [
        ({"status": "partial"}, "source_status_not_recoverable"),
        ({"status": "failed"}, "source_status_not_recoverable"),
        ({"status": "busy"}, "source_status_not_recoverable"),
        ({"authoritative": False}, "source_not_authoritative"),
        ({"dry_run": True}, "source_dry_run_not_recoverable"),
        ({"network": False}, "source_network_not_authorized"),
        (
            {
                "daily_data_chain_contract_version": (
                    DAILY_DATA_CHAIN_CONTRACT_VERSION - 1
                )
            },
            "source_contract_version_mismatch",
        ),
        ({"daily_data_chain_contract_version": None}, "source_contract_version_mismatch"),
    ],
)
def test_recovery_source_gates_are_fail_closed(
    tmp_path: Path,
    mutation: dict[str, Any],
    reason: str,
) -> None:
    manifest = _run_manifest(
        FIRST_RUN_ID, status="blocked", started_at=FIRST_STARTED_AT
    )
    manifest.update(copy.deepcopy(mutation))
    _seal_run(tmp_path, manifest)

    source, selection = _resolve_same_day_recovery_source(
        tmp_path,
        target_date=TARGET_DATE,
        current_run_id=RETRY_RUN_ID,
        retry_from_latest=True,
    )

    assert source == ""
    assert selection["recovery_kind"] == "none"
    assert selection["reason_codes"] == ["no_eligible_same_day_recovery_source"]
    assert reason in selection["rejected"][0]["reason_codes"]


def test_run_directory_without_a_manifest_is_ignored(tmp_path: Path) -> None:
    (tmp_path / "results" / "data_chain" / "runs" / FIRST_RUN_ID).mkdir(parents=True)

    source, selection = _resolve_same_day_recovery_source(
        tmp_path,
        target_date=TARGET_DATE,
        current_run_id=RETRY_RUN_ID,
        retry_from_latest=True,
    )

    assert source == ""
    assert selection["same_day_run_count"] == 0
    assert selection["reason_codes"] == ["no_same_day_recovery_source"]


def test_missing_runs_directory_is_not_an_error(tmp_path: Path) -> None:
    source, selection = _resolve_same_day_recovery_source(
        tmp_path,
        target_date=TARGET_DATE,
        current_run_id=RETRY_RUN_ID,
        retry_from_latest=True,
    )

    assert source == ""
    assert selection["reason_codes"] == ["no_same_day_recovery_source"]


def test_current_run_is_never_its_own_recovery_source(tmp_path: Path) -> None:
    _seal_run(
        tmp_path,
        _run_manifest(FIRST_RUN_ID, status="blocked", started_at=FIRST_STARTED_AT),
    )

    source, selection = _resolve_same_day_recovery_source(
        tmp_path,
        target_date=TARGET_DATE,
        current_run_id=FIRST_RUN_ID,
        retry_from_latest=True,
    )

    assert source == ""
    assert selection["same_day_run_count"] == 0


def _stub_chain_side_effects(chain, monkeypatch) -> None:
    """Neutralise every network/provider edge so the chain stays offline."""

    monkeypatch.setattr(
        chain,
        "build_runtime_preflight",
        lambda *args, **kwargs: {
            "status": "passed",
            "ready": True,
            "selected_executable": "project/.venv/Scripts/python.exe",
            "runtime_source": "project_venv",
            "modules": {"pandas": True, "mootdx": True, "akshare": True},
            "reason_codes": [],
        },
    )
    monkeypatch.setattr(
        chain,
        "update_daily_prices",
        lambda **kwargs: {
            "status": "dry_run",
            "target_date": kwargs["target_date"],
            "symbols_checked": len(kwargs["symbols"]),
            "after": {"fresh_count": 1, "stale_count": 0, "missing_count": 0},
        },
    )
    monkeypatch.setattr(
        chain,
        "collect_intraday_required_symbols",
        lambda **kwargs: {"items": [{"symbol": TIMEOUT_SYMBOL}]},
    )
    monkeypatch.setattr(
        chain,
        "update_intraday_1m_for_symbols",
        lambda *args, **kwargs: {
            "status": "dry_run",
            "updated_symbols": [],
            "unchanged_symbols": [],
            "failed_symbols": [],
        },
    )
    monkeypatch.setattr(
        chain,
        "audit_intraday_batch",
        lambda symbols, target_date, **kwargs: {
            "summary": {
                "complete_session": len(symbols),
                "formal_execution_eligible": len(symbols),
                "missing": 0,
                "partial_session": 0,
                "invalid": 0,
            },
            "items": [],
        },
    )


def test_chain_records_same_day_recovery_lineage_in_the_manifest(
    tmp_path: Path, monkeypatch
) -> None:
    """The resolver must actually be wired into the run manifest, not just exist."""

    from quantdesk import daily_data_chain as chain

    _stub_chain_side_effects(chain, monkeypatch)
    _seal_run(
        tmp_path,
        _run_manifest(FIRST_RUN_ID, status="blocked", started_at=FIRST_STARTED_AT),
    )

    payload = chain.run_daily_data_chain(
        project_root=tmp_path,
        target_date=TARGET_DATE,
        symbols=[TIMEOUT_SYMBOL],
        network=True,
        dry_run=True,
        run_id=RETRY_RUN_ID,
        rebuild_features=False,
        retry_from_latest=True,
        trigger="windows_scheduled_task",
    )

    selection = payload["components"]["recovery_source_selection"]
    assert payload["recovery_source_run_id"] == FIRST_RUN_ID
    assert payload["recovery_kind"] == "same_day_blocked_run"
    assert selection["source_run_id"] == FIRST_RUN_ID
    assert selection["schema_version"] == RECOVERY_SOURCE_SELECTION_SCHEMA_VERSION
    # A dry run may record lineage but must never touch canonical evidence.
    assert payload["authoritative"] is False
    assert payload["published_latest"] is False
    assert payload["components"]["sector_history_archive"]["status"] == "skipped"
    assert payload["components"]["sector_snapshot_recovery"]["attempted"] is False


def test_chain_records_no_lineage_without_a_recoverable_predecessor(
    tmp_path: Path, monkeypatch
) -> None:
    from quantdesk import daily_data_chain as chain

    _stub_chain_side_effects(chain, monkeypatch)

    payload = chain.run_daily_data_chain(
        project_root=tmp_path,
        target_date=TARGET_DATE,
        symbols=[TIMEOUT_SYMBOL],
        network=True,
        dry_run=True,
        run_id=FIRST_RUN_ID,
        rebuild_features=False,
        retry_from_latest=True,
        trigger="windows_scheduled_task",
    )

    selection = payload["components"]["recovery_source_selection"]
    assert payload["recovery_source_run_id"] == ""
    assert payload["recovery_kind"] == "none"
    assert selection["reason_codes"] == ["no_same_day_recovery_source"]


def _round_manifest(run_id: str, *, feature_sha256: str) -> dict[str, Any]:
    """One 08-11 round's chain manifest, differing only in its feature hash.

    Rounds 1 and 2 really did publish different feature hashes
    (``47a839fa...`` then ``5966e9b4...``) because round 2 refetched
    ``600789``.  That divergence is what made the day's dated candidate batch
    stop binding to the retry manifest.
    """

    manifest = _passed_manifest(TARGET_DATE)
    manifest["run_id"] = run_id
    manifest["coverage"]["features"]["output_sha256"] = feature_sha256
    manifest["components"]["features"]["output_sha256"] = feature_sha256
    return manifest


def test_second_round_append_only_conflict_recovers_without_relaxing_the_gate(
    tmp_path: Path,
) -> None:
    """Reproduce round 1 publishing the day, then round 2 hitting the conflict."""

    first = _round_manifest(FIRST_RUN_ID, feature_sha256="4" * 64)
    first_round = run_formal_candidate_refresh(
        project_root=tmp_path,
        data_chain_manifest=first,
        strategy_payload=_strategy_payload(TARGET_DATE),
        captured_at="2026-08-11T15:59:39+08:00",
        apply=True,
    )
    dated_path = (
        tmp_path / "results" / "candidate_refresh" / "daily" / f"{TARGET_DATE}.json"
    )
    dated_before = dated_path.read_bytes()

    assert first_round["status"] == "completed"
    assert first_round["applied"] is True

    retry = _round_manifest(RETRY_RUN_ID, feature_sha256="5" * 64)
    retry_round = run_formal_candidate_refresh(
        project_root=tmp_path,
        data_chain_manifest=retry,
        strategy_payload=_strategy_payload(TARGET_DATE),
        captured_at="2026-08-11T18:44:12+08:00",
        apply=True,
    )

    # The append-only gate held: the day's published batch was not rewritten.
    assert retry_round["status"] == "blocked"
    assert retry_round["applied"] is False
    assert retry_round["reason_codes"] == ["candidate_dated_snapshot_conflict"]
    assert dated_path.read_bytes() == dated_before

    # Pre-fix, the freeze path only ever looked at the dated snapshot, which
    # produced exactly the two reason codes observed in run 2's manifest.
    dated_batch = json.loads(dated_path.read_text(encoding="utf-8"))
    assert refresh._chain_candidate_binding(
        dated_batch, data_chain_manifest=retry
    ) == [
        "formal_candidate_batch_source_run_mismatch",
        "formal_candidate_batch_source_feature_mismatch",
    ]

    retry["components"]["candidate_refresh_gate"] = candidate_refresh_gate(retry)
    retry["components"]["candidate_refresh"] = copy.deepcopy(retry_round)
    resolved = resolve_chain_bound_formal_candidate_batch(
        tmp_path,
        data_chain_manifest=retry,
    )

    assert resolved["status"] == "passed"
    assert resolved["source_mode"] == "run_scoped_conflict_recovery"
    assert resolved["path"] == str(tmp_path / retry_round["staging_path"])
    assert dated_path.read_bytes() == dated_before


@pytest.mark.parametrize(
    ("mutation", "reason"),
    [
        (
            lambda manifest: manifest["components"]["candidate_refresh"].update(
                applied=True
            ),
            "formal_candidate_recovery_reason_invalid",
        ),
        (
            lambda manifest: manifest["components"]["candidate_refresh"].update(
                reason_codes=[
                    "candidate_dated_snapshot_conflict",
                    "candidate_publish_lock_busy",
                ]
            ),
            "formal_candidate_recovery_reason_invalid",
        ),
        (
            lambda manifest: manifest["components"]["candidate_refresh_gate"].update(
                upstream_manifest_status="blocked"
            ),
            "formal_candidate_recovery_gate_invalid",
        ),
        (
            lambda manifest: manifest["components"]["candidate_refresh"].update(
                staging_path=(
                    "results/candidate_refresh/runs/"
                    f"{FIRST_RUN_ID}/formal_candidate_batch.json"
                )
            ),
            "formal_candidate_recovery_staging_path_mismatch",
        ),
    ],
)
def test_conflict_recovery_stays_narrow_for_the_same_day_retry(
    tmp_path: Path,
    mutation,
    reason: str,
) -> None:
    """Recovery must not widen just because a same-day retry needs it."""

    first = _round_manifest(FIRST_RUN_ID, feature_sha256="4" * 64)
    run_formal_candidate_refresh(
        project_root=tmp_path,
        data_chain_manifest=first,
        strategy_payload=_strategy_payload(TARGET_DATE),
        captured_at="2026-08-11T15:59:39+08:00",
        apply=True,
    )
    retry = _round_manifest(RETRY_RUN_ID, feature_sha256="5" * 64)
    retry_round = run_formal_candidate_refresh(
        project_root=tmp_path,
        data_chain_manifest=retry,
        strategy_payload=_strategy_payload(TARGET_DATE),
        captured_at="2026-08-11T18:44:12+08:00",
        apply=True,
    )
    retry["components"]["candidate_refresh_gate"] = candidate_refresh_gate(retry)
    retry["components"]["candidate_refresh"] = copy.deepcopy(retry_round)
    mutation(retry)

    resolved = resolve_chain_bound_formal_candidate_batch(
        tmp_path,
        data_chain_manifest=retry,
    )

    assert resolved["status"] == "blocked"
    assert reason in resolved["reason_codes"]


def test_same_day_blocked_predecessor_reuses_its_sealed_sector_snapshot(
    tmp_path: Path, monkeypatch
) -> None:
    """End-to-end: the 18:30 retry stops degrading the day's sector archive.

    Run 1 sealed ``sha256:8e20fa6249...`` at 15:59; runs 2 and 3 both recorded
    ``sector_history_archive: degraded`` with
    ``['sector_history_same_day_conflict']`` purely because no lineage existed.
    With the same-day blocked run accepted as a source, the retry verifies and
    reuses that snapshot instead of re-capturing it, and still writes nothing.
    """

    from quantdesk import daily_data_chain as chain

    snapshot_path, snapshot = _write_sealed_sector_recovery_fixture(
        tmp_path,
        source_run_id=FIRST_RUN_ID,
        target_date=TARGET_DATE,
    )
    _seal_run(
        tmp_path,
        _run_manifest(
            FIRST_RUN_ID,
            status="blocked",
            started_at=FIRST_STARTED_AT,
            snapshot_hash=str(snapshot["snapshot_hash"]),
        ),
        write_attempt=False,
    )

    refresh_calls: list[bool] = []
    archive_calls: list[bool] = []
    monkeypatch.setattr(chain, "build_default_fetchers", lambda: [])
    monkeypatch.setattr(
        chain,
        "refresh_universe_snapshot",
        lambda *args, **kwargs: {
            "ok": True,
            "status": "completed",
            "formal_eligible": True,
            "quote_date": TARGET_DATE,
            "provider": "fixture",
        },
    )
    monkeypatch.setattr(
        chain,
        "_refresh_sector_snapshot",
        lambda *args, **kwargs: refresh_calls.append(True)
        or {"ok": True, "status": "completed", "network": True},
    )
    monkeypatch.setattr(
        chain,
        "_archive_forward_sector_snapshot",
        lambda *args, **kwargs: archive_calls.append(True)
        or {
            "ok": True,
            "status": "degraded",
            "evidence_eligible": False,
            "pit_eligible": False,
            "written": False,
            "canonical_write": False,
            "reason_codes": ["sector_history_same_day_conflict"],
        },
    )
    monkeypatch.setattr(chain, "resolve_symbols", lambda **kwargs: [{"symbol": "600000"}])
    monkeypatch.setattr(
        chain,
        "collect_intraday_required_symbols",
        lambda **kwargs: {"items": [], "source_manifest": {}},
    )
    monkeypatch.setattr(
        chain,
        "build_runtime_preflight",
        lambda *args, **kwargs: {
            "status": "blocked",
            "ready": False,
            "active_runtime_ready": False,
            "reason_codes": ["fixture_stop_after_archive"],
        },
    )

    payload = chain.run_daily_data_chain(
        project_root=tmp_path,
        target_date=TARGET_DATE,
        network=True,
        dry_run=False,
        run_id=RETRY_RUN_ID,
        rebuild_features=False,
        retry_from_latest=True,
        trigger="windows_scheduled_task",
    )

    sector = payload["components"]["sector_history_archive"]
    assert payload["recovery_kind"] == "same_day_blocked_run"
    assert payload["recovery_source_run_id"] == FIRST_RUN_ID
    assert refresh_calls == []
    assert archive_calls == []
    assert sector["status"] == "passed"
    assert sector["recovery_reuse"] is True
    assert sector["recovery_source_run_id"] == FIRST_RUN_ID
    assert sector["evidence_eligible"] is True
    assert sector["pit_eligible"] is True
    assert sector["written"] is False
    assert sector["canonical_write"] is False
    assert sector["original_snapshot_hash"] == snapshot["snapshot_hash"]
    assert "sector_history_archive_degraded" not in (payload.get("warnings") or [])
    # Reuse is read-only: the sealed snapshot bytes are untouched.
    assert (
        json.loads(snapshot_path.read_text(encoding="utf-8"))["snapshot_hash"]
        == snapshot["snapshot_hash"]
    )
    recovery = payload["components"]["sector_snapshot_recovery"]
    assert recovery["status"] == "reused"
    assert recovery["reason_codes"] == []


def test_completed_predecessor_never_unlocks_sector_reuse(
    tmp_path: Path, monkeypatch
) -> None:
    """Same fixture, but the day already closed: recovery must stay refused."""

    from quantdesk import daily_data_chain as chain

    _, snapshot = _write_sealed_sector_recovery_fixture(
        tmp_path,
        source_run_id=FIRST_RUN_ID,
        target_date=TARGET_DATE,
    )
    _seal_run(
        tmp_path,
        _run_manifest(
            FIRST_RUN_ID,
            status="completed",
            started_at=FIRST_STARTED_AT,
            snapshot_hash=str(snapshot["snapshot_hash"]),
        ),
        write_attempt=False,
    )
    reuse_calls: list[str] = []
    monkeypatch.setattr(
        chain,
        "_reuse_verified_sector_snapshot",
        lambda *args, **kwargs: reuse_calls.append(str(kwargs.get("source_run_id")))
        or (None, {"status": "unexpected", "attempted": True, "reason_codes": []}),
    )

    source, selection = _resolve_same_day_recovery_source(
        tmp_path,
        target_date=TARGET_DATE,
        current_run_id=RETRY_RUN_ID,
        retry_from_latest=True,
    )

    assert source == ""
    assert selection["reason_codes"] == ["same_day_run_already_completed"]
    assert reuse_calls == []
