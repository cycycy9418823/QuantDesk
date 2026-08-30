from __future__ import annotations

import hashlib
import importlib
import json
from pathlib import Path

import pytest

from quantdesk.paper.stage4_maintenance_evidence import stage4_terminal_disposition
from quantdesk.risk import risk_gate_shadow as shadow_archive


def _write_json(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2),
        encoding="utf-8",
    )


def _snapshot(day: str) -> dict[str, object]:
    payload: dict[str, object] = {
        "target_date": day,
        "status": "passed",
        "evidence_eligible": True,
        "pit_eligible": True,
        "reason_codes": [],
        "content_identity": "sha256:"
        + hashlib.sha256(f"identity:{day}".encode("utf-8")).hexdigest(),
        "membership_count": 10,
        "aligned_membership_count": 10,
        "sector_count": 2,
    }
    canonical = (
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")
    payload["snapshot_hash"] = "sha256:" + hashlib.sha256(canonical).hexdigest()
    return payload


def _readiness(t0: str | None, days: list[str]) -> dict[str, object]:
    return {
        "status": "t0_launched" if t0 else "blocked_pit_evidence_pending",
        "t0": t0,
        "current_eligible_forward_days": len(days),
        "eligible_forward_days": [
            {
                "date": day,
                "snapshot_path": f"data/sector_history/{day}/snapshot.json",
                "snapshot_hash": _snapshot(day)["snapshot_hash"],
                "membership_count": 10,
                "aligned_membership_count": 10,
                "sector_count": 2,
            }
            for day in days
        ],
        "milestones": [
            {
                "required_consecutive_trading_days": 1,
                "relative_to_t0": "T0",
                "calendar_date": t0,
                "status": "achieved" if t0 else "unanchored_t0_unknown",
                "purpose": "first_valid_forward_archive",
            },
            {
                "required_consecutive_trading_days": 5,
                "relative_to_t0": "T0+4 trading sessions",
                "calendar_date": "2026-07-31" if t0 else None,
                "status": "achieved" if len(days) >= 5 else "pending_forward_accumulation",
                "purpose": "control_window",
            },
            {
                "required_consecutive_trading_days": 9,
                "relative_to_t0": "T0+8 trading sessions",
                "calendar_date": "2026-08-06" if t0 else None,
                "status": "pending_forward_accumulation",
                "purpose": "main_window",
            },
            {
                "required_consecutive_trading_days": 19,
                "relative_to_t0": "T0+18 trading sessions",
                "calendar_date": "2026-08-20" if t0 else None,
                "status": "pending_forward_accumulation",
                "purpose": "terminal_window",
            },
        ],
    }


def _birth(t0: str | None) -> dict[str, object]:
    if t0 is None:
        return {"born": False, "t0": None, "reason_codes": ["t0_not_yet_born"]}
    snapshot = _snapshot(t0)
    run_id = f"daily-data-chain-{t0.replace('-', '')}T074500Z-fixture"
    return {
        "schema_version": "round8_t0_birth_evidence_v1",
        "born": True,
        "t0": t0,
        "reason_codes": [],
        "cross_checks": {
            "snapshot": {
                "ok": True,
                "snapshot_hash": snapshot["snapshot_hash"],
                "content_identity": snapshot["content_identity"],
            },
            "chain_manifest": {
                "found": True,
                "run_id": run_id,
                "archive_written": True,
                "archive_status": "passed",
                "archive_snapshot_hash": snapshot["snapshot_hash"],
                "archive_content_identity": snapshot["content_identity"],
            },
            "readiness": {"ok": True, "anchored_t0": t0},
        },
    }


def _shadow(day: str, *, evaluated: int, insufficient: int) -> dict[str, object]:
    return {
        "task_type": "risk_gate_shadow_daily_summary",
        "as_of": day,
        "generated_at": f"{day}T10:00:00+00:00",
        "observed_count": evaluated,
        "evaluation_count": evaluated,
        "would_pass_count": max(evaluated - insufficient, 0),
        "would_block_count": 0,
        "insufficient_evidence_count": insufficient,
        "insufficient_evidence_ratio": insufficient / evaluated if evaluated else 0.0,
        "legacy_blocked_count": 0,
        "shadow_error_count": 0,
        "missing_component_counts": {"sector": insufficient} if insufficient else {},
        "risk_gate_enforced": False,
        "enforcement_applied": False,
    }


def _run_evidence(root: Path, day: str, *, shadow_payload: dict[str, object] | None) -> None:
    run_id = f"daily-data-chain-{day.replace('-', '')}T074500Z-fixture"
    run_dir = root / "results" / "data_chain" / "runs" / run_id
    snapshot = _snapshot(day)
    component = {**snapshot, "written": True}
    _write_json(
        run_dir / "manifest.json",
        {
            "task_type": "daily_data_chain",
            "run_id": run_id,
            "target_date": day,
            "authoritative": True,
            "published_latest": True,
            "status": "passed",
            "universe": {"current_analysis_pool": {"symbol_count": 914}},
            "components": {"sector_history_archive": component},
        },
    )
    _write_json(run_dir / "sector_history_archive_attempt.json", component)
    _write_json(root / "data" / "sector_history" / day / "snapshot.json", snapshot)
    if shadow_payload is not None:
        shadow_archive.write_shadow_summary(
            shadow_payload,
            output_path=root / "results" / "risk_gate_shadow_latest.json",
        )


def _gate_fixture(root: Path) -> tuple[dict[str, Path], dict[str, str]]:
    gate_paths: dict[str, Path] = {}
    hashes: dict[str, str] = {}
    for name in ("archive_gate.py", "risk_gate.py", "default.yaml"):
        path = root / "gate" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"fixture:{name}\n", encoding="utf-8")
        gate_paths[name] = path
        hashes[name] = hashlib.sha256(path.read_bytes()).hexdigest()
    return gate_paths, hashes


def _collect(
    root: Path,
    *,
    as_of: str,
    expected_dates: list[str],
    t0_gate_hashes: dict[str, str] | None = None,
    gate_paths: dict[str, Path] | None = None,
    replay_adapter_fn=None,
) -> dict[str, object]:
    maintenance = importlib.import_module(
        "quantdesk.paper.stage4_maintenance_evidence"
    )
    return maintenance.collect_stage4_maintenance_evidence(
        root,
        as_of=as_of,
        readiness_path=root
        / "results"
        / "data_health"
        / "round7_forward_pit_readiness_latest.json",
        t0_birth_path=root
        / "results"
        / "data_health"
        / "round8_t0_birth_evidence_latest.json",
        chain_runs_dir=root / "results" / "data_chain" / "runs",
        shadow_archive_dir=root / "results" / "risk_gate_shadow_daily",
        shadow_latest_path=root / "results" / "risk_gate_shadow_latest.json",
        expected_trading_dates=expected_dates,
        gate_paths=gate_paths,
        t0_gate_hashes=t0_gate_hashes,
        replay_adapter_fn=replay_adapter_fn,
    )


