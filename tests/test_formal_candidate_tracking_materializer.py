from __future__ import annotations

import json
from pathlib import Path

import pytest

from quantdesk import daily_candidate_refresh as refresh
from quantdesk.daily_candidate_refresh import (
    build_formal_candidate_batch,
    publish_formal_candidate_batch,
)
from quantdesk.formal_candidate_tracking import (
    materialize_formal_candidate_tracking,
)

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
                    symbol: {
                        "eligible": True,
                        "price_basis": "raw",
                        "latest_date": target_date,
                    }
                    for symbol in symbols
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
                "counts": {"expected_trade": 2, "unknown": 0},
                "unknown_symbols": [],
            },
            "critical_sources": {
                "status": "passed",
                "formal_eligible": True,
                "failed_sources": [],
            },
        },
    }


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


def _publish_batch(root: Path, target_date: str = "2026-08-03") -> dict:
    batch = build_formal_candidate_batch(
        data_chain_manifest=_passed_manifest(target_date),
        strategy_payload=_strategy_payload(target_date),
        captured_at=f"{target_date}T18:02:03+08:00",
    )
    result = publish_formal_candidate_batch(root, batch)
    assert result["applied"] is True
    return batch


def _write_daily_history(
    root: Path,
    *,
    symbol: str = "600000",
    rows: list[tuple[str, float]],
) -> None:
    path = root / "data" / "daily" / f"{symbol}.csv"
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = ["date,symbol,open,high,low,close,volume,amount,adjust"]
    for day, close in rows:
        lines.append(f"{day},{symbol},{close},{close},{close},{close},100,1000,raw")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def test_materializer_reads_only_verified_dated_batch_and_writes_formal_output(
    tmp_path: Path,
) -> None:
    batch = _publish_batch(tmp_path)
    _write_daily_history(
        tmp_path,
        rows=[
            ("2026-08-03", 10.0),
            ("2026-08-04", 11.0),
            ("2026-08-05", 12.0),
            ("2026-08-06", 13.0),
        ],
    )

    payload = materialize_formal_candidate_tracking(
        tmp_path,
        target_date="2026-08-03",
        authoritative_chain_run_id=batch["provenance"]["source_run_id"],
    )

    assert payload["status"] == "completed_with_warnings"
    assert payload["batch_provenance_hash"] == batch["provenance"]["hash"]
    assert payload["authoritative_chain_run_id"] == batch["provenance"]["source_run_id"]
    assert payload["entries"][0]["horizons"]["1d"] == {
        "return": pytest.approx(0.1),
        "status": "mature",
        "as_of": "2026-08-04",
    }
    assert payload["entries"][0]["horizons"]["5d"]["return"] is None
    assert payload["entries"][0]["horizons"]["5d"]["status"] == "pending"
    assert (
        tmp_path / "results" / "candidate_tracking" / "daily" / "2026-08-03.json"
    ).is_file()
    assert (tmp_path / "results" / "candidate_tracking" / "latest.json").is_file()


def test_materializer_never_falls_back_to_latest_or_legacy_csv(tmp_path: Path) -> None:
    legacy = tmp_path / "results" / "daily_research" / "v420_daily_stock_candidates_latest.csv"
    legacy.parent.mkdir(parents=True, exist_ok=True)
    legacy.write_text("symbol,signal_date\n600000,2026-08-03\n", encoding="utf-8")
    latest = tmp_path / "results" / "candidate_refresh" / "formal_candidate_batch_latest.json"
    latest.parent.mkdir(parents=True, exist_ok=True)
    latest.write_text(json.dumps({"target_date": "2026-08-03"}), encoding="utf-8")

    payload = materialize_formal_candidate_tracking(
        tmp_path,
        target_date="2026-08-03",
        authoritative_chain_run_id="daily-data-chain-20260803T074500Z-fixture",
    )

    assert payload["status"] == "blocked"
    assert "formal_candidate_batch_missing" in payload["reason_codes"]
    assert not (tmp_path / "results" / "candidate_tracking").exists()


def test_materializer_blocks_source_run_mismatch_without_writing(tmp_path: Path) -> None:
    _publish_batch(tmp_path)

    payload = materialize_formal_candidate_tracking(
        tmp_path,
        target_date="2026-08-03",
        authoritative_chain_run_id="daily-data-chain-foreign-run",
    )

    assert payload["status"] == "blocked"
    assert "formal_candidate_batch_source_run_mismatch" in payload["reason_codes"]
    assert not (tmp_path / "results" / "candidate_tracking").exists()


def test_materializer_is_idempotent_and_rejects_same_date_conflict(tmp_path: Path) -> None:
    batch = _publish_batch(tmp_path)
    _write_daily_history(tmp_path, rows=[("2026-08-03", 10.0), ("2026-08-04", 11.0)])
    first = materialize_formal_candidate_tracking(
        tmp_path,
        target_date="2026-08-03",
        authoritative_chain_run_id=batch["provenance"]["source_run_id"],
    )
    second = materialize_formal_candidate_tracking(
        tmp_path,
        target_date="2026-08-03",
        authoritative_chain_run_id=batch["provenance"]["source_run_id"],
    )
    assert first["artifact_hash"] == second["artifact_hash"]
    assert second["publish_status"] == "idempotent"

    dated = tmp_path / "results" / "candidate_tracking" / "daily" / "2026-08-03.json"
    conflicting = json.loads(dated.read_text(encoding="utf-8"))
    conflicting["entries"][0]["name"] = "tampered"
    dated.write_text(json.dumps(conflicting), encoding="utf-8")
    conflict = materialize_formal_candidate_tracking(
        tmp_path,
        target_date="2026-08-03",
        authoritative_chain_run_id=batch["provenance"]["source_run_id"],
    )
    assert conflict["status"] == "blocked"
    assert "candidate_tracking_dated_snapshot_conflict" in conflict["reason_codes"]


def test_materializer_blocks_duplicate_local_daily_dates_without_silent_dedup(
    tmp_path: Path,
) -> None:
    batch = _publish_batch(tmp_path)
    _write_daily_history(
        tmp_path,
        rows=[
            ("2026-08-03", 10.0),
            ("2026-08-04", 11.0),
            ("2026-08-04", 12.0),
            ("2026-08-05", 13.0),
        ],
    )

    payload = materialize_formal_candidate_tracking(
        tmp_path,
        target_date="2026-08-03",
        authoritative_chain_run_id=batch["provenance"]["source_run_id"],
    )

    entry = payload["entries"][0]
    assert entry["sample_status"] == "blocked"
    assert entry["horizons"]["1d"] == {
        "return": None,
        "status": "blocked",
        "as_of": None,
    }
    assert "daily_history_duplicate_date:600000:2026-08-04" in entry["warnings"]
