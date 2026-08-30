"""Round 8 裁定十七 — sector-membership observation-timestamp alignment fix.

Root cause proven in Part A forensics: the same-day observation gate read the
membership *source file's* ``updated_at`` column as if it were the per-member
observation moment.  On 2026-07-23 the chain fell back to the static manual
theme map, whose ``updated_at`` column is a single constant
``2026-07-22 11:57:48`` (when that manual file was last rebuilt) across all
5,527 rows — so all 5,199 usable members were mis-graded ``non_target`` and the
snapshot degraded, even though we captured the live cross-section that very day.

The fix: the observation moment is ``captured_at`` (a single event that applies
uniformly to the whole retained cross-section), not the source column.  The
same-day *standard* is unchanged (裁定十七 red line) — it is now measured against
the true capture moment.  Source-side revision freshness remains a separate
concern via the mtime gate and the new ``embedded_source_update_days``
transparency field.

Every test runs in an isolated ``tmp_path`` root; no network, no ``--apply``, no
canonical/state write.  ``_local_today`` is frozen so the same-day guard is
deterministic regardless of when the suite runs.
"""

from __future__ import annotations

import hashlib
import os
from datetime import date, datetime
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
FROZEN_DAY = date(2026, 7, 23)
FROZEN_CAPTURE = "2026-07-23T18:32:16"


@pytest.fixture(autouse=True)
def _freeze_archive_day(monkeypatch) -> None:
    import quantdesk.data_truth.sector_history_archive as archive

    monkeypatch.setattr(archive, "_local_today", lambda: FROZEN_DAY)


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _pin_mtime(path: Path, when: str) -> None:
    stamp = datetime.fromisoformat(when).timestamp()
    os.utime(path, (stamp, stamp))


def _refresh_evidence(root: Path, *, board_count: int = 2) -> dict:
    theme_path = root / "data" / "universe" / "stock_theme_map.csv"
    board_path = root / "data" / "cache" / "industry_board_latest.csv"
    return {
        "sector_refresh": {
            "ok": True,
            "status": "completed",
            "network": True,
            "source": "fixture",
            "board_count": board_count,
            "cache_path": str(board_path.resolve()),
            "cache_sha256": f"sha256:{hashlib.sha256(board_path.read_bytes()).hexdigest()}",
            "captured_at": FROZEN_CAPTURE,
        },
        "membership_observation": {
            "ok": True,
            "status": "completed",
            "source": "fixture_membership_provider",
            "path": str(theme_path.resolve()),
            "sha256": f"sha256:{hashlib.sha256(theme_path.read_bytes()).hexdigest()}",
            "captured_at": FROZEN_CAPTURE,
            "row_count": sum(1 for _ in theme_path.open(encoding="utf-8")) - 1,
        },
    }


# ---------------------------------------------------------------------------
# Part A: the 07-23 reproduction — stale source ``updated_at``, same-day capture
# ---------------------------------------------------------------------------

def test_stale_source_updated_at_no_longer_fabricates_non_target(tmp_path: Path) -> None:
    """The exact 07-23 defect: source column dated the *previous* day, but the
    cross-section was captured today.  Post-fix it must NOT be flagged
    ``non_target`` and must remain eligible (T0-capable)."""
    from quantdesk.data_truth.sector_history_archive import (
        archive_forward_sector_snapshot,
    )

    # source updated_at = 07-22 (yesterday), like the real manual theme map.
    theme = _write(
        tmp_path / "data" / "universe" / "stock_theme_map.csv",
        "symbol,name,sector,industry,source,updated_at\n"
        "000001,Alpha,Finance,Bank,manual_import,2026-07-22 11:57:48\n"
        "000002,Beta,Energy,Oil,manual_import,2026-07-22 11:57:48\n",
    )
    board = _write(
        tmp_path / "data" / "cache" / "industry_board_latest.csv",
        "name,pct_chg,amount,leading_stock\n"
        "Finance,1.2,100,Alpha\n"
        "Energy,-0.4,80,Beta\n",
    )
    # Both files were touched today (the chain refreshed them this run).
    _pin_mtime(theme, FROZEN_CAPTURE)
    _pin_mtime(board, FROZEN_CAPTURE)

    result = archive_forward_sector_snapshot(
        tmp_path,
        target_date="2026-07-23",
        captured_at=FROZEN_CAPTURE,
        source_override=_refresh_evidence(tmp_path),
    )

    assert result["membership_non_target_observation_timestamp_count"] == 0
    assert result["membership_missing_observation_timestamp_count"] == 0
    assert "sector_membership_observation_not_same_day" not in result["reason_codes"]
    assert result["aligned_membership_count"] == 2
    assert result["status"] == "passed"
    assert result["written"] is True
    # source-side revision date is retained as transparency evidence, not graded
    assert all(
        row["source_updated_at"] == "2026-07-22 11:57:48"
        for row in result["sector_membership"]
    )
    assert all(
        row["observed_at"] == FROZEN_CAPTURE for row in result["sector_membership"]
    )