def test_accumulating_evidence_maps_all_five_preregistered_checks(tmp_path: Path) -> None:
    days = ["2026-07-27", "2026-07-28"]
    _write_json(
        tmp_path / "results" / "data_health" / "round7_forward_pit_readiness_latest.json",
        _readiness("2026-07-27", days),
    )
    _write_json(
        tmp_path / "results" / "data_health" / "round8_t0_birth_evidence_latest.json",
        _birth("2026-07-27"),
    )
    _run_evidence(tmp_path, days[0], shadow_payload=_shadow(days[0], evaluated=2, insufficient=2))
    _run_evidence(tmp_path, days[1], shadow_payload=_shadow(days[1], evaluated=2, insufficient=1))
    gate_paths, t0_hashes = _gate_fixture(tmp_path)

    payload = _collect(
        tmp_path,
        as_of="2026-07-28",
        expected_dates=days,
        gate_paths=gate_paths,
        t0_gate_hashes=t0_hashes,
    )

    assert payload["status"] == "accumulating"
    assert payload["conclusion_drawn"] is False
    assert payload["account_replay"] == "not_authorized"
    assert payload["synthetic_data_used"] is False
    checks = payload["preregistered_checks"]
    assert set(checks) == {
        "coverage_continuity",
        "shadow_evidence_trend",
        "daily_count_integrity",
        "qualification_gate_frozen",
        "outcome_conclusion_guard",
    }
    assert checks["coverage_continuity"]["complete"] is True
    assert checks["shadow_evidence_trend"]["baseline_ratio"] == 1.0
    assert checks["shadow_evidence_trend"]["latest_observed_ratio"] == 0.5
    assert checks["shadow_evidence_trend"]["direction"] == "decreased"
    assert checks["qualification_gate_frozen"]["matches_t0"] is True
    assert payload["daily_counts"][1]["candidate_count"] == 2
    assert payload["daily_counts"][1]["evaluation_count"] == 2
    assert payload["daily_counts"][1]["would_pass_count"] == 1
    assert payload["daily_counts"][1]["chain_analysis_pool_count"] == 914
    assert payload["daily_counts"][1]["run_id"].endswith("fixture")
    serialized = json.dumps(payload, ensure_ascii=False).lower()
    assert "预期收益" not in serialized
    assert '"pnl' not in serialized
    assert '"return' not in serialized


def test_missing_day_and_shadow_archive_are_disclosed_not_hidden(tmp_path: Path) -> None:
    days = ["2026-07-27", "2026-07-28", "2026-07-29"]
    _write_json(
        tmp_path / "results" / "data_health" / "round7_forward_pit_readiness_latest.json",
        _readiness("2026-07-27", [days[0], days[2]]),
    )
    _write_json(
        tmp_path / "results" / "data_health" / "round8_t0_birth_evidence_latest.json",
        _birth("2026-07-27"),
    )
    _run_evidence(tmp_path, days[0], shadow_payload=_shadow(days[0], evaluated=1, insufficient=1))
    _run_evidence(tmp_path, days[2], shadow_payload=None)
    gate_paths, t0_hashes = _gate_fixture(tmp_path)

    payload = _collect(
        tmp_path,
        as_of="2026-07-29",
        expected_dates=days,
        gate_paths=gate_paths,
        t0_gate_hashes=t0_hashes,
    )

    coverage = {row["date"]: row for row in payload["coverage_days"]}
    assert coverage["2026-07-28"]["complete"] is False
    assert "snapshot_missing" in coverage["2026-07-28"]["reason_codes"]
    counts = {row["date"]: row for row in payload["daily_counts"]}
    assert counts["2026-07-29"]["shadow_status"] == "missing"
    assert "shadow_archive_missing" in counts["2026-07-29"]["reason_codes"]
    assert payload["preregistered_checks"]["coverage_continuity"]["complete"] is False


@pytest.mark.parametrize(
    ("t0", "eligible_days", "expected_status"),
    [
        (None, [], "blocked_pit_evidence_pending"),
        ("2026-07-27", ["2026-07-27"], "accumulating"),
        (
            "2026-07-27",
            ["2026-07-27", "2026-07-28", "2026-07-29", "2026-07-30", "2026-07-31"],
            "maintenance_window_incomplete",
        ),
    ],
)
def test_readiness_states_never_authorize_account_replay(
    tmp_path: Path,
    t0: str | None,
    eligible_days: list[str],
    expected_status: str,
) -> None:
    _write_json(
        tmp_path / "results" / "data_health" / "round7_forward_pit_readiness_latest.json",
        _readiness(t0, eligible_days),
    )
    _write_json(
        tmp_path / "results" / "data_health" / "round8_t0_birth_evidence_latest.json",
        _birth(t0),
    )
    gate_paths, t0_hashes = _gate_fixture(tmp_path)

    payload = _collect(
        tmp_path,
        as_of=eligible_days[-1] if eligible_days else "2026-07-28",
        expected_dates=eligible_days,
        gate_paths=gate_paths,
        t0_gate_hashes=t0_hashes,
    )

    assert payload["status"] == expected_status
    assert payload["account_replay"] == "not_authorized"
    assert payload["synthetic_data_used"] is False
    assert "account_results" not in payload
    assert payload["conclusion_drawn"] is False


def test_gate_hash_change_is_fail_closed(tmp_path: Path) -> None:
    _write_json(
        tmp_path / "results" / "data_health" / "round7_forward_pit_readiness_latest.json",
        _readiness("2026-07-27", ["2026-07-27"]),
    )
    _write_json(
        tmp_path / "results" / "data_health" / "round8_t0_birth_evidence_latest.json",
        _birth("2026-07-27"),
    )
    gate_paths, t0_hashes = _gate_fixture(tmp_path)
    next(iter(gate_paths.values())).write_text("changed\n", encoding="utf-8")

    payload = _collect(
        tmp_path,
        as_of="2026-07-27",
        expected_dates=["2026-07-27"],
        gate_paths=gate_paths,
        t0_gate_hashes=t0_hashes,
    )

    gate = payload["preregistered_checks"]["qualification_gate_frozen"]
    assert gate["matches_t0"] is False
    assert gate["mismatches"]


def test_missing_t0_birth_evidence_is_disclosed(tmp_path: Path) -> None:
    _write_json(
        tmp_path / "results" / "data_health" / "round7_forward_pit_readiness_latest.json",
        _readiness("2026-07-27", ["2026-07-27"]),
    )
    gate_paths, t0_hashes = _gate_fixture(tmp_path)

    payload = _collect(
        tmp_path,
        as_of="2026-07-27",
        expected_dates=["2026-07-27"],
        gate_paths=gate_paths,
        t0_gate_hashes=t0_hashes,
    )

    assert payload["t0_birth_cross_check"]["found"] is False
    assert "t0_birth_evidence_missing" in payload["reason_codes"]


