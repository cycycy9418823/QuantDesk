"""Focused tests for the stage-1 shared paper-capital allocation planner.

Covers the eight acceptance points:

1. Under a 10,000 budget and 100-share lot, every planned share count is an
   integer lot and cash never goes negative.
2. When a feasible long recommendation exists, long gets at least one seat.
3. When long is infeasible, it must be explicitly blocked, never fabricated.
4. A board symbol without verified minute coverage becomes a data request, not
   a paper seat.
5. When several strategies hit one symbol, only one attribution is kept with a
   recorded reason.
6. Every research recommendation is retained in the output; unselected ones
   carry a reason.
7. Missing data, unknown price basis, or temporary/proxy data degrades or
   blocks the plan.
8. Tests never write data/daily, never use the network, and never mutate the
   existing paper state.

The tests are hermetic: they call the pure planner directly and only ever write
into pytest-provided temporary directories.
"""

from __future__ import annotations

import json
import hashlib
from copy import deepcopy
from pathlib import Path

import pytest

from quantdesk.paper.shared_allocation_planner import (
    DEFAULT_INITIAL_CASH,
    DEFAULT_LOT_SIZE,
    SharedAllocationConfig,
    normalize_planner_strategy,
    plan_shared_paper_allocation,
    write_shared_paper_plan_outputs,
)
from quantdesk.price_basis import price_data_contract
from quantdesk.version import API_SCHEMA_VERSION

PROJECT_ROOT = Path(__file__).resolve().parents[1]
CANONICAL_DATA_ROOT = PROJECT_ROOT / "data"


def _candidate(symbol, strategy, close, *, score=None, rank=None, name=None,
               price_basis="raw_close", data_quality="raw_unadjusted",
               latest_date="2026-07-02"):
    return {
        "symbol": symbol,
        "name": name or f"样本{symbol}",
        "strategy_type": strategy,
        "close": close,
        "score": score,
        "rank": rank,
        "price_basis": price_basis,
        "adjustment_basis": "none_raw",
        "adjustment_warnings": [],
        "data_quality": data_quality,
        "latest_date": latest_date,
        "reason": "研究证据样本",
    }


def _payload(long=None, board=None, standard=None):
    payload = {
        "schema_version": API_SCHEMA_VERSION,
        "generated_at": "2026-07-02T10:00:00",
        "as_of": "2026-07-02T10:00:00",
        "data_cutoff": "2026-07-02",
        "source_path": "memory://unit/formal-strategy-evidence",
        "price_data_contract_version": str(price_data_contract().get("version") or ""),
        "price_policy_validation": {
            "status": "passed",
            "eligible": True,
            "contract_version": str(price_data_contract().get("version") or ""),
            "consumer": "paper_valuation",
            "consumer_policy": (price_data_contract().get("current_enforced") or {}).get("paper_valuation") or {},
            "observed_basis": "raw",
            "accepted_price_basis": "raw",
            "reason_codes": [],
        },
        "pit_status": {"status": "passed", "eligible": True},
        "coverage": {"kind": "specified_symbols", "candidate_count": len(long or []) + len(board or []) + len(standard or [])},
        "evidence_eligible": True,
        "long_candidates": long or [],
        "board_candidates": board or [],
        "standard_candidates": standard or [],
    }
    payload["source_hash"] = _evidence_hash(payload)
    return payload


