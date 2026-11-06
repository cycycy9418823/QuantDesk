"""R4: the reconciliation's resume fingerprint must track the evidence it consumes.

The D1 repair made the total reconciliation read this run's verified no-session
evidence from the chain's published pointer
(``results/data_chain/daily_data_chain_latest.json``). A resume decision that
ignores that pointer would reuse the old gap verdict on the same day - exactly
the two-verdict problem D1 set out to remove.

Temp trees only. No real ``data/``, ``results/``, checkpoint, task or network
access; the pipeline is never executed.
"""

from __future__ import annotations

import csv
import json
from pathlib import Path

from scripts import run_data_close_pipeline as pipeline

TARGET = "2026-10-14"
SYMBOL = "000016"
RUN_ID = "daily-data-chain-20261014T103846Z-91d929b826"
POINTER_RELATIVE = Path("results/data_chain/daily_data_chain_latest.json")


def _session_evidence(symbol: str) -> dict:
    return {
        "schema_version": "baostock_session_evidence.v1",
        "symbol": symbol,
        "provider_code": f"sz.{symbol}",
        "source": "baostock.query_history_k_data_plus",
        "adjustflag": "3",
        "query_start_date": TARGET,
        "query_end_date": TARGET,
        "provider_error_code": "0",
        "captured_at": "2026-10-14T18:45:10",
        "records": [{"trade_date": TARGET, "tradestatus": "0"}],
        "records_sha256": "2278a657cb300ed6e34af963d51793db9ede5303bfc43a3aa59c10495b3523fd",
    }


def _root(tmp_path: Path) -> Path:
    root = tmp_path / "project"
    (root / "data" / "universe").mkdir(parents=True, exist_ok=True)
    with (root / "data" / "universe" / "a_share_symbols.csv").open(
        "w", encoding="utf-8", newline=""
    ) as handle:
        writer = csv.writer(handle)
        writer.writerow(["symbol", "name"])
        writer.writerow([SYMBOL, SYMBOL])
    return root


def _write_pointer(root: Path, session_evidence: dict) -> Path:
    path = root / POINTER_RELATIVE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "schema_version": "daily_data_chain.v1",
                "run_id": RUN_ID,
                "target_date": TARGET,
                "authoritative": True,
                "status": "completed",
                "components": {
                    "target_day_gap_recovery": {
                        "schema_version": "target_day_gap_recovery.v1",
                        "target_date": TARGET,
                        "status": "partial",
                        "appended_symbols": [],
                        "verified_no_session_symbols": sorted(session_evidence),
                        "session_evidence": session_evidence,
                    }
                },
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return path


def test_reconcile_input_manifest_tracks_the_consumed_chain_evidence(tmp_path: Path) -> None:
    root = _root(tmp_path)
    _write_pointer(root, {})
    before = pipeline._input_manifest_for("reconcile", root, TARGET)

    _write_pointer(root, {SYMBOL: _session_evidence(SYMBOL)})
    after = pipeline._input_manifest_for("reconcile", root, TARGET)

    assert before != after


def test_reconcile_stage_version_moved_past_the_pre_repair_revision() -> None:
    assert pipeline._stage_version("reconcile") == f"{pipeline.SCHEMA_VERSION}:reconcile:4"
    assert pipeline._stage_version("reconcile") != f"{pipeline.SCHEMA_VERSION}:reconcile:3"
    # The other stages keep their own revisions.
    assert pipeline._stage_version("research_chain") == f"{pipeline.SCHEMA_VERSION}:research_chain:4"


def test_completed_reconcile_is_not_reused_when_only_the_evidence_changes(tmp_path: Path) -> None:
    """Old reconcile done -> only the upstream evidence changes -> must re-run."""
    root = _root(tmp_path)
    _write_pointer(root, {})
    run_id = "resume-fixture"
    stages = {
        name: {
            "status": "completed",
            "stage_version": pipeline._stage_version(name),
            "input_manifest": pipeline._input_manifest_for(name, root, TARGET),
            "output_manifest": "outputs_unbound",
        }
        for name in pipeline.STAGE_ORDER
    }
    checkpoint = root / pipeline.CHECKPOINT_DIR_RELATIVE / run_id / "checkpoint.json"
    checkpoint.parent.mkdir(parents=True, exist_ok=True)
    checkpoint.write_text(
        json.dumps(
            {
                "schema_version": pipeline.SCHEMA_VERSION,
                "run_id": run_id,
                "target_date": TARGET,
                "config_hash": "fixture",
                "stages": stages,
            }
        ),
        encoding="utf-8",
    )

    _write_pointer(root, {SYMBOL: _session_evidence(SYMBOL)})

    loaded = pipeline.load_checkpoint(root, run_id, target=TARGET, config_hash="fixture")

    assert loaded is not None
    assert loaded["stages"]["reconcile"]["status"] == "input_changed_refusing_resume"