def test_writer_and_cli_write_only_requested_results_directory(tmp_path: Path) -> None:
    root = tmp_path / "project"
    _write_json(
        root / "results" / "data_health" / "round7_forward_pit_readiness_latest.json",
        _readiness(None, []),
    )
    _write_json(
        root / "results" / "data_health" / "round8_t0_birth_evidence_latest.json",
        _birth(None),
    )
    gate_paths, t0_hashes = _gate_fixture(root)
    payload = _collect(
        root,
        as_of="2026-07-28",
        expected_dates=[],
        gate_paths=gate_paths,
        t0_gate_hashes=t0_hashes,
    )
    output_dir = root / "results" / "isolated_data_health"

    maintenance = importlib.import_module(
        "quantdesk.paper.stage4_maintenance_evidence"
    )
    paths = maintenance.write_stage4_maintenance_evidence(
        payload,
        output_dir=output_dir,
        results_root=root / "results",
        as_of="2026-07-28",
    )

    assert set(paths) == {"dated", "latest"}
    assert all(path.parent == output_dir for path in paths.values())
    assert not (root / "state").exists()
    assert not (root / "data" / "daily").exists()

    runner = importlib.import_module("scripts.run_stage4_maintenance_evidence")
    assert runner.main(
        [
            "--project-root",
            str(root),
            "--output-dir",
            str(output_dir),
            "--as-of",
            "2026-07-28",
            "--network",
            "false",
            "--json",
            "true",
            "--summary",
            "true",
        ]
    ) == 0


def test_cli_pins_post_terminal_retries_to_t0_plus_18(tmp_path: Path) -> None:
    runner = importlib.import_module("scripts.run_stage4_maintenance_evidence")
    readiness = tmp_path / "readiness.json"
    _write_json(
        readiness,
        {"milestones": [{"relative_to_t0": "T0+18 trading sessions", "calendar_date": "2026-08-20"}]},
    )
    assert runner._fixed_terminal_date(tmp_path, readiness, "2026-08-21") == "2026-08-20"
    assert runner._fixed_terminal_date(tmp_path, readiness, "2026-08-19") == "2026-08-19"


def test_writer_rejects_outcome_prose_before_disk_write(tmp_path: Path) -> None:
    maintenance = importlib.import_module(
        "quantdesk.paper.stage4_maintenance_evidence"
    )
    payload = {
        "task_type": "stage4_maintenance_evidence",
        "conclusion_drawn": False,
        "note": "PnL outlook is positive",
    }
    output_dir = tmp_path / "results" / "data_health"

    with pytest.raises(ValueError, match="forbidden Stage 4 maintenance prose"):
        maintenance.write_stage4_maintenance_evidence(
            payload,
            output_dir=output_dir,
            results_root=tmp_path / "results",
            as_of="2026-07-28",
        )

    assert not output_dir.exists()


def test_qd_registers_stage4_maintenance_in_all_command_surfaces() -> None:
    text = (Path(__file__).resolve().parents[1] / "scripts" / "qd.py").read_text(
        encoding="utf-8"
    )
    assert '"stage4-maintenance": ("run_stage4_maintenance_evidence.py"' in text
    assert '"stage4-maintenance": [' in text
    assert '"stage4-maintenance": PROJECT_ROOT' in text
    assert '"stage4-maintenance --network false --json true --summary true"' in text


def test_count_integrity_rejects_underclassified_evaluations(tmp_path: Path) -> None:
    day = "2026-07-27"
    _write_json(
        tmp_path / "results" / "data_health" / "round7_forward_pit_readiness_latest.json",
        _readiness(day, [day]),
    )
    _write_json(
        tmp_path / "results" / "data_health" / "round8_t0_birth_evidence_latest.json",
        _birth(day),
    )
    payload = _shadow(day, evaluated=3, insufficient=0)
    payload["would_pass_count"] = 0
    _run_evidence(tmp_path, day, shadow_payload=payload)
    gate_paths, t0_hashes = _gate_fixture(tmp_path)

    evidence = _collect(
        tmp_path,
        as_of=day,
        expected_dates=[day],
        gate_paths=gate_paths,
        t0_gate_hashes=t0_hashes,
    )

    row = evidence["daily_counts"][0]
    assert row["count_consistent"] is False
    assert "shadow_count_inconsistent" in row["reason_codes"]


def test_latest_only_shadow_does_not_satisfy_dated_archive(tmp_path: Path) -> None:
    day = "2026-07-27"
    _write_json(
        tmp_path / "results" / "data_health" / "round7_forward_pit_readiness_latest.json",
        _readiness(day, [day]),
    )
    _write_json(
        tmp_path / "results" / "data_health" / "round8_t0_birth_evidence_latest.json",
        _birth(day),
    )
    _run_evidence(tmp_path, day, shadow_payload=None)
    _write_json(
        tmp_path / "results" / "risk_gate_shadow_latest.json",
        _shadow(day, evaluated=1, insufficient=1),
    )
    gate_paths, t0_hashes = _gate_fixture(tmp_path)

    evidence = _collect(
        tmp_path,
        as_of=day,
        expected_dates=[day],
        gate_paths=gate_paths,
        t0_gate_hashes=t0_hashes,
    )

    row = evidence["daily_counts"][0]
    assert row["shadow_status"] == "latest_only"
    assert "shadow_archive_missing" in row["reason_codes"]
    assert evidence["preregistered_checks"]["daily_count_integrity"]["complete"] is False


def test_readiness_count_without_dates_never_promotes_ready(tmp_path: Path) -> None:
    day = "2026-07-31"
    readiness = _readiness("2026-07-27", [])
    readiness["current_eligible_forward_days"] = 5
    _write_json(
        tmp_path / "results" / "data_health" / "round7_forward_pit_readiness_latest.json",
        readiness,
    )
    _write_json(
        tmp_path / "results" / "data_health" / "round8_t0_birth_evidence_latest.json",
        _birth("2026-07-27"),
    )
    gate_paths, t0_hashes = _gate_fixture(tmp_path)

    evidence = _collect(
        tmp_path,
        as_of=day,
        expected_dates=[
            "2026-07-27",
            "2026-07-28",
            "2026-07-29",
            "2026-07-30",
            "2026-07-31",
        ],
        gate_paths=gate_paths,
        t0_gate_hashes=t0_hashes,
    )

    assert evidence["status"] != "maintenance_window_ready"
    assert "readiness_eligible_days_mismatch" in evidence["reason_codes"]


def test_snapshot_manifest_attempt_identity_mismatch_breaks_coverage(
    tmp_path: Path,
) -> None:
    day = "2026-07-27"
    _write_json(
        tmp_path / "results" / "data_health" / "round7_forward_pit_readiness_latest.json",
        _readiness(day, [day]),
    )
    _write_json(
        tmp_path / "results" / "data_health" / "round8_t0_birth_evidence_latest.json",
        _birth(day),
    )
    _run_evidence(tmp_path, day, shadow_payload=_shadow(day, evaluated=1, insufficient=1))
    snapshot_path = tmp_path / "data" / "sector_history" / day / "snapshot.json"
    snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
    snapshot["target_date"] = "2026-07-26"
    snapshot["snapshot_hash"] = "sha256:replaced"
    _write_json(snapshot_path, snapshot)
    gate_paths, t0_hashes = _gate_fixture(tmp_path)

    evidence = _collect(
        tmp_path,
        as_of=day,
        expected_dates=[day],
        gate_paths=gate_paths,
        t0_gate_hashes=t0_hashes,
    )

    row = evidence["coverage_days"][0]
    assert row["complete"] is False
    assert "snapshot_target_date_mismatch" in row["reason_codes"]
    assert "snapshot_hash_mismatch" in row["reason_codes"]


