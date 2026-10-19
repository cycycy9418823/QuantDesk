from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from quantdesk.project_status import health_freshness, read_project_status, read_runtime_observations
from scripts.ops.write_health_line import last_snapshot_complete, snapshot_cell, snapshot_quality


def _manifest(root: Path, *, fields: int | None = None, completed: int = 48) -> Path:
    day = root / "data/snapshots/2026-10-12"
    day.mkdir(parents=True, exist_ok=True)
    payload = {"trade_date": "2026-10-12", "collected_slot_count": 48,
        "completed_slot_count": completed, "partial_slot_count": 48-completed,
        "expected_slot_count": 48}
    for index in range(48):
        (day / f"{index:04d}.csv").write_text("symbol,price\n600000,10\n", encoding="utf-8")
    if fields is not None:
        payload["field_verified_complete_slot_count"] = fields
        payload["field_verified_complete_slots"] = [f"{index:04d}" for index in range(fields)]
        for slot in payload["field_verified_complete_slots"]:
            (day / f"{slot}.json").write_text(json.dumps({"trade_date": "2026-10-12", "slot_time": slot,
                "csv_sha256": hashlib.sha256((day / f"{slot}.csv").read_bytes()).hexdigest()}), encoding="utf-8")
    (day / "manifest.json").write_text(json.dumps(payload), encoding="utf-8")
    return day


def test_48_archived_and_48_coverage_does_not_claim_complete_quote_fields(tmp_path: Path) -> None:
    _manifest(tmp_path)
    text, reason = snapshot_cell(tmp_path, "2026-10-12")
    assert text == "快照 ✗48张/覆盖完成 48/字段核实 未知"
    assert "已归档 48 张" in reason
    assert last_snapshot_complete(tmp_path) == ""


def test_all_partial_is_not_complete_and_does_not_claim_transfer_failed(tmp_path: Path) -> None:
    _manifest(tmp_path, fields=0, completed=0)
    text, reason = snapshot_cell(tmp_path, "2026-10-12")
    assert "✗48张/覆盖完成 0/字段核实 0" in text
    assert "未拉回" not in reason


def test_full_quote_fields_require_paired_archive_hashes(tmp_path: Path) -> None:
    day = _manifest(tmp_path, fields=48)
    assert snapshot_quality(tmp_path, "2026-10-12")["archive_integrity_verified"] is True
    (day / "0000.csv").write_text("changed", encoding="utf-8")
    assert snapshot_quality(tmp_path, "2026-10-12")["archive_integrity_verified"] is False
    assert "✗48张" in snapshot_cell(tmp_path, "2026-10-12")[0]


def _calendar(root: Path) -> None:
    p = root / "data/calendar/a_share_trading_calendar.csv"
    p.parent.mkdir(parents=True)
    p.write_text("date,is_open\n2026-10-09,true\n2026-10-10,false\n2026-10-11,false\n2026-10-12,true\n2026-10-13,true\n", encoding="utf-8")


def _parsed(day: str, at: str) -> dict:
    return {"document_updated_at": at, "document_updater": "health_line",
            "health_line": "日线 ✓100/100", "health_explanation": f"健康业务日期 {day}"}


@pytest.mark.parametrize("now,day,generated,status,expected", [
    ("2026-10-12T22:29:00+08:00", "2026-10-09", "2026-10-09 22:30", "not_due", "2026-10-09"),
    ("2026-10-12T22:31:00+08:00", "2026-10-09", "2026-10-09 22:30", "stale", "2026-10-12"),
    ("2026-10-12T22:31:00+08:00", "2026-10-12", "2026-10-12 22:30", "current", "2026-10-12"),
    ("2026-10-13T08:00:00+08:00", "2026-10-12", "2026-10-12 22:30", "not_due", "2026-10-12"),
    ("2026-10-12T22:31:00+08:00", "2026-10-12", "2026-10-12 08:00", "stale", "2026-10-12"),
])
def test_health_freshness_uses_calendar_and_due_time(tmp_path: Path, now: str, day: str,
        generated: str, status: str, expected: str) -> None:
    _calendar(tmp_path)
    result = health_freshness(tmp_path, _parsed(day, generated), now)
    assert result["status"] == status
    assert result["expected_trade_date"] == expected
    assert bool(result["warning"]) == (status == "stale")


def test_unknown_calendar_future_and_manual_edit_are_never_green(tmp_path: Path) -> None:
    now = "2026-10-12T23:00:00+08:00"
    assert health_freshness(tmp_path, _parsed("2026-10-12", "2026-10-12 22:30"), now)["status"] == "unknown"
    _calendar(tmp_path)
    assert health_freshness(tmp_path, _parsed("2026-10-13", "2026-10-13 22:30"), now)["status"] == "unknown"
    record = _parsed("2026-10-12", "2026-10-12 22:30")
    record.update(document_updater="manual", health_explanation="")
    assert health_freshness(tmp_path, record, now)["status"] == "unknown"
    assert read_project_status(tmp_path)["health_freshness"]["status"] == "missing"


def test_new_ai_hold_only_status_preserves_legacy_failure_explanation(tmp_path: Path) -> None:
    path = tmp_path / "results/ai_trader/cycle_latest.json"
    path.parent.mkdir(parents=True)
    payload = {"as_of": "2026-10-12", "plan": {"decision_mode": "hold_only", "llm": {"used": False}}}
    path.write_text(json.dumps(payload), encoding="utf-8")
    result = read_runtime_observations(tmp_path)
    assert any("本轮无新增交易意图" in item["message"] for item in result["profiles"].values())
