"""Acceptance must never hide a current unresolved chain attempt behind an old success.

Two independent guards live here:

* `_daily_chain_states` / `build_project_acceptance` must expose `latest_attempt`
  and `last_successful` separately, so a `running`, `blocked`, or `failed`
  newest run is reported as-is instead of being replaced by an older
  `completed` manifest.
* `strategy_alpha` may only read `validated` from a dedicated, provenance-bound
  alpha evidence contract -- never from a routine factor-panel `keep`.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from quantdesk.daily_data_chain import DAILY_DATA_CHAIN_CONTRACT_VERSION


def _mechanics() -> dict:
    return {
        "status": "passed",
        "acceptance_passed": True,
        "research_only": True,
        "paper_only": True,
        "no_broker": True,
    }


def _write_run(root: Path, *, run_id: str, target_date: str, status: str) -> None:
    """Seal a run manifest into the append-only run archive."""

    run_dir = root / "results" / "data_chain" / "runs" / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "manifest.json").write_text(
        json.dumps(
            {
                "run_id": run_id,
                "status": status,
                "target_date": target_date,
                "authoritative": True,
                "completed_at": f"{target_date}T17:00:00",
                "daily_data_chain_contract_version": DAILY_DATA_CHAIN_CONTRACT_VERSION,
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


def _write_latest_attempt(root: Path, *, run_id: str, target_date: str, status: str) -> None:
    """Publish the newest attempt at the current contract version.

    The contract version matters: a manifest from an older contract is read back
    as `superseded_contract`, which would mask the status under test.
    """

    latest = root / "results" / "data_chain" / "daily_data_chain_latest.json"
    latest.parent.mkdir(parents=True, exist_ok=True)
    latest.write_text(
        json.dumps(
            {
                "run_id": run_id,
                "status": status,
                "target_date": target_date,
                "authoritative": True,
                "daily_data_chain_contract_version": DAILY_DATA_CHAIN_CONTRACT_VERSION,
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


def _accept_with_chain_dir(root: Path, as_of: str = "2026-08-18") -> dict:
    from quantdesk.project_acceptance import build_project_acceptance

    return build_project_acceptance(
        root,
        as_of=as_of,
        paper_mechanics=_mechanics(),
        stage4={},
        factor_verdicts=[],
    )


def test_latest_running_attempt_is_not_masked_by_older_success(tmp_path: Path) -> None:
    """The bug: an old completed run stood in for a live `running` attempt."""

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
    assert local["latest_attempt_is_successful"] is False
    assert "latest_daily_chain_attempt_still_running" in local["reason_codes"]
    assert (
        "older_successful_run_does_not_supersede_current_attempt"
        in local["reason_codes"]
    )
    assert evidence["daily_chain"]["status"] == "running"
    assert evidence["daily_chain"]["target_date"] == "2026-08-18"
    assert evidence["daily_chain_last_successful"]["status"] == "completed"
    assert evidence["daily_chain_last_successful"]["target_date"] == "2026-08-14"
    assert evidence["daily_chain_superseded_attempt"]["status"] == "running"
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
    assert payload["evidence"]["daily_chain_last_successful"]["target_date"] == "2026-08-14"
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


def test_latest_completed_attempt_is_accepted_and_is_its_own_last_success(
    tmp_path: Path,
) -> None:
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
    assert evidence["daily_chain_last_successful"]["run_id"] == evidence["daily_chain"]["run_id"]
    assert evidence["daily_chain_superseded_attempt"] == {}
    assert payload["ok"] is True


def _alpha_verdict(root: Path, **overrides: object) -> dict:
    """A fully-compliant tradable-alpha verdict; each override weakens one field.

    The gate binds the verdict to a real on-disk artifact by sha256, so the
    fixture writes that artifact and declares its true hash.
    """

    source = root / "results" / "alpha_adjudication" / "alpha_latest.json"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_bytes(b'{"alpha":"fixture"}')
    digest = "sha256:" + hashlib.sha256(source.read_bytes()).hexdigest()
    prereg = root / "docs" / "internal" / "formal_alpha_preregistration.json"
    prereg.parent.mkdir(parents=True, exist_ok=True)
    prereg.write_text(
        '{"schema_version":"formal_alpha_preregistration_v1"}',
        encoding="utf-8",
    )
    prereg_digest = "sha256:" + hashlib.sha256(prereg.read_bytes()).hexdigest()

    payload: dict = {
        "dimension": "momentum",
        "verdict": "keep",
        "formal_evidence_eligible": True,
        "evidence_schema_version": "formal_tradable_alpha_adjudication_v1",
        "adjudication_id": "alpha-fixture-001",
        "publication_status": "approved",
        "preregistration": {
            "path": "docs/internal/formal_alpha_preregistration.json",
            "source_hash": prereg_digest,
            "schema_version": "formal_alpha_preregistration_v1",
        },
        "provenance": {
            "source_path": "results/alpha_adjudication/alpha_latest.json",
            "source_hash": digest,
        },
        "holdout": {"independent": True, "status": "passed"},
        "cost_model": {"net_of_costs": True, "slippage_bps": 5.0},
        "tradability": {"checked": True, "coverage": 1.0},
        "gates": {"holdout": True, "cost": True, "tradability": True},
        "alpha_evidence": {
            "evidence_kind": "formal",
            "tradable": True,
            "independent_holdout": True,
            "net_of_costs": True,
            "tradeability_checked": True,
        },
    }
    payload.update(overrides)
    return payload


def _alpha_status(tmp_path: Path, verdicts: list[dict]) -> dict:
    from quantdesk.project_acceptance import build_project_acceptance

    payload = build_project_acceptance(
        tmp_path,
        as_of="2026-08-18",
        paper_mechanics=_mechanics(),
        daily_chain={
            "status": "completed",
            "authoritative": True,
            "run_id": "daily-data-chain-test",
            "target_date": "2026-08-18",
        },
        stage4={},
        factor_verdicts=verdicts,
    )
    return payload["dispositions"]["strategy_alpha"]


def test_factor_panel_keep_does_not_validate_alpha(tmp_path: Path) -> None:
    """A routine factor-panel `keep` must not promote alpha to validated."""

    alpha = _alpha_status(
        tmp_path,
        [
            {"dimension": "momentum", "verdict": "keep"},
            {"dimension": "trend", "verdict": "keep"},
        ],
    )

    assert alpha["status"] == "not_validated"
    assert alpha["formal_evidence"] is False
    assert "no_formal_tradable_alpha_signal_passed_adjudication" in alpha["reason_codes"]


def test_alpha_requires_every_provenance_field(tmp_path: Path) -> None:
    """Weakening any single required field must keep alpha `not_validated`."""

    weakened_variants = [
        _alpha_verdict(tmp_path, verdict="kill"),
        _alpha_verdict(tmp_path, dimension="risk"),
        _alpha_verdict(tmp_path, dimension=""),
        _alpha_verdict(tmp_path, formal_evidence_eligible=False),
        _alpha_verdict(tmp_path, evidence_schema_version="other_v9"),
        _alpha_verdict(tmp_path, adjudication_id=""),
        _alpha_verdict(tmp_path, publication_status="draft"),
        _alpha_verdict(tmp_path, preregistration={}),
        _alpha_verdict(
            tmp_path,
            preregistration={
                "path": "docs/internal/formal_alpha_preregistration.json",
                "source_hash": "sha256:" + "0" * 64,
                "schema_version": "formal_alpha_preregistration_v1",
            },
        ),
        _alpha_verdict(tmp_path, provenance={}),
        _alpha_verdict(
            tmp_path,
            provenance={
                "source_path": "results/alpha_adjudication/alpha_latest.json",
                "source_hash": "sha256:" + "0" * 64,
            },
        ),
        _alpha_verdict(
            tmp_path,
            provenance={
                "source_path": "results/alpha_adjudication/missing.json",
                "source_hash": "sha256:" + "0" * 64,
            },
        ),
        _alpha_verdict(tmp_path, alpha_evidence={"evidence_kind": "formal", "tradable": False}),
        _alpha_verdict(tmp_path, alpha_evidence={"evidence_kind": "informal", "tradable": True}),
        _alpha_verdict(tmp_path, holdout={"independent": False, "status": "passed"}),
        _alpha_verdict(tmp_path, cost_model={"net_of_costs": False, "slippage_bps": 5.0}),
        _alpha_verdict(tmp_path, tradability={"checked": False, "coverage": 1.0}),
        _alpha_verdict(tmp_path, tradability={"checked": True, "coverage": "not-a-number"}),
        _alpha_verdict(tmp_path, gates={"holdout": True, "cost": False, "tradability": True}),
    ]

    for weakened in weakened_variants:
        assert _alpha_status(tmp_path, [weakened])["status"] == "not_validated"


def test_dedicated_provenance_bound_alpha_evidence_validates_alpha(tmp_path: Path) -> None:
    """The positive control: the explicit writer path still works."""

    alpha = _alpha_status(tmp_path, [_alpha_verdict(tmp_path)])

    assert alpha["status"] == "validated"
    assert alpha["formal_evidence"] is True
    assert alpha["reason_codes"] == []


def test_real_repo_factor_verdicts_keep_alpha_not_validated() -> None:
    """Guard against the live round22/round23 artifacts flipping alpha."""

    from quantdesk.project_acceptance import (
        _is_formal_tradable_alpha_verdict,
        _load_factor_verdicts,
    )

    root = Path(__file__).resolve().parents[1]
    verdicts = _load_factor_verdicts(root)

    assert verdicts, "expected the real factor adjudication artifacts to be present"
    assert not any(
        _is_formal_tradable_alpha_verdict(item, project_root=root) for item in verdicts
    )