def test_enforced_shadow_input_is_rejected_instead_of_relabelled(
    tmp_path: Path,
) -> None:
    day = "2026-07-27"
    _write_json(
        tmp_path / "results" / "data_health" / "round7_forward_pit_readiness_latest.json",
        _readiness(day, [day]),
    )
    _write_json(
        tmp_path / "results" / "data_health" / "round8_t0_birth_evidence_latest.json",
        _birth(day),
    )
    shadow = _shadow(day, evaluated=1, insufficient=1)
    shadow["risk_gate_enforced"] = True
    shadow["enforcement_applied"] = True
    _run_evidence(tmp_path, day, shadow_payload=shadow)
    gate_paths, t0_hashes = _gate_fixture(tmp_path)

    evidence = _collect(
        tmp_path,
        as_of=day,
        expected_dates=[day],
        gate_paths=gate_paths,
        t0_gate_hashes=t0_hashes,
    )

    row = evidence["daily_counts"][0]
    assert row["shadow_status"] == "invalid"
    assert "shadow_enforcement_boundary_breach" in row["reason_codes"]
    assert evidence["shadow_input_rejections"]


def test_terminal_window_is_fail_closed_without_account_replay(tmp_path: Path) -> None:
    days = [
        "2026-07-27", "2026-07-28", "2026-07-29", "2026-07-30", "2026-07-31",
        "2026-08-03", "2026-08-04", "2026-08-05", "2026-08-06", "2026-08-07",
        "2026-08-10", "2026-08-11", "2026-08-12", "2026-08-13", "2026-08-14",
        "2026-08-17", "2026-08-18", "2026-08-19", "2026-08-20",
    ]
    _write_json(
        tmp_path / "results" / "data_health" / "round7_forward_pit_readiness_latest.json",
        _readiness("2026-07-27", days),
    )
    _write_json(
        tmp_path / "results" / "data_health" / "round8_t0_birth_evidence_latest.json",
        _birth("2026-07-27"),
    )
    gate_paths, t0_hashes = _gate_fixture(tmp_path)

    evidence = _collect(
        tmp_path,
        as_of=days[-1],
        expected_dates=days,
        gate_paths=gate_paths,
        t0_gate_hashes=t0_hashes,
    )

    assert evidence["status"] == "closed_inconclusive"
    assert "t0_plus_18_account_replay_not_authorized" in evidence["reason_codes"]
    assert evidence["account_replay"] == "not_authorized"


def test_t0_plus8_enables_only_quality_passed_real_adapter(tmp_path: Path) -> None:
    from types import SimpleNamespace

    from quantdesk.paper.stage4_replay_adapter import AdaptedAccountResult

    days = [
        "2026-07-27", "2026-07-28", "2026-07-29", "2026-07-30", "2026-07-31",
        "2026-08-03", "2026-08-04", "2026-08-05", "2026-08-06",
    ]
    _write_json(
        tmp_path / "results" / "data_health" / "round7_forward_pit_readiness_latest.json",
        _readiness("2026-07-27", days),
    )
    _write_json(
        tmp_path / "results" / "data_health" / "round8_t0_birth_evidence_latest.json",
        _birth("2026-07-27"),
    )
    for day in days:
        _run_evidence(tmp_path, day, shadow_payload=_shadow(day, evaluated=1, insufficient=1))
    gate_paths, t0_hashes = _gate_fixture(tmp_path)
    accounts = {
        account_id: AdaptedAccountResult(
            account_id=account_id,
            candidate_count=1,
            fill_count=0,
            stage_counts={"gate": 1},
            nav_series=[{"date": days[0], "nav": 10_000.0}],
        )
        for account_id in ("A", "B", "C")
    }
    adapter_result = SimpleNamespace(
        maintenance_eligible=True,
        fixture_or_synthetic=False,
        quality_gate={"passed": True, "reason_codes": []},
        account_results=accounts,
        source_manifest={},
    )

    evidence = _collect(
        tmp_path,
        as_of=days[-1],
        expected_dates=days,
        gate_paths=gate_paths,
        t0_gate_hashes=t0_hashes,
        replay_adapter_fn=lambda *args, **kwargs: adapter_result,
    )

    assert evidence["account_replay"] == "enabled_real"
    assert evidence["account_results"]["milestone"] == "T0+8"
    assert evidence["account_results"]["account_candidate_counts"] == {
        "A": 1,
        "B": 1,
        "C": 1,
    }
    assert evidence["synthetic_data_used"] is False


def test_t0_plus8_real_adapter_stays_pending_when_shadow_gate_is_incomplete(
    tmp_path: Path,
) -> None:
    from types import SimpleNamespace

    from quantdesk.paper.stage4_replay_adapter import AdaptedAccountResult

    days = [
        "2026-07-27", "2026-07-28", "2026-07-29", "2026-07-30", "2026-07-31",
        "2026-08-03", "2026-08-04", "2026-08-05", "2026-08-06",
    ]
    _write_json(
        tmp_path / "results" / "data_health" / "round7_forward_pit_readiness_latest.json",
        _readiness("2026-07-27", days),
    )
    _write_json(
        tmp_path / "results" / "data_health" / "round8_t0_birth_evidence_latest.json",
        _birth("2026-07-27"),
    )
    for day in days[:-1]:
        _run_evidence(tmp_path, day, shadow_payload=_shadow(day, evaluated=1, insufficient=1))
    _run_evidence(tmp_path, days[-1], shadow_payload=None)
    gate_paths, t0_hashes = _gate_fixture(tmp_path)
    accounts = {
        account_id: AdaptedAccountResult(
            account_id=account_id,
            candidate_count=1,
            fill_count=0,
            stage_counts={"gate": 1},
            nav_series=[{"date": days[0], "nav": 10_000.0}],
        )
        for account_id in ("A", "B", "C")
    }
    adapter_result = SimpleNamespace(
        maintenance_eligible=True,
        fixture_or_synthetic=False,
        quality_gate={"passed": True, "reason_codes": []},
        account_results=accounts,
        source_manifest={},
    )

    evidence = _collect(
        tmp_path,
        as_of=days[-1],
        expected_dates=days,
        gate_paths=gate_paths,
        t0_gate_hashes=t0_hashes,
        replay_adapter_fn=lambda *args, **kwargs: adapter_result,
    )

    assert evidence["account_replay"] == "observed_real_pending_maintenance_gates"
    assert "stage4_maintenance_gate_failed:shadow_counts" in evidence["reason_codes"]
    assert evidence["formal_evidence_eligible"] is False