def test_observation_day_measured_against_capture_not_source(tmp_path: Path) -> None:
    """``embedded_observation_days`` must carry the capture day, and
    ``embedded_source_update_days`` must transparently carry the source column
    without conflating the two."""
    from quantdesk.data_truth.sector_history_archive import _membership_rows

    theme = _write(
        tmp_path / "theme.csv",
        "symbol,name,sector,industry,source,updated_at\n"
        "000001,Alpha,Finance,Bank,manual_import,2026-07-01 09:00:00\n",
    )
    rows, meta = _membership_rows(
        theme, captured_at=FROZEN_CAPTURE, target_date="2026-07-23"
    )
    assert meta["embedded_observation_days"] == ["2026-07-23"]
    assert meta["embedded_source_update_days"] == ["2026-07-01"]
    assert meta["non_target_observation_timestamp_count"] == 0
    assert meta["missing_observation_timestamp_count"] == 0
    assert rows[0]["observed_at"] == FROZEN_CAPTURE
    assert rows[0]["source_updated_at"] == "2026-07-01 09:00:00"


# ---------------------------------------------------------------------------
# Part A: capture-moment boundary cases (23:59 / next-day / mismatch)
# ---------------------------------------------------------------------------

def test_capture_at_end_of_target_day_is_same_day(tmp_path: Path) -> None:
    from quantdesk.data_truth.sector_history_archive import _membership_rows

    theme = _write(
        tmp_path / "theme.csv",
        "symbol,name,sector,industry,source,updated_at\n"
        "000001,Alpha,Finance,Bank,manual_import,2026-07-22 11:57:48\n",
    )
    _, meta = _membership_rows(
        theme, captured_at="2026-07-23T23:59:59", target_date="2026-07-23"
    )
    assert meta["embedded_observation_days"] == ["2026-07-23"]
    assert meta["non_target_observation_timestamp_count"] == 0


def test_capture_after_midnight_is_non_target(tmp_path: Path) -> None:
    """If capture rolled past midnight, the observation day no longer matches the
    target date — the same-day standard correctly flags it non_target."""
    from quantdesk.data_truth.sector_history_archive import _membership_rows

    theme = _write(
        tmp_path / "theme.csv",
        "symbol,name,sector,industry,source,updated_at\n"
        "000001,Alpha,Finance,Bank,manual_import,2026-07-22 11:57:48\n",
    )
    _, meta = _membership_rows(
        theme, captured_at="2026-07-24T00:00:01", target_date="2026-07-23"
    )
    assert meta["embedded_observation_days"] == ["2026-07-24"]
    assert meta["non_target_observation_timestamp_count"] == 1


def test_missing_capture_timestamp_flags_missing_not_non_target(tmp_path: Path) -> None:
    from quantdesk.data_truth.sector_history_archive import _membership_rows

    theme = _write(
        tmp_path / "theme.csv",
        "symbol,name,sector,industry,source,updated_at\n"
        "000001,Alpha,Finance,Bank,manual_import,2026-07-22 11:57:48\n",
    )
    rows, meta = _membership_rows(
        theme, captured_at="", target_date="2026-07-23"
    )
    assert meta["missing_observation_timestamp_count"] == len(rows) == 1
    assert meta["non_target_observation_timestamp_count"] == 0
    assert meta["embedded_observation_days"] == []


