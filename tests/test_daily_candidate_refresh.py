from __future__ import annotations

import copy
import json
import math
from contextlib import contextmanager
from pathlib import Path

import pytest

from quantdesk import daily_candidate_refresh as refresh
from quantdesk.daily_candidate_refresh import (
    archive_conditional_plan,
    build_candidate_envelope,
    build_conditional_replay_receipt,
    build_formal_candidate_batch,
    candidate_refresh_gate,
    coordinate_plan_rollover,
    load_archived_conditional_plan,
    publish_archived_conditional_plan,
    publish_formal_candidate_batch,
    resolve_chain_bound_formal_candidate_batch,
    run_formal_candidate_refresh,
    verify_candidate_envelope,
    verify_conditional_replay_receipt,
    verify_formal_candidate_batch,
)
from quantdesk.candidate_tracking import materialize_formal_candidate_tracking


SHA_A = "a" * 64
SHA_B = "b" * 64


def _universe_digest(*, target_date: str, symbols: list[str]) -> str:
    return refresh._stable_hash(
        {
            "name": "current_analysis_pool",
            "target_date": target_date,
            "symbols": sorted(set(symbols)),
        }
    )


def _passed_manifest(target_date: str = "2026-08-03") -> dict:
    symbols = ["600000", "600001"]
    return {
        "daily_data_chain_contract_version": 10,
        "run_id": "daily-data-chain-20260803T074500Z-fixture",
        "target_date": target_date,
        "status": "completed",
        "authoritative": True,
        "network": True,
        "dry_run": False,
        "protected_state_unchanged": True,
        "coverage": {
            "daily": {"status": "passed"},
            "current_analysis_pool": {"status": "passed"},
            "features": {"status": "passed", "output_sha256": SHA_B},
            "session_expectation": {
                "status": "passed",
                "unknown_count": 0,
                "unknown_symbols": [],
            },
        },
        "universe": {
            "current_analysis_pool": {
                "name": "current_analysis_pool",
                "target_date": target_date,
                "symbol_count": 2,
                "symbols": symbols,
                "sha256": _universe_digest(
                    target_date=target_date, symbols=symbols
                ),
            }
        },
        "components": {
            "features": {
                "status": "passed",
                "latest_date": target_date,
                "accepted_price_basis": "raw",
                "output_sha256": SHA_B,
                "per_symbol": {
                    "600000": {
                        "eligible": True,
                        "price_basis": "raw",
                        "latest_date": target_date,
                    },
                    "600001": {
                        "eligible": True,
                        "price_basis": "raw",
                        "latest_date": target_date,
                    },
                },
            },
            "session_expectation": {
                "target_date": target_date,
                "status": "passed",
                "formal_eligible": True,
                "symbols": symbols,
                "classification_counts": {
                    "expected_trade": 2,
                    "unknown": 0,
                },
                "counts": {
                    "expected_trade": 2,
                    "unknown": 0,
                },
                "unknown_symbols": [],
            },
            "critical_sources": {
                "status": "passed",
                "formal_eligible": True,
                "failed_sources": [],
            },
        },
    }


def _degraded_914_manifest(target_date: str = "2026-08-03") -> dict:
    symbols = [f"{index:06d}" for index in range(1, 915)]
    excluded = symbols[-8:]
    current = symbols[:-8]
    manifest = _passed_manifest(target_date)
    manifest["status"] = "blocked"
    manifest["universe"]["current_analysis_pool"] = {
        "name": "current_analysis_pool",
        "target_date": target_date,
        "symbol_count": len(symbols),
        "symbols": symbols,
        "sha256": _universe_digest(target_date=target_date, symbols=symbols),
    }
    manifest["coverage"]["daily"] = {
        "status": "blocked",
        "requested_count": len(symbols),
        "reported_count": len(symbols),
        "fresh_count": len(current),
        "stale_count": 4,
        "stale_symbols": excluded[:4],
        "missing_count": 2,
        "missing_symbols": excluded[4:6],
        "failed_count": 2,
        "failed_symbols": excluded[6:],
        "invalid_price_basis_count": 0,
        "invalid_price_basis_symbols": [],
        "legitimate_no_data_symbols": [],
        "reason_codes": [
            "daily_symbols_stale",
            "daily_symbols_missing",
            "daily_symbols_failed",
        ],
    }
    manifest["coverage"]["features"] = {
        "status": "blocked",
        "output_sha256": SHA_B,
        "stale_symbols": excluded[:4],
        "missing_symbols": excluded[4:],
        "invalid_symbols": [],
        "reason_codes": ["feature_target_date_mismatch"],
    }
    manifest["components"]["features"]["per_symbol"] = {
        symbol: {
            "eligible": True,
            "price_basis": "raw",
            "latest_date": target_date,
        }
        for symbol in current
    }
    manifest["coverage"]["session_expectation"] = {
        "status": "blocked",
        "unknown_count": len(excluded),
        "unknown_symbols": excluded,
    }
    manifest["components"]["session_expectation"] = {
        "schema_version": "session_expectation.v1",
        "target_date": target_date,
        "status": "blocked",
        "formal_eligible": False,
        "symbols": symbols,
        "classification_counts": {
            "expected_trade": len(current),
            "unknown": len(excluded),
        },
        "counts": {
            "expected_trade": len(current),
            "unknown": len(excluded),
        },
        "expected_trade_symbols": current,
        "legitimate_no_session_symbols": [],
        "unknown_symbols": excluded,
    }
    manifest["components"]["critical_sources"] = {
        "status": "passed",
        "formal_eligible": True,
        "failed_sources": [],
    }
    return manifest


def _partially_blocked_manifest(target_date: str = "2026-08-03") -> dict:
    manifest = _passed_manifest(target_date)
    manifest["status"] = "blocked"
    manifest["coverage"]["daily"] = {
        "status": "blocked",
        "stale_symbols": ["600001"],
        "failed_symbols": ["600001"],
        "missing_symbols": [],
        "invalid_price_basis_symbols": [],
        "reason_codes": ["daily_symbols_stale", "daily_symbols_failed"],
    }
    manifest["coverage"]["features"] = {
        "status": "blocked",
        "output_sha256": SHA_B,
        "stale_symbols": ["600001"],
        "missing_symbols": [],
        "invalid_symbols": [],
        "reason_codes": ["feature_target_date_mismatch"],
    }
    manifest["components"]["features"]["per_symbol"]["600001"].update(
        {"latest_date": "2026-07-31"}
    )
    return manifest


def _strategy_payload(target_date: str = "2026-08-03") -> dict:
    return {
        "ok": True,
        "summary": {"latest_daily_date": target_date},
        "daily_feature_status": {"latest_date": target_date},
        "standard_candidates": [
            {
                "symbol": "600000",
                "name": "eligible",
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
            },
            {
                "symbol": "600001",
                "name": "stale",
                "strategy_type": "board",
                "score": 99.0,
                "reason": "fixture stale signal",
                "candidate_source": "auto_strategy_sandbox",
                "data_latest_date": "2026-07-31",
                "is_target_date_covered": False,
                "raw": {
                    "data_health_status": "ok",
                    "daily_feature_available": True,
                    "daily_feature_latest_date": "2026-07-31",
                },
            },
        ],
    }


def _formal_plan(
    *,
    plan_id: str,
    plan_date: str,
    trade_date: str,
    symbols: tuple[str, ...] = ("600000",),
) -> dict:
    return {
        "ok": True,
        "plan_id": plan_id,
        "plan_date": plan_date,
        "trade_date": trade_date,
        "source_data_cutoff": plan_date,
        "formal_execution_eligible": True,
        "symbols": list(symbols),
        "price_data_validation": {
            "status": "passed",
            "formal_execution_eligible": True,
            "target_date": plan_date,
            "blocked_symbols": [],
        },
    }


def _complete_minute_evidence(
    *,
    plan_id: str = "due-plan",
    trade_date: str = "2026-08-03",
    symbols: tuple[str, ...] = ("600000",),
) -> dict:
    items = [
        {
            "symbol": symbol,
            "ok": True,
            "status": "available",
            "target_date_rows": 240,
            "warnings": [],
            "errors": [],
        }
        for symbol in symbols
    ]
    return {
        "ok": True,
        "plan_id": plan_id,
        "target_trade_date": trade_date,
        "total_symbols": len(symbols),
        "available_count": len(symbols),
        "missing_count": 0,
        "invalid_count": 0,
        "stale_count": 0,
        "insufficient_count": 0,
        "available_symbols": list(symbols),
        "missing_symbols": [],
        "invalid_symbols": [],
        "stale_symbols": [],
        "insufficient_symbols": [],
        "items": items,
    }