def test_active_v2_baseline_removes_only_approved_hash_drift_noise(
    tmp_path: Path,
    monkeypatch,
) -> None:
    maintenance = importlib.import_module(
        "quantdesk.paper.stage4_maintenance_evidence"
    )
    days = ["2026-07-27", "2026-07-28", "2026-07-29", "2026-07-30", "2026-07-31"]
    _write_json(
        tmp_path / "results" / "data_health" / "round7_forward_pit_readiness_latest.json",
        _readiness("2026-07-27", days),
    )
    _write_json(
        tmp_path / "results" / "data_health" / "round8_t0_birth_evidence_latest.json",
        _birth("2026-07-27"),
    )
    for day in days:
        _run_evidence(tmp_path, day, shadow_payload=_shadow(day, evaluated=1, insufficient=1))
    gate_paths, t0_hashes = _gate_fixture(tmp_path)
    original_gate_evidence = maintenance._gate_evidence

    def active_gate(*args, **kwargs):
        payload = original_gate_evidence(*args, **kwargs)
        payload["matches_t0"] = False
        payload["matches_active_baseline"] = True
        payload["active_baseline_applicability"] = "prospective_only"
        return payload

    monkeypatch.setattr(maintenance, "_gate_evidence", active_gate)

    evidence = _collect(
        tmp_path,
        as_of=days[-1],
        expected_dates=days,
        gate_paths=gate_paths,
        t0_gate_hashes=t0_hashes,
    )

    assert evidence["status"] == "maintenance_window_ready"
    assert evidence["preregistered_checks"]["qualification_gate_frozen"]["matches_t0"] is False
    assert evidence["preregistered_checks"]["qualification_gate_frozen"][
        "matches_active_baseline"
    ] is True
    assert "qualification_gate_hash_mismatch" not in evidence["reason_codes"]


def test_zero_traffic_observation_remains_visible_in_trend(tmp_path: Path) -> None:
    day = "2026-07-27"
    _write_json(
        tmp_path / "results" / "data_health" / "round7_forward_pit_readiness_latest.json",
        _readiness(day, [day]),
    )
    _write_json(
        tmp_path / "results" / "data_health" / "round8_t0_birth_evidence_latest.json",
        _birth(day),
    )
    shadow = _shadow(day, evaluated=0, insufficient=0)
    shadow["observation_reason"] = "stale_data_no_traffic"
    shadow["generated_at"] = "2026-07-26T09:36:40+00:00"
    shadow["archive_source"] = "latest_backfill"
    _run_evidence(tmp_path, day, shadow_payload=shadow)
    gate_paths, t0_hashes = _gate_fixture(tmp_path)

    evidence = _collect(
        tmp_path,
        as_of=day,
        expected_dates=[day],
        gate_paths=gate_paths,
        t0_gate_hashes=t0_hashes,
    )

    trend = evidence["preregistered_checks"]["shadow_evidence_trend"]
    assert len(trend["series"]) == 1
    assert trend["series"][0]["observation_reason"] == "stale_data_no_traffic"
    assert trend["diagnostic_classification"] == "stale_data_no_traffic"
    row = evidence["daily_counts"][0]
    assert row["source_generated_at"] == "2026-07-26T09:36:40+00:00"
    assert row["archive_source"] == "latest_backfill"
    assert row["captured_on_prior_calendar_day"] is True
    assert row["as_of_semantics"] == "next_trading_day_decision_date"


def test_cli_rejects_output_directory_outside_project_results(tmp_path: Path) -> None:
    root = tmp_path / "project"
    _write_json(
        root / "results" / "data_health" / "round7_forward_pit_readiness_latest.json",
        _readiness(None, []),
    )
    _write_json(
        root / "results" / "data_health" / "round8_t0_birth_evidence_latest.json",
        _birth(None),
    )
    runner = importlib.import_module("scripts.run_stage4_maintenance_evidence")

    exit_code = runner.main(
        [
            "--project-root", str(root),
            "--output-dir", str(root / "state"),
            "--as-of", "2026-07-28",
            "--network", "false",
        ]
    )

    assert exit_code == 2
    assert not (root / "state").exists()


def test_complete_five_session_evidence_is_maintenance_ready(tmp_path: Path) -> None:
    days = [
        "2026-07-27",
        "2026-07-28",
        "2026-07-29",
        "2026-07-30",
        "2026-07-31",
    ]
    _write_json(
        tmp_path / "results" / "data_health" / "round7_forward_pit_readiness_latest.json",
        _readiness("2026-07-27", days),
    )
    _write_json(
        tmp_path / "results" / "data_health" / "round8_t0_birth_evidence_latest.json",
        _birth("2026-07-27"),
    )
    for day in days:
        _run_evidence(
            tmp_path,
            day,
            shadow_payload=_shadow(day, evaluated=2, insufficient=1),
        )
    gate_paths, t0_hashes = _gate_fixture(tmp_path)

    evidence = _collect(
        tmp_path,
        as_of=days[-1],
        expected_dates=days,
        gate_paths=gate_paths,
        t0_gate_hashes=t0_hashes,
    )

    assert evidence["status"] == "maintenance_window_ready"
    assert evidence["readiness_integrity"]["consistent"] is True
    assert evidence["preregistered_checks"]["coverage_continuity"]["complete"] is True
    assert evidence["preregistered_checks"]["daily_count_integrity"]["complete"] is True


def test_corrupt_snapshot_and_negative_shadow_count_are_explicitly_rejected(
    tmp_path: Path,
) -> None:
    day = "2026-07-27"
    _write_json(
        tmp_path / "results" / "data_health" / "round7_forward_pit_readiness_latest.json",
        _readiness(day, [day]),
    )
    _write_json(
        tmp_path / "results" / "data_health" / "round8_t0_birth_evidence_latest.json",
        _birth(day),
    )
    shadow = _shadow(day, evaluated=1, insufficient=1)
    shadow["would_block_count"] = -1
    _run_evidence(tmp_path, day, shadow_payload=shadow)
    (tmp_path / "data" / "sector_history" / day / "snapshot.json").write_text(
        "{not-json",
        encoding="utf-8",
    )
    gate_paths, t0_hashes = _gate_fixture(tmp_path)

    evidence = _collect(
        tmp_path,
        as_of=day,
        expected_dates=[day],
        gate_paths=gate_paths,
        t0_gate_hashes=t0_hashes,
    )

    assert "snapshot_unreadable" in evidence["coverage_days"][0]["reason_codes"]
    assert evidence["daily_counts"][0]["shadow_status"] == "invalid"
    assert "shadow_count_malformed" in evidence["daily_counts"][0]["reason_codes"]


@pytest.mark.parametrize(
    "missing_field",
    ["evaluation_count", "risk_gate_enforced", "enforcement_applied"],
)
def test_missing_shadow_required_field_is_fail_closed(
    tmp_path: Path,
    missing_field: str,
) -> None:
    day = "2026-07-27"
    _write_json(
        tmp_path / "results" / "data_health" / "round7_forward_pit_readiness_latest.json",
        _readiness(day, [day]),
    )
    _write_json(
        tmp_path / "results" / "data_health" / "round8_t0_birth_evidence_latest.json",
        _birth(day),
    )
    shadow = _shadow(day, evaluated=0, insufficient=0)
    shadow.pop(missing_field)
    _run_evidence(tmp_path, day, shadow_payload=shadow)
    gate_paths, t0_hashes = _gate_fixture(tmp_path)

    evidence = _collect(
        tmp_path,
        as_of=day,
        expected_dates=[day],
        gate_paths=gate_paths,
        t0_gate_hashes=t0_hashes,
    )

    row = evidence["daily_counts"][0]
    assert row["shadow_status"] == "invalid"
    assert "shadow_required_field_missing" in row["reason_codes"]
    assert missing_field in row["missing_required_fields"]