def test_archive_captured_after_midnight_degrades(tmp_path: Path) -> None:
    """End-to-end: a capture stamped the day after the target degrades and writes
    nothing, proving the same-day gate is intact against the true capture moment."""
    from quantdesk.data_truth.sector_history_archive import (
        archive_forward_sector_snapshot,
    )

    theme = _write(
        tmp_path / "data" / "universe" / "stock_theme_map.csv",
        "symbol,name,sector,industry,source,updated_at\n"
        "000001,Alpha,Finance,Bank,manual_import,2026-07-22 11:57:48\n",
    )
    board = _write(
        tmp_path / "data" / "cache" / "industry_board_latest.csv",
        "name,pct_chg\nFinance,1.2\n",
    )
    _pin_mtime(theme, "2026-07-23T23:59:00")
    _pin_mtime(board, "2026-07-23T23:59:00")

    result = archive_forward_sector_snapshot(
        tmp_path,
        target_date="2026-07-23",
        captured_at="2026-07-24T00:01:00",
        source_override=_refresh_evidence(tmp_path, board_count=1),
    )
    assert result["written"] is False
    assert "sector_membership_observation_not_same_day" in result["reason_codes"]


# ---------------------------------------------------------------------------
# Part A: mixed-taxonomy still rejected (regression guard, unchanged standard)
# ---------------------------------------------------------------------------

def test_mixed_source_taxonomy_still_rejected(tmp_path: Path) -> None:
    """The fix must not weaken the taxonomy-alignment gate: members in a board
    taxonomy the ranking file does not contain remain unaligned/degraded."""
    from quantdesk.data_truth.sector_history_archive import (
        archive_forward_sector_snapshot,
    )

    theme = _write(
        tmp_path / "data" / "universe" / "stock_theme_map.csv",
        "symbol,name,sector,industry,source,updated_at\n"
        "000001,Alpha,Taxonomy A,Industry A,manual_import,2026-07-22 11:57:48\n",
    )
    board = _write(
        tmp_path / "data" / "cache" / "industry_board_latest.csv",
        "name,pct_chg\nTaxonomy B,1.2\n",
    )
    _pin_mtime(theme, FROZEN_CAPTURE)
    _pin_mtime(board, FROZEN_CAPTURE)

    result = archive_forward_sector_snapshot(
        tmp_path,
        target_date="2026-07-23",
        captured_at=FROZEN_CAPTURE,
        source_override=_refresh_evidence(tmp_path, board_count=1),
    )
    assert result["written"] is False
    assert result["aligned_membership_count"] == 0
    assert "sector_membership_board_taxonomy_unaligned" in result["reason_codes"]


# ---------------------------------------------------------------------------
# Part A: the artifact's self-declared ``captured_at`` column is authoritative
# ---------------------------------------------------------------------------

def test_self_declared_captured_at_column_is_the_observation_time(tmp_path: Path) -> None:
    """A real capture artifact carries its own ``captured_at`` column.  The gate
    must read that per-row value as the observation moment (not ``updated_at``),
    proving the capture output is self-describing."""
    from quantdesk.data_truth.sector_history_archive import _membership_rows

    # ``captured_at`` column present and same-day; ``updated_at`` stale.  Only the
    # captured_at column may decide the observation day.
    artifact = _write(
        tmp_path / "capture.csv",
        "symbol,name,sector,industry,source,captured_at,updated_at\n"
        "000001,Alpha,Finance,Bank,akshare.eastmoney_industry_cons,"
        "2026-07-23T18:32:16,2026-07-01 09:00:00\n",
    )
    rows, meta = _membership_rows(
        artifact, captured_at="2026-07-23T18:32:16", target_date="2026-07-23"
    )
    assert meta["embedded_observation_days"] == ["2026-07-23"]
    assert meta["embedded_source_update_days"] == ["2026-07-01"]
    assert meta["non_target_observation_timestamp_count"] == 0
    assert rows[0]["observed_at"] == "2026-07-23T18:32:16"
    assert rows[0]["source_updated_at"] == "2026-07-01 09:00:00"


def test_stale_per_row_captured_at_column_is_caught(tmp_path: Path) -> None:
    """Defense-in-depth: if a row's own ``captured_at`` column is a prior day, it
    is flagged ``non_target`` even when the trusted param says same-day — the
    per-row column can only make the gate stricter, never masquerade as fresh."""
    from quantdesk.data_truth.sector_history_archive import _membership_rows

    artifact = _write(
        tmp_path / "capture.csv",
        "symbol,name,sector,industry,source,captured_at\n"
        "000001,Alpha,Finance,Bank,manual_import,2026-07-22T18:32:16\n",
    )
    _, meta = _membership_rows(
        artifact, captured_at="2026-07-23T18:32:16", target_date="2026-07-23"
    )
    assert meta["embedded_observation_days"] == ["2026-07-22"]
    assert meta["non_target_observation_timestamp_count"] == 1


# ---------------------------------------------------------------------------
# Part C: T0 birth evidence pack — three cross-checks must all agree
# ---------------------------------------------------------------------------