def _partial_minute_evidence(
    *, plan_id: str = "due-plan", trade_date: str = "2026-08-03"
) -> dict:
    return {
        "ok": True,
        "plan_id": plan_id,
        "target_trade_date": trade_date,
        "total_symbols": 2,
        "available_count": 1,
        "missing_count": 1,
        "invalid_count": 0,
        "stale_count": 0,
        "insufficient_count": 0,
        "available_symbols": ["600000"],
        "missing_symbols": ["600001"],
        "invalid_symbols": [],
        "stale_symbols": [],
        "insufficient_symbols": [],
        "items": [
            {
                "symbol": "600000",
                "ok": True,
                "status": "available",
                "target_date_rows": 240,
                "warnings": [],
                "errors": [],
            },
            {
                "symbol": "600001",
                "ok": False,
                "status": "missing_intraday",
                "target_date_rows": 0,
                "warnings": [],
                "errors": ["fixture minute file missing"],
            },
        ],
    }


def _reseal_candidate_batch(payload: dict) -> None:
    payload["provenance"]["hash"] = refresh._stable_hash(
        refresh._candidate_hash_projection(payload)
    )


def _execution_payload(
    *,
    plan_id: str = "due-plan",
    trade_date: str = "2026-08-03",
    symbols: tuple[str, ...] = ("600000",),
) -> dict:
    no_fill = [
        {
            "symbol": symbol,
            "action": "no_fill",
            "fill_model": "missing_intraday",
            "reason": "fixture minute evidence did not produce a reliable fill",
            "is_reliable": False,
        }
        for symbol in symbols
    ]
    return {
        "ok": True,
        "plan_id": plan_id,
        "trade_date": trade_date,
        "execution_mode": "intraday_1m",
        "allow_daily_approx": False,
        "is_real_order": False,
        "events": [],
        "no_fill": no_fill,
        "blocked": [],
        "summary": {
            "plan_count": len(symbols),
            "event_count": 0,
            "no_fill_count": len(symbols),
            "blocked_count": 0,
        },
        "research_only": True,
        "paper_only": True,
        "paper_trading_only": True,
        "no_broker": True,
        "no_real_orders": True,
    }


def _applied_state(
    *, plan_id: str = "due-plan", trade_date: str = "2026-08-03"
) -> dict:
    return {
        "schema_version": "fixture",
        "cash": 10_000.0,
        "positions": [],
        "actions": [],
        "daily_actions": [],
        "applied_execution_ids": [f"{plan_id}:{trade_date}"],
        "last_conditional_plan_id": plan_id,
        "last_trade_date": trade_date,
    }


def _apply_result(
    *, plan_id: str = "due-plan", trade_date: str = "2026-08-03"
) -> dict:
    return {
        "ok": True,
        "already_updated_today": False,
        "state": _applied_state(plan_id=plan_id, trade_date=trade_date),
        "actions": [],
        "is_paper_simulation": True,
        "is_real_order": False,
        "research_only": True,
        "paper_only": True,
        "paper_trading_only": True,
        "no_broker": True,
        "no_real_orders": True,
    }


def _reseal_replay_receipt(receipt: dict) -> None:
    receipt["provenance"]["hash"] = refresh._stable_hash(
        refresh._replay_receipt_hash_projection(receipt)
    )


def test_formal_candidate_batch_rejects_stale_rows_and_seals_provenance() -> None:
    payload = build_formal_candidate_batch(
        data_chain_manifest=_passed_manifest(),
        strategy_payload=_strategy_payload(),
        captured_at="2026-08-03T18:02:03+08:00",
    )

    assert payload["status"] == "passed"
    assert payload["formal_eligible"] is True
    assert payload["candidate_count"] == 1
    assert payload["candidates"][0]["symbol"] == "600000"
    assert payload["candidates"][0]["source_qualification"] == {
        "universe_member": True,
        "feature_eligible": True,
        "feature_latest_date": "2026-08-03",
        "feature_price_basis": "raw",
        "excluded_by_daily_gate": False,
        "excluded_by_feature_gate": False,
    }

    assert payload["rejected_candidate_count"] == 1
    assert payload["rejected_candidates"][0]["symbol"] == "600001"
    assert "candidate_target_date_mismatch" in payload["rejected_candidates"][0]["reason_codes"]
    assert payload["provenance"]["source"] == (
        "daily_data_chain:daily-data-chain-20260803T074500Z-fixture"
    )
    assert payload["provenance"]["captured_at"] == "2026-08-03T18:02:03+08:00"
    assert len(payload["provenance"]["hash"]) == 64
    assert verify_formal_candidate_batch(payload) == []

    tampered = copy.deepcopy(payload)
    tampered["candidates"][0]["score"] = 999.0
    assert "candidate_batch_hash_mismatch" in verify_formal_candidate_batch(tampered)


def test_formal_candidate_tracking_materializer_uses_only_dated_verified_batch(
    tmp_path: Path,
) -> None:
    batch = build_formal_candidate_batch(
        data_chain_manifest=_passed_manifest(),
        strategy_payload=_strategy_payload(),
        captured_at="2026-08-03T18:02:03+08:00",
    )
    assert publish_formal_candidate_batch(tmp_path, batch)["applied"] is True

    result = materialize_formal_candidate_tracking(
        tmp_path,
        target_date="2026-08-03",
        authoritative_chain_run_id="daily-data-chain-20260803T074500Z-fixture",
    )

    assert result["status"] == "completed_with_warnings"
    assert result["candidate_count"] == 1
    assert result["source_mode"] == "dated_verified_formal_batch"
    assert result["source_run_id"] == "daily-data-chain-20260803T074500Z-fixture"
    assert result["publish_status"] == "created"
    assert (tmp_path / "results/candidate_tracking/daily/2026-08-03.json").exists()
    ledger = tmp_path / "data/tracking/candidate_ledger.csv"
    assert ledger.exists()
    assert "dated_verified_formal_batch" in ledger.read_text(encoding="utf-8")


def test_formal_candidate_tracking_materializer_fails_closed_without_dated_batch(
    tmp_path: Path,
) -> None:
    latest = tmp_path / "results/candidate_refresh/formal_candidate_batch_latest.json"
    latest.parent.mkdir(parents=True)
    latest.write_text("{}", encoding="utf-8")

    result = materialize_formal_candidate_tracking(
        tmp_path,
        target_date="2026-08-03",
        authoritative_chain_run_id="daily-data-chain-20260803T074500Z-fixture",
    )

    assert result["status"] == "blocked"
    assert "formal_candidate_batch_missing" in result["reason_codes"]
    assert not (tmp_path / "results/candidate_tracking/daily/2026-08-03.json").exists()


def test_formal_candidate_tracking_conflict_does_not_rewrite_legacy_projection(
    tmp_path: Path,
) -> None:
    batch = build_formal_candidate_batch(
        data_chain_manifest=_passed_manifest(),
        strategy_payload=_strategy_payload(),
        captured_at="2026-08-03T18:02:03+08:00",
    )
    assert publish_formal_candidate_batch(tmp_path, batch)["applied"] is True
    first = materialize_formal_candidate_tracking(
        tmp_path,
        target_date="2026-08-03",
        authoritative_chain_run_id="daily-data-chain-20260803T074500Z-fixture",
    )
    assert first["applied"] is True
    ledger = tmp_path / "data/tracking/candidate_ledger.csv"
    before = ledger.read_bytes()
    dated = tmp_path / "results/candidate_tracking/daily/2026-08-03.json"
    tampered = json.loads(dated.read_text(encoding="utf-8"))
    tampered["entries"] = []
    dated.write_text(json.dumps(tampered), encoding="utf-8")

    conflict = materialize_formal_candidate_tracking(
        tmp_path,
        target_date="2026-08-03",
        authoritative_chain_run_id="daily-data-chain-20260803T074500Z-fixture",
    )

    assert conflict["status"] == "blocked"
    assert "candidate_tracking_dated_snapshot_conflict" in conflict["reason_codes"]
    assert ledger.read_bytes() == before


def test_candidate_verifier_binds_gate_and_provenance_even_after_reseal() -> None:
    payload = build_formal_candidate_batch(
        data_chain_manifest=_passed_manifest(),
        strategy_payload=_strategy_payload(),
        captured_at="2026-08-03T18:02:03+08:00",
    )
    payload["qualification_gate"]["target_date"] = "2026-07-31"
    payload["qualification_gate"]["source_run_id"] = "other-run"
    payload["qualification_gate"]["source_universe_hash"] = "c" * 64
    payload["qualification_gate"]["source_feature_hash"] = "d" * 64
    _reseal_candidate_batch(payload)

    reasons = verify_formal_candidate_batch(payload)

    assert "candidate_batch_gate_target_date_mismatch" in reasons
    assert "candidate_batch_gate_source_run_id_mismatch" in reasons
    assert "candidate_batch_gate_universe_hash_mismatch" in reasons
    assert "candidate_batch_gate_feature_hash_mismatch" in reasons