@pytest.mark.parametrize(
    ("field", "value", "reason"),
    [
        ("task_type", "fixture_chain", "chain_manifest_task_type_invalid"),
        ("authoritative", False, "chain_manifest_not_authoritative"),
        ("published_latest", False, "chain_manifest_not_published"),
    ],
)
def test_only_official_daily_chain_manifest_satisfies_coverage(
    tmp_path: Path,
    field: str,
    value: object,
    reason: str,
) -> None:
    day = "2026-07-27"
    _write_json(
        tmp_path / "results" / "data_health" / "round7_forward_pit_readiness_latest.json",
        _readiness(day, [day]),
    )
    _write_json(
        tmp_path / "results" / "data_health" / "round8_t0_birth_evidence_latest.json",
        _birth(day),
    )
    _run_evidence(tmp_path, day, shadow_payload=_shadow(day, evaluated=1, insufficient=1))
    run_id = f"daily-data-chain-{day.replace('-', '')}T074500Z-fixture"
    manifest_path = tmp_path / "results" / "data_chain" / "runs" / run_id / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest[field] = value
    _write_json(manifest_path, manifest)
    gate_paths, t0_hashes = _gate_fixture(tmp_path)

    evidence = _collect(
        tmp_path,
        as_of=day,
        expected_dates=[day],
        gate_paths=gate_paths,
        t0_gate_hashes=t0_hashes,
    )

    row = evidence["coverage_days"][0]
    assert row["complete"] is False
    assert reason in row["reason_codes"]


@pytest.mark.parametrize(
    ("source", "reason"),
    [
        ("manifest", "manifest_archive_not_evidence_eligible"),
        ("attempt", "attempt_archive_not_evidence_eligible"),
    ],
)
def test_component_and_attempt_must_remain_evidence_eligible(
    tmp_path: Path,
    source: str,
    reason: str,
) -> None:
    day = "2026-07-27"
    _write_json(
        tmp_path / "results" / "data_health" / "round7_forward_pit_readiness_latest.json",
        _readiness(day, [day]),
    )
    _write_json(
        tmp_path / "results" / "data_health" / "round8_t0_birth_evidence_latest.json",
        _birth(day),
    )
    _run_evidence(tmp_path, day, shadow_payload=_shadow(day, evaluated=1, insufficient=1))
    run_id = f"daily-data-chain-{day.replace('-', '')}T074500Z-fixture"
    run_dir = tmp_path / "results" / "data_chain" / "runs" / run_id
    if source == "manifest":
        path = run_dir / "manifest.json"
        payload = json.loads(path.read_text(encoding="utf-8"))
        target = payload["components"]["sector_history_archive"]
    else:
        path = run_dir / "sector_history_archive_attempt.json"
        payload = json.loads(path.read_text(encoding="utf-8"))
        target = payload
    target["status"] = "degraded"
    target["evidence_eligible"] = False
    target["pit_eligible"] = False
    _write_json(path, payload)
    gate_paths, t0_hashes = _gate_fixture(tmp_path)

    evidence = _collect(
        tmp_path,
        as_of=day,
        expected_dates=[day],
        gate_paths=gate_paths,
        t0_gate_hashes=t0_hashes,
    )

    row = evidence["coverage_days"][0]
    assert row["complete"] is False
    assert reason in row["reason_codes"]


def test_snapshot_body_is_verified_against_its_declared_hash(tmp_path: Path) -> None:
    day = "2026-07-27"
    _write_json(
        tmp_path / "results" / "data_health" / "round7_forward_pit_readiness_latest.json",
        _readiness(day, [day]),
    )
    _write_json(
        tmp_path / "results" / "data_health" / "round8_t0_birth_evidence_latest.json",
        _birth(day),
    )
    _run_evidence(tmp_path, day, shadow_payload=_shadow(day, evaluated=1, insufficient=1))
    snapshot_path = tmp_path / "data" / "sector_history" / day / "snapshot.json"
    snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
    snapshot["membership_count"] = 11
    _write_json(snapshot_path, snapshot)
    gate_paths, t0_hashes = _gate_fixture(tmp_path)

    evidence = _collect(
        tmp_path,
        as_of=day,
        expected_dates=[day],
        gate_paths=gate_paths,
        t0_gate_hashes=t0_hashes,
    )

    row = evidence["coverage_days"][0]
    assert row["complete"] is False
    assert "snapshot_hash_invalid" in row["reason_codes"]


def test_expected_trading_dates_cannot_override_calendar_continuity(tmp_path: Path) -> None:
    days = ["2026-07-27", "2026-07-29"]
    _write_json(
        tmp_path / "results" / "data_health" / "round7_forward_pit_readiness_latest.json",
        _readiness(days[0], days),
    )
    _write_json(
        tmp_path / "results" / "data_health" / "round8_t0_birth_evidence_latest.json",
        _birth(days[0]),
    )
    gate_paths, t0_hashes = _gate_fixture(tmp_path)

    with pytest.raises(ValueError, match="independent trading calendar"):
        _collect(
            tmp_path,
            as_of=days[-1],
            expected_dates=days,
            gate_paths=gate_paths,
            t0_gate_hashes=t0_hashes,
        )


@pytest.mark.parametrize(
    "missing_metadata",
    ["captured_at", "capture_basis", "observed_mtime_utc", "mtime_predates_t0"],
)
def test_t0_gate_baseline_requires_complete_metadata(
    tmp_path: Path,
    missing_metadata: str,
) -> None:
    maintenance = importlib.import_module(
        "quantdesk.paper.stage4_maintenance_evidence"
    )
    baseline = json.loads(json.dumps(maintenance.T0_GATE_BASELINE))
    if missing_metadata in {"captured_at", "capture_basis"}:
        baseline.pop(missing_metadata)
    else:
        first = next(iter(baseline["files"].values()))
        first.pop(missing_metadata)
    path = tmp_path / "baseline.json"
    _write_json(path, baseline)

    with pytest.raises(ValueError, match="invalid Stage 4 T0 gate baseline"):
        maintenance._load_t0_gate_baseline(path)


def test_t0_gate_baseline_requires_exact_file_set(tmp_path: Path) -> None:
    maintenance = importlib.import_module(
        "quantdesk.paper.stage4_maintenance_evidence"
    )
    baseline = json.loads(json.dumps(maintenance.T0_GATE_BASELINE))
    baseline["files"] = {}
    path = tmp_path / "baseline.json"
    _write_json(path, baseline)

    with pytest.raises(ValueError, match="invalid Stage 4 T0 gate baseline"):
        maintenance._load_t0_gate_baseline(path)


def test_gate_baseline_t0_must_match_readiness_t0(tmp_path: Path) -> None:
    day = "2026-07-28"
    _write_json(
        tmp_path / "results" / "data_health" / "round7_forward_pit_readiness_latest.json",
        _readiness(day, [day]),
    )
    _write_json(
        tmp_path / "results" / "data_health" / "round8_t0_birth_evidence_latest.json",
        _birth(day),
    )
    _run_evidence(tmp_path, day, shadow_payload=_shadow(day, evaluated=1, insufficient=1))
    gate_paths, t0_hashes = _gate_fixture(tmp_path)

    evidence = _collect(
        tmp_path,
        as_of=day,
        expected_dates=[day],
        gate_paths=gate_paths,
        t0_gate_hashes=t0_hashes,
    )

    assert "qualification_gate_t0_mismatch" in evidence["reason_codes"]
    assert evidence["preregistered_checks"]["qualification_gate_frozen"][
        "t0_matches_readiness"
    ] is False


