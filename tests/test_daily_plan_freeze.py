from __future__ import annotations

import hashlib
import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest


SHANGHAI = ZoneInfo("Asia/Shanghai")


def _write_calendar(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "date,is_open,source,note\n"
        "2026-07-16,true,test,\n"
        "2026-07-17,true,test,\n",
        encoding="utf-8",
    )
    return path


def _completed_manifest() -> dict[str, object]:
    target_date = "2026-07-16"
    symbols = ["600000"]
    universe_sha = hashlib.sha256(
        json.dumps(
            {
                "name": "current_analysis_pool",
                "target_date": target_date,
                "symbols": symbols,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    return {
        "status": "completed",
        "authoritative": True,
        "network": True,
        "dry_run": False,
        "protected_state_unchanged": True,
        "run_id": "daily-run-20260716",
        "target_date": target_date,
        "daily_data_chain_contract_version": 10,
        "universe": {
            "current_analysis_pool": {
                "name": "current_analysis_pool",
                "target_date": target_date,
                "version": f"current_analysis_pool:{target_date}:fixture",
                "sha256": universe_sha,
                "symbol_count": len(symbols),
                "symbols": symbols,
            }
        },
        "coverage": {
            "daily": {"status": "passed"},
            "current_analysis_pool": {"status": "passed"},
            "features": {"status": "passed", "output_sha256": "b" * 64},
            "session_expectation": {
                "status": "passed",
                "unknown_count": 0,
                "unknown_symbols": [],
            },
        },
        "components": {
            "features": {
                "status": "passed",
                "latest_date": target_date,
                "accepted_price_basis": "raw",
                "output_sha256": "b" * 64,
                "per_symbol": {
                    "600000": {
                        "eligible": True,
                        "price_basis": "raw",
                        "latest_date": target_date,
                    }
                },
            },
            "session_expectation": {
                "target_date": target_date,
                "status": "passed",
                "formal_eligible": True,
                "symbols": symbols,
                "classification_counts": {"expected_trade": 1, "unknown": 0},
                "counts": {"expected_trade": 1, "unknown": 0},
                "unknown_symbols": [],
            },
            "critical_sources": {
                "status": "passed",
                "formal_eligible": True,
                "failed_sources": [],
            },
        },
    }


def _write_formal_candidate_batch(root: Path) -> tuple[dict[str, object], Path]:
    from quantdesk.daily_candidate_refresh import (
        build_formal_candidate_batch,
        verify_formal_candidate_batch,
    )

    target_date = "2026-07-16"
    batch = build_formal_candidate_batch(
        data_chain_manifest=_completed_manifest(),
        strategy_payload={
            "ok": True,
            "summary": {"latest_daily_date": target_date},
            "daily_feature_status": {"latest_date": target_date},
            "standard_candidates": [
                {
                    "symbol": "600000",
                    "name": "fixture",
                    "strategy_type": "long",
                    "score": 101.5,
                    "reason": "fixture research signal",
                    "candidate_source": "auto_strategy_sandbox",
                    "data_latest_date": target_date,
                    "is_target_date_covered": True,
                    "raw": {
                        "data_health_status": "ok",
                        "daily_feature_available": True,
                        "daily_feature_latest_date": target_date,
                    },
                }
            ],
        },
        captured_at="2026-07-16T18:30:00+08:00",
    )
    assert verify_formal_candidate_batch(batch) == []
    path = root / "results" / "candidate_refresh" / "daily" / f"{target_date}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(batch), encoding="utf-8")
    return batch, path


def _candidate_strategy_payload(target_date: str = "2026-07-16") -> dict[str, object]:
    return {
        "ok": True,
        "summary": {"latest_daily_date": target_date},
        "daily_feature_status": {"latest_date": target_date},
        "standard_candidates": [
            {
                "symbol": "600000",
                "name": "fixture",
                "strategy_type": "long",
                "score": 101.5,
                "reason": "fixture research signal",
                "candidate_source": "auto_strategy_sandbox",
                "data_latest_date": target_date,
                "is_target_date_covered": True,
                "raw": {
                    "data_health_status": "ok",
                    "daily_feature_available": True,
                    "daily_feature_latest_date": target_date,
                },
            }
        ],
    }


def test_upstream_failure_never_overwrites_existing_plan(tmp_path: Path) -> None:
    from quantdesk.daily_plan_freeze import freeze_t_plus_one_plan

    output_path = tmp_path / "results" / "conditional_trade_plans_latest.json"
    output_path.parent.mkdir(parents=True)
    original = b'{"plan_id":"previous-valid-plan"}\n'
    output_path.write_bytes(original)
    manifest = _completed_manifest()
    manifest["status"] = "partial"

    def forbidden(**kwargs):
        raise AssertionError("candidate or plan builder must not run")

    result = freeze_t_plus_one_plan(
        project_root=tmp_path,
        data_chain_manifest=manifest,
        now=datetime(2026, 7, 16, 18, 30, tzinfo=SHANGHAI),
        calendar_path=_write_calendar(tmp_path / "data" / "calendar.csv"),
        strategy_runner=forbidden,
        plan_builder=forbidden,
    )

    assert result["status"] == "blocked"
    assert result["written"] is False
    assert result["reason_codes"] == ["upstream_data_chain_not_completed"]
    assert output_path.read_bytes() == original


def test_old_or_incomplete_chain_contract_cannot_freeze_plan(tmp_path: Path) -> None:
    from quantdesk.daily_plan_freeze import freeze_t_plus_one_plan

    manifest = _completed_manifest()
    manifest["daily_data_chain_contract_version"] = 9
    manifest["run_id"] = ""
    manifest["universe"] = {"current_analysis_pool": {"sha256": "short"}}
    manifest["coverage"]["features"].pop("output_sha256")
    manifest["components"]["features"].pop("output_sha256")

    def forbidden(**kwargs):
        raise AssertionError("invalid chain contract must stop before builders")

    result = freeze_t_plus_one_plan(
        project_root=tmp_path,
        data_chain_manifest=manifest,
        now=datetime(2026, 7, 16, 18, 30, tzinfo=SHANGHAI),
        calendar_path=_write_calendar(tmp_path / "data" / "calendar.csv"),
        strategy_runner=forbidden,
        plan_builder=forbidden,
    )

    assert result["status"] == "blocked"
    assert result["written"] is False
    assert "data_chain_contract_version_mismatch" in result["reason_codes"]
    assert "data_chain_run_id_missing" in result["reason_codes"]
    assert "data_chain_universe_sha256_missing" in result["reason_codes"]
    assert "data_chain_feature_sha256_missing" in result["reason_codes"]


def test_blocked_freeze_surfaces_retroactive_previous_plan_evidence(
    tmp_path: Path,
) -> None:
    from quantdesk.daily_plan_freeze import freeze_t_plus_one_plan

    output_path = tmp_path / "results" / "conditional_trade_plans_latest.json"
    output_path.parent.mkdir(parents=True)
    output_path.write_text(
        json.dumps(
            {
                "plan_id": "retroactive",
                "plan_date": "2026-07-15",
                "trade_date": "2026-07-16",
                "source_data_cutoff": "2026-07-15",
                "generated_at": "2026-07-17T12:20:01+08:00",
                "symbols": [{"symbol": "600000"}],
            }
        ),
        encoding="utf-8",
    )
    manifest = _completed_manifest()
    manifest["status"] = "partial"

    result = freeze_t_plus_one_plan(
        project_root=tmp_path,
        data_chain_manifest=manifest,
        now=datetime(2026, 7, 17, 16, 0, tzinfo=SHANGHAI),
        calendar_path=_write_calendar(tmp_path / "data" / "calendar.csv"),
    )

    evidence = result["previous_plan_evidence"]
    assert result["status"] == "blocked"
    assert evidence["alignment"] == "retroactive"
    assert evidence["plan_freeze_status"] == "blocked"
    assert "plan_generated_after_freeze" in evidence["reason_codes"]


def test_retroactive_persisted_plan_is_visible_but_cannot_be_replayed(
    tmp_path: Path,
) -> None:
    from quantdesk.paper.conditional_trade_plan import conditional_trade_plan_latest
    from quantdesk.paper.position_management_engine import simulate_conditional_plan_file

    plan_path = tmp_path / "conditional_trade_plans_latest.json"
    plan_path.write_text(
        json.dumps(
            {
                "plan_id": "retroactive",
                "plan_date": "2026-07-15",
                "trade_date": "2026-07-16",
                "source_data_cutoff": "2026-07-15",
                "generated_at": "2026-07-17T12:20:01+08:00",
                "symbols": [{"symbol": "600000"}],
            }
        ),
        encoding="utf-8",
    )

    latest = conditional_trade_plan_latest(plan_path)
    replay = simulate_conditional_plan_file(
        latest,
        intraday_dir=tmp_path / "intraday",
        write=False,
        include_risk_overlays=False,
    )

    assert latest["exists"] is True
    assert latest["plan_validation"]["status"] == "blocked"
    assert latest["formal_execution_eligible"] is False
    assert "plan_generated_after_freeze" in latest["plan_validation"]["reason_codes"]
    assert replay["ok"] is False
    assert replay["status"] == "blocked"
    assert replay["events"] == []
    assert "plan_generated_after_freeze" in replay["reason_codes"]


def test_temporally_valid_legacy_file_without_chain_provenance_cannot_replay(
    tmp_path: Path,
) -> None:
    from quantdesk.paper.conditional_trade_plan import conditional_trade_plan_latest
    from quantdesk.paper.position_management_engine import simulate_conditional_plan_file

    plan_path = tmp_path / "conditional_trade_plans_latest.json"
    plan_path.write_text(
        json.dumps(
            {
                "plan_id": "legacy-unbound",
                "plan_date": "2026-07-15",
                "trade_date": "2026-07-16",
                "source_data_cutoff": "2026-07-15",
                "generated_at": "2026-07-15T18:00:00+08:00",
                "symbols": [{"symbol": "600000"}],
            }
        ),
        encoding="utf-8",
    )

    latest = conditional_trade_plan_latest(plan_path)
    replay = simulate_conditional_plan_file(
        latest,
        intraday_dir=tmp_path / "intraday",
        write=False,
        include_risk_overlays=False,
    )

    validation = latest["plan_validation"]
    assert validation["temporal_eligible"] is True
    assert validation["formal_data_chain_eligible"] is False
    assert validation["status"] == "blocked"
    assert "data_chain_freeze_status_missing" in validation["reason_codes"]
    assert replay["ok"] is False
    assert replay["status"] == "blocked"
    assert "data_chain_freeze_status_missing" in replay["reason_codes"]


def _bound_plan_payload() -> dict[str, object]:
    return {
        "ok": True,
        "plan_id": "plan_20260716_for_20260717",
        "plan_date": "2026-07-16",
        "trade_date": "2026-07-17",
        "source_data_cutoff": "2026-07-16",
        "generated_at": "2026-07-16T18:00:00+08:00",
        "freeze_status": "completed",
        "data_chain_run_id": "bound-run",
        "data_chain_target_date": "2026-07-16",
        "data_chain_contract_version": 10,
        "data_chain_universe_sha256": "a" * 64,
        "data_chain_feature_sha256": "b" * 64,
        "formal_execution_eligible": True,
        "price_data_validation": {
            "status": "passed",
            "target_date": "2026-07-16",
            "formal_execution_eligible": True,
            "blocked_symbols": [],
            "reason_codes": [],
            "items": [],
        },
        "symbols": [],
    }


def _bound_manifest() -> dict[str, object]:
    return {
        "task_type": "daily_data_chain",
        "status": "completed",
        "authoritative": True,
        "published_latest": True,
        "run_id": "bound-run",
        "target_date": "2026-07-16",
        "daily_data_chain_contract_version": 10,
        "universe": {
            "current_analysis_pool": {"sha256": "a" * 64}
        },
        "coverage": {
            "daily": {"status": "passed"},
            "current_analysis_pool": {"status": "passed"},
            "features": {"status": "passed"},
        },
        "components": {
            "features": {"output_sha256": "b" * 64},
            "conditional_plan_freeze": {
                "status": "completed",
                "written": True,
                "plan_id": "plan_20260716_for_20260717",
            },
        },
    }


def test_bound_plan_without_terminal_manifests_is_not_formal(tmp_path: Path) -> None:
    from quantdesk.paper.conditional_trade_plan import conditional_trade_plan_latest

    plan_path = tmp_path / "results" / "conditional_trade_plans_latest.json"
    plan_path.parent.mkdir(parents=True)
    plan_path.write_text(json.dumps(_bound_plan_payload()), encoding="utf-8")

    latest = conditional_trade_plan_latest(plan_path)

    assert latest["formal_execution_eligible"] is False
    assert latest["plan_validation"]["status"] == "blocked"
    assert "data_chain_terminal_manifest_missing" in latest["plan_validation"]["reason_codes"]


def test_bound_plan_requires_matching_run_and_authoritative_latest(
    tmp_path: Path,
) -> None:
    from quantdesk.paper.conditional_trade_plan import conditional_trade_plan_latest

    plan_path = tmp_path / "results" / "conditional_trade_plans_latest.json"
    plan_path.parent.mkdir(parents=True)
    plan_path.write_text(json.dumps(_bound_plan_payload()), encoding="utf-8")
    manifest = _bound_manifest()
    run_manifest = (
        tmp_path
        / "results"
        / "data_chain"
        / "runs"
        / "bound-run"
        / "manifest.json"
    )
    run_manifest.parent.mkdir(parents=True)
    run_manifest.write_text(json.dumps(manifest), encoding="utf-8")
    authoritative = (
        tmp_path / "results" / "data_chain" / "daily_data_chain_latest.json"
    )
    authoritative.write_text(json.dumps(manifest), encoding="utf-8")

    latest = conditional_trade_plan_latest(plan_path)

    assert latest["formal_execution_eligible"] is True
    assert latest["plan_validation"]["status"] == "passed"
    assert latest["plan_validation"]["terminal_manifest_status"] == "matched_completed"


def test_bound_plan_candidate_envelope_must_match_top_level_batch_provenance(
    tmp_path: Path,
) -> None:
    from quantdesk.paper.conditional_trade_plan import conditional_trade_plan_latest

    payload = _bound_plan_payload()
    batch_path = (
        tmp_path
        / "results"
        / "candidate_refresh"
        / "runs"
        / "bound-run"
        / "formal_candidate_batch.json"
    )
    candidate = {
        "symbol": "600000",
        "strategy_type": "long",
        "data_latest_date": "2026-07-16",
        "daily_feature_latest_date": "2026-07-16",
        "close": 10.0,
    }
    candidate_sha = hashlib.sha256(
        json.dumps(
            candidate,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()
    payload.update(
        {
            "formal_candidate_batch_path": str(batch_path),
            "formal_candidate_batch_provenance_hash": "c" * 64,
            "formal_candidate_batch_source_mode": "run_scoped_conflict_recovery",
            "symbols": [
                {
                    "symbol": "600000",
                    "strategy_type": "long",
                    "position_state": "new_candidate",
                    "candidate_envelope": {
                        "schema_version": "formal_candidate_envelope_v1",
                        "target_date": "2026-07-16",
                        "batch_path": str(batch_path),
                        "batch_provenance_hash": "c" * 64,
                        "source_run_id": "bound-run",
                        "candidate_sha256": candidate_sha,
                        "candidate": candidate,
                    },
                }
            ],
            "price_data_validation": {
                "status": "passed",
                "target_date": "2026-07-16",
                "formal_execution_eligible": True,
                "blocked_symbols": [],
                "reason_codes": [],
                "items": [
                    {
                        "symbol": "600000",
                        "formal_execution_eligible": True,
                        "price_basis": "raw",
                        "reason_codes": [],
                    }
                ],
            },
        }
    )
    plan_path = tmp_path / "results" / "conditional_trade_plans_latest.json"
    plan_path.parent.mkdir(parents=True, exist_ok=True)
    manifest = _bound_manifest()
    run_manifest = (
        tmp_path
        / "results"
        / "data_chain"
        / "runs"
        / "bound-run"
        / "manifest.json"
    )
    latest_manifest = (
        tmp_path / "results" / "data_chain" / "daily_data_chain_latest.json"
    )
    run_manifest.parent.mkdir(parents=True, exist_ok=True)
    run_manifest.write_text(json.dumps(manifest), encoding="utf-8")
    latest_manifest.write_text(json.dumps(manifest), encoding="utf-8")
    plan_path.write_text(json.dumps(payload), encoding="utf-8")

    valid = conditional_trade_plan_latest(plan_path)
    assert valid["plan_validation"]["status"] == "passed"

    payload["symbols"][0]["candidate_envelope"]["batch_path"] = str(
        batch_path.with_name("foreign.json")
    )
    plan_path.write_text(json.dumps(payload), encoding="utf-8")
    invalid = conditional_trade_plan_latest(plan_path)

    assert invalid["plan_validation"]["status"] == "blocked"
    assert invalid["formal_execution_eligible"] is False
    assert "formal_candidate_envelope_path_mismatch:600000" in invalid[
        "plan_validation"
    ]["reason_codes"]


def test_bound_plan_with_blocked_price_contract_is_not_formal(
    tmp_path: Path,
) -> None:
    from quantdesk.paper.conditional_trade_plan import conditional_trade_plan_latest

    plan_path = tmp_path / "results" / "conditional_trade_plans_latest.json"
    plan_path.parent.mkdir(parents=True)
    payload = _bound_plan_payload()
    payload["formal_execution_eligible"] = False
    payload["price_data_validation"] = {
        "status": "blocked",
        "target_date": "2026-07-16",
        "formal_execution_eligible": False,
        "blocked_symbols": ["600000"],
        "reason_codes": ["mixed_price_basis"],
        "items": [
            {
                "symbol": "600000",
                "formal_execution_eligible": False,
                "price_basis": "mixed_or_unknown",
                "reason_codes": ["mixed_price_basis"],
            }
        ],
    }
    plan_path.write_text(json.dumps(payload), encoding="utf-8")
    manifest = _bound_manifest()
    run_manifest = (
        tmp_path
        / "results"
        / "data_chain"
        / "runs"
        / "bound-run"
        / "manifest.json"
    )
    run_manifest.parent.mkdir(parents=True)
    run_manifest.write_text(json.dumps(manifest), encoding="utf-8")
    authoritative = (
        tmp_path / "results" / "data_chain" / "daily_data_chain_latest.json"
    )
    authoritative.write_text(json.dumps(manifest), encoding="utf-8")

    latest = conditional_trade_plan_latest(plan_path)

    assert latest["formal_execution_eligible"] is False
    assert latest["plan_validation"]["status"] == "blocked"
    assert "conditional_plan_price_data_not_formal" in latest["plan_validation"]["reason_codes"]


def test_missing_persisted_plan_remains_an_explicit_missing_plan(tmp_path: Path) -> None:
    from quantdesk.paper.position_management_engine import simulate_conditional_plan_file

    replay = simulate_conditional_plan_file(
        plan_path=tmp_path / "missing-plan.json",
        intraday_dir=tmp_path / "intraday",
        write=False,
        include_risk_overlays=False,
    )

    assert replay["ok"] is False
    assert replay["error"] == "missing_plan"


def test_retroactive_generation_after_trade_open_is_blocked(tmp_path: Path) -> None:
    from quantdesk.daily_plan_freeze import freeze_t_plus_one_plan

    output_path = tmp_path / "results" / "conditional_trade_plans_latest.json"
    output_path.parent.mkdir(parents=True)
    original = b'{"plan_id":"previous-valid-plan"}\n'
    output_path.write_bytes(original)

    def forbidden(**kwargs):
        raise AssertionError("retroactive freeze must stop before candidate generation")

    result = freeze_t_plus_one_plan(
        project_root=tmp_path,
        data_chain_manifest=_completed_manifest(),
        now=datetime(2026, 7, 17, 10, 0, tzinfo=SHANGHAI),
        calendar_path=_write_calendar(tmp_path / "data" / "calendar.csv"),
        strategy_runner=forbidden,
        plan_builder=forbidden,
    )

    assert result["status"] == "blocked"
    assert result["trade_date"] == "2026-07-17"
    assert "retroactive_generation_window_closed" in result["reason_codes"]
    assert output_path.read_bytes() == original


def test_stale_strategy_or_feature_evidence_cannot_freeze_plan(tmp_path: Path) -> None:
    from quantdesk.daily_plan_freeze import freeze_t_plus_one_plan

    output_path = tmp_path / "results" / "conditional_trade_plans_latest.json"
    output_path.parent.mkdir(parents=True)
    original = b'{"plan_id":"previous-valid-plan"}\n'
    output_path.write_bytes(original)
    plan_builder_called = False
    strategy_runner_called = False

    def stale_strategy(**kwargs):
        nonlocal strategy_runner_called
        strategy_runner_called = True
        return {
            "ok": True,
            "summary": {"latest_daily_date": "2026-07-15"},
            "daily_feature_status": {"latest_date": "2026-07-15"},
            "long_candidates": [],
            "board_candidates": [],
        }

    def forbidden_plan_builder(**kwargs):
        nonlocal plan_builder_called
        plan_builder_called = True
        raise AssertionError("stale candidate evidence must not build a plan")

    result = freeze_t_plus_one_plan(
        project_root=tmp_path,
        data_chain_manifest=_completed_manifest(),
        now=datetime(2026, 7, 16, 18, 30, tzinfo=SHANGHAI),
        calendar_path=_write_calendar(tmp_path / "data" / "calendar.csv"),
        strategy_runner=stale_strategy,
        plan_builder=forbidden_plan_builder,
    )

    assert result["status"] == "blocked"
    assert result["written"] is False
    assert plan_builder_called is False
    assert strategy_runner_called is False
    assert "formal_candidate_batch_missing" in result["reason_codes"]
    assert output_path.read_bytes() == original


def test_freeze_rejects_invalid_dated_formal_batch_without_legacy_fallback(
    tmp_path: Path,
) -> None:
    from quantdesk.daily_plan_freeze import freeze_t_plus_one_plan

    output_path = tmp_path / "results" / "conditional_trade_plans_latest.json"
    output_path.parent.mkdir(parents=True)
    original = b'{"plan_id":"previous-valid-plan"}\n'
    output_path.write_bytes(original)
    dated = tmp_path / "results" / "candidate_refresh" / "daily" / "2026-07-16.json"
    dated.parent.mkdir(parents=True)
    dated.write_text(
        json.dumps(
            {
                "task_type": "formal_candidate_daily_batch",
                "target_date": "2026-07-16",
            }
        ),
        encoding="utf-8",
    )
    strategy_runner_called = False
    plan_builder_called = False

    def forbidden_strategy(**_kwargs):
        nonlocal strategy_runner_called
        strategy_runner_called = True
        raise AssertionError("legacy strategy runner must never be used")

    def forbidden_builder(**_kwargs):
        nonlocal plan_builder_called
        plan_builder_called = True
        raise AssertionError("invalid formal batch must stop before plan builder")

    result = freeze_t_plus_one_plan(
        project_root=tmp_path,
        data_chain_manifest=_completed_manifest(),
        now=datetime(2026, 7, 16, 18, 30, tzinfo=SHANGHAI),
        calendar_path=_write_calendar(tmp_path / "data" / "calendar.csv"),
        strategy_runner=forbidden_strategy,
        plan_builder=forbidden_builder,
    )

    assert result["status"] == "blocked"
    assert result["written"] is False
    assert strategy_runner_called is False
    assert plan_builder_called is False
    assert any(code.startswith("candidate_batch_") for code in result["reason_codes"])
    assert output_path.read_bytes() == original


def test_freeze_rejects_valid_batch_bound_to_another_chain_run(tmp_path: Path) -> None:
    from quantdesk.daily_candidate_refresh import (
        build_formal_candidate_batch,
        verify_formal_candidate_batch,
    )
    from quantdesk.daily_plan_freeze import freeze_t_plus_one_plan

    output_path = tmp_path / "results" / "conditional_trade_plans_latest.json"
    output_path.parent.mkdir(parents=True)
    original = b'{"plan_id":"previous-valid-plan"}\n'
    output_path.write_bytes(original)
    foreign_manifest = _completed_manifest()
    foreign_manifest["run_id"] = "foreign-chain-run"
    batch = build_formal_candidate_batch(
        data_chain_manifest=foreign_manifest,
        strategy_payload={
            "ok": True,
            "summary": {"latest_daily_date": "2026-07-16"},
            "daily_feature_status": {"latest_date": "2026-07-16"},
            "standard_candidates": [
                {
                    "symbol": "600000",
                    "strategy_type": "long",
                    "score": 101.5,
                    "candidate_source": "fixture",
                    "data_latest_date": "2026-07-16",
                    "is_target_date_covered": True,
                    "raw": {
                        "data_health_status": "ok",
                        "daily_feature_available": True,
                        "daily_feature_latest_date": "2026-07-16",
                    },
                }
            ],
        },
        captured_at="2026-07-16T18:30:00+08:00",
    )
    assert verify_formal_candidate_batch(batch) == []
    dated = tmp_path / "results" / "candidate_refresh" / "daily" / "2026-07-16.json"
    dated.parent.mkdir(parents=True)
    dated.write_text(json.dumps(batch), encoding="utf-8")

    result = freeze_t_plus_one_plan(
        project_root=tmp_path,
        data_chain_manifest=_completed_manifest(),
        now=datetime(2026, 7, 16, 18, 30, tzinfo=SHANGHAI),
        calendar_path=_write_calendar(tmp_path / "data" / "calendar.csv"),
        plan_builder=lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError("foreign formal batch must stop before plan build")
        ),
    )

    assert result["status"] == "blocked"
    assert "formal_candidate_batch_source_run_mismatch" in result["reason_codes"]
    assert output_path.read_bytes() == original


@pytest.mark.parametrize("analysis_minute_requested", [False, True])
def test_valid_post_close_run_freezes_t_plus_one_plan_with_chain_provenance(
    tmp_path: Path,
    analysis_minute_requested: bool,
) -> None:
    from quantdesk.daily_plan_freeze import freeze_t_plus_one_plan

    calendar_path = _write_calendar(tmp_path / "data" / "calendar.csv")
    formal_batch, formal_path = _write_formal_candidate_batch(tmp_path)

    def strategy_runner(**kwargs):
        raise AssertionError("dated formal batch must replace the legacy strategy runner")

    def plan_builder(**kwargs):
        assert kwargs["write"] is False
        assert kwargs["plan_date"] == "2026-07-16"
        assert kwargs["trade_date"] == "2026-07-17"
        assert kwargs["source_data_cutoff"] == "2026-07-16"
        assert kwargs["output_path"] == (
            tmp_path / "results" / "conditional_trade_plans_latest.json"
        )
        assert "auto_strategy_path" not in kwargs
        candidates = kwargs["auto_strategy_payload"]["long_candidates"]
        assert len(candidates) == 1
        envelope = candidates[0]["candidate_envelope"]
        assert envelope["batch_path"] == str(formal_path)
        assert envelope["batch_provenance_hash"] == formal_batch["provenance"]["hash"]
        assert envelope["candidate"]["symbol"] == "600000"
        return {
            "ok": True,
            "plan_id": "plan_20260716_for_20260717",
            "plan_date": "2026-07-16",
            "trade_date": "2026-07-17",
            "source_data_cutoff": "2026-07-16",
            "formal_execution_eligible": True,
            "price_data_validation": {
                "status": "passed",
                "target_date": "2026-07-16",
                "formal_execution_eligible": True,
                "blocked_symbols": [],
                "reason_codes": [],
                "items": [{"symbol": "600000", "formal_execution_eligible": True, "price_basis": "raw", "reason_codes": []}],
            },
            "symbols": [{"symbol": "600000"}],
            "summary": {"symbol_count": 1},
        }

    manifest = _completed_manifest()
    if not analysis_minute_requested:
        manifest["coverage"]["current_analysis_pool"] = {
            "status": "not_requested", "requested_count": 0, "audited_count": 0,
            "reason_codes": ["analysis_pool_minute_on_demand"],
        }
    result = freeze_t_plus_one_plan(
        project_root=tmp_path,
        data_chain_manifest=manifest,
        now=datetime(2026, 7, 16, 18, 30, tzinfo=SHANGHAI),
        calendar_path=calendar_path,
        strategy_runner=strategy_runner,
        plan_builder=plan_builder,
    )

    output_path = tmp_path / "results" / "conditional_trade_plans_latest.json"
    frozen = json.loads(output_path.read_text(encoding="utf-8"))
    assert result["status"] == "completed"
    assert result["written"] is True
    assert result["plan_id"] == "plan_20260716_for_20260717"
    assert frozen["data_chain_run_id"] == "daily-run-20260716"
    assert frozen["data_chain_target_date"] == "2026-07-16"
    assert frozen["data_chain_universe_sha256"] == _completed_manifest()["universe"][
        "current_analysis_pool"
    ]["sha256"]
    assert frozen["data_chain_feature_sha256"] == "b" * 64
    assert frozen["formal_candidate_batch_path"] == str(formal_path)
    assert frozen["formal_candidate_batch_provenance_hash"] == formal_batch[
        "provenance"
    ]["hash"]
    assert frozen["freeze_status"] == "completed"
    assert frozen["boundaries"]["research_only"] is True
    assert frozen["boundaries"]["paper_account_only"] is True
    assert frozen["boundaries"]["broker_route"] is False
    assert frozen["boundaries"]["live_execution"] is False


def test_same_day_retry_uses_exact_run_scoped_batch_without_overwriting_dated_snapshot(
    tmp_path: Path,
) -> None:
    from quantdesk.daily_candidate_refresh import (
        build_formal_candidate_batch,
        candidate_refresh_gate,
        verify_formal_candidate_batch,
    )
    from quantdesk.daily_plan_freeze import freeze_t_plus_one_plan

    current_manifest = _completed_manifest()
    old_manifest = _completed_manifest()
    old_manifest["run_id"] = "daily-run-20260716-first"
    old_manifest["components"]["features"]["output_sha256"] = "a" * 64
    old_manifest["coverage"]["features"]["output_sha256"] = "a" * 64

    old_batch = build_formal_candidate_batch(
        data_chain_manifest=old_manifest,
        strategy_payload=_candidate_strategy_payload(),
        captured_at="2026-07-16T16:00:00+08:00",
    )
    current_batch = build_formal_candidate_batch(
        data_chain_manifest=current_manifest,
        strategy_payload=_candidate_strategy_payload(),
        captured_at="2026-07-16T18:30:00+08:00",
    )
    assert verify_formal_candidate_batch(old_batch) == []
    assert verify_formal_candidate_batch(current_batch) == []

    dated_path = (
        tmp_path / "results" / "candidate_refresh" / "daily" / "2026-07-16.json"
    )
    run_path = (
        tmp_path
        / "results"
        / "candidate_refresh"
        / "runs"
        / str(current_manifest["run_id"])
        / "formal_candidate_batch.json"
    )
    dated_path.parent.mkdir(parents=True, exist_ok=True)
    run_path.parent.mkdir(parents=True, exist_ok=True)
    dated_path.write_text(json.dumps(old_batch), encoding="utf-8")
    run_path.write_text(json.dumps(current_batch), encoding="utf-8")
    dated_before = dated_path.read_bytes()

    current_manifest["components"]["candidate_refresh_gate"] = (
        candidate_refresh_gate(current_manifest)
    )
    current_manifest["components"]["candidate_refresh"] = {
        "status": "blocked",
        "applied": False,
        "reason_codes": ["candidate_dated_snapshot_conflict"],
        "dated_path": "results/candidate_refresh/daily/2026-07-16.json",
        "staging_path": (
            "results/candidate_refresh/runs/"
            f"{current_manifest['run_id']}/formal_candidate_batch.json"
        ),
    }

    def plan_builder(**kwargs):
        envelope = kwargs["auto_strategy_payload"]["long_candidates"][0][
            "candidate_envelope"
        ]
        assert Path(envelope["batch_path"]) == run_path
        assert envelope["source_run_id"] == current_manifest["run_id"]
        return {
            "ok": True,
            "plan_id": "plan_20260716_for_20260717_retry",
            "plan_date": "2026-07-16",
            "trade_date": "2026-07-17",
            "source_data_cutoff": "2026-07-16",
            "formal_execution_eligible": True,
            "price_data_validation": {
                "status": "passed",
                "target_date": "2026-07-16",
                "formal_execution_eligible": True,
                "blocked_symbols": [],
                "reason_codes": [],
                "items": [],
            },
            "symbols": [],
            "summary": {"symbol_count": 0},
        }

    result = freeze_t_plus_one_plan(
        project_root=tmp_path,
        data_chain_manifest=current_manifest,
        now=datetime(2026, 7, 16, 18, 30, tzinfo=SHANGHAI),
        calendar_path=_write_calendar(tmp_path / "data" / "calendar.csv"),
        plan_builder=plan_builder,
        write=False,
    )

    assert result["status"] == "prepared"
    assert result["formal_candidate_batch_source_mode"] == (
        "run_scoped_conflict_recovery"
    )
    assert Path(result["formal_candidate_batch_path"]) == run_path
    assert result["prepared_plan"]["data_chain_run_id"] == current_manifest["run_id"]
    assert result["prepared_plan"]["formal_candidate_batch_source_mode"] == (
        "run_scoped_conflict_recovery"
    )
    assert dated_path.read_bytes() == dated_before


def test_prepare_mode_returns_plan_without_overwriting_latest(tmp_path: Path) -> None:
    from quantdesk.daily_plan_freeze import freeze_t_plus_one_plan

    output_path = tmp_path / "results" / "conditional_trade_plans_latest.json"
    output_path.parent.mkdir(parents=True)
    original = b'{"plan_id":"previous-valid-plan"}\n'
    output_path.write_bytes(original)
    formal_batch, formal_path = _write_formal_candidate_batch(tmp_path)

    def strategy_runner(**kwargs):
        raise AssertionError("dated formal batch must replace the legacy strategy runner")

    def plan_builder(**kwargs):
        envelope = kwargs["auto_strategy_payload"]["long_candidates"][0][
            "candidate_envelope"
        ]
        assert envelope["batch_path"] == str(formal_path)
        return {
            "ok": True,
            "plan_id": "plan_20260716_for_20260717",
            "plan_date": "2026-07-16",
            "trade_date": "2026-07-17",
            "source_data_cutoff": "2026-07-16",
            "formal_execution_eligible": True,
            "price_data_validation": {
                "status": "passed",
                "target_date": "2026-07-16",
                "formal_execution_eligible": True,
                "blocked_symbols": [],
                "reason_codes": [],
                "items": [],
            },
            "symbols": [],
            "summary": {"symbol_count": 0},
        }

    result = freeze_t_plus_one_plan(
        project_root=tmp_path,
        data_chain_manifest=_completed_manifest(),
        now=datetime(2026, 7, 16, 18, 30, tzinfo=SHANGHAI),
        calendar_path=_write_calendar(tmp_path / "data" / "calendar.csv"),
        strategy_runner=strategy_runner,
        plan_builder=plan_builder,
        write=False,
    )

    assert result["status"] == "prepared"
    assert result["prepared"] is True
    assert result["written"] is False
    assert result["prepared_plan"]["data_chain_run_id"] == "daily-run-20260716"
    assert result["formal_candidate_batch_path"] == str(formal_path)
    assert result["formal_candidate_batch_provenance_hash"] == formal_batch[
        "provenance"
    ]["hash"]
    assert output_path.read_bytes() == original


def test_authoritative_chain_preserves_completed_data_when_plan_freeze_gate_fails(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from quantdesk import daily_data_chain as chain

    symbol = "600000"
    monkeypatch.setattr(
        chain,
        "build_runtime_preflight",
        lambda *args, **kwargs: {
            "status": "passed",
            "ready": True,
            "active_runtime_ready": True,
            "reason_codes": [],
        },
    )
    monkeypatch.setattr(
        chain,
        "resolve_symbols",
        lambda **kwargs: [{"symbol": symbol}],
    )
    monkeypatch.setattr(
        chain,
        "collect_intraday_required_symbols",
        lambda **kwargs: {"items": []},
    )
    monkeypatch.setattr(
        chain,
        "update_daily_prices",
        lambda **kwargs: {
            "ok": True,
            "status": "completed",
            "target_date": kwargs["target_date"],
            "symbols_checked": 1,
            "stale_count_after": 0,
            "missing_count_after": 0,
            "failed_symbol_list": [],
            "invalid_price_basis_symbols": [],
        },
    )
    monkeypatch.setattr(
        chain,
        "build_session_expectation",
        lambda symbols, target_date, **kwargs: {
            "target_date": target_date,
            "symbols": list(symbols),
            "legitimate_no_session_symbols": [],
            "unknown_symbols": [],
        },
    )
    monkeypatch.setattr(
        chain,
        "update_intraday_1m_for_symbols",
        lambda symbols, target_date, **kwargs: {
            "ok": True,
            "status": "completed",
            "updated_symbols": len(symbols),
            "unchanged_symbols": 0,
            "failed_symbols": 0,
            "failed_symbol_list": [],
            "processed_symbols": list(symbols),
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
                "invalid_session": 0,
            },
            "items": [
                {
                    "symbol": item,
                    "complete_session": True,
                    "formal_execution_eligible": True,
                    "price_basis": "raw",
                    "session_reason_codes": [],
                }
                for item in symbols
            ],
        },
    )
    monkeypatch.setattr(
        chain,
        "build_daily_features",
        lambda **kwargs: {
            "ok": True,
            "status": "passed",
            "requested_count": 1,
            "feature_count": 1,
            "output_row_count": 1,
            "output_sha256": "b" * 64,
            "skipped_count": 0,
            "accepted_price_basis": "raw",
            "feature_input_snapshot_stable": True,
            "price_policy_validation": {
                "status": "passed",
                "run_status": "passed",
                "eligible": True,
                "accepted_price_basis": "raw",
                "blocked_symbols": [],
                "reason_codes": [],
            },
            "per_symbol": {
                symbol: {
                    "eligible": True,
                    "price_basis": "raw",
                    "latest_date": "2026-07-16",
                }
            },
        },
    )
    freeze_calls: list[dict[str, object]] = []
    order: list[str] = []

    def blocked_freeze(**kwargs):
        order.append("freeze")
        freeze_calls.append(kwargs)
        assert kwargs["data_chain_manifest"]["status"] == "completed"
        return {
            "ok": False,
            "status": "blocked",
            "written": False,
            "reason_codes": ["retroactive_generation_window_closed"],
        }

    monkeypatch.setattr(chain, "freeze_t_plus_one_plan", blocked_freeze)
    monkeypatch.setattr(
        chain,
        "_run_candidate_refresh_step",
        lambda **kwargs: order.append("candidate_refresh") or {
            "status": "completed",
            "applied": True,
            "reason_codes": [],
        },
    )
    monkeypatch.setattr(
        chain,
        "_refresh_data_health_snapshot",
        lambda root, manifest: {"ok": True, "status": "refreshed"},
    )
    universe = tmp_path / "data" / "universe" / "a_share_symbols.csv"
    universe.parent.mkdir(parents=True, exist_ok=True)
    universe.write_text("symbol,name\n600000,fixture\n", encoding="utf-8")

    payload = chain.run_daily_data_chain(
        project_root=tmp_path,
        target_date="2026-07-16",
        network=True,
        dry_run=False,
        run_id="plan-freeze-gate-run",
        refresh_universe=False,
    )

    assert len(freeze_calls) == 1
    assert payload["status"] == "completed"
    assert payload["ok"] is True
    assert payload["components"]["conditional_plan_freeze"]["written"] is False
    assert payload["components"]["conditional_plan_freeze"]["reason_codes"] == [
        "retroactive_generation_window_closed"
    ]
    assert "conditional_plan_freeze_blocked" in payload["warnings"]
    assert "conditional_plan_freeze_blocked" not in payload["errors"]
    assert payload["published_latest"] is True
    assert order == ["candidate_refresh", "freeze"]