def test_candidate_verifier_rejects_semantic_tamper_even_after_reseal() -> None:
    payload = build_formal_candidate_batch(
        data_chain_manifest=_passed_manifest(),
        strategy_payload=_strategy_payload(),
        captured_at="2026-08-03T18:02:03+08:00",
    )
    payload["candidates"][0]["data_health_status"] = "blocked"
    _reseal_candidate_batch(payload)

    assert "candidate_batch_candidate_data_health_invalid" in (
        verify_formal_candidate_batch(payload)
    )

    qualification_tamper = build_formal_candidate_batch(
        data_chain_manifest=_passed_manifest(),
        strategy_payload=_strategy_payload(),
        captured_at="2026-08-03T18:02:03+08:00",
    )
    qualification_tamper["candidates"][0]["source_qualification"][
        "universe_member"
    ] = False
    _reseal_candidate_batch(qualification_tamper)
    assert "candidate_batch_candidate_source_qualification_invalid" in (
        verify_formal_candidate_batch(qualification_tamper)
    )


def test_candidate_verifier_rejects_non_raw_feature_basis_after_reseal() -> None:
    payload = build_formal_candidate_batch(
        data_chain_manifest=_passed_manifest(),
        strategy_payload=_strategy_payload(),
        captured_at="2026-08-03T18:02:03+08:00",
    )
    payload["candidates"][0]["source_qualification"][
        "feature_price_basis"
    ] = "qfq"
    _reseal_candidate_batch(payload)

    assert "candidate_batch_candidate_source_qualification_invalid" in (
        verify_formal_candidate_batch(payload)
    )


def test_candidate_dedupe_key_is_symbol_and_strategy_type() -> None:
    strategy = _strategy_payload()
    long_row = strategy["standard_candidates"][0]
    board_row = copy.deepcopy(long_row)
    board_row["strategy_type"] = "board"
    duplicate_board = copy.deepcopy(board_row)
    strategy["standard_candidates"] = [long_row, board_row, duplicate_board]

    payload = build_formal_candidate_batch(
        data_chain_manifest=_passed_manifest(),
        strategy_payload=strategy,
        captured_at="2026-08-03T18:02:03+08:00",
    )

    assert payload["candidate_count"] == 2
    assert {
        (row["symbol"], row["strategy_type"]) for row in payload["candidates"]
    } == {("600000", "long"), ("600000", "board")}
    assert payload["rejected_candidate_count"] == 1
    assert "candidate_key_duplicate" in payload["rejected_candidates"][0]["reason_codes"]


def test_formal_candidate_batch_fails_closed_when_feature_component_is_not_passed() -> None:
    manifest = _passed_manifest()
    manifest["components"]["features"]["status"] = "blocked"

    payload = build_formal_candidate_batch(
        data_chain_manifest=manifest,
        strategy_payload=_strategy_payload(),
        captured_at="2026-08-03T18:02:03+08:00",
    )

    assert payload["status"] == "blocked"
    assert payload["formal_eligible"] is False
    assert payload["candidate_count"] == 0
    assert payload["candidates"] == []
    assert "upstream_feature_component_not_passed" in payload["reason_codes"]


def test_formal_candidate_batch_filters_stale_symbol_without_blocking_current_rows() -> None:
    payload = build_formal_candidate_batch(
        data_chain_manifest=_partially_blocked_manifest(),
        strategy_payload=_strategy_payload(),
        captured_at="2026-08-03T18:02:03+08:00",
    )

    assert payload["status"] == "passed"
    assert payload["formal_eligible"] is True
    assert payload["candidate_count"] == 1
    assert [row["symbol"] for row in payload["candidates"]] == ["600000"]
    assert payload["rejected_candidate_count"] == 1
    assert payload["rejected_candidates"][0]["symbol"] == "600001"
    assert "candidate_upstream_symbol_not_current" in payload["rejected_candidates"][0][
        "reason_codes"
    ]
    assert payload["qualification_gate"]["upstream_manifest_status"] == "blocked"
    assert payload["qualification_gate"]["candidate_level_filtering"] is True
    assert verify_formal_candidate_batch(payload) == []


def test_candidate_refresh_gate_allows_only_current_subset_without_upgrading_chain() -> None:
    manifest = _degraded_914_manifest()

    gate = candidate_refresh_gate(manifest)

    assert manifest["status"] == "blocked"
    assert gate["status"] == "passed"
    assert gate["formal_eligible"] is True
    assert gate["degraded"] is True
    assert gate["expected_count"] == 914
    assert gate["eligible_count"] == 906
    assert gate["excluded_count"] == 8
    assert gate["eligible_symbols"] == [f"{index:06d}" for index in range(1, 907)]
    assert gate["excluded_symbols"] == [f"{index:06d}" for index in range(907, 915)]
    assert {row["symbol"] for row in gate["exclusions"]} == set(
        gate["excluded_symbols"]
    )
    assert all(row["reason_codes"] for row in gate["exclusions"])


def test_candidate_refresh_gate_ignores_unrelated_blocked_chain_components() -> None:
    manifest = _passed_manifest()
    manifest["status"] = "blocked"
    manifest["coverage"]["current_analysis_pool"] = {
        "status": "blocked",
        "reason_codes": ["analysis_minute_coverage_incomplete"],
    }
    manifest["components"]["conditional_plan"] = {
        "status": "blocked",
        "reason_codes": ["conditional_plan_latest_same_date_conflict"],
    }

    gate = candidate_refresh_gate(manifest)

    assert manifest["status"] == "blocked"
    assert gate["status"] == "passed"
    assert gate["formal_eligible"] is True
    assert gate["degraded"] is False
    assert gate["eligible_symbols"] == ["600000", "600001"]


def test_candidate_refresh_gate_accepts_complete_passed_session_contract() -> None:
    gate = candidate_refresh_gate(_passed_manifest())

    assert gate["status"] == "passed"
    assert gate["formal_eligible"] is True
    assert gate["eligible_symbols"] == ["600000", "600001"]


@pytest.mark.parametrize(
    "mutation",
    [
        "wrong_target_date",
        "duplicate_symbols",
        "symbol_set_mismatch",
        "classification_counts_type",
        "counts_type",
        "classification_count_string",
        "count_string",
        "counts_mismatch",
        "counts_total_mismatch",
    ],
)
def test_candidate_refresh_gate_rejects_malformed_passed_session_contract(
    mutation: str,
) -> None:
    manifest = _passed_manifest()
    session = manifest["components"]["session_expectation"]
    if mutation == "wrong_target_date":
        session["target_date"] = "2026-08-02"
    elif mutation == "duplicate_symbols":
        session["symbols"] = ["600000", "600000"]
    elif mutation == "symbol_set_mismatch":
        session["symbols"] = ["600000", "600002"]
    elif mutation == "classification_counts_type":
        session["classification_counts"] = "not-a-mapping"
    elif mutation == "counts_type":
        session["counts"] = "not-a-mapping"
    elif mutation == "classification_count_string":
        session["classification_counts"]["expected_trade"] = "2"
    elif mutation == "count_string":
        session["counts"]["expected_trade"] = "2"
    elif mutation == "counts_mismatch":
        session["counts"]["unknown"] = 1
    elif mutation == "counts_total_mismatch":
        session["classification_counts"]["expected_trade"] = 1
        session["counts"]["expected_trade"] = 1

    gate = candidate_refresh_gate(manifest)

    assert gate["status"] == "blocked"
    assert gate["formal_eligible"] is False
    assert "upstream_session_expectation_not_passed" in gate["reason_codes"]


@pytest.mark.parametrize("raw_symbol", ["sh600000", 600000, {"code": "600000"}])
def test_candidate_refresh_gate_rejects_noncanonical_raw_universe_symbols(
    raw_symbol,
) -> None:
    manifest = _passed_manifest()
    manifest["universe"]["current_analysis_pool"]["symbols"] = [
        raw_symbol,
        "600001",
    ]

    gate = candidate_refresh_gate(manifest)

    assert gate["status"] == "blocked"
    assert gate["formal_eligible"] is False
    assert "upstream_universe_symbols_invalid" in gate["reason_codes"]


def test_candidate_refresh_gate_blocks_session_unknown_outside_daily_exclusions() -> None:
    manifest = _degraded_914_manifest()
    manifest["components"]["session_expectation"]["symbols"].append("999999")
    manifest["components"]["session_expectation"]["unknown_symbols"].append(
        "999999"
    )
    manifest["components"]["session_expectation"]["classification_counts"][
        "unknown"
    ] = 9
    manifest["components"]["session_expectation"]["counts"]["unknown"] = 9
    manifest["coverage"]["session_expectation"]["unknown_count"] = 9
    manifest["coverage"]["session_expectation"]["unknown_symbols"].append(
        "999999"
    )

    gate = candidate_refresh_gate(manifest)

    assert gate["status"] == "blocked"
    assert "upstream_session_expectation_not_passed" in gate["reason_codes"]


def test_candidate_refresh_gate_fails_closed_on_malformed_session_counts() -> None:
    manifest = _degraded_914_manifest()
    manifest["components"]["session_expectation"]["classification_counts"][
        "unknown"
    ] = "not-an-integer"

    gate = candidate_refresh_gate(manifest)

    assert gate["status"] == "blocked"
    assert "upstream_session_expectation_not_passed" in gate["reason_codes"]