def test_t0_birth_identity_must_match_coverage_identity(tmp_path: Path) -> None:
    day = "2026-07-27"
    _write_json(
        tmp_path / "results" / "data_health" / "round7_forward_pit_readiness_latest.json",
        _readiness(day, [day]),
    )
    birth = _birth(day)
    birth["cross_checks"]["snapshot"]["snapshot_hash"] = "sha256:mismatch"
    _write_json(
        tmp_path / "results" / "data_health" / "round8_t0_birth_evidence_latest.json",
        birth,
    )
    _run_evidence(tmp_path, day, shadow_payload=_shadow(day, evaluated=1, insufficient=1))
    gate_paths, t0_hashes = _gate_fixture(tmp_path)

    evidence = _collect(
        tmp_path,
        as_of=day,
        expected_dates=[day],
        gate_paths=gate_paths,
        t0_gate_hashes=t0_hashes,
    )

    assert "t0_birth_cross_check_mismatch" in evidence["reason_codes"]
    assert evidence["t0_birth_cross_check"]["matches_readiness"] is False


def test_later_same_day_conflict_does_not_replace_written_eligible_archive(
    tmp_path: Path,
) -> None:
    day = "2026-07-27"
    _write_json(
        tmp_path / "results" / "data_health" / "round7_forward_pit_readiness_latest.json",
        _readiness(day, [day]),
    )
    _write_json(
        tmp_path / "results" / "data_health" / "round8_t0_birth_evidence_latest.json",
        _birth(day),
    )
    _run_evidence(
        tmp_path,
        day,
        shadow_payload=_shadow(day, evaluated=1, insufficient=1),
    )
    later_run_id = "daily-data-chain-20260727T103000Z-conflict"
    later_dir = tmp_path / "results" / "data_chain" / "runs" / later_run_id
    conflict = {
        "status": "degraded",
        "evidence_eligible": False,
        "written": False,
        "canonical_write": False,
        "reason_codes": ["sector_history_same_day_conflict"],
    }
    _write_json(
        later_dir / "manifest.json",
        {
            "task_type": "daily_data_chain",
            "run_id": later_run_id,
            "target_date": day,
            "authoritative": True,
            "published_latest": True,
            "completed_at": f"{day}T18:45:00",
            "status": "blocked",
            "universe": {"current_analysis_pool": {"symbol_count": 914}},
            "components": {"sector_history_archive": conflict},
        },
    )
    _write_json(later_dir / "sector_history_archive_attempt.json", conflict)
    gate_paths, t0_hashes = _gate_fixture(tmp_path)

    evidence = _collect(
        tmp_path,
        as_of=day,
        expected_dates=[day],
        gate_paths=gate_paths,
        t0_gate_hashes=t0_hashes,
    )

    coverage = evidence["coverage_days"][0]
    assert coverage["complete"] is True
    assert coverage["run_id"].endswith("fixture")
    assert coverage["archive_written"] is True
    assert "sector_history_same_day_conflict" not in coverage["reason_codes"]


def test_verified_recovery_reuse_counts_as_landed_archive(tmp_path: Path) -> None:
    day = "2026-07-27"
    source_run_id = "daily-data-chain-20260727T074500Z-abandoned"
    recovery_run_id = "daily-data-chain-20260727T103000Z-recovery"
    snapshot = _snapshot(day)
    _write_json(
        tmp_path / "results" / "data_health" / "round7_forward_pit_readiness_latest.json",
        _readiness(day, [day]),
    )
    birth = _birth(day)
    birth["cross_checks"]["chain_manifest"]["run_id"] = recovery_run_id
    _write_json(
        tmp_path / "results" / "data_health" / "round8_t0_birth_evidence_latest.json",
        birth,
    )
    _write_json(
        tmp_path / "data" / "sector_history" / day / "snapshot.json",
        snapshot,
    )
    source_attempt = {**snapshot, "written": True}
    _write_json(
        tmp_path
        / "results"
        / "data_chain"
        / "runs"
        / source_run_id
        / "sector_history_archive_attempt.json",
        source_attempt,
    )
    recovery_component = {
        **snapshot,
        "written": False,
        "recovery_reuse": True,
        "recovery_source_run_id": source_run_id,
        "original_snapshot_hash": snapshot["snapshot_hash"],
    }
    recovery_dir = (
        tmp_path / "results" / "data_chain" / "runs" / recovery_run_id
    )
    _write_json(
        recovery_dir / "manifest.json",
        {
            "task_type": "daily_data_chain",
            "run_id": recovery_run_id,
            "target_date": day,
            "authoritative": True,
            "published_latest": True,
            "completed_at": f"{day}T18:45:00",
            "status": "blocked",
            "recovery_source_run_id": source_run_id,
            "universe": {"current_analysis_pool": {"symbol_count": 914}},
            "components": {"sector_history_archive": recovery_component},
        },
    )
    _write_json(
        recovery_dir / "sector_history_archive_attempt.json",
        recovery_component,
    )
    shadow_archive.write_shadow_summary(
        _shadow(day, evaluated=1, insufficient=1),
        output_path=tmp_path / "results" / "risk_gate_shadow_latest.json",
    )
    gate_paths, t0_hashes = _gate_fixture(tmp_path)

    evidence = _collect(
        tmp_path,
        as_of=day,
        expected_dates=[day],
        gate_paths=gate_paths,
        t0_gate_hashes=t0_hashes,
    )

    coverage = evidence["coverage_days"][0]
    assert coverage["complete"] is True
    assert coverage["archive_written"] is False
    assert coverage["archive_recovery_reuse"] is True
    assert coverage["recovery_source_run_id"] == source_run_id
    assert coverage["recovery_source_attempt_written"] is True


def test_recovery_reuse_rejects_mismatched_source_attempt(tmp_path: Path) -> None:
    day = "2026-07-27"
    source_run_id = "daily-data-chain-20260727T074500Z-abandoned"
    recovery_run_id = "daily-data-chain-20260727T103000Z-recovery"
    snapshot = _snapshot(day)
    _write_json(
        tmp_path / "results" / "data_health" / "round7_forward_pit_readiness_latest.json",
        _readiness(day, [day]),
    )
    _write_json(
        tmp_path / "data" / "sector_history" / day / "snapshot.json",
        snapshot,
    )
    source_attempt = {
        **snapshot,
        "written": True,
        "snapshot_hash": "sha256:" + "0" * 64,
    }
    _write_json(
        tmp_path
        / "results"
        / "data_chain"
        / "runs"
        / source_run_id
        / "sector_history_archive_attempt.json",
        source_attempt,
    )
    recovery_component = {
        **snapshot,
        "written": False,
        "recovery_reuse": True,
        "recovery_source_run_id": source_run_id,
        "original_snapshot_hash": snapshot["snapshot_hash"],
    }
    recovery_dir = (
        tmp_path / "results" / "data_chain" / "runs" / recovery_run_id
    )
    _write_json(
        recovery_dir / "manifest.json",
        {
            "task_type": "daily_data_chain",
            "run_id": recovery_run_id,
            "target_date": day,
            "authoritative": True,
            "published_latest": True,
            "status": "blocked",
            "recovery_source_run_id": source_run_id,
            "universe": {"current_analysis_pool": {"symbol_count": 914}},
            "components": {"sector_history_archive": recovery_component},
        },
    )
    _write_json(
        recovery_dir / "sector_history_archive_attempt.json",
        recovery_component,
    )
    gate_paths, t0_hashes = _gate_fixture(tmp_path)

    evidence = _collect(
        tmp_path,
        as_of=day,
        expected_dates=[day],
        gate_paths=gate_paths,
        t0_gate_hashes=t0_hashes,
    )

    coverage = evidence["coverage_days"][0]
    assert coverage["complete"] is False
    assert "recovery_source_attempt_snapshot_hash_mismatch" in coverage["reason_codes"]