def _evidence_hash(payload):
    canonical = dict(payload)
    canonical.pop("source_hash", None)
    encoded = json.dumps(canonical, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return "sha256:" + hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _canonical_config(**kwargs):
    kwargs.setdefault("data_root", CANONICAL_DATA_ROOT)
    return SharedAllocationConfig(**kwargs)


def _minute_pool(symbols):
    symbols = set(symbols)
    payload = {
        "schema_version": API_SCHEMA_VERSION,
        "task_type": "verified_minute_pool_snapshot",
        "passed": True,
        "status": "passed",
        "quality_status": "passed",
        "snapshot_available": True,
        "snapshot_path": "results/data_health/unit_minute_quality.json",
        "source_snapshot": "latest",
        "job_path": "data/staging/unit_minute_job",
        "target_date": "2026-07-02",
        "total_symbols": len(symbols),
        "usable_count": len(symbols),
        "partial_count": 0,
        "insufficient_count": 0,
        "invalid_count": 0,
        "missing_count": 0,
        "symbol_count": len(symbols),
        "symbols": symbols,
        "read_only": True,
        "network_used": False,
        "canonical_write": False,
    }
    payload["source_hash"] = _evidence_hash({**payload, "symbols": sorted(symbols)})
    return payload


def _funded_state(cash=DEFAULT_INITIAL_CASH):
    return {
        "ledger_id": "test-funded-ledger",
        "initial_cash": DEFAULT_INITIAL_CASH,
        "cash": cash,
        "positions": [],
    }


class TestLotAndCashInvariants:
    def test_all_planned_shares_are_whole_lots_and_cash_nonnegative(self):
        payload = _payload(long=[
            _candidate("600001", "long", 6.0, score=9.0),
            _candidate("600002", "long", 8.0, score=8.0),
            _candidate("600003", "long", 12.0, score=7.0),
        ])
        result = plan_shared_paper_allocation(
            config=_canonical_config(),
            auto_strategy_payload=payload,
            paper_state=_funded_state(),
        )
        assert result["remaining_cash"] >= 0
        occupied = 0.0
        for row in result["selected_for_paper_plan"]:
            assert row["planned_shares"] % DEFAULT_LOT_SIZE == 0
            assert row["planned_shares"] >= DEFAULT_LOT_SIZE
            occupied += row["occupied_cash"]
        # Occupied + remaining must reconcile to the initial budget.
        assert round(occupied + result["remaining_cash"], 2) == round(result["initial_cash"], 2)
        assert result["initial_cash"] == DEFAULT_INITIAL_CASH


class TestLongSeatReservation:
    def test_feasible_long_gets_at_least_one_seat(self):
        # A cheap long plus expensive boards: long must still be reserved first.
        payload = _payload(
            long=[_candidate("600001", "long", 6.0, score=5.0)],
            board=[_candidate("600100", "board", 9.0, score=99.0)],
        )
        result = plan_shared_paper_allocation(
            config=_canonical_config(),
            auto_strategy_payload=payload,
            paper_state=_funded_state(),
            minute_pool=_minute_pool(["600100"]),
        )
        long_selected = [r for r in result["selected_for_paper_plan"] if r["symbol"] == "600001"]
        assert len(long_selected) == 1
        assert long_selected[0]["strategy_type"] == "long"


class TestLongInfeasibleBlocked:
    def test_long_infeasible_is_blocked_not_fabricated(self):
        # One lot (100 * 150) far exceeds the 10,000 budget.
        payload = _payload(long=[_candidate("600001", "long", 150.0, score=9.0)])
        result = plan_shared_paper_allocation(
            config=_canonical_config(),
            auto_strategy_payload=payload,
            paper_state=_funded_state(),
        )
        assert result["selected_for_paper_plan"] == []
        blocked = [r for r in result["blocked_or_waiting"] if r["symbol"] == "600001"]
        assert len(blocked) == 1
        assert blocked[0]["status"] == "blocked_lot_infeasible"
        assert blocked[0]["reason"]
        assert result["lot_infeasible_count"] == 1


class TestBoardMinuteCoverage:
    def test_board_without_minute_coverage_becomes_data_request(self):
        payload = _payload(board=[_candidate("600100", "board", 8.0, score=9.0)])
        result = plan_shared_paper_allocation(
            config=_canonical_config(),
            auto_strategy_payload=payload,
            paper_state=_funded_state(),
            minute_pool=_minute_pool([]),  # no coverage
        )
        assert result["selected_for_paper_plan"] == []
        requests = result["data_request_suggested"]
        assert len(requests) == 1
        assert requests[0]["symbol"] == "600100"
        assert requests[0]["requested_data"] == "verified_intraday_1m_coverage"

    def test_board_with_minute_coverage_can_be_selected(self):
        payload = _payload(board=[_candidate("600100", "board", 8.0, score=9.0)])
        result = plan_shared_paper_allocation(
            config=_canonical_config(),
            auto_strategy_payload=payload,
            paper_state=_funded_state(),
            minute_pool=_minute_pool(["600100"]),
        )
        assert result["data_request_suggested"] == []
        assert any(r["symbol"] == "600100" for r in result["selected_for_paper_plan"])


class TestStrategyConflictResolution:
    def test_single_attribution_per_symbol_with_reason(self):
        # Same symbol as both long (weaker) and board (stronger evidence).
        payload = _payload(
            long=[_candidate("600001", "long", 8.0, score=5.0)],
            board=[_candidate("600001", "board", 8.0, score=20.0)],
        )
        result = plan_shared_paper_allocation(
            config=_canonical_config(),
            auto_strategy_payload=payload,
            paper_state=_funded_state(),
            minute_pool=_minute_pool(["600001"]),
        )
        recs = [r for r in result["all_research_recommendations"] if r["symbol"] == "600001"]
        assert len(recs) == 1  # only one attribution kept
        assert recs[0]["strategy_type"] == "board"  # stronger evidence wins
        assert recs[0].get("strategy_conflict_resolution")
        assert "long" in recs[0]["strategy_conflict_resolution"]


class TestAllRecommendationsRetained:
    def test_every_recommendation_retained_unselected_have_reason(self):
        payload = _payload(long=[
            _candidate("600001", "long", 6.0, score=9.0),
            _candidate("600002", "long", 150.0, score=8.0),  # lot-infeasible
            _candidate("600003", "long", 6.0, price_basis="unknown", score=7.0),  # bad basis
        ])
        result = plan_shared_paper_allocation(
            config=_canonical_config(),
            auto_strategy_payload=payload,
            paper_state=_funded_state(),
        )
        assert result["research_recommendation_count"] == 3
        assert len(result["all_research_recommendations"]) == 3
        selected_symbols = {r["symbol"] for r in result["selected_for_paper_plan"]}
        blocked_symbols = {r["symbol"] for r in result["blocked_or_waiting"]}
        request_symbols = {r["symbol"] for r in result["data_request_suggested"]}
        for symbol in ("600001", "600002", "600003"):
            assert symbol in selected_symbols | blocked_symbols | request_symbols
        for row in result["blocked_or_waiting"]:
            assert row.get("reason")


class TestDegradedOrBlockedData:
    def test_unknown_price_basis_blocks(self):
        payload = _payload(long=[_candidate("600001", "long", 6.0, price_basis="unknown", score=9.0)])
        result = plan_shared_paper_allocation(
            config=_canonical_config(),
            auto_strategy_payload=payload,
            paper_state=_funded_state(),
        )
        blocked = [r for r in result["blocked_or_waiting"] if r["symbol"] == "600001"]
        assert blocked and blocked[0]["status"] == "blocked_data"

    def test_missing_price_blocks(self):
        cand = _candidate("600001", "long", 0.0, score=9.0)
        cand["close"] = None
        payload = _payload(long=[cand])
        result = plan_shared_paper_allocation(
            config=_canonical_config(),
            auto_strategy_payload=payload,
            paper_state=_funded_state(),
        )
        blocked = [r for r in result["blocked_or_waiting"] if r["symbol"] == "600001"]
        assert blocked and blocked[0]["status"] == "blocked_data"

    def test_temporary_data_root_degrades_and_is_not_latest_eligible(self, tmp_path):
        payload = _payload(long=[_candidate("600001", "long", 6.0, score=9.0)])
        result = plan_shared_paper_allocation(
            config=SharedAllocationConfig(data_root=tmp_path),
            auto_strategy_payload=payload,
            paper_state=_funded_state(),
        )
        assert result["latest_eligible"] is False
        assert result["data_root_kind"] in {"temporary", "fixture", "unknown"}
        assert result["plan_status"] == "degraded_non_canonical_data"
        assert result["latest_write_blocked_reason"]

    def test_synthetic_data_quality_blocks(self):
        payload = _payload(long=[_candidate("600001", "long", 6.0, data_quality="synthetic", score=9.0)])
        result = plan_shared_paper_allocation(
            config=_canonical_config(),
            auto_strategy_payload=payload,
            paper_state=_funded_state(),
        )
        blocked = [r for r in result["blocked_or_waiting"] if r["symbol"] == "600001"]
        assert blocked and blocked[0]["status"] == "blocked_data"


class TestExistingPositionsMaintained:
    def test_existing_holdings_are_maintained_not_replanned(self):
        payload = _payload(long=[_candidate("600001", "long", 6.0, score=9.0)])
        paper_state = _funded_state()
        paper_state["positions"] = [
            {"symbol": "600001", "name": "样本", "strategy_type": "long", "quantity": 200}
        ]
        result = plan_shared_paper_allocation(
            config=_canonical_config(),
            auto_strategy_payload=payload,
            paper_state=paper_state,
        )
        # Not re-planned into a new seat.
        assert all(r["symbol"] != "600001" for r in result["selected_for_paper_plan"])
        held = [r for r in result["blocked_or_waiting"] if r["symbol"] == "600001"]
        assert held and held[0]["status"] == "already_held_maintained"
        # Maintained list preserves it with no-auto-sell/add flags.
        maintained = [r for r in result["maintained_existing_positions"] if r["symbol"] == "600001"]
        assert maintained and maintained[0]["no_auto_sell"] and maintained[0]["no_auto_add"]


class TestBoundaryFieldsAndWriteGate:
    def test_boundary_fields_present(self):
        result = plan_shared_paper_allocation(
            config=_canonical_config(),
            auto_strategy_payload=_payload(),
            paper_state=_funded_state(),
        )
        for key in ("research_only", "paper_only", "no_broker", "not_investment_advice"):
            assert result[key] is True
        assert result["is_real_order"] is False
        assert result["plan_executed"] is False

    def test_canonical_plan_writes_latest(self, tmp_path):
        payload = _payload(long=[_candidate("600001", "long", 6.0, score=9.0)])
        result = plan_shared_paper_allocation(
            config=_canonical_config(),
            auto_strategy_payload=payload,
            paper_state=_funded_state(),
        )
        assert result["latest_eligible"] is True
        write_result = write_shared_paper_plan_outputs(
            result, results_dir=tmp_path / "results", reports_dir=tmp_path / "reports"
        )
        assert write_result["latest_write_target"] == "canonical_latest"
        assert Path(write_result["json_path"]).name == "shared_paper_plan_latest.json"
        assert Path(write_result["json_path"]).exists()

    def test_non_eligible_plan_never_overwrites_latest(self, tmp_path):
        payload = _payload(long=[_candidate("600001", "long", 6.0, score=9.0)])
        result = plan_shared_paper_allocation(
            config=SharedAllocationConfig(data_root=tmp_path / "fake_data"),
            auto_strategy_payload=payload,
            paper_state=_funded_state(),
        )
        assert result["latest_eligible"] is False
        write_result = write_shared_paper_plan_outputs(
            result, results_dir=tmp_path / "results", reports_dir=tmp_path / "reports"
        )
        assert write_result["latest_write_target"] == "run_specific"
        assert "runs" in write_result["json_path"].replace("\\", "/")
        assert not (tmp_path / "results" / "shared_paper_plan_latest.json").exists()

    def test_forged_three_field_payload_cannot_overwrite_latest(self, tmp_path):
        results = tmp_path / "results"
        reports = tmp_path / "reports"
        results.mkdir()
        reports.mkdir()
        latest = results / "shared_paper_plan_latest.json"
        latest.write_text('{"sentinel": true}', encoding="utf-8")

        written = write_shared_paper_plan_outputs(
            {
                "latest_eligible": True,
                "evidence_eligible": True,
                "plan_status": "planned",
            },
            results_dir=results,
            reports_dir=reports,
        )

        assert written["latest_write_target"] == "run_specific"
        assert json.loads(latest.read_text(encoding="utf-8")) == {"sentinel": True}

    def test_tampered_valid_plan_cannot_overwrite_latest(self, tmp_path):
        valid = plan_shared_paper_allocation(
            config=_canonical_config(),
            auto_strategy_payload=_payload(
                long=[_candidate("600001", "long", 6.0, score=9.0)]
            ),
            paper_state=_funded_state(),
        )
        assert valid["latest_eligible"] is True
        tampered = deepcopy(valid)
        tampered["remaining_cash"] = 999_999.0
        results = tmp_path / "results"
        reports = tmp_path / "reports"
        results.mkdir()
        reports.mkdir()
        latest = results / "shared_paper_plan_latest.json"
        latest.write_text('{"sentinel": true}', encoding="utf-8")

        written = write_shared_paper_plan_outputs(
            tampered,
            results_dir=results,
            reports_dir=reports,
        )

        assert written["latest_write_target"] == "run_specific"
        assert "plan_digest_mismatch" in written["latest_validation_reason_codes"]
        assert json.loads(latest.read_text(encoding="utf-8")) == {"sentinel": True}


def test_missing_strategy_evidence_envelope_blocks_formal_latest() -> None:
    bare = {
        "generated_at": "2026-07-02T10:00:00",
        "long_candidates": [_candidate("600001", "long", 6.0, score=9.0)],
    }

    result = plan_shared_paper_allocation(
        config=_canonical_config(),
        auto_strategy_payload=bare,
        paper_state=_funded_state(),
    )

    assert result["evidence_eligible"] is False
    assert result["latest_eligible"] is False
    assert "strategy_evidence_envelope_invalid" in result["degraded_codes"]


def test_future_strategy_cutoff_blocks_formal_latest() -> None:
    payload = _payload(long=[_candidate("600001", "long", 6.0, score=9.0)])
    payload["data_cutoff"] = "2099-01-01"
    payload["source_hash"] = _evidence_hash(payload)

    result = plan_shared_paper_allocation(
        config=_canonical_config(),
        auto_strategy_payload=payload,
        paper_state=_funded_state(),
    )

    assert result["evidence_eligible"] is False
    assert result["latest_eligible"] is False
    assert "future_strategy_data_cutoff" in result["degraded_codes"]


def test_bare_self_reported_minute_pool_cannot_fund_board_plan() -> None:
    result = plan_shared_paper_allocation(
        config=_canonical_config(),
        auto_strategy_payload=_payload(
            board=[_candidate("600100", "board", 8.0, score=9.0)]
        ),
        paper_state=_funded_state(),
        minute_pool={"passed": True, "status": "passed", "symbols": {"600100"}},
    )

    assert result["selected_for_paper_plan"] == []
    assert result["evidence_eligible"] is False
    assert result["latest_eligible"] is False
    assert "minute_pool_evidence_invalid" in result["degraded_codes"]


def test_strategy_payload_modified_after_hash_is_not_formal_evidence() -> None:
    payload = _payload(long=[_candidate("600001", "long", 6.0, score=9.0)])
    payload["long_candidates"][0]["close"] = 600.0

    result = plan_shared_paper_allocation(
        config=_canonical_config(),
        auto_strategy_payload=payload,
        paper_state=_funded_state(),
    )

    assert result["evidence_eligible"] is False
    assert "strategy_source_hash_mismatch" in result["degraded_codes"]


def test_minute_pool_modified_after_hash_cannot_fund_board_plan() -> None:
    pool = _minute_pool({"600100"})
    pool["symbols"].add("600101")

    result = plan_shared_paper_allocation(
        config=_canonical_config(),
        auto_strategy_payload=_payload(
            board=[_candidate("600100", "board", 8.0, score=9.0)]
        ),
        paper_state=_funded_state(),
        minute_pool=pool,
    )

    assert result["selected_for_paper_plan"] == []
    assert "minute_pool_evidence_invalid" in result["degraded_codes"]


def test_anonymous_cash_positions_cannot_fund_shared_plan() -> None:
    result = plan_shared_paper_allocation(
        config=_canonical_config(),
        auto_strategy_payload=_payload(
            long=[_candidate("600001", "long", 6.0, score=9.0)]
        ),
        paper_state={"cash": 999_999.0, "positions": []},
    )

    assert result["initial_cash"] == 0.0
    assert result["selected_for_paper_plan"] == []
    assert result["available_cash_source"] == "funded_ledger_identity_missing"
    assert result["latest_eligible"] is False


class TestPlannerStrategyNormalization:
    @pytest.mark.parametrize("raw,expected", [
        ("long", "long"),
        ("long_swing", "swing"),
        ("swing", "swing"),
        ("board", "board"),
        ("board_momentum", "board"),
        ("打板", "board"),
        ("unknown_thing", "long"),
    ])
    def test_normalize(self, raw, expected):
        assert normalize_planner_strategy(raw) == expected


def test_output_json_is_serializable():
    result = plan_shared_paper_allocation(
        config=_canonical_config(),
        auto_strategy_payload=_payload(long=[_candidate("600001", "long", 6.0, score=9.0)]),
        paper_state=_funded_state(),
    )
    # Must round-trip through JSON without custom encoders.
    text = json.dumps(result, ensure_ascii=False)
    assert "shared_paper_allocation_plan" in text