@pytest.mark.parametrize(
    "mutation",
    [
        "target_date_mismatch",
        "dual_counts_mismatch",
        "component_unknown_duplicate",
        "coverage_unknown_duplicate",
    ],
)
def test_candidate_refresh_gate_rejects_inconsistent_session_degradation(
    mutation: str,
) -> None:
    manifest = _degraded_914_manifest()
    session = manifest["components"]["session_expectation"]
    session_coverage = manifest["coverage"]["session_expectation"]
    if mutation == "target_date_mismatch":
        session["target_date"] = "2026-08-02"
    elif mutation == "dual_counts_mismatch":
        session["counts"]["unknown"] = 999
    elif mutation == "component_unknown_duplicate":
        session["unknown_symbols"].append(session["unknown_symbols"][0])
    elif mutation == "coverage_unknown_duplicate":
        session_coverage["unknown_symbols"].append(
            session_coverage["unknown_symbols"][0]
        )

    gate = candidate_refresh_gate(manifest)

    assert gate["status"] == "blocked"
    assert "upstream_session_expectation_not_passed" in gate["reason_codes"]


def test_formal_candidate_batch_uses_gate_eligible_symbols_as_whitelist() -> None:
    manifest = _degraded_914_manifest()
    leaked_symbol = "000911"
    manifest["components"]["features"]["per_symbol"][leaked_symbol] = {
        "eligible": True,
        "price_basis": "raw",
        "latest_date": "2026-08-03",
    }
    strategy = _strategy_payload()
    leaked = strategy["standard_candidates"][0]
    leaked["symbol"] = leaked_symbol
    leaked["name"] = "stale cached candidate"
    strategy["standard_candidates"] = [leaked]

    payload = build_formal_candidate_batch(
        data_chain_manifest=manifest,
        strategy_payload=strategy,
        captured_at="2026-08-03T18:02:03+08:00",
    )

    assert payload["formal_eligible"] is True
    assert payload["candidate_count"] == 0
    assert payload["rejected_candidate_count"] == 1
    assert "candidate_not_in_gate_eligible_symbols" in payload["rejected_candidates"][
        0
    ]["reason_codes"]


def test_candidate_refresh_scopes_strategy_runner_to_gate_eligible_universe(
    tmp_path: Path,
) -> None:
    manifest = _degraded_914_manifest()
    universe_path = (
        tmp_path / "data" / "universe" / "low_price_learning_symbols.csv"
    )
    universe_path.parent.mkdir(parents=True)
    universe_path.write_text(
        "symbol,name,included,source,updated_at\n"
        + "".join(
            f"{index:06d},name-{index},True,fixture,2026-08-03T00:00:00+08:00\n"
            for index in range(1, 915)
        ),
        encoding="utf-8",
    )
    observed: dict[str, object] = {}

    def strategy_runner(**kwargs):
        scoped_path = Path(kwargs["universe_path"])
        rows = scoped_path.read_text(encoding="utf-8").splitlines()
        observed["path"] = scoped_path
        observed["symbols"] = [row.split(",", 1)[0] for row in rows[1:]]
        payload = _strategy_payload()
        candidate = payload["standard_candidates"][0]
        candidate["symbol"] = "000001"
        candidate["name"] = "eligible"
        payload["standard_candidates"] = [candidate]
        return payload

    result = run_formal_candidate_refresh(
        project_root=tmp_path,
        data_chain_manifest=manifest,
        strategy_runner=strategy_runner,
        captured_at="2026-08-03T18:02:03+08:00",
        apply=False,
    )

    assert result["status"] == "staged"
    assert observed["symbols"] == [f"{index:06d}" for index in range(1, 907)]
    assert Path(observed["path"]).name == "candidate_eligible_universe.csv"


@pytest.mark.parametrize(
    ("failure_mode", "expected_reason"),
    [
        ("critical_source_status", "upstream_critical_sources_not_formal"),
        ("critical_source_missing", "upstream_critical_sources_missing"),
        ("protected_state", "upstream_protected_state_not_unchanged"),
        ("feature_price_basis", "upstream_feature_per_symbol_price_basis_not_raw"),
        ("universe_provenance", "upstream_universe_hash_invalid"),
        ("universe_digest", "upstream_universe_contract_digest_mismatch"),
    ],
)
def test_candidate_refresh_gate_blocks_structural_failures(
    failure_mode: str,
    expected_reason: str,
) -> None:
    manifest = _degraded_914_manifest()
    if failure_mode == "critical_source_status":
        manifest["components"]["critical_sources"]["status"] = "blocked"
    elif failure_mode == "critical_source_missing":
        del manifest["components"]["critical_sources"]
    elif failure_mode == "protected_state":
        manifest["protected_state_unchanged"] = False
    elif failure_mode == "feature_price_basis":
        manifest["components"]["features"]["per_symbol"]["000001"][
            "price_basis"
        ] = "mixed_or_unknown"
    elif failure_mode == "universe_provenance":
        manifest["universe"]["current_analysis_pool"]["sha256"] = "invalid"
    elif failure_mode == "universe_digest":
        manifest["universe"]["current_analysis_pool"]["sha256"] = "c" * 64

    gate = candidate_refresh_gate(manifest)

    assert gate["status"] == "blocked"
    assert gate["formal_eligible"] is False
    assert expected_reason in gate["reason_codes"]


@pytest.mark.parametrize(
    ("failure_mode", "expected_reason"),
    [
        ("daily_stale_count", "upstream_daily_stale_count_invalid"),
        (
            "daily_invalid_basis_count",
            "upstream_daily_invalid_price_basis_count_invalid",
        ),
        ("universe_symbol_count", "upstream_universe_symbol_count_invalid"),
        ("daily_requested_count", "upstream_daily_requested_count_invalid"),
        ("critical_failed_sources", "upstream_critical_sources_not_formal"),
        ("universe_symbols_type", "upstream_universe_symbols_invalid"),
        ("universe_symbol_format", "upstream_universe_symbols_invalid"),
    ],
)
def test_candidate_refresh_gate_fails_closed_on_malformed_manifest_fields(
    failure_mode: str,
    expected_reason: str,
) -> None:
    manifest = _passed_manifest()
    if failure_mode == "daily_stale_count":
        manifest["coverage"]["daily"]["stale_count"] = "not-an-integer"
    elif failure_mode == "daily_invalid_basis_count":
        manifest["coverage"]["daily"]["invalid_price_basis_count"] = (
            "not-an-integer"
        )
    elif failure_mode == "universe_symbol_count":
        manifest["universe"]["current_analysis_pool"]["symbol_count"] = (
            "not-an-integer"
        )
    elif failure_mode == "daily_requested_count":
        manifest["coverage"]["daily"]["requested_count"] = "not-an-integer"
    elif failure_mode == "critical_failed_sources":
        manifest["components"]["critical_sources"]["failed_sources"] = 7
    elif failure_mode == "universe_symbols_type":
        manifest["universe"]["current_analysis_pool"]["symbols"] = 7
    elif failure_mode == "universe_symbol_format":
        pool = manifest["universe"]["current_analysis_pool"]
        pool["symbols"] = ["BAD"]
        pool["symbol_count"] = 1
        pool["sha256"] = _universe_digest(
            target_date=manifest["target_date"], symbols=["BAD"]
        )
        manifest["components"]["features"]["per_symbol"] = {
            "BAD": {
                "eligible": True,
                "price_basis": "raw",
                "latest_date": manifest["target_date"],
            }
        }

    gate = candidate_refresh_gate(manifest)

    assert gate["status"] == "blocked"
    assert gate["formal_eligible"] is False
    assert expected_reason in gate["reason_codes"]


def test_formal_candidate_batch_rejects_candidate_outside_manifest_universe() -> None:
    strategy = _strategy_payload()
    row = copy.deepcopy(strategy["standard_candidates"][0])
    row["symbol"] = "600099"
    strategy["standard_candidates"] = [row]

    payload = build_formal_candidate_batch(
        data_chain_manifest=_passed_manifest(),
        strategy_payload=strategy,
        captured_at="2026-08-03T18:02:03+08:00",
    )

    assert payload["candidate_count"] == 0
    assert payload["rejected_candidate_count"] == 1
    assert "candidate_not_in_source_universe" in payload["rejected_candidates"][0][
        "reason_codes"
    ]


def test_formal_candidate_batch_rejects_nonfinite_score_without_crashing() -> None:
    strategy = _strategy_payload()
    strategy["standard_candidates"] = [strategy["standard_candidates"][0]]
    strategy["standard_candidates"][0]["score"] = math.nan

    payload = build_formal_candidate_batch(
        data_chain_manifest=_passed_manifest(),
        strategy_payload=strategy,
        captured_at="2026-08-03T18:02:03+08:00",
    )

    assert payload["status"] == "passed_with_warnings"
    assert payload["candidate_count"] == 0
    assert payload["rejected_candidate_count"] == 1
    assert "candidate_score_nonfinite" in payload["rejected_candidates"][0]["reason_codes"]
    assert verify_formal_candidate_batch(payload) == []