def test_newer_manifest_with_missing_attempt_does_not_mask_complete_pair(
    tmp_path: Path,
) -> None:
    day = "2026-07-27"
    _write_json(
        tmp_path / "results" / "data_health" / "round7_forward_pit_readiness_latest.json",
        _readiness(day, [day]),
    )
    _write_json(
        tmp_path / "results" / "data_health" / "round8_t0_birth_evidence_latest.json",
        _birth(day),
    )
    _run_evidence(
        tmp_path,
        day,
        shadow_payload=_shadow(day, evaluated=1, insufficient=1),
    )
    newer_run_id = "daily-data-chain-20260727T103000Z-missing-attempt"
    newer_dir = tmp_path / "results" / "data_chain" / "runs" / newer_run_id
    component = {**_snapshot(day), "written": True}
    _write_json(
        newer_dir / "manifest.json",
        {
            "task_type": "daily_data_chain",
            "run_id": newer_run_id,
            "target_date": day,
            "authoritative": True,
            "published_latest": True,
            "completed_at": f"{day}T18:45:00",
            "status": "blocked",
            "universe": {"current_analysis_pool": {"symbol_count": 914}},
            "components": {"sector_history_archive": component},
        },
    )
    gate_paths, t0_hashes = _gate_fixture(tmp_path)

    evidence = _collect(
        tmp_path,
        as_of=day,
        expected_dates=[day],
        gate_paths=gate_paths,
        t0_gate_hashes=t0_hashes,
    )

    coverage = evidence["coverage_days"][0]
    assert coverage["complete"] is True
    assert coverage["run_id"].endswith("fixture")
    assert coverage["attempt_written"] is True


@pytest.mark.parametrize(
    ("missing_field", "expected_reason"),
    [
        ("manifest_source", "recovery_source_run_id_mismatch"),
        ("component_source", "recovery_source_run_id_mismatch"),
        ("attempt_source", "recovery_source_run_id_mismatch"),
        ("component_original_hash", "recovery_original_snapshot_hash_mismatch"),
        ("attempt_original_hash", "recovery_original_snapshot_hash_mismatch"),
    ],
)
def test_recovery_reuse_requires_all_identity_fields(
    tmp_path: Path,
    missing_field: str,
    expected_reason: str,
) -> None:
    day = "2026-07-27"
    source_run_id = "daily-data-chain-20260727T074500Z-abandoned"
    recovery_run_id = "daily-data-chain-20260727T103000Z-recovery"
    snapshot = _snapshot(day)
    _write_json(
        tmp_path / "results" / "data_health" / "round7_forward_pit_readiness_latest.json",
        _readiness(day, [day]),
    )
    _write_json(
        tmp_path / "data" / "sector_history" / day / "snapshot.json",
        snapshot,
    )
    _write_json(
        tmp_path
        / "results"
        / "data_chain"
        / "runs"
        / source_run_id
        / "sector_history_archive_attempt.json",
        {**snapshot, "written": True},
    )
    recovery_component = {
        **snapshot,
        "written": False,
        "recovery_reuse": True,
        "recovery_source_run_id": source_run_id,
        "original_snapshot_hash": snapshot["snapshot_hash"],
    }
    recovery_attempt = dict(recovery_component)
    manifest_source: str | None = source_run_id
    if missing_field == "manifest_source":
        manifest_source = None
    elif missing_field == "component_source":
        recovery_component.pop("recovery_source_run_id")
    elif missing_field == "attempt_source":
        recovery_attempt.pop("recovery_source_run_id")
    elif missing_field == "component_original_hash":
        recovery_component.pop("original_snapshot_hash")
    elif missing_field == "attempt_original_hash":
        recovery_attempt.pop("original_snapshot_hash")
    recovery_dir = (
        tmp_path / "results" / "data_chain" / "runs" / recovery_run_id
    )
    manifest = {
        "task_type": "daily_data_chain",
        "run_id": recovery_run_id,
        "target_date": day,
        "authoritative": True,
        "published_latest": True,
        "completed_at": f"{day}T18:45:00",
        "status": "blocked",
        "universe": {"current_analysis_pool": {"symbol_count": 914}},
        "components": {"sector_history_archive": recovery_component},
    }
    if manifest_source is not None:
        manifest["recovery_source_run_id"] = manifest_source
    _write_json(recovery_dir / "manifest.json", manifest)
    _write_json(
        recovery_dir / "sector_history_archive_attempt.json",
        recovery_attempt,
    )
    gate_paths, t0_hashes = _gate_fixture(tmp_path)

    evidence = _collect(
        tmp_path,
        as_of=day,
        expected_dates=[day],
        gate_paths=gate_paths,
        t0_gate_hashes=t0_hashes,
    )

    coverage = evidence["coverage_days"][0]
    assert coverage["complete"] is False
    assert expected_reason in coverage["reason_codes"]


def test_default_t0_gate_baseline_has_auditable_source_provenance() -> None:
    maintenance = importlib.import_module(
        "quantdesk.paper.stage4_maintenance_evidence"
    )

    baseline = maintenance.T0_GATE_BASELINE
    assert maintenance.T0_GATE_BASELINE_PATH.is_file()
    assert baseline["t0"] == "2026-07-27"
    assert baseline["captured_at"]
    assert baseline["capture_basis"] == "post_t0_freeze_with_pre_t0_mtime_check"
    assert set(baseline["files"]) == set(maintenance.T0_QUALIFICATION_GATE_SHA256)


def test_t0_plus_18_closes_irrecoverable_missing_shadow_as_inconclusive() -> None:
    disposition = stage4_terminal_disposition(
        terminal_reached=True,
        maintenance_ready=False,
        account_replay="observed_real_pending_maintenance_gates",
        missing_shadow_dates=[
            "2026-07-28",
            "2026-07-29",
            "2026-07-31",
            "2026-08-04",
        ],
    )

    assert disposition["status"] == "closed_inconclusive"
    assert disposition["maintenance_disposition"] == "inconclusive"
    assert disposition["sample_disposition"] == "not_evaluable"
    assert "missing_shadow_observations_irrecoverable" in disposition["reason_codes"]
