from __future__ import annotations

from quantdesk.data_chain_consumer_gate import evaluate_data_chain_consumer_gate


def _chain(status: str = "completed") -> dict:
    return {
        "exists": True,
        "status": status,
        "authoritative": status == "completed",
        "published_latest": status == "completed",
        "run_id": "run-gate",
        "target_date": "2026-07-16",
        "protected_state_unchanged": status == "completed",
        "universe": {"current_analysis_pool": {"sha256": "u" * 64}},
        "coverage": {
            "daily": {"status": "passed"},
            "current_analysis_pool": {"status": "passed"},
            "features": {"status": "passed"},
        },
    }


def test_consumer_gate_allows_only_completed_authoritative_chain() -> None:
    gate = evaluate_data_chain_consumer_gate(
        _chain(),
        target_date="2026-07-16",
        required_universe_sha256="u" * 64,
    )
    assert gate["status"] == "passed"
    assert gate["allowed"] is True
    assert gate["reasons"] == []


def test_consumer_gate_blocks_running_partial_and_digest_mismatch() -> None:
    for status in ("running", "partial", "failed", "abandoned"):
        gate = evaluate_data_chain_consumer_gate(_chain(status))
        assert gate["status"] == "blocked"
        assert gate["allowed"] is False
        assert any(reason.startswith("daily_chain_status_") for reason in gate["reasons"])

    mismatch = evaluate_data_chain_consumer_gate(
        _chain(),
        required_universe_sha256="x" * 64,
    )
    assert mismatch["status"] == "blocked"
    assert "daily_chain_universe_digest_mismatch" in mismatch["reasons"]