def test_candidate_refresh_stages_before_apply_and_never_touches_v420_csv(
    tmp_path: Path,
) -> None:
    legacy = tmp_path / "results" / "daily_research" / "v420_daily_stock_candidates_latest.csv"
    legacy.parent.mkdir(parents=True, exist_ok=True)
    legacy.write_bytes(b"legacy-v420-owner\n")

    staged = run_formal_candidate_refresh(
        project_root=tmp_path,
        data_chain_manifest=_passed_manifest(),
        strategy_payload=_strategy_payload(),
        captured_at="2026-08-03T18:02:03+08:00",
        apply=False,
    )

    assert staged["status"] == "staged"
    assert staged["applied"] is False
    assert (tmp_path / staged["staging_path"]).is_file()
    assert not (tmp_path / "results" / "candidate_refresh" / "formal_candidate_batch_latest.json").exists()
    assert legacy.read_bytes() == b"legacy-v420-owner\n"

    applied = run_formal_candidate_refresh(
        project_root=tmp_path,
        data_chain_manifest=_passed_manifest(),
        strategy_payload=_strategy_payload(),
        captured_at="2026-08-03T18:02:03+08:00",
        apply=True,
    )

    assert applied["status"] == "completed"
    assert applied["applied"] is True
    dated = tmp_path / "results" / "candidate_refresh" / "daily" / "2026-08-03.json"
    latest = tmp_path / "results" / "candidate_refresh" / "formal_candidate_batch_latest.json"
    assert dated.is_file()
    assert latest.is_file()
    assert json.loads(dated.read_text(encoding="utf-8"))["provenance"]["hash"] == (
        json.loads(latest.read_text(encoding="utf-8"))["provenance"]["hash"]
    )
    assert legacy.read_bytes() == b"legacy-v420-owner\n"


def test_candidate_run_scoped_batch_is_write_once_and_idempotent(
    tmp_path: Path,
) -> None:
    manifest = _passed_manifest()
    first = run_formal_candidate_refresh(
        project_root=tmp_path,
        data_chain_manifest=manifest,
        strategy_payload=_strategy_payload(),
        captured_at="2026-08-03T18:02:03+08:00",
        apply=False,
    )
    staging_path = tmp_path / first["staging_path"]
    original = staging_path.read_bytes()

    same = run_formal_candidate_refresh(
        project_root=tmp_path,
        data_chain_manifest=manifest,
        strategy_payload=_strategy_payload(),
        captured_at="2026-08-03T18:02:03+08:00",
        apply=False,
    )
    assert same["status"] == "staged"
    assert staging_path.read_bytes() == original

    conflict = run_formal_candidate_refresh(
        project_root=tmp_path,
        data_chain_manifest=manifest,
        strategy_payload=_strategy_payload(),
        captured_at="2026-08-03T18:03:00+08:00",
        apply=False,
    )
    assert conflict["status"] == "blocked"
    assert conflict["applied"] is False
    assert conflict["reason_codes"] == ["candidate_run_snapshot_conflict"]
    assert staging_path.read_bytes() == original


def test_chain_bound_resolver_prefers_exact_dated_snapshot(tmp_path: Path) -> None:
    manifest = _passed_manifest()
    batch = build_formal_candidate_batch(
        data_chain_manifest=manifest,
        strategy_payload=_strategy_payload(),
        captured_at="2026-08-03T18:02:03+08:00",
    )
    published = publish_formal_candidate_batch(tmp_path, batch)

    resolved = resolve_chain_bound_formal_candidate_batch(
        tmp_path,
        data_chain_manifest=manifest,
    )

    assert resolved["status"] == "passed"
    assert resolved["source_mode"] == "dated_snapshot"
    assert resolved["path"] == str(tmp_path / published["dated_path"])
    assert resolved["batch"]["provenance"]["hash"] == batch["provenance"]["hash"]


def test_chain_bound_resolver_uses_exact_run_scoped_conflict_recovery(
    tmp_path: Path,
) -> None:
    first_manifest = _passed_manifest()
    first_batch = build_formal_candidate_batch(
        data_chain_manifest=first_manifest,
        strategy_payload=_strategy_payload(),
        captured_at="2026-08-03T18:02:03+08:00",
    )
    published = publish_formal_candidate_batch(tmp_path, first_batch)
    dated_path = tmp_path / published["dated_path"]
    dated_before = dated_path.read_bytes()

    retry_manifest = _passed_manifest()
    retry_manifest["run_id"] = "daily-data-chain-20260803T103000Z-retry"
    retry = run_formal_candidate_refresh(
        project_root=tmp_path,
        data_chain_manifest=retry_manifest,
        strategy_payload=_strategy_payload(),
        captured_at="2026-08-03T18:30:00+08:00",
        apply=True,
    )
    retry_manifest["components"]["candidate_refresh_gate"] = candidate_refresh_gate(
        retry_manifest
    )
    retry_manifest["components"]["candidate_refresh"] = copy.deepcopy(retry)

    resolved = resolve_chain_bound_formal_candidate_batch(
        tmp_path,
        data_chain_manifest=retry_manifest,
    )

    assert retry["reason_codes"] == ["candidate_dated_snapshot_conflict"]
    assert resolved["status"] == "passed"
    assert resolved["source_mode"] == "run_scoped_conflict_recovery"
    assert resolved["path"] == str(tmp_path / retry["staging_path"])
    assert resolved["batch"]["provenance"]["source_run_id"] == retry_manifest["run_id"]
    assert dated_path.read_bytes() == dated_before


@pytest.mark.parametrize(
    ("mutation", "reason"),
    [
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
            lambda manifest: manifest["components"]["candidate_refresh"].update(
                staging_path="results/candidate_refresh/runs/foreign/formal_candidate_batch.json"
            ),
            "formal_candidate_recovery_staging_path_mismatch",
        ),
        (
            lambda manifest: manifest.update(status="blocked"),
            "authoritative_data_chain_not_completed",
        ),
    ],
)
def test_chain_bound_resolver_recovery_is_narrow_and_fail_closed(
    tmp_path: Path,
    mutation,
    reason: str,
) -> None:
    first_manifest = _passed_manifest()
    first_batch = build_formal_candidate_batch(
        data_chain_manifest=first_manifest,
        strategy_payload=_strategy_payload(),
        captured_at="2026-08-03T18:02:03+08:00",
    )
    publish_formal_candidate_batch(tmp_path, first_batch)

    retry_manifest = _passed_manifest()
    retry_manifest["run_id"] = "daily-data-chain-20260803T103000Z-retry"
    retry = run_formal_candidate_refresh(
        project_root=tmp_path,
        data_chain_manifest=retry_manifest,
        strategy_payload=_strategy_payload(),
        captured_at="2026-08-03T18:30:00+08:00",
        apply=True,
    )
    retry_manifest["components"]["candidate_refresh_gate"] = candidate_refresh_gate(
        retry_manifest
    )
    retry_manifest["components"]["candidate_refresh"] = copy.deepcopy(retry)
    mutation(retry_manifest)

    resolved = resolve_chain_bound_formal_candidate_batch(
        tmp_path,
        data_chain_manifest=retry_manifest,
    )

    assert resolved["status"] == "blocked"
    assert reason in resolved["reason_codes"]


def test_candidate_envelope_supports_strict_run_scoped_path_and_legacy_dated_path(
    tmp_path: Path,
) -> None:
    manifest = _passed_manifest()
    batch = build_formal_candidate_batch(
        data_chain_manifest=manifest,
        strategy_payload=_strategy_payload(),
        captured_at="2026-08-03T18:02:03+08:00",
    )
    candidate = batch["candidates"][0]
    dated = tmp_path / "results" / "candidate_refresh" / "daily" / "2026-08-03.json"
    run_scoped = (
        tmp_path
        / "results"
        / "candidate_refresh"
        / "runs"
        / manifest["run_id"]
        / "formal_candidate_batch.json"
    )

    legacy = build_candidate_envelope(batch, batch_path=dated, candidate=candidate)
    recovery = build_candidate_envelope(
        batch,
        batch_path=run_scoped,
        candidate=candidate,
    )

    assert verify_candidate_envelope(legacy) == []
    assert recovery["source_run_id"] == manifest["run_id"]
    assert verify_candidate_envelope(recovery) == []

    wrong_run = copy.deepcopy(recovery)
    wrong_run["source_run_id"] = "another-run"
    assert "candidate_envelope_source_run_id_mismatch" in verify_candidate_envelope(
        wrong_run
    )

    arbitrary = copy.deepcopy(recovery)
    arbitrary["batch_path"] = str(
        tmp_path / "results" / "candidate_refresh" / "runs" / manifest["run_id"] / "other.json"
    )
    assert "candidate_envelope_batch_path_invalid" in verify_candidate_envelope(arbitrary)