import json as _json


def _passed_snapshot(root: Path, day: str, *, content_identity: str) -> Path:
    """Write an eligible (passed) snapshot carrying a full provenance triple."""
    snap = root / "data" / "sector_history" / day / "snapshot.json"
    snap.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "status": "passed",
        "evidence_eligible": True,
        "pit_eligible": True,
        "target_date": day,
        "captured_at": f"{day}T18:32:16",
        "snapshot_hash": f"sha256:snap{day.replace('-', '')}",
        "content_identity": content_identity,
        "membership_count": 2,
        "aligned_membership_count": 2,
        "sector_count": 2,
        "provenance": {
            "source": "local_current_sector_observation",
            "captured_at": f"{day}T18:32:16",
            "hash": "sha256:prov0001",
        },
    }
    snap.write_text(_json.dumps(payload), encoding="utf-8")
    return snap


def _chain_manifest(
    root: Path,
    day: str,
    *,
    written: bool,
    content_identity: str,
    generated_at: str | None = None,
) -> Path:
    run_id = f"daily-data-chain-{day.replace('-', '')}T103008Z-abc123"
    manifest = root / "results" / "data_chain" / "runs" / run_id / "manifest.json"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "run_id": run_id,
        "target_date": day,
        "status": "completed",
        "generated_at": generated_at or f"{day}T19:08:00",
        "components": {
            "sector_history_archive": {
                "written": written,
                "status": "passed" if written else "degraded",
                "content_identity": content_identity,
                "snapshot_hash": f"sha256:snap{day.replace('-', '')}",
            }
        },
    }
    manifest.write_text(_json.dumps(payload), encoding="utf-8")
    return manifest


def test_t0_birth_pack_reports_not_born_without_eligible_day(tmp_path: Path) -> None:
    from quantdesk.data_truth.t0_birth_evidence import build_t0_birth_evidence

    pack = build_t0_birth_evidence(tmp_path, generated_at=FROZEN_CAPTURE)
    assert pack["born"] is False
    assert pack["t0"] is None
    assert pack["reason_codes"] == ["t0_not_yet_born"]


def test_t0_birth_pack_born_when_all_three_cross_checks_agree(tmp_path: Path) -> None:
    from quantdesk.data_truth.t0_birth_evidence import build_t0_birth_evidence

    day = "2026-07-23"
    cid = "sha256:identity0723"
    _passed_snapshot(tmp_path, day, content_identity=cid)
    _chain_manifest(tmp_path, day, written=True, content_identity=cid)

    pack = build_t0_birth_evidence(tmp_path, generated_at=FROZEN_CAPTURE)
    assert pack["born"] is True
    assert pack["t0"] == day
    assert pack["reason_codes"] == []
    assert pack["cross_checks"]["snapshot"]["ok"] is True
    assert pack["cross_checks"]["chain_manifest"]["archive_written"] is True
    assert pack["cross_checks"]["readiness"]["ok"] is True
    assert pack["cross_checks"]["snapshot"]["provenance_triple"]["captured_at"] == (
        f"{day}T18:32:16"
    )


def test_t0_birth_pack_fails_when_manifest_says_not_written(tmp_path: Path) -> None:
    from quantdesk.data_truth.t0_birth_evidence import build_t0_birth_evidence

    day = "2026-07-23"
    cid = "sha256:identity0723"
    _passed_snapshot(tmp_path, day, content_identity=cid)
    _chain_manifest(tmp_path, day, written=False, content_identity=cid)

    pack = build_t0_birth_evidence(tmp_path, generated_at=FROZEN_CAPTURE)
    assert pack["born"] is False
    assert "t0_chain_manifest_archive_not_written" in pack["reason_codes"]


def test_t0_birth_pack_fails_on_content_identity_mismatch(tmp_path: Path) -> None:
    from quantdesk.data_truth.t0_birth_evidence import build_t0_birth_evidence

    day = "2026-07-23"
    _passed_snapshot(tmp_path, day, content_identity="sha256:snapshot_side")
    _chain_manifest(tmp_path, day, written=True, content_identity="sha256:manifest_side")

    pack = build_t0_birth_evidence(tmp_path, generated_at=FROZEN_CAPTURE)
    assert pack["born"] is False
    assert "t0_content_identity_manifest_snapshot_mismatch" in pack["reason_codes"]


