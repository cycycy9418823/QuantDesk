"""B1: expiry-reviewed / intraday-simulated / applied-to-ledger are separate facts.

Only a bound, verified receipt proves review or replay. Insufficient evidence
records a performed review while retaining the unresolved gap.
"""

from __future__ import annotations

import json
from pathlib import Path

from quantdesk.daily_candidate_refresh import scan_unreplayed_archived_plans
from quantdesk.plan_review_receipts import bind_plan_replay_receipt
from tests.test_plan_review_receipts import archive, make_plan, review, v1_replay, write_minutes


def _archive(root: Path, trade_date: str, plan_id: str, *, symbols: int = 2) -> None:
    plan = make_plan(trade_date, plan_id, symbols)
    for i, row in enumerate(plan["symbols"]):
        row["symbol"] = f"{600000 + int(trade_date[-2:]) * 10 + i:06d}"
    archive(root, plan)


def _receipt(root: Path, trade_date: str, *, replay: bool = False, complete: bool = True) -> dict:
    plan = json.loads((root / "results/conditional_trade_plans_daily" / f"{trade_date}.json").read_text(encoding="utf-8"))
    if complete:
        write_minutes(root, plan)
    if replay:
        return bind_plan_replay_receipt(root, plan, v1_replay(plan), checked_at=f"{trade_date}T18:10:00+08:00")
    return review(root, plan)


_PLAN_ID = "plan_x"
_TRADE_DATE = "2026-08-03"
_EXECUTION_ID = f"{_PLAN_ID}:{_TRADE_DATE}"


def test_read_only_review_receipt_is_not_a_gap(tmp_path: Path) -> None:
    _archive(tmp_path, _TRADE_DATE, _PLAN_ID)
    result = scan_unreplayed_archived_plans(
        tmp_path,
        as_of="2026-08-06",
        applied_execution_ids=[],
        review_receipts=[_receipt(tmp_path, _TRADE_DATE)],
    )
    assert result["status"] == "passed"
    assert result["unreplayed_count"] == 0
    assert result["evidence_counts"]["expiry_reviewed"] == 1


def test_intraday_simulation_receipt_is_not_a_gap(tmp_path: Path) -> None:
    _archive(tmp_path, _TRADE_DATE, _PLAN_ID)
    result = scan_unreplayed_archived_plans(
        tmp_path,
        as_of="2026-08-06",
        applied_execution_ids=[],
        replay_receipts=[_receipt(tmp_path, _TRADE_DATE, replay=True)],
    )
    assert result["status"] == "passed"
    assert result["unreplayed_count"] == 0
    assert result["evidence_counts"]["intraday_simulation_executed"] == 1


def test_applied_execution_is_not_a_gap(tmp_path: Path) -> None:
    _archive(tmp_path, _TRADE_DATE, _PLAN_ID)
    result = scan_unreplayed_archived_plans(
        tmp_path, as_of="2026-08-06", applied_execution_ids=[_EXECUTION_ID]
    )
    assert result["status"] == "passed"
    assert result["evidence_counts"]["applied_to_ledger"] == 1


def test_failed_review_receipt_does_not_cover_the_plan(tmp_path: Path) -> None:
    _archive(tmp_path, _TRADE_DATE, _PLAN_ID)
    result = scan_unreplayed_archived_plans(
        tmp_path,
        as_of="2026-08-06",
        applied_execution_ids=[],
        review_receipts=[
            _receipt(tmp_path, _TRADE_DATE, complete=False)
        ],
    )
    assert result["status"] == "attention"
    assert result["unreplayed_count"] == 1
    row = result["unreplayed"][0]
    assert row["status"] == "evidence_insufficient"
    assert row["evidence"] == {
        "applied_to_ledger": False,
        "intraday_simulation_executed": False,
        "expiry_reviewed": True,
    }
    assert any("minute_file_missing" in reason for reason in row["missing_evidence_reasons"])


def test_no_evidence_at_all_is_a_gap(tmp_path: Path) -> None:
    _archive(tmp_path, _TRADE_DATE, _PLAN_ID)
    result = scan_unreplayed_archived_plans(tmp_path, as_of="2026-08-06")
    assert result["unreplayed_count"] == 1
    assert set(result["evidence_counts"]) == {
        "applied_to_ledger",
        "intraday_simulation_executed",
        "expiry_reviewed",
    }


def test_mixed_evidence_counts_each_layer(tmp_path: Path) -> None:
    _archive(tmp_path, "2026-07-30", "plan_applied")
    _archive(tmp_path, "2026-07-31", "plan_simulated")
    _archive(tmp_path, "2026-08-03", "plan_reviewed")
    _archive(tmp_path, "2026-08-04", "plan_gap", symbols=3)
    result = scan_unreplayed_archived_plans(
        tmp_path,
        as_of="2026-08-06",
        applied_execution_ids=["plan_applied:2026-07-30"],
        replay_receipts=[_receipt(tmp_path, "2026-07-31", replay=True)],
        review_receipts=[_receipt(tmp_path, "2026-08-03")],
    )
    assert result["evidence_counts"] == {
        "applied_to_ledger": 1,
        "intraday_simulation_executed": 1,
        "expiry_reviewed": 1,
    }
    assert result["unreplayed_count"] == 1
    assert result["unreplayed"][0]["plan_id"] == "plan_gap"


def test_scan_is_idempotent_and_writes_nothing(tmp_path: Path) -> None:
    _archive(tmp_path, _TRADE_DATE, _PLAN_ID)
    before = sorted(p.relative_to(tmp_path).as_posix() for p in tmp_path.rglob("*"))
    first = scan_unreplayed_archived_plans(tmp_path, as_of="2026-08-06")
    second = scan_unreplayed_archived_plans(tmp_path, as_of="2026-08-06")
    after = sorted(p.relative_to(tmp_path).as_posix() for p in tmp_path.rglob("*"))
    assert before == after
    assert first == second
    assert first["recovery_authorized"] is False