def test_structurally_blocked_upstream_never_calls_strategy_runner(
    tmp_path: Path,
) -> None:
    manifest = _passed_manifest()
    manifest["status"] = "blocked"
    manifest["protected_state_unchanged"] = False
    calls = []

    def forbidden_runner(**kwargs):
        calls.append(kwargs)
        raise AssertionError("strategy runner must not run after an upstream failure")

    result = run_formal_candidate_refresh(
        project_root=tmp_path,
        data_chain_manifest=manifest,
        strategy_runner=forbidden_runner,
        captured_at="2026-08-03T18:02:03+08:00",
        apply=True,
    )

    assert calls == []
    assert result["status"] == "blocked"
    assert result["applied"] is False
    assert "upstream_protected_state_not_unchanged" in result["reason_codes"]
    assert (tmp_path / result["staging_path"]).is_file()
    assert not (
        tmp_path
        / "results"
        / "candidate_refresh"
        / "formal_candidate_batch_latest.json"
    ).exists()


def test_candidate_dated_publish_is_idempotent_and_conflict_fail_closed(
    tmp_path: Path,
) -> None:
    first = build_formal_candidate_batch(
        data_chain_manifest=_passed_manifest(),
        strategy_payload=_strategy_payload(),
        captured_at="2026-08-03T18:02:03+08:00",
    )
    assert publish_formal_candidate_batch(tmp_path, first)["applied"] is True
    assert publish_formal_candidate_batch(tmp_path, first)["status"] == "idempotent"

    conflicting_strategy = _strategy_payload()
    conflicting_strategy["standard_candidates"][0]["score"] = 102.0
    conflict = build_formal_candidate_batch(
        data_chain_manifest=_passed_manifest(),
        strategy_payload=conflicting_strategy,
        captured_at="2026-08-03T18:03:00+08:00",
    )
    result = publish_formal_candidate_batch(tmp_path, conflict)

    assert result["status"] == "blocked"
    assert result["applied"] is False
    assert "candidate_dated_snapshot_conflict" in result["reason_codes"]
    latest = json.loads(
        (
            tmp_path
            / "results"
            / "candidate_refresh"
            / "formal_candidate_batch_latest.json"
        ).read_text(encoding="utf-8")
    )
    assert latest["provenance"]["hash"] == first["provenance"]["hash"]


@pytest.mark.parametrize("contents", ["", "{}", "not-json"])
def test_candidate_dated_publish_rejects_unreadable_or_empty_existing_file(
    tmp_path: Path, contents: str
) -> None:
    payload = build_formal_candidate_batch(
        data_chain_manifest=_passed_manifest(),
        strategy_payload=_strategy_payload(),
        captured_at="2026-08-03T18:02:03+08:00",
    )
    dated = (
        tmp_path
        / "results"
        / "candidate_refresh"
        / "daily"
        / "2026-08-03.json"
    )
    dated.parent.mkdir(parents=True, exist_ok=True)
    dated.write_text(contents, encoding="utf-8")

    result = publish_formal_candidate_batch(tmp_path, payload)

    assert result["status"] == "blocked"
    assert result["applied"] is False
    assert "candidate_dated_snapshot_unreadable" in result["reason_codes"]
    assert dated.read_text(encoding="utf-8") == contents


def test_candidate_exclusive_create_race_reloads_and_rejects_conflict(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    payload = build_formal_candidate_batch(
        data_chain_manifest=_passed_manifest(),
        strategy_payload=_strategy_payload(),
        captured_at="2026-08-03T18:02:03+08:00",
    )
    competing = copy.deepcopy(payload)
    competing["candidates"][0]["score"] = 777.0
    _reseal_candidate_batch(competing)

    def racing_create(path: Path, value: dict) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(competing), encoding="utf-8")
        raise FileExistsError(path)

    monkeypatch.setattr(refresh, "_exclusive_write_json", racing_create)

    result = publish_formal_candidate_batch(tmp_path, payload)

    assert result["status"] == "blocked"
    assert result["applied"] is False
    assert "candidate_dated_snapshot_conflict" in result["reason_codes"]


def test_candidate_publish_lock_busy_is_visible_and_writes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    payload = build_formal_candidate_batch(
        data_chain_manifest=_passed_manifest(),
        strategy_payload=_strategy_payload(),
        captured_at="2026-08-03T18:02:03+08:00",
    )

    @contextmanager
    def busy_lock(*args, **kwargs):
        raise TimeoutError("fixture lock busy")
        yield

    monkeypatch.setattr(refresh, "interprocess_file_lock", busy_lock)

    result = publish_formal_candidate_batch(tmp_path, payload)

    assert result["status"] == "blocked"
    assert result["applied"] is False
    assert "candidate_publish_lock_busy" in result["reason_codes"]
    assert not (
        tmp_path
        / "results"
        / "candidate_refresh"
        / "daily"
        / "2026-08-03.json"
    ).exists()


def test_candidate_latest_unreadable_is_not_overwritten(tmp_path: Path) -> None:
    payload = build_formal_candidate_batch(
        data_chain_manifest=_passed_manifest(),
        strategy_payload=_strategy_payload(),
        captured_at="2026-08-03T18:02:03+08:00",
    )
    latest = (
        tmp_path
        / "results"
        / "candidate_refresh"
        / "formal_candidate_batch_latest.json"
    )
    latest.parent.mkdir(parents=True, exist_ok=True)
    latest.write_text("{}", encoding="utf-8")

    result = publish_formal_candidate_batch(tmp_path, payload)

    assert result["status"] == "blocked"
    assert "candidate_latest_snapshot_unreadable" in result["reason_codes"]
    assert latest.read_text(encoding="utf-8") == "{}"


def test_custom_root_requires_explicit_strategy_runner(tmp_path: Path) -> None:
    result = run_formal_candidate_refresh(
        project_root=tmp_path,
        data_chain_manifest=_passed_manifest(),
        captured_at="2026-08-03T18:02:03+08:00",
        apply=False,
    )

    assert result["status"] == "blocked"
    assert result["applied"] is False
    assert "candidate_strategy_runner_injection_required_for_custom_root" in result[
        "reason_codes"
    ]


def test_custom_root_uses_only_the_explicit_strategy_runner(tmp_path: Path) -> None:
    calls: list[dict] = []

    def injected_runner(**kwargs) -> dict:
        calls.append(kwargs)
        return _strategy_payload()

    result = run_formal_candidate_refresh(
        project_root=tmp_path,
        data_chain_manifest=_passed_manifest(),
        strategy_runner=injected_runner,
        captured_at="2026-08-03T18:02:03+08:00",
        apply=False,
    )

    assert result["status"] == "staged"
    assert result["applied"] is False
    assert len(calls) == 1
    assert calls[0]["daily_dir"] == tmp_path / "data" / "daily"
    assert calls[0]["dry_run"] is True
    assert calls[0]["expected_latest_date"] == "2026-08-03"


def test_conditional_plan_archive_is_append_only_and_idempotent(tmp_path: Path) -> None:
    plan = _formal_plan(
        plan_id="next-plan",
        plan_date="2026-08-03",
        trade_date="2026-08-04",
    )

    first = archive_conditional_plan(tmp_path, plan)
    second = archive_conditional_plan(tmp_path, plan)

    assert first["status"] == "completed"
    assert first["archived"] is True
    assert second["status"] == "idempotent"
    assert second["archived"] is True
    archive_path = tmp_path / first["archive_path"]
    assert json.loads(archive_path.read_text(encoding="utf-8"))["plan_id"] == "next-plan"
    loaded = load_archived_conditional_plan(
        tmp_path,
        trade_date="2026-08-04",
    )
    assert loaded["status"] == "passed"
    assert loaded["plan"]["plan_id"] == "next-plan"
    assert loaded["plan_hash"] == first["plan_hash"]


@pytest.mark.parametrize("contents", ["", "{}", "not-json"])
def test_conditional_plan_archive_rejects_unreadable_existing_date(
    tmp_path: Path, contents: str
) -> None:
    plan = _formal_plan(
        plan_id="next-plan",
        plan_date="2026-08-03",
        trade_date="2026-08-04",
    )
    archive_path = (
        tmp_path
        / "results"
        / "conditional_trade_plans_daily"
        / "2026-08-04.json"
    )
    archive_path.parent.mkdir(parents=True, exist_ok=True)
    archive_path.write_text(contents, encoding="utf-8")

    result = archive_conditional_plan(tmp_path, plan)

    assert result["status"] == "blocked"
    assert result["archived"] is False
    assert "conditional_plan_archive_unreadable" in result["reason_codes"]
    assert archive_path.read_text(encoding="utf-8") == contents


def test_conditional_plan_archive_exclusive_create_race_rejects_conflict(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan = _formal_plan(
        plan_id="next-plan",
        plan_date="2026-08-03",
        trade_date="2026-08-04",
    )
    competing = copy.deepcopy(plan)
    competing["plan_id"] = "competing-plan"

    def racing_create(path: Path, value: dict) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(competing), encoding="utf-8")
        raise FileExistsError(path)

    monkeypatch.setattr(refresh, "_exclusive_write_json", racing_create)

    result = archive_conditional_plan(tmp_path, plan)

    assert result["status"] == "blocked"
    assert result["archived"] is False
    assert "conditional_plan_archive_conflict" in result["reason_codes"]