def test_t0_birth_pack_fails_when_chain_manifest_absent(tmp_path: Path) -> None:
    from quantdesk.data_truth.t0_birth_evidence import build_t0_birth_evidence

    day = "2026-07-23"
    _passed_snapshot(tmp_path, day, content_identity="sha256:identity0723")
    # No chain manifest written.
    pack = build_t0_birth_evidence(tmp_path, generated_at=FROZEN_CAPTURE)
    assert pack["born"] is False
    assert "t0_chain_manifest_not_found" in pack["reason_codes"]


def test_t0_birth_pack_write_only_touches_results_root(tmp_path: Path) -> None:
    from quantdesk.data_truth.t0_birth_evidence import write_t0_birth_evidence

    day = "2026-07-23"
    cid = "sha256:identity0723"
    _passed_snapshot(tmp_path, day, content_identity=cid)
    _chain_manifest(tmp_path, day, written=True, content_identity=cid)
    out = tmp_path / "results" / "data_health" / "round8_t0_birth_evidence_latest.json"

    pack = write_t0_birth_evidence(tmp_path, output_path=out, generated_at=FROZEN_CAPTURE)
    assert out.is_file()
    assert pack["evidence_pack_path"] == str(out.resolve())
    assert not (tmp_path / "data" / "universe").exists()
    assert not (tmp_path / "state").exists()


def test_chain_t0_birth_refresh_reports_not_born_in_isolated_root(tmp_path) -> None:
    """The chain's Part C wiring must run read-only, report an honest
    not-yet-born pack, and write only under results/ without breaking."""
    import quantdesk.daily_data_chain as chain

    result = chain._refresh_t0_birth_evidence(tmp_path)
    assert result["status"] == "refreshed"
    assert result["born"] is False
    assert result["t0"] is None
    assert result["reason_codes"] == ["t0_not_yet_born"]
    assert not (tmp_path / "data" / "universe").exists()
    assert not (tmp_path / "state").exists()


def test_chain_t0_birth_refresh_born_when_snapshot_and_manifest_agree(tmp_path) -> None:
    """End-to-end chain wiring: an eligible snapshot + agreeing manifest yields a
    born pack via the chain refresh entrypoint."""
    import quantdesk.daily_data_chain as chain

    day = "2026-07-23"
    cid = "sha256:chainwire0723"
    _passed_snapshot(tmp_path, day, content_identity=cid)
    _chain_manifest(tmp_path, day, written=True, content_identity=cid)

    result = chain._refresh_t0_birth_evidence(tmp_path)
    assert result["status"] == "refreshed"
    assert result["born"] is True
    assert result["t0"] == day
    assert result["reason_codes"] == []
    out = tmp_path / "results" / "data_health" / "round8_t0_birth_evidence_latest.json"
    assert out.is_file()


# ---------------------------------------------------------------------------
# Part D: forward-replay readiness detection dry-run (T0 unborn -> zero fills)
# ---------------------------------------------------------------------------

def test_readiness_dryrun_contract_complete_and_runs_nothing_before_t0(tmp_path: Path) -> None:
    """With no T0 yet, detect_t0_readiness returns a complete contract and the
    terminal runner must NOT run — proven by a tripwire replay_fn that raises if
    ever called (i.e. zero fills before T0)."""
    from quantdesk.data_truth.forward_pit_readiness import write_forward_pit_readiness
    from quantdesk.paper.stage4_forward_replay import (
        detect_t0_readiness,
        run_forward_replay_if_ready,
    )

    readiness_out = (
        tmp_path / "results" / "data_health" / "round7_forward_pit_readiness_latest.json"
    )
    write_forward_pit_readiness(tmp_path, output_path=readiness_out, generated_at=FROZEN_CAPTURE)

    status = detect_t0_readiness(readiness_out)
    contract = status.to_dict()
    for key in (
        "task_type",
        "t0",
        "current_eligible_forward_days",
        "milestone_dates",
        "ready_for_interim",
        "ready_for_terminal",
        "status",
    ):
        assert key in contract
    assert contract["t0"] is None
    assert contract["ready_for_terminal"] is False
    assert contract["status"] == "blocked_pit_evidence_pending"

    def _tripwire(_status):
        raise AssertionError("replay_fn must not be called before T0+18")

    result = run_forward_replay_if_ready(
        readiness_path=readiness_out,
        replay_fn=_tripwire,
        effective_sample_fn=lambda _r: 0,
    )
    assert result["ran"] is False
    assert result["reason"] == "blocked_pit_evidence_pending"