def test_conditional_plan_archive_fails_closed_when_lock_is_busy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan = _formal_plan(
        plan_id="next-plan",
        plan_date="2026-08-03",
        trade_date="2026-08-04",
    )

    @contextmanager
    def busy_lock(*args, **kwargs):
        raise TimeoutError("fixture lock busy")
        yield

    monkeypatch.setattr(refresh, "interprocess_file_lock", busy_lock)

    result = archive_conditional_plan(tmp_path, plan)

    assert result["status"] == "blocked"
    assert result["archived"] is False
    assert "conditional_plan_archive_lock_busy" in result["reason_codes"]


def test_publish_next_plan_archives_previous_latest_before_overwrite(
    tmp_path: Path,
) -> None:
    due = _formal_plan(
        plan_id="due-plan",
        plan_date="2026-07-31",
        trade_date="2026-08-03",
    )
    nxt = _formal_plan(
        plan_id="next-plan",
        plan_date="2026-08-03",
        trade_date="2026-08-04",
    )
    latest = tmp_path / "results" / "conditional_trade_plans_latest.json"
    latest.parent.mkdir(parents=True, exist_ok=True)
    latest.write_text(json.dumps(due), encoding="utf-8")

    result = publish_archived_conditional_plan(tmp_path, nxt)

    assert result["status"] == "completed"
    assert result["published_latest"] is True
    assert result["previous_latest_archived"] is True
    due_archive = (
        tmp_path
        / "results"
        / "conditional_trade_plans_daily"
        / "2026-08-03.json"
    )
    next_archive = (
        tmp_path
        / "results"
        / "conditional_trade_plans_daily"
        / "2026-08-04.json"
    )
    assert json.loads(due_archive.read_text(encoding="utf-8"))["plan_id"] == "due-plan"
    assert json.loads(next_archive.read_text(encoding="utf-8"))["plan_id"] == "next-plan"
    assert json.loads(latest.read_text(encoding="utf-8"))["plan_id"] == "next-plan"


def test_publish_next_plan_preserves_latest_when_due_archive_conflicts(
    tmp_path: Path,
) -> None:
    due = _formal_plan(
        plan_id="due-plan",
        plan_date="2026-07-31",
        trade_date="2026-08-03",
    )
    nxt = _formal_plan(
        plan_id="next-plan",
        plan_date="2026-08-03",
        trade_date="2026-08-04",
    )
    latest = tmp_path / "results" / "conditional_trade_plans_latest.json"
    latest.parent.mkdir(parents=True, exist_ok=True)
    latest.write_text(json.dumps(due), encoding="utf-8")
    conflict = copy.deepcopy(due)
    conflict["plan_id"] = "other-due-plan"
    due_archive = (
        tmp_path
        / "results"
        / "conditional_trade_plans_daily"
        / "2026-08-03.json"
    )
    due_archive.parent.mkdir(parents=True, exist_ok=True)
    due_archive.write_text(json.dumps(conflict), encoding="utf-8")

    result = publish_archived_conditional_plan(tmp_path, nxt)

    assert result["status"] == "blocked"
    assert result["published_latest"] is False
    assert "conditional_plan_archive_conflict" in result["reason_codes"]
    assert json.loads(latest.read_text(encoding="utf-8"))["plan_id"] == "due-plan"
    assert not (
        tmp_path
        / "results"
        / "conditional_trade_plans_daily"
        / "2026-08-04.json"
    ).exists()


def test_publish_next_plan_does_not_replace_unreadable_latest(tmp_path: Path) -> None:
    nxt = _formal_plan(
        plan_id="next-plan",
        plan_date="2026-08-03",
        trade_date="2026-08-04",
    )
    latest = tmp_path / "results" / "conditional_trade_plans_latest.json"
    latest.parent.mkdir(parents=True, exist_ok=True)
    latest.write_text("not-json", encoding="utf-8")

    result = publish_archived_conditional_plan(tmp_path, nxt)

    assert result["status"] == "blocked"
    assert result["published_latest"] is False
    assert "conditional_plan_latest_unreadable" in result["reason_codes"]
    assert latest.read_text(encoding="utf-8") == "not-json"
    assert not (
        tmp_path
        / "results"
        / "conditional_trade_plans_daily"
        / "2026-08-04.json"
    ).exists()


def test_replay_receipt_uses_execution_apply_and_persisted_state_contracts() -> None:
    execution = _execution_payload()
    apply_result = _apply_result()
    persisted_state = copy.deepcopy(apply_result["state"])

    receipt = build_conditional_replay_receipt(
        execution_payload=execution,
        apply_result=apply_result,
        persisted_state=persisted_state,
        minute_evidence=_complete_minute_evidence(),
        captured_at="2026-08-03T18:12:00+08:00",
    )

    assert receipt["status"] == "passed"
    assert receipt["formal_evidence_eligible"] is True
    assert receipt["execution_id"] == "due-plan:2026-08-03"
    assert receipt["execution_counts"] == {
        "event_count": 0,
        "no_fill_count": 1,
        "blocked_count": 0,
    }
    assert receipt["state_evidence"]["last_conditional_plan_id"] == "due-plan"
    assert receipt["state_evidence"]["last_trade_date"] == "2026-08-03"
    assert verify_conditional_replay_receipt(receipt) == []


def test_replay_receipt_blocks_when_persisted_state_does_not_prove_application() -> None:
    persisted_state = _applied_state()
    persisted_state["applied_execution_ids"] = []

    receipt = build_conditional_replay_receipt(
        execution_payload=_execution_payload(),
        apply_result=_apply_result(),
        persisted_state=persisted_state,
        minute_evidence=_complete_minute_evidence(),
        captured_at="2026-08-03T18:12:00+08:00",
    )

    assert receipt["status"] == "blocked"
    assert receipt["formal_evidence_eligible"] is False
    assert "replay_execution_id_not_persisted" in receipt["reason_codes"]


def test_replay_receipt_rejects_non_list_applied_execution_id_collections() -> None:
    execution_id = "due-plan:2026-08-03"
    apply_result = _apply_result()
    apply_result["state"]["applied_execution_ids"] = {execution_id: True}
    persisted_state = copy.deepcopy(apply_result["state"])

    receipt = build_conditional_replay_receipt(
        execution_payload=_execution_payload(),
        apply_result=apply_result,
        persisted_state=persisted_state,
        minute_evidence=_complete_minute_evidence(),
        captured_at="2026-08-03T18:12:00+08:00",
    )

    assert receipt["status"] == "blocked"
    assert "replay_apply_execution_ids_invalid" in receipt["reason_codes"]
    assert "replay_persisted_execution_ids_invalid" in receipt["reason_codes"]


@pytest.mark.parametrize(
    ("surface", "field", "value", "reason_code"),
    [
        ("execution", "allow_daily_approx", True, "replay_daily_approx_not_allowed"),
        ("execution", "is_real_order", True, "replay_execution_boundary_invalid"),
        ("apply", "no_broker", False, "replay_apply_boundary_invalid"),
    ],
)
def test_replay_receipt_rejects_non_paper_or_approximate_input_contracts(
    surface: str, field: str, value: object, reason_code: str
) -> None:
    execution = _execution_payload()
    apply_result = _apply_result()
    if surface == "execution":
        execution[field] = value
    else:
        apply_result[field] = value

    receipt = build_conditional_replay_receipt(
        execution_payload=execution,
        apply_result=apply_result,
        persisted_state=copy.deepcopy(apply_result["state"]),
        minute_evidence=_complete_minute_evidence(),
        captured_at="2026-08-03T18:12:00+08:00",
    )

    assert receipt["status"] == "blocked"
    assert reason_code in receipt["reason_codes"]


def test_replay_receipt_verifier_rejects_semantic_tamper_after_reseal() -> None:
    receipt = build_conditional_replay_receipt(
        execution_payload=_execution_payload(),
        apply_result=_apply_result(),
        persisted_state=_applied_state(),
        minute_evidence=_complete_minute_evidence(),
        captured_at="2026-08-03T18:12:00+08:00",
    )
    receipt["state_evidence"]["execution_id_persisted"] = False
    _reseal_replay_receipt(receipt)

    assert "replay_receipt_persisted_state_not_proven" in (
        verify_conditional_replay_receipt(receipt)
    )


@pytest.mark.parametrize(
    ("field", "value", "reason_code"),
    [
        ("application_action_count", -1, "replay_receipt_action_count_invalid"),
        ("already_updated_today", "yes", "replay_receipt_already_updated_invalid"),
    ],
)
def test_replay_receipt_verifier_rejects_typed_metadata_tamper_after_reseal(
    field: str, value: object, reason_code: str
) -> None:
    receipt = build_conditional_replay_receipt(
        execution_payload=_execution_payload(),
        apply_result=_apply_result(),
        persisted_state=_applied_state(),
        minute_evidence=_complete_minute_evidence(),
        captured_at="2026-08-03T18:12:00+08:00",
    )
    receipt[field] = value
    _reseal_replay_receipt(receipt)

    assert reason_code in verify_conditional_replay_receipt(receipt)


def test_replay_receipt_blocks_when_partial_minute_symbol_is_not_resolved() -> None:
    receipt = build_conditional_replay_receipt(
        execution_payload=_execution_payload(symbols=("600000",)),
        apply_result=_apply_result(),
        persisted_state=_applied_state(),
        minute_evidence=_partial_minute_evidence(),
        captured_at="2026-08-03T18:12:00+08:00",
    )

    assert receipt["status"] == "blocked"
    assert "replay_execution_symbol_set_mismatch" in receipt["reason_codes"]
    assert "replay_non_available_symbol_not_resolved" in receipt["reason_codes"]


def test_rollover_requires_due_replay_before_next_plan_publish() -> None:
    due = _formal_plan(
        plan_id="due-plan",
        plan_date="2026-07-31",
        trade_date="2026-08-03",
    )
    nxt = _formal_plan(
        plan_id="next-plan",
        plan_date="2026-08-03",
        trade_date="2026-08-04",
    )
    decision = coordinate_plan_rollover(
        as_of="2026-08-03",
        due_plan=due,
        next_plan=nxt,
        minute_evidence=_complete_minute_evidence(),
    )

    assert decision["status"] == "replay_due_first"
    assert decision["replay_due_plan"] is True
    assert decision["publish_next_plan"] is False
    assert decision["preserve_existing_plan"] is True
    assert decision["action_order"] == [
        "replay_due_plan",
        "publish_next_plan_after_replay_success",
    ]

    replayed = coordinate_plan_rollover(
        as_of="2026-08-03",
        due_plan=due,
        next_plan=nxt,
        minute_evidence=_complete_minute_evidence(),
        replay_receipt=build_conditional_replay_receipt(
            execution_payload=_execution_payload(),
            apply_result=_apply_result(),
            persisted_state=_applied_state(),
            minute_evidence=_complete_minute_evidence(),
            captured_at="2026-08-03T18:12:00+08:00",
        ),
    )

    assert replayed["status"] == "ready_to_publish_next"
    assert replayed["replay_due_plan"] is False
    assert replayed["publish_next_plan"] is True
    assert replayed["preserve_existing_plan"] is False
    assert replayed["reason_codes"] == []


def test_rollover_allows_partial_minute_evidence_and_preserves_missing_reason() -> None:
    due = _formal_plan(
        plan_id="due-plan",
        plan_date="2026-07-31",
        trade_date="2026-08-03",
        symbols=("600000", "600001"),
    )
    nxt = _formal_plan(
        plan_id="next-plan",
        plan_date="2026-08-03",
        trade_date="2026-08-04",
    )
    minute = _partial_minute_evidence()

    decision = coordinate_plan_rollover(
        as_of="2026-08-03",
        due_plan=due,
        next_plan=nxt,
        minute_evidence=minute,
    )

    assert decision["status"] == "replay_due_first"
    assert decision["replay_due_plan"] is True
    assert decision["reason_codes"] == []
    assert decision["minute_evidence_summary"]["status"] == "partial"
    assert decision["minute_evidence_summary"]["non_available_items"] == [
        {
            "symbol": "600001",
            "status": "missing_intraday",
            "warnings": [],
            "errors": ["fixture minute file missing"],
        }
    ]

    execution = _execution_payload(symbols=("600000", "600001"))
    receipt = build_conditional_replay_receipt(
        execution_payload=execution,
        apply_result=_apply_result(),
        persisted_state=_applied_state(),
        minute_evidence=minute,
        captured_at="2026-08-03T18:12:00+08:00",
    )
    assert receipt["status"] == "passed"
    missing = next(
        row
        for row in receipt["minute_reconciliation"]
        if row["symbol"] == "600001"
    )
    assert missing["minute_status"] == "missing_intraday"
    assert missing["execution_bucket"] == "no_fill"
    assert missing["reason"]

    replayed = coordinate_plan_rollover(
        as_of="2026-08-03",
        due_plan=due,
        next_plan=nxt,
        minute_evidence=minute,
        replay_receipt=receipt,
    )
    assert replayed["status"] == "ready_to_publish_next"
    assert replayed["publish_next_plan"] is True


@pytest.mark.parametrize("mutation", ["count", "symbol", "status"])
def test_rollover_blocks_only_untrustworthy_partial_minute_evidence(
    mutation: str,
) -> None:
    due = _formal_plan(
        plan_id="due-plan",
        plan_date="2026-07-31",
        trade_date="2026-08-03",
        symbols=("600000", "600001"),
    )
    nxt = _formal_plan(
        plan_id="next-plan",
        plan_date="2026-08-03",
        trade_date="2026-08-04",
    )
    minute = _partial_minute_evidence()
    if mutation == "count":
        minute["missing_count"] = 0
    elif mutation == "symbol":
        minute["items"][1]["symbol"] = "600099"
    else:
        minute["items"][1]["status"] = "invented_status"

    decision = coordinate_plan_rollover(
        as_of="2026-08-03",
        due_plan=due,
        next_plan=nxt,
        minute_evidence=minute,
    )

    assert decision["status"] == "blocked"
    assert decision["replay_due_plan"] is False
    assert "due_minute_evidence_untrustworthy" in decision["reason_codes"]


def test_rollover_blocks_on_inconsistent_minute_evidence_without_losing_plan() -> None:
    due = _formal_plan(
        plan_id="due-plan",
        plan_date="2026-07-31",
        trade_date="2026-08-03",
    )
    nxt = _formal_plan(
        plan_id="next-plan",
        plan_date="2026-08-03",
        trade_date="2026-08-04",
    )
    minute = _complete_minute_evidence()
    minute["available_count"] = 0
    minute["stale_count"] = 1
    minute["items"][0].update(
        {"ok": False, "status": "stale_intraday", "target_date_rows": 0}
    )

    decision = coordinate_plan_rollover(
        as_of="2026-08-03",
        due_plan=due,
        next_plan=nxt,
        minute_evidence=minute,
    )

    assert decision["status"] == "blocked"
    assert decision["replay_due_plan"] is False
    assert decision["publish_next_plan"] is False
    assert decision["preserve_existing_plan"] is True
    assert "due_minute_evidence_untrustworthy" in decision["reason_codes"]


def test_rollover_replay_result_must_match_due_plan() -> None:
    due = _formal_plan(
        plan_id="due-plan",
        plan_date="2026-07-31",
        trade_date="2026-08-03",
    )
    nxt = _formal_plan(
        plan_id="next-plan",
        plan_date="2026-08-03",
        trade_date="2026-08-04",
    )

    receipt = build_conditional_replay_receipt(
        execution_payload=_execution_payload(plan_id="wrong-plan"),
        apply_result=_apply_result(plan_id="wrong-plan"),
        persisted_state=_applied_state(plan_id="wrong-plan"),
        minute_evidence=_complete_minute_evidence(plan_id="wrong-plan"),
        captured_at="2026-08-03T18:12:00+08:00",
    )
    decision = coordinate_plan_rollover(
        as_of="2026-08-03",
        due_plan=due,
        next_plan=nxt,
        minute_evidence=_complete_minute_evidence(),
        replay_receipt=receipt,
    )

    assert decision["status"] == "blocked"
    assert decision["publish_next_plan"] is False
    assert decision["preserve_existing_plan"] is True
    assert "due_replay_receipt_plan_id_mismatch" in decision["reason_codes"]


def test_rollover_rejects_hand_forged_legacy_replay_result() -> None:
    due = _formal_plan(
        plan_id="due-plan",
        plan_date="2026-07-31",
        trade_date="2026-08-03",
    )
    nxt = _formal_plan(
        plan_id="next-plan",
        plan_date="2026-08-03",
        trade_date="2026-08-04",
    )

    decision = coordinate_plan_rollover(
        as_of="2026-08-03",
        due_plan=due,
        next_plan=nxt,
        minute_evidence=_complete_minute_evidence(),
        replay_receipt={
            "ok": True,
            "status": "completed",
            "plan_id": "due-plan",
            "trade_date": "2026-08-03",
            "application_ok": True,
        },
    )

    assert decision["status"] == "blocked"
    assert "replay_receipt_task_type_invalid" in decision["reason_codes"]


def test_rollover_can_bootstrap_next_plan_only_when_explicitly_allowed() -> None:
    nxt = _formal_plan(
        plan_id="next-plan",
        plan_date="2026-08-03",
        trade_date="2026-08-04",
    )

    blocked = coordinate_plan_rollover(
        as_of="2026-08-03",
        due_plan=None,
        next_plan=nxt,
        minute_evidence=None,
    )
    allowed = coordinate_plan_rollover(
        as_of="2026-08-03",
        due_plan=None,
        next_plan=nxt,
        minute_evidence=None,
        due_plan_required=False,
    )

    assert blocked["status"] == "blocked"
    assert "due_plan_missing" in blocked["reason_codes"]
    assert allowed["status"] == "ready_to_publish_next"
    assert allowed["publish_next_plan"] is True
    assert allowed["bootstrap_without_due_plan"] is True
